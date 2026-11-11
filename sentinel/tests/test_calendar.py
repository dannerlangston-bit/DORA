"""The exchange calendar, windows, operating day, windowed scheduler, G4, maintenance reschedule, lockstep pause and
DST (sentinel.calendar; 7.10, 7.11, 10.7, 11.1 PA-12, 11.2, 12.7 G4; SD-67, SD-76)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
import yaml

from sentinel import calendar as cal
from sentinel.calendar import WindowedItem, WindowsConfig, WindowsConfigError
from sentinel.orchestrator.gates import check_provisioned
from sentinel.orchestrator.rig import PreflightReport

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
NY = ZoneInfo("America/New_York")
ITEMS = ([f"R{i}" for i in range(1, 8)] + [f"B{i}" for i in range(1, 4)] + [f"H{i}" for i in range(1, 11)]
         + [f"N{i}" for i in range(1, 8)] + [f"G{i}" for i in range(1, 6)])


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, fold: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NY, fold=fold)


def doc() -> dict:
    return yaml.safe_load((EXAMPLE / "schedule" / "windows.yaml").read_text())


@pytest.fixture
def cfg() -> WindowsConfig:
    return WindowsConfig.from_doc(doc())


def report(*failed: str) -> PreflightReport:
    return PreflightReport({i: i not in failed for i in ITEMS})


# -- windows.yaml -------------------------------------------------------------------------------------------------

def test_the_example_file_reads(cfg):
    assert (cfg.operating_start.hour, cfg.operating_end.hour) == (6, 22)
    assert set(cfg.windows) == set(cal.WINDOW_NAMES)
    assert cfg.windows["all_quiet"].start == cal.Anchor("Fri", cfg.windows["all_quiet"].start.time, "XNYS")
    assert [str(m) for m in cfg.maintenance] == ["Sat 22:00-02:00 (ops calendar 2026-10)"]
    assert cal.config_problems(doc()) == []


@pytest.mark.parametrize("edit,path,text", [
    (lambda d: d["operating_day"].update(start="6:00"), "operating_day.start", "not a time HH:MM"),
    (lambda d: d["operating_day"].update(start="22:00", end="06:00"), "operating_day", "not before it ends"),
    (lambda d: d["windows"].pop("futures_break"), "windows.futures_break", "not defined"),
    (lambda d: d["windows"]["equities_quiet"].update(rule="XNYS closed"), "windows.equities_quiet.rule",
     "not one the scheduler resolves"),
    (lambda d: d["windows"].update(none={"rule": "x"}), "windows.none", "absence of a window"),
    (lambda d: d["windows"]["all_quiet"].pop("end"), "windows.all_quiet", "both start and end"),
    (lambda d: d["windows"]["all_quiet"].update(rule=cal.RULE_FUTURES_BREAK), "windows.all_quiet", "not both"),
    (lambda d: d["windows"]["all_quiet"]["end"].update(time="24:00"), "windows.all_quiet.end.time", "HH:MM"),
    (lambda d: d["windows"]["all_quiet"]["end"].update(weekday="Fri", time="20:00"), "windows.all_quiet",
     "same weekday and time"),
    (lambda d: d["maintenance_windows"][0].update(end="22:00"), "maintenance_windows[0]", "no length"),
    (lambda d: d["maintenance_windows"][0].update(source=""), "maintenance_windows[0].source", "names its source"),
    (lambda d: d["windows"]["all_quiet"]["start"].update(calendar="XLON"), "windows.all_quiet.start.calendar",
     "not one of XNYS, CMES"),
    (lambda d: d["windows"]["all_quiet"].update(end="Sun 17:00"), "windows.all_quiet.end", "an edge is"),
])
def test_config_problems(edit, path, text):
    d = doc()
    edit(d)
    found = cal.config_problems(d)
    assert any(p == path and text in m for p, m in found), found
    with pytest.raises(WindowsConfigError) as err:
        WindowsConfig.from_doc(d)
    assert err.value.problems == tuple(found)


def test_rule_text_is_matched_ignoring_case_and_spacing():
    d = doc()
    d["windows"]["futures_break"]["rule"] = "  cmes daily break 17:00 to 18:00,   or CMES weekend close "
    assert cal.config_problems(d) == []


# -- the exchange calendar ----------------------------------------------------------------------------------------

def test_xnys_holidays_2026_and_2027():
    assert cal.holidays(date(2026, 1, 1), date(2027, 12, 31)) == tuple(date.fromisoformat(s) for s in (
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
        "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
        "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"))


def test_a_saturday_new_year_is_not_observed_and_good_friday_moves_with_easter():
    assert cal.is_business_day(date(2027, 12, 31))                 # Jan 1 2028 is a Saturday (NYSE rule 7.2)
    assert not cal.is_business_day(date(2026, 4, 3)) and not cal.is_business_day(date(2028, 4, 14))
    assert not cal.is_business_day(date(2025, 1, 9))                # national day of mourning
    assert cal.is_business_day(date(2026, 4, 3), "XNYS") is False and cal.is_session_day("CMES", date(2026, 4, 3)) is False


def test_cmes_closes_three_days_and_halts_early_on_the_others():
    assert cal.holidays(date(2026, 1, 1), date(2026, 12, 31), "CMES") == (date(2026, 1, 1), date(2026, 4, 3),
                                                                          date(2026, 12, 25))
    mlk = cal.sessions("CMES", date(2026, 1, 19), date(2026, 1, 19))
    assert mlk == (cal.Span(at(2026, 1, 18, 18), at(2026, 1, 19, 13)),)
    friday = cal.sessions("CMES", date(2026, 11, 27), date(2026, 11, 27))
    assert friday[0].end == at(2026, 11, 27, 13, 15)


def test_business_days_count_thanksgiving_week():
    assert cal.business_days(date(2026, 11, 23), date(2026, 11, 27)) == (
        date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27))
    with pytest.raises(ValueError):
        cal.business_days(date(2026, 1, 1), date(2026, 1, 2), "FX")


def test_xnys_sessions_end_four_hours_after_the_close():
    assert cal.sessions("XNYS", date(2026, 11, 25), date(2026, 11, 27)) == (
        cal.Span(at(2026, 11, 25, 7), at(2026, 11, 25, 20)), cal.Span(at(2026, 11, 27, 7), at(2026, 11, 27, 17)))


def test_forex_and_crypto_profiles():
    monday = cal.sessions("FX", date(2026, 11, 9), date(2026, 11, 9))
    assert monday == (cal.Span(at(2026, 11, 8, 17), at(2026, 11, 9, 17)),)
    assert cal.sessions("FX", date(2026, 11, 7), date(2026, 11, 8)) == ()
    saturday = cal.sessions("CRYPTO", date(2026, 11, 7), date(2026, 11, 7))
    assert saturday == (cal.Span(at(2026, 11, 7), at(2026, 11, 7, 3)), cal.Span(at(2026, 11, 7, 5), at(2026, 11, 8)))


@pytest.mark.parametrize("symbol,asset,want", [
    ("AAPL", "equity", "XNYS"), (".AAPL261218C200", "option", "XNYS"), ("SPX", "index", "XNYS"),
    ("/MESZ26", "future", "CMES"), ("/MBTZ26", "future", "CRYPTO"), ("./BTCZ26C100000", "future_option", "CRYPTO"),
    ("EUR/USD", "forex", "FX")])
def test_symbol_calendars(symbol, asset, want):
    assert cal.symbol_calendar(symbol, asset) == want


def test_crypto_roots():
    assert [cal.is_crypto_future(s) for s in ("/BTC", "/MBTZ26", "/BTC:XCME", "/BTCE", "/MESZ26", "BTC")] == \
        [True, True, True, False, False, False]


# -- windows ------------------------------------------------------------------------------------------------------

def test_next_all_quiet_from_thursday_is_friday_20_to_sunday_17(cfg):
    span = cal.next_all_quiet(at(2026, 11, 5, 10), cfg)
    assert span == cal.Span(at(2026, 11, 6, 20), at(2026, 11, 8, 17))
    assert span.start.tzinfo == timezone.utc and str(span) == "Fri 2026-11-06 20:00 EST to Sun 2026-11-08 17:00 EST"


def test_all_quiet_inside_the_window_is_the_current_one(cfg):
    assert cal.window("all_quiet", at(2026, 11, 7, 3), cfg) == cal.Span(at(2026, 11, 6, 20), at(2026, 11, 8, 17))
    assert cal.window("all_quiet", at(2026, 11, 8, 17), cfg).start == at(2026, 11, 13, 20)


def test_all_quiet_over_the_dst_weekend_is_46_real_hours(cfg):
    span = cal.next_all_quiet(at(2026, 10, 29, 12), cfg)
    assert span == cal.Span(at(2026, 10, 30, 20), at(2026, 11, 1, 17))
    assert span.end - span.start == timedelta(hours=46)
    assert str(span) == "Fri 2026-10-30 20:00 EDT to Sun 2026-11-01 17:00 EST"


def test_all_quiet_on_thanksgiving_weekend_ignores_the_early_close(cfg):
    assert cal.next_all_quiet(at(2026, 11, 26, 12), cfg) == cal.Span(at(2026, 11, 27, 20), at(2026, 11, 29, 17))


def test_all_quiet_on_an_xnys_holiday_friday_starts_once_futures_and_forex_close(cfg):
    # w4-calendar-queue:0: XNYS is closed but CME Globex halts only at 13:00 (Juneteenth, July 3 observed) and forex
    # trades to 17:00 (Good Friday, Christmas): the start moves back over the XNYS holiday only to the last close
    assert cal.next_all_quiet(at(2026, 6, 17, 12), cfg) == cal.Span(at(2026, 6, 19, 17), at(2026, 6, 21, 17))
    assert cal.next_all_quiet(at(2026, 7, 1, 12), cfg) == cal.Span(at(2026, 7, 3, 17), at(2026, 7, 5, 17))
    assert cal.next_all_quiet(at(2026, 4, 1, 12), cfg) == cal.Span(at(2026, 4, 3, 17), at(2026, 4, 5, 17))
    assert cal.next_all_quiet(at(2026, 12, 23, 12), cfg) == cal.Span(at(2026, 12, 25, 17), at(2026, 12, 27, 17))


@pytest.mark.parametrize("end_calendar", [None, "XNYS", "CMES"])
def test_all_quiet_never_covers_an_xnys_cmes_or_forex_session_2026_to_2028(end_calendar):
    d = doc()
    if end_calendar:
        d["windows"]["all_quiet"]["end"]["calendar"] = end_calendar
    c = WindowsConfig.from_doc(d)
    live = [s for k in ("XNYS", "CMES", "FX") for s in cal.sessions(k, date(2025, 12, 20), date(2029, 1, 10))]
    t = at(2026, 1, 1)
    while t < at(2029, 1, 1):
        span = cal.next_all_quiet(t, c)
        assert span.end - span.start >= timedelta(hours=44), span
        assert not [s for s in live if s.start < span.end and s.end > span.start], span
        t = span.end


def test_an_end_with_a_calendar_moves_over_the_holiday_only_until_a_market_reopens():
    d = doc()
    d["windows"]["all_quiet"]["end"].update(calendar="CMES", time="12:00")
    c = WindowsConfig.from_doc(d)
    # Christmas Monday 2028: CMES is closed, so the end moves to Monday, but forex reopens on Sunday at 17:00
    assert cal.next_all_quiet(at(2028, 12, 21, 12), c) == cal.Span(at(2028, 12, 22, 20), at(2028, 12, 24, 17))
    assert cal.next_all_quiet(at(2028, 12, 14, 12), c) == cal.Span(at(2028, 12, 15, 20), at(2028, 12, 17, 12))


def test_equities_quiet_runs_between_extended_sessions(cfg):
    assert cal.window("equities_quiet", at(2026, 11, 10, 21), cfg, symbol="AAPL") == \
        cal.Span(at(2026, 11, 10, 20), at(2026, 11, 11, 7))
    assert cal.window("equities_quiet", at(2026, 11, 6, 21), cfg, symbol="AAPL") == \
        cal.Span(at(2026, 11, 6, 20), at(2026, 11, 9, 7))
    assert cal.window("equities_quiet", at(2026, 11, 25, 21), cfg, symbol="AAPL") == \
        cal.Span(at(2026, 11, 25, 20), at(2026, 11, 27, 7))
    assert cal.window("equities_quiet", at(2026, 11, 27, 12), cfg, symbol="AAPL") == \
        cal.Span(at(2026, 11, 27, 17), at(2026, 11, 30, 7))


def test_a_symbol_on_the_24_hour_list_never_gets_equities_quiet(cfg):
    assert cal.window("equities_quiet", at(2026, 11, 10, 21), cfg, symbol="AAPL", overnight=frozenset({"AAPL"})) is None
    assert cal.window("equities_quiet", at(2026, 11, 10, 21), cfg, symbol="CEI", overnight=frozenset({"AAPL"}))


def test_futures_break_daily_weekend_and_holiday(cfg):
    assert cal.window("futures_break", at(2026, 11, 10, 17, 30), cfg) == cal.Span(at(2026, 11, 10, 17), at(2026, 11, 10, 18))
    assert cal.window("futures_break", at(2026, 11, 10, 9), cfg) == cal.Span(at(2026, 11, 10, 17), at(2026, 11, 10, 18))
    assert cal.window("futures_break", at(2026, 11, 6, 19), cfg) == cal.Span(at(2026, 11, 6, 17), at(2026, 11, 8, 18))
    assert cal.window("futures_break", at(2026, 11, 26, 14), cfg) == cal.Span(at(2026, 11, 26, 13), at(2026, 11, 26, 18))
    assert cal.window("futures_break", at(2026, 12, 24, 14), cfg) == cal.Span(at(2026, 12, 24, 13, 15), at(2026, 12, 27, 18))


@pytest.mark.parametrize("name", ["all_quiet", "equities_quiet", "futures_break"])
def test_crypto_futures_have_no_quiet_window(cfg, name):
    assert cal.window(name, at(2026, 11, 7, 3), cfg, symbol="/MBTZ26") is None
    assert cal.window(name, at(2026, 11, 7, 3), cfg, symbol="/MESZ26") is not None


def test_window_refuses_none_unknown_and_naive(cfg):
    with pytest.raises(ValueError, match="absence"):
        cal.window("none", at(2026, 11, 7), cfg)
    with pytest.raises(ValueError, match="not defined"):
        cal.window("lunch", at(2026, 11, 7), cfg)
    with pytest.raises(ValueError, match="naive"):
        cal.window("all_quiet", datetime(2026, 11, 7), cfg)


# -- the operating day (11.1 PA-12, SD-76) ------------------------------------------------------------------------

@pytest.mark.parametrize("when,run_type,want", [
    (at(2026, 11, 10, 21, 59), "pr", True), (at(2026, 11, 10, 22), "merge", False), (at(2026, 11, 10, 5, 59), "rc", False),
    (at(2026, 11, 10, 6), "pr", True), (at(2026, 11, 10, 23), "nightly", True), (at(2026, 11, 7, 2), "calibration", True)])
def test_is_operating_time(cfg, when, run_type, want):
    assert cal.is_operating_time(when, cfg, run_type) is want


def test_a_run_outside_the_operating_day_waits_until_six(cfg):
    assert cal.operating_start(at(2026, 11, 10, 23, 30), cfg, "pr") == at(2026, 11, 11, 6)
    assert cal.operating_start(at(2026, 11, 10, 5), cfg, "merge") == at(2026, 11, 10, 6)
    assert cal.operating_start(at(2026, 11, 10, 10), cfg, "rc") == at(2026, 11, 10, 10)
    assert cal.operating_start(at(2026, 11, 10, 23, 30), cfg, "nightly") == at(2026, 11, 10, 23, 30)
    with pytest.raises(ValueError, match="unknown run type"):
        cal.operating_start(at(2026, 11, 10, 10), cfg, "weekly")


# -- maintenance windows and the reschedule (7.11, 10.7, 12.7) ----------------------------------------------------

@pytest.mark.parametrize("when,inside", [
    (at(2026, 11, 7, 23, 10), True), (at(2026, 11, 8, 1, 30), True), (at(2026, 11, 8, 2), False),
    (at(2026, 11, 7, 21, 59), False), (at(2026, 11, 8, 23), False)])
def test_in_maintenance_window(cfg, when, inside):
    assert cal.in_maintenance_window(when, cfg) is inside


def test_an_n3_failure_inside_the_window_reschedules_once_to_the_end_plus_15(cfg):
    r = cal.maintenance_reschedule(report("N3"), at(2026, 11, 7, 23, 10), cfg, run_id="run-1")
    assert r is not None and r.items == ("N3",) and r.start_at == at(2026, 11, 8, 2, 15)
    assert r.health_line() == ("preflight N3 failed at Sat 2026-11-07 23:10 EST inside maintenance window Sat "
                               "22:00-02:00 (ops calendar 2026-10); run rescheduled once to Sun 2026-11-08 02:15 EST "
                               "instead of env_error (7.11)")
    assert cal.maintenance_reschedule(report("N3"), at(2026, 11, 8, 2, 16), cfg, run_id="run-1",
                                      previous_reschedules=1) is None


@pytest.mark.parametrize("failed,when", [
    (("N3",), at(2026, 11, 7, 21, 0)),            # outside the window: env_error as usual
    (("N2",), at(2026, 11, 7, 23, 10)),           # a degraded mode, not a reschedule item
    (("N1", "R2"), at(2026, 11, 7, 23, 10)),      # a rig item failed too: its own outcome
    (("N4", "H1"), at(2026, 11, 7, 23, 10)),
    ((), at(2026, 11, 7, 23, 10))])
def test_no_reschedule(cfg, failed, when):
    assert cal.maintenance_reschedule(report(*failed), when, cfg, run_id="run-1") is None


def test_network_failures_reschedule_together(cfg):
    r = cal.maintenance_reschedule(report("N1", "N4", "N5"), at(2026, 11, 8, 1, 0), cfg, run_id="run-2")
    assert r is not None and r.items == ("N1", "N4") and r.start_at == at(2026, 11, 8, 2, 15)


def test_the_maintenance_window_across_both_dst_changes(cfg):
    fall = cal.maintenance_reschedule(report("N1"), at(2026, 11, 1, 1, 30, fold=1), cfg, run_id="r")
    assert fall.span.end - fall.span.start == timedelta(hours=5) and fall.start_at == at(2026, 11, 1, 2, 15)
    spring = cal.maintenance_reschedule(report("N1"), at(2027, 3, 14, 1, 30), cfg, run_id="r")
    assert spring.span.end - spring.span.start == timedelta(hours=4) and spring.start_at == at(2027, 3, 14, 3, 15)


def test_without_a_reschedule_the_provisioned_gate_gives_the_usual_env_error(cfg):
    rep = report("N1")
    assert cal.maintenance_reschedule(rep, at(2026, 11, 8, 2, 16), cfg, run_id="r", previous_reschedules=1) is None
    res = check_provisioned(rep)
    assert (res.unreached.outcome, res.unreached.gate) == ("env_error", "preflight.N1")


# -- the windowed-flow scheduler (7.10) ---------------------------------------------------------------------------

def test_a_flow_with_p95_50_min_at_2130_is_refused_by_the_operating_day(cfg):
    item = WindowedItem("flow.a", "equities_quiet", "AAPL", ("flow/x/flow.a/s01",))
    plan = cal.plan_windowed([item], at(2026, 11, 10, 21, 30), {"flow.a": 3000}, cfg, run_type="pr")
    (a,) = plan.refused
    assert "after the operating day ends Tue 2026-11-10 22:00 EST" in a.detail
    assert plan.outcomes() == {"flow/x/flow.a/s01": a.outcome}
    assert (a.outcome.outcome, a.outcome.reason) == ("not_run", "budget")
    ok = cal.plan_windowed([item], at(2026, 11, 10, 20, 30), {"flow.a": 3000}, cfg, run_type="pr")
    assert ok.admitted[0].start_at == at(2026, 11, 10, 20, 30) and ok.admitted[0].ends_by == at(2026, 11, 10, 21, 20)
    nightly = cal.plan_windowed([item], at(2026, 11, 10, 21, 30), {"flow.a": 3000}, cfg, run_type="nightly")
    assert nightly.admitted and not nightly.outcomes()


def test_a_flow_that_cannot_finish_before_the_window_ends_is_refused(cfg):
    item = WindowedItem("flow.b", "futures_break")
    plan = cal.plan_windowed([item], at(2026, 11, 10, 17, 30), {"flow.b": 2400}, cfg, run_type="merge")
    assert not plan.admitted and "after futures_break closes Tue 2026-11-10 18:00 EST" in plan.refused[0].detail


def test_a_flow_waits_for_its_window_until_the_deadline(cfg):
    item = WindowedItem("flow.b", "futures_break")
    plan = cal.plan_windowed([item], at(2026, 11, 10, 16), {"flow.b": 600}, cfg, run_type="rc",
                             deadline=at(2026, 11, 10, 17, 30))
    assert plan.admitted[0].start_at == at(2026, 11, 10, 17)
    late = cal.plan_windowed([item], at(2026, 11, 10, 16), {"flow.b": 600}, cfg, run_type="rc",
                             deadline=at(2026, 11, 10, 16, 30))
    assert "after the run deadline" in late.refused[0].detail
    tight = cal.plan_windowed([item], at(2026, 11, 10, 16), {"flow.b": 600}, cfg, run_type="rc",
                              deadline=at(2026, 11, 10, 17, 5))
    assert "ends Tue 2026-11-10 17:10 EST, after the run deadline" in tight.refused[0].detail


def test_windowed_items_queue_one_after_another(cfg):
    items = [WindowedItem(f"flow.{i}", "futures_break") for i in range(3)]
    plan = cal.plan_windowed(items, at(2026, 11, 10, 17), {f"flow.{i}": 1500 for i in range(3)}, cfg, run_type="nightly")
    assert [a.start_at for a in plan.admitted] == [at(2026, 11, 10, 17), at(2026, 11, 10, 17, 25)]
    assert [a.item_id for a in plan.refused] == ["flow.2"]


def test_an_unmeasured_flow_uses_its_declared_budget_else_five_minutes(cfg):
    items = [WindowedItem("flow.d", "futures_break", max_duration_s=1200), WindowedItem("flow.e", "futures_break")]
    plan = cal.plan_windowed(items, at(2026, 11, 10, 17), {}, cfg, run_type="nightly")
    assert [a.ends_by for a in plan.admitted] == [at(2026, 11, 10, 17, 20), at(2026, 11, 10, 17, 25)]
    assert plan.admitted[0].detail.startswith("declared max_duration_s 1200 s")
    assert plan.admitted[1].detail.startswith("unmeasured default 300 s")


def test_crypto_overnight_and_unwindowed_items(cfg):
    items = [WindowedItem("flow.c", "all_quiet", "/MBTZ26"), WindowedItem("flow.n"),
             WindowedItem("flow.o", "equities_quiet", "AAPL")]
    plan = cal.plan_windowed(items, at(2026, 11, 7, 1), {}, cfg, run_type="calibration", overnight=frozenset({"AAPL"}))
    assert plan.unwindowed == ("flow.n",)
    assert [a.detail for a in plan.refused] == [
        "no all_quiet window exists for /MBTZ26 (crypto futures are always live, 7.10)",
        "no equities_quiet window exists for AAPL (the symbol is on the 24-hour list read this run, 7.10)"]


def test_a_window_opening_after_the_operating_day_is_refused(cfg):
    item = WindowedItem("flow.q", "all_quiet")
    noon = cal.plan_windowed([item], at(2026, 11, 6, 12), {}, cfg, run_type="pr")
    assert noon.admitted[0].start_at == at(2026, 11, 6, 20)             # waits for the window inside the day
    late = cal.plan_windowed([item], at(2026, 11, 6, 21, 58), {}, cfg, run_type="pr")
    assert "after the operating day ends" in late.refused[0].detail
    sat = cal.plan_windowed([item], at(2026, 11, 7, 23), {}, cfg, run_type="pr")
    assert "outside the operating day 06:00 to 22:00" in sat.refused[0].detail


# -- preflight G4 (12.7) ------------------------------------------------------------------------------------------

def fixture_symbols() -> list[dict]:
    return yaml.safe_load((EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml").read_text())["symbols"]


def test_g4_resolves_every_fixture_symbol():
    g4 = cal.g4_horizon_check(date(2026, 11, 10), fixture_symbols(), overnight=frozenset({"AAPL"}))
    assert g4.ok and g4.outcome() is None
    assert g4.sessions["AAPL"] == len(cal.business_days(date(2026, 11, 10), date(2027, 2, 8)))
    assert g4.sessions["EUR/USD"] > 0 and g4.sessions["/MESZ26"] > 0


def test_g4_fails_when_the_calendar_horizon_is_under_90_days():
    g4 = cal.g4_horizon_check(date(2028, 10, 15), fixture_symbols(), overnight=frozenset())
    assert not g4.ok and "ends 2028-12-31, before the run date plus 90 days (2029-01-13)" in g4.detail
    o = g4.outcome()
    assert (o.outcome, o.reason, o.gate) == ("env_error", "gate", "preflight.G4")


def test_a_failed_g4_feeds_the_provisioned_gate():
    g4 = cal.g4_horizon_check(date(2028, 10, 15), fixture_symbols(), overnight=frozenset())
    res = check_provisioned(PreflightReport({**{i: True for i in ITEMS}, "G4": g4.ok}, {"G4": g4.detail}))
    assert (res.unreached.outcome, res.unreached.reason, res.unreached.gate) == ("env_error", "gate", "preflight.G4")


def test_g4_fails_without_the_24_hour_list_or_with_an_unknown_asset():
    assert "24-hour trading list was not read" in cal.g4_horizon_check(date(2026, 11, 10), [], overnight=None).detail
    bad = cal.g4_horizon_check(date(2026, 11, 10), [{"symbol": "XYZ", "asset": "bond"}], overnight=frozenset())
    assert not bad.ok and "asset 'bond' has no calendar" in bad.detail


def test_g4_fails_when_the_zone_data_cannot_load(monkeypatch):
    def missing() -> ZoneInfo:
        raise ZoneInfoNotFoundError("No time zone found with key America/New_York")
    monkeypatch.setattr(cal, "_zone", missing)
    g4 = cal.g4_horizon_check(date(2026, 11, 10), fixture_symbols(), overnight=frozenset())
    assert not g4.ok and g4.detail.startswith("time zone data for America/New_York unavailable")


# -- the lockstep pause (10.7, 11.3) ------------------------------------------------------------------------------

def test_a_partner_resuming_in_the_same_minute_keeps_the_checkpoint():
    d = cal.lockstep_pause("cp", "masked_live", 0.0, 200.0, 230.0)
    assert d.partner_paused and d.outcome is None and d.lag_s == 30.0 and d.pause_s == 230.0


def test_a_partner_lagging_more_than_60_s_is_not_run_budget():
    d = cal.lockstep_pause("cp", "masked_live", at(2026, 11, 10, 10), at(2026, 11, 10, 10, 3), at(2026, 11, 10, 10, 5))
    assert (d.outcome.outcome, d.outcome.reason) == ("not_run", "budget") and d.lag_s == 120.0 and d.pause_s == 300.0
    never = cal.lockstep_pause("cp", "masked_live", 0.0, 100.0, None)
    assert never.outcome.reason == "budget" and never.pause_s == 100.0


def test_a_frozen_checkpoint_has_no_partner():
    d = cal.lockstep_pause("cp", "frozen", 0.0, 90.0, None)
    assert not d.partner_paused and d.outcome is None and d.pause_s == 90.0
    with pytest.raises(ValueError):
        cal.lockstep_pause("cp", "live", 0.0, 1.0, 1.0)


# -- DST and the time-axis label check (7.10, 7.1) ----------------------------------------------------------------

def test_dst_transitions():
    assert cal.dst_transitions(2026) == (date(2026, 3, 8), date(2026, 11, 1))
    assert cal.dst_transitions(2027) == (date(2027, 3, 14), date(2027, 11, 7))


def test_the_nightly_after_a_dst_change_reruns_the_time_axis_check():
    assert cal.time_axis_check_due(at(2026, 11, 1, 22))
    assert cal.time_axis_check_due(at(2026, 3, 8, 22))
    assert not cal.time_axis_check_due(at(2026, 11, 2, 22))
    assert not cal.time_axis_check_due(at(2026, 10, 31, 22))
    assert cal.time_axis_check_due(at(2026, 11, 2, 22), previous_nightly=at(2026, 10, 31, 22))


def test_time_axis_labels_against_new_york_time():
    bars = [datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc), datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)]
    assert cal.time_axis_check(["9:30", "10:00"], bars).ok
    bad = cal.time_axis_check(["10:30", "11:00"], bars)                   # still on daylight time
    assert bad.mismatches == ((0, "10:30", "09:30"), (1, "11:00", "10:00"))
    o = bad.outcome()
    assert (o.outcome, o.reason, o.gate) == ("env_error", "fixture_drift", "settings_attested")
    assert cal.time_axis_check(["9:3O"], bars[:1]).mismatches == ((0, "9:3O", "09:30"),)
    with pytest.raises(ValueError):
        cal.time_axis_check(["9:30"], bars)
