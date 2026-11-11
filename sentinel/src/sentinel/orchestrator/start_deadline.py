"""Start deadlines and their outcomes (11.2 Queue and saturation, 10.2 Dead-man, 7.2 failure table row 3, 9.10, 11.6).

A run that never reached `running` is never `dead_man` (10.2). While it waits it emits `run_queued` every 60 s; at
its start deadline it takes the outcome of its run type:

- PR: 45 min from the trigger (one budget), measured from 06:00 for a run triggered outside the operating day.
  `aborted_budget (reason: pool_saturated)`, the abort comment "Sentinel did not run: pool saturated, N runs ahead;
  push again or re-run the build job", check `sentinel/pr-run` `error` "Sentinel did not run", and the hourly P2
  "pool saturated" naming the queue depth.
- Merge: 3 h from the trigger. Not aborted: it joins the next merge run (coalescing) and the report names the range.
- RC: the G-N sign-off time minus 8 h. Not aborted: P2 to the release manager and the system owner (release and
  on-call channels); the release manager decides (9.12).
- Nightly: 02:00 America/New_York. `aborted_harness (reason: start_failed)`, every scoped checkpoint `harness_error
  (reason: start_failed)`, P1 to the harness on-call, the banner "canaries pending: nightly run {run_id} did not
  start".
- Calibration: Saturday 02:00 (weekend), 01:00 (emergency). As the Nightly, and the run is queued again for the next
  `all_quiet` window (the next night for an emergency calibration).

The Nightly and Calibration outcome applies to a run that has not reached `running` by the deadline whether it is still
queued or already on a rig in its gate sequence: on a rig the Orchestrator writes it (and tears down), otherwise the
Reporter service does (11.2). The PR, merge and RC deadlines apply to a run still queued.

`outcome` decides; `deadline` computes the deadline of a run type from its trigger; `start_failed` is the Nightly and
Calibration abort alone (the Orchestrator rebuilds it with the gate it is in; the dead-man finalize reuses it);
`run_not_started` is the central dead-man's "run not started" alert (a trigger followed by neither `run_started` nor
`run_queued` within 15 minutes, 10.2, 9.10). The banner texts of 9.2 are `canaries_pending_banner`, `skipped_reason`
and `not_started_reason`; the value of the `canaries_pending` `system_state` key (8.3) is the reason text, empty when
no banner is due.

Choices (reported):

1. Local wall times are America/New_York; a deadline that falls in a DST gap or fold is resolved with `fold=0` (the
   02:00 of a spring-forward night is the instant 02:00 EST, 03:00 EDT).
2. A deadline is reached when `now >= deadline`; a PR run that could start exactly at its deadline is aborted, so the
   outcome never depends on the order of two events at the same instant.
3. "N runs ahead" (11.2, 9.2) counts the runs queued ahead of the PR run plus the runs holding pool rigs: the runs
   the developer waits for.
4. An RC run without a known sign-off time (`signoff_at` None: no release train policy loaded) has no start deadline.
5. The alert routes 9.10 and 11.6 give but `alerts.py` lacks (pool saturated, Nightly skipped for RC, RC start
   deadline or superseded) are built here from `alerts.AlertRoute`. 11.6 sends the RC row to the release channel
   too; `alerts.RECIPIENTS` has no release channel, so the route names the release manager, the system owner and the
   on-call channel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import alerts
from .alerts import AlertRoute
from .results import ControlResult, budget_abort_outcome, harness_abort_outcome

NEW_YORK = ZoneInfo("America/New_York")
RUN_TYPES = ("pr", "merge", "rc", "nightly", "calibration")
CALIBRATION_KINDS = ("weekend", "emergency")
DISPOSITIONS = ("abort", "coalesce", "release_manager")
PR_START = timedelta(minutes=45)                    # 11.2: one budget
MERGE_START = timedelta(hours=3)                    # 11.2
RC_SIGNOFF_LEAD = timedelta(hours=8)                # 11.2: the G-N sign-off time minus 8 hours (its budget)
NIGHTLY_START_DEADLINE = time(2, 0)                 # 11.2: its 4-hour budget before the 06:00 dead-man
WEEKEND_CALIBRATION_START_DEADLINE = time(2, 0)     # 11.2: Saturday 02:00
EMERGENCY_CALIBRATION_START_DEADLINE = time(1, 0)   # 11.2: five passes of about 5 hours end by 06:00
SATURDAY = 5
NOT_STARTED = timedelta(minutes=15)                 # 10.2, 9.10: neither run_started nor run_queued
POOL_SATURATED_TEXT = "Sentinel did not run: pool saturated, {n} ahead; push again or re-run the build job"
CHECK_TEXT = "Sentinel did not run"                 # 9.2: check `error` for pool_saturated
CANARIES_PENDING = "canaries pending: {reason}"     # 9.2 banner


def to_local(at: datetime) -> datetime:
    """`at` in America/New_York; `at` must be aware."""
    if at.tzinfo is None:
        raise ValueError(f"naive datetime {at.isoformat()}: start deadlines take aware times")
    return at.astimezone(NEW_YORK)


def first_local(at: datetime, wall: time, *, weekday: int | None = None, inclusive: bool = False) -> datetime:
    """The first America/New_York wall time `wall` (on `weekday`, Monday 0, when given) after `at`, or at `at` when
    `inclusive`; returned in UTC (choice 1 for DST)."""
    day = to_local(at).date()
    for i in range(9):
        d = day + timedelta(days=i)
        if weekday is not None and d.weekday() != weekday:
            continue
        cand = datetime.combine(d, wall, tzinfo=NEW_YORK).astimezone(timezone.utc)
        if cand > at or (inclusive and cand == at):
            return cand
    raise AssertionError("unreachable: a weekday recurs within 8 days")


def deadline(run_type: str, triggered_at: datetime, *, signoff_at: datetime | None = None,
             calibration: str | None = None) -> datetime | None:
    """The start deadline of 11.2 for a run triggered at `triggered_at` (a timer run: the time it was due).

    PR and merge deadlines run from `triggered_at`; for a run triggered outside the operating day pass 06:00 (11.2:
    "its start deadline is measured from 06:00"); the queue also suspends a PR run's deadline during an RC
    reservation, so it computes those itself. RC: `signoff_at` minus 8 hours, None without a sign-off time (choice
    4). Nightly: the next 02:00. Calibration: the next Saturday 02:00 (`weekend`) or the next 01:00 (`emergency`)."""
    to_local(triggered_at)
    if run_type == "pr":
        return triggered_at + PR_START
    if run_type == "merge":
        return triggered_at + MERGE_START
    if run_type == "rc":
        return None if signoff_at is None else to_local(signoff_at).astimezone(timezone.utc) - RC_SIGNOFF_LEAD
    if run_type == "nightly":
        return first_local(triggered_at, NIGHTLY_START_DEADLINE)
    if run_type == "calibration":
        if calibration == "weekend":
            return first_local(triggered_at, WEEKEND_CALIBRATION_START_DEADLINE, weekday=SATURDAY)
        if calibration == "emergency":
            return first_local(triggered_at, EMERGENCY_CALIBRATION_START_DEADLINE)
        raise ValueError(f"calibration kind {calibration!r}: one of {CALIBRATION_KINDS}")
    raise ValueError(f"run type {run_type!r}: one of {RUN_TYPES}")


# -- alert routes (9.10, 11.6; choice 5) -----------------------------------------------------------------------
def pool_saturated_alert() -> AlertRoute:
    """"Pool saturated": queue depth above 3 PR runs, a PR run waiting over 20 min outside an RC reservation, or a PR
    run `aborted_budget (reason: pool_saturated)`; P2 to the on-call channel, one per hour (9.10, 11.6)."""
    return AlertRoute("pool_saturated", alerts.P2, (), (alerts.ON_CALL_CHANNEL,))


def nightly_skipped_alert() -> AlertRoute:
    """"Nightly skipped for RC": P2 to the harness on-call and the system owner (9.10, 11.6)."""
    return AlertRoute("nightly_skipped_for_rc", alerts.P2, (), ("harness_on_call", "system_owner",
                                                               alerts.ON_CALL_CHANNEL))


def rc_start_deadline_alert() -> AlertRoute:
    """"RC start deadline": an RC run past its start deadline or superseded by a newer RC build; P2 to the release
    manager and the system owner (9.10, 11.6; choice 5)."""
    return AlertRoute("rc_start_deadline", alerts.P2, (), ("release_manager", "system_owner", alerts.ON_CALL_CHANNEL))


# -- banners (9.2, 11.2) ---------------------------------------------------------------------------------------
def skipped_reason(rc_run_id: str) -> str:
    """The `canaries_pending` reason after an RC pre-empted the Nightly run (11.2)."""
    return f"last Nightly skipped for RC run {rc_run_id}"


def not_started_reason(run_type: str, run_id: str) -> str:
    """The `canaries_pending` reason after a Nightly or Calibration run missed its start deadline (11.2)."""
    return f"{run_type} run {run_id} did not start"


def canaries_pending_banner(value: str | None) -> str | None:
    """The banner line of 9.2 from the `canaries_pending` `system_state` value; None when the value is empty."""
    return CANARIES_PENDING.format(reason=value) if value else None


def pool_saturated_text(runs_ahead: int) -> str:
    """The PR abort text of 11.2 and 9.2."""
    n = f"{runs_ahead} run" if runs_ahead == 1 else f"{runs_ahead} runs"
    return POOL_SATURATED_TEXT.format(n=n)


# -- the decision ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    """What a run takes at its start deadline (module table).

    `disposition`: `abort` (the run row ends with `result.run` and every scoped checkpoint without an outcome gets
    `result.unreached`, through `results.write_unreached`), `coalesce` (a merge run stays queued and joins the next
    merge run) or `release_manager` (an RC run stays queued; the release manager decides). `writer` is who writes
    the abort: `orchestrator` when the run is on a rig, else `reporter` (11.2). `comment` and `check` are the PR
    comment abort text and the `sentinel/pr-run` status; `abort` holds the abort-block facts the Reporter reads from
    `runs.summary_json` (`runs_ahead`, `queue_depth`); `state` the `system_state` writes; `requeue` asks for the next
    `all_quiet` window (Calibration)."""

    run_id: str
    run_type: str
    deadline: datetime
    disposition: str
    result: ControlResult | None = None
    alert: AlertRoute | None = None
    writer: str = "reporter"
    comment: str | None = None
    check: tuple[str, str] | None = None
    abort: dict[str, Any] = field(default_factory=dict)
    state: tuple[tuple[str, str], ...] = ()
    requeue: bool = False

    def __post_init__(self):
        if self.disposition not in DISPOSITIONS:
            raise ValueError(f"disposition {self.disposition!r}")
        if self.writer not in ("orchestrator", "reporter"):
            raise ValueError(f"writer {self.writer!r}")


def outcome(run_type: str, queued_since: datetime, now: datetime, *, run_id: str, deadline_at: datetime | None = None,
            signoff_at: datetime | None = None, calibration: str | None = None, runs_ahead: int = 0,
            queue_depth: int = 0, state: str = "provisioned", on_rig: bool = False) -> Decision | None:
    """The start-deadline outcome of a run that has not started (PR, merge, RC) or not reached `running` (Nightly,
    Calibration) at `now`; None before its deadline or when it has none.

    `queued_since` is the trigger (06:00 for a PR or merge run triggered outside the operating day); `deadline_at`
    overrides the computed deadline (the queue passes the PR deadline it moved for suspensions). `runs_ahead` and
    `queue_depth` are the PR abort's facts (choice 3); `state` is the gate in force and `on_rig` whether the run is
    on a rig (Nightly, Calibration)."""
    due = deadline_at if deadline_at is not None else deadline(run_type, queued_since, signoff_at=signoff_at,
                                                               calibration=calibration)
    if due is None or now < due:
        return None
    if run_type == "pr":
        run, un = budget_abort_outcome("pool_saturated")
        text = pool_saturated_text(runs_ahead)
        res = ControlResult("start_deadline", False, "provisioned", run=run, unreached=un,
                            alert=pool_saturated_alert(), detail=f"{text}; queue depth {queue_depth}")
        return Decision(run_id, run_type, due, "abort", res, res.alert, comment=text, check=("error", CHECK_TEXT),
                        abort={"runs_ahead": runs_ahead, "queue_depth": queue_depth})
    if run_type == "merge":
        return Decision(run_id, run_type, due, "coalesce")
    if run_type == "rc":
        return Decision(run_id, run_type, due, "release_manager", alert=rc_start_deadline_alert())
    if run_type == "calibration" and calibration not in CALIBRATION_KINDS:
        raise ValueError(f"calibration kind {calibration!r}: one of {CALIBRATION_KINDS}")
    res = start_failed(run_type, state=state, on_rig=on_rig,
                       detail=f"not at running by the start deadline {due.isoformat()}")
    return Decision(run_id, run_type, due, "abort", res, res.alert, writer="orchestrator" if on_rig else "reporter",
                    state=(("canaries_pending", not_started_reason(run_type, run_id)),),
                    requeue=run_type == "calibration")


def start_failed(run_type: str, *, state: str = "provisioned", on_rig: bool = False,
                 detail: str | None = None) -> ControlResult:
    """The Nightly or Calibration start-deadline abort (11.2): run `aborted_harness (reason: start_failed)`, every
    scoped checkpoint without an outcome `harness_error (reason: start_failed)`, P1 to the harness on-call (9.10);
    `teardown` when the run is on a rig, `queue_calibration` for a Calibration run."""
    if run_type not in ("nightly", "calibration"):
        raise ValueError(f"start_failed is a Nightly or Calibration outcome, not {run_type!r} (11.2)")
    run, un = harness_abort_outcome("start_failed")
    actions = (("teardown",) if on_rig else ()) + (("queue_calibration",) if run_type == "calibration" else ())
    return ControlResult("start_deadline", False, state, run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=actions, detail=detail or "not at running by the start deadline")


def run_not_started(run_type: str, triggered_at: datetime, last_signal_at: datetime | None,
                    now: datetime) -> AlertRoute | None:
    """The central dead-man's "run not started" (10.2, 9.10): a trigger followed by neither `run_started` nor
    `run_queued` within 15 minutes raises the dead-man alert (P1, P2 for PR runs). `last_signal_at` is the latest
    `run_started` or `run_queued` received for the run, None when none arrived."""
    if last_signal_at is not None and last_signal_at >= triggered_at:
        return None
    if now - triggered_at < NOT_STARTED:
        return None
    return alerts.dead_man(run_type)
