"""The emitter: stages 1 to 8 of 5.4.3 for one step, the single retry, the locator ladder's retry and agent rung, and
the step outcome rules (5.4, 5.4.3, 5.5, 4.7; SD-52, SD-53; executor/emitter.py).

The fakes here (`FakePorts`, `FakeLocator`, `FakePlanner`) are shared with test_executor_flow.py. `FakePorts.send_input`
runs the real slow half (`gateway.slow_half`) and answers the hook's part from a script."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from sentinel.executor.emitter import (DEGRADED_TEXT, NO_NEUTRAL, Effect, ExecContext, Foreground, ModalState, Rescue,
                                       agent_rung_block, click_crop, declares_guarded, emit, targets_dialog)
from sentinel.executor.heartbeat import AppLife, HeartbeatPolicy
from sentinel.executor.primitives import Planner
from sentinel.gateway.drainer import DrainContext, Drainer
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gateway import (VK_RETURN, VK_TAB, Action, DropLedger, Flag, HookDecision, InputEvent,
                                           SafetyContext, slow_half)
from sentinel.orchestrator.ports import Frame, LocateResult
from sentinel.orchestrator.results import ControlResult, FindingSpec, Outcome, step_failed
from sentinel.orchestrator.rig import EmitResult
from sentinel.orchestrator.watchdog import HeartbeatUnwritable
from sentinel.records import StepOutcome

FLOW = "flow.trade.order-ticket.stock.limit"
POINT = (100, 100)
OK = LocateResult("image", point=POINT, rung_results=({"rung": "jab", "result": "rung1_skipped"},
                                                      {"rung": "image", "result": "accepted", "score": 0.96}))
MISS = LocateResult("none", rung_results=({"rung": "image", "result": "locator_failed(image)", "score": 0.41},
                                          {"rung": "ocr", "result": "locator_failed(ocr)"}))
KEYS = {"enter": VK_RETURN, "tab": VK_TAB, "escape": 0x1B}


# -- fakes ----------------------------------------------------------------------------------------------------------
class FakeLocator:
    """Answers per target name from a script (the last answer repeats); OK otherwise."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def locate(self, target, frame):
        self.calls.append(dict(target))
        seq = self.script.get(target["name"])
        if not seq:
            return OK
        return seq.pop(0) if len(seq) > 1 else seq[0]


class FakePlanner:
    """One gateway action per step, as the 5.4.10 table's coarse shape (the real token sequences are primitives.py)."""

    def __init__(self):
        self.calls = []

    def plan(self, step, points, flag, now):
        self.calls.append((dict(step), dict(points), flag))
        kind = step["type"]
        if kind in ("click_named", "focus_neutral", "export", "copy_grid", "teardown"):
            return (Action("click", point=points.get("target")),)
        if kind in ("rclick", "dblclick"):
            return (Action(kind, point=points["target"]),)
        if kind == "drag":
            return (Action("drag", point=points["from"]),)
        if kind == "press":
            return (Action("press", keys=(KEYS[step["key"]],)),)
        if kind == "type_fixture":
            commit = (KEYS[step["commit"]],) if step.get("commit") in KEYS else ()
            return (Action("type_fixture", keys=(0x31, 0x30, 0x30) + commit),)
        if kind == "secret":
            return (Action("secret", keys=(0x41,) * 8),)
        return ()


def _events(a):
    if a.is_pointer:
        x, y = a.point or (0, 0)
        return (InputEvent("button_down", x, y, button="left"), InputEvent("button_up", x, y, button="left"))
    out = []
    for k in a.keys:
        out += [InputEvent("key_down", vk=k), InputEvent("key_up", vk=k)]
    return tuple(out)


class FakePorts:
    """Every port `emit` and `run_unit` read, with scripted answers."""

    def __init__(self, clock, *, safety=None):
        self.clock = clock
        self.fg = [Foreground(1, 100, True)]
        self.modal = ModalState(True)
        self.track_dialog = False        # an allowed order-stage click opens the dialog; close_dialog shuts it
        self.dialog_open = False
        self.flag_value = Flag.CLEAR
        self.label = None
        self.kept = []
        self.sent = []
        self.responses = []              # "allow", "never" (no decision: the hook never saw it), "drop:<reason>"
        self.probe = True
        self.resets = 0
        self.focus = []
        self.enumerations = 0
        self.effects = []
        self.popups = []
        self.popup_result = ControlResult("popup_sentinel", False, "running",
                                          findings=(FindingSpec("unknown_popup", "unknown modal closed"),))
        self.safety = safety or SafetyContext(flag=Flag.CLEAR, dismiss_allowlist=frozenset({"Delete"}))
        self.token = 0
        # run_unit's ports
        self.checks = {}
        self.reads = []
        self.ui_resets = self.profile_resets = self.reloads = 0
        self.waits = []
        self.captured = []
        self.capture_results = {}
        self.verdicts = {}
        self.teardowns = []
        self.teardown_reads = {}

    def foreground(self):
        return self.fg.pop(0) if len(self.fg) > 1 else self.fg[0]

    def set_focus(self, window):
        self.focus.append(window)

    def modal_state(self):
        return ModalState(False, "Order Confirmation", 7) if self.dialog_open else self.modal

    def flag(self):
        return self.flag_value

    def grab(self):
        return Frame(np.zeros((300, 400, 3), np.uint8), (0, 0), self.clock.monotonic())

    def enumerate_windows(self):
        self.enumerations += 1
        return ()

    def read_label(self, crop):
        return self.label

    def keep_image(self, image, *, name):
        self.kept.append((name, None if image is None else image.shape))
        return hashlib.sha256(name.encode()).hexdigest()

    def send_input(self, action):
        self.sent.append(action)
        s = slow_half(action, self.safety)
        if s.refused:
            return EmitResult(refused=s)
        events = _events(action)
        directive = self.responses.pop(0) if self.responses else "allow"
        if directive == "never":
            return EmitResult(decisions=(), events=events)
        decisions = []
        for i, _ in enumerate(events):
            self.token += 1
            if directive.startswith("drop:") and i == 0:
                decisions.append(HookDecision(False, directive.split(":", 1)[1], self.token))
            else:
                decisions.append(HookDecision(True, token_id=self.token))
        if self.track_dialog and all(d.allowed for d in decisions):
            if action.order_stage_step:
                self.dialog_open = True
            elif action.teardown_action == "close_dialog":
                self.dialog_open = False
        return EmitResult(decisions=tuple(decisions), events=events)

    def probe_hook(self):
        return self.probe

    def reset_input_state(self):
        self.resets += 1
        return True

    def postcondition(self, step):
        if not self.effects:
            return Effect(None)
        return self.effects.pop(0) if len(self.effects) > 1 else self.effects[0]

    def popup(self, modal):
        self.popups.append(modal)
        return self.popup_result

    # run_unit
    def precondition(self, check, expect):
        self.reads.append(check)
        seq = self.checks.get(check)
        from sentinel.executor.flow import CheckRead
        if not seq:
            return CheckRead(True)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def reset_ui(self):
        self.ui_resets += 1
        return True

    def reset_profile(self):
        self.profile_resets += 1
        return True

    def reload_workspace(self):
        self.reloads += 1
        return True

    def teardown_action(self, step):
        from sentinel.executor.flow import CheckRead
        self.teardowns.append(step["id"])
        return self.teardown_reads.get(step["id"], CheckRead(True))

    def wait_poll(self, step):
        from sentinel.executor.flow import WaitPoll
        if not self.waits:
            return WaitPoll(True)
        return self.waits.pop(0) if len(self.waits) > 1 else self.waits[0]

    def capture(self, step, *, rescued):
        from sentinel.executor.flow import Captured
        self.captured.append((step["id"], rescued))
        return self.capture_results.get(step["checkpoint_id"], Captured("f" * 63 + str(len(self.captured) % 10)))

    def evaluate(self, step):
        from sentinel.executor.flow import AssertVerdict
        return self.verdicts.get(step["id"], AssertVerdict())


class FakeRescue:
    def __init__(self, result, available=True):
        self.result, self._available, self.calls = result, available, []

    def available(self):
        return self._available

    def rescue(self, step, target, frame):
        self.calls.append((step["id"], target["name"]))
        return self.result


def make(clock=None, **ctx_kw):
    clock = clock or ManualClock()
    ports = FakePorts(clock, safety=ctx_kw.pop("safety", None))
    ctx = ExecContext(run_id="pr-1-1", shard_id="pr-1-1/1", clock=clock, flow_id=FLOW,
                      flow={"class": "standard", "agent_fallback": "allowed"},
                      resolve_target=lambda t: {"name": t, "label": "Qty", "window": "main"}, **ctx_kw)
    return ports, FakeLocator(), FakePlanner(), ctx


CLICK = {"id": "s04", "type": "click_named", "target": "trade.order-entry.stock.qty-field"}


# -- the happy path and the records ---------------------------------------------------------------------------------
def test_a_clean_click_passes_with_the_rung_every_rung_tried_and_a_96px_crop():
    ports, loc, plan, ctx = make()
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed" and r.attempt == 1 and r.rung == "image"
    assert [x["rung"] for x in r.rung_results] == ["jab", "image"]
    assert r.led_to is None and r.rest is None and r.consequences == ()
    crop = next(shape for name, shape in ports.kept if name.endswith("-crop"))
    assert crop == (96, 96, 3) and r.click_crop is not None
    row = next(e for e in r.events if e.action == "click")
    assert (row.screen_x, row.screen_y, row.click_crop_sha256, row.locator_level) == (100, 100, r.click_crop, "image")
    assert row.gate_decision == "emitted" and row.step_outcome == "passed"
    assert loc.calls[0]["agent_fallback"] is False                    # rung 4 waits for the scripted retry
    so = r.to_step_outcome("pr-1-1", FLOW, shard_id="pr-1-1/1", side="candidate", mode="scripted")
    assert isinstance(so, StepOutcome) and so.locator_rung == "image" and so.click_crop == r.click_crop


def test_actions_carry_the_under_point_label_and_the_step_declarations():
    ports, loc, plan, ctx = make()
    ports.label = "Qty"
    emit({"id": "s05", "type": "click_named", "target": "t.x", "order_stage": False}, ports, loc, plan, ctx)
    a = ports.sent[-1]
    assert (a.under_point_text, a.resolved_name, a.flow_class) == ("Qty", "Qty", "standard")
    emit({"id": "s06", "type": "press", "key": "enter"}, ports, loc, plan, ctx)
    assert ports.sent[-1].declared_guarded                               # 5.4.7: the step declares Enter
    assert declares_guarded({"type": "type_fixture", "commit": "enter"}) and not declares_guarded({"type": "press",
                                                                                                    "key": "tab"})


def test_step_rows_carry_the_app_age():
    clock = ManualClock()
    life = AppLife()
    life.launched(clock.monotonic() - 125)
    life.relaunched(clock.monotonic() - 20)
    ports, loc, plan, ctx = make(clock, life=life)
    r = emit(CLICK, ports, loc, plan, ctx)
    assert (r.app_uptime_s, r.app_restart_count) == (20, 1)
    assert all((e.app_uptime_s, e.app_restart_count) == (20, 1) for e in r.events)


def test_every_emitted_event_is_a_drainer_row_with_the_click_crop(tmp_path):
    ports, loc, plan, ctx = make()
    ctx.audit = Drainer(DrainContext("pr-1-1", "pr-1-1/1", "candidate"), journal=tmp_path / "drops.jsonl",
                        next_seq=ctx.next_seq, wall_at=lambda ns: ctx.clock.now().isoformat())
    r = emit(CLICK, ports, loc, plan, ctx)
    rows = [e for e in r.events if e.action == "gateway_decision"]
    assert [e.keys.split(":")[0] for e in rows] == ["button_down", "button_up"]
    assert all(e.gate_decision == "emitted" and e.click_crop_sha256 == r.click_crop for e in rows)
    assert all(e.step_id == "s04" and e.flow_id == FLOW for e in rows)


def test_a_dropped_event_is_a_dropped_row_and_a_refusal_a_refused_row(tmp_path):
    ports, loc, plan, ctx = make()
    ctx.audit = Drainer(DrainContext("pr-1-1", "pr-1-1/1", "candidate"), journal=tmp_path / "drops.jsonl",
                        next_seq=ctx.next_seq, wall_at=lambda ns: ctx.clock.now().isoformat())
    ports.responses = ["drop:token_expired", "allow"]
    r = emit(CLICK, ports, loc, plan, ctx)
    decisions = [e.gate_decision for e in r.events if e.action == "gateway_decision"]
    assert decisions == ["dropped", "emitted", "emitted", "emitted"]
    ports2, loc2, plan2, ctx2 = make()
    ctx2.audit = Drainer(DrainContext("pr-1-1", "pr-1-1/1", "candidate"), journal=tmp_path / "d2.jsonl",
                         next_seq=ctx2.next_seq, wall_at=lambda ns: ctx2.clock.now().isoformat())
    ctx2.resolve_target = lambda t: {"name": t, "label": "Cancel All"}
    r2 = emit(CLICK, ports2, loc2, plan2, ctx2)
    refused = [e for e in r2.events if e.gate_decision == "refused" and e.action == "gateway_decision"]
    assert len(refused) == 1 and refused[0].gate_reason == "deny_profile"


# -- stage 1 and 2 --------------------------------------------------------------------------------------------------
def test_foreground_lost_after_one_set_focus_fails_the_step():
    ports, loc, plan, ctx = make()
    ports.fg = [Foreground(9, 777, False, "explorer.exe", "File Explorer")]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and r.reason == "foreground_lost"
    assert ports.focus == ["main"]
    assert [e.action for e in r.events] == ["focus_correction", "foreign_window"]
    assert r.led_to == step_failed("s04") and r.rest == step_failed("s04")
    assert ports.sent == []


def test_a_corrected_foreground_continues_and_is_logged():
    ports, loc, plan, ctx = make()
    ports.fg = [Foreground(None, None, False), Foreground(1, 100, True)]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed"
    assert [e.action for e in r.events][:1] == ["focus_correction"]
    assert "foreign_window" not in [e.action for e in r.events]           # no foreground window: not a foreign one


def test_an_unexpected_modal_fails_the_step_and_the_popup_sentinel_takes_over():
    ports, loc, plan, ctx = make()
    ports.modal = ModalState(False, "Market Closed", 55)
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and ports.popups == [ports.modal] and ports.sent == []
    assert [f.kind for c in r.consequences for f in c.findings] == ["unknown_popup"]
    assert r.led_to == step_failed("s04")
    ports.popup_result = ControlResult("popup_sentinel", False, "running",
                                       checkpoint=Outcome("env_error", "maintenance", "connectivity"))
    assert emit(CLICK, ports, loc, plan, ctx).led_to == Outcome("env_error", "maintenance", "connectivity")


def test_a_step_that_targets_the_modal_continues():
    ports, loc, plan, ctx = make()
    ports.modal = ModalState(False, "Order Confirmation", 55)
    ctx.resolve_target = lambda t: {"name": t, "label": "Delete", "window": "^Order Confirmation"}
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed" and ports.popups == []
    assert targets_dialog({"window": "^Order"}, "Order Confirmation") and not targets_dialog({"window": "main"}, "x")


# -- stage 3: the ladder --------------------------------------------------------------------------------------------
def test_a_scripted_miss_then_a_hit_is_passed_after_retry_with_the_first_screenshot_kept():
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS, OK]}
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed_after_retry" and r.attempt == 2 and r.rung == "image"
    assert ports.resets == 1 and ports.enumerations == 1
    assert [n for n, _ in ports.kept if n.endswith("-pre")] == ["s04-pre"]          # the first attempt's only
    assert [x["rung"] for x in r.rung_results] == ["image", "ocr", "jab", "image"]
    assert "reset_input_state" in [e.action for e in r.events]


def test_every_rung_failing_with_the_agent_unavailable_is_landmark_missing():
    ports, loc, plan, ctx = make(agent_available=False)
    loc.script = {CLICK["target"]: [MISS]}
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and r.rung == "none"
    assert r.led_to == Outcome("landmark_missing", "locator:s04") and r.rest == step_failed("s04")
    [f] = r.findings
    assert f.kind == "locator_failure" and f.description == DEGRADED_TEXT
    detail = json.loads(f.detail)
    assert detail["step_id"] == "s04" and detail["agent_rung"] == "agent_unavailable"
    assert len(detail["rung_results"]) == 4 and detail["pre_screenshot"] == r.pre_screenshot
    assert ports.sent == []


def test_an_agent_rescue_is_passed_via_agent_fallback_with_a_locator_failure_finding():
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS]}
    rescue = FakeRescue(Rescue(LocateResult("agent", point=(120, 80), rung_results=(
        {"rung": "agent", "result": "accepted"},)), trace=({"action": "click", "rationale": "Qty label"},)))
    ctx.rescue = rescue
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed_via_agent_fallback" and r.rescued and r.rung == "agent"
    assert rescue.calls == [("s04", CLICK["target"])] and ports.sent == []
    [f] = r.findings
    detail = json.loads(f.detail)
    assert f.kind == "locator_failure" and "scripted locator broke at step s04" in f.description
    assert detail["final_target"] == [120, 80] and detail["agent_trace"][0]["action"] == "click"
    assert ("s04-target", (20, 40, 3)) in ports.kept                    # the 40x20 crop at the final target


def test_a_failed_rescue_or_a_spent_cap_is_landmark_missing():
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS]}
    ctx.rescue = FakeRescue(Rescue(LocateResult("none"), reason="cap"))
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.led_to == Outcome("landmark_missing", "locator:s04") and json.loads(r.findings[0].detail)["agent_rung"] \
        == "cap"
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS]}
    ctx.rescue = FakeRescue(Rescue(LocateResult("agent", point=(1, 1))), available=False)
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and ctx.rescue.calls == []
    assert json.loads(r.findings[0].detail)["agent_rung"] == "rescue_cap"


def test_an_attempted_send_inside_a_rescue_stops_the_run():
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS]}
    from sentinel.orchestrator.results import safety_abort_outcome
    run, un = safety_abort_outcome("running", "attempted_send")
    ctx.rescue = FakeRescue(Rescue(LocateResult("none"), consequences=(
        ControlResult("gateway", False, "running", run=run, unreached=un),), reason="refusal"))
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and r.stops_run and r.led_to is None and r.rest is None


@pytest.mark.parametrize("flow, step, kw, why", [
    ({"agent_fallback": "disabled"}, CLICK, {}, "flow_disabled"),
    ({"class": "login"}, CLICK, {}, "login_flow"),
    ({}, {"id": "pw", "type": "secret"}, {}, "secret_step"),
    ({}, CLICK, {"teardown": True}, "teardown_step"),
    ({}, CLICK, {"dr07_branch_b": True}, "dr07_branch_b"),
    ({}, CLICK, {"agent_available": False}, "agent_unavailable"),
    ({}, CLICK, {"rescue_available": False}, "rescue_cap"),
    ({}, CLICK, {"flag": Flag.UNKNOWN}, "flag_set"),
    ({}, CLICK, {"target": {"agent_fallback": False}}, "target_disabled"),
    ({}, CLICK, {}, None),
])
def test_the_agent_rung_gate(flow, step, kw, why):
    args = dict(agent_available=True, dr07_branch_b=False, flag=Flag.CLEAR, teardown=False, rescue_available=True)
    target = kw.pop("target", {"name": "t"})
    args.update(kw)
    assert agent_rung_block(step, target, flow, **args) == why


def test_no_rescue_in_a_teardown_step():
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS]}
    ctx.rescue = FakeRescue(Rescue(LocateResult("agent", point=(1, 1))))
    r = emit(CLICK, ports, loc, plan, ctx, teardown=True)
    assert r.status == "failed" and ctx.rescue.calls == []


# -- stages 4 to 7 --------------------------------------------------------------------------------------------------
def test_a_scripted_non_send_refusal_is_blocked_and_ends_the_flow():
    ports, loc, plan, ctx = make()
    ctx.resolve_target = lambda t: {"name": t, "label": "Cancel All"}
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway"
    assert r.gateway_event == {"decision": "refused", "reason": "deny_profile", "class": "integrity"}
    assert r.led_to == step_failed("s04") and r.rest == step_failed("s04") and not r.stops_run
    assert r.as_control().has("end_flow")


def test_an_attempted_send_aborts_the_run_and_leaves_the_rest_to_the_unreached_outcome():
    ports, loc, plan, ctx = make()
    ctx.resolve_target = lambda t: {"name": t, "label": "Send"}
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and r.stops_run
    [c] = r.consequences
    assert (c.run_status, c.run_reason) == ("aborted_safety", "attempted_send") and c.has("kill_switch")
    assert r.led_to is None and r.rest is None
    assert all(o.selector not in ("rest_of_flow", "in_progress") for o in c.outcomes)


def test_a_dead_hook_host_is_blocked_with_hook_lost_and_takes_its_own_budget():
    budgets = ShardBudgets()
    ports, loc, plan, ctx = make(budgets=budgets, safety=SafetyContext(hook_alive=False))
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and r.gateway_event["reason"] == "hook_host_dead"
    assert r.led_to == Outcome("harness_error", "hook_lost") and budgets.used_hook_host == 1 and budgets.used_app == 0
    assert r.consequences[0].has("restart_hook_host") and r.consequences[0].checkpoint is None


def test_an_integrity_drop_with_the_hook_answering_is_retried_once():
    ports, loc, plan, ctx = make()
    ports.responses = ["drop:token_expired", "allow"]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "passed_after_retry" and r.attempt == 2
    assert [f.kind for f in r.findings] == ["gateway_integrity"]
    assert "probe" in [e.action for e in r.events] and len(ports.sent) == 2
    assert ctx.ledger.unit_counts[FLOW] == 1                           # counted after the retry (5.4.8 rule 3)


def test_the_retry_dropped_again_by_a_live_hook_is_blocked_with_hook_lost():
    budgets = ShardBudgets()
    ports, loc, plan, ctx = make(budgets=budgets)
    ports.responses = ["drop:token_expired", "drop:token_mismatch_coords"]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and r.gateway_event["reason"] == "token_mismatch_coords"
    assert r.led_to == Outcome("harness_error", "hook_lost") and r.rest == step_failed("s04")
    [c] = r.consequences
    assert c.has("kill_tree") and c.has("restart_hook_host") and c.has("relaunch") and c.run is None
    assert budgets.used_hook_host == 1


def test_a_retried_event_that_never_reaches_the_hook_is_failed():
    ports, loc, plan, ctx = make()
    ports.responses = ["never", "never"]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed"                                         # SD-52
    assert r.gateway_event == {"decision": "dropped", "reason": "token_expired", "class": "integrity"}
    assert r.led_to == Outcome("harness_error", "hook_lost")


def test_a_silent_hook_fails_the_step_closed_without_a_retry():
    ports, loc, plan, ctx = make()
    ports.responses = ["never"]
    ports.probe = False
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "failed" and r.attempt == 1 and len(ports.sent) == 1
    assert r.led_to == Outcome("harness_error", "hook_lost") and r.consequences[0].has("restart_hook_host")


def test_the_second_fail_closed_in_a_shard_ends_the_run_hook_lost():
    budgets = ShardBudgets()
    ports, loc, plan, ctx = make(budgets=budgets)
    ports.probe = False
    ports.responses = ["never"]
    emit(CLICK, ports, loc, plan, ctx)
    ports.responses = ["never"]
    r = emit(dict(CLICK, id="s05"), ports, loc, plan, ctx)
    assert r.stops_run and r.led_to is None
    assert (r.consequences[0].run_status, r.consequences[0].run_reason) == ("aborted_harness", "hook_lost")


def test_one_retry_per_step_whatever_used_it():
    """The scripted ladder's second attempt spends the retry: an integrity drop after it fails closed (4.7)."""
    ports, loc, plan, ctx = make()
    loc.script = {CLICK["target"]: [MISS, OK]}
    ports.responses = ["drop:token_expired"]
    r = emit(CLICK, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and r.led_to == Outcome("harness_error", "hook_lost")
    assert len(ports.sent) == 1


def test_a_safety_drop_by_the_hook_is_an_attempted_send():
    ports, loc, plan, ctx = make()
    ports.responses = ["drop:blocked_key_dialog"]
    r = emit({"id": "s06", "type": "press", "key": "tab"}, ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and r.stops_run
    assert r.consequences[0].run_reason == "attempted_send"


def test_the_order_stage_declaration_holds_once_the_click_got_past_stage_4():
    """executor:0 (5.4.6 source 3, 6.2, choice 9): set when an order-stage action is not refused, whatever the step's
    post-condition then says; a refused click leaves it as it was."""
    ports, loc, plan, ctx = make()
    ports.effects = [Effect(False)]
    r = emit(dict(CLICK, id="oc", order_stage=True, timeout_ms=500), ports, loc, plan, ctx)
    assert (r.status, r.reason) == ("failed", "postcondition") and ctx.order_stage is True
    safety = SafetyContext(dismiss_allowlist=frozenset({"Delete"}), attestation_passed=True)    # no paper assertion
    ports, loc, plan, ctx = make(safety=safety)
    ctx.resolve_target = lambda t: {"name": t, "label": "Confirm and Send", "window": "main"}
    r = emit(dict(CLICK, id="oc", order_stage=True), ports, loc, plan, ctx)
    assert r.status == "blocked_by_gateway" and ctx.order_stage is False


# -- stage 8 --------------------------------------------------------------------------------------------------------
def test_a_postcondition_that_never_holds_fails_the_step():
    ports, loc, plan, ctx = make()
    ports.effects = [Effect(False)]
    started = ctx.clock.monotonic()
    r = emit(dict(CLICK, timeout_ms=2000), ports, loc, plan, ctx)
    assert r.status == "failed" and r.reason == "postcondition"
    assert ctx.clock.monotonic() - started == pytest.approx(2.0)
    assert r.led_to == step_failed("s04")


def test_a_postcondition_that_is_an_assertion_names_it_on_the_next_checkpoint():
    ports, loc, plan, ctx = make()
    ports.effects = [Effect(False, assertion_id="sym_header")]
    r = emit({"id": "s02", "type": "type_fixture", "target": "t.sym", "commit": "enter", "timeout_ms": 500}, ports,
             loc, plan, ctx)
    assert r.led_to == Outcome("assertion_failed", "assertion:sym_header")


def test_retry_once_retries_a_failed_postcondition():
    ports, loc, plan, ctx = make()
    ports.effects = [Effect(False), Effect(False), Effect(False), Effect(True)]
    r = emit(dict(CLICK, on_fail="retry_once", timeout_ms=500), ports, loc, plan, ctx)
    assert r.status == "passed_after_retry" and r.attempt == 2 and len(ports.sent) == 2


class _BreakingWriter:
    """A heartbeat writer whose writes fail from the second on (10.2: the write and its one retry both fail)."""

    def __init__(self):
        self.n = 0

    def write(self, hb):
        self.n += 1
        if self.n >= 2:
            raise HeartbeatUnwritable(5, "EIO")


@pytest.mark.parametrize("effects", [[Effect(False)], [Effect(False), Effect(True)]])
def test_retry_once_never_retries_after_a_stop_during_the_postcondition_poll(effects):
    """executor:1 (10.2 "never a retry loop", choice 3): a heartbeat that cannot be written during the poll stops the
    run; the step is not re-run, no input follows, and it never ends passed_after_retry."""
    clock = ManualClock()
    ports, loc, plan, ctx = make(clock, heartbeat=HeartbeatPolicy(_BreakingWriter(), clock, run_id="r", shard=1,
                                                                  period_s=0.1))
    ctx.heartbeat.write()
    ports.effects = list(effects)
    r = emit(dict(CLICK, on_fail="retry_once", timeout_ms=1000), ports, loc, plan, ctx)
    assert (r.status, r.attempt, len(ports.sent)) == ("failed", 1, 1)
    assert r.stops_run and r.consequences[0].run_reason == "heartbeat_unwritable"
    assert r.led_to is None and r.rest is None


def test_a_final_reason_from_the_postcondition_read_ends_the_poll_at_once():
    ports, loc, plan, ctx = make()
    ports.effects = [Effect(False, reason="effect_impossible")]
    started = ctx.clock.monotonic()
    r = emit(dict(CLICK, timeout_ms=9000), ports, loc, plan, ctx)
    assert (r.status, r.reason) == ("failed", "effect_impossible") and ctx.clock.monotonic() == started


def test_on_fail_values_are_checked():
    ports, loc, plan, ctx = make()
    with pytest.raises(ValueError):
        emit(dict(CLICK, on_fail="ignore"), ports, loc, plan, ctx)


# -- heartbeat, secrets ---------------------------------------------------------------------------------------------
def test_screen_changing_input_sets_the_expectation_and_a_park_does_not(tmp_path):
    clock = ManualClock()
    hb = HeartbeatPolicy(None, clock, run_id="r", shard=1)
    ports, loc, plan, ctx = make(clock, heartbeat=hb)
    emit({"id": "n2", "type": "park_mouse"}, ports, loc, plan, ctx)
    assert hb.expect_mono is None
    emit(CLICK, ports, loc, plan, ctx)
    assert hb.expect_mono == clock.monotonic()
    assert hb.heartbeat().expect_screen_change_since == clock.now().isoformat()


def test_park_wheel_and_a_targetless_focus_neutral_run_with_the_real_planner():
    """executor:3 (4.7, 5.4.10, 3.5, choices 10 and 11): park_mouse and wheel get the parking coordinate, the dead area
    8 px inside the grab's bottom-right corner (FakePorts grabs 400 x 300 at the origin); a focus_neutral naming no
    neutral control sends nothing and leaves condition 2 to the settle rule."""
    park = (400 - 8, 300 - 8)
    ports, loc, _, ctx = make()
    r = emit({"id": "n2", "type": "park_mouse"}, ports, loc, Planner(), ctx)
    assert r.status == "passed" and ports.sent[-1].units[0].lead[0].x == park[0]
    assert (ports.sent[-1].units[0].lead[0].y, ports.sent[-1].point) == (park[1], park)
    r = emit({"id": "z1", "type": "wheel", "at": [120, 90], "notches": -2}, ports, loc, Planner(), ctx)
    a = ports.sent[-1]
    assert r.status == "passed" and a.point == (120, 90) and len(a.units) == 3
    assert [(e.x, e.y) for e in a.units[-1].lead] == [park]                           # parked after the notches
    sent = len(ports.sent)
    r = emit({"id": "n3", "type": "focus_neutral"}, ports, loc, Planner(), ctx)
    assert (r.status, r.detail, len(ports.sent), loc.calls[-1:]) == ("passed", NO_NEUTRAL, sent, [])
    r = emit({"id": "n4", "type": "focus_neutral", "target": "shell.status-bar.neutral"}, ports, loc, Planner(), ctx)
    assert r.status == "passed" and ports.sent[-1].kind == "click" and ports.sent[-1].point == POINT


def test_a_postcondition_wait_keeps_the_heartbeat_fresh():
    clock = ManualClock()
    hb = HeartbeatPolicy(None, clock, run_id="r", shard=1)
    hb.step_start(FLOW, "s04")
    ports, loc, plan, ctx = make(clock, heartbeat=hb)
    ports.effects = [Effect(False)]
    emit(dict(CLICK, timeout_ms=25000), ports, loc, plan, ctx)
    assert hb.writes >= 3 and clock.monotonic() - hb.last_write_mono <= 10.0


def test_a_secret_step_row_is_redacted_with_a_character_count():
    ports, loc, plan, ctx = make()
    r = emit({"id": "pw", "type": "secret", "target": "login.password-field", "vault_ref": "login.password",
              "commit": "enter"}, ports, loc, plan, ctx)
    row = next(e for e in r.events if e.action == "secret")
    assert row.target_text == "<redacted:login.password>" and row.keys is None and row.reason == "8 characters"


def test_click_crop_clips_and_refuses_points_outside_the_frame():
    frame = Frame(np.zeros((50, 60, 3), np.uint8), (10, 10), 0.0)
    assert click_crop(frame, (12, 12)).shape == (50, 50, 3)          # clipped at the frame's corner
    assert click_crop(frame, (5, 5)) is None
    assert click_crop(Frame(None, (0, 0), 0.0), (1, 1)) is None
    assert click_crop(frame, (40, 30), (40, 20)).shape == (20, 40, 3)


def test_as_control_is_a_valid_control_result():
    ports, loc, plan, ctx = make(agent_available=False)
    loc.script = {CLICK["target"]: [MISS]}
    c = emit(CLICK, ports, loc, plan, ctx).as_control()
    assert c.checkpoint == Outcome("landmark_missing", "locator:s04") and c.step == "failed"
    assert {o.selector for o in c.outcomes} == {"in_progress", "rest_of_flow"}
    assert isinstance(DropLedger(), DropLedger)
