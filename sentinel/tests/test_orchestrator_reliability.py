"""Watchdog decisions, restart and re-login budgets, the dead-man and degraded modes (10.2, 7.7, 5.2, 10.7)."""
from __future__ import annotations

import errno
import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator.budgets import (RATE_CAPS, REPORT_MARGIN_S, WALL_BUDGET_S, RateCounters, ReloginBudget,
                                           ShardBudgets, run_budget, session_kicked, wall_budget_s)
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.degraded import (ModelAvailability, NetworkPause, SpendTracker, agent_only_step,
                                            locator_failed, model_preflight, rig_lost, rig_silent, verdict_outcome)
from sentinel.orchestrator.results import Outcome
from sentinel.orchestrator.rig import CrashSignal
from sentinel.orchestrator.watchdog import (Heartbeat, HeartbeatUnwritable, HeartbeatWriter, app_failure,
                                            crash_signal, dead_man_deadline, dead_man_finalize, disk_check,
                                            edt_frozen, heartbeat_failed, hook_host_died, orchestrator_died,
                                            orchestrator_hung, proactive_relaunch_due, resolve_detectors,
                                            watchdog_check)

CRASH = CrashSignal("jvm_crash", "crash_file")


# -- heartbeats -------------------------------------------------------------------------------------------
def test_heartbeat_staleness_thresholds_by_state():
    assert not orchestrator_hung("running", 120)
    assert orchestrator_hung("running", 121)
    assert not orchestrator_hung("launched", 299) and orchestrator_hung("provisioned", 301)
    assert orchestrator_hung("settings_attested", 121)


def test_watchdog_reverse_heartbeat():
    assert watchdog_check(10, True) is None
    for age, alive in ((31, True), (5, False)):
        r = watchdog_check(age, alive)
        assert r.run_reason == "watchdog_dead" and r.unreached.reason == "watchdog_dead" and r.has("teardown")
        assert r.alert == alerts.aborted_harness()


def test_heartbeat_writer_is_atomic_and_retries_once(tmp_path):
    clock = ManualClock()
    w = HeartbeatWriter(tmp_path / "shard-1" / "heartbeat.json", clock)
    w.write(Heartbeat("pr-1", 1, 1, "running", "2026-11-09T14:03:21-05:00", 1.5, step_id="s07"))
    data = json.loads((tmp_path / "shard-1" / "heartbeat.json").read_text())
    assert data["state"] == "running" and data["step_id"] == "s07" and data["capture_suspended"] is False
    assert not list((tmp_path / "shard-1").glob("*.tmp"))
    (tmp_path / "blocked").write_text("a file where the directory should be")
    bad = HeartbeatWriter(tmp_path / "blocked" / "heartbeat.json", clock)
    t0 = clock.monotonic()
    with pytest.raises(HeartbeatUnwritable):
        bad.write(Heartbeat("pr-1", 1, 1, "running", "t", 1.0))
    assert clock.monotonic() - t0 == 1.0


def test_heartbeat_failure_outcomes_and_disk():
    r = heartbeat_failed(OSError(errno.EACCES, "read-only"))
    assert r.run_reason == "heartbeat_unwritable" and r.has("self_abort")
    full = heartbeat_failed(OSError(errno.ENOSPC, "full"))
    assert full.run is None and full.has("free_space_check")
    assert disk_check(50 * 1024 ** 3, 20 * 1024 ** 3) is None
    warn = disk_check(10 * 1024 ** 3, 20 * 1024 ** 3)
    assert warn.passed and warn.run is None
    stop = disk_check(1024 ** 3, 20 * 1024 ** 3)
    assert stop.run_reason == "disk_full" and stop.unreached.reason == "disk_full"


# -- hangs and app failures -------------------------------------------------------------------------------
def test_edt_freeze_by_screenshot_staleness():
    base = dict(expect_change_since=100.0, last_hash_change=90.0, max_wait_s=20.0, anti_anchor=False,
                capture_suspended=False)
    assert not edt_frozen(now=159.0, **base)                       # max(2 x 20, 60) = 60 s
    assert edt_frozen(now=160.0, **base)
    assert not edt_frozen(now=300.0, **{**base, "anti_anchor": True})
    assert not edt_frozen(now=300.0, **{**base, "capture_suspended": True})
    assert not edt_frozen(now=300.0, **{**base, "last_hash_change": 101.0})
    assert not edt_frozen(now=300.0, **{**base, "expect_change_since": None})
    assert not edt_frozen(now=179.0, **{**base, "max_wait_s": 40.0}) and edt_frozen(now=180.0, **{**base, "max_wait_s": 40.0})


def test_crash_signal_prefers_oom():
    assert crash_signal([]) is None
    assert crash_signal([CRASH, CrashSignal("oom", "heap_dump")]).cause == "oom"


def test_three_app_relaunches_per_shard_then_app_restart_budget():
    b = ShardBudgets()
    for n in range(1, 4):
        r = app_failure(CRASH, b, phase="running")
        assert r.has("relaunch") and r.run is None and b.used_app == n
        sub = {s.selector: s.outcome for s in r.outcomes}
        assert (sub["in_progress"].outcome, sub["in_progress"].reason, sub["in_progress"].detail) == \
            ("assertion_failed", "assertion:app_alive", "jvm_crash")
        assert sub["rest_of_flow"] == Outcome("not_run", "app_crash")
        assert r.findings[0].kind == "app_crash" and r.findings[0].tier == 1
    fourth = app_failure(CRASH, b, phase="running")
    assert fourth.run_status == "aborted_budget" and fourth.run_reason == "app_restart_budget"
    assert fourth.unreached == Outcome("not_run", "budget") and fourth.alert.priority == "P2"


def test_first_launch_crash_is_an_app_failure_never_env_error_f62():
    b = ShardBudgets(app_relaunches=1)
    r = app_failure(CrashSignal("hang", "screenshot_staleness"), b, phase="first_launch")
    assert r.checkpoint.outcome == "assertion_failed" and r.outcomes[0].selector == "first_unreached"
    assert r.has("relaunch") and r.gate == "launched"
    spent = app_failure(CrashSignal("process_exit", "empty_tree"), b, phase="relaunch_gates")
    assert spent.run_reason == "app_restart_budget" and spent.unreached.reason == "budget"


def test_app_failure_carries_the_watchdog_evidence_manifest_for_the_app_crash_finding(tmp_path):
    from sentinel.orchestrator.watchdog_loop import StatusWriter, WatchdogStatus, crash_signal_of, read_status
    manifest = [{"kind": "screenshot", "ref": "sha256:ab"},
                {"kind": "thread_dump", "note": "thread dump unavailable: attach disabled"},
                {"kind": "client_log", "note": "client log: not located"}]
    StatusWriter(tmp_path / "watchdog.json").write(WatchdogStatus(4500, 10.0, "t", last_trigger={
        "trigger": "app_failure", "cause": "hang", "source": "screenshot_staleness", "evidence": manifest}))
    status = read_status(tmp_path / "watchdog.json")
    r = app_failure(crash_signal_of(status), ShardBudgets(), phase="running", artifacts=status.last_trigger["evidence"])
    assert r.findings[0].kind == "app_crash" and r.evidence["artifacts"] == manifest
    assert r.checkpoint.detail == "hang" and r.has("relaunch")
    spent = app_failure(CRASH, ShardBudgets(app_relaunches=0), phase="running", artifacts=manifest[:1])
    assert spent.run_reason == "app_restart_budget" and spent.evidence["artifacts"] == manifest[:1]
    assert app_failure(CRASH, ShardBudgets(), phase="running").evidence == {}


def test_launcher_update_is_version_changed_mid_run_and_consumes_no_relaunch():
    b = ShardBudgets()
    r = app_failure(CrashSignal("process_exit", "empty_tree", launcher_update=True), b, phase="running")
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "launched",
                                                                           "version_changed_mid_run")
    assert not r.findings and b.used_app == 0
    with pytest.raises(ValueError):
        app_failure(CrashSignal("edt_frozen", "x"), b, phase="running")


def test_app_death_in_teardown_relaunches_once_for_the_recovery_login():
    b = ShardBudgets(app_relaunches=1)
    assert app_failure(CRASH, b, phase="teardown").has("recovery_login")
    assert not app_failure(CRASH, b, phase="teardown").actions


def test_two_orchestrator_restarts_per_shard():
    b = ShardBudgets()
    for _ in range(2):
        r = orchestrator_died(b, step_id="s04")
        assert r.has("restart_orchestrator") and r.checkpoint.reason == "orchestrator_died"
        assert {s.selector: s.outcome.reason for s in r.outcomes}["rest_of_flow"] == "step_failed:s04"
    third = orchestrator_died(b, step_id="s05")
    assert third.run_reason == "orchestrator_restart_budget" and third.unreached.reason == "orchestrator_restart_budget"
    td = orchestrator_died(ShardBudgets(), step_id=None, state="teardown")
    assert td.gate == "teardown" and td.checkpoint is None and not td.has("resume_next_flow")


def test_hook_host_restart_has_its_own_budget_of_one():
    b = ShardBudgets()
    r = hook_host_died(b, step_id="s02")
    assert r.step == "blocked_by_gateway" and r.step_reason == "hook_host_dead"
    assert r.checkpoint == Outcome("harness_error", "hook_lost") and r.has("restart_hook_host")
    assert b.used_app == 0 and b.used_hook_host == 1
    second = hook_host_died(b, step_id="s03")
    assert second.run_reason == "hook_lost" and second.unreached.reason == "hook_lost"
    assert hook_host_died(ShardBudgets(), step_id=None, layout_mismatch=True).run_reason == "hook_lost"


def test_proactive_relaunch_and_detector_precedence():
    assert proactive_relaunch_due(40, 10, inside_flow=False)
    assert proactive_relaunch_due(1, 90 * 60, inside_flow=False)
    assert not proactive_relaunch_due(40, 0, inside_flow=True)
    env = Outcome("env_error", "gate", "connectivity")
    alive = Outcome("assertion_failed", "assertion:app_alive", detail="hang")
    harness = Outcome("harness_error", "orchestrator_died")
    assert resolve_detectors([harness, alive, env]) == env
    assert resolve_detectors([harness, alive]) == alive


# -- dead-man ---------------------------------------------------------------------------------------------
def test_dead_man_deadlines_and_finalize_only_after_running():
    d = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    assert dead_man_deadline("pr", d) == d + timedelta(minutes=15)
    assert dead_man_deadline("merge", d) == d + timedelta(minutes=30)
    assert dead_man_deadline("rc", d) == d + timedelta(minutes=60)
    cutoff = datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)
    assert dead_man_deadline("nightly", d, nightly_cutoff=cutoff) == cutoff
    with pytest.raises(ValueError):
        dead_man_deadline("soak", d)
    after = dead_man_finalize("running", True, run_type="merge")
    assert after.run_reason == "dead_man" and after.unreached == Outcome("harness_error", "dead_man")
    assert after.alert.priority == "P1" and dead_man_finalize("running", True, run_type="pr").alert.priority == "P2"
    before = dead_man_finalize("workspace_loaded", False, run_type="pr")
    assert before.run_status == "complete_with_env_errors"
    assert before.unreached == Outcome("env_error", "timeout", "workspace_loaded")


def test_nightly_and_calibration_dead_man_is_the_first_0600_new_york_after_the_pass():
    # SD-98, 10.2: the Nightly's 4 h budget from 22:00 ends 02:00, dead-man 06:00 the same morning
    nightly_end = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)                 # 02:00 EST
    assert dead_man_deadline("nightly", nightly_end) == datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)
    # a weekend Calibration's first night ends about 11:10 Saturday: the first 06:00 after it is Sunday's
    first_night = datetime(2026, 11, 14, 16, 10, tzinfo=timezone.utc)               # Sat 11:10 EST
    assert dead_man_deadline("calibration", first_night) == datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)
    # the Saturday-night sixth pass ends about 00:50 Sunday: 06:00 Sunday
    sixth = datetime(2026, 11, 15, 5, 50, tzinfo=timezone.utc)
    assert dead_man_deadline("calibration", sixth) == datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)
    # an emergency calibration scheduled to end at 06:00 has its dead-man at 06:00, not a day later
    emergency_end = datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)
    assert dead_man_deadline("calibration", emergency_end) == emergency_end
    # summer time: 06:00 EDT is 10:00 UTC
    assert dead_man_deadline("calibration", datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)


def test_finalize_of_a_nightly_or_calibration_never_at_running_is_start_failed_not_env_error():
    for run_type in ("nightly", "calibration"):
        r = dead_man_finalize("provisioned", False, run_type=run_type)
        assert r.run_status == "aborted_harness" and r.run_reason == "start_failed"
        assert r.unreached == Outcome("harness_error", "start_failed")
        assert r.alert == alerts.aborted_harness() and r.has("kill_tree")
    assert dead_man_finalize("launched", False, run_type="calibration").has("queue_calibration")
    assert not dead_man_finalize("launched", False, run_type="nightly").has("queue_calibration")
    # a Nightly that reached running and went silent is still dead_man
    assert dead_man_finalize("running", True, run_type="nightly").run_reason == "dead_man"


# -- 7.7 re-login and run budgets -------------------------------------------------------------------------
def test_relogin_budget_k():
    k = ReloginBudget()
    for _ in range(3):
        r = k.environment_relogin()
        assert r.passed and r.has("relaunch") and r.has("wait_session_release")
    over = k.environment_relogin()
    assert over.run_status == "complete_with_env_errors" and over.run_reason == "relogin_budget"
    assert over.unreached == Outcome("env_error", "relogin_budget", "authenticated")
    kicked = session_kicked()
    assert kicked.unreached.reason == "session_kicked" and "accounts_owner" in kicked.alert.notified


def test_run_budgets_wall_clock_rate_cap_superseded():
    assert run_budget(now_s=100, wall_budget_s=2700, report_margin_s=300) is None
    wc = run_budget(now_s=2400, wall_budget_s=2700, report_margin_s=300)
    assert wc.run_reason == "wall_clock" and wc.unreached == Outcome("not_run", "budget")
    rc = run_budget(now_s=1, wall_budget_s=2700, report_margin_s=300, counters={"orders_staged_per_hour": 60})
    assert rc.run_reason == "rate_cap" and "system_owner" in rc.alert.notified
    assert run_budget(now_s=1, wall_budget_s=2700, report_margin_s=300, superseded=True).run_reason == "superseded"


def test_rate_counters_count_rolling_hours_and_days_and_caps_can_be_replaced():
    now = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
    c = RateCounters()
    for minutes in (5, 59, 61, 23 * 60, 25 * 60):
        c.order_staged(now - timedelta(minutes=minutes))
    c.symbol_change(now)
    c.login(now - timedelta(hours=2))
    assert c.counts(now) == {"logins_per_day": 1, "symbol_changes_per_hour": 1, "orders_staged_per_hour": 2,
                             "orders_staged_per_day": 4}
    assert c.counts(now - timedelta(hours=3))["logins_per_day"] == 0        # an event after `now` is not counted
    tight = {**RATE_CAPS, "orders_staged_per_day": 4}
    assert run_budget(now_s=1, wall_budget_s=2700, report_margin_s=600, counters=c.counts(now)) is None
    hit = run_budget(now_s=1, wall_budget_s=2700, report_margin_s=600, counters=c.counts(now), caps=tight)
    assert hit.run_reason == "rate_cap" and "orders_staged_per_day 4 of 4" in hit.detail
    assert REPORT_MARGIN_S == 600 and WALL_BUDGET_S["merge"] == 3 * 3600
    assert wall_budget_s("nightly") == 4 * 3600
    assert wall_budget_s("calibration", started_at=now, ends_at=now + timedelta(hours=2)) == 7200
    with pytest.raises(ValueError):
        wall_budget_s("calibration", started_at=now, ends_at=now)


# -- 10.7 degraded modes ----------------------------------------------------------------------------------
def test_model_api_down_is_a_degraded_mode_never_env_error():
    r = model_preflight(True, False, blocking_control="corporate DLP")
    assert r.passed and r.run is None and r.alert.priority == "P2" and r.findings[0].kind == "hardening"
    av = ModelAvailability.from_preflight(False, True)
    assert av.review_mode == "deferred" and not av.agent_available
    assert verdict_outcome(av) == Outcome("needs_human", "reviewer_unavailable")
    lf = locator_failed(av, "s05")
    assert lf.step == "failed" and lf.checkpoint == Outcome("landmark_missing", "locator:s05")
    assert lf.outcomes[0].outcome.reason == "step_failed:s05"
    assert "agent fallback disabled" in lf.findings[0].description
    assert agent_only_step(av) == Outcome("not_run", "model_unavailable")
    assert av.recheck(True) and av.agent_available and av.recoveries == 1
    assert locator_failed(av, "s05") is None and verdict_outcome(av) is None and agent_only_step(av) is None


def test_spend_ceilings_per_run_aborts_at_the_flow_boundary_weekly_does_not():
    s = SpendTracker()
    assert s.add(30.0) is None and s.at_flow_boundary() is None
    hit = s.add(20.0)
    assert hit.has("stop_model_calls") and hit.run is None and not s.calls_allowed
    end = s.at_flow_boundary()
    assert end.run_reason == "dollar_cap" and end.unreached == Outcome("not_run", "budget")
    pend = {x.selector: x.outcome for x in end.outcomes}["pending_review"]
    assert (pend.reason, pend.detail) == ("reviewer_cap", "cause: dollar_cap")
    w = SpendTracker(week_usd=495.0)
    weekly = w.add(5.0)
    assert weekly.run is None and weekly.has("stop_model_calls") and w.at_flow_boundary() is None


def test_network_back_within_5_minutes_retries_the_step():
    p = NetworkPause(0.0)
    assert p.poll(120.0, connected=False) is None
    r = p.poll(240.0, connected=True)
    assert r.passed and r.has("retry_step") and r.has("reassert_paper_mode") and r.has("light_fixture_check")


def test_network_between_5_and_15_minutes_fails_the_flow_then_resumes():
    p = NetworkPause(0.0)
    r = p.poll(301.0, connected=False)
    assert r.checkpoint == Outcome("env_error", "gate", "connectivity") and r.run is None
    assert {s.selector for s in r.outcomes} == {"in_progress", "rest_of_flow"}
    assert p.poll(400.0, connected=False) is None
    back = p.poll(600.0, connected=True)
    assert back.has("resume_next_flow") and not back.has("retry_step")


def test_network_past_15_minutes_ends_the_shard():
    p = NetworkPause(0.0)
    r = p.poll(901.0, connected=False)
    assert r.run_status == "complete_with_env_errors" and r.unreached == Outcome("env_error", "gate", "connectivity")


def test_network_sparse_poll_decides_on_elapsed_time_before_the_connection_state():
    # 10.7: a reconnect first observed between 5 and 15 minutes still fails the flow, with the resume
    p = NetworkPause(0.0)
    r = p.poll(400.0, connected=True)
    env = Outcome("env_error", "gate", "connectivity")
    assert r.checkpoint == env and {s.selector: s.outcome for s in r.outcomes} == {"in_progress": env,
                                                                                   "rest_of_flow": env}
    assert r.has("resume_next_flow") and r.has("reassert_paper_mode") and not r.has("retry_step")
    assert r.run is None and r.alert == alerts.env_error()
    again = p.poll(410.0, connected=True)                    # the flow already failed: no second env_error
    assert again.checkpoint is None and again.has("resume_next_flow")
    # past 15 minutes the shard ends whenever the reconnect is observed
    late = NetworkPause(0.0).poll(1000.0, connected=True)
    assert late.run_status == "complete_with_env_errors" and late.unreached == env and not late.has("resume_next_flow")
    # after a dense poll failed the flow, a reconnect past 15 minutes ends the shard too
    dense = NetworkPause(0.0)
    assert dense.poll(301.0, connected=False).checkpoint == env
    assert dense.poll(1000.0, connected=True).run_status == "complete_with_env_errors"


def test_rig_loss_requeues_once_then_env_error_or_aborted_harness():
    assert not rig_silent(179) and rig_silent(180)
    assert rig_lost(0, budget_allows=True, same_class_rig=True).has("requeue_shard")
    none = rig_lost(0, budget_allows=False, same_class_rig=True)
    assert none.unreached == Outcome("env_error", "gate", "rig_lost") and none.run_status == "complete_with_env_errors"
    second = rig_lost(1, budget_allows=True, same_class_rig=True)
    assert second.run_reason == "rig_lost" and second.unreached == Outcome("harness_error", "rig_lost")
