"""Explorer findings: signature, dedupe across nights, the candidate flow, its lint and its single replay (2.9, 4.7,
4.13 finding.explorer, 9.4).

- A finding carries its signature (surface, invariant or observation, crop hash), severity, confidence, raw values,
  trace reference, build, and first and last seen (`ExplorerCandidate`). The same signature on a later night updates
  last seen instead of opening a new finding (`prior_findings`, `dedupe`, `mark_seen_again`).
- Each finding is emitted as a candidate flow `flows/quarantine/<finding-id>.yaml`, class `explorer_candidate`, with
  `origin: {kind: explorer, run_id, finding_ref}` (L-FLW-14), a budget inside 400 steps, 5,400 s and
  `policy/explorer_budget.yaml` (L-FLW-18), cut from the trace since the last named stable state and anchored by the
  click crops (`candidate_flow`). A candidate that does not lint is not replayed (`replayed: lint_failed`).
- The candidate is replayed exactly once in scripted mode on the same build (`ReplayRunner`). Only a reproducing
  finding is reported, through the replayed flow's own checkpoint outcome (`replay_outcome`): `invariant_violation`
  or `assertion_failed` when the finding is one of those, else `needs_baseline (reason: explorer_replay)`, never
  `needs_human`. A finding that does not reproduce is held with triage state `agent_mistake` or
  `expected_behavior` and listed under "Explorer: not reproduced" (`hold_state`). No automatic trimming: an owner may
  enable a bounded mode of at most 8 replays for one finding (`bounded_trim`).

Choices (reported):
1. The signature key is the sha256 of the canonical JSON of the three 4.13 signature fields; within one session the
   same signature is one finding (the highest confidence kept, the first trace position).
2. Last seen lives in the finding's evidence (`last_seen_run_id`, `last_seen_at`): 8.3 `findings` has no column.
3. The candidate's steps start after the last trace action, up to the finding, whose post-action screen matched a
   named stable state (from the start of the session without one); `zoom` is read-only and leaves no step. Each step
   targets the registry control the Explorer named and carries its click crop in `note`; a step on an OCR word that
   is no registry control cannot be written (4.7 targets are `<surface>.<control>`) and fails the candidate's lint.
   A `type_fixture` step types into the control clicked before it; `shift_tab` is a Shift+Tab `key_chord`.
4. The candidate's checkpoint is `data_mode: frozen, regions: default` unless the caller names a regions file: its
   replay has no base to diff (`needs_baseline`), and a human writes the regions when promoting it. Its flow has
   `agent_fallback: disabled` (a replay is scripted), the budget its steps need inside L-FLW-18, and the working
   orders check before and after that order flows carry (L-FLW-12), whatever surface the finding is on.
5. A finding that does not reproduce is held as `agent_mistake` when a post-action verification failed in its trace
   slice (an `agent_error`), else `expected_behavior`; triage may change it (8.3, 9.6).
6. A reproduction is the replay's checkpoint showing the finding's observation: the same invariant id, the same
   assertion id, or (for any other observation) the observation the replay's harness reports.
7. `bounded_trim` drops one step at a time from the start of the trace and keeps a drop that still reproduces,
   stopping after `max_replays` (at most 8) replays.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Protocol

import yaml

from .agent.session import RawFinding, TraceAction
from .canonical import canonical_json, sha256_id
from .lint.repo import Doc, Repo, load_yaml_text
from .orchestrator.explorer import ExplorerBudget
from .orchestrator.results import Outcome
from .records import ExplorerFinding, Finding

QUARANTINE_DIR = "flows/quarantine"
FLOW_STEP_CAP, FLOW_DURATION_CAP_S = 400, 5400        # L-FLW-18
MAX_TRIM_REPLAYS = 8                                  # 2.9: a bounded mode of at most 8 replays
CHECKPOINT_STEP = "finding"
NOT_REPRODUCED = "Explorer: not reproduced"
_INV = re.compile(r"^INV-\d\d$")
PRESS_KEY = {"page_up": "pageup", "page_down": "pagedown"}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s) or "finding"


# -- the finding -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ExplorerCandidate:
    """One Explorer finding before it is stored (module docstring)."""

    surface_id: str
    observation: str
    crop_sha256: str
    severity: str
    confidence: float
    raw_values: Mapping[str, str | None]
    trace_ref: str
    build_id: str
    seq: int
    session_id: str

    @property
    def signature(self) -> dict[str, str]:
        """The 4.13 signature: surface, invariant or observation, crop hash."""
        return {"surface_id": self.surface_id, "observation": self.observation, "crop_sha256": self.crop_sha256}

    @property
    def key(self) -> str:
        return signature_key(self.signature)

    @property
    def kind(self) -> str:
        """`invariant` (an INV id), `assertion` (an `assertion:<id>` observation) or `observation`."""
        if _INV.match(self.observation):
            return "invariant"
        return "assertion" if self.observation.startswith("assertion:") else "observation"


def signature_key(signature: Mapping[str, str]) -> str:
    """The dedupe key of a signature (choice 1)."""
    return sha256_id({k: signature[k] for k in ("surface_id", "observation", "crop_sha256")})


def candidates(raw: Iterable[RawFinding], *, session_id: str, trace_ref: str, build_id: str) -> list[ExplorerCandidate]:
    """One candidate per signature seen in the session (choice 1)."""
    best: dict[str, ExplorerCandidate] = {}
    for r in raw:
        o = r.observation
        c = ExplorerCandidate(o.surface_id, o.observation, o.crop_sha256, o.severity, o.confidence,
                              dict(o.raw_values), trace_ref, build_id, r.seq, session_id)
        old = best.get(c.key)
        if old is None or c.confidence > old.confidence:
            best[c.key] = c if old is None else replace(c, seq=min(c.seq, old.seq))
    return sorted(best.values(), key=lambda c: c.seq)


@dataclass(frozen=True)
class PriorFinding:
    """An Explorer finding of an earlier night: its id, signature key, first and last seen runs."""

    finding_id: str
    key: str
    first_seen_run_id: str
    last_seen_run_id: str


def prior_findings(store: Any) -> dict[str, PriorFinding]:
    """Every stored Explorer finding by signature key, the latest when one repeats (choice 2)."""
    out: dict[str, PriorFinding] = {}
    for r in store.query("SELECT finding_id, run_id, first_seen_run_id, evidence_json, explorer_json FROM findings "
                         "WHERE kind = 'explorer_candidate' AND explorer_json IS NOT NULL ORDER BY finding_id"):
        sig = json.loads(r["explorer_json"]).get("signature")
        if not isinstance(sig, Mapping):
            continue
        ev = json.loads(r["evidence_json"] or "{}")
        key = signature_key(sig)
        out[key] = PriorFinding(str(r["finding_id"]), key, r["first_seen_run_id"] or r["run_id"],
                                ev.get("last_seen_run_id") or r["run_id"])
    return out


def dedupe(found: Sequence[ExplorerCandidate], prior: Mapping[str, PriorFinding]
           ) -> tuple[list[ExplorerCandidate], list[tuple[PriorFinding, ExplorerCandidate]]]:
    """Split tonight's candidates into new findings and signatures seen on an earlier night."""
    new, again = [], []
    for c in found:
        (again.append((prior[c.key], c)) if c.key in prior else new.append(c))
    return new, again


def mark_seen_again(store: Any, prior: PriorFinding, run_id: str, at: str) -> None:
    """Update an earlier finding's last seen to this run (choice 2) instead of opening a new finding."""
    rows = store.query("SELECT evidence_json FROM findings WHERE finding_id = ?", (int(prior.finding_id),))
    ev = json.loads(rows[0]["evidence_json"] or "{}") if rows else {}
    ev.update(last_seen_run_id=run_id, last_seen_at=at)
    store.update("findings", {"finding_id": int(prior.finding_id)}, {"evidence_json": canonical_json(ev)})


def to_finding(c: ExplorerCandidate, *, finding_id: str, run_id: str, replayed: str, candidate_flow: str | None,
               held: bool = False, triage_state: str = "open", first_seen_run_id: str | None = None,
               trace: Sequence[TraceAction] = ()) -> Finding:
    """The `explorer_candidate` finding record (4.13 finding.explorer)."""
    seqs = [t.seq for t in trace if t.seq <= c.seq] or [c.seq]
    evidence: dict[str, Any] = {"raw_values": dict(c.raw_values), "step_events": {"first_seq": min(seqs),
                                                                                    "last_seq": c.seq},
                                "artifacts": [c.crop_sha256], "build_id": c.build_id, "last_seen_run_id": run_id,
                                "candidate_flow": candidate_flow}
    return Finding(finding_id=finding_id, run_id=run_id, kind="explorer_candidate",
                   description=f"Explorer: {c.observation} on {c.surface_id}", first_seen_run_id=first_seen_run_id
                   or run_id, severity=None, evidence=evidence, held=held, triage_state=triage_state,
                   explorer=ExplorerFinding(c.session_id, c.signature, c.severity, c.confidence, c.trace_ref,
                                            replayed, candidate_flow))


# -- the candidate flow ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FlowContext:
    """What the candidate's header needs: owner, persona, the Explorer workspace and fixture, the tier, the budget
    policy, and the checkpoint's data mode and regions (choice 4)."""

    owner: str
    persona: str
    workspace: str
    fixture: str
    tier: int = 2
    budget: ExplorerBudget = ExplorerBudget()
    data_mode: str = "frozen"
    regions: str = "default"


@dataclass(frozen=True)
class CandidateFlow:
    """A candidate flow: its id, repository path, document, the trace actions it was cut from and the steps that
    could not be written (choice 3)."""

    flow_id: str
    path: str
    doc: Mapping[str, Any]
    actions: tuple[TraceAction, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def checkpoint_id(self) -> str:
        return f"flow/{self.doc['surface']}/{self.flow_id}/{CHECKPOINT_STEP}"

    def yaml_text(self) -> str:
        return yaml.safe_dump(dict(self.doc), sort_keys=False, allow_unicode=True)


def trace_slice(trace: Sequence[TraceAction], seq: int) -> list[TraceAction]:
    """The actions since the last named stable state before the finding (choice 3)."""
    upto = [t for t in trace if t.seq <= seq]
    before = [i for i, t in enumerate(upto[:-1]) if t.stable_state]
    return upto[before[-1] + 1 if before else 0:]


def _step(i: int, t: TraceAction, controls: frozenset[str], last_click: str | None
          ) -> tuple[dict[str, Any] | None, str | None]:
    """One trace action as a 4.7 step, or the problem that prevents it."""
    sid = f"e{i:02d}"
    note = f"explorer seq {t.seq}" + (f"; click crop {t.click_crop_sha256}" if t.click_crop_sha256 else "")
    names = list(t.names)
    bad = [n for n in names if n not in controls]
    if bad:
        return None, f"explorer seq {t.seq} targets {', '.join(bad)}, which is no registry control (4.7)"
    if t.tool == "click_named":
        return {"id": sid, "type": "click_named", "target": names[0], "note": note}, None
    if t.tool == "scroll":
        return {"id": sid, "type": "wheel", "at": names[0], "notches": int(t.args["notches"]), "note": note}, None
    if t.tool == "drag_named":
        return {"id": sid, "type": "drag", "from": names[0], "to": names[1], "steps": 10, "hold_ms": 500,
                "note": note}, None
    if t.tool == "press":
        key = str(t.args["key"])
        if key == "shift_tab":
            return {"id": sid, "type": "key_chord", "keys": ["shift", "tab"], "note": note}, None
        return {"id": sid, "type": "press", "key": PRESS_KEY.get(key, key), "note": note}, None
    if t.tool == "type_fixture":
        if last_click is None:
            return None, f"explorer seq {t.seq} types a fixture value with no control clicked before it"
        return {"id": sid, "type": "type_fixture", "target": last_click, "value_ref": str(t.args["fixture_id"]),
                "commit": "none", "note": note}, None
    return None, None                                                   # zoom: read-only, no step


def candidate_flow(finding_ref: str, run_id: str, c: ExplorerCandidate, trace: Sequence[TraceAction],
                   ctx: FlowContext, controls: Iterable[str]) -> CandidateFlow:
    """The candidate flow of one finding (module docstring, choices 3 and 4)."""
    known = frozenset(controls)
    flow_id = f"flow.explorer.{_slug(finding_ref)}"
    actions = trace_slice(trace, c.seq)
    steps: list[dict[str, Any]] = [{"id": "e00", "type": "reset_input_state"}]
    problems: list[str] = []
    last_click: str | None = None
    for i, t in enumerate(actions, 1):
        step, problem = _step(i, t, known, last_click)
        if problem:
            problems.append(problem)
        if step:
            steps.append(step)
            if step["type"] == "click_named":
                last_click = step["target"]
    steps.append({"id": CHECKPOINT_STEP, "type": "checkpoint",
                  "checkpoint_id": f"flow/{c.surface_id}/{flow_id}/{CHECKPOINT_STEP}", "surface_id": c.surface_id,
                  "target_window": {"kind": "main"}, "data_mode": ctx.data_mode, "regions": ctx.regions,
                  "note": f"Explorer finding {c.observation}; crop {c.crop_sha256}"})
    teardown = [{"id": "t01", "type": "teardown", "action": "working_orders_check", "expect": 0},
                {"id": "t02", "type": "reset_input_state"}]
    cap_steps = min(FLOW_STEP_CAP, ctx.budget.max_actions)
    cap_s = min(FLOW_DURATION_CAP_S, int(ctx.budget.max_minutes * 60))
    total = len(steps) + len(teardown) + 2
    doc = {"schema_version": 2, "flow_id": flow_id,
           "title": f"Explorer candidate: {c.observation} on {c.surface_id}", "class": "explorer_candidate",
           "tier": ctx.tier, "owner": ctx.owner, "persona": ctx.persona, "surface": c.surface_id,
           "surfaces": [c.surface_id], "requires": [], "workspace": ctx.workspace, "fixture": ctx.fixture,
           "data_mode": ctx.data_mode, "agent_fallback": "disabled",
           "budget": {"max_duration_s": min(cap_s, max(60, 30 * total)), "max_steps": min(cap_steps, total)},
           "origin": {"kind": "explorer", "run_id": run_id, "finding_ref": finding_ref}, "normalize": [],
           "preconditions": [{"id": "p1", "type": "precondition", "check": "paper_mode"},
                             {"id": "p2", "type": "precondition", "check": "no_modal"},
                             {"id": "p3", "type": "precondition", "check": "working_orders", "expect": 0}],
           "steps": steps, "teardown": teardown}
    return CandidateFlow(flow_id, f"{QUARANTINE_DIR}/{finding_ref}.yaml", doc, tuple(actions), tuple(problems))


def lint_candidate(repo: Repo, flow: CandidateFlow, *, phase: int = 0, today: date | None = None) -> list[str]:
    """The candidate's lint errors against the config repository (its file added in memory), plus the steps that
    could not be written (choice 3); empty when it lints clean."""
    from .lint.rules import lint_repo                 # lazy: the lint rules import the oracle and the flow checks

    trial = Repo(repo.root, dict(repo.docs))
    trial.docs[flow.path] = Doc(flow.path, "flow", load_yaml_text(flow.yaml_text()))
    report = lint_repo(trial, phase, today)
    return list(flow.problems) + [v.line() for v in report.errors if v.file == flow.path]


# -- the single replay -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayResult:
    """What the single scripted replay of a candidate showed on its checkpoint (choice 6)."""

    invariant_ids: tuple[str, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    capture_sha256: str | None = None
    step_failed: str | None = None

    def reproduces(self, c: ExplorerCandidate) -> bool:
        if c.kind == "invariant":
            return c.observation in self.invariant_ids
        if c.kind == "assertion":
            return c.observation.removeprefix("assertion:") in self.assertion_ids
        return c.observation in self.observations


class ReplayRunner(Protocol):
    """Runs a candidate flow once in scripted mode on the same build within the RC run (2.9)."""

    def replay(self, flow: CandidateFlow) -> ReplayResult:
        """One replay."""


def replay_outcome(c: ExplorerCandidate, result: ReplayResult) -> Outcome | None:
    """The replayed checkpoint's outcome for a reproducing finding (2.9); None when it did not reproduce."""
    if not result.reproduces(c):
        return None
    if c.kind == "invariant":
        return Outcome("invariant_violation", c.observation, detail="Explorer finding reproduced on replay")
    if c.kind == "assertion":
        return Outcome("assertion_failed", c.observation, detail="Explorer finding reproduced on replay")
    return Outcome("needs_baseline", "explorer_replay", detail=f"{c.observation}: a new flow has no base capture; "
                                                                "the Explorer trace and candidate flow are attached")


def hold_state(c: ExplorerCandidate, trace: Sequence[TraceAction]) -> str:
    """The triage state a non-reproducing finding is held with (choice 5)."""
    return "agent_mistake" if any(t.agent_error for t in trace_slice(trace, c.seq)) else "expected_behavior"


def bounded_trim(flow: CandidateFlow, c: ExplorerCandidate, runner: ReplayRunner, rebuild: Callable[
        [Sequence[TraceAction]], CandidateFlow], *, max_replays: int = MAX_TRIM_REPLAYS) -> tuple[CandidateFlow, int]:
    """The owner-enabled bounded mode (2.9, choice 7): the shortest candidate found within `max_replays` replays
    that still reproduces, and the replays spent. `rebuild` turns a trace slice back into a candidate."""
    if not 1 <= max_replays <= MAX_TRIM_REPLAYS:
        raise ValueError(f"the bounded mode allows 1 to {MAX_TRIM_REPLAYS} replays (2.9)")
    kept = list(flow.actions)
    best = flow
    spent = 0
    i = 0
    while i < len(kept) and spent < max_replays:
        trial_actions = kept[:i] + kept[i + 1:]
        trial = rebuild(trial_actions)
        spent += 1
        if runner.replay(trial).reproduces(c):
            kept, best = trial_actions, trial
        else:
            i += 1
    return best, spent


@dataclass
class ReplayedFinding:
    """One finding after the candidate, the lint and the replay: what the report and the store need."""

    candidate: ExplorerCandidate
    flow: CandidateFlow
    replayed: str                                  # reproduced | not_reproduced | lint_failed
    outcome: Outcome | None = None
    lint_errors: tuple[str, ...] = ()
    hold: str | None = None
    seen_before: PriorFinding | None = None
    result: ReplayResult | None = None


def process(found: Sequence[ExplorerCandidate], trace: Sequence[TraceAction], *, run_id: str, ctx: FlowContext,
            controls: Iterable[str], runner: ReplayRunner, lint: Callable[[CandidateFlow], list[str]],
            prior: Mapping[str, PriorFinding] | None = None, ref: Callable[[ExplorerCandidate], str] | None = None
            ) -> list[ReplayedFinding]:
    """Every finding of the session through the candidate, its lint and its single replay (module docstring). A
    signature seen on an earlier night is replayed like a new one and keeps its `seen_before`."""
    prior = prior or {}
    ref = ref or (lambda c: f"exp-{run_id}-{c.seq:04d}")
    ctrl = frozenset(controls)
    out = []
    for c in found:
        flow = candidate_flow(_slug(ref(c)), run_id, c, trace, ctx, ctrl)
        errors = tuple(lint(flow))
        if errors:
            out.append(ReplayedFinding(c, flow, "lint_failed", lint_errors=errors, seen_before=prior.get(c.key)))
            continue
        res = runner.replay(flow)
        o = replay_outcome(c, res)
        out.append(ReplayedFinding(c, flow, "reproduced" if o else "not_reproduced", o,
                                   hold=None if o else hold_state(c, trace), seen_before=prior.get(c.key), result=res))
    return out
