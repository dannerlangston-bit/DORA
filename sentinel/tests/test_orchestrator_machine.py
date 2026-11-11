"""The Orchestrator end to end on the simulated rig: each gate's pass and failure outcomes, teardown and proof (5.2)."""
from __future__ import annotations

import pytest

from sentinel.orchestrator.budgets import SESSION_RELEASE_WAIT_S, ShardBudgets, session_kicked
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.degraded import SpendTracker
from sentinel.orchestrator.gateway import Action, refusal
from sentinel.orchestrator.killswitch import ORDERS_SENT, PROVEN_NO_SESSION, PROVEN_NONE, UNPROVEN
from sentinel.orchestrator.machine import LoginHistory, Orchestrator
from sentinel.orchestrator.rig import (AccountState, CanaryFlowObservation, ConfirmationCheck, CrashSignal,
                                       LaunchObservation, LoginObservation, SelectorRead, TeardownObservation,
                                       WorkspaceObservation)
from sentinel.orchestrator.simrig import EMPTY, GOLDEN, MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.orchestrator.watchdog import HeartbeatWriter, app_failure, hook_host_died

SCOPE = ["census/trade.order-entry.stock/ws.tier-a.active-trader",
         "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07",
         "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"]


def run(scenario: SimScenario | None = None, work=None, **kw):
    clock = ManualClock()
    rig = SimulatedRig(scenario, clock=clock)
    sink = MemorySink(SCOPE)
    ctx = kw.pop("ctx", default_context())
    orch = Orchestrator(rig, ctx, sink, clock=clock, **kw)
    report = orch.run(work)
    return report, rig, sink, orch


def all_work(orch: Orchestrator) -> None:
    for c in SCOPE:
        orch.sink.record(c, "identical")


def filled(sink: MemorySink) -> set:
    return {v[:3] for v in sink.outcomes.values()}


def test_clean_run_passes_every_gate_and_proves_no_orders_sent():
    report, rig, sink, _ = run(work=all_work)
    assert report.verdict.status == "complete" and report.proof.verdict == PROVEN_NONE
    assert [r.gate for r in report.results][:8] == ["provisioned", "launched", "authenticated", "paper_mode_asserted",
                                                    "settings_attested", "workspace_loaded", "fixture_verified",
                                                    "canary_flow_passed"]
    assert report.filled == [] and report.last_gate == "teardown"
    assert "load_order_chords" in rig.calls and rig.gw.chords
    assert report.to_run_outcome().status == "complete"


def test_preflight_h_item_aborts_harness_and_fills_every_checkpoint():
    report, rig, sink, _ = run(SimScenario(preflight_failures={"H4": None}))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "preflight.H4")
    assert filled(sink) == {("harness_error", "preflight.H4", None)} and "launch" not in rig.calls
    assert report.proof.verdict == PROVEN_NO_SESSION


def test_preflight_r_item_is_env_error_on_every_checkpoint():
    report, _, sink, _ = run(SimScenario(preflight_failures={"R1": None}))
    assert report.verdict.status == "complete_with_env_errors" and report.verdict.reason is None
    assert filled(sink) == {("env_error", "gate", "preflight.R1")}


def test_launched_wrong_build():
    report, _, sink, _ = run(SimScenario(launches=[LaunchObservation(True, "2026.43.0")]))
    assert filled(sink) == {("env_error", "version_mismatch", "launched")}
    assert report.verdict.status == "complete_with_env_errors"


def test_first_launch_crash_relaunches_then_four_crashes_spend_the_budget():
    crash = LaunchObservation(False, crash=CrashSignal("jvm_crash", "crash_file"))
    report, rig, _, orch = run(SimScenario(launches=[crash]), work=all_work)
    assert report.verdict.status == "complete" and orch.budgets.used_app == 1 and rig.calls.count("launch") == 2
    assert report.results[1].checkpoint.reason == "assertion:app_alive"
    report, rig, sink, _ = run(SimScenario(launches=[crash] * 4))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "app_restart_budget")
    assert filled(sink) == {("not_run", "budget", None)} and report.proof.verdict == PROVEN_NO_SESSION


def test_authentication_failures_and_the_no_session_proof():
    report, _, sink, _ = run(SimScenario(login=LoginObservation(vault_lease=False)))
    assert filled(sink) == {("env_error", "vault_unreachable", "authenticated")}
    assert report.proof.verdict == PROVEN_NO_SESSION and report.verdict.status == "complete_with_env_errors"


def test_session_kicked_waits_and_logs_in_once_more_for_the_proof():
    report, rig, sink, _ = run(SimScenario(login=LoginObservation(session_kicked=True)))
    assert filled(sink) == {("env_error", "session_kicked", "authenticated")}
    assert rig.calls.count("login") == 2 and report.proof.reason == "snapshot_missing"
    report, rig, _, _ = run(SimScenario(login=LoginObservation(session_kicked=True), recovery_login_fails=2))
    assert (report.proof.verdict, report.proof.reason) == (UNPROVEN, "session_unavailable")
    assert rig.calls.count("login") == 3 and report.gate("teardown").has("block_login")


def test_a_session_kicked_mid_run_waits_logs_in_once_more_and_proves_normally():
    seen = {}

    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        seen["t0"], seen["calls"] = orch.clock.monotonic(), len(orch.rig.calls)
        orch.apply(session_kicked())                              # POP-04 during a flow
    report, rig, sink, orch = run(work=work)
    assert orch.session == "kicked" and orch.safety.session_kicked
    assert report.verdict.status == "complete_with_env_errors" and report.proof.verdict == PROVEN_NONE
    assert {sink.outcomes[c][:3] for c in SCOPE[1:]} == {("env_error", "session_kicked", "authenticated")}
    after = rig.calls[seen["calls"]:]
    assert after[:3] == ["kill_process_tree", "launch", "login"] and "teardown" not in after
    assert orch.clock.monotonic() - seen["t0"] >= SESSION_RELEASE_WAIT_S
    report, rig, _, _ = run(SimScenario(recovery_login_fails=2), work=work)
    assert (report.proof.verdict, report.proof.reason) == (UNPROVEN, "session_unavailable")
    assert rig.calls.count("login") == 3 and report.gate("teardown").has("block_login")


def test_an_unreadable_read_after_a2_selects_the_account_takes_the_identity_unreadable_path():
    blind = SelectorRead(None, landmark_found=False)
    golden = SelectorRead(GOLDEN, crop_sha256="crop-a1")
    report, rig, sink, _ = run(SimScenario(selected=GOLDEN, selector_reads=[golden, blind, blind]))
    assert filled(sink) == {("env_error", "identity_unreadable", "paper_mode_asserted")}
    assert report.verdict.status == "complete_with_env_errors" and "set_hook_state:True:True" in rig.calls
    report, _, _, _ = run(SimScenario(selected=GOLDEN, selector_reads=[golden, blind]), work=all_work)
    assert report.verdict.status == "complete"                   # the second read 1 s later is readable


def test_live_identity_runs_the_kill_switch_and_stays_aborted_safety():
    report, rig, sink, _ = run(SimScenario(selector_reads=[SelectorRead("12345678SCHW")]))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "live_identity")
    assert filled(sink) == {("env_error", "live_identity", "paper_mode_asserted")}
    assert report.kill_switch is not None and rig.gw.deny_all and "kill_process_tree" in rig.calls
    assert report.proof.verdict == UNPROVEN and report.proof.reason == "snapshot_missing"


def test_paper_mode_mismatch_and_unreadable_identity():
    report, _, sink, _ = run(SimScenario(selector_reads=[SelectorRead("D-77777777")]))
    assert report.verdict.reason == "paper_mode_mismatch"
    blind = SelectorRead(None, landmark_found=False)
    report, rig, sink, _ = run(SimScenario(selector_reads=[blind, blind]))
    assert filled(sink) == {("env_error", "identity_unreadable", "paper_mode_asserted")}
    assert report.verdict.status == "complete_with_env_errors" and report.proof.verdict == PROVEN_NO_SESSION
    assert "set_hook_state:True:True" in rig.calls and "kill_process_tree" in rig.calls
    report, _, _, _ = run(SimScenario(selector_reads=[blind, blind]),
                          ctx=default_context(history=LoginHistory(identity_unreadable_streak=1)))
    assert report.gate("paper_mode_asserted").has("block_login")


def test_a2_selects_the_fixture_account():
    report, rig, _, _ = run(SimScenario(selected=GOLDEN), work=all_work)
    assert report.verdict.status == "complete" and "select_account" in rig.calls
    report, _, sink, _ = run(SimScenario(selected=GOLDEN, select_works=False))
    assert filled(sink) == {("env_error", "gate", "fixture_verified")}


def test_attestation_mismatch_aborts_and_proves_from_the_snapshot():
    report, rig, sink, _ = run(SimScenario(settings={**SimScenario().settings, "A4": True}))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "attestation_mismatch")
    assert filled(sink) == {("env_error", "attestation_mismatch", "settings_attested")}
    assert report.proof.verdict == PROVEN_NONE and "confirmation_check" not in rig.calls


def test_attestation_unreadable_rereads_once_then_env_error():
    report, rig, _, _ = run(SimScenario(unreadable_reads={"A3": 1}), work=all_work)
    assert report.verdict.status == "complete" and "reopen_settings" in rig.calls
    report, _, sink, _ = run(SimScenario(unreadable_reads={"A3": 2}))
    assert filled(sink) == {("env_error", "attestation_unreadable", "settings_attested")}
    assert report.verdict.status == "complete_with_env_errors" and report.proof.verdict == PROVEN_NONE


def test_a9_new_row_is_the_kill_switch():
    report, _, _, _ = run(SimScenario(confirmation=ConfirmationCheck(False, 1)))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "kill_switch")


def test_workspace_fixture_and_canary_gates():
    report, _, sink, _ = run(SimScenario(workspace=WorkspaceObservation("bad", True, (), 1)))
    assert filled(sink) == {("env_error", "gate", "workspace_loaded")}
    working = (AccountState(GOLDEN, "golden", {"AAPL": 100.0, "/MESZ26": 1.0}, 0, 50000.0),
               AccountState(EMPTY, "empty", {}, 1, 2500.0))
    report, _, sink, _ = run(SimScenario(accounts=working))
    assert report.verdict.reason == "unexpected_working_order"
    assert filled(sink) == {("env_error", "unexpected_working_order", "fixture_verified")}
    drift = (working[0], AccountState(EMPTY, "empty", {"CEI": 5.0}, 0, 2500.0))
    clean = (working[0], AccountState(EMPTY, "empty", {}, 0, 2500.0))
    report, rig, _, _ = run(SimScenario(accounts=drift, accounts_after_reset=clean), work=all_work)
    assert report.verdict.status == "complete" and "reset_empty_account" in rig.calls
    report, _, sink, _ = run(SimScenario(canary=CanaryFlowObservation(4, True, True, True)))
    assert filled(sink) == {("env_error", "gate", "canary_flow_passed")}


def test_attempted_send_in_running_aborts_through_the_kill_switch():
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        r = orch.rig.send_input(Action("press", keys=(0x20,), declared_guarded=True))
        assert r.refused_reason is None                     # no flag, declared Space: emitted
        orch.rig.ticket_visible = True
        r = orch.rig.send_input(Action("press", keys=(0x0D,), declared_guarded=True))
        orch.apply(orch.ledger.record(refusal(r.refused), unit="flow.t", order_stage=True))
    report, rig, sink, _ = run(work=work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "attempted_send")
    assert sink.outcomes[SCOPE[0]][0] == "identical"
    assert {sink.outcomes[c][:3] for c in SCOPE[1:]} == {("env_error", "attempted_send", "running")}
    assert report.proof.verdict == PROVEN_NONE and rig.sent_orders == 0 and rig.gw.deny_all


def test_an_order_that_got_out_is_found_by_the_proof():
    def work(orch: Orchestrator) -> None:
        all_work(orch)
        orch.rig._send()                                    # a send the gateway did not see
    report, _, _, _ = run(work=work)
    assert report.proof.verdict == ORDERS_SENT
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "undeclared_order_row")


def test_budget_exhaustion_in_running_tears_down_normally():
    spend = SpendTracker()

    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        spend.add(50.0)
        orch.apply(spend.at_flow_boundary())
    report, rig, sink, _ = run(work=work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "dollar_cap")
    assert {sink.outcomes[c][:2] for c in SCOPE[1:]} == {("not_run", "budget")}
    assert "teardown" in rig.calls and report.proof.verdict == PROVEN_NONE


@pytest.mark.parametrize("td,status,reason", [
    (TeardownObservation(secret_found=True), "aborted_harness", "secret_leaked"),
    (TeardownObservation(logout_ok=False), "complete_with_env_errors", "teardown_fault"),
    (TeardownObservation(cancel_all_verified=False), "complete_with_env_errors", "unproven_proof"),
])
def test_teardown_outcomes(td, status, reason):
    report, _, _, _ = run(SimScenario(teardown=td), work=all_work)
    assert (report.verdict.status, report.verdict.reason) == (status, reason)


def test_monitor_unreadable_at_teardown_is_unproven():
    report, _, _, _ = run(SimScenario(monitor_unreadable=True), work=all_work)
    assert report.proof.reason == "monitor_unreadable"
    assert (report.verdict.status, report.verdict.reason) == ("complete_with_env_errors", "unproven_proof")


def test_heartbeat_and_reverse_heartbeat_at_every_gate(tmp_path):
    clock = ManualClock()
    hb = HeartbeatWriter(tmp_path / "shard-1" / "heartbeat.json", clock)
    report, _, _, _ = run(work=all_work, heartbeat=hb, watchdog_status=lambda: (2.0, True))
    assert report.verdict.status == "complete" and (tmp_path / "shard-1" / "heartbeat.json").exists()
    report, rig, sink, _ = run(watchdog_status=lambda: (45.0, True))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "watchdog_dead")
    assert filled(sink) == {("harness_error", "watchdog_dead", None)} and "preflight" not in rig.calls
    (tmp_path / "file").write_text("x")
    report, _, _, _ = run(heartbeat=HeartbeatWriter(tmp_path / "file" / "hb.json", clock))
    assert report.verdict.reason == "heartbeat_unwritable"


def test_hook_host_and_app_budgets_from_running():
    def work(orch: Orchestrator) -> None:
        orch.sink.record(SCOPE[0], "identical")
        orch.apply(hook_host_died(orch.budgets, step_id="s02"))
        orch.apply(hook_host_died(orch.budgets, step_id="s03"))
    report, rig, sink, orch = run(work=work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "hook_lost")
    assert {sink.outcomes[c][:2] for c in SCOPE[1:]} == {("harness_error", "hook_lost")}
    assert orch.budgets.used_app == 0 and report.proof.verdict == PROVEN_NONE      # recovery login proof

    def crashes(orch: Orchestrator) -> None:
        for _ in range(4):
            orch.apply(app_failure(CrashSignal("hang", "screenshot_staleness"), orch.budgets, phase="running"))
    report, _, sink, _ = run(work=crashes, budgets=ShardBudgets())
    assert report.verdict.reason == "app_restart_budget" and filled(sink) == {("not_run", "budget", None)}


def test_rc_run_reads_a10():
    report, rig, _, _ = run(work=all_work, ctx=default_context(run_type="rc"))
    assert report.verdict.status == "complete" and "read_notifications" in rig.calls
    report, rig, _, _ = run(work=all_work)
    assert "read_notifications" not in rig.calls


# -- through the real store (8.2): the unreached outcomes and run outcomes are ones it accepts -------------
def _store(tmp_path):
    from sentinel.store import RunPlan, ScopeEntry, Store
    ws = "ws.tier-a.active-trader"
    entries = (ScopeEntry(SCOPE[0], ws, "active_trader", 1, "frozen"),
               ScopeEntry(SCOPE[1], ws, "active_trader", 1, "frozen", flow_class="standard"),
               ScopeEntry(SCOPE[2], ws, "active_trader", 2, "frozen"))
    s = Store(tmp_path / "run.db")
    s.begin_run(RunPlan(run_id="pr-48213-3", run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=entries))
    return s


@pytest.mark.parametrize("scenario,status,outcome", [
    (SimScenario(selector_reads=[SelectorRead("12345678SCHW")]), "aborted_safety",
     ("env_error", "live_identity", "paper_mode_asserted")),
    (SimScenario(settings={**SimScenario().settings, "A8": 2}), "aborted_safety",
     ("env_error", "unexpected_working_order", "settings_attested")),
    (SimScenario(preflight_failures={"H7": None}), "aborted_harness", ("harness_error", "preflight.H7", None)),
    (SimScenario(login=LoginObservation(auth_error=True)), "complete_with_env_errors",
     ("env_error", "authentication_failed", "authenticated")),
    (SimScenario(launches=[LaunchObservation(False, crash=CrashSignal("oom", "heap_dump"))] * 4), "aborted_budget",
     ("not_run", "budget", None)),
])
def test_orchestrator_writes_through_the_real_store(tmp_path, scenario, status, outcome):
    clock = ManualClock()
    with _store(tmp_path) as store:
        report = Orchestrator(SimulatedRig(scenario, clock=clock), default_context(), store, clock=clock).run()
        assert report.verdict.status == status
        assert {(o.outcome, o.reason, o.gate) for o in store.outcomes("pr-48213-3")} == {outcome}
        ended = store.end_run(report.to_run_outcome())
        assert (ended.status, ended.reason) == (report.verdict.status, report.verdict.reason)
