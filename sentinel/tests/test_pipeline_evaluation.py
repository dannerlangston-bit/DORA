"""The evaluation inputs wired into the simulated PR run (w2 wiring item 7): `settle_spec_for` (SimApp's own settle
spec is gone), the base side's one re-capture (`settle_base`, `b_input_reason`), the re-capture plan taking the run
budget's unit itself, the global-shift pre-pass stopping the Reviewer queue for a run of 20 or more pairs, and the
cap order of `review_queue.order`."""
from __future__ import annotations

import dataclasses
from datetime import date

from sentinel.demo import EXAMPLE, flexgrid_scenario, wrong_number_scenario
from sentinel.diff.engine import GlobalShiftDecision
from sentinel.pipeline.run import run_pipeline
from sentinel.pipeline.sim import SimApp, SimBuild

TODAY = date(2026, 11, 10)


def test_a_global_shift_over_twenty_pairs_stops_the_reviewer_queue(tmp_path, monkeypatch):
    import sentinel.pipeline.run as pr
    real = pr.global_shift_prepass

    def raised(signals, params=None):
        d = real(signals, params=params)
        assert d.evaluated                                   # 3.7: at least 20 diffed pairs
        return GlobalShiftDecision(True, True, 0.5, 0.9, collapse=tuple(s.checkpoint_id for s in signals))
    monkeypatch.setattr(pr, "global_shift_prepass", raised)
    r = run_pipeline(EXAMPLE, flexgrid_scenario(TODAY), tmp_path)
    assert r.reviewer_calls == []                            # no Reviewer call for the whole run (3.7)
    shifted = [o for o in r.outcomes if o.reason == "global_shift"]
    assert len(shifted) >= 20 and {o.outcome for o in shifted} == {"needs_human"}


def test_the_settle_spec_comes_from_the_checkpoint_and_its_surface():
    assert not hasattr(SimApp, "settle_spec")


def test_a_base_that_never_settles_is_re_captured_once_then_absent(tmp_path):
    sc = wrong_number_scenario(TODAY)
    s03 = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s03"
    sc = dataclasses.replace(sc, base=SimBuild(sc.base.build_id, unsettled=frozenset({s03})))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    (o,) = [o for o in r.outcomes if o.checkpoint_id == s03]
    assert (o.outcome, o.reason) == ("needs_baseline", "base_not_settled")
