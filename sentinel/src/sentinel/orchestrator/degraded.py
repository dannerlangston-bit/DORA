"""Degraded modes: the model API down, network loss, rig loss, and the spend ceilings (10.7, 11.4; F29, F30, F45).

Model API. Preflight N2 or N5 failing is a degraded mode, never `env_error`: the run continues with
`review_mode: deferred` and `agent_available: false`. Every item that needs a verdict is `needs_human
(reason: reviewer_unavailable)` with `review_state: pending`; a locator failure is step `failed`, its
checkpoint `landmark_missing (reason: locator:<step>)`, the rest of the flow `not_run (reason:
step_failed:<step>)` and a `locator_failure` finding labeled "scripted locator failed; agent fallback
disabled"; an agent-only step is `not_run (reason: model_unavailable)`; the Explorer does not start. The
flag is re-checked once per flow and a recovery is logged.

Spend. The per-run ceiling (50 dollars, every model call of the run) is the only one that ends a run:
no further model call, and at the next flow boundary the run ends `aborted_budget (reason: dollar_cap)`
with uncaptured checkpoints `not_run (reason: budget)` and pending items `needs_human (reason:
reviewer_cap)` with `cause: dollar_cap` in `detail` and `review_state: done`. The weekly ceiling (500)
stops model calls and aborts nothing. The Explorer's 25 per session is explorer.py.

Network. The anti-anchors Disconnected and Reconnecting stop capture; the Orchestrator polls connectivity.
Back within 5 minutes: re-assert paper mode, the light fixture check, retry the current step once.
Between 5 and 15 minutes: the checkpoint in progress and the rest of its flow `env_error (gate:
connectivity)`, resume at the next flow boundary after the same re-verification. Past 15 minutes: the
shard's remaining checkpoints `env_error (gate: connectivity)`, run `complete_with_env_errors`. Pause time
counts against the wall budget.

Rig. Three minutes without a Rig Manager heartbeat is a lost rig. The shard is re-queued once, in full, to
a rig of the same fingerprint class if the wall budget allows; otherwise its checkpoints are `env_error
(gate: rig_lost)` and the run `complete_with_env_errors`. A second loss of the same shard ends the run
`aborted_harness (reason: rig_lost)` with `harness_error (reason: rig_lost)` on every unfinished checkpoint.

Choice (reported): the connectivity and rig-loss `env_error` rows carry reason `gate` (the gate's own check,
no finer code in 4.13).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import alerts
from .results import (ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, budget_abort_outcome,
                      harness_abort_outcome)

PER_RUN_USD = 50.0
PER_WEEK_USD = 500.0
NETWORK_RETRY_S = 5 * 60.0
NETWORK_SHARD_S = 15 * 60.0
RIG_SILENCE_S = 180.0


# -- model API ------------------------------------------------------------------------------------------
@dataclass
class ModelAvailability:
    """`review_mode` and `agent_available` for the run (10.7, 12.7 N2 and N5)."""

    review_mode: str = "live"
    agent_available: bool = True
    recoveries: int = 0

    @classmethod
    def from_preflight(cls, n2_ok: bool, n5_ok: bool) -> "ModelAvailability":
        ok = n2_ok and n5_ok
        return cls("live" if ok else "deferred", ok)

    def recheck(self, endpoint_ok: bool) -> bool:
        """The once-per-flow re-check; True when it logs a recovery."""
        if endpoint_ok and not self.agent_available:
            self.review_mode, self.agent_available = "live", True
            self.recoveries += 1
            return True
        return False


def model_preflight(n2_ok: bool, n5_ok: bool, *, blocking_control: str | None = None) -> ControlResult:
    """N2 or N5 failing: a degraded mode at `provisioned`, never `env_error`; the run continues (F29)."""
    if n2_ok and n5_ok:
        return ControlResult("model_preflight", True, "provisioned")
    findings = (FindingSpec("hardening", f"model egress blocked by {blocking_control}"),) if not n5_ok and \
        blocking_control else ()
    return ControlResult("model_preflight", True, "provisioned", alert=alerts.model_degraded(), findings=findings,
                         detail="review deferred, agent fallback and the Explorer disabled; report unreviewed")


def verdict_outcome(avail: ModelAvailability) -> Outcome | None:
    """The outcome of an item that needs a verdict while review is deferred (written with `review_state: pending`)."""
    return Outcome("needs_human", "reviewer_unavailable") if avail.review_mode == "deferred" else None


def locator_failed(avail: ModelAvailability, step_id: str) -> ControlResult | None:
    """A scripted locator failure with the agent unavailable; None when agent fallback may run (5.5)."""
    if avail.agent_available:
        return None
    return ControlResult("degraded_model", False, "running", step="failed",
                         checkpoint=Outcome("landmark_missing", f"locator:{step_id}"),
                         outcomes=(SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{step_id}")),),
                         findings=(FindingSpec("locator_failure", "scripted locator failed; agent fallback disabled"),),
                         actions=("end_flow",))


def agent_only_step(avail: ModelAvailability) -> Outcome | None:
    """An agent-only step (no scripted rung) is skipped while the agent is unavailable (F45)."""
    return None if avail.agent_available else Outcome("not_run", "model_unavailable")


# -- spend ceilings -------------------------------------------------------------------------------------
@dataclass
class SpendTracker:
    """Sums the usage of every model response of the run against the per-run and weekly ceilings (11.4)."""

    per_run: float = PER_RUN_USD
    per_week: float = PER_WEEK_USD
    run_usd: float = 0.0
    week_usd: float = 0.0

    @property
    def calls_allowed(self) -> bool:
        return self.run_usd < self.per_run and self.week_usd < self.per_week

    def add(self, usd: float) -> ControlResult | None:
        """Count one response; a result when a ceiling is reached by it."""
        before_run, before_week = self.run_usd, self.week_usd
        self.run_usd += usd
        self.week_usd += usd
        if before_run < self.per_run <= self.run_usd:
            return ControlResult("spend", False, "running", actions=("stop_model_calls",), alert=alerts.model_degraded(),
                                 detail="per-run model spend ceiling reached: the run ends at the next flow boundary")
        if before_week < self.per_week <= self.week_usd:
            return ControlResult("spend", True, "running", actions=("stop_model_calls",), alert=alerts.model_degraded(),
                                 outcomes=(SubsetOutcome("pending_review", Outcome("needs_human", "reviewer_cap",
                                                                                   detail="cause: dollar_cap")),),
                                 detail="weekly model spend ceiling reached: model calls stop, no run aborts")
        return None

    def at_flow_boundary(self) -> ControlResult | None:
        """At a flow boundary after the per-run ceiling: `aborted_budget (reason: dollar_cap)` (F30)."""
        if self.run_usd < self.per_run:
            return None
        run, un = budget_abort_outcome("dollar_cap")
        return ControlResult("spend", False, "running", run=run, unreached=un,
                             outcomes=(SubsetOutcome("uncaptured", Outcome("not_run", "budget")),
                                       SubsetOutcome("pending_review", Outcome("needs_human", "reviewer_cap",
                                                                               detail="cause: dollar_cap"))),
                             alert=alerts.aborted_budget("dollar_cap"), actions=("stop_model_calls", "teardown"),
                             detail="pending items go review_state done; teardown and the proof run as usual")


# -- network --------------------------------------------------------------------------------------------
class NetworkPause:
    """One mid-run connectivity loss, polled until it resolves (10.7 Network).

    The elapsed outage decides before the connection state does, so the outcome does not depend on how densely the
    Orchestrator polls: a reconnect first observed past 15 minutes still ends the shard, and one first observed
    between 5 and 15 minutes still fails the flow in progress, together with the resume at the next flow boundary."""

    def __init__(self, started: float):
        self.started = started
        self.flow_failed = False

    def poll(self, now: float, *, connected: bool) -> ControlResult | None:
        """None while waiting; else what the elapsed outage decides."""
        elapsed = now - self.started
        reverify = ("reassert_paper_mode", "light_fixture_check")
        if elapsed > NETWORK_SHARD_S:
            return ControlResult("network", False, "connectivity", run=RunVerdict("complete_with_env_errors"),
                                 unreached=Outcome("env_error", "gate", "connectivity"), alert=alerts.env_error(),
                                 detail=f"network {'back' if connected else 'down'} after {elapsed:.0f} s, past 15 "
                                        "minutes: the shard's remaining checkpoints are env_error",
                                 evidence={"pause_s": elapsed})
        if elapsed > NETWORK_RETRY_S and not self.flow_failed:
            self.flow_failed = True
            o = Outcome("env_error", "gate", "connectivity")
            subset = (SubsetOutcome("in_progress", o), SubsetOutcome("rest_of_flow", o))
            if connected:
                return ControlResult("network", False, "connectivity", checkpoint=o, outcomes=subset,
                                     actions=reverify + ("resume_next_flow",), alert=alerts.env_error(),
                                     detail=f"network back after {elapsed:.0f} s, past 5 minutes: the flow is "
                                            "env_error; resume at the next flow boundary",
                                     evidence={"pause_s": elapsed})
            return ControlResult("network", False, "connectivity", checkpoint=o, outcomes=subset,
                                 actions=("pause_capture",), alert=alerts.env_error(),
                                 detail="network down past 5 minutes: the flow is env_error", evidence={"pause_s": elapsed})
        if connected:
            if not self.flow_failed:
                return ControlResult("network", True, "connectivity", actions=reverify + ("retry_step",),
                                     detail=f"network back after {elapsed:.0f} s: retry the step once",
                                     evidence={"pause_s": elapsed})
            return ControlResult("network", True, "connectivity", actions=reverify + ("resume_next_flow",),
                                 detail=f"network back after {elapsed:.0f} s", evidence={"pause_s": elapsed})
        return None


# -- rig ------------------------------------------------------------------------------------------------
def rig_silent(silence_s: float) -> bool:
    """Three minutes without a Rig Manager heartbeat is a lost rig (10.7, 5.3)."""
    return silence_s >= RIG_SILENCE_S


def rig_lost(previous_losses: int, *, budget_allows: bool, same_class_rig: bool) -> ControlResult:
    """A lost rig for one shard (10.7 Rig)."""
    if previous_losses >= 1:
        run, un = harness_abort_outcome("rig_lost")
        return ControlResult("rig_loss", False, "rig_lost", run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail="second loss of the same shard: a fleet or harness fault")
    if budget_allows and same_class_rig:
        return ControlResult("rig_loss", False, "rig_lost", actions=("requeue_shard",),
                             detail="shard re-queued once, in full, to a rig of the same fingerprint class")
    return ControlResult("rig_loss", False, "rig_lost", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "gate", "rig_lost"), alert=alerts.env_error(),
                         detail="no budget or rig for the re-queue")
