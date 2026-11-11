"""A relaunch re-enters at `launched` and passes every gate again, charging the budget its cause draws on
(7.7 Re-login flow, 10.2 Restart and resume rules, SD-66)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sentinel.orchestrator.budgets import SESSION_RELEASE_WAIT_S
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.killswitch import ORDERS_SENT
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import CrashSignal, LaunchObservation, OrderRow
from sentinel.orchestrator.simrig import BUILD, MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.orchestrator.watchdog import Heartbeat, HeartbeatWriter, app_failure

SCOPE = ["census/trade.order-entry.stock/ws.tier-a.active-trader",
         "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07",
         "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"]
GATES_AGAIN = ["launched", "authenticated", "paper_mode_asserted", "settings_attested", "workspace_loaded",
               "fixture_verified", "canary_flow_passed"]
CLEAN = LaunchObservation(True, BUILD, waited_s=40.0)


class StateLog(HeartbeatWriter):
    """A heartbeat writer that keeps the states it was asked to write."""

    def __init__(self, clock: ManualClock):
        super().__init__("unused", clock)
        self.states: list[str] = []

    def write(self, hb: Heartbeat) -> None:
        self.states.append(hb.state)


def run(work, scenario: SimScenario | None = None, **kw):
    clock = ManualClock()
    rig = SimulatedRig(scenario, clock=clock)
    sink = MemorySink(SCOPE)
    orch = Orchestrator(rig, kw.pop("ctx", default_context()), sink, clock=clock, **kw)
    report = orch.run(work)
    return report, rig, sink, orch


def rest(sink: MemorySink, from_index: int) -> set:
    return {sink.outcomes[c][:3] for c in SCOPE[from_index:]}


def test_an_app_failure_relaunch_passes_every_gate_again_and_keeps_the_run_start_snapshot():
    seen = {}

    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        r = orch.apply(app_failure(CrashSignal("hang", "screenshot_staleness"), orch.budgets, phase="running"))
        assert r.has("relaunch") and orch.budgets.used_app == 1
        seen["book"], seen["calls"], seen["results"] = orch.start_book, len(orch.rig.calls), len(orch.results)
        seen["states"], seen["t0"] = len(orch.heartbeat.states), orch.clock.monotonic()
        assert orch.relaunch("app_failure")
        seen["t1"] = orch.clock.monotonic()
        for c in SCOPE[1:]:
            orch.sink.record(c, "identical")
    clock = ManualClock()
    report, rig, _, orch = run(work, heartbeat=StateLog(clock))
    after = rig.calls[seen["calls"]:]
    for call in ("kill_process_tree", "launch", "login", "read_account_selector", "read_orders",
                 *[f"read_setting:A{i}" for i in range(3, 9)], "confirmation_check", "load_order_chords",
                 "load_workspace", "verify_account_fixture", "run_canary_flow"):
        assert call in after, call
    assert after.index("kill_process_tree") < after.index("launch")
    again = report.results[seen["results"]:]
    assert again[0].control == "relaunch" and again[0].evidence == {"cause": "app_failure", "budget": "app_relaunch",
                                                                   "flows_since_launch": 0}
    assert [g for g in GATES_AGAIN if any(r.gate == g and r.passed for r in again)] == GATES_AGAIN
    assert orch.heartbeat.states[seen["states"]:] == GATES_AGAIN + ["running"]
    assert orch.start_book is seen["book"] and orch.budgets.used_app == 1           # charged once, by app_failure
    assert seen["t1"] - seen["t0"] >= SESSION_RELEASE_WAIT_S
    assert report.verdict.status == "complete"


def test_an_attestation_row_changed_after_the_relaunch_aborts_as_at_first_launch():
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        orch.rig.s.settings["A3"] = False                    # something changed a safety setting meanwhile
        assert not orch.relaunch("app_failure")
    report, rig, sink, _ = run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "attestation_mismatch")
    assert rest(sink, 1) == {("env_error", "attestation_mismatch", "settings_attested")}
    assert report.kill_switch is not None and rig.calls.count("confirmation_check") == 1


def test_a_new_order_row_at_the_relaunch_is_the_kill_switch_and_the_snapshot_is_never_replaced():
    stray = OrderRow("stray-1", "working", "WORKING", "CEI", 1.0)

    def work(orch: Orchestrator) -> None:
        orch.rig.book = replace(orch.rig.book, working=(stray,))
        assert not orch.relaunch("app_failure")
        assert orch.stop.evidence["new_rows"] == ["stray-1"] and orch.start_book.working == ()
    report, _, sink, _ = run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "kill_switch")
    assert rest(sink, 0) == {("env_error", "kill_switch", "paper_mode_asserted")}
    assert report.proof.verdict == ORDERS_SENT and [r.key for r in report.proof.undeclared] == ["stray-1"]


def test_environment_relogins_draw_on_k_and_a_fourth_pop05_ends_the_run_relogin_budget():
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        for n in range(1, 4):
            assert orch.relaunch("environment_relogin") and orch.relogin.used == n
        assert not orch.relaunch("environment_relogin")
    report, rig, sink, orch = run(work)
    # the env_error checkpoints explain the run outcome, so it carries no run reason (5.2, gates.decide_run_outcome)
    assert report.verdict.status == "complete_with_env_errors" and orch.stop.run_reason == "relogin_budget"
    assert rest(sink, 1) == {("env_error", "relogin_budget", "authenticated")}
    assert rig.calls.count("launch") == 4 and rig.calls.count("login") == 4 and orch.budgets.used_app == 0
    assert [r.evidence["budget"] for r in report.results if r.control == "relaunch"] == ["relogin_k"] * 3


def test_the_proactive_and_hook_host_relaunches_spend_nothing_more():
    def work(orch: Orchestrator) -> None:
        assert orch.relaunch("proactive") and orch.relaunch("hook_host")
        for c in SCOPE:
            orch.sink.record(c, "identical")
    report, _, _, orch = run(work)
    assert report.verdict.status == "complete"
    assert (orch.budgets.used_app, orch.budgets.used_hook_host, orch.relogin.used) == (0, 0, 0)


def test_a_crash_during_the_relaunch_gate_sequence_spends_an_app_relaunch():
    def work(orch: Orchestrator) -> None:
        assert orch.relaunch("app_failure") and orch.budgets.used_app == 1
        for c in SCOPE:
            orch.sink.record(c, "identical")
    crash = LaunchObservation(False, crash=CrashSignal("jvm_crash", "crash_file"))
    report, rig, _, _ = run(work, SimScenario(launches=[CLEAN, crash]))
    assert report.verdict.status == "complete" and rig.calls.count("launch") == 3


def test_a_changed_build_at_the_relaunch_is_version_changed_mid_run():
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        assert not orch.relaunch("app_failure")
    report, _, sink, _ = run(work, SimScenario(launches=[CLEAN, LaunchObservation(True, "2026.45.0")]))
    assert report.verdict.status == "complete_with_env_errors"
    assert rest(sink, 1) == {("env_error", "version_changed_mid_run", "launched")}


RECOVERY = "set_hook_state:True:False:recovery_only"


def test_a_relaunch_whose_gates_fail_after_the_kill_proves_through_a_fresh_recovery_login():
    """session:2: the relaunch killed the session that did the work, then its own gates failed (here the build changed
    at `launched`). The proof is 6.6 step 5's fresh recovery login, never `teardown` and a Monitor read on an app that
    never logged in."""
    seen = {}

    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        orch.rig._send()                                   # an order the trip-wires missed, in the killed session
        seen["calls"] = len(orch.rig.calls)
        assert not orch.relaunch("app_failure")
    report, rig, _, _ = run(work, SimScenario(launches=[CLEAN, LaunchObservation(True, "2026.45.0")]))
    after = rig.calls[seen["calls"]:]
    assert "teardown" not in after and after.count("login") == 1
    k = len(after) - 1 - after[::-1].index("kill_process_tree")
    assert after[k + 1:k + 5] == ["reset_input_state", RECOVERY, "launch", "login"]
    assert report.proof.verdict == ORDERS_SENT and report.verdict.status == "aborted_safety"


def test_a_relaunch_login_that_hits_an_authentication_error_is_never_retried_for_the_proof():
    """session:2 with 6.9: the relaunch's login failed with an authentication error, which is never retried, so no
    recovery login follows; the proof is UNPROVEN recovery_login_failed with the hand-over, not a teardown read."""
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        assert not orch.relaunch("environment_relogin")
    report, rig, sink, _ = run(work, SimScenario(recovery_login_fails=1))
    assert rig.calls.count("login") == 2 and "teardown" not in rig.calls
    assert (report.proof.verdict, report.proof.reason) == ("UNPROVEN", "recovery_login_failed")
    assert report.gate("teardown").evidence["blocked_reason"] == "recovery_login_failed"
    assert rest(sink, 1) == {("env_error", "authentication_failed", "authenticated")}


def test_relaunch_refuses_an_unknown_cause_and_does_nothing_once_the_run_stopped():
    def work(orch: Orchestrator) -> None:
        with pytest.raises(ValueError):
            orch.relaunch("whim")
        orch.apply(app_failure(CrashSignal("oom", "heap_dump"), orch.budgets, phase="running"))
        orch.budgets.used_app = orch.budgets.app_relaunches
        orch.apply(app_failure(CrashSignal("oom", "heap_dump"), orch.budgets, phase="running"))
        launches = orch.rig.calls.count("launch")
        assert orch.stop is not None and not orch.relaunch("app_failure")
        assert orch.rig.calls.count("launch") == launches
    report, _, _, _ = run(work)
    assert report.verdict.reason == "app_restart_budget"
