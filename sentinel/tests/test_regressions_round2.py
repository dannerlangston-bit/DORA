"""Round-2 review regressions for sentinel.orchestrator, sentinel.reviewer and sentinel.oracle (R2-01 to R2-18).

Each test names its finding. The first test of every finding is the reviewer's failing test, moved here; the
others pin the decisions taken while fixing it. The `followup` tests at the end cover four items the
integrator found in the same packages. Self-contained: the reviewer helpers are copied from
test_reviewer.py, and nothing touches the network (the SDK client is only constructed)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest
import yaml

from sentinel.oracle.assertions import evaluate_assertions, summarize
from sentinel.oracle.sources import GroundTruthApi, GroundTruthReader, Reading, extract, problem
from sentinel.oracle.tables import EvalContext, OracleConfigError, api_comparison
from sentinel.oracle.values import digit_count, format_error, parse
from sentinel.orchestrator.attestation import AttestationProfile, RowReads, attest
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.drill import DrillObservation, DrillRecord, Item5, expected_records, score_drill
from sentinel.orchestrator.gateway import (DISMISS_ALLOWLIST, VK_F1, VK_RETURN, VK_TAB, Action, Flag, SafetyContext,
                                           refusal, slow_half)
from sentinel.orchestrator.explorer import match_deny
from sentinel.orchestrator.killswitch import PROVEN_NO_SESSION, PROVEN_NONE, ProofInputs, post_run_proof, recovery_login
from sentinel.report.model import ORDERS_SENT_PROOF, PROOF_FORMS, proof_severity
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import (ConfirmationCheck, HotkeyRow, LaunchObservation, OrderBook, OrderRow,
                                       SelectorRead, SettingRead)
from sentinel.orchestrator.simrig import (BUILD, CONFIRM_POINT, DELETE_POINT, EMPTY, GOLDEN, MemorySink, SimScenario,
                                          SimulatedRig, default_context)
from sentinel.orchestrator.watchdog import hook_host_died
from sentinel.reviewer import (BatchEntry, DependencyEdge, DiffMetrics, FlowContext, ManifestEntryRef, ModelResponse,
                               ReviewerConfig, ReviewerJob, ReviewItem, StepCrops, TransportError, Usage)
from sentinel.reviewer.schema import MATCHING_VERDICTS, TASK_A, TASK_B, TASK_C
from sentinel.reviewer.transport import DEFAULT_BASE_URL, AnthropicTransport

VK_F5 = VK_F1 + 4


# == orchestrator helpers ===================================================================================

def _armed_rig(scenario: SimScenario | None = None) -> SimulatedRig:
    """A simulated rig after launch, hook armed and A9 run (the state a scripted flow runs in)."""
    rig = SimulatedRig(scenario, clock=ManualClock())
    rig.launch()
    rig.set_hook_state(armed=True, deny_all=False)
    rig.confirmation_check()
    return rig


def _confirm_and_send(**kw: Any) -> Action:
    return Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send", under_point_text="Confirm and Send",
                  order_stage_step=True, **kw)


def _order_slips_in(rig: SimulatedRig, also=None):
    def work(o: Orchestrator) -> None:
        o.sink.record("c1", "passed")
        rig._send()                       # an order the gateway did not see (or a bypass): a new Working row
        if also is not None:
            o.apply(also(o))
    return work


def _hook_dies(o: Orchestrator):
    return hook_host_died(o.budgets, step_id="s03", layout_mismatch=True)


# == R2-01: Confirm and Send plus a declared Enter in one order-stage action ================================

def test_r2_01_confirm_and_send_plus_declared_enter_in_one_action_never_reaches_the_dialog():
    rig = _armed_rig()
    res = rig.send_input(_confirm_and_send(keys=(VK_RETURN,), declared_guarded=True))
    stopped = res.refused is not None or any(not d.allowed for d in res.decisions)
    assert rig.sent_orders == 0 and rig.book.working == (), "an order reached the Working grid"
    assert stopped, "the Enter was neither refused in the slow half nor dropped by the hook"


def test_r2_01_a_guarded_key_in_an_order_stage_is_refused_as_an_attempted_send():
    s = slow_half(_confirm_and_send(keys=(VK_RETURN,), declared_guarded=True),
                  SafetyContext(attestation_passed=True, paper_assertion_current=True,
                                dismiss_allowlist=DISMISS_ALLOWLIST))
    assert (s.reason, s.attempted_send) == ("guarded_key_refused", True)
    assert refusal(s).attempted_send
    later = Action("press", keys=(VK_RETURN,), declared_guarded=True)          # a later step, sticky flag held
    s = slow_half(later, SafetyContext(order_stage=True))
    assert (s.reason, s.attempted_send) == ("guarded_key_refused", True)
    assert not slow_half(later, SafetyContext()).refused                         # declared Enter outside it


def test_r2_01_the_rig_holds_the_sticky_flag_until_the_close_dialog_teardown():
    rig = _armed_rig()
    r = rig.send_input(_confirm_and_send())
    assert r.refused is None and all(d.allowed for d in r.decisions) and rig.dialog_open
    assert rig.order_stage and rig.gw.flag == Flag.SET
    r = rig.send_input(Action("press", keys=(VK_RETURN,), declared_guarded=True))
    assert r.refused.reason == "guarded_key_refused" and r.refused.attempted_send and rig.sent_orders == 0
    rig.send_input(Action("click", point=DELETE_POINT, resolved_name="Delete", under_point_text="Delete"))
    assert not rig.dialog_open and rig.order_stage                       # no teardown action: still declared
    rig.send_input(Action("click", point=DELETE_POINT, resolved_name="Delete", under_point_text="Delete",
                          teardown_action="close_dialog"))
    assert not rig.order_stage and rig.gw.flag == Flag.CLEAR


def test_r2_01_the_hook_reads_the_flag_per_event_within_one_action():
    """Even past the slow half, the Enter that follows the Confirm and Send click in the same emission meets
    the open dialog's flag: the Watcher is polled after every event, as the real hook reads it per event."""
    rig = _armed_rig()
    out = rig._emit(Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send",
                           under_point_text="Confirm and Send", keys=(VK_RETURN,)), True)
    enter = [d for e, d in out if e.type == "key_down"]
    assert rig.dialog_open and enter[0].reason == "blocked_key_dialog" and rig.sent_orders == 0


# == R2-02: agent or Explorer input while the ticket flag is set =============================================

def test_r2_02_explorer_input_is_dropped_while_the_ticket_flag_is_set():
    rig = _armed_rig()
    rig.ticket_visible = True
    res = rig.send_input(Action("click", profile="explorer", point=(400, 300), resolved_name="Qty",
                                under_point_text="100"))
    assert rig.gw.flag != Flag.CLEAR
    assert res.refused is not None or not any(d.allowed for d in res.decisions if d.token_id is not None), \
        "an Explorer click was delivered to the app while the ticket flag was set"


def test_r2_02_flag_refusals_take_the_flag_abort_path_and_sends_stay_attempted_sends():
    rig = _armed_rig()
    rig.ticket_visible = True
    for profile in ("explorer", "agent"):
        r = rig.send_input(Action("press", profile=profile, keys=(VK_TAB,)))
        assert (r.refused.reason, r.refused.flag_abort, r.refused.attempted_send) == ("deny_profile", True, False)
        r = rig.send_input(Action("press", profile=profile, keys=(VK_RETURN,)))
        assert (r.refused.reason, r.refused.attempted_send) == ("guarded_key_refused", True)
    assert rig.flag_aborts == ["explorer", "agent"]
    s = slow_half(Action("press", profile="explorer", keys=(VK_TAB,)), SafetyContext(flag=Flag.UNKNOWN))
    assert s.refused and s.flag_abort                                     # unknown is handled as set


# == R2-03: agent and Explorer keys whatever the action kind ================================================

def test_r2_03_agent_profile_cannot_emit_a_function_key_through_a_secret_or_click_action():
    ctx = SafetyContext()
    for profile in ("agent", "explorer"):
        for a in (Action("secret", profile=profile, keys=(VK_F5,)),
                  Action("click", profile=profile, point=(10, 10), resolved_name="Watchlist", keys=(VK_F5,))):
            assert slow_half(a, ctx).refused, f"{profile} {a.kind} with F5 was cleared by the slow half"


def test_r2_03_only_the_5_6_toolset_is_cleared_for_agent_and_explorer():
    ctx = SafetyContext()
    for profile in ("agent", "explorer"):
        for kind in ("secret", "dblclick", "rclick"):
            assert slow_half(Action(kind, profile=profile, point=(10, 10), resolved_name="Watchlist"),
                             ctx).reason == "deny_profile"
        shift_click = Action("click", profile=profile, point=(10, 10), resolved_name="Watchlist",
                             mods=frozenset({"shift"}))
        assert slow_half(shift_click, ctx).reason == "deny_profile"
        shift_tab = Action("press", profile=profile, keys=(VK_TAB,), mods=frozenset({"shift"}))
        assert not slow_half(shift_tab, ctx).refused                                  # `shift_tab` is offered
        assert not slow_half(Action("click", profile=profile, point=(10, 10), resolved_name="Watchlist"),
                             ctx).refused


# == R2-04: agent drag onto order geometry ==================================================================

def test_r2_04_agent_drag_that_ends_on_order_geometry_is_refused_like_a_scripted_one():
    ctx = SafetyContext()
    scripted = Action("drag", point=(500, 400), resolved_name="chart", end_kind="order_bubble")
    assert slow_half(scripted, ctx).refused
    agent = Action("drag", profile="agent", point=(500, 400), resolved_name="chart", end_kind="order_bubble")
    assert slow_half(agent, ctx).refused, "agent drag ending on an order bubble was cleared"
    rig = _armed_rig()
    res = rig.send_input(Action("drag", profile="agent", point=(500, 400), resolved_name="chart",
                                end_kind="ladder_order_cell"))
    assert res.refused is not None, "the simulated rig emitted an agent drag onto a ladder order cell"


def test_r2_04_order_geometry_is_denied_in_every_profile_and_the_deny_list_sees_the_end_target():
    assert match_deny(title=None, labels=("chart",), target_kind="splitter", end_kind="order_bubble") == "opens_ticket"
    assert match_deny(title=None, labels=("divider",), target_kind="splitter", end_kind="detach_handle") is None
    click = Action("click", profile="agent", point=(5, 5), resolved_name="cell", target_kind="ladder_order_cell")
    assert slow_half(click, SafetyContext()).reason == "deny_profile"
    staged = Action("click", point=(5, 5), resolved_name="cell", target_kind="ladder_order_cell", order_stage_step=True)
    assert not slow_half(staged, SafetyContext()).refused                 # a scripted order-stage step only


# == R2-05: fire drill item 4 ===============================================================================

def test_r2_05_fire_drill_item_4_expects_the_reason_the_slow_half_actually_gives_for_the_ladder_cell():
    ctx = SafetyContext(flag=Flag.SET)
    shift = frozenset({"shift"})
    ticket = slow_half(Action("click", point=(100, 100), resolved_name="Buy", under_point_text="Buy", mods=shift), ctx)
    ladder = slow_half(Action("click", point=(200, 200), resolved_name="ladder.buy-cell", under_point_text="5,325.00",
                              mods=shift, target_kind="ladder_order_cell"), ctx)
    assert ticket.refused and ladder.refused
    chords = default_context().default_order_chords
    records = [r for r in expected_records(chords).elements() if r.item != 4]
    records += [DrillRecord(4, "gateway", "refused", refusal(ticket).reason),
                DrillRecord(4, "gateway", "refused", refusal(ladder).reason),
                DrillRecord(4, "raw", "dropped", "shift_click_dialog"),
                DrillRecord(4, "raw", "dropped", "shift_click_dialog")]
    obs = DrillObservation(attestation_passed=True, probe_dropped_age_s=1.0, injector_signed=True,
                           ticket_on_empty=True, records=tuple(records),
                           item5=Item5(0.2, 2.0, True, True, PROVEN_NONE, True))
    result = score_drill(obs, chords)
    assert result.result.passed, f"a fully blocked drill failed: {result.failed} {result.result.checkpoint}"


# == R2-06: an undeclared order found by the proof is cancelled =============================================

def test_r2_06_undeclared_working_row_found_at_teardown_is_cancelled():
    rig = SimulatedRig(clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=rig.clock).run(_order_slips_in(rig))
    assert rep.verdict.status == "aborted_safety"
    assert "cancel_all" in rig.calls and rig.book.working == (), "the undeclared order was left Working"


def test_r2_06_undeclared_working_row_found_by_the_recovery_proof_is_cancelled():
    rig = SimulatedRig(clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1", "c2"]), clock=rig.clock).run(
        _order_slips_in(rig, also=_hook_dies))
    assert rep.verdict.status == "aborted_safety"
    assert "cancel_all" in rig.calls and rig.book.working == (), "the undeclared order was left Working"


def test_r2_06_the_teardown_path_runs_the_kill_switch_sequence_and_the_proof_records_the_cancel():
    rig = SimulatedRig(clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=rig.clock).run(_order_slips_in(rig))
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")
    assert rep.kill_switch is not None and [s for s, _ in rep.kill_switch.steps] == [
        "deny_all", "kill_tree", "recovery_login", "cancel_all", "proof", "revert_snapshot"]
    assert rep.proof.cancel_all == "done" and [r.key for r in rep.proof.undeclared] == ["sim-order-1"]
    assert rig.book.canceled[-1].key == "sim-order-1"
    rig = SimulatedRig(clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1", "c2"]), clock=rig.clock).run(
        _order_slips_in(rig, also=_hook_dies))
    assert rep.proof.cancel_all == "done"


# == R2-07: unreached checkpoints carry the final abort =====================================================

def test_r2_07_unreached_checkpoints_carry_the_abort_reason_of_the_final_aborted_safety_run():
    rig = SimulatedRig(clock=ManualClock())
    sink = MemorySink(["c1", "c2", "c3"])
    rep = Orchestrator(rig, default_context(), sink, clock=rig.clock).run(_order_slips_in(rig, also=_hook_dies))
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")
    for c in ("c2", "c3"):
        outcome, reason, gate, _ = sink.outcomes[c]
        assert (outcome, reason) == ("env_error", rep.verdict.reason) and gate, \
            f"{c} is {outcome} ({reason}) in an aborted_safety ({rep.verdict.reason}) run"


def test_r2_07_a_hook_abort_with_a_clean_proof_keeps_its_own_unreached_outcome():
    rig = SimulatedRig(clock=ManualClock())
    sink = MemorySink(["c1", "c2"])

    def work(o: Orchestrator) -> None:
        o.sink.record("c1", "passed")
        o.apply(_hook_dies(o))
    rep = Orchestrator(rig, default_context(), sink, clock=rig.clock).run(work)
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_harness", "hook_lost")
    assert sink.outcomes["c2"][:2] == ("harness_error", "hook_lost") and rep.filled == ["c2"]


# == R2-08: an authenticated, identity-asserted session is not "no session established" =====================

def test_r2_08_a2_failure_after_an_asserted_identity_is_not_proven_no_session_established():
    book = OrderBook(working=(OrderRow("ord-left-behind", "working", "WORKING", "AAPL", 1.0),), net_liq=2500.0)
    rig = SimulatedRig(SimScenario(select_works=False, selected=GOLDEN, book=book), clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=rig.clock).run()
    assert rep.gate("paper_mode_asserted") is not None and rep.gate("paper_mode_asserted").passed
    assert rep.proof is not None and rep.proof.verdict != PROVEN_NO_SESSION, \
        "an authenticated session with an asserted identity was proven 'no session established'"


def test_r2_08_the_start_snapshot_is_read_once_a1_passes_and_before_a2_selects():
    rig = SimulatedRig(SimScenario(select_works=False, selected=GOLDEN), clock=ManualClock())
    rep = Orchestrator(rig, default_context(), MemorySink(["c1"]), clock=rig.clock).run()
    assert rig.calls.index("read_orders") < rig.calls.index("select_account")
    assert rep.proof.verdict == PROVEN_NONE and "read_orders" in rig.calls[rig.calls.index("teardown"):]


# == R2-09: the account A2 selects is asserted against the allowlist ========================================

def test_r2_09_account_selected_by_a2_is_asserted_against_the_allowlist():
    rig = SimulatedRig(clock=ManualClock())
    rep = Orchestrator(rig, default_context(fixture_account="12345678SCHW"), MemorySink(["c1"]), clock=rig.clock).run()
    assert rig.selected == "12345678SCHW"
    assert rep.verdict.status == "aborted_safety", f"run {rep.verdict} on a non-allowlisted, live-format account"


def test_r2_09_a_selector_that_shows_a_live_id_after_the_select_aborts_live_identity():
    reads = [SelectorRead(GOLDEN), SelectorRead("12345678SCHW")]
    rig = SimulatedRig(SimScenario(selected=GOLDEN, selector_reads=reads), clock=ManualClock())
    sink = MemorySink(["c1"])
    rep = Orchestrator(rig, default_context(), sink, clock=rig.clock).run()
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "live_identity")
    assert sink.outcomes["c1"][:3] == ("env_error", "live_identity", "paper_mode_asserted")
    assert rep.kill_switch is not None and rig.gw.deny_all


def test_r2_09_the_recovery_login_refuses_a_selected_account_outside_the_allowlist():
    ctx = default_context()
    rig = SimulatedRig(SimScenario(selected=EMPTY), clock=ManualClock())
    assert recovery_login(rig, ctx.allowlist, rig.clock, EMPTY)
    assert not recovery_login(rig, ctx.allowlist, rig.clock, "12345678SCHW") and rig.selected == "12345678SCHW"


# == R2-10: A7 Off on every panel ===========================================================================

def test_r2_10_a7_auto_send_on_for_any_panel_read_is_a_mismatch():
    profile = AttestationProfile(active_trader_panels=("at-panel-1",))
    values = {"A3": True, "A4": False, "A5": False, "A6": (HotkeyRow("Buy at market"),),
              "A7": {"at-panel-1": False, "at-panel-2": True}, "A8": 0}
    reads = {i: RowReads(SettingRead(i, v, screenshot_sha256=f"sha-{i}")) for i, v in values.items()}
    ev = {i: f"sha-{i}" for i in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9")}
    res = attest(reads, profile, a9=ConfirmationCheck(True, 0, "sha-A9"), evidence=ev)
    assert res.rows["A7"] == "mismatch" and not res.result.passed, "Auto Send ON on a panel attested as a match"


# == R2-11: a start snapshot that failed its Copy All cross-check ===========================================

def test_r2_11_proof_with_a_start_snapshot_that_disagreed_with_copy_all_is_not_proven():
    row = OrderRow("ord-1", "working", "WORKING", "AAPL", 1.0)
    start = OrderBook(working=(row,), net_liq=2500.0, copy_all_agrees=False)
    end = OrderBook(working=(row,), net_liq=2500.0)
    proof = post_run_proof(ProofInputs(start=start, end=end))
    assert not proof.proven, f"{proof.verdict} with a Working row and a start read that failed its cross-check"
    assert proof.reason == "monitor_unreadable"


# == reviewer helpers (copied from test_reviewer.py) ========================================================

MODEL = "claude-opus-5-5"
OTHER_MODEL = "claude-opus-5"
S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
ENTRY = ManifestEntryRef("tos-48120-1", ("TOS-48120",), "Buy button turns green on the order confirmation",
                         ("dialog.order-confirmation",), "The Buy button background changes from blue to green.")
EDGE = DependencyEdge("FlexibleGrid", "tos-48120-1")
DEADLINE = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
EARLY = DEADLINE - timedelta(hours=2)


def _pattern(w: int, h: int, seed: int = 0) -> np.ndarray:
    return ((np.arange(w * h * 3, dtype=np.uint32).reshape(h, w, 3) * (seed + 7)) % 251).astype(np.uint8)


def make_item(task: str = TASK_A, key: str = "run-1|s07|buy-button|c1|d1", *, region: str = "buy-button",
              base_hash: str = "b" * 64) -> ReviewItem:
    return ReviewItem(
        task=task, queue_key=key, checkpoint_ids=(S07,), region=region, tier=1, data_mode="frozen",
        data_epoch="ep-2026-11-02", workspace_id="ws.tier-a.swing-trader", window_title="Order Confirmation",
        diff=DiffMetrics(1200, ((10, 10, 600, 1),), (0, 0), ("hue_shift",)),
        flow=FlowContext(("click order-entry.buy", "type_fixture qty", "press review"),
                         "Order Confirmation dialog with Send and Cancel", ("Send",), ("mask: clock",)),
        steps=(StepCrops(None, _pattern(120, 60, 0), _pattern(120, 60, 1), _pattern(120, 60, 2), False),),
        entry=None if task == TASK_B else ENTRY, edge=EDGE if task == TASK_B else None,
        base_hash=base_hash, candidate_hash="c" * 64, mask_version="m1", redaction_version="r1",
        registry_version="g1")


def answer(task: str = TASK_A, verdict: str | None = None, confidence: float = 0.95) -> dict:
    verdict = verdict or {TASK_A: "matches_manifest", TASK_B: "does_not_match_manifest", TASK_C: "not_applicable"}[task]
    return {"schema_version": "reviewer-1", "task": task, "verdict": verdict, "confidence": confidence,
            "matched_manifest_entry_id": ENTRY.entry_id if verdict in MATCHING_VERDICTS else None,
            "observed_changes": [{"change_type": "color_change",
                                  "pointer": {"image": "candidate", "x": 10, "y": 10, "w": 30, "h": 20},
                                  "explained_by_manifest": verdict in MATCHING_VERDICTS, "text": "Buy"}],
            "description": "The Buy button is green.",
            "visibility": "visible" if task == TASK_C else "not_applicable", "uncertainty_reasons": ["none"]}


def resp(obj: Any = None, *, text: str | None = None, model: str = MODEL, stop: str = "end_turn") -> ModelResponse:
    return ModelResponse("msg_1", model, stop, json.dumps(obj) if text is None else text, Usage(3000, 300, 0, 0),
                         "req_1", None)


class FakeTransport:
    """Canned responses: a script consumed in call order, then `respond(params)`; batches by id."""

    def __init__(self, script: list = (), respond: Callable[[dict], Any] | None = None):
        self.script, self.respond = list(script), respond
        self.calls: list[dict] = []
        self.batches: dict[str, list[tuple[str, dict]]] = {}
        self.results: dict[str, list] = {}
        self._lock = threading.Lock()

    def create_message(self, params: dict) -> ModelResponse:
        with self._lock:
            self.calls.append(params)
            nxt = self.script.pop(0) if self.script else self.respond(params)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def retrieve_model(self, model_id: str) -> bool:
        return model_id == MODEL

    def create_batch(self, requests: list) -> str:
        bid = f"msgbatch_{len(self.batches) + 1:02d}"
        self.batches[bid] = list(requests)
        return bid

    def batch_ended(self, batch_id: str) -> bool:
        return batch_id in self.results

    def batch_results(self, batch_id: str):
        return iter(self.results[batch_id])


def config(**kw: Any) -> ReviewerConfig:
    return ReviewerConfig(**{"sync_concurrency": 1, **kw})


def sync_job(fake: FakeTransport) -> ReviewerJob:
    return ReviewerJob(config(), fake, "pr", clock=lambda: 0.0)


def batch_job(fake: FakeTransport) -> ReviewerJob:
    return ReviewerJob(config(zero_data_retention=False), fake, "merge", deadline=DEADLINE, clock=lambda: 0.0)


def _batch_verdict(by_copy: Callable[[int], ModelResponse]):
    fake = FakeTransport()
    j = batch_job(fake)
    j.review([make_item(TASK_A, key="run-7|a1", region="r1", base_hash="1" * 64)])
    fake.results["msgbatch_01"] = [BatchEntry(cid, "succeeded", by_copy(int(cid.split("-")[2])))
                                   for cid, _ in fake.batches["msgbatch_01"]]
    return j.poll(EARLY)["run-7|a1"]


# == R2-12: a vote copy answered by another model ===========================================================

def test_r2_12_sync_vote_copy_answered_by_another_model_makes_the_item_unavailable():
    fake = FakeTransport([resp(answer(confidence=0.85)), resp(answer(confidence=0.82), model=OTHER_MODEL),
                          resp(answer(confidence=0.84))])
    v = sync_job(fake).review_now(make_item())
    assert OTHER_MODEL in v.response_models
    assert (v.state, v.cause) == ("unavailable", "model_mismatch"), \
        f"{v.state} with votes {v.votes} although one copy came from {OTHER_MODEL}"


def test_r2_12_a_refusal_on_an_extra_copy_is_still_not_a_vote():
    fake = FakeTransport([resp(answer(confidence=0.85)), resp(answer(), stop="refusal"), resp(answer(confidence=0.83))])
    v = sync_job(fake).review_now(make_item())
    assert v.state == "answered" and v.votes == ("matches_manifest", "matches_manifest")


# == R2-13: the Batch path maps a failed copy 0 like the synchronous path ===================================

@pytest.mark.parametrize("bad", [
    pytest.param(dict(model=OTHER_MODEL), id="copy0_model_mismatch"),
    pytest.param(dict(stop="refusal"), id="copy0_refusal"),
])
def test_r2_13_batch_path_maps_a_failed_first_copy_like_the_synchronous_path(bad):
    sync = sync_job(FakeTransport([resp(answer(confidence=0.95), **bad)])).review_now(make_item())
    assert sync.state == "unavailable"
    v = _batch_verdict(lambda c: resp(answer(confidence=0.95), **(bad if c == 0 else {})))
    assert v.state == "unavailable", f"batch path {v.state} with responses from {v.response_models}"


def test_r2_13_batch_path_votes_like_the_synchronous_path_on_a_borderline_copy_0():
    v = _batch_verdict(lambda c: resp(answer(confidence=0.95)) if c else resp(answer(confidence=0.85)))
    assert v.state == "answered" and len(v.votes) == 3
    v = _batch_verdict(lambda c: resp(answer(confidence=0.85), model=OTHER_MODEL if c == 2 else MODEL))
    assert (v.state, v.cause) == ("unavailable", "model_mismatch")
    v = _batch_verdict(lambda c: resp(answer(confidence=0.95), model=OTHER_MODEL if c == 2 else MODEL))
    assert v.state == "answered"                          # copy 0 is not borderline: the sync path sends no copy 2


# == R2-14: the live transport's endpoint ===================================================================

def test_r2_14_live_transport_never_takes_its_endpoint_from_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://collector.invalid")
    t = AnthropicTransport.from_env(allow_network=True, env={"ANTHROPIC_API_KEY": "sk-test"})
    assert "collector.invalid" not in str(t._client.base_url), \
        f"the client targets {t._client.base_url} from the process environment"
    assert str(t._client.base_url).rstrip("/") == DEFAULT_BASE_URL


def test_r2_14_an_explicit_config_endpoint_still_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://collector.invalid")
    t = AnthropicTransport.from_env(allow_network=True, env={"ANTHROPIC_API_KEY": "sk-test"},
                                    base_url="https://reviewer.example.internal")
    assert str(t._client.base_url).startswith("https://reviewer.example.internal")


# == R2-15: an integer confidence too large for a float =====================================================

def test_r2_15_an_integer_confidence_too_large_for_a_float_is_a_schema_violation_not_a_crash():
    text = '{"confidence": 1' + "0" * 400 + ', ' + json.dumps(answer())[1:].replace('"confidence": 0.95, ', "")
    fake = FakeTransport([resp(text=text), resp(text=text)])
    v = sync_job(fake).review_now(make_item())
    assert (v.state, v.cause) == ("unavailable", "schema_violation")
    assert "confidence outside [0, 1]" in v.violations[0]


def test_r2_15_integer_0_and_1_are_in_range():
    for c in (0, 1):
        v = sync_job(FakeTransport([resp(dict(answer(), confidence=c))])).review_now(make_item())
        assert v.state == "answered"


# == oracle helpers =========================================================================================

class _OneRead:
    """An OCR reader that returns a fixed Reading."""

    name = "ocr"

    def __init__(self, reading: Reading):
        self.reading = reading

    def read(self, field: str) -> Reading | None:
        return self.reading if field == self.reading.field else None


# == R2-16: a single-pass OCR read of a typed field =========================================================

def test_r2_16_ocr_money_read_without_the_2x_second_pass_is_unreadable():
    reading = Reading("rgn.confirm.bp-effect", "ocr", "-$15,025.00", 90.0, second_pass=None)
    ext = extract("rgn.confirm.bp-effect", "money", {"ocr": _OneRead(reading)}, "ocr")
    assert not ext.readable, "an OCR money read without its second pass was accepted"


def test_r2_16_the_second_pass_is_required_of_ocr_typed_fields_only():
    assert "2x pass missing" in problem(Reading("q", "ocr", "100", 99.0), "int")
    assert problem(Reading("t", "ocr", "BUY", 99.0), "text") is None                 # no digit count compared
    assert problem(Reading("q", "jab", "100", 100.0), "int") is None                 # not an OCR guess


# == R2-17: frozen OnDemand API cross-checks ================================================================

ONDEMAND = datetime(2026, 11, 6, 20, 30, tzinfo=timezone.utc)
CAPTURE = datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc)


def test_r2_17_frozen_ondemand_api_cross_check_uses_the_pinned_ondemand_timestamp():
    api = GroundTruthApi({("AAPL", "last"): [(ONDEMAND - timedelta(minutes=1), "228.40"),
                                            (CAPTURE - timedelta(hours=1), "231.10")]})
    fixture = {"data_mode": {"frozen": {"provider": "ondemand"}},
               "epoch": {"ondemand_timestamp": ONDEMAND.isoformat(), "data_delay": "delayed_20"}}
    step = {"id": "cp1", "data_mode": "frozen",
            "expect": [{"id": "last_px", "kind": "api_within", "region": "rgn.quote.last", "api_field": "last",
                        "format": "price"}]}
    ocr = _OneRead(Reading("rgn.quote.last", "ocr", "228.40", 96.0, second_pass="228.40"))
    (res,) = evaluate_assertions(step, {"ocr": ocr, "api": api}, {}, fixture,
                                 context=EvalContext(data_mode="frozen", capture_time=CAPTURE, symbol="AAPL"))
    assert res.status == "passed", f"{res.status}: {res.detail}"


def test_r2_17_a_frozen_cross_check_with_no_pinned_moment_is_a_config_error():
    ctx = EvalContext(data_mode="frozen", capture_time=CAPTURE, symbol="AAPL")
    with pytest.raises(OracleConfigError, match="ondemand_timestamp"):
        api_comparison(ctx, {"data_mode": {"frozen": {"provider": "ondemand"}}})
    pinned = {"data_mode": {"frozen": {"timestamp": "2026-11-09T15:30:00-05:00"}},
              "epoch": {"ondemand_timestamp": ONDEMAND.isoformat()}}
    assert api_comparison(ctx, pinned).as_of.isoformat() == "2026-11-09T15:30:00-05:00"    # the frozen one first


# == R2-18: ASCII digits only ===============================================================================

def test_r2_18_r10_rejects_numbers_drawn_with_non_ascii_digits():
    for text in ("$１５,０２５.００", "$١٥,٠٢٥.٠٠"):
        assert format_error("money", text) is not None, f"{text!r} parsed as {parse('money', text)}"


def test_r2_18_quantity_price_dates_and_the_digit_count_are_ascii_only():
    assert format_error("int", "１００") is not None and format_error("price", "@٢٢٨.٤٠ LMT") is not None
    assert format_error("date", "١١/٠٦/٢٦") is not None
    assert format_error("money", "$15,025.00") is None and format_error("int", "100") is None
    assert digit_count("$１５,025.00") == 5


# == integrator follow-ups (coordinator, round 2) ===========================================================

def _read_back_assertion(aid: str) -> dict:
    flow = Path(__file__).resolve().parents[1] / "config/example/flows/tier-1/flow.trade.active-trader.read-back.yaml"
    doc = yaml.safe_load(flow.read_text())
    return next(a for s in doc["steps"] if s.get("id") == "cp-positions" for a in s.get("expect", ()) if a["id"] == aid)


def _one(assertion: dict, values: dict) -> Any:
    (res,) = evaluate_assertions({"expect": [assertion]}, {"ocr": GroundTruthReader(values)}, {}, {},
                                 context=EvalContext(symbol="AAPL"))
    return res


def test_followup_present_and_absent_with_a_format_judge_every_row_of_a_column():
    """The example's `ps_qty` (present, format int) over the Position Statement's Qty column: every row is an
    int value, so it passes; one row that is not makes it fail and the detail names that row."""
    ps_qty = _read_back_assertion("ps_qty")
    assert (ps_qty["kind"], ps_qty.get("format")) == ("present", "int")
    assert _one(ps_qty, {"rgn.ps.qty": "100\n1\n7"}).status == "passed"
    bad = _one(ps_qty, {"rgn.ps.qty": "100\nabc\n7"})
    assert bad.status == "failed" and "'abc'" in bad.detail
    assert _one(ps_qty, {"rgn.ps.qty": "100"}).status == "passed"
    assert _one(ps_qty, {"rgn.ps.qty": ""}).status == "failed"                 # an empty region holds no value
    absent = dict(ps_qty, id="no_qty", kind="absent")
    assert _one(absent, {"rgn.ps.qty": "--\nN/A"}).status == "passed"
    assert _one(absent, {"rgn.ps.qty": "--\n5"}).status == "failed"
    assert _one(absent, {"rgn.ps.qty": ""}).status == "passed"


def test_followup_a_rule_named_after_its_rule_id_has_no_doubled_prefix():
    inputs = {"qty": "rgn.confirm.qty", "price": "rgn.confirm.price", "multiplier": 1, "total": "rgn.confirm.cost"}
    values = {"rgn.confirm.qty": "100", "rgn.confirm.price": "150.00", "rgn.confirm.cost": "16,000.00"}
    step = {"expect": [{"id": "R01", "kind": "rule", "rule_id": "R01", "tol": 0.01, "inputs": inputs},
                       {"id": "cost_math", "kind": "rule", "rule_id": "R01", "tol": 0.01, "inputs": inputs}]}
    results = evaluate_assertions(step, {"ocr": GroundTruthReader(values)}, {}, {}, context=EvalContext(symbol="AAPL"))
    assert [r.status for r in results] == ["failed", "failed"]
    detail = summarize(results).detail
    assert "R01: R01:" not in detail and detail.startswith("R01: ") and "; cost_math: R01: " in detail


def test_followup_the_simulated_rig_reports_the_build_it_launched():
    rig = SimulatedRig(SimScenario(launches=[LaunchObservation(True, "2026.45.0")]), clock=ManualClock())
    assert rig.read_title_build_id() is None                                   # nothing launched yet
    rig.launch()
    assert rig.read_title_build_id() == "2026.45.0"
    assert rig.enumerate_windows()[0].title == "Main@thinkorswim [build 2026.45.0]"
    rig.kill_process_tree()
    assert rig.read_title_build_id() is None
    rig.launch()                                                                # the scenario's clean relaunch
    assert rig.read_title_build_id() == BUILD


def test_followup_the_proof_line_is_the_6_6_text_form():
    row = OrderRow("x1", "working", "WORKING", "AAPL", 1.0)
    clean = OrderBook(net_liq=2500.0)
    assert post_run_proof(ProofInputs(start=clean, end=clean)).line == "PROVEN: no orders sent"
    assert post_run_proof(ProofInputs(None, None, session="never", tokens_issued=0)).line == PROVEN_NO_SESSION
    assert post_run_proof(ProofInputs(start=None, end=clean)).line == "UNPROVEN: snapshot_missing"
    sent = post_run_proof(ProofInputs(start=clean, end=OrderBook(working=(row,), net_liq=2500.0), cancel_all="done"))
    assert sent.line == "ORDERS SENT: undeclared rows (x1); Cancel All verified"          # the fifth verdict
    failed = post_run_proof(ProofInputs(start=clean, end=OrderBook(working=(row,), net_liq=2500.0),
                                        cancel_all="failed"))
    assert failed.line == "ORDERS SENT: undeclared rows (x1); Cancel All failed"
    for p in (sent, failed):
        assert p.line.startswith(PROOF_FORMS) and proof_severity(p.line) == ORDERS_SENT_PROOF   # the Reporter's forms
