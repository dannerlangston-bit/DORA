"""Rule regions (3.8, 4.8, 2.1, SD-29, SD-70): every rule field passing and failing on synthetic reads, the temporal
checks against the same side's previous read under a venue calendar, the API window, unreadable and calibration
outcomes, the history, the regions-file readers and the INV-03/04 rule regions."""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
import yaml

from sentinel.classify import Facts, classify
from sentinel.diff.regions import RegionsFile
from sentinel.oracle import (EvalContext, GroundTruthApi, GroundTruthReader, OracleConfigError, RegionHistory,
                             RegionRule, RegionRuleChecker, RegularSession, invariant_rule_regions, rule_regions,
                             summarize)
from sentinel.oracle.region_rules import NEW_YORK, REGION_RULE_KIND, PriorRead, region_entries
from sentinel.oracle.sources import literal
from sentinel.orchestrator.clock import ManualClock
from sentinel.records import CheckpointOutcome

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/example/regions"
FIXTURE = yaml.safe_load((ROOT / "config/example/fixtures/fx.rig-01.paper.yaml").read_text())
CENSUS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
FLOW = "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/cp-positions"
CP = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
T0 = datetime(2026, 11, 10, 11, 0, tzinfo=NEW_YORK)              # a Tuesday, mid-session (shown 10:40 with the delay)
LIVE = EvalContext(data_mode="masked_live", symbol="AAPL")


def regions(*rules: tuple[str, dict], cid: str = CP) -> dict:
    return {"checkpoint_id": cid, "regions": [{"id": rid, "type": "rule", "rule": rule} for rid, rule in rules]}


def run(rule: dict, text: str | None, *, rid: str = "rgn.x", history: RegionHistory | None = None,
        at: datetime = T0, checker: RegionRuleChecker | None = None, sources: dict | None = None, side="candidate",
        context: EvalContext | None = None) -> dict:
    """The results of one rule region by check name."""
    ck = checker or RegionRuleChecker(fixture=FIXTURE, context=LIVE, calendar=RegularSession())
    srcs = sources if sources is not None else {"ocr": GroundTruthReader({rid: text})}
    out = ck.check(CP, side, regions((rid, rule)), srcs, history, at, context=context)
    assert all(r.kind == REGION_RULE_KIND and r.region == rid for r in out)
    return {r.assertion_id.removeprefix(f"rule.{rid}."): r for r in out}


def status(results: dict) -> dict:
    return {k: r.status for k, r in results.items()}


# --------------------------------------------------------------------------------------------
# Reading the rule record


def test_example_rule_regions_parse_from_mappings_and_regions_files():
    census = yaml.safe_load((EXAMPLE / "census/monitor.activity.position-statement/ws.tier-a.active-trader.yaml")
                            .read_text())
    mark, pl = rule_regions(census, CENSUS)
    assert (mark.region_id, mark.field, mark.decimals, mark.min, mark.min_rows) == ("rgn.ps.mark", "price", 2, 0, 1)
    assert mark.checks == ("field", "decimals", "min", "min_rows")
    assert pl.checks == ("field", "decimals") and pl.assertion_id("decimals") == "rule.rgn.ps.pl-open.decimals"
    s07 = yaml.safe_load((EXAMPLE / "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml")
                         .read_text())
    [ts] = rule_regions(s07)
    assert (ts.region_id, ts.field, ts.checks) == ("rgn.confirm.timestamp", "time", ("field", "pattern"))
    flow = yaml.safe_load((EXAMPLE / "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/"
                                     "cp-positions.yaml").read_text())
    tpl = {lm["id"]: np.zeros((4, 4, 3), np.uint8) for lm in flow["landmarks"]}
    assert rule_regions(RegionsFile.from_dict(flow, templates=tpl), FLOW) == rule_regions(flow, FLOW)


def test_api_window_defaults_and_every_field():
    r = RegionRule.from_dict("rgn.q", {"field": "price", "api_window": {"field": "last"}, "pattern": "^[0-9]",
                                       "max": 10, "must_change_within_s": 30, "must_not_change_when_closed": True})
    assert (r.api_window.delay_s, r.api_window.tol_ticks) == (1200, 1)
    assert r.checks == ("field", "pattern", "max", "must_change_within_s", "must_not_change_when_closed",
                        "api_window")
    assert RegionRule.from_dict("rgn.q", {"field": "price", "api_window": {"field": "last", "delay_s": 0,
                                                                           "tol_ticks": 0}}).api_window.tol_ticks == 0


@pytest.mark.parametrize("rule, why", [
    ({"field": "price", "slack_s": 60}, "unknown rule fields"),
    ({"field": "color"}, "rule field"),
    ({"field": "time", "decimals": 2}, "decimals"),
    ({"field": "int", "decimals": 2}, "decimals"),
    ({"field": "price", "decimals": -1}, "at least 0"),
    ({"field": "text", "min": 0}, "numeric"),
    ({"field": "price", "min": 5, "max": 1}, "above max"),
    ({"field": "price", "min": True}, "a number"),
    ({"field": "price", "must_advance": True}, "time fields"),
    ({"field": "time", "must_advance": "yes"}, "true or false"),
    ({"field": "price", "must_change_within_s": 0}, "at least 1"),
    ({"field": "text", "pattern": "("}, "not a regex"),
    ({"field": "money", "api_window": {"field": "last"}}, "prices in ticks"),
    ({"field": "price", "api_window": {"delay_s": 60}}, "api_window is"),
    ({"field": "price", "api_window": {"field": "last", "slack_s": 60}}, "api_window is"),
])
def test_rules_the_oracle_cannot_evaluate_are_config_errors(rule, why):
    with pytest.raises(OracleConfigError, match=why):
        RegionRule.from_dict("rgn.x", rule)


def test_rule_only_on_rule_regions_and_on_the_checkpoints_own_file():
    with pytest.raises(OracleConfigError, match="L-RGN-03"):
        rule_regions({"regions": [{"id": "rgn.a", "type": "diff", "rule": {"field": "price"}}]})
    with pytest.raises(OracleConfigError, match="L-RGN-03"):
        rule_regions({"regions": [{"id": "rgn.a", "type": "rule"}]})
    with pytest.raises(OracleConfigError, match="L-RGN-01"):
        rule_regions(regions(("rgn.a", {"field": "price"})), CENSUS)
    with pytest.raises(OracleConfigError, match="assertion id"):
        RegionRule.from_dict("rgn a", {"field": "price"})
    assert rule_regions(None) == () and region_entries(None) == []


# --------------------------------------------------------------------------------------------
# Single-read checks


def test_field_is_the_r10_format_test_and_names_the_outcome():
    ok = run({"field": "money", "decimals": 2}, "-$1,150.00\n$25.50")
    assert status(ok) == {"field": "passed", "decimals": "passed"}
    bad = run({"field": "money", "decimals": 2}, "NaN")
    assert status(bad) == {"field": "failed", "decimals": "passed"}         # the NaN row is judged by field only
    assert "not a valid money: NaN (R10)" in bad["field"].detail
    for text, why in (("1,50,25.00", "misplaced separator"), ("--5.00", "double sign"), ("Infinity", "Infinity")):
        assert why in run({"field": "price"}, text)["field"].detail
    verdict = summarize(list(bad.values()))
    c = classify(Facts(**verdict.facts_fields()))
    assert (c.row, c.outcome, c.reason) == ("8", "assertion_failed", "assertion:rule.rgn.x.field")
    CheckpointOutcome(run_id="run-1", checkpoint_id=CP, outcome=c.outcome, reason=c.reason,
                      workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="masked_live")


def test_decimals_two_versus_three():
    assert run({"field": "price", "decimals": 2}, "180.25\n12.50")["decimals"].status == "passed"
    bad = run({"field": "price", "decimals": 2}, "180.25\n12.500")["decimals"]
    assert (bad.status, bad.expected, bad.extracted) == ("failed", "2 decimals", "3 decimals in '12.500'")
    assert "row 2" in bad.detail
    assert run({"field": "percent", "decimals": 1}, "+2.5%")["decimals"].status == "passed"
    assert run({"field": "price", "decimals": 2}, "1,234")["decimals"].status == "failed"
    assert run({"field": "int", "decimals": 0}, "12,000")["decimals"].status == "passed"


def test_min_and_max():
    assert run({"field": "price", "min": 0}, "0.00\n1.25")["min"].status == "passed"
    low = run({"field": "price", "min": 0}, "-1.00")["min"]
    assert (low.status, low.expected, low.extracted) == ("failed", "at least 0", "-1.00")
    high = run({"field": "percent", "max": 100}, "100.5%")["max"]
    assert (high.status, high.expected) == ("failed", "at most 100%")
    assert run({"field": "int", "max": 10}, "(3)")["max"].status == "passed"


def test_pattern_searches_every_row():
    rule = {"field": "time", "pattern": "^[0-9]{1,2}:[0-9]{2}:[0-9]{2}"}
    assert status(run(rule, "10:31:05 ET")) == {"field": "passed", "pattern": "passed"}
    bad = run(rule, "Loading...")
    assert status(bad) == {"field": "failed", "pattern": "failed"}
    col = run({"field": "text", "pattern": "^[A-Z]{1,5}$"}, "AAPL\nspy")["pattern"]
    assert col.status == "failed" and "row 2 reads 'spy'" in col.detail


def test_min_rows_on_an_empty_column_and_empty_rows_belong_to_min_rows():
    empty = run({"field": "price", "decimals": 2, "min": 0, "min_rows": 1}, "")
    assert status(empty) == {"field": "passed", "decimals": "passed", "min": "passed", "min_rows": "failed"}
    assert empty["min_rows"].extracted == "0 rows"
    assert run({"field": "price", "min_rows": 2}, "1.00\n\n2.00")["min_rows"].status == "passed"


# --------------------------------------------------------------------------------------------
# Temporal checks


def test_must_advance_against_the_previous_read_on_the_same_side():
    rule = {"field": "time", "must_advance": True}
    h = RegionHistory()
    first = run(rule, "10:59:58", history=h)["must_advance"]
    assert first.status == "passed" and "not judged: no earlier read" in first.expected
    assert run(rule, "10:59:59", history=h, at=T0 + timedelta(seconds=1))["must_advance"].status == "passed"
    equal = run(rule, "10:59:59", history=h, at=T0 + timedelta(seconds=2))["must_advance"]
    assert equal.status == "failed" and "not later than '10:59:59'" in equal.detail
    older = run(rule, "10:59:00", history=h, at=T0 + timedelta(seconds=3))["must_advance"]
    assert older.status == "failed"
    assert set(older.operands) == {"region", "previous"}
    assert older.raw_values()[f"candidate.{CP}:rgn.x.ocr"].startswith("'10:59:59'")
    base = run(rule, "10:00:00", history=h, at=T0 + timedelta(seconds=4), side="base")["must_advance"]
    assert "not judged" in base.expected                                # the candidate's reads are not the base's
    late = run(rule, "00:00:01", history=h, at=datetime(2026, 11, 11, 0, 0, 1, tzinfo=NEW_YORK))["must_advance"]
    assert late.status == "passed" and "different New York dates" in late.expected


def test_must_change_within_s_during_venue_hours():
    rule = {"field": "price", "must_change_within_s": 30}
    h = RegionHistory()
    run(rule, "180.25", history=h)
    soon = run(rule, "180.25", history=h, at=T0 + timedelta(seconds=10))["must_change_within_s"]
    assert soon.status == "passed" and "10 s apart" in soon.expected
    h2 = RegionHistory()
    run(rule, "180.25", history=h2)
    stuck = run(rule, "180.25", history=h2, at=T0 + timedelta(seconds=45))["must_change_within_s"]
    assert stuck.status == "failed" and "still reads '180.25' 45 s after" in stuck.detail
    h3 = RegionHistory()
    run(rule, "180.25", history=h3)
    assert run(rule, "180.27", history=h3, at=T0 + timedelta(seconds=45))["must_change_within_s"].status == "passed"


def test_must_change_within_s_outside_venue_hours_or_inside_the_delay_is_not_judged():
    rule = {"field": "price", "must_change_within_s": 30}
    for start in (datetime(2026, 11, 10, 17, 0, tzinfo=NEW_YORK),        # after the close
                  datetime(2026, 11, 10, 9, 45, tzinfo=NEW_YORK),        # open, but the delayed data shows 09:25
                  datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK),        # Saturday
                  datetime(2026, 11, 26, 11, 0, tzinfo=NEW_YORK)):       # Thanksgiving
        h = RegionHistory()
        cal = RegularSession(holidays=frozenset({date(2026, 11, 26)}))
        ck = RegionRuleChecker(fixture=FIXTURE, context=LIVE, calendar=cal)
        run(rule, "180.25", history=h, at=start, checker=ck)
        r = run(rule, "180.25", history=h, at=start + timedelta(seconds=60), checker=ck)["must_change_within_s"]
        assert r.status == "passed" and "venue was not open" in r.expected, start
    realtime = dict(FIXTURE, data_mode={"masked_live": {"realtime": True}})
    ck = RegionRuleChecker(fixture=realtime, context=LIVE, calendar=RegularSession())
    h = RegionHistory()
    run(rule, "180.25", history=h, at=datetime(2026, 11, 10, 9, 45, tzinfo=NEW_YORK), checker=ck)
    r = run(rule, "180.25", history=h, at=datetime(2026, 11, 10, 9, 46, tzinfo=NEW_YORK), checker=ck)
    assert r["must_change_within_s"].status == "failed"                 # real-time data: 09:45 is in session


def test_must_not_change_when_closed():
    rule = {"field": "price", "must_not_change_when_closed": True}
    evening = datetime(2026, 11, 10, 18, 0, tzinfo=NEW_YORK)
    h = RegionHistory()
    run(rule, "180.25", history=h, at=evening)
    moved = run(rule, "180.31", history=h, at=evening + timedelta(minutes=5))["must_not_change_when_closed"]
    assert moved.status == "failed" and "venue closed throughout" in moved.detail
    assert run(rule, "180.31", history=h, at=evening + timedelta(minutes=9))["must_not_change_when_closed"].status \
        == "passed"
    after_close = datetime(2026, 11, 10, 16, 5, tzinfo=NEW_YORK)        # the delayed data still shows 15:45
    h2 = RegionHistory()
    run(rule, "180.25", history=h2, at=after_close)
    r = run(rule, "180.40", history=h2, at=after_close + timedelta(minutes=5))["must_not_change_when_closed"]
    assert r.status == "passed" and "venue was open" in r.expected
    early = RegularSession(early_closes={date(2026, 11, 27): time(13, 0)})
    ck = RegionRuleChecker(fixture=FIXTURE, context=LIVE, calendar=early)
    h3 = RegionHistory()
    run(rule, "180.25", history=h3, at=datetime(2026, 11, 27, 14, 0, tzinfo=NEW_YORK), checker=ck)
    r = run(rule, "180.40", history=h3, at=datetime(2026, 11, 27, 14, 5, tzinfo=NEW_YORK), checker=ck)
    assert r["must_not_change_when_closed"].status == "failed"            # closed at 13:00 that day


def test_frozen_checkpoints_take_no_delay_and_venue_checks_need_a_calendar():
    ck = RegionRuleChecker(fixture=FIXTURE, context=EvalContext(data_mode="frozen"))
    assert ck.delay(ck.context) == timedelta(0)
    assert ck.delay(LIVE) == timedelta(minutes=20)
    with pytest.raises(OracleConfigError, match="exchange calendar"):
        run({"field": "price", "must_change_within_s": 30}, "1.00", checker=ck)
    assert run({"field": "time", "must_advance": True}, "10:00", checker=ck)["must_advance"].status == "passed"


def test_regular_session():
    cal = RegularSession(holidays=frozenset({date(2026, 11, 26)}))
    at = datetime(2026, 11, 10, 10, 0, tzinfo=NEW_YORK)
    assert cal.open_throughout(at, at + timedelta(hours=1))
    assert not cal.open_throughout(at, at + timedelta(hours=7))           # past 16:00
    assert not cal.closed_throughout(at - timedelta(hours=2), at)        # 08:00 to 10:00 overlaps the open
    assert cal.closed_throughout(datetime(2026, 11, 13, 16, 0, tzinfo=NEW_YORK),
                                 datetime(2026, 11, 16, 9, 30, tzinfo=NEW_YORK))   # Friday close to Monday open
    assert cal.session(date(2026, 11, 26)) is None and cal.session(date(2026, 11, 14)) is None
    assert cal.open_throughout(datetime(2026, 11, 10, 15, 0), datetime(2026, 11, 10, 15, 30))   # naive is UTC


# --------------------------------------------------------------------------------------------
# The API window (2.8.2, SD-70)

SERIES = {("AAPL", "last"): [(datetime(2026, 11, 10, 10, 39, tzinfo=NEW_YORK), "180.20"),
                             (datetime(2026, 11, 10, 10, 41, tzinfo=NEW_YORK), "181.00")]}


def api_run(text: str, *, api=None, checker=None, context=LIVE, window=None, at=T0) -> object:
    ck = checker or RegionRuleChecker(fixture=FIXTURE, context=context)
    rule = {"field": "price", "api_window": window or {"field": "last"}}
    srcs = {"ocr": GroundTruthReader({"rgn.x": text}), "api": api if api is not None else GroundTruthApi(SERIES)}
    return run(rule, text, checker=ck, sources=srcs, at=at)["api_window"]


def test_api_window_compares_with_the_point_value_twenty_minutes_back_within_one_tick():
    inside = api_run("180.21")                                            # API as of 10:40 is 180.20
    assert inside.status == "passed" and "180.20 +/- 0.01" in inside.expected and "10:40:00" in inside.expected
    outside = api_run("180.22")
    assert outside.status == "failed" and "within 1 ticks" in outside.detail
    assert set(outside.operands) == {"region", "api"} and outside.operands["api"].field == "api:last"
    assert api_run("181.00", window={"field": "last", "delay_s": 0}).status == "passed"   # 11:00 value
    assert api_run("180.23", window={"field": "last", "tol_ticks": 3}).status == "passed"


def test_api_window_is_exact_once_the_session_closed_more_than_the_delay_before():
    closed = EvalContext(data_mode="masked_live", symbol="AAPL", session_close=T0 - timedelta(hours=2))
    exact = api_run("180.21", context=closed)
    assert exact.status == "failed" and "exact" in exact.expected
    assert api_run("180.20", context=closed).status == "passed"
    overnight = EvalContext(data_mode="masked_live", symbol="AAPL", session_close=T0 - timedelta(hours=2),
                            trading_24h=frozenset({"AAPL"}))
    assert api_run("180.21", context=overnight).status == "passed"


def test_api_window_frozen_takes_the_fixtures_pinned_moment():
    series = {("AAPL", "last"): [(datetime(2026, 11, 9, 15, 30, tzinfo=NEW_YORK), "175.00")]}
    frozen = EvalContext(data_mode="frozen", symbol="AAPL")
    r = api_run("175.01", context=frozen, api=GroundTruthApi(series))
    assert r.status == "passed" and "as of 2026-11-09T15:30:00-05:00" in r.expected


def test_api_failures_are_env_errors_and_a_missing_value_is_unreadable():
    pre = api_run("180.21", checker=RegionRuleChecker(fixture=FIXTURE, context=LIVE, api_preflight_ok=False))
    assert (pre.status, pre.gate) == ("env_error", "preflight.N3")
    ck = RegionRuleChecker(fixture=FIXTURE, context=LIVE)
    lost = api_run("180.21", checker=ck, api=GroundTruthApi(SERIES, available=False))
    assert (lost.status, lost.gate) == ("env_error", "connectivity")
    later = api_run("180.21", checker=ck)                                  # the run's remaining API checks
    assert (later.status, later.gate) == ("env_error", "connectivity") and "lost mid-run" in later.detail
    none = api_run("180.21", api=GroundTruthApi({}))
    assert none.status == "unreadable" and none.unreadable == ("api:last",)
    c = classify(Facts(**summarize([lost]).facts_fields()))
    assert (c.outcome, c.gate) == ("env_error", "connectivity")
    with pytest.raises(OracleConfigError, match="symbol"):
        api_run("180.21", context=EvalContext(data_mode="masked_live"))
    with pytest.raises(OracleConfigError, match="market-data API source"):
        run({"field": "price", "api_window": {"field": "last"}}, "1.00")


# --------------------------------------------------------------------------------------------
# Unreadable values, alternates, the calibration panel


def test_an_unreadable_region_is_needs_human_on_every_check_and_never_a_pass():
    ocr = GroundTruthReader({"rgn.x": "180.25"}, confidence=60, crop_refs={"rgn.x": "sha256:" + "a" * 64})
    res = run({"field": "price", "decimals": 2, "min_rows": 1}, None, sources={"ocr": ocr})
    assert status(res) == {"field": "unreadable", "decimals": "unreadable", "min_rows": "unreadable"}
    assert res["field"].unreadable == ("rgn.x",) and "below the price floor 85" in res["field"].detail
    c = classify(Facts(**summarize(list(res.values())).facts_fields()))
    assert (c.row, c.outcome, c.reason) == ("9a", "needs_human", "oracle_unreadable")


def test_the_alternate_source_reads_a_cell_ocr_cannot():
    ocr = GroundTruthReader({"rgn.x": "180.25"}, confidence=60)
    jab = GroundTruthReader({"rgn.x": "180.251"}, source="jab")
    res = run({"field": "price", "decimals": 2}, None, sources={"ocr": ocr, "jab": jab})
    assert status(res) == {"field": "passed", "decimals": "failed"}       # the JAB value is the one judged
    assert [fb.event() for fb in summarize(list(res.values())).fallbacks] == ["oracle_source_fallback:rgn.x:ocr->jab"]


def test_a_calibration_miss_is_harness_error_unless_another_source_reads_the_cell():
    miss = RegionRuleChecker(fixture=FIXTURE, context=LIVE, ocr_calibrated=False)
    res = run({"field": "price", "decimals": 2}, "180.25", checker=miss)
    assert {r.reason for r in res.values()} == {"ocr_calibration"} and status(res)["field"] == "harness_error"
    c = classify(Facts(**summarize(list(res.values())).facts_fields(harness_event_id="wd-1")))
    assert (c.outcome, c.reason) == ("harness_error", "ocr_calibration")
    srcs = {"ocr": GroundTruthReader({"rgn.x": "180.25"}), "jab": GroundTruthReader({"rgn.x": "180.25"}, source="jab")}
    assert status(run({"field": "price", "decimals": 2}, None, checker=miss, sources=srcs)) == \
        {"field": "passed", "decimals": "passed"}


# --------------------------------------------------------------------------------------------
# History, clock, several regions


def test_history_records_readable_reads_after_the_checks_per_side():
    h = RegionHistory()
    run({"field": "price"}, "1.00", history=h)
    first = h.last("candidate", "rgn.x")
    assert first is not None and first.extraction.text == "1.00" and first.checkpoint_id == CP
    run({"field": "price"}, "2.00", history=h, sources={"ocr": GroundTruthReader({"rgn.x": "2.00"}, confidence=10)})
    assert h.last("candidate", "rgn.x") is first                           # an unreadable read is not recorded
    assert h.last("base", "rgn.x") is None
    h.record("base", "rgn.x", PriorRead(CP, T0, literal("rgn.x", "price", "3.00")))
    assert h.last("base", "rgn.x").extraction.text == "3.00"


def test_a_clock_gives_the_read_time_and_regions_run_in_file_order():
    clock = ManualClock(datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc))
    ck = RegionRuleChecker(fixture=FIXTURE, context=LIVE, calendar=RegularSession())
    h = RegionHistory()
    file = regions(("rgn.b", {"field": "price", "must_change_within_s": 30}), ("rgn.a", {"field": "text"}))
    srcs = {"ocr": GroundTruthReader({"rgn.a": "AAPL", "rgn.b": "1.00"})}
    first = ck.check(CP, "candidate", file, srcs, h, clock)
    assert [r.assertion_id for r in first] == ["rule.rgn.b.field", "rule.rgn.b.must_change_within_s",
                                               "rule.rgn.a.field"]
    assert h.last("candidate", "rgn.b").read_at == clock.now()
    clock.advance(40)
    again = ck.check(CP, "candidate", file, srcs, h, clock)
    assert again[1].status == "failed" and "40 s after" in again[1].detail
    assert ck.check(CP, "candidate", None, srcs, h, clock) == []
    with pytest.raises(ValueError, match="side"):
        ck.check(CP, "both", file, srcs, h, clock)


# --------------------------------------------------------------------------------------------
# INV-03 and INV-04 judge text inside a rule region by its rule (2.7)


def test_invariant_rule_regions_for_surface_rules():
    file = regions(("rgn.mark", {"field": "price", "decimals": 2, "min": 0}), ("rgn.sym", {"field": "text",
                                                                                          "pattern": "[A-Z]+"}))
    file["regions"].append({"id": "rgn.other", "type": "diff"})
    located = {"rgn.mark": type("R", (), {"x": 10, "y": 20, "w": 90, "h": 40})(), "rgn.sym": (0, 0, 50, 20),
               "rgn.other": (0, 0, 1, 1)}
    mark, sym = invariant_rule_regions(file, located)
    assert (mark.id, mark.rect.x, mark.rect.y, mark.rect.w, mark.field, mark.decimals, mark.min) == \
        ("rgn.mark", 10, 20, 90, "price", 2, 0.0)
    assert mark.accepts("180.25") and not mark.accepts("180.251") and not mark.accepts("-1.00")
    assert sym.pattern == "[A-Z]+" and sym.admits_ellipsis("AAPL")
    assert invariant_rule_regions(file, {}) == ()
