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
  order_surfaces_entitlement, optional map_patch (the run output file holding the map PR's proposed edges, 4.4)};
  optional `flow_tasks` {entry_id: task key} and `cells` (matrix).
- `health`: the run-health facts not derivable from outcome rows (8.3 indexes, 9.11):
  `wall_clock`, `budget`, `shards`, `fingerprint` {candidate, base}, `reviewer` {cost_usd, tier,
  top_tier_usd}, `canary` {money_detected, money_total, other_detected, other_total,
  detector_state, date}, `safety` {attestation, paper_mode_asserted, gateway_drops, cancel_all,
  proof (a 6.6 verdict in one of the `PROOF_FORMS`), login (a label or a list of labels)},
  `integrations` {pr_comment {state, time},
  jira_queued, slack {queued, to, posts}},
  optional `env_errors` [{gate, shard, screenshot}], `shard_rerun_queued`, `steps` (counts).
  Counts that outcome rows determine (env and harness errors, not settled, re-runs, Reviewer
  calls, layout shift) are computed here, never read from `health`.
- `Finding.evidence` keys the Reporter reads (all optional, all rendered as data):
  `target_window`, `layout` {grid, cell}, `overlay` (evidence note), `violation` (short phrase
  for the re-run line), `reviewer` {verdict, change_types, pending_since, batch},
  `raw_values` {expected, read, source} (4.13), `near_decisions` [{decision_id, hamming}],
  `conflicting_decisions` [{decision_id, verdict}], `side`, `pre_screenshot`, `base_screenshot`; on a
  `redaction_miss` finding `candidate_only` (3.6).
- `summary`: the run's `runs.summary_json` (`Store.run_summary`, 8.3, 8.7). `ReportInputs.from_store` reads
  `coverage` and `health` from it; these further top-level keys are read whenever a summary is given, each optional
  (the run's teardown writes them through `Store.end_run(summary=)` or `update_summary`):
  - `coverage.matrix` {surfaces, personas, profiles}: the 2.12 matrix dimensions that G2 checks `coverage.cells`
    against (`store.checks.matrix_failures`, 1.4 G2). Without it the Reporter does not check the matrix (the
    store's `post_run_check` does). `coverage.cells` rows are the 8.3 `coverage_cells` form (`coverage.Cell.to_row`:
    surface_id, persona, entitlement, state, not_run_reason, worst_outcome, checkpoint_count) or the short form
    (surface, persona, state, worst, n, reason).
  - `health.expired` [str]: `lint.rules.ExpiredRecord.run_health_line()` of every record that stopped applying at
    run start (4.8 L-MSK-02, L-RGN-05, L-SRF-11), the "Expired records" row. `health.redaction` {base, candidate}:
    the run's redaction box counts per side, summed over its checkpoints (`capture.RedactionResult.counts`, 3.6),
    the "Redaction" row with the run's `redaction_miss` count.
  - `abort` {incident, trigger, gate, screenshot, rig, login, proof, rule_ids, not_requeued, requeued, rebuild, deadline,
    last_heartbeat, runs_ahead, cost, cap}: the abort block facts of `ReportContext.abort`, persisted with the run;
    a key the context also sets takes the context's value. `abort.incident` is the id that
    `store.safety.apply_safety_actions` returned (`AppliedSafety.incident_id`): the commit status reads
    `aborted_safety: incident {id}` and the abort block `Incident: {id}`.
  - `manifest_lint` [str]: `[v.line() for v in lint.runstart.lint_manifest(...)]` of a manifest that failed the
    lint and is treated as missing (4.3); printed in the Intended section with the manifest template link.
  - `expiring` [str]: `lint.runstart.ExpiryWarning.line()` of every mask that expires within seven days
    (`expiring_soon`), the owner warning of 3.8, the "Masks expiring within 7 days" row.
  - `sweep_minimums` [{surface_id, host, w, h, clamped_w, clamped_h}]: the run's achieved minimum per gadget and
    host (`capture.sweep.minimums_summary`, 2.5.1), one "Sweep minimums" line each (`MinSize.report_line`).
  - `health_rows` [[label, text]]: further run-health rows the run adds as they are (the popup rate, the
    fingerprint notes, the Phase 0 branches, the decision lifecycle lines), each printed only when present.
  - `post_run_check` {failures [str], report_withheld bool}: what `store.checks.end_taxonomy_check_failed` writes.
    `report_withheld` makes the comment and the page the abort block alone, with each failure (1.4 G2, L-OUT-07).
  - `safety` {banner, nightly_run_id}: `orchestrator.safety_canaries.banner(results)` of the last Nightly (null when
    no safety canary missed), printed after the detector banner, and that Nightly's run id, whose `canary_results`
    rows `ReportInputs.from_store` reads.
  - `alerts` [entry]: the run's 9.10 alerts for the page's Alerts panel (9.5), each a
    `pipeline.orchestration.alert_entry` dict (run_id, shard_id, rule, priority, paged, notified, safety_table,
    control, gate, incident, run_outcome, run_reason, detail) plus `account`, `working_orders_screenshot` (a sha256)
    and `instruction`, copied from the control result's evidence when it has them (6.6 Cancel All failure). An entry
    of another shape (the 10.2 late arrival `{alert, closed_as, at}`) prints its keys.
- `SafetyRows`: the store rows the Safety row and the page's Safety proof section print (9.4 item 8): the run's
  `post_run_proofs` (6.6; each proof's declared sends, `orchestrator.killswitch.send_lines(counts)`, and its buying
  power line, `counts["rows"]["buying power"]`), the last Nightly's `canary_results` rows of kinds `fire_drill`
  (6.5) and `safety_canary` (10.3), the open `incidents` and the overdue postmortems (`store.safety.incidents(
  open_only=True)`, `overdue_postmortems`, 6.12).

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
- "Why unexpected" on an invariant or assertion cluster of an undeclared surface that carries a
  primitive and a manifest entry gives the undeclared-surface explanation, as 9.3 item 1 does
  (the invariant itself is in "What moved"); otherwise it states the invariant (2.7) or assertion.
- A census cluster of several surfaces in one workspace is described as "one census checkpoint
  per surface" (9.3 says "per cell" for its Flexible Grid cells; no record carries that noun).
- Comparable scope for the coverage percent is in-scope cells minus `not_run (entitlement)`
  minus not-compared (blocked) cells (2.12: blocked cells are not in the coverage percent). Every outcome a
  `blocked:<outcome>` cell can name, `harness_error` included, is a Not compared column entry (`NOT_COMPARED`).
- The Coverage table keeps the 2.12 surface-and-persona cells; beneath it one line counts the run's `not_run`
  checkpoints per reason ("Checkpoints not run: 7 app_crash (sweep steps after the crash)", `not_run_line`), so
  checkpoints that a cell's worse outcome hides from the table are still shown; no line when none is not_run.
- A run that stopped before scope (no coverage tiers and no scope facts) prints "Scope not computed" in place
  of an empty Coverage table, and an unread fingerprint prints "not read". An `aborted_safety` that disabled
  several rigs names them all; an `aborted_harness` that re-queuing cannot fix prints "Not re-queued: {why}"
  (abort key `not_requeued`). ORDERS SENT, the most severe proof verdict (6.6), adds the blocked login and
  disabled rig to the Safety row and the abort block (`ORDERS_SENT_NOTE`).
- `safety.login` is the readable label of the login the worst proof blocks, or a list of them when several
  shards' proofs block theirs ("logins A and B"); a `vault://` reference handed in place of a label is never
  printed (`login_phrase`, 6.9).
- `aborted_safety` and `aborted_harness` replace Unexpected and Intended with the abort block
  and keep Suppressed, Coverage and Run health; `pool_saturated` and `dead_man` comments carry
  only the header and the abort text; a secret leak carries only the header, result, check and
  "report withheld; security contact notified".
- A `redaction_miss` finding (3.6) is an Unexpected item of its own, ranked after `unknown_popup` (9.2 does not list
  it), tier 1 when the miss is on the candidate only (a suspected identifier leak), else its finding's tier; it never
  fails the check (the anchor list is the security contact's, not the PR's), and it is never a checkpoint's primary
  finding.
- G2 (1.4): when `coverage.matrix` is given and `matrix_failures` finds the cells do not sum, the Coverage table and
  the matrix are replaced by "Coverage matrix withheld" and the failures; the not_run checkpoint line stays (it
  counts checkpoints, not cells) and the commit status is unchanged (the post-run check ends the run).
- The manifest template link is the report UI URL `/docs/manifest-template` (9.1: the only links are report UI
  URLs). It follows the no-manifest line whenever no manifest is loaded (4.3 case (a), case (b) and a manifest that
  failed the lint, 2.10.2), after the lint lines. A loaded manifest with no entries prints its `empty_reason` (4.3).
- Run health gains a row only when it has something to say: Redaction, Expired records, Masks expiring within 7
  days, Fire drill (last Nightly), Safety canaries (last Nightly) and Incidents (open incidents and overdue
  postmortems). The Safety row adds each proof's declared sends and buying power line, prefixed with the shard when
  the run has several proofs; so does the abort block's post-run proof line. A proof row's login alias is never
  printed (8.3).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..canary import CanaryResult
from ..orchestrator.killswitch import CANCEL_ALL_CODES, CANCEL_ALL_TEXT, proof_line, send_lines
from ..records import CheckpointOutcome, Decision, Finding, RunOutcome, StepOutcome
from ..store import safety as store_safety
from ..store.checks import matrix_failures
from ..store.safety import Incident
from ..store.store import PostRunProof
from ..taxonomy import checkpoint_parts, load
from .cluster import Cluster, Member, cluster_members
from .inline import C, Image, Line, Link, T, Urls, join_and, number_word, plural, words_and

if TYPE_CHECKING:
    from ..store.store import Store

CHECK_CONTEXT = "sentinel/pr-run"
CHECK_MODES = ("informational", "blocking")

# 9.2 Unexpected kind order (rank groups); `redaction_miss`, which 9.2 does not list, ranks last (module docstring).
(APP_ALIVE, INVARIANT, ASSERTION, COLLATERAL, NO_MATCH, LANDMARK, UNREGISTERED, NEEDS_HUMAN,
 NEEDS_BASELINE, STEP_FAILED, AGENT_FALLBACK, MAP_GAP, MANIFEST_MISSING, UNKNOWN_POPUP, REDACTION_MISS) = range(15)
FAILURE_GROUPS = (APP_ALIVE, INVARIANT, ASSERTION)
# 9.2 commit status: tier-1 kinds that fail the check when undecided.
CHECK_FAILING_GROUPS = {APP_ALIVE, INVARIANT, ASSERTION, COLLATERAL, NO_MATCH, LANDMARK, NEEDS_HUMAN,
                        STEP_FAILED}
FINDING_ONLY = {"dependency_map_gap": MAP_GAP, "manifest_missing": MANIFEST_MISSING,
                "unknown_popup": UNKNOWN_POPUP, "redaction_miss": REDACTION_MISS}
# `canary_results` kinds of the Safety proof section: the fire drill (6.5) and the safety canaries (10.3, SD-81)
SAFETY_CANARY_KINDS = ("fire_drill", "safety_canary")
# 1.4 G2: what Coverage prints in place of the table and the matrix when the cells do not sum
G2_WITHHELD = ("Coverage matrix withheld: its cells do not sum to surfaces x personas x entitlement profiles "
               "(1.4 G2), so the Reporter publishes none")
NOT_RUN_ORDER = ("scope", "budget", "entitlement", "quarantine", "step_failed", "base_step_failed",
                 "app_crash", "model_unavailable")
# 2.12: the outcomes a `blocked:<outcome>` cell names, which stopped comparison: the Not compared column (9.2)
NOT_COMPARED = ("env_error", "harness_error", "not_settled", "not_comparable", "landmark_missing", "not_run")
# 1.10 not_run reasons, as the checkpoint line under the Coverage table says which checkpoints they are (9.2)
NOT_RUN_WHICH = {
    "scope": "outside the run's scope",
    "budget": "cut by the budget",
    "entitlement": "the login is not entitled to",
    "quarantine": "in quarantine",
    "step_failed": "after a failed step",
    "base_step_failed": "the base build could not reach",
    "app_crash": "after the crash",
    "model_unavailable": "skipped while the model was unavailable",
}
CHECKPOINT_NOUNS = {"sweep": "sweep step", "split": "splitter step", "flow": "flow checkpoint",
                    "census": "census checkpoint"}

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
    "rescued_capture": ("Captured after the scripted locator broke (passed_via_agent_fallback); a rescued "
                        "capture never inherits a decision."),
    "global_shift": "Text-edge-only diff under the run-level global_rendering_shift finding.",
    "oracle_unreadable": "The Oracle could not read the value.",
    "bounds_unreadable": "The Invariant Checker could not read the bounds.",
}
FIELD_EVIDENCE = "Evidence at 1:1"


class ReportInputError(ValueError):
    """The inputs cannot be rendered faithfully (unknown mode, vocabulary or shape)."""


# 6.6: the post-run proof verdict forms the Reporter prints (`Proof.line`), least to most severe. ORDERS SENT means
# a clean read found an undeclared order row: an order was sent (the run is aborted_safety, S1).
PROOF_FORMS = ("PROVEN: ", "UNPROVEN: ", "ORDERS SENT: ")
PROVEN_PROOF, UNPROVEN_PROOF, ORDERS_SENT_PROOF = range(3)
ORDERS_SENT_NOTE = ("an order reached the account: {login} blocked and {rig} disabled until a human verifies "
                    "the account and the incident closes")
# what a login prints as when the Reporter is handed its vault reference (4.9 `login_ref`), which it never prints
VAULT_LOGIN = "(vault reference not printed)"


def login_phrase(login: str | list[str] | tuple[str, ...] | None) -> str:
    """9.2's "login {alias}" for the login or logins a proof blocks: `login rig-01 paper login`, or `logins A and B`
    for several. Each is the readable label the run gives (6.4); a `vault://` reference is never printed (6.9,
    4.9 L-FIX-04) and reads as `VAULT_LOGIN`."""
    names = [login] if isinstance(login, str) or login is None else list(login)
    shown = list(dict.fromkeys(VAULT_LOGIN if "vault://" in (n or "") else (n or "unknown") for n in names))
    return ("login " if len(shown) <= 1 else "logins ") + (words_and(shown) or "unknown")


def orders_sent_note(login: str | list[str] | tuple[str, ...] | None) -> str:
    """`ORDERS_SENT_NOTE` for the login or logins a proof blocks (`login_phrase`)."""
    several = not (isinstance(login, str) or login is None) and len(set(login)) > 1
    return ORDERS_SENT_NOTE.format(login=login_phrase(login), rig="rigs" if several else "rig")


def proof_severity(proof: str) -> int:
    """How severe a post-run proof verdict is: `PROVEN_PROOF`, `UNPROVEN_PROOF` or `ORDERS_SENT_PROOF`, the most
    severe (6.6). Any other text is refused."""
    for severity, form in enumerate(PROOF_FORMS):
        if proof.startswith(form):
            return severity
    raise ReportInputError(f"post-run proof verdict {proof!r} (6.6)")


def worst_proof(proofs: list[str]) -> str:
    """The most severe of several proof verdicts (one per shard), the first of equals; 6.6, 8.2."""
    if not proofs:
        raise ReportInputError("no post-run proof verdict (6.6)")
    return max(proofs, key=proof_severity)


def _money(x: Any) -> str:
    return "not read" if x is None else f"{float(x):,.2f}"


def _drift_line(name: str, row: Mapping[str, Any]) -> str:
    return (f"{name} {_money(row.get('start'))} to {_money(row.get('end'))}, drift {_money(row.get('drift'))}, "
            f"{row.get('verdict', 'unknown')}")


def buying_power_line(row: Mapping[str, Any]) -> str:
    """The buying power line of a proof (6.6 step 5), from its `counts["rows"]["buying power"]`."""
    return _drift_line("buying power", row)


def proof_detail_lines(proofs: Sequence[PostRunProof]) -> list[str]:
    """Each proof's declared sends (`killswitch.send_lines`) and buying power line, in proof order; prefixed with the
    shard ("run level" for the SD-61 row) when the run has several proofs (6.6, 6.7)."""
    out = []
    for p in proofs:
        lines = send_lines(p.counts)
        bp = (p.counts.get("rows") or {}).get("buying power")
        if bp:
            lines.append(buying_power_line(bp))
        prefix = f"shard {p.shard_id or 'run level'}: " if len(proofs) > 1 else ""
        out += [prefix + line for line in lines]
    return out


@dataclass(frozen=True)
class SafetyRows:
    """The store rows the Safety row and the Safety proof section print (module docstring; 6.5, 6.6, 6.12, 9.4)."""

    proofs: tuple[PostRunProof, ...] = ()        # the run's post_run_proofs (Store.post_run_proofs)
    canaries: tuple[CanaryResult, ...] = ()      # the last Nightly's fire_drill and safety_canary rows
    nightly_run_id: str | None = None            # the Nightly those rows belong to
    incidents: tuple[Incident, ...] = ()         # open incidents (store.safety.incidents(open_only=True))
    overdue: tuple[Incident, ...] = ()           # S1 and S2 postmortems past due (store.safety.overdue_postmortems)

    def __post_init__(self) -> None:
        for r in self.canaries:
            if r.kind not in SAFETY_CANARY_KINDS:
                raise ReportInputError(f"canary row {r.mutant_id} of kind {r.kind!r}: the Safety section prints "
                                       f"{SAFETY_CANARY_KINDS} rows (6.5, 10.3)")

    @classmethod
    def from_store(cls, store: "Store", run_id: str, *, nightly_run_id: str | None = None,
                   at: str | None = None) -> "SafetyRows":
        """Read the rows of `run_id` (its proofs) and of `nightly_run_id` (its drill and safety canary rows), and the
        open incidents and postmortems overdue at `at` (default now)."""
        canaries = tuple(r for r in store.canary_results(nightly_run_id) if r.kind in SAFETY_CANARY_KINDS) \
            if nightly_run_id else ()
        return cls(tuple(store.post_run_proofs(run_id)), canaries, nightly_run_id,
                   store_safety.incidents(store, open_only=True), store_safety.overdue_postmortems(store, at=at))


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
    abort: dict = field(default_factory=dict)    # abort block facts, see `_abort`
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
    coverage_cells: list[dict]               # 2.12 matrix cells from the summary (`coverage_cell` form), when given
    scope_line: Line
    not_run_line: Line | None                # 9.2: every not_run reason among the run's checkpoints, counted
    health_rows: list[tuple[str, Line]]
    history: tuple[dict, ...]
    outcomes: list[CheckpointOutcome]
    manifest_lint_lead: Line | None = None   # 4.3: the manifest failed the lint and is treated as missing
    manifest_lint: list[Line] = field(default_factory=list)      # one code line per lint violation
    manifest_template: Line | None = None    # the template link, whenever no manifest is loaded (4.3, 2.10.2)
    empty_reason_line: Line | None = None    # 4.3: a loaded manifest with no entries
    g2_failures: list[str] = field(default_factory=list)         # 1.4 G2: the matrix is withheld when non-empty
    safety_proof: list[tuple[str, list[Line]]] = field(default_factory=list)   # 9.4 item 8, for the page
    alerts: list[Line] = field(default_factory=list)             # 9.5 Alerts panel, one line per alert

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
        if text and rng and not text.endswith((".", "!", "?")):
            text += "."                                   # a record's description may lack the full stop
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
        ordered = sorted(cl.members, key=lambda m: m.checkpoint_id)
        workspaces = list(dict.fromkeys(m.outcome.workspace_id for m in ordered))
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
        window = ev.get("target_window")
        if isinstance(window, dict):          # the flow file's {kind, title_regex} (4.7)
            window = window.get("kind")
        if window:
            parts.append((T(f"window {window}"),))
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
        called = cl.unsuppressed or cl.members     # a suppressed member skipped the Reviewer (9.7)
        n = len(called)
        if cl.kind == "sweep" and n > 1:
            sizes = Cluster(called).sizes
            call = (f"one call for the sweep cluster with the crops of its smallest and largest steps, "
                    f"{_size(sizes[0])} and {_size(sizes[-1])}")
        elif cl.kind == "split" and n > 1:
            fr = sorted({m.fraction for m in called})
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
                 ctx: ReportContext, steps, summary: Mapping[str, Any] | None = None,
                 safety: SafetyRows | None = None):
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
        self.summary: Mapping[str, Any] = summary or {}
        self.safety = safety or SafetyRows()
        self.abort: dict = {**(self.summary.get("abort") or {}), **ctx.abort}   # the context's facts win
        self.urls = Urls(ctx.base_url.rstrip("/"), run.run_id)
        self.ladder = "F" if run.run_type == "rc" else "S"
        self.declared = {s for e in (manifest or {}).get("entries", []) for s in e.get("declared_surfaces", [])}
        self.by_cp: dict[str, list[Finding]] = {}
        for f in self.findings:
            if f.checkpoint_id and f.kind != "redaction_miss":       # an item of its own, never a primary finding
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
            if (s.status == "failed" and side != "candidate") or (f is not None and f.decision_ref):
                continue
            group = STEP_FAILED if s.status == "failed" else AGENT_FALLBACK
            tier = f.tier if f else None
            key = (tier if tier is not None else 4, group, (1, 0), 0, 0, f"{s.flow_id}/{s.step_id}")
            out.append(Item(0, tier, group, (), [], decided=False, pending=False, reviewer_call=False, step=s,
                            finding=f, sort_key=key))
        return out

    def finding_items(self) -> list[Item]:
        out = []
        for f in self.findings:
            group = FINDING_ONLY.get(f.kind)
            if group is None or f.decision_ref:
                continue
            tier = f.tier
            if group == REDACTION_MISS and _candidate_only(f):
                tier = 1                                   # 3.6: a suspected identifier leak ranks tier 1
            key = (tier if tier is not None else 4, group, (1, 0), 0, 0, f.finding_id)
            out.append(Item(0, tier, group, (), [], decided=False, pending=False, reviewer_call=False,
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
            header: Line = (C(surface), T(f", {n} {verb} {entry_id}" if entry_id else
                                          f", {plural(n, 'checkpoint')} intended"))
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

    def reconciliation(self) -> Line | None:
        """9.2 Manifest reconciliation, one clause per entry (2.12 manifest coverage table)."""
        entries = (self.manifest or {}).get("entries", [])
        if not entries:
            return None
        parts: list[Line] = []
        tasks = self.coverage.get("flow_tasks", {})
        author = (self.manifest or {}).get("author", "the PR author")
        for e in entries:
            eid = e.get("entry_id", "?")
            visible = [o for o in self.outcomes if o.outcome == "intended" and o.manifest_entry_ref == eid]
            covering = sorted((o for o in self.outcomes if o.surface_id in e.get("declared_surfaces", [])),
                              key=lambda o: o.checkpoint_id)
            ids = [o.checkpoint_id for o in covering]
            if visible:
                parts.append((T(f"{eid}: verified visible on {plural(len(visible), 'checkpoint')}"),))
            elif covering and all(o.outcome in ("identical", "noise") for o in covering):
                parts.append((T(f"{eid}: covering checkpoints "),) + _codes(ids) +
                             (T(" showed no change above floor ("), C("intended_change_not_observed"),
                              T(f"; a question for {author} and the feature owner, not a bug)")))
            elif covering:
                parts.append((T(f"{eid}: not verified; covering checkpoints "),) + _codes(ids) +
                             (T(" have no intended outcome"),))
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
                      ctx: ReportContext, mode: str, target_url: str | None, *, abort: Mapping[str, Any] | None = None,
                      withheld: bool = False) -> CommitStatus:
    """The 9.2 state table applied to ranked Unexpected items. `abort` is the abort block facts (the summary's
    under the context's, default the context's); `withheld` is a report the post-run check withheld (1.4 G2)."""
    if mode not in CHECK_MODES:
        raise ReportInputError(f"check mode {mode!r}; expected one of {CHECK_MODES}")
    reason = run.reason or ""
    abort = ctx.abort if abort is None else abort

    def st(state: str, text: str) -> CommitStatus:
        return CommitStatus(state, text, mode, target_url)

    if run.status == "aborted_safety":
        return st("error", f"aborted_safety: incident {abort.get('incident', 'unknown')}")
    if run.status == "aborted_harness":
        if reason in ("dead_man", "secret_leaked"):
            return st("error", "Sentinel did not report")
        return st("error", f"aborted_harness: {reason}")
    if run.status == "aborted_budget" and reason == "pool_saturated":
        return st("error", "Sentinel did not run")
    if ctx.comment_overdue or withheld:
        return st("error", "Sentinel did not report")
    if ctx.override:
        return st("success", f"forced success by {ctx.override.get('by', '?')}: {ctx.override.get('reason', '')}")
    failing = [i for i in items if i.tier == 1 and not i.decided and i.group in CHECK_FAILING_GROUPS]
    if failing:
        noun = "finding has" if len(failing) == 1 else "findings have"
        return st("failure", f"{len(failing)} tier-1 {noun} no decision")
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


def coverage_cell(c: Mapping[str, Any]) -> dict:
    """One summary matrix cell in the form the page prints, from the 8.3 `coverage_cells` row form
    (`coverage.Cell.to_row`) or the short form (module docstring): surface, persona, entitlement, state, worst, n,
    reason."""
    return {"surface": c.get("surface", c.get("surface_id")), "persona": c.get("persona"),
            "entitlement": c.get("entitlement", c.get("profile")), "state": c.get("state"),
            "worst": c.get("worst", c.get("worst_outcome")), "n": int(c.get("n", c.get("checkpoint_count")) or 0),
            "reason": c.get("reason", c.get("not_run_reason"))}


def g2_failures(coverage: Mapping[str, Any]) -> list[str]:
    """1.4 G2 on the summary's matrix: `store.checks.matrix_failures` of `coverage.cells` against `coverage.matrix`
    {surfaces, personas, profiles}; empty when the summary gives no dimensions (module docstring)."""
    dims = coverage.get("matrix")
    if not dims:
        return []
    cells = [{"surface_id": c["surface"], "persona": c["persona"], "entitlement": c["entitlement"]}
             for c in map(coverage_cell, coverage.get("cells", []))]
    try:
        return matrix_failures(cells, int(dims["surfaces"]), int(dims["personas"]), int(dims["profiles"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ReportInputError(f"coverage.matrix {dims!r}: surfaces, personas and profiles are counts (2.12)") from e


def not_run_line(outcomes: list[CheckpointOutcome]) -> Line | None:
    """9.2's line under the Coverage table: the checkpoint count of every `not_run` reason in the run, in 1.10 order,
    each with which checkpoints they are ("Checkpoints not run: 7 app_crash (sweep steps after the crash)"). The
    table counts surface-and-persona cells (2.12), where a cell takes its worst outcome, so not_run checkpoints
    inside a cell that also holds a worse outcome show only here. None when no checkpoint is not_run."""
    by: dict[str, list[CheckpointOutcome]] = {}
    for o in outcomes:
        if o.outcome == "not_run" and o.reason:
            code = o.reason.split(":", 1)[0]                # step_failed:<step> counts under step_failed
            if code not in NOT_RUN_ORDER:
                raise ReportInputError(f"not_run reason {code!r} is not in 1.10")
            by.setdefault(code, []).append(o)
    if not by:
        return None
    parts = []
    for code in sorted(by, key=NOT_RUN_ORDER.index):
        kinds = {checkpoint_parts(o.checkpoint_id)["kind"] for o in by[code]}
        noun = CHECKPOINT_NOUNS.get(kinds.pop(), "checkpoint") if len(kinds) == 1 else "checkpoint"
        n = len(by[code])
        parts.append(f"{n} {code} ({noun if n == 1 else noun + 's'} {NOT_RUN_WHICH[code]})")
    return (T("Checkpoints not run: " + ", ".join(parts)),)


def scope_line(b: _Builder) -> Line:
    if not b.coverage.get("tiers") and not b.coverage.get("scope"):
        return (T(f"Scope not computed: the run stopped at {b.run.last_gate} before it planned any checkpoint."),)
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
    if s.get("map_patch"):                               # 4.4: the map PR's proposed edges, in the run output
        line.append(T(f"; proposed map edges in {s['map_patch']}"))
    line.append(T("."))
    if s.get("send_policy"):
        orders = plural(int(s.get("order_surfaces_entitlement", 0)), "order lifecycle surface")
        line += [T(f" Send policy {s['send_policy']}: {orders} "),
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


def _safety_row(s: dict, details: list[str]) -> Line:
    proof = s.get("proof", "unknown")
    severity = proof_severity(proof)
    text = (f"attestation {s.get('attestation', 'unknown')}, paper mode asserted "
            f"{plural(int(s.get('paper_mode_asserted', 0)), 'time')}, "
            f"gateway {plural(int(s.get('gateway_drops', 0)), 'drop')}, "
            f"Cancel All {s.get('cancel_all', 'unknown')}, proof {proof}")
    if details:                                  # the declared sends and buying power of each proof (6.6)
        text += "; " + "; ".join(details)
    login = s.get("login", "unknown")
    if severity == ORDERS_SENT_PROOF:
        text += "; " + orders_sent_note(login)
    elif severity == UNPROVEN_PROOF:
        text += f"; next run on {login_phrase(login)} blocked until a human verifies the account"
    return (T(text),)


def _candidate_only(f: Finding) -> bool:
    return bool(f.evidence.get("candidate_only")) or f.detail == "candidate_only"


def _redaction_row(b: _Builder) -> Line | None:
    """3.6: the run's redaction box counts per side and its `redaction_miss` findings; None with neither."""
    counts = b.health.get("redaction")
    misses = [f for f in b.findings if f.kind == "redaction_miss"]
    if counts is None and not misses:
        return None
    lead = f"boxes: base {int(counts.get('base', 0))}, candidate {int(counts.get('candidate', 0))}; " \
        if counts is not None else ""
    only = sum(1 for f in misses if _candidate_only(f))
    tail = f" ({only} candidate only, tier 1)" if only else ""
    return (T(f"{lead}{len(misses)} "), C("redaction_miss"), T(tail))


def _canary_rows_line(rows: Sequence[CanaryResult], nightly: str | None) -> Line:
    """The last Nightly's drill items or safety canaries: how many were detected, and each miss with the outcome it
    produced (6.5, 10.3)."""
    text = f"{sum(1 for r in rows if r.state == 'detected')} of {len(rows)} detected"
    if nightly:
        text += f" (Nightly run {nightly})"
    for r in rows:
        if r.state != "detected":
            text += f"; {r.mutant_id} {r.state}"
            if r.produced_outcome:
                text += f": {r.produced_outcome}" + (f" (reason: {r.produced_reason})" if r.produced_reason else "")
    return (T(text),)


def _incident_text(i: Incident) -> str:
    where = [x for x in (f"rig {i.rig_id}" if i.rig_id else "", f"run {i.run_id}" if i.run_id else "") if x]
    return f"{i.incident_id} {i.severity} ({i.trigger})" + (f" on {', '.join(where)}" if where else "")


def _overdue_text(i: Incident) -> str:
    return f"{i.incident_id} {i.severity} ({i.trigger}), due {i.postmortem_due_at}"


def _incidents_line(s: SafetyRows) -> Line:
    """6.12: the open incidents, and the S1 and S2 postmortems not linked by their due date."""
    parts = []
    if s.incidents:
        parts.append(f"{len(s.incidents)} open: " + ", ".join(_incident_text(i) for i in s.incidents))
    if s.overdue:
        parts.append("postmortem overdue: " + ", ".join(_overdue_text(i) for i in s.overdue))
    return (T("; ".join(parts)),)


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
        to = f" to {words_and(slack['to'])}" if slack.get("to") else ""
        out.append(T(f"{len(posts)} posted{to} ("))
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
    shards = plural(int(h.get("shards", 1)), "shard")
    wall = f"{h.get('wall_clock', 'unknown')} / {h.get('budget', '45:00')} on {shards}"
    if b.ctx.report_late:
        wall += "; report_late"
    fp = h.get("fingerprint", {})
    cand, base = fp.get("candidate", "unknown"), fp.get("base", "unknown")
    fp_text = f"candidate {cand} = base {base}" if cand == base else f"candidate {cand} != base {base}"
    if "unknown" in (cand, base):
        fp_text = "not read"
    elif cand == base and b.ctx.candidate_build != b.ctx.base_build:
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
    redaction = _redaction_row(b)
    if redaction is not None:
        rows.append(("Redaction", redaction))
    if h.get("expired"):                          # records that stopped applying at run start (4.8 L-MSK-02)
        rows.append(("Expired records", (T("; ".join(h["expired"])),)))
    if b.summary.get("expiring"):                 # the seven-day owner warning (3.8)
        rows.append(("Masks expiring within 7 days", (T("; ".join(b.summary["expiring"])),)))
    if b.summary.get("sweep_minimums"):           # 2.5.1: the achieved minimum of every gadget and host
        from ..capture.sweep import MinSize
        lines = [MinSize(**m).report_line() for m in b.summary["sweep_minimums"]]
        rows.append(("Sweep minimums", (T("; ".join(lines)),)))
    for label, text in b.summary.get("health_rows") or ():
        rows.append((str(label), (T(str(text)),)))
    if "canary" in h:
        rows.append(("Canary (last Nightly)", _canary_row(h["canary"])))
    if "safety" in h:
        rows.append(("Safety", _safety_row(h["safety"], proof_detail_lines(b.safety.proofs))))
    s = b.safety
    for kind, label in (("fire_drill", "Fire drill (last Nightly)"),
                        ("safety_canary", "Safety canaries (last Nightly)")):
        rows_of = [r for r in s.canaries if r.kind == kind]
        if rows_of:
            rows.append((label, _canary_rows_line(rows_of, s.nightly_run_id)))
    if s.incidents or s.overdue:
        rows.append(("Incidents", _incidents_line(s)))
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

def _withheld(b: _Builder, check: Mapping[str, Any]) -> tuple[str, str, list[Line]]:
    """1.4 G2, 4.13 L-OUT-07: the post-run check failed and withheld the report; the abort block alone is posted. A
    safety abort keeps its safety lines (6.6, 9.2): the trigger, the gate, the post-run proof and the incident, which
    are run facts, not findings, matrix or counts."""
    run, a = b.run, b.abort
    lines: list[Line] = []
    if run.status == "aborted_harness":
        lines.append((T("Reason: "), C(run.reason or "")))
    if run.status == "aborted_safety":
        proof = a.get("proof") or b.health.get("safety", {}).get("proof", "unknown")
        lines += [(T(f"Trigger: {a.get('trigger', run.reason)}"),), (T(f"Gate: {a.get('gate', run.last_gate)}"),),
                  (T(f"Post-run proof: {proof}"),), (T(f"Incident: {a.get('incident', 'unknown')}"),)]
    else:
        lines.append((T(f"Last gate reached: {run.last_gate}"),))
    lines += [(T(f"Post-run check: {f}"),) for f in check.get("failures") or ()]
    lines.append((T("Report withheld: the post-run check failed (1.4 G2, 4.13 L-OUT-07), so no finding, matrix or "
                    "count is published"),))
    return "withheld", "Abort", lines


def _abort(b: _Builder, members: list[Member]) -> tuple[str | None, str | None, list[Line]]:
    run, a = b.run, b.abort
    check = b.summary.get("post_run_check") or {}
    if check.get("report_withheld"):
        return _withheld(b, check)
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
            text += (f"; per-run model spend ceiling reached ({a.get('cost', '?')} of {a.get('cap', '?')}, "
                     f"11.4 Cost model)")
        return None, None, [(T(text),)]
    if run.status == "aborted_safety":
        lines: list[Line] = [(T(f"Trigger: {a.get('trigger', reason)}"),),
                             (T(f"Gate: {a.get('gate', run.last_gate)}"),)]
        img = b.urls.image("screenshot", a.get("screenshot"))
        if img is not None:
            lines.append((T("Last screenshot: "), Link("screenshot", img.url)))
        safety = b.health.get("safety", {})
        proof = a.get("proof") or safety.get("proof", "unknown")
        lines.append((T(f"Post-run proof: {proof}"),))
        lines += [(T(d),) for d in proof_detail_lines(b.safety.proofs)]
        if proof != "unknown" and proof_severity(proof) == ORDERS_SENT_PROOF:
            note = orders_sent_note(a.get("login") or safety.get("login", "unknown"))
            lines.append((T(note[0].upper() + note[1:] + "."),))
        rigs = a.get("rig", "unknown")
        rigs = [rigs] if isinstance(rigs, str) else list(rigs)
        lines += [(T(f"Incident: {a.get('incident', 'unknown')}"),),
                  (T(f"No run starts on {'rig' if len(rigs) == 1 else 'rigs'} {words_and(rigs)} until the incident "
                     "closes."),)]
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
        if reason == "base_build_missing" and a.get("trigger"):     # 3.1, 4.2: why there is no base
            lines.append((T(f"Trigger: {a['trigger']}"),))
        if a.get("rebuild"):                                       # 3.1: the pipeline builds the merge base
            lines.append((T(f"Rebuild requested: main at merge base {str(a['rebuild'])[:7]}"),))
        lines.append((T(f"Last gate reached: {run.last_gate}"),))
        img = b.urls.image("screenshot", a.get("screenshot"))
        if img is not None:
            lines.append((T("Screenshot: "), Link("screenshot", img.url)))
        if a.get("not_requeued"):                        # re-queuing cannot help, e.g. a config that fails lint
            lines.append((T(f"Not re-queued: {a['not_requeued']}"),))
        else:
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
    safety_banner = (b.summary.get("safety") or {}).get("banner")    # the blind safety canaries (10.3)
    if safety_banner:
        banner = f"{banner}\n{safety_banner}" if banner else safety_banner
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


def _manifest_notes(b: _Builder) -> tuple[Line | None, list[Line], Line | None, Line | None]:
    """The Intended section's manifest notes (4.3, 2.10.2): the lead and lines of a manifest that failed the lint,
    the template link whenever no manifest is loaded, and a loaded manifest's `empty_reason` when it has no entries."""
    if b.manifest is None:
        lint = [str(x) for x in b.summary.get("manifest_lint") or ()]
        lead = (T("The manifest failed the lint and is treated as missing (4.3):"),) if lint else None
        template = (Link("Manifest template", b.urls.manifest_template), T(" (4.3)"))
        return lead, [(C(x),) for x in lint], template, None
    empty = None
    if not b.manifest.get("entries") and b.manifest.get("empty_reason"):
        empty = (T(f"No manifest entries: {b.manifest['empty_reason']}"),)
    return None, [], None, empty


# --------------------------------------------------------------------------------------------
# Safety proof section and Alerts panel (6.5, 6.6, 6.12, 9.4 item 8, 9.5)
# --------------------------------------------------------------------------------------------

# the 8.3 `post_run_proofs.cancel_all` code as the 6.6 block says it (verified, failed, not needed)
_CANCEL_ALL_SAID = {code: CANCEL_ALL_TEXT[k] for k, code in CANCEL_ALL_CODES.items()}


def _count(x: Any) -> str:
    return "not read" if x is None else f"{float(x):g}"


def _proof_block(p: PostRunProof) -> tuple[str, list[Line]]:
    """One `post_run_proofs` row as the 6.6 ORDER SAFETY PROOF block prints it; the login alias is never printed."""
    title = f"ORDER SAFETY PROOF, shard {p.shard_id}" if p.shard_id else \
        "ORDER SAFETY PROOF, run level (no shard started)"
    lines: list[str] = [f"VERDICT {proof_line(p.verdict, p.reason, p.counts.get('undeclared') or [], p.cancel_all)}",
                        f"identity: paper mode asserted {plural(p.paper_mode_assertions, 'time')}, "
                        f"{p.paper_mode_mismatches} mismatches, {p.unreadable_count} unreadable"]
    if p.attestation_ref:
        lines.append(f"attestation {p.attestation_ref}")
    lines += [f"send policy {p.send_policy_ref}",
              f"gateway {plural(p.gateway_events, 'event')} allowed, {plural(p.gateway_drops, 'drop')}, "
              f"{plural(p.refusals_count, 'refusal')}"]
    for name, row in (p.counts.get("rows") or {}).items():
        if "drift" in row:
            lines.append(_drift_line(name, row))
        else:
            lines.append(f"{name}: start {_count(row.get('start'))}, end {_count(row.get('end'))}, new "
                         f"{row.get('new', 0)}, expected {row.get('expected', 0)}, {row.get('verdict', 'unknown')}")
    lines.append(f"cancel all {_CANCEL_ALL_SAID[p.cancel_all]}")
    lines += send_lines(p.counts)
    if p.verified_by:
        lines.append(f"verified by hand by {p.verified_by} at {p.verified_at}")
    out: list[Line] = [(T(x),) for x in lines]
    return title, out


def _canary_block(title: str, rows: Sequence[CanaryResult]) -> tuple[str, list[Line]]:
    out = []
    for r in rows:
        text = f"{r.mutant_id}: {r.state}"
        if r.produced_outcome:
            text += f", {r.produced_outcome}" + (f" (reason: {r.produced_reason})" if r.produced_reason else "")
        if r.absorbed_by:
            text += f": {r.absorbed_by}"
        out.append((T(text),))
    return title, out


def safety_proof_blocks(b: _Builder) -> list[tuple[str, list[Line]]]:
    """9.4 item 8 for the page: each proof block, the last Nightly's fire drill and safety canary rows, the blind
    safety canaries' banner, the open incidents and the overdue postmortems; empty when there is none of them."""
    s = b.safety
    blocks = [_proof_block(p) for p in s.proofs]
    night = f" (Nightly run {s.nightly_run_id})" if s.nightly_run_id else ""
    for kind, title in (("fire_drill", "Fire drill"), ("safety_canary", "Safety canaries")):
        rows = [r for r in s.canaries if r.kind == kind]
        if rows:
            blocks.append(_canary_block(title + night, rows))
    banner = (b.summary.get("safety") or {}).get("banner")
    if banner:
        blocks.append(("Safety canary banner", [(T(x),) for x in str(banner).splitlines()]))
    if s.incidents:
        blocks.append(("Open incidents", [(T(f"{_incident_text(i)}, opened {i.opened_at}"
                                            + (f", postmortem due {i.postmortem_due_at}" if i.postmortem_due_at
                                               else "")),) for i in s.incidents]))
    if s.overdue:
        blocks.append(("Postmortems overdue", [(T(_overdue_text(i) + ", no postmortem linked"),) for i in s.overdue]))
    return blocks


def _alert_line(b: _Builder, a: Mapping[str, Any]) -> Line:
    """One 9.10 alert of the summary, with the 6.6 hand-over facts (account, Working Orders screenshot, instruction)
    when the alert carries them."""
    if "rule" in a:
        text = f"{a.get('priority', '?')} {a['rule']}: {a.get('control', '?')} at {a.get('gate', '?')}"
        if a.get("incident"):
            text += f", incident {a['incident']}"
        if a.get("detail"):
            text += f": {a['detail']}"
        if a.get("paged"):
            text += f"; paged {', '.join(map(str, a['paged']))}"
    else:
        text = ", ".join(f"{k} {v}" for k, v in a.items()
                         if k not in ("account", "working_orders_screenshot", "instruction"))
    out: list = [T(text)]
    if a.get("account"):
        out.append(T(f"; account: {a['account']}"))
    shot = a.get("working_orders_screenshot")
    if shot:
        img = b.urls.image("Working Orders screenshot", shot)
        out += [T("; "), Link("Working Orders screenshot", img.url)] if img is not None else \
            [T(f"; Working Orders screenshot {shot}")]
    if a.get("instruction"):
        out.append(T(f"; instruction: {a['instruction']}"))
    return tuple(out)


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------

def build_report(run: RunOutcome, outcomes: list[CheckpointOutcome], findings: list[Finding],
                 decisions: list[Decision], coverage: dict | None, health: dict | None,
                 manifest: dict | None, context: ReportContext,
                 steps: list[StepOutcome] = (), *, summary: Mapping[str, Any] | None = None,
                 safety: SafetyRows | None = None) -> RunReport:
    """Arrange one run's records into the report of 9.2 (and 9.5 for the static page). `summary` is the run's
    `summary_json` and `safety` the store rows of the Safety section (module docstring); `ReportInputs.from_store`
    reads both, with `coverage` and `health` from the summary."""
    synthetic = {o.checkpoint_id for o in outcomes if o.synthetic}
    b = _Builder(run, outcomes, [f for f in findings if f.checkpoint_id not in synthetic],
                 decisions, coverage, health, manifest, context, steps, summary, safety)
    listed, intended = b.members()
    clusters = cluster_members(listed)
    items = [b.cluster_item(cl) for cl in clusters if not cl.fully_suppressed_one_verdict]
    items += b.step_items() + b.finding_items()
    items.sort(key=lambda i: i.sort_key)
    b.fill(items)
    intended_items, intended_clusters = b.intended_items(intended)
    special, abort_title, abort_lines = _abort(b, listed)
    banner, banner_lines = _banners(b, listed, items)
    status = status_from_items(run, items, b.outcomes, context, context.check_mode, b.urls.report, abort=b.abort,
                               withheld=special == "withheld")
    g2 = g2_failures(b.coverage)
    lint_lead, lint, template, empty = _manifest_notes(b)
    pending = sum(1 for i in items if i.pending)
    result = run.status
    if run.status.startswith("aborted_"):
        result += f" (reason: {run.reason})"
    elif pending:
        result += f" (unreviewed: {plural(pending, 'item')} pending review)"
    review_line = (T(f"Reviewer pending: {plural(pending, 'item')}; this comment is edited in place when "
                     f"verdicts land"),) if pending else None
    nb = sum(1 for o in b.outcomes if o.outcome == "needs_baseline" and o.surface_id in b.declared)
    nb_line = (T(f"{nb} checkpoints "), C("needs_baseline"),
               T(" on declared new surfaces; the merge run produces the candidates, nothing is approved from a "
                 "PR run")) if nb else None
    return RunReport(
        run=run, context=context, urls=b.urls, status=status, banner=banner, banner_lines=banner_lines,
        result=result, special=special, review_line=review_line, abort_title=abort_title,
        abort_lines=abort_lines,
        show_findings=run.status not in ("aborted_safety", "aborted_harness") and special is None,
        unexpected=items, intended=intended_items, manifest_line=_manifest_line(b),
        reconciliation=b.reconciliation(), needs_baseline_line=nb_line,
        suppressed=b.suppressed_rows(clusters),
        coverage_rows=[] if g2 else coverage_rows(b.coverage),                    # 1.4 G2: the matrix is withheld
        coverage_cells=[] if g2 else [coverage_cell(c) for c in b.coverage.get("cells", [])],
        scope_line=scope_line(b), not_run_line=not_run_line(b.outcomes),
        health_rows=health_rows(b, items, intended_clusters), history=tuple(context.history), outcomes=b.outcomes,
        manifest_lint_lead=lint_lead, manifest_lint=lint, manifest_template=template, empty_reason_line=empty,
        g2_failures=g2, safety_proof=safety_proof_blocks(b),
        alerts=[_alert_line(b, a) for a in b.summary.get("alerts") or ()])


@dataclass(frozen=True)
class ReportInputs:
    """Everything the Reporter reads for one finished run: its records, its `summary_json` and the Safety rows."""

    run: RunOutcome
    outcomes: list[CheckpointOutcome]
    findings: list[Finding]
    decisions: list[Decision]
    steps: list[StepOutcome]
    summary: dict
    safety: SafetyRows

    @classmethod
    def from_store(cls, store: "Store", run_id: str, *, at: str | None = None) -> "ReportInputs":
        """Read a finished run back from the store (9.2: rendered from the run's summary JSON at teardown, 8.7): the
        run row, outcomes, findings, step outcomes, the decisions they name, `Store.run_summary`, and the Safety rows
        (`SafetyRows.from_store` with the summary's `safety.nightly_run_id`); `at` dates the overdue postmortems."""
        run = store.run_outcome(run_id)
        if run is None:
            raise ReportInputError(f"run {run_id} has not ended; the Reporter reads a finished run (9.2)")
        summary = store.run_summary(run_id)
        outcomes, findings = store.outcomes(run_id), store.findings(run_id)
        ids = sorted({o.decision_ref for o in outcomes if o.decision_ref}
                     | {f.decision_ref for f in findings if f.decision_ref})
        decisions = [d for d in (store.get_decision(i) for i in ids) if d is not None]
        nightly = (summary.get("safety") or {}).get("nightly_run_id")
        safety = SafetyRows.from_store(store, run_id, nightly_run_id=nightly, at=at)
        return cls(run, outcomes, findings, decisions, store.step_outcomes(run_id), summary, safety)

    def build(self, manifest: dict | None, context: ReportContext) -> RunReport:
        """The report of these inputs; `manifest` is the run's loaded manifest (4.3) as a dict, None when missing."""
        return build_report(self.run, self.outcomes, self.findings, self.decisions, self.summary.get("coverage"),
                            self.summary.get("health"), manifest, context, self.steps, summary=self.summary,
                            safety=self.safety)


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
