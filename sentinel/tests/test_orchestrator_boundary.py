"""The flow and step boundary checks of the Orchestrator on the simulated rig (5.2, 5.14, 6.4, 6.6, 10.2, 10.7, 11.2)."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from sentinel.orchestrator.boundary import (BoundaryObservations, FlowTimer, flow_budget_s, new_order_rows,
                                            working_orders_precondition)
from sentinel.orchestrator.budgets import RATE_CAPS, RateCounters, WallBudget
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.degraded import ModelAvailability
from sentinel.orchestrator.identity import deferral_unreadable, paper_stamp, stamp_current
from sentinel.orchestrator.killswitch import ORDERS_SENT, PROVEN_NONE, TripWires
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.ports import SessionSafety
from sentinel.orchestrator.results import Outcome, SubsetOutcome
from sentinel.orchestrator.rig import OrderBook, OrderRow, SelectorRead
from sentinel.orchestrator.simrig import EMPTY, MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.records import CheckpointOutcome

SCOPE = ["census/trade.order-entry.stock/ws.tier-a.active-trader",
         "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07",
         "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"]
GB = 1024 ** 3
STRAY = OrderRow("stray-1", "working", "WORKING", "CEI", 1.0)


def run(work=None, scenario: SimScenario | None = None, **kw):
    clock = ManualClock()
    rig = SimulatedRig(scenario, clock=clock)
    sink = MemorySink(SCOPE)
    ctx = kw.pop("ctx", default_context())
    orch = Orchestrator(rig, ctx, sink, clock=clock, **kw)
    report = orch.run(work)
    return report, rig, sink, orch


def record(orch: Orchestrator, *cps: str) -> None:
    for c in cps:
        orch.sink.record(c, "identical")


def rest(sink: MemorySink, from_index: int) -> set:
    return {sink.outcomes[c][:3] for c in SCOPE[from_index:]}


def results(report, control: str) -> list:
    return [r for r in report.results if r.control == control]


# -- the run budgets (5.2, 11.2, 6.4) -------------------------------------------------------------------------
def test_the_wall_budget_per_run_type_and_the_report_margin():
    assert [WallBudget.for_run(t, 0.0).budget_s for t in ("pr", "merge", "rc", "nightly")] == [2700, 10800, 28800,
                                                                                                14400]
    assert WallBudget.for_run("merge", 100.0).deadline_mono == 100.0 + 3 * 3600 - 600
    start = ManualClock().now()
    cal = WallBudget.for_run("calibration", 0.0, started_at=start, ends_at=start + timedelta(hours=5))
    assert cal.budget_s == 5 * 3600
    with pytest.raises(ValueError):
        WallBudget.for_run("calibration", 0.0)
    with pytest.raises(ValueError):
        WallBudget.for_run("weekly", 0.0)
    with pytest.raises(ValueError):                    # a Calibration Orchestrator needs its window end
        Orchestrator(SimulatedRig(), default_context(run_type="calibration"), MemorySink(SCOPE), clock=ManualClock())


def test_a_pr_run_past_35_minutes_ends_aborted_budget_wall_clock_at_the_next_flow_boundary():
    def work(orch: Orchestrator) -> None:
        record(orch, SCOPE[0])
        orch.clock.advance(orch.wall.deadline_mono - orch.clock.monotonic() - 1)
        assert orch.flow_boundary(flow_id="flow.a") is None             # one second before the run deadline
        orch.clock.advance(1)
        r = orch.flow_boundary(flow_id="flow.b")
        assert r is orch.stop and r.run_reason == "wall_clock"
    report, rig, sink, orch = run(work)
    assert orch.wall.deadline_mono - orch.wall.started_mono == 35 * 60
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "wall_clock")
    assert sink.outcomes[SCOPE[0]][0] == "identical" and rest(sink, 1) == {("not_run", "budget", None)}
    assert "teardown" in rig.calls and report.proof.verdict == PROVEN_NONE


def test_sixty_orders_staged_in_an_hour_end_the_run_rate_cap_and_notify_the_system_owner():
    def work(orch: Orchestrator) -> None:
        now = orch.clock.now()
        for i in range(58):                                            # 58, plus A9's drill ticket at the gate
            orch.counters.order_staged(now - timedelta(minutes=50) + timedelta(seconds=i))
        orch.counters.order_staged(now - timedelta(hours=2))          # outside the hour
        assert orch.counters.counts(now)["orders_staged_per_hour"] == 59
        assert orch.flow_boundary(flow_id="flow.a") is None
        record(orch, SCOPE[0])
        orch.counters.order_staged(orch.clock.now())
        r = orch.flow_boundary(flow_id="flow.b")
        assert r.run_reason == "rate_cap" and "system_owner" in r.alert.notified
        assert "orders_staged_per_hour 60 of 60" in r.detail
    report, _, sink, _ = run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "rate_cap")
    assert rest(sink, 1) == {("not_run", "budget", None)}


def test_the_runs_own_login_counts_against_forty_logins_a_day():
    now = ManualClock().now()
    earlier = [now - timedelta(minutes=30 * i) for i in range(1, 40)]        # 39 in the last 24 hours

    def work(orch: Orchestrator) -> None:
        if orch.flow_boundary(flow_id="flow.a") is None:
            record(orch, *SCOPE)
    report, _, _, orch = run(work, counters=RateCounters(logins=earlier + [now - timedelta(hours=30)]))
    assert orch.counters.counts(now)["logins_per_day"] == 40 and len(orch.counters.logins) == 41
    assert report.verdict.reason == "rate_cap"
    report, _, _, _ = run(work, counters=RateCounters(logins=earlier[:38]))
    assert report.verdict.status == "complete"


def test_a_reached_cap_stops_the_login_and_the_a9_ticket_before_they_are_made():
    """session:3 (a): with 40 logins today the gate makes no 41st login; with 60 staged orders this hour the
    attestation stages no A9 ticket. Both end the run aborted_budget (rate_cap) at once."""
    now = ManualClock().now()
    report, rig, _, orch = run(record, counters=RateCounters(logins=[now - timedelta(minutes=5)] * 40))
    assert "login" not in rig.calls and len(orch.counters.logins) == 40
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "rate_cap")
    assert report.proof.verdict == "PROVEN: no session established"
    report, rig, _, orch = run(record, counters=RateCounters(orders_staged=[now - timedelta(minutes=5)] * 60))
    assert "confirmation_check" not in rig.calls and len(orch.counters.orders_staged) == 60
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "rate_cap")


def test_order_stage_steps_inside_a_flow_stop_at_the_cap_and_the_last_event_ends_the_run():
    """session:3 (b): order-stage steps inside one flow are capped before they run, and a cap the last event of the
    run reached still ends it aborted_budget (rate_cap), notifying the system owner."""
    now = ManualClock().now()
    seen = {}

    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        seen["ran"] = 0
        for _ in range(20):
            if orch.step_boundary(order_stage_step=True) is not None:
                break
            seen["ran"] += 1
    report, _, _, orch = run(work, counters=RateCounters(orders_staged=[now - timedelta(minutes=5)] * 50))
    assert seen["ran"] == 9 and orch.counters.counts(orch.clock.now())["orders_staged_per_hour"] == 60   # 50 + A9 + 9
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "rate_cap")

    def last(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None and orch.step_boundary(order_stage_step=True) is None
        record(orch, *SCOPE)
    report, _, _, _ = run(last, counters=RateCounters(orders_staged=[now - timedelta(minutes=5)] * 58))
    (cap,) = results(report, "run_budget")
    assert report.verdict.reason == "rate_cap" and "system_owner" in cap.alert.notified


def test_rate_caps_from_the_authorization_document_replace_the_defaults():
    caps = {**RATE_CAPS, "symbol_changes_per_hour": 2}

    def work(orch: Orchestrator) -> None:
        orch.counters.symbol_change(orch.clock.now())
        assert orch.flow_boundary() is None
        orch.counters.symbol_change(orch.clock.now())
        assert orch.flow_boundary().run_reason == "rate_cap"
    report, _, _, _ = run(work, counters=RateCounters(caps=caps))
    assert report.verdict.reason == "rate_cap"


def test_a_superseding_rc_build_and_the_spend_ceiling_end_the_run_at_the_flow_boundary():
    report, _, sink, _ = run(lambda o: o.flow_boundary(BoundaryObservations(superseded=True)),
                             ctx=default_context(run_type="rc"))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "superseded")

    def spend(orch: Orchestrator) -> None:
        record(orch, SCOPE[0])
        assert orch.spend.add(50.0).has("stop_model_calls") and orch.stop is None
        r = orch.flow_boundary(flow_id="flow.b")
        assert r.run_reason == "dollar_cap"
    report, rig, sink, _ = run(spend)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_budget", "dollar_cap")
    assert rest(sink, 1) == {("not_run", "budget", None)} and "teardown" in rig.calls


# -- the Watchdog's reads (10.2, 5.14) --------------------------------------------------------------------------
def test_a_stale_watchdog_json_at_a_flow_boundary_is_watchdog_dead_with_teardown_and_proof():
    status = [(2.0, True)]

    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        record(orch, SCOPE[0])
        status[0] = (31.0, True)                     # the callback the gates read serves the boundary too
        assert orch.flow_boundary(flow_id="flow.b").run_reason == "watchdog_dead"
    report, rig, sink, _ = run(work, watchdog_status=lambda: status[0])
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "watchdog_dead")
    assert rest(sink, 1) == {("harness_error", "watchdog_dead", None)}
    assert "teardown" in rig.calls and report.proof.verdict == PROVEN_NONE
    report, _, _, _ = run(lambda o: o.flow_boundary(BoundaryObservations(watchdog=(5.0, False))))
    assert report.verdict.reason == "watchdog_dead"


def test_free_space_below_2_gb_is_disk_full_and_below_the_floor_a_warning_once():
    report, _, sink, _ = run(lambda o: o.flow_boundary(BoundaryObservations(free_bytes=GB)))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "disk_full")
    assert rest(sink, 0) == {("harness_error", "disk_full", None)}

    def low(orch: Orchestrator) -> None:
        for _ in range(3):
            assert orch.flow_boundary(BoundaryObservations(free_bytes=10 * GB, disk_floor_bytes=20 * GB)) is None
        record(orch, *SCOPE)
    report, _, _, _ = run(low)
    warnings = results(report, "disk")
    assert report.verdict.status == "complete" and len(warnings) == 1 and warnings[0].passed


def test_the_check_order_trip_wires_then_watchdog_then_disk_then_budgets():
    def first_stop(obs: BoundaryObservations, advance: float = 0.0) -> str:
        def work(orch: Orchestrator) -> None:
            orch.clock.advance(advance)
            orch.flow_boundary(obs)
        report, _, _, _ = run(work)
        return report.gate("running").control if report.gate("running") else ""
    late = 40 * 60
    assert first_stop(BoundaryObservations(tripwires=TripWires(file_flag=True), watchdog=(60.0, True)), late) == \
        "kill_switch"
    assert first_stop(BoundaryObservations(watchdog=(60.0, True), free_bytes=GB), late) == "watchdog"
    assert first_stop(BoundaryObservations(free_bytes=GB), late) == "disk"
    assert first_stop(BoundaryObservations(superseded=True), late) == "run_budget"


# -- the kill switch's trip-wires (6.6) -------------------------------------------------------------------------
def test_a_new_working_row_after_a_flow_trips_the_kill_switch_at_the_next_boundary():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        record(orch, SCOPE[0])
        orch.rig.book = replace(orch.rig.book, working=(STRAY,))       # an order the gateway never saw
        r = orch.flow_boundary(flow_id="flow.b")
        assert r.control == "kill_switch" and r.evidence["new_rows"] == ["stray-1"]
        assert r.has("quarantine_flow") and "flow.a" in r.detail and orch.rig.gw.deny_all
    report, rig, sink, _ = run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "kill_switch")
    assert rest(sink, 1) == {("env_error", "kill_switch", "running")}
    assert [s for s, _ in report.kill_switch.steps] == ["deny_all", "kill_tree", "recovery_login", "cancel_all",
                                                        "proof", "revert_snapshot"]
    assert report.proof.verdict == ORDERS_SENT and [r.key for r in report.proof.undeclared] == ["stray-1"]


def test_declared_sends_are_not_new_working_rows_but_a_fill_always_is():
    declared = BoundaryObservations(declared_sends=frozenset({"stray-1"}))

    def work(orch: Orchestrator) -> None:
        orch.rig.book = replace(orch.rig.book, working=(STRAY,))
        assert orch.flow_boundary(declared) is None
        orch.rig.book = replace(orch.rig.book, filled=(replace(STRAY, grid="filled", status="FILLED"),))
        assert orch.flow_boundary(declared).control == "kill_switch"
    report, _, _, _ = run(work)
    assert report.verdict.status == "aborted_safety"
    start = OrderBook(net_liq=1.0)
    assert new_order_rows(start, OrderBook(working=(STRAY,), readable=False)) == ()
    assert new_order_rows(start, OrderBook(working=(STRAY,), copy_all_agrees=False)) == ()
    assert new_order_rows(None, OrderBook(working=(STRAY,))) == ()


def test_an_order_event_window_and_the_human_channels_trip_at_a_step_boundary():
    for wires in (TripWires(order_event_window=True), TripWires(named_pipe=True), TripWires(attempted_send=True)):
        def work(orch: Orchestrator, wires=wires) -> None:
            assert orch.flow_boundary(flow_id="flow.a") is None
            assert orch.step_boundary(BoundaryObservations(tripwires=wires)) is orch.stop
        report, _, _, _ = run(work)
        assert report.verdict.status == "aborted_safety" and report.kill_switch is not None


def test_an_unowned_order_confirmation_open_more_than_30_seconds_trips_owned_or_explorer_does_not():
    def dialog(wires: TripWires = TripWires(), *, close_between: bool = False):
        def work(orch: Orchestrator) -> None:
            assert orch.flow_boundary(flow_id="flow.a") is None
            for gap, visible in ((0, True), (20, not close_between), (11, True), (1, True)):
                orch.clock.advance(gap)
                if orch.step_boundary(BoundaryObservations(tripwires=wires, order_dialog_visible=visible)):
                    return
            record(orch, *SCOPE)
        return run(work)[0]
    tripped = dialog()                                        # open at 0 s, still open 32 s later
    assert tripped.verdict.reason == "kill_switch" and "dialog_open_30s" in tripped.gate("running").detail
    assert dialog(TripWires(dialog_owned=True)).verdict.status == "complete"
    assert dialog(TripWires(explorer_session=True)).verdict.status == "complete"
    assert dialog(close_between=True).verdict.status == "complete"   # closed at 20 s: the timer starts again


# -- the flow's own budget (11.2) -------------------------------------------------------------------------------
def test_a_flow_whose_p50_is_60s_is_cut_at_180s_and_the_run_continues():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a", p50_s=60.0) is None
        assert orch.flow.budget_s == 180.0
        orch.clock.advance(179)
        assert orch.step_boundary() is None
        orch.clock.advance(1)
        cut = orch.step_boundary()
        assert cut.has("end_flow") and orch.stop is None and orch.flow is None
        assert cut.outcomes == (SubsetOutcome("rest_of_flow", Outcome("not_run", "budget")),)
        record(orch, *SCOPE)
        assert orch.flow_boundary(flow_id="flow.b") is None and orch.flow.budget_s == 300.0
    report, _, _, _ = run(work)
    assert report.verdict.status == "complete" and len(results(report, "flow_budget")) == 1
    assert flow_budget_s(None) == 300.0 and flow_budget_s(100.0) == 300.0
    assert FlowTimer("f", 10.0, 30.0).over(40.0) and not FlowTimer("f", 10.0, 30.0).over(39.0)
    with pytest.raises(ValueError):
        flow_budget_s(0.0)


# -- the model re-check (10.7) ----------------------------------------------------------------------------------
def test_the_model_endpoint_is_rechecked_once_per_flow_and_a_recovery_is_logged():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(BoundaryObservations(model_endpoint_ok=False)) is None
        assert not orch.model.agent_available
        assert orch.flow_boundary(BoundaryObservations(model_endpoint_ok=True)) is None
        assert orch.model.agent_available and orch.model.review_mode == "live"
        assert orch.flow_boundary(BoundaryObservations(model_endpoint_ok=True)) is None
        record(orch, *SCOPE)
    report, _, _, orch = run(work, model=ModelAvailability.from_preflight(False, True))
    assert len(results(report, "model_recheck")) == 1 and orch.model.recoveries == 1
    assert report.verdict.status == "complete"


# -- the paper-mode assertion schedule (6.4) --------------------------------------------------------------------
def test_a_foreign_paper_id_at_the_second_flow_boundary_aborts_before_that_flow():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        record(orch, SCOPE[0])
        orch.rig.s.selector_reads.append(SelectorRead("D-77777777"))
        r = orch.flow_boundary(flow_id="flow.b")
        assert r.run_reason == "paper_mode_mismatch" and r.incident == "S2" and orch.flow is None
    report, rig, sink, _ = run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "paper_mode_mismatch")
    assert rest(sink, 1) == {("env_error", "paper_mode_mismatch", "paper_mode_asserted")}
    assert report.kill_switch is not None and report.proof.verdict == PROVEN_NONE


def test_the_two_minute_timer_defers_under_a_modal_and_a_deferral_past_five_minutes_is_unreadable():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a", p50_s=600.0) is None
        reads = orch.rig.calls.count("read_account_selector")
        orch.clock.advance(100)
        assert orch.step_boundary() is None                                      # not due yet
        orch.clock.advance(21)
        assert orch.step_boundary(BoundaryObservations(modal_up=True)) is None   # due, deferred
        assert orch.step_boundary(BoundaryObservations(main_foreground=False)) is None
        assert orch.rig.calls.count("read_account_selector") == reads
        assert orch.step_boundary() is None                                      # foreground, no modal: read now
        assert orch.rig.calls.count("read_account_selector") == reads + 1
        orch.clock.advance(120 + 301)
        assert orch.step_boundary(BoundaryObservations(modal_up=True)).unreached.reason == "identity_unreadable"
    report, rig, sink, _ = run(work)
    assert report.verdict.status == "complete_with_env_errors"
    assert rest(sink, 0) == {("env_error", "identity_unreadable", "paper_mode_asserted")}
    assert "set_hook_state:True:True" in rig.calls and "kill_process_tree" in rig.calls
    assert "teardown" not in rig.calls                                           # the recovery proof instead


def test_an_order_stage_step_and_a_session_dialog_are_asserted_at_once_and_reads_are_counted():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        assert len(orch.counters.orders_staged) == 1                            # A9's drill ticket at the gate
        assert orch.step_boundary(order_stage_step=True) is None
        assert len(orch.counters.orders_staged) == 2                            # the order-stage step about to run
        assert orch.step_boundary(session_dialog=True) is None
        assert orch.step_boundary() is None                                      # timer not due: no read
        record(orch, *SCOPE)
    report, rig, _, orch = run(work)
    passed = [r for r in results(report, "paper_mode") if r.passed]
    assert len(passed) == rig.calls.count("read_account_selector") == 4         # gate, flow, order stage, dialog
    assert len(orch.counters.orders_staged) == 2


def test_an_unreadable_first_read_at_a_boundary_rereads_under_deny_all_then_rearms():
    blind = SelectorRead(None, landmark_found=False)

    def work(orch: Orchestrator) -> None:
        orch.rig.s.selector_reads.append(blind)
        assert orch.flow_boundary(flow_id="flow.a") is None
        assert not orch.rig.gw.deny_all
        record(orch, *SCOPE)
    report, rig, _, _ = run(work)
    assert report.verdict.status == "complete" and "set_hook_state:True:True" in rig.calls


def test_working_orders_precondition_above_expect_is_aborted_safety():
    assert working_orders_precondition(0) is None and working_orders_precondition(1, expect=1) is None
    r = working_orders_precondition(1, flow_id="flow.a")
    assert (r.run_status, r.run_reason, r.incident) == ("aborted_safety", "unexpected_working_order", "S1")
    assert r.unreached == Outcome("env_error", "unexpected_working_order", "running", r.unreached.detail)
    assert r.has("kill_switch")
    with pytest.raises(ValueError):
        working_orders_precondition(-1)


# -- the proactive relaunch (10.2) ------------------------------------------------------------------------------
def test_41_flows_trigger_one_proactive_relaunch_that_spends_no_budget():
    def work(orch: Orchestrator) -> None:
        for i in range(41):
            assert orch.flow_boundary(flow_id=f"flow.{i}") is None
        assert orch.flows_since_launch == 1
        record(orch, *SCOPE)
    report, rig, _, orch = run(work)
    relaunches = results(report, "relaunch")
    assert len(relaunches) == 1 and relaunches[0].evidence["cause"] == "proactive"
    assert relaunches[0].evidence["budget"] is None and relaunches[0].evidence["flows_since_launch"] == 40
    assert orch.budgets.used_app == 0 and orch.relogin.used == 0
    assert rig.calls.count("launch") == 2 and rig.calls.count("confirmation_check") == 2
    assert report.verdict.status == "complete"


def test_ninety_minutes_of_app_uptime_trigger_the_proactive_relaunch_too():
    def work(orch: Orchestrator) -> None:
        assert orch.flow_boundary(flow_id="flow.a") is None
        orch.clock.advance(90 * 60)
        assert orch.flow_boundary() is None                    # a census visit: no flow counted
        assert orch.flows_since_launch == 0 and orch.clock.monotonic() - orch.launched_mono < 90 * 60
        record(orch, *SCOPE)
    report, _, _, _ = run(work, ctx=default_context(run_type="merge"))
    assert [r.evidence["cause"] for r in results(report, "relaunch")] == ["proactive"]


# -- the paper-asserted stamp and the session's safety state (6.4, SD-57) -------------------------------------
def test_the_paper_stamp_belongs_to_the_current_session():
    def work(orch: Orchestrator) -> None:
        stamp = orch.paper_stamp()
        assert stamp == {"account_id_sha256": "sha256:" + hashlib.sha256(EMPTY.encode()).hexdigest(),
                         "mode": "paperMoney", "asserted_at": orch.clock.now().isoformat()}
        CheckpointOutcome(run_id="pr-48213-3", checkpoint_id=SCOPE[0], outcome="identical",
                          workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="frozen",
                          paper_assertion=stamp)
        assert orch.may_release(stamp) and not orch.may_release(None)
        assert orch.relaunch("proactive")
        assert not orch.may_release(stamp)                     # a stamp of the session before the relaunch
        fresh = orch.paper_stamp()
        assert orch.may_release(fresh) and fresh["asserted_at"] > stamp["asserted_at"]
        record(orch, *SCOPE)
    report, _, _, orch = run(work)
    assert report.verdict.status == "complete" and orch.paper_stamp() is None and not orch.may_release(None)


def test_stamp_helpers_and_the_deferral_result():
    t = ManualClock().now()
    stamp = paper_stamp(EMPTY, t)
    assert stamp_current(stamp, t) and not stamp_current(stamp, t + timedelta(seconds=1))
    assert not stamp_current(None, t) and not stamp_current(stamp, None)
    assert not stamp_current({**stamp, "mode": "live"}, t) and not stamp_current({**stamp, "asserted_at": "x"}, t)
    with pytest.raises(ValueError):
        paper_stamp(EMPTY, t.replace(tzinfo=None))
    r = deferral_unreadable(deferred_s=301.0, unreadable_streak=1)
    assert (r.run_status, r.unreached) == ("complete_with_env_errors",
                                           Outcome("env_error", "identity_unreadable", "paper_mode_asserted"))
    assert r.has("deny_all") and r.has("kill_tree") and r.has("block_login") and "301 s" in r.detail


def test_session_safety_is_set_through_the_gates_and_cleared_when_the_session_ends():
    seen: list[SessionSafety] = []
    at_a9: list[str] = []

    class Rig(SimulatedRig):
        def confirmation_check(self):
            at_a9.append(self.orch.safety.attestation_stage)
            return super().confirmation_check()

    clock = ManualClock()
    rig = Rig(clock=clock)
    orch = Orchestrator(rig, default_context(), MemorySink(SCOPE), clock=clock, safety_listener=seen.append)
    rig.orch = orch
    stages = []

    def work(o: Orchestrator) -> None:
        stages.append(o.safety)
        record(o, *SCOPE)
    report = orch.run(work)
    assert report.verdict.status == "complete" and at_a9 == ["a3_a8"]
    running = stages[0]
    assert running.attestation_stage == "a3_a9" and running.attested_through("a3_a9")
    assert running.last_assertion_mono is not None and running.account_id_sha256.startswith("sha256:")
    assert [s.attestation_stage for s in seen if s.attestation_stage != "none"] == ["a3_a8", "a3_a9"]
    assert orch.safety == SessionSafety() and seen[-1] == SessionSafety()
