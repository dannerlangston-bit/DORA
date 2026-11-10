"""The Reporter's view of one run, shared by the PR comment and the static HTML report.

The Reporter never computes or changes an outcome (9 preamble); this module only arranges the
records of 4.13 and 4.14 into what 9.2 shows: Unexpected items (clustered per 9.7 and ranked
per 9.2), Intended items, the Suppressed table, Coverage, Run health (9.11 counters) and the
commit status of `sentinel/pr-run`.

Inputs beyond the records, and where each comes from:

- `ReportContext`: PR number, build pair, report UI base URL, check mode and the run-level
  facts 9.2 prints that no record holds (abort details, banners, history, overrides).
- `coverage`: the PR run's matrix summary from `runs.summary_json` (2.12): per tier
  `registered`, `in_scope`, `covered`, `not_compared` {outcome: n} and `not_run` {reason: n};
  `scope` {declared_surfaces, edges, primitives, static_impact_added, smoke_census, send_policy,
  order_surfaces_entitlement}; optional `flow_tasks` {entry_id: task key} and `cells` (matrix).
- `health`: the run-health facts not derivable from outcome rows (8.3 indexes, 9.11):
  `wall_clock`, `budget`, `shards`, `fingerprint` {candidate, base}, `reviewer` {cost_usd, tier,
  top_tier_usd}, `canary` {money_detected, money_total, other_detected, other_total,
  detector_state, date}, `safety` {attestation, paper_mode_asserted, gateway_drops, cancel_all,
  proof, login}, `integrations` {pr_comment {state, time}, jira_queued, slack {queued, to, posts}},
  optional `env_errors` [{gate, shard, screenshot}], `shard_rerun_queued`, `steps` (counts).
  Counts that outcome rows determine (env and harness errors, not settled, re-runs, Reviewer
  calls, layout shift) are computed here, never read from `health`.
- `Finding.evidence` keys the Reporter reads (all optional, all rendered as data):
  `target_window`, `layout` {grid, cell}, `overlay` (evidence note), `violation` (short phrase
  for the re-run line), `reviewer` {verdict, change_types, pending_since, batch},
  `raw_values` {expected, read, source} (4.13), `near_decisions` [{decision_id, hamming}],
  `conflicting_decisions` [{decision_id, verdict}], `side`, `pre_screenshot`, `base_screenshot`.

Choices where 9.2 is silent (reported to the coordinator):

- Ranking ties after novelty break on cluster size (larger first), then checkpoint id. 9.3's
  item 2 (8-step sweep cluster) therefore precedes item 3 (one census checkpoint).
- Novelty (signature unseen in 30 days) is read as `first_seen_run_id == run_id`.
- In a PR comment a finding that inherited any decision, `confirm_bug` included, is listed under
  Suppressed (a PR comment has no Known bugs section; 9.4 has one for merge and RC reports).
- Prose repeats nothing an earlier item already said, as 9.3 does: a workspace is introduced
  with the word "workspace" once, a grid layout once per workspace, an undeclared surface's
  manifest explanation once ("same undeclared surface, ..." afterwards), an entry's summary once
  and a Reviewer model id and prompt hash once.
- The sweep range follows 2.5.2 ("the Reporter names the first failing size and the failing
  range"): for invariant and assertion clusters "Fails at ... (k of N Ladder S steps); first
  failing step in ladder order ...; shown at ...", otherwise "Heights ... at ... (k of N
  steps)", as in 9.3. N counts this run's sweep checkpoints of the surface; ladder order is
  descending by width then height (2.5.2); Ladder F for RC runs, Ladder S otherwise (2.5.3).
- Comparable scope for the coverage percent is in-scope cells minus `not_run (entitlement)`
  minus not-compared (blocked) cells (2.12: blocked cells are not in the coverage percent).
- `aborted_safety` and `aborted_harness` replace Unexpected and Intended with the abort block
  and keep Suppressed, Coverage and Run health; `pool_saturated` and `dead_man` comments carry
  only the header and the abort text; a secret leak carries only the header, result, check and
  "report withheld; security contact notified".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

from ..records import CheckpointOutcome, Decision, Finding, RunOutcome, StepOutcome
from ..taxonomy import TaxonomyError, checkpoint_parts, load
from .cluster import Cluster, Member, cluster_members
from .inline import (C, Image, Line, Link, T, Text, Urls, is_artifact_hash, join_and,
                     number_word, plain, plural, words_and)

CHECK_CONTEXT = "sentinel/pr-run"
CHECK_MODES = ("informational", "blocking")

# 9.2 Unexpected kind order (rank groups).
(APP_ALIVE, INVARIANT, ASSERTION, COLLATERAL, NO_MATCH, LANDMARK, UNREGISTERED, NEEDS_HUMAN,
 NEEDS_BASELINE, STEP_FAILED, AGENT_FALLBACK, MAP_GAP, MANIFEST_MISSING, UNKNOWN_POPUP) = range(14)
FAILURE_GROUPS = (APP_ALIVE, INVARIANT, ASSERTION)
# 9.2 commit status: tier-1 kinds that fail the check when undecided.
CHECK_FAILING_GROUPS = {APP_ALIVE, INVARIANT, ASSERTION, COLLATERAL, NO_MATCH, LANDMARK, NEEDS_HUMAN,
                        STEP_FAILED}
FINDING_ONLY = {"dependency_map_gap": MAP_GAP, "manifest_missing": MANIFEST_MISSING,
                "unknown_popup": UNKNOWN_POPUP}
NOT_RUN_ORDER = ("scope", "budget", "entitlement", "quarantine", "step_failed", "base_step_failed",
                 "app_crash", "model_unavailable")
NOT_COMPARED = ("env_error", "not_settled", "not_comparable", "landmark_missing", "not_run")

# 2.7 Layout invariants, "Statement" column.
INVARIANT_STATEMENTS = {
    "INV-01": "No two sibling components' bounds intersect",
    "INV-02": "No component extends outside its parent",
    "INV-03": "No text clipped at a boundary",
    "INV-04": "No ellipsis where the base had none",
    "INV-05": "Required labels present",
    "INV-06": "No unexpected scrollbar",
    "INV-07": "Minimum legible font height",
    "INV-08": "No placeholder, loading or error text in a value region",
    "INV-09": "Every top-level window is inside the virtual screen",
    "INV-10": "Every required control present exactly once",
    "INV-11": "No two text runs overlap",
    "INV-12": "Grid columns aligned",
    "INV-13": "Focus traversal covers declared controls in order",
    "INV-14": "No error dialog or exception window present at capture",
    "INV-15": "Sign and color agree in P/L and change columns",
}
# 3.13 observed_changes[].change_type
CHANGE_TYPES = ("overlap", "missing_element", "added_element", "clipped_text", "shifted", "resized",
                "text_change", "color_change", "color_semantic_flip", "other")
REVIEWER_VERDICTS = tuple(load().list("reviewer_verdicts"))
# 1.10 needs_human reasons, as the "Why unexpected" line says them.
NEEDS_HUMAN_WHY = {
    "reviewer_unavailable": "Declared surface; the Reviewer was unavailable, so a human decides.",
    "reviewer_cap": "Declared surface; beyond the per-run Reviewer call cap or spend ceiling.",
    "no_manifest": "No manifest and no UI code touched; intent cannot be checked (no_manifest).",
    "uncalibrated": "No calibrated floor for this region or fingerprint, so the threshold is 0 (uncalibrated).",
    "rescued_capture": "Captured after the scripted locator broke (passed_via_agent_fallback); a rescued capture never inherits a decision.",
    "global_shift": "Text-edge-only diff under the run-level global_rendering_shift finding.",
    "oracle_unreadable": "The Oracle could not read the value.",
    "bounds_unreadable": "The Invariant Checker could not read the bounds.",
}
FIELD_EVIDENCE = "Evidence at 1:1"


class ReportInputError(ValueError):
    """The inputs cannot be rendered faithfully (unknown mode, vocabulary or shape)."""


@dataclass(frozen=True)
class ReportContext:
    """Run-level facts 9.2 prints that no outcome record holds."""

    pr: int
    candidate_build: str
    base_build: str
    base_url: str = "https://sentinel.internal"
    check_mode: str = "informational"           # 9.2: informational (Phase 1, default) | blocking
    base_reused_from: str | None = None          # merge run whose frozen base captures were reused (3.1)
    detector_banner: str | None = None           # 10.3 banner text, verbatim
    reviewer_disabled: dict | None = None        # {"since": date, "gate": eval gate name} (10.4)
    abort: dict = field(default_factory=dict)    # abort block facts, see `_abort_block`
    history: tuple[dict, ...] = ()               # earlier runs: {run_id, candidate, result, counts, reported}
    marker_recreated: bool = False               # 9.1: the marker comment was missing; a new one was made
    report_late: bool = False                    # 9.1: posted after the PR run's budget deadline
    comment_overdue: bool = False                # 9.2 error: no comment posted by budget plus 15 minutes
    override: dict | None = None                 # {"by": login, "reason": text}: forced success (9.2, 9.6)
    provenance: dict = field(default_factory=dict)  # 9.5: harness/config commit, VM image id


@dataclass(frozen=True)
class CommitStatus:
    """The `sentinel/pr-run` status of 9.2."""

    state: str               # pending | success | failure | error
    reason: str              # the description text without the mode suffix
    mode: str                # informational | blocking
    target_url: str | None = None
    context: str = CHECK_CONTEXT

    @property
    def description(self) -> str:
        """What is posted; ends "(informational)" in informational mode, capped at GitHub's 140."""
        suffix = " (informational)" if self.mode == "informational" else ""
        room = 140 - len(suffix)
        text = self.reason if len(self.reason) <= room else self.reason[: room - 3] + "..."
        return text + suffix

    @property
    def blocks_merge(self) -> bool:
        """True only in blocking mode, where the context is a required check (9.2)."""
        return self.mode == "blocking" and self.state in ("failure", "error")


@dataclass
class Evidence:
    """Crops at 1:1 and the checkpoint page link (9.1, 9.2)."""

    base: Image | None
    candidate: Image | None
    heatmap: Image | None
    checkpoint_url: str | None
    note: str | None = None


Field = tuple[str, "Line | Evidence"]


@dataclass
class Item:
    """One Unexpected entry: a cluster, a step or a run-level finding (9.2)."""

    rank: int
    tier: int | None
    group: int
    header: Line
    fields: list[Field]
    decided: bool
    pending: bool
    reviewer_call: bool
    cluster: Cluster | None = None
    step: StepOutcome | None = None
    finding: Finding | None = None
    sort_key: tuple = ()


@dataclass
class IntendedItem:
    surface: str
    header: Line
    fields: list[Field]


@dataclass
class SuppressedRow:
    checkpoint_id: str
    region: str | None
    decision_id: str
    decision: Decision | None


@dataclass
class RunReport:
    """Everything a renderer needs; renderers add no facts of their own."""

    run: RunOutcome
    context: ReportContext
    urls: Urls
    status: CommitStatus
    banner: str | None
    banner_lines: list[Line]
    result: str
    special: str | None                      # pool_saturated | dead_man | withheld
    review_line: Line | None
    abort_title: str | None
    abort_lines: list[Line]
    show_findings: bool
    unexpected: list[Item]
    intended: list[IntendedItem]
    manifest_line: Line | None
    reconciliation: Line | None
    needs_baseline_line: Line | None
    suppressed: list[SuppressedRow]
    coverage_rows: list[tuple[str, ...]]
    scope_line: Line
    health_rows: list[tuple[str, Line]]
    history: tuple[dict, ...]
    outcomes: list[CheckpointOutcome]

    @property
    def n_unexpected(self) -> int:
        return len(self.unexpected)

    @property
    def n_intended(self) -> int:
        return len(self.intended)

    @property
    def n_suppressed(self) -> int:
        return len(self.suppressed)


# --------------------------------------------------------------------------------------------
# Members, suppression and rank groups
# --------------------------------------------------------------------------------------------

def _decision_id(o: CheckpointOutcome, f: Finding | None) -> str | None:
    if o.decision_ref:
        return o.decision_ref
    if f is not None and f.decision_ref:
        return f.decision_ref
    if o.reason and o.reason.startswith("decision:"):
        return o.reason.partition(":")[2]
    return None


def _group(category: str, reason: str | None, side: str | None, declared: bool) -> int | None:
    """The 9.2 kind order of a member, None when the category is not listed in Unexpected."""
    code = reason.partition(":")[0] if reason else None
    if category == "assertion_failed":
        return APP_ALIVE if reason == "assertion:app_alive" else ASSERTION
    if category == "invariant_violation":
        return INVARIANT
    if category == "collateral":
        return COLLATERAL
    if category == "needs_human":
        return {"uncalibrated": COLLATERAL, "reviewer_no_match": NO_MATCH}.get(code or "", NEEDS_HUMAN)
    if category == "landmark_missing":
        return LANDMARK if side in (None, "candidate") else None
    if category == "unregistered_surface":
        return UNREGISTERED
    if category == "needs_baseline":
        return None if declared else NEEDS_BASELINE
    return None


def _primary_finding(o: CheckpointOutcome, fs: list[Finding]) -> Finding | None:
    for f in fs:
        if f.kind == o.outcome or (f.category and f.category == o.outcome):
            return f
    for kind in ("invariant_violation", "assertion_failed", o.outcome):
        for f in fs:
            if f.kind == kind:
                return f
    return fs[0] if fs else None


def _region(o: CheckpointOutcome, f: Finding | None) -> str | None:
    if f is not None and f.region_id:
        return f.region_id
    if o.diff:
        for r in o.diff.regions:
            if r.above_threshold:
                return r.region_id
    if o.classification and o.classification.region_pointer:
        return o.classification.region_pointer
    return None


def _member(o: CheckpointOutcome, fs: list[Finding], decisions: dict[str, Decision],
            declared: set[str]) -> Member:
    f = _primary_finding(o, fs)
    did = _decision_id(o, f)
    dec = decisions.get(did) if did else None
    kind = f.kind if f else None
    category, reason = o.outcome, o.reason
    if did and o.outcome in ("noise", "intended"):
        # the decision changed the outcome; the cluster key is the computed category (9.7)
        category = (dec.signature.category if dec else None) or (f.category if f else None) or o.outcome
        reason = None
    elif o.outcome == "not_run" and o.reason == "base_step_failed" and kind in ("invariant_violation",
                                                                                "assertion_failed"):
        category = kind   # 9.2: the candidate capture was still checked (3.1)
    side = (f.evidence.get("side") if f else None) or o.side
    group = _group(category, reason, side, o.surface_id in declared)
    return Member(o, f, category, group, _region(o, f), did, dec)


def outcome_label(m: Member) -> str:
    """The outcome name with its reason, as 1.10 writes it: `outcome (reason: code)`."""
    o = m.outcome
    if m.pending:
        return "needs_human (review_state: pending)"
    if o.outcome == "not_run" and m.category in ("invariant_violation", "assertion_failed"):
        return f"{m.category} (comparison not_run, reason: base_step_failed)"
    if m.suppressed and o.outcome in ("noise", "intended"):
        return m.category
    if o.outcome == "assertion_failed" and o.reason:
        return f"assertion_failed (assertion: {o.reason.partition(':')[2]})"
    if o.outcome == "invariant_violation" or not o.reason or o.reason.startswith("decision:"):
        return o.outcome
    return f"{o.outcome} (reason: {o.reason})"


# --------------------------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------------------------

def _surface_name(surface_id: str) -> str:
    """A surface's short name: the last segment of its id, hyphens as spaces ("chart cell")."""
    return surface_id.rsplit(".", 1)[-1].replace("-", " ")


def _codes(ids: list[str]) -> Line:
    return join_and([(C(i),) for i in ids])


def _size(s: tuple[int, int]) -> str:
    return f"{s[0]}x{s[1]}"


def _conf(c: float | None) -> str:
    return f"{c:.2f}" if c is not None else "n/a"


def _usd(x: float) -> str:
    return f"{x:.3f}" if x < 1 else f"{x:.2f}"


def _pct(num: int, den: int) -> str:
    v = round(100.0 * num / den, 1)
    return f"{v:g}"


class _Prose:
    """Builds item fields in rank order, remembering what earlier items already said (9.3)."""

    def __init__(self, b: "_Builder"):
        self.b = b
        self.workspaces: set[str] = set()
        self.grids: set[tuple[str, str]] = set()
        self.surfaces: set[str] = set()
        self.entries: set[str] = set()
        self.models: set[tuple[str | None, str | None]] = set()

    # ---- What moved --------------------------------------------------------------------
    def what_moved(self, cl: Cluster, rep: Member, group: int) -> Line:
        if rep.pending:
            rv = (rep.finding.evidence.get("reviewer") if rep.finding else None) or {}
            text = f"Reviewer: pending since {rv.get('pending_since', 'unknown')}"
            if rv.get("batch"):
                text += f", batch {rv['batch']}"
            return (T(text),)
        if group in FAILURE_GROUPS:
            text = (rep.finding.description if rep.finding else None) or rep.outcome.detail or ""
        else:
            text = self.b.description(cl) or (rep.finding.description if rep.finding else "") or ""
        rng = self.range_summary(cl, rep, group)
        return (T(" ".join(x for x in (text, rng) if x)),)

    def range_summary(self, cl: Cluster, rep: Member, group: int) -> str:
        if cl.kind == "sweep":
            return self._sweep_range(cl, rep, group)
        if cl.kind == "split":
            fr = sorted({m.fraction for m in cl.members})
            total = self.b.split_positions.get(rep.outcome.surface_id, len(fr))
            fs = words_and([f"{x:.2f}" for x in fr])
            word = "fraction" if len(fr) == 1 else "fractions"
            if group in FAILURE_GROUPS:
                return f"Fails at {word} {fs} ({len(cl.members)} of {total} splitter positions)."
            return f"{word.capitalize()} {fs} ({len(cl.members)} of {total} splitter positions)."
        return ""

    def _sweep_range(self, cl: Cluster, rep: Member, group: int) -> str:
        sizes = {m.size for m in cl.members}
        k = len(cl.members)
        all_sizes = self.b.sweep_sizes.get(rep.outcome.surface_id, sizes)
        n = len(all_sizes)
        widths = sorted({w for w, _ in sizes})
        heights = sorted({h for _, h in sizes})
        all_w = sorted({w for w, _ in all_sizes})
        all_h = sorted({h for _, h in all_sizes})
        failure = group in FAILURE_GROUPS
        if sizes == {(w, h) for w in widths for h in heights}:
            def axis(vals: list[int], every: list[int], noun: str) -> str:
                if vals == every and len(vals) > 1:
                    return f"all {number_word(len(vals))} {noun}s"
                return f"{noun if len(vals) == 1 else noun + 's'} {words_and([str(v) for v in vals])}"
            hp, wp = axis(heights, all_h, "height"), axis(widths, all_w, "width")
            where = f"at {hp} for {wp}" if failure else f"{hp[0].upper() + hp[1:]} at {wp}"
        else:
            listed = words_and([_size(s) for s in sorted(sizes, key=lambda s: (-s[0], -s[1]))])
            where = f"at steps {listed}" if failure else f"Steps {listed}"
        if not failure:
            return f"{where} ({k} of {n} steps)."
        text = f"Fails {where} ({k} of {n} Ladder {self.b.ladder} steps)"
        if k > 1:
            first = max(sizes, key=lambda s: (s[0], s[1]))
            text += f"; first failing step in ladder order {_size(first)}; shown at {_size(rep.size)}"
        return text + "."

    # ---- Why unexpected ----------------------------------------------------------------
    def why(self, cl: Cluster, rep: Member, group: int) -> Line:
        o = rep.outcome
        surfaces = cl.surfaces
        declared = self.b.declared
        undeclared = all(s not in declared for s in surfaces)
        if o.outcome == "collateral" and o.reason == "manifest_missing":
            return (T("no manifest entry and the PR touches UI code ("), C("manifest_missing"), T(")."))
        if group in (COLLATERAL, APP_ALIVE, INVARIANT, ASSERTION) and undeclared and o.primitive \
                and o.manifest_entry_ref and rep.reason_code != "uncalibrated":
            return self._undeclared(surfaces, o.primitive, o.manifest_entry_ref)
        if group == INVARIANT:
            ids = o.invariant_ids or ([o.reason] if o.reason in INVARIANT_STATEMENTS else [])
            return (T(" ".join(f"Invariant {i}: {INVARIANT_STATEMENTS.get(i, 'see the checkpoint page')}."
                               for i in ids) or "Invariant violation."),)
        if group in (APP_ALIVE, ASSERTION):
            aid = (o.reason or "assertion:").partition(":")[2] or "unknown"
            raw = (rep.finding.evidence.get("raw_values") if rep.finding else None) or {}
            if raw:
                return (T(f"Assertion {aid}: expected {raw.get('expected', '?')}, read "
                          f"{raw.get('read', '?')} by {raw.get('source', '?')}."),)
            return (T(f"Assertion {aid} failed" + (f" ({o.detail})." if o.detail else ".")),)
        code = rep.reason_code
        if group == COLLATERAL and code != "uncalibrated":
            return (C(surfaces[0]), T(" is not declared in the manifest."))
        if group == NO_MATCH or code in ("reviewer_uncertain", "reviewer_no_match"):
            rv = (rep.finding.evidence.get("reviewer") if rep.finding else None) or {}
            verdict = rv.get("verdict") or code
            if verdict not in REVIEWER_VERDICTS and verdict != code:
                raise ReportInputError(f"unknown reviewer verdict {verdict!r}")
            conf = o.classification.confidence if o.classification else None
            return (T("Declared surface; the Reviewer did not confirm the manifest entry ("), C(verdict),
                    T(f", confidence {_conf(conf)})."))
        if rep.pending:
            return (T("Declared surface; the Reviewer verdict is pending."),)
        if group == LANDMARK:
            return (T(f"Not found on the candidate: {o.reason or 'landmark'}."),)
        if group == UNREGISTERED:
            return (T("Not in the Surface Registry; a registry stub PR is proposed ("), C(o.reason or "probe"),
                    T(")."))
        if group == NEEDS_BASELINE:
            return (T("No comparable base ("), C(o.reason or "needs_baseline"),
                    T(") on a surface no manifest entry declares as new."))
        return (T(NEEDS_HUMAN_WHY.get(code or "", "A human decides.")),)

    def _undeclared(self, surfaces: list[str], primitive: str, entry_id: str) -> Line:
        entry = self.b.entry(entry_id)
        quoted = entry_id in self.entries
        self.entries.add(entry_id)
        if quoted:
            touched: Line = (T(f", touched by {entry_id}."),)
        else:
            touched = (T(f", touched by manifest entry {entry_id} (\""), T(str(entry.get("summary", ""))),
                       T(f"\", PR {self.b.ctx.pr})."))
        if len(surfaces) == 1:
            s = surfaces[0]
            if s in self.surfaces:
                return (T("same undeclared surface, primitive "), C(primitive), T(f", entry {entry_id}."))
            self.surfaces.add(s)
            return (C(s), T(" is not declared in the manifest. It consumes "), C(primitive)) + touched
        self.surfaces.update(surfaces)
        lead = "neither surface is declared. Both consume " if len(surfaces) == 2 else \
            f"none of the {number_word(len(surfaces))} surfaces is declared. All consume "
        return (T(lead), C(primitive)) + touched

    # ---- Where -------------------------------------------------------------------------
    def _workspace(self, ws: str) -> Line:
        if ws in self.workspaces:
            return (C(ws),)
        self.workspaces.add(ws)
        return (T("workspace "), C(ws))

    def where(self, cl: Cluster, rep: Member) -> Line:
        o = rep.outcome
        ev = rep.finding.evidence if rep.finding else {}
        parts: list[Line] = []
        workspaces = list(dict.fromkeys(m.outcome.workspace_id for m in sorted(cl.members, key=lambda m: m.checkpoint_id)))
        if len(workspaces) == 1:
            head = self._workspace(workspaces[0])
        else:
            self.workspaces.update(workspaces)
            head = (T("workspaces "),) + _codes(workspaces)
        if cl.kind in ("census", "flow") and len(cl.members) > 1:
            ids = sorted(m.checkpoint_id for m in cl.members)
            noun = "census checkpoint" if cl.kind == "census" else "flow checkpoint"
            if len(set(cl.surfaces)) == len(cl.members) and len(workspaces) == 1:
                head += (T(f" (one {noun} per surface: "),) + _codes(ids) + (T(")"),)
            else:
                head += (T(f" ({plural(len(ids), noun)}: "),) + _codes(ids) + (T(")"),)
            parts.append(head)
        else:
            parts.append(head)
            parts.append(self._layout(cl, rep, ev))
        if rep.region:
            parts.append((T("region "), C(rep.region)))
        if ev.get("target_window"):
            parts.append((T(f"window {ev['target_window']}"),))
        parts.append((T(f"data mode {o.data_mode}"),))
        return join_and(parts, last=", ")

    def _layout(self, cl: Cluster, rep: Member, ev: dict) -> Line:
        kind = cl.kind
        if kind == "census":
            return (T("census"),)
        if kind == "flow":
            _, _, flow_id, step = rep.checkpoint_id.split("/")
            return (T("flow "), C(flow_id), T(" step "), C(step))
        if kind == "split":
            return (T("splitter "), C(rep.host or ""), T(f" at {rep.fraction:.2f}"))
        text = f"Ladder {self.b.ladder} step {_size(rep.size)}"
        layout = ev.get("layout") or {}
        ws = rep.outcome.workspace_id
        if layout.get("grid") and (ws, layout["grid"]) not in self.grids:
            self.grids.add((ws, layout["grid"]))
            text += f" in a {layout['grid']} grid"
        if layout.get("cell"):
            text += f", cell {layout['cell']}"
        return (T(text),)

    # ---- Evidence, Reviewer, Re-run, Related decisions, Actions --------------------------
    def evidence(self, rep: Member) -> Evidence:
        arts = rep.outcome.artifacts or {}
        urls = self.b.urls
        ref = rep.finding.finding_id if rep.finding else rep.checkpoint_id
        note = rep.finding.evidence.get("overlay") if rep.finding else None
        return Evidence(urls.image("base", arts.get("base")), urls.image("candidate", arts.get("candidate")),
                        urls.image("heatmap", arts.get("heatmap")), urls.checkpoint(ref), note)

    def reviewer(self, cl: Cluster, rep: Member, group: int, earlier: list[Item]) -> Line:
        if group in FAILURE_GROUPS:
            return (T("not reviewed: invariant and assertion outcomes are never reclassified"),)
        if any(m.pending for m in cl.members):
            return (T("pending"),)
        cls = self.b.reviewed(cl)
        if cls is None:
            code = rep.reason_code
            if code in ("reviewer_unavailable", "reviewer_cap"):
                return (T("not reviewed: "), C(code))
            return (T("not reviewed"),)
        rv = (rep.finding.evidence.get("reviewer") if rep.finding else None) or {}
        if rep.outcome.outcome == "needs_human" and rv.get("verdict"):
            lead = f"{rv['verdict']} {_conf(cls.confidence)}"
        else:
            lead = "description"
        n = len(cl.unsuppressed) or len(cl.members)
        if cl.kind == "sweep" and n > 1:
            sizes = cl.sizes
            call = (f"one call for the sweep cluster with the crops of its smallest and largest steps, "
                    f"{_size(sizes[0])} and {_size(sizes[-1])}")
        elif cl.kind == "split" and n > 1:
            fr = sorted({m.fraction for m in cl.members})
            call = f"one call for the split cluster with the crops of fractions {fr[0]:.2f} and {fr[-1]:.2f}"
        elif n > 1:
            call = "one call for the cluster"
        else:
            call = "its own call"
            for item in earlier:
                c = item.cluster
                if c is not None and c.kind in ("sweep", "split") and item.reviewer_call \
                        and rep.outcome.surface_id in c.surfaces and c.members[0].category == rep.category:
                    noun = "a census checkpoint" if cl.kind == "census" else "a flow checkpoint"
                    what = "a step of the ladder" if c.kind == "sweep" else "a position of the splitter"
                    call += (f" ({noun} is not {what}, so it is not in item {item.rank}'s "
                             f"{c.kind} cluster, 9.7)")
                    break
        model = (cls.model_id, cls.prompt_hash)
        if model not in self.models and cls.model_id:
            self.models.add(model)
            call += f" ({cls.model_id}, prompt {cls.prompt_hash})" if cls.prompt_hash else f" ({cls.model_id})"
        types = [t for t in rv.get("change_types", []) if t]
        for t in types:
            if t not in CHANGE_TYPES:
                raise ReportInputError(f"unknown reviewer change type {t!r} (3.13)")
        tail = f", defect class {words_and(types)}" if len(types) == 1 else \
            (f", defect classes {words_and(types)}" if types else "")
        return (T(f"{lead}, {call}{tail}"),)

    def rerun(self, cl: Cluster, rep: Member, group: int) -> Line:
        o = rep.outcome
        n = o.recaptures
        if o.stability == "stable" and group in FAILURE_GROUPS:
            what = (rep.finding.evidence.get("violation") if rep.finding else None) or \
                ("violation" if group == INVARIANT else "failure")
            text = ("stable: the one confirming re-capture on the candidate shows the same " if n == 1 else
                    f"stable: all {n} confirming re-captures on the candidate show the same ")
            text += f"{what} (3.10 Targeted re-run and flake logic)"
        elif o.stability == "stable":
            text = f"stable: identical across {plural(n, 're-capture')} on the candidate (product change)"
        elif o.stability == "unknown":
            cause = o.stability_cause or "unknown"
            text = ("not re-captured: re-run budget exhausted (stability unknown, cause: budget)"
                    if cause == "budget" else f"not re-captured: stability unknown (cause: {cause})")
        else:
            text = "not re-captured: no targeted re-run recorded"
        base = self.b.ctx.base_reused_from
        if base:
            noun = "base capture" if len(cl.members) == 1 else "base captures"
            text += f"; {noun} reused from merge run {base} under the same fingerprint and fixture epoch"
        return (T(text),)

    def related(self, rep: Member) -> Line:
        ev = rep.finding.evidence if rep.finding else {}
        conflicts = ev.get("conflicting_decisions") or []
        if len(conflicts) >= 2:
            parts = [f"{d['decision_id']} ({d['verdict']})" for d in conflicts]
            return (T(f"conflicting decisions {words_and(parts)}: nothing inherited, triage rotation notified"),)
        near = ev.get("near_decisions") or []
        if near:
            return (T("; ".join(f"decision {d['decision_id']} at Hamming {d['hamming']}, not applied"
                                for d in near)),)
        return (T("none"),)

    def actions(self, ref: str) -> Line:
        u = self.b.urls
        return (Link("Confirm bug", u.action("confirm_bug", ref)), T(" | "),
                Link("Mark intended with ticket", u.action("mark_intended", ref)), T(" | "),
                Link("Request mask with reason", u.action("request_mask", ref)))


# --------------------------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------------------------

class _Builder:
    def __init__(self, run: RunOutcome, outcomes, findings, decisions, coverage, health, manifest,
                 ctx: ReportContext, steps):
        if ctx.check_mode not in CHECK_MODES:
            raise ReportInputError(f"check mode {ctx.check_mode!r}; expected one of {CHECK_MODES}")
        self.run, self.ctx = run, ctx
        self.outcomes = [o for o in outcomes if not o.synthetic]      # 9.1: synthetic never listed
        self.findings = list(findings)
        self.decisions = {d.decision_id: d for d in decisions}
        self.coverage = coverage or {}
        self.health = health or {}
        self.manifest = manifest
        self.steps = list(steps)
        self.urls = Urls(ctx.base_url.rstrip("/"), run.run_id)
        self.ladder = "F" if run.run_type == "rc" else "S"
        self.declared = {s for e in (manifest or {}).get("entries", []) for s in e.get("declared_surfaces", [])}
        self.by_cp: dict[str, list[Finding]] = {}
        for f in self.findings:
            if f.checkpoint_id:
                self.by_cp.setdefault(f.checkpoint_id, []).append(f)
        self.sweep_sizes: dict[str, set[tuple[int, int]]] = {}
        self.split_positions: dict[str, int] = {}
        for o in self.outcomes:
            parts = checkpoint_parts(o.checkpoint_id)
            if parts["kind"] == "sweep":
                w, _, h = parts["qualifier"].rsplit("/", 1)[1].partition("x")
                self.sweep_sizes.setdefault(o.surface_id, set()).add((int(w), int(h)))
            elif parts["kind"] == "split":
                self.split_positions[o.surface_id] = self.split_positions.get(o.surface_id, 0) + 1

    def entry(self, entry_id: str) -> dict:
        for e in (self.manifest or {}).get("entries", []):
            if e.get("entry_id") == entry_id:
                return e
        return {}

    def reviewed(self, cl: Cluster):
        """The classification carrying the cluster's Reviewer call, if one was made (5.11)."""
        for m in [cl.representative] + cl.members:
            c = m.outcome.classification
            if c is not None and c.model_id:
                return c
        return None

    def description(self, cl: Cluster) -> str | None:
        for m in [cl.representative] + cl.members:
            c = m.outcome.classification
            if c is not None and c.description:
                return c.description
        return None

    # ---- members --------------------------------------------------------------------------
    def members(self) -> tuple[list[Member], list[Member]]:
        listed, intended = [], []
        for o in self.outcomes:
            m = _member(o, self.by_cp.get(o.checkpoint_id, []), self.decisions, self.declared)
            if m.suppressed or m.group is not None:
                listed.append(m)
            elif o.outcome == "intended":
                intended.append(m)
        return listed, intended

    # ---- items ----------------------------------------------------------------------------
    def cluster_item(self, cl: Cluster) -> Item:
        rep = cl.representative
        group = min((m.group for m in cl.members if m.group is not None), default=NEEDS_HUMAN)
        tier = min((m.outcome.tier for m in cl.members if m.outcome.tier is not None), default=None)
        conf = None
        cls = self.reviewed(cl)
        if cls is not None and cls.confidence is not None and rep.outcome.outcome == "needs_human":
            conf = cls.confidence
        novel = rep.finding is None or rep.finding.first_seen_run_id == self.run.run_id
        key = (tier if tier is not None else 4, group, (0, -conf) if conf is not None else (1, 0),
               0 if novel else 1, -len(cl.members), rep.checkpoint_id)
        return Item(0, tier, group, (), [], decided=not cl.unsuppressed, pending=any(m.pending for m in cl.members),
                    reviewer_call=cls is not None and bool(cl.unsuppressed), cluster=cl, sort_key=key)

    def step_items(self) -> list[Item]:
        out = []
        fmap = {f.finding_id: f for f in self.findings}
        for s in self.steps:
            if s.status not in ("failed", "passed_via_agent_fallback"):
                continue
            f = fmap.get(s.finding_ref) if s.finding_ref else None
            side = (f.evidence.get("side") if f else None) or "candidate"
            if s.status == "failed" and side != "candidate":
                continue
            group = STEP_FAILED if s.status == "failed" else AGENT_FALLBACK
            tier = f.tier if f else None
            key = (tier if tier is not None else 4, group, (1, 0), 0, 0, f"{s.flow_id}/{s.step_id}")
            out.append(Item(0, tier, group, (), [], decided=bool(f and f.decision_ref), pending=False,
                            reviewer_call=False, step=s, finding=f, sort_key=key))
        return out

    def finding_items(self) -> list[Item]:
        out = []
        for f in self.findings:
            group = FINDING_ONLY.get(f.kind)
            if group is None or f.decision_ref:
                continue
            key = (f.tier if f.tier is not None else 4, group, (1, 0), 0, 0, f.finding_id)
            out.append(Item(0, f.tier, group, (), [], decided=False, pending=False, reviewer_call=False,
                            finding=f, sort_key=key))
        return out

    def fill(self, items: list[Item]) -> None:
        prose = _Prose(self)
        done: list[Item] = []
        for rank, item in enumerate(items, 1):
            item.rank = rank
            if item.cluster is not None:
                self._fill_cluster(prose, item, done)
            elif item.step is not None:
                self._fill_step(prose, item)
            else:
                self._fill_finding(item)
            done.append(item)
        self.prose = prose

    def _tier_tag(self, tier: int | None) -> str:
        return f"[Tier {tier}]" if tier is not None else "[Run level]"

    def _fill_cluster(self, prose: _Prose, item: Item, done: list[Item]) -> None:
        cl, rep, group = item.cluster, item.cluster.representative, item.group
        head: list = [T(f"{item.rank}. {self._tier_tag(item.tier)} {outcome_label(rep)} on ")]
        head += list(_codes(cl.surfaces)) + [T(", checkpoint "), C(rep.checkpoint_id)]
        if len(cl.members) > 1:
            head.append(T(f", cluster of {len(cl.members)} checkpoints"))
        sup = cl.suppressed
        if sup:
            ids = sorted({m.decision_id for m in sup})
            head.append(T(f", {len(sup)} of {len(cl.members)} members suppressed by decision {words_and(ids)}"))
        item.header = tuple(head)
        ref = rep.finding.finding_id if rep.finding else rep.checkpoint_id
        item.fields = [
            ("What moved", prose.what_moved(cl, rep, group)),
            ("Why unexpected", prose.why(cl, rep, group)),
            ("Where", prose.where(cl, rep)),
            (FIELD_EVIDENCE, prose.evidence(rep)),
            ("Reviewer", prose.reviewer(cl, rep, group, done)),
            ("Re-run", prose.rerun(cl, rep, group)),
            ("Related decisions", prose.related(rep)),
            ("Actions", prose.actions(ref)),
        ]

    def _fill_step(self, prose: _Prose, item: Item) -> None:
        s, f = item.step, item.finding
        label = "step failed" if s.status == "failed" else "step passed_via_agent_fallback"
        head: list = [T(f"{item.rank}. {self._tier_tag(item.tier)} {label}")]
        if f is not None and f.checkpoint_id:
            head += [T(" on "), C(checkpoint_parts(f.checkpoint_id)["surface"])]
        head += [T(", flow "), C(s.flow_id), T(" step "), C(s.step_id)]
        item.header = tuple(head)
        desc = f.description if f else ""
        if s.status == "passed_via_agent_fallback":
            moved: Line = (T(f"scripted locator broke at step {s.step_id}" + (f"; {desc}" if desc else "")),)
        else:
            moved = (T((desc or f"step {s.step_id} failed") + " (candidate side)"),)
        ev = f.evidence if f else {}
        ref = f.finding_id if f else f"{s.flow_id}/{s.step_id}"
        evidence = Evidence(self.urls.image("base step", ev.get("base_screenshot")),
                            self.urls.image("candidate step", ev.get("pre_screenshot")), None,
                            self.urls.checkpoint(ref), None)
        item.fields = [("What moved", moved), (FIELD_EVIDENCE, evidence), ("Actions", prose.actions(ref))]

    def _fill_finding(self, item: Item) -> None:
        f = item.finding
        head: list = [T(f"{item.rank}. {self._tier_tag(item.tier)} {f.kind}")]
        if f.checkpoint_id:
            head += [T(" on "), C(checkpoint_parts(f.checkpoint_id)["surface"]), T(", checkpoint "),
                     C(f.checkpoint_id)]
        item.header = tuple(head)
        item.fields = [("What", (T(f.description),)),
                       ("Details", (Link("finding page", self.urls.finding(f.finding_id)),))]

    # ---- intended -------------------------------------------------------------------------
    def intended_items(self, intended: list[Member]) -> tuple[list[IntendedItem], list[Cluster]]:
        groups: dict[tuple[str, str | None], list[Member]] = {}
        for m in intended:
            groups.setdefault((m.outcome.surface_id, m.outcome.manifest_entry_ref), []).append(m)
        out = []
        for (surface, entry_id), ms in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            cl = Cluster(ms)
            rep = cl.representative
            n = len(ms)
            verb = "checkpoint matches" if n == 1 else "checkpoints match"
            header: Line = (C(surface), T(f", {n} {verb} {entry_id}" if entry_id else f", {plural(n, 'checkpoint')} intended"))
            entry = self.entry(entry_id) if entry_id else {}
            fields: list[Field] = []
            if entry:
                tickets = ", ".join(entry.get("tickets", [])) or "none"
                fields.append(("Declared", (T('"'), T(str(entry.get("summary", ""))),
                                            T(f'" ({entry_id}, ticket {tickets}, PR {self.ctx.pr})'))))
            arts = rep.outcome.artifacts or {}
            fields.append((FIELD_EVIDENCE, Evidence(self.urls.image("base", arts.get("base")),
                                                    self.urls.image("candidate", arts.get("candidate")),
                                                    self.urls.image("heatmap", arts.get("heatmap")), None)))
            cls = next((m.outcome.classification for m in [rep] + ms
                        if m.outcome.classification and m.outcome.classification.by == "reviewer"), None)
            if cls is not None and cls.confidence is not None:
                conf: Line = (T(f"intended {_conf(cls.confidence)}: \""), T(cls.description or ""), T('"'))
            else:
                conf = (T("pending"),)
            fields.append(("Reviewer confirmation", conf))
            out.append(IntendedItem(surface, header, fields))
        return out, cluster_members(intended)

    def reconciliation(self, intended: list[Member]) -> Line | None:
        entries = (self.manifest or {}).get("entries", [])
        if not entries:
            return None
        parts: list[Line] = []
        tasks = self.coverage.get("flow_tasks", {})
        author = (self.manifest or {}).get("author", "the PR author")
        for e in entries:
            eid = e.get("entry_id", "?")
            visible = [m for m in intended if m.outcome.manifest_entry_ref == eid]
            covering = sorted(o.checkpoint_id for o in self.outcomes
                              if o.surface_id in e.get("declared_surfaces", []))
            if visible:
                parts.append((T(f"{eid}: verified visible on {plural(len(visible), 'checkpoint')}"),))
            elif covering:
                parts.append((T(f"{eid}: covering checkpoints "),) + _codes(covering) +
                             (T(" showed no change above floor ("), C("intended_change_not_observed"),
                              T(f"; a question for {author} and the feature owner, not a bug)")))
            else:
                task = tasks.get(eid)
                parts.append((T(f"{eid}: no covering checkpoint; flow-authoring task "
                                f"{task + ' ' if task else ''}opened"),))
        return (T("Manifest reconciliation: "),) + join_and(parts, sep="; ", last="; ")

    # ---- suppressed -----------------------------------------------------------------------
    @staticmethod
    def suppressed_rows(clusters: list[Cluster]) -> list[SuppressedRow]:
        rows = [SuppressedRow(m.checkpoint_id, m.region, m.decision_id, m.decision)
                for cl in clusters for m in cl.suppressed]
        return sorted(rows, key=lambda r: (r.checkpoint_id, r.decision_id))


# --------------------------------------------------------------------------------------------
# Commit status (9.2)
# --------------------------------------------------------------------------------------------

def _not_compared_tier1(outcomes: list[CheckpointOutcome]) -> tuple[set[str], list[str]]:
    surfaces, kinds = set(), Counter()
    for o in outcomes:
        if o.tier != 1:
            continue
        hit = (o.outcome in ("env_error", "not_settled", "not_comparable")
               or (o.outcome == "landmark_missing" and o.side in ("base", "both"))
               or (o.outcome == "not_run" and o.reason == "budget"))
        if hit:
            surfaces.add(o.surface_id)
            kinds[o.outcome if o.outcome != "not_run" else "not_run (budget)"] += 1
    return surfaces, [k for k, _ in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]


def status_from_items(run: RunOutcome, items: list[Item], outcomes: list[CheckpointOutcome],
                      ctx: ReportContext, mode: str, target_url: str | None) -> CommitStatus:
    """The 9.2 state table applied to ranked Unexpected items."""
    if mode not in CHECK_MODES:
        raise ReportInputError(f"check mode {mode!r}; expected one of {CHECK_MODES}")
    reason = run.reason or ""

    def st(state: str, text: str) -> CommitStatus:
        return CommitStatus(state, text, mode, target_url)

    if run.status == "aborted_safety":
        return st("error", f"aborted_safety: incident {ctx.abort.get('incident', 'unknown')}")
    if run.status == "aborted_harness":
        if reason in ("dead_man", "secret_leaked"):
            return st("error", "Sentinel did not report")
        return st("error", f"aborted_harness: {reason}")
    if run.status == "aborted_budget" and reason == "pool_saturated":
        return st("error", "Sentinel did not run")
    if ctx.comment_overdue:
        return st("error", "Sentinel did not report")
    if ctx.override:
        return st("success", f"forced success by {ctx.override.get('by', '?')}: {ctx.override.get('reason', '')}")
    failing = [i for i in items if i.tier == 1 and not i.decided and i.group in CHECK_FAILING_GROUPS]
    if failing:
        return st("failure", f"{len(failing)} tier-1 {'finding has' if len(failing) == 1 else 'findings have'} no decision")
    open_23 = sum(1 for i in items if i.tier in (2, 3) and not i.decided)
    surfaces, kinds = _not_compared_tier1(outcomes)
    text = f"{len(items)} unexpected ({open_23} tier 2 and 3 open); {len(surfaces)} tier-1 surfaces not compared"
    if surfaces:
        text += f" ({', '.join(kinds)}): see the merge run"
    return st("success", text)


def pending_status(base_build: str, mode: str = "informational", target_url: str | None = None) -> CommitStatus:
    """9.2 `pending`: set from the run row at T+0:00 while the run is queued or running."""
    if mode not in CHECK_MODES:
        raise ReportInputError(f"check mode {mode!r}; expected one of {CHECK_MODES}")
    return CommitStatus("pending", f"Sentinel run started, base = {base_build}", mode, target_url)


# --------------------------------------------------------------------------------------------
# Coverage and run health (2.12, 9.2, 9.11)
# --------------------------------------------------------------------------------------------

def _tiers(coverage: dict) -> list[tuple[int, dict]]:
    tiers = coverage.get("tiers", {})
    if isinstance(tiers, dict):
        rows = [(int(k), v) for k, v in tiers.items()]
    else:
        rows = [(int(v["tier"]), v) for v in tiers]
    return sorted(rows)


def coverage_rows(coverage: dict) -> list[tuple[str, ...]]:
    rows = []
    for tier, t in _tiers(coverage):
        not_run = {k: int(v) for k, v in t.get("not_run", {}).items()}
        for k in not_run:
            if k not in NOT_RUN_ORDER:
                raise ReportInputError(f"not_run reason {k!r} is not in 1.10")
        nc = {k: int(v) for k, v in t.get("not_compared", {}).items() if int(v)}
        for k in nc:
            if k not in NOT_COMPARED:
                raise ReportInputError(f"not-compared outcome {k!r}")
        n_nc = sum(nc.values())
        in_scope, covered = int(t.get("in_scope", 0)), int(t.get("covered", 0))
        comparable = in_scope - not_run.get("entitlement", 0) - n_nc
        cov = f"{covered} ({_pct(covered, comparable)} percent of comparable scope)" if comparable > 0 else str(covered)
        nc_text = f"{n_nc} ({', '.join(f'{k}: {v}' for k, v in nc.items())})" if n_nc else "0"
        nr_text = ", ".join(f"{not_run.get(k, 0)} {k}" for k in NOT_RUN_ORDER)
        rows.append((str(tier), str(t.get("registered", 0)), str(in_scope), cov, nc_text, nr_text))
    return rows


def scope_line(b: _Builder) -> Line:
    s = b.coverage.get("scope", {})
    n_decl = int(s.get("declared_surfaces", len(b.declared)))
    edges = int(s.get("edges", 0))
    text = f"Scope: {plural(n_decl, 'declared surface')}, expanded through {plural(edges, 'dependency-map edge')}"
    if s.get("primitives"):
        text += f" ({', '.join(s['primitives'])})"
    added = int(s.get("static_impact_added", 0))
    text += ", confirmed by the static impact step" if added == 0 else \
        f" and the static impact step ({plural(added, 'more surface')})"
    text += f", plus the smoke census ({plural(int(s.get('smoke_census', 0)), 'checkpoint')})"
    personas = [p for p in ("active_trader", "options_trader", "futures_trader", "swing_trader", "beginner")
                if any(o.persona == p for o in b.outcomes)]
    if personas:
        text += f"; personas {words_and([p.replace('_', ' ') for p in personas])}"
    n_unreg = sum(1 for o in b.outcomes if o.outcome == "unregistered_surface")
    gaps = [f for f in b.findings if f.kind == "dependency_map_gap"]
    line: list = [T(text + ". " + f"{n_unreg} "), C("unregistered_surface"), T(f", {len(gaps)} "),
                  C("dependency_map_gap")]
    if gaps:
        line.append(T(" ("))
        line += list(join_and([(Link(g.finding_id, b.urls.finding(g.finding_id)),) for g in gaps]))
        line.append(T(")"))
    line.append(T("."))
    if s.get("send_policy"):
        line += [T(f" Send policy {s['send_policy']}: {plural(int(s.get('order_surfaces_entitlement', 0)), 'order lifecycle surface')} "),
                 C("not_run"), T(" (entitlement).")]
    line += [T(" "), Link("Matrix", b.urls.matrix)]
    return tuple(line)


def _rerun_row(b: _Builder) -> Line:
    rec = [o for o in b.outcomes if o.recaptures > 0]
    stable = sum(1 for o in rec if o.stability == "stable")
    unstable = sum(1 for o in b.outcomes if o.stability == "unstable")
    exhausted = sum(1 for o in b.outcomes if o.stability == "unknown" and o.stability_cause == "budget")
    total = sum(o.recaptures for o in rec)
    out: list = [T(f"{len(rec)} ({stable} stable, {unstable} unstable), {plural(total, 're-capture')}")]
    fail = [o for o in rec if o.outcome in ("invariant_violation", "assertion_failed")]
    other = [o for o in rec if o not in fail]
    groups = []

    def describe(os_: list[CheckpointOutcome], label: Line) -> Line:
        counts = {o.recaptures for o in os_}
        lead = f"{counts.pop()} each for the " if len(counts) == 1 else f"{sum(o.recaptures for o in os_)} for the "
        return (T(lead + f"{len(os_)} "),) + label

    if fail:
        kinds = sorted({o.outcome for o in fail})
        noun = "steps" if all(o.test_kind in ("sweep", "split") for o in fail) else "checkpoints"
        label: Line = ((C(kinds[0]), T(f" {noun}")) if len(kinds) == 1 else (T(f"invariant and assertion {noun}"),))
        groups.append(describe(fail, label))
    if other:
        groups.append(describe(other, (T(("other " if fail else "") + "above-threshold checkpoints"),)))
    if groups:
        out.append(T(": "))
        out += list(join_and(groups, last=", "))
    out += [T(f"; {exhausted} "), C("rerun_budget_exhausted")]
    if exhausted:
        out += [T(" (stability left "), C("unknown (cause: budget)"), T(", 3.10 Targeted re-run and flake logic)")]
    return tuple(out)


def _reviewer_row(b: _Builder, items: list[Item], intended_clusters: list[Cluster]) -> Line:
    called = [i.rank for i in items if i.reviewer_call]
    intended_called = [cl for cl in intended_clusters if b.reviewed(cl) is not None]
    calls = len(called) + len(intended_called)
    parts = []
    if called:
        parts.append(("item " if len(called) == 1 else "items ") + words_and([str(r) for r in called]))
    if intended_called:
        names = list(dict.fromkeys(_surface_name(cl.surfaces[0]) for cl in intended_called))
        parts.append("the intended " + words_and(names))
    text = plural(calls, "call") + (f" ({' and '.join(parts)})" if parts else "")
    pending = sum(1 for i in items if i.pending)
    unavailable = sum(1 for i in items if i.cluster is not None and not i.pending
                      and i.cluster.representative.reason_code == "reviewer_unavailable")
    text += f", {pending} pending, {unavailable} unavailable"
    rv = b.health.get("reviewer", {})
    if "cost_usd" in rv:
        text += f", {_usd(float(rv['cost_usd']))} USD"
        if rv.get("tier") and rv.get("top_tier_usd") is not None and rv["tier"] != "top":
            text += f" ({rv['tier']} tier; {_usd(float(rv['top_tier_usd']))} USD top tier, 11.4)"
        elif rv.get("tier"):
            text += f" ({rv['tier']} tier, 11.4)"
    return (T(text),)


def _canary_row(c: dict) -> Line:
    def rate(det, tot, label):
        if not tot:
            return f"{label} not measured"
        text = f"{label} {_pct(int(det), int(tot))} percent"
        return text if det == tot else text + f" ({det} of {tot})"
    state = c.get("detector_state", "ok")
    if state not in ("ok", "detector_blind", "detector_degraded", "generation_failed"):
        raise ReportInputError(f"detector state {state!r} (10.3)")
    if state == "detector_degraded" and c.get("classes"):
        state += f": {', '.join(c['classes'])}"
    return (T(", ".join([rate(c.get("money_detected"), c.get("money_total"), "money cells"),
                         rate(c.get("other_detected"), c.get("other_total"), "other"),
                         f"detector state {state}", str(c.get("date", "unknown"))])),)


def _safety_row(s: dict) -> Line:
    proof = s.get("proof", "unknown")
    if not (proof.startswith("PROVEN: ") or proof.startswith("UNPROVEN: ")):
        raise ReportInputError(f"post-run proof verdict {proof!r} (6.6)")
    text = (f"attestation {s.get('attestation', 'unknown')}, paper mode asserted "
            f"{plural(int(s.get('paper_mode_asserted', 0)), 'time')}, gateway {plural(int(s.get('gateway_drops', 0)), 'drop')}, "
            f"Cancel All {s.get('cancel_all', 'unknown')}, proof {proof}")
    if proof.startswith("UNPROVEN"):
        text += f"; next run on login {s.get('login', 'unknown')} blocked until a human verifies the account"
    return (T(text),)


def _integrations_row(b: _Builder, items: list[Item]) -> Line:
    i = b.health.get("integrations", {})
    pc = i.get("pr_comment", {})
    state = pc.get("state", "unposted")
    if state == "posted":
        text = f"PR comment posted {pc.get('time', '')}".rstrip()
    elif state == "late":
        text = "PR comment late"
    else:
        text = f"PR comment unposted since {pc.get('time', 'unknown')}"
    if b.ctx.marker_recreated:
        text += " (the marker comment was missing; a new comment was created)"
    text += f", Jira {int(i.get('jira_queued', 0))} queued, Slack "
    out: list = [T(text)]
    slack = i.get("slack", {})
    posts = slack.get("posts", [])
    known = {it.rank for it in items}
    if posts:
        out.append(T(f"{len(posts)} posted" + (f" to {words_and(slack.get('to', []))}" if slack.get("to") else "") + " ("))
        segs = []
        for p in posts:
            if p.get("outcome") not in load().checkpoint_outcomes:
                raise ReportInputError(f"slack post outcome {p.get('outcome')!r}")
            ranks = [r for r in p.get("items", []) if r in known]
            seg: Line = (T(f"tier-{p.get('tier')} "), C(p["outcome"]),
                         T(", " + ("item " if len(ranks) == 1 else "items ") + words_and([str(r) for r in ranks])
                           + (" in one thread" if len(ranks) > 1 else "")))
            segs.append(seg)
        out += list(join_and(segs, sep="; ", last="; "))
        out.append(T(")"))
        if slack.get("queued"):
            out.append(T(f", {int(slack['queued'])} queued"))
    else:
        out.append(T(f"{int(slack.get('queued', 0))} queued"))
    return tuple(out)


def health_rows(b: _Builder, items: list[Item], intended_clusters: list[Cluster]) -> list[tuple[str, Line]]:
    h, os_ = b.health, b.outcomes
    wall = f"{h.get('wall_clock', 'unknown')} / {h.get('budget', '45:00')} on {plural(int(h.get('shards', 1)), 'shard')}"
    if b.ctx.report_late:
        wall += "; report_late"
    fp = h.get("fingerprint", {})
    cand, base = fp.get("candidate", "unknown"), fp.get("base", "unknown")
    fp_text = f"candidate {cand} = base {base}" if cand == base else f"candidate {cand} != base {base}"
    if cand == base and b.ctx.candidate_build != b.ctx.base_build:
        fp_text += " (build id differs, as expected)"
    if b.ctx.base_reused_from:
        fp_text += f"; base captures reused from {b.ctx.base_reused_from}"
    env = [o for o in os_ if o.outcome == "env_error"]
    harness = [o for o in os_ if o.outcome == "harness_error"]
    gates = list(dict.fromkeys(o.gate for o in env))
    reasons = list(dict.fromkeys((o.reason or "").partition(":")[0] for o in harness))
    env_text = f"{len(env)} ({', '.join(gates)})" if env else "0"
    harness_text = f"{len(harness)} ({', '.join(reasons)})" if harness else "0"
    count = Counter(o.outcome for o in os_)
    steps = Counter(s.status for s in b.steps) if b.steps else Counter(h.get("steps", {}))
    shifted = [o for o in os_ if o.diff and tuple(o.diff.shift) != (0, 0)]
    shift_text = plural(len(shifted), "checkpoint")
    if shifted:
        shifts = list(dict.fromkeys(tuple(o.diff.shift) for o in shifted))
        shift_text += " with a whole-window translation " + ", ".join(f"({dx}, {dy})" for dx, dy in shifts[:3])
    rows: list[tuple[str, Line]] = [
        ("Wall clock / budget", (T(wall),)),
        ("Fingerprint", (T(fp_text),)),
        ("env_error / harness_error", (T(f"{env_text} / {harness_text}"),)),
        ("not_settled / not_comparable / landmark_missing",
         (T(f"{count['not_settled']} / {count['not_comparable']} / {count['landmark_missing']}"),)),
        ("Steps: passed_after_retry / passed_via_agent_fallback / failed / blocked_by_gateway",
         (T(" / ".join(str(steps[k]) for k in ("passed_after_retry", "passed_via_agent_fallback", "failed",
                                                "blocked_by_gateway"))),)),
        ("Layout shift", (T(shift_text),)),
        ("Reviewer", _reviewer_row(b, items, intended_clusters)),
        ("Targeted re-runs", _rerun_row(b)),
    ]
    if env or h.get("env_errors"):
        rows.insert(3, ("Environment errors", _env_errors_line(b, env)))
    if "canary" in h:
        rows.append(("Canary (last Nightly)", _canary_row(h["canary"])))
    if "safety" in h:
        rows.append(("Safety", _safety_row(h["safety"])))
    rows.append(("Integrations", _integrations_row(b, items)))
    if b.ctx.override:
        rows.append(("Check override", (T(f"forced success by {b.ctx.override.get('by', '?')}: "
                                          f"{b.ctx.override.get('reason', '')}"),)))
    return rows


def _env_errors_line(b: _Builder, env: list[CheckpointOutcome]) -> Line:
    details = b.health.get("env_errors")
    out: list = []
    if details:
        segs = []
        for d in details:
            seg: list = [T(f"{d.get('gate', 'unknown')} on shard {d.get('shard', '?')}")]
            img = b.urls.image("screenshot", d.get("screenshot"))
            if img is not None:
                seg += [T(" ("), Link("screenshot", img.url), T(")")]
            segs.append(tuple(seg))
        out += list(join_and(segs, last=", "))
    else:
        per_gate = Counter(o.gate for o in env)
        out.append(T(", ".join(f"{g} ({plural(n, 'checkpoint')})" for g, n in per_gate.items())))
    if b.health.get("shard_rerun_queued"):
        out.append(T("; shard re-run queued"))
    return tuple(out)


# --------------------------------------------------------------------------------------------
# Abort block and banners
# --------------------------------------------------------------------------------------------

def _abort(b: _Builder, members: list[Member]) -> tuple[str | None, str | None, list[Line]]:
    run, a = b.run, b.ctx.abort
    reason = run.reason or ""
    if run.status == "aborted_budget":
        if reason == "pool_saturated":
            return "pool_saturated", None, [(T(f"Sentinel did not run: pool saturated, "
                                               f"{plural(int(a.get('runs_ahead', 0)), 'run')} ahead; "
                                               f"push again or re-run the build job"),)]
        budget = [o for o in b.outcomes if o.outcome == "not_run" and o.reason == "budget"]
        t1 = sum(1 for o in budget if o.tier == 1)
        text = f"Scope not finished: {len(budget)} checkpoints not_run (budget), {t1} of them tier 1"
        if reason == "dollar_cap":
            text += f"; per-run model spend ceiling reached ({a.get('cost', '?')} of {a.get('cap', '?')}, 11.4 Cost model)"
        return None, None, [(T(text),)]
    if run.status == "aborted_safety":
        lines: list[Line] = [(T(f"Trigger: {a.get('trigger', reason)}"),),
                             (T(f"Gate: {a.get('gate', run.last_gate)}"),)]
        img = b.urls.image("screenshot", a.get("screenshot"))
        if img is not None:
            lines.append((T("Last screenshot: "), Link("screenshot", img.url)))
        proof = a.get("proof") or b.health.get("safety", {}).get("proof", "unknown")
        lines += [(T(f"Post-run proof: {proof}"),), (T(f"Incident: {a.get('incident', 'unknown')}"),),
                  (T(f"No run starts on rig {a.get('rig', 'unknown')} until the incident closes."),)]
        listed = [m for m in members if not m.suppressed]
        if listed:
            lines.append((T(f"{plural(len(listed), 'finding')} recorded before the abort, collapsed on the "),
                          Link("run page", b.urls.anchor("unexpected"))))
        return None, "Abort", lines
    if run.status == "aborted_harness":
        if reason == "secret_leaked":
            return "withheld", None, [(T("report withheld; security contact notified"),)]
        lines = []
        special = None
        if reason == "dead_man":
            special = "dead_man"
            lines.append((T(f"Sentinel did not report: no run-completed event by {a.get('deadline', 'unknown')}; "
                            f"last heartbeat {a.get('last_heartbeat', 'unknown')}"),))
        rsn: list = [T("Reason: "), C(reason)]
        if reason == "config_invalid" and a.get("rule_ids"):
            rsn.append(T(f" (rules {', '.join(a['rule_ids'])})"))
        lines.append(tuple(rsn))
        lines.append((T(f"Last gate reached: {run.last_gate}"),))
        img = b.urls.image("screenshot", a.get("screenshot"))
        if img is not None:
            lines.append((T("Screenshot: "), Link("screenshot", img.url)))
        lines.append((T(f"Re-queued once: {a.get('requeued') or 'already re-queued'}"),))
        return special, "Abort", lines
    return None, None, []


def _banners(b: _Builder, members: list[Member], items: list[Item]) -> tuple[str | None, list[Line]]:
    canary = b.health.get("canary", {})
    state = canary.get("detector_state", "ok")
    divergence = any(o.outcome == "harness_error" and o.reason == "canary_path_divergence" for o in b.outcomes)
    banner = b.ctx.detector_banner
    if banner is None and (state != "ok" or divergence):
        what = state if state != "ok" else "detector_blind"
        banner = f"{what.upper().replace('_', ' ')} since {canary.get('since', canary.get('date', 'unknown'))}."
        if divergence:
            banner += " harness_error (reason: canary_path_divergence) recorded."
    lines: list[Line] = []
    rd = b.ctx.reviewer_disabled
    if rd:
        n = sum(1 for i in items if i.cluster is not None and not i.pending
                and i.cluster.representative.reason_code == "reviewer_unavailable")
        lines.append((T(f"Reviewer disabled since {rd.get('since', 'unknown')}: eval gate {rd.get('gate', 'unknown')} "
                        f"failed; {plural(n, 'item')} await{'s' if n == 1 else ''} a human"),))
    if any(f.kind == "global_rendering_shift" for f in b.findings):
        n = sum(1 for m in members if m.reason_code == "global_shift")
        lines.append((T(f"Global rendering shift: {n} checkpoints collapsed into one "),
                      C("needs_human (reason: global_shift)"), T(" cluster; no reviewer calls made")))
    return banner, lines


def _manifest_line(b: _Builder) -> Line | None:
    if b.manifest is not None:
        return None
    ui = any(f.kind == "manifest_missing" for f in b.findings) or \
        any(o.reason == "manifest_missing" for o in b.outcomes)
    if ui:
        return (T("No manifest entry and the PR touches UI code ("), C("manifest_missing"),
                T("): every diff is reported as collateral until you add one"))
    return (T("No manifest and no UI code touched: diffs are "), C("needs_human (reason: no_manifest)"))


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------

def build_report(run: RunOutcome, outcomes: list[CheckpointOutcome], findings: list[Finding],
                 decisions: list[Decision], coverage: dict | None, health: dict | None,
                 manifest: dict | None, context: ReportContext,
                 steps: list[StepOutcome] = ()) -> RunReport:
    """Arrange one run's records into the report of 9.2 (and 9.5 for the static page)."""
    tax = load()
    for o in outcomes:
        if o.outcome not in tax.checkpoint_outcomes:
            raise TaxonomyError(f"unknown outcome {o.outcome!r}")
    synthetic = {o.checkpoint_id for o in outcomes if o.synthetic}
    b = _Builder(run, outcomes, [f for f in findings if f.checkpoint_id not in synthetic],
                 decisions, coverage, health, manifest, context, steps)
    listed, intended = b.members()
    clusters = cluster_members(listed)
    items = [b.cluster_item(cl) for cl in clusters if not cl.fully_suppressed_one_verdict]
    items += b.step_items() + b.finding_items()
    items.sort(key=lambda i: i.sort_key)
    b.fill(items)
    intended_items, intended_clusters = b.intended_items(intended)
    special, abort_title, abort_lines = _abort(b, listed)
    banner, banner_lines = _banners(b, listed, items)
    status = status_from_items(run, items, b.outcomes, context, context.check_mode, b.urls.report)
    pending = sum(1 for i in items if i.pending)
    result = run.status
    if run.status.startswith("aborted_"):
        result += f" (reason: {run.reason})"
    elif pending:
        result += f" (unreviewed: {plural(pending, 'item')} pending review)"
    review_line = (T(f"Reviewer pending: {plural(pending, 'item')}; this comment is edited in place when verdicts land"),) \
        if pending else None
    nb = sum(1 for o in b.outcomes if o.outcome == "needs_baseline" and o.surface_id in b.declared)
    nb_line = (T(f"{nb} checkpoints "), C("needs_baseline"),
               T(" on declared new surfaces; the merge run produces the candidates, nothing is approved from a PR run")) \
        if nb else None
    return RunReport(
        run=run, context=context, urls=b.urls, status=status, banner=banner, banner_lines=banner_lines,
        result=result, special=special, review_line=review_line, abort_title=abort_title,
        abort_lines=abort_lines, show_findings=run.status not in ("aborted_safety", "aborted_harness") and special is None,
        unexpected=items, intended=intended_items, manifest_line=_manifest_line(b),
        reconciliation=b.reconciliation(intended), needs_baseline_line=nb_line,
        suppressed=b.suppressed_rows(clusters), coverage_rows=coverage_rows(b.coverage), scope_line=scope_line(b),
        health_rows=health_rows(b, items, intended_clusters), history=tuple(context.history), outcomes=b.outcomes)


def commit_status(run: RunOutcome, outcomes: list[CheckpointOutcome], mode: str = "informational", *,
                  findings: list[Finding] = (), decisions: list[Decision] = (), steps: list[StepOutcome] = (),
                  manifest: dict | None = None, context: ReportContext | None = None) -> CommitStatus:
    """The `sentinel/pr-run` commit status of 9.2 for a finished run.

    `mode` is the configured check mode: "informational" (Phase 1 and the default; the state is
    computed the same way but the context is not a required check and the description ends
    "(informational)") or "blocking". Findings and decisions refine clustering and suppression;
    without them each checkpoint's own `decision_ref` decides and census checkpoints do not
    cluster (no pHash), which can only raise the count in the failure description.
    """
    ctx = replace(context or ReportContext(pr=0, candidate_build="", base_build=""), check_mode=mode)
    return build_report(run, outcomes, findings, decisions, {}, {}, manifest, ctx, steps).status
