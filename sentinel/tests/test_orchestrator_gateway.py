"""Never-send guard: slow half, fast half, flag watcher, drop accounting and thresholds (6.2, 5.4.2 to 5.4.8)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.gateway import (DISMISS_ALLOWLIST, HOOK_STATES, PAPER_ASSERTION_CURRENT_S, VK_ESCAPE, VK_F1,
                                           VK_RETURN, VK_SPACE, VK_TAB, Action, Chord, DenyRect, DropLedger, Flag,
                                           FlagWatcher, GatewayDrop, GatewayError, GatewayState, GATEWAY_REASONS,
                                           InputEvent, Rect, SafetyContext, SlowDecision, agent_flag_abort, drain,
                                           hook_decide, recovery_admits, refusal, slow_half)
from sentinel.orchestrator.identity import TIMER_S
from sentinel.orchestrator.ports import SessionSafety
from sentinel.orchestrator.simrig import CONFIRM_POINT, DELETE_POINT, SEND_RECT, SimulatedRig

T = 100.0
SEND = DenyRect(Rect(900, 600, 80, 28), "Send")


def armed(flag: Flag = Flag.CLEAR) -> GatewayState:
    g = GatewayState(armed=True)
    g.stamp_watcher(flag, T)
    g.hook_alive_mono_ts = T
    return g


def ev(kind: str, x: int = 0, y: int = 0, **kw) -> InputEvent:
    if kind.startswith("button"):
        kw.setdefault("button", "left")
    return InputEvent(kind, x, y, **kw)


# -- fast half ------------------------------------------------------------------------------------------
def test_rule1_deny_all_and_not_armed_drop_everything():
    g = armed()
    g.issue_click(10, 10, T)
    g.deny_all = True
    assert hook_decide(g, ev("button_down", 10, 10), T).reason == "deny_all"
    g.deny_all, g.armed = False, False
    assert hook_decide(g, ev("button_down", 10, 10), T).reason == "not_armed"


def test_rule4_console_input_is_foreign_and_untokened_injection_is_logged():
    g = armed()
    assert hook_decide(g, ev("key_down", vk=0x41, origin="console"), T).reason == "foreign_input"
    e = ev("button_down", 5, 5, origin="foreign_process")
    d = hook_decide(g, e, T)
    assert d.reason == "no_token"
    drop = drain(d, e)
    assert (drop.reason, drop.gate_class) == ("untokened_injected", "safety")


def test_rule4_another_processs_injected_moves_are_dropped_and_logged_like_its_clicks():
    """6.2 rule 4, 5.4.1 layer 2: a move injected by any process other than the emitter is dropped `no_token` (logged
    `untokened_injected`, or tagged `drill` inside the drill's raw phase), with no token pending and inside a drag's
    `move_allow` box alike; the emitter's own moves still pass. Rule 2 keeps its codes for another origin's keys."""
    g = armed()
    for origin in ("foreign_process", "drill"):
        e = ev("move", 300, 300, origin=origin)
        d = hook_decide(g, e, T)
        assert (d.allowed, d.reason) == (False, "no_token")
        assert drain(d, e).reason == ("untokened_injected" if origin == "foreign_process" else "no_token")
    assert drain(hook_decide(g, ev("move", 3, 3, origin="drill"), T), ev("move", 3, 3, origin="drill"),
                 drill_phase=True).tag == "drill"
    assert hook_decide(g, ev("move", 300, 300), T).allowed
    g.issue_click(100, 100, T, hold_ms=300)
    g.open_move_allow(Rect(90, 90, 200, 50), T, 300)
    assert hook_decide(g, ev("button_down", 100, 100), T).allowed
    assert hook_decide(g, ev("move", 150, 110, origin="foreign_process"), T).reason == "no_token"   # steers no drag
    assert hook_decide(g, ev("move", 150, 110), T).allowed
    g = armed(Flag.SET)
    assert hook_decide(g, ev("key_down", vk=VK_RETURN, origin="foreign_process"), T).reason == "blocked_key_dialog"


def test_click_token_is_one_shot_with_2px_tolerance():
    g = armed()
    t = g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 102, 98), T).allowed
    assert g.tokens[t.id].state == "half_consumed"
    up = hook_decide(g, ev("button_up", 102, 98), T)
    assert up.allowed and up.decision == "emitted"
    assert hook_decide(g, ev("button_down", 100, 100), T).reason == "no_token"     # consumed
    g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 103, 100), T).reason == "token_mismatch_coords"


def test_expired_down_is_dropped_but_a_late_up_is_accepted():
    g = armed()
    g.issue_click(50, 50, T)
    assert hook_decide(g, ev("button_down", 50, 50), T + 0.6).reason == "token_expired"
    g.issue_click(60, 60, T)
    assert hook_decide(g, ev("button_down", 60, 60), T + 0.4).allowed
    up = hook_decide(g, ev("button_up", 60, 60), T + 0.9)
    assert up.allowed and up.late_up and up.decision == "late_up"


def test_drain_classes_integrity_and_own_token():
    g = armed()
    g.issue_click(50, 50, T)
    e = ev("button_down", 50, 50)
    d = hook_decide(g, e, T + 1)
    drop = drain(d, e)
    assert (drop.reason, drop.gate_class, drop.own_token) == ("token_expired", "integrity", True)
    e2 = ev("button_down", 1, 1)
    assert drain(hook_decide(g, e2, T), e2).gate_class == "integrity"           # no_token the slow half tokened
    with pytest.raises(ValueError):
        drain(hook_decide(g, ev("move", 1, 1), T), e2)


def test_stale_watcher_stamp_is_unknown_and_handled_as_set():
    g = armed(Flag.CLEAR)
    g.issue_key(VK_RETURN, T + 0.6)
    assert g.effective_flag(T + 0.6) == Flag.UNKNOWN
    assert hook_decide(g, ev("key_down", vk=VK_RETURN), T + 0.6).reason == "blocked_key_dialog"


@pytest.mark.parametrize("vk,mods,reason", [
    (VK_RETURN, frozenset(), "blocked_key_dialog"),
    (VK_SPACE, frozenset(), "blocked_key_dialog"),
    (VK_F1 + 4, frozenset(), "blocked_key_dialog"),
    (VK_RETURN, frozenset({"ctrl"}), "blocked_chord_dialog"),
    (ord("S"), frozenset({"alt"}), "blocked_chord_dialog"),
    (0x11, frozenset(), "blocked_chord_dialog"),                 # Ctrl itself starts a chord
])
def test_rule2_blocked_keys_under_the_flag_regardless_of_token(vk, mods, reason):
    g = armed(Flag.SET)
    g.issue_key(vk, T)
    assert hook_decide(g, ev("key_down", vk=vk, mods=mods), T).reason == reason


def test_rule2_loaded_order_chords_and_passing_keys():
    g = armed(Flag.SET)
    g.load_chords([Chord.parse("Shift+B"), Chord.parse("B")])
    g.issue_key(ord("B"), T)
    assert hook_decide(g, ev("key_down", vk=ord("B"), mods=frozenset({"shift"})), T).reason == "blocked_chord_dialog"
    assert hook_decide(g, ev("key_down", vk=ord("B")), T).reason == "blocked_chord_dialog"
    for vk in (VK_TAB, VK_ESCAPE, ord("7")):
        g.issue_key(vk, T)
        assert hook_decide(g, ev("key_down", vk=vk), T).allowed
    g.issue_key(VK_TAB, T)
    assert hook_decide(g, ev("key_down", vk=VK_TAB, mods=frozenset({"shift"})), T).allowed   # Shift+Tab passes


def test_rule2_shift_click_and_dialog_ok_and_deny_rect():
    g = armed(Flag.SET)
    g.issue_click(10, 10, T, dialog_ok=True)
    assert hook_decide(g, ev("button_down", 10, 10, mods=frozenset({"shift"})), T).reason == "shift_click_dialog"
    g.issue_click(20, 20, T)
    assert hook_decide(g, ev("button_down", 20, 20), T).reason == "dialog_not_ok"
    g.write_deny_rects([SEND])
    g.issue_click(895, 595, T, dialog_ok=True)                     # inside the 12 px inflation
    assert hook_decide(g, ev("button_down", 895, 595), T).reason == "deny_rect"
    g.issue_click(30, 30, T, dialog_ok=True)
    assert hook_decide(g, ev("button_down", 30, 30), T).allowed


def test_send_ok_token_passes_the_send_rectangle_once():
    g = armed(Flag.SET)
    g.write_deny_rects([SEND])
    g.issue_click(920, 610, T, dialog_ok=True, send_ok=True)
    assert hook_decide(g, ev("button_down", 920, 610), T).allowed
    assert hook_decide(g, ev("button_down", 920, 610), T).reason == "no_token"      # a second click (6.7)
    g.issue_click(920, 610, T, dialog_ok=True)                                     # not a send token
    assert hook_decide(g, ev("button_down", 920, 610), T).reason == "deny_rect"
    g.issue_click(920, 610, T, dialog_ok=True, send_ok=True)                       # the next declared send
    assert hook_decide(g, ev("button_down", 920, 610), T).allowed


def test_moves_drag_window_and_release_tokens():
    g = armed()
    assert hook_decide(g, ev("move", 5, 5), T).allowed                         # armed, no token pending
    g.issue_click(10, 10, T, hold_ms=500)
    g.open_move_allow(Rect(10, 10, 100, 20), T, 500)
    assert hook_decide(g, ev("button_down", 10, 10), T).allowed
    assert hook_decide(g, ev("move", 60, 15), T + 0.2).allowed
    assert hook_decide(g, ev("move", 60, 200), T + 0.2).reason == "move_outside_allow"
    assert hook_decide(g, ev("move", 60, 15), T + 2.0).reason == "move_outside_allow"   # past hold_ms + 1000
    assert hook_decide(g, ev("button_up", 109, 25), T + 2.0).allowed            # the up is still allowed
    g.issue_release(T)
    assert hook_decide(g, ev("key_up", vk=0x10), T).allowed                     # Shift released anywhere
    assert hook_decide(g, ev("button_up", 1500, 3, button="right"), T).allowed


def test_half_consumed_token_with_no_up_triggers_reset_after_2s():
    g = armed()
    g.issue_click(10, 10, T)
    hook_decide(g, ev("button_down", 10, 10), T)
    assert g.overdue_half_consumed(T + 1.0) == []
    assert len(g.overdue_half_consumed(T + 2.6)) == 1


# -- token lifecycle on drops and expiry (choice 17) --------------------------------------------------------
def test_a_click_dropped_under_deny_all_wedges_no_later_move():
    """The wedge: a click dropped `deny_all` left its token armed forever, so every later move was dropped
    `move_outside_allow` until `reset_input_state`. The drop now spends the token (5.4.3 stage 7 reads it)."""
    g = armed()
    t = g.issue_click(100, 100, T)
    g.deny_all = True
    d = hook_decide(g, ev("button_down", 100, 100), T)
    assert (d.allowed, d.reason, d.token_id) == (False, "deny_all", t.id)
    assert (t.state, t.drop_reason) == ("dropped", "deny_all")
    assert hook_decide(g, ev("button_up", 100, 100), T).reason == "deny_all"
    g.deny_all = False
    assert hook_decide(g, ev("move", 5, 5), T + 0.1).allowed
    assert hook_decide(g, ev("move", 5, 5), T + 0.6).allowed                     # after expiry as well
    assert hook_decide(g, ev("button_down", 100, 100), T + 0.1).reason == "no_token"   # spent: never usable again


@pytest.mark.parametrize("case", ["deny_all", "not_armed", "blocked_key_dialog", "blocked_chord_dialog",
                                  "shift_click_dialog", "wheel_deny_all"])
def test_rule1_and_rule2_drops_spend_the_token_the_event_would_have_used(case):
    g = armed(Flag.CLEAR if case in ("deny_all", "not_armed", "wheel_deny_all") else Flag.SET)
    other = g.issue_key(ord("Q"), T)                       # a pending token the event does not match
    if case in ("deny_all", "wheel_deny_all"):
        g.deny_all = True
    elif case == "not_armed":
        g.armed = False
    if case == "wheel_deny_all":
        t, e = g.issue_wheel(50, 50, 1, T), ev("wheel", 51, 50, delta=120)
    elif case in ("deny_all", "not_armed", "blocked_key_dialog"):
        t, e = g.issue_key(VK_RETURN, T), ev("key_down", vk=VK_RETURN)
    elif case == "blocked_chord_dialog":
        t, e = g.issue_key(ord("S"), T), ev("key_down", vk=ord("S"), mods=frozenset({"alt"}))
    else:
        t, e = g.issue_click(50, 50, T, dialog_ok=True), ev("button_down", 51, 49, mods=frozenset({"shift"}))
    reason = "deny_all" if case == "wheel_deny_all" else case
    d = hook_decide(g, e, T)
    assert (d.allowed, d.reason, d.token_id) == (False, reason, t.id)
    assert (t.state, t.drop_reason) == ("dropped", reason)
    assert (other.state, other.drop_reason) == ("armed", None)


def test_a_drop_of_another_origins_event_or_of_an_up_spends_no_token():
    g = armed()
    t = g.issue_click(100, 100, T)
    g.deny_all = True
    assert hook_decide(g, ev("button_down", 100, 100, origin="drill"), T).token_id is None
    assert hook_decide(g, ev("button_down", 100, 100, origin="foreign_process"), T).token_id is None
    assert t.state == "armed"
    g.deny_all = False
    assert hook_decide(g, ev("button_down", 100, 100), T).allowed
    g.deny_all = True
    assert hook_decide(g, ev("button_up", 100, 100), T).token_id is None
    assert t.state == "half_consumed"                      # still owed its up; reset_input_state covers it (5.4.5)


def test_an_unused_armed_token_blocks_moves_only_until_it_expires():
    g = armed()
    click = g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 110, 100), T).reason == "token_mismatch_coords"
    key = g.issue_key(ord("A"), T)
    assert hook_decide(g, ev("key_down", vk=ord("B")), T).reason == "no_token"
    assert hook_decide(g, ev("move", 5, 5), T + 0.2).reason == "move_outside_allow"   # still pending
    assert hook_decide(g, ev("move", 5, 5), T + 0.5).reason == "move_outside_allow"   # expires is inclusive
    assert hook_decide(g, ev("move", 5, 5), T + 0.6).allowed                          # dead: blocks nothing
    assert (click.state, key.state) == ("armed", "armed")                             # the hook wrote nothing
    assert hook_decide(g, ev("button_down", 100, 100), T + 0.6).reason == "token_expired"   # matching unchanged


def test_an_unexpired_match_wins_over_a_dead_token_left_armed():
    """Choice 18: a dead token left armed after `token_mismatch_coords` or `no_token` no longer drops a fresh token's
    down `token_expired`; when every match has expired, the first still drops it `token_expired`."""
    g = armed()
    dead_click = g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 110, 100), T).reason == "token_mismatch_coords"
    dead_key = g.issue_key(ord("A"), T)
    assert hook_decide(g, ev("key_down", vk=ord("B")), T).reason == "no_token"
    g.stamp_watcher(Flag.CLEAR, T + 0.6)                                         # a fresh poll: the flag is clear
    fresh_click = g.issue_click(101, 99, T + 0.6)
    fresh_key = g.issue_key(ord("A"), T + 0.6)
    d = hook_decide(g, ev("button_down", 100, 100), T + 0.7)
    assert (d.allowed, d.token_id) == (True, fresh_click.id)
    d = hook_decide(g, ev("key_down", vk=ord("A")), T + 0.7)
    assert (d.allowed, d.token_id) == (True, fresh_key.id)
    assert (dead_click.state, dead_key.state) == ("armed", "armed")
    d = hook_decide(g, ev("button_down", 100, 100), T + 0.7)                     # only the dead one is armed now
    assert (d.reason, d.token_id) == ("token_expired", dead_click.id)
    d = hook_decide(g, ev("key_down", vk=ord("A")), T + 0.7)
    assert (d.reason, d.token_id) == ("token_expired", dead_key.id)


def test_of_two_unexpired_matches_the_first_issued_wins_and_a_drop_spends_the_preferred_token():
    g = armed()
    first, second = g.issue_click(100, 100, T), g.issue_click(100, 100, T + 0.1)
    assert hook_decide(g, ev("button_down", 100, 100), T + 0.2).token_id == first.id
    g = armed(Flag.SET)
    old, fresh = g.issue_click(100, 100, T - 0.6, dialog_ok=True), g.issue_click(100, 100, T, dialog_ok=True)
    d = hook_decide(g, ev("button_down", 100, 100, mods=frozenset({"shift"})), T)
    assert (d.reason, d.token_id) == ("shift_click_dialog", fresh.id)
    assert (old.state, fresh.state) == ("armed", "dropped")


def test_a_half_consumed_token_keeps_blocking_moves_past_expiry_until_its_up():
    g = armed()
    g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 100, 100), T).allowed
    assert hook_decide(g, ev("move", 5, 5), T + 0.9).reason == "move_outside_allow"   # 5.4.5: the up is owed
    assert hook_decide(g, ev("button_up", 100, 100), T + 1.0).late_up
    assert hook_decide(g, ev("move", 5, 5), T + 1.0).allowed


def test_slots_of_expired_armed_tokens_are_reused_and_half_consumed_ones_kept():
    g = armed()
    held = g.issue_click(100, 100, T)
    assert hook_decide(g, ev("button_down", 100, 100), T).allowed
    old = [g.issue_key(ord("A"), T) for _ in range(15)]
    with pytest.raises(GatewayError):
        g.issue_key(ord("A"), T + 0.4)                     # every slot still live
    fresh = [g.issue_key(ord("B"), T + 0.6) for _ in range(15)]
    assert set(g.tokens) == {held.id} | {t.id for t in fresh} and not set(g.tokens) & {t.id for t in old}
    with pytest.raises(GatewayError):
        g.issue_key(ord("B"), T + 0.6)                     # the half consumed token keeps its slot


def test_wheel_tokens_match_point_and_sign():
    g = armed()
    g.issue_wheel(300, 300, -1, T)
    assert hook_decide(g, ev("wheel", 300, 300, delta=120), T).reason == "no_token"
    assert hook_decide(g, ev("wheel", 310, 300, delta=-120), T).reason == "token_mismatch_coords"
    assert hook_decide(g, ev("wheel", 301, 300, delta=-120), T).allowed


def test_segment_limits_fail_closed():
    g = armed()
    for i in range(16):
        g.issue_key(0x41, T)
    with pytest.raises(GatewayError):
        g.issue_key(0x41, T)
    with pytest.raises(GatewayError):
        g.write_deny_rects([(Rect(i, 0, 1, 1), "Send") for i in range(9)])
    with pytest.raises(GatewayError):
        g.load_chords([Chord(frozenset({"shift"}), 0x41 + i) for i in range(17)])
    with pytest.raises(ValueError):
        Chord.parse("Hyper+B")


def test_hook_liveness_before_emission():
    g = armed()
    assert g.hook_alive(T + 0.5)
    assert not g.hook_alive(T + 1.0)
    g.hook_host_alive = False
    assert not g.hook_alive(T)


def test_flag_watcher_sets_fast_clears_after_two_clear_polls_unknown_on_failure():
    w = FlagWatcher()
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=False) == Flag.UNKNOWN
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=False) == Flag.CLEAR
    assert w.poll(enumeration_hit=False, detector_hit=True, sticky=False) == Flag.SET
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=False) == Flag.SET
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=True) == Flag.SET
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=False) == Flag.SET
    assert w.poll(enumeration_hit=False, detector_hit=False, sticky=False) == Flag.CLEAR
    assert w.poll(enumeration_hit=True, detector_hit=False, sticky=False, failed=True) == Flag.UNKNOWN


# -- slow half ------------------------------------------------------------------------------------------
def click(label: str | None, under: str | None = None, **kw) -> Action:
    return Action("click", point=kw.pop("point", (10, 10)), resolved_name=label,
                  under_point_text=under if under is not None else label, **kw)


@pytest.mark.parametrize("a,reason,send", [
    (click("Send"), "never_tokenize", True),
    (click("Order Entry", "Auto Send"), "never_tokenize", True),
    (click("Blast All"), "never_tokenize", True),
    (click("Auto-hide"), "never_tokenize", True),                  # send-like outside the dialog
    (click("Cancel All"), "deny_profile", False),
    (click("Cancel", target_kind="working_order_action"), "deny_profile", False),
    (click("Confirm and Send"), "deny_profile", False),
    (click("Confirm and Send", profile="agent"), "never_tokenize", True),
    (click("Application Settings", profile="agent", target_kind="application_settings"), "deny_profile", False),
    (click("Buy", target_kind="ladder_order_cell"), "deny_profile", False),
    (Action("press", keys=(VK_RETURN,)), "guarded_key_refused", False),
    (Action("press", profile="agent", keys=(VK_RETURN,)), "deny_profile", False),
    (click("Splitter", ambiguous=True), "deny_profile", False),
])
def test_slow_half_refusals_use_4_13_codes(a, reason, send):
    s = slow_half(a, SafetyContext())
    assert s.refused and s.reason == reason and s.attempted_send is send
    assert refusal(s).reason in GATEWAY_REASONS


def test_slow_half_allows_cancel_all_only_to_the_teardown_action_and_declared_enter():
    assert not slow_half(click("Cancel All", teardown_action="cancel_all"), SafetyContext()).refused
    assert not slow_half(click("Cancel", target_kind="working_order_action", flow_class="paper_send_ok"),
                         SafetyContext()).refused
    assert not slow_half(Action("press", keys=(VK_RETURN,), declared_guarded=True), SafetyContext()).refused
    assert slow_half(click("Splitter"), SafetyContext(hook_alive=False)).reason == "hook_host_dead"


def pinned(**kw) -> SafetyContext:
    """A context with the dismiss allowlist pinned as Phase 0 would pin it (choice 10: the default is empty)."""
    return SafetyContext(dismiss_allowlist=DISMISS_ALLOWLIST, **kw)


def test_confirm_and_send_needs_order_stage_attestation_and_paper_assertion():
    a = click("Confirm and Send", order_stage_step=True)
    assert slow_half(a, pinned(attestation_passed=True)).reason == "deny_profile"
    ok = slow_half(a, pinned(flag=Flag.SET, attestation_passed=True, paper_assertion_current=True))
    assert not ok.refused and ok.dialog_ok                         # the sticky flag is already set; no dialog yet
    # the default context loads no allowlist: nothing could dismiss the dialog, so it is never opened
    assert SafetyContext().dismiss_allowlist == frozenset()
    s = slow_half(a, SafetyContext(flag=Flag.SET, attestation_passed=True, paper_assertion_current=True))
    assert s.reason == "deny_profile" and "empty dismiss allowlist" in s.detail


@pytest.mark.parametrize("other", ["Submit Order", "Flatten Position", "Replace Order", "Blast All Orders",
                                   "Auto Send ON"])
def test_confirm_and_send_exempts_only_its_own_label_from_the_send_like_check(other):
    """6.2 Slow half, 5.4.3 stage 4: the under-point OCR is a second, independent check, so a send-like label beside
    `Confirm and Send` (the resolver's anchor offset landed on a neighbour) is an attempted send, either way round,
    even where Confirm and Send itself would be refused for another reason."""
    ctx = pinned(attestation_passed=True, paper_assertion_current=True)
    for rn, ocr in (("Confirm and Send", other), (other, "Confirm and Send")):
        for c in (ctx, SafetyContext()):
            s = slow_half(click(rn, ocr, order_stage_step=True), c)
            assert (s.refused, s.reason, s.attempted_send) == (True, "never_tokenize", True), (rn, ocr, c)
    assert not slow_half(click("Confirm and Send", order_stage_step=True), ctx).refused


def test_dialog_allowlist_inverts_inside_the_order_dialog():
    ctx = pinned(flag=Flag.SET, order_dialog_present=True, deny_rects=(SEND,))
    ok = slow_half(click("Delete", point=DELETE_POINT), ctx)
    assert not ok.refused and ok.dialog_ok
    both = slow_half(click("Delete", "Del ete"), ctx)                                     # both labels must match
    assert both.reason == "dialog_allowlist" and both.unknown_dialog
    s = slow_half(click("Close"), ctx)
    assert s.reason == "dialog_allowlist" and s.unknown_dialog and not s.attempted_send and s.gate_class == "safety"
    s = slow_half(click("Cancel", point=(905, 605)), ctx)
    assert s.reason == "dialog_allowlist" and s.attempted_send and not s.unknown_dialog
    assert slow_half(click("Delete", "Send"), ctx).reason == "never_tokenize"


def test_flag_recheck_refuses_guarded_keys_chords_shift_click_and_deny_rects():
    ctx = SafetyContext(flag=Flag.SET, deny_rects=(SEND,), chords=(Chord.parse("Shift+B"),))
    for a in (Action("press", keys=(VK_SPACE,), declared_guarded=True), Action("press", keys=(VK_F1,)),
              Action("key_chord", keys=(ord("B"),), mods=frozenset({"shift"})),
              Action("key_chord", keys=(VK_RETURN,), mods=frozenset({"ctrl"}))):
        s = slow_half(a, ctx)
        assert s.reason == "guarded_key_refused" and s.attempted_send
    s = slow_half(click("Buy", mods=frozenset({"shift"})), ctx)
    assert s.reason == "shift_click_dialog" and s.attempted_send
    s = slow_half(click("Price", point=(920, 610)), ctx)
    assert s.reason == "deny_rect" and s.attempted_send
    ok = slow_half(click("Price", point=(10, 10)), ctx)
    assert not ok.refused and ok.dialog_ok
    assert not slow_half(Action("press", keys=(VK_TAB,)), ctx).refused


def test_explorer_deny_list_and_drag_named_rules():
    hit = SafetyContext(deny_list_hit="chat")
    assert slow_half(click("Chat Rooms", profile="explorer"), hit).reason == "deny_profile"
    bubble = Action("drag", profile="explorer", point=(5, 5), resolved_name="bubble", under_point_text="bubble",
                    target_kind="order_bubble", end_kind="splitter")
    assert slow_half(bubble, SafetyContext()).reason == "deny_profile"
    split = Action("drag", profile="explorer", point=(5, 5), resolved_name="divider", under_point_text="divider",
                   target_kind="splitter", end_kind="detach_handle")
    assert not slow_half(split, SafetyContext()).refused


# -- the bypass paths of 6.2, end to end on the simulated rig ----------------------------------------------
def open_dialog(rig: SimulatedRig) -> None:
    """Drill ticket built, `Confirm and Send` clicked as an order-stage step: the Order Confirmation dialog is up."""
    open_ticket(rig)
    r = rig.send_input(Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send",
                              under_point_text="Confirm and Send", order_stage_step=True))
    assert r.refused_reason is None and all(d.allowed for d in r.decisions) and rig.dialog_open


def open_ticket(rig: SimulatedRig) -> None:
    """The docked Order Entry ticket visible: the ticket detector sets the flag, no dialog (5.4.6 source 2)."""
    rig.set_hook_state(armed=True, deny_all=False)
    rig.attested = True
    rig.load_order_chords([Chord.parse("Shift+B")])
    rig.ticket_visible = True


BYPASS = [
    # name, gateway action, raw events, expected refusal, expected raw drop, attempted send (Enter and Space on
    # the open dialog; the rest with the drill ticket visible, which keeps the flag set, as in 6.5)
    ("enter_on_dialog", Action("press", keys=(VK_RETURN,), declared_guarded=True),
     [InputEvent("key_down", vk=VK_RETURN, origin="drill")], "guarded_key_refused", "blocked_key_dialog", True),
    ("space_on_dialog", Action("press", keys=(VK_SPACE,), declared_guarded=True),
     [InputEvent("key_down", vk=VK_SPACE, origin="drill")], "guarded_key_refused", "blocked_key_dialog", True),
    ("auto_send_ladder_click", Action("click", point=(300, 700), resolved_name="Buy", under_point_text="Buy",
                                      target_kind="ladder_order_cell"),
     [InputEvent("button_down", 300, 700, button="left", origin="drill")], "deny_profile", "no_token", False),
    ("shift_click", Action("click", point=(50, 50), resolved_name="Buy", under_point_text="Buy",
                           mods=frozenset({"shift"})),
     [InputEvent("button_down", 50, 50, button="left", mods=frozenset({"shift"}), origin="drill")],
     "shift_click_dialog", "shift_click_dialog", True),
    ("bound_order_hotkey", Action("key_chord", keys=(ord("B"),), mods=frozenset({"shift"})),
     [InputEvent("key_down", vk=ord("B"), mods=frozenset({"shift"}), origin="drill")],
     "guarded_key_refused", "blocked_chord_dialog", True),
    ("order_bubble_drag", Action("drag", point=(400, 300), resolved_name="order", under_point_text="order",
                                 target_kind="order_bubble", end_kind=None),
     [InputEvent("button_down", 400, 300, button="left", origin="drill")], "deny_profile", "no_token", False),
]


@pytest.mark.parametrize("name,action,raw,refused,dropped,send", BYPASS, ids=[b[0] for b in BYPASS])
def test_every_known_send_path_is_refused_dropped_and_counted(name, action, raw, refused, dropped, send):
    rig = SimulatedRig()
    on_dialog = name.endswith("on_dialog")
    open_dialog(rig) if on_dialog else open_ticket(rig)
    r = rig.send_input(action)
    assert r.refused_reason == refused
    hook = rig.send_input(action, events=raw)
    assert [d.reason for d in hook.decisions] == [dropped]
    assert rig.sent_orders == 0 and rig.dialog_open == on_dialog
    ledger = DropLedger()
    assert r.refused.attempted_send is send
    res = ledger.record(refusal(r.refused), unit="flow.x", order_stage=True)
    assert ledger.run_count == 1
    if send:
        assert res.run_status == "aborted_safety" and res.run_reason == "attempted_send"
        assert res.unreached.outcome == "env_error" and res.unreached.gate == "running"
        assert res.has("kill_switch") and res.has("quarantine_flow") and res.alert.is_safety
    else:
        assert res.run is None and res.step == "blocked_by_gateway" and res.has("end_flow")
    raw_drop = drain(hook.decisions[0], raw[0], drill_phase=True)
    assert raw_drop.tag == "drill" and ledger.record(raw_drop, unit="flow.x").passed      # drill drops exempt
    assert ledger.run_count == 1


def test_chart_right_click_buy_is_denied_to_agent_and_explorer_and_counted():
    rig = SimulatedRig()
    rig.set_hook_state(armed=True, deny_all=False)
    ledger = DropLedger()
    for i, profile in enumerate(("explorer", "agent")):
        a = Action("rclick", profile=profile, point=(700, 400), resolved_name="Buy", under_point_text="Buy")
        r = rig.send_input(a)
        assert r.refused_reason == "deny_profile"
        res = ledger.record(GatewayDrop("deny_profile", "refused", "integrity", profile), unit=f"s{i}")
        # 5.4.8 rule 3: the Explorer's tool call is blocked and its session goes on; an agent rescue ends, step failed
        expected = ("blocked_by_gateway", ()) if profile == "explorer" else ("failed", ("end_session",))
        assert (res.step, res.actions) == expected and res.run is None
    assert ledger.run_count == 2 and rig.sent_orders == 0


def test_a_non_send_refusal_ends_a_scripted_flow_blocked_and_an_agent_rescue_failed():
    """5.4.8 rule 3 by mode (SD-53): scripted step `blocked_by_gateway` with the rest of the flow `not_run`; agent
    rescue `failed`, the rescue ends, and the rest of the flow is the Executor's (5.5)."""
    scripted = DropLedger().record(GatewayDrop("deny_profile", "refused", "integrity"), unit="flow.a", step_id="s04")
    assert scripted.step == "blocked_by_gateway" and scripted.actions == ("end_flow",)
    assert [(o.selector, o.outcome.outcome, o.outcome.reason) for o in scripted.outcomes] == [
        ("rest_of_flow", "not_run", "step_failed:s04")]
    agent = DropLedger().record(GatewayDrop("deny_profile", "refused", "integrity", "agent"), unit="flow.a",
                                step_id="s04")
    assert (agent.step, agent.step_reason, agent.actions, agent.outcomes) == ("failed", "deny_profile",
                                                                              ("end_session",), ())


def test_an_unguarded_enter_would_send_on_the_simulated_app():
    """The simulated app sends when an Enter reaches it with the dialog open: the guard is what stops it."""
    rig = SimulatedRig()
    open_dialog(rig)
    rig.gw.flag = Flag.CLEAR                    # a broken watcher, for the demonstration only
    rig.gw.watcher_mono_ts = rig.clock.monotonic()
    rig.gw.issue_key(VK_RETURN, rig.clock.monotonic())
    rig._app(None, InputEvent("key_down", vk=VK_RETURN))
    assert rig.sent_orders == 1 and rig.book.working


def test_dismissing_the_dialog_through_the_allowlist_closes_it():
    rig = SimulatedRig()
    open_dialog(rig)
    r = rig.send_input(Action("click", point=DELETE_POINT, resolved_name="Delete", under_point_text="Delete"))
    assert r.refused_reason is None and all(d.allowed for d in r.decisions) and not rig.dialog_open
    assert SEND_RECT.contains(920, 610)


# -- drop accounting and thresholds ---------------------------------------------------------------------
def test_agent_rescue_attempted_send_aborts_the_run_like_a_scripted_flow():
    res = DropLedger().record(GatewayDrop("deny_rect", "dropped", "safety", "agent"), unit="flow.y")
    assert res.run_reason == "attempted_send" and res.incident == "S2"
    assert "flow_owner" in res.alert.paged and "accountable_owner" in res.alert.paged


def test_explorer_attempted_send_ends_the_session_and_counts_toward_ten():
    ledger = DropLedger()
    res = ledger.record(GatewayDrop("blocked_key_dialog", "dropped", "safety", "explorer"), unit="explorer-1")
    assert res.run is None and res.step == "failed" and res.has("end_session")
    assert res.incident == "S3" and res.alert == alerts.explorer_ticket() and ledger.run_count == 1


def test_three_per_flow_quarantine_and_ten_per_run_trip_the_kill_switch():
    ledger = DropLedger()
    nonsend = GatewayDrop("deny_profile", "refused", "integrity")
    for _ in range(2):
        assert not ledger.record(nonsend, unit="flow.a").has("quarantine_flow")
    third = ledger.record(nonsend, unit="flow.a")
    assert third.step == "failed" and third.has("quarantine_flow") and third.has("end_flow")
    for i in range(6):
        assert ledger.record(nonsend, unit=f"flow.b{i}").run is None
    tenth = ledger.record(nonsend, unit="flow.c")
    assert ledger.run_count == 10
    assert tenth.run_status == "aborted_safety" and tenth.run_reason == "kill_switch" and tenth.has("kill_switch")
    assert tenth.unreached.reason == "kill_switch"


def test_explorer_three_denied_attempts_end_the_session_without_quarantine():
    ledger = DropLedger()
    d = GatewayDrop("deny_profile", "refused", "integrity", "explorer")
    assert not ledger.record(d, unit="ex").has("end_session")
    assert not ledger.record(d, unit="ex").has("end_session")
    third = ledger.record(d, unit="ex")
    assert third.has("end_session") and not third.has("quarantine_flow")


def test_integrity_drop_of_own_token_retries_once_then_fails_closed():
    ledger = DropLedger()
    d = GatewayDrop("token_expired", "dropped", "integrity", own_token=True)
    assert ledger.record(d, unit="f").has("retry_step")
    again = ledger.record(d, unit="f", retried=True)
    assert again.step == "blocked_by_gateway" and again.checkpoint.reason == "hook_lost"
    assert again.has("kill_tree") and again.has("restart_hook_host")
    silent = DropLedger().record(d, unit="g", probe_answered=False)
    assert silent.checkpoint.outcome == "harness_error"


def test_untokened_and_foreign_input_raise_gateway_integrity_and_the_run_continues():
    ledger = DropLedger()
    res = ledger.record(GatewayDrop("untokened_injected", "dropped", "safety", "system"), unit="run")
    assert res.passed and res.run is None and res.findings[0].kind == "gateway_integrity"
    assert ledger.run_count == 1


def test_uncounted_and_exempt_drops():
    ledger = DropLedger()
    assert ledger.record(GatewayDrop("deny_all", "dropped", "safety"), unit="f").step == "blocked_by_gateway"
    assert ledger.record(GatewayDrop("deny_rect", "dropped", "safety"), unit="drill", exempt=True).passed
    assert ledger.run_count == 0


def test_unknown_dialog_label_depends_on_order_stage():
    unknown = GatewayDrop("dialog_allowlist", "refused", "safety", unknown_dialog=True)
    staged = DropLedger().record(unknown, unit="f", order_stage=True)
    assert staged.run_reason == "unknown_dialog_in_order_stage" and staged.has("kill_switch")
    loose = DropLedger().record(unknown, unit="f")
    assert loose.run is None and loose.has("quarantine_flow") and loose.findings[0].kind == "unknown_popup"


def test_agent_session_aborts_when_the_flag_is_set():
    r = agent_flag_abort("agent")
    assert r.step == "failed" and r.step_reason == "ticket_visible_in_agent_mode" and r.has("end_session")
    with pytest.raises(ValueError):
        agent_flag_abort("explorer")


def test_gateway_drops_carry_only_4_13_codes():
    with pytest.raises(ValueError):
        GatewayDrop("unknown_dialog_label", "refused", "safety")
    with pytest.raises(ValueError):
        SlowDecision(True, "no_token")                     # a hook code, never a slow-half refusal
    assert {"never_tokenize", "dialog_allowlist", "deny_profile", "guarded_key_refused", "hook_host_dead"} <= \
        GATEWAY_REASONS
    with pytest.raises(ValueError):
        SlowDecision(False, send_ok=True)                  # send_ok rides only on a dialog_ok click (6.7)


# -- session safety for Confirm and Send (choice 9, SD-57) -------------------------------------------------
NOW = 1000.0


def session(stage: str = "a3_a9", age: float | None = 1.0, kicked: bool = False) -> SessionSafety:
    return SessionSafety(stage, None if age is None else NOW - age, "sha-empty", kicked)


def confirm(**kw) -> Action:
    return click("Confirm and Send", point=CONFIRM_POINT, order_stage_step=True, **kw)


def test_confirm_and_send_reads_the_session_safety_the_orchestrator_sets():
    ok = slow_half(confirm(), pinned(flag=Flag.SET, session=session(), now=NOW))
    assert not ok.refused and ok.dialog_ok
    failed_attestation = pinned(session=session("none"), now=NOW)                 # attest() failed or never ran
    after_kill = pinned(session=SessionSafety(), now=NOW)                        # cleared on kill_tree or relaunch
    stale = pinned(session=session(age=PAPER_ASSERTION_CURRENT_S + 0.1), now=NOW)
    for ctx in (failed_attestation, after_kill, stale, pinned(session=session(kicked=True), now=NOW),
                pinned(session=session(), now=None), pinned(session=session(age=None), now=NOW)):
        s = slow_half(confirm(), ctx)
        assert (s.reason, s.attempted_send) == ("deny_profile", False), ctx
        assert "allowlist" not in s.detail                                       # refused for the session itself
    # with a session the legacy booleans are not read
    assert slow_half(confirm(), pinned(session=session("none"), now=NOW, attestation_passed=True,
                                       paper_assertion_current=True)).refused


def test_the_a9_click_needs_a3_to_a8_and_every_later_click_a3_to_a9():
    staged = pinned(session=session("a3_a8"), now=NOW)
    assert slow_half(confirm(), staged).reason == "deny_profile"
    assert not slow_half(confirm(a9_check=True), staged).refused
    assert slow_half(confirm(a9_check=True), pinned(session=session("none"), now=NOW)).refused


def test_paper_assertion_currency_is_the_two_minute_timer_of_6_4():
    assert PAPER_ASSERTION_CURRENT_S == TIMER_S
    edge = pinned(session=session(age=PAPER_ASSERTION_CURRENT_S), now=NOW)
    assert edge.assertion_current() and not slow_half(confirm(), edge).refused
    assert not SafetyContext(session=session(age=-1.0), now=NOW).assertion_current()   # a stamp from the future


# -- the dismiss allowlist (choice 10, 6.13 item 10) ---------------------------------------------------------
def test_an_empty_dismiss_allowlist_refuses_confirm_and_send_and_tokens_no_dialog_click():
    empty = SafetyContext(flag=Flag.SET, session=session(), now=NOW, dismiss_allowlist=frozenset())
    s = slow_half(confirm(), empty)
    assert (s.reason, s.attempted_send) == ("deny_profile", False) and "dismiss allowlist" in s.detail
    in_dialog = replace(empty, order_dialog_present=True, deny_rects=(SEND,))
    s = slow_half(click("Delete", point=DELETE_POINT), in_dialog)
    assert s.reason == "dialog_allowlist" and s.unknown_dialog and not s.dialog_ok


def test_edit_is_an_unknown_dialog_label_unless_pinned():
    ctx = pinned(flag=Flag.SET, order_dialog_present=True, deny_rects=(SEND,))
    assert DISMISS_ALLOWLIST == frozenset({"Delete"})
    s = slow_half(click("Edit"), ctx)
    assert s.reason == "dialog_allowlist" and s.unknown_dialog
    assert not slow_half(click("Edit"), replace(ctx, dismiss_allowlist=frozenset({"Delete", "Edit"}))).refused


def test_confirm_and_send_is_never_a_deny_rectangle():
    g = armed(Flag.SET)
    with pytest.raises(GatewayError):
        g.write_deny_rects([DenyRect(Rect(1490, 890, 130, 24), "Confirm and Send")])
    assert g.deny_rects == []


# -- recovery-only arm and disarm (choice 12) --------------------------------------------------------------
def test_recovery_only_arm_tokens_the_login_and_the_recovery_teardown_actions_only():
    rec = SafetyContext(recovery_only=True)
    assert slow_half(click("Watchlist"), rec).reason == "deny_profile"
    assert slow_half(Action("press", keys=(VK_TAB,)), rec).reason == "deny_profile"
    assert not slow_half(click("Log In", flow_class="login"), rec).refused
    assert not slow_half(Action("secret", keys=(ord("A"),), flow_class="login"), rec).refused
    assert not slow_half(click("Cancel All", teardown_action="cancel_all"), rec).refused
    assert not slow_half(click("Working Orders", teardown_action="working_orders_check"), rec).refused
    assert slow_half(click("Cancel All", flow_class="login"), rec).reason == "deny_profile"   # rules still apply
    s = slow_half(click("Send", teardown_action="cancel_all"), rec)
    assert s.reason == "never_tokenize" and s.attempted_send
    assert slow_half(click("Log In", profile="agent", flow_class="login"), rec).reason == "deny_profile"
    assert recovery_admits(click("x", teardown_action="positions_flat_check"))
    assert not recovery_admits(click("x", teardown_action="close_dialog"))


@pytest.mark.parametrize("phase,reason", [("between_runs", "not_armed"), ("kill_switch", "deny_all"),
                                          ("recovery", "no_token"), ("running", "no_token")])
def test_hook_states_per_phase(phase, reason):
    g = armed()
    g.armed, g.deny_all = HOOK_STATES[phase]
    assert hook_decide(g, ev("button_down", 5, 5), T).reason == reason


# -- the rest of the flow after a gateway block (choice 13) ---------------------------------------------------
def rest(res) -> list[tuple[str, str | None]]:
    return [(o.outcome.outcome, o.outcome.reason) for o in res.outcomes if o.selector == "rest_of_flow"]


def test_a_non_send_refusal_ends_the_flow_and_its_later_checkpoints_are_not_run():
    res = DropLedger().record(GatewayDrop("deny_profile", "refused", "integrity"), unit="flow.t", step_id="s03")
    assert res.step == "blocked_by_gateway" and res.has("end_flow") and res.run is None
    assert rest(res) == [("not_run", "step_failed:s03")]
    loose = DropLedger().record(GatewayDrop("dialog_allowlist", "refused", "safety", unknown_dialog=True),
                                unit="flow.t", step_id="s04")
    assert rest(loose) == [("not_run", "step_failed:s04")]
    ledger = DropLedger()
    for _ in range(2):
        ledger.record(GatewayDrop("deny_profile", "refused", "integrity"), unit="flow.q", step_id="s01")
    third = ledger.record(GatewayDrop("deny_profile", "refused", "integrity"), unit="flow.q", step_id="s02")
    assert third.step == "failed" and rest(third) == [("not_run", "step_failed:s02")]


def test_an_own_token_drop_after_the_retry_marks_its_checkpoint_hook_lost_and_the_rest_not_run():
    d = GatewayDrop("token_expired", "dropped", "integrity", own_token=True)
    ledger = DropLedger()
    assert rest(ledger.record(d, unit="f", step_id="s05")) == []                  # the single retry first
    again = ledger.record(d, unit="f", step_id="s05", retried=True)
    assert (again.checkpoint.outcome, again.checkpoint.reason) == ("harness_error", "hook_lost")
    assert rest(again) == [("not_run", "step_failed:s05")]


def test_aborting_and_session_results_carry_no_rest_of_flow():
    send = DropLedger().record(GatewayDrop("deny_rect", "dropped", "safety"), unit="f", step_id="s06")
    assert send.run_reason == "attempted_send" and rest(send) == []                # the run's unreached outcome
    agent = DropLedger().record(GatewayDrop("deny_profile", "refused", "integrity", "agent"), unit="f", step_id="s1")
    assert agent.has("end_session") and rest(agent) == []
    explorer = DropLedger().record(GatewayDrop("deny_profile", "refused", "integrity", "explorer"), unit="e")
    assert rest(explorer) == []


# -- a dead hook host goes to the Watchdog (choice 14) -------------------------------------------------------
def test_a_hook_host_dead_refusal_restarts_the_hook_host_once_then_ends_the_run():
    ledger, budgets = DropLedger(), ShardBudgets()
    dead = refusal(slow_half(click("Splitter"), SafetyContext(hook_alive=False)))
    first = ledger.record(dead, unit="flow.h", step_id="s02", budgets=budgets)
    assert first.step == "blocked_by_gateway" and first.step_reason == "hook_host_dead" and first.run is None
    for act in ("deny_all", "kill_tree", "restart_hook_host", "relaunch", "resume_next_flow"):
        assert first.has(act), act
    assert (first.checkpoint.outcome, first.checkpoint.reason) == ("harness_error", "hook_lost")
    assert rest(first) == [("not_run", "step_failed:s02")]
    second = ledger.record(dead, unit="flow.h", step_id="s07", budgets=budgets)
    assert (second.run_status, second.run_reason) == ("aborted_harness", "hook_lost") and second.has("kill_tree")
    assert ledger.run_count == 0                                                   # never counted (choice 3)
    with pytest.raises(ValueError):
        DropLedger().record(dead, unit="flow.h", step_id="s02")
