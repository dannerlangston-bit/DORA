"""Paper-mode assertion, settings attestation, fire drill, kill switch and proof, Explorer boundary (6.3 to 6.8)."""
from __future__ import annotations

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
from sentinel.orchestrator.killswitch import (ORDERS_SENT, PROVEN_DECLARED, PROVEN_NO_SESSION, PROVEN_NONE,
                                              UNPROVEN, Proof, ProofInputs, TripWires, check_tripwires,
                                              execute_kill_switch, kill_switch, post_run_proof, proof_result)
from sentinel.orchestrator.results import RunVerdict
from sentinel.orchestrator.rig import (ConfirmationCheck, HotkeyRow, NotificationsRead, OrderBook, OrderRow, Rig,
                                       SelectorRead, SettingRead)
from sentinel.orchestrator.simrig import EMPTY, GOLDEN, SimScenario, SimulatedRig, default_settings

ALLOW = Allowlist("rig-01", (GOLDEN, EMPTY), "0" * 64)
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example" / "gateway" / "allowlist.yaml"


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


def test_a_failed_cancel_of_declared_sends_alone_is_unproven_and_the_verdicts_are_consistent():
    declared = OrderBook(working=(OrderRow("s1", "working", "WORKING"),), net_liq=2500.0)
    p = post_run_proof(ProofInputs(start=START, end=declared, cancel_all="failed", declared_sends=frozenset({"s1"})))
    assert (p.verdict, p.reason, p.line) == (UNPROVEN, "cancel_all_failed", "UNPROVEN: cancel_all_failed")
    row = (OrderRow("x1", "working", "WORKING"),)
    with pytest.raises(ValueError, match="ORDERS SENT"):
        Proof(ORDERS_SENT)                                       # ORDERS SENT always names its rows
    with pytest.raises(ValueError, match="ORDERS SENT"):
        Proof(UNPROVEN, "cancel_all_failed", row)                # undeclared rows are never only UNPROVEN


def test_proof_only_declared_sends_in_the_gated_branch():
    end = OrderBook(canceled=(OrderRow("s1", "canceled", "CANCELED"),), net_liq=2500.0)
    p = post_run_proof(ProofInputs(start=START, end=end, declared_sends=frozenset({"s1"})))
    assert p.verdict == PROVEN_DECLARED and p.declared[0].key == "s1"


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
    assert r.has("block_login") and r.alert == alerts.unproven_proof()


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
    assert rig.gw.deny_all and "cancel_all" not in rig.calls


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
