"""Tests for sentinel.gateway.hookhost_win32: the hook procedures over real hook structures, their parity with
segment.decide_from_segment, and the HookHostOS facade under the real HookHost loop with a fake function table."""
from __future__ import annotations

import ctypes
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from sentinel.gateway import hookhost_win32 as hh
from sentinel.gateway import segment as sg
from sentinel.gateway.hookhost_loop import (EXIT_HOOK_INSTALL_FAILED, EXIT_WATCHDOG, HookHost)
from sentinel.gateway.segment import HookEvent, RingReader, SegmentWriter, read_header
from sentinel.orchestrator import gateway as gw
from sentinel.orchestrator.gateway import Chord, Flag, GatewayState, InputEvent, Rect
from sentinel.win32 import ffi

SRC = Path(__file__).resolve().parents[1] / "src"
MS = 1_000_000
START = 1_000 * 1_000 * MS
T = START / 1e9
ORCH = 4242
EVENT = 0xE7E7
LLKHF_UP = 0x80
TAGS = {"emitter": ffi.EMITTER_TAG, "drill": ffi.DRILL_TAG, "foreign_process": 0x1234, "console": 0}
MOUSE_WPARAM = {("move", None): 0x200, ("button_down", "left"): 0x201, ("button_up", "left"): 0x202,
                ("button_down", "right"): 0x204, ("button_up", "right"): 0x205, ("button_down", "middle"): 0x207,
                ("button_up", "middle"): 0x208, ("wheel", None): 0x20A, ("button_down", "x1"): 0x20B,
                ("button_up", "x1"): 0x20C, ("button_down", "x2"): 0x20B, ("button_up", "x2"): 0x20C}


def raw(e: InputEvent):
    """The hook structure Windows hands the hook for event `e`: ('mouse' | 'key', wParam, structure)."""
    injected = e.origin != "console"
    if e.type.startswith("key"):
        info = ffi.KBDLLHOOKSTRUCT(vkCode=e.vk or 0, scanCode=0,
                                   flags=(hh.LLKHF_INJECTED if injected else 0) | (LLKHF_UP if e.type == "key_up"
                                                                                   else 0),
                                   dwExtraInfo=TAGS[e.origin])
        return "key", hh.WM_KEYDOWN if e.type == "key_down" else hh.WM_KEYUP, info
    data = 0
    if e.type == "wheel":
        data = (e.delta & 0xFFFF) << 16
    elif e.button in ("x1", "x2"):
        data = (1 if e.button == "x1" else 2) << 16
    info = ffi.MSLLHOOKSTRUCT(pt=ffi.POINT(e.x, e.y), mouseData=data, flags=hh.LLMHF_INJECTED if injected else 0,
                              dwExtraInfo=TAGS[e.origin])
    return "mouse", MOUSE_WPARAM[(e.type, e.button)], info


class FakeApi:
    """The hook host's Win32 functions: a clock the message wait advances, a process table, the Watchdog's event and
    scripted raw events delivered through the installed hook procedures."""

    HOOKPROC = staticmethod(lambda f: f)

    def __init__(self, *, hook_ok=(True, True), dpi_set=True, dpi_equal=True):
        self.now = START
        self.calls: list = []
        self.hooks: dict[int, tuple[int, object]] = {}
        self._next = 100
        self.hook_ok = list(hook_ok)
        self.dpi_set, self.dpi_equal = dpi_set, dpi_equal
        self.alive = {ORCH}
        self.generation: dict[int, int] = {}             # a pid's process object: a reused pid is a new one
        self.denied: set[int] = set()
        self.event_set = False
        self.error = 0
        self.at: list[tuple[int, object]] = []
        self.results: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def clock(self):
        return self.now

    def get_last_error(self):
        return self.error

    def SetProcessDpiAwarenessContext(self, ctx):
        self.calls.append(("dpi", ctx.value))
        return self.dpi_set

    def GetThreadDpiAwarenessContext(self):
        return 0x22

    def AreDpiAwarenessContextsEqual(self, a, b):
        return self.dpi_equal

    def GetModuleHandleW(self, name):
        return 0x400000

    def GetCurrentThread(self):
        return -2

    def SetThreadPriority(self, h, prio):
        self.calls.append(("priority", prio))
        return 1

    def SetWindowsHookExW(self, kind, proc, module, tid):
        ok = self.hook_ok.pop(0) if self.hook_ok else True
        self.calls.append(("hook", kind, ok))
        if not ok:
            return None
        self._next += 1
        self.hooks[self._next] = (kind, proc)
        return self._next

    def UnhookWindowsHookEx(self, h):
        self.calls.append(("unhook", self.hooks.pop(h)[0]))
        return 1

    def CallNextHookEx(self, hook, n, w, l):
        return 0

    def MsgWaitForMultipleObjects(self, n, handles, wait_all, timeout, mask):
        assert mask == hh.QS_ALLINPUT and self.now < START + 120_000 * MS
        self.now += timeout * MS
        due = [a for a in self.at if a[0] <= self.now]
        self.at = [a for a in self.at if a[0] > self.now]
        for _, action in due:
            action(self)
        return 0

    def PeekMessageW(self, msg, hwnd, lo, hi, flags):
        return 0

    def OpenProcess(self, access, inherit, pid):
        assert access == hh.SYNCHRONIZE | hh.PROCESS_QUERY_LIMITED_INFORMATION
        if pid in self.denied:
            self.error = ffi.ERROR_ACCESS_DENIED
            return None
        if pid not in self.alive:
            self.error = ffi.ERROR_INVALID_PARAMETER
            return None
        return handle(pid, self.generation.get(pid, 0))

    def WaitForSingleObject(self, h, ms):
        if h == EVENT:
            return ffi.WAIT_OBJECT_0 if self.event_set else ffi.WAIT_TIMEOUT
        pid, gen = divmod(h - 0x10000, 256)
        return ffi.WAIT_TIMEOUT if pid in self.alive and self.generation.get(pid, 0) == gen else ffi.WAIT_OBJECT_0

    def CreateEventW(self, attrs, manual, initial, name):
        assert (manual, initial, name) == (True, False, hh.WATCHDOG_EVENT)
        return EVENT

    def CloseHandle(self, h):
        self.closed.append(h)
        return 1

    def fire(self, e: InputEvent) -> int:
        kind, wparam, info = raw(e)
        want = hh.WH_MOUSE_LL if kind == "mouse" else hh.WH_KEYBOARD_LL
        (proc,) = [p for k, p in self.hooks.values() if k == want]
        result = proc(hh.HC_ACTION, wparam, ctypes.addressof(info))
        self.results.append((self.now, result))
        return result


def handle(pid: int, gen: int) -> int:
    return 0x10000 + pid * 256 + gen


def segment(*, armed=True) -> bytearray:
    buf = bytearray(sg.SEGMENT_SIZE)
    sg.init_segment(buf, orchestrator_pid=ORCH, hook_host_pid=99)
    g = GatewayState(armed=armed)
    g.stamp_watcher(Flag.CLEAR, T + 1000)
    SegmentWriter(buf).publish(g)
    return buf


# -- translation ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("injected, extra, origin", [
    (False, ffi.EMITTER_TAG, "console"), (True, ffi.EMITTER_TAG, "emitter"), (True, ffi.DRILL_TAG, "drill"),
    (True, 0, "foreign_process"), (True, 7, "foreign_process")])
def test_origin_from_the_injected_flag_and_the_tag(injected, extra, origin):
    assert hh.origin_of(injected, extra) == origin


def test_mouse_structures_translate_to_hook_events():
    info = ffi.MSLLHOOKSTRUCT(pt=ffi.POINT(-1500, 30), mouseData=(0xFF88 << 16), flags=hh.LLMHF_LOWER_IL_INJECTED,
                              dwExtraInfo=ffi.EMITTER_TAG)
    assert hh.from_mouse_hook(0x20A, info, frozenset({"shift"})) == HookEvent("wheel", -1500, 30, None, None, -120,
                                                                         frozenset({"shift"}), "emitter")
    info.mouseData = 2 << 16
    assert hh.from_mouse_hook(0x20C, info, frozenset()).button == "x2"
    info.mouseData = 3 << 16
    assert hh.from_mouse_hook(0x20B, info, frozenset()) is None                      # an X button with no id
    assert hh.from_mouse_hook(hh.WM_MOUSEHWHEEL, info, frozenset()) is None          # not in the tables
    k = ffi.KBDLLHOOKSTRUCT(vkCode=0xE7, flags=hh.LLKHF_INJECTED, dwExtraInfo=ffi.DRILL_TAG)
    assert hh.from_key_hook(hh.WM_SYSKEYDOWN, k, frozenset()) == HookEvent("key_down", vk=0xE7, origin="drill")
    assert hh.from_key_hook(0x0102, k, frozenset()) is None                          # WM_CHAR never reaches a LL hook


def test_procedures_pass_other_codes_and_drop_what_they_cannot_judge():
    api = FakeApi()
    seen = []
    procs = hh.HookProcs(api, lambda e: seen.append(e) or True)
    info = ffi.MSLLHOOKSTRUCT(pt=ffi.POINT(1, 1))
    assert procs.mouse(-1, 0x200, ctypes.addressof(info)) == 0 and not seen      # nCode < 0: next hook only
    assert procs.mouse(0, hh.WM_MOUSEHWHEEL, ctypes.addressof(info)) == hh.DROP and not seen
    boom = hh.HookProcs(api, lambda e: 1 / 0)
    k = ffi.KBDLLHOOKSTRUCT(vkCode=0x41, flags=hh.LLKHF_INJECTED, dwExtraInfo=ffi.EMITTER_TAG)
    assert boom.keyboard(0, hh.WM_KEYDOWN, ctypes.addressof(k)) == hh.DROP      # an exception never passes input


def test_modifiers_follow_the_planners_convention_and_ignore_dropped_downs():
    api = FakeApi()
    seen: list[HookEvent] = []
    allow = {"value": True}
    procs = hh.HookProcs(api, lambda e: seen.append(e) or allow["value"])

    def key(t, vk):
        _, w, info = raw(InputEvent(t, vk=vk))
        procs.keyboard(0, w, ctypes.addressof(info))

    def click():
        _, w, info = raw(InputEvent("button_down", 5, 5, button="left"))
        procs.mouse(0, w, ctypes.addressof(info))

    # Ctrl+Shift+B as executor.primitives writes a chord unit: each modifier's down and up excludes itself
    for t, vk in [("key_down", 0x11), ("key_down", 0x10), ("key_down", 0x42), ("key_up", 0x42), ("key_up", 0x10),
                  ("key_up", 0x11)]:
        key(t, vk)
    assert [e.mods for e in seen] == [frozenset(), {"ctrl"}, {"ctrl", "shift"}, {"ctrl", "shift"}, {"ctrl"},
                                      frozenset()]
    key("key_down", 0xA0)                                                        # left Shift held
    key("key_down", 0xA1)
    key("key_up", 0xA1)                                                          # right Shift released
    click()
    assert seen[-1].mods == {"shift"}                                            # still held by the left key
    key("key_up", 0xA0)
    allow["value"] = False
    key("key_down", 0x10)                                                        # dropped: the app never saw it
    allow["value"] = True
    click()
    assert seen[-1].mods == frozenset()


# -- parity with decide_from_segment ---------------------------------------------------------------------------
def _random_state(rng: random.Random) -> GatewayState:
    g = GatewayState(armed=rng.random() < 0.9, deny_all=rng.random() < 0.05)
    g.stamp_watcher(rng.choice(list(Flag)), T - rng.choice((0.0, 0.1, 0.45, 0.6, 2.0)))
    points = [(100, 100), (102, 99), (400, 300), (920, 610)]
    for _ in range(rng.randint(0, 9)):
        try:
            k = rng.random()
            x, y = rng.choice(points)
            if k < 0.35:
                g.issue_click(x, y, T - rng.choice((0.0, 0.3, 0.7)), button=rng.choice(("left", "right", "x1")),
                              dialog_ok=rng.random() < 0.5, send_ok=rng.random() < 0.2,
                              hold_ms=rng.choice((None, None, 300)))
            elif k < 0.6:
                g.issue_key(rng.choice((0x41, 0x0D, 0x10, 0x11, 0xE7)), T - rng.choice((0.0, 0.7)),
                            secret=rng.random() < 0.2)
            elif k < 0.8:
                g.issue_wheel(x, y, rng.choice((1, -1)), T - rng.choice((0.0, 0.7)))
            else:
                g.issue_release(T)
        except gw.GatewayError:
            break
    if rng.random() < 0.4:
        g.open_move_allow(Rect(90, 90, 40, 30), T - rng.choice((0.0, 2.0)), 300)
    if rng.random() < 0.5:
        g.write_deny_rects([(Rect(900, 600, 80, 28), "Send")])
    if rng.random() < 0.5:
        g.load_chords([Chord.parse("B"), Chord.parse("Shift+B")])
    return g


def _random_event(rng: random.Random) -> InputEvent:
    kind = rng.choice(("move", "button_down", "button_up", "wheel", "key_down", "key_up"))
    x, y = rng.choice(((100, 100), (101, 101), (104, 100), (400, 300), (920, 610), (-30, 5)))
    kw = {"origin": rng.choice(("emitter",) * 6 + ("foreign_process", "console", "drill"))}
    if kind.startswith("button"):
        kw["button"] = rng.choice(("left", "left", "right", "middle", "x1", "x2"))
    if kind.startswith("key"):
        kw["vk"] = rng.choice((0x41, 0x42, 0x0D, 0x10, 0x11, 0xA0, 0x12, 0xE7, 0x5B))
        x = y = 0
    if kind == "wheel":
        kw["delta"] = rng.choice((120, -120, 240, 0))
    return InputEvent(kind, x, y, **kw)


def test_the_procedures_decide_as_decide_from_segment_on_a_seeded_corpus():
    """Each raw hook structure through the hook procedure (translation, modifiers, the decision through
    decide_from_segment on segment A) against decide_from_segment called directly with the event the structure
    stands for on an identical segment B: same answer, same event, same bytes written."""
    rng = random.Random(20261111)
    api = FakeApi()
    decisions = 0
    for _ in range(300):
        g = _random_state(rng)
        a = bytearray(sg.SEGMENT_SIZE)
        sg.init_segment(a, orchestrator_pid=ORCH)
        SegmentWriter(a).publish(g)
        b = bytearray(a)
        now = {"ns": START}
        seen: list[HookEvent] = []
        procs = hh.HookProcs(api, lambda e: seen.append(e) or sg.decide_from_segment(a, e, now["ns"]).allowed)
        for _ in range(rng.randint(1, 12)):
            now["ns"] = START + int(rng.choice((0.0, 0.2, 0.5, 0.6, 1.2, 2.5)) * 1e9)
            e = _random_event(rng)
            kind, wparam, info = raw(e)
            proc = procs.mouse if kind == "mouse" else procs.keyboard
            answer = proc(hh.HC_ACTION, wparam, ctypes.addressof(info))
            got = seen[-1]
            want = HookEvent(e.type, e.x, e.y, e.button, e.vk, e.delta, got.mods, e.origin)
            assert got == want
            direct = sg.decide_from_segment(b, want, now["ns"])
            assert (answer == 0) == direct.allowed and (answer == hh.DROP) != direct.allowed
            assert a == b
            decisions += 1
    assert decisions > 1500


# -- the facade under the real loop --------------------------------------------------------------------------------
def test_the_loop_runs_on_the_facade_dpi_first_hooks_in_and_out():
    api = FakeApi()
    buf = segment()
    host_os = hh.Win32HookHostOS(api, clock=api.clock)
    api.at.append((START + 100 * MS, lambda o: o.fire(InputEvent("move", 5, 5))))
    api.at.append((START + 150 * MS, lambda o: o.fire(InputEvent("key_down", vk=0x41))))
    api.at.append((START + 200 * MS, lambda o: o.fire(InputEvent("move", 5, 5, origin="console"))))
    api.at.append((START + 300 * MS, lambda o: setattr(o, "event_set", True)))
    assert HookHost(buf, host_os).run() == EXIT_WATCHDOG
    assert api.calls[0] == ("dpi", ctypes.c_void_p(hh.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).value)
    assert api.calls[1:4] == [("priority", hh.THREAD_PRIORITY_ABOVE_NORMAL), ("hook", hh.WH_MOUSE_LL, True),
                              ("hook", hh.WH_KEYBOARD_LL, True)]
    assert api.calls[-2:] == [("unhook", hh.WH_MOUSE_LL), ("unhook", hh.WH_KEYBOARD_LL)] and not api.hooks
    assert [r for _, r in api.results] == [0, hh.DROP, hh.DROP]          # allowed move; no token; console input
    assert [r.reason for r in RingReader().read(buf)] == ["no_token", "foreign_input"]
    assert read_header(buf).hook_alive_mono_ts > START


def test_a_dead_orchestrator_drops_everything_through_the_procedures():
    api = FakeApi()
    buf = segment()
    api.at.append((START + 400 * MS, lambda o: o.alive.discard(ORCH)))
    for t in range(100, 2000, 100):
        api.at.append((START + t * MS + 1, lambda o: o.fire(InputEvent("move", 5, 5))))
    api.at.append((START + 2000 * MS, lambda o: setattr(o, "event_set", True)))
    host = HookHost(buf, hh.Win32HookHostOS(api, clock=api.clock))
    assert host.run() == EXIT_WATCHDOG and host.deny_all
    assert api.results[0][1] == 0 and api.results[-1][1] == hh.DROP


def test_a_reinstall_request_removes_and_installs_both_hooks():
    api = FakeApi()
    buf = segment()
    api.at.append((START + 100 * MS, lambda o: SegmentWriter(buf).request_reinstall()))
    api.at.append((START + 300 * MS, lambda o: setattr(o, "event_set", True)))
    HookHost(buf, hh.Win32HookHostOS(api, clock=api.clock)).run()
    kinds = [c for c in api.calls if c[0] in ("hook", "unhook")]
    assert kinds[2:6] == [("unhook", 14), ("unhook", 13), ("hook", 14, True), ("hook", 13, True)]
    h = read_header(buf)
    assert (h.reinstall_done, h.reinstall_ok) == (1, True)


def test_a_hook_that_cannot_be_installed_leaves_none_behind():
    api = FakeApi(hook_ok=(True, False))
    assert HookHost(segment(), hh.Win32HookHostOS(api, clock=api.clock)).run() == EXIT_HOOK_INSTALL_FAILED
    assert api.calls[-1] == ("unhook", hh.WH_MOUSE_LL) and not api.hooks


def test_dpi_already_declared_is_accepted_and_anything_else_raises():
    hh.Win32HookHostOS(FakeApi(dpi_set=False, dpi_equal=True)).set_dpi_awareness()
    api = FakeApi(dpi_set=False, dpi_equal=False)
    api.error = 5
    with pytest.raises(ffi.Win32Error, match="SetProcessDpiAwarenessContext"):
        hh.Win32HookHostOS(api).set_dpi_awareness()


def test_pid_alive_holds_a_handle_so_a_reused_pid_stays_dead():
    api = FakeApi()
    host_os = hh.Win32HookHostOS(api)
    assert host_os.pid_alive(ORCH)
    api.alive.discard(ORCH)
    assert not host_os.pid_alive(ORCH)
    api.alive.add(ORCH)                     # a new process with the same pid: the held handle is still signalled
    api.generation[ORCH] = 1
    assert not host_os.pid_alive(ORCH)
    assert not host_os.pid_alive(77)        # never existed
    api.denied.add(88)
    assert host_os.pid_alive(88)            # exists, but its rights are not ours
    host_os.exit_signalled()
    host_os.close()
    assert sorted(api.closed) == sorted([handle(ORCH, 0), EVENT])


def test_main_attaches_to_the_named_segment_and_closes_its_handles():
    seg = sg.Segment.create(orchestrator_pid=ORCH)
    try:
        g = GatewayState(armed=True)
        g.stamp_watcher(Flag.CLEAR, T + 1000)
        SegmentWriter(seg.buf).publish(g)
        api = FakeApi()
        api.at.append((START + 100 * MS, lambda o: setattr(o, "event_set", True)))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hh.time, "monotonic_ns", api.clock)
            assert hh.main([seg.name], api=api) == EXIT_WATCHDOG
        assert EVENT in api.closed and read_header(seg.buf).hook_alive_mono_ts == START
    finally:
        seg.close()
        seg.unlink()


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        hh.Win32HookHostOS()


def test_the_hook_host_imports_the_standard_library_only():
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); before = set(sys.modules); "
            "import sentinel.gateway.hookhost_win32; print(json.dumps(sorted(set(sys.modules) - before)))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(SRC)], capture_output=True, text=True, check=True)
    loaded = json.loads(out.stdout)
    ours = {m for m in loaded if m.split(".")[0] == "sentinel"}
    assert ours == {"sentinel", "sentinel.gateway", "sentinel.gateway.hookhost_loop", "sentinel.gateway.segment",
                    "sentinel.gateway.hookhost_win32", "sentinel.win32", "sentinel.win32.ffi"}
    assert {m.split(".")[0] for m in loaded} - {"sentinel"} <= set(sys.stdlib_module_names)
