"""The gateway's Win32 emitter: `ports.InputPort` over `SendInput` (5.4.1, 5.4.3 stage 6, 5.4.10, 5.18).

This is the only module of the harness that calls `SendInput` (`sentinel lint --code` allows injection only under
`src/sentinel/gateway/emitter*`, 5.4.9). The executor's emission stage hands it the gateway's tokened events in order;
it converts them all first (`to_inputs`, pure), marshals them into one `INPUT` array tagged with `ffi.EMITTER_TAG`
(`marshal`) and makes one `SendInput` call, so a batch that cannot be converted sends nothing.

Conversion (5.4.3 stage 6, 5.4.4, 5.4.5):

- `move`: an absolute move over the virtual desktop (`MOUSEEVENTF_MOVE | ABSOLUTE | VIRTUALDESK`, plus
  `MOVE_NOCOALESCE` so every interpolated drag move reaches the app), the point normalised to 0..65535 by `normalize`.
- `button_down`, `button_up`: the button flags only, at the cursor the unit's lead move put there. A button event
  never carries `MOUSEEVENTF_MOVE`: with its token pending, the extra move would be dropped `move_outside_allow`.
- `wheel`: `MOUSEEVENTF_WHEEL` with the signed delta (120 per notch) in `mouseData`.
- `key_down`, `key_up`: the virtual key with its scan code (`MapVirtualKeyW`) and `KEYEVENTF_EXTENDEDKEY` for the
  extended keys; `VK_PACKET` events (typed characters, `executor.primitives` choice 1) are `KEYEVENTF_UNICODE` with
  the character in `wScan`, which the hook sees as virtual key `VK_PACKET`, the key the token carries.

Choices (reported):

1. Normalisation: `nx = ceil((x - left) * 65536 / width)`, clamped to 0..65535. Windows maps a normalised coordinate
   back by `nx * width / 65536`, truncated or rounded; for any width up to 32768 both give back `x` exactly, so the
   2 px token tolerance (5.4.5) is never spent on rounding. A point outside the virtual screen is refused: the slow
   half never tokens one (3.4 `window_mismatch`).
2. `InputPort.send(events)` carries no characters, and an `InputEvent` has none: the character of a `VK_PACKET` key
   event is the planner's `Emit.char`. `send` takes them as an optional `chars` sequence parallel to `events`
   (None for every other event); a `VK_PACKET` event without one, a character outside the Basic Multilingual Plane
   (two UTF-16 units would be two key pairs for one token), or a character on any other event is refused.
3. Only events of origin `emitter` are sent, tagged `EMITTER_TAG`; the fire drill injector (`gateway/drill/`, 6.5)
   reuses `to_inputs` and `marshal` with `ffi.DRILL_TAG`.
4. `send` returns how many events the OS accepted and keeps `GetLastError` in `last_error` when it accepted fewer
   (UIPI, a secure desktop, an endpoint control): stage 7 then sees the unconsumed tokens as an integrity drop.

Verified here: the conversion, the normalisation round trip and the `INPUT` marshalling. Only Phase 0 on a rig
verifies that `SendInput` delivers these events with `LLMHF_INJECTED`/`LLKHF_INJECTED` and the tag (H1), the vk the
hook sees for an injected generic modifier (`VK_SHIFT` against `VK_LSHIFT`), and that Swing starts a drag from the
interpolated moves (5.4.3 stage 6, 5.19).
"""
from __future__ import annotations

import ctypes
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..orchestrator.gateway import InputEvent
from ..win32 import ffi

Rect = tuple[int, int, int, int]                     # x, y, w, h

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOVE_FLAGS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE_NOCOALESCE
#: button -> (down flag, up flag, mouseData)
BUTTON_FLAGS = {"left": (0x0002, 0x0004, 0), "right": (0x0008, 0x0010, 0), "middle": (0x0020, 0x0040, 0),
                "x1": (0x0080, 0x0100, 1), "x2": (0x0080, 0x0100, 2)}
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_PACKET = 0xE7
MAPVK_VK_TO_VSC = 0
#: Keys sent with KEYEVENTF_EXTENDEDKEY: PgUp, PgDn, End, Home, the arrows, PrtSc, Insert, Delete, the Win and
#: Apps keys, numpad Divide, Num Lock, right Ctrl and right Alt.
EXTENDED_VK = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F,
                         0x90, 0xA3, 0xA5})
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

PROTOTYPES = {"SendInput": ("user32", ffi.UINT, (ffi.UINT, ctypes.POINTER(ffi.INPUT), ffi.INT))}
NEEDED = ("SendInput", "GetCursorPos", "GetSystemMetrics", "MapVirtualKeyW")


class EmitError(ValueError):
    """A batch the emitter refuses to convert; nothing of it was sent."""


@dataclass(frozen=True)
class InputSpec:
    """One `INPUT` before marshalling: a mouse input (`dx`, `dy`, `data`) or a keyboard input (`vk`, `scan`)."""

    type: int
    flags: int
    dx: int = 0
    dy: int = 0
    data: int = 0
    vk: int = 0
    scan: int = 0


def normalize(x: int, y: int, screen: Rect) -> tuple[int, int]:
    """A virtual-screen pixel as `MOUSEEVENTF_ABSOLUTE | VIRTUALDESK` coordinates (choice 1)."""
    left, top, w, h = screen
    if not (left <= x < left + w and top <= y < top + h):
        raise EmitError(f"point ({x}, {y}) outside the virtual screen {screen}")

    def axis(v: int, origin: int, size: int) -> int:
        return min(65535, -((origin - v) * 65536 // size))     # ceil((v - origin) * 65536 / size)

    return axis(x, left, w), axis(y, top, h)


def to_inputs(events: Sequence[InputEvent], screen: Rect, *, chars: Sequence[str | None] | None = None,
              scan: Callable[[int], int] = lambda vk: 0) -> tuple[InputSpec, ...]:
    """Every event as one `InputSpec`, or `EmitError` for the whole batch (module docstring, choices 2 and 3)."""
    if chars is not None and len(chars) != len(events):
        raise EmitError(f"{len(chars)} characters for {len(events)} events")
    out = []
    for i, e in enumerate(events):
        c = chars[i] if chars is not None else None
        if e.origin != "emitter":
            raise EmitError(f"event {i}: origin {e.origin!r}; the emitter sends only its own events (choice 3)")
        if c is not None and not (e.type in ("key_down", "key_up") and e.vk == VK_PACKET):
            raise EmitError(f"event {i}: a character on a {e.type} that is not a VK_PACKET key event")
        out.append(_spec(i, e, c, screen, scan))
    return tuple(out)


def _spec(i: int, e: InputEvent, c: str | None, screen: Rect, scan: Callable[[int], int]) -> InputSpec:
    if e.type == "move":
        nx, ny = normalize(e.x, e.y, screen)
        return InputSpec(INPUT_MOUSE, MOVE_FLAGS, nx, ny)
    if e.type in ("button_down", "button_up"):
        if e.button not in BUTTON_FLAGS:
            raise EmitError(f"event {i}: button {e.button!r}")
        down, up, data = BUTTON_FLAGS[e.button]
        return InputSpec(INPUT_MOUSE, down if e.type == "button_down" else up, data=data)
    if e.type == "wheel":
        if not e.delta:
            raise EmitError(f"event {i}: a wheel event with no delta")
        return InputSpec(INPUT_MOUSE, MOUSEEVENTF_WHEEL, data=e.delta)
    up = KEYEVENTF_KEYUP if e.type == "key_up" else 0
    if e.vk == VK_PACKET:
        if c is None or len(c) != 1:
            raise EmitError(f"event {i}: a VK_PACKET key event needs exactly one character (choice 2)")
        if ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF:
            raise EmitError(f"event {i}: character U+{ord(c):04X} is outside the Basic Multilingual Plane (choice 2)")
        return InputSpec(INPUT_KEYBOARD, KEYEVENTF_UNICODE | up, scan=ord(c))
    if e.vk is None or not 0 < e.vk < 0xFF:
        raise EmitError(f"event {i}: virtual key {e.vk!r}")
    ext = KEYEVENTF_EXTENDEDKEY if e.vk in EXTENDED_VK else 0
    return InputSpec(INPUT_KEYBOARD, ext | up, vk=e.vk, scan=scan(e.vk) & 0xFFFF)


def marshal(specs: Sequence[InputSpec], tag: int) -> Any:
    """The `INPUT` array for `SendInput`, every input tagged `tag` in `dwExtraInfo` (ffi choice 4)."""
    arr = (ffi.INPUT * len(specs))()
    for inp, s in zip(arr, specs):
        inp.type = s.type
        if s.type == INPUT_MOUSE:
            inp.mi.dx, inp.mi.dy = s.dx, s.dy
            inp.mi.mouseData = s.data & 0xFFFFFFFF
            inp.mi.dwFlags = s.flags
            inp.mi.dwExtraInfo = tag
        else:
            inp.ki.wVk, inp.ki.wScan, inp.ki.dwFlags = s.vk, s.scan, s.flags
            inp.ki.dwExtraInfo = tag
    return arr


class Win32Emitter:
    """`ports.InputPort` over `SendInput` (module docstring). `api` is the function table (`ffi.load` by default,
    Windows only); `screen` gives the virtual screen rectangle, by default from `GetSystemMetrics`."""

    def __init__(self, api: Any = None, *, screen: Callable[[], Rect] | None = None):
        self.api = api if api is not None else ffi.load(NEEDED, PROTOTYPES)
        self.screen = screen or self._virtual_screen
        self.last_error: int | None = None

    def _virtual_screen(self) -> Rect:
        m = self.api.GetSystemMetrics
        return m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN)

    def send(self, events: Sequence[InputEvent], chars: Sequence[str | None] | None = None) -> int:
        """Inject the events in one `SendInput` call; returns how many the OS accepted (choice 4)."""
        if not events:
            return 0
        specs = to_inputs(events, self.screen(), chars=chars,
                          scan=lambda vk: self.api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
        arr = marshal(specs, ffi.EMITTER_TAG)
        sent = int(self.api.SendInput(len(arr), arr, ctypes.sizeof(ffi.INPUT)))
        self.last_error = int(self.api.get_last_error()) if sent < len(arr) else None
        return sent

    def cursor(self) -> tuple[int, int]:
        """`GetCursorPos`, in physical pixels (the harness is PER_MONITOR_AWARE_V2, 5.18)."""
        pt = ffi.POINT()
        if not self.api.GetCursorPos(pt):
            raise ffi.fail(self.api, "GetCursorPos")
        return pt.x, pt.y
