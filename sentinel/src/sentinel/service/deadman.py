"""The Reporter service's dead-man timer: the central layer of the two-layer dead-man (5.13, 5.14, 10.2, 8.9, 9.10).

The service learns of a run from its run-started event (`run_started`: run type and run deadline), of its entry to
`running` from the scope manifest upload (`reached_running`, 5.12), of its end from the run-completed event
(`completed`) and of each rig from its heartbeat summary every 30 s (`heartbeat`, 5.1). `due(now)` lists the runs
whose dead-man deadline (`watchdog.dead_man_deadline`: PR budget plus 15 minutes, merge plus 30, RC plus 60, Nightly
and Calibration by 06:00 America/New_York) passed without a run-completed event, and `fire(store, watch, now)` closes
one through the writer: the outcome `watchdog.dead_man_finalize` decides (`aborted_harness (reason: dead_man)` with
`harness_error (reason: dead_man)` for every scoped checkpoint without an outcome when the run reached `running`;
the start-deadline outcome otherwise), the abort facts the 9.2 dead_man comment prints (deadline, last heartbeat) and
the dead-man alert (P1, P2 for PR runs) in the run summary.

`restore(store)` rebuilds the watches of the running runs after a service restart from the store alone: the run
deadline is `started_at + budget_sec` and "reached running" is a scope manifest recorded in the run summary
(`summary_json.scope_manifests`, written by the scope-manifest endpoint of `service.app`).

Choices where the spec is ambiguous or silent (reported to the coordinator):
- A rig is unreachable when its last heartbeat is older than 90 s (three missed 30 s beats, 5.1); at the deadline
  every shard on such a rig is named in the abort detail "rig unreachable, run data intact on rig" (8.9 Shard lost)
  and returned in `Fired.lost` for the import's `mark_shard_lost`; the timer itself changes no shard row.
- A run whose row is already finished at the deadline (its run-completed event was lost but the import ended it) is
  left as it is: the watch is closed and nothing is written.
- The deadline a Nightly run is watched by is the 06:00 cutoff `dead_man_deadline` computes from its run deadline;
  a Calibration run's from the scheduled end of its pass (SD-98), which the caller passes as the run deadline.
- `start_timer` drives `tick` from a daemon thread every 30 s; the service's `tick(now)` is what tests call.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..orchestrator.results import write_unreached
from ..orchestrator.watchdog import dead_man_deadline, dead_man_finalize
from ..records import RunOutcome
from ..store.store import Store

STALE_HEARTBEAT_S = 90             # three missed 30 s rig heartbeats (5.1): the rig is unreachable
TICK_S = 30                        # how often `start_timer` checks the deadlines
LOST_DETAIL = "rig unreachable, run data intact on rig"   # 8.9 Shard lost


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


@dataclass
class Watch:
    """One run the timer watches."""
    run_id: str
    run_type: str
    deadline: datetime                             # the dead-man deadline: run deadline plus grace (10.2)
    reached_running: bool = False                  # its scope manifest arrived (5.12): only then is it dead_man
    completed: bool = False                        # its run-completed event arrived
    fired: bool = False
    shards: dict[str, str] = field(default_factory=dict)   # shard id -> rig id


@dataclass(frozen=True)
class Fired:
    """What `fire` wrote for one run: its outcome and reason, the checkpoints filled, the shards on unreachable
    rigs and the alert entry."""
    run_id: str
    outcome: str | None
    reason: str | None
    filled: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    alert: dict | None = None


class DeadmanTimer:
    """Deadlines, running state, run-completed events and rig heartbeats of the runs in flight (5.14)."""

    def __init__(self, *, stale_after_s: int = STALE_HEARTBEAT_S):
        self.stale_after = timedelta(seconds=stale_after_s)
        self.watches: dict[str, Watch] = {}
        self.heartbeats: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def run_started(self, run_id: str, run_type: str, run_deadline: datetime, *,
                    shards: dict[str, str] | None = None, nightly_cutoff: datetime | None = None) -> Watch:
        """Watch a run from its run-started event; again for the same run keeps what the timer already knows."""
        deadline = dead_man_deadline(run_type, _aware(run_deadline), nightly_cutoff=nightly_cutoff)
        with self._lock:
            w = self.watches.get(run_id)
            if w is None:
                w = self.watches[run_id] = Watch(run_id, run_type, deadline)
            w.shards.update(shards or {})
            return w

    def reached_running(self, run_id: str) -> None:
        """The run's scope manifest arrived: from now on a silence past the deadline is `dead_man` (5.12, 5.14)."""
        with self._lock:
            if run_id in self.watches:
                self.watches[run_id].reached_running = True

    def completed(self, run_id: str) -> None:
        """The run-completed event arrived: the run is no longer watched."""
        with self._lock:
            if run_id in self.watches:
                self.watches[run_id].completed = True

    def heartbeat(self, rig_id: str, at: datetime) -> None:
        """A rig heartbeat summary (5.1, every 30 s)."""
        with self._lock:
            at = _aware(at)
            if rig_id not in self.heartbeats or self.heartbeats[rig_id] < at:
                self.heartbeats[rig_id] = at

    def due(self, now: datetime) -> list[Watch]:
        """The runs past their dead-man deadline with no run-completed event and not yet fired."""
        now = _aware(now)
        with self._lock:
            return [w for w in self.watches.values() if not (w.completed or w.fired) and now >= w.deadline]

    def last_heartbeat(self, w: Watch) -> datetime | None:
        """The newest heartbeat of any rig of the run (the 9.2 dead_man comment's "last heartbeat")."""
        beats = [self.heartbeats[r] for r in w.shards.values() if r in self.heartbeats]
        return max(beats) if beats else None

    def unreachable(self, w: Watch, now: datetime) -> tuple[str, ...]:
        """The run's shards whose rig has sent no heartbeat for longer than the stale limit."""
        now = _aware(now)
        return tuple(sorted(s for s, rig in w.shards.items()
                            if rig not in self.heartbeats or now - self.heartbeats[rig] > self.stale_after))

    def restore(self, store: Store) -> list[Watch]:
        """Watch every running run of the store again after a service restart (module docstring)."""
        rows = store.query("SELECT run_id, run_type, started_at, budget_sec, summary_json FROM runs "
                           "WHERE lifecycle = 'running' ORDER BY started_at")
        out = []
        for r in rows:
            shards = {s["shard_id"]: s["rig_id"] for s in store.query(
                "SELECT shard_id, rig_id FROM shards WHERE run_id = ?", (r["run_id"],))}
            deadline = datetime.fromisoformat(r["started_at"]) + timedelta(seconds=r["budget_sec"])
            w = self.run_started(r["run_id"], r["run_type"], deadline, shards=shards)
            if (json.loads(r["summary_json"] or "{}")).get("scope_manifests"):
                w.reached_running = True
            out.append(w)
        return out


def fire(store: Store, timer: DeadmanTimer, w: Watch, now: datetime) -> Fired:
    """Close one overdue run (5.14): fill the silent scoped checkpoints and end the run with the outcome
    `dead_man_finalize` decides, the abort facts and the alert entry in its summary. Runs on the writer."""
    now = _aware(now)
    w.fired = True
    run = store.query("SELECT lifecycle FROM runs WHERE run_id = ?", (w.run_id,))
    if not run or run[0]["lifecycle"] == "finished":
        return Fired(w.run_id, None, None)
    result = dead_man_finalize("running" if w.reached_running else "provisioned", w.reached_running,
                               run_type=w.run_type)
    lost = timer.unreachable(w, now) if w.reached_running else ()
    beat = timer.last_heartbeat(w)
    filled = write_unreached(store, w.run_id, result)
    abort = {"deadline": w.deadline.isoformat(timespec="seconds"),
             "last_heartbeat": beat.isoformat(timespec="seconds") if beat else "none"}
    if lost:
        abort["detail"] = LOST_DETAIL
        abort["lost_shards"] = list(lost)
    alert = None
    if result.alert is not None:
        alert = {"rule": result.alert.rule, "priority": result.alert.priority, "control": result.control,
                 "gate": result.gate, "detail": LOST_DETAIL if lost else result.detail,
                 "paged": list(result.alert.paged), "notified": list(result.alert.notified)}
    summary = store.run_summary(w.run_id)
    patch = {"abort": {**(summary.get("abort") or {}), **abort},
             "alerts": [*(summary.get("alerts") or []), *([alert] if alert else [])]}
    store.end_run(RunOutcome(w.run_id, w.run_type, result.run.status, result.gate or "running",
                             reason=result.run.reason, ended_at=now.isoformat(timespec="seconds")), summary=patch)
    return Fired(w.run_id, result.run.status, result.run.reason, tuple(filled), lost, alert)


def start_timer(tick: Callable[[datetime], object], *, interval_s: float = TICK_S,
                stop: threading.Event | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> threading.Thread:
    """Call `tick(now)` every `interval_s` seconds on a daemon thread until `stop` is set; returns the thread."""
    stop = stop or threading.Event()

    def loop() -> None:
        while not stop.wait(interval_s):
            tick(clock())

    t = threading.Thread(target=loop, name="sentinel-deadman", daemon=True)
    t.start()
    return t
