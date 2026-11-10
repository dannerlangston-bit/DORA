"""Gate checks of the 5.2 state machine, the run-outcome rules and the typed results (5.2, 12.7, 1.10, 9.10)."""
from __future__ import annotations

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.gates import (GATES, PREFLIGHT_ITEMS, AccountExpectation, WorkspaceExpectation,
                                         check_authenticated, check_build_unchanged, check_canary_flow, check_fixture,
                                         check_launched, check_provisioned, check_reported, check_teardown,
                                         check_workspace, decide_run_outcome)
from sentinel.orchestrator.results import ControlResult, FindingSpec, Outcome, RunVerdict, write_unreached
from sentinel.orchestrator.rig import (AccountState, CanaryFlowObservation, CrashSignal, LaunchObservation,
                                       LoginObservation, PreflightReport, TeardownObservation, WindowInfo,
                                       WorkspaceObservation)
from sentinel.orchestrator.simrig import MemorySink
from sentinel.taxonomy import TaxonomyError, load

BUILD = "2026.44.1"


def preflight(**failed: bool) -> PreflightReport:
    return PreflightReport({i: not failed.get(i, False) for i in PREFLIGHT_ITEMS})


def test_gate_names_are_the_taxonomy_gates_in_order():
    assert GATES == tuple(load().gates[:11]) and len(PREFLIGHT_ITEMS) == 32


# -- provisioned ------------------------------------------------------------------------------------------
def test_provisioned_pass_and_nothing_skipped():
    assert check_provisioned(preflight()).passed
    with pytest.raises(ValueError):
        check_provisioned(PreflightReport({"R1": True}))


def test_provisioned_h_item_aborts_harness_before_any_product_action():
    r = check_provisioned(PreflightReport({**preflight(H4=True).items}, {"H4": "AppLocker"}))
    assert r.run_status == "aborted_harness" and r.run_reason == "preflight.H4"
    assert r.unreached == Outcome("harness_error", "preflight.H4", detail="AppLocker")
    assert r.findings[0].kind == "hardening" and r.alert == alerts.aborted_harness()


def test_provisioned_g1_is_config_invalid():
    r = check_provisioned(preflight(G1=True, H1=True))
    assert r.run_reason == "config_invalid" and r.unreached.outcome == "harness_error"


@pytest.mark.parametrize("item", ["R1", "R7", "B2", "G3", "N1", "N6"])
def test_provisioned_env_item_is_env_error_with_reason_gate(item):
    r = check_provisioned(preflight(**{item: True}))
    assert r.run_status == "complete_with_env_errors"
    assert r.unreached == Outcome("env_error", "gate", f"preflight.{item}", r.unreached.detail)
    assert r.alert.rule == "env_error" and r.alert.priority == "P2"
    assert check_provisioned(preflight(**{item: True}), repeat_gates=frozenset({f"preflight.{item}"})).alert.priority == "P1"


def test_provisioned_partial_and_degraded_items_let_the_run_continue():
    r = check_provisioned(preflight(N3=True, N4=True, G5=True, N2=True, N7=True), calibration_record=False)
    assert r.passed and r.run is None
    sub = {s.selector: s.outcome for s in r.outcomes}
    assert sub["api_within"] == Outcome("env_error", "gate", "preflight.N3")
    assert sub["frozen"] == Outcome("env_error", "gate", "preflight.N4")
    assert sub["all"].outcome == "not_comparable" and sub["all"].reason == "fingerprint_mismatch"
    assert r.alert == alerts.model_degraded() and r.has("queue_calibration")


# -- launched ---------------------------------------------------------------------------------------------
def test_launched_pass_reads_the_title_build_id():
    r = check_launched(LaunchObservation(True, BUILD, waited_s=30), expected_build=BUILD, first_launch=True,
                       budgets=ShardBudgets())
    assert r.passed and r.evidence["source"] == "title"
    about = check_launched(LaunchObservation(True, None, about_build_id=BUILD), expected_build=BUILD,
                           first_launch=True, budgets=ShardBudgets())
    assert about.passed and about.evidence["source"] == "about"


def test_launched_wrong_build_and_mid_run_change():
    first = check_launched(LaunchObservation(True, "2026.43.9"), expected_build=BUILD, first_launch=True,
                           budgets=ShardBudgets())
    assert first.unreached == Outcome("env_error", "version_mismatch", "launched", first.unreached.detail)
    again = check_launched(LaunchObservation(True, "2026.45.0"), expected_build=BUILD, first_launch=False,
                           budgets=ShardBudgets())
    assert again.unreached.reason == "version_changed_mid_run"
    assert check_build_unchanged(BUILD, BUILD).passed
    assert check_build_unchanged("2026.45.0", BUILD).unreached.reason == "version_changed_mid_run"
    pop01 = check_launched(LaunchObservation(True, BUILD, update_progress=True), expected_build=BUILD,
                           first_launch=True, budgets=ShardBudgets())
    assert pop01.unreached.reason == "version_mismatch"


def test_launched_no_frame_is_env_error_only_at_first_launch_without_a_crash_signal():
    obs = LaunchObservation(False, windows=(WindowInfo(9, 1, "SunAwtFrame", "Updater"),), processes=("launcher.exe",))
    r = check_launched(obs, expected_build=BUILD, first_launch=True, budgets=ShardBudgets())
    assert r.unreached == Outcome("env_error", "gate", "launched", r.unreached.detail)
    assert r.evidence["windows"] == ["Updater"] and r.evidence["processes"] == ["launcher.exe"]
    b = ShardBudgets()
    hang = check_launched(obs, expected_build=BUILD, first_launch=False, budgets=b)
    assert hang.checkpoint.reason == "assertion:app_alive" and hang.checkpoint.detail == "hang" and b.used_app == 1


def test_launched_crash_at_first_launch_consumes_a_relaunch_f62():
    b = ShardBudgets(app_relaunches=1)
    obs = LaunchObservation(False, crash=CrashSignal("jvm_crash", "crash_file"))
    r = check_launched(obs, expected_build=BUILD, first_launch=True, budgets=b)
    assert r.checkpoint.outcome == "assertion_failed" and r.has("relaunch") and r.unreached is None
    spent = check_launched(obs, expected_build=BUILD, first_launch=False, budgets=b)
    assert spent.run_reason == "app_restart_budget" and spent.unreached == Outcome("not_run", "budget")


# -- authenticated ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("obs,reason", [
    (LoginObservation(mode_switch_located=False), "gate"),
    (LoginObservation(live_selected=True), "live_mode_selected"),
    (LoginObservation(remember_password_off=False), "gate"),
    (LoginObservation(vault_lease=False), "vault_unreachable"),
    (LoginObservation(challenge=True), "authentication_challenge"),
    (LoginObservation(auth_error=True), "authentication_failed"),
    (LoginObservation(session_kicked=True), "session_kicked"),
    (LoginObservation(main_window_s=None), "timeout"),
    (LoginObservation(main_window_s=121.0), "timeout"),
    (LoginObservation(post_login_anchor=False), "gate"),
])
def test_authenticated_failures(obs, reason):
    r = check_authenticated(obs)
    assert r.run_status == "complete_with_env_errors" and not r.actions
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "authenticated", reason)
    assert "login_owner" in r.alert.notified


def test_authenticated_pass_and_auth_error_pages_the_accounts_owner():
    assert check_authenticated(LoginObservation()).passed
    assert "accounts_owner" in check_authenticated(LoginObservation(auth_error=True)).alert.paged


# -- workspace, fixture, canary ---------------------------------------------------------------------------
WS = WorkspaceExpectation("w" * 64, ("Monitor", "Trade"), 1)


def test_workspace_loaded():
    ok = check_workspace(WorkspaceObservation("w" * 64, True, ("Monitor", "Trade"), 1, ("P3",)), WS)
    assert ok.passed and ok.outcomes[0].outcome == Outcome("landmark_missing", "probe:P3")
    bad = check_workspace(WorkspaceObservation("x" * 64, True, ("Monitor", "Trade"), 1), WS)
    assert bad.unreached == Outcome("env_error", "gate", "workspace_loaded", bad.unreached.detail)
    assert not check_workspace(WorkspaceObservation("w" * 64, True, ("Monitor",), 1), WS).passed


FIX = {"G": AccountExpectation("golden", {"AAPL": 100.0}, 50000.0, 5000.0),
       "E": AccountExpectation("empty", {}, 2500.0, 250.0)}
GOOD = (AccountState("G", "golden", {"AAPL": 100.0}, 0, 51000.0), AccountState("E", "empty", {}, 0, 2500.0))


def test_fixture_verified_pass_and_working_order_abort():
    assert check_fixture(GOOD, FIX).passed
    r = check_fixture((GOOD[0], AccountState("E", "empty", {}, 1, 2500.0)), FIX)
    assert r.run_reason == "unexpected_working_order" and r.unreached.gate == "fixture_verified"
    assert r.has("kill_tree") and r.alert.is_safety and r.incident == "S1"


def test_fixture_golden_drift_and_reseed_never_reset():
    drift = check_fixture((AccountState("G", "golden", {"AAPL": 99.0}, 0, 50000.0), GOOD[1]), FIX)
    assert drift.run is None and not drift.has("reset_empty_account") and drift.has("maintenance_task")
    assert drift.outcomes[0].selector == "account_state:golden"
    assert drift.outcomes[0].outcome == Outcome("env_error", "fixture_drift", "fixture_verified")
    reseed = check_fixture(GOOD, FIX, reseed_due_passed=True)
    assert reseed.outcomes[0].outcome.reason == "fixture_maintenance_needed"


def test_fixture_empty_drift_resets_once_then_reverifies():
    drifted = (GOOD[0], AccountState("E", "empty", {"CEI": 10.0}, 0, 2500.0))
    ask = check_fixture(drifted, FIX)
    assert ask.has("reset_empty_account") and ask.run is None and not ask.outcomes
    assert check_fixture(drifted, FIX, after_reset=GOOD).passed
    still = check_fixture(drifted, FIX, after_reset=drifted)
    assert still.outcomes[0].selector == "account_state" and still.outcomes[0].outcome.reason == "fixture_drift"


def test_canary_flow_passed():
    assert check_canary_flow(CanaryFlowObservation(2, True, True, True)).passed
    for obs in (CanaryFlowObservation(3, True, True, True), CanaryFlowObservation(1, False, True, True),
                CanaryFlowObservation(None, True, True, True), CanaryFlowObservation(1, True, True, True, False)):
        r = check_canary_flow(obs)
        assert r.unreached == Outcome("env_error", "gate", "canary_flow_passed", r.unreached.detail)
    ocr = check_canary_flow(CanaryFlowObservation(1, True, True, False))
    assert ocr.passed and ocr.outcomes[0].selector == "ocr_assertion"
    assert ocr.outcomes[0].outcome == Outcome("harness_error", "ocr_calibration")


# -- teardown and reported --------------------------------------------------------------------------------
def test_teardown_outcomes():
    assert check_teardown(TeardownObservation()).passed
    leak = check_teardown(TeardownObservation(secret_found=True))
    assert leak.run_reason == "secret_leaked" and leak.has("withhold_report") and leak.incident == "S1"
    blind = check_teardown(TeardownObservation(secret_scan_ran=False))
    assert blind.run_reason == "secret_scan_unavailable" and blind.has("withhold_report")
    fault = check_teardown(TeardownObservation(logout_ok=False))
    assert fault.run_status == "complete_with_env_errors" and fault.run_reason == "teardown_fault"
    unverified = check_teardown(TeardownObservation(cancel_all_verified=False, logout_ok=False))
    assert not unverified.passed and unverified.run is None


def test_reported_retries_until_the_deadline_then_dead_man():
    assert check_reported(True, now=0, deadline=10, run_type="pr").passed
    retry = check_reported(False, now=5, deadline=10, run_type="pr")
    assert not retry.passed and retry.run is None
    dm = check_reported(False, now=10, deadline=10, run_type="merge")
    assert dm.run_reason == "dead_man" and dm.unreached == Outcome("harness_error", "dead_man")


# -- run outcome rules ------------------------------------------------------------------------------------
def test_run_outcome_rules():
    assert decide_run_outcome([], any_env_error=False) == RunVerdict("complete")
    assert decide_run_outcome([None], any_env_error=True) == RunVerdict("complete_with_env_errors")
    assert decide_run_outcome([], any_env_error=False, any_fingerprint_mismatch=True).status == \
        "complete_with_env_errors"
    unproven = RunVerdict("complete_with_env_errors", "unproven_proof")
    fault = RunVerdict("complete_with_env_errors", "teardown_fault")
    assert decide_run_outcome([fault, unproven], any_env_error=False) == unproven
    assert decide_run_outcome([fault], any_env_error=True) == RunVerdict("complete_with_env_errors")
    safety = RunVerdict("aborted_safety", "undeclared_order_row")
    harness = RunVerdict("aborted_harness", "dead_man")
    budget = RunVerdict("aborted_budget", "dollar_cap")
    assert decide_run_outcome([budget, harness, unproven], any_env_error=True) == harness
    assert decide_run_outcome([budget, safety, harness], any_env_error=False) == safety
    with pytest.raises(ValueError):
        decide_run_outcome([], any_env_error=False, scope_finished=False)


# -- the typed results ------------------------------------------------------------------------------------
def test_results_validate_against_the_taxonomy_and_1_10():
    with pytest.raises(TaxonomyError):
        Outcome("env_error", "gate")                                  # env_error needs a gate (F65)
    with pytest.raises(TaxonomyError):
        Outcome("not_run", "made_up")
    with pytest.raises(TaxonomyError):
        RunVerdict("aborted_safety")                                 # an abort needs a reason
    with pytest.raises(TaxonomyError):
        ControlResult("x", False, "running", run=RunVerdict("aborted_safety", "kill_switch"),
                      unreached=Outcome("env_error", "attempted_send", "running"))
    with pytest.raises(TaxonomyError):
        ControlResult("x", False, "running", run=RunVerdict("aborted_harness", "dead_man"),
                      unreached=Outcome("not_run", "budget"))
    with pytest.raises(TaxonomyError):
        ControlResult("x", False, "nowhere")
    with pytest.raises(ValueError):
        ControlResult("x", True, actions=("launch_missiles",))
    with pytest.raises(TaxonomyError):
        FindingSpec("not_a_kind", "d")
    with pytest.raises(ValueError):
        alerts.AlertRoute("x", "P4")
    f = FindingSpec("app_crash", "jvm crash", tier=1).to_finding("F-1", "pr-1")
    assert f.kind == "app_crash" and f.first_seen_run_id == "pr-1"


def test_write_unreached_goes_through_the_sink_interface():
    sink = MemorySink(["census/a.b/ws.c", "census/a.d/ws.c"])
    sink.record("census/a.b/ws.c", "identical")
    r = check_authenticated(LoginObservation(vault_lease=False))
    assert write_unreached(sink, "pr-1", r) == ["census/a.d/ws.c"]
    assert sink.outcomes["census/a.d/ws.c"][:3] == ("env_error", "vault_unreachable", "authenticated")
    assert write_unreached(sink, "pr-1", ControlResult("ok", True)) == []
