"""Every Oracle extraction stored as an 8.3 `oracle_extractions` row, and rules re-run offline over the stored rows
(2.8.1, 5.10, SD-09)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sentinel.oracle import (EvalContext, GroundTruthApi, GroundTruthReader, RegionHistory, RegionRule,
                             RegionRuleChecker, RegularSession, evaluate_assertions, rerun_offline, to_rows)
from sentinel.oracle.extraction_log import reads
from sentinel.records import Extraction as Row
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = yaml.safe_load((ROOT / "config/example/fixtures/fx.rig-01.paper.yaml").read_text())
STOCK_FLOW = yaml.safe_load((ROOT / "config/example/flows/tier-1/flow.trade.order-ticket.stock.limit.yaml").read_text())
S07 = next(s for s in STOCK_FLOW["steps"] if s["id"] == "s07")
CONFIRM = {"rgn.confirm.side": "BUY", "rgn.confirm.qty": "100", "rgn.confirm.symbol": "AAPL",
           "rgn.confirm.price": "150.25 LMT", "rgn.confirm.cost": "15,025.00", "rgn.confirm.bp-effect": "-15,025.00",
           "rgn.confirm.max-loss": "15,025.00", "rgn.confirm.send-button": "Send",
           "rgn.confirm.cancel-button": "Cancel", "rgn.confirm.delete-button": "Delete"}
RUN = "run-01"
WS = "ws.tier-a.active-trader"
CP = f"census/trade.active-trader.ladder/{WS}"
AT = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)               # 11:00 New York
LIVE = EvalContext(data_mode="masked_live", symbol="AAPL")
SHA = "sha256:" + "c" * 64
MARK = {"field": "price", "decimals": 2, "min": 0}


def rule_results(rule: dict, sources: dict, *, rid: str = "rgn.x", history=None, at=AT, checker=None) -> list:
    ck = checker or RegionRuleChecker(fixture=FIXTURE, context=LIVE, calendar=RegularSession())
    file = {"checkpoint_id": CP, "regions": [{"id": rid, "type": "rule", "rule": rule}]}
    return ck.check(CP, "candidate", file, sources, history, at)


def rows_of(results, side="candidate", at=AT) -> list[Row]:
    return to_rows(results, run_id=RUN, checkpoint_id=CP, side=side, read_at=at)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=(ScopeEntry(CP, WS, "active_trader", 1, "masked_live"),),
                        shards=(ShardPlan(f"{RUN}/1", "rig-a1"),), base_commit_distance=2,
                        authorization_sha256="sha256:auth", pr_committers=("ann", "bob")))
    yield s
    s.close()


# --------------------------------------------------------------------------------------------
# Rows


def test_a_rule_regions_read_is_stored_once_with_its_second_pass():
    ocr = GroundTruthReader({"rgn.x": "180.25"}, crop_refs={"rgn.x": SHA})
    results = rule_results(MARK, {"ocr": ocr})
    assert len(results) == 3                                             # field, decimals, min share one read
    main, second = rows_of(results)
    assert (main.assertion_id, main.field, main.region_id, main.source) == ("rule.rgn.x.field", "rgn.x", "rgn.x", "ocr")
    assert (main.text, main.value, main.confidence, main.second_pass, main.crop_sha256) == \
        ("180.25", "180.25", 95.0, False, SHA)
    assert (second.text, second.value, second.confidence, second.second_pass) == ("180.25", None, None, True)
    assert main.read_at == AT.isoformat()


def test_every_source_tried_is_stored_and_only_the_accepted_read_carries_a_value():
    ocr = GroundTruthReader({"rgn.x": "180.25"}, confidence=60, crop_refs={"rgn.x": "crops/x.png"})
    jab = GroundTruthReader({"rgn.x": "180.25\n12.50"}, source="jab")
    rows = rows_of(rule_results(MARK, {"ocr": ocr, "jab": jab}))
    assert [(r.source, r.second_pass, r.value, r.confidence) for r in rows] == [
        ("ocr", False, None, 60.0), ("ocr", True, None, None), ("jab", False, "180.25\n12.50", 95.0)]
    assert all(r.crop_sha256 is None for r in rows)                      # a path is not a content hash
    nothing = rows_of(rule_results(MARK, {"ocr": GroundTruthReader({})}))
    assert [(r.source, r.text, r.confidence) for r in nothing] == [("ocr", None, None)]


def test_the_api_value_is_stored_against_its_region_and_the_previous_read_is_not_stored_again():
    series = {("AAPL", "last"): [(AT - timedelta(minutes=30), "180.20")]}
    rule = {"field": "price", "api_window": {"field": "last"}}
    rows = rows_of(rule_results(rule, {"ocr": GroundTruthReader({"rgn.x": "180.21"}), "api": GroundTruthApi(series)}))
    api = [r for r in rows if r.source == "api"]
    assert [(r.assertion_id, r.field, r.region_id, r.value) for r in api] == \
        [("rule.rgn.x.api_window", "api:last", "rgn.x", "180.20")]
    h = RegionHistory()
    tick = {"field": "time", "must_advance": True}
    rule_results(tick, {"ocr": GroundTruthReader({"rgn.t": "10:59:58"})}, rid="rgn.t", history=h)
    later = rule_results(tick, {"ocr": GroundTruthReader({"rgn.t": "10:59:59"})}, rid="rgn.t", history=h,
                         at=AT + timedelta(seconds=1))
    assert "previous" in later[1].operands
    assert {(r.field, r.text) for r in rows_of(later)} == {("rgn.t", "10:59:59")}


def test_rows_round_trip_through_the_store(store):
    ocr = GroundTruthReader({"rgn.x": "180.25"}, crop_refs={"rgn.x": SHA})
    cand = rows_of(rule_results(MARK, {"ocr": ocr}))
    base = rows_of(rule_results(MARK, {"ocr": ocr}), side="base")
    store.write_extractions(cand + base)
    store.write_extractions(cand)                                        # a replay is a no-op
    stored = store.extractions(RUN, CP)
    assert len(stored) == 4 and {r.side for r in stored} == {"candidate", "base"}
    rerun = rerun_offline(stored, [RegionRule.from_dict("rgn.x", MARK)])
    assert set(rerun) == {(CP, "candidate"), (CP, "base")}
    assert [r.status for r in rerun[(CP, "candidate")]] == ["passed", "passed", "passed"]


# --------------------------------------------------------------------------------------------
# Offline re-runs


def test_a_stored_read_is_re_judged_against_the_rules_own_floor():
    ocr = GroundTruthReader({"rgn.x": "180.255"}, confidence=80)
    rows = rows_of(rule_results({"field": "text"}, {"ocr": ocr}))           # text floor 70: accepted
    assert rows[0].value == "180.255"
    [text] = rerun_offline(rows, [RegionRule.from_dict("rgn.x", {"field": "text"})])[(CP, "candidate")]
    assert text.status == "passed"
    price = rerun_offline(rows, [RegionRule.from_dict("rgn.x", {"field": "price", "decimals": 2})])[(CP, "candidate")]
    assert [r.status for r in price] == ["unreadable", "unreadable"]      # price floor 85
    clear = rows_of(rule_results({"field": "text"}, {"ocr": GroundTruthReader({"rgn.x": "180.255"})}))
    again = rerun_offline(clear, [RegionRule.from_dict("rgn.x", {"field": "price", "decimals": 2})])
    assert [(r.assertion_id, r.status) for r in again[(CP, "candidate")]] == \
        [("rule.rgn.x.field", "passed"), ("rule.rgn.x.decimals", "failed")]


def test_rerun_skips_the_temporal_checks_and_re_judges_the_api_window_over_the_stored_value():
    series = {("AAPL", "last"): [(AT - timedelta(minutes=30), "180.20")]}
    rule = {"field": "price", "must_change_within_s": 30, "api_window": {"field": "last"}}
    online = rule_results(rule, {"ocr": GroundTruthReader({"rgn.x": "180.22"}), "api": GroundTruthApi(series)})
    assert [r.status for r in online] == ["passed", "passed", "failed"]
    offline = rerun_offline(rows_of(online), [RegionRule.from_dict("rgn.x", rule)], fixture=FIXTURE, context=LIVE)
    assert [(r.assertion_id, r.status) for r in offline[(CP, "candidate")]] == \
        [("rule.rgn.x.field", "passed"), ("rule.rgn.x.api_window", "failed")]
    no_api = rerun_offline(rows_of(online)[:2], [RegionRule.from_dict("rgn.x", rule)], context=LIVE)
    assert [r.assertion_id for r in no_api[(CP, "candidate")]] == ["rule.rgn.x.field"]
    assert rerun_offline(rows_of(online), [RegionRule.from_dict("rgn.other", rule)]) == {}


def test_flow_assertions_are_stored_and_an_r01_failure_re_runs_from_the_rows():
    wrong = dict(CONFIRM, **{"rgn.confirm.cost": "15,250.00"})
    results = evaluate_assertions(S07, {"ocr": GroundTruthReader(wrong)}, {}, FIXTURE,
                                  context=EvalContext(symbol="AAPL", opening=True))
    cost = next(r for r in results if r.assertion_id == "cost_math")
    assert cost.status == "failed" and all(r.region is None or r.region.startswith("rgn.") for r in results)
    rows = rows_of(results)
    assert {r.assertion_id for r in rows} >= {"cost_math", "qty", "price"}
    assert all(r.region_id == r.field for r in rows)
    spec = next(a for a in S07["expect"] if a["id"] == "cost_math")
    [again] = rerun_offline(rows, [spec], fixture=FIXTURE, context=EvalContext(symbol="AAPL"))[(CP, "candidate")]
    assert (again.assertion_id, again.kind, again.status, again.detail) == ("cost_math", "rule", "failed", cost.detail)
    assert set(again.operands) == set(cost.operands)
    partial = [r for r in rows if r.field != "rgn.confirm.cost"]
    assert rerun_offline(partial, [spec], fixture=FIXTURE) == {}


def test_reads_take_the_first_assertions_rows_for_a_field():
    at = AT.isoformat()
    first = Row(RUN, CP, "candidate", "a1", "rgn.x", "ocr", at, region_id="rgn.x", text="1.00", confidence=99.0)
    first_2x = Row(RUN, CP, "candidate", "a1", "rgn.x", "ocr", at, region_id="rgn.x", text="1.00", second_pass=True)
    other = Row(RUN, CP, "candidate", "a2", "rgn.x", "ocr", at, region_id="rgn.x", text="7.00", confidence=99.0)
    [[(key, read)]] = [list(v.items()) for v in reads([first, first_2x, other]).values()]
    assert key == ("rgn.x", "rgn.x") and read.read_at == AT
    [attempt] = read.extraction.attempts
    assert (attempt.reading.text, attempt.reading.second_pass, read.extraction.accepted) == ("1.00", "1.00", None)
