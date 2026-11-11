"""The Window Watcher: flag sources 1 and 2 of 5.4.6 and the deny rectangles of 5.4.2 (6.2 ticket flag, 6.6, 7.8).

Platform independent. The desktop port enumerates the visible windows of the launched process tree
(`ports.DesktopPort.enumerate_windows`: EnumWindows filtered to the PID set) and grabs the virtual screen
(`ports.DesktopPort.grab`); everything decided over those observations is here.

- `TitleSets`: the title regexes the classifier matches: the order-dialog set (the Order Confirmation dialog),
  the detached Order Entry window and the `order_event` windows (order submitted, rejected or filled). The popup
  sentinel derives them from the popup catalog (`popups.order_dialog_regexes`, 7.8); here they are inputs.
- `classify_windows(windows, titles, now, first_seen)`: the SunAwt windows whose titles match each set, with the
  time each was first seen, so the kill switch reads `order_event_window` and the 30 s age of an Order
  Confirmation dialog (6.6 Triggers) from it.
- `locate_send_controls`: source 1's template search for the send-like controls of the never-tokenize set (Send,
  Auto Send, Flatten, Blast All, Replace) inside an order window; their bounds become deny rectangles.
- `detect_ticket`: source 2, the ticket detector: the send-control templates of the active theme matched on a
  half-scale capture of the foreground window at floor 0.90; it covers the docked Order Entry panel that
  enumeration cannot see, and its send-like matches become deny rectangles too.
- `WindowWatcher.poll(windows, frame, templates, ...)`: one 100 ms poll: the flag through `gateway.FlagWatcher` (set
  fast, cleared after two clear polls, unknown on a failed enumeration or capture) and the deny rectangles, with
  the Active Trader ladder's order columns the caller passes. `poll_into` stamps the flag and writes the
  rectangles into the shared segment (`GatewayState`), which inflates them by 12 px.

`Confirm and Send` is never a deny rectangle (5.4.6): it opens the dialog rather than sending, and the slow half
guards it. A detector match on its template still sets the flag, since a visible ticket is what source 2 detects.

Choices where the spec is ambiguous or silent (reported):

1. Capture failure (5.4.6 "enumeration or capture fails"). The detector runs at 2 Hz on the newest grab the
   caller passes; a missing grab, or one older than two detector periods (`DETECTOR_STALE_S`, 1 s, one missed
   pass), is a capture failure and makes the flag unknown. Between grabs the last detector result stands.
2. Phase 0 titles (5.19, 6.13 item 10). Until the titles are read the detector is the source the flag relies on;
   an enumeration hit on a draft regex still sets the flag, since a hit can only raise it. The consequence that
   matters before Phase 0, an empty dismiss allowlist, is `gateway_config.load_dismiss_allowlist`'s.
3. The foreground window the detector captures is the topmost enumerated window that has bounds (lowest
   `z_order`); without one the whole grab is searched, which can only add matches. An order window without
   bounds is searched over the whole grab for the same reason.
4. Templates are matched in grayscale (`TM_CCOEFF_NORMED`), every match at or above the floor, so two Active
   Trader panels with an Auto Send each give two rectangles. Source 1 searches order windows at full scale with
   the same 0.90 floor, since 5.4.6 gives no other.
5. On a failed enumeration the previous classification and first-seen times are kept (the flag is unknown
   anyway), so an intermittent failure never resets the 30 s age of a dialog.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from .gateway import CONFIRM_AND_SEND, DenyRect, Flag, FlagWatcher, GatewayError, GatewayState, Rect

AWT_CLASSES = frozenset({"SunAwtFrame", "SunAwtDialog", "SunAwtWindow"})        # 5.4.6 source 1
SEND_CONTROL_LABELS = ("Send", "Auto Send", "Flatten", "Blast All", "Replace")   # the never-tokenize controls
TEMPLATE_LABELS = SEND_CONTROL_LABELS + ("Confirm and Send",)                    # what the detector may match
DETECTOR_FLOOR = 0.90
DETECTOR_SCALE = 0.5
DETECTOR_PERIOD_S = 0.5                                                          # 2 Hz
DETECTOR_STALE_S = 2 * DETECTOR_PERIOD_S                                         # choice 1
LADDER_COLUMN = "ladder order column"
MAX_MATCHES = 8                                                                  # deny_rects[8]


@dataclass(frozen=True)
class TitleSets:
    """The title regexes of the order-dialog set, the detached Order Entry window and the order events (7.8)."""

    order_dialog: tuple[re.Pattern[str], ...] = ()
    detached_ticket: tuple[re.Pattern[str], ...] = ()
    order_event: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def compile(cls, *, order_dialog: Iterable[str] = (), detached_ticket: Iterable[str] = (),
                order_event: Iterable[str] = ()) -> TitleSets:
        """Compile catalog `title_regex` strings (Python syntax, inline flags such as `(?i)` allowed)."""
        return cls(tuple(re.compile(r) for r in order_dialog), tuple(re.compile(r) for r in detached_ticket),
                   tuple(re.compile(r) for r in order_event))


@dataclass(frozen=True)
class SendTemplate:
    """One send-control template of the active theme: its label and its full-scale image (5.4.6)."""

    label: str
    image: Any

    def __post_init__(self) -> None:
        if self.label not in TEMPLATE_LABELS:
            raise ValueError(f"template label {self.label!r}; one of {TEMPLATE_LABELS}")


@dataclass(frozen=True)
class SeenWindow:
    """A classified window and the monotonic time it was first seen."""

    window: Any
    first_seen: float

    def age(self, now: float) -> float:
        return max(0.0, now - self.first_seen)


@dataclass(frozen=True)
class WindowClasses:
    """One classification of the enumerated windows (5.4.6, 6.6 Triggers)."""

    order_dialogs: tuple[SeenWindow, ...] = ()
    detached_tickets: tuple[SeenWindow, ...] = ()
    order_events: tuple[SeenWindow, ...] = ()
    first_seen: Mapping[int, float] = field(default_factory=dict)

    @property
    def order_windows(self) -> tuple[SeenWindow, ...]:
        """The windows that set the flag: Order Confirmation dialogs and detached Order Entry windows."""
        return self.order_dialogs + self.detached_tickets

    @property
    def enumeration_hit(self) -> bool:
        """Flag source 1 (5.4.6)."""
        return bool(self.order_windows)

    @property
    def order_event_window(self) -> bool:
        """The kill switch's `order_event_window` trigger (6.6)."""
        return bool(self.order_events)

    def dialog_open_s(self, now: float) -> float:
        """How long the oldest Order Confirmation dialog has been visible: the 30 s trigger's input (6.6)."""
        return max((w.age(now) for w in self.order_dialogs), default=0.0)


def classify_windows(windows: Sequence[Any], titles: TitleSets, now: float,
                     first_seen: Mapping[int, float] | None = None) -> WindowClasses:
    """Match the visible SunAwt windows against `titles`; `first_seen` carries the times from the previous poll
    (keyed by hwnd), and a window seen for the first time is stamped `now`."""
    prev = first_seen or {}
    seen: dict[int, float] = {}
    out: dict[str, list[SeenWindow]] = {"order_dialog": [], "detached_ticket": [], "order_event": []}
    for w in windows:
        if not w.visible or w.cls not in AWT_CLASSES:
            continue
        hits = [k for k in out if any(p.search(w.title or "") for p in getattr(titles, k))]
        if not hits:
            continue
        seen[w.hwnd] = prev.get(w.hwnd, now)
        for k in hits:
            out[k].append(SeenWindow(w, seen[w.hwnd]))
    return WindowClasses(tuple(out["order_dialog"]), tuple(out["detached_ticket"]), tuple(out["order_event"]), seen)


def _gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.ndim == 3 and img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.ndim == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    raise ValueError(f"an image is H x W, H x W x 3 or H x W x 4; got shape {img.shape}")


def _crop(frame: Any, area: tuple[int, int, int, int] | None) -> tuple[np.ndarray, int, int]:
    """The part of a grab under a virtual-screen rect, and the virtual-screen position of its pixel (0, 0)."""
    img = np.asarray(frame.image)
    ox, oy = frame.origin
    if area is None:
        return img, ox, oy
    x, y, w, h = area
    x0, y0 = max(0, x - ox), max(0, y - oy)
    x1, y1 = min(img.shape[1], x - ox + w), min(img.shape[0], y - oy + h)
    if x1 <= x0 or y1 <= y0:
        return img[:0, :0], ox + x0, oy + y0
    return img[y0:y1, x0:x1], ox + x0, oy + y0


def _shrink(img: np.ndarray, step: int) -> np.ndarray:
    """An exact `step` x `step` box average (the image cropped to a multiple of `step` first)."""
    h, w = img.shape[0] - img.shape[0] % step, img.shape[1] - img.shape[1] % step
    if step == 1 or h == 0 or w == 0:
        return img[:h, :w]
    return cv2.resize(img[:h, :w], (w // step, h // step), interpolation=cv2.INTER_AREA)


def _matches(image: np.ndarray, template: np.ndarray, floor: float, scale: float) -> list[tuple[int, int, float]]:
    """Every top-left at or above `floor` (choice 4), in `image` pixels at full scale, best first. At half scale
    the template is shrunk at each of its four pixel phases, so a control at an odd position still matches."""
    step = round(1 / scale)
    if image.size == 0:
        return []
    img, tpl = _shrink(_gray(image), step), _gray(np.asarray(template))
    th, tw = tpl.shape[:2]
    cands: list[tuple[float, int, int]] = []
    for dy in range(step):
        for dx in range(step):
            t = _shrink(tpl[dy:, dx:], step)
            if t.size == 0 or img.shape[0] < t.shape[0] or img.shape[1] < t.shape[1]:
                continue
            res = cv2.matchTemplate(np.ascontiguousarray(img), np.ascontiguousarray(t), cv2.TM_CCOEFF_NORMED)
            res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            ys, xs = np.nonzero(res >= floor)
            cands += [(float(res[y, x]), int(x) * step - dx, int(y) * step - dy) for y, x in zip(ys, xs)]
    out: list[tuple[int, int, float]] = []
    for score, x, y in sorted(cands, reverse=True):
        if len(out) == MAX_MATCHES:
            break
        if all(abs(x - ox) > tw // 2 or abs(y - oy) > th // 2 for ox, oy, _ in out):
            out.append((x, y, score))
    return out


def _found(frame: Any, area: tuple[int, int, int, int] | None, templates: Sequence[SendTemplate], floor: float,
           scale: float) -> list[DenyRect]:
    crop, ox, oy = _crop(frame, area)
    found = []
    for t in templates:
        th, tw = np.asarray(t.image).shape[:2]
        found += [DenyRect(Rect(ox + x, oy + y, tw, th), t.label) for x, y, _ in _matches(crop, t.image, floor, scale)]
    return found


def locate_send_controls(frame: Any, area: tuple[int, int, int, int] | None, templates: Sequence[SendTemplate],
                         floor: float = DETECTOR_FLOOR) -> tuple[DenyRect, ...]:
    """Source 1: the send-like controls inside an order window (`area`, virtual-screen x, y, w, h; None: the whole
    grab), as un-inflated deny rectangles; `Confirm and Send` never is one (5.4.6)."""
    sendlike = [t for t in templates if t.label in SEND_CONTROL_LABELS]
    return tuple(_found(frame, area, sendlike, floor, 1.0))


def detect_ticket(frame: Any, area: tuple[int, int, int, int] | None, templates: Sequence[SendTemplate],
                  floor: float = DETECTOR_FLOOR) -> tuple[bool, tuple[DenyRect, ...]]:
    """Source 2: the send-control templates on a half-scale capture of the foreground window (`area`) at `floor`.
    Returns whether any matched (the flag) and the send-like matches as deny rectangles (5.4.6)."""
    found = _found(frame, area, templates, floor, DETECTOR_SCALE)
    return bool(found), tuple(d for d in found if d.label.lower() != CONFIRM_AND_SEND)


def _foreground(windows: Sequence[Any]) -> tuple[int, int, int, int] | None:
    """The topmost enumerated window with bounds (choice 3)."""
    framed = [w for w in windows if w.visible and getattr(w, "rect", None) is not None]
    ranked = sorted(framed, key=lambda w: z if (z := getattr(w, "z_order", None)) is not None else 1 << 30)
    return ranked[0].rect if ranked else None


def _merge(rects: Iterable[DenyRect]) -> tuple[DenyRect, ...]:
    """One rectangle per control: a source-2 match of a control source 1 already located differs only by the
    half-scale rounding (at most 2 px), so a same-label rectangle within 2 px of a kept one is dropped."""
    kept: list[DenyRect] = []
    for d in rects:
        if not any(k.label == d.label and abs(k.rect.x - d.rect.x) <= 2 and abs(k.rect.y - d.rect.y) <= 2
                   for k in kept):
            kept.append(d)
    return tuple(kept)


class WindowWatcher:
    """Flag sources 1 and 2 and the deny rectangles, one poll every 100 ms (5.4.6, 5.4.2)."""

    def __init__(self, titles: TitleSets, *, floor: float = DETECTOR_FLOOR):
        self.titles = titles
        self.floor = floor
        self.flags = FlagWatcher()
        self.classes = WindowClasses()
        self._frame_ts: float | None = None
        self._detector_hit = False
        self._detected: tuple[DenyRect, ...] = ()

    def poll(self, windows: Sequence[Any] | None, frame: Any | None, templates: Sequence[SendTemplate], *,
             now: float, sticky: bool = False, ladder_columns: Sequence[Rect] = ()) -> tuple[Flag, tuple[DenyRect, ...]]:
        """One poll. `windows` is the enumeration (None: it failed), `frame` the newest grab (`ports.Frame`; None
        or stale: the capture failed), `templates` the active theme's send-control templates, `sticky` the flow's
        `order_stage` declaration (source 3) and `ladder_columns` the Active Trader ladder's order columns.
        Returns the flag and the un-inflated deny rectangles to write."""
        enumerated = windows is not None
        if enumerated:
            self.classes = classify_windows(windows, self.titles, now, self.classes.first_seen)
        captured = frame is not None and now - frame.mono_s <= DETECTOR_STALE_S
        rects: list[DenyRect] = []
        if captured:
            if frame.mono_s != self._frame_ts:
                area = _foreground(windows) if enumerated else None
                self._detector_hit, self._detected = detect_ticket(frame, area, templates, self.floor)
                self._frame_ts = frame.mono_s
            for w in self.classes.order_windows:
                rects += locate_send_controls(frame, getattr(w.window, "rect", None), templates, self.floor)
            rects += self._detected
        rects += [DenyRect(r, LADDER_COLUMN) for r in ladder_columns]
        flag = self.flags.poll(enumeration_hit=enumerated and self.classes.enumeration_hit,
                               detector_hit=captured and self._detector_hit, sticky=sticky,
                               failed=not (enumerated and captured))
        return flag, _merge(rects)

    def poll_into(self, state: GatewayState, windows: Sequence[Any] | None, frame: Any | None,
                  templates: Sequence[SendTemplate], *, now: float, sticky: bool = False,
                  ladder_columns: Sequence[Rect] = ()) -> Flag:
        """`poll`, then stamp `watcher_mono_ts` with the flag and rewrite `deny_rects` (inflated by 12 px). More
        than eight rectangles fail closed: the flag is stamped unknown and the `GatewayError` propagates."""
        flag, rects = self.poll(windows, frame, templates, now=now, sticky=sticky, ladder_columns=ladder_columns)
        try:
            state.write_deny_rects(list(rects))
        except GatewayError:
            state.stamp_watcher(Flag.UNKNOWN, now)
            raise
        state.stamp_watcher(flag, now)
        return flag
