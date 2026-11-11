"""The heartbeat's cadence and content and the app's age tags (10.2; executor/heartbeat.py)."""
from __future__ import annotations

import errno
import json

import pytest

from sentinel.executor.heartbeat import AppLife, HeartbeatPolicy, expect_since_mono, hang_detected, read_heartbeat
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.watchdog import HeartbeatUnwritable, HeartbeatWriter


class Recording(HeartbeatWriter):
    """A writer that keeps every heartbeat it wrote (and writes the file)."""

    def __init__(self, path, clock):
        super().__init__(path, clock)
        self.written = []

    def write(self, hb):
        super().write(hb)
        self.written.append(hb)


class Failing(HeartbeatWriter):
    def __init__(self, path, clock, code=errno.EROFS):
        super().__init__(path, clock)
        self.code = code
        self.calls = 0

    def write(self, hb):
        self.calls += 1
        raise HeartbeatUnwritable(self.code, "disk")


def policy(tmp_path, clock=None, writer_cls=Recording, **kw):
    clock = clock or ManualClock()
    w = writer_cls(tmp_path / "heartbeat.json", clock)
    return HeartbeatPolicy(w, clock, run_id="pr-1-1", shard=2, orchestrator_pid=4412, hook_host_pid=4420, **kw), w, clock


def test_a_step_writes_at_its_start_and_its_end(tmp_path):
    p, w, _ = policy(tmp_path)
    assert p.step_start("flow.a.b", "s01") is None
    assert p.step_end() is None
    assert len(w.written) == 2
    data = json.loads((tmp_path / "heartbeat.json").read_text())
    assert data["flow_id"] == "flow.a.b" and data["step_id"] == "s01" and data["state"] == "running"
    assert data["orchestrator_pid"] == 4412 and data["hook_host_pid"] == 4420


def test_a_long_wait_writes_no_more_than_ten_seconds_apart(tmp_path):
    p, w, clock = policy(tmp_path)
    p.step_start("flow.a.b", "w1")
    assert p.sleep(25.0) is None
    monos = [hb.mono_ts for hb in w.written]
    assert len(monos) >= 3                               # the start plus two periodic writes (10 s, 20 s)
    assert all(b - a <= 10.0 for a, b in zip(monos, monos[1:]))
    assert clock.monotonic() - monos[-1] <= 10.0


def test_tick_writes_only_when_the_last_write_is_ten_seconds_old(tmp_path):
    p, w, clock = policy(tmp_path)
    p.step_start("f", "s")
    clock.advance(9.9)
    p.tick()
    assert len(w.written) == 1
    clock.advance(0.1)
    p.tick()
    assert len(w.written) == 2


def test_expect_screen_change_is_set_by_input_and_cleared_by_settle(tmp_path):
    p, w, clock = policy(tmp_path)
    p.step_start("f", "s1")
    assert w.written[-1].expect_screen_change_since is None
    p.expect_screen_change()
    p.step_end()
    since = w.written[-1].expect_screen_change_since
    assert since == clock.now().isoformat()
    clock.advance(1)
    p.screen_settled("ab" * 32)
    p.step_end()
    assert w.written[-1].expect_screen_change_since is None
    assert w.written[-1].last_capture_sha256 == "ab" * 32


def test_capture_suspended_only_inside_a_secret_step(tmp_path):
    p, w, _ = policy(tmp_path)
    with p.secret_step():
        p.step_start("flow.login", "pw")
    p.step_end()
    p.step_start("flow.login", "s9")
    assert [hb.capture_suspended for hb in w.written] == [True, False, False]


def test_checkpoint_max_wait_and_the_app_pid_ride_along(tmp_path):
    life = AppLife()
    life.launched(1000.0, pid=6120)
    p, w, _ = policy(tmp_path, life=life)
    p.step_start("f", "cp1", checkpoint_id="flow/a.b/flow.a.b/cp1", max_wait_s=20)
    hb = read_heartbeat(tmp_path / "heartbeat.json")
    assert hb.max_wait_s == 20 and hb.app_pid == 6120 and hb.checkpoint_id == "flow/a.b/flow.a.b/cp1"


def test_gate_writes_the_state_and_clears_the_step(tmp_path):
    p, w, _ = policy(tmp_path)
    p.step_start("f", "s1")
    p.gate("teardown")
    assert w.written[-1].state == "teardown" and w.written[-1].step_id is None


def test_a_failed_write_is_returned_once_and_nothing_is_written_after(tmp_path):
    p, w, _ = policy(tmp_path, writer_cls=Failing)
    r = p.step_start("f", "s1")
    assert r is not None and r.run_status == "aborted_harness" and r.run_reason == "heartbeat_unwritable"
    assert r.has("self_abort")
    assert p.step_end() is None and p.tick() is None and p.sleep(30) is None
    assert w.calls == 1


def test_an_enospc_write_asks_for_the_free_space_check(tmp_path):
    clock = ManualClock()
    w = Failing(tmp_path / "hb.json", clock, code=errno.ENOSPC)
    p = HeartbeatPolicy(w, clock, run_id="r", shard=1)
    r = p.gate("running")
    assert r is not None and r.run is None and r.has("free_space_check")


def test_hang_detection_reads_the_expectation_from_the_heartbeat(tmp_path):
    p, w, clock = policy(tmp_path)
    p.step_start("f", "cp1", checkpoint_id="flow/a.b/flow.a.b/cp1", max_wait_s=15)
    clock.advance(2)
    p.expect_screen_change()
    since = clock.monotonic()
    p.step_end()
    hb = read_heartbeat(tmp_path / "heartbeat.json")
    assert expect_since_mono(hb) == pytest.approx(since)
    clock.advance(59)
    assert not hang_detected(hb, now=clock.monotonic(), last_hash_change=since - 1, anti_anchor=False)
    clock.advance(1)                                    # max(2 x 15, 60) = 60 s since the input
    assert hang_detected(hb, now=clock.monotonic(), last_hash_change=since - 1, anti_anchor=False)
    assert not hang_detected(hb, now=clock.monotonic(), last_hash_change=since + 1, anti_anchor=False)
    assert not hang_detected(hb, now=clock.monotonic(), last_hash_change=since - 1, anti_anchor=True)
    assert p.hang_detected(now=clock.monotonic(), last_hash_change=since - 1, anti_anchor=False)


def test_no_hang_while_capture_is_suspended_or_nothing_is_expected(tmp_path):
    p, w, clock = policy(tmp_path)
    with p.secret_step():
        p.expect_screen_change()
        p.step_start("flow.login", "pw", max_wait_s=15)
    clock.advance(120)
    assert not p.hang_detected(now=clock.monotonic(), last_hash_change=0.0, anti_anchor=False)
    p.screen_settled()
    p.step_end()
    assert not p.hang_detected(now=clock.monotonic() + 600, last_hash_change=0.0, anti_anchor=False)


def test_app_life_tags_and_the_relaunch_reset():
    life = AppLife()
    assert life.tags(5.0) == (None, 0)
    life.launched(100.0, pid=1)
    assert life.tags(160.5) == (60, 0)
    life.relaunched(200.0, pid=2)
    assert life.tags(203.0) == (3, 1) and life.app_pid == 2
    life.relaunched(300.0)
    assert life.tags(300.0) == (0, 2)


def test_period_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        HeartbeatPolicy(None, ManualClock(), run_id="r", shard=1, period_s=0)
