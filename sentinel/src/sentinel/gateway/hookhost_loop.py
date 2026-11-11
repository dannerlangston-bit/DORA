"""The gateway hook host's control loop (5.4.2 fail-closed rules, 5.4.4 reinstall), behind an injectable OS facade.

The hook host is a separate stdlib-only process (5.4.2): it declares `PER_MONITOR_AWARE_V2` first, installs the
two low-level hooks on one thread running a message loop, and its callback decides each event from the shared
segment (`segment.decide_from_segment`). Everything here is platform-independent; the Win32 parts
(`SetProcessDpiAwarenessContext`, `SetWindowsHookEx`, `MsgWaitForMultipleObjects`, `OpenProcess`, the
Watchdog's named event) are the `HookHostOS` facade, which the Windows track implements.

`HookHost.run()`:

- calls `set_dpi_awareness` before anything else;
- on a `magic` or `layout_version` mismatch latches deny_all and returns `EXIT_LAYOUT_MISMATCH`, at start and
  on any later tick (the halves were built from different commits; the slow half ends the run `aborted_harness
  (reason: hook_lost)` without a relaunch, `watchdog.hook_host_died(layout_mismatch=True)`);
- stamps `hook_alive_mono_ts` every 250 ms and polls `orchestrator_pid` every 500 ms; a gone Orchestrator
  latches deny_all, and 60 s later, still with no Orchestrator, the loop returns `EXIT_ORPHANED`;
- returns `EXIT_WATCHDOG` when the Watchdog's named event is set (it has killed the thinkorswim tree);
- answers each reinstall request of the slow half (5.4.4): reinstall both hooks once, log a `hook_reinstalled`
  ring record (aux 1 on success, 0 on failure) and write the answer into the segment;
- uninstalls the hooks on every exit.

Choices (reported):

1. The deny_all the hook host "switches to" (5.4.2) is a latch in the hook host's own memory, ORed with the
   segment's `deny_all` on every event. The segment's `deny_all` belongs to the slow half's seqlocked writes,
   and after a layout mismatch the offset of any field but `magic` and `layout_version` is unknown, so the hook
   host never writes it. The latch never clears: after an Orchestrator death the Watchdog kills the tree and
   starts a fresh hook host (10.2, 6.6 step 3).
2. A callback that raises drops the event: an event the hook cannot judge never reaches the app.
3. A reinstall request made before this hook host started is not answered (`HookHost` starts from the request
   number it finds): it was meant for a hook host that no longer exists. An `orchestrator_pid` of 0 is a gone
   Orchestrator.
4. Exit codes: 0 the Watchdog's event, 3 layout mismatch (the "distinct code" of 5.4.2), 4 orphaned after 60 s
   in deny_all, 5 the hooks could not be installed.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from . import segment as sg

EXIT_WATCHDOG = 0
EXIT_LAYOUT_MISMATCH = 3
EXIT_ORPHANED = 4
EXIT_HOOK_INSTALL_FAILED = 5
ALIVE_STAMP_NS = 250_000_000           # 5.4.2: every 250 ms from the message loop
PID_POLL_NS = 500_000_000              # 5.4.2: orchestrator_pid every 500 ms
ORPHAN_EXIT_NS = 60_000_000_000        # 5.4.2: 60 s in deny_all with no Orchestrator
PUMP_MS = 50                           # the message loop's slice between housekeeping ticks


class HookHostOS(Protocol):
    """The Win32 calls of the hook host, injected so the loop runs and is tested anywhere."""

    def set_dpi_awareness(self) -> None:
        """Declare `PER_MONITOR_AWARE_V2`, so hook coordinates are the slow half's physical pixels (5.4.2)."""

    def monotonic_ns(self) -> int:
        """The monotonic clock the slow half also uses (`time.monotonic_ns`)."""

    def pid_alive(self, pid: int) -> bool:
        """True while the process exists."""

    def exit_signalled(self) -> bool:
        """True once the Watchdog has set its named event (the thinkorswim tree is gone)."""

    def install_hooks(self, callback: Callable[[sg.EventLike], bool]) -> bool:
        """Install `WH_MOUSE_LL` and `WH_KEYBOARD_LL`; the callback returns True to allow an event."""

    def reinstall_hooks(self) -> bool:
        """Remove and install both hooks again (5.4.4); True on success."""

    def uninstall_hooks(self) -> None:
        """Remove both hooks."""

    def pump(self, timeout_ms: int) -> None:
        """Run the message loop for up to `timeout_ms`; hook callbacks run inside it."""


class HookHost:
    """One hook host process over one segment (module docstring)."""

    def __init__(self, buf: Any, os: HookHostOS):
        self.buf = buf
        self.os = os
        self.deny_all = False                     # choice 1: the latch
        self.orphaned_since: int | None = None
        self._next_alive = 0
        self._next_poll = 0
        self._handled = 0

    def callback(self, e: sg.EventLike) -> bool:
        """The hook callback body: decide from the segment with the latch; True allows the event."""
        try:
            return sg.decide_from_segment(self.buf, e, self.os.monotonic_ns(), deny_all=self.deny_all).allowed
        except Exception:                         # choice 2
            return False

    def tick(self) -> int | None:
        """One housekeeping step between message-loop slices; an exit code ends the loop."""
        now = self.os.monotonic_ns()
        if not sg.layout_ok(self.buf):
            self.deny_all = True
            return EXIT_LAYOUT_MISMATCH
        if self.os.exit_signalled():
            return EXIT_WATCHDOG
        if now >= self._next_alive:
            sg.stamp_alive(self.buf, now)
            self._next_alive = _next(self._next_alive, ALIVE_STAMP_NS, now)
        if now >= self._next_poll:
            self._next_poll = _next(self._next_poll, PID_POLL_NS, now)
            pid = sg.read_header(self.buf).orchestrator_pid
            if self.orphaned_since is None and not (pid > 0 and self.os.pid_alive(pid)):
                self.deny_all = True
                self.orphaned_since = now
        if self.orphaned_since is not None and now - self.orphaned_since >= ORPHAN_EXIT_NS:
            return EXIT_ORPHANED
        request = sg.read_header(self.buf).reinstall_request
        if request != self._handled:
            ok = self.os.reinstall_hooks()
            sg.append_record(self.buf, sg.reinstall_record(now, ok))
            sg.answer_reinstall(self.buf, request, ok)
            self._handled = request
        return None

    def run(self) -> int:
        """The hook host's life: DPI awareness first, layout check, hooks, the loop, hooks removed on exit."""
        self.os.set_dpi_awareness()
        if not sg.layout_ok(self.buf):
            self.deny_all = True
            return EXIT_LAYOUT_MISMATCH
        self._handled = sg.read_header(self.buf).reinstall_request      # choice 3
        if not self.os.install_hooks(self.callback):
            return EXIT_HOOK_INSTALL_FAILED
        try:
            while True:
                code = self.tick()
                if code is not None:
                    return code
                self.os.pump(PUMP_MS)
        finally:
            self.os.uninstall_hooks()


def _next(scheduled: int, period: int, now: int) -> int:
    """The next deadline on a fixed schedule; after a stall, one period from now."""
    nxt = scheduled + period
    return nxt if nxt > now else now + period


def main(segment_name: str, os: HookHostOS) -> int:
    """The hook host process: attach to the Orchestrator's segment by name and run; returns the exit code."""
    seg = sg.Segment.attach(segment_name)
    try:
        return HookHost(seg.buf, os).run()
    finally:
        seg.close()
