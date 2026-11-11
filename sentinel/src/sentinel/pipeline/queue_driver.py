"""Demo runs through the control host's run queue (11.2, 10.2, 9.10; `orchestrator.queue.RunQueue`).

`QueueDriver(store, clock, start=..., windows=..., state_path=...)` is the control host's loop around the pure queue:
`submit(event)` hands it a webhook or rig event, `tick()` advances it, and every output is delivered here:

- `RunStart`: the run through `start(request, rigs)` (the simulated run: `run.run_pipeline`), then `RunReachedRunning`
  and `RunFinished(run_id, status)` back into the queue, as the runs report them;
- `StateWrite`: `store.set_system_state` (`canaries_pending`, `nightly_skipped_for`, 8.3), which a later run's
  summary carries for the Reporter's banner (`run.run_pipeline` writes `summary_json["canaries_pending"]`);
- `DeadlineReached` with `abort`: the run row the Reporter writes for a run that never started (11.2 Start deadline):
  `aborted_budget (pool_saturated)` or `aborted_harness (start_failed)` with the abort-block facts (`runs_ahead`,
  `queue_depth`) in `summary_json["abort"]`, the PR comment and check text in `deadline_comments`;
- `Alert`: converted with `report.alerting.Alert.of(route, detail=, key=<run>:<rule>)` (9.10) and kept in `alerts`
  (the Dispatcher of the run that follows sends them; the queue's alerts are not a run's own);
- every other output (`RunQueued`, `Placeholder`, `Replaced`, `Coalesced`, `Scheduled`, `ReservationEnded`,
  `NightlySkipped`, `Supersede`) is kept in `log` for the summary and the tests.

Persistence across control-host restarts (choice, 11.2): a JSON-lines journal of every event the queue handled, with
the clock time it was handled at, under the control host's state directory (`state_path`). `QueueDriver.restore`
replays the journal into a fresh queue on a `ManualClock` stepped to each recorded time, discarding the outputs
(they were delivered before the restart), then ticks to the driver's clock. This needs no 8.3 change: `system_state`
is a closed set of keys and the queue's entries, consumed deadline time and reservations are derived state, which the
replay rebuilds exactly because `RunQueue` is a pure function of its events and the clock.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..calendar import WindowsConfig, is_operating_time, next_all_quiet
from ..orchestrator import queue as q
from ..orchestrator.clock import ManualClock
from ..orchestrator.results import write_unreached
from ..records import RunOutcome
from ..report.alerting import Alert
from ..store import Store

EVENTS = {cls.__name__: cls for cls in (q.PrBuild, q.MergeBuild, q.RcBuild, q.CalibrationRequest, q.RunReachedRunning,
                                        q.PoolReleased, q.RunFinished, q.Requeue, q.RigState)}
StartFn = Callable[[q.RunRequest, Sequence[int]], str]          # runs it; returns the run outcome status


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, Mapping):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def event_line(event: Any, at: datetime, run_ids: Sequence[str] = ()) -> str:
    """One journal line: the event's type and fields, the clock time it was handled at and the run ids the queue
    named while handling it (a replay names its runs the same)."""
    return json.dumps({"at": at.isoformat(), "type": type(event).__name__, "run_ids": list(run_ids),
                       "fields": _encode(dataclasses.asdict(event))}, sort_keys=True)


def read_event(line: str) -> tuple[datetime, Any, list[str]]:
    d = json.loads(line)
    return datetime.fromisoformat(d["at"]), EVENTS[d["type"]](**_decode(d["fields"])), list(d.get("run_ids") or ())


class QueueDriver:
    """The control host's loop around one pool's `RunQueue` (module docstring)."""

    def __init__(self, store: Store, clock: ManualClock, *, start: StartFn, cfg_root: Path,
                 windows: WindowsConfig | None = None, state_path: Path | None = None,
                 run_ids: Callable[[str], str] | None = None):
        self.store, self.clock, self.start_fn, self.cfg_root = store, clock, start, Path(cfg_root)
        self.windows, self.state_path = windows, state_path
        self._run_ids = run_ids or (lambda run_type: f"{run_type}-{len(self._issued_all) + 1}")
        self._issued: list[str] = []                      # the run ids named while handling the current event
        self._issued_all: list[str] = []
        self._replaying: list[str] | None = None
        self.queue = self._fresh(clock)
        self.alerts: list[Alert] = []
        self.log: list[Any] = []
        self.deadline_comments: dict[str, tuple[str | None, tuple[str, str] | None]] = {}
        self.finished: dict[str, str] = {}

    def _fresh(self, clock: Any) -> q.RunQueue:
        w = self.windows
        return q.RunQueue(clock, operating_day=(lambda t: is_operating_time(t, w, "pr")) if w else
                          q.fixed_operating_day,
                          next_all_quiet=(lambda t: next_all_quiet(t, w).start) if w else q.friday_all_quiet,
                          run_ids=self._name,
                          canaries_pending=self.store.get_system_state("canaries_pending") or "",
                          nightly_skipped_for=self.store.get_system_state("nightly_skipped_for") or "")

    def _name(self, run_type: str) -> str:
        """A new run's id: the recorded one during a replay, else a fresh one (kept for the journal)."""
        rid = self._replaying.pop(0) if self._replaying else self._run_ids(run_type)
        self._issued.append(rid)
        self._issued_all.append(rid)
        return rid

    # -- the loop ---------------------------------------------------------------------------------------------
    def submit(self, event: Any) -> list[Any]:
        """Hand the queue one event, journal it with the run ids it named (module docstring), deliver the outputs."""
        self._issued = []
        outputs = self.queue.handle(event)
        if self.state_path is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_path, "a", encoding="utf-8") as f:
                f.write(event_line(event, self.clock.now(), self._issued) + "\n")
        return self.deliver(outputs)

    def tick(self) -> list[Any]:
        return self.deliver(self.queue.tick())

    def advance(self, seconds: float, step: float = 60.0) -> None:
        """Let `seconds` pass, ticking at least every 60 s as the control host does (11.2)."""
        left = seconds
        while left > 0:
            self.clock.advance(min(step, left))
            left -= step
            self.tick()

    def deliver(self, outputs: Sequence[Any]) -> list[Any]:
        """Deliver each output (module docstring); returns them."""
        for out in outputs:
            if isinstance(out, q.StateWrite):
                self.store.set_system_state(out.key, out.value, at=out.at.isoformat())
            elif isinstance(out, q.Alert):
                rid = out.run_id or "queue"
                self.alerts.append(Alert.of(out.route, detail=out.detail, key=f"{rid}:{out.route.rule}"))
            elif isinstance(out, q.DeadlineReached):
                self._deadline(out)
            elif isinstance(out, q.RunStart):
                self._run(out)
            self.log.append(out)
        return list(outputs)

    def _run(self, out: q.RunStart) -> None:
        rid = out.request.run_id
        self.queue.handle(q.RunReachedRunning(rid))
        status = self.start_fn(out.request, out.rigs)
        self.finished[rid] = status
        self.deliver(self.queue.handle(q.RunFinished(rid, status)))

    def _deadline(self, out: q.DeadlineReached) -> None:
        """11.2: a run past its start deadline. An `abort` the Reporter writes is the run row of a run that never
        started, its abort block and its unreached scope (none: nothing was scoped); others stay queued."""
        d = out.decision
        if d.disposition != "abort" or d.result is None or d.result.run is None or d.writer != "reporter":
            return
        from .run import _run_plan                        # the run row's plan, as every simulated run writes it
        at = out.at.isoformat()
        req = out.request
        self.store.begin_run(dataclasses.replace(_run_plan(req.run_id, req.run_type, self.cfg_root, (), (),
                                                           req.triggered_at.isoformat(), None, req.pr, None)))
        write_unreached(self.store, req.run_id, d.result)
        r = d.result
        self.store.end_run(RunOutcome(req.run_id, req.run_type, r.run.status, r.gate or "queued", reason=r.run.reason,
                                      started_at=req.triggered_at.isoformat(), ended_at=at),
                           summary={"abort": dict(d.abort), "start_deadline": {"comment": d.comment,
                                                                                "check": list(d.check or ())}})
        self.deadline_comments[req.run_id] = (d.comment, d.check)
        self.finished[req.run_id] = r.run.status

    # -- persistence (module docstring) -----------------------------------------------------------------------
    def restore(self) -> int:
        """Rebuild the queue from the journal after a control-host restart; returns how many events it replayed."""
        if self.state_path is None or not self.state_path.exists():
            return 0
        events = [read_event(line) for line in self.state_path.read_text(encoding="utf-8").splitlines() if line]
        if not events:
            return 0
        replay = ManualClock(start=events[0][0])
        self.queue = self._fresh(replay)
        for at, ev, ids in events:
            replay.advance(max(0.0, (at - replay.now()).total_seconds()))
            self._replaying = list(ids)
            self.queue.handle(ev)
        self._replaying = None
        self.queue.clock = self.clock
        self.tick()
        return len(events)
