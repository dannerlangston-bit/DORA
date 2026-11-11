"""The wave-1 wiring of the Orchestrator over the rig's ports: the kill switch in the hook's phases, the run's end
(declared sends, the hand-over account, the recovery-only arm, `between_runs`), the boundaries' reads from the rig
(human channels first, the Watchdog, free space, the Window Watcher), the relaunch's `reset_input_state`, the session
pushed to the slow half, the order hotkeys, and the incident severities of 6.12 (5.2, 6.2 to 6.7, 7.7, 10.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gateway import Action, Chord, DropLedger, GatewayDrop
from sentinel.orchestrator.gateway_config import (DEFAULT_ORDER_HOTKEYS_PATH, DefaultOrderHotkeys,
                                                  load_default_order_hotkeys)
from sentinel.orchestrator.identity import deferral_unreadable, judge_identity
from sentinel.orchestrator.killswitch import (PROVEN_DECLARED, PROVEN_NONE, UNPROVEN, execute_kill_switch, kill_switch,
                                              live_identity_blocked)
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.ports import KillRequest, SessionSafety
from sentinel.orchestrator.results import RunVerdict
from sentinel.orchestrator.rig import SelectorRead
from sentinel.orchestrator.sendgate import SendPolicy
from sentinel.orchestrator.simrig import CONFIRM_POINT, MemorySink, SimScenario, SimulatedRig, default_context

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SCOPE = ["c1", "c2", "c3"]
DENY_ALL, RECOVERY, BETWEEN_RUNS = "set_hook_state:False:True", "set_hook_state:True:False:recovery_only", \
    "set_hook_state:False:False"
BLIND = SelectorRead(None, landmark_found=False)
CONFIRM = Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send", under_point_text="Confirm and Send",
                 order_stage_step=True)
SEND = Action("click", point=(920, 610), resolved_name="Send", under_point_text="Send", flow_class="paper_send_ok",
              account_role="empty", order_stage_step=True)


def run(scenario: SimScenario | None = None, work=None, *, pushed: bool = True, **kw):
    """One shard on the simulated rig; with `pushed` the session goes to the slow half (`set_session_safety`)."""
    clock = ManualClock()
    rig = SimulatedRig(scenario, clock=clock)
    ctx = kw.pop("ctx", default_context())
    orch = Orchestrator(rig, ctx, MemorySink(SCOPE), clock=clock,
                        safety_listener=rig.set_session_safety if pushed else None, **kw)
    return orch.run(work), rig, orch


def phases(rig: SimulatedRig) -> list[str]:
    return [c for c in rig.calls if c.startswith("set_hook_state")]


# -- the kill switch in the hook's phases (6.6, gateway.py choice 12, SD-64) ---------------------------------
def test_the_sequence_kills_resets_rearms_for_recovery_only_and_disarms_after_the_revert():
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    start = rig.read_orders()
    rig._send()                                            # an order got out: the recovery's Cancel All must click
    ctx = default_context()
    rep = execute_kill_switch(rig, kill_switch("file_flag", gate="running"), clock=clock, allowlist=ctx.allowlist,
                              start=start, fixture_account=ctx.fixture_account)
    c = rig.calls
    assert c.index(DENY_ALL) < c.index("kill_process_tree") < c.index("reset_input_state") < c.index(RECOVERY) \
        < c.index("login") < c.index("cancel_all") < c.index("revert_snapshot") < c.index(BETWEEN_RUNS)
    assert rep.proof.cancel_all == "done" and not rig.book.working                  # clicked inside the recovery arm
    assert not (rig.gw.armed or rig.gw.deny_all or rig.recovery_only)


def test_no_recovery_login_after_live_identity_on_any_path():
    ctx = default_context()
    assert live_identity_blocked(kill_switch("live_identity", gate="running"))
    # a later decision on a run already aborted for live_identity: still no recovery login (SD-64)
    late = kill_switch("file_flag", gate="running")
    assert live_identity_blocked(late, RunVerdict("aborted_safety", "live_identity")) and not live_identity_blocked(late)
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    rep = execute_kill_switch(rig, late, clock=clock, allowlist=ctx.allowlist, start=rig.read_orders(),
                              run_so_far=RunVerdict("aborted_safety", "live_identity"))
    assert "login" not in rig.calls and RECOVERY not in rig.calls
    assert (rep.proof.verdict, rep.proof.reason) == (UNPROVEN, "session_unavailable")
    # mid-run: the flow boundary's assertion reads a Live id
    def work(o: Orchestrator) -> None:
        o.rig.s.selector_reads = [SelectorRead("12345678SCHW")]
        assert o.flow_boundary() is o.stop
    report, rig, _ = run(work=work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "live_identity")
    assert rig.calls.count("login") == 1 and RECOVERY not in rig.calls and phases(rig)[-1] == BETWEEN_RUNS


def test_kill_switch_severity_is_s2_unless_a_row_or_a_live_identity_makes_it_s1():
    for trigger in ("file_flag", "named_pipe", "order_event_window", "dialog_open_30s", "ten_drops", "attempted_send",
                    "paper_mode_mismatch", "attestation_mismatch", "orchestrator_dead_flag_set"):
        assert kill_switch(trigger, gate="running").incident == "S2", trigger
    assert {kill_switch(t, gate="running").incident for t in ("new_order_row", "live_identity")} == {"S1"}


def test_unreadable_identity_and_drop_thresholds_carry_their_6_12_severities():
    allow = default_context().allowlist
    assert judge_identity([BLIND, BLIND], allow).incident == "S3"
    assert deferral_unreadable(deferred_s=400.0).incident == "S3"
    assert judge_identity([SelectorRead("D-77777777")], allow).incident == "S2"         # mismatch stays S2
    ledger = DropLedger()
    drop = GatewayDrop("deny_profile", "refused", "integrity")
    first, second, third = (ledger.record(drop, unit="flow.a") for _ in range(3))
    assert first.incident is None and second.incident is None
    assert third.has("quarantine_flow") and third.incident == "S3"                       # the per-flow threshold
    for _ in range(6):
        ledger.record(drop, unit="flow.b")
    ten = ledger.record(drop, unit="flow.c")
    assert ten.run_reason == "kill_switch" and ten.has("kill_switch") and ten.incident == "S2"
    unknown = GatewayDrop("dialog_allowlist", "refused", "safety", unknown_dialog=True)
    assert DropLedger().record(unknown, unit="f", order_stage=True).incident == "S2"
    lost = GatewayDrop("token_expired", "dropped", "integrity", own_token=True)
    hooks = DropLedger()
    hooks.record(drop, unit="flow.d")
    hooks.record(drop, unit="flow.d")
    assert hooks.record(lost, unit="flow.d", retried=True).incident == "S3"             # hook_lost at the threshold


# -- the run's end (6.6, 6.7) ---------------------------------------------------------------------------------
def test_every_run_ends_between_runs_and_a_clean_run_uses_no_recovery_arm():
    report, rig, _ = run(work=lambda o: None)
    assert report.proof.verdict == PROVEN_NONE and rig.calls[-1] == BETWEEN_RUNS and RECOVERY not in rig.calls
    report, rig, _ = run(SimScenario(preflight_failures={"H4": None}))                   # stopped before any launch
    assert rig.calls[-1] == BETWEEN_RUNS


def test_the_recovery_proof_after_a_killed_tree_logs_in_inside_the_recovery_arm():
    def work(o: Orchestrator) -> None:
        o.rig.s.selector_reads = [BLIND, BLIND]            # the flow boundary's assertion: unreadable twice
        assert o.flow_boundary() is o.stop
    report, rig, _ = run(work=work)
    assert (report.verdict.status, report.proof.verdict) == ("complete_with_env_errors", PROVEN_NONE)
    c = rig.calls
    assert "set_hook_state:True:True" in c                                              # deny-all from the first read
    k = len(c) - 1 - c[::-1].index("kill_process_tree")
    assert c[k + 1:k + 5] == ["reset_input_state", RECOVERY, "launch", "login"]
    report, rig, _ = run(SimScenario(recovery_login_fails=1), work=work)
    proof = report.gate("teardown")
    assert (report.proof.verdict, report.proof.reason) == (UNPROVEN, "recovery_login_failed")
    assert proof.evidence["account"] == "account D-xxxxxxxx (allowlist entry 2) on rig rig-01"   # the hand-over page


def test_declared_sends_reach_the_trip_wires_and_every_proof():
    policy = SendPolicy(enabled=True)

    def send_and_declare(o: Orchestrator) -> None:
        rig = o.rig
        rig.ticket_visible = True
        assert o.step_boundary(order_stage_step=True) is None          # asserts paper mode: the frame is fresh
        assert rig.send_input(CONFIRM).refused is None and rig.dialog_open
        r = rig.send_input(SEND)
        assert r.refused is None and rig.sent_orders == 1
        rig.send_gate.declare(run_id="pr-1", shard_id="shard-1", flow_id="flow.paper.send", step_id="s09",
                              row_key=rig.book.working[0].key, sent_at="2026-11-10T19:00:00+00:00")
        assert o.flow_boundary(flow_id="flow.paper.send") is None       # the declared Working row trips nothing

    def cancel(o: Orchestrator) -> None:
        send_and_declare(o)
        assert o.rig.cancel_all().clicked                              # the flow's cancel; teardown proves it

    report, rig, _ = run(SimScenario(send_policy=policy), work=cancel)
    assert report.verdict.status == "complete" and report.proof.verdict == PROVEN_DECLARED
    assert [d.row_key for d in report.proof.sends] == ["sim-order-1"]

    def stop(o: Orchestrator) -> None:
        send_and_declare(o)
        o.rig.kill_channels()[1].post(KillRequest("rig-01", "watchdog", "watchdog", "stop", "t"))
        assert o.flow_boundary() is o.stop

    report, rig, _ = run(SimScenario(send_policy=policy), work=stop)
    assert report.verdict.status == "aborted_safety" and report.kill_switch.proof.verdict == PROVEN_DECLARED
    assert report.kill_switch.proof.cancel_all == "done" and not rig.book.working       # the recovery canceled it
    # without the gate the same Working row is undeclared: the boundary's new-row trip-wire fires
    def unannounced(o: Orchestrator) -> None:
        o.rig._send()
        assert o.flow_boundary() is o.stop
    report, _, _ = run(work=unannounced)
    assert report.kill_switch.result.evidence["trigger"] == "new_order_row"


# -- the boundaries' reads from the rig (5.2, 6.6, 10.2) --------------------------------------------------------
def test_the_watchdog_and_free_space_are_read_from_the_rig():
    report, rig, _ = run(SimScenario(watchdog=(1.0, False)))
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "watchdog_dead")
    assert rig.calls == [BETWEEN_RUNS]                              # read on entering the first gate, before preflight

    def stale(o: Orchestrator) -> None:
        o.rig.s.watchdog = (31.0, True)
        assert o.flow_boundary() is o.stop
    assert run(work=stale)[0].verdict.reason == "watchdog_dead"

    def full(o: Orchestrator) -> None:
        o.rig.s.free_bytes = 1024 ** 3
        assert o.flow_boundary() is o.stop
    assert run(work=full)[0].verdict.reason == "disk_full"


def test_the_window_watcher_feeds_the_order_event_and_dialog_trip_wires():
    def event(o: Orchestrator) -> None:
        o.rig.extra_windows.append("Order Rejected")
        assert o.flow_boundary() is o.stop
    report, _, _ = run(work=event)
    assert report.kill_switch.result.evidence["trigger"] == "order_event_window"

    def dialog(o: Orchestrator) -> None:
        o.rig.ticket_visible = True
        assert o.step_boundary(order_stage_step=True) is None
        assert o.rig.send_input(CONFIRM).refused is None
        o.clock.advance(31.0)
        assert o.step_boundary() is o.stop                            # open 31 s with no owning step
    report, _, _ = run(work=dialog)
    assert report.kill_switch.result.evidence["trigger"] == "dialog_open_30s"


def test_the_human_channels_come_first():
    def both(o: Orchestrator) -> None:
        o.rig._send()                                                 # a new row and a stop request at once
        o.rig.kill_channels()[0].post(KillRequest("rig-01", "cli", "asmith", "stop", "t"))
        assert o.flow_boundary() is o.stop
    report, _, _ = run(work=both)
    ev = report.kill_switch.result.evidence
    assert ev["trigger"] == "file_flag" and ev["request"]["requested_by"] == "asmith"


# -- relaunch, the session pushed to the slow half, the order hotkeys (5.4.7, 6.4, 7.7) --------------------------
def test_relaunch_resets_input_state_after_the_kill_and_the_session_is_cleared_then_attested_again():
    pushed: list[SessionSafety] = []

    def work(o: Orchestrator) -> None:
        before = len(o.rig.calls)
        assert o.relaunch("proactive")
        after = o.rig.calls[before:]
        assert after[after.index("kill_process_tree") + 1] == "reset_input_state"

    clock = ManualClock()
    rig = SimulatedRig(clock=clock)

    def listen(s: SessionSafety) -> None:
        pushed.append(s)
        rig.set_session_safety(s)

    Orchestrator(rig, default_context(), MemorySink(SCOPE), clock=clock, safety_listener=listen).run(work)
    stages = [s.attestation_stage for s in pushed]
    first = stages.index("a3_a9")
    assert "none" in stages[first:] and stages[first:].count("a3_a9") == 2                # cleared, then attested again
    assert pushed[-1] == SessionSafety() and rig.session_safety == SessionSafety()     # the run's end clears it


@pytest.mark.parametrize("pushed", [True, False])
def test_the_pushed_session_gates_confirm_and_send_through_the_assertion_schedule(pushed):
    def work(o: Orchestrator) -> None:
        rig = o.rig
        rig.ticket_visible = True
        o.clock.advance(121.0)                                        # no assertion for longer than the 6.4 timer
        stale = rig.send_input(CONFIRM).refused
        if pushed:
            assert stale.reason == "deny_profile" and not rig.dialog_open
            assert o.step_boundary(order_stage_step=True) is None      # the assertion before an order-stage step
            assert rig.send_input(CONFIRM).refused is None and rig.dialog_open
        else:                                                         # no listener: the booleans stand in
            assert stale is None and rig.session_safety is None
    assert run(work=work, pushed=pushed)[0].verdict.status == "complete"


def test_the_loaded_order_hotkeys_reach_the_hook_and_more_than_sixteen_fail_closed():
    loaded = load_default_order_hotkeys(EXAMPLE / DEFAULT_ORDER_HOTKEYS_PATH)
    _, rig, _ = run(ctx=default_context(order_hotkeys=loaded, default_order_chords=()), work=lambda o: None)
    assert rig.gw.chords == [Chord.parse("Ctrl+Shift+C"), Chord.parse("Shift+B"), Chord.parse("Shift+S")]
    many = DefaultOrderHotkeys("b", True, tuple(Chord.parse(f"Alt+F{i}") for i in range(1, 17)), ("buy",) * 16)
    report, rig, _ = run(ctx=default_context(order_hotkeys=many))
    assert (report.verdict.status, report.verdict.reason, report.last_gate) == ("aborted_harness", "config_invalid",
                                                                                "settings_attested")
    assert "load_order_chords" not in rig.calls
    with pytest.raises(ValueError):
        Chord.parse("Hyper+B")
