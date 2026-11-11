"""The record shapes of 4.13 (outcomes, findings), 4.14 (decisions), 4.12 (the fingerprint) and the 8.3 rows the
store keeps beside them (step events, extractions, egress, outbox, incidents and the rest).

Every module exchanges these. Construction validates against the taxonomy, so an illegal outcome, reason, gate or
id raises TaxonomyError at the point it is made; a malformed value (a negative count, an unknown enum value, a
mis-shaped nested block) raises TaxonomyError too, so a record that exists is one the store can write.

Choices where the spec is ambiguous or silent:
- The step event actions, quarantine reasons and decision statuses and sources are the generated `taxonomy.yaml`
  lists (`step_event_actions` of 8.3 with SD-16, `quarantine_reasons` of 10.5 and SD-96, `decision_statuses` and
  `decision_sources` of 4.14); no copy of them lives here.
- `ExplorerFinding.replayed` accepts 4.13's `reproduced`, `not_reproduced` and `lint_failed`, plus `not_replayed`
  for a finding stored before its single replay (2.9; named by the build plan).
- `paper_assertion` is the 6.4 stamp `{account_id_sha256, mode, asserted_at}` ("the asserted account_id and mode
  are written into ... every checkpoint and outcome record"). Its presence is not required here: the pre-gate
  exemption of SD-48 and L-OUT-06 are enforced where the session is known.
- `outcome_id` is `oc-<run_id>-<seq>` with the sequence zero-padded to at least six digits, as the 4.13 worked
  example writes it (`outcome_id()` builds it).
- `Finding.evidence` may carry keys beyond the three of 4.13 (callers add context such as `layout`); the three
  4.13 keys, when present, must have their 4.13 shapes.
- `Decision.hamming_tolerance` None means unset (4.14's default 8 applies); a value is one a human asked for, which
  L-DEC-03 bounds (at most 8 here; 6 on tier 1 is checked where the tier is known).
- `Decision.role` is the 4.14 role the decider acted in (triage rotation, flow owner, system owner, release manager,
  PR author) or the ticket resolver of a `jira_sync` row (9.12, L-DEC-01); 4.14 is authoritative for the list.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .canonical import fingerprint_id as canonical_fingerprint_id
from .taxonomy import Taxonomy, TaxonomyError, checkpoint_parts, load

PERSONAS = ("active_trader", "options_trader", "futures_trader", "swing_trader", "beginner")
DATA_MODES = ("frozen", "masked_live")
CATEGORIES = ("intended", "collateral", "noise", "needs_human")
RUN_TYPES = ("pr", "merge", "rc", "nightly", "calibration")
# 4.13 finding.category and the 4.14 signature category
FINDING_CATEGORIES = ("intended", "collateral", "noise", "needs_human", "invariant_violation", "assertion_failed")
SIDES = ("candidate", "base")
# 4.13 step_outcome: the locator ladder rungs; `none` only as locator_rung (no rung resolved)
RUNGS = ("jab", "image", "ocr", "agent")
LOCATOR_RUNGS = RUNGS + ("none",)
# 8.3 step_events.mode and .source; the gateway_event decision and class of 4.13
STEP_EVENT_MODES = ("scripted", "agent", "explorer", "system")
STEP_EVENT_SOURCES = ("orchestrator", "gateway_hook", "watchdog")
GATE_DECISIONS = ("emitted", "dropped", "refused", "late_up", "none")
GATE_CLASSES = ("allowed", "safety", "integrity", "none")
REDACTED = re.compile(r"^<redacted:[^<>\s]+>$")          # 6.2, 8.3: '<redacted:login.password>'
DECIDER_ROLES = ("triage_rotation", "flow_owner", "system_owner", "release_manager", "pr_author",
                 "ticket_resolver")                                             # 4.14 role, 9.12, L-DEC-01
# 4.13 finding.explorer (2.9)
EXPLORER_SEVERITIES = ("low", "medium", "high")
EXPLORER_REPLAYED = ("not_replayed", "reproduced", "not_reproduced", "lint_failed")
EXPLORER_SIGNATURE_KEYS = frozenset({"surface_id", "observation", "crop_sha256"})
EVIDENCE_STEP_KEYS = frozenset({"first_seq", "last_seq"})
PAPER_ASSERTION_KEYS = frozenset({"account_id_sha256", "mode", "asserted_at"})
LOWER_RESULT_KEYS = frozenset({"outcome", "reason", "detail"})
RUNG_RESULT_KEYS = frozenset({"rung", "result", "score"})
# 4.7 assertion source enum: where an Oracle extraction comes from (2.8.1, 5.10)
EXTRACTION_SOURCES = ("ocr", "export", "jab", "api")
# 8.3 egress_log and outbox
EGRESS_PURPOSES = ("review", "batch_review", "agent_step", "explorer_step", "pr_comment", "jira", "slack", "digest",
                   "preflight", "model_check")
EGRESS_STATUSES = ("sent", "refused", "failed")
OUTBOX_KINDS = ("pr_comment", "commit_status", "jira_create", "jira_comment", "jira_transition", "slack", "page",
                "digest", "manifest_pr", "mask_pr", "quarantine_pr")   # quarantine_pr: the quarantine.yaml PR (10.5)
OUTBOX_STATES = ("queued", "sent", "failed", "abandoned")
SEVERITIES = ("S1", "S2", "S3")                                 # 6.12
MODEL_COMPONENTS = ("reviewer", "agent_rescue", "explorer")     # 11.4: every model call of a run, per component
PROPOSAL_STATES = ("proposed", "confirmed", "declined")         # 9.6 two-person rule
ESCAPE_CLASSES = ("covered_missed", "not_run", "not_registered", "not_detectable")   # 9.11, 2.13
QUARANTINE_TARGETS = ("flow", "rig", "login", "activity")       # 6.12: what a safety action quarantines
_OUTCOME_ID = re.compile(r"^oc-(?P<run>.+)-(?P<seq>[0-9]{6,})$")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def outcome_id(run_id: str, seq: int) -> str:
    """4.13 `outcome_id`: `oc-<run_id>-<seq>`, the sequence zero-padded to six digits."""
    if seq < 1:
        raise TaxonomyError("outcome sequence starts at 1")
    return f"oc-{run_id}-{seq:06d}"


def _asdict(obj) -> dict:
    return {k: v for k, v in dataclasses.asdict(obj).items() if v is not None}


def _need(ok: bool, message: str) -> None:
    if not ok:
        raise TaxonomyError(message)


def _member(value: Any, allowed: tuple[str, ...] | frozenset[str], name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    _need(value in allowed, f"{name} {value!r}; one of {sorted(allowed)}")


def _count(value: Any, name: str) -> None:
    """None or a non-negative int (a bool is refused)."""
    _need(value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
          f"{name} must be a non-negative int, got {value!r}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    _need(isinstance(value, str) and value.strip() != "", f"{name} must be non-empty text, got {value!r}")


def _ts(value: Any, name: str, *, optional: bool = False) -> None:
    """ISO 8601 text (8.3 stores UTC text; the store normalises it)."""
    if optional and value is None:
        return
    _need(isinstance(value, str), f"{name} must be ISO 8601 text, got {value!r}")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise TaxonomyError(f"{name} {value!r} is not an ISO 8601 timestamp") from None


def _human(value: Any, name: str, *, optional: bool = False) -> None:
    """L-DEC-01 and the 8.3 CHECKs: a person, never a service identity."""
    if optional and value is None:
        return
    _text(value, name)
    _need(value.strip().lower() not in NON_HUMAN, f"{name} {value!r} is not a person (L-DEC-01)")


def _fraction(value: Any, name: str) -> None:
    _need(value is None or (_number(value) and 0.0 <= value <= 1.0), f"{name} must be in [0, 1], got {value!r}")


def _paired(a: Any, b: Any, names: str) -> None:
    _need((a is None) == (b is None), f"{names} are set together")


# L-DEC-01 and the 8.3 CHECK: identities that are never a human decider
NON_HUMAN = frozenset({"", "reviewer", "system", "ai", "orchestrator", "reporter"})


@dataclass
class RegionDiff:
    region_id: str
    pixel_fraction: float
    ssim: float
    above_threshold: bool


@dataclass
class Component:
    area: int
    bbox: tuple[int, int, int, int]          # x, y, w, h in window coordinates
    region_id: str | None = None
    bbox_region: tuple[int, int, int, int] | None = None   # relative to the owning region's anchored bounds (9.7)


@dataclass
class DiffSummary:
    whole_pixel_fraction: float = 0.0
    whole_ssim: float = 1.0
    shift: tuple[int, int] = (0, 0)
    regions: list[RegionDiff] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)


@dataclass
class Classification:
    category: str
    by: str                                   # rule | reviewer
    confidence: float | None = None
    model_id: str | None = None
    prompt_hash: str | None = None
    description: str | None = None
    region_pointer: str | None = None

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise TaxonomyError(f"classification category {self.category!r}")
        if self.by not in ("rule", "reviewer"):
            raise TaxonomyError(f"classification by {self.by!r}")


def _check_paper_assertion(stamp: Any) -> None:
    """The 6.4 stamp: exactly account_id_sha256, mode and asserted_at, each non-empty text."""
    if stamp is None:
        return
    _need(isinstance(stamp, Mapping) and set(stamp) == PAPER_ASSERTION_KEYS,
          f"paper_assertion must be {{{', '.join(sorted(PAPER_ASSERTION_KEYS))}}} (6.4), got {stamp!r}")
    _text(stamp["account_id_sha256"], "paper_assertion.account_id_sha256")
    _text(stamp["mode"], "paper_assertion.mode")
    _ts(stamp["asserted_at"], "paper_assertion.asserted_at")


def _check_lower_results(t: Taxonomy, results: Any) -> None:
    """9.1: the lower-precedence results kept as evidence, each {outcome, reason?, detail?} of the taxonomy."""
    if results is None:
        return
    _need(isinstance(results, list), "lower_results must be a list")
    for r in results:
        _need(isinstance(r, Mapping) and "outcome" in r and set(r) <= LOWER_RESULT_KEYS,
              f"lower result {r!r}: outcome, and optionally reason and detail (9.1)")
        _member(r["outcome"], tuple(t.checkpoint_outcomes), "lower result outcome")
        if r.get("reason") is not None:
            _need(t.reason_allowed(r["outcome"], r["reason"]),
                  f"lower result reason {r['reason']!r} is not allowed with {r['outcome']!r}")


@dataclass
class CheckpointOutcome:
    run_id: str
    checkpoint_id: str
    outcome: str
    workspace_id: str
    persona: str
    data_mode: str
    reason: str | None = None
    detail: str | None = None
    gate: str | None = None
    side: str | None = None
    surface_id: str | None = None
    test_kind: str | None = None
    tier: int | None = None
    flow_id: str | None = None
    step_id: str | None = None
    size_step: dict | None = None
    review_state: str = "none"
    rescued: bool = False
    diff: DiffSummary | None = None
    threshold_source: str | None = None
    classification: Classification | None = None
    manifest_entry_ref: str | None = None
    primitive: str | None = None
    invariant_ids: list[str] | None = None
    assertion_ids: list[str] | None = None
    artifacts: dict | None = None
    recaptures: int = 0
    stability: str | None = None
    stability_cause: str | None = None
    decision_ref: str | None = None
    synthetic: bool = False
    flow_class: str | None = None
    paper_assertion: dict | None = None      # {account_id_sha256, mode, asserted_at} (6.4)
    base_source_run_id: str | None = None    # frozen base reused from this run (3.1)
    data_epoch_id: str | None = None
    fingerprint_id: str | None = None
    captured_at: str | None = None
    recorded_at: str = field(default_factory=now)
    outcome_id: str | None = None            # oc-<run_id>-<seq> (4.13)
    pair_id: str | None = None               # the build pair (4.13 ref:build_pair.pair_id)
    profile_id: str | None = None            # the login's entitlement profile the checkpoint ran under (2.12)
    diff_pixels: int | None = None           # 8.3 outcomes.diff_pixels
    masked_pct: float | None = None          # 8.3 outcomes.masked_pct: masked plus redaction share, percent (3.8)
    settle_ms: int | None = None
    cand_rig_id: str | None = None
    base_rig_id: str | None = None
    app_uptime_s: int | None = None
    app_restart_count: int | None = None
    lower_results: list[dict] | None = None  # the lower-precedence results kept as evidence (9.1)
    mutant_id: str | None = None             # the canary item a synthetic-cause row belongs to (10.3, SD-91)

    def __post_init__(self):
        t = load()
        parts = checkpoint_parts(self.checkpoint_id)
        self.surface_id = self.surface_id or parts["surface"]
        self.test_kind = self.test_kind or parts["kind"]
        if self.test_kind != parts["kind"]:
            raise TaxonomyError("test_kind must equal the checkpoint id's kind segment")
        t.check_checkpoint(self.outcome, self.reason, self.gate)
        if self.persona not in PERSONAS:
            raise TaxonomyError(f"persona {self.persona!r}")
        if self.data_mode not in DATA_MODES:
            raise TaxonomyError(f"data_mode {self.data_mode!r}")
        t.check_member("review_states", self.review_state)
        if self.review_state == "pending" and self.outcome != "needs_human":
            raise TaxonomyError("review_state pending only with needs_human")
        if self.side not in (None, "candidate", "base", "both"):
            raise TaxonomyError(f"side {self.side!r}")
        if self.stability not in (None, "stable", "unstable", "unknown"):
            raise TaxonomyError(f"stability {self.stability!r}")
        if self.invariant_ids:
            for i in self.invariant_ids:
                if i not in t.data["reasons"]["invariant_violation"]:
                    raise TaxonomyError(f"unknown invariant {i!r}")
        if (self.classification is not None and self.classification.by == "reviewer"
                and self.outcome in ("invariant_violation", "assertion_failed")):
            raise TaxonomyError(f"classification.by reviewer never appears with {self.outcome} (L-OUT-03)")
        if self.rescued and self.outcome in ("identical", "noise", "intended"):
            # 1.10 identical, F32: an agent-rescued capture is needs_human (rescued_capture) unless a higher outcome
            raise TaxonomyError(f"a rescued capture is never {self.outcome} (1.10, F32)")
        if self.outcome_id is not None:
            m = _OUTCOME_ID.match(self.outcome_id)
            _need(m is not None and m.group("run") == self.run_id,
                  f"outcome_id {self.outcome_id!r} is not oc-{self.run_id}-<seq> (4.13)")
        for name in ("diff_pixels", "settle_ms", "app_uptime_s", "app_restart_count"):
            _count(getattr(self, name), name)
        _need(self.masked_pct is None or (_number(self.masked_pct) and 0.0 <= self.masked_pct <= 100.0),
              f"masked_pct must be a percent in [0, 100], got {self.masked_pct!r}")
        _check_paper_assertion(self.paper_assertion)
        _check_lower_results(t, self.lower_results)
        _text(self.mutant_id, "mutant_id", optional=True)

    def to_dict(self) -> dict:
        return _asdict(self)


def _check_rung_results(results: Any) -> None:
    """4.13 step_outcome.rung_results: every rung tried, {rung, result, score?}."""
    if results is None:
        return
    _need(isinstance(results, list), "rung_results must be a list")
    for r in results:
        _need(isinstance(r, Mapping) and {"rung", "result"} <= set(r) <= RUNG_RESULT_KEYS,
              f"rung result {r!r}: rung, result and optionally score (4.13)")
        _member(r["rung"], RUNGS, "rung")
        _text(r["result"], "rung result")
        _need(r.get("score") is None or _number(r["score"]), f"rung score {r.get('score')!r} is not a number")


@dataclass
class StepOutcome:
    run_id: str
    flow_id: str
    step_id: str
    status: str
    attempt: int = 1
    locator_rung: str | None = None
    duration_ms: int = 0
    finding_ref: str | None = None
    side: str | None = None
    shard_id: str | None = None
    pre_screenshot: str | None = None
    gateway_event: dict | None = None        # {decision, reason, class} (4.13)
    recorded_at: str = field(default_factory=now)
    reason: str | None = None                # export_timeout, export_unavailable (SD-32)
    rung_results: list[dict] | None = None   # every rung tried (4.13)
    mode: str | None = None                  # 8.3 step_events.mode
    app_uptime_s: int | None = None
    app_restart_count: int | None = None
    post_screenshot: str | None = None       # sha256 of the post screenshot (8.3 post_hash)
    click_crop: str | None = None            # sha256 of the 96x96 click crop (8.3 click_crop_sha256)
    action: str | None = None                # the 8.3 step_events action (capture, gateway_decision, click, ...)

    def __post_init__(self):
        load().check_step(self.status)
        if self.action is not None and self.action not in load().step_event_actions:
            raise TaxonomyError(f"step action {self.action!r} is not an 8.3 step_events action")
        _member(self.locator_rung, LOCATOR_RUNGS, "locator_rung", optional=True)
        _check_rung_results(self.rung_results)
        _member(self.mode, STEP_EVENT_MODES, "mode", optional=True)
        _text(self.reason, "reason", optional=True)
        for name in ("app_uptime_s", "app_restart_count"):
            _count(getattr(self, name), name)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class StepEvent:
    """One 8.3 `step_events` row: an input event, popup action, capture, gateway decision, Watchdog event or
    relaunch, in any mode (8.3, 8.6, 5.4.8, SD-16). `usage` is stored as `usage_json`; `event_id` is the store's.

    A `secret` action's target_text is always `<redacted:...>` (6.2, 6.9): a plaintext secret never becomes a
    record. `mono_ms` is the event's own monotonic time, never the writer's."""
    run_id: str
    shard_id: str
    side: str
    seq: int
    source: str
    wall_at: str
    mono_ms: int
    mode: str
    action: str
    flow_id: str | None = None
    step_id: str | None = None
    checkpoint_id: str | None = None
    locator_level: str | None = None
    attempt: int = 1
    step_outcome: str | None = None
    target_text: str | None = None
    screen_x: int | None = None
    screen_y: int | None = None
    win_x: int | None = None
    win_y: int | None = None
    keys: str | None = None
    gate_decision: str = "none"
    gate_reason: str | None = None
    gate_class: str | None = None
    drill: bool = False
    pre_hash: str | None = None
    post_hash: str | None = None
    click_crop_sha256: str | None = None
    popup_title: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None
    reason: str | None = None
    app_uptime_s: int | None = None
    app_restart_count: int | None = None
    duration_ms: int | None = None
    expected_outcome: str | None = None      # SD-16: the Explorer's stated expectation
    rect_w: int | None = None                # SD-16: target rect size
    rect_h: int | None = None
    usage: dict | None = None                # SD-16: the model response's usage block (usage_json)
    injected: bool = False                   # SD-16: the hook saw the injected flag (5.4.8)
    token_id: int | None = None              # SD-16: the gateway token the event consumed

    def __post_init__(self):
        t = load()
        _member(self.side, SIDES, "side")
        _need(isinstance(self.seq, int) and not isinstance(self.seq, bool) and self.seq >= 1, "seq starts at 1")
        _member(self.source, STEP_EVENT_SOURCES, "source")
        _ts(self.wall_at, "wall_at")
        _count(self.mono_ms, "mono_ms")
        _member(self.mode, STEP_EVENT_MODES, "mode")
        _member(self.action, tuple(t.step_event_actions), "action")
        _member(self.locator_level, RUNGS, "locator_level", optional=True)
        _need(isinstance(self.attempt, int) and self.attempt >= 1, f"attempt {self.attempt!r}")
        if self.step_outcome is not None:
            t.check_step(self.step_outcome)
        _member(self.gate_decision, GATE_DECISIONS, "gate_decision")
        _member(self.gate_class, GATE_CLASSES, "gate_class", optional=True)
        if self.gate_decision in ("dropped", "refused"):
            _text(self.gate_reason, "gate_reason of a dropped or refused event (4.13)")
        if self.action == "secret":
            _need(isinstance(self.target_text, str) and bool(REDACTED.match(self.target_text)),
                  "a secret step's target_text is '<redacted:...>', never the value (6.2, 6.9)")
        if self.checkpoint_id is not None:
            checkpoint_parts(self.checkpoint_id)
        for name in ("app_uptime_s", "app_restart_count", "duration_ms", "rect_w", "rect_h", "token_id"):
            _count(getattr(self, name), name)
        _need(self.usage is None or isinstance(self.usage, Mapping), "usage is the response's usage block")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class RunOutcome:
    run_id: str
    run_type: str
    status: str
    last_gate: str
    reason: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    started_at: str = field(default_factory=now)
    ended_at: str | None = None
    pair_id: str | None = None               # 4.13 ref:build_pair.pair_id
    summary: dict | None = None              # run health and the other teardown summaries (8.3 runs.summary_json)
    report_sha256: str | None = None         # the rendered report artifact (9.4)
    comment_sha256: str | None = None        # the PR comment artifact (9.2)
    base_commit_distance: int | None = None  # SD-02: merge base to the base build's commit, at most 20
    authorization_sha256: str | None = None  # the signed authorization the run ran under (6.12)
    coverage_matrix_ref: str | None = None   # 4.13 artifact refs
    egress_log_ref: str | None = None
    post_run_proof_ref: str | None = None

    def __post_init__(self):
        t = load()
        if self.run_type not in RUN_TYPES:
            raise TaxonomyError(f"run_type {self.run_type!r}")
        t.check_run(self.status, self.reason)
        if self.last_gate not in t.gates:
            raise TaxonomyError(f"last_gate {self.last_gate!r}")
        for k in self.counts:
            if k not in t.checkpoint_outcomes:
                raise TaxonomyError(f"count key {k!r}")
        _count(self.base_commit_distance, "base_commit_distance")
        _need(self.base_commit_distance is None or self.base_commit_distance <= 20,
              "base_commit_distance is at most 20 commits (SD-02, L-BLD-04)")
        _need(self.summary is None or isinstance(self.summary, Mapping), "summary is a mapping")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class ExplorerFinding:
    """4.13 finding.explorer: an Explorer observation with its signature, trace and replay result (2.9)."""
    session_id: str
    signature: dict                          # {surface_id, observation, crop_sha256}
    severity: str
    confidence: float
    trace_ref: str
    replayed: str = "not_replayed"
    candidate_flow: str | None = None

    def __post_init__(self):
        _text(self.session_id, "explorer session_id")
        _need(isinstance(self.signature, Mapping) and set(self.signature) == EXPLORER_SIGNATURE_KEYS,
              f"explorer signature must be {{{', '.join(sorted(EXPLORER_SIGNATURE_KEYS))}}} (4.13)")
        for k in EXPLORER_SIGNATURE_KEYS:
            _text(self.signature[k], f"explorer signature {k}")
        _member(self.severity, EXPLORER_SEVERITIES, "explorer severity")
        _need(self.confidence is not None, "explorer confidence is required")
        _fraction(self.confidence, "explorer confidence")
        _text(self.trace_ref, "explorer trace_ref")
        _member(self.replayed, EXPLORER_REPLAYED, "explorer replayed")
        _text(self.candidate_flow, "explorer candidate_flow", optional=True)


def _check_evidence(evidence: Any) -> None:
    """The three 4.13 evidence keys, when present, have their shapes; other keys are the caller's context."""
    _need(isinstance(evidence, dict), "evidence is a mapping")
    if "artifacts" in evidence:
        arts = evidence["artifacts"]
        _need(isinstance(arts, list) and all(isinstance(a, str) and a for a in arts),
              "evidence.artifacts is a list of sha256 (4.13)")
    if "step_events" in evidence:
        se = evidence["step_events"]
        _need(isinstance(se, Mapping) and set(se) == EVIDENCE_STEP_KEYS
              and all(isinstance(se[k], int) and not isinstance(se[k], bool) for k in EVIDENCE_STEP_KEYS)
              and 1 <= se["first_seq"] <= se["last_seq"],
              f"evidence.step_events is {{first_seq, last_seq}} with 1 <= first <= last (4.13), got {se!r}")
    if "raw_values" in evidence:
        raw = evidence["raw_values"]
        _need(isinstance(raw, Mapping) and all(isinstance(k, str) and (v is None or isinstance(v, str))
                                               for k, v in raw.items()),
              "evidence.raw_values maps text to text (4.13)")


@dataclass
class Finding:
    finding_id: str
    run_id: str
    kind: str
    description: str
    first_seen_run_id: str
    checkpoint_id: str | None = None
    tier: int | None = None
    region_id: str | None = None
    primitive: str | None = None
    manifest_entry_ref: str | None = None
    phash: str | None = None
    category: str | None = None
    evidence: dict = field(default_factory=dict)
    detail: str | None = None                # e.g. `static_impact` on missed_in_pr_run, gap reasons (1.8, 2.10)
    synthetic: bool = False
    severity: str | None = None
    side: str | None = None
    step_id: str | None = None
    triage_state: str = "open"
    decision_ref: str | None = None
    ticket: str | None = None
    created_at: str = field(default_factory=now)
    pre_existing: bool = False               # present on both builds (2.7)
    cluster_id: str | None = None            # signature cluster (3.7, 9.7)
    contested: bool = False                  # conflicting decisions on the signature (9.7)
    held: bool = False                       # held from the report: a non-reproducing Explorer finding (2.9)
    closed_at: str | None = None             # starts the 180-day release of its evidence (8.6)
    explorer: ExplorerFinding | None = None  # explorer_candidate only (2.9)

    def __post_init__(self):
        t = load()
        t.check_member("finding_kinds", self.kind)
        t.check_member("triage_states", self.triage_state)
        if self.checkpoint_id is not None:
            checkpoint_parts(self.checkpoint_id)
        if self.tier not in (None, 1, 2, 3):
            raise TaxonomyError(f"tier {self.tier!r}")
        if self.phash is not None and (len(self.phash) != 16 or any(c not in "0123456789abcdef" for c in self.phash)):
            raise TaxonomyError("phash must be 16 lowercase hex characters")
        _member(self.category, FINDING_CATEGORIES, "finding category", optional=True)
        _check_evidence(self.evidence)
        _ts(self.closed_at, "closed_at", optional=True)
        if self.explorer is not None:
            _need(isinstance(self.explorer, ExplorerFinding), "explorer is an ExplorerFinding")
            _need(self.kind == "explorer_candidate", "the explorer block is on explorer_candidate findings only (4.13)")

    def to_dict(self) -> dict:
        return _asdict(self)


# 4.14: action -> stored verdict
ACTION_VERDICT = {
    "confirm_bug": "confirmed_bug",
    "mark_intended": "intended",
    "request_mask": "noise",
    "noise_signature": "noise",
    "agent_mistake": "noise",
    "expected_behavior": "noise",
    "approve_baseline": "intended",
    "fixed_in_build": "confirmed_bug",
}


@dataclass
class Signature:
    checkpoint_id: str
    region_id: str
    phash: str
    category: str


@dataclass
class Decision:
    decision_id: str
    action: str
    verdict: str
    signature: Signature
    decided_by: str
    hamming_tolerance: int | None = None     # None: unset, 4.14's default 8 applies
    ticket: str | None = None
    manifest_entry_ref: str | None = None
    status: str = "active"
    source: str = "report_ui"
    reason: str | None = None
    run_id: str | None = None
    build_pair_id: str | None = None
    finding_ref: str | None = None
    confirmed_by: str | None = None
    expires_at: str | None = None
    supersedes: str | None = None
    decided_at: str = field(default_factory=now)
    bbox_q4: tuple[int, int, int, int] | None = None   # largest component box quantized to 4 px (4.14 fallback)
    role: str | None = None                  # the role the decider acted in (9.12, L-DEC-01)
    reopen_count: int = 0                    # times a fixed signature returned (4.14 reopened)

    def __post_init__(self):
        t = load()
        t.check_member("decision_actions", self.action)
        t.check_member("decision_verdicts", self.verdict)
        if self.action == "reject_baseline":
            if self.verdict not in ("confirmed_bug", "noise"):
                raise TaxonomyError("reject_baseline stores confirmed_bug or noise")
        elif ACTION_VERDICT.get(self.action) != self.verdict:
            raise TaxonomyError(f"action {self.action} stores verdict {ACTION_VERDICT.get(self.action)}")
        if self.action in ("mark_intended", "fixed_in_build") and not self.ticket:
            raise TaxonomyError(f"{self.action} requires a ticket (L-DEC-02)")
        if self.hamming_tolerance is not None and not 0 <= self.hamming_tolerance <= 8:
            raise TaxonomyError("hamming_tolerance must be 0..8 (L-DEC-03)")
        if self.source not in t.decision_sources:
            raise TaxonomyError(f"source {self.source!r}")
        if self.status not in t.decision_statuses:
            raise TaxonomyError(f"status {self.status!r}")
        if self.decided_by in NON_HUMAN:
            raise TaxonomyError("decisions are human-only (4.14)")
        if self.bbox_q4 is not None:
            box = tuple(self.bbox_q4)
            _need(len(box) == 4 and all(isinstance(v, int) and v >= 0 and v % 4 == 0 for v in box)
                  and box[2] > 0 and box[3] > 0,
                  f"bbox_q4 {self.bbox_q4!r} is x, y, w, h on the 4 px grid (4.14)")
            self.bbox_q4 = box
        _member(self.role, DECIDER_ROLES, "role", optional=True)
        _count(self.reopen_count, "reopen_count")

    def to_dict(self) -> dict:
        d = _asdict(self)
        d["signature"] = dataclasses.asdict(self.signature)
        return d


# ---------------------------------------------------------------------------------------------------------------
# records of the 8.3 rows beside outcomes and decisions

@dataclass
class Fingerprint:
    """The 4.12 environment fingerprint of one side of one shard. `fingerprint_id` recomputes from `match`
    (L-FPR-01); left None it is computed."""
    run_id: str
    side: str
    rig_id: str
    read_at: str
    match: dict
    record: dict = field(default_factory=dict)
    shard_id: str | None = None
    fingerprint_id: str | None = None
    schema_version: int = 2

    def __post_init__(self):
        _member(self.side, SIDES, "fingerprint side")
        _text(self.rig_id, "rig_id")
        _ts(self.read_at, "read_at")
        _need(isinstance(self.match, dict) and bool(self.match), "fingerprint match is a non-empty mapping")
        _need(isinstance(self.record, dict), "fingerprint record is a mapping")
        _text(self.match.get("build_id"), "match.build_id")
        none = sorted(k for k, v in self.match.items() if v is None)
        _need(not none, f"every match field is non-null (L-FPR-01): {none}")
        want = canonical_fingerprint_id(self.match)
        if self.fingerprint_id is None:
            self.fingerprint_id = want
        _need(self.fingerprint_id == want, f"fingerprint_id does not recompute; expected {want} (L-FPR-01)")

    @property
    def build_id(self) -> str:
        return self.match["build_id"]

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class CaptureMeta:
    """The metadata stored with every capture (3.3, 5.7): window geometry and identity, the fingerprint, the app's
    age, how the executor got there, the settle time and the redaction applied. `rect` is the DWM extended frame
    bounds x, y, w, h."""
    sha256: str
    run_id: str
    checkpoint_id: str
    side: str
    window_class: str
    title: str
    rect: tuple[int, int, int, int]
    captured_at: str
    z_order: int | None = None
    monitor: int | None = None
    dpi: int | None = None
    scale_percent: int | None = None
    dpi_awareness: str | None = None
    fingerprint_id: str | None = None
    app_uptime_s: int | None = None
    app_restart_count: int | None = None
    mode: str | None = None                  # executor mode (8.3 step_events.mode)
    action_path: str | None = None           # the step and locator path that reached the screen
    settle_ms: int | None = None
    redaction_version: str | None = None
    redaction_boxes: int = 0

    def __post_init__(self):
        _text(self.sha256, "capture sha256")
        checkpoint_parts(self.checkpoint_id)
        _member(self.side, SIDES, "capture side")
        _need(self.window_class.startswith("SunAwt"), f"capture window class {self.window_class!r} (3.4: SunAwt*)")
        _need(len(self.rect) == 4 and all(isinstance(v, int) for v in self.rect) and self.rect[2] > 0
              and self.rect[3] > 0, f"capture rect {self.rect!r} is x, y, w, h with a positive size")
        _ts(self.captured_at, "captured_at")
        _member(self.mode, STEP_EVENT_MODES, "capture mode", optional=True)
        for name in ("z_order", "monitor", "dpi", "scale_percent", "app_uptime_s", "app_restart_count", "settle_ms",
                     "redaction_boxes"):
            _count(getattr(self, name), name)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class Extraction:
    """One 8.3 `oracle_extractions` row (SD-09): what one source read for one field of one assertion."""
    run_id: str
    checkpoint_id: str
    side: str
    assertion_id: str
    field: str
    source: str
    read_at: str
    region_id: str | None = None
    text: str | None = None                  # raw text read (rows separated by newlines)
    value: str | None = None                 # the parsed value, as text
    confidence: float | None = None          # 0 to 100 (5.10; JAB and export reads report 100)
    second_pass: bool = False                # the 2x OCR re-read (2.8.1)
    crop_sha256: str | None = None

    def __post_init__(self):
        checkpoint_parts(self.checkpoint_id)
        _member(self.side, SIDES, "extraction side")
        _text(self.assertion_id, "assertion_id")
        _text(self.field, "field")
        _member(self.source, EXTRACTION_SOURCES, "extraction source")
        _ts(self.read_at, "read_at")
        _need(self.confidence is None or (_number(self.confidence) and 0 <= self.confidence <= 100),
              f"confidence {self.confidence!r} is 0 to 100 (5.10)")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class DeclaredSend:
    """A send the run declared (6.6, 6.7): the consumed paper_send token and the Working row it produced, which
    the post-run proof lists under PROVEN: only declared sends."""
    run_id: str
    shard_id: str
    flow_id: str
    step_id: str
    row_key: str                             # the Working row's stable key (6.6 OrderRow.key)
    sent_at: str
    token_id: int | None = None
    symbol: str | None = None
    qty: float | None = None

    def __post_init__(self):
        for name in ("shard_id", "flow_id", "step_id", "row_key"):
            _text(getattr(self, name), name)
        _ts(self.sent_at, "sent_at")
        _count(self.token_id, "token_id")
        _need(self.qty is None or (_number(self.qty) and self.qty > 0), f"qty {self.qty!r}")


@dataclass
class Incident:
    """One 8.3 `incidents` row (6.12): severity, trigger, the rig and login, closure by a human, the postmortem
    due five business days after an S1 or S2 opens."""
    severity: str
    trigger: str
    opened_at: str
    run_id: str | None = None
    rig_id: str | None = None
    login_alias: str | None = None
    incident_id: int | None = None
    closed_at: str | None = None
    closed_by: str | None = None
    close_reason: str | None = None
    ticket_ref: str | None = None
    postmortem_ref: str | None = None
    postmortem_due_at: str | None = None

    def __post_init__(self):
        _member(self.severity, SEVERITIES, "severity")
        _text(self.trigger, "trigger")
        _ts(self.opened_at, "opened_at")
        _paired(self.closed_at, self.closed_by, "closed_at and closed_by")
        _human(self.closed_by, "closed_by", optional=True)
        _ts(self.closed_at, "closed_at", optional=True)
        _ts(self.postmortem_due_at, "postmortem_due_at", optional=True)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class Proposal:
    """A two-person-rule proposal (9.6, L-DEC-06): a PR committer's action on a tier-1 or tier-2 finding, written
    as a decision only when a different human confirms it."""
    finding_id: str
    run_id: str
    action: str
    proposed_by: str
    reason: str
    proposed_at: str = field(default_factory=now)
    ticket: str | None = None
    state: str = "proposed"
    decided_by: str | None = None
    decided_at: str | None = None
    decline_reason: str | None = None
    decision_ref: str | None = None
    proposal_id: int | None = None

    def __post_init__(self):
        load().check_member("decision_actions", self.action)
        _human(self.proposed_by, "proposed_by")
        _text(self.reason, "proposal reason")
        _ts(self.proposed_at, "proposed_at")
        _member(self.state, PROPOSAL_STATES, "proposal state")
        _need((self.state == "proposed") == (self.decided_by is None), "decided_by is set exactly when decided")
        _paired(self.decided_by, self.decided_at, "decided_by and decided_at")
        _human(self.decided_by, "decided_by", optional=True)
        _need(self.decided_by is None or self.decided_by != self.proposed_by,
              "the confirmer is a different human (9.6 two-person rule)")
        _need(self.state != "declined" or bool(self.decline_reason), "a declined proposal says why (9.6)")
        _need(self.state != "confirmed" or self.decision_ref is not None, "a confirmed proposal names its decision")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class OutboxJob:
    """One 8.3 `outbox` job (9.1): every outbound post is a job before it is a send."""
    kind: str
    destination: str
    payload_sha256: str
    created_at: str = field(default_factory=now)
    run_id: str | None = None
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    state: str = "queued"
    sent_at: str | None = None
    closed_by: str | None = None             # a human closing an abandoned job (9.1)
    closed_at: str | None = None
    job_id: int | None = None

    def __post_init__(self):
        _member(self.kind, OUTBOX_KINDS, "outbox kind")
        _text(self.destination, "destination")
        _text(self.payload_sha256, "payload_sha256")
        _ts(self.created_at, "created_at")
        _count(self.attempts, "attempts")
        _member(self.state, OUTBOX_STATES, "outbox state")
        _need((self.state == "sent") == (self.sent_at is not None), "sent_at is set exactly when sent (8.3)")
        _paired(self.closed_by, self.closed_at, "closed_by and closed_at")
        _need(self.closed_by is None or self.state == "abandoned", "only an abandoned job is closed by a human (9.1)")
        _human(self.closed_by, "closed_by", optional=True)
        for name in ("next_attempt_at", "sent_at", "closed_at"):
            _ts(getattr(self, name), name, optional=True)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class EgressRow:
    """One 8.3 `egress_log` row (6.10): every outbound request, sent, refused or failed."""
    destination: str
    purpose: str
    status: str
    bytes: int
    sent_at: str = field(default_factory=now)
    run_id: str | None = None
    shard_id: str | None = None
    checkpoint_id: str | None = None
    response_status: int | None = None
    artifact_sha256: str | None = None
    redaction_version: str | None = None
    model_id: str | None = None
    request_id: str | None = None
    refusal_reason: str | None = None

    def __post_init__(self):
        _text(self.destination, "destination")
        _member(self.purpose, EGRESS_PURPOSES, "egress purpose")
        _member(self.status, EGRESS_STATUSES, "egress status")
        _count(self.bytes, "bytes")
        _need(self.bytes is not None, "bytes is required (8.3)")
        _ts(self.sent_at, "sent_at")
        _need(self.status != "refused" or bool(self.refusal_reason), "a refused request names its refusal_reason "
              "(destination not allowlisted, redaction version missing; 6.10, 8.9)")
        _count(self.response_status, "response_status")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class ModelUsage:
    """The usage block of one model response, per component (11.4): Reviewer, agent rescue or Explorer."""
    component: str
    model_id: str
    at: str
    run_id: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self):
        _member(self.component, MODEL_COMPONENTS, "model usage component")
        _text(self.model_id, "model_id")
        _ts(self.at, "at")
        for name in ("input_tokens", "output_tokens", "cache_read_tokens"):
            _count(getattr(self, name), name)
        _need(_number(self.cost_usd) and self.cost_usd >= 0, f"cost_usd {self.cost_usd!r}")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass(frozen=True)
class PreflightAttempt:
    """One attempt of one 12.7 preflight item; both attempts of an item are logged (7.x run health)."""
    run_id: str
    item: str                                # 12.7 item id, e.g. H7
    attempt: int
    passed: bool
    at: str
    shard_id: str | None = None
    detail: str | None = None

    def __post_init__(self):
        _need(f"preflight.{self.item}" in load().gates, f"preflight item {self.item!r} is not a 12.7 item")
        _member(self.attempt, (1, 2), "preflight attempt")
        _ts(self.at, "at")


@dataclass
class MaintenanceItem:
    """One flow-maintenance item of 11.5 (`maintenance_items`): what, on which flow, step or anchor, whose, and
    the hours logged against it."""
    kind: str
    ref: str
    owner: str
    opened_at: str
    hours: float = 0.0
    run_id: str | None = None
    ticket_ref: str | None = None
    detail: str | None = None
    closed_at: str | None = None
    closed_by: str | None = None
    item_id: int | None = None

    def __post_init__(self):
        _need(isinstance(self.kind, str) and bool(_SNAKE.match(self.kind)), f"maintenance kind {self.kind!r}")
        _text(self.ref, "ref")
        _text(self.owner, "owner")
        _ts(self.opened_at, "opened_at")
        _need(_number(self.hours) and self.hours >= 0, f"hours {self.hours!r}")
        _paired(self.closed_at, self.closed_by, "closed_at and closed_by")
        _ts(self.closed_at, "closed_at", optional=True)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class Escape:
    """One escape row entered at G+1 (10.9, 9.11): a production bug, the checkpoint that should have shown it (none
    when not_registered) and its class."""
    release: str
    ticket: str
    escape_class: str
    entered_by: str
    entered_at: str = field(default_factory=now)
    checkpoint_id: str | None = None
    surface_id: str | None = None
    tier: int | None = None
    escape_id: int | None = None

    def __post_init__(self):
        _text(self.release, "release")
        _text(self.ticket, "ticket")
        _member(self.escape_class, ESCAPE_CLASSES, "escape class")
        _human(self.entered_by, "entered_by")
        _ts(self.entered_at, "entered_at")
        _need((self.escape_class == "not_registered") == (self.checkpoint_id is None),
              "an escape names its checkpoint, or is not_registered (10.9)")
        if self.checkpoint_id is not None:
            checkpoint_parts(self.checkpoint_id)
        _member(self.tier, (1, 2, 3), "tier", optional=True)

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class SafetyQuarantine:
    """A safety action that took a flow, rig, login or activity out of service (6.5, 6.12, 10.5); re-enabling is
    human-only with a reason and a ticket."""
    target_kind: str
    target: str
    reason: str
    opened_at: str
    run_id: str | None = None
    detail: str | None = None
    ticket_ref: str | None = None
    reenabled_by: str | None = None
    reenabled_at: str | None = None
    reenable_reason: str | None = None
    quarantine_id: int | None = None

    def __post_init__(self):
        _member(self.target_kind, QUARANTINE_TARGETS, "quarantine target_kind")
        _text(self.target, "target")
        _member(self.reason, tuple(load().quarantine_reasons), "quarantine reason")
        _ts(self.opened_at, "opened_at")
        _paired(self.reenabled_by, self.reenabled_at, "reenabled_by and reenabled_at")
        _human(self.reenabled_by, "reenabled_by", optional=True)
        _need(self.reenabled_by is None or (bool(self.reenable_reason) and bool(self.ticket_ref)),
              "re-enabling records a reason and a ticket (6.12)")

    def to_dict(self) -> dict:
        return _asdict(self)


@dataclass
class SyntheticOutcome:
    """The outcome one canary mutant produced on one checkpoint (10.3, SD-91): stored in `synthetic_outcomes`,
    never in `outcomes`, and never matched against a decision."""
    run_id: str
    mutant_id: str
    checkpoint_id: str
    outcome: str
    reason: str | None = None
    detail: str | None = None
    gate: str | None = None
    mutant_sha256: str | None = None
    recorded_at: str = field(default_factory=now)

    def __post_init__(self):
        _text(self.mutant_id, "mutant_id")
        checkpoint_parts(self.checkpoint_id)
        load().check_checkpoint(self.outcome, self.reason, self.gate)
        _ts(self.recorded_at, "recorded_at")

    def to_dict(self) -> dict:
        return _asdict(self)


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def from_dict(cls, data: dict):
    """Build a record from a dict as written by to_dict (nested Classification, DiffSummary, Signature,
    ExplorerFinding)."""
    data = dict(data)
    if cls is CheckpointOutcome:
        if isinstance(data.get("classification"), dict):
            data["classification"] = Classification(**data["classification"])
        if isinstance(data.get("diff"), dict):
            d = dict(data["diff"])
            d["regions"] = [RegionDiff(**r) for r in d.get("regions", [])]
            comps = []
            for c in d.get("components", []):
                c = dict(c)
                c["bbox"] = tuple(c["bbox"])
                if c.get("bbox_region") is not None:
                    c["bbox_region"] = tuple(c["bbox_region"])
                cnames = {f.name for f in dataclasses.fields(Component)}
                comps.append(Component(**{k: v for k, v in c.items() if k in cnames}))
            d["components"] = comps
            if "shift" in d:
                d["shift"] = tuple(d["shift"])
            data["diff"] = DiffSummary(**d)
    if cls is Decision and isinstance(data.get("signature"), dict):
        data["signature"] = Signature(**data["signature"])
    if cls is Finding and isinstance(data.get("explorer"), dict):
        data["explorer"] = ExplorerFinding(**data["explorer"])
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})
