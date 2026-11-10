"""Assertion evaluation (2.6.2, 4.7) and its outcomes (3.11, 4.13, 2.8.1): every kind passing and
failing, unreadable values and the alternate source, the calibration panel, export headers, the
paperMoney delay model and the classifier's view of the results."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sentinel.classify import Facts, classify
from sentinel.oracle import (Assertion, EvalContext, ExportSource, ExportTable, GroundTruthApi, GroundTruthReader,
                             OracleConfigError, RuleConfig, evaluate_assertions, step_assertions, summarize)
from sentinel.oracle.sources import extract
from sentinel.oracle.tables import api_comparison, delay_minutes
from sentinel.records import CheckpointOutcome, Finding

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = yaml.safe_load((ROOT / "config/example/fixtures/fx.rig-01.paper.yaml").read_text())
STOCK_FLOW = yaml.safe_load((ROOT / "config/example/flows/tier-1/flow.trade.order-ticket.stock.limit.yaml").read_text())
READ_BACK = yaml.safe_load((ROOT / "config/example/flows/tier-1/flow.trade.active-trader.read-back.yaml").read_text())
S07 = next(s for s in STOCK_FLOW["steps"] if s["id"] == "s07")
S07_ID = S07["checkpoint_id"]
CONFIRM = {"rgn.confirm.side": "BUY", "rgn.confirm.qty": "100", "rgn.confirm.symbol": "AAPL",
           "rgn.confirm.price": "150.25 LMT", "rgn.confirm.cost": "15,025.00", "rgn.confirm.bp-effect": "-15,025.00",
           "rgn.confirm.max-loss": "15,025.00", "rgn.confirm.send-button": "Send",
           "rgn.confirm.cancel-button": "Cancel", "rgn.confirm.delete-button": "Delete"}
STOCK = EvalContext(symbol="AAPL", opening=True)
PALETTE = {"positive": (60, 200, 90), "negative": (230, 70, 70), "neutral": (200, 200, 200)}
BP = "rgn.confirm.bp-effect"


FROZEN_AAPL = EvalContext(data_mode="frozen", symbol="AAPL")


def one(assertion: dict, values: dict | None = None, *, sources: dict | None = None, previous: dict | None = None,
        context: EvalContext = FROZEN_AAPL, config: RuleConfig | None = None, **flags):
    srcs = sources if sources is not None else {"ocr": GroundTruthReader(values or {})}
    [r] = evaluate_assertions({"assertions": [assertion]}, srcs, previous or {}, FIXTURE, config or RuleConfig(),
                              context=context, **flags)
    return r


def facts_for(results, **kw) -> Facts:
    return Facts(**summarize(results).facts_fields(**kw))


# --------------------------------------------------------------------------------------------
# Every kind, passing and failing


def test_equals_text_int_price_and_money():
    side = {"id": "side", "kind": "equals", "region": "s", "expected": "BUY"}
    assert one(side, {"s": "BUY"}).status == "passed"
    assert one(side, {"s": "SELL"}).status == "failed"
    qty = {"id": "qty", "kind": "equals", "region": "q", "format": "int", "expected": 100}
    assert one(qty, {"q": "+100"}).status == "passed"
    bad = one(qty, {"q": "108"})
    assert (bad.status, bad.expected, bad.extracted) == ("failed", "100", "108")
    price = {"id": "price", "kind": "equals", "region": "p", "format": "price",
             "value_ref": "fixture.prices.aapl_far_below.value"}
    assert one(price, {"p": "150.25 LMT"}).status == "passed"
    assert one(price, {"p": "150.52 LMT"}).status == "failed"
    symbol = {"id": "symbol", "kind": "equals", "region": "y", "value_ref": "fixture.symbols.aapl.symbol"}
    assert one(symbol, {"y": "AAPL"}).status == "passed"
    money = {"id": "cash", "kind": "equals", "region": "m", "format": "money", "expected": 50000.004}
    assert one(money, {"m": "$50,000.00"}).status == "passed"           # exact after rounding to the cent
    nan = one(money, {"m": "NaN"})
    assert nan.status == "failed" and "not a valid money: NaN" in nan.detail


def test_matches():
    a = {"id": "ts", "kind": "matches", "region": "t", "pattern": "^[0-9]{1,2}:[0-9]{2}:[0-9]{2}"}
    assert one(a, {"t": "10:31:05"}).status == "passed"
    assert one(a, {"t": "Loading..."}).status == "failed"


def test_within_tol_absolute_percent_and_default():
    a = {"id": "mark", "kind": "within_tol", "region": "m", "expected": 180.25, "tol": 0.05}
    assert one(a, {"m": "180.29"}).status == "passed"
    assert one(a, {"m": "180.31"}).status == "failed"
    pct = {"id": "sma", "kind": "within_tol", "region": "m", "expected": 200, "tol": "0.5%"}
    assert one(pct, {"m": "200.99"}).status == "passed" and one(pct, {"m": "201.01"}).status == "failed"
    default = {"id": "chg", "kind": "within_tol", "region": "m", "format": "percent", "expected": 1.25}
    assert one(default, {"m": "+1.26%"}).status == "passed"            # 2.8.2 percent: 0.01 absolute
    assert one(default, {"m": "+1.27%"}).status == "failed"
    tick = {"id": "last", "kind": "within_tol", "region": "m", "expected": 180.25}
    assert one(tick, {"m": "180.26"}).status == "passed" and one(tick, {"m": "180.27"}).status == "failed"


def test_present_and_absent():
    bp = {"id": "bp_effect", "kind": "present", "region": BP, "format": "money"}
    assert one(bp, {BP: "-15,025.00"}).status == "passed"
    assert one(bp, {BP: "--"}).status == "failed"
    no_placeholder = {"id": "no_placeholder", "kind": "absent", "region": BP, "pattern": "(?i)loading|n/a|--"}
    assert one(no_placeholder, {BP: "-15,025.00"}).status == "passed"
    assert one(no_placeholder, {BP: "Loading..."}).status == "failed"
    empty = {"id": "ticket_empty", "kind": "absent", "region": "q", "pattern": "[0-9]"}
    assert one(empty, {"q": ""}).status == "passed" and one(empty, {"q": "100"}).status == "failed"
    label = {"id": "label", "kind": "present", "region": "l"}
    assert one(label, {"l": "Send"}).status == "passed" and one(label, {"l": ""}).status == "failed"
    gone = {"id": "gone", "kind": "absent", "region": "l"}
    assert one(gone, {"l": ""}).status == "passed" and one(gone, {"l": "Send"}).status == "failed"


def test_changed_and_unchanged_since_previous_step():
    region = "rgn.order-entry.price"
    stored = {"cp1": {region: extract(region, "text", {"ocr": GroundTruthReader({region: ""})})}}
    changed = {"id": "price_changed", "kind": "changed_since_previous_step", "region": region, "since_step": "cp1"}
    assert one(changed, {region: "5,325.00"}, previous=stored).status == "passed"
    assert one(changed, {region: ""}, previous=stored).status == "failed"
    same = {"id": "kept", "kind": "unchanged_since_previous_step", "region": region, "since_step": "cp1",
            "format": "price"}
    before = {"cp1": {region: extract(region, "price", {"ocr": GroundTruthReader({region: "5,325.00"})})}}
    assert one(same, {region: "5325.00"}, previous=before).status == "passed"      # same value, other display
    assert one(same, {region: "5,325.25"}, previous=before).status == "failed"
    unreadable_before = {"cp1": {region: extract(region, "price", {"ocr": GroundTruthReader({region: None})})}}
    r = one(same, {region: "5,325.00"}, previous=unreadable_before)
    assert r.status == "unreadable" and r.unreadable == (f"cp1:{region}",)
    with pytest.raises(OracleConfigError, match="no extraction"):
        one(same, {region: "5,325.00"})


def test_count():
    exact = {"id": "rows", "kind": "count", "region": "g", "expected": 3}
    assert one(exact, {"g": "a\nb\nc"}).status == "passed" and one(exact, {"g": "a\nb"}).status == "failed"
    ranged = {"id": "scan", "kind": "count", "region": "g", "range": {"min": 2, "max": 5}}
    assert one(ranged, {"g": "a\nb"}).status == "passed" and one(ranged, {"g": "a"}).status == "failed"


def _export(text: str, kind: str = "file", header=("Symbol", "Qty", "P/L Open")) -> ExportSource:
    return ExportSource(ExportTable.parse(text, kind), header, field_map={"rgn.ps.total": ("P/L Open", 0)})


CSV = "Symbol,Qty,P/L Open\nAAPL,100,\"1,150.00\"\n/MESZ26,1,-76.25\n"
EXPORT_EQ = {"id": "totals", "kind": "export_equals", "region": "rgn.ps.pl-total", "export_column": "P/L Open"}


def test_export_equals():
    srcs = {"ocr": GroundTruthReader({"rgn.ps.pl-total": "$1,073.75"}), "export": _export(CSV)}
    r = one(EXPORT_EQ, sources=srcs)
    assert r.status == "passed" and r.extracted == "$1,073.75"
    srcs["ocr"] = GroundTruthReader({"rgn.ps.pl-total": "$1,037.75"})
    bad = one(EXPORT_EQ, sources=srcs)
    assert bad.status == "failed" and (bad.expected, bad.extracted) == ("$1,037.75", "$1,073.75")
    nan = one(EXPORT_EQ, sources={"ocr": GroundTruthReader({"rgn.ps.pl-total": "$1.00"}),
                                  "export": _export("Symbol,Qty,P/L Open\nAAPL,100,NaN\n")})
    assert nan.status == "failed" and "NaN" in nan.detail


def test_rule_kind_pass_and_fail():
    rule = {"id": "cost_math", "kind": "rule", "rule_id": "R01", "tol": 0.01,
            "inputs": {"qty": "q", "price": "p", "multiplier": 1, "total": "c"}}
    assert one(rule, {"q": "100", "p": "150.25", "c": "15,025.00"}).status == "passed"
    r = one(rule, {"q": "100", "p": "150.25", "c": "15,250.00"})
    assert r.status == "failed" and r.rule.rule_id == "R01"


def test_sign_color_agrees():
    a = {"id": "pl_color", "kind": "sign_color_agrees", "region": "pl"}
    config = RuleConfig(palette=PALETTE)
    green = GroundTruthReader({"pl": "+115.00"}, colors={"pl": (62, 198, 92)})
    assert one(a, sources={"ocr": green}, config=config).status == "passed"
    red_on_gain = GroundTruthReader({"pl": "+115.00"}, colors={"pl": (228, 72, 70)})
    r = one(a, sources={"ocr": red_on_gain}, config=config)
    assert r.status == "failed" and "negative" in r.detail
    paren_loss = GroundTruthReader({"pl": "(45.00)"}, colors={"pl": (228, 72, 70)})
    assert one(a, sources={"ocr": paren_loss}, config=config).status == "passed"
    no_color = one(a, sources={"ocr": GroundTruthReader({"pl": "+115.00"})}, config=config)
    assert no_color.status == "unreadable" and no_color.unreadable == ("pl:color",)
    with pytest.raises(OracleConfigError, match="palette"):
        one(a, sources={"ocr": green})


def test_api_within_kind_pass_and_fail():
    capture = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    api = GroundTruthApi({("AAPL", "last"): [(capture - timedelta(minutes=20), "180.10")]})
    ctx = EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=capture)
    a = {"id": "last_vs_api", "kind": "api_within", "region": "l", "api_field": "last"}
    assert one(a, sources={"ocr": GroundTruthReader({"l": "180.10"}), "api": api}, context=ctx).status == "passed"
    assert one(a, sources={"ocr": GroundTruthReader({"l": "180.30"}), "api": api}, context=ctx).status == "failed"


# --------------------------------------------------------------------------------------------
# Unreadable values and the alternate source (2.8.1)


def test_low_confidence_money_cell_is_needs_human_after_the_alternate_also_fails():
    ocr = GroundTruthReader(CONFIRM, confidence={BP: 72}, crop_refs={BP: "sha256:crop-bp"})
    jab = GroundTruthReader({BP: "-15,025.00"}, source="jab", confidence=40)
    results = evaluate_assertions(S07, {"ocr": ocr, "jab": jab}, {}, FIXTURE, context=STOCK)
    r02 = next(r for r in results if r.assertion_id == "R02")
    assert r02.status == "unreadable" and r02.unreadable == (BP,)
    verdict = summarize(results)
    assert verdict.failed == () and verdict.unreadable == (BP,)
    raw = r02.raw_values()
    assert raw[f"candidate.{BP}.ocr"].startswith("'-15,025.00' (confidence 72); rejected: confidence 72 below")
    assert raw[f"candidate.{BP}.jab"].startswith("'-15,025.00' (confidence 40); rejected")
    assert raw[f"candidate.{BP}.crop"] == "sha256:crop-bp"
    c = classify(Facts(**verdict.facts_fields()))
    assert (c.row, c.outcome, c.reason) == ("9a", "needs_human", "oracle_unreadable") and BP in c.detail


def test_low_confidence_money_cell_passes_when_the_alternate_agrees():
    ocr = GroundTruthReader(CONFIRM, confidence={BP: 72})
    jab = GroundTruthReader({BP: "-15,025.00"}, source="jab")
    results = evaluate_assertions(S07, {"ocr": ocr, "jab": jab}, {}, FIXTURE, context=STOCK)
    assert all(r.status == "passed" for r in results)
    verdict = summarize(results)
    assert [fb.event() for fb in verdict.fallbacks] == [f"oracle_source_fallback:{BP}:ocr->jab"]
    assert classify(Facts(**verdict.facts_fields())).outcome == "identical"


def test_alternate_that_disagrees_is_the_value_compared():
    ocr = GroundTruthReader({"q": "100"}, confidence=50)
    jab = GroundTruthReader({"q": "108"}, source="jab")
    r = one({"id": "qty", "kind": "equals", "region": "q", "format": "int", "expected": 100},
            sources={"ocr": ocr, "jab": jab})
    assert r.status == "failed" and r.extracted == "108" and r.raw_values()["source"] == "jab"


def test_two_pass_digit_disagreement_without_alternate_is_unreadable():
    ocr = GroundTruthReader({"q": "100"}, second_pass={"q": "10"})
    r = one({"id": "qty", "kind": "equals", "region": "q", "format": "int", "expected": 100}, sources={"ocr": ocr})
    assert r.status == "unreadable" and "disagree on digit count" in r.detail


def test_text_floor_is_70():
    a = {"id": "side", "kind": "equals", "region": "s", "expected": "BUY"}
    assert one(a, sources={"ocr": GroundTruthReader({"s": "BUY"}, confidence=75)}).status == "passed"
    assert one(a, sources={"ocr": GroundTruthReader({"s": "BUY"}, confidence=65)}).status == "unreadable"


# --------------------------------------------------------------------------------------------
# Calibration panel and export headers (harness_error, never assertion_failed)


def test_calibration_miss_is_harness_error_for_ocr_assertions():
    wrong = dict(CONFIRM, **{"rgn.confirm.qty": "108"})               # would be assertion_failed if OCR were trusted
    results = evaluate_assertions(S07, {"ocr": GroundTruthReader(wrong)}, {}, FIXTURE, context=STOCK,
                                  ocr_calibrated=False)
    assert {r.status for r in results} == {"harness_error"} and {r.reason for r in results} == {"ocr_calibration"}
    verdict = summarize(results)
    assert verdict.failed == () and verdict.harness_reason == "ocr_calibration"
    c = classify(facts_for(results, harness_event_id="wd-0042"))
    assert (c.outcome, c.reason, c.detail) == ("harness_error", "ocr_calibration", "watchdog_event: wd-0042")
    with pytest.raises(ValueError, match="Watchdog"):
        verdict.facts_fields()


def test_calibration_miss_spares_jab_reads_unless_they_need_ocr():
    a = {"id": "qty", "kind": "equals", "source": "jab", "region": "q", "format": "int", "expected": 100}
    jab = GroundTruthReader({"q": "100"}, source="jab")
    assert one(a, sources={"ocr": GroundTruthReader({"q": "100"}), "jab": jab}, ocr_calibrated=False).status == "passed"
    hidden = GroundTruthReader({}, source="jab")
    r = one(a, sources={"ocr": GroundTruthReader({"q": "100"}), "jab": hidden}, ocr_calibrated=False)
    assert (r.status, r.reason) == ("harness_error", "ocr_calibration")


def test_export_header_mismatch_file_is_harness_error():
    srcs = {"ocr": GroundTruthReader({"rgn.ps.pl-total": "$1,073.75"}),
            "export": _export(CSV.replace("P/L Open", "P/L Day"))}
    r = one(EXPORT_EQ, sources=srcs)
    assert (r.status, r.reason) == ("harness_error", "export_header_mismatch")
    c = classify(facts_for([r], harness_event_id="wd-7"))
    assert (c.outcome, c.reason) == ("harness_error", "export_header_mismatch")


def test_copy_all_header_mismatch_falls_back_to_ocr():
    copy_all = _export(CSV.replace(",", "\t").replace("P/L Open", "PL"), kind="copy_all")
    a = {"id": "total", "kind": "equals", "source": "export", "region": "rgn.ps.total", "format": "money",
         "expected": 1150}
    r = one(a, sources={"ocr": GroundTruthReader({"rgn.ps.total": "1,150.00"}), "export": copy_all})
    assert r.status == "passed"
    assert [fb.event() for fb in r.fallbacks] == ["oracle_source_fallback:rgn.ps.total:export->ocr"]
    assert "header differs" in r.fallbacks[0].why


def test_export_that_produced_nothing_is_unreadable():
    srcs = {"ocr": GroundTruthReader({"rgn.ps.pl-total": "$1,073.75"}),
            "export": ExportSource(None, ("Symbol", "Qty", "P/L Open"), kind="file")}
    r = one(EXPORT_EQ, sources=srcs)
    assert r.status == "unreadable" and r.unreadable == ("export:P/L Open",)


# --------------------------------------------------------------------------------------------
# The paperMoney delay model (2.8.2)

CAPTURE = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)         # 15:00 America/New_York, session open
SERIES = {("AAPL", "last"): [(CAPTURE - timedelta(minutes=30), "179.90"), (CAPTURE - timedelta(minutes=20), "180.10"),
                             (CAPTURE - timedelta(minutes=5), "181.00")]}
API_A = {"id": "last_vs_api", "kind": "api_within", "region": "l", "api_field": "last"}


def api_case(screen: str, **ctx):
    context = EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=CAPTURE, **ctx)
    return one(API_A, sources={"ocr": GroundTruthReader({"l": screen}), "api": GroundTruthApi(SERIES)}, context=context)


def test_delay_model_compares_with_the_value_twenty_minutes_before_capture():
    assert delay_minutes(FIXTURE) == 20
    assert api_case("180.10").status == "passed"
    assert api_case("180.11").status == "passed"                       # within one tick
    late = api_case("181.00")                                          # the real-time value: not what paperMoney shows
    assert late.status == "failed" and "as of 2026-11-10T19:40:00+00:00" in late.detail


def test_delay_model_exact_after_the_session_closed_unless_on_the_24h_list():
    closed = CAPTURE - timedelta(minutes=21)
    assert api_case("180.10", session_close=closed).status == "passed"
    exact = api_case("180.11", session_close=closed)
    assert exact.status == "failed" and "exact" in exact.detail
    assert api_case("180.11", session_close=CAPTURE - timedelta(minutes=19)).status == "passed"   # closed too recently
    listed = frozenset({"AAPL", "SPY"})
    assert api_case("180.11", session_close=closed, trading_24h=listed).status == "passed"
    assert api_case("180.12", session_close=closed, trading_24h=listed).status == "failed"
    mode = api_comparison(EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=CAPTURE,
                                      session_close=closed, trading_24h=listed), FIXTURE)
    assert mode.mode == "tick" and "24-hour" in mode.why


def test_realtime_and_frozen_comparison_times():
    realtime = dict(FIXTURE, data_mode={"masked_live": {"delay_minutes": 20, "realtime": True}})
    assert delay_minutes(realtime) == 0
    ctx = EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=CAPTURE)
    assert api_comparison(ctx, realtime).as_of == CAPTURE
    frozen = api_comparison(EvalContext(data_mode="frozen", symbol="AAPL", capture_time=CAPTURE), FIXTURE)
    assert frozen.mode == "frozen" and frozen.as_of.isoformat() == "2026-11-09T15:30:00-05:00"


def test_api_failures_are_env_errors_and_leave_other_assertions_in_force():
    step = {"assertions": [API_A, {"id": "side", "kind": "equals", "region": "s", "expected": "BUY"},
                           dict(API_A, id="bid_vs_api", api_field="bid")]}
    ctx = EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=CAPTURE)
    ocr = GroundTruthReader({"l": "180.10", "s": "SELL"})
    pre = evaluate_assertions(step, {"ocr": ocr, "api": GroundTruthApi(SERIES)}, {}, FIXTURE, context=ctx,
                              api_preflight_ok=False)
    assert [(r.status, r.gate) for r in pre] == [("env_error", "preflight.N3"), ("failed", None),
                                                 ("env_error", "preflight.N3")]
    mid = evaluate_assertions(step, {"ocr": ocr, "api": GroundTruthApi(SERIES, available=False)}, {}, FIXTURE,
                              context=ctx)
    assert [(r.status, r.gate) for r in mid] == [("env_error", "connectivity"), ("failed", None),
                                                 ("env_error", "connectivity")]
    verdict = summarize(mid)
    assert verdict.failed == ("side",)
    c = classify(Facts(**verdict.facts_fields()))
    assert (c.outcome, c.gate, c.reason) == ("env_error", "connectivity", "gate")


# --------------------------------------------------------------------------------------------
# The wrong number, the classifier and the reporter


def test_wrong_number_order_confirmation_cost_is_assertion_failed():
    wrong = dict(CONFIRM, **{"rgn.confirm.cost": "15,250.00"})        # 100 x 150.25 is 15,025.00
    results = evaluate_assertions(S07, {"ocr": GroundTruthReader(wrong)}, {}, FIXTURE, context=STOCK)
    verdict = summarize(results)
    assert verdict.failed == ("cost_math",)
    c = classify(Facts(**verdict.facts_fields()))                     # pixel-identical to base: diff zero
    assert (c.row, c.outcome, c.reason, c.assertion_ids) == ("8", "assertion_failed", "assertion:cost_math",
                                                             ("cost_math",))
    assert "$15,025.00" in c.detail and "$15,250.00" in c.detail
    CheckpointOutcome(run_id="run-1", checkpoint_id=S07_ID, outcome=c.outcome, workspace_id="ws.tier-a.swing-trader",
                      persona="swing_trader", data_mode="frozen", reason=c.reason, detail=c.detail,
                      assertion_ids=list(c.assertion_ids))
    base = evaluate_assertions(S07, {"ocr": GroundTruthReader(CONFIRM)}, {}, FIXTURE, context=STOCK)
    cost = next(r for r in results if r.assertion_id == "cost_math")
    raw = cost.raw_values(next(r for r in base if r.assertion_id == "cost_math"))
    assert (raw["expected"], raw["read"], raw["source"]) == ("$15,025.00", "$15,250.00", "ocr")
    assert raw["candidate.rgn.confirm.cost.ocr"].startswith("'15,250.00'")
    assert raw["base.rgn.confirm.cost.ocr"].startswith("'15,025.00'")
    Finding("f-1", "run-1", "assertion_failed", "Assertion cost_math failed", "run-1", checkpoint_id=S07_ID,
            evidence={"raw_values": raw})


def test_swapped_price_digit_fails_price_first_and_r01_too():
    swapped = dict(CONFIRM, **{"rgn.confirm.price": "150.52 LMT"})     # mutant M05
    verdict = summarize(evaluate_assertions(S07, {"ocr": GroundTruthReader(swapped)}, {}, FIXTURE, context=STOCK))
    assert verdict.failed == ("price", "cost_math")
    assert classify(Facts(**verdict.facts_fields())).reason == "assertion:price"


def test_bp_effect_outside_bracket_is_r02_with_bracket_label():
    surcharge = dict(CONFIRM, **{BP: "-15,032.51"})
    results = evaluate_assertions(S07, {"ocr": GroundTruthReader(surcharge)}, {}, FIXTURE, context=STOCK)
    r02 = next(r for r in results if r.assertion_id == "R02")
    assert r02.status == "failed" and r02.label == "bracket check" and r02.raw_values()["label"] == "bracket check"
    assert summarize(results).failed == ("R02",)


def test_example_position_statement_r08_masked_live():
    cp = next(s for s in READ_BACK["steps"] if s["id"] == "cp-positions")
    ocr = GroundTruthReader({"rgn.ps.qty": "100", "rgn.ps.trade-price": "180.25", "rgn.ps.mark": "181.40",
                             "rgn.ps.pl-open": "+115.00", "rgn.ps.instrument": "AAPL"})
    results = evaluate_assertions(cp, {"ocr": ocr}, {}, FIXTURE, context=EvalContext(symbol="AAPL"))
    assert [r.status for r in results] == ["passed", "passed"]
    off = GroundTruthReader(dict(ocr.values, **{"rgn.ps.pl-open": "+115.02"}))
    assert summarize(evaluate_assertions(cp, {"ocr": off}, {}, FIXTURE, context=EvalContext(symbol="AAPL"))).failed == (
        "pl_math",)


# --------------------------------------------------------------------------------------------
# Validation


def test_assertion_validation():
    with pytest.raises(OracleConfigError, match="4.7 kinds"):
        Assertion.from_dict({"id": "x", "kind": "invariant", "region": "r"})
    with pytest.raises(OracleConfigError, match="flow assertion id"):
        Assertion.from_dict({"id": "app_alive", "kind": "present", "region": "r"})
    with pytest.raises(OracleConfigError, match="source api"):
        Assertion.from_dict({"id": "x", "kind": "equals", "source": "api", "region": "r", "expected": 1})
    with pytest.raises(OracleConfigError, match="needs a region"):
        Assertion.from_dict({"id": "x", "kind": "present"})
    with pytest.raises(OracleConfigError, match="needs expected or value_ref"):
        Assertion.from_dict({"id": "x", "kind": "equals", "region": "r"})
    with pytest.raises(OracleConfigError, match="value_ref"):
        one({"id": "x", "kind": "equals", "region": "r", "value_ref": "fixture.prices.nope.value"}, {"r": "1"})


def test_step_assertions_and_data_mode_from_step():
    s02 = next(s for s in STOCK_FLOW["steps"] if s["id"] == "s02")
    assert [a["id"] for a in step_assertions(s02)] == ["sym_header"]
    assert len(step_assertions(S07)) == 10
    results = evaluate_assertions(S07, {"ocr": GroundTruthReader(CONFIRM)}, {}, FIXTURE, context=STOCK)
    assert [r.status for r in results] == ["passed"] * 10                 # data_mode frozen read from the step
    assert next(r for r in results if r.assertion_id == "cost_math").rule.status == "passed"


def test_api_env_error_outranks_the_calibration_harness_fault():
    ctx = EvalContext(data_mode="masked_live", symbol="AAPL", capture_time=CAPTURE)
    r = one(API_A, sources={"ocr": GroundTruthReader({"l": "180.10"}), "api": GroundTruthApi(SERIES)}, context=ctx,
            ocr_calibrated=False, api_preflight_ok=False)
    assert (r.status, r.gate) == ("env_error", "preflight.N3")


def test_numeric_kinds_and_literals_are_validated():
    with pytest.raises(OracleConfigError, match="not numeric"):
        Assertion.from_dict({"id": "x", "kind": "within_tol", "region": "r", "expected": 1, "format": "text"})
    with pytest.raises(OracleConfigError, match="count needs"):
        Assertion.from_dict({"id": "x", "kind": "count", "region": "r", "expected": "many"})
    with pytest.raises(OracleConfigError, match="not a valid int"):
        one({"id": "x", "kind": "equals", "region": "r", "format": "int", "expected": "ten"}, {"r": "10"})


def test_noisy_ocr_with_a_clean_jab_value_passes_by_fallback():
    noisy = GroundTruthReader({"q": "100"}, noise=1.0, seed=3)
    assert noisy.read("q").text != "100" and noisy.read("q").confidence < 85
    jab = GroundTruthReader({"q": "100"}, source="jab")
    r = one({"id": "qty", "kind": "equals", "region": "q", "format": "int", "expected": 100},
            sources={"ocr": noisy, "jab": jab})
    assert r.status == "passed" and r.fallbacks[0].to_source == "jab"


def test_ground_truth_from_synth_drawn_text():
    from sentinel import synth
    screen = synth.render(synth.sample_layout(), (640, 400))
    ocr = GroundTruthReader({d.name: d.text for d in screen.components if d.text})
    step = {"data_mode": "frozen", "expect": [
        {"id": "qty", "kind": "equals", "region": "ticket.qty", "format": "int", "expected": 100},
        {"id": "cost_math", "kind": "rule", "rule_id": "R01",
         "inputs": {"qty": "ticket.qty", "price": "ticket.price", "multiplier": 1, "total": "ticket.cost"}},
        {"id": "R02", "kind": "rule", "rule_id": "R02", "inputs": {"bp_effect": "ticket.bp", "notional": "ticket.cost"}},
        {"id": "send", "kind": "present", "region": "ticket.send.label", "pattern": "Confirm and Send"}]}
    results = evaluate_assertions(step, {"ocr": ocr}, {}, FIXTURE, context=STOCK)
    assert [r.status for r in results] == ["passed"] * 4
