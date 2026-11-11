"""masked_live lockstep: the pair's fingerprint assertion and the 60 s slip rule (3.1, 7.4, 11.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator.lockstep import (LOCKSTEP_WINDOW_S, NOT_RUN, PAIRED, REQUEUE, LockstepTracker,
                                            assert_pair, lockstep_health_row, slip_seconds)
from sentinel.records import CheckpointOutcome, Fingerprint

CENSUS = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
OTHER = "census/monitor.positions/ws.tier-a.active-trader"
T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def match(build_id: str, **over) -> dict:
    m = {"build_id": build_id, "windows_build": "10.0.19045.5011", "display": {"w": 1920, "h": 1080, "dpi": 96},
         "cleartype": False, "data_epoch": {"epoch_id": "sha256:" + "e" * 64, "data_mode": "none"},
         "render_probe_hash": "0123456789abcdef"}
    m.update(over)
    return m


def fp(side: str, rig: str, build_id: str, **over) -> Fingerprint:
    return Fingerprint(run_id="run-01", side=side, rig_id=rig, read_at="2026-11-09T15:00:00+00:00",
                       match=match(build_id, **over), shard_id="run-01/3")


# -- the pair assertion -------------------------------------------------------------------------------------------

def test_a_pair_that_agrees_on_every_field_but_build_id_passes():
    assert assert_pair(fp("base", "rig-01", "main-1998.2.0-4f1c9a2"),
                       fp("candidate", "rig-02", "branch-1998.3.0-5d3e812")) is None
    # the 64-bit probe hash compares at Hamming distance zero, hex case ignored (3.3)
    assert assert_pair(match("a"), match("b", render_probe_hash="0123456789ABCDEF")) is None


def test_a_pair_mismatch_makes_the_shards_masked_live_checkpoints_not_comparable_and_leaves_frozen_ones():
    r = assert_pair(fp("base", "rig-01", "main-a"), fp("candidate", "rig-02", "branch-b", cleartype=True,
                                                       display={"w": 1920, "h": 1080, "dpi": 120}))
    assert r is not None and not r.passed
    assert (r.run.status, r.run.reason) == ("complete_with_env_errors", None)
    assert r.unreached is None                                   # frozen checkpoints of the shard are untouched
    [sub] = r.outcomes
    assert sub.selector == "masked_live"
    o = sub.outcome
    assert (o.outcome, o.reason, o.gate, o.detail) == ("not_comparable", "fingerprint_mismatch", None,
                                                       "field: cleartype, display")
    assert r.alert == alerts.env_error() and r.evidence["fields"] == ["cleartype", "display"]
    # the outcome is a valid 4.13 record for a masked_live checkpoint of the shard
    CheckpointOutcome(run_id="run-01", checkpoint_id=CENSUS, outcome=o.outcome, reason=o.reason, detail=o.detail,
                      workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="masked_live")


def test_a_data_epoch_difference_is_a_pair_mismatch():
    r = assert_pair(match("a"), match("b", data_epoch={"epoch_id": "sha256:" + "f" * 64, "data_mode": "none"}))
    assert r is not None and r.outcomes[0].outcome.detail == "field: data_epoch"


# -- the 60 s slip rule --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gap,action", [(0, PAIRED), (59, PAIRED), (60, PAIRED), (61, REQUEUE), (70, REQUEUE)])
def test_the_window_is_sixty_seconds_either_way(gap, action):
    t = LockstepTracker(("rig-01", "rig-02"))
    d = t.record(CENSUS, T0 + timedelta(seconds=gap), T0)
    assert (d.action, d.slip_s, d.outcome) == (action, float(gap), None)
    assert t.lockstep_slip == 0
    assert LOCKSTEP_WINDOW_S == 60.0


def test_a_first_slip_requeues_the_pair_once_at_the_end_of_the_shard():
    t = LockstepTracker(("rig-01", "rig-02"))
    assert t.record(CENSUS, T0, T0 + timedelta(seconds=70)).action == REQUEUE
    assert t.record(OTHER, T0, T0 + timedelta(seconds=10)).action == PAIRED
    assert t.drain() == [CENSUS] and t.drain() == []           # handed back once, in slip order
    assert t.requeued == (CENSUS,) and t.lockstep_slip == 0
    assert t.record(CENSUS, T0 + timedelta(minutes=9), T0 + timedelta(minutes=9, seconds=5)).action == PAIRED
    assert t.lockstep_slip == 0 and t.not_run == ()


def test_a_second_slip_is_not_run_budget_and_counts_lockstep_slip():
    t = LockstepTracker(("rig-01", "rig-02"))
    t.record(CENSUS, T0, T0 + timedelta(seconds=70))
    assert t.drain() == [CENSUS]
    d = t.record(CENSUS, T0 + timedelta(minutes=9), T0 + timedelta(minutes=10, seconds=15))
    assert d.action == NOT_RUN and d.slip_s == 75.0
    assert (d.outcome.outcome, d.outcome.reason, d.outcome.gate) == ("not_run", "budget", None)
    assert t.lockstep_slip == 1 and t.not_run == (CENSUS,) and t.drain() == []
    with pytest.raises(ValueError, match="already ended not_run"):
        t.record(CENSUS, T0, T0)
    # a counter, never a finding: the record is a plain not_run (budget) outcome of the checkpoint
    CheckpointOutcome(run_id="run-01", checkpoint_id=CENSUS, outcome="not_run", reason="budget",
                      detail=d.outcome.detail, workspace_id="ws.tier-a.active-trader", persona="active_trader",
                      data_mode="masked_live")


def test_a_second_slip_before_the_drain_leaves_the_requeue_empty():
    t = LockstepTracker()
    t.record(CENSUS, 0.0, 61.0)
    assert t.record(CENSUS, 100.0, 200.0).action == NOT_RUN
    assert t.drain() == [] and t.lockstep_slip == 1


def test_capture_times_are_aware_wall_times_or_seconds_on_one_base():
    assert slip_seconds("2026-11-09T10:00:00-05:00", "2026-11-09T15:01:01+00:00") == 61.0
    assert slip_seconds(T0, "2026-11-09T15:00:30+00:00") == 30.0
    assert slip_seconds(5, 65.5) == 60.5
    with pytest.raises(ValueError, match="UTC offset"):
        slip_seconds(datetime(2026, 11, 9, 15, 0), T0)
    with pytest.raises(ValueError, match="both wall times"):
        slip_seconds(T0, 12.0)


def test_the_run_health_row_sums_the_shards_and_is_absent_without_a_slip():
    a, b = LockstepTracker(("rig-01", "rig-02")), LockstepTracker(("rig-03", "rig-04"))
    a.record(CENSUS, 0, 30)
    assert lockstep_health_row([a, b]) is None
    a.record(CENSUS, 0, 90)
    a.record(CENSUS, 500, 640)
    b.record(OTHER, 0, 100)
    label, text = lockstep_health_row([a, b])
    assert label == "Lockstep"
    assert text == (f"lockstep_slip 1 ({CENSUS}: not_run, budget); 2 pairs re-queued once at the end of the shard "
                    "after a slip over 60 s")
