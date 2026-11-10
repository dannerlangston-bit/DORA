"""One simulated Sentinel run, end to end: `run_pipeline` (a PR run, 1.7) and `run_nightly` (10.3).

`run_pipeline(config_dir, scenario, out_dir, today=...)` follows the PR run of 1.7 and the
checkpoint sequence of 5.17 with the rig replaced by `sim`:

1. T+0:00. The config the run pins (the given directory plus the PR's manifest and build records,
   4.3) is linted; any error is preflight item G1 (12.7): the shard's Orchestrator stops at
   `provisioned` and the run ends `aborted_harness (reason: config_invalid)` with the abort block in
   the PR comment. The build pair is read from the candidate's build record.
2. T+0:01 to 0:02. The manifest for the candidate is loaded (or not: the two no-manifest cases),
   the changed paths go through the static impact step and `scope.compute_scope` plans the run.
3. Before any threshold is used (3.9, 1.6 Calibration run) an A/A calibration of the base build is
   run with `diff.calibration` over every planned checkpoint: five captures in one session and one
   in a fresh session; the same A/A pairs drive the invariant enablement test of 2.7.
4. Per planned checkpoint (5.17): both builds are captured, `diff.settle` gates each capture, the
   Diff Engine compares them against the calibrated floors, the Invariant Checker runs on both
   builds, targeted re-runs decide stability (3.10, re-run budget max(30, 10 percent)), decisions
   are looked up by signature in the store (9.7), and `classify.classify` gives the outcome.
5. The run-level global-shift pre-pass of 3.7 (granularity 0) over every diffed pair; when it
   is raised its checkpoints become one `needs_human (reason: global_shift)` cluster.
6. Reviewer pass (3.12, 3.15). The checkpoints are clustered exactly as the report will show them
   (`report.cluster.cluster_members`, 9.7); one simulated Reviewer call per cluster that needs one
   (task A for declared surfaces, task B descriptions for `collateral`; never for invariants or
   assertions, never for a member that inherited a decision), up to the PR cap of 60 calls.
7. Everything goes through `store.Store` (single writer) and `store.ArtifactStore` (content
   addressed, redaction header set: synthetic screens carry no account data). The coverage matrix
   (`coverage.build_matrix`) and the Reporter's PR comment, commit status and static HTML report
   are rendered from what the store returns.

`run_nightly` is the canary pass of a Nightly run (10.3): `canary.generate` mutates approved
captures of the base build, every mutant goes through the same `evaluate` path as a PR checkpoint
with `synthetic: true`, behavioral canaries run their canary flow on the simulated app, and
`canary.score` gives the result rows, the detector state, the synthetic `harness_error` rows and
the morning banner.

The Orchestrator (5.2): every shard goes through `orchestrator.Orchestrator` on a simulated rig
(`pipeline.orchestration`): provisioned, launched, authenticated, the paper-mode assertion against
the example allowlist, settings attestation, workspace, fixture verification and the canary flow, then
`running`, where the Executor (`_ShardWork`) re-reads the build id before each flow, writes a heartbeat
at every step, sends each input-bearing flow step through the gateway (`Rig.send_input`, the drop
ledger of 5.4.8), runs the Watchdog's hang detector, and captures the checkpoints of every shard in
`running` in plan order; then each shard's teardown and post-run proof. A control result that stops
a shard gives its unreached checkpoints the Orchestrator's exact outcome (1.10); the run outcome is the
worst of the shards' (8.2). The A/A calibration runs once a shard reaches `running`, so a run whose
shards all stop at a gate captures nothing. Every gateway drop or refusal is a `step_events` row in the store with
its `gateway_event` (8.3 `gate_decision`, `gate_reason`, `gate_class`), and the Safety row counts those rows. Every
control result's 9.10 route goes to `alerts.json`; the shards, their gates and proofs (`Proof.line`, 6.6), the
gateway events and the actions the Orchestrator does not perform itself go to `orchestrator.json`.

The Oracle (2.8, `pipeline.assertions`): every flow checkpoint that declares assertions is evaluated
on both builds; the candidate's verdict feeds `classify.Facts` (3.11 G and H) and the failed
assertion's raw values of both builds go to `Finding.evidence.raw_values` and the finding text.

Choices (reported):

- The Nightly canary pass writes one synthetic outcome row per checkpoint at most: 8.3 keys
  `outcomes` by (run, checkpoint), so the per-mutant result rows go to `canary-results.json` (and the
  artifact store) and only the synthetic `harness_error` rows `canary.score` returns are written to
  `outcomes`. The Nightly pass runs without the Oracle: a mutant changes pixels, and the simulated
  Oracle reads what synth drew, not pixels.
- A Reviewer the run cannot reach for an item (`reviewer.ReviewerUnavailable`, the live adapter) gives
  that item's classifier input R from the failed verdict (3.11 row 23); the run goes on.
"""
from __future__ import annotations

import dataclasses
from collections import abc
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import cv2
import numpy as np

from .. import canary as cn
from .. import invariants as inv
from ..classify import (REVIEWER_ROWS, Classified, Condition, DecisionMatch, Facts, HarnessFailure, Review, classify)
from ..coverage import Profile, build_matrix
from ..diff import load_params
from ..diff.calibration import CalibrationFile, Capture as CalCapture, calibrate_checkpoint
from ..diff.engine import (DiffContext, DiffResult, compare_fingerprints, diff_pair, fingerprint_id,
                           global_shift_prepass)
from ..diff.phash import phash
from ..diff.regions import RegionsFile, effective_registry, park_rect
from ..diff.settle import Recapture, RerunBudget, SettleResult, diff_stability, settle
from ..lint import lint
from ..records import PERSONAS, CheckpointOutcome, Decision, Finding, RunOutcome, Signature, StepOutcome
from ..report import ReportContext, build_report, render_pr_comment, render_run_report
from ..report.cluster import Member, cluster_members
from ..report.model import worst_proof
from ..scope import (DependencyIndex, Manifest, PlannedCheckpoint, Rig, ScopeConfigError, ScopePlan, classes_in,
                     compute_scope)
from ..store import ArtifactStore, Store
from ..store.artifacts import ManifestHeader, Redaction, StoredArtifact
from ..store.store import RunPlan, ScopeEntry, ShardPlan
from ..taxonomy import checkpoint_parts, load
from ..oracle.assertions import AssertionResult, step_assertions
from ..oracle.tables import OracleConfigError
from ..orchestrator.budgets import SESSION_RELEASE_WAIT_S
from ..orchestrator.clock import ManualClock
from ..orchestrator.gates import check_authenticated, check_build_unchanged, check_launched
from ..orchestrator.gateway import VK_ESCAPE, VK_RETURN, VK_SPACE, VK_TAB, Action, DropLedger, drain, refusal
from ..orchestrator.identity import judge_identity
from ..orchestrator.machine import Orchestrator, RunContext, RunReport
from ..orchestrator.results import ControlResult, Outcome, RunVerdict, worse
from ..orchestrator.rig import CrashSignal
from ..orchestrator.simrig import CONFIRM_POINT, DELETE_POINT, SimScenario, SimulatedRig, default_context
from ..orchestrator.watchdog import (HANG_FLOOR_S, Heartbeat, HeartbeatUnwritable, HeartbeatWriter, app_failure,
                                     edt_frozen, heartbeat_failed)
from .assertions import FlowOracle, OracleCheck
from .config import SentinelConfig, as_dict, materialize, parse_time, tree_sha256
from ..reviewer.adapter import ModelReviewer, ReviewerUnavailable
from ..reviewer.config import ReviewerConfig
from ..reviewer.job import ReviewerJob
from .live import CollectingReviewer, ItemBuilder
from .orchestration import RigScript, ShardSink, alert_entry, persisted_actions, shard_setup
from .reviewer import ReviewerAdapter, ReviewRequest, SimulatedReviewer, change_types, to_review, validate
from .sim import Capture, SimApp, SimBuild, observe

DEFAULT_TODAY = date(2026, 11, 10)
RIGS = ("rig-01", "rig-02", "rig-03", "rig-04")
REDACTION = Redaction("redaction-v4-sim")
PR_REVIEWER_CAP = 60                     # 3.15 per-run call cap of a PR run
GATES_S, TEARDOWN_S = 9 * 60, 6 * 60     # 1.7: gates T+0:03 to 0:09; re-runs, teardown and report
LISTED = ("invariant_violation", "assertion_failed", "collateral", "needs_human", "intended", "landmark_missing",
          "needs_baseline", "harness_error")
FINGERPRINT = {"os_build": "10.0.22631.4460", "display": "1920x1080@100", "theme": "dark",
               "font_set": "sentinel-pinned-2", "jre": "17.0.11+9", "gpu_driver": "basic-display-10.0",
               "render_probe_hash": "8f3c2a1b4d5e6f70", "bounds_source": "B"}


@dataclass(frozen=True)
class Scenario:
    """What the build pipeline's webhook delivers for one run (1.7 T+0:00), plus the simulated builds."""

    name: str
    run_id: str
    candidate: SimBuild
    base: SimBuild
    run_type: str = "pr"
    pr: int | None = None
    author: str = "jdoe"
    overlay: Mapping[str, str] = field(default_factory=dict)       # config files the PR or an edit adds
    changed_paths: tuple[str, ...] = ()
    depends_on: Mapping[str, tuple[str, ...]] | None = None        # the candidate's dependency index
    clock: str = "10:31:00"                                         # run start, America/New_York
    reviewer_cap: int = PR_REVIEWER_CAP
    canary: Mapping[str, Any] | None = None                         # last Nightly's state for run health
    detector_banner: str | None = None
    history: tuple[dict, ...] = ()
    rig: RigScript = RigScript()                                    # what the simulated rig does (pipeline.orchestration)


@dataclass
class Evaluated:
    """One checkpoint after capture, diff, invariants, re-runs and the decision lookup."""

    cp: PlannedCheckpoint
    base: Capture | None
    cand: Capture
    registry: RegionsFile
    facts: Facts
    classified: Classified
    diff: DiffResult | None = None
    comparison: inv.Comparison | None = None
    region: str | None = None
    phash: str | None = None
    crop: tuple[int, int, int, int] | None = None
    recaptures: int = 0
    related: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    answer: dict | None = None
    review: Review | None = None
    settle_ms: int | None = None
    oracle: OracleCheck | None = None              # the candidate's assertion results (flow checkpoints)
    oracle_base: OracleCheck | None = None         # the same assertions on the base build

    @property
    def category(self) -> str:
        """The computed category before any decision changed the outcome (9.7 signature)."""
        return self.facts_category or self.classified.outcome

    facts_category: str | None = None


@dataclass
class RunResult:
    """What one run produced, for the demo, the tests and summary.txt."""

    run_id: str
    run_type: str
    status: str
    reason: str | None
    out_dir: Path
    outcomes: list[CheckpointOutcome] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    comment: str = ""
    commit_status: dict = field(default_factory=dict)
    plan: ScopePlan | None = None
    manifest: dict | None = None
    reviewer_calls: list[dict] = field(default_factory=list)
    disabled_invariants: dict[str, list[str]] = field(default_factory=dict)
    lint_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    canary: Any = None
    alerts: list[dict] = field(default_factory=list)
    shards: list[dict] = field(default_factory=list)
    proof: str | None = None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            if not o.synthetic:
                out[o.outcome] = out.get(o.outcome, 0) + 1
        return out


# ------------------------------------------------------------------------------------------------
# Small helpers


def _ts(today: date, clock: str, plus_s: int = 0) -> str:
    """An ISO time in America/New_York (EST in November) for this run's clock."""
    d = datetime.fromisoformat(f"{today.isoformat()}T{clock}-05:00") + timedelta(seconds=plus_s)
    return d.isoformat()


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def _clip(box: tuple[int, int, int, int], w: int, h: int, pad: int = 0) -> tuple[int, int, int, int]:
    x, y, bw, bh = box
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def _crop(img: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = box
    return img[y:y + h, x:x + w]


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _fingerprint(epoch: str) -> tuple[dict, str]:
    match = dict(FINGERPRINT, data_epoch_id=epoch)
    return match, fingerprint_id(match)


def _flow_class(cfg: SentinelConfig, cp: PlannedCheckpoint) -> str | None:
    if cp.test_kind != "flow":
        return None
    return str(cfg.flow_docs.get(cp.flow_id or "", {}).get("class", "standard"))


def _recapturable(cfg: SentinelConfig, cp: PlannedCheckpoint) -> bool:
    if cp.test_kind != "flow":
        return True
    return bool(cfg.flow_step(cp.flow_id or "", cp.step_id or "").get("recapturable", True))


def _host(cp: PlannedCheckpoint) -> str | None:
    step = dict(cp.size_step or {})
    return step.get("host") or step.get("splitter")


# ------------------------------------------------------------------------------------------------
# The context of one run

# Calibrations already measured in this process, by fingerprint, base build, checkpoint and registry:
# a fingerprint is calibrated once and reused until it changes (3.9, 7.13), so a second run of the same
# base build in one process reads the floors instead of re-measuring them.
_CALIBRATIONS: dict[tuple, Any] = {}


def _registry_key(reg: RegionsFile) -> str:
    parts = [f"{lm.id}:{hashlib.sha256(lm.template.tobytes()).hexdigest()[:16]}:{lm.expected}" for lm in reg.landmarks]
    parts += [f"{r.id}:{r.type}:{r.rect}:{r.money_cell}:{r.mask_ref}" for r in reg.regions]
    return "|".join(parts)


class _Run:
    """Shared state of one run: config, apps, calibration, rules, budget, store handles."""

    def __init__(self, cfg: SentinelConfig, run_id: str, run_type: str, base: SimBuild, cand: SimBuild,
                 now: datetime, manifest: Manifest | None, plan: ScopePlan | None, x_input: Callable[[str], str]):
        self.cfg, self.run_id, self.run_type = cfg, run_id, run_type
        self.base_app, self.cand_app = SimApp(base), SimApp(cand)
        self.now, self.manifest, self.plan, self.x_input = now, manifest, plan, x_input
        self.params = load_params()
        epoch = str(((cfg.fixture.get("epoch") or {}).get("epoch_id")) or "epoch-unknown")
        self.fp_match, self.fp_id = _fingerprint(epoch)
        self.epoch = epoch
        self.calibration: CalibrationFile | None = None
        self.rules: dict[str, inv.SurfaceRules] = {}
        self.disabled: dict[str, list[str]] = {}
        self.registries: dict[str, RegionsFile] = {}
        self.expiry_notices: list = []
        self.budget: RerunBudget | None = None
        self.entries = frozenset(e.entry_id for e in manifest.entries) if manifest else frozenset()
        self.tickets = frozenset(t for e in manifest.entries for t in e.tickets) if manifest else frozenset()

    # -- registry and rules ----------------------------------------------------------------
    def registry(self, cp: PlannedCheckpoint, base: Capture) -> RegionsFile:
        if cp.checkpoint_id not in self.registries:
            doc = self.cfg.regions_doc(cp.checkpoint_id, cp.surface_id, cp.test_kind, _host(cp))
            reg = SimApp.registry(cp, base.screen, base.info, doc)
            reg, notices = effective_registry(reg, self.cfg.masks, self.now)
            self.expiry_notices += notices
            self.registries[cp.checkpoint_id] = reg
        return self.registries[cp.checkpoint_id]

    def surface_rules(self, cp: PlannedCheckpoint) -> inv.SurfaceRules:
        key = f"{cp.surface_id}|{cp.data_mode}"
        if key not in self.rules:
            self.rules[key] = self.cfg.rules(cp.surface_id, cp.data_mode)
        return self.rules[key]

    def capture(self, cp: PlannedCheckpoint, side: str, tick: int = 0) -> Capture:
        app = self.base_app if side == "base" else self.cand_app
        return app.capture(cp, side, controls=self.cfg.control_ids(cp.surface_id), tick=tick)

    # -- 3.9 A/A calibration and the 2.7 enablement test -------------------------------------
    def calibrate(self, planned: Sequence[PlannedCheckpoint], out_dir: Path) -> None:
        """The Calibration run of 1.6 on the base build, before any threshold is used."""
        cal = CalibrationFile.load(out_dir / "calibration", self.fp_id) or CalibrationFile(self.fp_id, f"cal-{self.run_id}")
        pairs: dict[str, list[inv.AAPair]] = {}
        for cp in planned:
            caps = [self.capture(cp, "base", tick=10 + k) for k in range(6)]
            reg = self.registry(cp, caps[0])
            if cp.checkpoint_id not in cal.checkpoints:     # a fingerprint is calibrated once (3.9, 7.13)
                key = (self.fp_id, self.base_app.build, cp.checkpoint_id, cp.data_mode, _registry_key(reg))
                if key not in _CALIBRATIONS:
                    _CALIBRATIONS[key] = calibrate_checkpoint(
                        [CalCapture(c.screen.image, time_to_settle_ms=3 * 250) for c in caps], reg,
                        data_mode=cp.data_mode, system_masks=[park_rect(_park(caps[0]))], now=self.now,
                        params=self.params)
                cal.checkpoints[cp.checkpoint_id] = _CALIBRATIONS[key]
            pairs.setdefault(f"{cp.surface_id}|{cp.data_mode}", []).append(
                inv.AAPair(cp.data_mode, caps[0].models, caps[5].models))
        for key, ps in sorted(pairs.items()):
            surface, mode = key.split("|")
            rules = self.cfg.rules(surface, mode)
            statuses = inv.enablement(ps, rules)
            self.rules[key] = inv.with_enablement(rules, statuses)
            off = [i for i, s in statuses.items() if s != inv.ENABLED]
            if off:
                self.disabled[surface] = off
        self.calibration = cal
        cal.save(out_dir / "calibration")


# ------------------------------------------------------------------------------------------------
# One checkpoint (5.17): settle, diff, invariants, re-runs, decisions, classification


def _park(cap: Capture) -> tuple[int, int]:
    """The declared parking coordinate (3.5): a dead area 8 px inside the capture's bottom-right corner."""
    return cap.screen.image.shape[1] - 8, cap.screen.image.shape[0] - 8


def _region_choice(ev_reg: RegionsFile, res: DiffResult | None, comparison: inv.Comparison | None,
                   cand_lm: Mapping[str, tuple[int, int]], size: tuple[int, int]) -> tuple[str | None, tuple | None]:
    """The signature region (9.7) and its candidate bounds: the declared region the finding lands in,
    else the 8x8 grid cell (relative to the landmark) holding the largest component."""
    W, H = size
    located = {}
    for r in ev_reg.regions:
        if r.pixel_diffed and r.landmark in cand_lm:
            lx, ly = cand_lm[r.landmark]
            located[r.id] = (r.rect.x + lx, r.rect.y + ly, r.rect.w, r.rect.h)
    if comparison is not None and comparison.fired:
        crop = comparison.reported[0].violation.crop
        best, area = None, 0
        for rid, (x, y, w, h) in located.items():
            ox = min(x + w, crop.right) - max(x, crop.x)
            oy = min(y + h, crop.bottom) - max(y, crop.y)
            if ox > 0 and oy > 0 and ox * oy > area:
                best, area = rid, ox * oy
        if best:
            return best, located[best]
    if res is not None and res.regions:
        above = [m for m in res.regions if m.above_threshold and m.region_id != "_rest"]
        if above:
            m = max(above, key=lambda m: (m.changed_px + 100 * m.shift_px, m.region_id))
            return m.region_id, located.get(m.region_id, m.candidate_rect.as_tuple())
        comps = sorted(res.components, key=lambda c: (-c.area, c.bbox))
        if comps:
            ox, oy = next(iter(cand_lm.values()), (0, 0))
            cx, cy = comps[0].centroid
            col, row = min(7, max(0, int((cx - ox) * 8 // W))), min(7, max(0, int((cy - oy) * 8 // H)))
            cw, ch = math.ceil(W / 8), math.ceil(H / 8)
            box = (ox + col * cw, oy + row * ch, cw, ch)
            return f"g-{row}-{col}", box
    return None, None


def _located(reg: RegionsFile, res: DiffResult | None, side: int) -> dict[str, tuple[int, int]]:
    """Where each landmark sits on one side's capture: the Diff Engine's match, else where it was cut."""
    if res is not None and res.landmarks:
        return {k: m[side].pos for k, m in res.landmarks.items() if m[side].found}
    return {lm.id: lm.expected for lm in reg.landmarks if lm.expected is not None}


def _event_id(run: _Run, cp: PlannedCheckpoint) -> str:
    """The Watchdog event id a harness fault at this checkpoint carries (4.13)."""
    return "wd-" + hashlib.sha256(f"{run.run_id}|{cp.checkpoint_id}".encode()).hexdigest()[:8]


def evaluate(run: _Run, cp: PlannedCheckpoint, base: Capture | None, cand: Capture, *, x: str,
             primitive: str | None, entry_ref: str | None, recapture: Callable[[], Capture],
             recapturable: bool = True, store: Store | None = None, synthetic: bool = False,
             absent_reason: str | None = None, injected: bool = False, oracle: FlowOracle | None = None) -> Evaluated:
    """5.17 from capture to classification for one checkpoint; PR checkpoints and canary mutants alike.
    An `injected` candidate (a canary mutant, 10.3 Injection) is already a settled capture: the settle
    loop, a capture-time gate, does not run on it. With `oracle`, a flow checkpoint's assertions are
    evaluated on both builds (module docstring)."""
    reg = run.registry(cp, base if base is not None else cand)
    rules = run.surface_rules(cp)
    park = _park(cand)
    spec = SimApp.settle_spec(reg, park)
    fkw: dict[str, Any] = dict(manifest=x, primitive=primitive, manifest_entry_ref=entry_ref,
                               manifest_entries=run.entries, manifest_tickets=run.tickets)
    s_c = settle(cand.frames, spec, run.params) if not injected else _as_settled(cand)
    s_b = settle(base.frames, spec, run.params) if base is not None else None
    res, comparison = None, None
    recaps = 0
    if not s_c.settled:
        facts = Facts(not_settled=Condition(s_c.reason, side="candidate", detail=s_c.detail), **fkw)
    elif base is None or (s_b is not None and not s_b.settled):
        facts = Facts(base="absent", base_reason=absent_reason or "base_not_settled", **fkw)
        comparison = inv.compare(None, cand.models, rules)
    else:
        res = diff_pair(s_b.capture, s_c.capture, reg, calibration=run.calibration, system_masks=[park_rect(park)],
                        context=DiffContext(run.run_id, cp.checkpoint_id, cp.tier, synthetic=synthetic),
                        params=run.params)
        comparison = inv.compare(base.models, cand.models, rules)
        if res.terminal is not None:
            t = res.terminal
            if t.outcome == "landmark_missing":
                facts = Facts(landmark=Condition(t.reason, side=t.side, detail=t.detail), **fkw)
            else:
                facts = Facts(harness=HarnessFailure(t.reason, _event_id(run, cp)), **fkw)
        else:
            inv_ids, inv_stab, inv_cause = (), None, None
            stab, cause = None, None
            budget_ok = run.budget.take() if (run.budget and (comparison.fired or res.level == "above")
                                              and recapturable) else True
            if comparison.fired:
                if not recapturable or not budget_ok:
                    p = inv.confirm(comparison, None, cause="not_recapturable" if not recapturable else "budget")
                else:
                    again = recapture()
                    recaps = 1
                    p = inv.confirm(comparison, inv.check_capture(again.models, rules, reference=base.models[0]))
                inv_ids = tuple(r.violation.invariant_id for r in (p.confirmed or p.unconfirmed))
                inv_stab, inv_cause = p.stability, p.stability_cause
            if res.level == "above":
                if comparison.fired:
                    stab, cause = inv_stab, inv_cause
                else:
                    def above(a: np.ndarray, b: np.ndarray) -> bool:
                        return diff_pair(a, b, reg, calibration=run.calibration, system_masks=[park_rect(park)],
                                         context=DiffContext(run.run_id, cp.checkpoint_id, cp.tier, synthetic=synthetic),
                                         params=run.params).level == "above"
                    if recapturable and budget_ok:
                        again = [recapture(), recapture()]
                        recaps = 2
                        st = diff_stability(s_c.capture, [Recapture(True, a.screen.image) for a in again], above,
                                            params=run.params)
                    else:
                        st = diff_stability(s_c.capture, [], above, recapturable=recapturable, budget_ok=budget_ok,
                                            params=run.params)
                    stab, cause = st.stability, st.cause
            fix = None
            if res.threshold_source == "uncalibrated":
                fix = "no calibrated floor for this fingerprint (3.9); threshold 0"
            facts = Facts(diff=res.level, stability=stab, stability_cause=cause, threshold_source=res.threshold_source,
                          fix_list_item=fix, invariant_ids=inv_ids, invariant_stability=inv_stab,
                          invariant_stability_cause=inv_cause, bounds_unreadable=comparison.unreadable, **fkw)
    checks: tuple[OracleCheck | None, OracleCheck | None] = (None, None)
    if oracle is not None and cp.test_kind == "flow" and s_c.settled and facts.landmark is None and facts.harness is None:
        try:
            c_chk = oracle.check(cp.flow_id or "", cp.step_id or "", cp.data_mode, cand, reg, _located(reg, res, 1),
                                 "candidate")
            b_chk = oracle.check(cp.flow_id or "", cp.step_id or "", cp.data_mode, base, reg, _located(reg, res, 0),
                                 "base") if base is not None and c_chk is not None else None
        except OracleConfigError:              # a flow the lint accepted that the Oracle cannot evaluate: harness
            c_chk = b_chk = None
            facts = dataclasses.replace(facts, harness=HarnessFailure("exception:OracleConfigError", _event_id(run, cp)))
        if c_chk is not None:
            facts = dataclasses.replace(facts, **c_chk.verdict.facts_fields(harness_event_id=_event_id(run, cp)))
            checks = (c_chk, b_chk)
    classified = classify(facts)
    ev = Evaluated(cp, base, cand, reg, facts, classified, res, comparison, recaptures=recaps,
                   settle_ms=s_c.time_to_settle_ms, oracle=checks[0], oracle_base=checks[1])
    ev.facts_category = classified.outcome
    cand_lm = {k: m[1].pos for k, m in (res.landmarks.items() if res else []) if m[1].found}
    h, w = cand.screen.image.shape[:2]
    if classified.outcome in LISTED and classified.outcome != "harness_error":
        region, box = _region_choice(reg, res, comparison, cand_lm, (w, h))
        asserted = _asserted_region(ev, cand_lm) if classified.outcome == "assertion_failed" else None
        if asserted is not None:
            region, box = asserted
        if region is None and comparison is not None and comparison.fired:
            region, box = "_window", (0, 0, w, h)
        if region is not None and box is not None:
            ev.region = region
            ev.phash = phash(_crop(cand.screen.image, _clip(box, w, h)))
            ev.crop = _clip(box, w, h, 24)
    if comparison is not None and comparison.fired:
        u = inv.Rect.union(r.violation.crop for r in comparison.reported)
        ev.crop = _clip(u.as_tuple(), w, h, 24)
    if ev.crop is None:
        ev.crop = (0, 0, w, h)
    if store is not None and not synthetic and ev.phash and ev.region:
        look = store.lookup_decisions(Signature(cp.checkpoint_id, ev.region, ev.phash, ev.category),
                                      at=run.now.isoformat(), run_id=run.run_id, manifest_entries=run.entries,
                                      manifest_tickets=run.tickets)
        ev.related = [{"decision_id": m.decision.decision_id, "hamming": m.distance} for m in look.related]
        if look.applicable and not look.frozen:      # a frozen signature inherits nothing (9.7)
            facts = dataclasses.replace(facts, decisions=tuple(DecisionMatch(m.decision, m.distance)
                                                               for m in look.applicable))
            ev.facts, ev.classified = facts, classify(facts)
            if look.conflict:
                ev.conflicts = [{"decision_id": m.decision.decision_id, "verdict": m.decision.verdict}
                                for m in look.matches]
    return ev


def _asserted_region(ev: Evaluated, cand_lm: Mapping[str, tuple[int, int]]) -> tuple[str, tuple] | None:
    """The signature region of an `assertion_failed` checkpoint (9.7): the region the failed assertion read;
    for a rule, the operand region the diff saw move, else its last operand (R01's total)."""
    if ev.oracle is None:
        return None
    failed = ev.oracle.failed()
    if not failed:
        return None
    fields = [e.field for e in failed[0].operands.values()]
    moved = {m.region_id for m in (ev.diff.regions if ev.diff is not None else ()) if m.above_threshold}
    rid = next((f for f in fields if f in moved), fields[-1] if fields else None)
    try:
        r = ev.registry.region(rid) if rid else None
    except KeyError:
        r = None
    if r is None or r.landmark not in cand_lm:
        return None
    lx, ly = cand_lm[r.landmark]
    return r.id, (r.rect.x + lx, r.rect.y + ly, r.rect.w, r.rect.h)


def _as_settled(cap: Capture) -> SettleResult:
    """An injected capture is a settled capture already (10.3 Injection)."""
    return SettleResult(True, cap.screen.image, 0, 750, None, None, None, 750)


def _with_review(ev: Evaluated, review: Review) -> None:
    ev.review = review
    ev.facts = dataclasses.replace(ev.facts, review=review)
    c = classify(ev.facts)
    ev.classified = c
    if not (c.reason or "").startswith("decision:"):
        ev.facts_category = c.outcome     # the verdict decided the category; a decision would not have


# ------------------------------------------------------------------------------------------------
# Records


_NOUNS = {"ladder.viewport": "the ladder viewport", "ladder.strip": "the order-control strip",
          "grid.cell00.header": "the cell header"}
# 1.10's short names for the fifteen invariants (the `invariant_violation` definition)
SHORT = {"INV-01": "sibling bounds intersect", "INV-02": "component outside parent", "INV-03": "text clipped",
         "INV-04": "ellipsis", "INV-05": "required label missing", "INV-06": "unexpected scrollbar",
         "INV-07": "font below minimum", "INV-08": "placeholder or error text", "INV-09": "window off screen",
         "INV-10": "control count wrong", "INV-11": "text runs overlap", "INV-12": "columns misaligned",
         "INV-13": "focus order wrong", "INV-14": "error dialog present", "INV-15": "sign and color disagree"}


def _noun(name: str, screen) -> str:
    try:
        x, y, w, h = screen.get(name).bbox
    except KeyError:
        return name
    return f"{_NOUNS.get(name, name)} (y {y} to {y + h})"


def _violation_text(ev: Evaluated) -> tuple[str, dict]:
    """The invariant finding's description and the evidence notes 9.2 prints (overlay, violation)."""
    parts, ev_notes = [], {}
    cand, base = ev.cand.screen, ev.base.screen if ev.base else None
    by_id: dict[str, list] = {}
    for r in ev.comparison.reported if ev.comparison else ():
        by_id.setdefault(r.violation.invariant_id, []).append(r)
    for inv_id, reps in by_id.items():
        pre = " (pre-existing on the base, reported one severity lower)" if all(r.pre_existing for r in reps) else ""
        v = reps[0].violation
        if inv_id == "INV-01" and "|" in v.subject:
            a, b = v.subject.split("|")
            oy = inv.Rect(*cand.get(a).bbox).overlap(inv.Rect(*cand.get(b).bbox))[1]
            parts.append(f"{inv_id} {SHORT[inv_id]}: {_noun(b, cand)} overlaps {_noun(a, cand)} by {oy} px "
                         f"across its width{pre}.")
            ev_notes.setdefault("violation", f"{oy} px intersection")
            if base is not None:
                try:
                    ba, bb = inv.Rect(*base.get(a).bbox), inv.Rect(*base.get(b).bbox)
                    gap = max(ba.y, bb.y) - min(ba.bottom, bb.bottom)
                    touch = f"touch at y={max(ba.y, bb.y)}, 0 px gap" if gap == 0 else f"are {gap} px apart"
                    ev_notes.setdefault("overlay", f"bounds overlaid, intersection hatched; on the base the "
                                                   f"{_NOUNS.get(a, a).replace('the ', '')} and "
                                                   f"{_NOUNS.get(b, b).replace('the ', '')} {touch}")
                except KeyError:
                    pass
        elif inv_id == "INV-10":
            ids = [r.violation.subject for r in reps]
            parts.append(f"{inv_id} {SHORT[inv_id]}: required control{'s' if len(ids) > 1 else ''} "
                         f"{' and '.join(ids)} not found by template (covered by a later-drawn component){pre}.")
        else:
            details = "; ".join(r.violation.detail for r in reps)
            for k, noun in _NOUNS.items():
                details = details.replace(k, noun.replace("the ", ""))
            parts.append(f"{inv_id} {SHORT[inv_id]}: {details}{pre}.")
    return " ".join(parts), ev_notes


def _base_result(ev: Evaluated, assertion_id: str) -> AssertionResult | None:
    if ev.oracle_base is None:
        return None
    return next((r for r in ev.oracle_base.verdict.results if r.assertion_id == assertion_id), None)


def _raw_values(ev: Evaluated) -> dict[str, str]:
    """`Finding.evidence.raw_values` (4.13): the first failed assertion's expected, read and source, and what
    every source read on the candidate and on the base build (`AssertionResult.raw_values`)."""
    first = ev.oracle.failed()[0]
    return first.raw_values(_base_result(ev, first.assertion_id))


def _assertion_text(ev: Evaluated) -> str:
    """An `assertion_failed` finding: the Oracle's expected and extracted values (4.13 detail), then the raw
    reads of the failed assertion's regions on both builds, so a reader sees what each build drew."""
    first = ev.oracle.failed()[0]
    base = _base_result(ev, first.assertion_id)

    def reads(r: AssertionResult | None) -> str:
        if r is None:
            return "not read"
        return ", ".join(f"{e.field} '{e.text}' ({e.source or 'no source'})" for e in r.operands.values()) or "nothing"
    others = [r.assertion_id for r in ev.oracle.failed()[1:]]
    text = (f"Assertion {first.assertion_id} failed: {first.detail}. Raw values, candidate: {reads(first)}; "
            f"base: {reads(base)}" + (f" (base {base.status})" if base is not None else "") + ".")
    if others:
        text += f" Also failed: {', '.join(others)}."
    return text


def _ocr_in(model: inv.ScreenModel, box: tuple[int, int, int, int]) -> str:
    x, y, w, h = box
    words = [wd for wd in model.words if x <= wd.bbox.center[0] < x + w and y <= wd.bbox.center[1] < y + h]
    return " ".join(wd.text for wd in sorted(words, key=lambda wd: (wd.bbox.y, wd.bbox.x)))


def _metrics_text(ev: Evaluated) -> str:
    """A finding description from what the run measured: the Diff Engine's regions and, inside them,
    the OCR words of the bounds source on both builds (there is no Reviewer description)."""
    if ev.diff is None:
        return ""
    money = {r.id for r in ev.registry.regions if r.money_cell}
    parts = []
    for m in ev.diff.regions:
        if not m.above_threshold:
            continue
        text = f"{m.region_id}{' (money cell, zero floor)' if m.region_id in money else ''}: {m.changed_px} px changed"
        if any(m.shift):
            text += f", shifted {m.shift}"
        if ev.base is not None:
            was, now = _ocr_in(ev.base.model, m.base_rect.as_tuple()), _ocr_in(ev.cand.model, m.candidate_rect.as_tuple())
            if was != now:
                text += f"; OCR reads '{now}' where the base read '{was}'"
        parts.append(text)
    return "; ".join(parts) + "." if parts else ""


def _overlay(img: np.ndarray, rects: Iterable[inv.Rect]) -> np.ndarray:
    """Violation bounds drawn over a capture, the intersection hatched (1.11 evidence)."""
    out = img.copy()
    rs = list(rects)
    for r in rs:
        cv2.rectangle(out, (r.x, r.y), (r.right - 1, r.bottom - 1), (0, 170, 255), 1)
    for i, a in enumerate(rs):
        for b in rs[i + 1:]:
            x0, y0 = max(a.x, b.x), max(a.y, b.y)
            x1, y1 = min(a.right, b.right), min(a.bottom, b.bottom)
            if x1 <= x0 or y1 <= y0:
                continue
            for k in range(x0 - (y1 - y0), x1, 6):
                cv2.line(out, (max(k, x0), y0 + max(0, x0 - k)), (min(k + (y1 - y0), x1 - 1), min(y1 - 1, y0 + x1 - 1 - k)),
                         (255, 64, 64), 1)
    return out


class _Writer:
    """Turns evaluated checkpoints into the records of 4.13 and their artifacts (8.4)."""

    def __init__(self, run: _Run, arts: ArtifactStore, today: date, clock: str):
        self.run, self.arts, self.today, self.clock = run, arts, today, clock
        tag = run.run_id.split("-pr-")[-1].replace("-", "") if "-pr-" in run.run_id else \
            hashlib.sha256(run.run_id.encode()).hexdigest()[:6]
        self.tag, self.n = tag, 0

    def _put(self, img: np.ndarray, name: str, kind: str, cid: str, side: str | None, tag: str = "") -> StoredArtifact:
        path = f"{self.run.run_id}/{cid}/{tag + '/' if tag else ''}{name}.png"
        return self.arts.put(_png(img), name=path, kind=kind, media_type="image/png",
                             redaction=REDACTION, checkpoint_id=cid, side=side,
                             captured_at=_ts(self.today, self.clock))

    def artifacts(self, ev: Evaluated, tag: str = "") -> tuple[dict, list[StoredArtifact]]:
        cid = ev.cp.checkpoint_id
        stored: list[StoredArtifact] = []
        cimg = ev.cand.screen.image
        bimg = ev.base.screen.image if ev.base else None
        listed = ev.classified.outcome in LISTED or ev.classified.decision_ref is not None
        box = ev.crop if listed else (0, 0, cimg.shape[1], cimg.shape[0])
        if ev.comparison is not None and ev.comparison.fired:
            rects = [r for rep in ev.comparison.reported[:1] for r in rep.violation.rects]
            cimg = _overlay(cimg, rects)
            if bimg is not None:
                names = ev.comparison.reported[0].violation.subject.split("|")
                brects = []
                for nm in names:
                    try:
                        brects.append(inv.Rect(*ev.base.screen.get(nm).bbox))
                    except KeyError:
                        pass
                bimg = _overlay(bimg, brects or rects)
        out: dict[str, Any] = {}
        a = self._put(_crop(cimg, _clip(box, cimg.shape[1], cimg.shape[0])), "candidate", "crop", cid, "candidate", tag)
        stored.append(a)
        out["candidate"] = a.sha256
        if bimg is not None:
            b = self._put(_crop(bimg, _clip(box, bimg.shape[1], bimg.shape[0])), "base", "crop", cid, "base", tag)
            stored.append(b)
            out["base"] = b.sha256
        if listed and ev.diff is not None and ev.diff.terminal is None and ev.diff.level != "zero":
            hm = ev.diff.heatmap
            hmap = self._put(_crop(hm, _clip(box, hm.shape[1], hm.shape[0])), "heatmap", "heatmap", cid, None, tag)
            stored.append(hmap)
            out["heatmap"] = hmap.sha256
        return out, stored

    def records(self, ev: Evaluated, *, synthetic: bool = False, data_mode: str | None = None, tag: str = ""
                ) -> tuple[CheckpointOutcome, list[Finding], list[StoredArtifact]]:
        cp, c = ev.cp, ev.classified
        arts, stored = self.artifacts(ev, tag)
        diff = None
        if ev.diff is not None and ev.diff.terminal is None:
            diff = ev.diff.summary
        rec_kw = c.fields()
        o = CheckpointOutcome(
            run_id=self.run.run_id, checkpoint_id=cp.checkpoint_id, workspace_id=cp.workspace_id,
            persona=cp.persona, data_mode=data_mode or cp.data_mode, surface_id=cp.surface_id,
            test_kind=cp.test_kind, tier=cp.tier, flow_id=cp.flow_id, step_id=cp.step_id,
            size_step=dict(cp.size_step) if cp.size_step else None, diff=diff, artifacts=arts,
            recaptures=ev.recaptures, synthetic=synthetic, flow_class=_flow_class(self.run.cfg, cp),
            fingerprint_id=self.run.fp_id, data_epoch_id=self.run.epoch,
            captured_at=_ts(self.today, self.clock, 600), recorded_at=_ts(self.today, self.clock, 900), **rec_kw)
        findings: list[Finding] = []
        if synthetic:
            return o, findings, stored
        if c.outcome in LISTED or c.decision_ref:
            self.n += 1
            fid = f"F-{self.tag}-{self.n:03d}"
            evidence: dict[str, Any] = {"target_window": ev.cand.info.target_window}
            if cp.test_kind in ("sweep", "split") and ev.cand.info.layout:
                evidence["layout"] = ev.cand.info.layout
            sev = None
            if ev.comparison is not None and ev.comparison.fired and c.outcome == "invariant_violation":
                desc, notes = _violation_text(ev)
                evidence.update(notes)
                sev = ev.comparison.reported[0].severity
            elif c.outcome == "landmark_missing":
                desc = f"Landmark not found ({c.reason}, side {c.side}): {c.detail}"
                evidence["side"] = c.side
            elif c.outcome == "assertion_failed" and ev.oracle is not None and ev.oracle.failed():
                desc = _assertion_text(ev)
            elif c.outcome == "needs_human" and c.reason == "oracle_unreadable" and ev.oracle is not None:
                desc = (f"The Oracle could not read {', '.join(ev.oracle.verdict.unreadable)} on any source "
                        f"(2.8.1): {c.detail or 'no read above its confidence floor'}.")
            elif c.classification is not None and c.classification.description:
                desc = c.classification.description
            else:
                desc = _metrics_text(ev) or (c.detail or c.outcome)
            if ev.oracle is not None and ev.oracle.failed():
                evidence["raw_values"] = _raw_values(ev)
            if ev.answer is not None:
                evidence["reviewer"] = {"verdict": ev.answer["verdict"], "change_types": change_types(ev.answer)}
            if ev.related:
                evidence["near_decisions"] = ev.related
            if ev.conflicts:
                evidence["conflicting_decisions"] = ev.conflicts
            kind = ev.category if ev.category in load().finding_kinds else c.outcome
            if kind not in load().finding_kinds:
                kind = "collateral"
            findings.append(Finding(
                finding_id=fid, run_id=self.run.run_id, kind=kind, description=desc, first_seen_run_id=self.run.run_id,
                checkpoint_id=cp.checkpoint_id, tier=cp.tier, region_id=ev.region, primitive=c.primitive,
                manifest_entry_ref=c.manifest_entry_ref, phash=ev.phash, category=ev.category, evidence=evidence,
                severity=sev, side=c.side, step_id=cp.step_id, decision_ref=c.decision_ref,
                created_at=_ts(self.today, self.clock, 900)))
        if ev.diff is not None:
            for f in ev.diff.findings:
                findings.append(dataclasses.replace(f, created_at=_ts(self.today, self.clock, 900), tier=cp.tier))
        return o, findings, stored


# ------------------------------------------------------------------------------------------------
# Coverage and run-health inputs


def coverage_summary(cfg: SentinelConfig, plan: ScopePlan, outcomes: Sequence[CheckpointOutcome], run_id: str) -> dict:
    """The PR run's coverage summary the Reporter reads (`report.model` docstring, 2.12)."""
    matrix = build_matrix(run_id, cfg.registry, [Profile("margin", cfg.entitlements)], {"margin": list(outcomes)},
                          plan.cuts, PERSONAS)
    tiers: dict[int, dict] = {}
    for s in cfg.registry:
        t = tiers.setdefault(s.tier, {"registered": 0, "in_scope": 0, "covered": 0, "not_compared": {}, "not_run": {}})
        t["registered"] += 1
        cells = [matrix.cell(s.id, p, "margin") for p in PERSONAS]
        scoped = any(o.surface_id == s.id for o in outcomes) or any(
            c.surface_id == s.id and c.reason != "scope" for c in plan.cuts)
        if not scoped:
            t["not_run"]["scope"] = t["not_run"].get("scope", 0) + 1
            continue
        t["in_scope"] += 1
        if any(c.state == "covered" for c in cells):
            t["covered"] += 1
            continue
        reasons = [c.reason for c in cells if c.state == "not_run" and c.reason and c.reason != "scope"]
        blocked = [r for r in reasons if r.startswith("blocked:")]
        if blocked:
            k = blocked[0].split(":", 1)[1]
            t["not_compared"][k] = t["not_compared"].get(k, 0) + 1
        elif reasons:
            k = reasons[0].split(":", 1)[0]
            t["not_run"][k] = t["not_run"].get(k, 0) + 1
    impact = plan.impact
    touched = sorted(impact.touched_primitives) if impact else []
    edges = sum(1 for p in touched for _ in cfg.dep_map.surfaces_of(p, cfg.registry))
    static_added = len(impact.static_surfaces - impact.map_expanded - plan.declared) if impact else 0
    smoke = cfg.registry.smoke_subset()
    smoke_cps = sum(1 for cp in plan.planned if cp.surface_id in smoke and cp.test_kind == "census")
    cells = [{"surface": c.surface_id, "persona": c.persona, "state": c.state, "worst": c.worst, "n": c.count,
              "reason": c.reason} for c in matrix.cells.values()
             if not (c.state == "not_run" and c.reason == "scope" and c.persona not in
                     {o.persona for o in outcomes})]
    return {"tiers": tiers,
            "scope": {"declared_surfaces": len(plan.declared), "edges": edges, "primitives": touched,
                      "static_impact_added": static_added, "smoke_census": smoke_cps,
                      "send_policy": cfg.send_policy, "order_surfaces_entitlement": 0},
            "cells": cells}


# ------------------------------------------------------------------------------------------------
# The store: identity rows


def _register(store: Store, cfg: SentinelConfig, builds: Iterable[str]) -> None:
    for i, rig in enumerate(RIGS, 1):
        store.register_rig(rig, login_alias=f"vault://sentinel/rigs/{rig}/tos-login", owner="asmith",
                           snapshot_id=f"snap-{rig}-2026-11-01", allowlist_entry=i)
    for bid in builds:
        doc = cfg.builds.get(bid)
        if doc is None:
            continue
        b = doc["build"]
        store.register_build(bid, channel=b["channel"], source_commit=b["commit_sha"],
                             pipeline_artifact_url=(b.get("artifact") or {}).get("uri", "sim://"),
                             installer_sha256=str((b.get("artifact") or {}).get("sha256", "sha256:sim")).split(":")[-1],
                             branch=b.get("branch"), first_seen_at=str(b.get("built_at")) if b.get("built_at") else None)


def _run_plan(run_id: str, run_type: str, cfg_root: Path, scope: Sequence[ScopeEntry], shards: Sequence[ShardPlan],
              started: str, manifest_text: str | None, pr: int | None, author: str | None) -> RunPlan:
    sha = tree_sha256(cfg_root)
    return RunPlan(run_id=run_id, run_type=run_type, config_commit=sha[:12], config_tarball_sha256=sha,
                   snapshot_set_sha256=hashlib.sha256(",".join(RIGS).encode()).hexdigest(),
                   harness_sha="sentinel-core-0.1.0-sim", authorization_ref="AUTH-SIM-2026-11",
                   budget_sec=2700 if run_type == "pr" else 4 * 3600, scope=tuple(scope), shards=tuple(shards),
                   manifest_sha256=hashlib.sha256(manifest_text.encode()).hexdigest() if manifest_text else None,
                   pr_ref=f"PR {pr}" if pr else None, pr_author=author, started_at=started)


def _status_json(status) -> dict:
    return {"context": status.context, "state": status.state, "description": status.description,
            "target_url": status.target_url, "mode": status.mode}


# ------------------------------------------------------------------------------------------------
# The Orchestrator and the Executor (5.2, 5.4, 10.2): one shard through the gate machine


FLOW_INPUT_STEPS = ("click_named", "press", "type_fixture")
NAMED_KEYS = {"enter": VK_RETURN, "return": VK_RETURN, "space": VK_SPACE, "tab": VK_TAB, "escape": VK_ESCAPE}
GUARDED_KEYS = ("enter", "return", "space")
MAX_WAIT_S = 15.0                        # 3.5: the settle loop's maximum wait, the hang detector's max_wait_s
STEP_S = 2.0                             # simulated seconds per Executor step (heartbeats, token expiry)


def _units(cps: Sequence[PlannedCheckpoint]) -> list[list[PlannedCheckpoint]]:
    """The shard's work in order: a flow's checkpoints, a sweep ladder or splitter series of one surface and
    host, or one census checkpoint. A unit is what `rest_of_flow` and `resume_next_flow` mean (10.2)."""
    def key(cp: PlannedCheckpoint) -> tuple:
        if cp.test_kind == "flow":
            return ("flow", cp.flow_id)
        if cp.test_kind in ("sweep", "split"):
            return (cp.test_kind, cp.surface_id, _host(cp))
        return ("census", cp.checkpoint_id)
    out: list[list[PlannedCheckpoint]] = []
    for cp in cps:
        if out and key(out[-1][0]) == key(cp):
            out[-1].append(cp)
        else:
            out.append([cp])
    return out


@dataclass
class _Shard:
    """One shard's moving parts while the Orchestrator runs it."""

    shard_id: str
    rig_id: str
    cps: list[PlannedCheckpoint]
    orch: Orchestrator
    rig: SimulatedRig
    sink: ShardSink
    clock: ManualClock
    ctx: RunContext
    order_stage: bool = False
    noted: set[int] = field(default_factory=set)


class _ShardWork:
    """The Executor's side of every shard of one run (module docstring): build re-read, heartbeats, gateway
    input, captures, the Watchdog's hang detector, the subset outcomes control results return."""

    def __init__(self, run: _Run, cfg: SentinelConfig, scenario: Scenario, out: Path, oracle: FlowOracle | None,
                 store: Store, planned: Sequence[PlannedCheckpoint], plan: ScopePlan | None, today: date):
        self.run, self.cfg, self.scenario, self.out, self.oracle = run, cfg, scenario, out, oracle
        self.store, self.planned, self.plan, self.today = store, list(planned), plan, today
        self.script: RigScript = scenario.rig
        self.ledger = DropLedger()                         # 5.4.8 counts per flow and per run, across shards
        self.evals: list[Evaluated] = []
        self.bare: dict[str, tuple[PlannedCheckpoint, Outcome]] = {}   # outcomes a control result decided
        self.findings: list[Finding] = []
        self.steps: list[tuple[StepOutcome, str]] = []
        self.alerts: list[dict] = []
        self.actions: list[dict] = []
        self.summaries: list[dict] = []
        self.reports: list[tuple[_Shard, RunReport]] = []
        self.calibrated = False
        self.frozen: set[str] = set()

    # -- the shards -------------------------------------------------------------------------------------
    def run_shards(self, specs: Sequence[tuple[str, int, str, list[PlannedCheckpoint]]]) -> None:
        """Every shard through its Orchestrator, as parallel shards on their rigs would run (module docstring):
        each shard's gates in rig order, then every shard that reached `running` captures its checkpoints in
        plan order across the shards, then each shard tears down and proves."""
        self._specs = list(specs)
        self._running: list[_Shard] = []
        self._chain(0)
        self.reports.sort(key=lambda sr: sr[0].ctx.shard)
        self.summaries.sort(key=lambda d: d["shard_id"])

    def _chain(self, k: int) -> None:
        """Shard k's Orchestrator, whose `running` hands over to shard k+1's; the last one runs the captures."""
        if k == len(self._specs):
            self._capture_all()
            return
        shard_id, shard_no, rig_id, cps = self._specs[k]
        sc = self.scenario
        clock = ManualClock(start=self.run.now.astimezone(timezone.utc), mono=1000.0 + 10_000 * shard_no)
        ctx, sim = shard_setup(self.cfg, run_id=sc.run_id, run_type="pr", shard_id=shard_id, shard_no=shard_no,
                               build_id=sc.candidate.build_id, workspace_id=cps[0].workspace_id, script=self.script)
        rig = SimulatedRig(sim, clock=clock)
        sink = ShardSink([cp.checkpoint_id for cp in cps])
        hb = HeartbeatWriter(self.out / "heartbeat" / f"shard-{shard_no}.json", clock)
        orch = Orchestrator(rig, ctx, sink, clock=clock, ledger=self.ledger, heartbeat=hb,
                            watchdog_status=lambda: (1.0, True))
        st = _Shard(shard_id, rig_id, list(cps), orch, rig, sink, clock, ctx)
        entered: list[bool] = []

        def work(o: Orchestrator) -> None:
            entered.append(True)
            for r in list(o.results):                   # gate results may name parts of the scope
                self._subsets(st, r)
            self._running.append(st)
            self._chain(k + 1)

        report = orch.run(work)
        if not entered:                                  # stopped at a gate; the other shards still run
            self._chain(k + 1)
        for r in report.results:
            self._note(st, r)
        for cid, o in sink.filled.items():
            self.bare.setdefault(cid, (next(cp for cp in cps if cp.checkpoint_id == cid), o))
        self.reports.append((st, report))
        self.summaries.append({
            "shard_id": shard_id, "rig": rig_id, "simulated_rig_identity": ctx.allowlist.rig,
            "checkpoints": len(cps), "verdict": report.verdict.status, "reason": report.verdict.reason,
            "last_gate": report.last_gate, "proof": report.proof.line,
            "kill_switch": [name for name, _ in report.kill_switch.steps] if report.kill_switch else None,
            "gates": [{"control": r.control, "gate": r.gate, "passed": r.passed, "detail": r.detail}
                      for r in report.results],
            "unreached_filled": sorted(sink.filled)})

    def _note(self, st: _Shard, r: ControlResult, checkpoint: PlannedCheckpoint | None = None,
              flow_id: str | None = None) -> None:
        """Record a control result once: its 9.10 route, the actions to persist and its findings."""
        if id(r) in st.noted:
            return
        st.noted.add(id(r))
        sc = self.scenario
        a = alert_entry(sc.run_id, st.shard_id, r)
        if a is not None:
            self.alerts.append(a)
        self.actions += persisted_actions(sc.run_id, st.shard_id, st.rig_id, str(self.cfg.fixture.get("login_ref", "")),
                                          r, flow_id)
        for spec in r.findings:
            n = len(self.findings) + 1
            extra: dict[str, Any] = {"created_at": _ts(self.today, sc.clock, 900)}
            if checkpoint is not None:
                extra.update(checkpoint_id=checkpoint.checkpoint_id, step_id=checkpoint.step_id)
            self.findings.append(spec.to_finding(f"fnd-{sc.run_id}-orc-{n:03d}", sc.run_id, **extra))

    # -- outcomes a control result decides ----------------------------------------------------------
    def _todo(self, st: _Shard) -> list[PlannedCheckpoint]:
        return [cp for cp in st.cps if cp.checkpoint_id not in st.sink.done and cp.checkpoint_id not in st.sink.filled]

    def _select(self, st: _Shard, selector: str, current: PlannedCheckpoint | None,
                rest: Sequence[PlannedCheckpoint]) -> list[PlannedCheckpoint]:
        """The shard checkpoints a `SubsetOutcome` selector names (results.SubsetOutcome)."""
        todo = self._todo(st)
        if selector in ("all", "shard"):
            return todo
        if selector == "frozen":
            return [cp for cp in todo if cp.data_mode == "frozen"]
        if selector in ("api_within", "ocr_assertion"):
            def has(cp: PlannedCheckpoint) -> bool:
                exp = step_assertions(self.cfg.flow_step(cp.flow_id or "", cp.step_id or "")) if cp.flow_id else []
                if selector == "api_within":
                    return any(a.get("kind") == "api_within" for a in exp)
                return any(a.get("source", "ocr") == "ocr" and a.get("kind") != "api_within" for a in exp)
            return [cp for cp in todo if has(cp)]
        if selector == "in_progress":
            return [current] if current is not None else []
        if selector == "rest_of_flow":
            return [cp for cp in rest if cp in todo]
        if selector == "first_unreached":
            return todo[:1]
        return []        # account_state, probe:<Pn>, explorer_stage, drill, ...: no checkpoint of a PR shard's scope

    def _subsets(self, st: _Shard, r: ControlResult, current: PlannedCheckpoint | None = None,
                 rest: Sequence[PlannedCheckpoint] = ()) -> None:
        for sub in r.outcomes:
            for cp in self._select(st, sub.selector, current, rest):
                self.bare[cp.checkpoint_id] = (cp, sub.outcome)
                st.sink.done.add(cp.checkpoint_id)

    # -- running --------------------------------------------------------------------------------------
    def _capture_all(self) -> None:
        """The Executor's work of every shard in `running`, unit by unit in plan order (3.10's re-run budget is
        then spent in the plan's tier order, as the run budget of max(30, 10 percent) is shared by the run)."""
        if not self._running:
            return
        if not self.calibrated:                         # the Calibration run of 1.6, before any threshold
            self.run.calibrate(self.planned, self.out)
            self.calibrated = True
        index = {cp.checkpoint_id: i for i, cp in enumerate(self.planned)}
        units = sorted(((st, u) for st in self._running for u in _units(st.cps)),
                       key=lambda su: min(index[cp.checkpoint_id] for cp in su[1]))
        for st, unit in units:
            orch = st.orch
            if orch.stop is not None or not [cp for cp in unit if cp in self._todo(st)]:
                continue
            r = orch.apply(check_build_unchanged(st.rig.read_title_build_id(), st.ctx.expected_build))
            self._note(st, r)
            if not r.passed:
                continue
            if unit[0].test_kind == "flow":
                self._flow(st, unit)
            else:
                for i, cp in enumerate(unit):
                    self._heartbeat(st, None, None, cp)
                    if not self._capture(st, cp, unit[i + 1:]) or orch.stop is not None:
                        break

    def _heartbeat(self, st: _Shard, flow_id: str | None, step_id: str | None, cp: PlannedCheckpoint | None) -> None:
        """10.2: a heartbeat at every step start; a write failing twice ends the run (watchdog.heartbeat_failed)."""
        st.clock.advance(STEP_S)
        hb = st.orch.heartbeat
        if hb is None:
            return
        try:
            hb.write(Heartbeat(self.scenario.run_id, st.ctx.shard, st.ctx.attempt, "running", st.clock.now().isoformat(),
                               st.clock.monotonic(), flow_id=flow_id, step_id=step_id,
                               checkpoint_id=cp.checkpoint_id if cp else None, max_wait_s=MAX_WAIT_S))
        except HeartbeatUnwritable as e:
            self._note(st, st.orch.apply(heartbeat_failed(e)))

    def _flow(self, st: _Shard, unit: Sequence[PlannedCheckpoint]) -> None:
        """One flow: its steps in order, input steps through the gateway, its checkpoints captured, then its
        teardown input (5.4.3, 4.7). Preconditions and normalize steps send no input here."""
        flow_id = unit[0].flow_id or ""
        doc = self.cfg.flow_docs.get(flow_id, {})
        want = {cp.step_id: cp for cp in unit}
        cps = list(unit)
        for step in doc.get("steps") or ():
            if st.orch.stop is not None:
                return
            sid = str(step.get("id"))
            cp = want.get(sid) if step.get("type") == "checkpoint" else None
            self._heartbeat(st, flow_id, sid, cp)
            if cp is not None:
                if cp in self._todo(st) and not self._capture(st, cp, cps[cps.index(cp) + 1:]):
                    return
            elif step.get("type") in FLOW_INPUT_STEPS and not self._input(st, flow_id, step, doc):
                return
            if self.script.enter_after == (flow_id, sid):     # the scenario's scripted Enter on the open dialog
                if not self._send(st, Action("press", keys=(VK_RETURN,), declared_guarded=True,
                                             flow_class=str(doc.get("class", "standard"))),
                                  flow_id, f"{sid}+enter"):
                    return
        for step in doc.get("teardown") or ():
            if st.orch.stop is not None:
                return
            self._heartbeat(st, flow_id, str(step.get("id")), None)
            if step.get("action") == "close_dialog" and st.rig.dialog_open:
                # the Order Confirmation's dismiss allowlist (6.2): Delete closes the dialog, Send never
                self._send(st, Action("click", point=DELETE_POINT, resolved_name="Delete", under_point_text="Delete",
                                      teardown_action="close_dialog", flow_class=str(doc.get("class"))),
                           flow_id, str(step.get("id")))
                st.order_stage = st.rig.dialog_open

    def _label(self, target: str) -> str:
        """A step target's control text from the Surface Registry (`<surface>.<control id>`)."""
        for sid in sorted(self.cfg.surfaces, key=len, reverse=True):
            if target.startswith(sid + "."):
                cid = target[len(sid) + 1:]
                for c in self.cfg.surfaces[sid].get("controls") or ():
                    if str(c.get("id")) == cid:
                        return str(c.get("ocr_text") or cid)
                return cid
        return target

    def _input(self, st: _Shard, flow_id: str, step: Mapping[str, Any], doc: Mapping[str, Any]) -> bool:
        """One input-bearing flow step as the gateway's `Action` (5.4.3 stages 1 to 3 resolved here)."""
        kind, cls = step.get("type"), str(doc.get("class", "standard"))
        if kind == "click_named":
            label = self._label(str(step.get("target", "")))
            stage = step.get("order_stage") is True
            ok = self._send(st, Action("click", point=CONFIRM_POINT if label.casefold() == "confirm and send" else (40, 40),
                                       resolved_name=label, under_point_text=label, order_stage_step=stage,
                                       flow_class=cls), flow_id, str(step.get("id")))
            st.order_stage = st.order_stage or (stage and st.rig.dialog_open)
            return ok
        key = str(step.get("key") if kind == "press" else step.get("commit") or "").casefold()
        if key not in NAMED_KEYS:
            return True
        return self._send(st, Action(kind if kind == "press" else "type_fixture", keys=(NAMED_KEYS[key],),
                                     declared_guarded=key in GUARDED_KEYS, flow_class=cls), flow_id, str(step.get("id")))

    def _send(self, st: _Shard, action: Action, flow_id: str, step_id: str) -> bool:
        """`Rig.send_input` and the drop ledger of 5.4.8: a refusal or a hook drop is a step event, and the
        ledger's result may end the flow or the run (an attempted send is `aborted_safety`)."""
        res = st.rig.send_input(action)
        drops = [refusal(res.refused)] if res.refused is not None else []
        drops += [drain(d, e) for e, d in zip(res.events, res.decisions) if not d.allowed]
        for drop in drops:
            r = st.orch.apply(self.ledger.record(drop, unit=flow_id, order_stage=st.order_stage))
            self._note(st, r, flow_id=flow_id)
            self.steps.append((StepOutcome(self.scenario.run_id, flow_id, step_id, r.step or "blocked_by_gateway",
                                           shard_id=st.shard_id, gateway_event={"decision": drop.decision,
                                                                                "reason": drop.reason,
                                                                                "class": drop.gate_class},
                                           recorded_at=_ts(self.today, self.scenario.clock, 600)), st.shard_id))
        return not drops and st.orch.stop is None

    def _capture(self, st: _Shard, cp: PlannedCheckpoint, rest: Sequence[PlannedCheckpoint]) -> bool:
        """Capture and evaluate one checkpoint; False when the unit ends here (an app failure, 10.2)."""
        cid = cp.checkpoint_id
        if self.script.freeze_at == cid and cid not in self.frozen:
            return self._freeze(st, cp, rest)
        live = cp.data_mode == "masked_live"
        base_cap = self.run.capture(cp, "base", tick=1 if live else 0)
        cand_cap = self.run.capture(cp, "candidate", tick=2 if live else 0)
        att = self.plan.attribution.get(cp.surface_id)
        ticks = iter(range(3, 9))
        ev = evaluate(self.run, cp, base_cap, cand_cap, x=self.plan.x_input(cp.surface_id),
                      primitive=att.primitive if att else None, entry_ref=att.entry_id if att else None,
                      recapture=lambda: self.run.capture(cp, "candidate", tick=next(ticks)),
                      recapturable=_recapturable(self.cfg, cp), store=self.store, oracle=self.oracle)
        self.evals.append(ev)
        st.sink.done.add(cid)
        return True

    def _freeze(self, st: _Shard, cp: PlannedCheckpoint, rest: Sequence[PlannedCheckpoint]) -> bool:
        """The app's EDT freezes while `cp` is captured: the screenshot-staleness detector fires after
        max(2 x max wait, 60 s), `app_failure` decides, and the Executor kills, relaunches and logs in again
        before the next unit (10.2 sampling 5, F62)."""
        self.frozen.add(cp.checkpoint_id)
        since = st.clock.monotonic()
        st.clock.advance(max(2 * MAX_WAIT_S, HANG_FLOOR_S))
        if not edt_frozen(now=st.clock.monotonic(), expect_change_since=since, last_hash_change=since - STEP_S,
                          max_wait_s=MAX_WAIT_S, anti_anchor=False, capture_suspended=False):
            raise RuntimeError("the hang detector did not fire on a frozen screen")
        r = st.orch.apply(app_failure(CrashSignal("hang", "screenshot_staleness"), st.orch.budgets, phase="running"))
        self._note(st, r, checkpoint=cp, flow_id=cp.flow_id)
        self._subsets(st, r, cp, rest)
        if st.orch.stop is not None:
            return False
        if r.has("kill_tree"):
            st.rig.kill_process_tree()
            st.order_stage = False
        if r.has("wait_session_release"):
            st.clock.sleep(SESSION_RELEASE_WAIT_S)
        if r.has("relaunch"):
            self._relaunch(st)
        return False

    def _relaunch(self, st: _Shard) -> None:
        """Re-enter at `launched` after an app failure (10.2): launch, log in, assert paper mode again."""
        orch, ctx, rig = st.orch, st.ctx, st.rig
        for check in (lambda: check_launched(rig.launch(), expected_build=ctx.expected_build, first_launch=False,
                                             budgets=orch.budgets),
                      lambda: check_authenticated(rig.login()),
                      lambda: judge_identity([rig.read_account_selector()], ctx.allowlist)):
            r = orch.apply(check())
            self._note(st, r)
            if not r.passed or orch.stop is not None:
                return

    # -- after every shard ----------------------------------------------------------------------------
    def bare_outcome(self, cp: PlannedCheckpoint, o: Outcome) -> CheckpointOutcome:
        sc = self.scenario
        return CheckpointOutcome(
            run_id=sc.run_id, checkpoint_id=cp.checkpoint_id, outcome=o.outcome, reason=o.reason, detail=o.detail,
            gate=o.gate, workspace_id=cp.workspace_id, persona=cp.persona, data_mode=cp.data_mode,
            surface_id=cp.surface_id, test_kind=cp.test_kind, tier=cp.tier, flow_id=cp.flow_id, step_id=cp.step_id,
            size_step=dict(cp.size_step) if cp.size_step else None, flow_class=_flow_class(self.cfg, cp),
            fingerprint_id=self.run.fp_id, data_epoch_id=self.run.epoch, recorded_at=_ts(self.today, sc.clock, 900))

    def stop(self) -> tuple[_Shard, ControlResult] | None:
        """The shard and result that ended the run (an `aborted_*` run outcome), if one did."""
        for st, rep in self.reports:
            if rep.verdict.aborted and st.orch.stop is not None:
                return st, st.orch.stop
        return None

    def verdict(self) -> RunVerdict:
        """8.2: the run outcome is the worst of the shards'."""
        v: RunVerdict | None = None
        for _, rep in self.reports:
            v = worse(v, rep.verdict)
        return v or RunVerdict("complete")

    def proof(self) -> str:
        """The run's proof line (`Proof.line`, 6.6) for run health and the abort block: the most severe of the
        shards' (ORDERS SENT, then UNPROVEN, then PROVEN; 8.2), the stopping shard's among equals."""
        lines = [r.proof.line for _, r in self.reports]
        stop = self.stop()
        if stop is not None:
            lines.insert(0, next(r.proof.line for s, r in self.reports if s is stop[0]))
        return worst_proof(lines) if lines else "UNPROVEN: no shard ran"

    def safety(self) -> dict:
        """Run health's safety facts (9.11), from what the shards' controls returned. The gateway drops are the
        run's stored `step_events` rows the hook dropped or the slow half refused (9.11), so it runs after
        `write_steps`."""
        results = [r for _, rep in self.reports for r in rep.results]
        att = [r for r in results if r.gate == "settings_attested" and r.control == "attestation"]
        did = {rep.proof.cancel_all for _, rep in self.reports}          # what Cancel All did per shard (6.6)
        cancel = "failed" if "failed" in did else "verified" if "done" in did else "not needed"
        return {"attestation": ("passed" if att and all(r.passed for r in att) else
                                "not reached" if not att else "failed"),
                "paper_mode_asserted": sum(1 for r in results if r.control == "paper_mode" and r.passed),
                "gateway_drops": self.store.gateway_drops(self.scenario.run_id), "cancel_all": cancel,
                "proof": self.proof(),
                "login": str(self.cfg.fixture.get("login_ref", "unknown"))}

    def write(self, extra: Mapping[str, Any] | None = None) -> None:
        """`alerts.json` and `orchestrator.json` in the scenario output (appended to by every run there)."""
        _append_json(self.out / "alerts.json", self.alerts)
        _append_json(self.out / "orchestrator.json", [{
            "run_id": self.scenario.run_id, "shards": self.summaries, "persisted_actions": self.actions,
            "gateway_events": [s.to_dict() for s, _ in self.steps], **dict(extra or {})}])


class _EvalIndex(abc.Mapping):
    """The run's evaluated checkpoints by checkpoint id, as the shards produce them (for `ItemBuilder`)."""

    def __init__(self, work: _ShardWork):
        self.work = work

    def __getitem__(self, cid: str) -> Evaluated:
        return next(ev for ev in self.work.evals if ev.cp.checkpoint_id == cid)

    def __iter__(self):
        return (ev.cp.checkpoint_id for ev in self.work.evals)

    def __len__(self) -> int:
        return len(self.work.evals)


def _append_json(path: Path, entries: Sequence[Any]) -> None:
    old = json.loads(path.read_text()) if path.exists() else []
    path.write_text(json.dumps(old + list(entries), indent=1, sort_keys=True, default=str) + "\n")


# ------------------------------------------------------------------------------------------------
# run_pipeline


def _abort_config(store: Store, scenario: Scenario, cfg_root: Path, errors: list[str], rule_ids: list[str],
                  out_dir: Path, today: date, started: str) -> RunResult:
    """1.7 T+0:00 and preflight G1 (12.7): config lint failed, so the shard's Orchestrator fails G1 at
    `provisioned` and the run ends `aborted_harness (reason: config_invalid)` before any product action."""
    store.begin_run(_run_plan(scenario.run_id, scenario.run_type, cfg_root, (), (), started, None, scenario.pr,
                              scenario.author))
    sink = ShardSink(())                                 # 1.10: none was scoped yet
    clock = ManualClock(start=parse_time(started).astimezone(timezone.utc))
    ctx = default_context(scenario.run_id, scenario.run_type, expected_build=scenario.candidate.build_id)
    report = Orchestrator(SimulatedRig(SimScenario(preflight_failures={"G1": None}), clock=clock), ctx, sink,
                          clock=clock).run()
    store.fill_unreached(scenario.run_id, "harness_error", "config_invalid")
    run = store.end_run(RunOutcome(scenario.run_id, scenario.run_type, report.verdict.status, report.last_gate,
                                   reason=report.verdict.reason, started_at=started,
                                   ended_at=_ts(today, scenario.clock, 60)))
    alerts = [a for r in report.results if (a := alert_entry(scenario.run_id, ctx.shard_id, r)) is not None]
    _append_json(out_dir / "alerts.json", alerts)
    shard = {"shard_id": ctx.shard_id, "rig": "none (stopped at provisioned)", "verdict": report.verdict.status,
             "reason": report.verdict.reason, "last_gate": report.last_gate, "proof": report.proof.line,
             "gates": [{"control": r.control, "gate": r.gate, "passed": r.passed, "detail": r.detail}
                       for r in report.results]}
    _append_json(out_dir / "orchestrator.json", [{"run_id": scenario.run_id, "shards": [shard], "persisted_actions": [],
                                                  "gateway_events": [], "lint_errors": errors}])
    ctx_r = ReportContext(pr=scenario.pr or 0, candidate_build=scenario.candidate.build_id,
                          base_build=scenario.base.build_id,
                          abort={"rule_ids": rule_ids,
                                 "not_requeued": "the same config would fail again; fix it first"})
    health = {"wall_clock": _mmss(60), "budget": "45:00", "shards": 1,
              "safety": {"attestation": "not reached", "paper_mode_asserted": 0, "gateway_drops": 0,
                         "cancel_all": "not needed", "proof": report.proof.line},
              "integrations": {"pr_comment": {"state": "posted", "time": "(simulated: written to pr-comment.md)"},
                               "jira_queued": 0, "slack": {"queued": 0}}}
    comment = render_pr_comment(run, [], [], [], {}, health, None, ctx_r)
    rep = build_report(run, [], [], [], {}, health, None, ctx_r)
    (out_dir / "pr-comment.md").write_text(comment, encoding="utf-8")
    (out_dir / "commit-status.json").write_text(json.dumps(_status_json(rep.status), indent=2) + "\n")
    render_run_report(run, [], [], [], {}, health, None, ctx_r, out_dir / "report.html")
    return RunResult(scenario.run_id, scenario.run_type, run.status, run.reason, out_dir, comment=comment,
                     commit_status=_status_json(rep.status), lint_errors=errors, alerts=alerts, shards=[shard],
                     proof=report.proof.line)


def run_pipeline(config_dir: str | Path, scenario: Scenario, out_dir: str | Path, today: date = DEFAULT_TODAY,
                 reviewer: ReviewerAdapter | None = None, live: ReviewerJob | None = None) -> RunResult:
    """One simulated PR run from webhook to PR comment (module docstring). The Reviewer is `reviewer`, else
    with `live` (a `reviewer.ReviewerJob`, `pipeline.live.live_job`) the real adapter `ModelReviewer`, else
    the offline `SimulatedReviewer`. Writes into `out_dir`:
    `config/<run_id>/`, `sentinel.db` (shared by every run of the directory), `artifacts/`,
    `calibration/`, `heartbeat/`, `pr-comment.md`, `commit-status.json`, `report.html`,
    `reviewer-calls.json`, and appends to `alerts.json` and `orchestrator.json`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _ts(today, scenario.clock)
    now = parse_time(started)
    cfg_root = materialize(config_dir, scenario.overlay, out / "config" / scenario.run_id)
    report_lint = lint(cfg_root, phase=1, today=today)
    with Store(out / "sentinel.db") as store:
        if report_lint.errors:
            errs = [v.line() for v in report_lint.errors]
            rules = sorted({v.rule for v in report_lint.errors})
            return _abort_config(store, scenario, cfg_root, errs, rules, out, today, started)
        cfg = SentinelConfig.load(cfg_root)
        try:
            oracle = FlowOracle.load(cfg, now)
        except OracleConfigError as exc:
            return _abort_config(store, scenario, cfg_root, [str(exc)], [], out, today, started)
        cand_doc = cfg.builds.get(scenario.candidate.build_id, {})
        pair = cand_doc.get("build_pair") or {}
        base_id = pair.get("base", scenario.base.build_id)
        mdoc = cfg.manifests.get(scenario.candidate.build_id)
        manifest = Manifest.from_dict(mdoc) if mdoc else None
        dep_index = DependencyIndex.from_depends_on(scenario.depends_on) if scenario.depends_on is not None else None
        try:
            plan = compute_scope("pr", manifest, cfg.dep_map, cfg.registry, dep_index, classes_in(scenario.changed_paths),
                                 run_id=scenario.run_id, workspaces=cfg.workspaces, flows=cfg.flows,
                                 entitlements=cfg.entitlements, quarantine=cfg.quarantine,
                                 rigs=[Rig(r, "fp") for r in RIGS], candidate_build_id=scenario.candidate.build_id)
        except ScopeConfigError as exc:
            return _abort_config(store, scenario, cfg_root, [str(exc)], ["L-SRF-10"], out, today, started)
        planned = list(plan.planned)
        rig_of = {cp.checkpoint_id: it.rigs[0] for it in plan.items for cp in it.checkpoints}
        run = _Run(cfg, scenario.run_id, "pr", scenario.base, scenario.candidate, now, manifest, plan, plan.x_input)
        run.budget = RerunBudget(len(planned), run.params)
        # 3.3: fingerprint equality on every field but build id, before any capture
        mismatch = compare_fingerprints({"match": dict(run.fp_match, build_id=base_id)},
                                        {"match": dict(run.fp_match, build_id=scenario.candidate.build_id)})
        assert mismatch is None
        # -- T+0:00 to 0:02: the run row, its scope and shards (1.7)
        arts = ArtifactStore(out / "artifacts", f"runs/{scenario.run_id}")
        writer = _Writer(run, arts, today, scenario.clock)
        _register(store, cfg, [base_id, scenario.candidate.build_id])
        used = sorted({rig_of[cp.checkpoint_id] for cp in planned})
        shard_of = {r: f"{scenario.run_id}/{i}" for i, r in enumerate(used, 1)}
        scope_entries = [ScopeEntry(cp.checkpoint_id, cp.workspace_id, cp.persona, cp.tier, cp.data_mode,
                                    flow_class=_flow_class(cfg, cp), recapturable=_recapturable(cfg, cp))
                         for cp in planned]
        scope_entries += [ScopeEntry(c.checkpoint.checkpoint_id, c.checkpoint.workspace_id, c.checkpoint.persona,
                                     c.checkpoint.tier, c.checkpoint.data_mode,
                                     flow_class="standard" if c.checkpoint.test_kind == "flow" else None)
                          for c in plan.cuts if c.checkpoint is not None]
        shards = [ShardPlan(shard_of[r], r, scope_count=sum(1 for cp in planned if rig_of[cp.checkpoint_id] == r))
                  for r in used]
        mtext = (cfg.root / "manifests" / f"{scenario.candidate.build_id}.yaml")
        store.begin_run(_run_plan(scenario.run_id, "pr", cfg.root, scope_entries, shards, started,
                                  mtext.read_text() if mtext.exists() else None, scenario.pr, scenario.author))
        work = _ShardWork(run, cfg, scenario, out, oracle, store, planned, plan, today)
        rev = reviewer or SimulatedReviewer()
        items = ItemBuilder(scenario.run_id, arts, REDACTION, _EvalIndex(work), manifest=manifest, epoch=run.epoch,
                            flow_docs=cfg.flow_docs, pad=(live.config if live else ReviewerConfig()).crop.pad_px)
        if reviewer is None and live is not None:          # 3.12: the real Reviewer; crops from the artifact store
            rev = ModelReviewer(live, items)
        if isinstance(rev, CollectingReviewer):
            rev.item_for = items
        # -- 5.2: every shard through the Orchestrator; 5.17 per checkpoint inside `running`
        work.run_shards([(shard_of[r], i, r, [cp for cp in planned if rig_of[cp.checkpoint_id] == r])
                         for i, r in enumerate(used, 1)])
        evals = work.evals
        shift = global_shift_prepass([ev.diff.signal for ev in evals if ev.diff is not None and ev.diff.signal is not None],
                                     params=run.params)
        if shift.raised:                       # 3.7 granularity 0: one needs_human (global_shift) cluster
            for ev in evals:
                if ev.cp.checkpoint_id in shift.collapse:
                    ev.facts = dataclasses.replace(ev.facts, global_shift=True)
                    ev.classified = classify(ev.facts)
            gf = shift.finding(scenario.run_id)
            if gf is not None:
                store.write_finding(dataclasses.replace(gf, created_at=started))
        calls = _review_pass(run, evals, rev, scenario.reviewer_cap)
        for f in plan.findings:
            store.write_finding(dataclasses.replace(f, created_at=started))
        for n in run.expiry_notices:
            store.write_finding(Finding(f"fnd-{scenario.run_id}-expiry-{n.record_id}", scenario.run_id, n.kind,
                                        f"{n.kind}: {n.record_id} on {n.region_id} stopped applying at run start",
                                        scenario.run_id, created_at=started))
        for ev in evals:
            o, fs, stored = writer.records(ev)
            steps = []
            if ev.cp.test_kind == "flow":
                steps = [StepOutcome(scenario.run_id, ev.cp.flow_id or "", ev.cp.step_id or "", "passed",
                                     locator_rung="image", duration_ms=ev.settle_ms or 750,
                                     recorded_at=_ts(today, scenario.clock, 600))]
            store.write_checkpoint(o, findings=fs, artifacts=stored, shard_id=shard_of[rig_of[ev.cp.checkpoint_id]],
                                   steps=steps)
        by_cp = {}
        for f in work.findings:
            by_cp.setdefault(f.checkpoint_id, []).append(f)
        for cid, (cp, oc) in sorted(work.bare.items()):
            store.write_checkpoint(work.bare_outcome(cp, oc), findings=by_cp.pop(cid, []),
                                   shard_id=shard_of[rig_of[cid]])
        for fs in by_cp.values():
            for f in fs:
                store.write_finding(f)
        for s, sid in work.steps:                         # each with its gateway_event (8.3 gate_* columns)
            store.write_steps([s], shard_id=sid, side="candidate")
        for c in plan.cuts:
            if c.checkpoint is not None:
                store.write_checkpoint(dataclasses.replace(c.outcome(scenario.run_id), recorded_at=started))
        verdict = work.verdict()
        stop = work.stop()
        reported = {st.shard_id: rep for st, rep in work.reports}
        for sp in shards:
            v = reported[sp.shard_id].verdict
            store.end_shard(sp.shard_id, v.status, v.reason)
            store.mark_shard_imported(sp.shard_id)
        arts.write_manifest(ManifestHeader(config_commit=tree_sha256(cfg.root)[:12], harness_commit="sentinel-core-0.1.0",
                                           run_id=scenario.run_id, created_at=started))
        wall = GATES_S + max(sum(it.rig_cost_s for it in plan.items if r in it.rigs) for r in used) + \
            8 * sum(ev.recaptures for ev in evals) + TEARDOWN_S
        status, reason = verdict.status, verdict.reason
        if status == "complete" and store.complete_blockers(scenario.run_id):
            status, reason = "complete_with_env_errors", None
        last_gate = stop[1].gate if stop is not None and stop[1].gate else "reported"
        runrec = store.end_run(RunOutcome(scenario.run_id, "pr", status, last_gate, reason=reason, started_at=started,
                                          ended_at=_ts(today, scenario.clock, int(wall))))
        abort = {}
        if stop is not None and runrec.status == "aborted_safety":
            st, r = stop
            stopped = [s.rig_id for s, rep in work.reports if rep.verdict.status == "aborted_safety"]
            abort = {"trigger": r.run_reason, "gate": r.gate, "proof": work.proof(), "incident": r.incident or "unknown",
                     "rig": stopped or [st.rig_id]}                    # every rig the abort disabled (6.6 step 6)
        result = _report(store, cfg, plan, run, scenario, runrec, manifest, mdoc, out, today, wall, len(work.reports),
                         safety=work.safety(), abort=abort)
        result.reviewer_calls = calls
        n_pairs = sum(1 for ev in evals if ev.diff is not None and ev.diff.signal is not None)
        result.notes.append(
            f"global-shift pre-pass (3.7) over {n_pairs} diffed pairs: "
            + ("not evaluated (it needs at least 20 pairs)" if not shift.evaluated else
               f"{'raised' if shift.raised else 'not raised'} ({shift.fraction_above_floor:.1%} of pairs above floor, "
               f"{shift.text_edge_share:.1%} of their changed pixels on text edges; it is raised only when more than "
               "2 percent of pairs are above floor and more than 70 percent of their changed pixels are on text "
               "edges)"))
        result.disabled_invariants = run.disabled
        result.alerts, result.shards, result.proof = work.alerts, work.summaries, work.proof()
        work.write()
        (out / "reviewer-calls.json").write_text(json.dumps(calls, indent=1, sort_keys=True, default=str) + "\n")
        return result


def _review_pass(run: _Run, evals: list[Evaluated], rev: ReviewerAdapter, cap: int) -> list[dict]:
    """5.11, 9.7: one Reviewer call per report cluster that needs one, in cluster order, up to `cap`."""
    members = []
    by_id: dict[str, Evaluated] = {}
    for ev in evals:
        c = ev.classified
        if c.outcome not in LISTED and c.decision_ref is None:
            continue
        rec = CheckpointOutcome(run_id=run.run_id, checkpoint_id=ev.cp.checkpoint_id, workspace_id=ev.cp.workspace_id,
                                persona=ev.cp.persona, data_mode=ev.cp.data_mode, tier=ev.cp.tier,
                                diff=ev.diff.summary if ev.diff and ev.diff.terminal is None else None, **c.fields())
        f = Finding("pre", run.run_id, "collateral", "", run.run_id, checkpoint_id=ev.cp.checkpoint_id,
                    region_id=ev.region, phash=ev.phash, category=ev.category)
        members.append(Member(rec, f, ev.category, 0, ev.region, c.decision_ref))
        by_id[ev.cp.checkpoint_id] = ev
    calls: list[dict] = []
    for cl in cluster_members(members):
        evs = [by_id[m.checkpoint_id] for m in cl.unsuppressed]
        if not evs:
            continue
        rows = {ev.classified.row for ev in evs}
        if rows & (REVIEWER_ROWS - {"24", "25", "25a"}):
            task = "manifest_match"
        elif all(ev.classified.outcome == "collateral" for ev in evs):
            task = "describe_collateral"
        else:
            continue
        if len(calls) >= cap:
            if task == "manifest_match":
                for ev in evs:
                    _with_review(ev, Review("capped", cause="call_cap"))
            continue
        ordered = sorted(evs, key=lambda e: (_area(e), e.cp.checkpoint_id))
        rep = ordered[0]
        sizes = tuple(f"{dict(e.cp.size_step)['w']}x{dict(e.cp.size_step)['h']}" for e in (ordered[0], ordered[-1])
                      ) if rep.cp.test_kind == "sweep" and len(ordered) > 1 else ()
        tickets = run.cand_app.build.tickets_for(rep.cp.surface_id) - run.base_app.build.tickets_for(rep.cp.surface_id)
        obs = observe(rep.base.screen, rep.cand.screen, tickets) if rep.base else []
        entry = None
        if run.manifest is not None and task == "manifest_match":
            e = run.manifest.entry_declaring(rep.cp.surface_id)
            entry = {"entry_id": e.entry_id, "tickets": list(e.tickets), "summary": e.summary,
                     "expected_visual": e.expected_visual} if e else None
        att = run.plan.attribution.get(rep.cp.surface_id) if run.plan else None
        res = rep.diff
        req = ReviewRequest(
            task=task, checkpoint_ids=tuple(e.cp.checkpoint_id for e in ordered), surface_id=rep.cp.surface_id,
            region_id=rep.region, tier=rep.cp.tier, data_mode=rep.cp.data_mode, workspace_id=rep.cp.workspace_id,
            diff={"changed_px": sum(m.changed_px for m in res.regions) if res else 0,
                  "shift": list(res.summary.shift) if res else [0, 0],
                  "components": [list(c.bbox) for c in (res.summary.components if res else [])][:20]},
            observations=tuple(obs), crop=rep.crop or (0, 0, 1, 1), entry=entry,
            edge={"primitive": att.primitive, "entry_id": att.entry_id} if att and task == "describe_collateral" else None,
            sizes=sizes, ladder=any(d.kind == "order_strip" for d in rep.cand.screen.components))
        try:
            answer = rev.review(req)
            problems = validate(answer, req)
            if problems:                  # 3.13: re-sent once, then unavailable (row 23)
                answer = rev.review(req)
                problems = validate(answer, req)
        except ReviewerUnavailable as exc:       # the live adapter gave up on the item (3.11 row 23)
            for ev in evs:
                _with_review(ev, exc.review())
            calls.append({"task": task, "checkpoints": list(req.checkpoint_ids), "request_sha256": req.sha256,
                          "model_id": rev.model_id, "prompt_hash": rev.prompt_hash, "answer": None,
                          "unavailable": f"{exc.verdict.state}: {exc.verdict.cause}"})
            continue
        if problems:
            for ev in evs:
                _with_review(ev, Review("unavailable", cause="reviewer_schema_violation"))
            continue
        review = to_review(answer, rev, rep.region)
        for ev in evs:
            ev.answer = answer
            _with_review(ev, review)
        calls.append({"task": task, "checkpoints": list(req.checkpoint_ids), "request_sha256": req.sha256,
                      "model_id": rev.model_id, "prompt_hash": rev.prompt_hash, "answer": answer})
    return calls


def _area(ev: Evaluated) -> tuple:
    step = dict(ev.cp.size_step or {})
    return (int(step.get("w", 0)) * int(step.get("h", 0)), int(step.get("w", 0)))


def _report(store: Store, cfg: SentinelConfig, plan: ScopePlan, run: _Run, scenario: Scenario, runrec: RunOutcome,
            manifest: Manifest | None, mdoc: dict | None, out: Path, today: date, wall: float, shards: int, *,
            safety: dict, abort: dict) -> RunResult:
    """Read the run back from the store and render the PR comment, commit status and report (9.2, 9.5).
    `safety` is run health's safety row from the shards' control results; `abort` the abort block's facts."""
    outcomes = store.outcomes(scenario.run_id)
    findings = store.findings(scenario.run_id)
    steps = store.step_outcomes(scenario.run_id)
    ids = sorted({o.decision_ref for o in outcomes if o.decision_ref} | {f.decision_ref for f in findings if f.decision_ref})
    decisions = [d for d in (store.get_decision(i) for i in ids) if d is not None]
    coverage = coverage_summary(cfg, plan, outcomes, scenario.run_id)
    health = {
        "wall_clock": _mmss(wall), "budget": "45:00", "shards": shards,
        "fingerprint": {"candidate": run.fp_id[7:13], "base": run.fp_id[7:13]},
        "reviewer": {"cost_usd": 0.0},
        "safety": safety,
        "integrations": {"pr_comment": {"state": "posted", "time": "(simulated: written to pr-comment.md)"},
                         "jira_queued": 0, "slack": {"queued": 0}},
    }
    if scenario.canary:
        health["canary"] = dict(scenario.canary)
    ctx = ReportContext(pr=scenario.pr or 0, candidate_build=scenario.candidate.build_id,
                        base_build=scenario.base.build_id, detector_banner=scenario.detector_banner,
                        history=scenario.history, abort=abort,
                        provenance={"harness_commit": "sentinel-core-0.1.0 (simulated run)",
                                    "config_commit": tree_sha256(cfg.root)[:12],
                                    "vm_image_id": "none: synthetic screens from sentinel.synth"})
    mdict = as_dict(mdoc) if mdoc else None
    comment = render_pr_comment(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, steps=steps)
    (out / "pr-comment.md").write_text(comment, encoding="utf-8")
    rep = build_report(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, steps)
    status = _status_json(rep.status)
    (out / "commit-status.json").write_text(json.dumps(status, indent=2) + "\n")
    render_run_report(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, out / "report.html",
                      steps=steps, objects_dir=out / "artifacts" / "objects", embed=True)
    return RunResult(scenario.run_id, "pr", runrec.status, runrec.reason, out, outcomes, findings, decisions, comment,
                     status, plan, mdict)


# ------------------------------------------------------------------------------------------------
# Decisions (4.14, 9.7): what a human clicks in the report UI


def record_decisions(store: Store, result: RunResult, select: Callable[[CheckpointOutcome], bool], *, action: str,
                     decided_by: str, reason: str, ticket: str | None = None, confirmed_by: str | None = None,
                     proposed_by: str | None = None,
                     entry: str | None = None, cluster_id: str | None = None, at: str | None = None) -> list[Decision]:
    """Write one decision per member signature of the selected checkpoints (9.7: a cluster decision is
    written per member signature with the cluster id). Returns the decisions as stored."""
    from ..records import ACTION_VERDICT
    primary = {f.checkpoint_id: f for f in result.findings if f.finding_id.startswith("F-")}
    out = []
    for o in sorted(result.outcomes, key=lambda o: o.checkpoint_id):
        f = primary.get(o.checkpoint_id)
        if not select(o) or f is None or not f.phash or not f.region_id:
            continue
        nxt = store.conn.execute("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions").fetchone()[0]
        d = Decision(f"dec-{max(nxt, 900):06d}", action, ACTION_VERDICT[action],
                     Signature(o.checkpoint_id, f.region_id, f.phash, f.category or f.kind), decided_by=decided_by,
                     ticket=ticket, manifest_entry_ref=entry, reason=reason, run_id=result.run_id,
                     source="pr_comment", decided_at=at or o.recorded_at)
        out.append(store.record_decision(d, run_id=result.run_id, finding_id=f.finding_id, confirmed_by=confirmed_by,
                                         proposed_by=proposed_by, cluster_id=cluster_id))
    return out


# ------------------------------------------------------------------------------------------------
# The Nightly canary pass (10.3)


@dataclass(frozen=True)
class NightlyScenario:
    """One Nightly run's canary pass on the main build."""

    name: str
    run_id: str
    build: SimBuild
    overlay: Mapping[str, str] = field(default_factory=dict)
    mutants: tuple[str, ...] = ()                     # catalog ids simulated (the rest listed as not simulated)
    clock: str = "03:12:00"
    history_nights: int = 14


SIMULATED_MUTANTS = ("M01", "M02", "M03", "M04", "M05", "M06", "M08", "M09", "M12", "M14", "M18", "M20", "M22",
                     "M24", "M25", "M26", "M27", "M28", "M29", "M30")
NOT_SIMULATED = {
    "M07": "color_swap expects INV-15; the registry declares no signed P/L column and palette for the synthetic app",
    "M10": "clipped_label needs OCR to read the cropped header word; the synthetic OCR reads drawn text, not pixels",
    "M11": "clipped_label (ellipsis) needs OCR of the pixels, as M10",
    "M13": "missing_row expects an Oracle assertion (R08); the simulated Oracle reads what synth drew, not a mutant's "
           "pixels, so the Nightly pass runs without it",
    "M15": "placeholder lands on a rule region asserted by the Oracle, which reads drawn text, not a mutant's pixels",
    "M16": "font_height needs OCR cap heights from the pixels",
    "M17": "scrollbar needs a template match of the drawn thumb",
    "M19": "redaction is proven by the redaction stage and the egress log, which the simulation has no rig for",
    "M21": "global_shift is judged by the run-level pre-pass over at least 20 pairs of one run",
    "M23": "global_shift, as M21",
    "M31": "misaligned_column expects INV-12; the registry declares no grid columns for the synthetic app",
    "M32": "focus_order needs the Tab focus trace of a keyboard-only flow",
    "M33": "error_dialog needs the popup catalog's error title patterns and the dialog as a window",
}


def planned_for(cfg: SentinelConfig, cid: str) -> PlannedCheckpoint:
    """A PlannedCheckpoint for a checkpoint id of the config (the canary catalog binds ids, not plans)."""
    p = checkpoint_parts(cid)
    kind, surface, q = p["kind"], p["surface"], p["qualifier"].split("/")
    s = cfg.surfaces[surface]
    tier, mode = int(s["tier"]), str(s.get("data_mode", "frozen"))
    if kind == "flow":
        flow = cfg.flow_docs[q[0]]
        step = cfg.flow_step(q[0], q[1])
        return PlannedCheckpoint(cid, surface, "flow", int(step.get("tier", tier)), flow["workspace"], flow["persona"],
                                 str(step.get("data_mode", flow.get("data_mode", mode))), flow_id=q[0], step_id=q[1])
    if kind == "sweep":
        w, h = q[1].split("x")
        return PlannedCheckpoint(cid, surface, "sweep", tier, "ws.tier-c.sweep-harness", s["personas"][0], mode,
                                 size_step=(("host", q[0]), ("w", int(w)), ("h", int(h))))
    ws = next(w for w in cfg.workspaces if w.id == q[0])
    return PlannedCheckpoint(cid, surface, "census", tier, ws.id, ws.persona or s["personas"][0], mode)


def _mutant_model(model: inv.ScreenModel, m: cn.Mutant, boxes: Mapping[str, Sequence[int]]) -> inv.ScreenModel:
    """What the bounds source reads on a mutant (best effort per op, module `sim` docstring): erased
    boxes lose their controls and words; translated boxes move them; a moved window moves."""
    e = m.entry
    if m.defect_class == "window_offscreen":
        x = int(e.params.get("x", -200))
        w = model.windows[0]
        return dataclasses.replace(model, windows=(dataclasses.replace(w, rect=inv.Rect(x, w.rect.y, w.rect.w, w.rect.h)),))
    box = boxes.get(e.region_ref or "")
    if box is None:
        return model
    bx, by, bw, bh = box
    inside = lambda r: bx <= r.center[0] < bx + bw and by <= r.center[1] < by + bh  # noqa: E731
    if e.op == "erase" and m.defect_class in ("erased_control", "landmark_loss"):
        return dataclasses.replace(model, components=tuple(c for c in model.components if c.kind != "control"
                                                           or not inside(c.bbox)),
                                   words=tuple(w for w in model.words if not inside(w.bbox)))
    if e.op == "translate" and m.defect_class in ("shifted_panel", "overlapped_panels", "outside_parent"):
        dx, dy = int(e.params.get("dx", 0)), int(e.params.get("dy", 0))
        if m.defect_class == "outside_parent" and not (dx or dy):
            dy = int(e.params.get("distance", 6))
        src = boxes.get(str(e.params.get("layer"))) if e.params.get("layer") else box
        sx, sy, sw, sh = src
        def moved(r: inv.Rect) -> inv.Rect:
            if sx <= r.center[0] < sx + sw and sy <= r.center[1] < sy + sh:
                return inv.Rect(r.x + dx, r.y + dy, r.w, r.h)
            return r
        comps = []
        for c in model.components:
            r = moved(c.bbox)
            if r == c.bbox and c.kind == "panel" and (c.bbox.x, c.bbox.y, c.bbox.w, c.bbox.h) == tuple(src):
                r = inv.Rect(r.x + dx, r.y + dy, r.w, r.h)
            comps.append(dataclasses.replace(c, bbox=r))
        return dataclasses.replace(model, components=tuple(comps),
                                   words=tuple(dataclasses.replace(w, bbox=moved(w.bbox)) for w in model.words))
    return model


def _static_capture(cid: str, img: np.ndarray, model: inv.ScreenModel, template: Capture) -> Capture:
    from ..diff.settle import Frame
    import copy
    screen = copy.copy(template.screen)
    screen.image = img
    frames = [Frame(k * 250, img, texts=template.frames[0].texts) for k in range(3)]
    return Capture(cid, "candidate", frames, screen, model, template.info)


def run_nightly(config_dir: str | Path, scenario: NightlyScenario, out_dir: str | Path, today: date = DEFAULT_TODAY,
                history: Mapping[str, Sequence[float]] | None = None) -> RunResult:
    """The canary pass of one Nightly run (module docstring). Writes `canary-results.json`,
    `morning-report.txt`, the run's rows in `sentinel.db` and its artifacts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _ts(today, scenario.clock)
    now = parse_time(started)
    cfg_root = materialize(config_dir, scenario.overlay, out / "config" / scenario.run_id)
    lrep = lint(cfg_root, phase=1, today=today)
    with Store(out / "sentinel.db") as store:
        if lrep.errors:
            sc = Scenario(scenario.name, scenario.run_id, scenario.build, scenario.build, run_type="nightly",
                          clock=scenario.clock)
            return _abort_config(store, sc, cfg_root, [v.line() for v in lrep.errors],
                                 sorted({v.rule for v in lrep.errors}), out, today, started)
        cfg = SentinelConfig.load(cfg_root)
        assert cfg.catalog is not None
        wanted = set(scenario.mutants or SIMULATED_MUTANTS)
        entries = [e for e in cfg.catalog.entries if e.mutant_id in wanted]
        run = _Run(cfg, scenario.run_id, "nightly", scenario.build, scenario.build, now, None, None,
                   lambda s: "undeclared")
        bound = sorted({e.checkpoint_id for e in entries} | {str(e.params.get("donor_checkpoint")) for e in entries
                                                              if e.params.get("donor_checkpoint")})
        cps = {cid: planned_for(cfg, cid) for cid in bound}
        work = _ShardWork(run, cfg, Scenario(scenario.name, scenario.run_id, scenario.build, scenario.build,
                                             run_type="nightly", clock=scenario.clock), out, None, store, (), None,
                          today)
        clock = ManualClock(start=now.astimezone(timezone.utc))
        ctx, sim = shard_setup(cfg, run_id=scenario.run_id, run_type="nightly", shard_id=f"{scenario.run_id}/1",
                               shard_no=1, build_id=scenario.build.build_id, workspace_id="ws.tier-a.swing-trader",
                               script=RigScript())
        orch = Orchestrator(SimulatedRig(sim, clock=clock), ctx, ShardSink(()),
                            clock=clock, heartbeat=HeartbeatWriter(out / "heartbeat" / f"{scenario.run_id}.json", clock),
                            watchdog_status=lambda: (1.0, True))
        done: dict[str, Any] = {}

        def canary_pass(_: Orchestrator) -> None:
            """The Nightly run's work in `running` (5.2): the canary pass of 10.3."""
            run.budget = RerunBudget(len(cps), run.params)
            run.calibrate(list(cps.values()), out)
            approved: dict[str, Capture] = {cid: run.capture(cp, "base") for cid, cp in cps.items()}
            boxes: dict[str, dict[str, tuple[int, int, int, int]]] = {}
            for cid, cp in cps.items():
                reg = run.registry(cp, approved[cid])
                lm = {l.id: l.expected for l in reg.landmarks}
                boxes[cid] = {r.id: (r.rect.x + lm[r.landmark][0], r.rect.y + lm[r.landmark][1], r.rect.w, r.rect.h)
                              for r in reg.regions if lm.get(r.landmark) is not None}
            gen = cn.generate(entries, {cid: c.screen.image for cid, c in approved.items()}, boxes)
            results: list[cn.MutantResult] = []
            produced: dict[str, CheckpointOutcome] = {}
            writer = _Writer(run, ArtifactStore(out / "artifacts", f"runs/{scenario.run_id}"), today, scenario.clock)
            rows_meta = []
            arts_by_sha: dict[str, StoredArtifact] = {}
            for m in gen.mutants:
                cp = cps[m.checkpoint_id]
                if m.failed or m.image is None:
                    results.append(cn.MutantResult(m))
                    continue
                base_cap = approved[m.checkpoint_id]
                model = _mutant_model(base_cap.model, m, boxes[m.checkpoint_id])
                image = m.image
                if m.defect_class == "window_offscreen":
                    image = base_cap.screen.image    # Capture grabs the window by its DWM bounds (5.7): same pixels
                mutant_cap = _static_capture(m.checkpoint_id, image, model, base_cap)
                ev = evaluate(run, cp, base_cap, mutant_cap, x="undeclared", primitive=None, entry_ref=None,
                              recapture=lambda c=mutant_cap: c, recapturable=_recapturable(cfg, cp), synthetic=True,
                              injected=True)
                rec, _, stored = writer.records(ev, synthetic=True, tag=m.mutant_id)
                arts_by_sha.update({a.sha256: a for a in stored})
                step_status = None
                if m.defect_class == "gateway":
                    step_status = "blocked_by_gateway"       # the simulated gateway refuses the unmatched Send template
                absorbed = None
                if ev.classified.outcome in ("identical", "noise"):
                    masks = [f"mask {r.mask_ref} (region {r.id})" for r in ev.registry.regions if r.type == "mask"]
                    absorbed = ", ".join(masks) if masks else None
                results.append(cn.MutantResult(m, rec, absorbed_by=absorbed, step_status=step_status))
                produced[m.key] = rec
                rows_meta.append((m, ev))
            report = cn.score(results, history=history or {}, since=f"{today.isoformat()} {scenario.clock[:5]} ET",
                              context=cn.RowContext(scenario.run_id, "ws.tier-a.swing-trader", "swing_trader", "frozen"))
            done.update(report=report, rows_meta=rows_meta, produced=produced, writer=writer, arts_by_sha=arts_by_sha)

        shard = orch.run(canary_pass)
        st = _Shard(ctx.shard_id, cfg.fixture.get("rig", "rig-01"), [], orch, orch.rig, orch.sink, clock, ctx)
        for r in shard.results:
            work._note(st, r)
        work.summaries.append({"shard_id": ctx.shard_id, "rig": st.rig_id, "verdict": shard.verdict.status,
                               "reason": shard.verdict.reason, "last_gate": shard.last_gate,
                               "proof": shard.proof.line,
                               "gates": [{"control": r.control, "gate": r.gate, "passed": r.passed, "detail": r.detail}
                                         for r in shard.results]})
        work.write()
        if "report" not in done:                 # a gate stopped the Nightly run before the canary pass
            _register(store, cfg, [scenario.build.build_id])
            store.begin_run(_run_plan(scenario.run_id, "nightly", cfg.root, (), (), started, None, None, None))
            runrec = store.end_run(RunOutcome(scenario.run_id, "nightly", shard.verdict.status, shard.last_gate,
                                              reason=shard.verdict.reason, started_at=started,
                                              ended_at=_ts(today, scenario.clock, 600)))
            return RunResult(scenario.run_id, "nightly", runrec.status, runrec.reason, out, alerts=work.alerts,
                             shards=work.summaries, proof=shard.proof.line)
        report, rows_meta = done["report"], done["rows_meta"]
        produced, writer, arts_by_sha = done["produced"], done["writer"], done["arts_by_sha"]
        # -- store: identity, the run, synthetic rows (one per checkpoint at most, run docstring)
        _register(store, cfg, [scenario.build.build_id])
        store.register_checkpoints([ScopeEntry(cp.checkpoint_id, cp.workspace_id, cp.persona, cp.tier, cp.data_mode,
                                               flow_class=_flow_class(cfg, cp)) for cp in cps.values()],
                                   config_commit=tree_sha256(cfg.root)[:12])
        store.begin_run(_run_plan(scenario.run_id, "nightly", cfg.root, (), (), started, None, None, None))
        written: set[str] = set()
        for row in report.outcome_rows:
            if row.checkpoint_id in written:
                continue
            written.add(row.checkpoint_id)
            named = [arts_by_sha[h] for h in (row.artifacts or {}).values() if isinstance(h, str) and h in arts_by_sha]
            store.write_checkpoint(dataclasses.replace(row, recorded_at=_ts(today, scenario.clock, 1800)), artifacts=named)
        for r in report.results:
            if r.state in ("detector_blind", "detector_degraded", "generation_failed"):
                store.write_finding(Finding(
                    f"fnd-{scenario.run_id}-canary-{r.mutant_id}", scenario.run_id, "canary_miss",
                    f"{r.mutant_id} {r.defect_class} {r.state}: produced {r.produced_outcome or 'nothing'}"
                    f"{' (' + r.produced_reason + ')' if r.produced_reason else ''}; absorbed by "
                    f"{r.absorbed_by or 'not recorded'}", scenario.run_id, checkpoint_id=r.checkpoint_id, tier=1,
                    evidence={"raw_values": {"state": r.state, "money_cell": str(r.money_cell)}},
                    created_at=_ts(today, scenario.clock, 1800)))
        runrec = store.end_run(RunOutcome(scenario.run_id, "nightly", shard.verdict.status, "reported",
                                          reason=shard.verdict.reason, started_at=started,
                                          ended_at=_ts(today, scenario.clock, 2 * 3600)))
        money = [r for r in report.results if r.money_cell and r.state != "generation_failed"]
        other = [r for r in report.results if not r.money_cell and r.state != "generation_failed"]
        canary_health = {"money_detected": sum(r.state == "detected" for r in money), "money_total": len(money),
                         "other_detected": sum(r.state == "detected" for r in other), "other_total": len(other),
                         "detector_state": report.state, "date": today.isoformat(),
                         "since": f"{today.isoformat()} {scenario.clock[:5]} ET"}
        body = {"run_id": scenario.run_id, "detector_state": report.state, "banner": report.banner,
                "catalog_version": cfg.catalog.version, "simulated": sorted(wanted),
                "not_simulated": {k: v for k, v in NOT_SIMULATED.items() if k not in wanted},
                "results": [r.to_dict() for r in report.results],
                "class_rates": report.class_rates, "class_floors": report.class_floors}
        (out / f"canary-results-{scenario.run_id}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        writer.arts.put(json.dumps(body, sort_keys=True).encode(), name=f"{scenario.run_id}/canary-results.json",
                        kind="canary_results", media_type="application/json")
        writer.arts.write_manifest(ManifestHeader(config_commit=tree_sha256(cfg.root)[:12],
                                                  harness_commit="sentinel-core-0.1.0", run_id=scenario.run_id,
                                                  created_at=started))
        ctx = ReportContext(pr=0, candidate_build=scenario.build.build_id, base_build=scenario.build.build_id,
                            detector_banner=report.banner)
        health = {"wall_clock": "2:41:00", "budget": "4:00:00", "shards": 1, "canary": canary_health,
                  "fingerprint": {"candidate": run.fp_id[7:13], "base": run.fp_id[7:13]},
                  "integrations": {"pr_comment": {"state": "unposted", "time": "n/a (Nightly run)"}, "jira_queued":
                                   1 if report.state != "ok" else 0, "slack": {"queued": 0}}}
        outcomes = store.outcomes(scenario.run_id)
        render_run_report(runrec, outcomes, store.findings(scenario.run_id), [], {}, health, None, ctx,
                          out / f"nightly-report-{scenario.run_id}.html")
        res = RunResult(scenario.run_id, "nightly", runrec.status, runrec.reason, out, outcomes,
                        store.findings(scenario.run_id), alerts=work.alerts, shards=work.summaries,
                        proof=shard.proof.line)
        res.canary = {"report": report, "health": canary_health, "produced": produced, "evals": rows_meta,
                      "simulated": sorted(wanted)}
        return res
