"""The Win32 rig composition (orchestrator.win32rig): import-safe on Linux, composes the adapters behind the ports
with fakes, and the Watchdog journal writes the line format store.journal ingests (5.1, 5.18, 8.2, 10.2)."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.orchestrator.gateway import InputEvent
from sentinel.orchestrator.ports import (AccessNode, Frame, OsReads, AppReads, PreflightRead, ProcessInfo,
                                         WindowInfo)
from sentinel.orchestrator.watchdog_loop import Journal
from sentinel.orchestrator.win32rig import Win32RigConfig, compose
from sentinel.pipeline.simports import SimInput
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.journal import ingest_journal

SRC = Path(__file__).resolve().parents[1] / "src"


def test_the_win32_rig_imports_on_linux_without_a_windows_module():
    code = ("import sys; import sentinel.orchestrator.win32rig as w; "
            "bad = [m for m in ('winreg', 'msvcrt', '_winapi') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(SRC)})
    assert out.stdout.strip() == "[]"


class FakeProcesses:
    def __init__(self):
        self.roots = []

    def process_tree(self, root_pid):
        self.roots.append(root_pid)
        return (ProcessInfo(4242, 1, "thinkorswim.exe"),)

    def kill_tree(self, root_pid):
        return True

    def crash_files(self, since):
        return ()


class FakeDesktop:
    def __init__(self, pids):
        self.pids = pids

    def enumerate_windows(self):
        assert self.pids() == {4242}                       # windows follow the process tree's pids
        return (WindowInfo(77, 4242, "SunAwtFrame", "Main@thinkorswim [build 1998.3]"),)

    def grab(self):
        return Frame(None, (0, 0), 0.0)

    def window_from_point(self, points):
        return ()

    def virtual_screen(self):
        return (0, 0, 1920, 1080)

    def process_dpi_awareness(self, pid):
        return "PER_MONITOR_AWARE_V2"

    def input_desktop(self):
        return "Default"

    def close_window(self, hwnd):
        return True

    def foreground_window(self):
        return None


class FakeAccess:
    def __init__(self, main_hwnd):
        self.main_hwnd = main_hwnd

    def available(self):
        return True

    def tree(self, hwnd):
        return AccessNode("frame", "Main", None)

    def focus_owner(self, hwnd):
        return None


class FakeClipboard:
    def read_text(self):
        return None

    def clear(self):
        pass


class FakeFingerprint:
    def __init__(self, title):
        self.title = title

    def read_os(self):
        return OsReads(None, None, None, None, None, None, None, None)

    def read_app(self):
        return AppReads(self.title(), None, None, None, None, None, None, None, None, None)


class FakePreflight:
    def read(self, item):
        return PreflightRead(item, {})


FACTORIES = {
    "processes": lambda cfg, api: FakeProcesses(),
    "desktop": lambda pids, api: FakeDesktop(pids),
    "input": lambda api: SimInput(),
    "segment": lambda cfg: None,
    "access": lambda main_hwnd, api: FakeAccess(main_hwnd),
    "clipboard": lambda api: FakeClipboard(),
    "probe": lambda desktop, api: object(),
    "fingerprint": lambda probe, cfg, title: FakeFingerprint(title),
    "preflight": lambda probe, api: FakePreflight(),
}


def test_compose_puts_one_adapter_behind_each_port(tmp_path):
    cfg = Win32RigConfig(install_dir=r"C:\thinkorswim", shard_dir=tmp_path, orchestrator_pid=100,
                         root_pid=lambda: 4242)
    ports = compose(cfg, factories=FACTORIES)
    assert ports.missing() == []
    assert ports.access.main_hwnd() == 77                    # the main frame by the pinned title regex (choice 1)
    assert ports.fingerprint.read_app().build_id_title == "Main@thinkorswim [build 1998.3]"
    assert ports.watchdog_journal.path == tmp_path / "watchdog_events.jsonl"
    assert ports.processes.roots == [4242, 4242]


def test_compose_reports_an_adapter_that_misses_its_port(tmp_path):
    cfg = Win32RigConfig(install_dir=r"C:\thinkorswim", shard_dir=tmp_path, orchestrator_pid=100)
    ports = compose(cfg, factories={**FACTORIES, "clipboard": lambda api: object()})
    assert ports.missing() == ["clipboard"]


def test_the_simulated_input_port_takes_typed_characters_with_the_events():
    port = SimInput()
    events = [InputEvent("move", 10, 20), InputEvent("key_down", vk=0xE7), InputEvent("key_up", vk=0xE7)]
    assert port.send(events, chars=[None, "Q", "Q"]) == 3
    assert port.cursor() == (10, 20) and port.typed() == "QQ"
    with pytest.raises(ValueError):
        port.send(events, chars=["Q"])


def test_the_rig_watchdog_journal_is_ingested_as_watchdog_step_events(tmp_path):
    cfg = Win32RigConfig(install_dir=r"C:\thinkorswim", shard_dir=tmp_path / "rig" / "shard-1", orchestrator_pid=100)
    j = compose(cfg, factories=FACTORIES).watchdog_journal
    at = datetime(2026, 11, 10, 15, 3, 21, tzinfo=timezone.utc)
    j.append(j.entry("watchdog_hang", at=at, mono=184233.118, detail="cause: hang", app_uptime_s=1820,
                     app_restart_count=1))
    j.append(Journal(j.path).entry("main_frame_missing", at=at, mono=184240.0))    # CD-4: in the vocabulary now
    line = json.loads(j.path.read_text().splitlines()[0])
    assert set(line) >= {"event_id", "action", "at", "mono_ms"} and line["event_id"] == "wd-0001"
    with Store(tmp_path / "central.db") as s:
        s.register_rig("rig-01", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(RunPlan(run_id="run-01", run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry("census/x/ws", "ws", "active_trader", 1, "frozen"),),
                            shards=(ShardPlan("run-01/1", "rig-01", scope_count=1),),
                            started_at="2026-11-10T15:00:00Z"))
        r = ingest_journal(s, "run-01/1", j.path, "watchdog")
        assert (r.read, r.written) == (2, 2)
        assert ingest_journal(s, "run-01/1", j.path, "watchdog").written == 0      # idempotent (8.2)
        rows = s.query("SELECT action, source, mode, reason FROM step_events ORDER BY seq")
        assert [(x["action"], x["source"], x["mode"]) for x in rows] == [
            ("watchdog_hang", "watchdog", "system"), ("main_frame_missing", "watchdog", "system")]
        assert rows[0]["reason"] == "wd-0001: cause: hang"
