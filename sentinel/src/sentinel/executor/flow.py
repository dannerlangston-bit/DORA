"""One unit of work through the Executor: a scripted flow or a census visit, from its preconditions to its teardown
(2.4 Surface census procedure, 2.6.1 Flow model, 4.7 Flow file, 5.2, 10.1).

`run_unit(unit, ports, locator, planner, ctx)` returns a `UnitResult`: every step's `StepResult`, the outcome every
scoped checkpoint the steps decided (`outcomes`), the checkpoints captured for the evaluation to classify
(`captured`, flagged `rescued` after an agent rescue, with any `failed_assertions` of later `assert` steps), the
consequences for the Orchestrator and the findings and step events. The sequence:

1. A census visit starts with `reset_input_state` (2.4 `visit()`); a flow's first step is its own (L-FLW-01). A census
   visit in a workspace that already had three consecutive `env_error` visits on this shard is not attempted: it
   takes that workspace's outcome (2.4 Preconditions, `WorkspaceStreak`).
2. Preconditions (`precondition` steps; a census visit's are `CENSUS_CHECKS`): every check is read
   (`ports.precondition`). A check that decides by itself stops at once: the paper-mode assertion's result (6.4) and a
   `working_orders` count above `expect`, which is `aborted_safety (reason: unexpected_working_order)` (4.7,
   `boundary.working_orders_precondition`). Any other failure gets one `reset_ui` and a re-check, then a flow gets one
   `reset_profile` (the workspace reload with the profile reset of 7.6) and a census visit one workspace reload, and a
   last re-check; a check still failing makes every checkpoint of the unit `env_error (gate: workspace_loaded, reason:
   precondition:<check>)` and the next unit re-checks (2.4, 2.6.1, 4.7). Three consecutive such census visits in one
   workspace mark the shard's remaining visits there `env_error` with the same gate and reason, and on-call is told
   once per workspace (a `workspace:<id>` subset outcome).
3. Steps in order. Input steps go through `emitter.emit` (a `secret` step with the heartbeat's `capture_suspended`
   held, 5.4.10); `reset_input_state` through the gateway; `wait_for` polls its anchor and anti-anchors until
   `timeout_ms`, and a timeout is step `failed` with the next checkpoint `not_settled` naming the condition still
   failing (`timeout` only when no frame was obtained, 4.7, 3.5); a `checkpoint` step is captured (`ports.capture`),
   after the flow's `normalize` steps when the step before it was rescued (5.5); an `assert` step's failure is step
   `failed` with the enclosing checkpoint's failing assertion ids in `failed_assertions` (4.7). A `failed` or
   `blocked_by_gateway` step ends the flow: the checkpoint it led to takes the step's `led_to`, every later checkpoint
   `not_run (reason: step_failed:<step>)` (2.6.1, 4.7, 5.2). Every step and checkpoint runs inside
   `failures.guard`, so an exception is the 10.1 harness row (`harness_error` with a listed reason or
   `exception:<class>` on the checkpoint in progress, the rest of the flow `not_run (reason: step_failed:<step>)`).
4. Teardown runs after a failed or blocked step too (2.6.1), with no agent rung (4.7); it does not run when the run
   stopped or the process tree was killed. A `close_dialog` or `clear_ticket` that passes with the flag clear lifts
   the sticky `order_stage` declaration (5.4.6 source 3).

`UnitHooks` lets the caller put the Orchestrator's own checks between steps: `before_step` (its `step_boundary`: the
trip-wires, the flow budget, the paper-mode timer) and `after_step` (the Watchdog's app failure: `in_progress` becomes
the checkpoint the flow was heading to, `rest_of_flow` the later ones). The heartbeat is written at every step's start
and end, around the preconditions, and every 10 s during waits (`ExecContext.heartbeat`).

Choices where the spec is ambiguous (reported):

1. After a failed precondition the check is re-read after `reset_ui`, and `reset_profile` (or the census reload) runs
   only when it still fails: 4.7 orders "one reset_ui, then one reset_profile" and 2.4 "reset_ui; reload the workspace
   once; re-check"; a check that the cheap reset fixed needs no profile reset. The resets carry no step events (8.3
   has no action for them); the `UnitResult.recovery` list names what ran.
2. The three-consecutive rule counts census visits only (2.4); flows re-check at the next flow (2.6.1). A visit in a
   marked workspace takes the outcome of the visit that marked it.
3. A checkpoint step's own result is a step row like any other: `passed` when captured, `failed` when its capture
   raised. A capture that returns an outcome (`not_settled`, `env_error (reason: occluded)`) does not end the flow.
4. An interrupting result between steps (`UnitHooks`) that ends the flow without naming outcomes for the remaining
   checkpoints gives them `not_run (reason: step_failed:<step>)` with the step it interrupted.
5. A checkpoint's max wait is its `max_settle_ms` when the step carries one, else `DEFAULT_MAX_WAIT_S` (3.5's
   default; the caller fills `max_settle_ms` for chart and scan checkpoints).
6. Base-side outcomes (2.4 and 2.6.1 base step failure, `not_run (reason: base_step_failed)` or `needs_baseline`)
   belong to the evaluation of both sides; `UnitResult.side` says which side ran.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ..orchestrator import alerts
from ..orchestrator.boundary import working_orders_precondition
from ..orchestrator.gateway import Flag
from ..orchestrator.ports import Locator, PrimitivePlanner
from ..orchestrator.results import ControlResult, FindingSpec, Outcome, SubsetOutcome, step_failed
from ..records import StepEvent, StepOutcome
from .emitter import EmitterPorts, ExecContext, StepResult, _run_level, emit
from .failures import FailureLedger, FaultInjector, guard

CENSUS_CHECKS = ("workspace_loaded", "single_main_frame", "no_modal", "fixture_symbol_linked", "paper_mode")   # 2.4
INPUT_STEPS = ("click_named", "dblclick", "rclick", "drag", "wheel", "press", "key_chord", "type_fixture", "secret",
               "park_mouse", "focus_neutral", "export", "copy_grid")
DEFAULT_MAX_WAIT_S = 15.0               # 3.5: a checkpoint's max wait
DEFAULT_WAIT_MS = 5000                  # 4.7 timeout_ms
STREAK_LIMIT = 3                        # 2.4: three consecutive env_error visits in one workspace
PRECONDITION_GATE = "workspace_loaded"
TREE_GONE = ("kill_tree", "kill_switch", "relaunch")


# -- observations ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckRead:
    """One precondition or composite teardown action as the rig read it. `control` is a result the check decided by
    itself (the paper-mode assertion of 6.4, a Cancel All proof); `value` is the raw read (a Working row count)."""

    passed: bool
    value: Any = None
    control: ControlResult | None = None
    detail: str | None = None


@dataclass(frozen=True)
class WaitPoll:
    """One poll of a `wait_for` (4.7, 3.5): satisfied, or the condition failing now; `frame` False when no frame
    could be obtained."""

    satisfied: bool
    failing: str | None = None
    frame: bool = True


@dataclass(frozen=True)
class Captured:
    """A checkpoint capture: its sha256 and, when the capture itself decided one (`not_settled`, `env_error (reason:
    occluded)`, the app failure), the outcome; None leaves the outcome to the evaluation."""

    sha256: str | None = None
    outcome: Outcome | None = None


@dataclass(frozen=True)
class AssertVerdict:
    """An `assert` step's evaluation by the Oracle (4.7): the ids of the assertions that failed."""

    failed: tuple[str, ...] = ()
    detail: str | None = None


class UnitPorts(EmitterPorts, Protocol):
    """What a unit needs beyond the emitter's ports."""

    def precondition(self, check: str, expect: Any) -> CheckRead:
        """Read one precondition (4.7 `check` enum)."""

    def reset_ui(self) -> bool:
        """`reset_ui`: Escape through the gateway, close non-main windows, re-enter the symbol, park (2.6.1)."""

    def reset_profile(self) -> bool:
        """The workspace reload with `reset_profile` (7.6)."""

    def reload_workspace(self) -> bool:
        """Reload the corpus workspace (2.4)."""

    def teardown_action(self, step: Mapping[str, Any]) -> CheckRead:
        """A teardown action with no target (`cancel_all`, `working_orders_check`, `close_non_main_windows`, ...)."""

    def wait_poll(self, step: Mapping[str, Any]) -> WaitPoll:
        """One poll of a `wait_for` step's anchor and anti-anchors."""

    def capture(self, step: Mapping[str, Any], *, rescued: bool) -> Captured:
        """Settle, capture, redact and store a checkpoint (3.5, 5.7)."""

    def evaluate(self, step: Mapping[str, Any]) -> AssertVerdict:
        """An `assert` step's assertions (5.10)."""


@dataclass(frozen=True)
class UnitHooks:
    """The Orchestrator's checks between steps: `before_step` (`Orchestrator.step_boundary`) and `after_step` (the
    Watchdog's app failure). Each returns a result, or None to go on."""

    before_step: Callable[[Mapping[str, Any]], ControlResult | None] | None = None
    after_step: Callable[[Mapping[str, Any]], ControlResult | None] | None = None


@dataclass(frozen=True)
class Unit:
    """A flow (`kind` flow, `doc` its 4.7 document) or a census visit (`census_unit`). `checkpoints` are the scoped
    checkpoint ids; a checkpoint step outside them is passed over without a capture."""

    kind: str
    unit_id: str
    doc: Mapping[str, Any]
    checkpoints: tuple[str, ...]
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("flow", "census"):
            raise ValueError(f"unit kind {self.kind!r}")


def census_unit(checkpoint_id: str, path: Sequence[Mapping[str, Any]], *, workspace_id: str,
                checks: Sequence[str] = CENSUS_CHECKS, agent_fallback: str = "allowed") -> Unit:
    """A census visit (2.4) as a unit: the surface's path steps, then its checkpoint. Path steps without an id get
    `path-<n>`; the checkpoint step's id is `visit`."""
    steps = [dict(s, id=s.get("id") or f"path-{i}") for i, s in enumerate(path, 1)]
    doc = {"flow_id": checkpoint_id, "class": "standard", "agent_fallback": agent_fallback,
           "preconditions": [{"id": f"pre-{c}", "type": "precondition", "check": c} for c in checks],
           "steps": steps + [{"id": "visit", "type": "checkpoint", "checkpoint_id": checkpoint_id}], "teardown": []}
    return Unit("census", checkpoint_id, doc, (checkpoint_id,), workspace_id)


# -- the census workspace rule --------------------------------------------------------------------------------------
class WorkspaceStreak:
    """Consecutive `env_error` census visits per workspace on one shard (2.4): at the third, the workspace's remaining
    visits take the same outcome without being attempted, and on-call is told once."""

    def __init__(self, limit: int = STREAK_LIMIT):
        self.limit = limit
        self.counts: dict[str, int] = {}
        self.marked: dict[str, Outcome] = {}

    def record(self, workspace_id: str, outcome: Outcome | None) -> ControlResult | None:
        """One visit's step-decided outcome (None: captured); the marking result at the limit, else None."""
        if outcome is None or outcome.outcome != "env_error":
            self.counts[workspace_id] = 0
            return None
        self.counts[workspace_id] = self.counts.get(workspace_id, 0) + 1
        if self.counts[workspace_id] < self.limit or workspace_id in self.marked:
            return None
        self.marked[workspace_id] = outcome
        return ControlResult("census_workspace", False, "running",
                             outcomes=(SubsetOutcome(f"workspace:{workspace_id}", outcome),), alert=alerts.env_error(),
                             detail=f"{self.limit} consecutive env_error visits in {workspace_id}: its remaining visits "
                                    "on this shard are env_error without being attempted",
                             evidence={"workspace_id": workspace_id})

    def outcome_for(self, workspace_id: str | None) -> Outcome | None:
        """The outcome a visit in a marked workspace takes; None when the workspace is not marked."""
        return self.marked.get(workspace_id) if workspace_id is not None else None


# -- the result -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UnitResult:
    """Everything one unit decided (module docstring). `undecided` are scoped checkpoints left to the run's unreached
    outcome because the run stopped (1.10)."""

    unit_id: str
    kind: str
    side: str
    steps: tuple[StepResult, ...] = ()
    teardown: tuple[StepResult, ...] = ()
    outcomes: Mapping[str, Outcome] = field(default_factory=dict)
    captured: tuple[str, ...] = ()
    captures: Mapping[str, Captured] = field(default_factory=dict)
    rescued: tuple[str, ...] = ()
    failed_assertions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    consequences: tuple[ControlResult, ...] = ()
    findings: tuple[FindingSpec, ...] = ()
    events: tuple[StepEvent, ...] = ()
    stopped: bool = False
    ended_by: str | None = None
    undecided: tuple[str, ...] = ()
    recovery: tuple[str, ...] = ()

    def step_outcomes(self, run_id: str, *, shard_id: str | None = None, mode: str = "scripted") -> list[StepOutcome]:
        """The 4.13 step outcome rows of the steps and the teardown, for `Store.write_steps`."""
        return [s.to_step_outcome(run_id, self.unit_id, shard_id=shard_id, side=self.side, mode=mode)
                for s in self.steps + self.teardown]


# -- the run of one unit --------------------------------------------------------------------------------------------
class _Unit:
    """The working state of one `run_unit` call."""

    def __init__(self, unit: Unit, ports: UnitPorts, locator: Locator, planner: PrimitivePlanner, ctx: ExecContext,
                 hooks: UnitHooks, failures: FailureLedger | None, faults: FaultInjector | None,
                 streak: WorkspaceStreak | None):
        self.unit, self.ports, self.locator, self.planner, self.ctx = unit, ports, locator, planner, ctx
        self.hooks, self.failures, self.faults, self.streak = hooks, failures, faults, streak
        self.doc = unit.doc
        self.flow_id = unit.unit_id
        self.scoped = list(unit.checkpoints)
        self.steps: list[StepResult] = []
        self.teardown: list[StepResult] = []
        self.outcomes: dict[str, Outcome] = {}
        self.captured: list[str] = []
        self.captures: dict[str, Captured] = {}
        self.rescued: list[str] = []
        self.failed_assertions: dict[str, tuple[str, ...]] = {}
        self.consequences: list[ControlResult] = []
        self.findings: list[FindingSpec] = []
        self.events: list[StepEvent] = []
        self.recovery: list[str] = []
        self.stopped = False
        self.tree_gone = False
        self.ended_by: str | None = None

    # -- bookkeeping ----------------------------------------------------------------------------------------------
    def note(self, r: ControlResult | None) -> bool:
        """Keep a run-level consequence; True when it stops the run (or the tree is gone)."""
        if r is None:
            return self.stopped
        self.consequences.append(r)
        self.tree_gone = self.tree_gone or any(r.has(a) for a in TREE_GONE)
        self.stopped = self.stopped or r.run is not None
        return self.stopped

    def heartbeat(self, call: Callable[[], ControlResult | None]) -> bool:
        return self.note(call()) if self.ctx.heartbeat is not None else self.stopped

    def undecided(self, after_index: int = -1) -> list[str]:
        """Scoped checkpoints without a step-decided outcome or a capture, in step order after `after_index`."""
        order = [str(s.get("checkpoint_id")) for i, s in enumerate(self.doc.get("steps") or ())
                 if s.get("type") == "checkpoint" and i > after_index]
        return [c for c in order if c in self.scoped and c not in self.outcomes and c not in self.captured]

    def decide(self, led_to: Outcome | None, rest: Outcome | None, after_index: int,
               current: str | None = None) -> None:
        """`led_to` for the checkpoint in progress (or the next one), `rest` for every later one."""
        pending = self.undecided(after_index)
        if current is not None and current in self.scoped and current not in self.outcomes:
            pending = [current] + [c for c in pending if c != current]
        if led_to is not None and pending:
            self.outcomes[pending[0]] = led_to
            pending = pending[1:]
        if rest is not None:
            for c in pending:
                self.outcomes[c] = rest

    def record(self, r: StepResult, *, teardown: bool = False) -> None:
        (self.teardown if teardown else self.steps).append(r)
        self.events.extend(r.events)
        self.findings.extend(r.findings)
        for c in r.consequences:
            self.note(c)

    def result(self) -> UnitResult:
        return UnitResult(
            unit_id=self.unit.unit_id, kind=self.unit.kind, side=self.ctx.side, steps=tuple(self.steps),
            teardown=tuple(self.teardown), outcomes=dict(self.outcomes), captured=tuple(self.captured),
            captures=dict(self.captures), rescued=tuple(self.rescued), failed_assertions=dict(self.failed_assertions),
            consequences=tuple(self.consequences), findings=tuple(self.findings), events=tuple(self.events),
            stopped=self.stopped, ended_by=self.ended_by,
            undecided=tuple(self.undecided()) if self.stopped else (), recovery=tuple(self.recovery))


def run_unit(unit: Unit, ports: UnitPorts, locator: Locator, planner: PrimitivePlanner, ctx: ExecContext, *,
             hooks: UnitHooks | None = None, failures: FailureLedger | None = None,
             faults: FaultInjector | None = None, streak: WorkspaceStreak | None = None) -> UnitResult:
    """Run one flow or census visit (module docstring). `failures` collects the run taint of 8.9 for teardown,
    `faults` injects simulated failures (sites `step` and `capture`), `streak` is the shard's census workspace rule."""
    u = _Unit(unit, ports, locator, planner, ctx, hooks or UnitHooks(), failures, faults, streak)
    ctx.flow_id, ctx.flow = u.flow_id, u.doc
    if unit.kind == "census":
        marked = streak.outcome_for(unit.workspace_id) if streak is not None else None
        if marked is not None:
            u.decide(marked, marked, -1)
            return u.result()
        ok = ports.reset_input_state()
        u.events.append(ctx.event("reset_input_state", None, reason=None if ok else "reset_input_state failed"))
    pre = _preconditions(u)
    if pre is not None:
        u.decide(pre, pre, -1)
    elif not u.stopped:
        _steps(u)
        if not u.stopped and not u.tree_gone:
            _teardown(u)
    if unit.kind == "census" and streak is not None and unit.workspace_id is not None and not u.stopped:
        u.note(streak.record(unit.workspace_id, u.outcomes.get(unit.unit_id)))
    return u.result()


def _preconditions(u: _Unit) -> Outcome | None:
    """Step 2 of the module docstring: None when every check holds (or a check stopped the run), else the
    `env_error` every checkpoint of the unit takes."""
    checks = [s for s in u.doc.get("preconditions") or () if s.get("type") == "precondition"]
    if not checks:
        return None
    first = str(checks[0].get("id"))
    if u.heartbeat(lambda: u.ctx.heartbeat.step_start(u.flow_id, first)):        # type: ignore[union-attr]
        return None
    escalation: list[tuple[str, Callable[[], bool]]] = [("reset_ui", u.ports.reset_ui)]
    escalation.append(("reset_profile", u.ports.reset_profile) if u.unit.kind == "flow"
                      else ("reload_workspace", u.ports.reload_workspace))
    failing = _check_all(u, checks)
    for name, action in escalation:
        if failing is None or u.stopped:
            break
        u.recovery.append(name if action() else f"{name} failed")
        failing = _check_all(u, checks)
    u.heartbeat(lambda: u.ctx.heartbeat.step_end())                                 # type: ignore[union-attr]
    if failing is None or u.stopped:
        return None
    return Outcome("env_error", f"precondition:{failing}", PRECONDITION_GATE)


def _check_all(u: _Unit, checks: Sequence[Mapping[str, Any]]) -> str | None:
    """Read every check: the first failing check's name, or None (also when a check stopped the run)."""
    for s in checks:
        name = str(s.get("check"))
        read = u.ports.precondition(name, s.get("expect"))
        if u.note(read.control):
            return None
        if name == "working_orders" and isinstance(read.value, int) and not isinstance(read.value, bool):
            if u.note(working_orders_precondition(read.value, expect=int(s.get("expect") or 0), flow_id=u.flow_id)):
                return None
        if not read.passed:
            return name
    return None


def _steps(u: _Unit) -> None:
    """Step 3 of the module docstring."""
    steps = list(u.doc.get("steps") or ())
    last_rescued = False
    for i, step in enumerate(steps):
        sid = str(step.get("id"))
        if _interrupt(u, u.hooks.before_step, step, i - 1, sid):
            return
        kind = step.get("type")
        if kind == "checkpoint":
            cid = str(step.get("checkpoint_id"))
            if cid not in u.scoped:
                continue
            if last_rescued and not _normalize(u, i):
                return
            r = _checkpoint(u, step, cid, rescued=last_rescued)
        else:
            r = _step(u, step, i)
        u.record(r)
        # 5.5: the checkpoint reached after a rescued step is captured after normalize and flagged rescued
        last_rescued = False if kind == "checkpoint" else last_rescued or r.rescued
        if u.stopped:
            u.ended_by = sid
            return
        if not r.passed:
            u.ended_by = sid
            current = str(step.get("checkpoint_id")) if kind == "checkpoint" else None
            u.decide(r.led_to, r.rest, i, current)
            return
        if step.get("order_stage") is True:
            u.ctx.order_stage = True
        if _interrupt(u, u.hooks.after_step, step, i, sid):
            return


def _interrupt(u: _Unit, hook: Callable[[Mapping[str, Any]], ControlResult | None] | None,
               step: Mapping[str, Any], after_index: int, sid: str) -> bool:
    """A result from the Orchestrator between steps: True when it ends the flow (choice 4)."""
    r = hook(step) if hook is not None else None
    if r is None:
        return False
    subsets = {o.selector: o.outcome for o in r.outcomes}
    if u.note(_run_level(r)):
        u.ended_by = sid
        return True
    if not (r.has("end_flow") or "in_progress" in subsets or "rest_of_flow" in subsets or r.step == "failed"):
        return False
    rest = subsets.get("rest_of_flow", step_failed(sid))
    u.decide(subsets.get("in_progress", rest), rest, after_index)
    u.ended_by = sid
    return True


def _guarded(u: _Unit, step: Mapping[str, Any], work: Callable[[], StepResult], *, site: str,
             checkpoint_id: str | None = None) -> StepResult:
    """Run a step through `failures.guard`: an exception becomes the 10.1 row as this step's result."""
    sid = str(step.get("id"))
    value, mapped = guard(work, step_id=sid, checkpoint_id=checkpoint_id, site=site, faults=u.faults,
                          ledger=u.failures, last_screenshot=u.ctx.last_capture_sha256)
    if mapped is None:
        assert value is not None
        return value
    stops = mapped.run is not None
    up, restarts = u.ctx.tags()
    return StepResult(step_id=sid, status="failed", consequences=(_run_level(mapped),),
                      led_to=None if stops else mapped.checkpoint, rest=None if stops else step_failed(sid),
                      app_uptime_s=up, app_restart_count=restarts, detail=mapped.detail)


def _step(u: _Unit, step: Mapping[str, Any], index: int, *, teardown: bool = False) -> StepResult:
    """One non-checkpoint step, with its heartbeat at start and end."""
    ctx, sid, kind = u.ctx, str(step.get("id")), str(step.get("type"))
    hb = ctx.heartbeat

    def work() -> StepResult:
        if kind in INPUT_STEPS or (teardown and kind == "teardown" and step.get("target")):
            return emit(step, u.ports, u.locator, u.planner, ctx, teardown=teardown)
        if kind == "reset_input_state":
            return _reset(u, step)
        if kind == "wait_for":
            return _wait_for(u, step)
        if kind == "assert":
            return _assert(u, step, index, teardown=teardown)
        if kind == "teardown":
            return _teardown_action(u, step)
        raise ValueError(f"step {sid}: type {kind!r} cannot run in this block (4.7)")

    if kind == "secret" and hb is not None:
        with hb.secret_step():
            stop = u.note(hb.step_start(u.flow_id, sid))
            r = _guarded(u, step, work, site="step") if not stop else _halted(sid)
    else:
        stop = u.heartbeat(lambda: hb.step_start(u.flow_id, sid))                 # type: ignore[union-attr]
        r = _guarded(u, step, work, site="step") if not stop else _halted(sid)
    u.heartbeat(lambda: hb.step_end())                                             # type: ignore[union-attr]
    return r


def _halted(sid: str) -> StepResult:
    """A step that did not run because the heartbeat write stopped the run."""
    return StepResult(step_id=sid, status="failed", detail="not run: the run stopped at the step's heartbeat")


def _reset(u: _Unit, step: Mapping[str, Any]) -> StepResult:
    sid = str(step.get("id"))
    ok = u.ports.reset_input_state()
    ev = u.ctx.event("reset_input_state", sid, step_outcome="passed" if ok else "failed")
    return StepResult(step_id=sid, status="passed" if ok else "failed", events=(ev,),
                      led_to=None if ok else step_failed(sid), rest=None if ok else step_failed(sid),
                      detail=None if ok else "reset_input_state did not complete")


def _wait_for(u: _Unit, step: Mapping[str, Any]) -> StepResult:
    """`wait_for` (4.7): poll until satisfied or `timeout_ms`; a timeout is `failed` with the next checkpoint
    `not_settled` naming the condition still failing, `timeout` only when no frame was obtained."""
    ctx, sid = u.ctx, str(step.get("id"))
    started = ctx.clock.monotonic()
    deadline = started + float(step.get("timeout_ms", DEFAULT_WAIT_MS)) / 1000.0
    consequences: list[ControlResult] = []
    while True:
        poll = u.ports.wait_poll(step)
        left = deadline - ctx.clock.monotonic()
        if poll.satisfied or left <= 0:
            break
        r = ctx.sleep(min(ctx.poll_s, left))
        if r is not None:
            consequences.append(r)
            if r.run is not None:
                break
    ms = int((ctx.clock.monotonic() - started) * 1000)
    status = "passed" if poll.satisfied else "failed"
    ev = ctx.event("wait", sid, step_outcome=status, duration_ms=ms,
                   reason=None if poll.satisfied else (poll.failing or "timeout"))
    up, restarts = ctx.tags()
    if poll.satisfied:
        if ctx.heartbeat is not None:
            ctx.heartbeat.screen_settled()
        return StepResult(step_id=sid, status=status, events=(ev,), consequences=tuple(consequences),
                          duration_ms=ms, app_uptime_s=up, app_restart_count=restarts)
    reason = (poll.failing or "anchor_missing") if poll.frame else "timeout"
    return StepResult(step_id=sid, status=status, events=(ev,), consequences=tuple(consequences),
                      led_to=Outcome("not_settled", reason), rest=step_failed(sid), duration_ms=ms,
                      app_uptime_s=up, app_restart_count=restarts, detail=f"wait_for timed out: {reason}")


def _assert(u: _Unit, step: Mapping[str, Any], index: int, *, teardown: bool) -> StepResult:
    """An `assert` step (4.7): a failure is step `failed`; the enclosing checkpoint's failing ids are kept."""
    sid = str(step.get("id"))
    verdict = u.ports.evaluate(step)
    if not verdict.failed:
        return StepResult(step_id=sid, status="passed")
    if not teardown:
        enclosing = next((c for c in reversed(u.captured)), None)
        if enclosing is not None:
            u.failed_assertions[enclosing] = u.failed_assertions.get(enclosing, ()) + tuple(verdict.failed)
    return StepResult(step_id=sid, status="failed", led_to=step_failed(sid), rest=step_failed(sid),
                      detail=verdict.detail or f"assertions failed: {', '.join(verdict.failed)}")


def _teardown_action(u: _Unit, step: Mapping[str, Any]) -> StepResult:
    """A teardown action with no target: the rig performs it; a check it decides by itself is a consequence."""
    sid = str(step.get("id"))
    read = u.ports.teardown_action(step)
    cons = (read.control,) if read.control is not None else ()
    return StepResult(step_id=sid, status="passed" if read.passed else "failed", consequences=cons,
                      detail=read.detail)


def _normalize(u: _Unit, index: int) -> bool:
    """The flow's `normalize` steps before a rescued capture (5.5, 4.7); False when one failed and ended the flow."""
    for n in u.doc.get("normalize") or ():
        r = _step(u, n, index - 1)
        u.record(r)
        if u.stopped or not r.passed:
            u.ended_by = str(n.get("id"))
            if not u.stopped:
                u.decide(r.led_to, r.rest or step_failed(str(n.get("id"))), index - 1)
            return False
    return True


def _checkpoint(u: _Unit, step: Mapping[str, Any], cid: str, *, rescued: bool) -> StepResult:
    """Capture one scoped checkpoint (3.5, 5.7) inside `guard`; the heartbeat carries its id and max wait and the
    accepted capture clears `expect_screen_change_since` (10.2)."""
    ctx, sid = u.ctx, str(step.get("id"))
    hb = ctx.heartbeat
    max_wait = float(step["max_settle_ms"]) / 1000.0 if step.get("max_settle_ms") else DEFAULT_MAX_WAIT_S
    if u.heartbeat(lambda: hb.step_start(u.flow_id, sid, checkpoint_id=cid, max_wait_s=max_wait)):  # type: ignore
        return _halted(sid)
    started = ctx.clock.monotonic()

    def work() -> StepResult:
        cap = u.ports.capture(step, rescued=rescued)
        u.captures[cid] = cap
        if cap.outcome is not None:
            u.outcomes[cid] = cap.outcome
        else:
            u.captured.append(cid)
            if rescued:
                u.rescued.append(cid)
        if cap.sha256 is not None:
            ctx.last_capture_sha256 = cap.sha256
            if hb is not None:
                hb.screen_settled(cap.sha256)
        ev = ctx.event("capture", sid, checkpoint_id=cid, post_hash=cap.sha256, step_outcome="passed")
        up, restarts = ctx.tags()
        return StepResult(step_id=sid, status="passed", events=(ev,), post_screenshot=cap.sha256,
                          duration_ms=int((ctx.clock.monotonic() - started) * 1000), app_uptime_s=up,
                          app_restart_count=restarts)

    r = _guarded(u, step, work, site="capture", checkpoint_id=cid)
    u.heartbeat(lambda: hb.step_end())                                             # type: ignore[union-attr]
    return r


def _teardown(u: _Unit) -> None:
    """Step 4 of the module docstring."""
    for i, step in enumerate(u.doc.get("teardown") or ()):
        r = _step(u, step, i, teardown=True)
        u.record(replace(r, led_to=None, rest=None), teardown=True)
        if u.stopped or u.tree_gone:
            return
        if r.passed and step.get("action") in ("close_dialog", "clear_ticket") and u.ports.flag() == Flag.CLEAR:
            u.ctx.order_stage = False
