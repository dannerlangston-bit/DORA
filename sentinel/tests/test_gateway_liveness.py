"""Hook liveness probe and the reinstall rule (5.4.4), into the fail-closed sequence of 5.4.2."""
from __future__ import annotations

import pytest

from sentinel.gateway import segment as sg
from sentinel.gateway.hookhost_loop import HookHost
from sentinel.gateway.liveness import (PROBE_INTERVAL_S, HookRemovals, ProbeScheduler, judge_probe,
                                       judge_reinstall)
from sentinel.gateway.segment import HookEvent, RingReader, SegmentWriter, read_header, to_ns, to_seconds
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.gateway import Flag, GatewayState

T = 500.0


# -- the schedule ---------------------------------------------------------------------------------------------
def test_probe_due_every_5s_while_idle_and_before_every_step_batch():
    s = ProbeScheduler()
    assert s.due(T, idle=True)                                                     # never probed yet
    s.sent(T)
    assert not s.due(T + 10, idle=True, before_batch=True)                          # one in flight
    assert s.judge(T + 0.02, T + 0.05) == "seen"
    assert not s.due(T + 4.9, idle=True)
    assert s.due(T + PROBE_INTERVAL_S, idle=True)
    assert not s.due(T + 60, idle=False)                                           # mid-step: not idle
    assert s.due(T + 1, before_batch=True)                                         # before every batch
    assert s.due(T + 1, integrity_drop=True)                                       # stage 7: at once
    s.sent(T + 1)
    assert s.judge(float("-inf"), T + 1.05) == "pending" and s.in_flight == T + 1
    assert s.judge(float("-inf"), T + 1.2) == "missed" and s.in_flight is None
    assert not s.due(T + 5.9, idle=True) and s.due(T + 6, idle=True)                # the clock restarts at a probe
    with pytest.raises(ValueError):
        s.judge(T, T)


def test_judge_probe_window_is_100ms_and_a_late_stamp_is_a_removal():
    assert judge_probe(T, T + 0.05, T + 0.06) == "seen"
    assert judge_probe(T, T + 0.1, T + 0.2) == "seen"
    assert judge_probe(T, T + 0.15, T + 0.15) == "missed"                           # stamped 150 ms late
    assert judge_probe(T, T - 3.0, T + 0.08) == "pending"                           # an older probe's stamp
    assert judge_probe(T, T - 3.0, T + 0.11) == "missed"


def test_judge_reinstall_answers_and_timeout():
    assert judge_reinstall(2, 2, True, asked_at=T, now=T + 0.1) == "ok"
    assert judge_reinstall(2, 2, False, asked_at=T, now=T + 0.1) == "failed"
    assert judge_reinstall(2, 1, True, asked_at=T, now=T + 0.5) == "pending"
    assert judge_reinstall(2, 1, True, asked_at=T, now=T + 1.1) == "failed"


# -- removals -------------------------------------------------------------------------------------------------
def test_first_removal_reinstalls_with_a_finding_and_an_alert():
    b = ShardBudgets()
    v = HookRemovals(b).probe_missed(step_id="s3")
    assert v.reinstall and v.result.passed and v.result.run is None
    assert [f.kind for f in v.result.findings] == ["gateway_integrity"]
    assert v.result.alert is not None and v.result.alert.rule == "harness_error"
    assert b.used_hook_host == 0


def test_the_next_probe_missing_is_a_dead_hook_host_then_the_second_restart_aborts():
    b = ShardBudgets()
    r = HookRemovals(b)
    r.probe_missed(step_id="s1")
    v = r.probe_missed(step_id="s2")                                                # the next probe failed
    assert not v.reinstall and b.used_hook_host == 1
    assert v.result.has("restart_hook_host") and v.result.has("kill_tree") and v.result.run is None
    assert (v.result.checkpoint.outcome, v.result.checkpoint.reason) == ("harness_error", "hook_lost")
    assert v.result.step == "blocked_by_gateway" and v.result.step_reason == "hook_host_dead"
    v = r.probe_missed(step_id="s9")                                                # after the restart: per shard
    assert (v.result.run_status, v.result.run_reason) == ("aborted_harness", "hook_lost")


def test_a_failed_reinstall_is_a_dead_hook_host_at_once():
    b = ShardBudgets()
    r = HookRemovals(b)
    assert r.probe_missed(step_id="s1").reinstall
    v = r.reinstall_failed(step_id="s1")
    assert not v.reinstall and v.result.has("restart_hook_host") and b.used_hook_host == 1


# -- end to end over the segment and the hook host loop -------------------------------------------------------
class OS:
    def __init__(self, now_ns):
        self.now = now_ns
        self.reinstall_ok = True

    def monotonic_ns(self):
        return self.now

    def pid_alive(self, pid):
        return True

    def exit_signalled(self):
        return False

    def reinstall_hooks(self):
        return self.reinstall_ok


def test_probe_through_the_segment_then_a_removal_reinstalled_by_the_hook_host():
    buf = bytearray(sg.SEGMENT_SIZE)
    sg.init_segment(buf, orchestrator_pid=1)
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    w = SegmentWriter(buf)
    w.publish(g)
    os = OS(to_ns(T))
    host = HookHost(buf, os)
    sched, removals = ProbeScheduler(), HookRemovals(ShardBudgets())
    # a probe the hook sees
    assert sched.due(T, before_batch=True)
    w.issue_probe(5, 5, to_ns(T))
    sched.sent(T)
    os.now = to_ns(T + 0.003)
    assert host.callback(HookEvent("move", 5, 5))
    assert sched.judge(to_seconds(read_header(buf).last_probe_mono_ts), T + 0.01) == "seen"
    # the hook is removed: the next probe's move never reaches the callback
    w.issue_probe(5, 5, to_ns(T + 5))
    sched.sent(T + 5)
    assert sched.judge(to_seconds(read_header(buf).last_probe_mono_ts), T + 5.15) == "missed"
    v = removals.probe_missed(step_id="s1")
    assert v.reinstall
    req = w.request_reinstall()
    os.now = to_ns(T + 5.2)
    assert host.tick() is None
    h = read_header(buf)
    assert judge_reinstall(req, h.reinstall_done, h.reinstall_ok, asked_at=T + 5.15, now=T + 5.2) == "ok"
    assert [(r.event_type, r.reason, r.aux) for r in RingReader().read(buf)][-1] == ("control", "hook_reinstalled", 1)
