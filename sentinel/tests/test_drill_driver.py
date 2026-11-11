"""`run_drill`: the Nightly fire drill driven on the simulated rig and scored (6.5, 6.6 item 5)."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator import simrig as simrig_module
from sentinel.orchestrator.drill import (DRILL_UNIT, DrillPort, DrillPreconditions, SimDrillPort, expected_records,
                                         run_drill)
from sentinel.orchestrator.gateway import Chord, DropLedger, SlowDecision
from sentinel.orchestrator.ports import KillChannel, KillRequest
from sentinel.orchestrator.simrig import GOLDEN, SimScenario, SimulatedRig, default_context

CHORDS = (Chord.parse("Shift+B"), Chord.parse("Shift+S"))
CTX = default_context()
OK = DrillPreconditions(attestation_passed=True, probe_dropped_age_s=1.0, injector_signed=True)


class MemoryChannel:
    """An in-memory `KillChannel`: the file flag of item 5 as a queue; `deliver=False` loses every request."""

    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.pending: list[KillRequest] = []
        self.posted: list[KillRequest] = []
        self.acked: list[KillRequest] = []

    def poll(self) -> KillRequest | None:
        return self.pending.pop(0) if self.pending else None

    def post(self, request: KillRequest) -> None:
        self.posted.append(request)
        if self.deliver:
            self.pending.append(request)

    def acknowledge(self, request: KillRequest) -> None:
        self.acked.append(request)


def ready(rig: SimulatedRig, *, chords: tuple[Chord, ...] = CHORDS) -> SimulatedRig:
    """A rig through `settings_attested`: launched, hook armed, A1 to A9 passed, the order chords loaded."""
    rig.launch()
    rig.set_hook_state(armed=True, deny_all=False)
    rig.attested = True
    rig.load_order_chords(list(chords))
    return rig


def drill(rig: SimulatedRig | None = None, *, port=None, pre: DrillPreconditions = OK, channel=None,
          ledger: DropLedger | None = None):
    rig = rig or ready(SimulatedRig())
    return run_drill(rig, ledger or DropLedger(), rig.clock, CHORDS, rig.read_orders(), channel or MemoryChannel(),
                     port=port or SimDrillPort(rig), preconditions=pre, allowlist=CTX.allowlist, rig_id="rig-01",
                     fixture_account=CTX.fixture_account)


def test_clean_drill_passes_with_exactly_the_declared_set_and_exempt_counts():
    rig = ready(SimulatedRig())
    ledger, channel = DropLedger(), MemoryChannel()
    r = drill(rig, ledger=ledger, channel=channel)
    assert r.result.passed and r.failed == () and r.result.detail == "every item blocked; PROVEN"
    assert Counter(r.observation.records) == expected_records(CHORDS)
    assert r.observation.new_order_rows == 0 and r.observation.could_not_run == {}
    item5 = r.observation.item5
    assert item5.failures() == [] and item5.cancel_all_located and item5.proof_verdict.startswith("PROVEN")
    # the drill's drops never count toward a threshold, and the kill flag went through the channel and was handled
    assert ledger.run_count == 0 and ledger.unit_counts[DRILL_UNIT] == 0 and sum(ledger.by_reason.values()) > 0
    assert [k.channel for k in channel.posted] == ["file"] and channel.acked == channel.posted
    assert rig.sent_orders == 0 and "cancel_all" not in rig.calls                 # located, never clicked
    assert rig.calls.index("set_hook_state:False:True") < rig.calls.index("kill_process_tree") \
        < rig.calls.index("revert_snapshot")
    # item 5 runs the sequence in the hook's phases: reset after the kill, the recovery-only arm, disarmed at the end
    c = rig.calls
    assert c.index("kill_process_tree") < c.index("reset_input_state") \
        < c.index("set_hook_state:True:False:recovery_only") < len(c) - 1 - c[::-1].index("login") \
        < c.index("revert_snapshot") < c.index("set_hook_state:False:False")
    assert not (rig.gw.armed or rig.gw.deny_all or rig.recovery_only)
    assert isinstance(SimDrillPort(rig), DrillPort) and isinstance(channel, KillChannel)


def test_a_hook_and_slow_half_that_let_enter_through_send_an_order_and_block_the_login(monkeypatch):
    real = simrig_module.slow_half
    monkeypatch.setattr(simrig_module, "slow_half", lambda a, ctx: SlowDecision(False) if a.keys else real(a, ctx))
    rig = ready(SimulatedRig())
    rig.gw.blocked_vk = frozenset()                     # a broken blocked-key map
    r = drill(rig)
    assert rig.sent_orders >= 1 and r.observation.new_order_rows >= 1
    res = r.result
    assert res.run_status == "aborted_safety" and res.run_reason == "drill_order_placed" and res.incident == "S1"
    assert res.has("block_login") and res.has("kill_switch") and res.alert == alerts.safety_abort()
    assert "cancel_all" in rig.calls                     # item 5's recovery clicked Cancel All on the new row


def test_a_broken_blocked_key_map_alone_is_item1_failed_without_an_order():
    rig = ready(SimulatedRig())
    rig.gw.blocked_vk = frozenset()                     # the raw Enter and Space now drop no_token, not blocked_key
    r = drill(rig)
    assert rig.sent_orders == 0 and r.failed == ("item1",) and r.result.run is None
    assert r.result.checkpoint.reason == "drill_failed:item1" and r.result.has("quarantine_order_stage_flows")


def test_cancel_all_not_located_fails_item5_and_quarantines_every_flow():
    rig = ready(SimulatedRig(SimScenario(cancel_all_located=False)))
    r = drill(rig)
    assert r.failed == ("item5",)
    res = r.result
    assert (res.checkpoint.outcome, res.checkpoint.reason) == ("harness_error", "drill_failed:item5")
    assert res.has("quarantine_all_flows") and res.has("quarantine_order_stage_flows") and res.incident == "S2"
    assert res.run is None and "Cancel All not located" in res.checkpoint.detail


def test_a_failed_precondition_stops_the_drill_before_anything_is_injected():
    rig = ready(SimulatedRig())
    r = drill(rig, pre=DrillPreconditions(True, None, True))
    assert r.failed[0] == "hook_open" and r.result.checkpoint.reason == "drill_failed:hook_open"
    assert r.result.has("maintenance_task") and r.observation.records == ()
    assert rig.drops == 0 and rig.refusals == 0 and not rig.ticket_visible       # nothing injected, ticket cleared
    stale = drill(ready(SimulatedRig()), pre=DrillPreconditions(True, 6.0, True))
    assert stale.failed[0] == "hook_open"
    unsigned = drill(ready(SimulatedRig()), pre=DrillPreconditions(False, 1.0, False))
    assert unsigned.failed == ("attestation", "injector")


def test_the_drill_ticket_on_another_account_fails_the_account_precondition():
    r = drill(ready(SimulatedRig(SimScenario(selected=GOLDEN))))
    assert r.failed == ("account",) and r.result.checkpoint.reason == "drill_failed:account"


def test_a_precondition_failure_with_a_new_row_is_the_s1_case():
    rig = ready(SimulatedRig())
    start = rig.read_orders()
    rig._send()                                          # an order already on the book when the drill reads at once
    r = run_drill(rig, DropLedger(), rig.clock, CHORDS, start, MemoryChannel(), port=SimDrillPort(rig),
                  preconditions=DrillPreconditions(True, None, True), allowlist=CTX.allowlist, rig_id="rig-01")
    assert r.result.run_reason == "drill_order_placed" and r.observation.new_order_rows == 1


def test_an_item_whose_target_is_not_located_could_not_run_and_the_rest_still_run():
    rig = ready(SimulatedRig())
    r = drill(rig, port=SimDrillPort(rig, unlocated=frozenset({"ladder_buy_cell"})))
    obs = r.observation
    assert set(obs.could_not_run) == {2, 4} and "ladder cell" in obs.could_not_run[2]
    assert r.failed == ("item2", "item4") and r.result.has("maintenance_task")
    assert any(rec.item == 3 for rec in obs.records) and obs.item5.failures() == []


def test_the_dialog_not_opening_is_could_not_run_and_an_a9_failure():
    rig = ready(SimulatedRig())
    rig.attested = False                                 # the slow half refuses Confirm and Send
    r = drill(rig)
    assert 1 in r.observation.could_not_run and r.failed[0] == "item1"
    assert "also an A9 failure" in r.result.checkpoint.detail


def test_order_chords_not_loaded_at_settings_attested_fail_item3():
    r = drill(ready(SimulatedRig(), chords=()))
    assert r.failed == ("item3",) and "item 3 drop set differs" in r.result.checkpoint.detail


def test_a_ticket_that_cannot_be_rebuilt_leaves_items_3_and_4_unrun():
    rig = ready(SimulatedRig())

    class OnceTicket(SimDrillPort):
        built = 0

        def build_drill_ticket(self):
            self.built += 1
            return super().build_drill_ticket() if self.built == 1 else None

    r = drill(rig, port=OnceTicket(rig))
    assert set(r.observation.could_not_run) == {3, 4} and r.failed == ("item3", "item4")


def test_a_kill_flag_that_never_arrives_fails_item5():
    rig = ready(SimulatedRig())
    r = drill(rig, channel=MemoryChannel(deliver=False))
    assert r.failed == ("item5",) and "did not arrive" in r.observation.could_not_run[5]
    assert "kill_process_tree" not in rig.calls and not rig.ticket_visible


class SlowKillRig(SimulatedRig):
    """The process tree takes 6 s to die."""

    def kill_process_tree(self) -> bool:
        self.clock.sleep(6.0)
        return super().kill_process_tree()


class RevertFailsRig(SimulatedRig):
    """The hypervisor cannot revert the snapshot."""

    def revert_snapshot(self) -> None:
        super().revert_snapshot()
        raise RuntimeError("snapshot_revert_failed")


def test_item5_times_the_tree_and_requires_the_revert():
    slow = drill(ready(SlowKillRig()))
    assert slow.failed == ("item5",) and slow.observation.item5.tree_gone_s == pytest.approx(6.0)
    assert "process tree alive after 5 s" in slow.result.checkpoint.detail
    bad = drill(ready(RevertFailsRig()))
    assert bad.failed == ("item5",) and not bad.observation.item5.reverted
    assert "rig not reverted" in bad.result.checkpoint.detail


def test_a_recovery_login_that_fails_fails_item5_with_no_read():
    rig = ready(SimulatedRig(SimScenario(recovery_login_fails=1)))
    rig.login()                                          # the session's own login; the recovery login is the next
    r = drill(rig)
    assert r.failed == ("item5",) and not r.observation.item5.recovery_login_ok
    assert r.observation.new_order_rows == 0 and "recovery login failed" in r.result.checkpoint.detail


def test_an_order_found_by_item5s_clean_read_is_the_s1_case_though_the_rereads_go_unreadable():
    """killswitch:0: item 5's recovery read finds a stray order and clicks Cancel All; every re-read after the click is
    unreadable. The clean read that found it still counts, so the drill is the S1 drill_order_placed case."""
    class BlurAfterCancel(SimulatedRig):
        blurred = False

        def cancel_all(self):
            self.blurred = True
            return super().cancel_all()

        def read_orders(self):
            book = super().read_orders()
            return book if not self.blurred else replace(book, readable=False)

    rig = ready(BlurAfterCancel())
    start = rig.read_orders()
    rig._send()                                          # a stray order before item 5
    r = run_drill(rig, DropLedger(), rig.clock, CHORDS, start, MemoryChannel(), port=SimDrillPort(rig),
                  preconditions=OK, allowlist=CTX.allowlist, rig_id="rig-01", fixture_account=CTX.fixture_account)
    assert r.observation.new_order_rows == 1 and r.result.incident == "S1"
    assert (r.result.run_status, r.result.run_reason) == ("aborted_safety", "drill_order_placed")


def test_a_live_account_at_item5s_recovery_login_is_live_identity_s1_and_blocks_the_login():
    """killswitch:3: item 5 fails (the recovery login failed A1), and the 6.4 live identity it saw goes on the
    drill's result: aborted_safety live_identity, S1, the login blocked for live_identity, the rig quarantined."""
    from sentinel.orchestrator.rig import SelectorRead
    from sentinel.orchestrator.safety_canaries import drill_rows
    from sentinel.store.safety import blocked_reason_for

    rig = ready(SimulatedRig(SimScenario(selector_reads=[SelectorRead("12345678SCHW", crop_sha256="c-live")])))
    r = drill(rig)
    res = r.result
    assert r.failed == ("item5",) and not r.observation.item5.recovery_login_ok
    assert (res.run_status, res.run_reason, res.incident) == ("aborted_safety", "live_identity", "S1")
    assert res.has("block_login") and blocked_reason_for(res) == "live_identity"
    assert res.has("quarantine_all_flows") and res.checkpoint.reason == "drill_failed:item5"
    assert res.alert == alerts.safety_abort() and res.evidence["crop"] == "c-live"
    rows = drill_rows(r, CHORDS)
    assert rows[4].produced_reason == "drill_failed:item5" and rows[0].state == "detected"


def test_sim_drill_port_names_only_the_drill_targets():
    with pytest.raises(ValueError):
        SimDrillPort(SimulatedRig()).locate("send_button")
    rig = SimulatedRig()
    assert SimDrillPort(rig).build_drill_ticket() is None and SimDrillPort(rig).locate("delete") is None
