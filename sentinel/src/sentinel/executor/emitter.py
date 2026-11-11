"""The emitter: stages 1 to 8 of 5.4.3 for one step, the step's single retry, the locator ladder's retry and agent
rung (5.5), and the step outcome rules (5.4, 4.7, 2.6.1; SD-52, SD-53).

`emit(step, ports, locator, planner, ctx)` runs one input-bearing flow step and returns a `StepResult`: the step
outcome with its reason, attempt, rung and every rung tried, the step events, the findings, what the checkpoint the
step led to gets (`led_to`) and what every later checkpoint of the flow gets (`rest`), and the consequences, the
`ControlResult`s the Orchestrator applies (`Orchestrator.apply`) for run-level effects. Per attempt:

1. Foreground: the foreground window must belong to the process tree; otherwise one `set_focus` on the target window
   and a re-check, else the step `failed` with reason `foreground_lost`. Each correction is a `focus_correction`
   event; a foreground window of another process is also a `foreign_window` event (7.8 hardening).
2. Modal: `IsWindowEnabled(main)` false means a modal owns the frame. A step whose target window is that dialog (its
   title regex) continues; otherwise the popup sentinel takes over (`ports.popup`, a `popup` event) and the step
   fails; the checkpoint it led to takes the sentinel's checkpoint outcome (an `env_error` for a catalog abort, 4.7)
   or `not_run (reason: step_failed:<step>)`.
3. Resolution: the scripted rungs of the locator ladder (`Locator.locate`) on a fresh grab, for every point the step
   names (`target`, a drag's `from` and `to`, a wheel's `at`; a literal `[x, y]` needs no ladder). All rungs failing
   runs `reset_input_state`, re-enumerates, re-grabs and tries once more (the step's single retry: success is
   `passed_after_retry`, the first attempt's screenshot kept); then rung 4, the agent rescue, when
   `agent_rung_block` allows it (5.5, 4.7): success is `passed_via_agent_fallback`, failure or a skipped rung is
   `failed` with the checkpoint `landmark_missing (reason: locator:<step>)`; both write a `locator_failure` finding.
   The 96x96 crop under every pointer action's point is kept (`ports.keep_image`) and OCR'd (`ports.read_label`),
   so the label under the pointer reaches the safety stage even when the resolver used an offset.
4. to 7. Safety, token write, emission and consumption: `PrimitivePlanner.plan` turns the step into gateway
   `Action`s, which `ports.send_input` (the rig's `send_input`: the slow half, the tokens, the hook liveness check,
   `SendInput` and the hook's decisions) runs one by one. A refusal or a drop goes through `DropLedger.record` (5.4.8):
   a scripted non-send refusal is `blocked_by_gateway` and ends the flow (SD-53), an attempted send aborts the run,
   `hook_host_dead` takes the shard's hook host budget. An integrity drop of the step's own token (a token the hook
   dropped `token_expired` or `token_mismatch_coords`, or one the hook never saw) runs the probe: a hook that answers
   gets the single retry (re-resolve, re-tokenize, re-emit; success is `passed_after_retry` with a
   `gateway_integrity` finding); a silent hook, or a retried event that again never reached it, is `failed` (SD-52),
   a hook that answers and drops the retry is `blocked_by_gateway` with its reason, and in each case the checkpoint
   the step led to is `harness_error (reason: hook_lost)` and the fail-closed sequence of 5.4.2 runs
   (`watchdog.hook_host_died`: kill the tree, restart the hook host out of its budget of one per shard, relaunch;
   the second time `aborted_harness (reason: hook_lost)`).
8. Post-condition: the step's declared effect (`ports.postcondition`) is polled within the step's `timeout_ms`
   (default 5000); a failure is `failed` with reason `postcondition` (`export_timeout` for an export or copy-grid step
   that produced nothing, 4.7), and with `on_fail: retry_once` the step's single retry runs first.

A step has one retry, whatever uses it (the scripted ladder's second attempt, the integrity retry, `retry_once`), and
it never crosses a capture: a checkpoint is its own step (4.7, 5.4.3). The emitter's own step events are one action row
per planned action (with the rung, the click point, the label and the 96x96 crop hash), the focus corrections, the
probe and the popup; every gateway decision (emitted, dropped, refused) is a `gateway_decision` row of the drop-log
drainer (`ctx.audit`, a `gateway.drainer.Drainer`), which the emitter feeds with a `TokenNote` per token and the
records of `segment.record_for`, as 5.4.8 and 6.2 have it.

Choices where the spec is ambiguous (reported):

1. Step reasons: 4.13 lists `step_outcome.reason` for export steps; 5.4.3 also names `foreground_lost` (stage 1) and
   `postcondition` (stage 8), which are stored the same way. Gateway codes stay in `gateway_event`.
2. The checkpoint a failed step led to is `landmark_missing (reason: locator:<step>)` for an exhausted ladder,
   `harness_error (reason: hook_lost)` for the hook cases, `assertion_failed (assertion: <id>)` when the post-condition
   is an assertion (`Effect.assertion_id`, a `type_fixture` `verify`), the popup sentinel's checkpoint outcome when it
   gives one, and otherwise `not_run (reason: step_failed:<step>)` like the rest of the flow (a lost foreground, a
   plain post-condition, a non-send refusal, 2.6.1).
3. A step whose run-level consequence stops the run (an attempted send, a second hook host death, a heartbeat that
   cannot be written) leaves `led_to` and `rest` empty: the run's unreached outcome covers those checkpoints (1.10).
4. Consequences carry run-level effects only: the `in_progress` and `rest_of_flow` subsets and the `checkpoint` of
   the ledger's, the Watchdog's and the sentinel's results are dropped, because `StepResult.led_to` and `rest` already
   say what the unit's checkpoints get; every other subset (a whole shard, `account_state`) is kept.
5. The agent rung runs only for a step with a single located point (a drag has two, and the 5.6 toolset has no drag
   for the agent profile); its success means the agent reached the target and the step's post-condition passed within
   the rescue cap (5.5), so stages 4 to 8 are not repeated. A rescue's own gateway results come back in
   `Rescue.consequences` (an attempted send inside a rescue aborts the run, 5.4.8 rule 1).
6. `on_fail: teardown` ends the flow as `fail` does: teardown runs after every failed step anyway (2.6.1), so the two
   differ in name only; `retry_once` adds the post-condition retry.
7. `focus_neutral` and every other emitted step set `expect_screen_change_since`; `park_mouse` (a move) does not.
8. A secret step's action row carries `<redacted:<vault_ref>>` and the character count in `reason` (6.9, 5.4.10).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Any, Protocol

from ..gateway.drainer import TokenNote
from ..gateway.segment import record_for
from ..orchestrator.budgets import ShardBudgets
from ..orchestrator.clock import Clock
from ..orchestrator.gateway import (Action, DropLedger, Flag, GatewayDrop, HookDecision, InputEvent, agent_flag_abort,
                                    drain, refusal)
from ..orchestrator.ports import Frame, LocateResult, Locator, PrimitivePlanner, WindowInfo
from ..orchestrator.results import ControlResult, FindingSpec, Outcome, SubsetOutcome, step_failed
from ..orchestrator.rig import EmitResult
from ..orchestrator.watchdog import hook_host_died
from ..records import RUNG_RESULT_KEYS, RUNGS, StepEvent, StepOutcome
from .heartbeat import AppLife, HeartbeatPolicy

DEFAULT_TIMEOUT_MS = 5000                 # 4.7 timeout_ms default
CROP_PX = 96                              # 5.4, 5.4.3 stage 3: the click crop
TARGET_CROP = (40, 20)                    # 5.5: the crop around the agent's final target
POLL_S = 0.25                             # the settle loop's frame interval (3.5)
PASSED = ("passed", "passed_after_retry", "passed_via_agent_fallback")
POINT_FIELDS = ("target", "from", "to", "at")
GUARDED_NAMES = ("enter", "return", "space")
ON_FAIL = ("fail", "retry_once", "teardown")
EVENT_ACTION = {"click": "click", "dblclick": "dblclick", "rclick": "rclick", "drag": "drag", "wheel": "wheel",
                "press": "key_chord", "key_chord": "key_chord", "type_fixture": "type", "secret": "secret"}
STEP_EVENT_ACTION = {"export": "export", "copy_grid": "clipboard"}
EXPORT_STEPS = ("export", "copy_grid")
LOCATOR_TEXT = "scripted locator broke at step {step}"
DEGRADED_TEXT = "scripted locator failed; agent fallback disabled"     # 10.7


# -- observations the emitter reads ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Foreground:
    """`GetForegroundWindow` and its owner (5.4.3 stage 1): `in_tree` when it belongs to the launched process tree."""

    hwnd: int | None
    pid: int | None
    in_tree: bool
    process_name: str | None = None
    title: str = ""


@dataclass(frozen=True)
class ModalState:
    """`IsWindowEnabled(main_hwnd)` and the modal owning the frame, when one does (5.4.3 stage 2)."""

    main_enabled: bool
    modal_title: str | None = None
    modal_hwnd: int | None = None


@dataclass(frozen=True)
class Effect:
    """A post-condition read (stage 8). `passed` None: the step declares no effect. `reason` is a final failure
    reason that replaces `postcondition` (an export step's `export_unavailable`); `assertion_id` names the failing
    assertion when the effect is one (a `type_fixture` `verify`)."""

    passed: bool | None
    reason: str | None = None
    assertion_id: str | None = None


@dataclass(frozen=True)
class Rescue:
    """What the agent rescue did (5.5 rung 4): `locate.rung` is `agent` when the agent reached the target and the
    post-condition passed within the cap, `none` when it failed (`reason`: cap, refusal, vocabulary, abort_flag)."""

    locate: LocateResult
    consequences: tuple[ControlResult, ...] = ()
    trace: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None


class AgentRescue(Protocol):
    """The agent rung (5.5, 5.6): an agent session with the dismiss-free toolset and the goal "click the control
    named X in window W", within the per-run rescue cap."""

    def available(self) -> bool:
        """The per-run rescue cap is not spent."""

    def rescue(self, step: Mapping[str, Any], target: Mapping[str, Any], frame: Frame) -> Rescue:
        """Run one rescue for the step's target."""


class GatewayAudit(Protocol):
    """The drop-log drainer as the emitter feeds it (`gateway.drainer.Drainer`, 5.4.8)."""

    def set_context(self, **changes: Any) -> None:
        """The step, mode, latest capture and secret flag in force."""

    def note_token(self, note: TokenNote) -> None:
        """A token the emitter wrote."""

    def drain(self, records: Any) -> list[StepEvent]:
        """One row per ring record."""

    def refused(self, decision: Any, *, mono_ns: int, keys: str | None = None, point: tuple[int, int] | None = None,
                target_text: str | None = None, click_crop_sha256: str | None = None, attempt: int = 1,
                rect: tuple[int, int] | None = None) -> StepEvent:
        """A stage 4 refusal's row."""


class EmitterPorts(Protocol):
    """What stages 1 to 8 need from the rig (5.4.3). The Windows track implements them over Win32, DWM, OCR, the
    gateway's shared segment and `SendInput`; the simulated rig fakes them."""

    def foreground(self) -> Foreground:
        """Stage 1: the foreground window."""

    def set_focus(self, window: str) -> None:
        """Stage 1: one pywinauto `set_focus()` on the target window (`main` or a dialog title regex)."""

    def modal_state(self) -> ModalState:
        """Stage 2: whether a modal owns the main frame."""

    def flag(self) -> Flag:
        """The Window Watcher's `flag_ticket` (5.4.6)."""

    def grab(self) -> Frame:
        """Stage 3: a fresh grab of the virtual screen."""

    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        """The re-enumeration before the scripted retry (5.5)."""

    def read_label(self, crop: Any) -> str | None:
        """Stage 3: OCR of the crop under the point."""

    def keep_image(self, image: Any, *, name: str) -> str | None:
        """Store a crop or a screenshot (redacted, 5.7) and return its sha256."""

    def send_input(self, action: Action) -> EmitResult:
        """Stages 4 to 7 for one action: slow half, tokens, liveness check, `SendInput`, the hook's decisions."""

    def probe_hook(self) -> bool:
        """The liveness probe of 5.4.4: True when the hook saw it within 100 ms."""

    def reset_input_state(self) -> bool:
        """`reset_input_state` through the gateway (5.4.10)."""

    def postcondition(self, step: Mapping[str, Any]) -> Effect:
        """Stage 8: one read of the step's declared effect."""

    def popup(self, modal: ModalState) -> ControlResult:
        """Stage 2: the popup sentinel handles the modal (7.8): dismiss, wait or abort per the catalog, else the
        agent with the dismiss-only toolset or `WM_CLOSE`, with an `unknown_popup` finding."""


# -- the context ----------------------------------------------------------------------------------------------------
@dataclass
class ExecContext:
    """One shard's executor state: identity, clock, the drop ledger and budgets, the model availability, the
    heartbeat and the app's age, the audit sink and the sticky `order_stage` declaration (5.4.6 source 3)."""

    run_id: str
    shard_id: str
    clock: Clock
    ledger: DropLedger = field(default_factory=DropLedger)
    budgets: ShardBudgets = field(default_factory=ShardBudgets)
    side: str = "candidate"
    mode: str = "scripted"
    agent_available: bool = True                # preflight N2 and N5 (10.7)
    dr07_branch_b: bool = False                 # phase0/branches.yaml says DR-07 is Branch B (4.7)
    rescue: AgentRescue | None = None
    heartbeat: HeartbeatPolicy | None = None
    life: AppLife | None = None
    audit: GatewayAudit | None = None
    order_stage: bool = False
    resolve_target: Callable[[str], Mapping[str, Any]] = field(default=lambda t: {"name": t})
    poll_s: float = POLL_S
    next_seq: Callable[[], int] = field(default_factory=lambda: count(1).__next__)
    flow_id: str | None = None
    flow: Mapping[str, Any] = field(default_factory=dict)
    last_capture_sha256: str | None = None

    def tags(self) -> tuple[int | None, int | None]:
        """`(app_uptime_s, app_restart_count)` now, or `(None, None)` without an `AppLife` (10.2)."""
        if self.life is None:
            return None, None
        return self.life.tags(self.clock.monotonic())

    def sleep(self, seconds: float) -> ControlResult | None:
        """Wait, writing the heartbeat at least every 10 s (10.2)."""
        if self.heartbeat is not None:
            return self.heartbeat.sleep(seconds)
        self.clock.sleep(seconds)
        return None

    def event(self, action: str, step_id: str | None, *, attempt: int = 1, **fields: Any) -> StepEvent:
        """One `step_events` row the Orchestrator writes for this step (8.3)."""
        up, restarts = self.tags()
        mono = self.clock.monotonic()
        return StepEvent(run_id=self.run_id, shard_id=self.shard_id, side=self.side, seq=self.next_seq(),
                         source="orchestrator", wall_at=self.clock.now().isoformat(), mono_ms=int(mono * 1000),
                         mode=self.mode, action=action, flow_id=self.flow_id, step_id=step_id, attempt=attempt,
                         pre_hash=self.last_capture_sha256, app_uptime_s=up, app_restart_count=restarts, **fields)


# -- the result -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class StepResult:
    """One step's outcome and everything it decided (module docstring)."""

    step_id: str
    status: str
    attempt: int = 1
    reason: str | None = None
    rung: str | None = None
    rung_results: tuple[Mapping[str, Any], ...] = ()
    gateway_event: Mapping[str, Any] | None = None
    findings: tuple[FindingSpec, ...] = ()
    events: tuple[StepEvent, ...] = ()
    led_to: Outcome | None = None
    rest: Outcome | None = None
    consequences: tuple[ControlResult, ...] = ()
    pre_screenshot: str | None = None
    post_screenshot: str | None = None
    click_crop: str | None = None
    duration_ms: int = 0
    app_uptime_s: int | None = None
    app_restart_count: int | None = None
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in PASSED

    @property
    def rescued(self) -> bool:
        """The next checkpoint is captured after `normalize` and flagged rescued (5.5, 4.7)."""
        return self.status == "passed_via_agent_fallback"

    @property
    def stops_run(self) -> bool:
        """A consequence set a run outcome: the shard stops (`Orchestrator.apply`)."""
        return any(r.run is not None for r in self.consequences)

    def to_step_outcome(self, run_id: str, flow_id: str, *, shard_id: str | None = None, side: str | None = None,
                        mode: str | None = None, finding_ref: str | None = None) -> StepOutcome:
        """The 4.13 step outcome record."""
        return StepOutcome(run_id=run_id, flow_id=flow_id, step_id=self.step_id, status=self.status,
                           attempt=self.attempt, locator_rung=self.rung, duration_ms=self.duration_ms,
                           finding_ref=finding_ref, side=side, shard_id=shard_id, pre_screenshot=self.pre_screenshot,
                           gateway_event=dict(self.gateway_event) if self.gateway_event else None, reason=self.reason,
                           rung_results=[dict(r) for r in self.rung_results] or None, mode=mode,
                           app_uptime_s=self.app_uptime_s, app_restart_count=self.app_restart_count,
                           post_screenshot=self.post_screenshot, click_crop=self.click_crop)

    def as_control(self) -> ControlResult:
        """The step as one `ControlResult`: the checkpoint it led to and the rest of the flow (for a caller that
        applies subset outcomes, `results.SubsetOutcome` selectors `in_progress` and `rest_of_flow`)."""
        subsets = tuple(SubsetOutcome(sel, o) for sel, o in (("in_progress", self.led_to), ("rest_of_flow", self.rest))
                        if o is not None)
        return ControlResult("step", self.passed, "running", checkpoint=self.led_to, outcomes=subsets,
                             findings=self.findings, step=self.status, step_reason=self.reason,
                             actions=() if self.passed or self.stops_run else ("end_flow",), detail=self.detail)


# -- pure rules -----------------------------------------------------------------------------------------------------
def agent_rung_block(step: Mapping[str, Any], target: Mapping[str, Any] | None, flow: Mapping[str, Any], *,
                     agent_available: bool, dr07_branch_b: bool, flag: Flag, teardown: bool,
                     rescue_available: bool) -> str | None:
    """None when rung 4 may run for this step; else why it is skipped, `agent_failed(disabled)` (5.5, 4.7): the
    endpoint unavailable at preflight, DR-07 Branch B, the flow or the target forbidding it, a `login` flow, a secret
    step, a teardown step, the per-run rescue cap spent, or `flag_ticket` set or unknown."""
    if not agent_available:
        return "agent_unavailable"
    if dr07_branch_b:
        return "dr07_branch_b"
    if flow.get("agent_fallback") == "disabled":
        return "flow_disabled"
    if flow.get("class") == "login":
        return "login_flow"
    if step.get("type") == "secret":
        return "secret_step"
    if teardown:
        return "teardown_step"
    if step.get("agent_fallback") is False or (target is not None and target.get("agent_fallback") is False):
        return "target_disabled"
    if not rescue_available:
        return "rescue_cap"
    if flag != Flag.CLEAR:
        return "flag_set"
    return None


def click_crop(frame: Frame, point: tuple[int, int], size: int | tuple[int, int] = CROP_PX) -> Any:
    """The crop of `size` (one side, or width and height) centred on `point` and clipped to the frame (an edge point
    gives a smaller crop); None when the frame has no image or the point is outside it."""
    img = frame.image
    if img is None:
        return None
    w, h = (size, size) if isinstance(size, int) else size
    x, y = point[0] - frame.origin[0], point[1] - frame.origin[1]
    height, width = img.shape[0], img.shape[1]
    if not (0 <= x < width and 0 <= y < height):
        return None
    left, top = x - w // 2, y - h // 2
    return img[max(0, top):min(height, top + h), max(0, left):min(width, left + w)].copy()


def declares_guarded(step: Mapping[str, Any]) -> bool:
    """The step requests Enter or Space explicitly: `press: enter` or `commit: enter` (5.4.7)."""
    key = step.get("key") if step.get("type") == "press" else step.get("commit")
    return str(key or "").casefold() in GUARDED_NAMES


def targets_dialog(target: Mapping[str, Any] | None, modal_title: str | None) -> bool:
    """The step targets the modal by title regex (5.4.3 stage 2)."""
    window = (target or {}).get("window")
    return bool(window and window != "main" and modal_title is not None and re.search(str(window), modal_title))


def _point(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
        return int(value[0]), int(value[1])
    return None


def _rungs(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """4.13 rung results: `{rung, result, score?}` for the four rungs only."""
    out = []
    for r in results:
        if r.get("rung") in RUNGS and "result" in r:
            out.append({k: r[k] for k in RUNG_RESULT_KEYS if k in r and (k != "score" or r[k] is not None)})
    return out


def _run_level(r: ControlResult) -> ControlResult:
    """Choice 4: a consequence without the step-level outcomes the `StepResult` carries."""
    keep = tuple(o for o in r.outcomes if o.selector not in ("in_progress", "rest_of_flow"))
    return replace(r, checkpoint=None, outcomes=keep, step=None, step_reason=None)


def _keys(a: Action) -> str | None:
    if not a.keys:
        return None
    mods = "+".join(sorted(a.mods))
    return (mods + "+" if mods else "") + ",".join(f"0x{k:02X}" for k in a.keys)


# -- the emitter ----------------------------------------------------------------------------------------------------
class _Step:
    """The working state of one `emit` call."""

    def __init__(self, step: Mapping[str, Any], ports: EmitterPorts, locator: Locator, planner: PrimitivePlanner,
                 ctx: ExecContext, teardown: bool):
        self.step, self.ports, self.locator, self.planner, self.ctx = step, ports, locator, planner, ctx
        self.teardown = teardown
        self.step_id = str(step.get("id"))
        self.kind = str(step.get("type"))
        self.started = ctx.clock.monotonic()
        self.attempt = 1
        self.retry_used = False
        self.events: list[StepEvent] = []
        self.findings: list[FindingSpec] = []
        self.consequences: list[ControlResult] = []
        self.rung_results: list[dict[str, Any]] = []
        self.rung: str | None = None
        self.pre_screenshot: str | None = None
        self.click_crop: str | None = None
        self.targets = {f: ctx.resolve_target(str(step[f])) for f in POINT_FIELDS
                        if f in step and _point(step[f]) is None}

    @property
    def target(self) -> Mapping[str, Any] | None:
        return self.targets.get("target") or next(iter(self.targets.values()), None)

    def done(self, status: str, *, reason: str | None = None, led_to: Outcome | None = None,
             rest: bool = False, gateway_event: Mapping[str, Any] | None = None,
             detail: str | None = None) -> StepResult:
        stops = any(r.run is not None for r in self.consequences)
        up, restarts = self.ctx.tags()
        events = tuple(replace(e, step_outcome=status) if e.source == "orchestrator" else e for e in self.events)
        return StepResult(
            step_id=self.step_id, status=status, attempt=self.attempt, reason=reason, rung=self.rung,
            rung_results=tuple(self.rung_results), gateway_event=gateway_event, findings=tuple(self.findings),
            events=events, led_to=None if stops or status in PASSED else led_to,
            rest=step_failed(self.step_id) if rest and not stops and status not in PASSED else None,
            consequences=tuple(_run_level(r) for r in self.consequences), pre_screenshot=self.pre_screenshot,
            click_crop=self.click_crop,
            duration_ms=max(0, int((self.ctx.clock.monotonic() - self.started) * 1000)), app_uptime_s=up,
            app_restart_count=restarts, detail=detail)

    def failed(self, led_to: Outcome | None = None, **kw: Any) -> StepResult:
        return self.done(kw.pop("status", "failed"), led_to=led_to or step_failed(self.step_id), rest=True, **kw)

    def note(self, r: ControlResult | None) -> bool:
        """Keep a consequence; True when it stops the run."""
        if r is None:
            return False
        self.consequences.append(r)
        return r.run is not None


def emit(step: Mapping[str, Any], ports: EmitterPorts, locator: Locator, planner: PrimitivePlanner,
         ctx: ExecContext, *, teardown: bool = False) -> StepResult:
    """Run one input-bearing step through stages 1 to 8 (module docstring). `teardown` marks a teardown step (no
    agent rung, 4.7); `ctx.flow` is the flow document (class, `agent_fallback`) and `ctx.flow_id` the unit."""
    on_fail = str(step.get("on_fail", "fail"))
    if on_fail not in ON_FAIL:
        raise ValueError(f"on_fail {on_fail!r}; one of {ON_FAIL} (4.7)")
    s = _Step(step, ports, locator, planner, ctx, teardown)
    if ctx.audit is not None:
        ctx.audit.set_context(mode=ctx.mode, flow_id=ctx.flow_id, step_id=s.step_id,
                              secret_step=s.kind == "secret", pre_hash=ctx.last_capture_sha256)
    while True:
        out = _attempt(s, on_fail)
        if out is not None:
            return out
        s.attempt += 1


def _attempt(s: _Step, on_fail: str) -> StepResult | None:
    """One attempt; None when the step's single retry runs next."""
    ctx, ports = s.ctx, s.ports
    if ctx.audit is not None:
        ctx.audit.set_context(step_id=s.step_id)
    # 1. foreground
    window = str((s.target or {}).get("window") or "main")
    fg = ports.foreground()
    if not fg.in_tree:
        s.events.append(ctx.event("focus_correction", s.step_id, attempt=s.attempt, target_text=fg.title or None,
                                  reason=f"foreground {fg.process_name or fg.pid}"))
        if fg.pid is not None:
            s.events.append(ctx.event("foreign_window", s.step_id, attempt=s.attempt, popup_title=fg.title or None,
                                      reason=fg.process_name))
        ports.set_focus(window)
        if not ports.foreground().in_tree:
            return s.failed(reason="foreground_lost", detail="foreground not in the process tree after one set_focus")
    # 2. modal
    modal = ports.modal_state()
    if not modal.main_enabled and not targets_dialog(s.target, modal.modal_title):
        s.events.append(ctx.event("popup", s.step_id, attempt=s.attempt, popup_title=modal.modal_title))
        r = ports.popup(modal)
        if s.note(r):
            return s.failed(detail="a modal owns the main frame; the popup sentinel stopped the run")
        return s.failed(led_to=r.checkpoint, detail=f"a modal owns the main frame: {modal.modal_title!r}")
    # 3. resolution
    frame = ports.grab()
    if s.attempt == 1:
        s.pre_screenshot = ports.keep_image(frame.image, name=f"{s.step_id}-pre")
    points = _resolve(s, frame)
    if points is None:
        return _ladder_exhausted(s, frame)
    if points == "retry":
        return None
    # 4. to 7.
    drop = _send(s, frame, points)
    if isinstance(drop, StepResult):
        return drop
    if drop == "retry":
        return None
    # 8. post-condition
    effect = _postcondition(s)
    if effect is not None and effect.passed is False:
        if on_fail == "retry_once" and not s.retry_used:
            s.retry_used = True
            return None
        led = Outcome("assertion_failed", f"assertion:{effect.assertion_id}") if effect.assertion_id else None
        reason = effect.reason or ("export_timeout" if s.kind in EXPORT_STEPS else "postcondition")
        return s.failed(led, reason=reason, detail="the step's declared effect did not appear within its timeout")
    return s.done("passed_after_retry" if s.retry_used else "passed")


def _resolve(s: _Step, frame: Frame) -> dict[str, tuple[int, int]] | str | None:
    """Stage 3's ladder: the points, or "retry" for the scripted second attempt, or None when exhausted."""
    points: dict[str, tuple[int, int]] = {}
    missed = False
    for f in POINT_FIELDS:
        if f not in s.step:
            continue
        literal = _point(s.step[f])
        if literal is not None:
            points[f] = literal
            continue
        target = {**s.targets[f], "agent_fallback": False}       # rung 4 runs only after the scripted retry (5.5)
        res = s.locator.locate(target, frame)
        s.rung_results += _rungs(res.rung_results)
        if res.rung in ("jab", "image", "ocr") and res.point is not None:
            points[f] = res.point
            s.rung = res.rung if s.rung in (None, "none") else s.rung
        else:
            missed = True
    if not missed:
        return points
    if not s.retry_used:                       # 5.5: reset_input_state, re-enumerate, re-capture, once more
        s.retry_used = True
        reset = s.ports.reset_input_state()
        s.events.append(s.ctx.event("reset_input_state", s.step_id, attempt=s.attempt,
                                    reason="scripted rungs failed: one more scripted attempt (5.5)"
                                    + ("" if reset else "; reset_input_state failed")))
        s.ports.enumerate_windows()
        s.rung = None
        return "retry"
    return None


def _ladder_exhausted(s: _Step, frame: Frame) -> StepResult:
    """Both scripted attempts failed: rung 4 when allowed, else `failed` with `landmark_missing` (5.5)."""
    ctx = s.ctx
    s.rung = "none"
    single = len(s.targets) == 1 and not any(_point(s.step.get(f)) for f in POINT_FIELDS)
    block = agent_rung_block(s.step, s.target, ctx.flow, agent_available=ctx.agent_available,
                             dr07_branch_b=ctx.dr07_branch_b, flag=s.ports.flag(), teardown=s.teardown,
                             rescue_available=ctx.rescue is not None and ctx.rescue.available())
    if block is None and not single:
        block = "multi_point_step"
    trace: tuple[Mapping[str, Any], ...] = ()
    final: tuple[int, int] | None = None
    if block is None and ctx.rescue is not None and s.target is not None:
        rescue = ctx.rescue.rescue(s.step, s.target, frame)
        trace, final = rescue.trace, rescue.locate.point
        s.rung_results += _rungs(rescue.locate.rung_results)
        stop = any([s.note(r) for r in rescue.consequences])
        if rescue.locate.rung == "agent" and not stop:
            s.rung = "agent"
            s.findings.append(_locator_finding(s, frame, trace, final, LOCATOR_TEXT.format(step=s.step_id)))
            return s.done("passed_via_agent_fallback")
        block = rescue.reason or "agent_failed"
        if stop:
            return s.failed(detail="the agent rescue ended the run")
    text = DEGRADED_TEXT if block == "agent_unavailable" else LOCATOR_TEXT.format(step=s.step_id)
    s.findings.append(_locator_finding(s, frame, trace, final, text, agent=block))
    return s.failed(Outcome("landmark_missing", f"locator:{s.step_id}"),
                    detail=f"locator exhausted on every rung; agent rung: {block}")


def _locator_finding(s: _Step, frame: Frame, trace: Sequence[Mapping[str, Any]], final: tuple[int, int] | None,
                     text: str, *, agent: str | None = None) -> FindingSpec:
    """The `locator_failure` finding (5.5): every rung's result and score, the pre-attempt screenshot, the agent
    trace, the final target and a 40x20 crop around it."""
    crop = s.ports.keep_image(click_crop(frame, final, TARGET_CROP), name=f"{s.step_id}-target") \
        if final is not None else None
    detail = {"step_id": s.step_id, "rung_results": s.rung_results, "pre_screenshot": s.pre_screenshot,
              "agent_trace": [dict(t) for t in trace], "final_target": list(final) if final else None,
              "target_crop_sha256": crop, "agent_rung": agent or "passed"}
    return FindingSpec("locator_failure", text, detail=json.dumps(detail, sort_keys=True, default=str))


def _enrich(s: _Step, a: Action, label: str | None, under: str | None) -> Action:
    """Fill what the step and stage 3 know and the planner may not: the labels, the order-stage declaration, the
    guarded-key declaration, the flow class, the account role and the teardown action (5.4.3, 5.4.7)."""
    flow = s.ctx.flow
    return replace(
        a, resolved_name=a.resolved_name or (label if a.is_pointer else None),
        under_point_text=a.under_point_text or (under if a.is_pointer else None),
        order_stage_step=a.order_stage_step or s.step.get("order_stage") is True,
        declared_guarded=a.declared_guarded or declares_guarded(s.step),
        flow_class=a.flow_class or (str(flow["class"]) if flow.get("class") else None),
        account_role=a.account_role or flow.get("account_role"),
        teardown_action=a.teardown_action or (str(s.step["action"]) if s.teardown and s.step.get("action") else None))


def _send(s: _Step, frame: Frame, points: Mapping[str, tuple[int, int]]) -> StepResult | str | None:
    """Stages 4 to 7 for every planned action: None when all went through, "retry" for the integrity retry, or the
    step's final result."""
    ctx, ports = s.ctx, s.ports
    label = next((str(t.get("label") or t.get("name")) for t in (s.target,) if t), None)
    actions = s.planner.plan(s.step, points, ports.flag(), ctx.clock.monotonic())
    for i, raw in enumerate(actions):
        crop_sha = under = None
        if raw.is_pointer and raw.point is not None:
            crop = click_crop(frame, raw.point)
            under = ports.read_label(crop) if crop is not None else None
            crop_sha = ports.keep_image(crop, name=f"{s.step_id}-{s.attempt}-{i}-crop") if crop is not None else None
            s.click_crop = s.click_crop or crop_sha
        a = _enrich(s, raw, label, under)
        if ctx.heartbeat is not None and s.kind != "park_mouse":
            ctx.heartbeat.expect_screen_change()
        res = ports.send_input(a)
        s.events.append(_action_row(s, a, res, crop_sha))
        _audit(s, a, res, crop_sha)
        if res.refused is not None:
            return _refused(s, a, res)
        out = _consumed(s, a, res)
        if out is not None:
            return out
    return None


def _action_row(s: _Step, a: Action, res: EmitResult, crop_sha: str | None) -> StepEvent:
    """The emitter's row for one action (choice 8 for secrets)."""
    action = STEP_EVENT_ACTION.get(s.kind) or EVENT_ACTION.get(a.kind, "click")
    secret = a.kind == "secret"
    decision, reason, cls = "emitted", None, "allowed"
    if res.refused is not None:
        decision, reason, cls = "refused", res.refused.reason, res.refused.gate_class
    else:
        bad = _first_drop(res, a)
        if bad is not None:
            decision, reason, cls = "dropped", bad[0].reason, bad[0].gate_class
    target_text = f"<redacted:{s.step.get('vault_ref') or 'secret'}>" if secret else (a.resolved_name or None)
    return s.ctx.event(
        action, s.step_id, attempt=s.attempt, locator_level=s.rung if s.rung in RUNGS else None,
        target_text=target_text, screen_x=a.point[0] if a.point else None, screen_y=a.point[1] if a.point else None,
        keys=None if secret else _keys(a), gate_decision=decision, gate_reason=reason, gate_class=cls,
        click_crop_sha256=crop_sha, reason=f"{len(a.keys)} characters" if secret else None)


def _decisions(res: EmitResult) -> list[HookDecision | None]:
    """One decision per emitted event; None for an event the hook never saw (its token expired unconsumed)."""
    ds: list[HookDecision | None] = list(res.decisions)
    return ds + [None] * (len(res.events) - len(ds))


def _first_drop(res: EmitResult, a: Action) -> tuple[GatewayDrop, bool] | None:
    """The first event the hook dropped or never saw, as its drop and whether it never reached the hook; a token
    that expired unconsumed is the integrity reason `token_expired` on the gateway's own event (5.4.3 stage 7)."""
    for e, d in zip(res.events, _decisions(res)):
        if d is None:
            return GatewayDrop("token_expired", "dropped", "integrity", a.profile, own_token=True), True
        if not d.allowed:
            return drain(d, e, mode=a.profile), False
    return None


def _audit(s: _Step, a: Action, res: EmitResult, crop_sha: str | None) -> None:
    """Hand the gateway's decisions to the drop-log drainer (5.4.8, 6.2)."""
    audit, ctx = s.ctx.audit, s.ctx
    if audit is None:
        return
    now_ns = int(ctx.clock.monotonic() * 1e9)
    target_text = f"<redacted:{s.step.get('vault_ref') or 'secret'}>" if a.kind == "secret" else a.resolved_name
    if res.refused is not None:
        s.events.append(audit.refused(res.refused, mono_ns=now_ns, keys=_keys(a) if a.kind != "secret" else None,
                                      point=a.point, target_text=target_text, click_crop_sha256=crop_sha,
                                      attempt=s.attempt))
        return
    records = []
    for e, d in zip(res.events, _decisions(res)):
        if d is None:
            continue
        if d.token_id is not None:
            audit.note_token(TokenNote(d.token_id, ctx.mode, ctx.flow_id, s.step_id, attempt=s.attempt,
                                       target_text=target_text, click_crop_sha256=crop_sha))
        rec = record_for(e, d, now_ns, secret=a.kind == "secret")
        if rec is not None:
            records.append(rec)
    s.events.extend(audit.drain(records))


def _refused(s: _Step, a: Action, res: EmitResult) -> StepResult:
    """A stage 4 refusal (5.4.3, 5.4.8; SD-53): the ledger decides what it ends."""
    ctx, d = s.ctx, res.refused
    assert d is not None
    ev = {"decision": "refused", "reason": d.reason, "class": d.gate_class}
    if d.flag_abort:
        r = agent_flag_abort(a.profile)
    else:
        r = ctx.ledger.record(refusal(d, mode=a.profile), unit=ctx.flow_id or s.step_id, step_id=s.step_id,
                              order_stage=ctx.order_stage or a.order_stage_step, budgets=ctx.budgets)
    s.note(r)
    status = r.step or "blocked_by_gateway"
    return s.failed(r.checkpoint, status=status, gateway_event=ev, detail=d.detail)


def _consumed(s: _Step, a: Action, res: EmitResult) -> StepResult | str | None:
    """Stage 7: None when every token was consumed; "retry" for the integrity retry; else the final result."""
    ctx = s.ctx
    bad = _first_drop(res, a)
    if bad is None:
        return None
    drop, never = bad
    ev = {"decision": "dropped", "reason": drop.reason, "class": drop.gate_class}
    own_integrity = drop.gate_class == "integrity" and drop.own_token
    unit = ctx.flow_id or s.step_id
    stage = ctx.order_stage or a.order_stage_step
    if not own_integrity:
        r = ctx.ledger.record(drop, unit=unit, step_id=s.step_id, order_stage=stage, budgets=ctx.budgets)
        s.note(r)
        return s.failed(r.checkpoint, status=r.step or "blocked_by_gateway", gateway_event=ev)
    if not s.retry_used:
        answered = s.ports.probe_hook()
        s.events.append(ctx.event("probe", s.step_id, attempt=s.attempt, reason="integrity drop: liveness probe "
                                  + ("answered" if answered else "not answered")))
        r = ctx.ledger.record(drop, unit=unit, step_id=s.step_id, order_stage=stage, probe_answered=answered,
                              budgets=ctx.budgets)
        if answered and r.has("retry_step"):
            s.retry_used = True
            s.findings.append(FindingSpec("gateway_integrity", f"own token dropped {drop.reason}; the hook answered "
                                          "the probe and the step was retried once (5.4.3 stage 7)"))
            return "retry"
        if answered:                             # a threshold of 5.4.8 decided instead (quarantine, kill switch)
            s.note(r)
            return s.failed(r.checkpoint, status=r.step or "failed", gateway_event=ev, detail=r.detail)
        return _fail_closed(s, r, "failed", ev, "the hook did not answer the probe (5.4.3 stage 7, SD-52)")
    r = ctx.ledger.record(drop, unit=unit, step_id=s.step_id, order_stage=stage, retried=True, budgets=ctx.budgets)
    status = "failed" if never else "blocked_by_gateway"
    return _fail_closed(s, r, status, ev, "the retried event again " + ("never reached the hook" if never
                                                                       else f"dropped {drop.reason}"))


def _fail_closed(s: _Step, ledger_r: ControlResult, status: str, ev: Mapping[str, Any], why: str) -> StepResult:
    """The hook cases of stage 7: the checkpoint `harness_error (reason: hook_lost)` and the fail-closed sequence of
    5.4.2 through `watchdog.hook_host_died`, which spends the shard's hook host budget (SD-52)."""
    if ledger_r.run is not None:                 # the ten-per-run trip came first
        s.note(ledger_r)
        return s.failed(status=status, gateway_event=ev, detail=why)
    dead = hook_host_died(s.ctx.budgets, step_id=s.step_id)
    actions = tuple(dict.fromkeys(ledger_r.actions + dead.actions))
    s.note(replace(dead, control="gateway", actions=actions, findings=ledger_r.findings or dead.findings,
                   incident=ledger_r.incident or dead.incident, evidence={**ledger_r.evidence, "why": why}))
    return s.failed(Outcome("harness_error", "hook_lost"), status=status, gateway_event=ev, detail=why)


def _postcondition(s: _Step) -> Effect | None:
    """Stage 8: poll the declared effect until it holds, a final reason arrives, or `timeout_ms` passes."""
    ctx = s.ctx
    timeout_s = float(s.step.get("timeout_ms", DEFAULT_TIMEOUT_MS)) / 1000.0
    deadline = ctx.clock.monotonic() + timeout_s
    while True:
        effect = s.ports.postcondition(s.step)
        left = deadline - ctx.clock.monotonic()
        if effect.passed is not False or effect.reason is not None or left <= 0:
            return effect
        if s.note(ctx.sleep(min(ctx.poll_s, left))):
            return effect
