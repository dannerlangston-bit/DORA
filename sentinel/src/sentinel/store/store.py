"""The SQLite store of 8.1 to 8.3: one writer, the 8.3 schema generated from taxonomy.yaml, the 4.13 later changes,
the 4.14 decision lookup and inheritance, the 8.6 retention safety rules and the 8.9 failure modes.

`Store(path)` is the single writer (8.2) of a Run Store or the Central Store; both have the same schema.
`connect_reader(path)` opens the read-only connection 8.2 gives every other process. Records go in and come out as
`sentinel.records` types; the few inputs records has no type for (run identity, scope, reviews) are the small
frozen dataclasses below.

Enforced here and, where SQL can say it, again by the schema:
- one writer process: a `writer.lock` beside the database holds the writer's PID and a second writer refuses to
  start while that PID is alive (8.2); a stale lock (dead PID) is taken over. The connection is bound to the
  thread that opened it, which is the "one writer thread" of 8.2.
- the database and the artifact root must be on a local volume: UNC paths, and on POSIX the network file systems
  of /proc/mounts, are refused; on Windows the drive must be DRIVE_FIXED (8.2, H7, 8.9).
- every outcome, reason, gate, finding kind, triage state and decision value is in the closed taxonomy: records
  validate at construction, the store re-validates (records are mutable), and the generated CHECKs refuse the rest.
- outcomes are written once (4.13, 8.2). A replay of the same record is a no-op; a different record for the same
  (run, checkpoint) is an `ImportConflict`. The only later changes are the three of 4.13: `set_decision_ref`,
  `apply_review` (pending -> done with the verdict's outcome, or as an annotation only when a human decision
  already exists on the item, F28) and `end_review` (reviewer_cap or reviewer_unavailable, no review id).
- decisions are human-only (4.14, L-DEC-01) and never edited in place: supersede and revoke only (8.3). Also
  refused at write: approve_baseline from a PR run, a run not complete, a synthetic outcome or by the change's
  author on tier 1 (L-DEC-05); a waiver without an expiry at most 30 days out (L-DEC-07); a fixed_in_build build
  not newer, by first_seen_at, than build_id or the superseded row's build_id (L-DEC-08).
- the lookup reads only actions that act through the signature (request_mask once its mask_ref is set); a
  signature with two supersessions within 30 days is frozen; a mark_intended decision applies (counts toward the
  verdict set and inheritance) only within its 9.7 scope: made on this run or its build pair, or the run's loaded
  manifests carry its entry or ticket; a waiver applies to every run. Inheritance replaces only a computed
  collateral or needs_human (1.10).
- a run is `complete` only with an outcome for every scoped checkpoint, no env_error, no not_comparable
  (fingerprint_mismatch), no store-failure harness_error, no UNPROVEN or ORDERS SENT post-run proof, no unimported
  shard, no shard that ended worse than complete and every outcome-named artifact stored (1.10, 8.1, 8.2, 8.3, 8.9);
  no run ends with a scoped checkpoint silent (L-OUT-07), or less severe than its worst shard (8.2). An outcome that
  names an artifact the store has no row for is refused at write (8.2); an artifact row needs bytes on disk that
  hash to its name, and an image its redaction version (8.3, 8.4); a `decision:<id>` reason needs an active decision
  with the outcome's verdict (4.13).
- L-OUT-06: once `set_session_context` names a shard, a record written for it that references a capture needs the
  session's paper-mode stamp from an allowlisted account (`PaperAssertionMissing`; SD-48 exempts records without a
  capture). L-OUT-02: a `strict_records` store refuses a record the 4.13 required-fields table refuses
  (`RecordIncomplete`, through `sentinel.required_fields`); `write_checkpoint_or_repair` is the Orchestrator's
  repair-once path ending in `harness_error (reason: store_write_failed)`.
- 9.7 Two humans, one signature (`ActiveDecisionExists`), the frozen signature's system owner, 9.6 idempotency and
  L-DEC-03 (an explicit tolerance above the tier's default is refused) at `record_decision`; the 4.13 late verdict
  produces only intended or needs_human (SD-23).
- the run row carries its pair (`register_pair`), its teardown summary (`end_run(summary=)`, `update_summary`) and
  its review state (unreviewed, partial, reviewed, recomputed as reviews land). A finished run is final except the
  10.2 late arrival: `replace_dead_man_run` replaces a dead-man run with its real rows and outcome.
- every write batch is one `BEGIN IMMEDIATE` transaction, appended to `results.jsonl` before it commits (8.2).

Choices where the spec is ambiguous or silent (reported to the coordinator):
- schema.sql rendered equals the 8.3 DDL blocks (tests/test_schema_mirror.py), including what once were store
  additions: `run_scope` (the scoped checkpoint ids with the persona of the visit; `runs.scope_count` cannot say
  which checkpoint is missing), `outcome_records` (the 4.13 record as written, so the store returns persona,
  classification and diff regions, which have no column), and triggers for F65 (an env_error names a gate from
  taxonomy.yaml), for "nothing else is ever rewritten" (every outcome column the other triggers leave open, and
  deletion), for decision_ref set once, review_id only with the review transition, complete-needs-every-scoped-id
  and complete-needs-no ORDERS SENT proof or worse shard (trg_run_complete_env; SD-80 put the fingerprint mismatch
  and UNPROVEN conditions into trg_run_complete itself), and the SD-78 non-empty fingerprint_mismatch detail.
- outcome_reasons seed: owner_section and detail_means come from taxonomy.yaml `reason_metadata` when it lists the
  reason; otherwise owner_section is '4.13' (the reason vocabulary table the file is generated from; '1.10' for
  rows inherited through `reason_inheritance`, as 8.3's INSERT ... SELECT writes) and detail_means the parameter
  placeholder (`<step>`). A reason is parametric when its taxonomy pattern has a placeholder; `fingerprint_mismatch`
  is plain (SD-78).
- At service start the seed is synced (8.3: "seeded ... at service start"): new reasons are added, reasons no
  longer in the file are retired (active = 0), never deleted. A taxonomy change that alters a generated CHECK
  (any list) is refused at open: that needs a migration, and none exists yet.
- `reason_detail` holds the reason's parameter for a parametric reason and the record's `detail` otherwise; with
  both, `<param>\\n<detail>`. For invariant_violation without a detail it holds the fired invariant ids (8.3).
- Decision ids are `dec-<zero-padded sequence>` (4.14) and map to the integer `decisions.decision_id`; finding ids
  are the findings' `natural_key`. The stored `hamming_tolerance` is the effective one (4.14): an unset
  records.Decision.hamming_tolerance takes the tier's default (8, or 6 on tier 1: "tier-1 checkpoint, so the
  override applies"), an explicit value above that default is refused (L-DEC-03: "at most 6 when the checkpoint's
  tier is 1", write refused; never clamped), and the region's calibrated tolerance at the run's config commit
  lowers it. noise_signature and request_mask get `expires_at` = decided_at + 30 days when none is given (8.3,
  4.14).
- A decision's `finding_ref`, `manifest_entry_ref` and `build_pair_id` are read back from its source finding and
  its run's pair (8.3 has no decision columns for them); `bbox_q4` is stored as `x,y,w,h`.
- 9.7 Two humans, one signature, and the 9.6 idempotency are the Reporter API's rules for a decision made on a
  finding (4.14 makes `finding_ref` required; only store-level fixtures and imports write one without), so they
  apply to a decision that names its finding: the same (finding, action, decided_by) on an active row returns that
  row; a plain decision is refused with `ActiveDecisionExists` while an applicable (9.7 scope) active decision
  matches the signature. approve_baseline and reject_baseline move a baselines row and are not signature
  decisions. The frozen-signature rule (the next decision needs the system owner) applies to every signature
  decision; the system owners are the store's configured `system_owners`. A supersession by another human marks the
  findings of both rows contested; fixed_in_build superseding its confirm_bug is the 4.14 lifecycle, not a contest.
  The triage-channel notice to the earlier decider is the Reporter's (an outbox job needs its payload stored).
- Step outcomes become `step_events` rows with source orchestrator, action `step`, mode `agent` for an agent rung
  or fallback, and `finding:<ref>` in `reason`. The step's `gateway_event` (4.13) is stored as `gate_decision`,
  `gate_reason` and `gate_class` (the code itself, 5.4.4), and `gate_decision` none when the step has none;
  `step_outcomes` reads it back. `gateway_drops` counts the rows the hook dropped or the slow half refused (9.11).
- Run counts exclude synthetic rows (canary mutants are not checkpoints of the run, 10.3).
- `canary_results` (8.3) holds one row per mutant, safety canary, drill item or path-equivalence pair, which
  `outcomes`, one record per (run, checkpoint), cannot. 8.3 gives the table no natural key; the store uses
  (run_id, kind, mutant_id, checkpoint_id), since an `aa_control` entry binds one mutant id to several
  checkpoints (10.3): a replayed row is a no-op and a different row for the same key an `ImportConflict`
  (8.2). `created_at` is a write-time argument and not compared. Rows are `canary.CanaryResult`, whose
  construction enforces the 8.3 CHECKs; the synthetic `harness_error` rows still go through `write_checkpoint`.
- `post_run_proofs` (8.3) take `PostRunProof` rows through `write_proof`, one per shard, written once while the run
  is open, and come back through `post_run_proofs`. `counts_json` holds the 6.6 table lines and, beside the counts
  8.3 names, the undeclared and declared row keys, since an ORDERS SENT verdict lists every undeclared row (6.6)
  and no other column can hold them.
- A finished run is not re-ended with a different outcome (the 10.7 re-evaluation of a returning rig belongs
  to the import, which is not this module), except the 10.2 late arrival of a dead-man run. A RunOutcome's summary
  and 4.13 artifact refs go into `summary_json` (8.3 has no column for them) under `summary` and `refs`, beside
  `counts` and `last_gate`; those four keys are the store's, and the teardown summary and `update_summary` add any
  other top-level key (8.3: coverage matrix, run health, detector state; 8.7, 9.10: acknowledgements). Top-level
  keys replace, so a caller updating a list (the alerts) passes the whole list. `base_commit_distance` and
  `authorization_sha256` have their `runs` columns.
- runs.review_state (8.3, 9.2): `reviewed` when no outcome of the run is pending; `unreviewed` while items are
  pending and none of the run's reviews has landed (`review_state` done on none of its outcomes); `partial` between.
- L-OUT-02 at write is `strict_records` (off by default) because records built by tools and older callers do not
  carry every required field yet; `record_errors` and `write_checkpoint_or_repair` apply it whatever the setting.
  `step_failed:<step>` is checked against the run plan's `flow_steps` and the steps of registered flow checkpoints
  (a writer restarted mid-run knows only the latter, 8.3 has no table of flow steps).
- L-OUT-06: the session context is per shard (None: writes without a shard) and holds this session's assertion
  times; a stamp that is present is checked even on an exempt record; synthetic mutant rows are generated, not
  captured, and are not checked (10.3).
- 10.2 late arrival: the placeholders move to `outcomes_superseded` (the 8.3 table for rows kept for diagnosis and
  never counted); the deletion briefly lifts the two keep triggers inside the batch and restores their exact SQL.
  The delay is `late_arrival` (also under `health`) and the closed alert an `alerts` entry of the summary.
- The foundation helpers are thin: no business rule beyond the 8.3 constraints and the record validation. Rows
  with a natural key (step events by shard and seq, extractions, fingerprints by shard and side, soak samples,
  coverage cells, synthetic outcomes, canary manifests) are written once: a replay is a no-op, a different row an
  ImportConflict (8.2). Step events from `write_events` carry their own seq; a shard writes its events either
  there or through `write_steps`, whose seq is the next free one.
- `post_run_proofs` may hold one run-level row (shard NULL) for a run that started no session (SD-61); the
  clearance (`verified_by`, `verified_at`) is set once, on an unproven verdict only (trigger).
- `calibration_floors` are keyed by the rendering key (SD-69): `write_calibration` supersedes the current floor of
  the same checkpoint, region and key in the batch that writes the new one.
- The outbox job's human close is `close_job` (`close` is the store's own lifecycle). A confirm_bug written without
  a ticket gets it once through `fill_ticket_ref`; a request_mask decision its mask once through `set_mask_ref`
  (both also guarded by triggers).
- `reassign_login` (11.2) deactivates the failed rig and gives the spare, re-snapshotted under a new snapshot id,
  that rig's login and role; one active rig per login is a partial unique index, and the rig role and login kind
  must agree (explorer with explorer, pool and nightly with scripted; 11.3).
- Timestamps are normalised to UTC ISO 8601 seconds on write (8.3).
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence

from .. import required_fields
from ..classify import DecisionMatch
from ..canonical import canonical_json
from ..records import (ACTION_VERDICT, DATA_MODES, PERSONAS, RUN_TYPES, CheckpointOutcome, Classification, Component,
                       Decision, DiffSummary, EgressRow, Escape, ExplorerFinding, Extraction, Finding, Fingerprint,
                       MaintenanceItem, ModelUsage, OutboxJob, Proposal, RegionDiff, RunOutcome, Signature, StepEvent,
                       StepOutcome, SyntheticOutcome, now, to_jsonable)
from ..taxonomy import Taxonomy, TaxonomyError, checkpoint_parts, load
from .artifacts import ArtifactStore, StoredArtifact, is_visual

if TYPE_CHECKING:
    from ..canary import CanaryResult

SCHEMA_VERSION = 2              # 2: the foundation tables and columns of the 8.3 DDL
DEFAULT_TOLERANCE = 8          # 4.14, 9.7: bits of 64
TIER1_TOLERANCE = 6
RELATED_LIMIT = 12             # 4.14: distances above the tolerance and up to 12 are shown, never inherited
EXPIRY_DAYS = 30               # 8.3: noise_signature and request_mask default expiry
# 4.14: the one action whose stored verdict depends on the case; every other action maps through ACTION_VERDICT.
MULTI_VERDICT = {"reject_baseline": ("confirmed_bug", "noise")}
# 4.14 table: the finding triage state an action leaves.
TRIAGE_FOR_ACTION = {
    "confirm_bug": "confirmed_bug", "mark_intended": "intended", "request_mask": "open",
    "noise_signature": "noise", "agent_mistake": "agent_mistake", "expected_behavior": "expected_behavior",
    "approve_baseline": "closed", "fixed_in_build": "fixed_pending_verify",
}
# L-DEC-01 and the 8.3 CHECKs: identities that are never a human.
SERVICE_IDENTITIES = frozenset({"", "ai", "reviewer", "system", "orchestrator", "reporter"})
STORE_FAILURE_REASONS = ("store_write_failed", "artifact_persist_failed", "redaction_version_missing")
PENDING_REASONS = ("reviewer_unavailable", "global_shift")      # 4.13: pending only while queued with these
# 8.2: the run outcome is the worst of the shards' outcomes, least severe first.
RUN_SEVERITY = ("complete", "complete_with_env_errors", "aborted_budget", "aborted_harness", "aborted_safety")
# 4.13 required fields: the verdict a `decision:<id>` reason names, by outcome.
DECISION_REASON_VERDICT = {"noise": "noise", "intended": "intended", "collateral": "confirmed_bug"}
LOCATOR_LEVELS = ("jab", "image", "ocr", "agent")
GATEWAY_EVENT_KEYS = frozenset({"decision", "reason", "class"})   # 4.13 step_outcome.gateway_event
# 8.3 post_run_proofs CHECKs (6.6): the five verdicts and what Cancel All did
PROOF_VERDICTS = ("proven_no_sends", "proven_declared_sends", "proven_no_session", "unproven", "orders_sent")
PROOF_CANCEL_ALL = ("not_needed", "performed_verified", "performed_unverified")
NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "afs", "ncpfs", "fuse.sshfs"})
_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
_DECISION_RE = re.compile(r"^dec-(\d+)$")
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)(?::([^}]*))?\}\}")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
# 8.3 system_state keys a store client may set; schema_version and schema_sha256 are the store's own
SYSTEM_STATE_KEYS = frozenset({
    "last_backup_at", "last_backup_error", "last_integrity_check_at", "last_restore_drill_at",
    "last_artifact_audit_at", "kill_switch_state", "detector_state", "canaries_pending", "nightly_skipped_for",
    "reviewer_disabled", "harness_freeze", "free_bytes_at_last_preflight"})
OUTBOX_OPEN = ("queued", "failed")             # 9.1: states a job is retried from
PAIR_KINDS = ("pr", "merge", "rc", "aa")       # 8.3 build_pairs.pair_kind
SUMMARY_RESERVED = frozenset({"counts", "last_gate", "summary", "refs"})   # summary_json keys end_run owns
RUN_REFS = ("report_sha256", "comment_sha256", "coverage_matrix_ref", "egress_log_ref", "post_run_proof_ref")   # 4.13


# ---------------------------------------------------------------------------------------------------------------
# errors

class StoreError(Exception):
    """The store cannot open or cannot do what was asked."""


class WriterLockError(StoreError):
    """Another live process holds the writer lock (8.2)."""


class StoreRefused(StoreError, ValueError):
    """A write broke a rule of 4.13, 4.14 or 8.3; nothing was written (8.9: refused with the reason)."""


class ImportConflict(StoreRefused):
    """A row with the same natural key exists and differs (8.2, 8.9: import_conflict); never overwritten."""


class RunNotComplete(StoreRefused):
    """`complete` was asked for a run that cannot be complete (8.1, 8.3 run trigger)."""


class StoreWriteFailed(StoreError):
    """The writer could not commit (busy after retries, I/O error, disk full): 8.9 store_write_failed."""


class RecordIncomplete(StoreRefused):
    """L-OUT-02: the record lacks fields the 4.13 required-fields table names; `errors` lists them."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class PaperAssertionMissing(StoreRefused):
    """L-OUT-06: a record without the current session's paper-mode stamp, or with an account that is not on the
    allowlist. The run is `aborted_safety (reason: paper_mode_mismatch)`; the caller ends it through the kill
    switch (6.4)."""


class ActiveDecisionExists(StoreRefused):
    """9.7 Two humans, one signature: a plain decision on a signature that already has an active decision. `active`
    holds the active rows, which the API shows; the ways forward are supersede and revoke."""

    def __init__(self, message: str, active: Sequence[Decision]):
        self.active = tuple(active)
        super().__init__(message)


# ---------------------------------------------------------------------------------------------------------------
# inputs records has no type for

@dataclass(frozen=True)
class ScopeEntry:
    """One checkpoint a run is asked to visit: the 8.3 `checkpoints` row plus the persona of the visit."""
    checkpoint_id: str
    workspace_id: str
    persona: str
    tier: int
    data_mode: str
    flow_class: str | None = None          # flow checkpoints only (8.3 checkpoints.flow_class)
    recapturable: bool = True


@dataclass(frozen=True)
class ShardPlan:
    """One shard of a run (8.3 `shards`). The rig must be registered first."""
    shard_id: str                          # <run_id>/<n>
    rig_id: str
    sides: str = "both"
    scope_count: int | None = None         # None: the whole run scope (a one-shard run)
    attempt: int = 1


@dataclass(frozen=True)
class RunPlan:
    """What `begin_run` needs: the 8.3 `runs` identity columns and the scope (2.11)."""
    run_id: str
    run_type: str
    config_commit: str
    config_tarball_sha256: str
    snapshot_set_sha256: str
    harness_sha: str
    authorization_ref: str
    budget_sec: int
    scope: tuple[ScopeEntry, ...]
    shards: tuple[ShardPlan, ...] = ()
    pair_id: int | None = None
    manifest_sha256: str | None = None
    pr_ref: str | None = None
    pr_author: str | None = None
    started_at: str = field(default_factory=now)
    base_commit_distance: int | None = None     # SD-02, L-BLD-04
    authorization_sha256: str | None = None     # 6.12
    diff_params_sha256: str | None = None
    reviewer_version_sha256: str | None = None
    requeue_of: str | None = None
    pr_committers: tuple[str, ...] | None = None   # 9.6 two-person rule, from the PR host
    flow_steps: Mapping[str, Sequence[str]] | None = None   # flow id -> its step ids, for step_failed:<step> (4.13)
    catalog_versions: Mapping[str, str] | None = None       # popup and mutant catalog versions (preflight G3, 8.3)
    data_window_start: str | None = None                    # 8.3 runs.data_window_start and _end
    data_window_end: str | None = None


@dataclass(frozen=True)
class SessionContext:
    """The paper-mode facts of the current session of one shard that L-OUT-06 checks a record's stamp against:
    the allowlisted accounts' `account_id_sha256` values and the times of this session's assertions (6.4)."""
    shard_id: str | None
    allowlisted: frozenset[str]
    assertion_times: frozenset[str]        # UTC ISO 8601 seconds


@dataclass(frozen=True)
class PostRunProof:
    """One 8.3 `post_run_proofs` row: the ORDER SAFETY PROOF of one shard (6.6). `counts` is `counts_json`; it
    names the undeclared rows an `orders_sent` verdict lists (`counts["undeclared"]`, 6.6). `shard_id` None is the
    run-level row of a run that started no session (SD-61), which may have no login."""
    shard_id: str | None
    run_id: str
    login_alias: str | None                # the vault alias the store keys logins by (8.3); never printed
    verdict: str
    send_policy_ref: str
    counts: dict
    cancel_all: str
    paper_mode_assertions: int
    paper_mode_mismatches: int
    gateway_events: int
    gateway_drops: int
    produced_at: str
    reason: str | None = None
    start_snapshot_sha256: str | None = None
    end_snapshot_sha256: str | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    unreadable_count: int = 0              # SD-61
    refusals_count: int = 0
    attestation_ref: str | None = None

    def __post_init__(self):
        if self.shard_id is not None and not self.login_alias:
            raise StoreRefused("a shard's proof names its login (8.3)")
        if self.verdict not in PROOF_VERDICTS:
            raise StoreRefused(f"post-run proof verdict {self.verdict!r}; one of {PROOF_VERDICTS} (8.3)")
        if self.cancel_all not in PROOF_CANCEL_ALL:
            raise StoreRefused(f"post-run proof cancel_all {self.cancel_all!r}; one of {PROOF_CANCEL_ALL} (8.3)")
        if (self.verdict == "unproven") != (self.reason is not None):
            raise StoreRefused(f"post-run proof {self.verdict} with reason {self.reason!r}: a reason exactly when "
                               "the verdict is unproven (6.6, 8.3)")
        if self.verdict == "orders_sent" and not self.counts.get("undeclared"):
            raise StoreRefused("an orders_sent proof names its undeclared rows (counts_json undeclared, 6.6)")


@dataclass(frozen=True)
class Review:
    """A Reviewer verdict for the 8.3 `reviews` table (3.13 output with its provenance)."""
    run_id: str
    checkpoint_id: str
    diff_hash: str
    model_id: str
    prompt_hash: str
    request_sha256: str
    response_json: str
    verdict: str
    mapped_outcome: str
    confidence: float
    region_id: str | None = None
    fewshot_version: str | None = None
    redaction_version: str | None = None
    mask_version: str | None = None
    votes: int = 1
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    batch_id: str | None = None
    created_at: str = field(default_factory=now)
    response_model: str | None = None      # the 3.13 provenance columns
    registry_version: str | None = None
    request_ids_json: str | None = None
    cache_read_tokens: int | None = None
    votes_json: str | None = None
    task: str | None = None                # manifest_match | describe_collateral | manifest_visibility
    intent: str | None = None


@dataclass(frozen=True)
class DecisionLookup:
    """The answer of the 8.3 signature lookup: the full verdict set (F25, 9.7)."""
    matches: tuple[DecisionMatch, ...] = ()       # every active decision within tolerance, nearest then newest
    related: tuple[DecisionMatch, ...] = ()       # above tolerance, up to 12 bits: shown, never inherited (4.14)
    frozen: bool = False                          # two supersessions within 30 days: nothing inherited (9.7)
    out_of_scope: tuple[DecisionMatch, ...] = ()  # the mark_intended matches outside their 9.7 scope for this run

    @property
    def applicable(self) -> tuple[DecisionMatch, ...]:
        """The matches that apply to this run: all but the out-of-scope mark_intended ones (9.7)."""
        outside = {m.decision.decision_id for m in self.out_of_scope}
        return tuple(m for m in self.matches if m.decision.decision_id not in outside)

    @property
    def verdicts(self) -> frozenset[str]:
        return frozenset(m.decision.verdict for m in self.applicable)

    @property
    def conflict(self) -> bool:
        """Verdicts differ: nothing is inherited and the finding lists both decisions (9.7)."""
        return len(self.verdicts) > 1

    @property
    def inherited(self) -> Decision | None:
        """The nearest, then newest, applicable match when every applicable match carries one verdict and the
        signature is not frozen; else None (F25, 9.7)."""
        ok = self.applicable
        if not ok or self.conflict or self.frozen:
            return None
        return ok[0].decision


@dataclass(frozen=True)
class _Prepared:
    """A checked checkpoint batch, ready for `Store._put_checkpoint`."""
    outcome: CheckpointOutcome
    record: str
    steps: tuple[tuple[StepOutcome, str, str], ...]     # (step, shard, side)
    findings: tuple[Finding, ...]
    artifacts: tuple[StoredArtifact, ...]
    shard_id: str | None


@dataclass(frozen=True)
class SweepResult:
    """What one retention sweep did (8.6)."""
    deleted: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    skipped: bool = False                         # "retention skipped: backup failed"


# ---------------------------------------------------------------------------------------------------------------
# small helpers

def utc(ts: str | None) -> str | None:
    """ISO 8601 UTC text to the second (8.3); a naive time is taken as UTC."""
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def phash_bytes(p: bytes | str) -> bytes:
    """A 64-bit pHash as the 8-byte blob 8.3 stores; accepts the blob or 16 hex characters (L-DEC-03)."""
    if isinstance(p, (bytes, bytearray, memoryview)):
        b = bytes(p)
    else:
        s = str(p).lower()
        if not _PHASH_RE.match(s):
            raise StoreRefused(f"phash must be 16 hex characters, got {p!r} (L-DEC-03)")
        b = bytes.fromhex(s)
    if len(b) != 8:
        raise StoreRefused("phash must be 8 bytes (8.3)")
    return b


def hamming(a: bytes | str | None, b: bytes | str | None) -> int | None:
    """Population count of the XOR of two 8-byte pHashes: the SQL function 8.2 registers on every connection."""
    if a is None or b is None:
        return None
    return (int.from_bytes(phash_bytes(a), "big") ^ int.from_bytes(phash_bytes(b), "big")).bit_count()


def decision_int(ref: str | None) -> int | None:
    """`dec-000812` -> 812 (4.14 ids; 8.3 stores the integer)."""
    if ref is None:
        return None
    m = _DECISION_RE.match(ref)
    if not m:
        raise StoreRefused(f"decision id {ref!r} is not dec-<sequence> (4.14)")
    return int(m.group(1))


def decision_ref(n: int | None) -> str | None:
    return None if n is None else f"dec-{n:06d}"


def _sql_list(values: Iterable[str]) -> str:
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def _pattern(p: str) -> tuple[str, int, str | None, str]:
    """A taxonomy reason pattern -> (code, parametric, detail placeholder, separator)."""
    if "<" not in p:
        return p, 0, None, ":"
    i = p.index("<")
    return p[: i - 1], 1, p[i:], p[i - 1]


def _vocab(t: Taxonomy, name: str) -> list[str]:
    if name == "reason_codes":
        return sorted({_pattern(p)[0] for pats in t.data["reasons"].values() for p in pats})
    try:
        return list(t.list(name))
    except KeyError:
        raise StoreError(f"schema.sql names taxonomy list {name!r}, which taxonomy.yaml lacks") from None


def _not_run_reason_check(t: Taxonomy) -> str:
    pats = [_pattern(p) for p in t.data["reasons"]["not_run"]]
    expr = f"not_run_reason IN ({_sql_list(c for c, par, _, _ in pats if not par)})"
    return expr + "".join(f" OR not_run_reason LIKE '{c}{sep}%'" for c, par, _, sep in pats if par)


def _action_verdict_check(t: Taxonomy) -> str:
    actions, verdicts = t.list("decision_actions"), t.list("decision_verdicts")
    mapping = {a: (v,) for a, v in ACTION_VERDICT.items()} | MULTI_VERDICT
    unmapped = [a for a in actions if a not in mapping]
    unknown = [x for a, vs in mapping.items() for x in (a, *vs) if x not in actions and x not in verdicts]
    if unmapped or unknown:
        raise StoreError(f"decision mapping and taxonomy.yaml disagree: unmapped {unmapped}, unknown {unknown}")
    clauses = []
    for v in verdicts:
        group = [a for a in actions if mapping[a] == (v,)]
        if group:
            clauses.append(f"(action IN ({_sql_list(group)}) AND verdict = {_sql_list([v])})")
    for a in actions:
        if len(mapping[a]) > 1:
            clauses.append(f"(action = {_sql_list([a])} AND verdict IN ({_sql_list(mapping[a])}))")
    return "\n      OR ".join(clauses)


def render_schema(t: Taxonomy) -> str:
    """schema.sql with every placeholder filled from the taxonomy (8.3, L-OUT-01)."""
    template = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        name, items = m.group(1), m.group(2)
        if name == "not_run_reason":
            return _not_run_reason_check(t)
        if name == "decision_action_verdict":
            return _action_verdict_check(t)
        values = _vocab(t, name)
        if items is None:
            return _sql_list(values)
        wanted = items.split(",")
        missing = [w for w in wanted if w not in values]
        if missing:
            raise StoreError(f"schema.sql names {missing}, which taxonomy.yaml {name} lacks")
        return _sql_list(wanted)

    return _PLACEHOLDER.sub(sub, template)


def seed_rows(t: Taxonomy) -> list[tuple[str, str, int, str | None, str]]:
    """The outcome_reasons seed (outcome, reason, parametric, detail_means, owner_section) from taxonomy.yaml.

    With a `reason_metadata` map in taxonomy.yaml (`{outcome: {code: {owner_section, detail_means}}}`, generated
    from the 8.3 seed and 12.2), each row takes its owner section and detail meaning from it; a reason the map
    lacks keeps the fallback of the module docstring."""
    meta = t.data.get("reason_metadata") or {}
    rows: dict[tuple[str, str], tuple[str, str, int, str | None, str]] = {}
    for outcome, pats in t.data["reasons"].items():
        for p in pats:
            code, par, means, _ = _pattern(p)
            m = (meta.get(outcome) or {}).get(code) or {}
            rows.setdefault((outcome, code), (outcome, code, par, m.get("detail_means", means),
                                              m.get("owner_section", "4.13")))
    for child, parent in t.data.get("reason_inheritance", {}).items():
        for (o, code), (_, _, par, means, _) in list(rows.items()):
            if o == parent:
                rows.setdefault((child, code), (child, code, par, means, "1.10"))
    return list(rows.values())


def _seed_sql(rows: Sequence[tuple]) -> str:
    def lit(v: Any) -> str:
        return "NULL" if v is None else str(v) if isinstance(v, int) else _sql_list([v])
    values = ",\n ".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows)
    return ("INSERT INTO outcome_reasons (outcome, reason, parametric, detail_means, owner_section) VALUES\n "
            + values + ";\n")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_local_volume(path: str | os.PathLike) -> None:
    """Refuse a database or artifact root on a share (8.2, preflight H7, 8.9)."""
    s = os.fspath(path)
    if s.startswith(("\\\\", "//")):
        raise StoreError(f"{s} is a UNC path; the store needs a fixed local volume (8.2, H7)")
    if sys.platform == "win32":
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(s))[0] + "\\"
        if ctypes.windll.kernel32.GetDriveTypeW(drive) != 3:   # DRIVE_FIXED
            raise StoreError(f"{s} is not on a fixed local drive (8.2, H7)")
        return
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return
    target = os.path.realpath(s)
    best, fstype = "", ""
    for line in mounts.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, fstype = mnt, parts[2]
    if fstype in NETWORK_FS:
        raise StoreError(f"{s} is on a network file system ({fstype}); the store needs a local volume (8.2, H7)")


class WriterLock:
    """`writer.lock` beside the database, holding the writer's PID (8.2, 8.4)."""

    STALE_EMPTY_SEC = 60

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._stale():
                    raise WriterLockError(f"another writer holds {self.path} ({self._text()!r}); 8.2 allows one")
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
                f.flush()
                os.fsync(f.fileno())
            self.held = True
            return
        raise WriterLockError(f"could not take {self.path}")

    def _text(self) -> str:
        try:
            return self.path.read_text().strip()
        except OSError:
            return ""

    def _stale(self) -> bool:
        text = self._text()
        if text.isdigit():
            return not _pid_alive(int(text))
        try:                                       # a writer between create and write leaves it empty briefly
            return time.time() - self.path.stat().st_mtime > self.STALE_EMPTY_SEC
        except FileNotFoundError:
            return True

    def release(self) -> None:
        if self.held and self._text() == str(os.getpid()):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
        self.held = False


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.create_function("hamming", 2, hamming, deterministic=True)


def connect_reader(path: str | os.PathLike, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """A read-only connection: `mode=ro`, `PRAGMA query_only = 1`, `hamming()` registered (8.2, 8.7)."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    _configure(conn)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA query_only = 1")
    return conn


# ---------------------------------------------------------------------------------------------------------------
# the decision lookup (8.3) and inheritance (4.14), usable on any connection with the decisions table

# Only actions that act on a later checkpoint outcome through the signature are looked up (S-02; 8.3 and 4.14
# action tables): request_mask is inert until its mask merges (mask_ref set); agent_mistake and expected_behavior
# close an Explorer finding's signature; approve_baseline and reject_baseline move a baselines row.
_LOOKUP_WHERE = """
WHERE d.checkpoint_id = :cp AND d.region_id = :region AND d.category = :cat
  AND d.verdict IS NOT NULL AND d.status IN ('active','reopened')
  AND d.revoked_at IS NULL AND (d.expires_at IS NULL OR d.expires_at > :now)
  AND (d.action IN ('confirm_bug','mark_intended','noise_signature','fixed_in_build')
       OR (d.action = 'request_mask' AND d.mask_ref IS NOT NULL))"""
_DECISION_FROM = """FROM decisions d LEFT JOIN runs r ON r.run_id = d.run_id
  LEFT JOIN findings f ON f.finding_id = d.source_finding_id"""
_DECISION_COLUMNS = """d.*, r.pair_id AS run_pair_id, f.manifest_entry AS finding_manifest_entry,
  f.natural_key AS finding_natural_key"""
_LOOKUP_FROM = f"SELECT {_DECISION_COLUMNS}, hamming(d.phash, :phash) AS distance\n{_DECISION_FROM}"
LOOKUP_SQL = (_LOOKUP_FROM + _LOOKUP_WHERE + """
  AND hamming(d.phash, :phash) <= d.hamming_tolerance
ORDER BY hamming(d.phash, :phash), d.decided_at DESC""")   # 8.3: no LIMIT, the verdict set is the answer (9.7)
RELATED_SQL = (_LOOKUP_FROM + _LOOKUP_WHERE + """
  AND hamming(d.phash, :phash) > d.hamming_tolerance AND hamming(d.phash, :phash) <= :related
ORDER BY hamming(d.phash, :phash), d.decided_at DESC""")
FREEZE_DAYS = 30               # 9.7: two supersessions within 30 days on one signature freeze it
WAIVER_CATEGORIES = ("invariant_violation", "assertion_failed")   # mark_intended on these is a waiver (9.7)
# 1.10 precedence: the computed diff outcomes a noise or intended verdict may replace (S-06). Everything ranked
# above them (env_error to needs_baseline) is not a diff the decision speaks for, and stays as computed.
INHERITABLE_OUTCOMES = ("collateral", "needs_human")
BASELINE_ACTIONS = ("approve_baseline", "reject_baseline")   # 9.8: act on baselines rows, not through the signature


def _decision_from_row(r: sqlite3.Row) -> Decision:
    """A `decisions` row selected with `_DECISION_COLUMNS` as the 4.14 record: the build pair is the run's, the
    finding ref and manifest entry are the source finding's (8.3 has no decision columns for them)."""
    pair, box = r["run_pair_id"], r["bbox_q4"]
    return Decision(
        decision_id=decision_ref(r["decision_id"]), action=r["action"], verdict=r["verdict"],
        signature=Signature(r["checkpoint_id"], r["region_id"], bytes(r["phash"]).hex(), r["category"]),
        decided_by=r["decided_by"], hamming_tolerance=r["hamming_tolerance"], ticket=r["ticket_ref"],
        status=r["status"], source=r["source"], reason=r["reason"], decided_at=r["decided_at"],
        run_id=r["run_id"], build_pair_id=None if pair is None else str(pair),
        manifest_entry_ref=r["finding_manifest_entry"], finding_ref=r["finding_natural_key"],
        confirmed_by=r["confirmed_by"], expires_at=r["expires_at"], role=r["role"], reopen_count=r["reopen_count"],
        bbox_q4=None if box is None else tuple(int(v) for v in box.split(",")),
        supersedes=decision_ref(r["supersedes_decision_id"]))


def _match(r: sqlite3.Row) -> DecisionMatch:
    """A lookup row as the one match type (classify.DecisionMatch), with the build pair of the decision's run."""
    d = _decision_from_row(r)
    return DecisionMatch(d, r["distance"], build_pair_id=d.build_pair_id)


def _frozen(conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]) -> bool:
    """9.7: two supersessions within 30 days on one signature freeze it (S-05). Walks each row's chain."""
    for row in rows:
        times, seen = [], set()
        while row is not None and row["supersedes_decision_id"] is not None and row["decision_id"] not in seen:
            seen.add(row["decision_id"])
            times.append(datetime.fromisoformat(row["decided_at"]))
            row = conn.execute("SELECT decision_id, decided_at, supersedes_decision_id FROM decisions "
                               "WHERE decision_id = ?", (row["supersedes_decision_id"],)).fetchone()
        times.sort()
        if any(b - a <= timedelta(days=FREEZE_DAYS) for a, b in zip(times, times[1:])):
            return True
    return False


def _in_ticket_scope(d: Decision, run_id: str | None, run_pair: str | None, entries: frozenset[str],
                     tickets: frozenset[str]) -> bool:
    """9.7 mark_intended scope: the build pair it was made on, or a run whose loaded manifest carries its entry or
    its ticket. A waiver (mark_intended on an invariant or assertion) applies to every run while active."""
    if d.action != "mark_intended" or d.signature.category in WAIVER_CATEGORIES:
        return True
    return ((run_id is not None and d.run_id == run_id)
            or (run_pair is not None and d.build_pair_id == run_pair)
            or (d.manifest_entry_ref is not None and d.manifest_entry_ref in entries)
            or (d.ticket is not None and d.ticket in tickets))


def lookup_decisions(conn: sqlite3.Connection, signature: Signature, *, at: str | None = None,
                     rescued: bool = False, synthetic: bool = False, run_id: str | None = None,
                     manifest_entries: Iterable[str] = (), manifest_tickets: Iterable[str] = ()
                     ) -> DecisionLookup:
    """Every active decision within tolerance of `signature`, plus the related ones up to 12 bits (8.3, 4.14).

    A rescued checkpoint (5.5) and a synthetic outcome (10.3) never run the query: the answer is empty.
    `run_id` (the run being classified; its build pair is read from `runs`), `manifest_entries` and
    `manifest_tickets` (the entry ids and tickets of the run's loaded manifests) give the 9.7 scope of a
    `mark_intended` decision: one outside it stays in `matches` (the full 8.3 answer) but is listed in
    `out_of_scope`, so it neither conflicts nor is inherited. Without them no `mark_intended` decision made on
    another run is in scope.
    """
    if rescued or synthetic:
        return DecisionLookup()
    args = {"cp": signature.checkpoint_id, "region": signature.region_id, "cat": signature.category,
            "phash": phash_bytes(signature.phash), "now": utc(at) or now(), "related": RELATED_LIMIT}
    rows = conn.execute(LOOKUP_SQL, args).fetchall()
    pair = None
    if run_id is not None:
        r = conn.execute("SELECT pair_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        pair = None if r is None or r["pair_id"] is None else str(r["pair_id"])
    entries, tickets = frozenset(manifest_entries), frozenset(manifest_tickets)
    found = [_match(r) for r in rows]
    outside = [m for m in found if not _in_ticket_scope(m.decision, run_id, pair, entries, tickets)]
    related = [_match(r) for r in conn.execute(RELATED_SQL, args)]
    return DecisionLookup(tuple(found), tuple(related), frozen=_frozen(conn, rows), out_of_scope=tuple(outside))


def apply_inheritance(outcome: CheckpointOutcome, lookup: DecisionLookup) -> CheckpointOutcome:
    """The 4.14 inheritance rule for one computed outcome; returns a new record, never edits `outcome`.

    No match, a conflict (F25), a frozen signature (9.7), a rescued or a synthetic row: unchanged.
    `invariant_violation` and `assertion_failed` keep their outcome and only carry the decision (it suppresses the
    finding). A `noise` or `intended` verdict sets that outcome with `decision:<id>` on a computed `collateral` or
    `needs_human` diff; a `confirmed_bug` verdict keeps the computed outcome and links the decision (a computed
    `collateral` carries reason `decision:<id>`, 8.3 seed). Every other outcome is returned unchanged (1.10).
    """
    d = lookup.inherited
    if d is None or outcome.rescued or outcome.synthetic:
        return outcome
    if outcome.outcome in WAIVER_CATEGORIES:
        return dataclasses.replace(outcome, decision_ref=d.decision_id)
    if outcome.outcome not in INHERITABLE_OUTCOMES:
        return outcome
    if d.verdict in ("noise", "intended"):
        return dataclasses.replace(outcome, outcome=d.verdict, reason=f"decision:{d.decision_id}",
                                   decision_ref=d.decision_id, review_state="none")
    if outcome.outcome == "collateral" and outcome.reason is None:
        return dataclasses.replace(outcome, reason=f"decision:{d.decision_id}", decision_ref=d.decision_id)
    return dataclasses.replace(outcome, decision_ref=d.decision_id)


# ---------------------------------------------------------------------------------------------------------------
# record <-> row

def _component(c: dict) -> Component:
    """A stored diff component; one with a region-relative box comes back as the engine's `RegionComponent`."""
    c = {**c, "bbox": tuple(c["bbox"])}
    if "bbox_region" not in c:
        return Component(**c)
    from ..diff.engine import RegionComponent      # records.Component has no bbox_region field (engine choice 16)
    return RegionComponent(**{**c, "bbox_region": tuple(c["bbox_region"])})


def _outcome_from_dict(d: dict) -> CheckpointOutcome:
    d = dict(d)
    if "diff" in d:
        diff = dict(d["diff"])
        diff["shift"] = tuple(diff.get("shift", (0, 0)))
        diff["regions"] = [RegionDiff(**r) for r in diff.get("regions", [])]
        diff["components"] = [_component(c) for c in diff.get("components", [])]
        d["diff"] = DiffSummary(**diff)
    if "classification" in d:
        d["classification"] = Classification(**d["classification"])
    return CheckpointOutcome(**d)


def _gateway_columns(event: dict | None) -> dict:
    """A step's 4.13 `gateway_event` as the 8.3 `step_events` columns; the schema's CHECKs refuse a decision, code
    or class outside the closed lists, and a drop or refusal without its code."""
    if event is None:
        return {"gate_decision": "none", "gate_reason": None, "gate_class": None}
    if "decision" not in event or set(event) - GATEWAY_EVENT_KEYS:
        raise StoreRefused(f"gateway_event {event!r}: decision, and optionally reason and class (4.13)")
    return {"gate_decision": event["decision"], "gate_reason": event.get("reason"), "gate_class": event.get("class")}


def _gateway_event(row: sqlite3.Row) -> dict | None:
    """The 4.13 `gateway_event` of a `step_events` row (reason and class only when set), None for gate_decision
    none."""
    if row["gate_decision"] == "none":
        return None
    event = {"decision": row["gate_decision"], "reason": row["gate_reason"], "class": row["gate_class"]}
    return {k: v for k, v in event.items() if v is not None}


def _revalidate(record):
    """Records are mutable dataclasses: rebuilding one re-runs its taxonomy checks (TaxonomyError)."""
    return dataclasses.replace(record)


def _json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"))


def _summary_json(summary: Mapping[str, Any]) -> str:
    return json.dumps(summary, sort_keys=True)


def _ledger_default(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    raise TypeError(type(v).__name__)


# ---------------------------------------------------------------------------------------------------------------

class Store:
    """The single writer of one Sentinel database (8.2). Use as a context manager or call `close()`."""

    def __init__(self, path: str | os.PathLike, *, taxonomy: Taxonomy | None = None, ledger: bool = True,
                 busy_timeout_ms: int = 5000, require_local: bool = True, strict_records: bool = False,
                 system_owners: Iterable[str] = ()):
        """`strict_records` refuses, at `write_checkpoint`, a record that fails the 4.13 required-fields table
        (L-OUT-02, `RecordIncomplete`); `system_owners` are the logins that may decide on a frozen signature (9.7)."""
        self.path = Path(path)
        self.taxonomy = taxonomy or load()
        self.strict_records = strict_records
        self.system_owners = frozenset(system_owners)
        self._sessions: dict[str | None, SessionContext] = {}
        self._flow_steps: dict[str, dict[str, frozenset[str]]] = {}
        if require_local:
            check_local_volume(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = WriterLock(self.path.parent / "writer.lock")
        self._lock.acquire()
        try:
            self._conn = sqlite3.connect(str(self.path), isolation_level=None)
            _configure(self._conn)
            for p in ("journal_mode = WAL", "synchronous = NORMAL", "foreign_keys = ON",
                      f"busy_timeout = {int(busy_timeout_ms)}", "wal_autocheckpoint = 1000"):
                self._conn.execute(f"PRAGMA {p}")
            self._ledger = self.path.parent / "results.jsonl" if ledger else None
            self._forms = {(o, c): (par, sep) for o, pats in self.taxonomy.data["reasons"].items()
                           for c, par, _, sep in map(_pattern, pats)}
            for child, parent in self.taxonomy.data.get("reason_inheritance", {}).items():
                for (o, c), form in list(self._forms.items()):
                    if o == parent:
                        self._forms.setdefault((child, c), form)
            self._open_schema()
        except BaseException:
            if hasattr(self, "_conn"):
                self._conn.close()
            self._lock.release()
            raise

    # -- lifecycle -----------------------------------------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None
        self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """The writer connection, for reads in this process. Write only through the methods."""
        return self._conn

    def _open_schema(self) -> None:
        ddl = render_schema(self.taxonomy)
        digest = hashlib.sha256(ddl.encode()).hexdigest()
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise StoreError(f"{self.path} holds tables but no Sentinel schema version")
            at = now()
            state = _sql_list(["schema_version", str(SCHEMA_VERSION), at]), _sql_list(["schema_sha256", digest, at])
            self._conn.executescript(
                "BEGIN IMMEDIATE;\n" + ddl + "\n" + _seed_sql(seed_rows(self.taxonomy))
                + "".join(f"INSERT INTO system_state (key, value, updated_at) VALUES ({s});\n" for s in state)
                + f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
            return
        if version != SCHEMA_VERSION:
            raise StoreError(f"{self.path} has schema version {version}; this store reads {SCHEMA_VERSION} and has "
                             "no migration for it (migrations are forward-only, after VACUUM INTO, 8.6)")
        row = self._conn.execute("SELECT value FROM system_state WHERE key = 'schema_sha256'").fetchone()
        if row is None or row[0] != digest:
            raise StoreError(f"{self.path} was created from a different taxonomy.yaml (a generated CHECK changed); "
                             "it needs a migration before this store can write it (8.3, 8.6)")
        self._sync_reasons()

    def _sync_reasons(self) -> None:
        """8.3: the reason table is seeded from taxonomy.yaml at service start; reasons are retired, never deleted."""
        rows = seed_rows(self.taxonomy)
        keys = {(r[0], r[1]) for r in rows}
        with self._batch("reasons_sync") as _:
            self._conn.executemany("INSERT OR IGNORE INTO outcome_reasons (outcome, reason, parametric, "
                                   "detail_means, owner_section) VALUES (?,?,?,?,?)", rows)
            for o, r, active in self._conn.execute("SELECT outcome, reason, active FROM outcome_reasons").fetchall():
                want = 1 if (o, r) in keys else 0
                if active != want:
                    self._conn.execute("UPDATE outcome_reasons SET active = ? WHERE outcome = ? AND reason = ?",
                                       (want, o, r))

    # -- transactions and the ledger -----------------------------------------------------------------------
    @contextlib.contextmanager
    def _batch(self, kind: str) -> Iterator[list[dict]]:
        """One BEGIN IMMEDIATE transaction; its rows go to results.jsonl before COMMIT (8.2)."""
        if self._conn is None:
            raise StoreError("store is closed")
        for attempt in range(4):                       # 8.9: busy timeout and 3 retries
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if attempt == 3 or not any(w in str(e) for w in ("locked", "busy")):
                    raise StoreWriteFailed(f"cannot begin a write: {e}") from e
        rows: list[dict] = []
        try:
            yield rows
            if self._ledger is not None and rows:
                line = json.dumps({"batch": kind, "at": now(), "rows": rows}, sort_keys=True,
                                  default=_ledger_default)
                with open(self._ledger, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            self._conn.execute("COMMIT")
        except BaseException as e:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(e, sqlite3.IntegrityError):
                raise StoreRefused(f"refused by the schema: {e}") from e
            if isinstance(e, sqlite3.OperationalError):
                raise StoreWriteFailed(f"write failed: {e}") from e
            if isinstance(e, OSError):
                raise StoreWriteFailed(f"ledger append failed: {e}") from e
            raise

    def _insert(self, rows: list[dict], table: str, values: dict, *, upsert: str | None = None) -> int:
        cols = list(values)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        if upsert:
            sql += f" ON CONFLICT {upsert}"
        cur = self._conn.execute(sql, [values[c] for c in cols])
        rows.append({"op": "upsert" if upsert else "insert", "table": table, "row": values})
        return cur.lastrowid

    def _update(self, rows: list[dict], table: str, key: dict, values: dict) -> int:
        sets = ",".join(f"{c} = ?" for c in values)
        where = " AND ".join(f"{c} = ?" for c in key)
        cur = self._conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*values.values(), *key.values()])
        rows.append({"op": "update", "table": table, "key": key, "set": values})
        return cur.rowcount

    def _one(self, sql: str, args: Sequence = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    # -- reasons -------------------------------------------------------------------------------------------
    def _split_reason(self, outcome: str, reason: str | None) -> tuple[str | None, str | None]:
        if reason is None or reason == "":
            return None, None
        code, sep, param = reason.partition(":")
        if sep:
            return code, param
        head, dot, tail = reason.partition(".")
        if dot and self._forms.get((outcome, head), (0, ":"))[1] == ".":
            return head, tail
        return reason, None

    def _join_reason(self, outcome: str, code: str | None, param: str | None) -> str | None:
        if code is None:
            return None
        parametric, sep = self._forms.get((outcome, code), (0, ":"))
        return f"{code}{sep}{param}" if parametric else code

    def _encode_reason(self, o: CheckpointOutcome) -> tuple[str | None, str | None]:
        """(reason code, reason_detail) for an outcome record; see the module docstring."""
        code, param = self._split_reason(o.outcome, o.reason)
        if param is not None and "\n" in param:
            raise StoreRefused("a reason parameter is one line (8.3)")
        detail = o.detail
        if detail is None and o.outcome == "invariant_violation" and o.invariant_ids and param is None:
            detail = ",".join(o.invariant_ids)
        if param is None:
            return code, detail
        return code, param if detail is None else f"{param}\n{detail}"

    def _decode_reason(self, outcome: str, code: str | None, stored: str | None) -> tuple[str | None, str | None]:
        if code is None:
            return None, stored
        if self._forms.get((outcome, code), (0, ":"))[0]:
            param, _, detail = (stored or "").partition("\n")
            return self._join_reason(outcome, code, param), detail or None
        return code, stored

    # -- identity: logins, rigs, builds, checkpoints -------------------------------------------------------
    def register_rig(self, rig_id: str, *, login_alias: str, owner: str, snapshot_id: str, allowlist_entry: int,
                     tier: str = "A", role: str = "pool", login_kind: str = "scripted") -> None:
        """Add or refresh a rig and its login (8.3 `rigs`, `logins`); the vault alias only, never a credential. A
        refresh never puts back in service a rig a safety action took out (`disabled_at` set and no human re-enable
        since): only `safety.enable_rig` does that (6.6 step 6, 6.12)."""
        with self._batch("rig") as rows:
            if self._one("SELECT 1 FROM logins WHERE login_alias = ?", (login_alias,)) is None:
                self._insert(rows, "logins", {"login_alias": login_alias, "kind": login_kind,
                                              "allowlist_entry": allowlist_entry, "owner": owner})
            self._insert(rows, "rigs", {"rig_id": rig_id, "tier": tier, "role": role, "login_alias": login_alias,
                                        "snapshot_id": snapshot_id, "active": 1},
                         upsert="(rig_id) DO UPDATE SET tier = excluded.tier, role = excluded.role, "
                                "login_alias = excluded.login_alias, snapshot_id = excluded.snapshot_id, "
                                "active = CASE WHEN rigs.disabled_at IS NOT NULL AND rigs.enabled_at IS NULL "
                                "THEN rigs.active ELSE 1 END")

    def register_build(self, build_id: str, *, channel: str, source_commit: str, pipeline_artifact_url: str,
                       installer_sha256: str, branch: str | None = None, first_seen_at: str | None = None,
                       released_at: str | None = None) -> None:
        """Add a build (8.3 `builds`, 4.2). A build id read again with different facts is an import conflict."""
        row = {"build_id": build_id, "channel": channel, "source_commit": source_commit, "branch": branch,
               "pipeline_artifact_url": pipeline_artifact_url, "installer_sha256": installer_sha256,
               "first_seen_at": utc(first_seen_at) or now(), "released_at": utc(released_at)}
        old = self._one("SELECT * FROM builds WHERE build_id = ?", (build_id,))
        if old is not None:
            same = all(old[k] == row[k] for k in ("channel", "source_commit", "installer_sha256"))
            if not same:
                raise ImportConflict(f"build {build_id} is already recorded with other facts (4.2)")
            return
        with self._batch("build") as rows:
            self._insert(rows, "builds", row)

    def register_pair(self, candidate_build_id: str, base_build_id: str, pair_kind: str, *,
                      merge_base_commit: str | None = None, created_at: str | None = None) -> int:
        """The 8.3 `build_pairs` row of a run's two builds (3.1), registered before `begin_run` so the run plan can
        carry its `pair_id`; returns the pair id. The same builds, kind and merge base again return the same pair,
        so every run on the pair shares it (the 9.7 mark_intended scope). Both builds must be registered; candidate
        and base differ except for an `aa` pair (8.3 CHECK)."""
        if pair_kind not in PAIR_KINDS:
            raise StoreRefused(f"pair_kind {pair_kind!r}; one of {PAIR_KINDS} (8.3)")
        for b in (candidate_build_id, base_build_id):
            if self._one("SELECT 1 FROM builds WHERE build_id = ?", (b,)) is None:
                raise StoreRefused(f"build {b} is not registered (register_build first, 8.3)")
        old = self._one("SELECT pair_id FROM build_pairs WHERE candidate_build_id = ? AND base_build_id = ? AND "
                        "pair_kind = ? AND merge_base_commit IS ?",
                        (candidate_build_id, base_build_id, pair_kind, merge_base_commit))
        if old is not None:
            return old[0]
        with self._batch("build_pair") as rows:
            return self._insert(rows, "build_pairs", {
                "candidate_build_id": candidate_build_id, "base_build_id": base_build_id, "pair_kind": pair_kind,
                "merge_base_commit": merge_base_commit, "created_at": utc(created_at) or now()})

    def _checkpoint_row(self, e: ScopeEntry, config_commit: str) -> dict:
        parts = checkpoint_parts(e.checkpoint_id)
        kind, q = parts["kind"], parts["qualifier"].split("/")
        if e.persona not in PERSONAS:
            raise TaxonomyError(f"persona {e.persona!r}")
        if e.data_mode not in DATA_MODES:
            raise TaxonomyError(f"data_mode {e.data_mode!r}")
        row: dict[str, Any] = {"checkpoint_id": e.checkpoint_id, "kind": kind, "surface_id": parts["surface"],
                               "workspace_id": e.workspace_id, "tier": e.tier, "data_mode": e.data_mode,
                               "recapturable": int(e.recapturable), "first_seen_commit": config_commit}
        if kind == "census" and q[0] != e.workspace_id:
            raise StoreRefused(f"{e.checkpoint_id}: a census id's qualifier is its workspace id (8.3)")
        if kind == "sweep":
            w, h = q[1].removesuffix("-asc").split("x")       # SD-04: Ladder F ascending ids end in -asc
            row.update(host=q[0], size_w=int(w), size_h=int(h))
        elif kind == "split":
            row.update(splitter_id=q[0], fraction=float(q[1]))
        elif kind == "flow":
            if not e.flow_class:
                raise StoreRefused(f"{e.checkpoint_id}: a flow checkpoint needs its flow_class (8.3)")
            row.update(flow_id=q[0], step_id=q[1], flow_class=e.flow_class)
        return row

    def _upsert_checkpoint(self, rows: list[dict], e: ScopeEntry, config_commit: str) -> None:
        self._insert(rows, "checkpoints", self._checkpoint_row(e, config_commit),
                     upsert="(checkpoint_id) DO UPDATE SET workspace_id = excluded.workspace_id, tier = excluded.tier, "
                            "data_mode = excluded.data_mode, flow_class = excluded.flow_class, "
                            "recapturable = excluded.recapturable")

    def register_checkpoints(self, entries: Iterable[ScopeEntry], *, config_commit: str) -> None:
        """Add or refresh `checkpoints` rows (8.3) outside a run's scope, e.g. for probe-found surfaces."""
        with self._batch("checkpoints") as rows:
            for e in entries:
                self._upsert_checkpoint(rows, e, config_commit)

    # -- runs ----------------------------------------------------------------------------------------------
    def begin_run(self, plan: RunPlan) -> None:
        """Write the run, its checkpoints, its scope and its shards in one batch; lifecycle `running` (8.3)."""
        if plan.run_type not in RUN_TYPES:
            raise TaxonomyError(f"run_type {plan.run_type!r}")
        ids = [e.checkpoint_id for e in plan.scope]
        if len(ids) != len(set(ids)):
            raise StoreRefused("a checkpoint appears twice in the scope (one row per checkpoint per run, 8.3)")
        if self._one("SELECT 1 FROM runs WHERE run_id = ?", (plan.run_id,)) is not None:
            raise StoreRefused(f"run {plan.run_id} has already begun")
        with self._batch("run_begin") as rows:
            self._insert(rows, "runs", {
                "run_id": plan.run_id, "run_type": plan.run_type, "pair_id": plan.pair_id, "lifecycle": "running",
                "config_commit": plan.config_commit, "config_tarball_sha256": plan.config_tarball_sha256,
                "snapshot_set_sha256": plan.snapshot_set_sha256, "harness_sha": plan.harness_sha,
                "authorization_ref": plan.authorization_ref, "manifest_sha256": plan.manifest_sha256,
                "pr_ref": plan.pr_ref, "pr_author": plan.pr_author, "budget_sec": plan.budget_sec,
                "started_at": utc(plan.started_at), "scope_count": len(ids),
                "base_commit_distance": plan.base_commit_distance, "authorization_sha256": plan.authorization_sha256,
                "diff_params_sha256": plan.diff_params_sha256, "reviewer_version_sha256": plan.reviewer_version_sha256,
                "requeue_of": plan.requeue_of,
                "pr_committers_json": None if plan.pr_committers is None else json.dumps(list(plan.pr_committers)),
                "catalog_versions_json": None if plan.catalog_versions is None else canonical_json(
                    dict(plan.catalog_versions)),
                "data_window_start": utc(plan.data_window_start), "data_window_end": utc(plan.data_window_end)})
            for e in plan.scope:
                self._upsert_checkpoint(rows, e, plan.config_commit)
                self._insert(rows, "run_scope", {"run_id": plan.run_id, "checkpoint_id": e.checkpoint_id,
                                                 "persona": e.persona})
            for s in plan.shards:
                if not s.shard_id.startswith(plan.run_id + "/"):
                    raise StoreRefused(f"shard id {s.shard_id!r} is not <run_id>/<n> (8.3)")
                self._insert(rows, "shards", {
                    "shard_id": s.shard_id, "run_id": plan.run_id, "rig_id": s.rig_id, "attempt": s.attempt,
                    "sides": s.sides, "scope_count": len(ids) if s.scope_count is None else s.scope_count,
                    "state": "running", "started_at": utc(plan.started_at)})
        if plan.flow_steps is not None:
            self._flow_steps[plan.run_id] = {f: frozenset(s) for f, s in plan.flow_steps.items()}

    def set_session_context(self, shard_id: str | None, allowlisted_sha256s: Iterable[str],
                            assertion_times: Iterable[str]) -> None:
        """L-OUT-06: from now on a record written for `shard_id` (None: written without a shard) is refused with
        `PaperAssertionMissing` unless its `paper_assertion` names an allowlisted `account_id_sha256` and the time
        of one of this session's paper-mode assertions (6.4). Call it again at each assertion with every time of
        the current session; a new session replaces the earlier one's times. A record that references no capture
        artifact may omit the stamp (SD-48); a synthetic mutant row is generated, not captured, and is not checked.
        """
        times = frozenset(utc(t) for t in assertion_times)
        allowed = frozenset(allowlisted_sha256s)
        if not allowed or not times:
            raise StoreRefused("a session context names the allowlisted accounts and its assertion times (6.4)")
        self._sessions[shard_id] = SessionContext(shard_id, allowed, times)

    def _check_paper(self, o: CheckpointOutcome, shard_id: str | None) -> None:
        ctx = self._sessions.get(shard_id)
        if ctx is None or o.synthetic:
            return
        stamp = o.paper_assertion
        if stamp is None:
            if required_fields.references_capture(o):
                raise PaperAssertionMissing(f"{o.checkpoint_id} references a capture but carries no paper_assertion "
                                            "(L-OUT-06)")
            return
        if stamp["account_id_sha256"] not in ctx.allowlisted:
            raise PaperAssertionMissing(f"{o.checkpoint_id}: paper_assertion account {stamp['account_id_sha256']} is "
                                        "not an allowlisted account's (L-OUT-06)")
        if utc(stamp["asserted_at"]) not in ctx.assertion_times:
            raise PaperAssertionMissing(f"{o.checkpoint_id}: paper_assertion at {stamp['asserted_at']} is not from "
                                        "the current session (L-OUT-06)")

    def record_errors(self, outcome: CheckpointOutcome) -> list[str]:
        """What `outcome` lacks under the 4.13 required-fields table (L-OUT-02); `step_failed:<step>` is checked
        against the steps of its flow: the run plan's `flow_steps`, plus the steps of registered flow checkpoints."""
        steps: dict[str, set[str]] = {}
        if outcome.outcome == "not_run" and (outcome.reason or "").startswith("step_failed:"):
            steps = {f: set(s) for f, s in self._flow_steps.get(outcome.run_id, {}).items()}
            for f, s in self._conn.execute("SELECT flow_id, step_id FROM checkpoints WHERE flow_id IS NOT NULL"):
                steps.setdefault(f, set()).add(s)
        return required_fields.errors(outcome, flow_steps=steps)

    def _running(self, run_id: str) -> sqlite3.Row:
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        if run["lifecycle"] == "finished":
            raise StoreRefused(f"run {run_id} is finished; its outcome rows are final (4.13)")
        return run

    def end_shard(self, shard_id: str, status: str, reason: str | None = None) -> None:
        """The shard's own outcome at teardown (8.3 `shards.shard_outcome`), state `finished`."""
        self.taxonomy.check_run(status, reason)
        code, param = self._split_reason(status, reason)
        with self._batch("shard_end") as rows:
            self._update(rows, "shards", {"shard_id": shard_id},
                         {"state": "finished", "shard_outcome": status, "abort_reason": code,
                          "abort_detail": param, "finished_at": now()})

    def mark_shard_imported(self, shard_id: str, *, run_store_sha256: str | None = None,
                            ledger_sha256: str | None = None, ledger_rows: int | None = None) -> None:
        """Mark a shard imported (8.2 shard import, done by the import), then checkpoint the WAL passively."""
        with self._batch("shard_import") as rows:
            self._update(rows, "shards", {"shard_id": shard_id},
                         {"state": "imported", "imported_at": now(), "run_store_sha256": run_store_sha256,
                          "ledger_sha256": ledger_sha256, "ledger_rows": ledger_rows})
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def run_counts(self, run_id: str, *, include_synthetic: bool = False) -> dict[str, int]:
        """Outcome counts of a run over every checkpoint outcome of the taxonomy, zeros included (4.13)."""
        counts = dict.fromkeys(self.taxonomy.checkpoint_outcomes, 0)
        sql = "SELECT outcome, count(*) FROM outcomes WHERE run_id = ?" + (
            "" if include_synthetic else " AND synthetic = 0") + " GROUP BY outcome"
        for outcome, n in self._conn.execute(sql, (run_id,)):
            counts[outcome] = n
        return counts

    def missing_outcomes(self, run_id: str) -> list[str]:
        """Scoped checkpoint ids of the run with no non-synthetic outcome row, sorted."""
        return [r[0] for r in self._conn.execute(
            "SELECT s.checkpoint_id FROM run_scope s WHERE s.run_id = ? AND NOT EXISTS (SELECT 1 FROM outcomes o "
            "WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND o.synthetic = 0) "
            "ORDER BY s.checkpoint_id", (run_id,))]

    def complete_blockers(self, run_id: str) -> list[str]:
        """Why the run cannot be `complete` now; empty when it can (8.1, 8.3 run trigger, 8.9)."""
        out = []
        missing = self.missing_outcomes(run_id)
        if missing:
            out.append(f"{len(missing)} scoped checkpoint(s) without an outcome, first {missing[0]}")

        def q(sql: str) -> int:
            return self._one(sql, (run_id,))[0]

        if n := q("SELECT count(*) FROM outcomes WHERE run_id = ? AND synthetic = 0 AND outcome = 'env_error'"):
            out.append(f"{n} env_error outcome(s)")
        if n := q("SELECT count(*) FROM outcomes WHERE run_id = ? AND outcome = 'harness_error' AND reason IN ("
                  + _sql_list(STORE_FAILURE_REASONS) + ")"):
            out.append(f"{n} store-failure harness_error outcome(s)")
        if n := q("SELECT count(*) FROM outcomes WHERE run_id = ? AND synthetic = 0 AND outcome = 'not_comparable' "
                  "AND reason = 'fingerprint_mismatch'"):
            out.append(f"{n} not_comparable (fingerprint_mismatch) outcome(s)")      # 1.10 per run
        if n := q("SELECT count(*) FROM post_run_proofs WHERE run_id = ? AND verdict = 'unproven' "
                  "AND verified_by IS NULL"):
            out.append(f"{n} UNPROVEN post-run proof(s)")                            # 1.10 per run, 6.6
        if n := q("SELECT count(*) FROM post_run_proofs WHERE run_id = ? AND verdict = 'orders_sent'"):
            out.append(f"{n} ORDERS SENT post-run proof(s)")                         # 6.6: aborted_safety
        if n := q("SELECT count(*) FROM shards WHERE run_id = ? AND state <> 'imported'"):
            out.append(f"{n} shard(s) not imported")
        if n := q("SELECT count(*) FROM shards WHERE run_id = ? AND shard_outcome <> 'complete'"):
            out.append(f"{n} shard(s) ended worse than complete")                    # 8.2: worst of the shards
        if n := q("SELECT count(*) FROM outcomes o WHERE o.run_id = ? AND ("
                  + " OR ".join(f"(o.{c} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.{c}))"
                                for c in ("cand_capture_sha256", "base_capture_sha256", "heatmap_sha256")) + ")"):
            out.append(f"{n} outcome(s) naming an artifact the store lacks")
        return out

    def end_run(self, outcome: RunOutcome, *, summary: Mapping[str, Any] | None = None) -> RunOutcome:
        """Write the run outcome (8.2: the worst of the shards, decided by the caller); counts are computed here.

        `complete` is refused with `RunNotComplete` while `complete_blockers` lists anything; any other outcome is
        refused while a scoped checkpoint is silent (write it with `fill_unreached` first, L-OUT-07) or when it is
        less severe than a shard's own outcome (8.2). `summary` is the teardown summary of 8.3 `runs.summary_json`
        (coverage matrix of a PR run, run health, detector state, the report context): its keys join the counts and
        whatever `update_summary` wrote before. `runs.review_state` is computed from the pending reviews.
        """
        r = _revalidate(outcome)
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (r.run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {r.run_id}")
        code, param = self._split_reason(r.status, r.reason)
        if run["lifecycle"] == "finished":
            if (run["outcome"], run["abort_reason"], run["abort_detail"]) == (r.status, code, param):
                return self.run_outcome(r.run_id)
            raise ImportConflict(f"run {r.run_id} already ended {run['outcome']}")
        change = self._end_change(run, r, summary)
        with self._batch("run_end") as rows:
            self._update(rows, "runs", {"run_id": r.run_id}, change)
        return self.run_outcome(r.run_id)

    def _end_change(self, run: sqlite3.Row, r: RunOutcome, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        """The checks of `end_run` and the `runs` columns it sets; reads only, so it runs inside a batch too."""
        code, param = self._split_reason(r.status, r.reason)
        if code is not None and self._forms.get((r.status, code), (0, ":"))[0] and not param:
            raise StoreRefused(f"reason {code} needs its parameter (8.3)")
        if r.status != "complete" and (missing := self.missing_outcomes(r.run_id)):
            # L-OUT-07, 8.2: no checkpoint is left silent; a finished run is final, so fill them first (S-10)
            raise StoreRefused(f"run {r.run_id} cannot end {r.status} with {len(missing)} scoped checkpoint(s) "
                               f"without an outcome, first {missing[0]}; write them (fill_unreached) first (L-OUT-07)")
        worst = max((RUN_SEVERITY.index(s[0]) for s in self._conn.execute(
            "SELECT shard_outcome FROM shards WHERE run_id = ? AND shard_outcome IS NOT NULL", (r.run_id,))),
            default=0)
        if r.status != "complete" and RUN_SEVERITY.index(r.status) < worst:
            raise StoreRefused(f"run {r.run_id} cannot end {r.status}: a shard ended {RUN_SEVERITY[worst]} (8.2: the "
                               "run outcome is the worst of the shards')")
        if r.status == "complete" and (blockers := self.complete_blockers(r.run_id)):
            raise RunNotComplete(f"run {r.run_id} cannot be complete: " + "; ".join(blockers))
        summary: dict[str, Any] = {**json.loads(run["summary_json"] or "{}"), **self._summary_patch(extra or {}),
                                   "counts": self.run_counts(r.run_id), "last_gate": r.last_gate}
        summary.pop("summary", None)
        summary.pop("refs", None)
        if r.summary is not None:
            summary["summary"] = r.summary
        refs = {k: getattr(r, k) for k in RUN_REFS if getattr(r, k) is not None}
        if refs:
            summary["refs"] = refs
        change: dict[str, Any] = {"lifecycle": "finished", "outcome": r.status, "abort_reason": code,
                                  "abort_detail": param, "finished_at": utc(r.ended_at) or now(),
                                  "summary_json": _summary_json(summary), "review_state": self._review_state(r.run_id)}
        for k in ("base_commit_distance", "authorization_sha256"):
            if getattr(r, k) is not None:
                change[k] = getattr(r, k)
        return change

    @staticmethod
    def _summary_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
        reserved = sorted(set(patch) & SUMMARY_RESERVED)
        if reserved:
            raise StoreRefused(f"summary keys {reserved} are the store's own (counts, last_gate, the run outcome's "
                               "summary and refs)")
        try:
            return json.loads(_summary_json(dict(patch)))
        except (TypeError, ValueError) as e:
            raise StoreRefused(f"a run summary is JSON: {e}") from e

    def update_summary(self, run_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Merge later facts into `runs.summary_json` (8.3, 8.7, 9.10): alert acknowledgements, integration state,
        the Nightly metrics. Top-level keys of `patch` replace the stored ones; counts, last_gate, summary and refs
        stay as `end_run` wrote them. Returns the summary as stored."""
        run = self._one("SELECT summary_json FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        summary = {**json.loads(run["summary_json"] or "{}"), **self._summary_patch(patch)}
        with self._batch("run_summary") as rows:
            self._update(rows, "runs", {"run_id": run_id}, {"summary_json": _summary_json(summary)})
        return summary

    def run_summary(self, run_id: str) -> dict[str, Any]:
        """The run's `summary_json` (8.3): what the run page and the PR comment read first (8.7); {} before any."""
        run = self._one("SELECT summary_json FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        return json.loads(run["summary_json"] or "{}")

    def _review_state(self, run_id: str) -> str:
        """8.3 runs.review_state: reviewed with nothing pending; unreviewed while items are pending and no review of
        the run has landed; partial between (9.2, 10.7)."""
        pending = self._one("SELECT count(*) FROM outcomes WHERE run_id = ? AND review_state = 'pending'",
                            (run_id,))[0]
        if not pending:
            return "reviewed"
        landed = self._one("SELECT count(*) FROM outcomes WHERE run_id = ? AND review_state = 'done'", (run_id,))[0]
        return "partial" if landed else "unreviewed"

    def _sync_review_state(self, rows: list[dict], run_id: str) -> None:
        """Recompute `runs.review_state` after a review moved, in the same batch (8.3)."""
        self._update(rows, "runs", {"run_id": run_id}, {"review_state": self._review_state(run_id)})

    def run_outcome(self, run_id: str) -> RunOutcome | None:
        """The finished run as a records.RunOutcome; None while the run is not finished."""
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None or run["outcome"] is None:
            return None
        summary = json.loads(run["summary_json"] or "{}")
        return RunOutcome(run_id=run_id, run_type=run["run_type"], status=run["outcome"],
                          last_gate=summary.get("last_gate", "reported"),
                          reason=self._join_reason(run["outcome"], run["abort_reason"], run["abort_detail"]),
                          counts=summary.get("counts", self.run_counts(run_id)),
                          started_at=run["started_at"], ended_at=run["finished_at"],
                          pair_id=None if run["pair_id"] is None else str(run["pair_id"]),
                          summary=summary.get("summary"), base_commit_distance=run["base_commit_distance"],
                          authorization_sha256=run["authorization_sha256"], **summary.get("refs", {}))

    # -- outcomes, steps, findings, artifacts ----------------------------------------------------------------
    def _outcome_row(self, o: CheckpointOutcome, shard_id: str | None) -> dict:
        code, detail = self._encode_reason(o)
        did = decision_int(o.decision_ref)
        if code == "decision":
            from_reason = decision_int(self._split_reason(o.outcome, o.reason)[1])
            if did is not None and did != from_reason:
                raise StoreRefused("reason decision:<id> and decision_ref name different decisions (4.13)")
            did = from_reason
        arts = o.artifacts or {}
        diff = o.diff
        largest = max(diff.components, key=lambda c: c.area) if diff and diff.components else None
        return {
            "run_id": o.run_id, "checkpoint_id": o.checkpoint_id, "shard_id": shard_id, "side": o.side,
            "gate": o.gate, "outcome": o.outcome, "reason": code, "reason_detail": detail,
            "rescued": int(o.rescued), "review_state": o.review_state,
            "cand_capture_sha256": arts.get("candidate"), "base_capture_sha256": arts.get("base"),
            "heatmap_sha256": arts.get("heatmap"),
            "diff_pct": diff.whole_pixel_fraction * 100 if diff else None, "ssim": diff.whole_ssim if diff else None,
            "shift_dx": diff.shift[0] if diff else None, "shift_dy": diff.shift[1] if diff else None,
            "components": len(diff.components) if diff else None,
            "largest_bbox": ",".join(map(str, largest.bbox)) if largest else None,
            "threshold_source": o.threshold_source, "recaptures": o.recaptures, "stability": o.stability,
            "stability_cause": o.stability_cause, "decision_id": did, "manifest_entry": o.manifest_entry_ref,
            "primitive": o.primitive, "synthetic": int(o.synthetic), "recorded_at": utc(o.recorded_at),
            "outcome_id": o.outcome_id, "profile_id": o.profile_id, "settle_ms": o.settle_ms,
            "cand_rig_id": o.cand_rig_id, "base_rig_id": o.base_rig_id, "diff_pixels": o.diff_pixels,
            "masked_pct": o.masked_pct, "data_epoch_id": o.data_epoch_id,
            "paper_asserted_at": utc((o.paper_assertion or {}).get("asserted_at")),
            "base_source_run_id": o.base_source_run_id, "captured_at": utc(o.captured_at)}

    @staticmethod
    def _step_place(s: StepOutcome, shard_id: str | None, side: str | None) -> tuple[str, str]:
        """The (shard, side) a step is stored under: the record's own, else the call's; a record and a call that
        disagree are refused (8.3 step_events.shard_id and side)."""
        for name, mine, call in (("shard_id", s.shard_id, shard_id), ("side", s.side, side)):
            if mine is not None and call is not None and mine != call:
                raise StoreRefused(f"step {s.step_id} has {name} {mine!r} but was written for {call!r} (8.3)")
        shard = s.shard_id or shard_id
        if shard is None:
            raise StoreRefused("step events need the shard they ran in (8.3 step_events.shard_id)")
        return shard, s.side or side or "candidate"

    def _step_row(self, s: StepOutcome, shard_id: str, side: str, checkpoint_id: str | None) -> dict:
        seq = self._one("SELECT COALESCE(MAX(seq), 0) + 1 FROM step_events WHERE shard_id = ?", (shard_id,))[0]
        agent = s.status == "passed_via_agent_fallback" or s.locator_rung == "agent"
        finding = f"finding:{s.finding_ref}" if s.finding_ref else None
        return {"run_id": s.run_id, "shard_id": shard_id, "side": side, "seq": seq, "source": "orchestrator",
                "flow_id": s.flow_id, "step_id": s.step_id, "checkpoint_id": checkpoint_id,
                "wall_at": utc(s.recorded_at), "mono_ms": time.monotonic_ns() // 1_000_000,
                "mode": s.mode or ("agent" if agent else "scripted"), "action": "step",
                "locator_level": s.locator_rung if s.locator_rung in LOCATOR_LEVELS else None,
                "attempt": s.attempt, "step_outcome": s.status, **_gateway_columns(s.gateway_event),
                "reason": ";".join(filter(None, (s.reason, finding))) or None, "duration_ms": s.duration_ms,
                "pre_hash": s.pre_screenshot, "post_hash": s.post_screenshot, "click_crop_sha256": s.click_crop,
                "app_uptime_s": s.app_uptime_s, "app_restart_count": s.app_restart_count}

    def _finding_row(self, f: Finding, synthetic: bool) -> dict:
        return {"natural_key": f.finding_id, "run_id": f.run_id, "checkpoint_id": f.checkpoint_id, "kind": f.kind,
                "tier": f.tier, "region_id": f.region_id, "primitive": f.primitive,
                "manifest_entry": f.manifest_entry_ref, "phash": phash_bytes(f.phash) if f.phash else None,
                "category": f.category, "description": f.description,
                "evidence_json": json.dumps(f.evidence, sort_keys=True), "decision_id": decision_int(f.decision_ref),
                "ticket_ref": f.ticket, "triage_state": f.triage_state, "synthetic": int(synthetic),
                "first_seen_run_id": f.first_seen_run_id, "created_at": utc(f.created_at),
                "severity": f.severity, "side": f.side, "step_id": f.step_id, "detail": f.detail,
                "pre_existing": int(f.pre_existing), "cluster_id": f.cluster_id, "contested": int(f.contested),
                "held": int(f.held), "closed_at": utc(f.closed_at),
                "explorer_json": None if f.explorer is None else _json(f.explorer)}

    def _put_finding(self, rows: list[dict], f: Finding, synthetic: bool) -> None:
        row = self._finding_row(f, synthetic)
        old = self._one("SELECT * FROM findings WHERE natural_key = ?", (f.finding_id,))
        if old is None:
            self._insert(rows, "findings", row)
            return
        later = {"decision_id", "ticket_ref", "triage_state",          # 4.14: decisions move these
                 "closed_at", "contested", "held"}                       # 8.6, 9.7, 2.9: triage moves these
        if any(old[k] != v for k, v in row.items() if k not in later):
            raise ImportConflict(f"finding {f.finding_id} is already recorded with other content (8.2)")

    def _check_decision_reason(self, o: CheckpointOutcome, code: str | None) -> None:
        """4.13 required fields: `decision:<id>` names an active (or reopened) decision whose verdict is the one the
        outcome claims (noise, intended, or confirmed_bug for a known-bug collateral); a decision_ref exists."""
        did = None
        if code == "decision":
            did = self._split_reason(o.outcome, o.reason)[1]
            want = DECISION_REASON_VERDICT.get(o.outcome)
            row = self._one("SELECT status, verdict FROM decisions WHERE decision_id = ?", (decision_int(did),))
            if row is None or row["status"] not in ("active", "reopened") or row["verdict"] != want:
                raise StoreRefused(f"{o.outcome} (reason: decision:{did}) needs an active decision {did} with verdict "
                                   f"{want} (4.13)")
        if o.decision_ref and o.decision_ref != did and self._one(
                "SELECT 1 FROM decisions WHERE decision_id = ?", (decision_int(o.decision_ref),)) is None:
            raise StoreRefused(f"{o.checkpoint_id} names unknown decision {o.decision_ref} (4.13 decision_ref)")

    def _verify_artifact(self, a: StoredArtifact) -> None:
        """8.2, 8.3 artifacts: a row exists only for verified bytes on disk, and an image only with the redaction
        version of its passing redaction header (8.4, 8.9). A hash already indexed was verified when it was."""
        if self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (a.sha256,)) is not None:
            return
        try:
            data = Path(a.path).read_bytes()
        except OSError as e:
            raise StoreRefused(f"artifact {a.sha256} is not on disk at {a.path} (8.2)") from e
        if hashlib.sha256(data).hexdigest() != a.sha256:
            raise StoreRefused(f"{a.path} does not hash to {a.sha256} (8.2)")
        if is_visual(data, a.kind, a.media_type) and not a.redaction_version:
            raise StoreRefused(f"image {a.sha256} has no redaction version (8.4, 8.9 redaction_version_missing)")

    def _put_artifact(self, rows: list[dict], a: StoredArtifact, run_id: str) -> None:
        if self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (a.sha256,)) is not None:
            return                                   # 8.4: a hash already indexed; the first set keeps the row
        self._insert(rows, "artifacts", {
            "sha256": a.sha256, "kind": a.kind, "media_type": a.media_type, "bytes": a.bytes, "width": a.width,
            "height": a.height, "redaction_passed": 1, "redaction_version": a.redaction_version,
            "set_path": a.set_path, "first_run_id": run_id, "created_at": now()})

    def _ensure_checkpoint(self, rows: list[dict], o: CheckpointOutcome) -> None:
        if self._one("SELECT 1 FROM checkpoints WHERE checkpoint_id = ?", (o.checkpoint_id,)) is not None:
            return
        if o.tier is None or o.test_kind == "flow":
            raise StoreRefused(f"{o.checkpoint_id} is not registered; register it (tier, flow_class) first (8.3)")
        commit = self._one("SELECT config_commit FROM runs WHERE run_id = ?", (o.run_id,))[0]
        self._upsert_checkpoint(rows, ScopeEntry(o.checkpoint_id, o.workspace_id, o.persona, o.tier, o.data_mode),
                                commit)

    def _prepare_checkpoint(self, outcome: CheckpointOutcome, steps: Sequence[StepOutcome],
                            findings: Sequence[Finding], artifacts: Sequence[StoredArtifact], shard_id: str | None,
                            step_side: str | None) -> _Prepared | None:
        """Every write-time check of one checkpoint batch; None for a replay of the record already written. Reads
        only, so it also runs inside a batch (`replace_dead_man_run`)."""
        o = _revalidate(outcome)
        placed = [(s, *self._step_place(s, shard_id, step_side)) for s in map(_revalidate, steps)]
        findings = [_revalidate(f) for f in findings]
        self._running(o.run_id)
        record = _json(o)
        old = self._one("SELECT record_json FROM outcome_records WHERE run_id = ? AND checkpoint_id = ?",
                        (o.run_id, o.checkpoint_id))
        if old is not None:
            if old[0] == record:
                return None
            raise ImportConflict(f"{o.checkpoint_id} in {o.run_id} already has an outcome; outcomes are written "
                                 "once (4.13, 8.2)")
        self._check_paper(o, shard_id)                              # L-OUT-06 before anything repairable
        if self.strict_records and (errs := self.record_errors(o)):
            raise RecordIncomplete(errs)                            # L-OUT-02
        code = self._split_reason(o.outcome, o.reason)[0]
        if o.review_state == "pending" and code not in PENDING_REASONS:
            raise StoreRefused("review_state pending only with reason reviewer_unavailable or global_shift (4.13)")
        self._check_decision_reason(o, code)
        if o.base_source_run_id is not None and self._one("SELECT 1 FROM runs WHERE run_id = ?",
                                                          (o.base_source_run_id,)) is None:
            raise StoreRefused(f"base_source_run_id {o.base_source_run_id} is not a run of this store (8.3)")
        for a in artifacts:
            self._verify_artifact(a)
        named = [h for h in (o.artifacts or {}).get("crops", [])] + [
            (o.artifacts or {}).get(k) for k in ("candidate", "base", "heatmap")]
        batch_hashes = {a.sha256 for a in artifacts}
        for h in filter(None, named):
            if h not in batch_hashes and self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (h,)) is None:
                raise StoreRefused(f"{o.checkpoint_id} names artifact {h}, which was not stored (8.2)")
        return _Prepared(o, record, tuple(placed), tuple(findings), tuple(artifacts), shard_id)

    def _put_checkpoint(self, rows: list[dict], p: _Prepared) -> None:
        o = p.outcome
        self._ensure_checkpoint(rows, o)
        for a in p.artifacts:
            self._put_artifact(rows, a, o.run_id)
        self._insert(rows, "outcomes", self._outcome_row(o, p.shard_id))
        self._insert(rows, "outcome_records", {"run_id": o.run_id, "checkpoint_id": o.checkpoint_id,
                                               "record_json": p.record})
        for s, shard, side in p.steps:
            self._insert(rows, "step_events", self._step_row(s, shard, side, o.checkpoint_id))
        for f in p.findings:
            self._put_finding(rows, f, o.synthetic)

    def write_checkpoint(self, outcome: CheckpointOutcome, *, steps: Sequence[StepOutcome] = (),
                         findings: Sequence[Finding] = (), artifacts: Sequence[StoredArtifact] = (),
                         shard_id: str | None = None, step_side: str | None = None) -> None:
        """Write one checkpoint's batch (8.2): its artifact rows, its outcome, its step events and its findings.

        Refuses: anything outside the taxonomy (TaxonomyError); a finished or unknown run; a record without the
        session's paper-mode stamp once `set_session_context` names the shard (PaperAssertionMissing, L-OUT-06); on
        a `strict_records` store, a record the 4.13 required-fields table refuses (RecordIncomplete, L-OUT-02);
        `review_state: pending` without reason reviewer_unavailable or global_shift (4.13); an artifact the batch
        and the store lack (8.2); a different record for an outcome already written (ImportConflict); a step whose
        own shard or side disagrees with the call's; any 8.3 constraint. Writing the same record again is a no-op
        (replay is idempotent, 8.2). A step is stored under its own shard and side, else the call's (default side
        candidate).
        """
        p = self._prepare_checkpoint(outcome, steps, findings, artifacts, shard_id, step_side)
        if p is None:
            return
        with self._batch("checkpoint") as rows:
            self._put_checkpoint(rows, p)

    def write_checkpoint_or_repair(self, outcome: CheckpointOutcome, *,
                                   repair: Callable[[CheckpointOutcome, list[str]], CheckpointOutcome],
                                   on_refused: Callable[[CheckpointOutcome, list[str]], str],
                                   **kw: Any) -> CheckpointOutcome:
        """The Orchestrator's write under L-OUT-02 (4.13): a record the required-fields table refuses gets one
        `repair(record, errors)`; if the repaired record is still refused, `on_refused(record, errors)` logs the
        Watchdog event and returns its id, and the checkpoint is written `harness_error (reason:
        store_write_failed)` with that id in `detail`. `kw` goes to `write_checkpoint`. Returns the record written.
        L-OUT-06 and every other refusal propagate: they are not repairable."""
        o = outcome
        if errs := self.record_errors(o):
            o = repair(o, errs)
            if errs := self.record_errors(o):
                event = on_refused(o, errs)
                o = CheckpointOutcome(
                    run_id=o.run_id, checkpoint_id=o.checkpoint_id, outcome="harness_error",
                    reason="store_write_failed", detail=f"watchdog_event: {event}; refused: {'; '.join(errs)}",
                    workspace_id=o.workspace_id, persona=o.persona, data_mode=o.data_mode, surface_id=o.surface_id,
                    test_kind=o.test_kind, tier=o.tier, flow_id=o.flow_id, step_id=o.step_id, size_step=o.size_step,
                    flow_class=o.flow_class, profile_id=o.profile_id, outcome_id=o.outcome_id, pair_id=o.pair_id,
                    synthetic=o.synthetic, mutant_id=o.mutant_id, recorded_at=o.recorded_at)
        self.write_checkpoint(o, **kw)
        return o

    def write_steps(self, steps: Sequence[StepOutcome], *, shard_id: str | None = None, side: str | None = None,
                    checkpoint_id: str | None = None) -> None:
        """Write step outcomes as `step_events` rows outside a checkpoint batch (8.3), each under its own shard and
        side, else the call's; a disagreement is refused."""
        placed = [(s, *self._step_place(s, shard_id, side)) for s in map(_revalidate, steps)]
        for s, _, _ in placed:
            self._running(s.run_id)
        with self._batch("steps") as rows:
            for s, shard, sd in placed:
                self._insert(rows, "step_events", self._step_row(s, shard, sd, checkpoint_id))

    def write_finding(self, finding: Finding, *, synthetic: bool = False) -> None:
        """Write a finding outside a checkpoint batch, e.g. a run-level finding (4.13)."""
        f = _revalidate(finding)
        self._running(f.run_id)
        with self._batch("finding") as rows:
            self._put_finding(rows, f, synthetic)

    def fill_unreached(self, run_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None,
                       detail: str | None = None) -> list[str]:
        """Write `outcome` for every scoped checkpoint that has none, so no checkpoint is left silent.

        1.10: unreached checkpoints of an aborted run carry its abort reason (env_error with the aborting gate,
        or harness_error); 8.2 and 8.9 use the same for orchestrator_restart_budget, store_write_failed, disk_full.
        """
        run = self._running(run_id)
        filled = []
        for cid in self.missing_outcomes(run_id):
            c = self._one("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (cid,))
            persona = self._one("SELECT persona FROM run_scope WHERE run_id = ? AND checkpoint_id = ?",
                                (run_id, cid))[0]
            self.write_checkpoint(CheckpointOutcome(
                run_id=run["run_id"], checkpoint_id=cid, outcome=outcome, reason=reason, detail=detail, gate=gate,
                workspace_id=c["workspace_id"], persona=persona, data_mode=c["data_mode"], tier=c["tier"],
                flow_id=c["flow_id"], step_id=c["step_id"]))
            filled.append(cid)
        return filled

    _KEEP_TRIGGERS = ("trg_outcome_keep", "trg_outcome_record_keep")

    def replace_dead_man_run(self, run_id: str, rows: Sequence[CheckpointOutcome], *, outcome: RunOutcome,
                             artifacts: Sequence[StoredArtifact] = (), findings: Sequence[Finding] = (),
                             arrived_at: str | None = None) -> RunOutcome:
        """10.2 crash during teardown, late arrival: the run the control host ended `aborted_harness (reason:
        dead_man)` at the deadline takes its real rows and outcome when they arrive.

        In one batch: the `harness_error (reason: dead_man)` placeholders of the checkpoints `rows` cover move to
        `outcomes_superseded` (kept for diagnosis, never counted, 8.3); `rows`, their `artifacts` and `findings`
        are written with every write-time check; the run is ended `outcome` with the late arrival in its summary
        (`late_arrival` with the deadline, the arrival and the delay in seconds, also under `health`, and the
        dead-man alert closed as "late arrival" in `alerts`). Refused for a run that did not end dead_man, and with
        everything rolled back when any row or the outcome is refused. The caller ends and imports the shards first.
        """
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        if (run["outcome"], run["abort_reason"]) != ("aborted_harness", "dead_man"):
            raise StoreRefused(f"run {run_id} ended {run['outcome']}, not aborted_harness (reason: dead_man): only a "
                               "dead-man placeholder is replaced by late rows (10.2)")
        r = _revalidate(outcome)
        if r.run_id != run_id or (r.status, self._split_reason(r.status, r.reason)[0]) == ("aborted_harness",
                                                                                           "dead_man"):
            raise StoreRefused("the late arrival carries the run's real outcome (10.2)")
        if not rows or any(o.run_id != run_id for o in rows):
            raise StoreRefused(f"a late arrival carries rows of run {run_id}, and only of it (10.2)")
        at = utc(arrived_at) or now()
        delay = int((datetime.fromisoformat(at) - datetime.fromisoformat(run["finished_at"])).total_seconds())
        old = json.loads(run["summary_json"] or "{}")
        late = {"dead_man_at": run["finished_at"], "arrived_at": at, "delay_s": delay}
        extra = {"late_arrival": late, "health": {**old.get("health", {}), "late_arrival": late},
                 "alerts": [*old.get("alerts", []), {"alert": "dead_man", "closed_as": "late arrival", "at": at}]}
        ids = [o.checkpoint_id for o in rows]
        marks = ",".join("?" * len(ids))
        with self._batch("late_arrival") as batch:
            placeholders = [c for (c,) in self._conn.execute(
                f"SELECT checkpoint_id FROM outcomes WHERE run_id = ? AND checkpoint_id IN ({marks}) AND synthetic = 0 "
                "AND outcome = 'harness_error' AND reason = 'dead_man'", (run_id, *ids))]
            if placeholders:
                pm = ",".join("?" * len(placeholders))
                self._conn.execute(f"INSERT INTO outcomes_superseded SELECT o.*, 1 FROM outcomes o WHERE o.run_id = ? "
                                   f"AND o.checkpoint_id IN ({pm})", (run_id, *placeholders))
                keep = self._conn.execute(f"SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN "
                                          f"({_sql_list(self._KEEP_TRIGGERS)})").fetchall()
                for name, _ in keep:
                    self._conn.execute(f"DROP TRIGGER {name}")
                for table in ("outcome_records", "outcomes"):
                    self._conn.execute(f"DELETE FROM {table} WHERE run_id = ? AND checkpoint_id IN ({pm})",
                                       (run_id, *placeholders))
                for _, sql in keep:                 # restored as they were, in the same transaction
                    self._conn.execute(sql)
                batch.append({"op": "supersede", "table": "outcomes", "key": {"run_id": run_id},
                              "checkpoints": placeholders, "cause": "late arrival (10.2)"})
            self._update(batch, "runs", {"run_id": run_id}, {"lifecycle": "running", "outcome": None,
                                                             "abort_reason": None, "abort_detail": None,
                                                             "finished_at": None})
            for a in artifacts:
                self._verify_artifact(a)
                self._put_artifact(batch, a, run_id)
            for o in rows:
                p = self._prepare_checkpoint(o, (), (), (), None, None)
                if p is not None:
                    self._put_checkpoint(batch, p)
            for f in findings:
                self._put_finding(batch, _revalidate(f), False)
            reopened = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            self._update(batch, "runs", {"run_id": run_id}, self._end_change(reopened, r, extra))
        return self.run_outcome(run_id)

    # -- canary results (8.3 canary_results, 10.3) ---------------------------------------------------------
    _CANARY_KEY = ("run_id", "kind", "mutant_id", "checkpoint_id")
    _CANARY_FIELDS = ("kind", "mutant_id", "catalog_version", "checkpoint_id", "defect_class", "money_cell", "seed",
                      "base_sha256", "mutant_sha256", "produced_outcome", "produced_reason", "state", "absorbed_by",
                      "class_rate", "class_floor", "reviewer_verdict", "reviewer_confidence", "mask_version",
                      "threshold_version", "model_id", "prompt_hash")

    def write_canary_results(self, run_id: str, results: Iterable[CanaryResult], *,
                             created_at: str | None = None) -> None:
        """Write a run's per-mutant canary rows in one batch (8.3 `canary_results`, 10.3 Scoring).

        Each row is re-validated (`CanaryResult` enforces the 8.3 CHECKs; the generated schema refuses the rest).
        Refuses an unknown or finished run. The same row again is a no-op; a different row for the same
        (kind, mutant_id, checkpoint_id) is an ImportConflict (module docstring).
        """
        rows_in = [dataclasses.replace(r) for r in results]
        self._running(run_id)
        at = utc(created_at) or now()
        where = " AND ".join(f"{k} = ?" for k in self._CANARY_KEY)
        with self._batch("canary_results") as rows:
            for r in rows_in:
                values = {"run_id": run_id, **{f: getattr(r, f) for f in self._CANARY_FIELDS},
                          "money_cell": int(r.money_cell), "expected_json": json.dumps(list(r.expected))}
                old = self._one(f"SELECT * FROM canary_results WHERE {where}", [values[k] for k in self._CANARY_KEY])
                if old is not None:
                    if {k: old[k] for k in values} == values:
                        continue
                    raise ImportConflict(f"{r.mutant_id} on {r.checkpoint_id} in {run_id} already has a canary "
                                         "result; rows are written once (8.2)")
                self._insert(rows, "canary_results", {**values, "created_at": at})

    def canary_results(self, run_id: str) -> list[CanaryResult]:
        """The run's canary rows in the order they were written (8.3 `canary_results`)."""
        from ..canary import CanaryResult
        out = []
        for r in self._conn.execute("SELECT * FROM canary_results WHERE run_id = ? ORDER BY canary_id", (run_id,)):
            d = {f: r[f] for f in self._CANARY_FIELDS}
            out.append(CanaryResult(**{**d, "money_cell": bool(r["money_cell"]),
                                       "expected": json.loads(r["expected_json"])}))
        return out

    # -- post-run proofs -------------------------------------------------------------------------------------
    _PROOF_COLUMNS = ("shard_id", "run_id", "login_alias", "verdict", "reason", "send_policy_ref", "cancel_all",
                      "paper_mode_assertions", "paper_mode_mismatches", "gateway_events", "gateway_drops",
                      "start_snapshot_sha256", "end_snapshot_sha256", "produced_at", "verified_by", "verified_at",
                      "unreadable_count", "refusals_count", "attestation_ref")

    def write_proof(self, proof: PostRunProof) -> None:
        """Write a shard's post-run proof (8.3 `post_run_proofs`, 6.6): one per shard, written once, before the run
        ends. Refuses a shard not of the run, an unregistered login and a finished run; the same row again is a
        no-op, a different row for the shard an ImportConflict (8.2)."""
        p = dataclasses.replace(proof)                  # re-validates
        self._running(p.run_id)
        if p.shard_id is not None:
            shard = self._one("SELECT run_id FROM shards WHERE shard_id = ?", (p.shard_id,))
            if shard is None or shard["run_id"] != p.run_id:
                raise StoreRefused(f"{p.shard_id} is not a shard of run {p.run_id} (8.3)")
            old = self._one("SELECT * FROM post_run_proofs WHERE shard_id = ?", (p.shard_id,))
        else:
            old = self._one("SELECT * FROM post_run_proofs WHERE run_id = ? AND shard_id IS NULL", (p.run_id,))
        values = {c: getattr(p, c) for c in self._PROOF_COLUMNS}
        values.update(counts_json=_json(p.counts), produced_at=utc(p.produced_at), verified_at=utc(p.verified_at))
        if old is not None:
            if {k: old[k] for k in values} == values:
                return
            raise ImportConflict(f"{p.shard_id or p.run_id} already has a post-run proof; one per shard (or one "
                                 "run-level row), written once (8.2)")
        with self._batch("post_run_proof") as rows:
            self._insert(rows, "post_run_proofs", values)

    def post_run_proofs(self, run_id: str) -> list[PostRunProof]:
        """The run's post-run proofs, one per shard, in shard order (8.3 `post_run_proofs`)."""
        return [PostRunProof(**{c: r[c] for c in self._PROOF_COLUMNS}, counts=json.loads(r["counts_json"]))
                for r in self._conn.execute("SELECT * FROM post_run_proofs WHERE run_id = ? ORDER BY shard_id",
                                            (run_id,))]

    # -- reading outcomes, steps, findings ------------------------------------------------------------------
    def _outcome(self, row: sqlite3.Row, record_json: str) -> CheckpointOutcome:
        d = json.loads(record_json)
        rec = _outcome_from_dict(d)
        code, enc = self._encode_reason(rec)
        if (rec.outcome, code, enc) != (row["outcome"], row["reason"], row["reason_detail"]):
            d["outcome"] = row["outcome"]
            d["reason"], d["detail"] = self._decode_reason(row["outcome"], row["reason"], row["reason_detail"])
            for k in ("reason", "detail"):
                if d[k] is None:
                    del d[k]
        d["review_state"] = row["review_state"]
        if decision_int(rec.decision_ref) != row["decision_id"]:
            d["decision_ref"] = decision_ref(row["decision_id"])
        return _outcome_from_dict(d)

    def get_outcome(self, run_id: str, checkpoint_id: str) -> CheckpointOutcome | None:
        row = self._one("SELECT o.*, r.record_json FROM outcomes o JOIN outcome_records r USING (run_id, "
                        "checkpoint_id) WHERE o.run_id = ? AND o.checkpoint_id = ?", (run_id, checkpoint_id))
        return None if row is None else self._outcome(row, row["record_json"])

    def outcomes(self, run_id: str) -> list[CheckpointOutcome]:
        rows = self._conn.execute("SELECT o.*, r.record_json FROM outcomes o JOIN outcome_records r USING (run_id, "
                                  "checkpoint_id) WHERE o.run_id = ? ORDER BY o.checkpoint_id", (run_id,))
        return [self._outcome(r, r["record_json"]) for r in rows]

    def step_outcomes(self, run_id: str) -> list[StepOutcome]:
        """The run's step outcomes in shard and seq order, with the shard, side, mode and screenshots they were
        stored under (8.3 step_events)."""
        rows = self._conn.execute("SELECT * FROM step_events WHERE run_id = ? AND step_outcome IS NOT NULL "
                                  "ORDER BY shard_id, seq", (run_id,))
        out = []
        for r in rows:
            reason, _, finding = (r["reason"] or "").partition("finding:")
            out.append(StepOutcome(
                run_id=r["run_id"], flow_id=r["flow_id"], step_id=r["step_id"], status=r["step_outcome"],
                attempt=r["attempt"], locator_rung=r["locator_level"], duration_ms=r["duration_ms"] or 0,
                finding_ref=finding or None, side=r["side"], shard_id=r["shard_id"], pre_screenshot=r["pre_hash"],
                gateway_event=_gateway_event(r), recorded_at=r["wall_at"], reason=reason.rstrip(";") or None,
                mode=r["mode"], app_uptime_s=r["app_uptime_s"], app_restart_count=r["app_restart_count"],
                post_screenshot=r["post_hash"], click_crop=r["click_crop_sha256"]))
        return out

    def gateway_drops(self, run_id: str) -> int:
        """Gateway drops in a run: `step_events` rows the hook dropped or the slow half refused (9.11, 8.3)."""
        return self._one("SELECT count(*) FROM step_events WHERE run_id = ? AND gate_decision IN ('dropped','refused')",
                         (run_id,))[0]

    def _finding(self, r: sqlite3.Row) -> Finding:
        return Finding(finding_id=r["natural_key"], run_id=r["run_id"], kind=r["kind"], description=r["description"],
                       first_seen_run_id=r["first_seen_run_id"], checkpoint_id=r["checkpoint_id"], tier=r["tier"],
                       region_id=r["region_id"], primitive=r["primitive"], manifest_entry_ref=r["manifest_entry"],
                       phash=bytes(r["phash"]).hex() if r["phash"] is not None else None, category=r["category"],
                       evidence=json.loads(r["evidence_json"]), triage_state=r["triage_state"],
                       decision_ref=decision_ref(r["decision_id"]), ticket=r["ticket_ref"],
                       created_at=r["created_at"], severity=r["severity"], side=r["side"], step_id=r["step_id"],
                       detail=r["detail"], pre_existing=bool(r["pre_existing"]), cluster_id=r["cluster_id"],
                       contested=bool(r["contested"]), held=bool(r["held"]), closed_at=r["closed_at"],
                       explorer=(ExplorerFinding(**json.loads(r["explorer_json"])) if r["explorer_json"] else None))

    def findings(self, run_id: str) -> list[Finding]:
        return [self._finding(r) for r in self._conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY natural_key", (run_id,))]

    def get_finding(self, finding_id: str) -> Finding | None:
        r = self._one("SELECT * FROM findings WHERE natural_key = ?", (finding_id,))
        return None if r is None else self._finding(r)

    # -- the three later changes of 4.13 --------------------------------------------------------------------
    def _outcome_row_for(self, run_id: str, checkpoint_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM outcomes WHERE run_id = ? AND checkpoint_id = ?", (run_id, checkpoint_id))
        if row is None:
            raise StoreRefused(f"no outcome for {checkpoint_id} in {run_id}")
        return row

    def set_decision_ref(self, run_id: str, checkpoint_id: str, decision_id: str) -> CheckpointOutcome:
        """First later change (4.13): link the decision that suppresses this row's finding. Set once."""
        row = self._outcome_row_for(run_id, checkpoint_id)
        did = decision_int(decision_id)
        if self._one("SELECT 1 FROM decisions WHERE decision_id = ?", (did,)) is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        if row["decision_id"] != did:
            if row["decision_id"] is not None:
                raise StoreRefused(f"decision_ref is already {decision_ref(row['decision_id'])} (4.13: set once)")
            with self._batch("decision_ref") as rows:
                self._update(rows, "outcomes", {"run_id": run_id, "checkpoint_id": checkpoint_id},
                             {"decision_id": did})
        return self.get_outcome(run_id, checkpoint_id)

    def _pending(self, run_id: str, checkpoint_id: str) -> sqlite3.Row:
        row = self._outcome_row_for(run_id, checkpoint_id)
        if row["outcome"] != "needs_human" or row["review_state"] != "pending":
            raise StoreRefused(f"{checkpoint_id} in {run_id} is not a pending review; outcomes are final (4.13)")
        return row

    def human_decision_on(self, run_id: str, checkpoint_id: str) -> bool:
        """True when a human decision already exists on the item (8.3, F28): inherited by the row, made on a
        finding of the item, or made on this run and checkpoint; revoked and superseded rows do not count."""
        row = self._outcome_row_for(run_id, checkpoint_id)
        if row["decision_id"] is not None:
            return True
        return self._one(
            "SELECT 1 FROM decisions d LEFT JOIN findings f ON f.finding_id = d.source_finding_id "
            "WHERE d.status IN ('active','reopened') AND d.revoked_at IS NULL AND ("
            "(f.run_id = :run AND f.checkpoint_id = :cp) OR (d.run_id = :run AND d.checkpoint_id = :cp))",
            {"run": run_id, "cp": checkpoint_id}) is not None

    _REVIEW_KEY = ("run_id", "checkpoint_id", "diff_hash", "model_id", "prompt_hash")     # 8.3 reviews UNIQUE

    def apply_review(self, review: Review, *, outcome: str | None = None, reason: str | None = None
                     ) -> CheckpointOutcome:
        """Second later change (4.13, 8.3, F28, SD-23): a pending verdict lands.

        The `reviews` row is stored. If a human decision already exists on the item the verdict is an annotation
        only: `review_state` goes to done, the outcome and reason stay. Otherwise the row takes the outcome 3.11
        gives (default: the review's mapped outcome), which for a pending item is only `intended` (no reason) or
        `needs_human` with reviewer_no_match or reviewer_uncertain: a late verdict never produces `collateral` or
        `noise` and is refused if it tries (SD-23); review_state done with the review id. The same review again
        (the 8.3 reviews key: run, checkpoint, diff hash, model, prompt) is a no-op returning the row; a different
        review under that key is an ImportConflict. `runs.review_state` follows (8.3).
        """
        if not 0.0 <= review.confidence <= 1.0:
            raise StoreRefused("review confidence is in [0, 1] (3.13)")
        values = dataclasses.asdict(review)
        values["created_at"] = utc(review.created_at)
        old = self._one("SELECT * FROM reviews WHERE " + " AND ".join(f"{k} = ?" for k in self._REVIEW_KEY),
                        [values[k] for k in self._REVIEW_KEY])
        if old is not None:
            if any(old[k] != v for k, v in values.items() if k != "created_at"):
                raise ImportConflict(f"{review.checkpoint_id} in {review.run_id} already has a different review under "
                                     "the same key (8.3 reviews UNIQUE)")
            return self.get_outcome(review.run_id, review.checkpoint_id)
        row = self._pending(review.run_id, review.checkpoint_id)
        human = self.human_decision_on(review.run_id, review.checkpoint_id)
        new = outcome or review.mapped_outcome
        if not human:
            allowed = {"intended": (None,), "needs_human": ("reviewer_no_match", "reviewer_uncertain")}
            if new not in allowed or reason not in allowed[new]:
                raise StoreRefused(f"a late verdict cannot end in {new} (reason {reason}); a pending item becomes "
                                   "intended or needs_human (reviewer_no_match, reviewer_uncertain) only (4.13, SD-23)")
            self.taxonomy.check_checkpoint(new, reason)
        key = {"run_id": row["run_id"], "checkpoint_id": row["checkpoint_id"]}
        with self._batch("review") as rows:
            review_id = self._insert(rows, "reviews", values)
            change = {"review_state": "done", "review_id": review_id}
            if not human:
                change = {"outcome": new, "reason": reason, "reason_detail": None, **change}
            self._update(rows, "outcomes", key, change)
            self._sync_review_state(rows, review.run_id)
        return self.get_outcome(review.run_id, review.checkpoint_id)

    def end_review(self, run_id: str, checkpoint_id: str, reason: str) -> CheckpointOutcome:
        """Third later change (4.13): a pending item that never got a verdict ends its review.

        `reviewer_cap` when the per-run spend ceiling ended the run (`cause: dollar_cap` in reason_detail);
        `reviewer_unavailable` when the Reviewer job gave up. Outcome stays needs_human; no review id.
        `runs.review_state` follows (8.3).
        """
        row = self._pending(run_id, checkpoint_id)
        if reason not in ("reviewer_cap", "reviewer_unavailable"):
            raise StoreRefused("a review ends without a verdict only as reviewer_cap or reviewer_unavailable (4.13)")
        detail = "cause: dollar_cap" if reason == "reviewer_cap" else (
            row["reason_detail"] if row["reason"] == reason else None)
        with self._batch("review_end") as rows:
            self._update(rows, "outcomes", {"run_id": run_id, "checkpoint_id": checkpoint_id},
                         {"reason": reason, "reason_detail": detail, "review_state": "done"})
            self._sync_review_state(rows, run_id)
        return self.get_outcome(run_id, checkpoint_id)

    # -- decisions (4.14) -------------------------------------------------------------------------------------
    @staticmethod
    def _human(identity: str | None, role: str) -> None:
        if identity is not None and identity.strip().lower() in SERVICE_IDENTITIES:
            raise StoreRefused(f"{role} {identity!r} is not a person: decisions are human-only (4.14, L-DEC-01)")

    def _approvable(self, run_id: str, d: Decision, tier: int) -> None:
        """L-DEC-05: approve_baseline never from a PR run, a run not `complete`, a run with a synthetic outcome on
        the checkpoint (the 8.3 trg_baseline_approve form), or by the change's author on a tier-1 checkpoint."""
        run = self._one("SELECT run_type, outcome, pr_author FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        if run["run_type"] == "pr":
            raise StoreRefused("approve_baseline is refused from a PR run; promotion happens at merge-run triage "
                               "(L-DEC-05, 9.8)")
        if run["outcome"] != "complete":
            raise StoreRefused(f"approve_baseline needs a complete run; {run_id} is "
                               f"{run['outcome'] or 'not finished'} (L-DEC-05)")
        if self._one("SELECT 1 FROM outcomes WHERE run_id = ? AND checkpoint_id = ? AND synthetic = 1",
                     (run_id, d.signature.checkpoint_id)) is not None:
            raise StoreRefused("approve_baseline is refused from a synthetic outcome (L-DEC-05)")
        if tier == 1 and run["pr_author"] is not None and d.decided_by == run["pr_author"]:
            raise StoreRefused("the author of the change never approves a tier-1 baseline (L-DEC-05, 9.6)")

    def _newer_build(self, fixed: str, base: str | None) -> None:
        """L-DEC-08: fixed_in_build names a build newer (by builds.first_seen_at) than the decision's build_id,
        or the build_id of the confirm_bug row it supersedes."""
        if base is None:
            raise StoreRefused("fixed_in_build needs the build_id it is newer than (L-DEC-08)")
        seen = {r["build_id"]: r["first_seen_at"] for r in self._conn.execute(
            "SELECT build_id, first_seen_at FROM builds WHERE build_id IN (?, ?)", (fixed, base))}
        if fixed == base or fixed not in seen or base not in seen or seen[fixed] <= seen[base]:
            raise StoreRefused(f"fixed_in_build {fixed} is not a build newer than {base} (L-DEC-08)")

    @staticmethod
    def _same(name: str, call: Any, record: Any) -> Any:
        """A 4.14 record field given both on the Decision and as a keyword: they must agree (one value wins)."""
        if call is not None and record is not None and call != record:
            raise StoreRefused(f"{name} {call!r} given to record_decision and {record!r} on the decision differ (4.14)")
        return call if call is not None else record

    def record_decision(self, decision: Decision, *, run_id: str, finding_id: str | None = None,
                        build_id: str | None = None, expires_at: str | None = None,
                        confirmed_by: str | None = None, proposed_by: str | None = None,
                        fixed_in_build_id: str | None = None, baseline_id: int | None = None,
                        mask_ref: str | None = None, cluster_id: str | None = None,
                        supersedes: str | None = None, manifest_entries: Iterable[str] = (),
                        manifest_tickets: Iterable[str] = ()) -> Decision:
        """Write a human decision (4.14, 8.3) and return it as stored (canonical id, effective tolerance, UTC).

        `finding_id`, `expires_at`, `confirmed_by` and `supersedes` default to the Decision's `finding_ref`,
        `expires_at`, `confirmed_by` and `supersedes`; a keyword and a record field that disagree are refused. The
        other keywords are 4.14 record fields records.Decision does not carry. With a finding the finding takes the
        decision and the triage state of the 4.14 table. With `supersedes` the old decision is marked superseded in
        the same batch (8.3); a decision is never edited otherwise.

        Refused besides the 8.3 constraints: a service identity (L-DEC-01); an explicit `hamming_tolerance` above 6
        on a tier-1 checkpoint (L-DEC-03); a waiver without an expiry at most 30 days out (L-DEC-07); the L-DEC-05
        and L-DEC-08 cases; any decision on a frozen signature by a login not in `system_owners` (9.7); and, for a
        decision made on a finding, a plain (not superseding) decision on a signature that already has an
        applicable active decision (`ActiveDecisionExists`, 9.7; `manifest_entries` and `manifest_tickets` of the
        run give the mark_intended scope). The same action on the same finding by the same person again returns the
        decision already written (9.6 idempotent).
        """
        d = _revalidate(decision)
        finding_id = self._same("finding_id", finding_id, d.finding_ref)
        expires_at = self._same("expires_at", utc(expires_at), utc(d.expires_at))
        confirmed_by = self._same("confirmed_by", confirmed_by, d.confirmed_by)
        supersedes = self._same("supersedes", supersedes, d.supersedes)
        self._human(d.decided_by, "decided_by")
        self._human(confirmed_by, "confirmed_by")
        self._human(proposed_by, "proposed_by")
        if not d.reason:
            raise StoreRefused("a decision carries its reason (8.3 decisions.reason)")
        if d.status != "active":
            raise StoreRefused("a new decision is written active; revoke and supersede change status (8.3)")
        did = decision_int(d.decision_id)
        cp = self._one("SELECT tier FROM checkpoints WHERE checkpoint_id = ?", (d.signature.checkpoint_id,))
        if cp is None:
            raise StoreRefused(f"unknown checkpoint {d.signature.checkpoint_id}")
        default = TIER1_TOLERANCE if cp["tier"] == 1 else DEFAULT_TOLERANCE
        if d.hamming_tolerance is not None and d.hamming_tolerance > default:
            raise StoreRefused(f"hamming_tolerance {d.hamming_tolerance} is above {default}, the most a tier-"
                               f"{cp['tier']} checkpoint allows (L-DEC-03)")
        tolerance = default if d.hamming_tolerance is None else d.hamming_tolerance
        region = self._one("SELECT g.hamming_tolerance FROM regions g JOIN runs r ON r.config_commit = g.config_commit "
                           "WHERE r.run_id = ? AND g.checkpoint_id = ? AND g.region_id = ?",
                           (run_id, d.signature.checkpoint_id, d.signature.region_id))
        if region is not None and region[0] is not None:          # lowered by calibration, never raised (9.7)
            tolerance = min(tolerance, region[0])
        decided_at = utc(d.decided_at)
        if expires_at is None and d.action in ("noise_signature", "request_mask"):
            expires_at = (datetime.fromisoformat(decided_at) + timedelta(days=EXPIRY_DAYS)).isoformat()
        if d.action == "mark_intended" and d.signature.category in WAIVER_CATEGORIES:
            # L-DEC-07, 9.7 waiver row: an expiry at most 30 days out, no silent renewal (S-03)
            if expires_at is None or (datetime.fromisoformat(expires_at)
                                      > datetime.fromisoformat(decided_at) + timedelta(days=EXPIRY_DAYS)):
                raise StoreRefused("a waiver (mark_intended on an invariant or assertion) carries expires_at at most "
                                   "30 days after decided_at (L-DEC-07)")
        source_finding = None
        if finding_id is not None:
            f = self._one("SELECT finding_id, synthetic FROM findings WHERE natural_key = ?", (finding_id,))
            if f is None:
                raise StoreRefused(f"unknown finding {finding_id}")
            source_finding = f["finding_id"]
            again = self._one("SELECT decision_id FROM decisions WHERE source_finding_id = ? AND action = ? AND "
                              "decided_by = ? AND status IN ('active','reopened')",
                              (source_finding, d.action, d.decided_by))
            if again is not None:                                   # 9.6: idempotent per finding and action
                return self._stored_decision(decision_ref(again[0]))
        old = None
        if supersedes is not None:
            old = self._one("SELECT status, build_id, decided_by, source_finding_id FROM decisions "
                            "WHERE decision_id = ?", (decision_int(supersedes),))
            if old is None or old["status"] not in ("active", "reopened"):
                raise StoreRefused(f"{supersedes} is not an active decision to supersede (8.3)")
        if d.action == "approve_baseline":
            self._approvable(run_id, d, cp["tier"])
        if d.action == "fixed_in_build" and fixed_in_build_id is not None:
            self._newer_build(fixed_in_build_id, build_id or (old["build_id"] if old is not None else None))
        triage = (TRIAGE_FOR_ACTION.get(d.action)
                  or ("confirmed_bug" if d.verdict == "confirmed_bug" else "open"))   # reject_baseline (4.14)
        self.taxonomy.check_member("triage_states", triage)
        with self._batch("decision") as rows:
            if d.action not in BASELINE_ACTIONS:                    # inside the batch: writes serialize (9.7)
                self._check_signature_free(d, run_id, decided_at, supersedes, source_finding, manifest_entries,
                                           manifest_tickets)
            self._insert(rows, "decisions", {
                "decision_id": did, "checkpoint_id": d.signature.checkpoint_id, "region_id": d.signature.region_id,
                "phash": phash_bytes(d.signature.phash), "hamming_tolerance": tolerance,
                "category": d.signature.category, "action": d.action, "verdict": d.verdict, "source": d.source,
                "decided_by": d.decided_by, "proposed_by": proposed_by, "confirmed_by": confirmed_by,
                "decided_at": decided_at, "run_id": run_id, "build_id": build_id, "reason": d.reason,
                "ticket_ref": d.ticket, "mask_ref": mask_ref, "baseline_id": baseline_id,
                "fixed_in_build_id": fixed_in_build_id, "cluster_id": cluster_id, "expires_at": expires_at,
                "status": "active", "supersedes_decision_id": decision_int(supersedes),
                "source_finding_id": source_finding, "role": d.role, "reopen_count": d.reopen_count,
                "bbox_q4": None if d.bbox_q4 is None else ",".join(map(str, d.bbox_q4))})
            if supersedes is not None:
                self._update(rows, "decisions", {"decision_id": decision_int(supersedes)}, {"status": "superseded"})
            if source_finding is not None:
                change = {"decision_id": did, "triage_state": triage}
                if d.ticket:
                    change["ticket_ref"] = d.ticket
                self._update(rows, "findings", {"finding_id": source_finding}, change)
            if old is not None and d.action != "fixed_in_build" and old["decided_by"] != d.decided_by:
                # 9.7: a supersession by another human marks the finding contested (fixed_in_build is the 4.14
                # lifecycle of a confirmed bug, not a contest)
                for fid in {old["source_finding_id"], source_finding} - {None}:
                    self._update(rows, "findings", {"finding_id": fid}, {"contested": 1})
        return self.get_decision(decision_ref(did))

    def _check_signature_free(self, d: Decision, run_id: str, at: str, supersedes: str | None,
                              source_finding: int | None, manifest_entries: Iterable[str],
                              manifest_tickets: Iterable[str]) -> None:
        """9.7 Two humans, one signature: on a frozen signature only a system owner decides; a plain decision made
        on a finding is refused while an applicable active decision matches the signature."""
        look = lookup_decisions(self._conn, d.signature, at=at, run_id=run_id, manifest_entries=manifest_entries,
                                manifest_tickets=manifest_tickets)
        if look.frozen and d.decided_by not in self.system_owners:
            raise StoreRefused(f"the signature of {d.signature.checkpoint_id} / {d.signature.region_id} is frozen "
                               "(two supersessions within 30 days): the next decision needs the system owner (9.7)")
        active = [m.decision for m in look.applicable]
        if supersedes is None and source_finding is not None and active:
            ids = ", ".join(f"{a.decision_id} ({a.action} by {a.decided_by})" for a in active)
            raise ActiveDecisionExists(f"the signature already has an active decision: {ids}; supersede or revoke "
                                       "it (9.7)", active)

    def revoke_decision(self, decision_id: str, *, by: str, reason: str, at: str | None = None) -> Decision:
        """Revoke a decision: the three revoke fields together and status revoked (8.3, 4.14); the row stays."""
        self._human(by, "revoked_by")
        if not reason:
            raise StoreRefused("a revoke carries its reason (8.3)")
        did = decision_int(decision_id)
        row = self._one("SELECT status FROM decisions WHERE decision_id = ?", (did,))
        if row is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        if row["status"] == "revoked":
            raise StoreRefused(f"{decision_id} is already revoked")
        with self._batch("decision_revoke") as rows:
            self._update(rows, "decisions", {"decision_id": did},
                         {"status": "revoked", "revoked_at": utc(at) or now(), "revoked_by": by,
                          "revoke_reason": reason})
        return self.get_decision(decision_id)

    def get_decision(self, decision_id: str) -> Decision | None:
        """The decision as stored, with every 4.14 field the store keeps (expiry, confirmer, finding ref, role, the
        build pair of its run)."""
        r = self._one(f"SELECT {_DECISION_COLUMNS} {_DECISION_FROM} WHERE d.decision_id = ?",
                      (decision_int(decision_id),))
        return None if r is None else _decision_from_row(r)

    def lookup_decisions(self, signature: Signature, *, at: str | None = None, rescued: bool = False,
                         synthetic: bool = False, run_id: str | None = None, manifest_entries: Iterable[str] = (),
                         manifest_tickets: Iterable[str] = ()) -> DecisionLookup:
        """The 8.3 signature lookup on this database (see the module-level `lookup_decisions`)."""
        return lookup_decisions(self._conn, signature, at=at, rescued=rescued, synthetic=synthetic, run_id=run_id,
                                manifest_entries=manifest_entries, manifest_tickets=manifest_tickets)

    # -- retention (8.6) -------------------------------------------------------------------------------------
    def deletable(self, sha256: str) -> bool:
        """8.6: never a pinned object, a quarantined one (8.4), one a baseline row names, or one a finding not closed
        references."""
        art = self._one("SELECT pinned, deleted_at FROM artifacts WHERE sha256 = ?", (sha256,))
        if art is None or art["pinned"] or art["deleted_at"] is not None:
            return False
        if self._one("SELECT 1 FROM artifact_sets WHERE sha256 = ? AND quarantined_at IS NOT NULL", (sha256,)):
            return False
        if self._one("SELECT 1 FROM baselines WHERE image_sha256 = ?1 OR sidecar_sha256 = ?1", (sha256,)):
            return False
        return self._one("SELECT 1 FROM findings WHERE triage_state <> 'closed' AND instr(evidence_json, ?) > 0",
                         (sha256,)) is None

    def sweep(self, artifacts: ArtifactStore, candidates: Iterable[str], *, backup_ok: bool,
              at: str | None = None) -> SweepResult:
        """Remove the candidates 8.6 allows from `artifacts`: deleted_at is set before the file is unlinked.

        Choosing candidates by class and age is the sweep job's; this enforces the safety rules. Nothing runs on
        a night whose backup failed (8.6: "retention skipped: backup failed").
        """
        if not backup_ok:
            return SweepResult(skipped=True)
        deleted, kept = [], []
        for sha in candidates:
            if not self.deletable(sha):
                kept.append(sha)
                continue
            with self._batch("retention") as rows:
                self._update(rows, "artifacts", {"sha256": sha}, {"deleted_at": utc(at) or now()})
            artifacts.unlink(sha)
            deleted.append(sha)
        return SweepResult(tuple(deleted), tuple(kept))

    # -- the public writer path (8.2): batches, inserts, updates and read queries for the store/* modules ------------
    def _table_columns(self, table: str) -> frozenset[str]:
        if not _IDENT.match(table):
            raise StoreRefused(f"table name {table!r}")
        cols = frozenset(r[1] for r in self._conn.execute(f"PRAGMA table_info({table})"))
        if not cols:
            raise StoreRefused(f"unknown table {table!r} (8.3)")
        return cols

    def _checked(self, table: str, *parts: Mapping[str, Any]) -> None:
        cols = self._table_columns(table)
        unknown = sorted(c for part in parts for c in part if c not in cols)
        if unknown:
            raise StoreRefused(f"{table} has no column {', '.join(unknown)} (8.3)")

    @contextlib.contextmanager
    def batch(self, kind: str) -> Iterator[WriteBatch]:
        """One writer batch (8.2): a `BEGIN IMMEDIATE` transaction whose rows reach results.jsonl before COMMIT.
        Write through the yielded `WriteBatch`; an exception rolls everything back."""
        with self._batch(kind) as rows:
            yield WriteBatch(self, rows)

    def insert(self, table: str, values: Mapping[str, Any], *, upsert: str | None = None) -> int:
        """Insert one row in its own batch; returns its rowid. The schema's CHECKs and triggers apply."""
        with self.batch(f"insert:{table}") as b:
            return b.insert(table, values, upsert=upsert)

    def update(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        """Update the rows matching `key` in its own batch; returns how many changed."""
        with self.batch(f"update:{table}") as b:
            return b.update(table, key, values)

    def query(self, sql: str, args: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        """A read on the writer's connection with `query_only` set, so it can never write (8.2)."""
        if self._conn is None:
            raise StoreError("store is closed")
        self._conn.execute("PRAGMA query_only = 1")
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            raise StoreRefused(f"query refused: {e}") from e
        finally:
            self._conn.execute("PRAGMA query_only = 0")

    def _write_once(self, rows: list[dict], table: str, key: Mapping[str, Any], values: dict) -> int | None:
        """Insert a row with a natural key once: the same row again is a no-op (None), a different one an
        ImportConflict (8.2). `values` must include the key columns."""
        where = " AND ".join(f"{c} IS ?" for c in key)
        old = self._one(f"SELECT * FROM {table} WHERE {where}", list(key.values()))
        if old is not None:
            if all(old[k] == v for k, v in values.items()):
                return None
            raise ImportConflict(f"{table} {dict(key)} is already recorded with other content (8.2)")
        return self._insert(rows, table, values)

    # -- system state (8.3, 8.6) -------------------------------------------------------------------------------------
    def set_system_state(self, key: str, value: str, *, at: str | None = None) -> None:
        """Set one documented `system_state` key; schema_version and schema_sha256 are the store's own."""
        if key not in SYSTEM_STATE_KEYS:
            raise StoreRefused(f"system_state key {key!r}; one of {sorted(SYSTEM_STATE_KEYS)} (8.3)")
        if not isinstance(value, str):
            raise StoreRefused("system_state values are text")
        with self._batch("system_state") as rows:
            self._insert(rows, "system_state", {"key": key, "value": value, "updated_at": utc(at) or now()},
                         upsert="(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")

    def get_system_state(self, key: str) -> str | None:
        r = self._one("SELECT value FROM system_state WHERE key = ?", (key,))
        return None if r is None else r[0]

    # -- step events (8.3, SD-16) ------------------------------------------------------------------------------------
    @staticmethod
    def _event_row(e: StepEvent) -> dict:
        row = {f.name: getattr(e, f.name) for f in dataclasses.fields(e) if f.name != "usage"}
        row.update(wall_at=utc(e.wall_at), drill=int(e.drill), injected=int(e.injected),
                   usage_json=None if e.usage is None else canonical_json(e.usage))
        return row

    def write_events(self, events: Sequence[StepEvent]) -> None:
        """Write `step_events` rows (8.3): inputs, popups, captures, gateway decisions, Watchdog events and
        relaunches, with each event's own seq and mono_ms. Keyed (shard_id, seq): the same event again is a no-op,
        a different one an ImportConflict. Refuses a shard not of the event's run and a finished run."""
        evs = [_revalidate(e) for e in events]
        for e in evs:
            self._running(e.run_id)
            shard = self._one("SELECT run_id FROM shards WHERE shard_id = ?", (e.shard_id,))
            if shard is None or shard["run_id"] != e.run_id:
                raise StoreRefused(f"{e.shard_id} is not a shard of run {e.run_id} (8.3)")
        with self._batch("events") as rows:
            for e in evs:
                self._write_once(rows, "step_events", {"shard_id": e.shard_id, "seq": e.seq}, self._event_row(e))

    def step_events(self, run_id: str) -> list[StepEvent]:
        """The run's step events in shard and seq order (8.3)."""
        names = {f.name for f in dataclasses.fields(StepEvent)}
        out = []
        for r in self._conn.execute("SELECT * FROM step_events WHERE run_id = ? ORDER BY shard_id, seq", (run_id,)):
            d = {k: v for k, v in dict(r).items() if k in names}
            d.update(drill=bool(r["drill"]), injected=bool(r["injected"]),
                     usage=json.loads(r["usage_json"]) if r["usage_json"] else None)
            out.append(StepEvent(**d))
        return out

    # -- Oracle extractions (SD-09) ----------------------------------------------------------------------------------
    _EXTRACTION_KEY = ("run_id", "checkpoint_id", "side", "assertion_id", "field", "source", "second_pass")

    def write_extractions(self, extractions: Sequence[Extraction]) -> None:
        """Store every Oracle extraction (2.8.1, 8.3 oracle_extractions); replays are no-ops, a different read for
        the same key an ImportConflict."""
        xs = [dataclasses.replace(x) for x in extractions]
        for x in xs:
            self._running(x.run_id)
        with self._batch("extractions") as rows:
            for x in xs:
                row = {**dataclasses.asdict(x), "second_pass": int(x.second_pass), "read_at": utc(x.read_at)}
                self._write_once(rows, "oracle_extractions", {k: row[k] for k in self._EXTRACTION_KEY}, row)

    def extractions(self, run_id: str, checkpoint_id: str | None = None) -> list[Extraction]:
        sql = "SELECT * FROM oracle_extractions WHERE run_id = ?" + (" AND checkpoint_id = ?" if checkpoint_id else "")
        args = (run_id, checkpoint_id) if checkpoint_id else (run_id,)
        names = {f.name for f in dataclasses.fields(Extraction)}
        return [Extraction(**{**{k: v for k, v in dict(r).items() if k in names}, "second_pass": bool(r["second_pass"])})
                for r in self._conn.execute(sql + " ORDER BY extraction_id", args)]

    # -- fingerprints (4.12, 7.4) ------------------------------------------------------------------------------------
    def write_fingerprint(self, fp: Fingerprint) -> None:
        """Write one side's fingerprint of a shard (8.3 env_fingerprints), once: the same fingerprint again is a
        no-op, a different one an ImportConflict (8.2). The build and rig must be registered."""
        f = dataclasses.replace(fp)                      # re-validates; fingerprint_id recomputes (L-FPR-01)
        if f.shard_id is None:
            raise StoreRefused("a stored fingerprint names its shard (8.3 env_fingerprints.shard_id)")
        self._running(f.run_id)
        probe = f.match.get("render_probe_hash")
        row = {"run_id": f.run_id, "shard_id": f.shard_id, "side": f.side, "rig_id": f.rig_id, "build_id": f.build_id,
               "fingerprint_sha256": f.fingerprint_id, "read_at": utc(f.read_at),
               "fields_json": canonical_json({"schema_version": f.schema_version, "match": f.match,
                                              "record": f.record}),
               "render_probe_hash": phash_bytes(probe) if isinstance(probe, str) and _PHASH_RE.match(probe) else None}
        with self._batch("fingerprint") as rows:
            self._write_once(rows, "env_fingerprints", {"shard_id": f.shard_id, "side": f.side}, row)

    def fingerprint(self, shard_id: str, side: str) -> Fingerprint | None:
        r = self._one("SELECT * FROM env_fingerprints WHERE shard_id = ? AND side = ?", (shard_id, side))
        if r is None:
            return None
        fields_ = json.loads(r["fields_json"])
        return Fingerprint(run_id=r["run_id"], side=r["side"], rig_id=r["rig_id"], read_at=r["read_at"],
                           match=fields_["match"], record=fields_["record"], shard_id=r["shard_id"],
                           fingerprint_id=r["fingerprint_sha256"], schema_version=fields_["schema_version"])

    # -- egress and the outbox (6.10, 9.1) ---------------------------------------------------------------------------
    def log_egress(self, row: EgressRow) -> int:
        """Write one `egress_log` row (6.10): every outbound request, sent, refused (with its refusal_reason) or
        failed. Returns the egress id."""
        e = dataclasses.replace(row)
        with self._batch("egress") as rows:
            return self._insert(rows, "egress_log", {**dataclasses.asdict(e), "sent_at": utc(e.sent_at)})

    def enqueue(self, job: OutboxJob) -> int:
        """Write an outbound post as a queued job before any send (9.1); returns its job id."""
        j = dataclasses.replace(job)
        if j.state != "queued" or j.attempts or j.job_id is not None:
            raise StoreRefused("a new outbox job is queued, with no attempts and no id (9.1)")
        values = {k: v for k, v in dataclasses.asdict(j).items() if k != "job_id"}
        values.update(created_at=utc(j.created_at), next_attempt_at=utc(j.next_attempt_at))
        with self._batch("outbox_enqueue") as rows:
            return self._insert(rows, "outbox", values)

    def outbox_job(self, job_id: int) -> OutboxJob | None:
        r = self._one("SELECT * FROM outbox WHERE job_id = ?", (job_id,))
        return None if r is None else OutboxJob(**dict(r))

    def due(self, at: str | None = None) -> list[OutboxJob]:
        """Queued and failed jobs whose next attempt is due at `at` (default now), oldest first (9.1)."""
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE state IN ('queued','failed') AND (next_attempt_at IS NULL "
            "OR next_attempt_at <= ?) ORDER BY created_at, job_id", (utc(at) or now(),))
        return [OutboxJob(**dict(r)) for r in rows]

    def _open_job(self, job_id: int, states: tuple[str, ...]) -> sqlite3.Row:
        r = self._one("SELECT * FROM outbox WHERE job_id = ?", (job_id,))
        if r is None:
            raise StoreRefused(f"unknown outbox job {job_id}")
        if r["state"] not in states:
            raise StoreRefused(f"outbox job {job_id} is {r['state']}; this change needs {' or '.join(states)} (9.1)")
        return r

    def mark_sent(self, job_id: int, *, at: str | None = None) -> None:
        """A send succeeded: state sent with sent_at (8.3)."""
        r = self._open_job(job_id, OUTBOX_OPEN)
        with self._batch("outbox_sent") as rows:
            self._update(rows, "outbox", {"job_id": job_id},
                         {"state": "sent", "sent_at": utc(at) or now(), "attempts": r["attempts"] + 1})

    def mark_failed(self, job_id: int, *, error: str, next_attempt_at: str) -> None:
        """A send failed: state failed, the error and when to try again (the 9.1 schedule is the caller's)."""
        r = self._open_job(job_id, OUTBOX_OPEN)
        if not error:
            raise StoreRefused("a failed attempt records its error (9.1)")
        with self._batch("outbox_failed") as rows:
            self._update(rows, "outbox", {"job_id": job_id},
                         {"state": "failed", "last_error": error, "next_attempt_at": utc(next_attempt_at),
                          "attempts": r["attempts"] + 1})

    def abandon(self, job_id: int) -> None:
        """24 hours of failure: the job is abandoned and listed until a human closes it (9.1)."""
        self._open_job(job_id, OUTBOX_OPEN)
        with self._batch("outbox_abandon") as rows:
            self._update(rows, "outbox", {"job_id": job_id}, {"state": "abandoned", "next_attempt_at": None})

    def close_job(self, job_id: int, *, by: str, at: str | None = None) -> None:
        """A human closes an abandoned job (9.1)."""
        r = self._open_job(job_id, ("abandoned",))
        self._human(by, "closed_by")
        if r["closed_by"] is not None:
            raise StoreRefused(f"outbox job {job_id} is already closed")
        with self._batch("outbox_close") as rows:
            self._update(rows, "outbox", {"job_id": job_id}, {"closed_by": by, "closed_at": utc(at) or now()})

    def fill_ticket_ref(self, decision_id: str, ticket: str) -> Decision:
        """The outbox's late ticket for a confirm_bug written while Jira was unreachable: set once (L-DEC-02,
        9.9). The finding that took the decision gets the ticket too."""
        did = decision_int(decision_id)
        if not ticket:
            raise StoreRefused("a ticket reference is non-empty")
        if self._one("SELECT 1 FROM decisions WHERE decision_id = ?", (did,)) is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        with self._batch("decision_ticket") as rows:
            self._update(rows, "decisions", {"decision_id": did}, {"ticket_ref": ticket})
            for f in self._conn.execute("SELECT finding_id FROM findings WHERE decision_id = ? AND ticket_ref IS NULL",
                                        (did,)).fetchall():
                self._update(rows, "findings", {"finding_id": f[0]}, {"ticket_ref": ticket})
        return self._stored_decision(decision_id)

    def set_mask_ref(self, decision_id: str, mask_id: str) -> Decision:
        """A request_mask decision's mask file landed: mask_ref set once, which makes the decision act (4.14)."""
        did = decision_int(decision_id)
        if self._one("SELECT 1 FROM decisions WHERE decision_id = ?", (did,)) is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        with self._batch("decision_mask") as rows:
            self._update(rows, "decisions", {"decision_id": did}, {"mask_ref": mask_id})
        return self._stored_decision(decision_id)

    def _stored_decision(self, decision_id: str) -> Decision:
        d = self.get_decision(decision_id)
        if d is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        return d

    # -- flake, calibration, evals, soak, coverage, usage (8.1, 8.3) ------------------------------------------------
    def write_flake_event(self, run_id: str, checkpoint_id: str, *, captures: Sequence[str],
                          metrics: Mapping[str, Any], region_id: str | None = None,
                          created_at: str | None = None) -> int:
        """One `flake_events` row per unstable targeted re-run (3.10 step 5)."""
        if len(captures) < 2:
            raise StoreRefused("a flake event compares at least two captures (3.10)")
        with self._batch("flake_event") as rows:
            return self._insert(rows, "flake_events", {
                "run_id": run_id, "checkpoint_id": checkpoint_id, "region_id": region_id,
                "captures_json": json.dumps(list(captures)), "metrics_json": canonical_json(dict(metrics)),
                "created_at": utc(created_at) or now()})

    def replace_flake_stats(self, rows_in: Iterable[Mapping[str, Any]]) -> None:
        """Replace the rolling 30-day `flake_stats` row of each checkpoint given (10.5); the CHECK refuses a
        quarantined row without its commit, owner and ticket."""
        stats = [dict(r) for r in rows_in]
        with self._batch("flake_stats") as rows:
            for r in stats:
                r.setdefault("updated_at", now())
                r["updated_at"] = utc(r["updated_at"])
                self._checked("flake_stats", r)
                sets = ", ".join(f"{c} = excluded.{c}" for c in r if c != "checkpoint_id")
                self._insert(rows, "flake_stats", r, upsert=f"(checkpoint_id) DO UPDATE SET {sets}")

    def write_calibration(self, floors: Sequence[Mapping[str, Any]],
                          fixes: Sequence[Mapping[str, Any]] = ()) -> list[int]:
        """Write calibration floors and fix-list items (3.9, SD-19, SD-20). A floor supersedes the current floor of
        its (checkpoint, region, rendering key), so `ux_floor_current` holds; a fix item without `floor_id` that
        names a region links to the floor written here for its checkpoint and region. Returns the floor ids."""
        with self._batch("calibration") as rows:
            ids, by_region = [], {}
            for f in floors:
                f = dict(f)
                f["valid_from"] = utc(f.get("valid_from")) or now()
                self._checked("calibration_floors", f)
                self._conn.execute(
                    "UPDATE calibration_floors SET superseded_at = ? WHERE checkpoint_id = ? AND region_id = ? "
                    "AND rendering_key_sha256 = ? AND superseded_at IS NULL",
                    (f["valid_from"], f["checkpoint_id"], f["region_id"], f["rendering_key_sha256"]))
                rows.append({"op": "supersede", "table": "calibration_floors",
                             "key": {k: f[k] for k in ("checkpoint_id", "region_id", "rendering_key_sha256")}})
                fid = self._insert(rows, "calibration_floors", f)
                ids.append(fid)
                by_region[(f["checkpoint_id"], f["region_id"])] = fid
            for x in fixes:
                x = dict(x)
                x["opened_at"] = utc(x.get("opened_at")) or now()
                if x.get("floor_id") is None and x.get("region_id") is not None:
                    x["floor_id"] = by_region.get((x["checkpoint_id"], x["region_id"]))
                self._checked("calibration_fixes", x)
                self._insert(rows, "calibration_fixes", x)
        return ids

    def write_reviewer_eval(self, *, eval_set_version: str, model_id: str, prompt_hash: str, items: int,
                            metrics: Mapping[str, Any], passed_gate: bool, run_at: str | None = None) -> int:
        """One `reviewer_evals` row per evaluation of a model and prompt version (3.14, 10.4)."""
        with self._batch("reviewer_eval") as rows:
            return self._insert(rows, "reviewer_evals", {
                "eval_set_version": eval_set_version, "model_id": model_id, "prompt_hash": prompt_hash,
                "items": items, "metrics_json": canonical_json(dict(metrics)), "passed_gate": int(passed_gate),
                "run_at": utc(run_at) or now()})

    def write_soak_samples(self, run_id: str, samples: Iterable[Mapping[str, Any]]) -> None:
        """`soak_samples` rows, one per process, metric and 60 s sample (10.11); replays are no-ops."""
        with self._batch("soak") as rows:
            for smp in samples:
                row = {"run_id": run_id, "sampled_at": utc(smp["sampled_at"]), "pid": smp["pid"],
                       "metric": smp["metric"], "value": smp["value"]}
                self._write_once(rows, "soak_samples", {k: row[k] for k in ("run_id", "sampled_at", "pid", "metric")},
                                 row)

    def write_coverage_cells(self, cells: Iterable[Mapping[str, Any]]) -> None:
        """The 2.12 coverage cells of a merge, RC or Nightly run (the 8.3 trigger refuses a PR run's); rows as
        `coverage.CoverageMatrix.to_rows` gives them, optionally with profile_id. Replays are no-ops."""
        with self._batch("coverage") as rows:
            for c in cells:
                row = dict(c)
                self._checked("coverage_cells", row)
                key = {k: row[k] for k in ("run_id", "surface_id", "persona", "entitlement")}
                self._write_once(rows, "coverage_cells", key, row)

    def write_model_usage(self, usage: ModelUsage) -> int:
        """The usage block of one model response, per component (11.4)."""
        u = dataclasses.replace(usage)
        with self._batch("model_usage") as rows:
            return self._insert(rows, "model_usage", {**dataclasses.asdict(u), "at": utc(u.at)})

    # -- canaries (10.3, SD-91) --------------------------------------------------------------------------------------
    def write_synthetic_outcome(self, o: SyntheticOutcome) -> int | None:
        """What one canary mutant produced on one checkpoint (SD-91), keyed (run, mutant, checkpoint): the same row
        again is a no-op (None), a different one an ImportConflict. Returns the synthetic id when written."""
        so = dataclasses.replace(o)
        self._running(so.run_id)
        code, param = self._split_reason(so.outcome, so.reason)
        detail = so.detail if param is None else (param if so.detail is None else f"{param}\n{so.detail}")
        row = {"run_id": so.run_id, "mutant_id": so.mutant_id, "checkpoint_id": so.checkpoint_id,
               "outcome": so.outcome, "reason": code, "reason_detail": detail, "gate": so.gate,
               "mutant_sha256": so.mutant_sha256, "recorded_at": utc(so.recorded_at)}
        with self._batch("synthetic_outcome") as rows:
            sid = self._write_once(rows, "synthetic_outcomes",
                                   {k: row[k] for k in ("run_id", "mutant_id", "checkpoint_id")}, row)
        self.link_synthetic_refs(so.run_id)
        return sid

    def link_synthetic_refs(self, run_id: str) -> int:
        """Point every mutant `canary_results` row of the run at its synthetic outcome (SD-91); returns how many."""
        with self._batch("synthetic_link") as rows:
            cur = self._conn.execute(
                "UPDATE canary_results SET synthetic_ref = (SELECT s.synthetic_id FROM synthetic_outcomes s "
                "WHERE s.run_id = canary_results.run_id AND s.mutant_id = canary_results.mutant_id "
                "AND s.checkpoint_id = canary_results.checkpoint_id) WHERE run_id = ? AND kind = 'mutant' "
                "AND synthetic_ref IS NULL AND EXISTS (SELECT 1 FROM synthetic_outcomes s "
                "WHERE s.run_id = canary_results.run_id AND s.mutant_id = canary_results.mutant_id "
                "AND s.checkpoint_id = canary_results.checkpoint_id)", (run_id,))
            if cur.rowcount:
                rows.append({"op": "link", "table": "canary_results", "key": {"run_id": run_id}})
            return cur.rowcount

    def write_canary_manifest(self, run_id: str, *, catalog_sha256: str, manifest: Any,
                              created_at: str | None = None) -> None:
        """The generated catalog of a run for the 10.3 determinism check, once per run (8.2)."""
        row = {"run_id": run_id, "catalog_sha256": catalog_sha256, "manifest_json": canonical_json(manifest)}
        old = self._one("SELECT catalog_sha256, manifest_json FROM canary_manifests WHERE run_id = ?", (run_id,))
        if old is not None:
            if (old[0], old[1]) == (catalog_sha256, row["manifest_json"]):
                return
            raise ImportConflict(f"run {run_id} already has a canary manifest (8.2)")
        with self._batch("canary_manifest") as rows:
            self._insert(rows, "canary_manifests", {**row, "created_at": utc(created_at) or now()})

    # -- triage records (9.6, 9.11, 9.12, 10.9, 11.5) ---------------------------------------------------------------
    def _finding_int(self, finding_id: str) -> int:
        r = self._one("SELECT finding_id FROM findings WHERE natural_key = ?", (finding_id,))
        if r is None:
            raise StoreRefused(f"unknown finding {finding_id}")
        return r[0]

    def write_escape(self, e: Escape) -> int:
        """One G+1 escape row (10.9, 9.11)."""
        x = dataclasses.replace(e)
        values = {"release": x.release, "ticket_ref": x.ticket, "escape_class": x.escape_class,
                  "checkpoint_id": x.checkpoint_id, "surface_id": x.surface_id, "tier": x.tier,
                  "entered_by": x.entered_by, "entered_at": utc(x.entered_at)}
        with self._batch("escape") as rows:
            return self._insert(rows, "escapes", values)

    def write_proposal(self, p: Proposal) -> int:
        """A two-person-rule proposal on a finding (9.6), written in state proposed; returns its id."""
        x = dataclasses.replace(p)
        if x.state != "proposed" or x.proposal_id is not None:
            raise StoreRefused("a new proposal is written proposed, without an id (9.6)")
        values = {"finding_id": self._finding_int(x.finding_id), "run_id": x.run_id, "action": x.action,
                  "proposed_by": x.proposed_by, "proposed_at": utc(x.proposed_at), "ticket_ref": x.ticket,
                  "reason": x.reason, "state": "proposed"}
        with self._batch("proposal") as rows:
            return self._insert(rows, "proposals", values)

    def proposal(self, proposal_id: int) -> Proposal | None:
        r = self._one("SELECT p.*, f.natural_key FROM proposals p JOIN findings f USING (finding_id) "
                      "WHERE p.proposal_id = ?", (proposal_id,))
        if r is None:
            return None
        return Proposal(finding_id=r["natural_key"], run_id=r["run_id"], action=r["action"],
                        proposed_by=r["proposed_by"], reason=r["reason"], proposed_at=r["proposed_at"],
                        ticket=r["ticket_ref"], state=r["state"], decided_by=r["decided_by"],
                        decided_at=r["decided_at"], decline_reason=r["decline_reason"],
                        decision_ref=decision_ref(r["decision_id"]), proposal_id=r["proposal_id"])

    def update_proposal(self, proposal_id: int, *, state: str, decided_by: str, decided_at: str | None = None,
                        decline_reason: str | None = None, decision_id: str | None = None) -> Proposal:
        """Confirm (naming the decision written for it) or decline (with the reason) an open proposal (9.6)."""
        old = self.proposal(proposal_id)
        if old is None:
            raise StoreRefused(f"unknown proposal {proposal_id}")
        if old.state != "proposed":
            raise StoreRefused(f"proposal {proposal_id} is already {old.state} (9.6)")
        new = dataclasses.replace(old, state=state, decided_by=decided_by, decided_at=utc(decided_at) or now(),
                                  decline_reason=decline_reason, decision_ref=decision_id)
        with self._batch("proposal_update") as rows:
            self._update(rows, "proposals", {"proposal_id": proposal_id},
                         {"state": new.state, "decided_by": new.decided_by, "decided_at": new.decided_at,
                          "decline_reason": new.decline_reason, "decision_id": decision_int(new.decision_ref)})
        return new

    def write_maintenance_item(self, m: MaintenanceItem) -> int:
        """One flow-maintenance item (11.5); returns its id."""
        x = dataclasses.replace(m)
        values = {k: v for k, v in dataclasses.asdict(x).items() if k != "item_id"}
        values.update(opened_at=utc(x.opened_at), closed_at=utc(x.closed_at))
        with self._batch("maintenance_item") as rows:
            return self._insert(rows, "maintenance_items", values)

    def write_triage_assignment(self, finding_id: str, *, assignee: str, assigned_by: str,
                                sla_due_at: str | None = None, escalation_step: int = 0,
                                at: str | None = None) -> int:
        """Assign a finding (9.12): the current assignment, if any, is released in the same batch."""
        fid = self._finding_int(finding_id)
        when = utc(at) or now()
        with self._batch("triage_assignment") as rows:
            self._conn.execute("UPDATE triage_assignments SET released_at = ? WHERE finding_id = ? "
                               "AND released_at IS NULL", (when, fid))
            rows.append({"op": "release", "table": "triage_assignments", "key": {"finding_id": fid}})
            return self._insert(rows, "triage_assignments", {
                "finding_id": fid, "assignee": assignee, "assigned_by": assigned_by, "assigned_at": when,
                "escalation_step": escalation_step, "sla_due_at": utc(sla_due_at)})

    # -- artifact sets (SD-82) and the fleet (11.2) ------------------------------------------------------------------
    def write_artifact_set(self, sha256: str, set_path: str, *, created_at: str | None = None) -> None:
        """Record a copy of an indexed object in a set (SD-82); a copy already recorded is a no-op."""
        if self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (sha256,)) is None:
            raise StoreRefused(f"artifact {sha256} is not indexed (8.3)")
        if self._one("SELECT 1 FROM artifact_sets WHERE sha256 = ? AND set_path = ?", (sha256, set_path)):
            return
        with self._batch("artifact_set") as rows:
            self._insert(rows, "artifact_sets", {"sha256": sha256, "set_path": set_path,
                                                 "created_at": utc(created_at) or now()})

    def reassign_login(self, from_rig: str, to_rig: str, *, operator: str, snapshot_id: str,
                       at: str | None = None) -> None:
        """Swap a spare in for a failed rig (11.2): the old rig is deactivated and the spare, re-snapshotted with
        the old rig's login (`snapshot_id`), takes its login and role; both rows record the operator and time."""
        self._human(operator, "operator")
        old = self._one("SELECT * FROM rigs WHERE rig_id = ?", (from_rig,))
        spare = self._one("SELECT * FROM rigs WHERE rig_id = ?", (to_rig,))
        if old is None or spare is None:
            raise StoreRefused(f"unknown rig {from_rig if old is None else to_rig}")
        if not old["active"]:
            raise StoreRefused(f"{from_rig} is not active; its login is not on it (11.2)")
        if not snapshot_id or snapshot_id == spare["snapshot_id"]:
            raise StoreRefused("the spare is re-snapshotted with the moved login first (11.2): a new snapshot id")
        when = utc(at) or now()
        with self._batch("reassign_login") as rows:
            self._update(rows, "rigs", {"rig_id": from_rig},
                         {"active": 0, "reassigned_by": operator, "reassigned_at": when})
            self._update(rows, "rigs", {"rig_id": to_rig},
                         {"login_alias": old["login_alias"], "role": old["role"], "snapshot_id": snapshot_id,
                          "active": 1, "reassigned_by": operator, "reassigned_at": when})


class WriteBatch:
    """The writer handle of one `Store.batch` (8.2): every insert and update is journaled with the batch."""

    def __init__(self, store: Store, rows: list[dict]):
        self._store = store
        self.rows = rows

    def insert(self, table: str, values: Mapping[str, Any], *, upsert: str | None = None) -> int:
        """Insert one row; returns its rowid."""
        self._store._checked(table, values)
        return self._store._insert(self.rows, table, dict(values), upsert=upsert)

    def update(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        """Update the rows matching `key`; returns how many changed."""
        if not key or not values:
            raise StoreRefused("an update names its key and its values")
        self._store._checked(table, key, values)
        return self._store._update(self.rows, table, dict(key), dict(values))
