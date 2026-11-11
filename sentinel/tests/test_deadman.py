"""The central dead-man intake and the monthly chaos self-test (10.2 Dead-man, two layers; 9.10; 10.7; 1.4 G5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator.deadman import (CHAOS_RIG, ChaosObservation, DeadmanIntake, RigHeartbeat, RunCompleted,
                                           RunQueued, RunStarted, RunTriggered, ScopeUploaded, chaos_due, chaos_stall,
                                           evaluate_chaos, observe_chaos, record_chaos)
from sentinel.service.deadman import LOST_DETAIL, Fired, fire
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

T0 = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
WS = "ws.tier-a.active-trader"
CIDS = tuple(f"census/trade.{s}/{WS}" for s in ("ladder", "chain", "monitor"))


def central(tmp_path, run_id: str, run_type: str, rig: str = "rig-a1") -> Store:
    s = Store(tmp_path / f"{run_id}.db")
    s.register_rig(rig, login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=3 * 3600, started_at=T0.isoformat(),
                        scope=tuple(ScopeEntry(c, WS, "active_trader", 1, "frozen") for c in CIDS),
                        shards=(ShardPlan(f"{run_id}/1", rig),)))
    return s


def test_a_run_that_reached_running_and_went_silent_is_dead_man_with_a_p1_naming_run_rig_and_heartbeat(tmp_path):
    store = central(tmp_path, "merge-1", "merge")
    intake = DeadmanIntake()
    deadline = T0 + timedelta(hours=3)
    intake.intake(RunTriggered("merge-1", "merge", T0))
    w = intake.intake(RunStarted("merge-1", "merge", T0, deadline, {"merge-1/1": "rig-a1"}))
    assert w.deadline == deadline + timedelta(minutes=30)                         # merge: plus 30
    intake.intake(ScopeUploaded("merge-1", T0 + timedelta(minutes=5)))
    beat = T0 + timedelta(hours=3, minutes=29)
    intake.intake(RigHeartbeat("rig-a1", beat))
    assert intake.due(w.deadline - timedelta(seconds=1)) == []
    due = intake.due(w.deadline)
    assert [d.run_id for d in due] == ["merge-1"]
    fired = fire(store, intake.timer, due[0], w.deadline)
    out = store.run_outcome("merge-1")
    assert (out.status, out.reason) == ("aborted_harness", "dead_man")
    assert {(o.outcome, o.reason) for o in store.outcomes("merge-1")} == {("harness_error", "dead_man")}
    alert = intake.dead_man_alert(fired, w.deadline)
    assert alert.priority == "P1" and alert.rule == "dead_man" and alert.run_id == "merge-1"
    assert "rig-a1" in alert.detail and beat.isoformat(timespec="seconds") in alert.detail
    assert alert.status == "aborted_harness (reason: dead_man)" and "harness_on_call" in alert.route.paged
    assert LOST_DETAIL not in alert.detail                                        # the rig beat a minute ago
    store.close()


def test_pr_runs_page_p2_and_a_lost_rig_is_named_unreachable_with_data_intact(tmp_path):
    store = central(tmp_path, "pr-7", "pr")
    intake = DeadmanIntake()
    intake.intake(RunStarted("pr-7", "pr", T0, T0 + timedelta(minutes=35), {"pr-7/1": "rig-a1"}))
    intake.intake(ScopeUploaded("pr-7", T0))
    intake.intake(RigHeartbeat("rig-a1", T0 + timedelta(minutes=10)))
    now = T0 + timedelta(minutes=50)
    fired = fire(store, intake.timer, intake.due(now)[0], now)
    alert = intake.dead_man_alert(fired, now)
    assert alert.priority == "P2" and LOST_DETAIL in alert.detail and "pr-7/1" in alert.detail
    store.close()


def test_never_dead_man_before_running_and_nothing_after_run_completed():
    intake = DeadmanIntake()
    intake.intake(RunStarted("pr-1", "pr", T0, T0 + timedelta(minutes=35), {"pr-1/1": "rig-a1"}))
    intake.intake(RunStarted("pr-2", "pr", T0, T0 + timedelta(minutes=35), {"pr-2/1": "rig-a2"}))
    intake.intake(ScopeUploaded("pr-2", T0))
    intake.intake(RunCompleted("pr-2", T0 + timedelta(minutes=30)))
    late = T0 + timedelta(hours=1)
    assert [w.run_id for w in intake.timer.due(late)] == ["pr-1"]                # the timer alone would close it
    assert intake.due(late) == []                                                # the central layer does not (10.2)
    assert intake.dead_man_alert(Fired("pr-2", None, None), late) is None       # fire wrote nothing
    with pytest.raises(TypeError):
        intake.intake("run_started")


def test_a_trigger_without_run_started_or_run_queued_in_15_minutes_is_run_not_started_once():
    intake = DeadmanIntake()
    intake.intake(RunTriggered("nightly-1", "nightly", T0))
    intake.intake(RunTriggered("pr-3", "pr", T0))
    intake.intake(RunTriggered("pr-4", "pr", T0))
    intake.intake(RunQueued("pr-3", T0 + timedelta(minutes=5), position=2))
    intake.timer.run_started("pr-4", "pr", T0 + timedelta(minutes=35))           # the service endpoint fed the timer
    assert intake.not_started(T0 + timedelta(minutes=14, seconds=59)) == []
    alerts = intake.not_started(T0 + timedelta(minutes=15))
    assert [(a.run_id, a.priority, a.rule) for a in alerts] == [("nightly-1", "P1", "dead_man")]
    assert alerts[0].status == "run not started" and "neither run_started nor run_queued" in alerts[0].detail
    assert intake.not_started(T0 + timedelta(minutes=30)) == []                  # once per run
    pr = DeadmanIntake()
    pr.intake(RunTriggered("pr-5", "pr", T0))
    assert pr.not_started(T0 + timedelta(minutes=16))[0].priority == "P2"


def test_chaos_stall_observed_within_15_minutes_passes_and_is_recorded_in_run_health(tmp_path):
    store = central(tmp_path, "nightly-chaos", "nightly", rig=CHAOS_RIG)
    stall = chaos_stall("nightly-chaos", T0, checkpoint_id=CIDS[0])
    assert stall.deadline == T0 + timedelta(minutes=30) and stall.shard_id == "nightly-chaos/1"
    intake = DeadmanIntake()
    w = intake.intake(stall.started())
    assert w.deadline == stall.deadline                                          # the shortened deadline, not 06:00
    intake.intake(ScopeUploaded("nightly-chaos", T0 + timedelta(minutes=1)))
    now = stall.deadline + timedelta(minutes=1)
    fired = fire(store, intake.timer, intake.due(now)[0], now)
    assert intake.dead_man_alert(fired, now).priority == "P1"
    seen = observe_chaos(store, "nightly-chaos")
    assert seen.row_at == seen.alert_at == now
    verdict = evaluate_chaos(stall, seen, now)
    assert verdict.observed and verdict.result is None
    assert not record_chaos(store, stall, verdict)
    assert store.run_summary("nightly-chaos")["health"]["chaos_self_test"]["observed"] is True
    store.close()


def test_a_stall_the_central_layer_never_noticed_is_chaos_unobserved_and_a_p1(tmp_path):
    store = central(tmp_path, "nightly-chaos", "nightly", rig=CHAOS_RIG)
    stall = chaos_stall("nightly-chaos", T0, checkpoint_id=CIDS[0])
    nothing = observe_chaos(store, "nightly-chaos")
    assert nothing == ChaosObservation(None, None)
    assert evaluate_chaos(stall, nothing, stall.deadline + timedelta(minutes=14)) is None   # still in time
    verdict = evaluate_chaos(stall, nothing, stall.deadline + timedelta(minutes=15, seconds=1))
    assert not verdict.observed
    o = verdict.result.checkpoint
    assert (o.outcome, o.reason) == ("harness_error", "chaos_unobserved")
    assert verdict.result.alert.priority == "P1" and "harness_on_call" in verdict.result.alert.paged
    assert "no dead-man alert and no dead_man row" in verdict.detail
    assert record_chaos(store, stall, verdict)
    row = store.get_outcome("nightly-chaos", CIDS[0])
    assert (row.outcome, row.reason) == ("harness_error", "chaos_unobserved")
    assert store.run_summary("nightly-chaos")["health"]["chaos_self_test"]["observed"] is False
    assert not record_chaos(store, stall, verdict)                                # the row is written once
    store.close()


def test_a_late_alert_alone_is_still_unobserved():
    stall = chaos_stall("nightly-chaos", T0)
    limit = stall.deadline + timedelta(minutes=15)
    late = evaluate_chaos(stall, ChaosObservation(limit + timedelta(seconds=1), stall.deadline),
                          limit + timedelta(hours=1))
    assert not late.observed and "no dead-man alert" in late.detail and "dead_man row" not in late.detail
    on_time = evaluate_chaos(stall, ChaosObservation(limit, limit), limit)
    assert on_time.observed


def test_the_self_test_is_due_once_per_new_york_month():
    assert chaos_due(None, T0)
    assert not chaos_due(datetime(2026, 11, 2, 15, tzinfo=timezone.utc), T0)
    assert chaos_due(datetime(2026, 10, 30, 15, tzinfo=timezone.utc), T0)
    # 23:30 on 30 November in New York is already 1 December in UTC: still November's stall
    nov_30_ny = datetime(2026, 12, 1, 4, 30, tzinfo=timezone.utc)
    assert not chaos_due(datetime(2026, 11, 3, tzinfo=timezone.utc), nov_30_ny)
    assert chaos_due(datetime(2026, 11, 3, tzinfo=timezone.utc), datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc))
