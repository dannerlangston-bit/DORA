"""The Watchdog process: sampling, reaction order, watchdog.json, the journal (10.2, 5.14, 6.6)."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.ports import Frame, KillRequest, ProcessInfo, WindowInfo
from sentinel.orchestrator.watchdog import Heartbeat, watchdog_check
from sentinel.orchestrator.watchdog_loop import (GIB, Collection, EventLogEntry, Journal, Observation, Reaction,
                                                 StatusWriter, Step, ThreadDump, Watchdog, WatchdogConfig,
                                                 WatchdogLoop, WatchdogStatus, app_tree, classify_crash_file,
                                                 crash_signal_of, main_frame, read_status, run_reaction,
                                                 status_check, tree_roots)
from sentinel.store.journal import read_journal

T0 = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
RUN, SHARD = "pr-48213-3", "pr-48213-3/2"
INSTALL = r"C:\thinkorswim"
TITLE = "Main@thinkorswim [build 1987]"
CID = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"


def config(tmp_path=None, **kw) -> WatchdogConfig:
    base = dict(run_id=RUN, shard_id=SHARD, run_type="pr", run_deadline=T0 + timedelta(minutes=35),
                install_dir=INSTALL, shard_dir=str(tmp_path or "/rig/run/shard"), xmx_bytes=4 * GIB,
                launcher_pid=6100)
    return WatchdogConfig(**{**base, **kw})


def hb(state="running", mono=1000.0, **kw) -> Heartbeat:
    base = dict(run_id=RUN, shard=2, attempt=1, state=state, ts=(T0 + timedelta(seconds=mono - 1000)).isoformat(),
                mono_ts=mono, flow_id="flow.trade.order-ticket.stock.limit", step_id="s07", checkpoint_id=CID,
                app_pid=6120, orchestrator_pid=4412, hook_host_pid=4420, max_wait_s=20)
    return Heartbeat(**{**base, **kw})


FRAME = WindowInfo(hwnd=77, pid=6120, cls="SunAwtFrame", title=TITLE, rect=(0, 0, 40, 30))
TREE = (ProcessInfo(6100, 1, "thinkorswim.exe", exe=INSTALL + r"\thinkorswim.exe", working_set=GIB, gdi_objects=900),
        ProcessInfo(6120, 6100, "javaw.exe", exe=INSTALL + r"\jre\bin\javaw.exe", working_set=3 * GIB,
                    gdi_objects=4000))


def obs(now=1002.0, heartbeat=None, **kw) -> Observation:
    base = dict(mono=now, wall=T0 + timedelta(seconds=now - 1000), heartbeat=heartbeat or hb(mono=now - 1),
                orchestrator_alive=True, hook_host_alive=True, main_frame=FRAME, tree=TREE, install_alive=True)
    return Observation(**{**base, **kw})


# -- Orchestrator hang and death -----------------------------------------------------------------------------------
def test_stale_heartbeat_is_a_hang_with_the_reaction_in_the_specified_order():
    w = Watchdog(config())
    assert w.sample(1120.0, obs(1120.0, hb(mono=1000.0))) is None                # 120 s: not yet
    r = w.sample(1121.0, obs(1121.0, hb(mono=1000.0)))
    assert r.trigger == "orchestrator_hung" and r.decision.has("restart_orchestrator")
    assert r.names == ("screenshot", "thread_dump", "collect", "kill_orchestrator", "reset_input_state", "kill_tree",
                       "kill_hook_host", "journal", "start_resume")
    assert r.step("kill_orchestrator").args["pid"] == 4412 and r.step("kill_hook_host").args["pid"] == 4420
    assert r.step("thread_dump").args == {"pid": 6120, "timeout_s": 15.0}
    assert r.step("kill_tree").args["roots"] == (6100,)
    assert r.step("journal").args["action"] == "resume"
    assert r.step("start_resume").args["argv"] == ("--resume", RUN, "--shard-dir", "/rig/run/shard")
    assert w.budgets.used_orchestrator == 1
    # the in-progress checkpoint the decision gives the resume: harness_error (orchestrator_died)
    assert {s.selector: s.outcome.reason for s in r.decision.outcomes} == {"in_progress": "orchestrator_died",
                                                                           "rest_of_flow": "step_failed:s07"}


def test_launch_states_allow_five_minutes_and_self_abort_suppresses_the_hang():
    w = Watchdog(config())
    assert w.sample(1299.0, obs(1299.0, hb("launched", mono=1000.0), main_frame=None)) is None
    assert w.sample(1301.0, obs(1301.0, hb("launched", mono=1000.0), main_frame=None)).trigger == "orchestrator_hung"
    quiet = Watchdog(config())
    assert quiet.sample(1002.0, obs(1002.0, self_abort=True)) is None
    assert quiet.sample(1500.0, obs(1500.0, hb(mono=1000.0))) is None            # sticky: a stale heartbeat is no hang
    assert quiet.sample(1502.0, obs(1502.0, hb(mono=1000.0), orchestrator_alive=False)) is None


def test_a_dead_orchestrator_is_resumed_twice_then_the_shard_ships_as_it_is():
    w = Watchdog(config())
    first = w.sample(1010.0, obs(1010.0, hb(mono=1009.0), orchestrator_alive=False))
    assert first.trigger == "orchestrator_died" and "kill_orchestrator" not in first.names
    assert first.names[-3:] == ("kill_hook_host", "journal", "start_resume")
    # the old heartbeat's PID is not checked again (choice 2)
    assert w.sample(1012.0, obs(1012.0, hb(mono=1009.0), orchestrator_alive=False)) is None
    # the resumed Orchestrator writes its own heartbeat, then dies too
    second = w.sample(1100.0, obs(1100.0, hb(mono=1099.0, orchestrator_pid=5000), orchestrator_alive=False))
    assert second.step("start_resume") is not None and w.budgets.used_orchestrator == 2
    third = w.sample(1200.0, obs(1200.0, hb(mono=1199.0, orchestrator_pid=5100), orchestrator_alive=False))
    assert third.decision.run_reason == "orchestrator_restart_budget"
    assert third.names[-2:] == ("journal", "ship_as_is") and third.step("journal").args["action"] == "watchdog_crash"
    assert w.finished and w.sample(1300.0, obs(1300.0, orchestrator_alive=False)) is None


def test_a_resumed_orchestrator_that_never_writes_a_heartbeat_is_hung_again():
    w = Watchdog(config())
    w.sample(1010.0, obs(1010.0, hb(mono=1009.0), orchestrator_alive=False))
    assert w.sample(1129.0, obs(1129.0, hb(mono=1009.0), orchestrator_alive=False)) is None
    again = w.sample(1131.0, obs(1131.0, hb(mono=1009.0), orchestrator_alive=False))
    assert again.trigger == "orchestrator_hung" and w.budgets.used_orchestrator == 2
    assert again.step("kill_orchestrator") is None                               # the old PID is gone


def test_hook_host_death_is_signalled_once():
    w = Watchdog(config())
    r = w.sample(1002.0, obs(hook_host_alive=False))
    assert r.trigger == "hook_host_dead" and r.names == ("signal",)
    assert r.step("signal").args["code"] == "hook_host_dead"
    assert w.sample(1004.0, obs(1004.0, hook_host_alive=False)) is None


# -- app failures --------------------------------------------------------------------------------------------------
def test_working_set_ceiling_and_gdi_count():
    w = Watchdog(config(xmx_bytes=4 * GIB))                                       # ceiling 7 GiB
    assert w.sample(1002.0, obs(tree=(replace(TREE[0], working_set=3 * GIB), TREE[1]))) is None   # 6 GiB
    r = w.sample(1004.0, obs(1004.0, tree=(replace(TREE[0], working_set=5 * GIB), TREE[1])))
    assert (r.trigger, r.cause, r.source) == ("app_failure", "oom", "working_set")
    assert r.names == ("screenshot", "thread_dump", "collect", "reset_input_state", "kill_tree", "journal", "signal")
    assert r.step("journal").args["action"] == "watchdog_crash" and r.step("signal").args["code"] == "app_failure"
    g = Watchdog(config())
    assert g.sample(1002.0, obs(tree=(TREE[0], replace(TREE[1], gdi_objects=8_999)))) is None
    hang = g.sample(1004.0, obs(1004.0, tree=(TREE[0], replace(TREE[1], gdi_objects=9_000))))
    assert (hang.cause, hang.source) == ("hang", "gdi_objects")
    assert hang.step("journal").args["action"] == "watchdog_hang"


def test_crash_files_event_log_and_client_log():
    assert classify_crash_file(r"C:\thinkorswim\hs_err_pid6120.log").cause == "jvm_crash"
    assert classify_crash_file("/dumps/java_pid6120.hprof").source == "heap_dump"
    assert classify_crash_file("/wer/javaw.exe.6120.dmp").source == "wer_dump"
    assert classify_crash_file("/logs/usergui.log") is None
    r = Watchdog(config()).sample(1002.0, obs(crash_files=(r"C:\thinkorswim\hs_err_pid6120.log",)))
    assert (r.cause, r.source) == ("jvm_crash", "crash_file")
    # a heap dump is oom even while the process is alive, and oom wins over the crash file
    both = Watchdog(config()).sample(1002.0, obs(crash_files=("hs_err_pid6120.log", "java_pid6120.hprof")))
    assert (both.cause, both.source) == ("oom", "heap_dump")
    ev = Watchdog(config()).sample(1002.0, obs(event_log=(EventLogEntry(1002, 6120, "t"),)))
    assert (ev.cause, ev.source) == ("hang", "event_log")
    assert Watchdog(config()).sample(1002.0, obs(event_log=(EventLogEntry(1000, 999, "t"),))) is None   # not the tree
    assert Watchdog(config()).sample(1002.0, obs(event_log=(EventLogEntry(1000, None, "t"),))).cause == "jvm_crash"
    assert Watchdog(config()).sample(1002.0, obs(log_oom=True)).source == "client_log"
    # no app detector before launch (provisioned) or after teardown
    assert Watchdog(config()).sample(1002.0, obs(heartbeat=hb("provisioned", mono=1001.0),
                                                 crash_files=("hs_err_pid1.log",))) is None


def test_screenshot_staleness_is_the_hang_detector_and_anti_anchors_suppress_it():
    expect = (T0 + timedelta(seconds=5)).isoformat()                               # mono 1005
    beat = dict(expect_screen_change_since=expect, max_wait_s=20)
    w = Watchdog(config())
    assert w.sample(1004.0, obs(1004.0, hb(mono=1004.0, **beat), frame_hash="aaa")) is None
    assert w.sample(1064.0, obs(1064.0, hb(mono=1063.0, **beat), frame_hash="aaa")) is None   # 59 s after expect
    r = w.sample(1065.0, obs(1065.0, hb(mono=1064.0, **beat), frame_hash="aaa"))
    assert (r.cause, r.source) == ("hang", "screenshot_staleness")
    moved = Watchdog(config())
    moved.sample(1004.0, obs(1004.0, hb(mono=1004.0, **beat), frame_hash="aaa"))
    moved.sample(1010.0, obs(1010.0, hb(mono=1009.0, **beat), frame_hash="bbb"))  # the screen changed after expect
    assert moved.sample(1100.0, obs(1100.0, hb(mono=1099.0, **beat), frame_hash="bbb")) is None
    spinner = Watchdog(config())
    spinner.sample(1004.0, obs(1004.0, hb(mono=1004.0, **beat), frame_hash="aaa"))
    assert spinner.sample(1100.0, obs(1100.0, hb(mono=1099.0, **beat), frame_hash="aaa", anti_anchor=True)) is None
    secret = Watchdog(config())                                                    # capture_suspended: no hash, no hang
    secret.sample(1004.0, obs(1004.0, hb(mono=1004.0, **beat), frame_hash="aaa"))
    assert secret.sample(1100.0, obs(1100.0, hb(mono=1099.0, capture_suspended=True, **beat))) is None
    assert Watchdog(config()).sample(1002.0, obs(hung_window=True)).source == "is_hung_app_window"
    assert Watchdog(config()).sample(1002.0, obs(jab_stalled=True)).source == "jab_timeout"


def test_empty_tree_is_process_exit_only_for_an_app_the_watchdog_saw_alive():
    w = Watchdog(config())
    assert w.sample(1002.0, obs()) is None
    r = w.sample(1004.0, obs(1004.0, tree=(), main_frame=None, launcher_update=True))
    assert (r.cause, r.source) == ("process_exit", "empty_tree") and r.signal.launcher_update
    # the reaction killed that app: nothing more until the relaunch names a new app pid
    assert w.sample(1006.0, obs(1006.0, tree=(), main_frame=None, crash_files=("hs_err_pid6120.log",))) is None
    relaunched = hb(mono=1007.0, app_pid=7000)
    assert w.sample(1008.0, obs(1008.0, relaunched, crash_files=("hs_err_pid7000.log",))).cause == "jvm_crash"
    # the Orchestrator cleared app_pid before killing the tree itself (choice 3): no app failure
    own = Watchdog(config())
    own.sample(1002.0, obs())
    assert own.sample(1004.0, obs(1004.0, hb(mono=1003.0, app_pid=None), tree=(), main_frame=None)) is None


# -- frame rules ---------------------------------------------------------------------------------------------------
def test_no_main_frame_for_30_s_while_running_is_main_frame_missing_once():
    w = Watchdog(config())
    assert w.sample(1000.0, obs(1000.0, main_frame=None)) is None
    assert w.sample(1029.0, obs(1029.0, main_frame=None)) is None
    r = w.sample(1030.0, obs(1030.0, main_frame=None))
    assert r.trigger == "main_frame_missing" and r.steps == ()
    assert w.sample(1040.0, obs(1040.0, main_frame=None)) is None
    w.sample(1042.0, obs(1042.0))                                                  # the frame is back
    assert w.sample(1072.0, obs(1072.0, main_frame=None)).trigger == "main_frame_missing"


def test_no_frame_five_minutes_after_launch_with_the_install_folder_alive_fails_launched():
    w = Watchdog(config())
    launching = dict(main_frame=None, install_alive=True)
    assert w.sample(1000.0, obs(1000.0, hb("launched", mono=999.0), **launching)) is None
    assert w.sample(1299.0, obs(1299.0, hb("launched", mono=1298.0), **launching)) is None
    r = w.sample(1300.0, obs(1300.0, hb("launched", mono=1299.0), **launching))
    assert r.trigger == "launched_failed" and r.step("signal").args["code"] == "launched_failed"
    assert w.sample(1302.0, obs(1302.0, hb("launched", mono=1301.0), **launching)) is None
    nothing = Watchdog(config())
    nothing.sample(1000.0, obs(1000.0, hb("launched", mono=999.0), main_frame=None, install_alive=False))
    assert nothing.sample(1300.0, obs(1300.0, hb("launched", mono=1299.0), main_frame=None,
                                      install_alive=False)) is None


def test_tree_rules():
    stray = ProcessInfo(6200, 1, "updater.exe", exe=INSTALL + r"\updater.exe")
    gone = ProcessInfo(6300, 1, "x.exe", alive=False)
    tree = app_tree(TREE, (stray, TREE[1], gone))
    assert [p.pid for p in tree] == [6100, 6120, 6200] and tree_roots(tree) == (6100, 6200)
    wins = (WindowInfo(1, 6120, "SunAwtDialog", TITLE), WindowInfo(2, 6120, "SunAwtFrame", "Login"), FRAME)
    assert main_frame(wins, r"Main@thinkorswim \[build \d+\]") is FRAME
    assert main_frame(wins[:2], r"Main@thinkorswim") is None


# -- kill switch and dead-man --------------------------------------------------------------------------------------
def test_kill_switch_second_half_on_the_flag_once_per_request():
    req = KillRequest(rig_id="rig-02", channel="cli", requested_by="ops", reason="stop", at="2026-11-09T19:00:03Z")
    w = Watchdog(config())
    r = w.sample(1002.0, obs(kill_requests={"file_flag": req}))
    assert r.trigger == "kill_switch" and r.names == ("deny_all", "reset_input_state", "kill_tree", "journal", "signal")
    assert r.decision.run_status == "aborted_safety" and r.decision.run_reason == "kill_switch"
    assert r.step("journal").args["action"] == "kill_switch" and r.step("signal").args["code"] == "kill_switch"
    assert w.sample(1004.0, obs(1004.0, kill_requests={"file_flag": req})) is None
    # a dead Orchestrator while the ticket flag is set: the kill switch, and the resume runs the recovery
    d = Watchdog(config())
    k = d.sample(1010.0, obs(1010.0, hb(mono=1009.0), orchestrator_alive=False, ticket_flag=True))
    assert k.trigger == "kill_switch" and k.names[-1] == "start_resume" and "kill_hook_host" not in k.names
    assert k.decision.alert == alerts.safety_abort() and d.budgets.used_orchestrator == 0   # choice 7


def test_local_dead_man_kills_and_runs_finalize_once():
    w = Watchdog(config())
    assert w.dead_man_at == T0 + timedelta(minutes=50)                              # PR: deadline plus 15
    w.sample(1002.0, obs())
    at = 1000.0 + 50 * 60
    r = w.sample(at, obs(at, hb(mono=at - 1)))
    assert r.trigger == "dead_man" and r.decision.run_reason == "dead_man"
    assert r.names == ("screenshot", "collect", "kill_orchestrator", "reset_input_state", "kill_tree", "kill_hook_host",
                       "journal", "start_finalize")
    assert r.step("start_finalize").args["argv"] == ("--resume", RUN, "--shard-dir", "/rig/run/shard", "--finalize",
                                                     "--state", "running", "--reached-running")
    assert r.step("journal").args["detail"].startswith("finalize")
    assert w.finished and w.sample(at + 2, obs(at + 2, hb(mono=at + 1))) is None
    early = Watchdog(config())
    late = 1000.0 + 50 * 60
    r2 = early.sample(late, obs(late, hb("workspace_loaded", mono=late - 1)))
    assert r2.decision.run_status == "complete_with_env_errors" and "--reached-running" not in \
        r2.step("start_finalize").args["argv"]
    done = Watchdog(config())
    assert done.sample(late, obs(late, hb("reported", mono=late - 1))) is None


# -- watchdog.json and the journal -------------------------------------------------------------------------------
def test_watchdog_json_older_than_30_s_aborts_the_run(tmp_path):
    path = tmp_path / "watchdog.json"
    assert status_check(path, now=10.0, pid_alive=lambda p: True) == (float("inf"), False)
    StatusWriter(path).write(WatchdogStatus(pid=4500, mono_ts=1000.0, ts="2026-11-09T19:00:00Z", free_bytes=10 * GIB))
    assert watchdog_check(*status_check(path, now=1030.0, pid_alive=lambda p: p == 4500)) is None
    stale = watchdog_check(*status_check(path, now=1031.0, pid_alive=lambda p: True))
    assert stale.run_reason == "watchdog_dead" and stale.unreached.reason == "watchdog_dead"
    assert watchdog_check(*status_check(path, now=1001.0, pid_alive=lambda p: False)).run_reason == "watchdog_dead"
    assert read_status(path).free_bytes == 10 * GIB and not list(tmp_path.glob("*.tmp"))
    (tmp_path / "torn.json").write_text("{")
    assert read_status(tmp_path / "torn.json") is None
    assert not StatusWriter(tmp_path / "torn.json" / "watchdog.json").write(WatchdogStatus(1, 1.0, "t"))


def test_crash_signal_round_trips_through_the_last_trigger():
    r = Reaction("app_failure", (), "cause: oom (heap_dump)", cause="oom", source="heap_dump",
                 signal=classify_crash_file("java_pid1.hprof"))
    s = WatchdogStatus(1, 2.0, "t", last_trigger=r.trigger_record("wd-0001", 2.0))
    sig = crash_signal_of(s)
    assert (sig.cause, sig.source) == ("oom", "heap_dump")
    assert crash_signal_of(WatchdogStatus(1, 2.0, "t", last_trigger={"trigger": "main_frame_missing"})) is None
    assert crash_signal_of(None) is None


def test_journal_lines_parse_back_into_step_events(tmp_path):
    j = Journal(tmp_path / "watchdog_events.jsonl")
    j.append(j.entry("watchdog_hang", at=T0, mono=184233.118, detail="cause: hang", heartbeat=hb()))
    j.append(j.entry("resume", at=T0 + timedelta(seconds=5), mono=184238.0, detail="cause: orchestrator_died"))
    j.append(j.entry("resume", at=T0 + timedelta(seconds=9), mono=184242.0, detail="finalize: dead-man deadline"))
    events = read_journal(j.path, "watchdog", run_id=RUN, shard_id=SHARD)
    assert [(e.action, e.reason, e.source, e.mode) for e in events] == [
        ("watchdog_hang", "wd-0001: cause: hang", "watchdog", "system"),
        ("resume", "wd-0002: cause: orchestrator_died", "watchdog", "system"),
        ("resume", "wd-0003: finalize: dead-man deadline", "watchdog", "system")]
    assert events[0].checkpoint_id == CID and events[0].mono_ms == 184233118
    again = Journal(j.path)                                    # a restarted reader continues the ids
    assert again.entry("relaunch", at=T0, mono=1.0)["event_id"] == "wd-0004" and again.restarts() == 1
    with pytest.raises(ValueError):
        j.entry("main_frame_missing", at=T0, mono=1.0)                              # not in the closed vocabulary


# -- running a reaction ------------------------------------------------------------------------------------------
class FakeProcesses:
    def __init__(self, trees=None, crash=(), survive=()):
        self.trees, self.crash, self.survive = trees or {}, list(crash), set(survive)
        self.killed: list[int] = []

    def process_tree(self, root_pid):
        return tuple(self.trees.get(root_pid, ()))

    def kill_tree(self, root_pid):
        self.killed.append(root_pid)
        return root_pid not in self.survive

    def crash_files(self, since):
        out, self.crash = tuple(self.crash), []
        return out


class FakeHost:
    def __init__(self, *, dump="ok", client_log="log-1", alive=(4412, 4420), named=None, folder=()):
        self.calls: list[tuple] = []
        self.dump, self.client_log, self.alive, self.named, self.folder = dump, client_log, set(alive), named, folder

    def _log(self, *call):
        self.calls.append(call)

    def processes_in(self, folder):
        return tuple(self.folder)

    def pid_alive(self, pid):
        return pid in self.alive

    def free_bytes(self):
        return 50 * GIB

    def event_log(self, since):
        return ()

    def client_log_oom(self, since):
        return False

    def version_changed(self, since):
        return (False, False)

    def hung(self, hwnd):
        return False

    def anti_anchor(self, frame):
        return False

    def ticket_flag(self):
        return False

    def named_event(self):
        code, self.named = self.named, None
        return code

    def screenshot(self):
        self._log("screenshot")
        return "shot-1"

    def thread_dump(self, pid, timeout_s):
        self._log("thread_dump", pid, timeout_s)
        return ThreadDump(self.dump, "dump-1" if self.dump == "ok" else None)

    def collect(self, since, gateway_events):
        self._log("collect", since, gateway_events)
        return Collection(client_log=self.client_log, hs_err=("hs-1",), heartbeats=("hb-1", "wd-1"),
                          gateway_events="gw-1")

    def reset_input_state(self):
        self._log("reset_input_state")

    def deny_all(self):
        self._log("deny_all")

    def signal(self, code):
        self._log("signal", code)

    def start(self, argv):
        self._log("start", tuple(argv))

    def ship_as_is(self, shard_id):
        self._log("ship_as_is", shard_id)

    def write_event_log(self, event_id, message):
        self._log("event_log", event_id)


def test_reaction_runs_in_order_with_unavailable_notes_and_one_journal_line(tmp_path):
    host, procs = FakeHost(dump="attach_disabled", client_log=None), FakeProcesses(survive={6100})
    j = Journal(tmp_path / "watchdog_events.jsonl")
    r = Watchdog(config(tmp_path)).sample(1121.0, obs(1121.0, hb(mono=1000.0)))
    rep = run_reaction(r, host=host, processes=procs, journal=j, at=T0, mono=1121.0, heartbeat=hb(mono=1000.0))
    assert [c[0] for c in host.calls] == ["screenshot", "thread_dump", "collect", "reset_input_state", "start"]
    assert procs.killed == [4412, 6100, 4420]                                      # Orchestrator, tree, hook host last
    assert set(rep.notes) == {"thread dump unavailable: attach disabled", "client log: not located",
                              "kill: a process from the install folder survived"} and rep.survivors
    lines = j.path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["action"] == "resume"
    assert "thread dump unavailable: attach disabled" in json.loads(lines[0])["detail"]
    assert rep.journal_line["event_id"] == "wd-0001"
    assert {"kind": "screenshot", "ref": "shot-1"} in rep.manifest()
    timeout = run_reaction(Reaction("app_failure", (Step("thread_dump", {"pid": 6120, "timeout_s": 15.0}),), "x"),
                           host=FakeHost(dump="timeout"), processes=procs, journal=j, at=T0, mono=1.0, heartbeat=None)
    assert timeout.notes == ("thread dump unavailable: timed out after 15 s",)
    none = run_reaction(Reaction("app_failure", (Step("thread_dump", {"pid": None, "timeout_s": 15.0}),), "x"),
                        host=host, processes=procs, journal=j, at=T0, mono=1.0, heartbeat=None)
    assert none.notes == ("thread dump unavailable: no app process",)


def test_a_capture_suspended_reaction_takes_no_screenshot():
    w = Watchdog(config())
    r = w.sample(1121.0, obs(1121.0, hb(mono=1000.0, capture_suspended=True)))
    assert "screenshot" not in r.names and r.names[0] == "thread_dump"


def test_a_failed_journal_append_writes_event_log_9001_and_raises_the_named_event(tmp_path):
    (tmp_path / "blocked").write_text("a file where the directory should be")
    host = FakeHost()
    j = Journal(tmp_path / "blocked" / "watchdog_events.jsonl")
    r = Reaction("app_failure", (Step("journal", {"action": "watchdog_crash", "detail": "cause: oom"}),
                                 Step("signal", {"code": "app_failure"})), "cause: oom")
    rep = run_reaction(r, host=host, processes=FakeProcesses(), journal=j, at=T0, mono=1.0, heartbeat=None)
    assert rep.journal_line is None
    assert host.calls == [("event_log", 9001), ("signal", "journal_failed"), ("signal", "app_failure")]


# -- the loop ------------------------------------------------------------------------------------------------------
class FakeDesktop:
    def __init__(self, windows=(FRAME,)):
        self.windows, self.grabs = windows, 0

    def enumerate_windows(self):
        return tuple(self.windows)

    def grab(self):
        self.grabs += 1
        return Frame(np.zeros((30, 40, 3), dtype=np.uint8), (0, 0), 0.0)


def write_hb(tmp_path, beat: Heartbeat) -> None:
    (tmp_path / "heartbeat.json").write_text(json.dumps(beat.__dict__))


def test_loop_writes_watchdog_json_every_tick_and_grabs_nothing_while_capture_is_suspended(tmp_path):
    clock = ManualClock(T0, mono=1000.0)
    desktop, procs = FakeDesktop(), FakeProcesses(trees={6100: TREE, 6120: TREE[1:]})
    host = FakeHost(folder=TREE[:1])
    loop = WatchdogLoop(config(tmp_path), desktop=desktop, processes=procs, host=host, clock=clock, pid=4500)
    write_hb(tmp_path, hb(mono=999.0, capture_suspended=True))
    assert loop.tick() == (None, None) and desktop.grabs == 0
    st = read_status(tmp_path / "watchdog.json")
    assert (st.pid, st.mono_ts, st.last_trigger, st.free_bytes) == (4500, 1000.0, None, 50 * GIB)
    clock.advance(2.0)
    write_hb(tmp_path, hb(mono=1001.0))
    loop.tick()
    assert desktop.grabs == 1 and loop.watchdog._hash is not None
    # an orchestrator death: the reaction runs and the last trigger names its journal line and evidence
    host.alive.discard(4412)
    clock.advance(2.0)
    reaction, report = loop.tick()
    assert reaction.trigger == "orchestrator_died" and ("start", ("--resume", RUN, "--shard-dir", str(tmp_path))) \
        in host.calls
    st = read_status(tmp_path / "watchdog.json")
    assert st.last_trigger["trigger"] == "orchestrator_died" and st.last_trigger["event_id"] == "wd-0001"
    assert {"kind": "client_log", "ref": "log-1"} in st.last_trigger["evidence"]
    assert len((tmp_path / "watchdog_events.jsonl").read_text().splitlines()) == 1
    # a restarted Watchdog seeds its restart budget from the journal
    assert WatchdogLoop(config(tmp_path), desktop=desktop, processes=procs, host=host,
                        clock=clock).watchdog.budgets.used_orchestrator == 1


def test_loop_reads_self_abort_and_the_kill_flag_and_runs_until_stopped(tmp_path):
    clock = ManualClock(T0, mono=1000.0)
    host = FakeHost(named="self_abort")
    loop = WatchdogLoop(config(tmp_path), desktop=FakeDesktop(), processes=FakeProcesses(trees={6100: TREE}),
                        host=host, clock=clock, pid=4500)
    write_hb(tmp_path, hb(mono=999.0))
    stop = threading.Event()
    ticks = []
    real_tick = loop.tick

    def counted():
        ticks.append(clock.monotonic())
        if len(ticks) == 70:
            stop.set()
        return real_tick()
    loop.tick = counted
    loop.run(stop)
    assert len(ticks) == 70 and ticks[1] - ticks[0] == 2.0
    assert loop.watchdog.self_abort and loop.last_trigger is None                  # 138 s of stale heartbeat: no hang
