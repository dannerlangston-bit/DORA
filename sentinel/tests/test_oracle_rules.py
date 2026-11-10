"""The arithmetic invariants R01 to R10 (2.8.3), each with a pass and a fail case, over stored values
(`check_rules`, the offline re-run of 2.8.1)."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from sentinel.lint.rules import RULE_OPERANDS
from sentinel.oracle.rules import RULE_IDS, RuleSpec, check_rules, rebind
from sentinel.oracle.sources import GroundTruthReader, extract
from sentinel.oracle.tables import (EvalContext, MarginRules, OracleConfigError, RuleConfig, Tolerance,
                                    multiplier_for, symbol_root, tick_size)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = yaml.safe_load((ROOT / "config/example/fixtures/fx.rig-01.paper.yaml").read_text())
FROZEN = EvalContext(data_mode="frozen", symbol="AAPL", opening=True)


def run(rule_id: str, values: dict, inputs: dict, context: EvalContext = FROZEN, config: RuleConfig | None = None,
        **extra):
    spec = {"id": f"t_{rule_id.lower()}", "kind": "rule", "rule_id": rule_id, "inputs": inputs, **extra}
    [result] = check_rules(values, FIXTURE, [spec], config=config or RuleConfig(), context=context)
    return result


R01_IN = {"qty": "q", "price": "p", "multiplier": 1, "total": "c"}


def test_rule_ids_match_the_lint_operand_table():
    assert set(RULE_IDS) == set(RULE_OPERANDS)
    with pytest.raises(OracleConfigError, match="L-FLW-11"):
        RuleSpec.from_assertion({"id": "x", "rule_id": "R01", "inputs": {"qty": "q", "price": "p"}})
    with pytest.raises(OracleConfigError, match="R01 to R10"):
        RuleSpec.from_assertion({"id": "x", "rule_id": "R11", "inputs": {}})


def test_r01_pass_and_wrong_cost():
    ok = run("R01", {"q": "100", "p": "150.25 LMT", "c": "15,025.00"}, R01_IN, tol=0.01)
    assert ok.status == "passed" and ok.expected == "$15,025.00"
    bad = run("R01", {"q": "100", "p": "150.25 LMT", "c": "15,250.00"}, R01_IN)
    assert bad.status == "failed" and (bad.expected, bad.extracted) == ("$15,025.00", "$15,250.00")
    assert "100 x 150.25 x 1 = $15,025.00" in bad.detail


def test_r01_futures_multiplier_per_leg_and_masked_live_tolerance():
    ctx = EvalContext(data_mode="masked_live", symbol="/MESZ26")
    inputs = {"qty": "q", "price": "p", "multiplier": 5, "total": "c"}
    assert run("R01", {"q": "1", "p": "5,325.00", "c": "26,625.00"}, inputs, ctx).status == "passed"
    # masked_live money tolerance: 0.01 plus one tick (0.25 x 1 x 5 = 1.25) per leg
    assert run("R01", {"q": "1", "p": "5,325.00", "c": "26,626.26"}, inputs, ctx).status == "passed"
    assert run("R01", {"q": "1", "p": "5,325.00", "c": "26,626.27"}, inputs, ctx).status == "failed"
    # frozen: exact after rounding to the cent
    frozen = EvalContext(data_mode="frozen")
    assert run("R01", {"q": "1", "p": "5,325.00", "c": "26,625.01"}, inputs, frozen).status == "failed"
    spread = {"q": "+1\n-1", "p": "12.40\n8.10", "c": "1,240.00\n-810.00\n430.00"}
    opt = {"qty": "q", "price": "p", "multiplier": 100, "total": "c"}
    assert run("R01", spread, opt).status == "passed"
    leg_wrong = run("R01", dict(spread, c="1,240.00\n-801.00\n430.00"), opt)
    assert leg_wrong.status == "failed" and "leg 2" in leg_wrong.detail


def test_r01_placeholder_cost_fails_not_unreadable():
    r = run("R01", {"q": "100", "p": "150.25", "c": "--"}, R01_IN)
    assert r.status == "failed" and "not a valid money" in r.detail


R02_IN = {"bp_effect": "bp", "notional": "n"}


def test_r02_stock_margin_bracket_pass_and_fail():
    ok = run("R02", {"bp": "-15,025.00", "n": "15,025.00"}, R02_IN)
    assert ok.status == "passed" and ok.label == "bracket check"
    half = run("R02", {"bp": "-7,512.50", "n": "15,025.00"}, R02_IN)
    assert half.status == "passed"
    over = run("R02", {"bp": "-15,032.51", "n": "15,025.00"}, R02_IN)
    assert over.status == "failed" and "outside the equity margin bracket $7,512.50 to $15,025.00" in over.detail
    under = run("R02", {"bp": "-7,000.00", "n": "15,025.00"}, R02_IN)
    assert under.status == "failed"


def test_r02_sign_ira_and_closing():
    wrong_sign = run("R02", {"bp": "15,025.00", "n": "15,025.00"}, R02_IN)
    assert wrong_sign.status == "failed" and "not negative for an opening order" in wrong_sign.detail
    closing = EvalContext(data_mode="frozen", symbol="AAPL", opening=False)
    assert run("R02", {"bp": "+15,025.00", "n": "15,025.00"}, R02_IN, closing).status == "passed"
    ira = EvalContext(data_mode="frozen", symbol="AAPL", opening=True, account_kind="ira")
    assert run("R02", {"bp": "-15,025.00", "n": "15,025.00"}, R02_IN, ira).status == "passed"
    assert run("R02", {"bp": "-7,512.50", "n": "15,025.00"}, R02_IN, ira).status == "failed"
    with pytest.raises(OracleConfigError, match="opening or closing"):
        run("R02", {"bp": "-1.00", "n": "1.00"}, R02_IN, EvalContext(data_mode="frozen", symbol="AAPL"))


def test_r02_futures_from_margin_rules_file(tmp_path):
    path = tmp_path / "margin_rules.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "pinned": True, "brackets": [
        {"asset": "future", "account": "margin", "of": "initial_margin", "low": 1.0, "high": 1.0},
        {"asset": "equity", "account": "margin", "of": "notional", "low": 0.5, "high": 0.5}],
        "initial_margin": {"MES": 2464.00}}))
    config = RuleConfig.load(path)
    ctx = EvalContext(data_mode="frozen", symbol="/MESZ26", opening=True, quantity=Decimal(1))
    ok = run("R02", {"bp": "-2,464.00", "n": "26,625.00"}, R02_IN, ctx, config)
    assert ok.status == "passed" and ok.label is None                  # pinned: no "bracket check" label
    assert run("R02", {"bp": "-2,465.00", "n": "26,625.00"}, R02_IN, ctx, config).status == "failed"
    assert run("R02", {"bp": "-7,512.50", "n": "15,025.00"}, R02_IN, FROZEN, config).status == "passed"
    with pytest.raises(OracleConfigError, match="initial margin"):
        run("R02", {"bp": "-1.00", "n": "1.00"}, R02_IN, ctx)          # the default table has no initial margins


def test_margin_rules_validation():
    with pytest.raises(OracleConfigError, match="low == high"):
        MarginRules.from_dict({"schema_version": 1, "pinned": True, "brackets": [
            {"asset": "equity", "account": "margin", "of": "notional", "low": 0.5, "high": 1.0}]})
    with pytest.raises(OracleConfigError, match="schema_version"):
        MarginRules.from_dict({"brackets": []})
    with pytest.raises(OracleConfigError, match="no R02 bracket"):
        MarginRules.from_dict({"schema_version": 1, "brackets": []}).bracket("equity", "margin")
    with pytest.raises(OracleConfigError, match="enums"):
        MarginRules.from_dict({"schema_version": 1, "brackets": [
            {"asset": "crypto", "account": "margin", "of": "notional", "low": 1, "high": 1}]})


def test_r03_quotes_and_session_range():
    inputs = {"bid": "b", "mark": "m", "ask": "a"}
    rows3 = {"b": "180.10\n12.30", "m": "180.15\n12.40", "a": "180.20\n12.50"}
    assert run("R03", rows3, inputs).status == "passed"
    crossed = run("R03", dict(rows3, m="180.15\n12.60"), inputs)
    assert crossed.status == "failed" and "row 2" in crossed.detail
    full = dict(inputs, last="l", low="lo", high="hi")
    session = dict(rows3, l="180.12\n12.45", lo="179.00\n12.00", hi="181.00\n13.00")
    assert run("R03", session, full).status == "passed"
    assert run("R03", dict(session, l="182.00\n12.45"), full).status == "failed"
    with pytest.raises(OracleConfigError, match="go together"):
        run("R03", dict(rows3, l="180.12\n12.45"), dict(inputs, last="l"))


def test_r04_strike_count_all_and_expiration_list():
    inputs = {"count": "rows", "selector": "sel"}
    strikes = "\n".join(str(s) for s in range(190, 200))
    assert run("R04", {"rows": strikes, "sel": "10"}, inputs).status == "passed"
    missing = run("R04", {"rows": "\n".join(str(s) for s in range(190, 199)), "sel": "10"}, inputs)
    assert missing.status == "failed" and (missing.expected, missing.extracted) == ("10 strikes", "9 strikes")
    all_ctx = EvalContext(data_mode="frozen", all_strikes=10)
    assert run("R04", {"rows": strikes, "sel": "All"}, inputs, all_ctx).status == "passed"
    with pytest.raises(OracleConfigError, match="All"):
        run("R04", {"rows": strikes, "sel": "All"}, inputs)
    header = "20 NOV 26\n18 DEC 26"
    assert run("R04", {"rows": "20 NOV 26\n18 DEC 26", "sel": header}, inputs).status == "passed"
    moved = run("R04", {"rows": "21 NOV 26\n18 DEC 26", "sel": header}, inputs)
    assert moved.status == "failed" and "expirations differ" in moved.detail


def test_r05_greeks():
    inputs = {"delta": "d", "gamma": "g", "vega": "v", "theta": "t"}
    good = {"d": "0.85\n0.62\n0.40\n0.21", "g": "0.010\n0.020\n0.021\n0.015", "v": "0.10\n0.15\n0.16\n0.12",
            "t": "-0.05\n-0.08\n-0.07\n-0.04"}
    assert run("R05", good, inputs).status == "passed"
    assert run("R05", dict(good, d="-0.21\n-0.40\n-0.62\n-0.85"), inputs).status == "passed"   # puts, monotone
    assert run("R05", dict(good, d="0.85\n0.62\n0.63\n0.21"), inputs).status == "passed"      # within 0.01
    assert run("R05", dict(good, d="0.85\n0.62\n0.70\n0.21"), inputs).status == "failed"
    assert run("R05", dict(good, d="1.02\n0.62\n0.40\n0.21"), inputs).status == "failed"
    assert run("R05", dict(good, g="0.010\n-0.020\n0.021\n0.015"), inputs).status == "failed"
    positive_theta = dict(good, t="-0.05\n0.08\n-0.07\n-0.04")
    assert run("R05", positive_theta, inputs).status == "failed"
    short = EvalContext(data_mode="frozen", long_options=False)
    assert run("R05", positive_theta, inputs, short).status == "passed"


def test_r06_parity_european_only_for_spx():
    inputs = {"call": "c", "put": "p", "strike": "k", "underlying": "s", "rate": 0.05, "dte": 30}
    spx = EvalContext(data_mode="frozen", symbol=".SPXW261120C6000")
    # S - K e^(-rT) = 6000 - 6000 e^(-0.05 x 30/365) = 24.61
    parity = {"c": "100.00", "p": "75.39", "k": "6,000.00", "s": "6,000.00"}
    assert run("R06", parity, inputs, spx).status == "passed"
    broken = run("R06", dict(parity, p="70.00"), inputs, spx)
    assert broken.status == "failed" and "European" in broken.detail
    # an American option inside the bounds S - K <= C - P <= S - K e^(-rT) passes although parity's equality fails
    aapl = EvalContext(data_mode="frozen", symbol=".AAPL261218C200")
    american = {"c": "12.40", "p": "2.30", "k": "200.00", "s": "210.00"}       # C - P = 10.10 in [10.00, 10.82]
    assert run("R06", american, inputs, aapl).status == "passed"
    assert run("R06", american, inputs, EvalContext(data_mode="frozen", symbol=".SPXW261218C200")).status == "failed"
    outside = run("R06", dict(american, p="4.00"), inputs, aapl)                 # C - P = 8.40 < 10.00
    assert outside.status == "failed" and "American bounds" in outside.detail


def test_r07_dte():
    ctx = EvalContext(data_mode="frozen", capture_time=datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc))
    inputs = {"dte": "d", "expiration": "e"}
    # 03:30 UTC on Nov 10 is still Nov 9 in New York
    assert run("R07", {"d": "(11)\n(39)", "e": "20 NOV 26\n18 DEC 26"}, inputs, ctx).status == "passed"
    off = run("R07", {"d": "(10)\n(39)", "e": "20 NOV 26\n18 DEC 26"}, inputs, ctx)
    assert off.status == "failed" and (off.expected, off.extracted) == ("11", "10")


def test_r08_position_pl_rows_and_total():
    inputs = {"pl_open": "pl", "mark": "m", "trade_price": "tp", "qty": "q", "multiplier": "inst"}
    values = {"inst": "AAPL\n/MESZ26\n.AAPL261218C200", "q": "100\n1\n1", "tp": "180.25\n6,210.50\n12.40",
              "m": "181.40\n6,195.25\n11.95", "pl": "+115.00\n-76.25\n-45.00\n-6.25"}
    assert run("R08", values, inputs).status == "passed"
    wrong_row = run("R08", dict(values, pl="+115.00\n-76.52\n-45.00\n-6.52"), inputs)
    assert wrong_row.status == "failed" and "row 2" in wrong_row.detail and wrong_row.expected == "-$76.25"
    wrong_total = run("R08", dict(values, pl="+115.00\n-76.25\n-45.00\n-16.25"), inputs)
    assert wrong_total.status == "failed" and "not the sum of the rows" in wrong_total.detail
    literal_mult = {"pl_open": "pl", "mark": "m", "trade_price": "tp", "qty": "q", "multiplier": 1}
    one_row = {"pl": "+115.00", "m": "181.40", "tp": "180.25", "q": "100"}
    assert run("R08", one_row, literal_mult, tol=0.01).status == "passed"


def test_r09_export_total():
    inputs = {"export_total": "x", "screen_total": "s"}
    assert run("R09", {"x": "1,234.56", "s": "$1,234.56"}, inputs).status == "passed"
    assert run("R09", {"x": "1,234.65", "s": "$1,234.56"}, inputs).status == "failed"


def test_r10_number_format():
    assert run("R10", {"v": "1,234.56\n-12.00"}, {"value": "v"}, format="money").status == "passed"
    for text in ("NaN", "Infinity", "null", "--5.00", "1,23,4.00"):
        r = run("R10", {"v": f"1.00\n{text}"}, {"value": "v"}, format="money")
        assert r.status == "failed", text
    with pytest.raises(OracleConfigError, match="format"):
        run("R10", {"v": "1.00"}, {"value": "v"})


def test_unreadable_operand_and_rebind():
    low = extract("c", "money", {"ocr": GroundTruthReader({"c": "15,025.00"}, confidence=80)})
    assert not low.readable                                             # 80 is below the money floor
    r = run("R01", {"q": "100", "p": "150.25", "c": low}, R01_IN)
    assert r.status == "unreadable" and r.unreadable == ("c",)
    as_text = extract("c", "text", {"ocr": GroundTruthReader({"c": "15,025.00"}, confidence=80)})
    assert as_text.readable and not rebind(as_text, "money").readable  # re-judged under the money floor
    with pytest.raises(OracleConfigError, match="no stored value"):
        run("R01", {"q": "100", "p": "150.25"}, R01_IN)


def test_tables():
    config = RuleConfig()
    assert multiplier_for("/MESZ26", config) == 5 and multiplier_for("/ES", config) == 50
    assert multiplier_for("/NQH27", config) == 20 and multiplier_for("/MNQZ26", config) == 2
    assert multiplier_for(".SPXW261120C6000", config) == 100 and multiplier_for("AAPL", config) == 1
    assert multiplier_for("EUR/USD", config) == 1
    with pytest.raises(OracleConfigError):
        multiplier_for("/ZBZ26", config)
    assert symbol_root("/MESZ26") == "MES" and symbol_root(".AAPL261218C200") == "AAPL"
    assert tick_size("/MESZ26", config) == Decimal("0.25") and tick_size("AAPL", config) == Decimal("0.01")
    assert tick_size("CEI", config, FIXTURE, Decimal("0.58")) == Decimal("0.0001")
    assert Tolerance.parse("0.5%").bound(Decimal(200)) == 1
    assert Tolerance.parse(0.01).bound(Decimal(5)) == Decimal("0.01")
    with pytest.raises(OracleConfigError):
        Tolerance.parse(True)
