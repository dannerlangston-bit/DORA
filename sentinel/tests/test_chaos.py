"""The RC chaos suite (7.11): scenarios, scheduling, the evaluators, rule removal and the simulated shaper."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sentinel import calendar
from sentinel.orchestrator import chaos
from sentinel.orchestrator.chaos import (SCENARIOS, ChaosPlan, SimulatedChaosDriver, SimulatedShaper, healthy,
                                         run_chaos)
from sentinel.orchestrator.ports import TrafficShaper
from sentinel.orchestrator.results import Outcome
from sentinel.taxonomy import is_checkpoint_id

PLAN = ChaosPlan("monitor.activity.position-statement", "ws.tier-a.active-trader", "active_trader",
                 "tos-login.example")
BY_NAME = {s.name: s for s in SCENARIOS}
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"


def test_the_scenario_table_of_7_11():
    assert [(s.kind, s.value, s.duration_s) for s in SCENARIOS] == [
        ("latency", 500.0, 120.0), ("loss", 0.05, 120.0), ("throttle", 256.0, 120.0),
        ("blackhole", 0.0, 30.0), ("blackhole", 0.0, 300.0)]
    assert all(is_checkpoint_id(s.checkpoint_id(PLAN.surface_id)) for s in SCENARIOS)
    assert BY_NAME["disconnect-30s"].assertions == chaos.ASSERTIONS
    assert "chaos.indicator" not in BY_NAME["latency-500ms"].assertions
    assert isinstance(SimulatedShaper(), TrafficShaper)


def test_scheduling_refuses_a_chaos_flow_beside_census_or_outside_rc():
    assert chaos.refusal("rc", ["flow"]) is None
    assert "census" in chaos.refusal("rc", ["flow", "census"])
    assert "RC runs only" in chaos.refusal("merge", ["flow"])


def test_the_window_scheduler_refuses_a_scenario_outside_all_quiet():
    items = chaos.windowed_items(PLAN)
    assert {i.requires_window for i in items} == {"all_quiet"}
    cfg = calendar.WindowsConfig.from_doc(yaml.safe_load((EXAMPLE / "schedule" / "windows.yaml").read_text()))
    plan = calendar.plan_windowed(items, datetime(2026, 11, 11, 15, tzinfo=timezone.utc), {}, cfg, run_type="rc",
                                  deadline=datetime(2026, 11, 11, 23, tzinfo=timezone.utc))   # a Wednesday RC run
    outcomes = plan.outcomes()
    assert set(outcomes) == {s.checkpoint_id(PLAN.surface_id) for s in SCENARIOS}
    assert all((o.outcome, o.reason) == ("not_run", "budget") for o in outcomes.values())


def test_a_healthy_client_passes_every_scenario_and_the_rules_are_removed():
    shaper, driver = SimulatedShaper(), SimulatedChaosDriver()
    res = run_chaos(shaper, driver, PLAN)
    assert [r.outcome for r in res.results] == [None] * 5 and res.rig_clean
    assert [r.host for r in shaper.applied] == [PLAN.host] * 5 and shaper.active() == ()
    assert shaper.clears == 5 and res.records("rc-1", PLAN) == []
    assert res.results[0].report_row()["outcome"] == "passed"


def test_a_rule_left_in_force_is_chaos_rule_stuck_and_the_helper_removes_all():
    shaper = SimulatedShaper(stuck_clears=1)
    res = run_chaos(shaper, SimulatedChaosDriver(), PLAN, SCENARIOS[:2])
    first = res.results[0]
    assert (first.outcome.outcome, first.outcome.reason) == ("harness_error", "chaos_rule_stuck")
    assert first.removed_all and shaper.clears == 3 and res.rig_clean        # removal, remove-all, next scenario
    assert res.results[1].outcome is None
    rec = first.record("rc-1", PLAN)
    assert rec.tier == 2 and rec.flow_id == "flow.chaos.latency-500ms" and rec.step_id == "reconnected"


def test_a_rule_the_remove_all_cannot_clear_stops_the_suite():
    shaper = SimulatedShaper(stuck_clears=5)
    res = run_chaos(shaper, SimulatedChaosDriver(), PLAN)
    assert not res.rig_clean
    assert all((r.outcome.outcome, r.outcome.reason) == ("harness_error", "chaos_rule_stuck") for r in res.results)
    assert len(shaper.applied) == 1


def test_a_stuck_rule_keeps_the_failed_assertions_as_evidence():
    bad = replace(healthy(BY_NAME["disconnect-30s"]), pop10_seen=True)
    res = run_chaos(SimulatedShaper(stuck_clears=1), SimulatedChaosDriver({"disconnect-30s": bad}), PLAN,
                    [BY_NAME["disconnect-30s"]])
    r = res.results[0]
    assert r.outcome.reason == "chaos_rule_stuck" and r.assertion_ids == ("chaos.no_pop10",)
    assert r.record("rc-1", PLAN).lower_results == [{"outcome": "assertion_failed", "reason": "assertion:chaos.no_pop10",
                                                    "detail": "POP-10 appeared after the reconnect"}]


def test_duplicate_working_rows_after_reconnect_fail_the_scenario():
    sc = BY_NAME["disconnect-5min"]
    bad = replace(healthy(sc), duplicate_rows={"monitor": 0, "working_orders": 2})
    r = run_chaos(SimulatedShaper(), SimulatedChaosDriver({sc.name: bad}), PLAN, [sc]).results[0]
    assert (r.outcome.outcome, r.outcome.reason) == ("assertion_failed", "assertion:chaos.no_duplicates")
    assert "working_orders 2" in r.detail and r.findings[0].tier == 2


def test_every_disconnect_assertion_is_evaluated():
    sc = BY_NAME["disconnect-30s"]
    frozen = chaos.ChaosObservation(True, indicator_change_s=40.0, quotes_stopped=True, stale_badge=False,
                                    error_state=False, quotes_resumed=False,
                                    duplicate_rows={"monitor": 1, "working_orders": 0}, pop10_seen=True)
    assert [a for a, _ in chaos.evaluate(sc, frozen)] == list(chaos.ASSERTIONS)
    advancing = replace(healthy(sc), quotes_stopped=False)
    assert [a for a, _ in chaos.evaluate(sc, advancing)] == ["chaos.quotes_stale"]
    silent = replace(healthy(BY_NAME["throttle-256kbps"]), quotes_stopped=True, stale_badge=False)
    assert [a for a, _ in chaos.evaluate(BY_NAME["throttle-256kbps"], silent)] == ["chaos.quotes_stale"]


def test_a_scenario_the_harness_did_not_observe_is_chaos_unobserved():
    sc = BY_NAME["disconnect-30s"]
    for obs in (None, replace(healthy(sc), rule_effective=False), replace(healthy(sc), error_state=None),
                replace(healthy(sc), duplicate_rows={"monitor": 0})):
        r = run_chaos(SimulatedShaper(), SimulatedChaosDriver({sc.name: obs}), PLAN, [sc]).results[0]
        assert (r.outcome.outcome, r.outcome.reason) == ("harness_error", "chaos_unobserved")


def test_failed_checks_after_a_scenario_stop_the_remaining_ones():
    res = run_chaos(SimulatedShaper(), SimulatedChaosDriver(connectivity=False), PLAN)
    assert res.results[0].outcome is None
    assert all((r.outcome.outcome, r.outcome.gate, r.outcome.reason) == ("env_error", "connectivity",
                                                                           "backend_degraded")
               for r in res.results[1:])
    res = run_chaos(SimulatedShaper(), SimulatedChaosDriver(fixture=False), PLAN)
    assert res.results[1].outcome.gate == "fixture_verified" and res.health


def test_skipped_scenarios_take_the_window_outcome():
    sc = SCENARIOS[0]
    skip = {sc.checkpoint_id(PLAN.surface_id): Outcome("not_run", "budget", detail="all_quiet closes")}
    driver = SimulatedChaosDriver()
    res = run_chaos(SimulatedShaper(), driver, PLAN, SCENARIOS[:2], skip=skip)
    assert res.results[0].outcome.outcome == "not_run" and driver.observed == [SCENARIOS[1].name]
