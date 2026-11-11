"""Tests for sentinel.win32.process: the ProcessPort over a fake Toolhelp and process API, and the crash-file scan on
real files."""
import ctypes
import os
import sys
from dataclasses import dataclass

import pytest

from sentinel.orchestrator.ports import ProcessInfo, ProcessPort
from sentinel.win32 import ffi
from sentinel.win32 import process as pr

INSTALL = "C:\\Program Files\\thinkorswim"


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    exe: str
    created: int
    ws: int | None = 100
    private: int | None = 50
    gdi: int | None = 10
    user: int | None = 5
    handles: int = 300
    threads: int = 4
    cpu: tuple[int, int] = (10_000_000, 5_000_000)
    alive: bool = True
    killable: bool = True
    readable: bool = True


def app_tree():
    return [Proc(4, 0, "System", "System", 1), Proc(900, 4, "explorer.exe", "C:\\Windows\\explorer.exe", 50),
            Proc(1000, 900, "thinkorswim.exe", INSTALL + "\\thinkorswim.exe", 100),
            Proc(1001, 1000, "java.exe", INSTALL + "\\jre\\bin\\java.exe", 110, ws=2_000_000_000, gdi=0),
            Proc(1002, 1001, "jcef_helper.exe", INSTALL + "\\jcef\\jcef_helper.exe", 120),
            Proc(1003, 1000, "conhost.exe", "C:\\Windows\\System32\\conhost.exe", 105, readable=False),
            Proc(1500, 1000, "stale.exe", "C:\\stale.exe", 90)]          # parent pid reused: born before 1000


class FakeApi:
    def __init__(self, procs):
        self.procs = {p.pid: p for p in procs}
        self.error = 0
        self.open: dict[int, int] = {}
        self.kills: list[int] = []
        self.next_error_after_snapshot = ffi.ERROR_NO_MORE_FILES
        self.snapshot_ok = True

    def get_last_error(self):
        return self.error

    def set_last_error(self, v):
        self.error = v

    def CreateToolhelp32Snapshot(self, flags, pid):
        assert flags == pr.TH32CS_SNAPPROCESS
        if not self.snapshot_ok:
            self.error = 5
            return ffi.INVALID_HANDLE_VALUE
        self.rows = [p for p in self.procs.values() if p.alive]
        self.open[0x50] = self.open.get(0x50, 0) + 1
        return 0x50

    def Process32FirstW(self, snap, e):
        assert e.dwSize == ctypes.sizeof(ffi.PROCESSENTRY32W)
        self.i = 0
        return self._fill(e)

    def Process32NextW(self, snap, e):
        self.i += 1
        return self._fill(e)

    def _fill(self, e):
        if self.i >= len(self.rows):
            self.error = self.next_error_after_snapshot
            return 0
        p = self.rows[self.i]
        e.th32ProcessID, e.th32ParentProcessID, e.cntThreads = p.pid, p.ppid, p.threads
        raw = p.name.encode("utf-16-le") + b"\0\0"
        ctypes.memmove(e.szExeFile, raw, len(raw))
        return 1

    def OpenProcess(self, access, inherit, pid):
        p = self.procs.get(pid)
        if p is None or not p.alive:
            self.error = ffi.ERROR_INVALID_PARAMETER
            return None
        if not p.readable and access & (pr.PROCESS_QUERY_INFORMATION | pr.PROCESS_TERMINATE):
            self.error = ffi.ERROR_ACCESS_DENIED
            return None
        h = 0x10000 + pid
        self.open[h] = self.open.get(h, 0) + 1
        return h

    def CloseHandle(self, h):
        self.open[h] -= 1
        return 1

    def _p(self, h):
        return self.procs[h - 0x10000]

    def GetProcessTimes(self, h, created, exited, kernel, user):
        p = self._p(h)
        for ft, v in ((created, p.created), (kernel, p.cpu[0]), (user, p.cpu[1])):
            ft.dwLowDateTime, ft.dwHighDateTime = v & 0xFFFFFFFF, v >> 32
        return 1

    def GetProcessMemoryInfo(self, h, mem, cb):
        assert cb == ctypes.sizeof(ffi.PROCESS_MEMORY_COUNTERS_EX) == mem.cb
        p = self._p(h)
        if p.ws is None:
            return 0
        mem.WorkingSetSize, mem.PrivateUsage = p.ws, p.private
        return 1

    def GetGuiResources(self, h, which):
        v = self._p(h).gdi if which == pr.GR_GDIOBJECTS else self._p(h).user
        if v is None:
            self.error = ffi.ERROR_ACCESS_DENIED
            return 0
        return v

    def GetProcessHandleCount(self, h, count):
        count.value = self._p(h).handles
        return 1

    def QueryFullProcessImageNameW(self, h, flags, buf, size):
        buf.value = self._p(h).exe
        size.value = len(buf.value)
        return 1

    def TerminateProcess(self, h, code):
        p = self._p(h)
        self.kills.append(p.pid)
        if p.killable:
            p.alive = False
        return int(p.killable)

    def WaitForSingleObject(self, h, ms):
        assert ms == pr.KILL_WAIT_MS
        return 0


def port(procs=None, **kw):
    api = FakeApi(procs or app_tree())
    return pr.Win32Processes(INSTALL, api=api, **kw), api


def test_it_is_a_process_port():
    assert isinstance(port()[0], ProcessPort)


def test_descendants_follow_parents_breadth_first_and_skip_reused_parent_pids():
    rows = [pr.Entry(p.pid, p.ppid, p.name) for p in app_tree()]
    created = {p.pid: p.created for p in app_tree()}
    assert [e.pid for e in pr.descendants(rows, 1000)] == [1000, 1001, 1003, 1500, 1002]
    assert [e.pid for e in pr.descendants(rows, 1000, created)] == [1000, 1001, 1003, 1002]   # choice 1
    assert pr.descendants(rows, 4242) == []
    loop = [pr.Entry(1, 2, "a"), pr.Entry(2, 1, "b")]
    assert [e.pid for e in pr.descendants(loop, 1)] == [1, 2]


def test_kill_order_is_children_first_and_the_root_last():
    tree = [ProcessInfo(1000, 900, "a"), ProcessInfo(1003, 1000, "c"), ProcessInfo(1001, 1000, "j"),
            ProcessInfo(1002, 1001, "h")]
    assert pr.kill_order(tree) == [1002, 1003, 1001, 1000]
    assert pr.kill_order([]) == []
    two_roots = [ProcessInfo(1001, 1000, "j"), ProcessInfo(1002, 1001, "h"), ProcessInfo(2000, None, "x"),
                 ProcessInfo(2001, 2000, "y"), ProcessInfo(1000, 900, "a")]       # the JVM first, its launcher later
    assert pr.kill_order(two_roots) == [1002, 1001, 2001, 2000, 1000]


def test_the_tree_carries_the_soak_metrics_and_unreadable_fields_are_none():
    p, api = port()
    tree = {i.pid: i for i in p.process_tree(1000)}
    assert sorted(tree) == [1000, 1001, 1002, 1003]
    java = tree[1001]
    assert (java.ppid, java.name, java.exe) == (1000, "java.exe", INSTALL + "\\jre\\bin\\java.exe")
    assert (java.working_set, java.private_bytes, java.gdi_objects, java.user_objects) == (2_000_000_000, 50, 0, 5)
    assert (java.handles, java.threads, java.cpu_s) == (300, 4, 1.5)
    con = tree[1003]                                  # access denied: identity only (choice 2)
    assert (con.exe, con.working_set, con.gdi_objects, con.threads) == (None, None, None, 4)
    assert all(n == 0 for n in api.open.values())     # every handle closed


def test_a_gdi_count_that_cannot_be_read_is_none_not_zero():
    procs = app_tree()
    procs[3].gdi, procs[3].ws = None, None
    p, _ = port(procs)
    java = next(i for i in p.process_tree(1000) if i.pid == 1001)
    assert (java.gdi_objects, java.user_objects, java.working_set, java.private_bytes) == (None, 5, None, None)


def test_kill_tree_kills_children_first_and_proves_nothing_survives():
    procs = app_tree()
    procs[5].readable = True                          # conhost can be killed this time
    p, api = port(procs)
    assert p.kill_tree(1000)
    assert api.kills == [1002, 1001, 1003, 1000]
    assert all(n == 0 for n in api.open.values())


def test_a_process_that_refuses_to_die_is_a_survivor():
    procs = app_tree()
    procs[3].killable = False
    p, _ = port(procs)
    assert not p.kill_tree(1000)


def test_an_install_folder_process_outside_the_roots_descendants_is_in_the_tree_and_killed():
    procs = app_tree() + [Proc(2000, 900, "thinkorswim.exe", INSTALL.upper() + "/thinkorswim.exe", 300),
                          Proc(2001, 2000, "helper.exe", "C:\\Temp\\helper.exe", 310)]
    procs[5].readable = True
    p, api = port(procs)
    assert sorted(i.pid for i in p.process_tree(1000)) == [1000, 1001, 1002, 1003, 2000, 2001]   # 10.2 item 3
    assert p.kill_tree(1000)
    assert api.kills.index(2001) < api.kills.index(2000)                     # children first
    assert not any(api.procs[pid].alive for pid in (1000, 1001, 1002, 1003, 2000, 2001))


def test_anything_from_the_install_folder_that_survives_the_kill_is_a_failure():
    procs = app_tree() + [Proc(2000, 900, "thinkorswim.exe", INSTALL.upper() + "/thinkorswim.exe", 300,
                               killable=False)]
    procs[5].readable = True
    p, _ = port(procs)
    assert not p.kill_tree(1000) and p.survivors({}) == [2000]


def test_with_the_jvm_window_as_root_the_launcher_joins_by_the_install_folder_rule_and_dies_last():
    procs = app_tree()
    procs[5].readable = True
    p, api = port(procs)
    assert sorted(i.pid for i in p.process_tree(1001)) == [1000, 1001, 1002, 1003]   # Branch child (10.2 item 3)
    assert p.kill_tree(1001)
    assert api.kills == [1002, 1001, 1003, 1000]                               # children first, the launcher last
    assert not api.procs[1000].alive


def test_a_jvm_whose_launcher_exited_is_still_the_tree_and_is_killed():
    procs = app_tree()
    procs[2].alive = False                                                     # the install4j launcher is gone
    p, api = port(procs)
    assert [i.pid for i in p.process_tree(1000)] == [1001, 1002]               # not an empty tree (process_exit)
    assert p.kill_tree(1000)
    assert api.kills == [1002, 1001] and not api.procs[1001].alive


def test_no_root_is_the_install_folder_rule_alone_and_never_the_whole_system():
    procs = [Proc(0, 0, "[System Process]", "", 0)] + app_tree()
    p, _ = port(procs)
    assert sorted(i.pid for i in p.process_tree(0)) == [1000, 1001, 1002, 1003]     # main_frame_missing (10.2)


def test_the_install_folder_is_required():
    for missing in (None, ""):
        with pytest.raises(ValueError, match="install folder"):
            pr.Win32Processes(missing, api=FakeApi(app_tree()))


def test_a_reused_pid_after_the_kill_is_not_a_survivor():
    procs = app_tree()
    procs[5].readable = True
    p, api = port(procs)
    real_terminate = api.TerminateProcess

    def terminate_and_reuse(h, code):
        r = real_terminate(h, code)
        if h == 0x10000 + 1002:
            api.procs[1002] = Proc(1002, 900, "notepad.exe", "C:\\Windows\\notepad.exe", 999)
        return r

    api.TerminateProcess = terminate_and_reuse
    assert p.kill_tree(1000)


def test_snapshot_failures_raise():
    p, api = port()
    api.snapshot_ok = False
    with pytest.raises(ffi.Win32Error, match="CreateToolhelp32Snapshot"):
        p.process_tree(1000)
    api.snapshot_ok, api.next_error_after_snapshot = True, 31
    with pytest.raises(ffi.Win32Error, match="Process32NextW"):
        p.snapshot()
    assert api.open[0x50] == 0                        # the snapshot handle is closed either way


def test_under_is_case_and_slash_insensitive():
    assert pr.under(INSTALL + "\\jre\\java.exe", INSTALL.lower() + "/")
    assert not pr.under(INSTALL + "2\\x.exe", INSTALL) and not pr.under(None, INSTALL)


def test_crash_files_finds_new_dumps_one_level_down(tmp_path):
    old, new = 1_700_000_000, 1_800_000_000
    (tmp_path / "wer").mkdir()
    files = {"hs_err_pid1001.log": new, "java_pid1001.hprof": new, "wer/thinkorswim.exe.1001.dmp": new,
             "HS_ERR_PID7.LOG": new, "hs_err_pid1.log": old, "notes.txt": new}
    for name, t in files.items():
        f = tmp_path / name
        f.write_text("x")
        os.utime(f, (t, t))
    since = "2027-01-15T08:00:00+00:00"                       # between old and new
    got = pr.find_crash_files([str(tmp_path), str(tmp_path / "missing")], since)
    assert [os.path.relpath(g, tmp_path).replace(os.sep, "/") for g in got] == [
        "HS_ERR_PID7.LOG", "hs_err_pid1001.log", "java_pid1001.hprof", "wer/thinkorswim.exe.1001.dmp"]
    with pytest.raises(ValueError, match="UTC offset"):
        pr.find_crash_files([str(tmp_path)], "2027-01-15T08:00:00")
    p = pr.Win32Processes(INSTALL, [str(tmp_path)], api=FakeApi([]))
    assert p.crash_files(since) == got


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        pr.Win32Processes(INSTALL)
