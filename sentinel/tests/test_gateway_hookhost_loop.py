"""The hook host's control loop (5.4.2 fail-closed rules, 5.4.4 reinstall) over a fake OS facade."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from sentinel.gateway import segment as sg
from sentinel.gateway.hookhost_loop import (ALIVE_STAMP_NS, EXIT_HOOK_INSTALL_FAILED, EXIT_LAYOUT_MISMATCH,
                                            EXIT_ORPHANED, EXIT_WATCHDOG, ORPHAN_EXIT_NS, PID_POLL_NS, HookHost,
                                            main)
from sentinel.gateway.segment import HookEvent, RingReader, SegmentWriter, read_header
from sentinel.orchestrator.gateway import Flag, GatewayState

SRC = Path(__file__).resolve().parents[1] / "src"
MS = 1_000_000
START = 1_000 * 1_000 * MS                     # 1000 s on the monotonic clock
ORCH = 4242


class FakeOS:
    """A clock that `pump` advances, a process table, the Watchdog's event and scripted events."""

    def __init__(self, *, install_ok: bool = True, reinstall_ok: bool = True, max_ns: int = 120_000 * MS):
        self.now = START
        self.alive = {ORCH}
        self.exit = False
        self.install_ok, self.reinstall_ok = install_ok, reinstall_ok
        self.calls: list[str] = []
        self.callback = None
        self.at: list[tuple[int, object]] = []      # (time, action(fake)) run when the clock passes it
        self.decisions: list[tuple[int, bool]] = []
        self.max_ns = START + max_ns

    def set_dpi_awareness(self):
        self.calls.append("dpi")

    def monotonic_ns(self):
        return self.now

    def pid_alive(self, pid):
        return pid in self.alive

    def exit_signalled(self):
        return self.exit

    def install_hooks(self, callback):
        self.calls.append("install")
        self.callback = callback
        return self.install_ok

    def reinstall_hooks(self):
        self.calls.append("reinstall")
        return self.reinstall_ok

    def uninstall_hooks(self):
        self.calls.append("uninstall")

    def pump(self, timeout_ms):
        assert self.now < self.max_ns, "the loop never exited"
        self.now += timeout_ms * MS
        due = [a for a in self.at if a[0] <= self.now]
        self.at = [a for a in self.at if a[0] > self.now]
        for _, action in due:
            action(self)

    def send(self, e):
        self.decisions.append((self.now, self.callback(e)))


def segment(*, deny_all: bool = False) -> bytearray:
    buf = bytearray(sg.SEGMENT_SIZE)
    sg.init_segment(buf, orchestrator_pid=ORCH, hook_host_pid=99)
    g = GatewayState(armed=True, deny_all=deny_all)
    g.stamp_watcher(Flag.CLEAR, START / 1e9 + 1000)                 # fresh for the whole test
    SegmentWriter(buf).publish(g)
    return buf


def test_dpi_awareness_comes_first_and_hooks_are_removed_on_exit():
    os = FakeOS()
    os.at.append((START + 300 * MS, lambda o: setattr(o, "exit", True)))
    assert HookHost(segment(), os).run() == EXIT_WATCHDOG
    assert os.calls == ["dpi", "install", "uninstall"]


def test_layout_mismatch_at_start_exits_with_the_distinct_code_without_hooks():
    buf = segment()
    struct.pack_into("<I", buf, sg.OFF_VERSION, sg.LAYOUT_VERSION + 1)
    os = FakeOS()
    host = HookHost(buf, os)
    assert host.run() == EXIT_LAYOUT_MISMATCH and host.deny_all
    assert os.calls == ["dpi"]


def test_layout_mismatch_mid_run_latches_deny_all_and_exits():
    buf = segment()
    os = FakeOS()
    os.at.append((START + 200 * MS, lambda o: struct.pack_into("<I", buf, sg.OFF_MAGIC, 0)))
    host = HookHost(buf, os)
    assert host.run() == EXIT_LAYOUT_MISMATCH and host.deny_all
    assert os.calls == ["dpi", "install", "uninstall"] and os.now == START + 200 * MS


def test_install_failure_exits_with_its_own_code():
    os = FakeOS(install_ok=False)
    assert HookHost(segment(), os).run() == EXIT_HOOK_INSTALL_FAILED
    assert "uninstall" not in os.calls


def test_hook_alive_is_stamped_every_250ms():
    buf = segment()
    os = FakeOS()
    stamps = []
    for t in range(0, 2000, 50):
        os.at.append((START + t * MS, lambda o: stamps.append(read_header(buf).hook_alive_mono_ts)))
    os.at.append((START + 2000 * MS, lambda o: setattr(o, "exit", True)))
    HookHost(buf, os).run()
    distinct = sorted(set(stamps))
    assert distinct[0] == START
    assert all(b - a == ALIVE_STAMP_NS for a, b in zip(distinct, distinct[1:])) and len(distinct) == 8


def test_orchestrator_gone_switches_to_deny_all_within_500ms():
    buf = segment()
    os = FakeOS()
    death = START + 1_130 * MS
    os.at.append((death, lambda o: o.alive.discard(ORCH)))
    for t in range(0, 3000, 50):
        os.at.append((START + t * MS + 1, lambda o: o.send(HookEvent("move", 5, 5))))
    os.at.append((START + 3000 * MS, lambda o: setattr(o, "exit", True)))
    host = HookHost(buf, os)
    assert host.run() == EXIT_WATCHDOG
    first_drop = next(t for t, ok in os.decisions if not ok)
    assert all(ok for t, ok in os.decisions if t < death)
    assert death < first_drop <= death + PID_POLL_NS and host.orphaned_since is not None
    assert all(not ok for t, ok in os.decisions if t >= first_drop)
    assert {r.reason for r in RingReader().read(buf)} == {"deny_all"}               # every drop logged


def test_sixty_seconds_in_deny_all_with_no_orchestrator_exits():
    os = FakeOS()
    death = START + 700 * MS
    os.at.append((death, lambda o: o.alive.discard(ORCH)))
    host = HookHost(segment(), os)
    assert host.run() == EXIT_ORPHANED
    assert death + ORPHAN_EXIT_NS <= os.now <= death + ORPHAN_EXIT_NS + PID_POLL_NS
    assert os.calls[-1] == "uninstall"


def test_kill_switch_deny_all_with_a_live_orchestrator_does_not_exit_on_its_own():
    os = FakeOS(max_ns=200_000 * MS)
    os.at.append((START + 90_000 * MS, lambda o: setattr(o, "exit", True)))
    os.at.append((START + 100 * MS, lambda o: o.send(HookEvent("move", 1, 1))))
    host = HookHost(segment(deny_all=True), os)
    assert host.run() == EXIT_WATCHDOG and os.now == START + 90_000 * MS
    assert os.decisions[0][1] is False and not host.deny_all                        # the segment's flag, no latch


def test_reinstall_requests_are_answered_once_and_logged():
    buf = segment()
    w = SegmentWriter(buf)
    w.request_reinstall()                                       # before this hook host: not answered (choice 3)
    os = FakeOS()
    host = HookHost(buf, os)
    os.at.append((START + 100 * MS, lambda o: w.request_reinstall()))
    os.at.append((START + 300 * MS, lambda o: setattr(o, "reinstall_ok", False)))
    os.at.append((START + 300 * MS, lambda o: w.request_reinstall()))
    os.at.append((START + 600 * MS, lambda o: setattr(o, "exit", True)))
    host.run()
    assert os.calls.count("reinstall") == 2
    h = read_header(buf)
    assert (h.reinstall_request, h.reinstall_done, h.reinstall_ok) == (3, 3, False)
    recs = RingReader().read(buf)
    assert [(r.event_type, r.reason, r.aux, r.injected, r.decision) for r in recs] == [
        ("control", "hook_reinstalled", 1, False, "none"), ("control", "hook_reinstalled", 0, False, "none")]


def test_callback_decides_from_the_segment_and_fails_closed_on_error():
    buf = segment()
    os = FakeOS()
    host = HookHost(buf, os)
    assert host.callback(HookEvent("move", 1, 1)) is True
    assert host.callback(HookEvent("key_down", vk=0x41)) is False                   # no token
    assert host.callback(HookEvent("teleport")) is False                            # cannot judge: dropped
    host.deny_all = True
    assert host.callback(HookEvent("move", 1, 1)) is False
    assert RingReader().read(buf)[-1].reason == "deny_all"


def test_main_attaches_to_the_named_segment():
    seg = sg.Segment.create(orchestrator_pid=ORCH)
    try:
        os = FakeOS()
        os.at.append((START + 100 * MS, lambda o: setattr(o, "exit", True)))
        assert main(seg.name, os) == EXIT_WATCHDOG
        assert read_header(seg.buf).hook_alive_mono_ts == START
    finally:
        seg.close()
        seg.unlink()


def test_the_hook_host_modules_import_the_standard_library_only():
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); before = set(sys.modules); "
            "import sentinel.gateway.hookhost_loop, sentinel.gateway.segment; "
            "print(json.dumps(sorted(set(sys.modules) - before)))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(SRC)], capture_output=True, text=True, check=True)
    loaded = json.loads(out.stdout)
    ours = {m for m in loaded if m.split(".")[0] == "sentinel"}
    assert ours == {"sentinel", "sentinel.gateway", "sentinel.gateway.hookhost_loop", "sentinel.gateway.segment"}
    assert {m.split(".")[0] for m in loaded} - {"sentinel"} <= set(sys.stdlib_module_names)
