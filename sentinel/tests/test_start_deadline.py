"""Start deadlines and their outcomes per run type (11.2 Queue and saturation, 10.2, 7.2 row 3, 9.10, 11.6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator import start_deadline as sd
from sentinel.orchestrator.results import Outcome, write_unreached

NY = ZoneInfo("America/New_York")


def ny(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(timezone.utc)


TUE_1031 = ny(2026, 11, 10, 10, 31)


def test_deadlines_per_run_type():
    assert sd.deadline("pr", TUE_1031) == TUE_1031 + timedelta(minutes=45)
    assert sd.deadline("merge", TUE_1031) == TUE_1031 + timedelta(hours=3)
    signoff = ny(2026, 11, 17, 17, 0)
    assert sd.deadline("rc", TUE_1031, signoff_at=signoff) == ny(2026, 11, 17, 9, 0)
    assert sd.deadline("rc", TUE_1031) is None                                  # choice 4: no sign-off time
    assert sd.deadline("nightly", ny(2026, 11, 10, 22, 0)) == ny(2026, 11, 11, 2, 0)
    friday = ny(2026, 11, 13, 22, 15)
    assert sd.deadline("calibration", friday, calibration="weekend") == ny(2026, 11, 14, 2, 0)
    assert sd.deadline("calibration", ny(2026, 11, 10, 22, 15), calibration="emergency") == ny(2026, 11, 11, 1, 0)
    with pytest.raises(ValueError):
        sd.deadline("calibration", friday)
    with pytest.raises(ValueError):
        sd.deadline("soak", friday)
    with pytest.raises(ValueError):
        sd.deadline("pr", datetime(2026, 11, 10, 10, 31))                     # naive


def test_deadlines_follow_new_york_wall_time_across_dst():
    # 2026-11-01 is the fall-back Sunday: the Nightly of Saturday 22:00 EDT has its deadline at 02:00 EST
    assert sd.deadline("nightly", ny(2026, 10, 31, 22, 0)) == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    # 2026-03-08 springs forward: 02:00 does not exist and resolves to the instant 02:00 EST (choice 1)
    assert sd.deadline("nightly", ny(2026, 3, 7, 22, 0)) == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    # in summer the 02:00 deadline is 06:00 UTC
    assert sd.deadline("nightly", ny(2026, 7, 7, 22, 0)) == datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)


def test_pr_run_still_queued_at_45_minutes_is_aborted_budget_pool_saturated():
    assert sd.outcome("pr", TUE_1031, TUE_1031 + timedelta(minutes=44, seconds=59), run_id="pr-1") is None
    d = sd.outcome("pr", TUE_1031, TUE_1031 + timedelta(minutes=45), run_id="pr-1", runs_ahead=3, queue_depth=5)
    assert d.disposition == "abort" and d.writer == "reporter"
    r = d.result
    assert r.run_status == "aborted_budget" and r.run_reason == "pool_saturated"
    assert r.unreached == Outcome("not_run", "budget") and r.gate == "provisioned"
    assert d.comment == "Sentinel did not run: pool saturated, 3 runs ahead; push again or re-run the build job"
    assert d.check == ("error", "Sentinel did not run")
    assert d.abort == {"runs_ahead": 3, "queue_depth": 5}
    assert d.alert == sd.pool_saturated_alert() and d.alert.priority == "P2"
    assert d.alert.notified == (alerts.ON_CALL_CHANNEL,)
    assert sd.pool_saturated_text(1).endswith("1 run ahead; push again or re-run the build job")


def test_merge_run_past_its_deadline_coalesces_and_rc_goes_to_the_release_manager():
    d = sd.outcome("merge", TUE_1031, TUE_1031 + timedelta(hours=3), run_id="merge-1")
    assert d.disposition == "coalesce" and d.result is None and d.alert is None
    assert sd.outcome("merge", TUE_1031, TUE_1031 + timedelta(hours=2, minutes=59), run_id="merge-1") is None
    signoff = ny(2026, 11, 17, 17, 0)
    assert sd.outcome("rc", TUE_1031, ny(2026, 11, 17, 8, 59), run_id="rc-1", signoff_at=signoff) is None
    rc = sd.outcome("rc", TUE_1031, ny(2026, 11, 17, 9, 0), run_id="rc-1", signoff_at=signoff)
    assert rc.disposition == "release_manager" and rc.result is None
    assert rc.alert.priority == "P2" and {"release_manager", "system_owner"} <= set(rc.alert.notified)
    assert sd.outcome("rc", TUE_1031, ny(2027, 1, 1, 0, 0), run_id="rc-1") is None                 # no sign-off


def test_nightly_not_at_running_by_0200_is_aborted_harness_start_failed():
    trig = ny(2026, 11, 10, 22, 0)
    assert sd.outcome("nightly", trig, ny(2026, 11, 11, 1, 59), run_id="n-1") is None
    d = sd.outcome("nightly", trig, ny(2026, 11, 11, 2, 0), run_id="n-1")
    r = d.result
    assert r.run_status == "aborted_harness" and r.run_reason == "start_failed"
    assert r.unreached == Outcome("harness_error", "start_failed")
    assert r.alert == alerts.aborted_harness() and r.alert.priority == "P1"
    assert d.state == (("canaries_pending", "nightly run n-1 did not start"),) and not d.requeue
    assert d.writer == "reporter" and not r.has("teardown")
    # on a rig, stuck in its gate sequence: the Orchestrator writes it and tears down
    on = sd.outcome("nightly", trig, ny(2026, 11, 11, 2, 0), run_id="n-1", state="workspace_loaded", on_rig=True)
    assert on.writer == "orchestrator" and on.result.has("teardown") and on.result.gate == "workspace_loaded"


def test_calibration_start_failed_is_requeued():
    fri = ny(2026, 11, 13, 22, 15)
    d = sd.outcome("calibration", fri, ny(2026, 11, 14, 2, 0), run_id="cal-1", calibration="weekend")
    assert d.result.run_reason == "start_failed" and d.requeue and d.result.has("queue_calibration")
    assert d.state == (("canaries_pending", "calibration run cal-1 did not start"),)
    em = sd.outcome("calibration", ny(2026, 11, 10, 22, 15), ny(2026, 11, 11, 1, 0), run_id="cal-2",
                    calibration="emergency")
    assert em.requeue and em.result.run_reason == "start_failed"
    with pytest.raises(ValueError):
        sd.outcome("calibration", fri, ny(2026, 11, 14, 2, 0), run_id="cal-1", deadline_at=fri)
    with pytest.raises(ValueError):
        sd.start_failed("pr")


def test_start_failed_fills_every_scoped_checkpoint_with_harness_error():
    class Sink:
        def __init__(self):
            self.calls = []

        def fill_unreached(self, run_id, outcome, reason=None, *, gate=None, detail=None):
            self.calls.append((run_id, outcome, reason, gate))
            return ["cp-1", "cp-2"]

    sink = Sink()
    d = sd.outcome("nightly", ny(2026, 11, 10, 22, 0), ny(2026, 11, 11, 2, 30), run_id="n-1")
    assert write_unreached(sink, "n-1", d.result) == ["cp-1", "cp-2"]
    assert sink.calls == [("n-1", "harness_error", "start_failed", None)]


def test_banner_texts():
    assert sd.canaries_pending_banner("") is None and sd.canaries_pending_banner(None) is None
    assert sd.canaries_pending_banner(sd.skipped_reason("rc-7")) == \
        "canaries pending: last Nightly skipped for RC run rc-7"
    assert sd.canaries_pending_banner(sd.not_started_reason("nightly", "n-3")) == \
        "canaries pending: nightly run n-3 did not start"


def test_run_not_started_within_15_minutes_raises_the_dead_man_alert():
    t = TUE_1031
    assert sd.run_not_started("merge", t, None, t + timedelta(minutes=14)) is None
    alert = sd.run_not_started("merge", t, None, t + timedelta(minutes=15))
    assert alert == alerts.dead_man("merge") and alert.priority == "P1"
    assert sd.run_not_started("pr", t, None, t + timedelta(minutes=16)).priority == "P2"
    # a run_queued received holds it: the dead-man accepts it in place of run_started
    assert sd.run_not_started("merge", t, t + timedelta(minutes=1), t + timedelta(minutes=30)) is None
    # a signal from before this trigger does not count
    assert sd.run_not_started("merge", t, t - timedelta(minutes=1), t + timedelta(minutes=30)) is not None


def test_decision_validates_its_fields():
    with pytest.raises(ValueError):
        sd.Decision("r", "pr", TUE_1031, "retry")
    with pytest.raises(ValueError):
        sd.Decision("r", "pr", TUE_1031, "abort", writer="watchdog")
