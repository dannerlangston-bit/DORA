"""Never-send guard: the decision logic of the Input Gateway's two halves (6.2; mechanism 5.4.2 to 5.4.8).

Everything here is a pure decision over an in-memory picture of the shared segment of 5.4.2
(`GatewayState`: `armed`, `deny_all`, `flag_ticket`, the sixteen token slots, `move_allow`, the eight
deny rectangles, the blocked key map and the sixteen chord entries). The Windows parts, `SendInput`
and the low-level hooks, live behind `Rig.send_input` (rig.py); the real hook host reimplements
`hook_decide` with the same rules in its callback.

- `slow_half` is stage 4 of 5.4.3: never-tokenize labels, the Order Confirmation dismiss allowlist,
  the deny profile and the synchronous flag re-check. It refuses before any token is written.
- `GatewayState.issue_*` is stage 5: one-shot tokens with coordinates, a 2 px tolerance and a 500 ms
  expiry (drags `hold_ms + 1000`, 5.4.5).
- `hook_decide` is the fast half's table of 5.4.4 with the rules of 6.2 numbered 1 to 4.
- `FlagWatcher` maintains the ticket-or-dialog flag of 5.4.6 (set fast, cleared after two clear polls,
  unknown on a failed poll or a stale stamp).
- `drain` turns a hook decision into the logged drop of 5.4.8 (`untokened_injected`, `foreign_input`,
  the `drill` tag) with its class, and `DropLedger` applies the consequences and thresholds of 5.4.8
  and 6.2 (attempted send, three per flow or session, ten per run).
- `SafetyContext.session` carries the session's attestation stage and paper-mode assertion into stage 4,
  `SendClearance` is the gated send's gate (sendgate.py), and `HOOK_STATES` with `recovery_admits` are the
  hook's state per phase and the recovery-only arm of 6.6. The Window Watcher that feeds the flag and the
  deny rectangles is windows.py; the two gateway files are read by gateway_config.py.

Choices where the spec is ambiguous (reported):

1. Reason codes. Every refusal and drop carries one code of the closed list of 4.13 `gateway_event.reason`
   and the 8.3 `step_events.gate_reason` CHECK (`GATEWAY_REASONS`). The slow half uses `never_tokenize` (a
   send label), `dialog_allowlist` (a click in an order dialog that is not on the dismiss allowlist),
   `deny_profile` (a hit on the active profile's own list: Cancel All outside `cancel_all`, Working Orders
   cancel or replace outside `paper_send_ok`, Application Settings and the Explorer deny list for agent and
   Explorer, an action kind outside the 5.6 toolset (`secret`, `dblclick`, `rclick`; `drag` under profile
   agent), `drag_named` on anything but splitters and detach handles, keys or modifiers outside the agent
   toolset whatever the action kind, a ladder order cell or order bubble at the start or end of a gesture
   outside an order-stage step of a scripted flow, in every profile (5.4.10), any agent or Explorer action
   while the flag is set or unknown (choice 8), and `Confirm and Send` from a scripted step that is not an
   order-stage step or before attestation and a current paper-mode assertion), `guarded_key_refused` (a
   guarded key: Enter, Space, an F-key or a blocked chord under the flag or in an order-stage step; Enter or
   Space not declared by the step outside it), `shift_click_dialog` and `deny_rect` (the flag re-check's
   click rules, named as the hook names them) and `hook_host_dead`. An ambiguous
   resolution has no code in that list although 5.4.8 counts it; it is refused `deny_profile` with the
   detail "ambiguous name" (reported as a taxonomy gap).
2. Whether a refusal is an attempted send follows 5.4.3 stage 4 and is carried as a flag, since a code can
   cover both cases: a never-tokenize hit on a send label, a guarded key requested under the flag, a
   Shift+click or deny-rectangle click under the flag, and a non-allowlisted dialog click whose label is
   send-like or lies in a deny rectangle. A `dialog_allowlist` refusal on a label that is neither is the
   unknown dialog label. Hook drops are attempted sends by code (`deny_rect`, `blocked_key_dialog`,
   `blocked_chord_dialog`, `shift_click_dialog`, `dialog_not_ok`; 5.4.8 rule 1). Ladder or bubble
   geometry outside an order stage is not among 5.4.3's causes, so it is a counted non-send refusal;
   inside an order stage the ladder's order columns are deny rectangles and a click there is an attempted
   send.
3. `deny_all` and `not_armed` drops (the kill switch has fired, or the hook is between runs) are
   safety-class codes that count toward no threshold: they are the effect of a trip, not a new one.
4. Rule 2's "any mouse event while Shift is held" is applied to button downs, as the 5.4.4 table
   states it; dropping a button up would leave a button logically held (5.4.5).
5. A wheel token that has expired is not live, so the wheel row's `no_token` applies (5.4.4 lists no
   `token_expired` for the wheel).
6. The `gate_class` of a slow-half refusal is `safety` for attempted sends and unknown dialog labels,
   `integrity` for every other refusal (the 5.4.4 split covers hook codes only).
7. Explorer refusals that are not attempted sends leave the session running until the third (6.8, 2.9:
   "three denied attempts end the session"), although 5.4.3 says a refusal ends "the flow or session";
   scripted flows and agent rescues end at the first.
8. The flag in the slow half is the fresh enumeration OR the flow's sticky `order_stage` declaration
   (`SafetyContext.order_stage`, or the action's own `order_stage_step`, since the declaration sets the flag
   before the step's events are emitted, 6.2). So a guarded key in an order-stage step is refused as an
   attempted send even in the action that clicks `Confirm and Send`. While the flag is up, every agent or
   Explorer action is refused before any token is written (5.4.6 "drops all input from that moment"): a
   guarded key, Shift+click or deny-rectangle click as the attempted send it is, anything else `deny_profile`
   with `flag_abort` set, which the caller routes to `agent_flag_abort` or `ExplorerSession.order_stage`.
9. Session safety (6.2 Label rules, 5.4.3 stage 4, SD-57). `SafetyContext.session` is the
   `ports.SessionSafety` the Orchestrator sets and clears (attestation stage, last paper-mode assertion,
   session kicked) and `SafetyContext.now` the monotonic time of the check. `Confirm and Send` needs stage
   `a3_a8` for the A9 click itself (`Action.a9_check`) and `a3_a9` for every later click, a session that was
   not kicked, and a paper-mode assertion no older than the two-minute timer of 6.4 (`PAPER_ASSERTION_CURRENT_S`,
   equal to `identity.TIMER_S`): an older one means a scheduled read was missed or deferred, so it is not
   current. A caller that does not pass `session` (SimulatedRig until the integrator wires
   `set_session_safety`) is judged on the two booleans `attestation_passed` (stage `a3_a9`) and
   `paper_assertion_current`; with `session` set the booleans are not read.
10. Dismiss allowlist (6.2, 6.13 item 10, SD-65). The pinned set comes from `gateway/dismiss_allowlist.yaml`
   through `gateway_config.load_dismiss_allowlist`, which is empty until Phase 0 records its read. An empty
   allowlist refuses `Confirm and Send` (`deny_profile`), so no dialog is opened that no click could dismiss
   and ticket flows stop before the dialog. `SafetyContext`'s default is the empty set: a caller that does not
   pass the loaded file gets no dialog click. `DISMISS_ALLOWLIST` is the label 6.2 expects the dialog to show
   (`Delete`; `Edit` only when Phase 0 observes it), which a pinned file lists; it is never a default.
11. The gated send (6.7). A click whose two labels are exactly `Send` consults `SafetyContext.send_gate` (a
   `SendClearance`, implemented by `sendgate.SendGate`) before the never-tokenize check: when every gate of
   6.7 holds, the click skips the never-tokenize list and the dialog allowlist, the flag re-check ignores
   the Send deny rectangle (the hook passes it once for the `send_ok` token) but still refuses Shift and the
   other rectangles, and the decision carries `send_ok`, after which the slow half commits the send to the
   gate (it counts toward the caps and stays pending until declared). Anything else stays the never-tokenize
   attempted send it was. With no gate (the policy disabled) nothing ever carries `send_ok`.
12. Recovery-only arm and disarm (6.6 steps 1 and 5, 6.2 rule 1; 6.6 does not say when deny-all is lifted).
   `HOOK_STATES` names the `(armed, deny_all)` pair of each phase: `running` armed; `kill_switch` deny-all;
   `recovery` re-armed for the 6.6 recovery after the tree is killed and the hook host restarted, with
   `SafetyContext.recovery_only` set so the slow half tokens only `recovery_admits` actions (scripted actions
   of class `login`, which carry the secret steps, and the `cancel_all`, `working_orders_check` and
   `positions_flat_check` teardown actions the proof's reads and the Cancel All need); `between_runs` not
   armed, which the Orchestrator sets at the end of every run and after `revert_snapshot`. Every other rule
   of the slow half still applies inside the recovery arm.
13. Flow-ending results carry the rest of the flow (5.4, 5.4.3, 5.4.8 rule 3): when a drop or refusal ends a
   scripted flow and the run goes on, `DropLedger.record` adds `rest_of_flow: not_run (step_failed:<step>)`.
   An aborting result does not, because the run's unreached outcome covers those checkpoints (1.10).
14. A `hook_host_dead` refusal is routed through `watchdog.hook_host_died` with the shard's budgets (5.4.2,
   6.2, 10.2): deny-all, kill the tree, restart the hook host once per shard and relaunch, or the second time
   `aborted_harness (reason: hook_lost)`. The closed action list of results.py has no `hook_host_died`
   action, so `DropLedger.record` returns the Watchdog's own result and refuses to run without the budgets.
15. Rule 3 by mode (5.4.8, 5.4.3 stage 4, 5.5): one non-send refusal leaves a scripted step `blocked_by_gateway`
   and ends the flow; in an agent rescue it ends the rescue and the step is `failed`; in an Explorer session the
   tool call is `blocked_by_gateway` and the session goes on until the third.
16. Incident severities (6.12): the per-flow threshold's quarantine is S3 ("drop or refusal count above the per-flow
   threshold"); every result that runs the kill switch is S2 at least ("kill switch tripped with a clean proof"),
   which the proof raises to S1 when it finds an undeclared row (6.6).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import TYPE_CHECKING, Protocol

from . import alerts
from .budgets import ShardBudgets
from .results import ControlResult, FindingSpec, Outcome, SubsetOutcome, safety_abort_outcome

if TYPE_CHECKING:                      # ports imports this module; only the type is needed here
    from .ports import SessionSafety

# -- virtual keys (winuser.h) ------------------------------------------------------------------------------
VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12
VK_ESCAPE, VK_SPACE, VK_PRIOR, VK_NEXT, VK_END, VK_HOME = 0x1B, 0x20, 0x21, 0x22, 0x23, 0x24
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_DELETE = 0x25, 0x26, 0x27, 0x28, 0x2E
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_F1, VK_F24 = 0x70, 0x87
FKEYS = frozenset(range(VK_F1, VK_F24 + 1))
MODIFIER_VK = {VK_SHIFT: "shift", 0xA0: "shift", 0xA1: "shift", VK_CONTROL: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
               VK_MENU: "alt", 0xA4: "alt", 0xA5: "alt", VK_LWIN: "win", VK_RWIN: "win"}
CHORD_MODS = frozenset({"ctrl", "alt", "win"})
NAMED_VK = {"enter": VK_RETURN, "return": VK_RETURN, "space": VK_SPACE, "tab": VK_TAB, "escape": VK_ESCAPE,
            "esc": VK_ESCAPE, "delete": VK_DELETE, "backspace": VK_BACK, "home": VK_HOME, "end": VK_END,
            "pageup": VK_PRIOR, "pagedown": VK_NEXT, "left": VK_LEFT, "up": VK_UP, "right": VK_RIGHT, "down": VK_DOWN,
            **{f"f{i}": VK_F1 + i - 1 for i in range(1, 25)}}

# 5.4.7: dropped under the flag regardless of token (VK_RETURN covers numpad Enter, an extended VK_RETURN)
BLOCKED_VK = frozenset({VK_RETURN, VK_SPACE}) | FKEYS
GUARDED_VK = frozenset({VK_RETURN, VK_SPACE})          # outside the flag: only by explicit declaration
# 2.9, 5.6: the only keys the agent and Explorer `press` tool offers
AGENT_KEYS = frozenset({VK_TAB, VK_ESCAPE, VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN})
# 5.6: the action kinds the agent and Explorer tools resolve to (`click_named`, `scroll`, `press`, `type_fixture`;
# `key_chord` only as Shift+Tab; `drag_named` under profile explorer only)
TOOLSET_KINDS = {"agent": frozenset({"click", "wheel", "press", "key_chord", "type_fixture"}),
                 "explorer": frozenset({"click", "wheel", "press", "key_chord", "type_fixture", "drag"})}
# 5.4.10, 6.2: order geometry, a deny target outside an order-stage step of a scripted flow
ORDER_GEOMETRY = frozenset({"ladder_order_cell", "order_bubble"})

TOLERANCE_PX = 2                 # 5.4.5
TOKEN_EXPIRY_S = 0.5             # 5.4.3 stage 5
DRAG_EXTRA_S = 1.0               # drags expire at hold_ms + 1000
TOKEN_SLOTS = 16                 # tokens[16]
DENY_RECT_SLOTS = 8              # deny_rects[8]
CHORD_SLOTS = 16                 # 16 chord entries
DENY_INFLATE_PX = 12             # 5.4.2
WATCHER_STALE_S = 0.5            # a stamp older than 500 ms is flag_ticket = unknown
HOOK_ALIVE_MAX_S = 1.0           # the stamp must be under 1 s old before every emission (5.4.2)
HALF_CONSUMED_RESET_S = 2.0      # 5.4.5: no up after 2 s -> reset_input_state
PER_FLOW_LIMIT = 3               # 5.4.8 rule 3
PER_RUN_LIMIT = 10

NEVER_TOKENIZE_RE = re.compile(r"^(send|auto send|flatten|blast all|replace|submit)$", re.I)
SEND_LIKE_RE = re.compile(r"send|submit|replace|flatten|blast|auto", re.I)
CONFIRM_AND_SEND = "confirm and send"
SEND_LABEL = "Send"                                   # the only label the gated send token may click (6.7)
# choice 10: the label 6.2 expects on the Order Confirmation dialog (Edit only when Phase 0 observes it); never a default
DISMISS_ALLOWLIST = frozenset({"Delete"})
A9_STAGE, CONFIRM_STAGE = "a3_a8", "a3_a9"           # SD-57: the A9 click itself, every later Confirm and Send
PAPER_ASSERTION_CURRENT_S = 120.0                     # choice 9: the 6.4 two-minute timer (identity.TIMER_S)
# choice 12: (armed, deny_all) per phase; `recovery` also sets SafetyContext.recovery_only
HOOK_STATES = {"running": (True, False), "kill_switch": (False, True), "recovery": (True, False),
               "between_runs": (False, False)}
RECOVERY_TEARDOWN = frozenset({"cancel_all", "working_orders_check", "positions_flat_check"})

# 5.4.4: the hook's codes split into safety and integrity
HOOK_SAFETY = frozenset({"deny_all", "not_armed", "deny_rect", "shift_click_dialog", "dialog_not_ok",
                         "blocked_key_dialog", "blocked_chord_dialog", "foreign_input", "untokened_injected"})
HOOK_INTEGRITY = frozenset({"token_expired", "token_mismatch_coords", "move_outside_allow"})
# 5.4.8 rule 1 and 6.2: a hook drop with one of these codes is an attempted send
HOOK_ATTEMPTED_SEND = frozenset({"deny_rect", "blocked_key_dialog", "blocked_chord_dialog", "shift_click_dialog",
                                 "dialog_not_ok"})
# 5.4.8 rule 2: a gateway_integrity finding and an on-call alert
INTEGRITY_FINDING = frozenset({"untokened_injected", "foreign_input"})
# choice 3: the effect of a trip, never a new count
UNCOUNTED = frozenset({"deny_all", "not_armed"})
# 4.13 gateway_event.reason and the 8.3 gate_reason CHECK: the closed list of hook and slow-half codes
GATEWAY_REASONS = frozenset({"no_token", "token_mismatch_coords", "token_expired", "deny_rect", "shift_click_dialog",
                             "dialog_not_ok", "blocked_key_dialog", "blocked_chord_dialog", "move_outside_allow",
                             "deny_all", "not_armed", "foreign_input", "untokened_injected", "never_tokenize",
                             "dialog_allowlist", "deny_profile", "guarded_key_refused", "hook_host_dead",
                             "hook_reinstalled"})
SLOW_REFUSALS = frozenset({"never_tokenize", "dialog_allowlist", "deny_profile", "guarded_key_refused",
                           "shift_click_dialog", "deny_rect", "hook_host_dead"})
PROFILES = ("scripted", "agent", "explorer")
MODES = PROFILES + ("system",)


class GatewayError(RuntimeError):
    """The gateway cannot do what was asked without breaking its own contract (no slot, too many rects)."""


class Flag(IntEnum):
    """`flag_ticket` of 5.4.2: 0 clear, 1 set, 2 unknown (handled as set)."""

    CLEAR = 0
    SET = 1
    UNKNOWN = 2


@dataclass(frozen=True)
class Rect:
    """A screen rectangle in physical pixels: x, y, width, height."""

    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def inflate(self, m: int) -> "Rect":
        return Rect(self.x - m, self.y - m, self.w + 2 * m, self.h + 2 * m)


@dataclass(frozen=True)
class DenyRect:
    """One `deny_rects` entry: the inflated bounds of a visible send-like control and its label."""

    rect: Rect
    label: str


@dataclass(frozen=True)
class Chord:
    """A chord of the order-hotkey list: modifiers plus one virtual key (5.4.7)."""

    mods: frozenset[str]
    vk: int

    @classmethod
    def parse(cls, text: str) -> "Chord":
        """`Ctrl+Shift+B`, `Alt+F4`, `B`: modifiers by name, the key by name or single character."""
        *mods, key = [p.strip().lower() for p in text.split("+")]
        bad = set(mods) - {"shift", "ctrl", "alt", "win"}
        if bad or not key:
            raise ValueError(f"bad chord {text!r}")
        if key in NAMED_VK:
            vk = NAMED_VK[key]
        elif len(key) == 1 and key.isalnum():
            vk = ord(key.upper())
        else:
            raise ValueError(f"bad chord key {key!r} in {text!r}")
        return cls(frozenset(mods), vk)


@dataclass
class Token:
    """One token slot (5.4.2 tokens[16]); `state` is written by the hook only."""

    id: int
    kind: str                     # click | key | wheel | release
    x: int = 0
    y: int = 0
    button: str | None = None
    vk: int | None = None
    sign: int = 0                 # wheel delta sign
    tol_px: int = TOLERANCE_PX
    expires: float = 0.0
    dialog_ok: bool = False
    secret: bool = False
    send_ok: bool = False         # gated branch only (6.7); never set while the send policy is disabled
    state: str = "armed"          # armed | half_consumed | consumed | dropped | expired
    drop_reason: str | None = None

    @property
    def live(self) -> bool:
        return self.state in ("armed", "half_consumed")


@dataclass(frozen=True)
class MoveAllow:
    """`move_allow`: moves inside the path bounding box are allowed until `until` (a drag, 5.4.3)."""

    until: float
    bbox: Rect


@dataclass
class GatewayState:
    """The shared segment of 5.4.2 as the two halves see it."""

    armed: bool = False
    deny_all: bool = False
    flag: Flag = Flag.UNKNOWN
    watcher_mono_ts: float = float("-inf")
    tokens: dict[int, Token] = field(default_factory=dict)
    move_allow: MoveAllow | None = None
    deny_rects: list[DenyRect] = field(default_factory=list)
    blocked_vk: frozenset[int] = BLOCKED_VK
    chords: list[Chord] = field(default_factory=list)
    hook_host_alive: bool = True
    hook_alive_mono_ts: float = float("-inf")
    send_rect_passed: bool = False
    tokens_issued: int = 0
    _next_id: int = 1

    # -- header ---------------------------------------------------------------------------------------
    def effective_flag(self, now: float) -> Flag:
        """The flag the hook acts on: a Watcher stamp older than 500 ms is `unknown` (5.4.2)."""
        if now - self.watcher_mono_ts > WATCHER_STALE_S:
            return Flag.UNKNOWN
        return self.flag

    def stamp_watcher(self, flag: Flag, now: float) -> None:
        """A Watcher poll: the flag it computed and its stamp (5.4.6)."""
        self.flag = flag
        self.watcher_mono_ts = now

    def write_deny_rects(self, rects: list[DenyRect] | list[tuple[Rect, str]]) -> None:
        """Rewrite `deny_rects` from a Watcher poll, each inflated by 12 px (5.4.2); more than 8 fails closed.
        `Confirm and Send` is never a deny rectangle: it opens the dialog and the slow half guards it (5.4.6)."""
        items = [r if isinstance(r, DenyRect) else DenyRect(r[0], r[1]) for r in rects]
        if any(d.label.strip().lower() == CONFIRM_AND_SEND for d in items):
            raise GatewayError("Confirm and Send is never a deny rectangle (5.4.6)")
        if len(items) > DENY_RECT_SLOTS:
            raise GatewayError(f"{len(items)} send-like controls visible; deny_rects holds {DENY_RECT_SLOTS}")
        self.deny_rects = [DenyRect(d.rect.inflate(DENY_INFLATE_PX), d.label) for d in items]

    def load_chords(self, chords: list[Chord]) -> None:
        """Load the order-hotkey list at `settings_attested` (5.4.7); more than 16 entries fails closed."""
        unique = list(dict.fromkeys(chords))
        if len(unique) > CHORD_SLOTS:
            raise GatewayError(f"{len(unique)} order chords; the segment holds {CHORD_SLOTS}")
        self.chords = unique

    def hook_alive(self, now: float) -> bool:
        """The pre-emission liveness check of 5.4.2: hook host alive and its stamp under 1 s old."""
        return self.hook_host_alive and now - self.hook_alive_mono_ts < HOOK_ALIVE_MAX_S

    # -- stage 5: token write -------------------------------------------------------------------------
    def _slot(self) -> int:
        for tid in [t.id for t in self.tokens.values() if not t.live]:
            del self.tokens[tid]
        if len(self.tokens) >= TOKEN_SLOTS:
            raise GatewayError("all 16 token slots hold live tokens")
        tid = self._next_id
        self._next_id += 1
        self.tokens_issued += 1
        return tid

    def issue_click(self, x: int, y: int, now: float, *, button: str = "left", dialog_ok: bool = False,
                    hold_ms: int | None = None, send_ok: bool = False) -> Token:
        """A click (or a drag's press and release) token; a drag also opens `move_allow` (5.4.3 stage 5).

        A `send_ok` token (the gated branch's single-use send token, 6.7) opens one pass through the Send deny
        rectangle for its own button down; the token is then half consumed, so a second click is `no_token`."""
        expiry = TOKEN_EXPIRY_S if hold_ms is None else hold_ms / 1000 + DRAG_EXTRA_S
        t = Token(self._slot(), "click", x, y, button=button, expires=now + expiry, dialog_ok=dialog_ok,
                  send_ok=send_ok)
        if send_ok:
            self.send_rect_passed = False
        self.tokens[t.id] = t
        return t

    def open_move_allow(self, path: Rect, now: float, hold_ms: int) -> None:
        """The `move_allow` window over a drag's path bounding box, until `hold_ms + 1000` (5.4.5)."""
        self.move_allow = MoveAllow(now + hold_ms / 1000 + DRAG_EXTRA_S, path)

    def issue_key(self, vk: int, now: float, *, secret: bool = False) -> Token:
        t = Token(self._slot(), "key", vk=vk, expires=now + TOKEN_EXPIRY_S, secret=secret)
        self.tokens[t.id] = t
        return t

    def issue_wheel(self, x: int, y: int, sign: int, now: float) -> Token:
        t = Token(self._slot(), "wheel", x, y, sign=1 if sign > 0 else -1, expires=now + TOKEN_EXPIRY_S)
        self.tokens[t.id] = t
        return t

    def issue_release(self, now: float) -> list[Token]:
        """`reset_input_state`: release tokens for Shift, Ctrl, Alt, Win and every mouse button (5.4.10)."""
        out = []
        for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN):
            out.append(Token(self._slot(), "release", vk=vk, expires=now + TOKEN_EXPIRY_S))
            self.tokens[out[-1].id] = out[-1]
        for b in ("left", "right", "middle"):
            out.append(Token(self._slot(), "release", button=b, expires=now + TOKEN_EXPIRY_S))
            self.tokens[out[-1].id] = out[-1]
        return out

    def overdue_half_consumed(self, now: float) -> list[Token]:
        """Tokens pressed but not released 2 s after expiry: the slow half emits `reset_input_state` (5.4.5)."""
        return [t for t in self.tokens.values()
                if t.state == "half_consumed" and now - t.expires >= HALF_CONSUMED_RESET_S]


# -- the fast half ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class InputEvent:
    """One OS input event as the hook sees it.

    `origin` is what the hook can tell from the injected flag and the emitter's tag: `emitter` (the
    gateway's own `SendInput`), `foreign_process` (an injected event from any other process), `console`
    (hardware input, not injected) or `drill` (the fire drill injector, 6.5). `mods` are the modifiers
    held when the event arrives.
    """

    type: str                     # move | button_down | button_up | wheel | key_down | key_up
    x: int = 0
    y: int = 0
    button: str | None = None
    vk: int | None = None
    delta: int = 0
    mods: frozenset[str] = frozenset()
    origin: str = "emitter"

    def __post_init__(self):
        if self.type not in ("move", "button_down", "button_up", "wheel", "key_down", "key_up"):
            raise ValueError(f"event type {self.type!r}")
        if self.origin not in ("emitter", "foreign_process", "console", "drill"):
            raise ValueError(f"event origin {self.origin!r}")


@dataclass(frozen=True)
class HookDecision:
    """The callback's answer: allow or drop with a 5.4.4 reason code; `late_up` for a release after expiry."""

    allowed: bool
    reason: str | None = None
    token_id: int | None = None
    late_up: bool = False

    @property
    def decision(self) -> str:
        """The 8.3 `gate_decision`: `emitted`, `late_up` or `dropped`."""
        if not self.allowed:
            return "dropped"
        return "late_up" if self.late_up else "emitted"


def _drop(reason: str, token: Token | None = None) -> HookDecision:
    if token is not None and reason in ("token_expired", "deny_rect", "dialog_not_ok"):
        token.state = "expired" if reason == "token_expired" else "dropped"
        token.drop_reason = reason
    return HookDecision(False, reason, token.id if token else None)


def _near(t: Token, e: InputEvent) -> bool:
    return abs(t.x - e.x) <= t.tol_px and abs(t.y - e.y) <= t.tol_px


def hook_decide(state: GatewayState, e: InputEvent, now: float) -> HookDecision:
    """The fast half (5.4.4; 6.2 rules 1 to 4). Updates token states in `state` as the hook does."""
    if state.deny_all:                                                       # rule 1
        return _drop("deny_all")
    if not state.armed:
        return _drop("not_armed")
    if e.origin == "console":                                                # rule 4
        return _drop("foreign_input")
    flag = state.effective_flag(now)
    if flag != Flag.CLEAR:                                                   # rule 2, regardless of token
        if e.type == "key_down":
            mods = e.mods | ({MODIFIER_VK[e.vk]} if e.vk in MODIFIER_VK else set())
            if mods & CHORD_MODS or Chord(frozenset(e.mods), e.vk) in state.chords:
                return _drop("blocked_chord_dialog")
            if e.vk in state.blocked_vk:
                return _drop("blocked_key_dialog")
        if e.type == "button_down" and "shift" in e.mods:
            return _drop("shift_click_dialog")
    tokened = e.origin == "emitter"                                          # rule 3
    if e.type == "move":
        ma = state.move_allow
        if ma is not None and now <= ma.until and ma.bbox.contains(e.x, e.y):
            return HookDecision(True)
        if not any(t.live and t.kind != "release" for t in state.tokens.values()):
            return HookDecision(True)
        return _drop("move_outside_allow")
    if e.type == "button_down":
        cands = [t for t in state.tokens.values()
                 if tokened and t.kind == "click" and t.button == e.button and t.state == "armed"]
        if not cands:
            return _drop("no_token")
        near = [t for t in cands if _near(t, e)]
        if not near:
            return _drop("token_mismatch_coords")
        t = near[0]
        if now > t.expires:
            return _drop("token_expired", t)
        for d in state.deny_rects:
            if d.rect.contains(e.x, e.y):
                if t.send_ok and d.label.lower() == "send" and not state.send_rect_passed:
                    state.send_rect_passed = True                            # 6.7: once
                    continue
                return _drop("deny_rect", t)
        if flag != Flag.CLEAR and not t.dialog_ok:
            return _drop("dialog_not_ok", t)
        t.state = "half_consumed"
        return HookDecision(True, token_id=t.id)
    if e.type in ("button_up", "key_up"):
        key = "button" if e.type == "button_up" else "vk"
        val = e.button if e.type == "button_up" else e.vk
        kind = "click" if e.type == "button_up" else "key"
        for t in state.tokens.values():
            if tokened and t.kind == kind and t.state == "half_consumed" and getattr(t, key) == val:
                t.state = "consumed"
                return HookDecision(True, token_id=t.id, late_up=now > t.expires)
        for t in state.tokens.values():
            if tokened and t.kind == "release" and t.state == "armed" and getattr(t, key) == val:
                t.state = "consumed"                                         # a release alone activates nothing
                return HookDecision(True, token_id=t.id)
        return _drop("no_token")
    if e.type == "wheel":
        sign = 1 if e.delta > 0 else -1
        cands = [t for t in state.tokens.values() if tokened and t.kind == "wheel" and t.state == "armed"
                 and t.sign == sign and now <= t.expires]
        if not cands:
            return _drop("no_token")
        near = [t for t in cands if _near(t, e)]
        if not near:
            return _drop("token_mismatch_coords")
        near[0].state = "consumed"
        return HookDecision(True, token_id=near[0].id)
    # key_down
    cands = [t for t in state.tokens.values() if tokened and t.kind == "key" and t.vk == e.vk and t.state == "armed"]
    if not cands:
        return _drop("no_token")
    t = cands[0]
    if now > t.expires:
        return _drop("token_expired", t)
    t.state = "half_consumed"
    return HookDecision(True, token_id=t.id)


# -- the ticket-or-dialog flag ----------------------------------------------------------------------------
class FlagWatcher:
    """`flag_ticket` as the OR of its three sources, set fast and cleared slowly (5.4.6)."""

    def __init__(self) -> None:
        self.flag = Flag.UNKNOWN
        self._clear = 0

    def poll(self, *, enumeration_hit: bool, detector_hit: bool, sticky: bool, failed: bool = False) -> Flag:
        """One 100 ms poll. A failed enumeration or capture makes it `unknown`; two clear polls clear it."""
        if failed:
            self._clear = 0
            self.flag = Flag.UNKNOWN
        elif enumeration_hit or detector_hit or sticky:
            self._clear = 0
            self.flag = Flag.SET
        else:
            self._clear += 1
            if self._clear >= 2:
                self.flag = Flag.CLEAR
        return self.flag


# -- the slow half ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    """One requested action after resolution (5.4.3 stages 1 to 3), ready for the safety stage.

    `target_kind` names what the resolver found under the point when it matters to safety:
    `ladder_order_cell`, `order_bubble`, `splitter`, `detach_handle`, `application_settings`,
    `working_order_action`, or None.
    """

    kind: str                                   # click | dblclick | rclick | drag | wheel | press | key_chord | type_fixture | secret
    profile: str = "scripted"
    point: tuple[int, int] | None = None
    resolved_name: str | None = None
    under_point_text: str | None = None
    keys: tuple[int, ...] = ()                  # vk of press / key_chord / the commit key of type_fixture
    mods: frozenset[str] = frozenset()          # modifiers held with a click (a Shift+click)
    target_kind: str | None = None
    end_kind: str | None = None                 # a drag's end target kind
    declared_guarded: bool = False              # `press: enter` or `commit: enter` in the flow file (5.4.7)
    order_stage_step: bool = False
    flow_class: str | None = None
    teardown_action: str | None = None          # cancel_all | close_dialog | clear_ticket
    ambiguous: bool = False
    a9_check: bool = False                      # the A9 click of the attestation: stage a3_a8 suffices (SD-57)
    account_role: str | None = None             # golden | empty: the account the flow declares (4.7, 6.7)

    def __post_init__(self):
        if self.profile not in PROFILES:
            raise ValueError(f"profile {self.profile!r}")

    @property
    def is_pointer(self) -> bool:
        return self.kind in ("click", "dblclick", "rclick", "drag", "wheel")

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in (self.resolved_name, self.under_point_text) if s and s.strip())


class SendClearance(Protocol):
    """The gated branch's send gate as the slow half consults it (6.7; `sendgate.SendGate` implements it)."""

    def check(self, a: Action, ctx: SafetyContext) -> str | None:
        """None when every 6.7 gate holds for this click on `Send` now; else the first failing gate. No effect."""

    def commit(self, a: Action, ctx: SafetyContext) -> None:
        """The slow half cleared the click with `send_ok`: count it toward the caps and hold it pending."""


@dataclass(frozen=True)
class SafetyContext:
    """What stage 4 reads: the flag and order dialog from a fresh synchronous enumeration (5.4.3).

    `order_stage` is the flow's sticky `order_stage` declaration (5.4.6 source 3): while it is held the
    slow half applies the flag rules even if the enumeration and the detector see nothing yet. `session` and
    `now` are the session's safety state and the time of the check (choice 9); `dialog_foreground` and
    `dialog_read` say the Order Confirmation dialog is the foreground window and the Oracle read its numbers,
    and `send_gate` is the gated branch's gate, None while the send policy is disabled (6.7, choice 11);
    `recovery_only` is the recovery arm of 6.6 (choice 12)."""

    flag: Flag = Flag.CLEAR
    order_dialog_present: bool = False
    deny_rects: tuple[DenyRect, ...] = ()
    chords: tuple[Chord, ...] = ()
    dismiss_allowlist: frozenset[str] = frozenset()   # the loaded gateway/dismiss_allowlist.yaml (choice 10)
    attestation_passed: bool = False            # read only without `session` (choice 9)
    paper_assertion_current: bool = False       # read only without `session` (choice 9)
    deny_list_hit: str | None = None            # the Explorer deny list entry the target matches (explorer.py)
    hook_alive: bool = True
    order_stage: bool = False                   # the sticky order_stage declaration is held (5.4.6 source 3)
    session: SessionSafety | None = None
    now: float | None = None                    # monotonic seconds, the clock of `session.last_assertion_mono`
    dialog_foreground: bool = False
    dialog_read: bool = False
    send_gate: SendClearance | None = None
    recovery_only: bool = False

    def attested(self, stage: str) -> bool:
        """The session's attestation reached `stage` and the session was not kicked (SD-57, choice 9)."""
        if self.session is None:
            return self.attestation_passed
        return not self.session.session_kicked and self.session.attested_through(stage)

    def assertion_age(self) -> float | None:
        """Seconds since the last paper-mode assertion; None when there is none or no clock (choice 9)."""
        if self.session is None or self.session.last_assertion_mono is None or self.now is None:
            return None
        age = self.now - self.session.last_assertion_mono
        return age if age >= 0 else None

    def assertion_current(self) -> bool:
        """The paper-mode assertion is current: no older than the 6.4 two-minute timer (choice 9)."""
        if self.session is None:
            return self.paper_assertion_current
        age = self.assertion_age()
        return age is not None and age <= PAPER_ASSERTION_CURRENT_S


@dataclass(frozen=True)
class SlowDecision:
    """Stage 4's answer: refused with a reason, or cleared with `dialog_ok` for the click token.

    `flag_abort` marks the refusal of an agent or Explorer action while the flag is set or unknown (5.4.6):
    the caller takes `agent_flag_abort` or `explorer.ExplorerSession.order_stage` for it, not a plain count.
    `send_ok` is the gated branch's single-use send token (6.7): the emitter passes it to `issue_click`."""

    refused: bool
    reason: str | None = None
    dialog_ok: bool = False
    detail: str | None = None
    attempted_send: bool = False
    unknown_dialog: bool = False
    flag_abort: bool = False
    send_ok: bool = False

    def __post_init__(self):
        if self.refused and self.reason not in SLOW_REFUSALS:
            raise ValueError(f"not a slow-half refusal code: {self.reason!r}")
        if self.send_ok and (self.refused or not self.dialog_ok):
            raise ValueError("send_ok is set only on a cleared click that also carries dialog_ok (6.7)")

    @property
    def gate_class(self) -> str | None:
        """8.3 `gate_class` of a refusal (choice 6)."""
        if not self.refused:
            return None
        return "safety" if self.attempted_send or self.unknown_dialog else "integrity"


def _refuse(reason: str, detail: str | None = None, *, send: bool = False, unknown: bool = False) -> SlowDecision:
    return SlowDecision(True, reason, detail=detail, attempted_send=send, unknown_dialog=unknown)


def _in_deny_rect(point: tuple[int, int] | None, rects: tuple[DenyRect, ...]) -> DenyRect | None:
    if point is None:
        return None
    return next((d for d in rects if d.rect.contains(*point)), None)


def _flag_rules(a: Action, ctx: SafetyContext, *, send_ok: bool = False) -> SlowDecision | None:
    """The flag re-check's refusals (the hook's rule 2 applied in stage 4): a guarded key or blocked chord, a
    Shift+click and a click in a deny rectangle. Each is an attempted send (5.4.3 stage 4, 6.2). A click the
    send gate cleared passes the Send rectangle, as its `send_ok` token does in the hook (6.7, choice 11)."""
    if a.keys and (a.mods & CHORD_MODS or any(Chord(a.mods, k) in ctx.chords or k in BLOCKED_VK for k in a.keys)):
        return _refuse("guarded_key_refused", "guarded key or blocked chord under the flag", send=True)
    if a.is_pointer:
        if "shift" in a.mods:
            return _refuse("shift_click_dialog", send=True)
        rects = tuple(d for d in ctx.deny_rects if not (send_ok and d.label == SEND_LABEL))
        hit = _in_deny_rect(a.point, rects)
        if hit is not None:
            return _refuse("deny_rect", f"point in the deny rectangle of {hit.label!r}", send=True)
    return None


def recovery_admits(a: Action) -> bool:
    """What the recovery-only arm tokens (choice 12): the scripted login of class `login` and the recovery
    flow's `cancel_all`, `working_orders_check` and `positions_flat_check` teardown actions (6.6 step 5)."""
    return a.profile == "scripted" and (a.flow_class == "login" or a.teardown_action in RECOVERY_TEARDOWN)


def _confirm_refusal(a: Action, ctx: SafetyContext) -> SlowDecision | None:
    """`Confirm and Send` (6.2 Label rules, 5.4.3 stage 4, SD-57): scripted only, never with an empty dismiss
    allowlist (choice 10), only from an order-stage step, with the attestation stage the click needs and a
    current paper-mode assertion (choice 9)."""
    if a.profile != "scripted":
        return _refuse("never_tokenize", "Confirm and Send under profile " + a.profile, send=True)
    if not ctx.dismiss_allowlist:
        return _refuse("deny_profile", "Confirm and Send with an empty dismiss allowlist: no dialog click could be "
                                       "tokened until Phase 0 pins gateway/dismiss_allowlist.yaml (6.13 item 10)")
    if not a.order_stage_step:
        return _refuse("deny_profile", "Confirm and Send outside an order-stage step")
    stage = A9_STAGE if a.a9_check else CONFIRM_STAGE
    if not ctx.attested(stage):
        return _refuse("deny_profile", f"Confirm and Send before attestation stage {stage} in this session")
    if not ctx.assertion_current():
        return _refuse("deny_profile", "Confirm and Send without a current paper-mode assertion")
    return None


def slow_half(a: Action, ctx: SafetyContext) -> SlowDecision:
    """Stage 4 of 5.4.3 (and the liveness check of stage 6), in the spec's order; first refusal wins."""
    if not ctx.hook_alive:
        return _refuse("hook_host_dead")
    if ctx.recovery_only and not recovery_admits(a):
        return _refuse("deny_profile", "recovery-only arm: only the scripted login and the recovery teardown "
                                       "actions are tokened (6.6)")
    if a.ambiguous:
        return _refuse("deny_profile", "ambiguous name")
    labels = a.labels
    lowered = {s.lower() for s in labels}
    confirm = CONFIRM_AND_SEND in lowered
    pointer = a.is_pointer
    # The flag as stage 4 applies it: set or unknown in the fresh enumeration, or held by the flow's sticky
    # order_stage declaration, which an order-stage step sets before its own events are emitted (6.2: "so
    # Enter is already blocked when the dialog appears"), including the step that clicks Confirm and Send.
    flag_up = ctx.flag != Flag.CLEAR or ctx.order_stage or a.order_stage_step
    # the gated send (choice 11): a click on exactly `Send` asks the send gate before the never-tokenize list
    send_click = pointer and len(labels) == 2 and all(s == SEND_LABEL for s in labels)
    gate_says = ctx.send_gate.check(a, ctx) if send_click and ctx.send_gate is not None else "no send gate"
    send_ok = send_click and gate_says is None
    # never-tokenize (exact list anywhere; the send-like regex outside the Order Confirmation dialog)
    if pointer and not send_ok:
        if any(NEVER_TOKENIZE_RE.match(s) for s in labels):
            return _refuse("never_tokenize", f"label {labels}" + (f"; {gate_says}" if send_click else ""), send=True)
        if confirm:
            hit = _confirm_refusal(a, ctx)
            if hit is not None:
                return hit
        elif not ctx.order_dialog_present and any(SEND_LIKE_RE.search(s) for s in labels):
            return _refuse("never_tokenize", f"send-like label {labels}", send=True)
        if a.profile == "scripted":
            if "cancel all" in lowered and a.teardown_action != "cancel_all":
                return _refuse("deny_profile", "Cancel All outside the cancel_all teardown action")
            if a.target_kind == "working_order_action" and a.flow_class != "paper_send_ok":
                return _refuse("deny_profile", "Working Orders cancel or replace outside paper_send_ok")
        elif a.target_kind == "application_settings":
            return _refuse("deny_profile", "Application Settings under profile " + a.profile)
    # dialog allowlist: while an order dialog is in front only exact dismiss labels are clickable (a cleared
    # send lifts it for its one click, 6.7)
    dialog_ok = send_ok
    if pointer and ctx.order_dialog_present and not send_ok:
        if labels and all(s in ctx.dismiss_allowlist for s in labels) and len(labels) == 2:
            dialog_ok = True
        elif any(SEND_LIKE_RE.search(s) for s in labels) or _in_deny_rect(a.point, ctx.deny_rects):
            return _refuse("dialog_allowlist", f"send-like click in the order dialog on {labels}", send=True)
        else:
            return _refuse("dialog_allowlist", f"label {labels} is neither allowlisted nor send-like", unknown=True)
    # deny profile
    if a.profile in ("agent", "explorer"):
        if flag_up:
            # 5.4.6, 6.8: from the moment the flag is set or unknown no agent or Explorer input reaches the app;
            # a guarded key, a Shift+click or a deny-rectangle click is still the attempted send it is (choice 8)
            return _flag_rules(a, ctx) or SlowDecision(
                True, "deny_profile", detail=f"flag set or unknown in an {a.profile} session: all input dropped",
                flag_abort=True)
        if ctx.deny_list_hit:
            return _refuse("deny_profile", ctx.deny_list_hit)
        if a.kind not in TOOLSET_KINDS[a.profile]:
            return _refuse("deny_profile", f"{a.kind} is not in the {a.profile} toolset (5.6)")
        if a.kind == "drag" and not (
                a.target_kind in ("splitter", "detach_handle") and a.end_kind in ("splitter", "detach_handle")):
            return _refuse("deny_profile", "drag_named resolves only splitters and detach handles")
        shift_tab = a.kind in ("press", "key_chord") and bool(a.keys) and set(a.keys) <= {VK_TAB}
        if a.mods & CHORD_MODS or any(k not in AGENT_KEYS for k in a.keys) or ("shift" in a.mods and not shift_tab):
            return _refuse("deny_profile", "key or modifier outside the agent toolset")
    # order geometry: a ladder order cell or an order bubble at either end of the gesture is deny geometry except
    # in an order-stage step of a scripted flow (5.4.10, 6.2, 6.8)
    geometry = next((k for k in (a.target_kind, a.end_kind) if k in ORDER_GEOMETRY), None)
    if pointer and geometry and (a.profile != "scripted" or not a.order_stage_step):
        return _refuse("deny_profile", f"{geometry} outside an order-stage step of a scripted flow")
    # flag re-check from the fresh enumeration (and the sticky declaration)
    if flag_up:
        hit = _flag_rules(a, ctx, send_ok=send_ok)
        if hit is not None:
            return hit
        if pointer:
            dialog_ok = dialog_ok or not ctx.order_dialog_present
    elif any(k in GUARDED_VK for k in a.keys) and not (a.profile == "scripted" and a.declared_guarded):
        return _refuse("guarded_key_refused", "Enter or Space not declared by the step")
    if send_ok and ctx.send_gate is not None:
        ctx.send_gate.commit(a, ctx)
    return SlowDecision(False, dialog_ok=dialog_ok, send_ok=send_ok)


# -- the drop-log drainer and the thresholds --------------------------------------------------------------
@dataclass(frozen=True)
class GatewayDrop:
    """One `gateway_drop` step event (5.4.8): the logged reason, decision and class."""

    reason: str
    decision: str                 # dropped (hook) | refused (slow half)
    gate_class: str               # safety | integrity
    mode: str = "scripted"
    tag: str | None = None        # `drill` inside the drill's declared raw-injection phase (6.5)
    own_token: bool = False       # the step's own token was dropped (5.4.3 stage 7)
    send: bool | None = None      # a refusal's attempted-send flag; None: decided by the hook code
    unknown_dialog: bool = False  # a `dialog_allowlist` refusal on a label neither allowlisted nor send-like

    def __post_init__(self):
        if self.reason not in GATEWAY_REASONS:
            raise ValueError(f"gateway reason {self.reason!r} is not a 4.13 code")
        if self.mode not in MODES:
            raise ValueError(f"mode {self.mode!r}")
        if self.decision not in ("dropped", "refused"):
            raise ValueError(f"decision {self.decision!r}")

    @property
    def attempted_send(self) -> bool:
        return self.send if self.send is not None else self.reason in HOOK_ATTEMPTED_SEND


def drain(d: HookDecision, e: InputEvent, *, mode: str = "scripted", drill_phase: bool = False) -> GatewayDrop:
    """Turn a hook drop into its logged form (5.4.1, 5.4.4): `no_token` on another process's injected event is
    `untokened_injected`, except inside the drill's declared raw phase, where it is tagged `drill`."""
    if d.allowed:
        raise ValueError("an allowed event is not a drop")
    reason = d.reason or "no_token"
    tag = "drill" if drill_phase and e.origin == "drill" else None
    if reason == "no_token" and e.origin == "foreign_process":
        reason = "untokened_injected"
    if reason in HOOK_SAFETY:
        cls = "safety"
    elif reason in HOOK_INTEGRITY or (reason == "no_token" and e.origin == "emitter"):
        cls = "integrity"            # no_token on an event the slow half believes it tokened
    else:
        cls = "safety"               # no_token on an event the slow half did not issue
    return GatewayDrop(reason, "dropped", cls, mode, tag, own_token=e.origin == "emitter")


def refusal(s: SlowDecision, *, mode: str = "scripted") -> GatewayDrop:
    """A slow-half refusal as a `gateway_drop` step event (decision `refused`)."""
    if not s.refused or s.reason is None:
        raise ValueError("not a refusal")
    return GatewayDrop(s.reason, "refused", s.gate_class or "integrity", mode, send=s.attempted_send,
                       unknown_dialog=s.unknown_dialog)


class DropLedger:
    """The consequences and thresholds of 5.4.8 and 6.2 for one run.

    Counts are kept per flow or session (`unit`) and per run, across retries and targeted re-runs. Drill
    flows (class `canary` with `drill=True`, or drops tagged `drill`) and the H4 self-test are exempt.
    """

    def __init__(self, per_flow: int = PER_FLOW_LIMIT, per_run: int = PER_RUN_LIMIT):
        self.per_flow = per_flow
        self.per_run = per_run
        self.unit_counts: Counter[str] = Counter()
        self.run_count = 0
        self.by_reason: Counter[tuple[str, str]] = Counter()

    def record(self, drop: GatewayDrop, *, unit: str, step_id: str | None = None, order_stage: bool = False,
               exempt: bool = False, probe_answered: bool = True, retried: bool = False,
               budgets: ShardBudgets | None = None) -> ControlResult:
        """Apply 5.4.8 to one drop or refusal. `unit` is the flow id (or the agent or Explorer session id) and
        `step_id` the step it blocked, for `rest_of_flow` (choice 13); `order_stage` is whether an order-stage
        step is in force; `probe_answered` and `retried` decide the single retry of an own-token integrity drop
        (5.4.3 stage 7); `budgets` are the shard's restart budgets, which a `hook_host_dead` refusal needs
        (choice 14)."""
        res = self._record(drop, unit=unit, step_id=step_id, order_stage=order_stage, exempt=exempt,
                           probe_answered=probe_answered, retried=retried, budgets=budgets)
        if res.run is None and res.has("end_flow") and res.step in ("blocked_by_gateway", "failed") \
                and not any(o.selector == "rest_of_flow" for o in res.outcomes):
            rest = SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{step_id or 'unknown'}"))
            res = replace(res, outcomes=res.outcomes + (rest,))
        return res

    def _record(self, drop: GatewayDrop, *, unit: str, step_id: str | None, order_stage: bool, exempt: bool,
                probe_answered: bool, retried: bool, budgets: ShardBudgets | None) -> ControlResult:
        self.by_reason[(drop.mode, drop.reason)] += 1
        ev = {"reason": drop.reason, "decision": drop.decision, "gate_class": drop.gate_class, "mode": drop.mode}
        if exempt or drop.tag == "drill":
            return ControlResult("gateway", True, "running", detail="drill or self-test drop, exempt from counts",
                                 evidence=ev)
        if drop.reason == "hook_host_dead":
            if budgets is None:
                raise ValueError("a hook_host_dead refusal goes through watchdog.hook_host_died: pass the shard's "
                                 "budgets (5.4.2, choice 14)")
            from .watchdog import hook_host_died    # watchdog imports rig, which imports this module
            return hook_host_died(budgets, step_id=step_id)
        if drop.reason in UNCOUNTED:
            return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                                 detail="not counted (choice 3)", evidence=ev)
        if drop.unknown_dialog:
            if order_stage:
                run, un = safety_abort_outcome("running", "unknown_dialog_in_order_stage")
                return ControlResult("gateway", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
                                     actions=("end_flow", "kill_switch"), step="blocked_by_gateway",
                                     step_reason=drop.reason, incident="S2", evidence=ev)
            return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                                 actions=("end_flow", "quarantine_flow"), alert=alerts.flow_quarantined(),
                                 findings=(FindingSpec("unknown_popup", "click requested on an unknown label in an "
                                                       "order dialog", tier=1),), evidence=ev)
        if drop.attempted_send and drop.mode != "explorer":
            # rule 1: one is enough on a scripted flow or an agent rescue inside it
            self.run_count += 1
            run, un = safety_abort_outcome("running", "attempted_send")
            return ControlResult("gateway", False, "running", run=run, unreached=un,
                                 alert=alerts.safety_abort().plus(paged=("flow_owner",)),
                                 actions=("end_flow", "quarantine_flow", "kill_switch"), step="blocked_by_gateway",
                                 step_reason=drop.reason, incident="S2", evidence=ev)
        self.unit_counts[unit] += 1
        self.run_count += 1
        findings = ()
        alert = None
        if drop.reason in INTEGRITY_FINDING:
            findings = (FindingSpec("gateway_integrity", f"{drop.reason} drop: input from outside the gateway"),)
            alert = alerts.harness_error()
        if self.run_count >= self.per_run:
            run, un = safety_abort_outcome("running", "kill_switch")
            return ControlResult("gateway", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
                                 actions=("end_flow", "quarantine_flow", "kill_switch"), step="blocked_by_gateway",
                                 step_reason=drop.reason, findings=findings, incident="S2",
                                 detail=f"{self.run_count} gateway drops or refusals in the run", evidence=ev)
        if drop.attempted_send:
            # an Explorer attempted send: the session ends at once, S3 ticket, counted toward the ten
            return ControlResult("gateway", False, "running", step="failed", step_reason=drop.reason,
                                 actions=("end_session",), alert=alerts.explorer_ticket(), incident="S3", evidence=ev)
        quarantine = self.unit_counts[unit] >= self.per_flow
        own_integrity = drop.gate_class == "integrity" and drop.decision == "dropped" and drop.own_token
        if own_integrity and (retried or not probe_answered):
            # 5.4.3 stage 7: the retry dropped again or the hook did not answer: fail closed (5.4.2)
            return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                                 checkpoint=Outcome("harness_error", "hook_lost"),
                                 actions=("end_flow", "kill_tree", "restart_hook_host")
                                 + (("quarantine_flow",) if quarantine else ()),
                                 alert=alerts.harness_error(), incident="S3" if quarantine else None,
                                 findings=(FindingSpec("gateway_integrity", f"own token dropped {drop.reason} after "
                                                       "the retry or with the hook silent"),), evidence=ev)
        if quarantine:
            if drop.mode == "explorer":
                return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                                     actions=("end_session",), findings=findings, evidence=ev,
                                     detail=f"{self.unit_counts[unit]} denied attempts end the session (6.8)")
            end = "end_session" if drop.mode == "agent" else "end_flow"
            return ControlResult("gateway", False, "running", step="failed", step_reason=drop.reason,
                                 actions=(end, "quarantine_flow"), alert=alert or alerts.flow_quarantined(),
                                 findings=findings, incident="S3",
                                 detail=f"{self.unit_counts[unit]} drops or refusals in {unit}", evidence=ev)
        if drop.reason in INTEGRITY_FINDING:
            return ControlResult("gateway", True, "running", alert=alert, findings=findings, evidence=ev,
                                 detail="nothing reached the app; the run continues")
        if own_integrity:
            return ControlResult("gateway", False, "running", actions=("retry_step",), step_reason=drop.reason,
                                 evidence=ev)
        if drop.mode == "explorer":
            return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                                 evidence=ev, detail=f"denied attempt {self.unit_counts[unit]} of {self.per_flow}")
        if drop.mode == "agent":                    # rule 3: an agent rescue ends at once, its step failed (choice 15)
            return ControlResult("gateway", False, "running", step="failed", step_reason=drop.reason,
                                 actions=("end_session",), evidence=ev)
        return ControlResult("gateway", False, "running", step="blocked_by_gateway", step_reason=drop.reason,
                             actions=("end_flow",), evidence=ev)


def agent_flag_abort(mode: str) -> ControlResult:
    """The flag became set or unknown in an agent session: abort it (step `failed`, `ticket_visible_in_agent_mode`,
    5.4.6). The Explorer case is `explorer.ExplorerSession.order_stage`."""
    if mode != "agent":
        raise ValueError("only agent sessions; the Explorer takes the explorer_order_stage path (6.8)")
    return ControlResult("gateway", False, "running", step="failed", step_reason="ticket_visible_in_agent_mode",
                         actions=("end_session",))
