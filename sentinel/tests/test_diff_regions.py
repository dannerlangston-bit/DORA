"""Region Registry semantics (3.8, 4.8): validation, expiry, the mask budget, money cells (F33, F34, F41)."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sentinel.diff.regions import (
    UNCALIBRATED, FloorException, Landmark, MaskRecord, Rect, RedactionBox, Region, RegionError, RegionsFile, RegionThreshold,
    effective_registry, locate, mask_budget, money_cell_threshold, sensitivity,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
CID = "flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2"
BUDGETS = {"order_confirmation": 0.10, "order_ticket": 0.10, "position_statement": 0.10, "account_info": 0.05}
TPL = np.arange(12 * 40 * 3, dtype=np.uint8).reshape(12, 40, 3)


def _lm(i="lm.title"):
    return Landmark(i, TPL)


def _exc(days=30):
    return FloorException("jdoe", "quote flash", "TOS-1", NOW + timedelta(days=days))


def _mask_rec(mask_id="msk.1", region_id="rgn.ts", days=30):
    return MaskRecord(mask_id, CID, region_id, "tos-trade-ui", "staging time", "human", "run-1",
                      NOW - timedelta(days=1), NOW + timedelta(days=days), "dec-1")


def test_regions_file_from_the_4_8_yaml_shape():
    data = {"schema_version": 1, "checkpoint_id": CID,
            "landmarks": [{"id": "lm.order-confirmation-title", "template": "t.png", "confidence_min": 0.93}],
            "regions": [
                {"id": "rgn.confirm.qty", "type": "diff", "rect": {"dx": 90, "dy": 48, "w": 48, "h": 20},
                 "landmark": "lm.order-confirmation-title", "panel": "order_confirmation", "money_cell": True},
                {"id": "rgn.confirm.timestamp", "type": "mask", "rect": {"dx": 260, "dy": 200, "w": 110, "h": 16},
                 "landmark": "lm.order-confirmation-title", "panel": "order_confirmation",
                 "mask_ref": "msk.confirm.timestamp-01"}]}
    rf = RegionsFile.from_dict(data, templates={"lm.order-confirmation-title": TPL})
    assert rf.landmark("lm.order-confirmation-title").confidence_min == 0.93
    assert rf.region("rgn.confirm.qty").money_cell and rf.region("rgn.confirm.qty").rect == Rect(90, 48, 48, 20)
    assert locate(rf, {"lm.order-confirmation-title": (100, 50)})["rgn.confirm.qty"] == Rect(190, 98, 48, 20)


@pytest.mark.parametrize("region, message", [
    (Region("rgn.m", "rule", "lm.title", Rect(0, 0, 5, 5), money_cell=True, rule={"field": "money"}), "money cell"),
    (Region("rgn.m", "mask", "lm.title", Rect(0, 0, 5, 5), money_cell=True, mask_ref="msk.1"), "money cell"),
    (Region("rgn.m", "diff", "lm.title", Rect(0, 0, 5, 5), money_cell=True, floor_exception=_exc()), "zero floor"),
    (Region("rgn.r", "rule", "lm.title", Rect(0, 0, 5, 5)), "rule present"),
    (Region("rgn.k", "mask", "lm.title", Rect(0, 0, 5, 5)), "mask_ref"),
    (Region("rgn.f", "freeze", "lm.title", Rect(0, 0, 5, 5)), "fixture_dependency"),
    (Region("rgn.f", "freeze", "lm.title", Rect(0, 0, 5, 5), fixture_dependency="x", floor_exception=_exc()),
     "diff regions only"),
    (Region("rgn.x", "diff", "lm.other", Rect(0, 0, 5, 5)), "not declared"),
    (Region("_rest", "diff", "lm.title", Rect(0, 0, 5, 5)), "reserved"),
    (Region("rgn.p", "diff", "lm.title", Rect(0, 0, 5, 5), panel="ladder"), "panel"),
])
def test_validation_refuses_illegal_regions(region, message):
    with pytest.raises(RegionError, match=message):
        RegionsFile(CID, [_lm()], [region]).validate()


def test_kinds_do_not_overlap_except_masks():
    d = Region("rgn.d", "diff", "lm.title", Rect(0, 0, 50, 20))
    r = Region("rgn.r", "rule", "lm.title", Rect(40, 0, 50, 20), rule={"field": "price"})
    with pytest.raises(RegionError, match="overlaps"):
        RegionsFile(CID, [_lm()], [d, r]).validate()
    m = Region("rgn.ts", "mask", "lm.title", Rect(10, 5, 10, 5), mask_ref="msk.1")
    RegionsFile(CID, [_lm()], [d, m]).validate()


def test_expired_mask_is_dropped_and_expired_floor_exception_stripped():
    d = Region("rgn.d", "diff", "lm.title", Rect(0, 0, 50, 20), floor_exception=_exc(days=-1))
    m = Region("rgn.ts", "mask", "lm.title", Rect(10, 5, 10, 5), mask_ref="msk.1")
    rf = RegionsFile(CID, [_lm()], [d, m])
    live, notices = effective_registry(rf, {"msk.1": _mask_rec()}, NOW)
    assert [r.id for r in live.regions] == ["rgn.d", "rgn.ts"] and live.region("rgn.d").floor_exception is None
    assert [(n.kind, n.region_id) for n in notices] == [("floor_exception_expired", "rgn.d")]
    live, notices = effective_registry(rf, {"msk.1": _mask_rec(days=-2)}, NOW)
    assert [r.id for r in live.regions] == ["rgn.d"]
    assert ("mask_expired", "msk.1") in [(n.kind, n.record_id) for n in notices]
    with pytest.raises(RegionError, match="not found"):
        effective_registry(rf, {}, NOW)


def test_mask_record_must_be_human_created():
    data = {"mask_id": "msk.1", "checkpoint_id": CID, "region_id": "rgn.ts", "owner": "o", "reason": "r",
            "created_by": "reviewer", "created_run": "run-1", "created_at": "2026-11-03T15:20:00-05:00",
            "expires_at": "2026-12-03T15:20:00-05:00", "decision_ref": "dec-1"}
    with pytest.raises(RegionError, match="human"):
        MaskRecord.from_dict(data)
    assert MaskRecord.from_dict(dict(data, created_by="human")).expired(datetime(2026, 12, 4, tzinfo=timezone.utc))


def _ticket_registry(mask_rect=Rect(0, 0, 10, 10)):
    body = Region("rgn.ticket.body", "diff", "lm.title", Rect(0, 0, 200, 100), panel="order_ticket")
    qty = Region("rgn.ticket.qty", "diff", "lm.other", Rect(0, 0, 40, 20), panel="order_ticket", money_cell=True)
    mask = Region("rgn.ticket.clock", "mask", "lm.title", mask_rect, panel="order_ticket", mask_ref="msk.1")
    return RegionsFile(CID, [_lm(), _lm("lm.other")], [body, qty, mask])


def _located(rf, title=(0, 0), other=(100, 50)):
    loc = locate(rf, {"lm.title": title, "lm.other": other})
    return {"base": loc, "candidate": loc}


def test_mask_budget_share_per_panel_counts_masks_and_non_identity_redaction():
    rf = _ticket_registry(Rect(0, 0, 40, 40))            # 1600 of 20000 = 8 percent
    ok = mask_budget(rf, _located(rf), [], BUDGETS)
    assert not ok.exceeded and ok.panels[0].share == pytest.approx(0.08)
    red = [RedactionBox(Rect(50, 0, 30, 20))]           # +600 -> 11 percent
    over = mask_budget(rf, _located(rf), red, BUDGETS)
    assert over.exceeded and "order_ticket" in over.detail()
    identity = [RedactionBox(Rect(50, 0, 30, 20), identity_field=True)]   # excluded (F33)
    assert not mask_budget(rf, _located(rf), identity, BUDGETS).exceeded


def test_mask_or_redaction_on_a_money_cell_breaches_its_zero_budget():
    rf = _ticket_registry(Rect(0, 0, 10, 10))
    clean = mask_budget(rf, _located(rf), [], BUDGETS)
    assert not clean.exceeded
    red = mask_budget(rf, _located(rf), [RedactionBox(Rect(110, 55, 4, 4), anchor_id="red.acct")], BUDGETS)
    assert red.exceeded and red.money_cell_hits[0].region_id == "rgn.ticket.qty"
    # a landmark move at run time lands the mask on the money cell on the candidate side only
    moved = locate(rf, {"lm.title": (0, 0), "lm.other": (5, 5)})
    res = mask_budget(rf, {"base": _located(rf)["base"], "candidate": moved}, [], BUDGETS)
    assert res.exceeded and {h.side for h in res.money_cell_hits} == {"candidate"}
    assert "rgn.ticket.qty covered by rgn.ticket.clock (candidate)" in res.detail()


def test_explicit_panel_bounds_and_panels_without_budget():
    rf = _ticket_registry(Rect(0, 0, 40, 40))
    bounds = {"base": {"order_ticket": Rect(0, 0, 100, 100)}, "candidate": {"order_ticket": Rect(0, 0, 100, 100)}}
    assert mask_budget(rf, _located(rf), [], BUDGETS, panel_bounds=bounds).exceeded     # 16 percent
    other = RegionsFile(CID, [_lm()], [Region("rgn.o", "diff", "lm.title", Rect(0, 0, 10, 10)),
                                        Region("rgn.k", "mask", "lm.title", Rect(0, 0, 10, 10), mask_ref="m")])
    assert not mask_budget(other, _located(other), [], BUDGETS).exceeded              # `other` has no budget


def test_sensitivity_and_thresholds():
    rule = Region("rgn.q", "rule", "lm.title", Rect(0, 0, 10, 10), rule={"field": "price"})
    mask = Region("rgn.k", "mask", "lm.title", Rect(20, 0, 10, 10), mask_ref="m")
    rf = RegionsFile(CID, [_lm()], [rule, mask])
    s = sensitivity(rf, locate(rf, {"lm.title": (0, 0)}), [RedactionBox(Rect(40, 0, 10, 10))], 100, 10)
    assert s == {"diff_freeze": pytest.approx(0.7), "rule": pytest.approx(0.1), "mask_redaction": pytest.approx(0.2)}
    assert UNCALIBRATED.changed_px_min == 1 and UNCALIBRATED.component_area_min == 1
    loose = money_cell_threshold(RegionThreshold(8, 12, 2.5, 2))
    assert (loose.component_area_min, loose.changed_px_min, loose.delta_e_min, loose.shift_px_min) == (1, 1, 2.5, 1)
