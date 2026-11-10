"""The record shapes of 4.13 (outcomes, findings) and 4.14 (decisions).

Every module exchanges these. Construction validates against the taxonomy, so an
illegal outcome, reason, gate or id raises TaxonomyError at the point it is made.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .taxonomy import TaxonomyError, checkpoint_parts, is_slug, load

PERSONAS = ("active_trader", "options_trader", "futures_trader", "swing_trader", "beginner")
DATA_MODES = ("frozen", "masked_live")
CATEGORIES = ("intended", "collateral", "noise", "needs_human")
RUN_TYPES = ("pr", "merge", "rc", "nightly", "calibration")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _asdict(obj) -> dict:
    return {k: v for k, v in dataclasses.asdict(obj).items() if v is not None}


@dataclass
class RegionDiff:
    region_id: str
    pixel_fraction: float
    ssim: float
    above_threshold: bool


@dataclass
class Component:
    area: int
    bbox: tuple[int, int, int, int]          # x, y, w, h
    region_id: str | None = None


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
    recorded_at: str = field(default_factory=now)

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

    def to_dict(self) -> dict:
        return _asdict(self)


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
    recorded_at: str = field(default_factory=now)

    def __post_init__(self):
        load().check_step(self.status)

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

    def to_dict(self) -> dict:
        return _asdict(self)


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
    triage_state: str = "open"
    decision_ref: str | None = None
    ticket: str | None = None
    created_at: str = field(default_factory=now)

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
    hamming_tolerance: int = 8
    ticket: str | None = None
    manifest_entry_ref: str | None = None
    status: str = "active"
    source: str = "report_ui"
    reason: str | None = None
    decided_at: str = field(default_factory=now)

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
        if not 0 <= self.hamming_tolerance <= 8:
            raise TaxonomyError("hamming_tolerance must be 0..8 (L-DEC-03)")
        if self.source not in ("report_ui", "pr_comment", "jira_sync", "explorer_triage"):
            raise TaxonomyError(f"source {self.source!r}")
        if self.status not in ("active", "superseded", "revoked", "expired", "reopened"):
            raise TaxonomyError(f"status {self.status!r}")
        if self.decided_by in ("", "reviewer", "system", "ai"):
            raise TaxonomyError("decisions are human-only (4.14)")

    def to_dict(self) -> dict:
        d = _asdict(self)
        d["signature"] = dataclasses.asdict(self.signature)
        return d


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj
