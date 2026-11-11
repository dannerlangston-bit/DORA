"""The control host's dead-man intake and the monthly chaos self-test (10.2 Dead-man, two layers; 5.13, 5.14, 9.10,
10.7, 11.6; 1.4 G5).

The dead-man has two layers (10.2). The local layer is the Watchdog: at the deadline plus grace it kills the
Orchestrator and the tree and runs `orchestrator --resume <run_id> --finalize` (`watchdog_loop`, `resume.finalize`).
The central layer is the Reporter service on the control host. Its timer and its row writer already exist and are not
duplicated here: `service.deadman.DeadmanTimer` watches each run from its run-started event (deadline from
`watchdog.dead_man_deadline`), learns `running` from the scope manifest, stops at the run-completed event and keeps the
rigs' heartbeats; `service.deadman.fire` writes the run row through the writer (`aborted_harness (reason: dead_man)`
with `harness_error (reason: dead_man)` for every silent scoped checkpoint) and names unreachable rigs "rig
unreachable, run data intact on rig". This module is what the central layer still lacked, built around one shared
timer:

- `DeadmanIntake.intake(event)`: one typed stream, `RunTriggered`, `RunQueued`, `RunStarted`, `ScopeUploaded`,
  `RigHeartbeat`, `RunCompleted`. The last four reach the timer (the service's endpoints may feed it directly
  instead; the intake reads the same timer). Triggers and `run_queued` are the intake's own.
- `DeadmanIntake.not_started(now)`: a trigger followed by neither `run_started` nor `run_queued` within 15 minutes
  raises the dead-man alert as "run not started" (`start_deadline.run_not_started`: P1, P2 for PR runs), once per run;
  the run then takes its start-deadline outcome from the run queue (11.2).
- `DeadmanIntake.due(now)`: the timer's overdue runs that reached `running`, the only runs the central layer closes
  (10.2: "covers only a run that reached running"; "a run that never reached running is never dead_man").
- `DeadmanIntake.dead_man_alert(fired, now)`: the `report.alerting.Alert` for a run `service.deadman.fire` closed,
  naming the run, the rig and the last heartbeat received (10.2), with "rig unreachable, run data intact on rig" when
  a rig is gone (10.7 Loss of the central store).
- The monthly self-test (10.2, G5): `chaos_due` says when, `chaos_stall` plans the stall of a test Nightly run on
  rig 6 with a shortened deadline (`ChaosStall.started` is its run-started event, the deadline as the Nightly cutoff the
  timer takes), `observe_chaos` reads what the central layer produced, and `evaluate_chaos` decides: the dead-man alert
  and the `aborted_harness (reason: dead_man)` row both within 15 minutes of the deadline, or `harness_error (reason:
  chaos_unobserved)` on the self-test row and a P1. `record_chaos` writes the verdict to run health (G5: "recorded in
  run health") and the outcome on the self-test row.

Choices where the spec is ambiguous or silent (reported):

1. 10.2 says a stall "that produces neither" alert nor row is `chaos_unobserved`; G5 asks for both, so either one
   missing or later than the deadline plus 15 minutes is `chaos_unobserved`, its detail naming what was missing.
2. The P1 of `chaos_unobserved` is the 9.10 `harness_error` row at P1 with the harness on-call paged (9.10 has no row
   of its own; P1 pages the harness on-call).
3. The alert's time is the summary alert entry's `raised_at` when it has one, else the dead-man row's `finished_at`:
   `service.deadman.fire` writes the alert entry and the row in one batch.
4. The self-test row is a checkpoint of the stalled run's scope the Rig Manager names (`ChaosStall.checkpoint_id`); with
   none, or once the run is finished, the verdict goes to run health and the alert only.
5. The self-test is due once per America/New_York calendar month; its shortened deadline defaults to 30 minutes after
   `run_started`.
6. A run that reached `running` and whose rig never sent a heartbeat is named with "last heartbeat: none".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Union

from ..report.alerting import Alert, RunRef
from ..service.deadman import LOST_DETAIL, DeadmanTimer, Fired, Watch
from ..store.store import Store
from . import alerts
from .results import ControlResult, Outcome
from .resume import silent_record
from .start_deadline import NEW_YORK, NOT_STARTED, run_not_started

CHAOS_RIG = "rig-06"                              # 10.2: rig 6, the Explorer rig and pool spare (7.1)
CHAOS_WINDOW = timedelta(minutes=15)              # 10.2: alert and row within 15 minutes of the deadline
CHAOS_DEADLINE = timedelta(minutes=30)            # choice 5: the shortened deadline after run_started
UNOBSERVED = "chaos_unobserved"


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _iso(at: datetime | None) -> str:
    return "none" if at is None else _aware(at).astimezone(timezone.utc).isoformat(timespec="seconds")


# -- the events -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RunTriggered:
    """A run was triggered (a PR push, a merge, an RC build, the Nightly or Calibration timer, 11.2)."""

    run_id: str
    run_type: str
    at: datetime


@dataclass(frozen=True)
class RunQueued:
    """The `run_queued` event with the queue position, every 60 s while the run waits (10.2, 11.2)."""

    run_id: str
    at: datetime
    position: int = 1


@dataclass(frozen=True)
class RunStarted:
    """The `run_started` event: the run type, the run deadline and the rigs of its shards; `nightly_cutoff` overrides
    the 06:00 dead-man of a Nightly run (the chaos self-test's shortened deadline)."""

    run_id: str
    run_type: str
    at: datetime
    deadline: datetime
    shards: Mapping[str, str] = field(default_factory=dict)
    nightly_cutoff: datetime | None = None


@dataclass(frozen=True)
class ScopeUploaded:
    """The scope manifest arrived at `running` entry (5.12): from now on a silence past the deadline is dead_man."""

    run_id: str
    at: datetime


@dataclass(frozen=True)
class RigHeartbeat:
    """A rig heartbeat summary (5.1, 10.2)."""

    rig_id: str
    at: datetime


@dataclass(frozen=True)
class RunCompleted:
    """The `run_completed` event."""

    run_id: str
    at: datetime


DeadmanEvent = Union[RunTriggered, RunQueued, RunStarted, ScopeUploaded, RigHeartbeat, RunCompleted]


@dataclass
class _Trigger:
    run_type: str
    triggered_at: datetime
    last_signal: datetime | None = None
    alerted: bool = False


# -- the intake -------------------------------------------------------------------------------------------------
class DeadmanIntake:
    """The central dead-man's event intake around the Reporter service's timer (module docstring)."""

    def __init__(self, timer: DeadmanTimer | None = None, *, base_url: str = "https://sentinel.internal"):
        self.timer = timer or DeadmanTimer()
        self.base_url = base_url
        self.triggers: dict[str, _Trigger] = {}

    def intake(self, event: DeadmanEvent) -> Watch | None:
        """Take one event; the timer's watch for a run-started event, else None."""
        if isinstance(event, RunTriggered):
            self.triggers.setdefault(event.run_id, _Trigger(event.run_type, _aware(event.at)))
            return None
        if isinstance(event, RunQueued):
            self._signal(event.run_id, event.at)
            return None
        if isinstance(event, RunStarted):
            self._signal(event.run_id, event.at)
            return self.timer.run_started(event.run_id, event.run_type, event.deadline, shards=dict(event.shards),
                                          nightly_cutoff=event.nightly_cutoff)
        if isinstance(event, ScopeUploaded):
            self.timer.reached_running(event.run_id)
        elif isinstance(event, RigHeartbeat):
            self.timer.heartbeat(event.rig_id, event.at)
        elif isinstance(event, RunCompleted):
            self.timer.completed(event.run_id)
        else:
            raise TypeError(f"not a dead-man event: {event!r}")
        return None

    def _signal(self, run_id: str, at: datetime) -> None:
        t = self.triggers.get(run_id)
        if t is not None and (t.last_signal is None or t.last_signal < _aware(at)):
            t.last_signal = _aware(at)

    def not_started(self, now: datetime) -> list[Alert]:
        """"Run not started" for each trigger with neither `run_started` nor `run_queued` within 15 minutes, once per
        run (10.2, 9.10). A run the timer already watches (its run-started event reached the service) has started."""
        out = []
        for run_id, t in self.triggers.items():
            if t.alerted or run_id in self.timer.watches:
                continue
            route = run_not_started(t.run_type, t.triggered_at, t.last_signal, _aware(now))
            if route is None:
                continue
            t.alerted = True
            out.append(Alert.of(route, key=f"dead_man:{run_id}:not_started",
                                detail=f"run not started: triggered {_iso(t.triggered_at)}, neither run_started nor "
                                       f"run_queued within {int(NOT_STARTED.total_seconds() // 60)} minutes",
                                run=RunRef(run_id, t.run_type, base_url=self.base_url), status="run not started"))
        return out

    def due(self, now: datetime) -> list[Watch]:
        """The overdue runs the central layer closes: past the dead-man deadline with no run-completed event, and
        reached `running` (module docstring)."""
        return [w for w in self.timer.due(now) if w.reached_running]

    def dead_man_alert(self, fired: Fired, now: datetime) -> Alert | None:
        """The dead-man alert for a run `service.deadman.fire` closed, naming the run, its rigs and the last heartbeat
        (10.2, choice 6); None when the fire wrote nothing (the run had already finished)."""
        w = self.timer.watches.get(fired.run_id)
        if w is None or fired.outcome is None:
            return None
        rigs = ", ".join(sorted(set(w.shards.values()))) or "unknown"
        detail = (f"no run_completed by {_iso(w.deadline)}; rig {rigs}; last heartbeat: "
                  f"{_iso(self.timer.last_heartbeat(w))}")
        if fired.lost:
            detail += f"; {LOST_DETAIL} (shards {', '.join(fired.lost)})"
        status = f"{fired.outcome} (reason: {fired.reason})" if fired.reason else fired.outcome
        return Alert.of(alerts.dead_man(w.run_type), key=f"dead_man:{w.run_id}", detail=detail,
                        run=RunRef(w.run_id, w.run_type, base_url=self.base_url), status=status)


# -- the monthly chaos self-test --------------------------------------------------------------------------------
@dataclass(frozen=True)
class ChaosStall:
    """One deliberate stall (10.2): a test Nightly run on rig 6, stalled after `run_started` (Orchestrator suspended,
    heartbeats and `run_completed` withheld) with a shortened `deadline`. `checkpoint_id` is its self-test row
    (choice 4)."""

    run_id: str
    started_at: datetime
    deadline: datetime
    shard_id: str
    rig_id: str = CHAOS_RIG
    checkpoint_id: str | None = None

    def started(self) -> RunStarted:
        """The run-started event the intake takes: the shortened deadline is the Nightly cutoff of the timer."""
        return RunStarted(self.run_id, "nightly", self.started_at, self.deadline, {self.shard_id: self.rig_id},
                          nightly_cutoff=self.deadline)


def chaos_due(last_stall_at: datetime | None, now: datetime) -> bool:
    """True when no self-test ran in the current America/New_York calendar month (choice 5)."""
    if last_stall_at is None:
        return True
    a, b = _aware(last_stall_at).astimezone(NEW_YORK), _aware(now).astimezone(NEW_YORK)
    return (a.year, a.month) != (b.year, b.month)


def chaos_stall(run_id: str, started_at: datetime, *, shard_id: str | None = None, checkpoint_id: str | None = None,
                after: timedelta = CHAOS_DEADLINE) -> ChaosStall:
    """Plan the stall of a test Nightly run started at `started_at`: its deadline `after` later (choice 5)."""
    return ChaosStall(run_id, _aware(started_at), _aware(started_at) + after, shard_id or f"{run_id}/1",
                      checkpoint_id=checkpoint_id)


@dataclass(frozen=True)
class ChaosObservation:
    """What the central layer produced for the stalled run: when the dead-man alert was raised and when the
    `aborted_harness (reason: dead_man)` row was written; None for what never came."""

    alert_at: datetime | None
    row_at: datetime | None


def observe_chaos(store: Store, run_id: str) -> ChaosObservation:
    """Read the stalled run's row and alert entries from the Central Store (choice 3)."""
    run = store.query("SELECT outcome, abort_reason, finished_at, summary_json FROM runs WHERE run_id = ?", (run_id,))
    if not run or run[0]["outcome"] != "aborted_harness" or run[0]["abort_reason"] != "dead_man":
        row_at = None
    else:
        row_at = datetime.fromisoformat(run[0]["finished_at"])
    alert_at = None
    if run:
        entries = [a for a in (store.run_summary(run_id).get("alerts") or []) if a.get("rule") == "dead_man"]
        if entries:
            raised = entries[0].get("raised_at")
            alert_at = datetime.fromisoformat(raised) if raised else row_at
    return ChaosObservation(alert_at, row_at)


@dataclass(frozen=True)
class ChaosVerdict:
    """The self-test's verdict: observed, or `harness_error (reason: chaos_unobserved)` with its P1 (`result`)."""

    run_id: str
    observed: bool
    deadline: datetime
    alert_at: datetime | None
    row_at: datetime | None
    detail: str
    result: ControlResult | None = None

    def health(self) -> dict[str, Any]:
        """The run-health entry (G5: the monthly stall is recorded in run health)."""
        return {"observed": self.observed, "deadline": _iso(self.deadline), "alert_at": _iso(self.alert_at),
                "row_at": _iso(self.row_at), "detail": self.detail}


def evaluate_chaos(stall: ChaosStall, seen: ChaosObservation, now: datetime) -> ChaosVerdict | None:
    """The verdict at `now`: observed when both the alert and the row came by the deadline plus 15 minutes; None while
    that time has not passed and something is still missing; else `chaos_unobserved` (choice 1)."""
    limit = stall.deadline + CHAOS_WINDOW
    alert_ok = seen.alert_at is not None and _aware(seen.alert_at) <= limit
    row_ok = seen.row_at is not None and _aware(seen.row_at) <= limit
    if alert_ok and row_ok:
        return ChaosVerdict(stall.run_id, True, stall.deadline, seen.alert_at, seen.row_at,
                            "dead-man alert and dead_man row within 15 minutes of the deadline")
    if _aware(now) < limit:
        return None
    missing = [what for what, ok in (("dead-man alert", alert_ok), ("dead_man row", row_ok)) if not ok]
    detail = f"stall of {stall.run_id} on {stall.rig_id}: no {' and no '.join(missing)} by {_iso(limit)}"
    route = alerts.harness_error(repeat=True).plus(paged=("harness_on_call",))
    result = ControlResult("chaos_self_test", False, "running", checkpoint=Outcome("harness_error", UNOBSERVED,
                                                                                    detail=detail),
                           alert=route, detail=detail)
    return ChaosVerdict(stall.run_id, False, stall.deadline, seen.alert_at, seen.row_at, detail, result)


def record_chaos(store: Store, stall: ChaosStall, verdict: ChaosVerdict) -> bool:
    """Write the verdict to the stalled run's health and, for `chaos_unobserved`, the outcome on its self-test row while
    the run is open and the row silent (choice 4). True when the outcome row was written."""
    health = store.run_summary(stall.run_id).get("health") or {}
    store.update_summary(stall.run_id, {"health": {**health, "chaos_self_test": verdict.health()}})
    if verdict.result is None or stall.checkpoint_id is None:
        return False
    run = store.query("SELECT lifecycle FROM runs WHERE run_id = ?", (stall.run_id,))
    scoped = store.query("SELECT 1 FROM run_scope WHERE run_id = ? AND checkpoint_id = ?",
                         (stall.run_id, stall.checkpoint_id))
    o = verdict.result.checkpoint
    if o is None or not run or run[0]["lifecycle"] == "finished" or not scoped or \
            store.get_outcome(stall.run_id, stall.checkpoint_id):
        return False
    store.write_checkpoint(silent_record(store, stall.run_id, stall.checkpoint_id, o), shard_id=stall.shard_id)
    return True
