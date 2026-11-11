"""The bounds source under DR-01 Branch A (5.9, 7.5, SD-54): the Java Access Bridge enablement check at gate
`launched`, a bridge lost mid-run (`harness_error (reason: bounds_source_unavailable)` until the relaunch), the
relaunch at the next flow boundary, and `aborted_harness` when the bridge is still down after it."""
from __future__ import annotations

import pytest

from sentinel.orchestrator.bounds import (BoundsError, BoundsState, after_relaunch, bridge_lost, checkpoint_outcome,
                                          jab_enablement, relaunch_due)

REASON = "bounds_source_unavailable"


def test_branch_b_has_no_bridge_checks():
    s = BoundsState(branch="B")
    assert jab_enablement(s, properties_present=False, query_answers=False) is None
    assert after_relaunch(s, properties_present=False, query_answers=False) is None
    assert checkpoint_outcome(s) is None and not relaunch_due(s)
    with pytest.raises(BoundsError):
        bridge_lost(s)
    with pytest.raises(BoundsError):
        BoundsState(branch="C")


def test_enabled_bridge_passes_the_launched_check():
    s = BoundsState()
    assert jab_enablement(s, properties_present=True, query_answers=True) is None
    assert s.status == "up" and checkpoint_outcome(s) is None


@pytest.mark.parametrize("present,answers,why", [(False, True, ".accessibility.properties"),
                                                 (True, False, "no JAB query answered")])
def test_jab_not_enabled_is_harness_error_for_bounds_invariants_only(present, answers, why):
    s = BoundsState()
    r = jab_enablement(s, properties_present=present, query_answers=answers)
    assert not r.passed and r.gate == "launched" and r.run is None and r.unreached is None
    [subset] = r.outcomes
    assert subset.selector == "bounds_invariants"
    assert (subset.outcome.outcome, subset.outcome.reason) == ("harness_error", REASON)
    assert why in r.detail and "next rung" in r.detail
    assert checkpoint_outcome(s, needs_bounds=True).reason == REASON
    assert checkpoint_outcome(s, needs_bounds=False) is None
    assert not relaunch_due(s)


def test_bridge_lost_at_checkpoint_3_until_the_relaunch_restores_it():
    s = BoundsState()
    outcomes = []
    for i in range(1, 9):
        cp = f"cp{i}"
        if i == 3:
            r = bridge_lost(s, checkpoint_id=cp)
            assert r.gate == "running" and r.run is None and r.alert is not None
            assert [x.selector for x in r.outcomes] == ["in_progress", "rest_of_flow"]
            assert all((x.outcome.outcome, x.outcome.reason) == ("harness_error", REASON) for x in r.outcomes)
        if i == 6:                                            # the next flow boundary
            assert relaunch_due(s)
            assert after_relaunch(s, properties_present=True, query_answers=True) is None
            assert not relaunch_due(s)
        o = checkpoint_outcome(s, needs_bounds=False)
        outcomes.append(o.reason if o else None)
    assert outcomes == [None, None, REASON, REASON, REASON, None, None, None]
    assert s.losses == 1 and s.status == "up"


def test_the_bridge_answering_by_itself_does_not_lift_the_outcome_before_the_relaunch():
    s = BoundsState()
    bridge_lost(s, checkpoint_id="cp1")
    again = bridge_lost(s, checkpoint_id="cp2")              # still down at the next checkpoint: counted once
    assert s.losses == 1 and s.lost_at == "cp1" and again.checkpoint.reason == REASON
    assert checkpoint_outcome(s).reason == REASON and relaunch_due(s)


def test_bridge_still_down_after_the_relaunch_aborts_the_run():
    s = BoundsState()
    bridge_lost(s, checkpoint_id="cp4")
    r = after_relaunch(s, properties_present=True, query_answers=False)
    assert (r.run.status, r.run.reason) == ("aborted_harness", REASON)
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", REASON)
    assert r.gate == "launched" and r.has("teardown") and r.evidence["lost_at"] == "cp4"


def test_a_second_loss_after_a_restore_gets_its_own_relaunch():
    s = BoundsState()
    bridge_lost(s)
    after_relaunch(s, properties_present=True, query_answers=True)
    bridge_lost(s, checkpoint_id="cp9")
    assert s.losses == 2 and relaunch_due(s)


def test_never_enabled_bridge_after_another_relaunch_is_the_launched_check_not_the_abort():
    s = BoundsState()
    jab_enablement(s, properties_present=False, query_answers=False)
    r = after_relaunch(s, properties_present=False, query_answers=False)      # relaunch for an app crash
    assert r.run is None and r.outcomes[0].selector == "bounds_invariants"
    assert after_relaunch(s, properties_present=True, query_answers=True) is None
    assert s.status == "up" and checkpoint_outcome(s) is None
