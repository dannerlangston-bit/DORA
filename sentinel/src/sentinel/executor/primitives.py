"""Input primitives: the token sequence each 4.7 step type produces (5.4.10, 5.4.3 stages 5 and 6, 5.4.5).

`Planner.plan(step, points, flag, now)` implements the `ports.PrimitivePlanner` protocol: it turns one resolved step
into `PlannedAction`s. A `PlannedAction` is a `gateway.Action` (what stage 4, `gateway.slow_half`, judges) that also
carries its `units`, the batches stages 5 to 7 run in order:

- `Unit.lead`: untokened absolute moves, emitted before the unit's tokens are written, so the hook's move rule
  (no live token) lets them through (5.4.3 stage 6: "an absolute move first");
- `Unit.tokens`: the `TokenSpec`s written in stage 5 (`issue_tokens` writes them into a `gateway.GatewayState`);
- `Unit.events`: the tokened events in order (`Emit`: the `InputEvent`, the index of the token that authorizes it,
  its offset in ms from the unit's start, and for typed characters the character itself);
- `Unit.move_allow`: a drag's path box, opened with the token write until `hold_ms + 1000`.

The emitter writes one unit's tokens, sends its lead and its events, waits for consumption (stage 7) and only then
starts the next unit, so a step never holds more than a drag's token or a chord's modifier pairs at once and typing
emits one character at a time (5.4.5: "Sixteen slots suffice").

The table of 5.4.10, one row per step type:

| type | units |
|---|---|
| `click_named`, `focus_neutral`, `teardown` with a target | move; click token; down, up (button left, right or middle) |
| `rclick` | as `click_named` with the right button |
| `dblclick` | move; two click tokens; down, up, down, up, the second pair inside the double-click time |
| `drag` | move to `from`; one click token (`hold_ms`) and `move_allow` over the path box; down; `steps` interpolated moves; up at `to` |
| `wheel` | move to `at`; one wheel token and one wheel event per notch; a move to the park point after |
| `press` | key token; down, up (a `shift+tab` style key is a chord, as `key_chord`) |
| `key_chord` | one key token per modifier and one for the key; modifier downs, key down, key up, modifier ups (reverse order) |
| `type_fixture` | one key pair per character (`VK_PACKET`), then the commit key pair |
| `secret` (`plan_secret`) | as `type_fixture` with `secret` tokens; only for `executor.secret.run_secret_step` |
| `reset_input_state` | release tokens: key ups Shift, Ctrl, Alt, Win and button ups left, right, middle; plus one Escape press only while the flag is clear |
| `park_mouse` | one untokened move to the park point |

`overdue_reset(state, flag, now)` is the 5.4.5 rule for the emitter loop: a `half_consumed` token with no up 2 s on
makes the slow half emit `reset_input_state`.

Choices where the spec is ambiguous or silent (reported):

1. Typing (5.4.3 stage 6, 5.4.10): every character is one key pair with virtual key `VK_PACKET` carrying the
   character (Unicode input), so "one key pair per character" holds for any value whatever the keyboard layout and
   no Shift is ever held for typing. A character of Unicode category C (control and format characters: Enter, Tab,
   line breaks) or Z (spaces and separators) is refused: Enter and Space are guarded keys (5.4.7) and Tab a commit,
   which only `commit` or `press` declare, never a typed value. A value needing a space cannot be typed.
2. `type_fixture` and `secret` do not click their target: the flow focuses the field with an earlier step (the 4.7
   example clicks the price field, then Ctrl+A, then types; a click would drop that selection). The target names the
   field for the step record and the post-condition.
3. The values: a literal `value` comes only from the flow file and is refused under profile `agent` or `explorer`
   (5.6: a `value` literal is a tool error); a `value_ref` is looked up in `values`, the fixture's typed values (and
   odd-input entries) the caller resolved; nothing else is accepted, so no text from a model is ever typed.
4. `dblclick` sends its second pair `double_click_ms // 5` ms after the first (100 ms at the Windows default of 500),
   inside the double-click time and the 500 ms token expiry.
5. A drag's moves are spaced evenly over `hold_ms` (move i of n at `hold_ms * i / (n + 1)`), the up at `hold_ms`;
   `move_allow` is the path's bounding box inflated by the 2 px token tolerance, for the rounding of absolute moves.
6. The Escape of `reset_input_state` is its own `PlannedAction` with `flag_clear_only`: it is planned only when the
   flag passed in is clear, and the emitter re-checks it against the fresh flag with `allowed_under` before stage 4,
   since the slow half does not refuse Escape (it is not a guarded key).
7. `teardown` steps with a resolved `target` point are planned as one click carrying `teardown_action`, which the
   slow half needs to let `cancel_all` click Cancel All (6.2). `wait_for`, `checkpoint`, `assert` and
   `precondition` steps emit nothing; `export` and `copy_grid` are composite (a click, a located menu item, then the
   typed path) and are driven by `executor.export.run_export`; `secret` is planned only through `plan_secret`, which
   `executor.secret.run_secret_step` calls with the leased value. Each of these raises `PlanError` here.
8. `Resolution` carries what stage 3 resolved for a point (the control name, the under-point OCR text, the target kind
   and ambiguity) into the `Action` fields the slow half reads; the emitter passes it as `resolved=`.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..orchestrator.gateway import (GUARDED_VK, TOLERANCE_PX, VK_CONTROL, VK_ESCAPE, VK_LWIN, VK_MENU, VK_RETURN,
                                    VK_SHIFT, VK_TAB, Action, Chord, Flag, GatewayState, InputEvent, Rect, Token)

VK_PACKET = 0xE7                  # a Unicode character carried in the scan code (KEYEVENTF_UNICODE)
WHEEL_DELTA = 120                 # one wheel notch
DOUBLE_CLICK_MS = 500             # the Windows default GetDoubleClickTime
MIN_DRAG_STEPS = 10               # 4.7, 5.4.3 stage 6, L-FLW-09
MAX_HOLD_MS = 2000                # 4.7, L-FLW-09
MODIFIERS = {"shift": VK_SHIFT, "ctrl": VK_CONTROL, "alt": VK_MENU, "win": VK_LWIN}
COMMIT_VK = {"enter": VK_RETURN, "tab": VK_TAB, "none": None}
BUTTONS = ("left", "right", "middle")
# 5.4.10 reset_input_state: what `GatewayState.issue_release` writes, in its order
RELEASE_KEYS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN)
POINTER_TYPES = ("click_named", "rclick", "dblclick", "focus_neutral", "teardown")
NOT_INPUT = ("wait_for", "checkpoint", "assert", "precondition")
COMPOSITE = ("export", "copy_grid")
TOOL_PROFILES = ("agent", "explorer")


class PlanError(ValueError):
    """The step cannot be planned as asked: the emitter fails the step before any token is written."""


@dataclass(frozen=True)
class TokenSpec:
    """One token to write in stage 5 (5.4.2 `tokens[16]`): a click (a drag's press when `hold_ms` is set), a key
    (`secret` for a secret step), a wheel notch, or a `reset_input_state` release."""

    kind: str                         # click | key | wheel | release
    x: int = 0
    y: int = 0
    button: str | None = None
    vk: int | None = None
    sign: int = 0
    hold_ms: int | None = None
    secret: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("click", "key", "wheel", "release"):
            raise ValueError(f"token kind {self.kind!r}")


RELEASE_SPECS = tuple(TokenSpec("release", vk=vk) for vk in RELEASE_KEYS) + tuple(
    TokenSpec("release", button=b) for b in BUTTONS)


@dataclass(frozen=True)
class Emit:
    """One tokened event of a unit: the event, the index of its token in `Unit.tokens` (None for a drag's moves,
    which `move_allow` covers), its offset from the unit's start, and the character a `VK_PACKET` key carries."""

    event: InputEvent
    token: int | None = None
    at_ms: int = 0
    char: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Unit:
    """One stage 5 to 7 batch (module docstring)."""

    lead: tuple[InputEvent, ...] = ()
    tokens: tuple[TokenSpec, ...] = ()
    events: tuple[Emit, ...] = ()
    move_allow: Rect | None = None
    hold_ms: int | None = None

    def __post_init__(self) -> None:
        if any(e.type != "move" for e in self.lead):
            raise ValueError("a unit's lead events are moves")
        if any(e.token is not None and not 0 <= e.token < len(self.tokens) for e in self.events):
            raise ValueError("an event names a token the unit does not write")
        if (self.move_allow is None) != (self.hold_ms is None):
            raise ValueError("move_allow and hold_ms come together (a drag)")
        releases = [t for t in self.tokens if t.kind == "release"]
        if releases and tuple(self.tokens) != RELEASE_SPECS:
            raise ValueError("release tokens are written as the full reset_input_state set")

    def pairs(self) -> tuple[tuple[str | None, str], ...]:
        """(token kind or None, event type) for every event, lead moves first: the sequence 5.4.10 tabulates."""
        lead = tuple((None, e.type) for e in self.lead)
        return lead + tuple((self.tokens[e.token].kind if e.token is not None else None, e.event.type)
                            for e in self.events)


@dataclass(frozen=True)
class PlannedAction(Action):
    """A `gateway.Action` with the units that perform it (module docstring). `step_type` is the 4.7 step type,
    `flag_clear_only` marks the Escape of `reset_input_state` (choice 6), `planned_at` the monotonic time of the
    plan and `secret_ref` the vault reference of a secret step (never its value)."""

    step_type: str = ""
    units: tuple[Unit, ...] = ()
    flag_clear_only: bool = False
    planned_at: float = 0.0
    secret_ref: str | None = None

    def allowed_under(self, flag: Flag) -> bool:
        """False when the action may only run with the flag clear and the fresh flag is not (choice 6)."""
        return not self.flag_clear_only or flag == Flag.CLEAR

    def pairs(self) -> tuple[tuple[str | None, str], ...]:
        """Every unit's (token kind, event type) sequence, in order."""
        return tuple(p for u in self.units for p in u.pairs())

    def chars(self) -> int:
        """How many characters the action types (the count a secret step's event records, 5.4.10)."""
        return sum(1 for u in self.units for e in u.events if e.char is not None and e.event.type == "key_down")


@dataclass(frozen=True)
class Resolution:
    """What stage 3 resolved at a point (choice 8): control name, under-point OCR text, target kind
    (`gateway.Action.target_kind`) and whether the name was ambiguous."""

    name: str | None = None
    under_point_text: str | None = None
    kind: str | None = None
    ambiguous: bool = False


# -- token write ----------------------------------------------------------------------------------------------
def issue_tokens(unit: Unit, state: GatewayState, now: float, *, dialog_ok: bool = False,
                 send_ok: bool = False) -> tuple[Token, ...]:
    """Stage 5 for one unit: write its tokens into the gateway state, in order, and open a drag's `move_allow`.
    `dialog_ok` and `send_ok` are the slow half's answer for the action (5.4.3 stage 4, 6.7); a `send_ok` token
    is single use, so it is accepted only on a unit with exactly one click token."""
    if unit.tokens == RELEASE_SPECS:
        return tuple(state.issue_release(now))
    clicks = sum(1 for t in unit.tokens if t.kind == "click")
    if send_ok and clicks != 1:
        raise ValueError("send_ok goes on a unit with exactly one click token (6.7)")
    out: list[Token] = []
    for t in unit.tokens:
        if t.kind == "click":
            out.append(state.issue_click(t.x, t.y, now, button=t.button or "left", dialog_ok=dialog_ok,
                                         hold_ms=t.hold_ms, send_ok=send_ok))
        elif t.kind == "key":
            out.append(state.issue_key(int(t.vk or 0), now, secret=t.secret))
        else:
            out.append(state.issue_wheel(t.x, t.y, t.sign, now))
    if unit.move_allow is not None and unit.hold_ms is not None:
        state.open_move_allow(unit.move_allow, now, unit.hold_ms)
    return tuple(out)


def overdue_reset(state: GatewayState, flag: Flag, now: float) -> tuple[PlannedAction, ...]:
    """5.4.5: a `half_consumed` token with no up 2 s on (`GatewayState.overdue_half_consumed`) makes the slow half
    emit `reset_input_state`; () while no token is overdue. The emitter calls it on every pass of its loop."""
    if not state.overdue_half_consumed(now):
        return ()
    return _reset(flag, now, "scripted")


# -- the planner ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Planner:
    """`ports.PrimitivePlanner` for one flow (or agent or Explorer session): the fixture's typed values, the gateway
    profile, the flow class and account role the slow half reads, and the double-click time."""

    values: Mapping[str, str] = field(default_factory=dict)
    profile: str = "scripted"
    flow_class: str | None = None
    account_role: str | None = None
    double_click_ms: int = DOUBLE_CLICK_MS

    def plan(self, step: Mapping[str, Any], points: Mapping[str, tuple[int, int]], flag: Flag, now: float, *,
             resolved: Mapping[str, Resolution] | None = None) -> tuple[PlannedAction, ...]:
        """The actions for one step, in order (5.4.10). `points` are the physical screen points the ladder resolved:
        `target` (clicks, `focus_neutral`'s neutral control, a teardown's target), `from` and `to` (drag), `at`
        (wheel) and `park` (wheel, `park_mouse`)."""
        kind = str(step.get("type", ""))
        res = resolved or {}
        if kind in NOT_INPUT:
            raise PlanError(f"a {kind} step emits no input")
        if kind in COMPOSITE:
            raise PlanError(f"{kind} is composite: executor.export.run_export drives it (5.4.10, 5.10)")
        if kind == "secret":
            raise PlanError("a secret step is planned only by executor.secret.run_secret_step with the leased value")
        if kind == "reset_input_state":
            return _reset(flag, now, self.profile)
        if kind == "park_mouse":
            p = _point(points, "park")
            return (self._action("park_mouse", kind, now, point=p, units=(Unit(lead=(InputEvent("move", *p),)),)),)
        if kind in POINTER_TYPES:
            return (self._click(step, points, res, now),)
        if kind == "drag":
            return (self._drag(step, points, res, now),)
        if kind == "wheel":
            return (self._wheel(step, points, res, now),)
        if kind in ("press", "key_chord"):
            return (self._keys(step, kind, now),)
        if kind == "type_fixture":
            return (self._type(step, self._value(step), now),)
        raise PlanError(f"unknown step type {kind!r} (4.7)")

    def plan_secret(self, step: Mapping[str, Any], value: str, now: float) -> PlannedAction:
        """The `secret` step (5.4.10, 6.9): key pairs with `secret` tokens, then the commit pair. Scripted `login`
        flows only (L-FLW-06, 5.6); the action records the vault reference, never the value."""
        if step.get("type") != "secret":
            raise PlanError("plan_secret plans secret steps")
        if self.profile != "scripted" or self.flow_class != "login":
            raise PlanError("a secret step runs only in a scripted login flow (4.7, L-FLW-06)")
        ref = step.get("vault_ref")
        if not isinstance(ref, str) or not ref:
            raise PlanError("a secret step names its vault_ref")
        return self._type(step, value, now, secret_ref=ref)

    # -- step types ---------------------------------------------------------------------------------------
    def _action(self, kind: str, step_type: str, now: float, **kw: Any) -> PlannedAction:
        return PlannedAction(kind, profile=self.profile, flow_class=self.flow_class, account_role=self.account_role,
                             step_type=step_type, planned_at=now, **kw)

    def _common(self, step: Mapping[str, Any]) -> dict[str, Any]:
        return {"order_stage_step": bool(step.get("order_stage", False))}

    def _click(self, step: Mapping[str, Any], points: Mapping[str, tuple[int, int]],
               res: Mapping[str, Resolution], now: float) -> PlannedAction:
        kind = str(step["type"])
        x, y = _point(points, "target")
        if kind == "teardown":
            action_kind, button, teardown = "click", "left", str(step.get("action") or "")
            if not teardown:
                raise PlanError("a teardown step names its action")
        else:
            teardown = None
            button = "right" if kind == "rclick" else str(step.get("button", "left"))
            action_kind = {"rclick": "rclick", "dblclick": "dblclick"}.get(kind, "click")
        if button not in BUTTONS:
            raise PlanError(f"button {button!r}; one of {BUTTONS}")
        if kind == "dblclick":
            gap = self.double_click_ms // 5
            tokens: tuple[TokenSpec, ...] = (TokenSpec("click", x, y, button=button),) * 2
            events = (Emit(InputEvent("button_down", x, y, button=button), 0),
                      Emit(InputEvent("button_up", x, y, button=button), 0),
                      Emit(InputEvent("button_down", x, y, button=button), 1, gap),
                      Emit(InputEvent("button_up", x, y, button=button), 1, gap))
        else:
            tokens = (TokenSpec("click", x, y, button=button),)
            events = (Emit(InputEvent("button_down", x, y, button=button), 0),
                      Emit(InputEvent("button_up", x, y, button=button), 0))
        r = res.get("target", Resolution())
        unit = Unit(lead=(InputEvent("move", x, y),), tokens=tokens, events=events)
        return self._action(action_kind, kind, now, point=(x, y), resolved_name=r.name,
                            under_point_text=r.under_point_text, target_kind=r.kind, ambiguous=r.ambiguous,
                            teardown_action=teardown, units=(unit,), **self._common(step))

    def _drag(self, step: Mapping[str, Any], points: Mapping[str, tuple[int, int]],
              res: Mapping[str, Resolution], now: float) -> PlannedAction:
        steps, hold = step.get("steps"), step.get("hold_ms")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < MIN_DRAG_STEPS:
            raise PlanError(f"drag steps must be an int >= {MIN_DRAG_STEPS} (4.7, L-FLW-09)")
        if not isinstance(hold, int) or isinstance(hold, bool) or not 0 < hold <= MAX_HOLD_MS:
            raise PlanError(f"drag hold_ms must be an int in 1..{MAX_HOLD_MS} (4.7, L-FLW-09)")
        (x0, y0), (x1, y1) = _point(points, "from"), _point(points, "to")
        moves = tuple(Emit(InputEvent("move", round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)),
                           None, round(hold * i / (steps + 1))) for i in range(1, steps + 1))
        box = Rect(min(x0, x1), min(y0, y1), abs(x1 - x0) + 1, abs(y1 - y0) + 1).inflate(TOLERANCE_PX)
        unit = Unit(lead=(InputEvent("move", x0, y0),), tokens=(TokenSpec("click", x0, y0, button="left", hold_ms=hold),),
                    events=(Emit(InputEvent("button_down", x0, y0, button="left"), 0),) + moves
                    + (Emit(InputEvent("button_up", x1, y1, button="left"), 0, hold),),
                    move_allow=box, hold_ms=hold)
        start, end = res.get("from", Resolution()), res.get("to", Resolution())
        return self._action("drag", "drag", now, point=(x0, y0), resolved_name=start.name,
                            under_point_text=start.under_point_text, target_kind=start.kind, end_kind=end.kind,
                            ambiguous=start.ambiguous or end.ambiguous, units=(unit,), **self._common(step))

    def _wheel(self, step: Mapping[str, Any], points: Mapping[str, tuple[int, int]],
               res: Mapping[str, Resolution], now: float) -> PlannedAction:
        notches = step.get("notches")
        if not isinstance(notches, int) or isinstance(notches, bool) or notches == 0:
            raise PlanError("wheel notches is a non-zero int (4.7, L-FLW-09)")
        x, y = _point(points, "at")
        park = _point(points, "park")
        sign = 1 if notches > 0 else -1
        units = [Unit(lead=(InputEvent("move", x, y),) if i == 0 else (), tokens=(TokenSpec("wheel", x, y, sign=sign),),
                      events=(Emit(InputEvent("wheel", x, y, delta=sign * WHEEL_DELTA), 0),))
                 for i in range(abs(notches))]
        units.append(Unit(lead=(InputEvent("move", *park),)))
        r = res.get("at", Resolution())
        return self._action("wheel", "wheel", now, point=(x, y), resolved_name=r.name,
                            under_point_text=r.under_point_text, target_kind=r.kind, ambiguous=r.ambiguous,
                            units=tuple(units), **self._common(step))

    def _keys(self, step: Mapping[str, Any], kind: str, now: float) -> PlannedAction:
        if kind == "press":
            key = step.get("key")
            if not isinstance(key, str) or not key.strip():
                raise PlanError("press names its key")
            names = [k.strip() for k in key.split("+")]
        else:
            keys = step.get("keys")
            if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) for k in keys):
                raise PlanError("key_chord names its keys: modifiers, then one key")
            names = [k.strip() for k in keys]
        mods, vk = _chord(names)
        unit = _chord_unit(mods, vk)
        # a step that names Enter or Space (`press: enter`, Ctrl+Enter) declares the guarded key explicitly (5.4.7)
        declared = vk in GUARDED_VK
        return self._action(kind, kind, now, keys=(vk,), mods=frozenset(mods), declared_guarded=declared,
                            units=(unit,), **self._common(step))

    def _value(self, step: Mapping[str, Any]) -> str:
        has_value, has_ref = "value" in step, "value_ref" in step
        if has_value == has_ref:
            raise PlanError("type_fixture takes exactly one of value_ref and value (4.7)")
        if has_value:
            if self.profile in TOOL_PROFILES:
                raise PlanError(f"a value literal under profile {self.profile} is a tool error: only value_ref (5.6)")
            value = step["value"]
            if not isinstance(value, str):
                raise PlanError("a type_fixture value literal is a string from the flow file")
            return value
        ref = step["value_ref"]
        if not isinstance(ref, str) or ref not in self.values:
            raise PlanError(f"value_ref {ref!r} is not among the fixture's typed values")
        return str(self.values[ref])

    def _type(self, step: Mapping[str, Any], value: str, now: float, *,
              secret_ref: str | None = None) -> PlannedAction:
        commit = str(step.get("commit", "none"))
        if commit not in COMMIT_VK:
            raise PlanError(f"commit {commit!r}; one of {sorted(COMMIT_VK)}")
        secret = secret_ref is not None
        units = [_char_unit(c, secret) for c in _typeable(value)]
        commit_vk = COMMIT_VK[commit]
        if commit_vk is not None:
            units.append(_chord_unit([], commit_vk, secret=secret))
        kind = "secret" if secret else "type_fixture"
        return self._action(kind, kind, now, keys=(commit_vk,) if commit_vk is not None else (),
                            declared_guarded=commit == "enter", units=tuple(units), secret_ref=secret_ref,
                            **self._common(step))


def plan(step: Mapping[str, Any], points: Mapping[str, tuple[int, int]], flag: Flag, now: float, *,
         resolved: Mapping[str, Resolution] | None = None, values: Mapping[str, str] | None = None,
         profile: str = "scripted", flow_class: str | None = None, account_role: str | None = None,
         double_click_ms: int = DOUBLE_CLICK_MS) -> tuple[PlannedAction, ...]:
    """`Planner(...).plan(step, points, flag, now)` in one call (module docstring)."""
    return Planner(dict(values or {}), profile, flow_class, account_role, double_click_ms).plan(
        step, points, flag, now, resolved=resolved)


# -- helpers --------------------------------------------------------------------------------------------------
def _point(points: Mapping[str, tuple[int, int]], name: str) -> tuple[int, int]:
    p = points.get(name)
    if p is None or len(p) != 2:
        raise PlanError(f"the step needs the resolved point {name!r}")
    return int(p[0]), int(p[1])


def _chord(names: list[str]) -> tuple[list[str], int]:
    """Modifiers in declared order, then the key's virtual key (`gateway.Chord.parse` names keys)."""
    *mods, key = [n.lower() for n in names]
    if any(m not in MODIFIERS for m in mods) or len(set(mods)) != len(mods):
        raise PlanError(f"modifiers {mods}; each at most once, from {sorted(MODIFIERS)}")
    if key in MODIFIERS:
        raise PlanError(f"a chord ends with one key that is not a modifier, got {key!r}")
    try:
        vk = Chord.parse(key).vk
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    return mods, vk


def _chord_unit(mods: list[str], vk: int, *, secret: bool = False) -> Unit:
    """Modifier downs in order, the key pair, modifier ups in reverse: one key token each (5.4.10 key_chord)."""
    tokens = tuple(TokenSpec("key", vk=MODIFIERS[m]) for m in mods) + (TokenSpec("key", vk=vk, secret=secret),)
    downs = tuple(Emit(InputEvent("key_down", vk=MODIFIERS[m], mods=frozenset(mods[:i])), i) for i, m in enumerate(mods))
    held = frozenset(mods)
    key = (Emit(InputEvent("key_down", vk=vk, mods=held), len(mods)), Emit(InputEvent("key_up", vk=vk, mods=held), len(mods)))
    ups = tuple(Emit(InputEvent("key_up", vk=MODIFIERS[mods[i]], mods=frozenset(mods[:i])), i)
                for i in reversed(range(len(mods))))
    return Unit(tokens=tokens, events=downs + key + ups)


def _char_unit(c: str, secret: bool) -> Unit:
    return Unit(tokens=(TokenSpec("key", vk=VK_PACKET, secret=secret),),
                events=(Emit(InputEvent("key_down", vk=VK_PACKET), 0, char=c),
                        Emit(InputEvent("key_up", vk=VK_PACKET), 0, char=c)))


def _typeable(value: str) -> str:
    """The value's characters, refusing control, format and separator characters (choice 1). The message never
    quotes the value: it may be a secret."""
    for i, c in enumerate(value):
        if unicodedata.category(c)[0] in "CZ":
            raise PlanError(f"character {i + 1} of the value is a control or space character; Enter, Tab and Space "
                            "are declared keys, never typed characters (5.4.7)")
    return value


def _reset(flag: Flag, now: float, profile: str) -> tuple[PlannedAction, ...]:
    """reset_input_state (5.4.10): the release unit, and one Escape only while the flag is clear (choice 6)."""
    release = Unit(tokens=RELEASE_SPECS,
                   events=tuple(Emit(InputEvent("key_up", vk=vk), i) for i, vk in enumerate(RELEASE_KEYS))
                   + tuple(Emit(InputEvent("button_up", button=b), len(RELEASE_KEYS) + i) for i, b in enumerate(BUTTONS)))
    out = [PlannedAction("reset_input_state", profile=profile, step_type="reset_input_state", units=(release,),
                         planned_at=now)]
    if flag == Flag.CLEAR:
        out.append(PlannedAction("press", profile=profile, keys=(VK_ESCAPE,), step_type="reset_input_state",
                                 units=(_chord_unit([], VK_ESCAPE),), flag_clear_only=True, planned_at=now))
    return tuple(out)
