"""One flow or census visit through the Executor (2.4, 2.6.1, 4.7, 5.2, 10.1, 10.2; executor/flow.py)."""
from __future__ import annotations

import errno
from dataclasses import replace

import pytest
from test_executor_emitter import MISS, FakeLocator, FakePlanner, FakePorts, FakeRescue
from test_executor_secret import SECRET, FakeCapture, FakeVault

from sentinel.executor.emitter import Effect, ExecContext, Rescue
from sentinel.executor.failures import FailureLedger, FaultInjector
from sentinel.executor.flow import (CENSUS_CHECKS, NO_DIALOG, AssertVerdict, Captured, CheckRead, ExportPorts,
                                    SecretPorts, Unit, UnitHooks, WaitPoll, WorkspaceStreak, census_unit, run_unit)
from sentinel.executor.heartbeat import AppLife, HeartbeatPolicy
from sentinel.executor.primitives import Planner
from sentinel.orchestrator.boundary import FlowTimer, flow_budget_cut
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gateway import VK_TAB, Flag, SafetyContext
from sentinel.orchestrator.ports import LocateResult
from sentinel.orchestrator.results import ControlResult, Outcome, safety_abort_outcome, step_failed
from sentinel.orchestrator.rig import CrashSignal
from sentinel.orchestrator.watchdog import HeartbeatUnwritable, HeartbeatWriter, app_failure
from sentinel.records import StepOutcome
from sentinel.store import ArtifactPersistError, StoreWriteFailed

FLOW = "flow.trade.order-ticket.stock.limit"
CP3 = f"flow/trade.order-entry.stock/{FLOW}/s03"
CP6 = f"flow/trade.order-entry.stock/{FLOW}/s06"
CP7 = f"flow/dialog.order-confirmation/{FLOW}/s07"
WS = "ws.tier-a.swing-trader"
QTY = "trade.order-entry.stock.qty-field"
DOC = {
    "flow_id": FLOW, "class": "standard", "agent_fallback": "allowed", "account_role": "empty",
    "normalize": [{"id": "n1", "type": "press", "key": "escape"}, {"id": "n2", "type": "park_mouse"}],
    "preconditions": [{"id": "p1", "type": "precondition", "check": "paper_mode"},
                      {"id": "p3", "type": "precondition", "check": "no_modal"},
                      {"id": "p4", "type": "precondition", "check": "working_orders", "expect": 0}],
    "steps": [
        {"id": "s01", "type": "reset_input_state"},
        {"id": "s02", "type": "type_fixture", "target": "trade.order-entry.stock.symbol-box", "commit": "enter"},
        {"id": "w-ticket", "type": "wait_for", "anchor": {"landmark": "lm.order-entry-header"}, "timeout_ms": 8000},
        {"id": "s03", "type": "checkpoint", "checkpoint_id": CP3},
        {"id": "s04", "type": "type_fixture", "target": QTY, "value": "100", "commit": "tab"},
        {"id": "s05", "type": "type_fixture", "target": "trade.order-entry.stock.price-field", "commit": "tab"},
        {"id": "s06", "type": "checkpoint", "checkpoint_id": CP6},
        {"id": "a1", "type": "assert", "assertions": [{"id": "qty", "kind": "equals"}]},
        {"id": "open-confirm", "type": "click_named", "target": "trade.order-entry.stock.confirm-and-send",
         "order_stage": True},
        {"id": "w-confirm", "type": "wait_for", "anchor": {"window_title_regex": "^Order Confirmation"},
         "timeout_ms": 6000},
        {"id": "s07", "type": "checkpoint", "checkpoint_id": CP7},
    ],
    "teardown": [{"id": "t1", "type": "teardown", "action": "close_dialog",
                  "target": "dialog.order-confirmation.title-close"},
                 {"id": "t2", "type": "teardown", "action": "clear_ticket"},
                 {"id": "t4", "type": "teardown", "action": "working_orders_check", "expect": 0},
                 {"id": "t5", "type": "reset_input_state"}],
}
LABELS = {"trade.order-entry.stock.confirm-and-send": "Confirm and Send", "dialog.order-confirmation.title-close": "X"}


def unit(checkpoints=(CP3, CP6, CP7), doc=DOC):
    return Unit("flow", FLOW, doc, tuple(checkpoints), WS)


def setup(clock=None, labels=None, **ctx_kw):
    clock = clock or ManualClock()
    ports = FakePorts(clock)
    ports.track_dialog = True
    # attestation passed and a current paper-mode assertion: the slow half lets Confirm and Send open the dialog
    ports.safety = SafetyContext(dismiss_allowlist=frozenset({"Delete"}), attestation_passed=True,
                                 paper_assertion_current=True)
    names = {**LABELS, **(labels or {})}
    ctx = ExecContext(run_id="pr-1-1", shard_id="pr-1-1/1", clock=clock,
                      resolve_target=lambda t: {"name": t, "label": names.get(t, "Field"),
                                                "window": "^Order" if t.startswith("dialog.") else "main"},
                      **ctx_kw)
    return ports, FakeLocator(), FakePlanner(), ctx


def run(ports, loc, plan, ctx, u=None, **kw):
    return run_unit(u or unit(), ports, loc, plan, ctx, **kw)


# -- the whole flow -------------------------------------------------------------------------------------------------
def test_a_clean_flow_captures_every_scoped_checkpoint_and_tears_down():
    ports, loc, plan, ctx = setup()
    r = run(ports, loc, plan, ctx)
    assert r.captured == (CP3, CP6, CP7) and r.outcomes == {} and not r.stopped and r.ended_by is None
    assert [s.status for s in r.steps] == ["passed"] * 11
    assert [s.step_id for s in r.teardown] == ["t1", "t2", "t4", "t5"] and all(s.passed for s in r.teardown)
    assert ports.teardowns == ["t2", "t4"] and ports.resets == 2
    assert ports.reads == ["paper_mode", "no_modal", "working_orders"]
    assert ctx.order_stage is False                                    # set by open-confirm, lifted by close_dialog
    assert any(a.order_stage_step for a in ports.sent) and any(a.teardown_action == "close_dialog" for a in ports.sent)
    rows = r.step_outcomes("pr-1-1", shard_id="pr-1-1/1")
    assert all(isinstance(x, StepOutcome) and x.flow_id == FLOW for x in rows) and len(rows) == 15
    assert [e.checkpoint_id for e in r.events if e.action == "capture"] == [CP3, CP6, CP7]


def test_an_unscoped_checkpoint_is_passed_over():
    ports, loc, plan, ctx = setup()
    r = run(ports, loc, plan, ctx, unit((CP6,)))
    assert r.captured == (CP6,) and [c for c, _ in ports.captured] == ["s06"]


# -- preconditions --------------------------------------------------------------------------------------------------
def test_a_failing_precondition_gets_one_reset_ui_one_reset_profile_then_env_error():
    ports, loc, plan, ctx = setup()
    ports.checks = {"no_modal": [CheckRead(False)]}
    r = run(ports, loc, plan, ctx)
    env = Outcome("env_error", "precondition:no_modal", "workspace_loaded")
    assert r.outcomes == {CP3: env, CP6: env, CP7: env}
    assert (ports.ui_resets, ports.profile_resets, ports.reloads) == (1, 1, 0)
    assert r.recovery == ("reset_ui", "reset_profile") and r.steps == () and r.teardown == ()


def test_a_precondition_the_reset_ui_fixed_needs_no_profile_reset():
    ports, loc, plan, ctx = setup()
    ports.checks = {"no_modal": [CheckRead(False), CheckRead(True)]}
    r = run(ports, loc, plan, ctx)
    assert (ports.ui_resets, ports.profile_resets) == (1, 0) and r.captured == (CP3, CP6, CP7)


def test_working_orders_above_expect_is_aborted_safety_at_once():
    ports, loc, plan, ctx = setup()
    ports.checks = {"working_orders": [CheckRead(False, value=1)]}
    r = run(ports, loc, plan, ctx)
    assert r.stopped and r.undecided == (CP3, CP6, CP7) and r.outcomes == {}
    assert (r.consequences[0].run_status, r.consequences[0].run_reason) == ("aborted_safety",
                                                                            "unexpected_working_order")
    assert ports.ui_resets == 0


def test_a_paper_mode_check_that_decides_by_itself_stops_the_run():
    ports, loc, plan, ctx = setup()
    run_, un = safety_abort_outcome("paper_mode_asserted", "paper_mode_mismatch")
    abort = ControlResult("paper_mode", False, "paper_mode_asserted", run=run_, unreached=un, actions=("kill_tree",))
    ports.checks = {"paper_mode": [CheckRead(False, control=abort)]}
    r = run(ports, loc, plan, ctx)
    assert r.stopped and r.consequences == (abort,) and ports.ui_resets == 0 and r.steps == ()


# -- step failures --------------------------------------------------------------------------------------------------
def test_a_locator_miss_on_every_rung_is_landmark_missing_then_step_failed():
    ports, loc, plan, ctx = setup(agent_available=False)
    loc.script = {QTY: [MISS]}
    r = run(ports, loc, plan, ctx)
    assert r.captured == (CP3,) and r.ended_by == "s04"
    assert r.outcomes == {CP6: Outcome("landmark_missing", "locator:s04"), CP7: step_failed("s04")}
    assert [f.kind for f in r.findings] == ["locator_failure"]
    assert [s.step_id for s in r.teardown] == ["t1", "t2", "t4", "t5"]              # teardown runs after a failure


def test_close_dialog_with_no_dialog_open_is_a_teardown_no_op():
    """Choice 7: the flow failed before its dialog opened; the close target is rightly absent, so t1 passes with no
    input and files no locator_failure (the ladder would miss on every rung)."""
    ports, loc, plan, ctx = setup(agent_available=False)
    loc.script = {QTY: [MISS], "dialog.order-confirmation.title-close": [MISS]}
    r = run(ports, loc, plan, ctx)
    assert r.ended_by == "s04" and not ports.dialog_open
    t1 = r.teardown[0]
    assert (t1.step_id, t1.status, t1.detail, t1.events) == ("t1", "passed", NO_DIALOG, ())
    assert all(s.passed for s in r.teardown) and [f.kind for f in r.findings] == ["locator_failure"]
    assert not any(a.teardown_action == "close_dialog" for a in ports.sent)
    ports, loc, plan, ctx = setup()                                                 # the dialog is open: it is closed
    r = run(ports, loc, plan, ctx)
    assert r.teardown[0].detail != NO_DIALOG and any(a.teardown_action == "close_dialog" for a in ports.sent)
    assert not ports.dialog_open


def test_an_agent_rescue_captures_the_next_checkpoint_after_normalize_flagged_rescued():
    ports, loc, plan, ctx = setup()
    loc.script = {QTY: [MISS]}
    ctx.rescue = FakeRescue(Rescue(LocateResult("agent", point=(120, 80))))
    r = run(ports, loc, plan, ctx)
    assert r.rescued == (CP6,) and ("s06", True) in ports.captured and ("s03", False) in ports.captured
    ids = [s.step_id for s in r.steps]
    assert ids.index("s04") < ids.index("s05") < ids.index("n1") < ids.index("n2") < ids.index("s06")
    assert next(s for s in r.steps if s.step_id == "s04").status == "passed_via_agent_fallback"
    assert r.captured == (CP3, CP6, CP7)


def test_a_rescued_checkpoint_is_captured_after_normalize_with_the_real_planner():
    """executor:3 (5.5, 4.7): with the real primitives.Planner the example's normalize block (Escape, a park_mouse
    and a focus_neutral naming no target) runs, so the checkpoint after the rescue is captured flagged rescued
    instead of becoming harness_error (exception:PlanError)."""
    ports, loc, _, ctx = setup()
    loc.script = {QTY: [MISS]}
    ctx.rescue = FakeRescue(Rescue(LocateResult("agent", point=(120, 80))))
    doc = dict(DOC, normalize=[{"id": "n1", "type": "press", "key": "escape"}, {"id": "n2", "type": "park_mouse"},
                               {"id": "n3", "type": "focus_neutral"}])
    doc["steps"] = [s for s in DOC["steps"] if s["id"] in ("s01", "s03", "s04", "s06")]
    r = run(ports, loc, Planner(), ctx, unit((CP3, CP6), doc))
    assert [(s.step_id, s.status) for s in r.steps] == [
        ("s01", "passed"), ("s03", "passed"), ("s04", "passed_via_agent_fallback"), ("n1", "passed"),
        ("n2", "passed"), ("n3", "passed"), ("s06", "passed")]
    assert r.captured == (CP3, CP6) and r.rescued == (CP6,) and r.outcomes == {} and ("s06", True) in ports.captured


GRID = "monitor.position-statement.grid"
GRID_TEXT = "Symbol\tQty\nAAPL\t100\n"


class CopiedClipboard:
    """The clipboard holds the grid's text once a Ctrl chord went through the gateway, until it is cleared."""

    def __init__(self, ports, text=GRID_TEXT):
        self.ports, self.text, self.log = ports, text, []

    def read_text(self):
        self.log.append("read")
        copied = any("ctrl" in a.mods for a in self.ports.sent[self.cleared_at:])
        return self.text if copied else None

    def clear(self):
        self.log.append("clear")
        self.cleared_at = len(self.ports.sent)

    cleared_at = 0


class NoFiles:
    def read_text(self, path):
        return None


def export_doc(step):
    return dict(DOC, steps=[{"id": "s01", "type": "reset_input_state"}, step,
                            {"id": "s03", "type": "checkpoint", "checkpoint_id": CP3}])


def test_an_export_step_runs_through_run_export_and_keeps_its_source():
    """executor:4 (4.7 export, 5.10, SD-32, choice 8): a copy_grid step with the real Planner clicks the grid, sends
    Ctrl+C, reads the clipboard and clears it; the step passes and the export reaches the Oracle's source."""
    ports, loc, _, ctx = setup()
    clip = CopiedClipboard(ports)
    step = {"id": "x1", "type": "copy_grid", "target": GRID, "headers": ["Symbol", "Qty"], "timeout_ms": 1000}
    r = run(ports, loc, Planner(), ctx, unit((CP3,), export_doc(step)), exports=ExportPorts(clip, NoFiles()))
    x1 = next(s for s in r.steps if s.step_id == "x1")
    assert (x1.status, x1.reason) == ("passed", None) and r.captured == (CP3,) and r.outcomes == {}
    assert r.exports["x1"].produced and r.exports["x1"].source().column("Qty") == ("100",)
    assert [e.step_id for e in x1.events if e.action in ("click", "key_chord")] == ["x1-focus", "x1-copy"]
    row = x1.events[-1]
    assert (row.action, row.step_id, row.step_outcome) == ("clipboard", "x1", "passed")
    assert clip.log[0] == "clear" and clip.log[-1] == "clear"


def test_an_export_that_produced_nothing_or_cannot_run_is_a_step_failure_not_a_harness_error():
    """executor:4 (SD-32): nothing in the clipboard within timeout_ms is export_timeout; a rig without export ports
    is export_unavailable with no input sent; either way the flow ends like any failed step (2.6.1)."""
    step = {"id": "x1", "type": "export", "target": GRID, "kind": "copy_all", "headers": ["Symbol", "Qty"],
            "timeout_ms": 1000}
    ports, loc, _, ctx = setup()
    r = run(ports, loc, Planner(), ctx, unit((CP3,), export_doc(step)),
            exports=ExportPorts(CopiedClipboard(ports, text=None), NoFiles()))
    x1 = next(s for s in r.steps if s.step_id == "x1")
    assert (x1.status, x1.reason, r.ended_by) == ("failed", "export_timeout", "x1")
    assert r.outcomes == {CP3: step_failed("x1")} and x1.events[-1].action == "clipboard"
    ports, loc, _, ctx = setup()
    r = run(ports, loc, Planner(), ctx, unit((CP3,), export_doc(step)))
    x1 = next(s for s in r.steps if s.step_id == "x1")
    assert (x1.status, x1.reason) == ("failed", "export_unavailable") and r.outcomes == {CP3: step_failed("x1")}
    assert not any(a.mods for a in ports.sent) and r.exports == {}


class BreaksAfter(HeartbeatWriter):
    """Writes `ok` heartbeats, then every write fails (10.2: the write and its retry)."""

    def __init__(self, path, clock, ok):
        super().__init__(path, clock)
        self.ok = ok

    def write(self, hb):
        if self.ok <= 0:
            raise HeartbeatUnwritable(errno.EROFS, "read-only")
        self.ok -= 1
        super().write(hb)


def test_a_run_stop_during_an_export_or_a_secret_wait_ends_the_step_at_once(tmp_path):
    """executor:4 (10.2, choices 8 and 9): the export's poll and the lease's retry wait write the heartbeat; when it
    cannot be written the step ends there, nothing more is read, leased or typed."""
    clock = ManualClock()
    # five writes: the preconditions, s01 and x1's start; the poll's ticks then fail
    hb = HeartbeatPolicy(BreaksAfter(tmp_path / "hb.json", clock, ok=5), clock, run_id="pr-1-1", shard=1,
                         period_s=0.1)
    ports, loc, _, ctx = setup(clock, heartbeat=hb)
    clip = CopiedClipboard(ports, text=None)
    step = {"id": "x1", "type": "copy_grid", "target": GRID, "headers": ["Symbol", "Qty"], "timeout_ms": 60000}
    r = run(ports, loc, Planner(), ctx, unit((CP3,), export_doc(step)), exports=ExportPorts(clip, NoFiles()))
    x1 = next(s for s in r.steps if s.step_id == "x1")
    assert r.stopped and x1.status == "failed" and r.exports == {} and r.undecided == (CP3,)
    assert r.consequences[-1].run_reason == "heartbeat_unwritable" and clock.monotonic() < 1000 + 60
    assert clip.log[-1] == "clear"                                       # the clipboard is cleared all the same
    log = []
    clock = ManualClock()
    # five writes: the preconditions, s01 and pw's start; the tick inside the lease's 30 s retry wait fails
    hb = HeartbeatPolicy(BreaksAfter(tmp_path / "hb2.json", clock, ok=5), clock, run_id="pr-1-1", shard=1)
    ports, loc, _, ctx = setup(clock, heartbeat=hb)
    login = dict(export_doc(PW), **{"class": "login"})
    r = run(ports, loc, Planner(), ctx, unit((CP3,), login),
            secrets=SecretPorts(FakeVault(log, fail=1), FakeCapture(log)))
    assert r.stopped and log == ["lease:login.password"] and r.secrets == {}
    assert r.consequences[-1].run_reason == "heartbeat_unwritable"
    assert not any(a.kind == "secret" for a in ports.sent)


PW = {"id": "pw", "type": "secret", "target": "login.password-field", "vault_ref": "login.password", "commit": "tab"}


def test_a_secret_step_is_leased_and_typed_through_run_secret_step_with_the_real_planner():
    """executor:4 (5.4.10, 6.9, choice 9): the value is leased at the last moment, planned with secret tokens and
    emitted while Capture is suspended; the rows show the redacted reference, the lease is kept for teardown."""
    log = []
    ports, loc, _, ctx = setup()
    login = dict(export_doc(PW), **{"class": "login"})
    r = run(ports, loc, Planner(), ctx, unit((CP3,), login), secrets=SecretPorts(FakeVault(log), FakeCapture(log)))
    pw = next(s for s in r.steps if s.step_id == "pw")
    assert pw.status == "passed" and r.captured == (CP3,)
    a = next(a for a in ports.sent if a.kind == "secret")
    assert a.secret_ref == "login.password" and a.chars() == len(SECRET) and a.keys == (VK_TAB,)
    assert log == ["lease:login.password", "secret", "suspend", "resume"]
    row = next(e for e in pw.events if e.action == "secret")
    assert row.target_text == "<redacted:login.password>" and SECRET not in repr(r.events)
    assert r.secrets["pw"].lease is not None and r.secrets["pw"].chars == len(SECRET)


def test_a_secret_step_without_a_vault_or_with_one_down_types_nothing():
    """executor:4 (6.9, 7.7, choice 9): no SecretPorts is a failed step; a lease that fails twice is the
    vault_unreachable run consequence; neither leaves a planner error or types a key."""
    ports, loc, _, ctx = setup()
    login = dict(export_doc(PW), **{"class": "login"})
    r = run(ports, loc, Planner(), ctx, unit((CP3,), login))
    assert next(s for s in r.steps if s.step_id == "pw").status == "failed"
    assert r.outcomes == {CP3: step_failed("pw")} and not any(a.kind == "secret" for a in ports.sent)
    log = []
    ports, loc, _, ctx = setup()
    r = run(ports, loc, Planner(), ctx, unit((CP3,), login),
            secrets=SecretPorts(FakeVault(log, fail=2), FakeCapture(log)))
    assert r.stopped and r.undecided == (CP3,) and not any(a.kind == "secret" for a in ports.sent)
    assert (r.consequences[-1].run_status, r.consequences[-1].unreached.reason) == ("complete_with_env_errors",
                                                                                    "vault_unreachable")
    assert next(e for e in r.events if e.step_id == "pw").target_text == "<redacted:login.password>"


def test_a_wait_for_timeout_is_not_settled_with_the_failing_condition():
    ports, loc, plan, ctx = setup()
    ports.waits = [WaitPoll(False, "anti_anchor_present")]
    started = ctx.clock.monotonic()
    r = run(ports, loc, plan, ctx)
    assert r.outcomes == {CP3: Outcome("not_settled", "anti_anchor_present"), CP6: step_failed("w-ticket"),
                          CP7: step_failed("w-ticket")}
    assert ctx.clock.monotonic() - started >= 8.0
    ports, loc, plan, ctx = setup()
    ports.waits = [WaitPoll(False, frame=False)]
    assert run(ports, loc, plan, ctx).outcomes[CP3] == Outcome("not_settled", "timeout")


def test_a_failed_assert_names_the_enclosing_checkpoint_and_ends_the_flow():
    ports, loc, plan, ctx = setup()
    ports.verdicts = {"a1": AssertVerdict(("qty",), "qty read 10, expected 100")}
    r = run(ports, loc, plan, ctx)
    assert r.captured == (CP3, CP6) and r.failed_assertions == {CP6: ("qty",)}
    assert r.outcomes == {CP7: step_failed("a1")} and r.ended_by == "a1"


def test_an_exception_at_capture_is_harness_error_and_the_flow_resumes_next():
    ports, loc, plan, ctx = setup()
    faults = FaultInjector().add(FaultInjector.exception("capture", KeyError, CP6))
    r = run(ports, loc, plan, ctx, faults=faults)
    assert r.outcomes == {CP6: Outcome("harness_error", "exception:KeyError"), CP7: step_failed("s06")}
    assert any(c.has("resume_next_flow") for c in r.consequences) and not r.stopped
    assert next(s for s in r.steps if s.step_id == "s06").status == "failed"
    assert [s.step_id for s in r.teardown] == ["t1", "t2", "t4", "t5"]


def test_an_exception_in_a_step_is_harness_error_on_the_checkpoint_it_led_to():
    ports, loc, plan, ctx = setup()
    faults = FaultInjector().add(FaultInjector.exception("step", RuntimeError, "s05"))
    r = run(ports, loc, plan, ctx, faults=faults)
    assert r.outcomes == {CP6: Outcome("harness_error", "exception:RuntimeError"), CP7: step_failed("s05")}


class RaisingCapture(FakePorts):
    def __init__(self, clock, exc):
        super().__init__(clock)
        self.exc = exc

    def capture(self, step, *, rescued):
        if step["id"] == "s06":
            raise self.exc
        return super().capture(step, rescued=rescued)


def test_an_artifact_persist_failure_at_capture_taints_the_run_for_teardown():
    clock = ManualClock()
    _, loc, plan, ctx = setup(clock)
    ports = RaisingCapture(clock, ArtifactPersistError("write and read-back failed twice", enospc=True))
    ledger = FailureLedger()
    r = run_unit(unit(), ports, loc, plan, ctx, failures=ledger)
    assert r.outcomes[CP6] == Outcome("harness_error", "artifact_persist_failed")
    assert any(c.has("free_space_check") for c in r.consequences)
    assert ledger.persist_failed == [CP6]
    end = ledger.at_teardown(None)
    assert (end.run_status, end.run_reason) == ("aborted_harness", "artifact_persist_failed")


def test_a_store_that_cannot_commit_stops_the_shard():
    clock = ManualClock()
    _, loc, plan, ctx = setup(clock)
    ports = RaisingCapture(clock, StoreWriteFailed("database is locked"))
    r = run_unit(unit(), ports, loc, plan, ctx)
    assert r.stopped and r.undecided == (CP6, CP7) and r.outcomes == {}
    assert r.consequences[-1].run_reason == "store_write_failed" and r.teardown == ()


def test_an_attempted_send_stops_the_run_without_flow_teardown():
    ports, loc, plan, ctx = setup(labels={"trade.order-entry.stock.confirm-and-send": "Send"})
    r = run(ports, loc, plan, ctx)
    assert r.stopped and r.ended_by == "open-confirm" and r.undecided == (CP7,)
    assert r.consequences[-1].run_reason == "attempted_send" and r.teardown == () and ports.teardowns == []


def test_a_non_send_refusal_ends_the_flow_and_teardown_runs():
    ports, loc, plan, ctx = setup(labels={"trade.order-entry.stock.confirm-and-send": "Cancel All"})
    r = run(ports, loc, plan, ctx)
    assert next(s for s in r.steps if s.step_id == "open-confirm").status == "blocked_by_gateway"
    assert r.outcomes == {CP7: step_failed("open-confirm")} and not r.stopped
    assert [s.step_id for s in r.teardown] == ["t1", "t2", "t4", "t5"]


def test_a_dead_hook_host_is_hook_lost_and_no_teardown_on_the_killed_tree():
    ports, loc, plan, ctx = setup(budgets=ShardBudgets())
    ports.responses = ["allow", "allow"]
    ports.safety = SafetyContext(hook_alive=False)
    r = run(ports, loc, plan, ctx)
    assert r.outcomes == {CP3: Outcome("harness_error", "hook_lost"), CP6: step_failed("s02"),
                          CP7: step_failed("s02")}
    assert r.teardown == () and not r.stopped


# -- the Orchestrator between steps ---------------------------------------------------------------------------------
def test_a_flow_past_its_budget_leaves_its_remaining_checkpoints_not_run_budget():
    ports, loc, plan, ctx = setup()
    cut = flow_budget_cut(FlowTimer(FLOW, 0.0, 300.0), 301.0)
    r = run(ports, loc, plan, ctx, hooks=UnitHooks(before_step=lambda s: cut if s["id"] == "s04" else None))
    assert r.outcomes == {CP6: Outcome("not_run", "budget"), CP7: Outcome("not_run", "budget")}
    assert r.captured == (CP3,) and r.ended_by == "s04" and len(r.teardown) == 4


def test_an_app_failure_after_a_step_is_app_alive_then_app_crash():
    ports, loc, plan, ctx = setup()
    hang = app_failure(CrashSignal("hang", "screenshot_staleness"), ShardBudgets(), phase="running")
    r = run(ports, loc, plan, ctx, hooks=UnitHooks(after_step=lambda s: hang if s["id"] == "s05" else None))
    assert r.outcomes == {CP6: Outcome("assertion_failed", "assertion:app_alive", detail="hang"),
                          CP7: Outcome("not_run", "app_crash")}
    assert r.teardown == ()                                            # the tree is killed and relaunched
    assert all(not any(o.selector in ("in_progress", "rest_of_flow") for o in c.outcomes) for c in r.consequences)


# -- heartbeat ------------------------------------------------------------------------------------------------------
class Recording(HeartbeatWriter):
    def __init__(self, path, clock):
        super().__init__(path, clock)
        self.written = []

    def write(self, hb):
        self.written.append(hb)


def test_every_step_writes_the_heartbeat_at_its_start_and_its_end(tmp_path):
    clock = ManualClock()
    w = Recording(tmp_path / "hb.json", clock)
    hb = HeartbeatPolicy(w, clock, run_id="pr-1-1", shard=1)
    ports, loc, plan, ctx = setup(clock, heartbeat=hb)
    r = run(ports, loc, plan, ctx)
    assert len(w.written) == 2 + 2 * (len(r.steps) + len(r.teardown))
    s03_end = [h for h in w.written if h.step_id == "s03"][-1]
    assert s03_end.checkpoint_id == CP3 and s03_end.max_wait_s == 15.0
    assert s03_end.expect_screen_change_since is None and s03_end.last_capture_sha256 is not None
    s04_end = [h for h in w.written if h.step_id == "s04"][-1]
    assert s04_end.expect_screen_change_since is not None                  # typed into the qty field


def test_a_secret_step_suspends_capture_only_while_it_types(tmp_path):
    clock = ManualClock()
    w = Recording(tmp_path / "hb.json", clock)
    hb = HeartbeatPolicy(w, clock, run_id="pr-1-1", shard=1)
    ports, loc, plan, ctx = setup(clock, heartbeat=hb)
    login = {"flow_id": "flow.login.primary", "class": "login", "agent_fallback": "disabled",
             "steps": [{"id": "s01", "type": "reset_input_state"},
                       {"id": "user", "type": "type_fixture", "target": "login.user", "commit": "tab"},
                       {"id": "pw", "type": "secret", "target": "login.password", "vault_ref": "login.password",
                        "commit": "enter"},
                       {"id": "s04", "type": "reset_input_state"}], "teardown": []}
    log = []
    r = run_unit(Unit("flow", "flow.login.primary", login, ()), ports, loc, plan, ctx,
                 secrets=SecretPorts(FakeVault(log), FakeCapture(log)))
    assert [s.status for s in r.steps] == ["passed"] * 4
    suspended = [(h.step_id, h.capture_suspended) for h in w.written]
    assert suspended == [("s01", False), ("s01", False), ("user", False), ("user", False), ("pw", True),
                         ("pw", False), ("s04", False), ("s04", False)]


def test_a_heartbeat_that_cannot_be_written_stops_the_unit(tmp_path):
    class Failing(HeartbeatWriter):
        def write(self, hb):
            raise HeartbeatUnwritable(errno.EROFS, "read-only")

    clock = ManualClock()
    hb = HeartbeatPolicy(Failing(tmp_path / "hb.json", clock), clock, run_id="pr-1-1", shard=1)
    ports, loc, plan, ctx = setup(clock, heartbeat=hb)
    r = run(ports, loc, plan, ctx)
    assert r.stopped and r.consequences[0].run_reason == "heartbeat_unwritable" and r.steps == ()


# -- census visits --------------------------------------------------------------------------------------------------
CENSUS = f"census/trade.order-entry.stock/{WS}"
PATH = [{"type": "click_named", "target": "shell.tab-bar.trade"}]


def test_a_census_visit_resets_input_checks_preconditions_and_captures():
    ports, loc, plan, ctx = setup()
    u = census_unit(CENSUS, PATH, workspace_id=WS)
    r = run_unit(u, ports, loc, plan, ctx)
    assert r.kind == "census" and r.captured == (CENSUS,) and ports.resets == 1
    assert tuple(ports.reads) == CENSUS_CHECKS and [s.step_id for s in r.steps] == ["path-1", "visit"]
    assert r.events[0].action == "reset_input_state"


def test_a_census_path_step_failing_is_landmark_missing_on_its_side():
    ports, loc, plan, ctx = setup(agent_available=False, side="base")
    loc.script = {"shell.tab-bar.trade": [MISS]}
    r = run_unit(census_unit(CENSUS, PATH, workspace_id=WS), ports, loc, plan, ctx)
    assert r.outcomes == {CENSUS: Outcome("landmark_missing", "locator:path-1")} and r.side == "base"


def test_a_census_precondition_failure_reloads_the_workspace_not_the_profile():
    ports, loc, plan, ctx = setup()
    ports.checks = {"fixture_symbol_linked": [CheckRead(False)]}
    r = run_unit(census_unit(CENSUS, PATH, workspace_id=WS), ports, loc, plan, ctx)
    assert r.outcomes == {CENSUS: Outcome("env_error", "precondition:fixture_symbol_linked", "workspace_loaded")}
    assert (ports.ui_resets, ports.reloads, ports.profile_resets) == (1, 1, 0)


def test_three_consecutive_env_error_visits_mark_the_workspace_once():
    ports, loc, plan, ctx = setup()
    ports.checks = {"no_modal": [CheckRead(False)]}
    streak = WorkspaceStreak()
    results = [run_unit(census_unit(f"census/s{i}.x/{WS}", PATH, workspace_id=WS), ports, loc, plan, ctx,
                        streak=streak) for i in range(1, 4)]
    env = Outcome("env_error", "precondition:no_modal", "workspace_loaded")
    assert [r.consequences for r in results[:2]] == [(), ()]
    [mark] = results[2].consequences
    assert mark.outcomes[0].selector == f"workspace:{WS}" and mark.outcomes[0].outcome == env
    assert mark.alert is not None and mark.alert.rule == "env_error"
    reads, resets = len(ports.reads), ports.resets
    fourth = run_unit(census_unit(f"census/s4.x/{WS}", PATH, workspace_id=WS), ports, loc, plan, ctx, streak=streak)
    assert fourth.outcomes == {f"census/s4.x/{WS}": env} and fourth.consequences == ()
    assert (len(ports.reads), ports.resets) == (reads, resets)         # not attempted
    ports.checks = {}
    other = run_unit(census_unit("census/s5.x/ws.tier-a.options-trader", PATH, workspace_id="ws.tier-a.options-trader"),
                     ports, loc, plan, ctx, streak=streak)
    assert other.captured == ("census/s5.x/ws.tier-a.options-trader",)


def test_a_clean_visit_breaks_the_streak():
    streak = WorkspaceStreak()
    env = Outcome("env_error", "precondition:no_modal", "workspace_loaded")
    assert streak.record(WS, env) is None and streak.record(WS, env) is None
    assert streak.record(WS, None) is None
    assert streak.record(WS, env) is None and streak.record(WS, env) is None
    assert streak.record(WS, env) is not None and streak.outcome_for(WS) == env
    assert streak.record(WS, env) is None                              # told once per workspace


def test_unit_kinds_are_checked():
    with pytest.raises(ValueError):
        Unit("sweep", "x", {}, ())


def test_a_capture_that_decides_its_outcome_does_not_end_the_flow():
    ports, loc, plan, ctx = setup()
    ports.capture_results = {CP3: Captured(None, Outcome("not_settled", "frames_unstable"))}
    r = run(ports, loc, plan, ctx)
    assert r.outcomes == {CP3: Outcome("not_settled", "frames_unstable")} and r.captured == (CP6, CP7)


def test_a_flag_left_up_keeps_the_order_stage_declaration():
    ports, loc, plan, ctx = setup()
    ports.flag_value = Flag.SET
    run(ports, loc, plan, ctx)
    assert ctx.order_stage is True


def test_an_order_stage_step_that_fails_after_its_click_holds_the_declaration_into_teardown():
    """executor:0 (5.4.6 source 3, 6.2, 5.4.8 rule 1): the open-confirm click went out and opened the dialog, then
    the step failed its post-condition. The declaration is held all the same, so the teardown's click on an order
    dialog label that is neither allowlisted nor send-like ends the run aborted_safety, not a flow quarantine."""
    ports, loc, plan, ctx = setup(labels={"dialog.order-confirmation.title-close": "Close"})

    def post(step):
        if step["id"] != "open-confirm":
            return Effect(None)
        ports.safety = replace(ports.safety, flag=Flag.SET, order_dialog_present=True)       # the dialog opened
        return Effect(False)                                            # but the declared effect never showed
    ports.postcondition = post
    r = run(ports, loc, plan, ctx)
    opened = next(s for s in r.steps if s.step_id == "open-confirm")
    assert (opened.status, opened.reason, r.ended_by) == ("failed", "postcondition", "open-confirm")
    assert ports.dialog_open and ctx.order_stage is True
    t1 = r.teardown[0]
    assert t1.status == "blocked_by_gateway" and t1.gateway_event["reason"] == "dialog_allowlist"
    assert r.stopped and (r.consequences[-1].run_status, r.consequences[-1].run_reason) == \
        ("aborted_safety", "unknown_dialog_in_order_stage")
    assert not any(f.kind == "unknown_popup" for f in r.findings)


def test_rows_after_a_relaunch_carry_the_restart_count_and_a_reset_uptime():
    clock = ManualClock()
    life = AppLife()
    life.launched(clock.monotonic(), pid=6120)
    ports, loc, plan, ctx = setup(clock, life=life)
    clock.advance(600)
    first = run(ports, loc, plan, ctx).step_outcomes("pr-1-1")
    assert {(x.app_restart_count, x.app_uptime_s >= 600) for x in first} == {(0, True)}
    life.relaunched(clock.monotonic(), pid=6200)                    # the app-crash relaunch (10.2)
    clock.advance(5)
    after = run(ports, loc, plan, ctx)
    assert {x.app_restart_count for x in after.step_outcomes("pr-1-1")} == {1}
    assert max(x.app_uptime_s for x in after.step_outcomes("pr-1-1")) < 60
    assert all(e.app_restart_count == 1 for e in after.events)
