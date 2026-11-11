"""Paper-mode assertion, settings attestation, fire drill, kill switch and proof, Explorer boundary (6.3 to 6.8)."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

import sentinel.orchestrator.attestation as A
from sentinel.orchestrator import alerts
from sentinel.orchestrator.attestation import AttestationProfile, RowReads, attest
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.drill import DrillObservation, DrillRecord, Item5, expected_records, score_drill
from sentinel.orchestrator.explorer import (ExplorerBudget, ExplorerSession, explorer_teardown, match_deny,
                                            not_started_model_unavailable)
from sentinel.orchestrator.gateway import Chord, DropLedger, GatewayDrop
from sentinel.orchestrator.identity import (Allowlist, assertion_due, classify, judge_fixture_account,
                                            judge_identity, load_allowlist, send_frame_fresh)
from sentinel.orchestrator.killswitch import (CANCEL_BY_HAND, ORDERS_SENT, PROVEN_DECLARED, PROVEN_NO_SESSION,
                                              PROVEN_NONE, UNPROVEN, UNPROVEN_BLOCK, Proof, ProofInputs, TripWires,
                                              account_ref, book_sha256, cancel_new_working, check_tripwires,
                                              config_invalid_proof,
                                              execute_kill_switch, kill_switch, masked_account, post_run_proof,
                                              proof_result, send_lines)
from sentinel.orchestrator.orderbook import MonitorBook
from sentinel.orchestrator.ports import KillRequest
from sentinel.orchestrator.results import RunVerdict
from sentinel.orchestrator.rig import (CancelAllResult, ConfirmationCheck, GatewayLog, HotkeyRow, NotificationsRead,
                                       OrderBook, OrderRow, Rig, SelectorRead, SettingRead)
from sentinel.orchestrator.simrig import EMPTY, GOLDEN, SimScenario, SimulatedRig, default_settings
from sentinel.records import DeclaredSend
from sentinel.store import RunPlan, Store

ALLOW = Allowlist("rig-01", (GOLDEN, EMPTY), "0" * 64)
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example" / "gateway" / "allowlist.yaml"
SCHEMA = Path(__file__).resolve().parents[1] / "src" / "sentinel" / "store" / "schema.sql"


# -- 6.4 paper-mode assertion ---------------------------------------------------------------------------
def test_allowlist_loads_exact_entries_from_gateway_allowlist_yaml():
    al = load_allowlist(EXAMPLE, "rig-01")
    assert al.entries == (GOLDEN, EMPTY) and len(al.sha256) == 64
    assert "D-2048113" not in al and "d-20481133" not in al and GOLDEN in al          # exact, no prefix or case
    with pytest.raises(ValueError):
        load_allowlist(EXAMPLE, "rig-09")


def test_paper_mode_exact_match_passes():
    r = judge_identity([SelectorRead(EMPTY)], ALLOW)
    assert r.passed and r.gate == "paper_mode_asserted" and r.evidence["allowlist_entry"] == 2
    assert classify(EMPTY + " ", ALLOW) == "paper_mode_mismatch"                       # exact, not stripped


def test_live_identity_aborts_blocks_the_login_and_pages_s1():
    r = judge_identity([SelectorRead("12345678SCHW")], ALLOW)
    assert r.run_status == "aborted_safety" and r.run_reason == "live_identity"
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "paper_mode_asserted",
                                                                           "live_identity")
    assert {"deny_all", "kill_tree", "block_login"} <= set(r.actions) and r.incident == "S1"
    assert r.alert == alerts.safety_abort()


def test_foreign_paper_id_is_paper_mode_mismatch_s2_without_login_block():
    r = judge_identity([SelectorRead("D-99999999")], ALLOW)
    assert r.run_reason == "paper_mode_mismatch" and r.incident == "S2" and not r.has("block_login")
    assert r.unreached.reason == "paper_mode_mismatch"


def test_identity_unreadable_on_two_reads_is_env_error_not_a_pass():
    blind = SelectorRead(None, landmark_found=False)
    with pytest.raises(ValueError):
        judge_identity([blind], ALLOW)                                                # the second read is required
    r = judge_identity([blind, SelectorRead("D-x", confidence=40.0)], ALLOW)
    assert r.run_status == "complete_with_env_errors"
    assert (r.unreached.outcome, r.unreached.reason, r.unreached.gate) == ("env_error", "identity_unreadable",
                                                                           "paper_mode_asserted")
    assert r.has("deny_all") and r.has("kill_tree") and not r.has("block_login")
    assert r.alert.priority == "P2" and "login_owner" in r.alert.notified
    again = judge_identity([blind, blind], ALLOW, unreadable_streak=1)
    assert again.has("block_login") and again.has("maintenance_task") and again.alert.priority == "P1"


def test_unreadable_then_readable_rearms():
    r = judge_identity([SelectorRead(None, landmark_found=False), SelectorRead(GOLDEN)], ALLOW)
    assert r.passed and r.has("rearm")


def test_fixture_account_role_check_a2():
    assert judge_fixture_account(EMPTY, EMPTY).passed
    first = judge_fixture_account(GOLDEN, EMPTY)
    assert not first.passed and first.has("select_fixture_account") and first.run is None
    assert judge_fixture_account(GOLDEN, EMPTY, after_select=EMPTY).passed
    still = judge_fixture_account(GOLDEN, EMPTY, after_select=GOLDEN)
    assert still.unreached.gate == "fixture_verified" and still.run_status == "complete_with_env_errors"


def test_assertion_schedule():
    assert assertion_due(10.0, 0.0, boundary=True) == "now"
    assert assertion_due(100.0, 0.0) is None
    assert assertion_due(130.0, 0.0) == "now"
    assert assertion_due(130.0, 0.0, modal_up=True) == "defer"
    assert assertion_due(500.0, 0.0, main_foreground=False) == "unreadable"
    assert send_frame_fresh(10.0, 10.4) and not send_frame_fresh(10.0, 10.6)


# -- 6.3 settings attestation ---------------------------------------------------------------------------
PROFILE = AttestationProfile(active_trader_panels=("at-1", "at-2"), sha256="p" * 64)
EVIDENCE = {i: f"sha-{i}" for i in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10")}
OK_A9 = ConfirmationCheck(True, 0, "sha-A9")


def rows(**over) -> dict[str, RowReads]:
    vals = {**default_settings(), "A7": {"at-1": False, "at-2": False}, **over}
    return {i: RowReads(SettingRead(i, vals[i], screenshot_sha256=f"sha-{i}")) for i in vals}


def test_attestation_pass_loads_the_hot_keys_list_as_chords():
    res = attest(rows(), PROFILE, a9=OK_A9, evidence=EVIDENCE)
    assert res.result.passed and res.result.gate == "settings_attested"
    assert all(v == "match" for v in res.rows.values())
    assert res.chords == (Chord.parse("Ctrl+Shift+C"),)


@pytest.mark.parametrize("item,value", [("A3", False), ("A4", True), ("A5", True),
                                        ("A6", (HotkeyRow("Buy at market", "Shift+B"),)),
                                        ("A7", {"at-1": False, "at-2": True})])
def test_safety_row_mismatch_aborts_attestation_mismatch(item, value):
    res = attest(rows(**{item: value}), PROFILE, a9=OK_A9, evidence=EVIDENCE)
    r = res.result
    assert r.run_status == "aborted_safety" and r.run_reason == "attestation_mismatch" and r.incident == "S2"
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "settings_attested",
                                                                           "attestation_mismatch")
    assert res.rows[item] == "mismatch" and r.alert.is_safety


def test_working_order_at_a8_is_unexpected_working_order_s1():
    r = attest(rows(A8=1, A3=False), PROFILE, a9=OK_A9, evidence=EVIDENCE).result
    assert r.run_reason == "unexpected_working_order" and r.incident == "S1"


def test_unreadable_twice_is_attestation_unreadable_never_a_pass_or_mismatch():
    reads = rows()
    reads["A5"] = RowReads(SettingRead("A5", None), SettingRead("A5", True, landmark_score=0.5))
    r = attest(reads, PROFILE, a9=OK_A9, evidence=EVIDENCE).result
    assert r.run_status == "complete_with_env_errors" and not r.passed
    assert (r.unreached.reason, r.unreached.gate) == ("attestation_unreadable", "settings_attested")
    assert r.alert.priority == "P2"
    repeat = attest(reads, PROFILE, a9=OK_A9, evidence=EVIDENCE, previous_unreadable=True).result
    assert repeat.alert.priority == "P1" and repeat.has("maintenance_task")


def test_unreadable_first_read_and_readable_second_read_passes():
    reads = rows()
    reads["A3"] = RowReads(SettingRead("A3", None), SettingRead("A3", True))
    assert attest(reads, PROFILE, a9=OK_A9, evidence=EVIDENCE).result.passed


def test_a7_missing_panel_and_bad_binding_are_unreadable():
    res = attest(rows(A7={"at-1": False}), PROFILE, a9=OK_A9, evidence=EVIDENCE)
    assert res.rows["A7"] == "unreadable" and res.result.unreached.reason == "attestation_unreadable"
    res = attest(rows(A6=(HotkeyRow("Show chart", "Hyper+Q"),)), PROFILE, a9=OK_A9, evidence=EVIDENCE)
    assert res.rows["A6"] == "unreadable"


def test_a9_absent_dialog_and_new_row():
    absent = attest(rows(), PROFILE, a9=ConfirmationCheck(False, 0), evidence=EVIDENCE).result
    assert absent.run_reason == "attestation_mismatch" and absent.incident == "S2"
    sent = attest(rows(), PROFILE, a9=ConfirmationCheck(False, 1), evidence=EVIDENCE).result
    assert sent.run_reason == "kill_switch" and sent.incident == "S1" and sent.has("kill_switch")
    with pytest.raises(ValueError):
        attest(rows(), PROFILE, a9=None, evidence=EVIDENCE)


def test_a10_on_rc_runs_aborts_or_skips_the_explorer_stage_only():
    ok = attest(rows(), PROFILE, a9=OK_A9, evidence=EVIDENCE, rc_run=True, a10=NotificationsRead(True, False))
    assert ok.result.passed and ok.explorer_stage == "allowed"
    bad = attest(rows(), PROFILE, a9=OK_A9, evidence=EVIDENCE, rc_run=True, a10=NotificationsRead(True, True))
    assert bad.result.passed and bad.result.run is None and bad.explorer_stage == "aborted_safety"
    blind = attest(rows(), PROFILE, a9=OK_A9, evidence=EVIDENCE, rc_run=True, a10=NotificationsRead(True, None))
    assert blind.explorer_stage == "not_run"
    assert blind.result.outcomes[0].outcome.reason == "entitlement"


def test_a11_missing_evidence_is_aborted_harness_and_profile_drift_is_env_error():
    r = attest(rows(), PROFILE, a9=OK_A9, evidence={**EVIDENCE, "A4": None}).result
    assert r.run_status == "aborted_harness" and r.unreached.outcome == "harness_error"
    d = attest(rows(), PROFILE, a9=OK_A9, evidence=EVIDENCE, profile_drift=True).result
    assert (d.unreached.reason, d.run_status) == ("fixture_drift", "complete_with_env_errors")


def test_attestation_reads_and_never_repairs():
    assert not [n for n in dir(Rig) if n.startswith(("set_setting", "write_setting", "repair"))]
    assert "repair" not in [n for n in dir(A) if not n.startswith("_")]


# -- 6.5 fire drill -------------------------------------------------------------------------------------
CHORDS = (Chord.parse("Shift+B"), Chord.parse("Shift+S"))
GOOD5 = Item5(0.2, 3.0, True, True, PROVEN_NONE, True)


def drill(**over) -> DrillObservation:
    base = dict(attestation_passed=True, probe_dropped_age_s=1.0, injector_signed=True, ticket_on_empty=True,
                records=tuple(expected_records(CHORDS).elements()), item5=GOOD5)
    return DrillObservation(**{**base, **over})


def test_drill_passes_only_on_exactly_the_declared_set():
    assert score_drill(drill(), CHORDS).result.passed
    extra = drill(records=tuple(expected_records(CHORDS).elements()) + (DrillRecord(3, "raw", "dropped",
                                                                                    "blocked_chord_dialog"),))
    assert score_drill(extra, CHORDS).failed == ("item3",)


def test_drill_case_item_not_blocked_without_a_new_row():
    recs = [r for r in expected_records(CHORDS).elements() if not (r.item == 1 and r.attempt == "raw")]
    res = score_drill(drill(records=tuple(recs)), CHORDS)
    r = res.result
    assert res.failed == ("item1",) and r.run is None                                  # the Nightly stays complete
    assert (r.checkpoint.outcome, r.checkpoint.reason) == ("harness_error", "drill_failed:item1")
    assert r.has("quarantine_order_stage_flows") and r.alert == alerts.drill_failure() and r.incident == "S2"


def test_drill_case_new_order_row_is_drill_order_placed():
    r = score_drill(drill(new_order_rows=1), CHORDS).result
    assert r.run_status == "aborted_safety" and r.run_reason == "drill_order_placed"
    assert {"kill_switch", "block_login", "cancel_all"} <= set(r.actions) and r.incident == "S1"


def test_drill_preconditions_item_that_cannot_run_and_item5():
    assert score_drill(drill(probe_dropped_age_s=None), CHORDS).result.checkpoint.reason == "drill_failed:hook_open"
    cannot = score_drill(drill(could_not_run={1: "dialog did not open"}), CHORDS).result
    assert cannot.checkpoint.reason == "drill_failed:item1" and cannot.has("maintenance_task")
    assert "A9" in cannot.checkpoint.detail
    k = score_drill(drill(item5=Item5(0.9, 3.0, True, True, PROVEN_NONE, True)), CHORDS)
    assert k.failed == ("item5",) and k.result.has("quarantine_all_flows")


# -- 6.6 kill switch and post-run proof -----------------------------------------------------------------
def test_tripwires_in_order_and_the_30s_rule_skips_owned_and_explorer_dialogs():
    assert check_tripwires(TripWires()) is None
    assert check_tripwires(TripWires(file_flag=True, attempted_send=True)) == "file_flag"
    assert check_tripwires(TripWires(dialog_open_s=31)) == "dialog_open_30s"
    assert check_tripwires(TripWires(dialog_open_s=31, dialog_owned=True)) is None
    assert check_tripwires(TripWires(dialog_open_s=31, explorer_session=True)) is None
    assert check_tripwires(TripWires(gateway_drops=10)) == "ten_drops"
    assert check_tripwires(TripWires(orchestrator_dead=True)) is None
    assert check_tripwires(TripWires(orchestrator_dead=True, flag_set=True)) == "orchestrator_dead_flag_set"


def test_kill_switch_decision_marks_unreached_with_the_aborting_gate():
    r = kill_switch("file_flag", gate="running", running_flow="flow.trade.order-ticket.stock.limit")
    assert r.run_reason == "kill_switch" and r.unreached.gate == "running" and r.unreached.reason == "kill_switch"
    assert r.actions[:2] == ("deny_all", "quarantine_flow") and r.alert.is_safety
    assert kill_switch("attempted_send", gate="running").run_reason == "attempted_send"
    asked = KillRequest("rig-01", "cli", "asmith", "wrong build", "2026-11-10T14:00:00+00:00")
    h = kill_switch("file_flag", gate="running", request=asked)
    assert h.evidence["request"]["requested_by"] == "asmith" and "requested by asmith via cli: wrong build" in h.detail


@pytest.mark.parametrize("trigger", ["file_flag", "named_pipe", "new_order_row", "order_event_window",
                                     "dialog_open_30s", "ten_drops", "orchestrator_dead_flag_set"])
def test_unreached_checkpoints_carry_kill_switch_for_human_channels_and_automatic_tripwires(trigger):
    r = kill_switch(trigger, gate="running")                                            # SD-59
    assert (r.run_status, r.run_reason) == ("aborted_safety", "kill_switch")
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "running", "kill_switch")
    assert r.has("recovery_login") and not r.has("block_login") and r.evidence["trigger"] == trigger


START = OrderBook(position_rows=0, net_liq=2500.0)


def test_proof_proven_no_orders_sent():
    p = post_run_proof(ProofInputs(start=START, end=START))
    assert p.verdict == PROVEN_NONE and p.proven and [r.verdict for r in p.rows] == ["ok"] * 5
    assert proof_result(p).passed


def test_proof_undeclared_row_is_aborted_safety_after_the_fact():
    end = OrderBook(working=(OrderRow("x1", "working", "WORKING"),), net_liq=2500.0)
    p = post_run_proof(ProofInputs(start=START, end=end))
    assert p.verdict == ORDERS_SENT and p.undeclared[0].key == "x1"
    r = proof_result(p)
    assert r.run_status == "aborted_safety" and r.run_reason == "undeclared_order_row" and r.incident == "S1"
    rejected = OrderBook(canceled=(OrderRow("r1", "canceled", "REJECTED"),), net_liq=2500.0)
    assert post_run_proof(ProofInputs(start=START, end=rejected)).undeclared


@pytest.mark.parametrize("cancel,text", [("done", "verified"), ("failed", "failed"), ("not_needed", "not needed")])
def test_orders_sent_is_the_fifth_verdict_and_names_the_rows_and_cancel_all(cancel, text):
    end = OrderBook(working=(OrderRow("x1", "working", "WORKING"),), filled=(OrderRow("f1", "filled", "FILLED"),),
                    net_liq=2400.0, position_rows=1)
    p = post_run_proof(ProofInputs(start=START, end=end, cancel_all=cancel))
    assert (p.verdict, p.reason, p.proven) == (ORDERS_SENT, None, False)
    assert p.line == f"ORDERS SENT: undeclared rows (x1, f1); Cancel All {text}"   # 6.6 text form
    r = proof_result(p, run_so_far=RunVerdict("aborted_safety", "attempted_send"))
    assert (r.run_status, r.run_reason, r.gate, r.incident) == ("aborted_safety", "undeclared_order_row", "teardown",
                                                              "S1")
    assert r.has("block_login") and r.has("disable_rig") and "login_owner" in r.alert.paged
    assert r.detail == p.line and r.evidence["verdict"] == p.line and r.evidence["undeclared"] == ["x1", "f1"]


def sent(key: str = "s1", step: str = "s05") -> DeclaredSend:
    """A gated-branch send as SendGate records it (6.7): the token's flow and step and the Working row's key."""
    return DeclaredSend("pr-48213-3", "shard-1", "flow.paper.send.spy-limit", step, key, "2026-11-10T14:02:11-05:00",
                        token_id=1, symbol="SPY", qty=1)


WORKING_S1 = OrderBook(working=(OrderRow("s1", "working", "WORKING"),), net_liq=2500.0)
CANCELED_S1 = OrderBook(canceled=(OrderRow("s1", "canceled", "CANCELED"),), net_liq=2500.0)


def test_a_failed_cancel_of_declared_sends_alone_is_unproven_and_the_verdicts_are_consistent():
    p = post_run_proof(ProofInputs(start=START, end=WORKING_S1, cancel_all="failed", declared_sends=(sent(),)))
    assert (p.verdict, p.reason, p.line) == (UNPROVEN, "cancel_all_failed", "UNPROVEN: cancel_all_failed")
    row = (OrderRow("x1", "working", "WORKING"),)
    with pytest.raises(ValueError, match="ORDERS SENT"):
        Proof(ORDERS_SENT)                                       # ORDERS SENT always names its rows
    with pytest.raises(ValueError, match="ORDERS SENT"):
        Proof(UNPROVEN, "cancel_all_failed", row)                # undeclared rows are never only UNPROVEN
    with pytest.raises(ValueError, match="declared send"):
        Proof(PROVEN_DECLARED)                                   # PROVEN: only declared sends names a send
    with pytest.raises(ValueError, match="declared send"):
        Proof(PROVEN_NONE, sends=(sent(),))                      # a run that sent is never "no orders sent"
    with pytest.raises(TypeError):
        ProofInputs(START, CANCELED_S1, declared_sends=("s1",))  # SendGate's records, not bare keys
    with pytest.raises(ValueError):
        ProofInputs(START, START, session="maybe")


def test_proof_only_declared_sends_lists_each_send_with_its_working_and_canceled_rows():
    p = post_run_proof(ProofInputs(start=START, end=CANCELED_S1, declared_sends=(sent(),)))
    assert p.verdict == PROVEN_DECLARED and p.declared[0].key == "s1" and p.sends == (sent(),)
    (s,) = p.counts["sends"]
    assert (s["flow_id"], s["step_id"], s["working"], s["canceled"], s["left_working"]) == (
        "flow.paper.send.spy-limit", "s05", "s1", "s1", False)
    assert p.counts["rows"]["canceled orders"] == {"start": 0, "end": 1, "new": 1, "expected": 1, "verdict": "ok"}
    assert send_lines(p.counts) == ["send flow.paper.send.spy-limit s05 at 2026-11-10T14:02:11-05:00: Working row "
                                    "s1, Canceled row s1"]
    assert proof_result(p).passed and proof_result(p).evidence["sends"] == send_lines(p.counts)


def test_a_declared_send_left_working_is_never_proven_whatever_cancel_all_says():
    for cancel in ("not_needed", "done"):
        p = post_run_proof(ProofInputs(START, WORKING_S1, cancel_all=cancel, declared_sends=(sent(),)))
        assert (p.verdict, p.reason) == (UNPROVEN, "cancel_all_failed") and not p.proven
        assert p.counts["rows"]["working orders"]["verdict"] == "not canceled"
        assert send_lines(p.counts)[0].endswith("still Working, no Canceled row")
    r = proof_result(post_run_proof(ProofInputs(START, WORKING_S1, declared_sends=(sent(),))))
    assert r.run_reason == "unproven_proof" and r.evidence["blocked_reason"] == "cancel_all_failed"


def test_a_declared_send_with_no_canceled_row_is_unproven_monitor_unreadable():
    p = post_run_proof(ProofInputs(START, START, declared_sends=(sent(),)))
    assert (p.verdict, p.reason) == (UNPROVEN, "monitor_unreadable")
    assert p.counts["rows"]["canceled orders"]["verdict"] == "missing"
    assert send_lines(p.counts) == ["send flow.paper.send.spy-limit s05 at 2026-11-10T14:02:11-05:00: Working row "
                                    "s1, no Canceled row"]
    both = OrderBook(canceled=(OrderRow("s1", "canceled", "CANCELED"),), net_liq=2500.0)
    two = post_run_proof(ProofInputs(START, both, declared_sends=(sent(), sent("s2", "s09"))))
    assert two.reason == "monitor_unreadable" and [s["canceled"] for s in two.counts["sends"]] == ["s1", None]


def test_a_filled_declared_send_or_an_undeclared_row_beside_sends_is_orders_sent():
    filled = OrderBook(filled=(OrderRow("s1", "filled", "FILLED"),), net_liq=2500.0)
    assert post_run_proof(ProofInputs(START, filled, declared_sends=(sent(),))).verdict == ORDERS_SENT
    extra = OrderBook(canceled=(OrderRow("s1", "canceled", "CANCELED"), OrderRow("x9", "canceled", "REJECTED")),
                      net_liq=2500.0)
    p = post_run_proof(ProofInputs(START, extra, declared_sends=(sent(),)))
    assert p.verdict == ORDERS_SENT and [r.key for r in p.undeclared] == ["x9"] and p.counts["declared"] == ["s1"]


def test_proof_no_session_established_needs_no_token():
    assert post_run_proof(ProofInputs(None, None, session="never", tokens_issued=0)).verdict == PROVEN_NO_SESSION
    p = post_run_proof(ProofInputs(None, None, session="never", tokens_issued=3))
    assert p.verdict == UNPROVEN


@pytest.mark.parametrize("inputs,reason", [
    (ProofInputs(START, None, session="kicked", kicked_login_ok=False), "session_unavailable"),
    (ProofInputs(START, None, recovery_login_ok=False), "recovery_login_failed"),
    (ProofInputs(None, START), "snapshot_missing"),
    (ProofInputs(START, OrderBook(net_liq=2500.0, copy_all_agrees=False)), "monitor_unreadable"),
    (ProofInputs(START, OrderBook(net_liq=2501.0)), "monitor_unreadable"),
    (ProofInputs(START, START, cancel_all="failed"), "cancel_all_failed"),
])
def test_proof_unproven_reasons(inputs, reason):
    p = post_run_proof(inputs)
    assert (p.verdict, p.reason) == (UNPROVEN, reason)
    r = proof_result(p)
    assert r.run_status == "complete_with_env_errors" and r.run_reason == "unproven_proof"
    assert r.has("block_login") and r.evidence["blocked_reason"] == UNPROVEN_BLOCK[reason]
    if reason in ("recovery_login_failed", "cancel_all_failed"):        # handed to a human: the login owner too
        assert r.alert == alerts.unproven_proof().plus(paged=("login_owner",))
        assert r.evidence["instruction"] == CANCEL_BY_HAND and CANCEL_BY_HAND in r.detail
    else:
        assert r.alert == alerts.unproven_proof() and r.detail == p.line and "instruction" not in r.evidence


def test_unproven_keeps_an_aborted_safety_run_and_twice_disables_the_rig():
    p = post_run_proof(ProofInputs(START, None, recovery_login_ok=False))
    assert proof_result(p, run_so_far=RunVerdict("aborted_safety", "kill_switch")).run is None
    twice = proof_result(p, unproven_streak=1)
    assert twice.has("disable_rig") and twice.findings[0].kind == "harness_error"


def _ks(rig: SimulatedRig, clock: ManualClock, start: OrderBook):
    return execute_kill_switch(rig, kill_switch("file_flag", gate="running"), clock=clock, allowlist=ALLOW,
                               start=start, fixture_account=EMPTY)


def test_kill_switch_sequence_then_proven_no_orders_sent():
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    rep = _ks(rig, clock, rig.read_orders())
    assert rep.proof.verdict == PROVEN_NONE and rep.proof_effect.passed
    assert [s for s, _ in rep.steps] == ["deny_all", "kill_tree", "recovery_login", "proof", "revert_snapshot"]
    assert "cancel_all" not in rig.calls
    # the hook per phase (gateway.HOOK_STATES): deny-all, the recovery-only arm for the login, disarmed at the end
    assert [c for c in rig.calls if c.startswith("set_hook_state")] == [
        "set_hook_state:False:True", "set_hook_state:True:False:recovery_only", "set_hook_state:False:False"]
    assert rig.calls.index("set_hook_state:True:False:recovery_only") < rig.calls.index("login") \
        < rig.calls.index("revert_snapshot") < rig.calls.index("set_hook_state:False:False")
    assert not (rig.gw.armed or rig.gw.deny_all or rig.recovery_only)


def test_kill_switch_cancels_a_sent_order_and_the_proof_names_the_row():
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    start = rig.read_orders()
    rig._send()                                                                         # an order got out
    rep = _ks(rig, clock, start)
    assert "cancel_all" in rig.calls and not rig.book.working
    assert rep.proof.verdict == ORDERS_SENT and rep.proof_effect.run_reason == "undeclared_order_row"


def test_kill_switch_proof_unproven_recovery_login_failed_and_orders_sent_with_cancel_all_failed():
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(recovery_login_fails=1), clock=clock)
    rig.login()
    rep = _ks(rig, clock, rig.read_orders())
    assert (rep.proof.verdict, rep.proof.reason) == (UNPROVEN, "recovery_login_failed")
    assert rep.proof_effect.run is None                                                 # stays aborted_safety
    rig2 = SimulatedRig(SimScenario(cancel_all_located=False), clock=clock)
    start = rig2.read_orders()
    rig2._send()
    rep2 = _ks(rig2, clock, start)
    assert (rep2.proof.verdict, rep2.proof.cancel_all) == (ORDERS_SENT, "failed")       # the read itself was clean
    assert rep2.proof.line == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All failed"
    assert rep2.proof_effect.run_reason == "undeclared_order_row" and "login_owner" in rep2.proof_effect.alert.paged


def test_kill_switch_proof_monitor_unreadable():
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(monitor_unreadable=True), clock=clock)
    rep = _ks(rig, clock, rig.read_orders())
    assert rep.proof.reason == "monitor_unreadable"


class StuckRig(SimulatedRig):
    """A rig whose `Cancel All` is located and clicked but leaves the rows `stuck` keys in Working (6.6 Cancel All
    failure: rows remain after 60 seconds)."""

    def __init__(self, *a, stuck: frozenset[str] = frozenset(), **kw):
        super().__init__(*a, **kw)
        self.stuck = stuck

    def cancel_all(self) -> CancelAllResult:
        self._log("cancel_all")
        keep = tuple(r for r in self.book.working if r.key in self.stuck)
        moved = tuple(replace(r, grid="canceled", status="CANCELED") for r in self.book.working if r not in keep)
        self.book = replace(self.book, working=keep, canceled=self.book.canceled + moved)
        return CancelAllResult(True, True)


def _with_send(rig: SimulatedRig, key: str = "s1") -> None:
    rig.book = replace(rig.book, working=rig.book.working + (OrderRow(key, "working", "WORKING", "SPY", 1.0),))


def test_kill_switch_cancels_declared_sends_and_proves_only_declared_sends():
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    start = rig.read_orders()
    _with_send(rig)
    rep = execute_kill_switch(rig, kill_switch("file_flag", gate="running"), clock=clock, allowlist=ALLOW, start=start,
                              fixture_account=EMPTY, declared_sends=(sent(),))
    assert "cancel_all" in rig.calls and rep.proof.cancel_all == "done"
    assert rep.proof.verdict == PROVEN_DECLARED and rep.proof.counts["sends"][0]["canceled"] == "s1"
    assert rep.proof_effect.passed


def test_a_declared_row_cancel_all_leaves_working_is_unproven_cancel_all_failed_and_pages_the_login_owner():
    clock = ManualClock()
    rig = StuckRig(clock=clock, stuck=frozenset({"s1"}))
    start = rig.read_orders()
    _with_send(rig)
    t0 = clock.monotonic()
    rep = execute_kill_switch(rig, kill_switch("file_flag", gate="running"), clock=clock, allowlist=ALLOW, start=start,
                              fixture_account=EMPTY, declared_sends=(sent(),))
    assert clock.monotonic() - t0 >= 60.0 and rig.calls.count("cancel_all") == 1          # re-read for 60 s
    assert (rep.proof.verdict, rep.proof.reason, rep.proof.cancel_all) == (UNPROVEN, "cancel_all_failed", "failed")
    e = rep.proof_effect
    assert e.run is None                                                                 # stays aborted_safety
    assert e.alert == alerts.safety_abort().plus(paged=("login_owner",))                # the safety page re-sent
    assert e.has("block_login") and e.evidence["blocked_reason"] == "cancel_all_failed"
    assert e.evidence["account"] == "account D-xxxxxxxx (allowlist entry 2) on rig rig-01"
    assert e.evidence["instruction"] == CANCEL_BY_HAND and CANCEL_BY_HAND in e.detail and EMPTY not in e.detail


def test_cancel_all_rereads_until_no_new_row_of_any_kind_remains():
    clock = ManualClock()

    class SlowRig(SimulatedRig):
        """Cancel All clicks at once; Working clears on the third read after it."""
        reads = 0

        def cancel_all(self) -> CancelAllResult:
            self._log("cancel_all")
            return CancelAllResult(True, True)

        def read_orders(self) -> OrderBook:
            self.reads += 1
            if self.reads == 3:
                self.book = replace(self.book, working=(), canceled=tuple(
                    replace(r, grid="canceled", status="CANCELED") for r in self.book.working))
            return self.book if self.reads != 2 else replace(self.book, readable=False)

    rig = SlowRig(clock=clock)
    start = OrderBook(net_liq=2500.0)
    _with_send(rig)
    cancel, end, found = cancel_new_working(rig, start, rig.book, clock=clock)
    assert cancel == "done" and rig.reads == 3 and not end.working and end.readable     # an unreadable read is no "clear"
    assert found is not None and [r.key for r in found.working] == ["s1"]               # the read that found the row
    assert cancel_new_working(rig, start, OrderBook(net_liq=2500.0), clock=clock)[0] == "not_needed"


class BlurAfterCancel(SimulatedRig):
    """A rig whose Monitor reads go bad once Cancel All is clicked (a confirmation popup over the grid): unreadable,
    or with `disagree` readable with no Working row but disagreeing with Copy All."""

    def __init__(self, *a, disagree: bool = False, **kw):
        super().__init__(*a, **kw)
        self.disagree, self.blurred = disagree, False

    def cancel_all(self) -> CancelAllResult:
        self.blurred = True
        return super().cancel_all() if not self.disagree else CancelAllResult(True, True)

    def read_orders(self) -> MonitorBook:
        book = super().read_orders()
        if not self.blurred:
            return book
        return replace(book, working=(), copy_all_agrees=False) if self.disagree else replace(book, readable=False)


@pytest.mark.parametrize("disagree", [False, True])
def test_a_clean_read_that_found_an_undeclared_row_is_orders_sent_though_every_reread_after_cancel_all_is_bad(
        disagree):
    """killswitch:0: the clean read that triggered Cancel All is kept (`ProofInputs.found`), so re-reads that are
    unreadable, or readable but disagree with Copy All, for 60 s give ORDERS SENT with Cancel All failed and the
    login-owner hand-over, not UNPROVEN monitor_unreadable."""
    clock = ManualClock()
    rig = BlurAfterCancel(clock=clock, disagree=disagree)
    start = rig.read_orders()
    rig._send()
    rep = _ks(rig, clock, start)
    assert rig.calls.count("cancel_all") <= 1 and rep.proof.cancel_all == "failed"
    assert rep.proof.line == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All failed"
    e = rep.proof_effect
    assert e.incident == "S1" and e.run_reason == "undeclared_order_row" and "login_owner" in e.alert.paged
    assert e.evidence["blocked_reason"] == "incident" and e.evidence["instruction"] == CANCEL_BY_HAND


def test_the_recovery_proof_after_a_killed_tree_keeps_the_clean_read_that_found_the_row():
    """killswitch:0 on machine._recovery_proof: a result that killed the tree (a deferred assertion past five minutes)
    with an order out; every re-read after Cancel All is unreadable, so the run is ORDERS SENT, not UNPROVEN."""
    from sentinel.orchestrator.identity import deferral_unreadable
    from sentinel.orchestrator.machine import Orchestrator
    from sentinel.orchestrator.simrig import MemorySink, default_context

    clock = ManualClock()
    rig = BlurAfterCancel(clock=clock)

    def work(o):
        rig._send()
        o.apply(deferral_unreadable(deferred_s=400.0))

    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=clock).run(work)
    assert rep.proof.line == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All failed"
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")


def test_an_order_found_at_teardown_stays_orders_sent_when_the_recovery_login_then_fails():
    """killswitch:1: the teardown read is clean and finds an order; the kill switch sequence's recovery login fails.
    The run's proof (the 8.3 row, the report) stays ORDERS SENT with Cancel All failed, and the one page blocks the
    login for `incident` with the hand-over; the sequence's own UNPROVEN line is kept beside it."""
    from sentinel.orchestrator.machine import Orchestrator
    from sentinel.orchestrator.simrig import MemorySink, default_context

    clock = ManualClock()
    rig = SimulatedRig(SimScenario(recovery_login_fails=1), clock=clock)

    def work(o):
        o.sink.record("c1", "passed")
        rig._send()

    rep = Orchestrator(rig, default_context(), MemorySink(["c1", "c2"]), clock=clock).run(work)
    assert rep.proof.line == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All failed"
    assert (rep.proof.verdict_code, rep.proof.cancel_all_code) == ("orders_sent", "performed_unverified")
    assert rep.proof.counts["undeclared"] == ["sim-order-1"]
    (page,) = [r for r in rep.results if r.alert is not None]
    assert page.evidence["verdict"] == rep.proof.line and page.evidence["undeclared"] == ["sim-order-1"]
    assert page.evidence["blocked_reason"] == "incident" and page.evidence["instruction"] == CANCEL_BY_HAND
    assert page.evidence["kill_switch_proof"] == "UNPROVEN: recovery_login_failed" == rep.kill_switch.proof.line
    assert page.evidence["teardown_undeclared"] == ["sim-order-1"] and not page.findings
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")


LIVE = SelectorRead("12345678SCHW", crop_sha256="crop-live")


def test_a_live_account_at_the_recovery_login_is_live_identity_s1_and_the_proof_session_unavailable():
    """killswitch:3: the file-flag kill switch's recovery login reads a live-format id. The sequence reports the 6.4
    result (aborted_safety live_identity, S1, the login blocked for live_identity, the selector crop) for the caller to
    apply, reads nothing more through that session, and the proof is UNPROVEN session_unavailable (SD-64) with no
    cancel-by-hand hand-over through it."""
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(selector_reads=[LIVE]), clock=clock)
    rep = _ks(rig, clock, rig.read_orders())
    i = rep.identity
    assert i is not None and (i.run_status, i.run_reason, i.incident) == ("aborted_safety", "live_identity", "S1")
    assert i.has("block_login") and i.evidence["crop"] == "crop-live"
    assert (rep.proof.verdict, rep.proof.reason) == (UNPROVEN, "session_unavailable")
    assert rig.calls.count("read_orders") == 1                                           # the start read only
    e = rep.proof_effect
    assert e.evidence["blocked_reason"] == "live_identity" and "instruction" not in e.evidence
    assert "login_owner" not in e.alert.paged


def test_a_foreign_paper_id_at_the_recovery_login_is_paper_mode_mismatch_and_recovery_login_failed():
    """killswitch:3: any other text that is not an allowlist entry is the S2 mismatch; the proof stays UNPROVEN
    recovery_login_failed with the hand-over."""
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(selector_reads=[SelectorRead("D-99999999")]), clock=clock)
    rep = _ks(rig, clock, rig.read_orders())
    assert (rep.identity.run_reason, rep.identity.incident) == ("paper_mode_mismatch", "S2")
    assert not rep.identity.has("block_login")
    assert (rep.proof.reason, rep.proof_effect.evidence["blocked_reason"]) == ("recovery_login_failed",) * 2
    assert rep.proof_effect.evidence["instruction"] == CANCEL_BY_HAND
    clean_rig = SimulatedRig(clock=clock)
    assert _ks(clean_rig, clock, clean_rig.read_orders()).identity is None


def test_the_orchestrator_applies_a_live_identity_its_kill_switch_recovery_login_saw():
    """killswitch:3 through the Orchestrator: S1, the login blocked for live_identity on every result that blocks."""
    from sentinel.orchestrator.machine import Orchestrator
    from sentinel.orchestrator.simrig import MemorySink, default_context
    from sentinel.store.safety import blocked_reason_for

    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    flag, _ = rig.kill_channels()

    def work(o):
        rig.s.selector_reads.append(LIVE)                     # the next read is the recovery login's
        flag.post(KillRequest("rig-01", "cli", "oncall", "stop", "t"))
        assert o.step_boundary() is o.stop is not None

    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=clock).run(work)
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "kill_switch")      # the first abort stands
    live = [r for r in rep.results if r.run_reason == "live_identity"]
    assert len(live) == 1 and live[0].incident == "S1"
    assert {blocked_reason_for(r) for r in rep.results if r.has("block_login")} == {"live_identity"}
    assert rep.proof.line == "UNPROVEN: session_unavailable"


def test_the_proof_takes_orders_sent_from_a_clean_found_read_and_names_that_read():
    """killswitch:0: `found` only ever adds undeclared rows, and only from a clean read; its sha is the end read the
    proof compared when the last read was not clean."""
    row = OrderRow("x1", "working", "WORKING")
    found = OrderBook(working=(row,), net_liq=2500.0)
    p = post_run_proof(ProofInputs(START, replace(found, readable=False), cancel_all="failed", found=found))
    assert p.line == "ORDERS SENT: undeclared rows (x1); Cancel All failed" and p.end_sha256 == book_sha256(found)
    dirty = post_run_proof(ProofInputs(START, None, cancel_all="failed", found=replace(found, copy_all_agrees=False)))
    assert (dirty.verdict, dirty.reason) == (UNPROVEN, "monitor_unreadable")
    canceled = OrderBook(canceled=(replace(row, grid="canceled", status="CANCELED"),), net_liq=2500.0)
    both = post_run_proof(ProofInputs(START, canceled, cancel_all="done", found=found))
    assert both.line == "ORDERS SENT: undeclared rows (x1); Cancel All verified"     # one row, by its key
    vanished = post_run_proof(ProofInputs(START, START, cancel_all="done", found=found))
    assert vanished.verdict == ORDERS_SENT and [r.key for r in vanished.undeclared] == ["x1"]


def test_a_failed_recovery_login_pages_the_login_owner_and_blocks_with_its_reason():
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(recovery_login_fails=1), clock=clock)
    rig.login()
    rep = _ks(rig, clock, rig.read_orders())
    e = rep.proof_effect
    assert (rep.proof.verdict, rep.proof.reason) == (UNPROVEN, "recovery_login_failed")
    assert "login_owner" in e.alert.paged and e.alert.rule == "safety:aborted_safety"
    assert e.evidence["blocked_reason"] == "recovery_login_failed" and e.has("block_login")
    assert e.evidence["working_orders_screenshot"] is None and "no Working Orders read" in e.detail
    assert e.detail.startswith("UNPROVEN: recovery_login_failed; account D-xxxxxxxx (allowlist entry 2) on rig rig-01")


def test_the_hand_over_page_carries_the_working_orders_screenshot_of_the_last_read():
    shot = MonitorBook(working=(OrderRow("s1", "working", "WORKING"),), net_liq=2500.0, buying_power=5000.0,
                       screenshot_sha256="sha-wo")
    p = post_run_proof(ProofInputs(START, shot, cancel_all="failed", declared_sends=(sent(),)))
    r = proof_result(p, account=account_ref(ALLOW, GOLDEN), screenshot_sha256=shot.screenshot_sha256)
    assert r.evidence["working_orders_screenshot"] == "sha-wo" and "Working Orders screenshot sha-wo" in r.detail
    assert r.alert == alerts.unproven_proof().plus(paged=("login_owner",))              # the run was not aborted
    assert account_ref(ALLOW, GOLDEN) == "account D-xxxxxxxx (allowlist entry 1) on rig rig-01"
    assert account_ref(ALLOW, "D-1") == account_ref(ALLOW, None) == "the fixture account on rig rig-01"
    assert masked_account("D-20481133") == "D-xxxxxxxx" and masked_account("1234") == "xxxx"


def test_orders_sent_with_cancel_all_failed_keeps_its_line_and_carries_the_hand_over():
    end = OrderBook(working=(OrderRow("x1", "working", "WORKING"),), net_liq=2500.0)
    p = post_run_proof(ProofInputs(START, end, cancel_all="failed"))
    r = proof_result(p, account="account D-xxxxxxxx (allowlist entry 2) on rig rig-01")
    assert r.detail == p.line and r.evidence["instruction"] == CANCEL_BY_HAND
    assert r.evidence["blocked_reason"] == "incident" and "login_owner" in r.alert.paged
    verified = proof_result(post_run_proof(ProofInputs(START, end, cancel_all="done")))
    assert "instruction" not in verified.evidence and verified.evidence["blocked_reason"] == "incident"


def test_after_live_identity_no_recovery_login_is_made_and_the_proof_is_session_unavailable():
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    decision = kill_switch("live_identity", gate="paper_mode_asserted")
    assert not decision.has("recovery_login") and decision.evidence["blocked_reason"] == "live_identity"   # SD-64
    rep = execute_kill_switch(rig, decision, clock=clock, allowlist=ALLOW, start=rig.read_orders(),
                              fixture_account=EMPTY)
    assert "login" not in rig.calls and "launch" not in rig.calls
    assert [s for s, _ in rep.steps] == ["deny_all", "kill_tree", "proof", "revert_snapshot"]
    assert (rep.proof.verdict, rep.proof.reason) == (UNPROVEN, "session_unavailable")
    e = rep.proof_effect
    assert e.run is None and e.has("block_login") and e.evidence["blocked_reason"] == "live_identity"
    assert "login_owner" not in e.alert.paged                                            # not a hand-over case
    kicked = proof_result(post_run_proof(ProofInputs(START, None, session="kicked", kicked_login_ok=False)))
    assert kicked.evidence["blocked_reason"] == "session_unavailable"


def test_buying_power_is_a_proof_line_and_an_unexplained_drift_is_monitor_unreadable():
    start = MonitorBook(net_liq=2500.0, buying_power=5000.0)
    p = post_run_proof(ProofInputs(start, replace(start, buying_power=5000.25)))
    assert p.verdict == PROVEN_NONE
    assert p.counts["rows"]["buying power"] == {"start": 5000.0, "end": 5000.25, "drift": 0.25, "verdict": "ok"}
    drift = post_run_proof(ProofInputs(start, replace(start, buying_power=4000.0)))
    assert (drift.verdict, drift.reason) == (UNPROVEN, "monitor_unreadable")
    assert drift.counts["rows"]["buying power"]["verdict"] == "drift"
    assert "buying power" not in post_run_proof(ProofInputs(START, START)).counts["rows"]   # a read without it


def test_the_config_invalid_run_level_proof_is_written_and_read_back(tmp_path):
    row = config_invalid_proof("run-cfg", produced_at="2026-11-10T15:00:00Z")
    assert (row.shard_id, row.login_alias, row.verdict, row.reason, row.cancel_all) == (
        None, None, "proven_no_session", None, "not_needed")
    assert (row.gateway_events, row.gateway_drops, row.send_policy_ref) == (0, 0, "OPEN")
    with Store(tmp_path / "s.db") as store:
        store.begin_run(RunPlan("run-cfg", "pr", "cfg", "0" * 64, "0" * 64, "h", "AUTH", 2700, ()))
        store.write_proof(row)                                                          # SD-61: shard_id NULL
        (back,) = store.post_run_proofs("run-cfg")
    assert (back.shard_id, back.verdict, back.counts["rows"]) == (None, "proven_no_session", {})
    leaked = config_invalid_proof("run-cfg", produced_at="2026-11-10T15:00:00Z", gateway=GatewayLog(tokens_issued=1))
    assert leaked.verdict == "unproven" and leaked.reason == "snapshot_missing"


def test_every_blocked_reason_the_proof_names_is_an_8_3_logins_value():
    check = re.search(r"blocked_reason\s+TEXT CHECK \(blocked_reason IN \(([^)]*)\)\)", SCHEMA.read_text(), re.S)
    allowed = set(re.findall(r"'([a-z_]+)'", check.group(1)))
    assert set(UNPROVEN_BLOCK.values()) | {"incident", "live_identity"} <= allowed


# -- 6.8 Explorer boundary ------------------------------------------------------------------------------
def session(clock: ManualClock, **kw) -> ExplorerSession:
    return ExplorerSession("explorer-1", clock, ledger=kw.pop("ledger", DropLedger()), **kw)


@pytest.mark.parametrize("title,labels,kind,hit", [
    ("Chat Rooms", (), None, "chat"),
    (None, ("Share",), None, "share"),
    (None, ("Application Settings",), None, "setup"),
    (None, ("Adjust Account",), None, "adjust_account"),
    (None, ("Log Out",), None, "logout_account_switch"),
    (None, ("Export",), None, "leaves_rig_or_persists"),
    (None, ("Blast All",), None, "persistent_and_send"),
    (None, ("Buy custom",), None, "opens_ticket"),
    (None, ("SELL MKT",), None, "opens_ticket"),
    (None, ("1.25",), "ladder_bid_ask_cell", "opens_ticket"),
    (None, ("Zoom in",), None, None),
])
def test_deny_list_by_title_label_and_geometry(title, labels, kind, hit):
    assert match_deny(title=title, labels=labels, target_kind=kind) == hit


def test_explorer_budgets_400_actions_90_minutes_20_no_change_25_dollars():
    clock = ManualClock()
    s = session(clock)
    for i in range(399):
        assert s.record_action(f"h{i}") is None
    r = s.record_action("h-last")
    assert r.evidence["budget"] == "actions" and r.has("end_session") and r.run is None and s.ended
    s = session(clock)
    clock.advance(90 * 60)
    assert s.record_action("a").evidence["budget"] == "minutes"
    s = session(clock)
    s.record_action("same")
    for _ in range(19):
        assert s.record_action("same") is None
    assert s.record_action("same").evidence["budget"] == "no_change"
    s = session(clock)
    assert s.record_action("a", usd=24.0) is None
    assert s.record_action("b", usd=1.0).evidence["budget"] == "usd_session"
    s = session(clock, week_spent_usd=499.5)
    assert s.record_action("a", usd=0.5).evidence["budget"] == "usd_week"
    s = session(clock, deadline_mono=clock.monotonic() + 60)
    clock.advance(61)
    assert s.record_action("a").evidence["budget"] == "rc_deadline"
    with pytest.raises(RuntimeError):
        s.record_action("b")
    assert ExplorerBudget.from_policy({"max_actions": 10}).max_actions == 10


def test_explorer_three_denied_attempts_end_the_session_and_a_send_ends_it_at_once():
    clock = ManualClock()
    s = session(clock)
    d = GatewayDrop("deny_profile", "refused", "integrity", "explorer")
    s.record_drop(d)
    s.record_drop(d)
    assert not s.ended
    assert s.record_drop(d).has("end_session") and s.ended
    s2 = session(clock)
    r = s2.record_drop(GatewayDrop("deny_rect", "dropped", "safety", "explorer"))
    assert s2.ended and r.incident == "S3" and r.run is None


def test_explorer_order_stage_finding_counts_toward_ten_and_the_run_continues():
    clock = ManualClock()
    ledger = DropLedger()
    r = session(clock, ledger=ledger).order_stage()
    assert r.run is None and r.findings[0].kind == "explorer_order_stage" and r.incident == "S1"
    assert set(r.actions) == {"end_session", "close_window_escape"} and "kill_switch" not in r.actions
    assert r.alert == alerts.explorer_order_stage() and ledger.run_count == 1
    ledger.run_count = 9
    trip = session(clock, ledger=ledger).order_stage()
    assert trip.run_reason == "kill_switch"


def test_explorer_not_started_when_the_model_is_down_and_teardown_ledger():
    r = not_started_model_unavailable()
    assert r.outcomes[0].selector == "explorer_stage" and r.outcomes[0].outcome.reason == "model_unavailable"
    assert explorer_teardown(["watchlist X"], {"watchlist X": True}).passed
    t = explorer_teardown(["alert A", "study B"], {"alert A": True})
    o = t.outcomes[0].outcome
    assert (o.outcome, o.reason, o.gate, o.detail) == ("env_error", "precondition:explorer_ledger_clean",
                                                       "teardown", "study B")
    assert t.has("block_login") and t.alert.priority == "P2"
