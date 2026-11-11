"""The Executor on the simulated rig: the ports `executor.flow.run_unit` and `executor.emitter.emit` read (5.4.3,
5.5, 2.4, 2.6.1, 4.7, 10.1, 10.2), over `orchestrator.SimulatedRig` and the run's own capture and evaluation.

`SimDesk` is the simulated screen the locator ladder reads. Every control of the Surface Registry has a fixed
rectangle inside the rig's grab area (`simrig.FRAME_ORIGIN`, `FRAME_SIZE`) and a texture of its own, which is the
control's registry template as the image rung matches it (5.5 rung 2): the ticket's Confirm and Send and the
dialog's Send keep the rig's own rectangles and textures (the Window Watcher's detector templates), the dialog's
Delete is centred on `simrig.DELETE_POINT`, its title-bar Close sits in the dialog's top-right corner, every other
control takes the next free cell of a grid below the dialog. The Order Confirmation's controls show only while its
dialog is open, every other control while the app runs. `SimLadder` is the real `executor.locator.Ladder` over that
screen (its own second attempt re-grabs the desk); it records the label of the control it resolved, which
`SimUnitPorts.read_label` returns for the crop under the point (the simulated OCR of stage 3).

`SimUnitPorts` implements `executor.flow.UnitPorts` (and so `executor.emitter.EmitterPorts`) for one shard:

- the foreground window, the modal state and the flag come from the rig (its Window Watcher, `poll_flag`); the grab
  is the desk; `send_input` is the rig's (the slow half, the tokens, the hook and the simulated app), with the rig's
  own drainer observer held off, since the emitter feeds the shard's drainer itself (`ExecContext.audit`);
- preconditions read the rig: `paper_mode` holds while the session has a current paper-asserted stamp (the flow
  boundary asserted it, 6.4) and is asserted again through `Orchestrator.assert_paper_mode` otherwise;
  `working_orders` counts the rig's Working rows; `no_modal` reads the dialog; the other checks hold;
- `capture` hands the checkpoint to the caller's capture function (settle, redaction, diff, invariants, re-runs,
  decisions and classification: `run._ShardWork`);
- a `wait_for` on a window title is satisfied while the rig shows a window of that title, any other at once;
- teardown actions with no target: `cancel_all` clicks the rig's Cancel All through the gateway, `working_orders_check`
  counts the Working rows, the others complete;
- a type_fixture step's `verify` is its post-condition (an assertion, `Effect.assertion_id`), which holds.

`RigScript` knobs (pipeline.orchestration) reach the ports: `locator_miss` hides a control from the ladder for that
many scripted attempts (a step has two: the ladder's and the emitter's retry), `precondition_fail` fails a check
for that many reads, `wait_timeout` keeps a step's wait unsatisfied, `postcondition_fail` makes a step's declared
effect never appear.

`fixture_values(fixture, docs)` gives the Planner the fixture's typed values (every `value_ref` the flows name,
resolved with `oracle.tables.resolve_ref`); `step_action` names the 8.3 action of a step outcome row.

Choices (reported):

1. The image rung is the rung the simulated screen answers: rung 1 is skipped while DR-01 is undecided (the example's
   `phase0/branches.yaml`), and a control without a template would need the OCR rung, which the desk does not draw.
2. `read_label` returns the label of the control the ladder last resolved: the simulated OCR reads what the desk drew
   under the point, and the desk draws that control there. A literal point names no control: no label.
3. `keep_image` hashes the crop or screenshot (sha256 of its pixels) without storing it: the desk is synthetic and the
   checkpoint captures are stored by the evaluation, redacted (5.7).
4. The ladder runs its own second attempt (5.5) on a fresh grab, and the emitter its step retry after it
   (`reset_input_state`, re-enumerate, re-grab): a control hidden for two attempts is found on the step's retry
   (`passed_after_retry`), one hidden for four is `landmark_missing (reason: locator:<step>)` with the agent rung
   disabled (no rescuer in the simulation).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from ..executor.emitter import Effect, Foreground, ModalState
from ..executor.flow import AssertVerdict, Captured, CheckRead, WaitPoll
from ..executor.locator import Ladder, LadderContext, target_from_control
from ..oracle.tables import OracleConfigError, resolve_ref
from ..orchestrator.gateway import Action, Flag
from ..orchestrator.ports import Frame, LocateResult, PointOwner, ProcessInfo, VaultLease, WindowInfo
from ..orchestrator.results import ControlResult
from ..orchestrator.rig import EmitResult
from ..orchestrator.simrig import (CONFIRM_RECT, DELETE_POINT, DIALOG_RECT, FRAME_ORIGIN, FRAME_SIZE, SEND_RECT,
                                   SIM_TEMPLATES, SimulatedRig)
from ..orchestrator.gateway import Rect as GRect

BACKGROUND = 128
CONTROL_SIZE = (90, 20)                   # w, h of a grid control on the desk
GRID_ORIGIN = (860, 700)                  # the first grid cell (below the dialog, above the ticket's Confirm and Send)
GRID_STEP = (110, 36)
GRID_COLUMNS = 6
TITLE_CLOSE = GRect(DIALOG_RECT[0] + DIALOG_RECT[2] - 28, DIALOG_RECT[1] + 2, 22, 16)
DELETE_RECT = GRect(DELETE_POINT[0] - 30, DELETE_POINT[1] - 10, 60, 20)
FIXED = {"trade.order-entry.stock.confirm-and-send": (CONFIRM_RECT, SIM_TEMPLATES[1].image),
         "dialog.order-confirmation.send-button": (SEND_RECT, SIM_TEMPLATES[0].image),
         "dialog.order-confirmation.delete-button": (DELETE_RECT, None),
         "dialog.order-confirmation.title-close": (TITLE_CLOSE, None)}
STEP_ACTIONS = {"click_named": "click", "dblclick": "dblclick", "rclick": "rclick", "drag": "drag", "wheel": "wheel",
                "press": "key_chord", "key_chord": "key_chord", "type_fixture": "type", "secret": "secret",
                "focus_neutral": "click", "export": "export", "copy_grid": "clipboard", "checkpoint": "capture",
                "wait_for": "wait", "reset_input_state": "reset_input_state"}


def _texture(name: str, w: int, h: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big")
    img = np.random.default_rng(seed).integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    img.setflags(write=False)
    return img


def _bgr(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 3 else np.repeat(img[:, :, None], 3, axis=2)


@dataclass(frozen=True)
class DeskControl:
    """One control as the desk draws it: its registry name, label, window, screen rectangle and texture."""

    name: str
    label: str
    window: str
    rect: GRect
    template: str
    texture: np.ndarray = field(repr=False)
    dialog: bool = False


class SimDesk:
    """The simulated screen of the locator ladder (module docstring)."""

    def __init__(self, surfaces: Mapping[str, Mapping[str, Any]]):
        self.controls: dict[str, DeskControl] = {}
        self.targets: dict[str, dict[str, Any]] = {}
        slot = 0
        for sid in sorted(surfaces):
            doc = surfaces[sid] or {}
            tw = doc.get("target_window") or {}
            window = str(tw.get("title_regex")) if tw.get("kind") not in (None, "main") and tw.get("title_regex") \
                else "main"
            for c in doc.get("controls") or ():
                if not c.get("template"):
                    continue
                name = f"{sid}.{c['id']}"
                label = str(c.get("ocr_text") or (c.get("jab") or {}).get("name") or c["id"])
                fixed = FIXED.get(name)
                if fixed is not None:
                    rect, tex = fixed
                else:
                    col, row = slot % GRID_COLUMNS, slot // GRID_COLUMNS
                    slot += 1
                    rect = GRect(GRID_ORIGIN[0] + col * GRID_STEP[0], GRID_ORIGIN[1] + row * GRID_STEP[1],
                                 *CONTROL_SIZE)
                    tex = None
                texture = _bgr(tex) if tex is not None else _texture(name, rect.w, rect.h)
                self.controls[name] = DeskControl(name, label, window, rect, str(c["template"]), texture,
                                                  dialog=sid.startswith("dialog."))
                target = target_from_control(sid, c)
                target.update(label=label, window=window)
                self.targets[name] = target
        self.templates = {c.template: c.texture for c in self.controls.values()}

    def target(self, name: str) -> dict[str, Any]:
        """The 5.5 target of a step's `target` (`ExecContext.resolve_target`): the registry control's ladder
        definition with its label and window; a name the registry lacks has nothing to locate."""
        return dict(self.targets.get(name) or {"name": name, "label": name.rsplit(".", 1)[-1], "window": "main"})

    def frame(self, rig: SimulatedRig, *, hidden: frozenset[str] = frozenset()) -> Frame:
        """A grab of the desk as the rig shows it now: the dialog's controls while it is open, the others while the
        app runs; `hidden` controls are not drawn."""
        img = np.full((FRAME_SIZE[1], FRAME_SIZE[0], 3), BACKGROUND, dtype=np.uint8)
        for c in self.controls.values():
            shown = rig.dialog_open if c.dialog else rig.app_running
            if not shown or c.name in hidden:
                continue
            x, y = c.rect.x - FRAME_ORIGIN[0], c.rect.y - FRAME_ORIGIN[1]
            img[y:y + c.rect.h, x:x + c.rect.w] = c.texture
        return Frame(img, FRAME_ORIGIN, rig.clock.monotonic())


APPLIED = "applied:"                      # a result the Orchestrator applied already, handed back to the unit


def applied(r: ControlResult) -> ControlResult:
    """`r` marked as applied already: a hook or port hands an Orchestrator result back to `run_unit` (to end the
    unit), and the caller does not apply it a second time from `UnitResult.consequences`."""
    return r if r.control.startswith(APPLIED) else replace(r, control=APPLIED + r.control)


class SimVault:
    """The credential vault of the simulated rigs (`ports.VaultPort`, 6.9, 7.7): every login's secret is the run's
    stand-in plaintext (`SECRET`); `unreachable` makes every lease fail (`vault_unreachable`)."""

    SECRET = "sim-paper-login-secret-4b7e19"

    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable
        self.leased: list[VaultLease] = []
        self.released: list[VaultLease] = []

    def lease(self, alias: str) -> VaultLease:
        if self.unreachable:
            raise ConnectionError("the simulated vault is unreachable")
        lease = VaultLease(alias, f"lease-{len(self.leased) + 1}", "2026-11-10T23:59:59Z")
        self.leased.append(lease)
        return lease

    def secret(self, lease: VaultLease) -> str:
        if lease not in self.leased or lease in self.released:
            raise KeyError(f"lease {lease.lease_id} is not held")
        return self.SECRET

    def release(self, lease: VaultLease) -> None:
        self.released.append(lease)


class SimLadder(Ladder):
    """`executor.locator.Ladder` over the desk (module docstring), one scripted attempt per `locate` (the emitter
    owns the step's retry, locator choice 9): it hides a control `misses` asks for and records the label of the
    control it resolved (choice 2)."""

    def __init__(self, desk: SimDesk, rig: SimulatedRig, ctx: LadderContext, misses: dict[str, int], *,
                 branches: Mapping[str, Any] | None = None):
        super().__init__(None, None, dict(branches or {}), ctx, single_attempt=True)
        self.desk, self.rig, self.misses = desk, rig, misses
        self.label: str | None = None
        self.results: list[Any] = []

    def _hidden(self, name: str) -> frozenset[str]:
        left = self.misses.get(name, 0)
        if left <= 0:
            return frozenset()
        self.misses[name] = left - 1
        return frozenset({name})

    def locate(self, target: Mapping[str, Any], frame: Frame) -> LocateResult:
        name = str(target.get("name", ""))
        if name in self.misses and self.misses[name] > 0:
            frame = self.desk.frame(self.rig, hidden=self._hidden(name))
        self.ctx = replace(self.ctx, templates=self.desk.templates)
        res = super().locate(target, frame)
        self.results.append(self.last)
        self.label = str(target.get("label") or name) if res.point is not None else None
        return res


def fixture_values(fixture: Mapping[str, Any], docs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The typed values the Planner may type (primitives choice 3): every `value_ref` the flows' steps name, resolved
    in the fixture; a whole-number float keeps two decimals (a price)."""
    out: dict[str, str] = {}
    for doc in docs:
        for block in ("steps", "normalize", "teardown", "preconditions"):
            for st in doc.get(block) or ():
                ref = st.get("value_ref") if isinstance(st, Mapping) else None
                if not isinstance(ref, str) or ref in out:
                    continue
                try:
                    v = resolve_ref(fixture, ref)
                except OracleConfigError:
                    continue
                out[ref] = f"{v:.2f}" if isinstance(v, float) and v == int(v) else str(v)
    return out


def step_action(step: Mapping[str, Any], status: str, gateway_event: Mapping[str, Any] | None) -> str | None:
    """The 8.3 action of a step outcome row: what the step did; None leaves it to the store (a gateway block is a
    `gateway_decision`)."""
    if status == "blocked_by_gateway" or gateway_event:
        return None
    kind = str(step.get("type"))
    if kind == "teardown":
        return "click" if step.get("target") else None
    return STEP_ACTIONS.get(kind)


LAUNCHER_PID, APP_PID, FOREIGN_PID = 4000, 4100, 5100       # the launcher, the app (the rig's windows) and a stranger
FOREIGN_IMAGE = "notepad.exe"
VIRTUAL_SCREEN = (0, 0, 1920, 1080)


def sim_processes() -> tuple[ProcessInfo, ...]:
    """The launched process tree of the simulated rig and one foreign process (5.7 PID filter)."""
    return (ProcessInfo(LAUNCHER_PID, None, "thinkorswim-launcher.exe"),
            ProcessInfo(APP_PID, LAUNCHER_PID, "thinkorswim.exe"), ProcessInfo(FOREIGN_PID, None, FOREIGN_IMAGE))


def sim_windows(rig: SimulatedRig, *, foreign: bool = False, hide: str | None = None) -> tuple[WindowInfo, ...]:
    """The rig's windows as Capture enumerates them (3.4 rule 1): the main frame and the open dialog, a foreign
    window over the screen's centre with `foreign`, and without the window whose title matches `hide`."""
    out = [w for w in rig.enumerate_windows() if hide is None or not re.search(hide, w.title or "")]
    if foreign:
        out.append(WindowInfo(9100, FOREIGN_PID, "Notepad", "Untitled - Notepad", rect=(0, 0, 1920, 1080), z_order=0,
                              process_name=FOREIGN_IMAGE))
    return tuple(out)


def sim_point_owners(points: Sequence[tuple[int, int]], *, foreign: bool = False) -> tuple[PointOwner, ...]:
    """`WindowFromPoint` at each sample (3.4): the app, or the foreign window over every point with `foreign`."""
    if foreign:
        return tuple(PointOwner(x, y, 9100, FOREIGN_PID, "Untitled - Notepad") for x, y in points)
    return tuple(PointOwner(x, y, 1, APP_PID, "Main@thinkorswim") for x, y in points)


class SimUnitPorts:
    """`executor.flow.UnitPorts` over the simulated rig for one shard (module docstring)."""

    def __init__(self, rig: SimulatedRig, desk: SimDesk, *, orch: Any,
                 capture: Callable[[Mapping[str, Any], bool], Captured], hold_observer: Callable[[bool], None],
                 precondition_fail: Mapping[str, int] | None = None, wait_timeout: Sequence[str] = (),
                 postcondition_fail: Sequence[str] = (), popup: Callable[[ModalState], ControlResult] | None = None):
        self.rig, self.desk, self.orch = rig, desk, orch
        self._capture, self._hold = capture, hold_observer
        self.pre_fail = dict(precondition_fail or {})
        self.wait_timeout, self.post_fail = frozenset(wait_timeout), frozenset(postcondition_fail)
        self._popup = popup
        self.ladder: SimLadder | None = None

    # -- stages 1 to 3 --------------------------------------------------------------------------------------
    def foreground(self) -> Foreground:
        if self.rig.dialog_open:
            return Foreground(self.rig._dialog_hwnd, 4100, True, "thinkorswim.exe", "Order Confirmation")
        return Foreground(1, 4100, self.rig.app_running, "thinkorswim.exe", "Main@thinkorswim")

    def set_focus(self, window: str) -> None:
        return None

    def modal_state(self) -> ModalState:
        if self.rig.dialog_open:
            return ModalState(False, "Order Confirmation", self.rig._dialog_hwnd)
        return ModalState(True)

    def flag(self) -> Flag:
        return self.rig.poll_flag()

    def grab(self) -> Frame:
        return self.desk.frame(self.rig)

    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(self.rig.enumerate_windows())

    def read_label(self, crop: Any) -> str | None:
        return self.ladder.label if self.ladder is not None else None

    def keep_image(self, image: Any, *, name: str) -> str | None:
        if image is None:
            return None
        return hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()

    # -- stages 4 to 8 --------------------------------------------------------------------------------------
    def send_input(self, action: Action) -> EmitResult:
        self._hold(True)
        try:
            return self.rig.send_input(action)
        finally:
            self._hold(False)

    def probe_hook(self) -> bool:
        return not self.rig.s.hook_dead

    def reset_input_state(self) -> bool:
        return self.rig.reset_input_state()

    def postcondition(self, step: Mapping[str, Any]) -> Effect:
        sid = str(step.get("id"))
        verify = step.get("verify") if isinstance(step.get("verify"), Mapping) else None
        if sid in self.post_fail:
            return Effect(False, assertion_id=str(verify["id"]) if verify and verify.get("id") else None)
        return Effect(True) if verify else Effect(None)

    def popup(self, modal: ModalState) -> ControlResult:
        if self._popup is not None:
            return self._popup(modal)
        return ControlResult("popup", False, "running", detail=f"a modal owns the main frame: {modal.modal_title!r}")

    # -- the unit's own ports ---------------------------------------------------------------------------------
    def precondition(self, check: str, expect: Any) -> CheckRead:
        left = self.pre_fail.get(check, 0)
        if left > 0:
            self.pre_fail[check] = left - 1
            return CheckRead(False, detail=f"{check} does not hold (scripted)")
        if check == "paper_mode":
            stamp = self.orch.paper_stamp()
            if stamp is not None and self.orch.may_release(stamp):
                return CheckRead(True)
            r = self.orch.assert_paper_mode()
            return CheckRead(r.passed, control=None if r.passed else applied(r))
        if check == "working_orders":
            n = len(self.rig.book.working)
            return CheckRead(n <= int(expect or 0), value=n)
        if check == "no_modal":
            return CheckRead(not self.rig.dialog_open)
        return CheckRead(True)

    def reset_ui(self) -> bool:
        return self.rig.reset_input_state()

    def reset_profile(self) -> bool:
        return True

    def reload_workspace(self) -> bool:
        return True

    def teardown_action(self, step: Mapping[str, Any]) -> CheckRead:
        action = str(step.get("action"))
        if action == "cancel_all":
            r = self.rig.cancel_all()
            return CheckRead(r.located and r.clicked, detail=None if r.clicked else "Cancel All was not clicked")
        if action == "working_orders_check":
            n = len(self.rig.book.working)
            return CheckRead(n <= int(step.get("expect") or 0), value=n,
                             detail=None if n <= int(step.get("expect") or 0) else f"{n} working orders")
        if action == "clear_ticket":
            self.rig.ticket_visible = False
        return CheckRead(True)

    def wait_poll(self, step: Mapping[str, Any]) -> WaitPoll:
        if str(step.get("id")) in self.wait_timeout:
            return WaitPoll(False, "anchor_missing")
        regex = (step.get("anchor") or {}).get("window_title_regex") if isinstance(step.get("anchor"), Mapping) \
            else None
        if regex and not any(re.search(str(regex), w.title or "") for w in self.rig.enumerate_windows()):
            return WaitPoll(False, "anchor_missing")
        return WaitPoll(True)

    def capture(self, step: Mapping[str, Any], *, rescued: bool) -> Captured:
        return self._capture(step, rescued)

    def evaluate(self, step: Mapping[str, Any]) -> AssertVerdict:
        return AssertVerdict()
