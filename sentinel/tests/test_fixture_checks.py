"""Stage 2 of the fixture reset flow around the account comparison, and its selectors (7.6, 7.9, 7.11, 4.9, 4.5)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from sentinel.orchestrator import fixture_checks as fc
from sentinel.orchestrator.ports import AccountState, ConnectivityObservation, DelayBadge
from sentinel.orchestrator.results import Outcome
from sentinel.orchestrator.rig import AccountState as BaseAccountState
from sentinel.pipeline.config import SentinelConfig
from sentinel.scope import PlannedCheckpoint

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
FIXTURE = yaml.safe_load((EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml").read_text())
NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone(timedelta(hours=-5)))
DELAYED = DelayBadge("Delayed 20 min", True, 20)
UP = ConnectivityObservation("Connected", True)


def accounts(**over: Any) -> tuple[AccountState, ...]:
    exp = fc.expectations(FIXTURE)
    out = []
    for aid, e in exp.items():
        a = AccountState(aid, e.role, dict(e.positions), 0, e.cash, buying_power=e.cash if e.role == "empty" else 90000.0,
                         alert_count=0)
        out.append(replace(a, **over.get(e.role, {})))
    return tuple(out)


class Quotes:
    def __init__(self, name: str, last: dict[str, float]):
        self.name, self._last = name, last

    def last_delayed(self, symbol: str) -> float | None:
        return self._last.get(symbol)


def good_reads(**over: Any) -> fc.Stage2Reads:
    dataset = fc.EpochRead(last=231.15, bid=231.10, ask=231.20, quote_time="15:30:00")
    base = fc.Stage2Reads(
        accounts=accounts(), ondemand_toolbar=False, delay_badge=DELAYED, connectivity=UP, reconnects_since_launch=0,
        quote_age_s=1210.0, prices=fc.recompute_prices(FIXTURE, Quotes("api", {"/MESZ26": 6325.10, "AAPL": 231.15}), NOW),
        symbols=tuple(fc.SymbolFacts(s["id"], True) for s in FIXTURE["symbols"]), pinned=dataset, dataset=dataset)
    return replace(base, **over)


# -- the fixture's accounts ---------------------------------------------------------------------------------
def test_expectations_carry_the_buying_power_rule():
    exp = fc.expectations(FIXTURE)
    golden, empty = exp["D-20481133"], exp["D-20481134"]
    assert golden.positions["/MESZ26"] == 1.0 and golden.buying_power_max is None and golden.buying_power_min == 0.0
    assert empty.positions == {} and empty.buying_power_max == 2750.0       # cash_floor 2500 + tolerance 250
    assert fc.data_delay(FIXTURE) == ("delayed_20", 20)


# -- delay badge and connectivity ---------------------------------------------------------------------------
def test_delay_badge_against_the_epoch():
    # 4.9, 7.9: realtime while the epoch pins delayed_20 is env_error (gate: fixture_verified, reason: delay_state).
    r = fc.check_delay_state(DelayBadge("Real time", False), "delayed_20")
    assert r.unreached == Outcome("env_error", "delay_state", "fixture_verified", r.unreached.detail)
    assert r.run_status == "complete_with_env_errors"
    assert fc.check_delay_state(DELAYED, "delayed_20").passed
    assert not fc.check_delay_state(DelayBadge("Delayed 15 min", True, 15), "delayed_20").passed
    assert fc.check_delay_state(DelayBadge(None, None), "delayed_20") is None      # connectivity owns it


@pytest.mark.parametrize("obs,reconnects,age,badge,ok", [
    (UP, 0, 19 * 60.0, DELAYED, True),
    (UP, 0, 22 * 60.0, DELAYED, False),                              # 22 min old on delayed data
    (ConnectivityObservation("Disconnected", False), 0, 30.0, DELAYED, False),
    (ConnectivityObservation(None, None), 0, 30.0, DELAYED, False),
    (ConnectivityObservation("Connected", True, backend_degraded=True), 0, 30.0, DELAYED, False),
    (UP, 1, 30.0, DELAYED, True),
    (UP, 2, 30.0, DELAYED, False),                                   # a reconnect loop
    (UP, 0, None, DELAYED, False),
    (UP, 0, 30.0, DelayBadge(None, None), False),
])
def test_connectivity_check(obs, reconnects, age, badge, ok):
    r = fc.check_connectivity(obs, reconnects_since_launch=reconnects, quote_age_s=age, badge=badge,
                              delay="delayed_20", delay_minutes=20)
    assert r.passed is ok
    if not ok:
        assert r.unreached == Outcome("env_error", "backend_degraded", "connectivity", r.unreached.detail)


def test_connectivity_real_time_freshness_and_the_nightly_maintenance_event():
    rt = dict(reconnects_since_launch=0, badge=DelayBadge("Real time", False), delay="realtime", delay_minutes=0)
    assert fc.check_connectivity(UP, quote_age_s=4.0, **rt).passed
    bad = fc.check_connectivity(UP, quote_age_s=6.0, **rt)
    assert not bad.passed
    ev = fc.connectivity_maintenance_event([("rig-02", bad), ("rig-01", bad), ("rig-03", fc.check_connectivity(
        UP, quote_age_s=4.0, **rt)), ("rig-01", bad)], night=date(2026, 11, 10), owner="tos-connectivity",
        opened_at="2026-11-10T23:00:00-05:00")
    assert ev.kind == "backend_degraded" and ev.ref == "connectivity/2026-11-10"
    assert ev.detail == "post-login connectivity failed on rig-01, rig-02"
    assert fc.connectivity_maintenance_event([], night=date(2026, 11, 10), owner="x", opened_at="2026-11-10") is None


def test_alert_book_drift_is_whole_shard():
    # 7.5 alerts row, 7.6 step 3: alerts present after the reset are fixture drift; the re-read must carry a count.
    first = accounts()
    assert fc.check_alerts(first, None, 0).passed
    one = fc.check_alerts(accounts(golden={"alert_count": 1}), None, 0)
    assert one.unreached == Outcome("env_error", "fixture_drift", "fixture_verified", one.unreached.detail)
    unread = fc.check_alerts(first, accounts(empty={"alert_count": None}), 0)
    assert not unread.passed and "not re-read" in unread.detail
    base = (BaseAccountState("E", "empty", {}, 0, 2500.0),)
    assert fc.check_alerts(base, base, 0).passed                   # the base read has no alert book


# -- prices -------------------------------------------------------------------------------------------------
def test_price_rules():
    assert fc.parse_price_rule("last_delayed - 1000, rounded to 0.25").apply(6325.10) == 5325.00
    assert fc.parse_price_rule("last_delayed x 0.65, rounded to 0.01").apply(231.15) == 150.25
    assert fc.parse_price_rule("last_delayed * 0.5, rounded to 0.05").apply(10.13) == 5.05
    for bad in ("last - 5, rounded to 1", "last_delayed - 5", "last_delayed - 5, rounded to 0"):
        with pytest.raises(ValueError):
            fc.parse_price_rule(bad)


def test_prices_recomputed_and_the_source_recorded():
    rc = fc.recompute_prices(FIXTURE, Quotes("api", {"/MESZ26": 6325.10, "AAPL": 231.15}), NOW)
    assert rc.values == {"mes_far_below": 5325.0, "aapl_far_below": 150.25} and not rc.stale
    assert set(rc.sources.values()) == {"api"} and fc.check_prices(rc).detail.startswith("fixture prices: api")
    ocr = fc.recompute_prices(FIXTURE, Quotes("app_quote", {"/MESZ26": 6400.0, "AAPL": 230.0}), NOW)
    assert set(ocr.sources.values()) == {"app_quote"}


def test_stale_prices_mark_only_order_bearing_checkpoints():
    # L-FIX-08: computed_at 30 h old and no source: prices_stale on order-bearing checkpoints; the run proceeds.
    late = NOW.replace(day=10, hour=14, minute=30)                   # 2026-11-09T08:30-05:00 + 30 h
    rc = fc.recompute_prices(FIXTURE, None, late)
    assert rc.stale == ("mes_far_below", "aapl_far_below") and set(rc.sources.values()) == {"fixture"}
    r = fc.check_prices(rc)
    assert r.run is None and r.unreached is None
    assert [(s.selector, s.outcome) for s in r.outcomes] == [
        ("order_bearing", Outcome("env_error", "prices_stale", "fixture_verified"))]
    fresh = fc.recompute_prices(FIXTURE, Quotes("api", {}), NOW)      # 21.5 h old and unreadable: kept, not stale
    assert not fresh.stale and fc.check_prices(fresh).passed


# -- pinned quote and symbols -------------------------------------------------------------------------------
def test_pinned_quote_off_by_one_tick_is_data_epoch_mismatch_on_frozen_only():
    ds = fc.EpochRead(last=231.15, bid=231.10, ask=231.20, quote_time="15:30:00", expirations=("2026-11-20",))
    r = fc.check_pinned_quote(replace(ds, bid=231.09), ds)
    assert [(s.selector, s.outcome) for s in r.outcomes] == [
        ("frozen", Outcome("env_error", "data_epoch_mismatch", "fixture_verified"))]
    assert r.run is None and "bid" in r.detail
    assert fc.check_pinned_quote(ds, ds).passed
    assert fc.check_pinned_quote(replace(ds, chart_last_bar=1.0), ds).passed      # the dataset names no last bar
    assert not fc.check_pinned_quote(replace(ds, expirations=None), ds).passed


def test_symbol_precheck_scopes_failures_to_the_symbol():
    ids = ["aapl", "spx", "mes_front"]
    facts = (fc.SymbolFacts("aapl", True, changed=True), fc.SymbolFacts("spx", True),
             fc.SymbolFacts("mes_front", True, expirations_ok=False))
    r = fc.check_symbols(facts, ids, source="api")
    assert [s.selector for s in r.outcomes] == ["symbol:aapl", "symbol:mes_front"]
    assert r.outcomes[0].outcome == Outcome("env_error", "fixture_maintenance_needed", "fixture_verified")
    assert set(r.alert.notified) >= {"flow_owner", "accounts_owner"} and r.run is None
    ok = fc.check_symbols((fc.SymbolFacts("aapl", True, expirations_ok=None),), ["aapl"], source="app_quote")
    assert ok.passed and ok.detail == "symbol pre-check: app quote only"
    missing = fc.check_symbols((), ["aapl"], source="api")
    assert missing.outcomes[0].selector == "symbol:aapl" and "not checked" in missing.detail
    with pytest.raises(ValueError):
        fc.check_symbols((), [], source="ocr")


# -- stage 2 in order ---------------------------------------------------------------------------------------
def test_stage2_all_good():
    rs = fc.check_stage2(good_reads(), FIXTURE, reseed_due_passed=False)
    assert all(r.passed for r in rs)
    assert [r.control for r in rs] == ["fixture", "ondemand", "delay_badge", "connectivity", "alerts", "prices",
                                       "pinned_quote", "symbol_precheck"]


def test_stage2_stops_at_the_first_run_outcome_or_action():
    abort = fc.check_stage2(good_reads(accounts=accounts(empty={"working_orders": 1})), FIXTURE,
                            reseed_due_passed=False)
    assert len(abort) == 1 and abort[0].run_reason == "unexpected_working_order"
    reset = fc.check_stage2(good_reads(accounts=accounts(empty={"cash": 100000.0, "buying_power": 100000.0})),
                            FIXTURE, reseed_due_passed=False)
    assert len(reset) == 1 and reset[0].has("reset_empty_account")
    od = fc.check_stage2(good_reads(ondemand_toolbar=True), FIXTURE, reseed_due_passed=False)
    assert od[-1].unreached.reason == "ondemand_state" and len(od) == 2
    rt = fc.check_stage2(good_reads(delay_badge=DelayBadge("Real time", False)), FIXTURE, reseed_due_passed=False)
    assert rt[-1].unreached.reason == "delay_state" and "prices" not in [r.control for r in rt]


def test_stage2_subsets_proceed_together():
    golden_drift = accounts(golden={"cash": 1000.0})
    stale = fc.recompute_prices(FIXTURE, None, NOW + timedelta(days=2))
    rs = fc.check_stage2(good_reads(accounts=golden_drift, prices=stale), FIXTURE, reseed_due_passed=False)
    subs = {s.selector: s.outcome.reason for r in rs for s in r.outcomes}
    assert subs == {"account_state:golden": "fixture_drift", "order_bearing": "prices_stale"}
    assert all(r.run is None for r in rs)
    reseed = fc.check_stage2(good_reads(), FIXTURE, reseed_due_passed=True)
    assert reseed[0].outcomes[0].outcome.reason == "fixture_maintenance_needed"
    with pytest.raises(ValueError):                                  # frozen Branch A needs the pinned quote
        fc.check_stage2(good_reads(pinned=None), FIXTURE, reseed_due_passed=False)


# -- selectors ----------------------------------------------------------------------------------------------
def cp(cid: str, kind: str, ws: str, flow: str | None = None, step: str | None = None, mode: str = "frozen"):
    return PlannedCheckpoint(cid, cid.split("/")[1], kind, 1, ws, "active_trader", mode, flow, step)


LADDER = cp("flow/trade.active-trader.ladder/flow.trade.active-trader.read-back/cp-ladder", "flow",
            "ws.tier-a.active-trader", "flow.trade.active-trader.read-back", "cp-ladder")
S06 = cp("flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06", "flow", "ws.tier-a.swing-trader",
         "flow.trade.order-ticket.stock.limit", "s06")
S07 = cp("flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07", "flow", "ws.tier-a.swing-trader",
         "flow.trade.order-ticket.stock.limit", "s07")
POS = cp("census/monitor.activity.position-statement/ws.tier-a.active-trader", "census", "ws.tier-a.active-trader")
CHART = cp("census/charts.flexible-grid.chart-cell/ws.tier-a.options-trader", "census", "ws.tier-a.options-trader")
SCOPE = [LADDER, S06, S07, POS, CHART]


@pytest.fixture(scope="module")
def cfg() -> SentinelConfig:
    return SentinelConfig.load(EXAMPLE)


def test_select_account_state_and_golden(cfg):
    # 4.9, 7.6 steps 4-5: account_state checkpoints; golden drift only reaches those on the golden account.
    assert fc.select_subset("account_state", SCOPE, cfg) == [LADDER, S07, POS]
    assert fc.select_subset("account_state:golden", SCOPE, cfg) == [POS]


def test_select_order_bearing_and_symbol(cfg):
    assert fc.select_subset("order_bearing", SCOPE, cfg) == [S06, S07]
    assert fc.select_subset("symbol:mes_front", SCOPE, cfg) == [LADDER, POS]
    assert fc.select_subset("symbol:spx", SCOPE, cfg) == [CHART]
    assert fc.select_subset("symbol:aapl", SCOPE, cfg) == [LADDER, S06, S07, POS]
    assert fc.handles("symbol:x") and not fc.handles("frozen")
    with pytest.raises(ValueError):
        fc.select_subset("frozen", SCOPE, cfg)


@dataclass
class _Doc:
    data: dict


@dataclass
class _Repo:
    docs: list[_Doc] = field(default_factory=list)

    def of(self, kind: str) -> list[_Doc]:
        return self.docs if kind == "workspace" else []


@dataclass
class _Cfg:
    surfaces: dict
    flow_docs: dict
    fixture: dict = field(default_factory=dict)
    repo: _Repo = field(default_factory=_Repo)


def test_step_override_and_flow_account_role():
    # 4.7 checkpoint_step account_state overrides the surface flag; a golden flow's checkpoints are golden (SD-75).
    flow = {"account_role": "golden", "steps": [{"id": "a", "type": "checkpoint", "account_state": False},
                                                {"id": "b", "type": "checkpoint"}]}
    c = _Cfg({"s.x": {"account_state": True}}, {"flow.f": flow},
             repo=_Repo([_Doc({"id": "ws.e", "account_role": "empty"})]))
    a = PlannedCheckpoint("flow/s.x/flow.f/a", "s.x", "flow", 1, "ws.e", "p", "frozen", "flow.f", "a")
    b = PlannedCheckpoint("flow/s.x/flow.f/b", "s.x", "flow", 1, "ws.e", "p", "frozen", "flow.f", "b")
    census = PlannedCheckpoint("census/s.x/ws.e", "s.x", "census", 1, "ws.e", "p", "frozen")
    assert fc.select_subset("account_state", [a, b, census], c) == [b, census]
    assert fc.select_subset("account_state:golden", [a, b, census], c) == [b]
