"""Typed results every gate and safety control returns (5.2, 1.10, 4.13, 9.10).

A `ControlResult` states, in the closed taxonomy, exactly what a control decided:

- `run`: the run outcome it sets (`RunVerdict`), or None when the run continues;
- `checkpoint`: the outcome of the checkpoint the control is about (the one in progress, the drill
  checkpoint, the canary row);
- `unreached`: what every scoped checkpoint still without an outcome gets when the run (or shard)
  stops here, written through an `OutcomeSink`;
- `outcomes`: outcomes for named subsets of the scope while the run continues (the `account_state`
  checkpoints of 7.6, the OCR-assertion checkpoints of 5.10, the frozen checkpoints of N4);
- `gate`: the gate in force; `alert`: the 9.10 route (who is paged); `actions`: what must happen
  next (deny-all, kill the tree, block the login, relaunch, quarantine); `findings`; the incident
  severity of 6.12.

Every outcome, reason and gate is validated against `sentinel.taxonomy` at construction, and the
unreached outcome of an aborted run is checked against 1.10: an `aborted_safety` run's unreached
checkpoints are `env_error` with the aborting gate and the run's abort reason, an `aborted_harness`
run's are `harness_error` with its abort reason, an `aborted_budget` run's are `not_run (reason:
budget)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..records import Finding
from ..taxonomy import TaxonomyError, load
from .alerts import AlertRoute

INCIDENT_SEVERITIES = ("S1", "S2", "S3")

# What a control asks the driver to do next. Each name is a step the spec names (section in the comment).
ACTIONS = frozenset({
    "deny_all",                      # the hook enters deny-all (5.4.2, 6.4, 6.6 step 1)
    "rearm",                         # deny-all lifted after a readable identity (6.4)
    "kill_tree",                     # the thinkorswim process tree is killed, hook host last (6.6 step 3, 10.2)
    "kill_switch",                   # run the 6.6 sequence
    "block_login",                   # logins.blocked_at (6.4, 6.5, 6.6)
    "disable_rig",                   # rig disabled in the scheduler (6.6)
    "revert_snapshot",               # 6.6 step 6
    "recovery_login",                # 6.6 step 5
    "cancel_all",                    # the recovery flow's `cancel_all` teardown action (6.6)
    "teardown",                      # enter `teardown` (5.2)
    "relaunch",                      # relaunch and re-enter at `launched` (7.7, 10.2)
    "restart_hook_host",             # 5.4.2
    "restart_orchestrator",          # `orchestrator --resume` (10.2)
    "resume_next_flow",              # resume at the next flow boundary (10.2)
    "retry_step",                    # the single step retry (5.4.3 stage 7, 10.7)
    "end_flow",                      # the flow ends (5.4.8)
    "end_session",                   # the agent or Explorer session ends (5.4.8, 6.8)
    "close_window_escape",           # the gateway's recovery path closes the window with a tokened Escape (6.8)
    "quarantine_flow",               # quarantine record for the flow (5.4.8, 6.6)
    "quarantine_order_stage_flows",  # every flow with an order-stage step (6.5)
    "quarantine_all_flows",          # every flow of every class on the rig (6.5 item 5)
    "maintenance_task",              # a flow maintenance task for an owner (6.3, 6.4, 6.5)
    "select_fixture_account",        # 7.7 step 7, A2
    "reset_empty_account",           # 7.6 stage 2 step 3
    "withhold_report",               # 6.9
    "rotate_credential",             # 6.9
    "requeue_shard",                 # 10.7 rig loss
    "pause_capture",                 # 10.7 network loss
    "reassert_paper_mode",           # 10.7 reconnect
    "light_fixture_check",           # 10.7 reconnect
    "wait_session_release",          # `session_release_wait` before a login (7.7, 6.6)
    "stop_model_calls",              # 10.7 spend ceilings
    "self_abort",                    # the heartbeat named-event code (10.2)
    "free_space_check",              # the Watchdog's immediate free-space check (10.2)
    "reset_input_state",             # 5.4.5, 5.4.10
    "queue_calibration",             # 7.13
})


@dataclass(frozen=True)
class Outcome:
    """One checkpoint outcome: outcome, reason, gate and detail, validated against 4.13."""

    outcome: str
    reason: str | None = None
    gate: str | None = None
    detail: str | None = None

    def __post_init__(self):
        load().check_checkpoint(self.outcome, self.reason, self.gate)


@dataclass(frozen=True)
class RunVerdict:
    """A run outcome of 1.10 with its reason, validated against 4.13."""

    status: str
    reason: str | None = None

    def __post_init__(self):
        load().check_run(self.status, self.reason)

    @property
    def aborted(self) -> bool:
        return self.status.startswith("aborted_")


# 8.2: the run outcome is the worst of the shards' in this order
RUN_SEVERITY = ("complete", "complete_with_env_errors", "aborted_budget", "aborted_harness", "aborted_safety")


def worse(a: RunVerdict | None, b: RunVerdict | None) -> RunVerdict | None:
    """The more severe of two run verdicts (8.2 order); the first wins a tie."""
    if a is None or b is None:
        return a or b
    return b if RUN_SEVERITY.index(b.status) > RUN_SEVERITY.index(a.status) else a


@dataclass(frozen=True)
class FindingSpec:
    """A finding a control raises, before the store gives it an id (4.13 finding kinds)."""

    kind: str
    description: str
    severity: str | None = None
    detail: str | None = None
    tier: int | None = None

    def __post_init__(self):
        load().check_member("finding_kinds", self.kind)

    def to_finding(self, finding_id: str, run_id: str, first_seen_run_id: str | None = None, **extra: Any) -> Finding:
        """The `records.Finding` for this spec."""
        return Finding(finding_id=finding_id, run_id=run_id, kind=self.kind, description=self.description,
                       first_seen_run_id=first_seen_run_id or run_id, severity=self.severity, detail=self.detail,
                       tier=self.tier, **extra)


@dataclass(frozen=True)
class SubsetOutcome:
    """An outcome for a named part of the scope.

    `selector` names which checkpoints: `all`, `shard`, `account_state` (4.5, 7.6), `account_state:golden`,
    `api_within` (N3), `frozen` (N4), `ocr_assertion` (5.10), `probe:<Pn>` (2.2.3), `in_progress` (the
    checkpoint being captured), `rest_of_flow`, `first_unreached` (10.1), `explorer_stage` (6.3 A10, 6.8),
    `drill` (6.5), `pending_review` (10.7), `uncaptured` (10.7).
    """

    selector: str
    outcome: Outcome


@dataclass(frozen=True)
class ControlResult:
    """What one gate or safety control decided (module docstring)."""

    control: str
    passed: bool
    gate: str | None = None
    run: RunVerdict | None = None
    checkpoint: Outcome | None = None
    unreached: Outcome | None = None
    outcomes: tuple[SubsetOutcome, ...] = ()
    alert: AlertRoute | None = None
    actions: tuple[str, ...] = ()
    findings: tuple[FindingSpec, ...] = ()
    incident: str | None = None
    step: str | None = None
    step_reason: str | None = None
    detail: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        t = load()
        if self.gate is not None and self.gate not in t.gates:
            raise TaxonomyError(f"unknown gate {self.gate!r}")
        if self.step is not None:
            t.check_step(self.step)
        if self.incident not in (None, *INCIDENT_SEVERITIES):
            raise TaxonomyError(f"incident severity {self.incident!r}")
        bad = set(self.actions) - ACTIONS
        if bad:
            raise ValueError(f"unknown actions {sorted(bad)}")
        if self.run is not None and self.run.aborted and self.unreached is not None:
            _check_unreached(self.run, self.unreached)

    @property
    def run_status(self) -> str | None:
        return self.run.status if self.run else None

    @property
    def run_reason(self) -> str | None:
        return self.run.reason if self.run else None

    def has(self, action: str) -> bool:
        return action in self.actions


def _check_unreached(run: RunVerdict, u: Outcome) -> None:
    """1.10: unreached checkpoints of an aborted run carry its abort reason."""
    if run.status == "aborted_safety":
        ok = u.outcome == "env_error" and u.reason == run.reason and u.gate is not None
    elif run.status == "aborted_harness":
        ok = u.outcome == "harness_error" and u.reason == run.reason
    else:
        ok = u.outcome == "not_run" and u.reason == "budget"
    if not ok:
        raise TaxonomyError(f"unreached {u.outcome} ({u.reason}) does not carry the abort of a {run.status} "
                            f"({run.reason}) run (1.10)")


def safety_abort_outcome(gate: str, reason: str, detail: str | None = None) -> tuple[RunVerdict, Outcome]:
    """The run verdict and unreached outcome of an `aborted_safety` at `gate` (1.10, 6.6 step 2)."""
    return RunVerdict("aborted_safety", reason), Outcome("env_error", reason, gate, detail)


def harness_abort_outcome(reason: str, detail: str | None = None) -> tuple[RunVerdict, Outcome]:
    """The run verdict and unreached outcome of an `aborted_harness` (1.10, 10.1)."""
    return RunVerdict("aborted_harness", reason), Outcome("harness_error", reason, None, detail)


def budget_abort_outcome(reason: str) -> tuple[RunVerdict, Outcome]:
    """The run verdict and unreached outcome of an `aborted_budget` (5.2)."""
    return RunVerdict("aborted_budget", reason), Outcome("not_run", "budget")


class OutcomeSink(Protocol):
    """Where unreached checkpoints are written: `store.Store.fill_unreached` has this signature (8.2)."""

    def fill_unreached(self, run_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None,
                       detail: str | None = None) -> list[str]:
        """Write `outcome` for every scoped checkpoint of the run that has none; return their ids."""


def write_unreached(sink: OutcomeSink, run_id: str, result: ControlResult) -> list[str]:
    """Give every silent scoped checkpoint the result's unreached outcome (L-OUT-07, 1.10); [] when it has none."""
    u = result.unreached
    if u is None:
        return []
    return sink.fill_unreached(run_id, u.outcome, u.reason, gate=u.gate, detail=u.detail)
