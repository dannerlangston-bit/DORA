"""The agent-mode toolset (5.6): tool schemas and their hash, the dispatcher, the rescue and Explorer loops, zoom."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pytest

from sentinel import agent
from sentinel.agent import tools
from sentinel.agent.dispatcher import (AgentRefused, CatalogEntry, Dispatcher, NameCatalog, ToolCall, ToolError,
                                       ToolRequest, check_response)
from sentinel.agent.session import (PROMPT_HASH, ExplorerLoop, HandlerResult, ModelResponse, Observation,
                                    RescueSession, StepEventFactory)
from sentinel.agent.zoom import NATIVE, ZoomError, ZoomRect, model_screenshot, to_native, verify_scale, zoom
from sentinel.executor.locator import RescueRequest
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.explorer import ExplorerBudget, ExplorerSession
from sentinel.orchestrator.gateway import (VK_TAB, DropLedger, Flag, SafetyContext, SlowDecision, slow_half)

PIN = "claude-pinned-1"
CATALOG = NameCatalog([
    CatalogEntry("Symbol", ref="trade.order-entry.stock.symbol-box", labels=("Symbol",)),
    CatalogEntry("Splitter", ref="charts.grid.splitter-1", target_kind="splitter"),
    CatalogEntry("Detach", ref="charts.grid.detach", target_kind="detach_handle"),
    CatalogEntry("Bubble", ref="charts.chart.order-bubble", target_kind="order_bubble"),
    CatalogEntry("Edit", ref="charts.chart.edit", labels=("Edit",)),
    CatalogEntry("Edit", ref="analyze.grid.edit", labels=("Edit",)),
    CatalogEntry("AAPL", source="ocr"),
])
TYPED = ("sym.aapl", "qty.one")


# -- tools and the schema version ------------------------------------------------------------------------------------
def test_the_profiles_get_the_5_6_tools():
    assert tools.tool_names("explorer") == ("click_named", "type_fixture", "press", "scroll", "drag_named", "zoom")
    assert "drag_named" not in tools.tool_names("agent")
    assert tools.schema_of("press")["properties"]["key"]["enum"] == list(tools.PRESS_KEYS)
    with pytest.raises(ValueError):
        tools.toolset("scripted")


def test_the_schema_hash_changes_when_any_tool_definition_changes():
    base = tools.toolset_schema_version()
    assert base == tools.TOOLSET_SCHEMA_VERSION and base.startswith("sha256:")
    for i in range(len(tools.TOOLS)):
        changed = copy.deepcopy([dict(t) for t in tools.TOOLS])
        changed[i]["description"] += " "
        assert tools.toolset_schema_version(changed) != base
    changed = copy.deepcopy([dict(t) for t in tools.TOOLS])
    changed[2]["input_schema"]["properties"]["key"]["enum"].append("enter")
    assert tools.toolset_schema_version(changed) != base
    tools.toolset("explorer")[0]["name"] = "mutated"                 # copies: the hashed definitions stay
    assert tools.toolset_schema_version() == base


def test_agent_enabled_only_on_dr07_branch_a():
    assert agent.agent_enabled({"computer_use": {"branch": "A"}})
    assert not agent.agent_enabled({"computer_use": {"branch": "B"}})
    assert not agent.agent_enabled({"computer_use": "undecided"})
    assert not agent.agent_enabled({})


# -- the dispatcher ----------------------------------------------------------------------------------------------
def explorer() -> Dispatcher:
    return Dispatcher("explorer", CATALOG, TYPED, "explorer_candidate")


def test_unknown_tool_bad_key_and_unknown_fixture_name_the_allowed_values_and_count():
    d = explorer()
    e1 = d.dispatch(ToolCall("computer", {"action": "left_click", "coordinate": [10, 10]}))
    assert isinstance(e1, ToolError) and e1.code == "unknown_tool" and "click_named" in e1.allowed
    assert d.refusals == 1 and not d.exhausted
    e2 = d.dispatch(ToolCall("press", {"key": "enter"}))
    assert e2.code == "key_not_allowed" and e2.allowed == tools.PRESS_KEYS and "shift_tab" in e2.message
    assert d.refusals == 2 and not d.exhausted
    e3 = d.dispatch(ToolCall("type_fixture", {"fixture_id": "free text"}))
    assert e3.code == "unknown_fixture" and e3.allowed == TYPED and "AAPL" not in e3.message
    assert d.refusals == 3 and d.exhausted                          # the third ends an Explorer session


def test_one_refusal_exhausts_an_agent_rescue():
    d = Dispatcher("agent", CATALOG, TYPED, "standard")
    assert d.limit == 1
    assert d.dispatch(ToolCall("drag_named", {"from": "Splitter", "to": "Detach"})).code == "unknown_tool"
    assert d.exhausted


def test_unknown_target_names_the_catalog_and_ambiguous_names_return_candidates():
    d = explorer()
    e = d.dispatch(ToolCall("click_named", {"target": "Buy MKT"}))
    assert e.code == "unknown_target" and "Symbol" in e.allowed and d.refusals == 1
    amb = d.dispatch(ToolCall("click_named", {"target": "Edit"}))
    assert amb.code == "ambiguous" and amb.allowed == ("analyze.grid.edit", "charts.chart.edit")
    assert not amb.refusal and d.refusals == 1
    ok = d.dispatch(ToolCall("click_named", {"target": "charts.chart.edit"}))
    assert isinstance(ok, ToolRequest) and ok.entries[0].ref == "charts.chart.edit"


def test_malformed_arguments_name_the_tool_arguments():
    d = explorer()
    e = d.dispatch(ToolCall("scroll", {"target": "AAPL", "notches": 0}))
    assert e.code == "malformed" and e.allowed == ("target", "notches")
    assert d.dispatch(ToolCall("click_named", {})).code == "malformed"
    assert d.dispatch(ToolCall("click_named", {"target": "AAPL", "x": 5})).code == "malformed"
    assert d.dispatch(ToolCall("zoom", {"region": {"x": 1200, "y": 0, "w": 100, "h": 10}})).code == "malformed"
    assert d.refusals == 4 and d.exhausted


@pytest.mark.parametrize("cls", ["login", "fixture_reset", "teardown", "canary", "paper_send_ok"])
def test_agent_mode_is_refused_in_the_excluded_flow_classes(cls):
    with pytest.raises(AgentRefused):
        Dispatcher("agent", CATALOG, TYPED, cls)


def test_response_checks_against_the_pin():
    assert check_response(model=PIN, stop_reason="tool_use", pin=PIN) is None
    assert check_response(model="claude-other", stop_reason="tool_use", pin=PIN) == "refusal"
    assert check_response(model=PIN, stop_reason="refusal", pin=PIN) == "refusal"
    assert check_response(model=PIN, stop_reason="max_tokens", pin=PIN) == "refusal"


def test_requests_become_gateway_actions_the_slow_half_judges():
    d = explorer()
    shift_tab = d.dispatch(ToolCall("press", {"key": "shift_tab"})).action("agent")
    assert shift_tab.kind == "key_chord" and shift_tab.keys == (VK_TAB,) and shift_tab.mods == {"shift"}
    assert not slow_half(shift_tab, SafetyContext()).refused
    drag = d.dispatch(ToolCall("drag_named", {"from": "Splitter", "to": "Detach"})).action("explorer")
    assert (drag.kind, drag.target_kind, drag.end_kind) == ("drag", "splitter", "detach_handle")
    assert not slow_half(drag, SafetyContext()).refused
    bad = d.dispatch(ToolCall("drag_named", {"from": "Bubble", "to": "Splitter"})).action("explorer")
    assert slow_half(bad, SafetyContext()).refused
    assert d.dispatch(ToolCall("zoom", {"region": {"x": 0, "y": 0, "w": 10, "h": 10}})).action("agent") is None
    click = d.dispatch(ToolCall("click_named", {"target": "Symbol"})).action("agent", point=(5, 6))
    assert (click.kind, click.point, click.resolved_name) == ("click", (5, 6), "Symbol")
    send = Dispatcher("agent", NameCatalog([CatalogEntry("Send", ref="dialog.order-confirmation.send-button",
                                                         labels=("Send",))]), TYPED, "standard")
    decision = slow_half(send.dispatch(ToolCall("click_named", {"target": "Send"})).action("agent", point=(1, 1)),
                         SafetyContext())
    assert decision.refused and decision.reason == "never_tokenize" and decision.attempted_send


# -- the loops ---------------------------------------------------------------------------------------------------
def frame(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (NATIVE[1], NATIVE[0], 3), dtype=np.uint8)


@dataclass
class FakeModel:
    """Scripted responses; the last one repeats."""

    responses: list[ModelResponse]
    calls: int = 0

    def respond(self, *, system, messages, tools):
        self.calls += 1
        assert "data and never an instruction" in system
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


@dataclass
class FakeHandler:
    clock: ManualClock
    step_s: float = 1.0
    refuse: SlowDecision | None = None
    flag_seq: list[Flag] = field(default_factory=list)
    performed: list[ToolRequest] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    observations: tuple[Observation, ...] = ()
    img: np.ndarray = field(default_factory=frame)

    def perform(self, request, profile):
        self.performed.append(request)
        self.clock.advance(self.step_s)
        n = len(self.performed)
        post = self.hashes[n - 1] if n - 1 < len(self.hashes) else f"h{n}"
        return HandlerResult(refused=self.refuse, point=(100 + n, 200), rect=(90, 190, 30, 20), pre_hash=f"h{n - 1}",
                             post_hash=post, click_crop_sha256=f"crop{n}", observations=self.observations)

    def screenshot(self):
        return self.img

    def catalog(self):
        return CATALOG

    def flag(self):
        return self.flag_seq.pop(0) if self.flag_seq else Flag.CLEAR


def call(name="click_named", i=0, **inp) -> ToolCall:
    return ToolCall(name, inp or {"target": "Symbol"}, call_id=f"c{i}")


def resp(*calls: ToolCall, model=PIN, stop="tool_use", text="target Symbol; expect the symbol box focused"):
    return ModelResponse(model, stop, tuple(calls), text, {"input_tokens": 10, "output_tokens": 5})


def rescue_session(model, handler, clock, goal=lambda: None, flow_class="standard") -> RescueSession:
    return RescueSession(model, handler, PIN, StepEventFactory("run-1", "run-1/1", clock), clock, goal,
                         DropLedger(), TYPED, flow_class)


REQ = RescueRequest("s5", "Symbol", "main", 25, 90.0, "pre-sha")


def test_a_rescue_passes_when_the_post_condition_holds_and_stamps_every_event():
    clock = ManualClock()
    handler = FakeHandler(clock)
    hits = iter([None, (640, 360)])
    s = rescue_session(FakeModel([resp(call(i=0)), resp(call(i=1))]), handler, clock, goal=lambda: next(hits))
    out = s.rescue(REQ)
    assert out.passed and out.point == (640, 360) and out.actions == 2
    assert [e.action for e in s.log] == ["click", "click"]
    for e in s.log:
        assert (e.mode, e.model_id, e.prompt_hash, e.locator_level) == ("agent", PIN, PROMPT_HASH, "agent")
        assert e.expected_outcome.startswith("target Symbol")
        assert e.usage["toolset_schema_version"] == tools.TOOLSET_SCHEMA_VERSION
        assert e.gate_decision == "emitted" and e.rect_w == 30


def test_the_26th_action_ends_the_rescue_with_cap():
    clock = ManualClock()
    handler = FakeHandler(clock, step_s=0.5)
    s = rescue_session(FakeModel([resp(call())]), handler, clock)
    out = s.rescue(REQ)
    assert out.failure == "cap" and out.actions == 25 and len(handler.performed) == 25


def test_the_91st_second_ends_the_rescue_with_cap():
    clock = ManualClock()
    handler = FakeHandler(clock, step_s=10.0)
    out = rescue_session(FakeModel([resp(call())]), handler, clock).rescue(REQ)
    assert out.failure == "cap" and len(handler.performed) == 10 and out.seconds > 90


def test_a_rescue_ends_on_a_vocabulary_refusal_a_model_mismatch_or_a_refused_response():
    clock = ManualClock()
    out = rescue_session(FakeModel([resp(call("press", key="enter"))]), FakeHandler(clock), clock).rescue(REQ)
    assert out.failure == "vocabulary"
    out = rescue_session(FakeModel([resp(call(), model="claude-other")]), FakeHandler(clock), clock).rescue(REQ)
    assert out.failure == "refusal"
    out = rescue_session(FakeModel([resp(stop="refusal")]), FakeHandler(clock), clock).rescue(REQ)
    assert out.failure == "refusal"
    out = rescue_session(FakeModel([resp(stop="end_turn")]), FakeHandler(clock), clock).rescue(REQ)
    assert out.failure == "cap"                                     # choice 2


@pytest.mark.parametrize("cls", ["login", "fixture_reset", "teardown", "canary", "paper_send_ok"])
def test_a_rescue_never_runs_in_the_excluded_classes(cls):
    clock = ManualClock()
    model = FakeModel([resp(call())])
    out = rescue_session(model, FakeHandler(clock), clock, flow_class=cls).rescue(REQ)
    assert out.failure == "disabled" and model.calls == 0


def test_the_flag_aborts_the_rescue_the_instant_it_is_set_or_unknown():
    clock = ManualClock()
    handler = FakeHandler(clock, flag_seq=[Flag.CLEAR, Flag.CLEAR, Flag.UNKNOWN])
    s = rescue_session(FakeModel([resp(call(i=0)), resp(call(i=1))]), handler, clock)
    out = s.rescue(REQ)
    assert out.failure == "abort_flag" and len(handler.performed) == 1
    assert s.consequences[0].step_reason == "ticket_visible_in_agent_mode"


def test_a_gateway_refusal_ends_the_rescue_through_the_ledger():
    clock = ManualClock()
    deny = SlowDecision(True, "deny_profile", detail="opens_ticket")
    s = rescue_session(FakeModel([resp(call())]), FakeHandler(clock, refuse=deny), clock)
    out = s.rescue(REQ)
    assert out.failure == "refusal" and s.consequences[0].step == "failed" and s.consequences[0].has("end_session")
    assert s.log[0].gate_decision == "refused" and s.log[0].gate_reason == "deny_profile"
    send = SlowDecision(True, "never_tokenize", attempted_send=True)
    s = rescue_session(FakeModel([resp(call())]), FakeHandler(clock, refuse=send), clock)
    s.rescue(REQ)
    assert s.consequences[0].run_status == "aborted_safety" and s.consequences[0].run_reason == "attempted_send"


def test_the_emitter_view_of_a_rescue():
    clock = ManualClock()
    s = rescue_session(FakeModel([resp(call())]), FakeHandler(clock), clock, goal=lambda: (1, 2))
    locate, consequences, reason, trace = s.emitter_rescue(REQ)
    assert locate.rung == "agent" and locate.point == (1, 2) and reason is None and consequences == ()


def explorer_loop(model, handler, clock, *, verify=lambda r, e, res: True, budget=ExplorerBudget()) -> ExplorerLoop:
    session = ExplorerSession("exp-1", clock, budget=budget, ledger=DropLedger())
    return ExplorerLoop(model, handler, session, PIN, StepEventFactory("run-1", "run-1/6", clock), verify,
                        lambda usage: 0.01, TYPED)


def test_the_third_refusal_ends_an_explorer_session_with_agent_failed_vocabulary():
    clock = ManualClock()
    model = FakeModel([resp(call("press", 0, key="enter"), call("computer", 1, x=1), call("click_named", 2,
                                                                                            target="Buy"))])
    out = explorer_loop(model, FakeHandler(clock), clock).run()
    assert out.ended == "agent_failed(vocabulary)" and len(out.events) == 3
    assert all(e.reason.startswith("tool_error:") for e in out.events)


def test_explorer_actions_fill_the_sd16_columns_and_a_failed_verification_is_an_agent_error():
    clock = ManualClock()
    obs = Observation("charts.chart", "INV-03", "sha-crop")
    handler = FakeHandler(clock, observations=(obs,))
    verdicts = iter([True, False])
    loop = explorer_loop(FakeModel([resp(call(i=0)), resp(call(i=1))]), handler, clock,
                         verify=lambda r, e, res: next(verdicts, True), budget=ExplorerBudget(max_actions=2))
    out = loop.run()
    assert out.ended == "budget actions" and out.budget_hit == "actions" and out.actions == 2
    acts = [e for e in out.events if e.reason is None]
    assert len(acts) == 2
    e = acts[0]
    assert (e.mode, e.action, e.target_text, e.rect_w, e.rect_h) == ("explorer", "explorer_action",
                                                                      "trade.order-entry.stock.symbol-box", 30, 20)
    assert e.expected_outcome.startswith("target Symbol") and e.usage["input_tokens"] == 10
    assert (e.pre_hash, e.post_hash, e.click_crop_sha256, e.model_id) == ("h0", "h1", "crop1", PIN)
    errs = [e for e in out.events if e.reason == "agent_error"]
    assert len(errs) == 1 and out.agent_errors == 1 and out.trace[1].agent_error
    assert [f.seq for f in out.findings] == [acts[0].seq, acts[1].seq]
    assert out.usd == pytest.approx(0.02)


def test_the_flag_in_an_explorer_session_takes_the_order_stage_path():
    clock = ManualClock()
    handler = FakeHandler(clock, flag_seq=[Flag.CLEAR, Flag.SET])
    out = explorer_loop(FakeModel([resp(call(i=0)), resp(call(i=1))]), handler, clock).run()
    assert out.ended == "order_stage" and out.consequences[-1].findings[0].kind == "explorer_order_stage"


def test_three_denied_attempts_end_an_explorer_session():
    clock = ManualClock()
    deny = SlowDecision(True, "deny_profile", detail="chat")
    out = explorer_loop(FakeModel([resp(call())]), FakeHandler(clock, refuse=deny), clock).run()
    assert out.deny_hits == 3 and out.ended.startswith("denied")


def test_no_new_screenshot_hash_for_twenty_actions_stops_the_session():
    clock = ManualClock()
    handler = FakeHandler(clock, hashes=["same"] * 40)
    out = explorer_loop(FakeModel([resp(call())]), handler, clock).run()
    assert out.budget_hit == "no_change" and out.actions == 21


# -- zoom ----------------------------------------------------------------------------------------------------------
def test_zoom_of_a_known_template_region_maps_to_the_native_rect_and_matches():
    img = frame(3)
    tpl_rect = ZoomRect(600, 300, 90, 60)
    template = img[300:360, 600:690].copy()
    assert verify_scale(img, template, tpl_rect)
    assert to_native(ZoomRect(400, 200, 60, 40)) == ZoomRect(600, 300, 90, 60)
    crop = zoom(img, ZoomRect(400, 200, 60, 40))
    assert np.array_equal(crop, img[300:360, 600:690])


def test_a_wrong_scale_fails_the_check():
    img = frame(4)
    template = img[300:360, 600:690].copy()
    assert not verify_scale(img, template, ZoomRect(400, 200, 90, 60))     # the rect a 1.0 scale would give


def test_zoom_caps_the_long_edge_and_refuses_outside_the_screenshot():
    img = frame(5)
    assert max(zoom(img, ZoomRect(0, 0, 1280, 720)).shape[:2]) == 1280
    assert model_screenshot(img).shape[:2] == (720, 1280)
    with pytest.raises(ZoomError):
        zoom(img, ZoomRect(1200, 0, 100, 10))
    with pytest.raises(ZoomError):
        model_screenshot(img[:720, :1280])


def test_an_action_that_raises_the_flag_aborts_before_the_goal_check():
    clock = ManualClock()
    handler = FakeHandler(clock, flag_seq=[Flag.CLEAR, Flag.CLEAR, Flag.SET])
    s = rescue_session(FakeModel([resp(call())]), handler, clock, goal=lambda: (1, 1))
    out = s.rescue(REQ)
    assert out.failure == "abort_flag" and len(handler.performed) == 1 and not out.passed


def test_tool_results_of_one_turn_go_back_in_one_message():
    clock = ManualClock()
    seen = []

    class Recorder(FakeModel):
        def respond(self, *, system, messages, tools):
            seen.append(list(messages))
            return super().respond(system=system, messages=messages, tools=tools)

    hits = iter([None, None, (3, 3)])
    s = rescue_session(Recorder([resp(call(i=0), call(i=1)), resp(call(i=2))]), FakeHandler(clock), clock,
                       goal=lambda: next(hits))
    assert s.rescue(REQ).passed
    last = seen[-1]
    assert [m["role"] for m in last] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in last[2]["content"]] == ["c0", "c1"]


def test_an_explorer_model_that_stops_calling_tools_meets_the_no_change_stop():
    clock = ManualClock()
    out = explorer_loop(FakeModel([resp(stop="end_turn")]), FakeHandler(clock), clock).run()
    assert out.budget_hit == "no_change" and out.events == []
