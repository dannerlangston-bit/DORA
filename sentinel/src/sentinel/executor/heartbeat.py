"""The heartbeat's cadence and content, and the app's age tags (10.2 Heartbeats, both directions; 5.2; 5.4.10).

The Orchestrator writes `heartbeat.json` atomically at every step start and end, every gate, and at least every
10 s during long waits (10.2). `HeartbeatPolicy` owns when and what:

- `gate(state)`, `step_start(...)` and `step_end(...)` write at once; `tick()` writes when 10 s have passed since the
  last write, and `sleep(seconds)` waits in slices of at most 10 s with a tick after each, so a long wait (a settle
  loop, a `wait_for`, a post-condition poll) never leaves the file older than 10 s.
- `expect_screen_change()` is called when the Executor emits an input that should change the screen and
  `screen_settled(sha)` when the settle rule accepts a capture; the heartbeat carries `expect_screen_change_since`
  between the two, and `last_capture_sha256` after (10.2, the Watchdog's screenshot-staleness detector).
- `secret_step()` holds `capture_suspended: true` for the length of a secret step, so the Watchdog takes no
  screenshot while a credential is typed (5.4.10, 10.2).
- the app, Orchestrator and hook host PIDs and the checkpoint's `max_wait_s` ride along on every write.
- a write that fails twice (the writer's one retry after 1 s, `watchdog.HeartbeatWriter`) is returned once as
  `watchdog.heartbeat_failed`'s result for the Orchestrator to apply, and nothing is written after it: a failed
  heartbeat write is a harness failure, never a retry loop (10.2).

`hang_detected(heartbeat, ...)` is the Watchdog's sampling rule 5 over a heartbeat as written, so a caller reads the
expectation and the max wait from the file rather than inventing its own (`expect_since_mono` turns the wall
`expect_screen_change_since` back into the monotonic clock through the heartbeat's own `ts` and `mono_ts` pair).

`AppLife` is the app's age for one shard: uptime since the last launch and the number of relaunches, which tag every
capture and every step row (10.2 Proactive relaunch: "Every capture is tagged with app uptime and restart count").

Choices where the spec is ambiguous (reported):

1. `expect_screen_change_since` takes the time of the latest screen-changing input: the detector asks whether the
   screen moved after the input it is waiting on. A pure pointer move (`park_mouse`) is not screen-changing; every
   other emitted step is. A passing `wait_for` clears the expectation as an accepted capture does, since its anchor
   check is the settle rule's own (4.7, 3.5).
2. The restart count counts every relaunch of the app in the shard (crash, hang, hook host fail-closed, environment
   re-login and the proactive relaunch alike): the tag describes the process the capture came from, not a budget.
   Uptime restarts at each relaunch.
3. After a failed write the policy writes nothing more, ENOSPC included: the ENOSPC result asks the Watchdog for its
   immediate free-space check, which decides `disk_full` (10.2, 8.9).
4. `max_wait_s` absent from a heartbeat reads as 0 in `hang_detected`, so the 60 s floor of sampling rule 5 applies.
5. The expectation is kept as a monotonic time and written, at each write, as the wall-clock ISO string 10.2 names:
   `ts` less the monotonic seconds since the input. A wall-clock correction between the input and a write (a time
   sync step) then moves `ts` and the expectation together, so it can neither fake nor hide a hang (10.2: "`mono_ts`
   is the monotonic clock, so a wall-clock correction cannot fake or hide staleness"); the string is the wall time of
   the input only while the wall clock ran evenly.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..orchestrator.clock import Clock
from ..orchestrator.results import ControlResult
from ..orchestrator.watchdog import Heartbeat, HeartbeatUnwritable, HeartbeatWriter, edt_frozen, heartbeat_failed

PERIOD_S = 10.0                  # 10.2: at least every 10 s during long waits


@dataclass
class AppLife:
    """The app's age in one shard (10.2): when it was last launched and how often it was relaunched."""

    launched_mono: float | None = None
    restarts: int = 0
    app_pid: int | None = None

    def launched(self, now: float, *, pid: int | None = None) -> None:
        """The shard's first launch passed `launched` at `now` (monotonic seconds)."""
        self.launched_mono, self.app_pid = now, pid

    def relaunched(self, now: float, *, pid: int | None = None) -> None:
        """A relaunch of any cause (choice 2): the count goes up and uptime starts again."""
        self.restarts += 1
        self.launched_mono, self.app_pid = now, pid

    def uptime_s(self, now: float) -> int | None:
        """Whole seconds since the last launch; None before the first."""
        return None if self.launched_mono is None else max(0, int(now - self.launched_mono))

    def tags(self, now: float) -> tuple[int | None, int]:
        """`(app_uptime_s, app_restart_count)` for a capture, a step row or a step event (10.2, 8.3)."""
        return self.uptime_s(now), self.restarts


def expect_since_mono(hb: Heartbeat) -> float | None:
    """The monotonic time of `expect_screen_change_since`, from the heartbeat's own `ts` and `mono_ts` pair."""
    if hb.expect_screen_change_since is None:
        return None
    lag = (datetime.fromisoformat(hb.ts) - datetime.fromisoformat(hb.expect_screen_change_since)).total_seconds()
    return hb.mono_ts - lag


def hang_detected(hb: Heartbeat, *, now: float, last_hash_change: float, anti_anchor: bool) -> bool:
    """10.2 sampling rule 5 read from a heartbeat as written: the expectation, the max wait (choice 4) and
    `capture_suspended` come from the file; `now` and `last_hash_change` are the Watchdog's monotonic times."""
    return edt_frozen(now=now, expect_change_since=expect_since_mono(hb), last_hash_change=last_hash_change,
                      max_wait_s=hb.max_wait_s or 0.0, anti_anchor=anti_anchor, capture_suspended=hb.capture_suspended)


def read_heartbeat(path: str | Path) -> Heartbeat:
    """The heartbeat file as the Watchdog reads it."""
    return Heartbeat(**json.loads(Path(path).read_text(encoding="utf-8")))


class HeartbeatPolicy:
    """When the heartbeat is written and what it carries (module docstring). `writer` None keeps the bookkeeping
    without a file (a caller that writes elsewhere)."""

    def __init__(self, writer: HeartbeatWriter | None, clock: Clock, *, run_id: str, shard: int, attempt: int = 1,
                 orchestrator_pid: int | None = None, hook_host_pid: int | None = None,
                 life: AppLife | None = None, period_s: float = PERIOD_S):
        if period_s <= 0:
            raise ValueError("the heartbeat period is positive")
        self.writer, self.clock = writer, clock
        self.run_id, self.shard, self.attempt = run_id, shard, attempt
        self.orchestrator_pid, self.hook_host_pid = orchestrator_pid, hook_host_pid
        self.life = life if life is not None else AppLife()
        self.period_s = period_s
        self.state = "running"
        self.flow_id: str | None = None
        self.step_id: str | None = None
        self.checkpoint_id: str | None = None
        self.max_wait_s: float | None = None
        self.expect_mono: float | None = None     # the monotonic time of the latest screen-changing input
        self.capture_suspended = False
        self.last_capture_sha256: str | None = None
        self.last: Heartbeat | None = None
        self.last_write_mono: float | None = None
        self.failed: ControlResult | None = None
        self.writes = 0

    # -- what the file says -------------------------------------------------------------------------------------
    def heartbeat(self) -> Heartbeat:
        """The heartbeat as it would be written now. `expect_screen_change_since` is the wall time `ts` less the
        monotonic time since the input, so `mono_ts - (ts - expect_screen_change_since)` is the input's monotonic
        time whatever the wall clock did meanwhile (choice 5)."""
        now, mono = self.clock.now(), self.clock.monotonic()
        since = None if self.expect_mono is None else (now - timedelta(seconds=mono - self.expect_mono)).isoformat()
        return Heartbeat(self.run_id, self.shard, self.attempt, self.state, now.isoformat(), mono,
                         flow_id=self.flow_id, step_id=self.step_id, checkpoint_id=self.checkpoint_id,
                         app_pid=self.life.app_pid, orchestrator_pid=self.orchestrator_pid,
                         hook_host_pid=self.hook_host_pid, expect_screen_change_since=since,
                         max_wait_s=self.max_wait_s, capture_suspended=self.capture_suspended,
                         last_capture_sha256=self.last_capture_sha256)

    def write(self) -> ControlResult | None:
        """Write now; the first failure's result once, then nothing (choice 3)."""
        if self.failed is not None:
            return None
        hb = self.heartbeat()
        if self.writer is not None:
            try:
                self.writer.write(hb)
            except HeartbeatUnwritable as e:
                self.failed = heartbeat_failed(e)
                return self.failed
        self.last, self.last_write_mono = hb, hb.mono_ts
        self.writes += 1
        return None

    # -- the cadence ----------------------------------------------------------------------------------------------
    def gate(self, state: str) -> ControlResult | None:
        """A gate: the state changes and no step is in progress."""
        self.state, self.flow_id, self.step_id, self.checkpoint_id = state, None, None, None
        return self.write()

    def step_start(self, flow_id: str | None, step_id: str | None, *, checkpoint_id: str | None = None,
                   max_wait_s: float | None = None) -> ControlResult | None:
        """A step starts: its flow, step, the checkpoint it captures (if any) and that checkpoint's max wait."""
        self.state = "running"
        self.flow_id, self.step_id, self.checkpoint_id = flow_id, step_id, checkpoint_id
        if max_wait_s is not None:
            self.max_wait_s = max_wait_s
        return self.write()

    def step_end(self) -> ControlResult | None:
        """The step in progress ended (the ids stay until the next step starts)."""
        return self.write()

    def tick(self) -> ControlResult | None:
        """Write when the last write is `period_s` old or older."""
        if self.last_write_mono is not None and self.clock.monotonic() - self.last_write_mono < self.period_s:
            return None
        return self.write()

    def sleep(self, seconds: float) -> ControlResult | None:
        """Wait `seconds` in slices of at most `period_s`, ticking after each; the first failure, if any."""
        failure = None
        left = seconds
        while left > 0:
            step = min(left, self.period_s)
            self.clock.sleep(step)
            left -= step
            failure = failure or self.tick()
        return failure

    # -- content --------------------------------------------------------------------------------------------------
    def expect_screen_change(self) -> None:
        """A screen-changing input was emitted now (choice 1), kept on the monotonic clock (choice 5)."""
        self.expect_mono = self.clock.monotonic()

    def screen_settled(self, capture_sha256: str | None = None) -> None:
        """The settle rule accepted a capture (or a `wait_for` passed): the expectation is cleared."""
        self.expect_mono = None
        if capture_sha256 is not None:
            self.last_capture_sha256 = capture_sha256

    @contextmanager
    def secret_step(self) -> Iterator[None]:
        """`capture_suspended: true` while a secret step types (5.4.10): writes made inside carry it, so the caller
        writes the step's start inside the block and its end after it."""
        self.capture_suspended = True
        try:
            yield
        finally:
            self.capture_suspended = False

    def set_pids(self, *, hook_host_pid: int | None = None, orchestrator_pid: int | None = None) -> None:
        """A hook host or Orchestrator restart changed a PID (the app's comes from `life`)."""
        if hook_host_pid is not None:
            self.hook_host_pid = hook_host_pid
        if orchestrator_pid is not None:
            self.orchestrator_pid = orchestrator_pid

    def hang_detected(self, *, now: float, last_hash_change: float, anti_anchor: bool) -> bool:
        """`hang_detected` over the last heartbeat written; False before the first write."""
        if self.last is None:
            return False
        return hang_detected(self.last, now=now, last_hash_change=last_hash_change, anti_anchor=anti_anchor)
