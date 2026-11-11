"""Input primitives: the token sequence of every 4.7 step type (5.4.10, 5.4.3 stages 5 and 6, 5.4.5)."""
from __future__ import annotations

import pytest

from sentinel.executor.primitives import (MIN_DRAG_STEPS, RELEASE_SPECS, VK_PACKET, WHEEL_DELTA, PlanError,
                                          PlannedAction, Planner, Resolution, issue_tokens, overdue_reset, plan)
from sentinel.orchestrator.gateway import (DRAG_EXTRA_S, VK_CONTROL, VK_ESCAPE, VK_LWIN, VK_MENU, VK_RETURN,
                                           VK_SHIFT, VK_TAB, Flag, GatewayState, InputEvent, SafetyContext,
                                           hook_decide, slow_half)
from sentinel.orchestrator.ports import PrimitivePlanner

T0 = 100.0


def armed(flag: Flag = Flag.CLEAR) -> GatewayState:
    s = GatewayState(armed=True)
    s.stamp_watcher(flag, T0)
    return s


def drive(action: PlannedAction, state: GatewayState, t0: float = T0, *, flag: Flag = Flag.CLEAR,
          dialog_ok: bool = False, at: dict[int, float] | None = None):
    """Stages 5 to 7 against the real hook: each unit's lead moves, its tokens, then its events at their offsets
    (`at` overrides the time of the n-th tokened event of the action)."""
    out = []
    t, n = t0, 0
    for unit in action.units:
        for e in unit.lead:
            state.stamp_watcher(flag, t)
            out.append((e, hook_decide(state, e, t)))
        issue_tokens(unit, state, t, dialog_ok=dialog_ok)
        last = t
        for em in unit.events:
            when = (at or {}).get(n, t + em.at_ms / 1000)
            state.stamp_watcher(flag, when)
            out.append((em.event, hook_decide(state, em.event, when)))
            last, n = max(last, when), n + 1
        t = last + 0.01
    return out


def all_allowed(decisions) -> bool:
    return all(d.allowed for _, d in decisions)


# -- pointer steps ------------------------------------------------------------------------------------------
def test_click_named_is_move_then_one_token_for_down_and_up():
    (a,) = plan({"type": "click_named", "target": "trade.tab"}, {"target": (400, 300)}, Flag.CLEAR, T0,
                resolved={"target": Resolution("Trade", "Trade", None)})
    assert a.kind == "click" and a.point == (400, 300) and a.labels == ("Trade", "Trade")
    assert a.pairs() == ((None, "move"), ("click", "button_down"), ("click", "button_up"))
    (unit,) = a.units
    assert unit.lead == (InputEvent("move", 400, 300),)
    state = armed()
    decisions = drive(a, state)
    assert all_allowed(decisions)
    assert [t.state for t in state.tokens.values()] == ["consumed"]


def test_rclick_uses_the_right_button_and_click_named_takes_a_button():
    (a,) = plan({"type": "rclick", "target": "x"}, {"target": (5, 6)}, Flag.CLEAR, T0)
    assert a.kind == "rclick" and {e.event.button for e in a.units[0].events} == {"right"}
    (m,) = plan({"type": "click_named", "target": "x", "button": "middle"}, {"target": (5, 6)}, Flag.CLEAR, T0)
    assert m.units[0].tokens[0].button == "middle"
    with pytest.raises(PlanError):
        plan({"type": "click_named", "target": "x", "button": "x2"}, {"target": (5, 6)}, Flag.CLEAR, T0)


def test_click_without_its_resolved_point_is_refused():
    with pytest.raises(PlanError, match="target"):
        plan({"type": "click_named", "target": "x"}, {}, Flag.CLEAR, T0)


def test_dblclick_is_two_pairs_inside_the_double_click_time():
    (a,) = plan({"type": "dblclick", "target": "x"}, {"target": (50, 60)}, Flag.CLEAR, T0, double_click_ms=500)
    assert a.kind == "dblclick"
    assert a.pairs() == ((None, "move"), ("click", "button_down"), ("click", "button_up"),
                         ("click", "button_down"), ("click", "button_up"))
    unit = a.units[0]
    assert len(unit.tokens) == 2 and [e.token for e in unit.events] == [0, 0, 1, 1]
    second = unit.events[2].at_ms
    assert 0 < second < 500 and second < 500                      # double-click time and the 500 ms expiry
    state = armed()
    assert all_allowed(drive(a, state))
    assert sorted(t.state for t in state.tokens.values()) == ["consumed", "consumed"]


def test_drag_interpolates_at_least_ten_moves_inside_move_allow():
    step = {"type": "drag", "from": "a", "to": "b", "steps": 12, "hold_ms": 600}
    (a,) = plan(step, {"from": (100, 200), "to": (300, 260)}, Flag.CLEAR, T0)
    unit = a.units[0]
    moves = [e for e in unit.events if e.event.type == "move"]
    assert len(moves) == 12 >= MIN_DRAG_STEPS
    assert all(e.token is None for e in moves)
    assert all(unit.move_allow.contains(e.event.x, e.event.y) for e in moves)
    assert (moves[-1].event.x, moves[-1].event.y) == (300, 260)
    assert unit.events[0].event.type == "button_down" and (unit.events[0].event.x, unit.events[0].event.y) == (100, 200)
    up = unit.events[-1]
    assert up.event.type == "button_up" and (up.event.x, up.event.y) == (300, 260) and up.at_ms == 600
    assert all(0 < e.at_ms < 600 for e in moves)
    state = armed()
    assert all_allowed(drive(a, state))
    tok = next(iter(state.tokens.values()))
    assert tok.expires == pytest.approx(T0 + 0.6 + DRAG_EXTRA_S)              # hold_ms + 1000
    assert state.move_allow.until == pytest.approx(T0 + 0.6 + DRAG_EXTRA_S)


def test_drag_moves_after_hold_plus_one_second_are_dropped_and_the_up_still_passes():
    (a,) = plan({"type": "drag", "from": "a", "to": "b", "steps": 10, "hold_ms": 200}, {"from": (0, 0), "to": (90, 0)},
                Flag.CLEAR, T0)
    late = T0 + 0.2 + DRAG_EXTRA_S + 0.05
    # the last move and the up arrive after move_allow expired, while the press token is still half consumed
    decisions = drive(a, armed(), at={10: late, 11: late})
    *early, (last_move, d_move), (up, d_up) = decisions
    assert all_allowed(early)
    assert last_move.type == "move" and d_move.reason == "move_outside_allow"
    assert up.type == "button_up" and d_up.allowed and d_up.late_up


def test_drag_bounds_follow_l_flw_09():
    pts = {"from": (0, 0), "to": (10, 10)}
    with pytest.raises(PlanError, match="steps"):
        plan({"type": "drag", "steps": 9, "hold_ms": 100}, pts, Flag.CLEAR, T0)
    with pytest.raises(PlanError, match="hold_ms"):
        plan({"type": "drag", "steps": 10, "hold_ms": 2001}, pts, Flag.CLEAR, T0)


def test_drag_start_and_end_kinds_reach_the_slow_half():
    (a,) = plan({"type": "drag", "steps": 10, "hold_ms": 300}, {"from": (0, 0), "to": (40, 0)}, Flag.CLEAR, T0,
                resolved={"from": Resolution("bubble", None, "order_bubble"), "to": Resolution(kind="splitter")})
    assert a.target_kind == "order_bubble" and a.end_kind == "splitter"
    s = slow_half(a, SafetyContext())
    assert s.refused and s.reason == "deny_profile" and "order_bubble" in s.detail


def test_wheel_is_one_token_per_notch_then_a_park_move():
    (a,) = plan({"type": "wheel", "at": "chart", "notches": -3}, {"at": (700, 400), "park": (5, 1070)}, Flag.CLEAR, T0)
    assert a.kind == "wheel" and len(a.units) == 4
    assert a.pairs() == ((None, "move"), ("wheel", "wheel"), ("wheel", "wheel"), ("wheel", "wheel"), (None, "move"))
    wheels = [e.event for u in a.units for e in u.events]
    assert all(e.delta == -WHEEL_DELTA for e in wheels)
    assert all(u.tokens[0].sign == -1 for u in a.units[:3])
    assert a.units[-1].lead == (InputEvent("move", 5, 1070),) and not a.units[-1].tokens
    state = armed()
    assert all_allowed(drive(a, state))                         # the park move passes once every notch is consumed


def test_wheel_needs_notches_and_a_park_point():
    with pytest.raises(PlanError, match="notches"):
        plan({"type": "wheel", "notches": 0}, {"at": (1, 1), "park": (2, 2)}, Flag.CLEAR, T0)
    with pytest.raises(PlanError, match="park"):
        plan({"type": "wheel", "notches": 1}, {"at": (1, 1)}, Flag.CLEAR, T0)


def test_park_mouse_is_one_untokened_move_and_focus_neutral_a_tokened_click_never_escape():
    (park,) = plan({"type": "park_mouse"}, {"park": (3, 1077)}, Flag.SET, T0)
    assert park.pairs() == ((None, "move"),) and not park.units[0].tokens
    (neutral,) = plan({"type": "focus_neutral", "target": "shell.status-bar.neutral"}, {"target": (900, 1060)},
                      Flag.CLEAR, T0)
    assert neutral.kind == "click"
    assert neutral.pairs() == ((None, "move"), ("click", "button_down"), ("click", "button_up"))
    assert all(e.event.vk != VK_ESCAPE for u in neutral.units for e in u.events)


# -- keys --------------------------------------------------------------------------------------------------
def test_press_is_one_key_token_for_down_and_up():
    (a,) = plan({"type": "press", "key": "escape"}, {}, Flag.CLEAR, T0)
    assert a.kind == "press" and a.keys == (VK_ESCAPE,) and not a.declared_guarded
    assert a.pairs() == (("key", "key_down"), ("key", "key_up"))
    assert all_allowed(drive(a, armed()))


def test_press_enter_is_the_explicit_declaration_of_a_guarded_key():
    (a,) = plan({"type": "press", "key": "enter"}, {}, Flag.CLEAR, T0)
    assert a.declared_guarded and a.keys == (VK_RETURN,)
    assert not slow_half(a, SafetyContext()).refused
    (chord,) = plan({"type": "press", "key": "shift+tab"}, {}, Flag.CLEAR, T0)
    assert chord.keys == (VK_TAB,) and chord.mods == frozenset({"shift"})
    assert chord.pairs() == (("key", "key_down"),) * 2 + (("key", "key_up"),) * 2


def test_key_chord_holds_modifiers_in_order_and_releases_them_in_reverse():
    (a,) = plan({"type": "key_chord", "keys": ["ctrl", "shift", "a"]}, {}, Flag.CLEAR, T0)
    assert a.kind == "key_chord" and a.keys == (ord("A"),) and a.mods == frozenset({"ctrl", "shift"})
    events = [e.event for e in a.units[0].events]
    assert [(e.type, e.vk) for e in events] == [
        ("key_down", VK_CONTROL), ("key_down", VK_SHIFT), ("key_down", ord("A")), ("key_up", ord("A")),
        ("key_up", VK_SHIFT), ("key_up", VK_CONTROL)]
    assert events[2].mods == frozenset({"ctrl", "shift"}) and events[0].mods == frozenset()
    assert all_allowed(drive(a, armed()))


def test_key_chord_releases_the_modifiers_even_when_the_key_is_dropped():
    (a,) = plan({"type": "key_chord", "keys": ["ctrl", "alt", "x"]}, {}, Flag.CLEAR, T0)
    late = T0 + 0.6                                       # the key's down arrives after its 500 ms expiry
    decisions = drive(a, armed(), at={2: late, 3: late, 4: late, 5: late})
    kinds = [(e.type, e.vk, d.allowed) for e, d in decisions]
    assert kinds[2] == ("key_down", ord("X"), False) and decisions[2][1].reason == "token_expired"
    assert kinds[-2:] == [("key_up", VK_MENU, True), ("key_up", VK_CONTROL, True)]   # late ups still pass


def test_key_chord_shape_is_checked():
    for keys in (["ctrl"], ["a", "ctrl"], ["ctrl", "ctrl", "a"], ["hyper", "a"]):
        with pytest.raises(PlanError):
            plan({"type": "key_chord", "keys": keys}, {}, Flag.CLEAR, T0)


# -- typing ------------------------------------------------------------------------------------------------
def test_type_fixture_is_one_key_pair_per_character_then_the_commit_pair():
    step = {"type": "type_fixture", "target": "t.symbol-box", "value_ref": "fixture.symbols.mes_front.symbol",
            "commit": "enter"}
    (a,) = plan(step, {}, Flag.CLEAR, T0, values={"fixture.symbols.mes_front.symbol": "/MESZ26"})
    assert a.kind == "type_fixture" and a.point is None                    # no click (choice 2)
    assert a.keys == (VK_RETURN,) and a.declared_guarded                     # commit: enter is a guarded key
    assert len(a.units) == 8 and a.chars() == 7
    for unit, c in zip(a.units, "/MESZ26"):
        assert [(e.event.type, e.event.vk, e.char) for e in unit.events] == [("key_down", VK_PACKET, c),
                                                                             ("key_up", VK_PACKET, c)]
    assert [(e.event.type, e.event.vk) for e in a.units[-1].events] == [("key_down", VK_RETURN), ("key_up", VK_RETURN)]
    assert all_allowed(drive(a, armed()))


def test_type_fixture_commit_tab_and_none():
    (tab,) = plan({"type": "type_fixture", "value": "1", "commit": "tab"}, {}, Flag.CLEAR, T0)
    assert tab.keys == (VK_TAB,) and not tab.declared_guarded
    (none,) = plan({"type": "type_fixture", "value": "12"}, {}, Flag.CLEAR, T0)
    assert none.keys == () and len(none.units) == 2


def test_type_fixture_never_types_text_from_an_agent():
    step = {"type": "type_fixture", "value": "AAPL", "commit": "none"}
    for profile in ("agent", "explorer"):
        with pytest.raises(PlanError, match="tool error"):
            plan(step, {}, Flag.CLEAR, T0, profile=profile)
    (ok,) = plan({"type": "type_fixture", "value_ref": "qty"}, {}, Flag.CLEAR, T0, profile="agent",
                 values={"qty": "100"})
    assert ok.profile == "agent" and ok.chars() == 3
    with pytest.raises(PlanError, match="typed values"):
        plan({"type": "type_fixture", "value_ref": "model.said"}, {}, Flag.CLEAR, T0, profile="agent")
    with pytest.raises(PlanError, match="exactly one"):
        plan({"type": "type_fixture", "value": "1", "value_ref": "qty"}, {}, Flag.CLEAR, T0, values={"qty": "1"})
    with pytest.raises(PlanError, match="exactly one"):
        plan({"type": "type_fixture", "commit": "tab"}, {}, Flag.CLEAR, T0)


@pytest.mark.parametrize("value", ["1\n", "a b", "x\ty", " "])
def test_control_and_space_characters_are_never_typed(value):
    with pytest.raises(PlanError) as exc:
        plan({"type": "type_fixture", "value": value}, {}, Flag.CLEAR, T0)
    assert value not in str(exc.value)


def test_secret_plan_uses_secret_tokens_and_keeps_only_the_reference():
    planner = Planner(flow_class="login")
    a = planner.plan_secret({"type": "secret", "target": "login.password-field", "vault_ref": "login.password",
                             "commit": "tab"}, "hunter2!", T0)
    assert a.kind == "secret" and a.secret_ref == "login.password" and a.chars() == 8
    assert all(t.secret for u in a.units for t in u.tokens)
    assert "hunter2" not in repr(a)
    state = armed()
    assert all_allowed(drive(a, state))
    assert all(t.secret for t in state.tokens.values())


def test_secret_steps_plan_only_in_scripted_login_flows_and_only_through_plan_secret():
    step = {"type": "secret", "vault_ref": "login.password"}
    with pytest.raises(PlanError, match="login"):
        Planner(flow_class="standard").plan_secret(step, "x", T0)
    with pytest.raises(PlanError, match="login"):
        Planner(profile="agent", flow_class="login").plan_secret(step, "x", T0)
    with pytest.raises(PlanError, match="run_secret_step"):
        Planner(flow_class="login").plan(step, {}, Flag.CLEAR, T0)


# -- reset_input_state and the 2 s rule --------------------------------------------------------------------
def test_reset_input_state_releases_everything_and_escapes_only_with_the_flag_clear():
    release, escape = plan({"type": "reset_input_state"}, {}, Flag.CLEAR, T0)
    assert release.units[0].tokens == RELEASE_SPECS
    assert [(e.event.type, e.event.vk or e.event.button) for e in release.units[0].events] == [
        ("key_up", VK_SHIFT), ("key_up", VK_CONTROL), ("key_up", VK_MENU), ("key_up", VK_LWIN),
        ("button_up", "left"), ("button_up", "right"), ("button_up", "middle")]
    assert escape.keys == (VK_ESCAPE,) and escape.flag_clear_only
    assert escape.allowed_under(Flag.CLEAR) and not escape.allowed_under(Flag.SET)
    assert not escape.allowed_under(Flag.UNKNOWN)
    for flag in (Flag.SET, Flag.UNKNOWN):
        (only,) = plan({"type": "reset_input_state"}, {}, flag, T0)
        assert only.kind == "reset_input_state"
        assert all(e.event.vk != VK_ESCAPE for u in only.units for e in u.events)


def test_release_tokens_pass_at_any_coordinate_with_nothing_held():
    (release,) = plan({"type": "reset_input_state"}, {}, Flag.SET, T0)
    state = armed(Flag.SET)
    assert all_allowed(drive(release, state, flag=Flag.SET))


def test_an_overdue_half_consumed_token_produces_a_reset_plan():
    state = armed()
    t = state.issue_click(10, 10, T0)
    hook_decide(state, InputEvent("button_down", 10, 10, button="left"), T0)
    assert t.state == "half_consumed"
    assert overdue_reset(state, Flag.CLEAR, T0 + 1.0) == ()
    plans = overdue_reset(state, Flag.SET, t.expires + 2.0)
    assert [p.kind for p in plans] == ["reset_input_state"]
    assert len(overdue_reset(state, Flag.CLEAR, t.expires + 2.0)) == 2


# -- token write, teardown and the protocol ----------------------------------------------------------------
def test_issue_tokens_carries_the_slow_half_answer():
    (click,) = plan({"type": "click_named", "target": "x"}, {"target": (1, 2)}, Flag.CLEAR, T0)
    state = armed()
    (tok,) = issue_tokens(click.units[0], state, T0, dialog_ok=True, send_ok=True)
    assert tok.dialog_ok and tok.send_ok and tok.expires == pytest.approx(T0 + 0.5)
    (dbl,) = plan({"type": "dblclick", "target": "x"}, {"target": (1, 2)}, Flag.CLEAR, T0)
    with pytest.raises(ValueError, match="send_ok"):
        issue_tokens(dbl.units[0], armed(), T0, send_ok=True)


def test_teardown_with_a_target_is_a_click_that_names_its_teardown_action():
    lbl = {"target": Resolution("Cancel All", "Cancel All")}
    (a,) = plan({"type": "teardown", "action": "cancel_all"}, {"target": (10, 10)}, Flag.CLEAR, T0, resolved=lbl)
    assert a.kind == "click" and a.teardown_action == "cancel_all"
    assert not slow_half(a, SafetyContext()).refused
    (bad,) = plan({"type": "click_named", "target": "x"}, {"target": (10, 10)}, Flag.CLEAR, T0, resolved=lbl)
    assert slow_half(bad, SafetyContext()).reason == "deny_profile"


@pytest.mark.parametrize("kind", ["wait_for", "checkpoint", "assert", "precondition", "export", "copy_grid", "bogus"])
def test_steps_that_are_not_primitives_are_refused(kind):
    with pytest.raises(PlanError):
        plan({"type": kind}, {}, Flag.CLEAR, T0)


def test_order_stage_and_flow_context_reach_the_action():
    (a,) = plan({"type": "click_named", "target": "x", "order_stage": True}, {"target": (1, 1)}, Flag.CLEAR, T0,
                flow_class="standard", account_role="golden")
    assert a.order_stage_step and a.flow_class == "standard" and a.account_role == "golden" and a.planned_at == T0


def test_planner_is_a_primitive_planner():
    assert isinstance(Planner(), PrimitivePlanner)
