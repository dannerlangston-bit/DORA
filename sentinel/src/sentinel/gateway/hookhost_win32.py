"""The gateway hook host's Win32 side (5.4.2, 5.4.4, 5.18): `hookhost_loop.HookHostOS` over `SetWindowsHookExW`.

Run as its own process (`python -m sentinel.gateway.hookhost_win32 <segment name>`); it imports only ctypes and the
standard library (with the stdlib-only `gateway.segment`, `gateway.hookhost_loop` and `win32.ffi`). Its first
statement after `import sys` declares `PER_MONITOR_AWARE_V2`, so hook coordinates are the slow half's physical
pixels, and `Win32HookHostOS.set_dpi_awareness` confirms it when `HookHost.run` starts.

- `Win32HookHostOS.install_hooks` installs `WH_MOUSE_LL` and `WH_KEYBOARD_LL` from the calling thread (the loop's
  thread, raised one priority step above normal) and keeps the two `HOOKPROC`s alive.
- The hook procedures (`HookProcs`) read the `MSLLHOOKSTRUCT`/`KBDLLHOOKSTRUCT` at `lParam`, translate it into a
  `segment.HookEvent` (`from_mouse_hook`, `from_key_hook`: pure) and allow it only when the loop's callback, which
  decides through `segment.decide_from_segment` and nothing else, says so: allow is `CallNextHookEx`, drop is a
  nonzero return.
- The origin (`origin_of`): not injected is `console`; injected (`LLMHF_INJECTED`, `LLKHF_INJECTED`, or their
  lower-integrity variants) is `emitter` or `drill` by the `dwExtraInfo` tag (`ffi.EMITTER_TAG`, `ffi.DRILL_TAG`),
  else `foreign_process` (`segment` choice 5).
- `pump` is the message loop slice: `MsgWaitForMultipleObjects` on all input, then `PeekMessageW` until empty; the
  low-level hook callbacks run inside it.
- `pid_alive` holds a handle to each polled pid, so a reused pid never revives a dead Orchestrator; `exit_signalled`
  waits on the Watchdog's named event (`WATCHDOG_EVENT`, created here if the Watchdog has not created it yet).

Choices (reported):

1. Modifiers: `HookEvent.mods` are the modifiers held when the event arrives, as the hook itself tracks them
   (`Modifiers`): a modifier key's down that the hook allowed until its allowed up, by virtual key, so the left and
   right keys are counted apart. A modifier's own event does not count itself, as the planner writes its events
   (`executor.primitives`); a dropped modifier event changes nothing, since the app never saw it.
2. An event the hook cannot classify (`WM_MOUSEHWHEEL`, an X button with no button id, a message outside the
   tables) and any exception in a hook procedure drop the event: returning 0 without `CallNextHookEx` would pass it.
3. A key event carries no point (x = y = 0): the hook decision reads coordinates only for mouse events.
4. `WATCHDOG_EVENT` is `Local\\SentinelHookHostExit` unless `--exit-event` names another; the Watchdog sets it after
   killing the thinkorswim tree (5.4.2).

Verified here: the translation of every hook struct and message, the origin and modifier rules, the parity of the
procedures with `decide_from_segment` on a seeded corpus, and the facade's calls under the real `HookHost` loop with
a fake table. Only Phase 0 on a rig verifies the hooks themselves: H2 (both hooks receive events, callback latency
under 20 ms and under 1 ms at p99.9), H4 (the deny self-test), that the injected flag and the tag arrive as set,
that a removed hook is reported by the probe, and the fire drill items 1 to 4 (5.4.4, 6.5, 12.7).
"""
import sys

if sys.platform == "win32":                    # 5.4.2: PER_MONITOR_AWARE_V2 before anything else
    import ctypes as _dpi
    _dpi.WinDLL("user32").SetProcessDpiAwarenessContext(_dpi.c_void_p(-4))

import argparse  # noqa: E402
import ctypes  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

from ..win32 import ffi  # noqa: E402
from . import hookhost_loop  # noqa: E402
from . import segment as sg  # noqa: E402

WH_KEYBOARD_LL, WH_MOUSE_LL, HC_ACTION = 13, 14, 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEHWHEEL = 0x0200, 0x020A, 0x020B, 0x020C, 0x020E
#: mouse message -> (event type, button); X buttons take theirs from the high word of mouseData
MOUSE_MESSAGES = {WM_MOUSEMOVE: ("move", None), 0x0201: ("button_down", "left"), 0x0202: ("button_up", "left"),
                  0x0204: ("button_down", "right"), 0x0205: ("button_up", "right"),
                  0x0207: ("button_down", "middle"), 0x0208: ("button_up", "middle"), WM_MOUSEWHEEL: ("wheel", None),
                  WM_XBUTTONDOWN: ("button_down", None), WM_XBUTTONUP: ("button_up", None)}
KEY_MESSAGES = {WM_KEYDOWN: "key_down", WM_SYSKEYDOWN: "key_down", WM_KEYUP: "key_up", WM_SYSKEYUP: "key_up"}
X_BUTTONS = {1: "x1", 2: "x2"}
LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02
LLKHF_LOWER_IL_INJECTED, LLKHF_INJECTED = 0x02, 0x10
QS_ALLINPUT, PM_REMOVE = 0x04FF, 0x0001
THREAD_PRIORITY_ABOVE_NORMAL = 1
SYNCHRONIZE, PROCESS_QUERY_LIMITED_INFORMATION = 0x0010_0000, 0x1000
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
WATCHDOG_EVENT = "Local\\SentinelHookHostExit"
DROP = 1
NEEDED = ("SetProcessDpiAwarenessContext", "GetThreadDpiAwarenessContext", "AreDpiAwarenessContextsEqual",
          "SetWindowsHookExW", "UnhookWindowsHookEx", "CallNextHookEx", "MsgWaitForMultipleObjects", "PeekMessageW",
          "TranslateMessage", "DispatchMessageW", "GetModuleHandleW", "GetCurrentThread", "SetThreadPriority",
          "OpenProcess", "WaitForSingleObject", "CloseHandle", "CreateEventW")


def origin_of(injected: bool, extra_info: int) -> str:
    """The event's origin from the injected flag and the `dwExtraInfo` tag (module docstring)."""
    if not injected:
        return "console"
    return {ffi.EMITTER_TAG: "emitter", ffi.DRILL_TAG: "drill"}.get(extra_info, "foreign_process")


def _hiword(v: int) -> int:
    return (v >> 16) & 0xFFFF


def from_mouse_hook(wparam: int, info: Any, mods: frozenset[str]) -> sg.HookEvent | None:
    """A low-level mouse hook's event, or None when the hook cannot classify it (choice 2)."""
    kind = MOUSE_MESSAGES.get(wparam)
    if kind is None:
        return None
    typ, button = kind
    if wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        button = X_BUTTONS.get(_hiword(info.mouseData))
        if button is None:
            return None
    delta = ctypes.c_int16(_hiword(info.mouseData)).value if typ == "wheel" else 0
    injected = bool(info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
    return sg.HookEvent(typ, info.pt.x, info.pt.y, button, None, delta, mods, origin_of(injected, info.dwExtraInfo))


def from_key_hook(wparam: int, info: Any, mods: frozenset[str]) -> sg.HookEvent | None:
    """A low-level keyboard hook's event, or None for a message outside the table (choices 2 and 3)."""
    typ = KEY_MESSAGES.get(wparam)
    if typ is None:
        return None
    injected = bool(info.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
    return sg.HookEvent(typ, vk=int(info.vkCode), mods=mods, origin=origin_of(injected, info.dwExtraInfo))


class Modifiers:
    """The modifiers held as the hook saw them pass (choice 1)."""

    def __init__(self) -> None:
        self.held: set[int] = set()

    def mods_for(self, vk: int | None) -> frozenset[str]:
        """The modifiers held at an event of virtual key `vk` (None for a mouse event), not counting `vk` itself."""
        return frozenset(sg.MODIFIER_VK[v] for v in self.held if v != vk)

    def passed(self, e: sg.HookEvent) -> None:
        """An allowed event: a modifier's down holds it, its up releases it."""
        if e.vk in sg.MODIFIER_VK:
            (self.held.add if e.type == "key_down" else self.held.discard)(e.vk)


class HookProcs:
    """The two `HOOKPROC` bodies over the loop's callback (`HookHost.callback`): True allows the event."""

    def __init__(self, api: Any, decide: Callable[[sg.HookEvent], bool]):
        self.api = api
        self.decide = decide
        self.mods = Modifiers()

    def mouse(self, n_code: int, wparam: int, lparam: int) -> int:
        if n_code != HC_ACTION:
            return self.api.CallNextHookEx(None, n_code, wparam, lparam)
        try:
            e = from_mouse_hook(wparam, ffi.MSLLHOOKSTRUCT.from_address(lparam), self.mods.mods_for(None))
            allowed = e is not None and self.decide(e)
        except Exception:                                  # choice 2
            allowed = False
        return self.api.CallNextHookEx(None, n_code, wparam, lparam) if allowed else DROP

    def keyboard(self, n_code: int, wparam: int, lparam: int) -> int:
        if n_code != HC_ACTION:
            return self.api.CallNextHookEx(None, n_code, wparam, lparam)
        try:
            info = ffi.KBDLLHOOKSTRUCT.from_address(lparam)
            e = from_key_hook(wparam, info, self.mods.mods_for(int(info.vkCode)))
            allowed = e is not None and self.decide(e)
            if allowed:
                self.mods.passed(e)
        except Exception:                                  # choice 2
            allowed = False
        return self.api.CallNextHookEx(None, n_code, wparam, lparam) if allowed else DROP


class Win32HookHostOS:
    """`hookhost_loop.HookHostOS` over Win32 (module docstring). `api` is the function table (`ffi.load` by default,
    Windows only)."""

    def __init__(self, api: Any = None, *, exit_event: str = WATCHDOG_EVENT, clock: Callable[[], int] | None = None):
        self.api = api if api is not None else ffi.load(NEEDED)
        self.exit_event = exit_event
        self.clock = clock or time.monotonic_ns
        self.procs: HookProcs | None = None
        self._callbacks: tuple[Any, Any] | None = None
        self._hooks: list[int] = []
        self._pids: dict[int, int] = {}
        self._event: int | None = None

    def set_dpi_awareness(self) -> None:
        """Declare PER_MONITOR_AWARE_V2; already declared (by the first statement) is fine, anything else raises."""
        v2 = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if self.api.SetProcessDpiAwarenessContext(v2):
            return
        if self.api.AreDpiAwarenessContextsEqual(self.api.GetThreadDpiAwarenessContext(), v2):
            return
        raise ffi.fail(self.api, "SetProcessDpiAwarenessContext")

    def monotonic_ns(self) -> int:
        return self.clock()

    def pid_alive(self, pid: int) -> bool:
        """True while the process exists: a handle opened at the first poll and waited on at each later one; a pid
        that cannot be opened for lack of rights exists, one that does not exist is gone."""
        if pid not in self._pids:
            h = self.api.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return int(self.api.get_last_error()) == ffi.ERROR_ACCESS_DENIED
            self._pids[pid] = h
        h = self._pids[pid]
        return self.api.WaitForSingleObject(h, 0) == ffi.WAIT_TIMEOUT

    def exit_signalled(self) -> bool:
        if self._event is None:
            self._event = self.api.CreateEventW(None, True, False, self.exit_event) or None
            if self._event is None:
                return False
        return self.api.WaitForSingleObject(self._event, 0) == ffi.WAIT_OBJECT_0

    def install_hooks(self, callback: Callable[[sg.EventLike], bool]) -> bool:
        self.procs = HookProcs(self.api, callback)
        self._callbacks = (self.api.HOOKPROC(self.procs.mouse), self.api.HOOKPROC(self.procs.keyboard))
        self.api.SetThreadPriority(self.api.GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL)
        return self._install()

    def _install(self) -> bool:
        if self._callbacks is None:
            return False
        module = self.api.GetModuleHandleW(None)
        mouse, keyboard = self._callbacks
        self._hooks = [self.api.SetWindowsHookExW(WH_MOUSE_LL, mouse, module, 0),
                       self.api.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard, module, 0)]
        if all(self._hooks):
            return True
        self.uninstall_hooks()
        return False

    def reinstall_hooks(self) -> bool:
        self.uninstall_hooks()
        return self._install()

    def uninstall_hooks(self) -> None:
        for h in self._hooks:
            if h:
                self.api.UnhookWindowsHookEx(h)
        self._hooks = []

    def pump(self, timeout_ms: int) -> None:
        self.api.MsgWaitForMultipleObjects(0, None, False, timeout_ms, QS_ALLINPUT)
        msg = ffi.MSG()
        while self.api.PeekMessageW(msg, None, 0, 0, PM_REMOVE):
            self.api.TranslateMessage(msg)
            self.api.DispatchMessageW(msg)

    def close(self) -> None:
        """Close the handles this facade opened (the process exits right after the loop)."""
        for h in [*self._pids.values(), self._event]:
            if h:
                self.api.CloseHandle(h)
        self._pids, self._event = {}, None


def main(argv: list[str] | None = None, api: Any = None) -> int:
    """The hook host process: attach to the Orchestrator's segment and run the loop; returns the exit code."""
    p = argparse.ArgumentParser(prog="sentinel-hookhost", description="the gateway hook host (5.4.2)")
    p.add_argument("segment", help="the shared segment's name")
    p.add_argument("--exit-event", default=WATCHDOG_EVENT, help="the Watchdog's named event (choice 4)")
    args = p.parse_args(argv)
    host_os = Win32HookHostOS(api, exit_event=args.exit_event)
    try:
        return hookhost_loop.main(args.segment, host_os)
    finally:
        host_os.close()


if __name__ == "__main__":
    sys.exit(main())
