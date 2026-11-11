"""The churn calendar at run time and its generator (4.9 churn_calendar, 7.6 stage 2 step 4, 7.9)."""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel import churn
from sentinel.orchestrator.gates import check_fixture
from sentinel.orchestrator.fixture_checks import expectations
from sentinel.orchestrator.results import Outcome
from sentinel.orchestrator.rig import AccountState
from sentinel.pipeline.config import SentinelConfig
from sentinel.scope import PlannedCheckpoint

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "example"
FIXTURE = yaml.safe_load((EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml").read_text())


def cp(cid: str, kind: str, ws: str, mode: str, flow: str | None = None, step: str | None = None):
    return PlannedCheckpoint(cid, cid.split("/")[1], kind, 1, ws, "active_trader", mode, flow, step)


LADDER = cp("flow/trade.active-trader.ladder/flow.trade.active-trader.read-back/cp-ladder", "flow",
            "ws.tier-a.active-trader", "frozen", "flow.trade.active-trader.read-back", "cp-ladder")
POS_LIVE = cp("census/monitor.activity.position-statement/ws.tier-a.active-trader", "census",
              "ws.tier-a.active-trader", "masked_live")
CHART = cp("census/charts.flexible-grid.chart-cell/ws.tier-a.options-trader", "census", "ws.tier-a.options-trader",
           "frozen")
SCOPE = [LADDER, POS_LIVE, CHART]


@pytest.fixture(scope="module")
def cfg() -> SentinelConfig:
    return SentinelConfig.load(EXAMPLE)


def test_events_for_the_run_date():
    (hol,) = churn.events_for(date(2026, 11, 26), FIXTURE)
    assert (hol.event, hol.action, hol.symbol_ref, hol.detail) == ("holiday", "skip_checkpoints", None,
                                                                    "holiday 2026-11-26")
    assert churn.events_for(date(2026, 11, 27), FIXTURE) == ()


def test_holiday_skips_the_affected_checkpoints_with_the_event_in_detail(cfg):
    # 4.9: skip_checkpoints makes the affected checkpoints not_run (reason: scope) with the event in detail.
    out = churn.skip_outcomes(date(2026, 11, 26), FIXTURE, SCOPE, cfg)
    assert out == {POS_LIVE.checkpoint_id: Outcome("not_run", "scope", detail="holiday 2026-11-26")}
    assert churn.skip_outcomes(date(2026, 11, 25), FIXTURE, SCOPE, cfg) == {}
    symbol_skip = {**FIXTURE, "churn_calendar": [{"date": date(2026, 11, 25), "event": "earnings",
                                                  "symbol_ref": "spx", "action": "skip_checkpoints"}]}
    assert churn.skip_outcomes(date(2026, 11, 25), symbol_skip, SCOPE, cfg) == {
        CHART.checkpoint_id: Outcome("not_run", "scope", detail="earnings 2026-11-25 spx")}


def test_reapprove_marks_frozen_baselines_instead_of_scoring(cfg):
    # 4.9, 7.9: a roll date marks the frozen baselines that show the rolled symbol for re-approval.
    assert churn.reapprove(date(2026, 12, 11), FIXTURE, SCOPE, cfg) == {LADDER.checkpoint_id: "roll 2026-12-11 mes_front"}
    assert churn.reapprove(date(2026, 11, 20), FIXTURE, SCOPE, cfg) == {CHART.checkpoint_id: "expiration 2026-11-20 spx"}
    no_symbol = {**FIXTURE, "churn_calendar": [{"date": date(2026, 11, 2), "event": "holiday", "action": "reapprove"}]}
    assert set(churn.reapprove(date(2026, 11, 2), no_symbol, SCOPE, cfg)) == {LADDER.checkpoint_id,
                                                                               CHART.checkpoint_id}


def test_reseed_due_passed_makes_golden_account_state_maintenance_needed():
    # 7.6 step 4, SD-40: run date 2026-12-19 with reseed_due 2026-12-18 is fixture_maintenance_needed.
    assert not churn.reseed_due_passed(date(2026, 12, 18), FIXTURE)
    assert churn.reseed_due_passed(date(2026, 12, 19), FIXTURE)
    exp = expectations(FIXTURE)
    reads = tuple(AccountState(aid, e.role, dict(e.positions), 0, e.cash) for aid, e in exp.items())
    r = check_fixture(reads, exp, reseed_due_passed=churn.reseed_due_passed(date(2026, 12, 19), FIXTURE))
    assert [(s.selector, s.outcome.reason) for s in r.outcomes] == [("account_state:golden",
                                                                     "fixture_maintenance_needed")]


def test_reseed_is_a_calendar_task_for_the_fixture_owner():
    (task,) = churn.reseed_tasks(FIXTURE, opened_at="2026-11-09T08:00:00-05:00")
    assert (task.kind, task.owner, task.ref) == ("reseed", "asmith", "fx.rig-01.paper/2026-12-18")


def test_generator_reproduces_the_example_calendar():
    # 7.9: generated from the exchange calendar (explicit holidays for now) and the fixture.
    events = churn.generate(FIXTURE, start=date(2026, 11, 9), holidays=[date(2026, 11, 26), date(2026, 12, 25)])
    got = {(e.date, e.event, e.symbol_ref, e.action) for e in events}
    want = {(e.date, e.event, e.symbol_ref, e.action) for e in churn.calendar(FIXTURE)}
    assert want <= got
    assert (date(2026, 12, 18), "expiration", "aapl_c200", "reapprove") in got
    assert all(date(2026, 11, 9) <= e.date <= date(2027, 2, 7) for e in events)
    assert [e for e in events if e.event == "reseed_due"] == [churn.ChurnEvent(date(2026, 12, 18), "reseed_due",
                                                                               "reseed")]
    extra = churn.generate(FIXTURE, start=date(2026, 11, 9), earnings={"aapl": [date(2027, 1, 28)]},
                           ex_dividend={"aapl": [date(2026, 11, 10)]})
    kinds = {(e.event, e.action) for e in extra}
    assert ("earnings", "none") in kinds and ("ex_dividend", "reapprove") in kinds


def test_expiration_dates():
    assert churn.third_friday(2026, 11) == date(2026, 11, 20)
    assert churn.monthly_expirations(date(2026, 11, 1), date(2026, 12, 31), [date(2026, 11, 20)]) == [
        date(2026, 11, 19), date(2026, 12, 18)]
    assert churn.future_expiration("/MESZ26") == date(2026, 12, 18)
    assert churn.future_expiration("/ES") is None


def _tool():
    spec = importlib.util.spec_from_file_location("gen_churn_calendar", ROOT / "tools" / "gen_churn_calendar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_prints_a_checkable_calendar(capsys):
    tool = _tool()
    assert tool.main([str(EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml"), "--start", "2026-11-09",
                      "--holiday", "2026-11-26", "--earnings", "aapl=2027-01-28"]) == 0
    doc = yaml.safe_load(capsys.readouterr().out)
    entries = doc["churn_calendar"]
    assert {"date": date(2026, 11, 26), "event": "holiday", "action": "skip_checkpoints"} in entries
    assert {"date": date(2026, 12, 11), "event": "roll", "symbol_ref": "mes_front", "action": "reapprove"} in entries
    with pytest.raises(SystemExit):
        tool.main([str(EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml"), "--start", "2026-11-09", "--earnings", "x"])
