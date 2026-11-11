"""Capture's window rules and frame mechanics (3.3, 3.4, 3.5, 5.7, 7.8, 7.9), platform independent.

The rig grabs the virtual screen, enumerates windows, samples `WindowFromPoint` and reads DPI awareness through
`orchestrator.ports.DesktopPort`; everything decided over those observations is here:

- `pid_tree(roots, processes)`: the PID set 5.7 filters by (the launcher PID, the main frame's PID, every
  descendant).
- `select_windows(windows, pids, virtual_screen)`: 3.4 rule 1. Visible windows of the PID set whose class starts
  with `SunAwt` are kept and captured; a window of the PID set whose class is not `SunAwtFrame`, `SunAwtDialog` or
  `SunAwtWindow` is listed in `unknown_class` for the `unknown_window_class` log line (5.7); kept windows whose
  rect misses the virtual screen are listed in `offscreen` (INV-09's input, 2.7).
- `choose_target(windows, target, ...)`: 3.4 rule 3. The checkpoint's `target_window` by title regex plus
  template; none found, or a rect outside the virtual screen, is `not_comparable (reason: window_mismatch)`. The
  other windows present come back for the metadata.
- `surface_check(expected, base=..., candidate=...)`: 3.4 rule 3. Active tab or panel headers that do not match the
  expected screen id are `not_comparable (reason: surface_mismatch)` with the side named.
- `occlusion_grid(rect)` and `occlusion(samples, pids)`: the 8 by 8 `WindowFromPoint` grid of 3.4 and 5.7; a
  foreign process's window at any sample point is `occluded(...)`: `env_error (gate: running, reason: occluded)`
  with pid and title, a `foreign_window` step event (`foreign_window_event`), and one on-call alert and one
  `hardening` finding per run per foreign image name (`ForeignWindowLog`, 5.7, 7.8 POP-19).
- `modal_blocking(windows, main_hwnd=..., target_hwnd=...)`: 3.4 rule 4. `IsWindowEnabled(main)` false means a
  modal is up; when it is not the expected target window its title is the settle loop's `modal_title`
  (`diff.settle.Frame`), which ends `not_settled (reason: unexpected_window:<title>)` if the popup policy has not
  cleared it by `max_wait`.
- `dpi_self_test(value)`: 3.4 rule 5, 10.8. Anything but Per-Monitor-V2 ends the run `aborted_harness (reason:
  dpi_awareness)` before any capture.
- `crop_windows(frame, windows)` and `composite(...)`, `composite_jpeg(...)`: 3.4 rule 2, 5.7 Grab. One grab per
  frame; each window is a crop at its DWM extended frame bounds; the composite is the whole frame with the window
  rects drawn and titled, for the report only (never diffed, never sent to the Reviewer).
- `masked_live_offsets(params)` and `masked_live_median(frames, times_ms, ...)`: 7.9 and 3.5. At `masked_live`
  checkpoints Capture takes 3 to 5 frames over 1 to 2 s; their per-pixel median equals the settled frame outside
  masks. The frame count and spacing are `settle.masked_live` in `config/diff_engine.yaml`.
- `capture_meta(...)`, `meta_to_json`, `meta_from_json` and `put_capture(...)`: the 3.3 and 5.7 metadata stored with
  every capture (`records.CaptureMeta`), written beside the redacted PNG in the artifact store (8.4).

Choices where the spec is ambiguous or silent (reported):

1. Classes (3.4 rule 1 against 5.7 Window enumeration). 3.4 owns the rule: a window is kept when its class starts
   with `SunAwt`. 5.7's "other classes from the same PIDs are captured and logged" is read as: every visible
   window of the PID set outside the three known classes is logged `unknown_window_class`; those starting with
   `SunAwt` are kept and captured, the others are not (3.4 drops them and `CaptureMeta` accepts only `SunAwt*`).
2. Target classes. `main` is a `SunAwtFrame` matching the main-window regex of 7.3 (any title when none is
   given); `dialog`, `detached` and `popup` match any kept class, because which AWT class a Swing dialog or a
   detached gadget uses is a Phase 0 read (5.19). Several matches: the topmost (lowest `z_order`) wins. A
   window whose bounds could not be read cannot be cropped and counts as outside the virtual screen.
3. The template of 3.4 rule 3 is optional here: given with the frame, a candidate is kept only when the template
   matches inside its crop at the landmark floor (0.92, 3.7); without a frame or template the title decides.
4. Headers (3.4 rule 3). The expected screen id becomes header levels (`header_levels`): one level per surface
   of the chain the caller passes (for example the tab, then the panel), each the surface's `title` plus its
   `aliases`. A side matches when every level has one of its labels among the headers read on that side
   (case and runs of white space ignored). A side whose headers were not read (None) is not judged.
5. Occlusion grid. The 64 sample points are the centres of an 8 by 8 split of the target rect. A sample whose
   owner pid is unknown (the read failed) is not a foreign window. The `hardening` finding is raised with the
   alert, once per run per foreign image name; the `foreign_window` event is written for every occurrence. The
   gate defaults to `running`; POP-19 allows the gate in force before `running`, which the caller passes.
6. Modal (3.4 rule 4). The modal is the enabled kept window owned by the main frame, else the topmost enabled
   kept window other than the main frame; it is expected when it is the target window. A modal that cannot be
   identified, or has an empty title, is reported as `untitled`.
7. DPI (3.4 rule 5). The port reports the awareness by name (`PER_MONITOR_AWARE_V2` expected, 7.1 H3); case,
   hyphens and spaces are ignored. The self-test runs at gate `provisioned`, before any capture (10.8).
8. Crops. A window partly off the frame is cropped to the part on the frame; a window wholly off it, or without
   bounds, has no crop. The composite pastes each window's redacted crop over its rect and fills a window with no
   redacted crop with the fill colour, so no unredacted window pixel reaches it, and writes the titles redacted
   as in choice 10; the redaction stage still sweeps the composite before it is stored (the artifact store
   refuses an image without a redaction header).
   Frames follow `ports.Frame` (BGR), so the JPEG is encoded as given.
9. masked_live (7.9, 3.5, 5.7). The frames start at the settle frame (the first of the settled triple) and are
   `settle.masked_live.interval_ms` apart. The parameters are valid when the count is 3 to 5, the span is 1 to
   2 s, and more than half of the frames fall on the settled triple's times: those frames are identical outside
   masks, so the per-pixel median equals the settled frame there whatever the other frames show. The median of
   an even count rounds the mean of the two middle values to the nearest integer.
10. Metadata (3.3, 5.7). The window title is stored after the redaction stage's text patterns
    (`redaction.Sweep`, at least the generic id, email and phone patterns), since a title bar can carry the login
    name (3.6). The metadata JSON is stored as a text artifact of kind `bounds_json` (the capture's window bounds
    and identity) named `<capture name>.meta.json`; 8.4 names no kind for it.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from ..diff import load_params
from ..diff.engine import Terminal, match_template
from ..orchestrator import alerts
from ..orchestrator.ports import Frame, PointOwner, ProcessInfo, Rect, WindowInfo
from ..orchestrator.results import ControlResult, FindingSpec, Outcome, SubsetOutcome, harness_abort_outcome
from ..orchestrator.windows import AWT_CLASSES
from ..records import CaptureMeta, StepEvent
from ..store.artifacts import ArtifactStore, Redaction, StoredArtifact, sha256_hex
from .redaction import Sweep

CLASS_PREFIX = "SunAwt"                         # 3.4 rule 1
MAIN_CLASS = "SunAwtFrame"                      # 7.3: the main window is a SunAwtFrame
TARGET_KINDS = ("main", "dialog", "detached", "popup")   # 4.5, 4.7 target_window.kind
GRID = 8                                        # 3.4: WindowFromPoint on an 8 by 8 grid
TEMPLATE_FLOOR = 0.92                           # 3.7 landmark floor, used for the target template (choice 3)
DPI_EXPECTED = "PER_MONITOR_AWARE_V2"           # 3.4 rule 5, 7.1 H3
MASKED_LIVE_FRAMES = (3, 5)                     # 7.9: 3 to 5 frames
MASKED_LIVE_SPAN_MS = (1000, 2000)              # 7.9: over 1 to 2 seconds
UNTITLED = "untitled"                           # choice 6
META_KIND = "bounds_json"                       # choice 10
OUTLINE_BGR = (0, 255, 255)
TITLE_SCALE = 0.5


class CaptureError(ValueError):
    """A capture input or parameter that breaks a rule of 3.4, 5.7 or 7.9."""


def _rect(w: Any) -> Rect | None:
    """The DWM extended frame bounds of a window observation (None when not read)."""
    return getattr(w, "rect", None)


def _intersects(r: Rect | None, screen: Rect) -> bool:
    if r is None:
        return False
    x, y, w, h = r
    sx, sy, sw, sh = screen
    return w > 0 and h > 0 and x < sx + sw and sx < x + w and y < sy + sh and sy < y + h


def _z(w: Any) -> float:
    z = getattr(w, "z_order", None)
    return math.inf if z is None else float(z)


# -- window selection (3.4 rule 1, 5.7) ---------------------------------------------------------------------------

def pid_tree(roots: Iterable[int], processes: Sequence[ProcessInfo]) -> frozenset[int]:
    """The launcher PID, the main frame's PID and every descendant of either in `processes` (5.7)."""
    pids = set(roots)
    children: dict[int, list[int]] = {}
    for p in processes:
        if p.ppid is not None:
            children.setdefault(p.ppid, []).append(p.pid)
    todo = list(pids)
    while todo:
        for c in children.get(todo.pop(), ()):
            if c not in pids:
                pids.add(c)
                todo.append(c)
    return frozenset(pids)


@dataclass(frozen=True)
class WindowSelection:
    """The windows Capture keeps (3.4 rule 1) and what it logs about the rest (5.7, choice 1)."""

    kept: tuple[WindowInfo, ...]
    unknown_class: tuple[WindowInfo, ...] = ()
    offscreen: tuple[WindowInfo, ...] = ()


def select_windows(windows: Sequence[WindowInfo], pids: Collection[int], virtual_screen: Rect) -> WindowSelection:
    """3.4 rule 1: visible windows of the PID set with a `SunAwt*` class, in z-order (topmost first)."""
    ours = [w for w in windows if w.pid in pids and w.visible]
    kept = tuple(sorted((w for w in ours if w.cls.startswith(CLASS_PREFIX)), key=_z))
    unknown = tuple(w for w in ours if w.cls not in AWT_CLASSES)
    offscreen = tuple(w for w in kept if _rect(w) is not None and not _intersects(_rect(w), virtual_screen))
    return WindowSelection(kept, unknown, offscreen)


# -- target window (3.4 rule 3) -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetWindow:
    """A checkpoint's `target_window` (4.7): kind and, except for `main`, a title regex (L-SRF-04)."""

    kind: str
    title_regex: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise CaptureError(f"target_window.kind {self.kind!r}; one of {TARGET_KINDS}")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TargetWindow:
        return cls(str(d["kind"]), d.get("title_regex"))


@dataclass(frozen=True)
class TargetChoice:
    """The target window, or the `window_mismatch` terminal; `others` are the other kept windows (metadata)."""

    window: WindowInfo | None
    terminal: Terminal | None
    others: tuple[WindowInfo, ...] = ()


def _crop_at(frame: Frame, r: Rect) -> np.ndarray | None:
    img = np.asarray(frame.image)
    ox, oy = frame.origin
    x0, y0 = max(r[0] - ox, 0), max(r[1] - oy, 0)
    x1, y1 = min(r[0] + r[2] - ox, img.shape[1]), min(r[1] + r[3] - oy, img.shape[0])
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def choose_target(windows: Sequence[WindowInfo], target: TargetWindow, *, virtual_screen: Rect,
                  main_title_regex: str | None = None, side: str | None = None,
                  template: np.ndarray | None = None, frame: Frame | None = None,
                  floor: float = TEMPLATE_FLOOR) -> TargetChoice:
    """3.4 rule 3 over the kept windows (`select_windows(...).kept`), choices 2 and 3."""
    main = target.kind == "main"
    rx = re.compile((main_title_regex if main else target.title_regex) or "")
    found = [w for w in windows if (not main or w.cls == MAIN_CLASS) and rx.search(w.title)]
    if template is not None and frame is not None:
        found = [w for w in found if _template_in(frame, _rect(w), template, floor)]
    what = f"{target.kind} window titled /{rx.pattern}/" + (" with its template" if template is not None else "")
    if not found:
        return TargetChoice(None, Terminal("not_comparable", "window_mismatch", side, f"no {what}"), tuple(windows))
    win = min(found, key=_z)
    others = tuple(w for w in windows if w is not win)
    if not _intersects(_rect(win), virtual_screen):
        detail = f"{what} at {_rect(win)} does not intersect the virtual screen {virtual_screen}"
        return TargetChoice(None, Terminal("not_comparable", "window_mismatch", side, detail), others)
    return TargetChoice(win, None, others)


def _template_in(frame: Frame, r: Rect | None, template: np.ndarray, floor: float) -> bool:
    crop = None if r is None else _crop_at(frame, r)
    return crop is not None and match_template(crop, template)[0] >= floor


# -- surface headers (3.4 rule 3) ---------------------------------------------------------------------------------

def _norm(label: str) -> str:
    return " ".join(label.split()).casefold()


def header_levels(surfaces: Sequence[Mapping[str, Any]]) -> tuple[frozenset[str], ...]:
    """The expected header levels of a screen id (choice 4): per surface record, its `title` plus `aliases`."""
    return tuple(frozenset(_norm(t) for t in [s["title"], *(s.get("aliases") or ())]) for s in surfaces)


def _matches(levels: Sequence[Collection[str]], read: Sequence[str]) -> bool:
    seen = {_norm(h) for h in read}
    return all(seen & {_norm(x) for x in level} for level in levels)


def surface_check(expected: Sequence[Collection[str]], *, base: Sequence[str] | None = None,
                  candidate: Sequence[str] | None = None) -> Terminal | None:
    """`not_comparable (reason: surface_mismatch)` naming the side(s) whose headers miss a level; None when both
    sides that were read match (choice 4)."""
    bad = [s for s, read in (("base", base), ("candidate", candidate)) if read is not None and
           not _matches(expected, read)]
    if not bad:
        return None
    side = bad[0] if len(bad) == 1 else "both"
    shown = "; ".join(f"{s} shows {list(base if s == 'base' else candidate or ())}" for s in bad)
    want = " > ".join("/".join(sorted(level)) for level in expected)
    return Terminal("not_comparable", "surface_mismatch", side, f"expected headers {want}; {shown}")


# -- occlusion (3.4 rule 3, 5.7, 7.8) -----------------------------------------------------------------------------

def occlusion_grid(rect: Rect, n: int = GRID) -> tuple[tuple[int, int], ...]:
    """The n x n sample points inside the target rect: the centre of each cell (choice 5)."""
    x, y, w, h = rect
    return tuple((x + int((i + 0.5) * w / n), y + int((j + 0.5) * h / n)) for j in range(n) for i in range(n))


@dataclass(frozen=True)
class Occluder:
    """The foreign process window found at a sample point of the grid."""

    pid: int
    title: str
    hwnd: int | None
    point: tuple[int, int]


def occlusion(samples: Sequence[PointOwner], pids: Collection[int]) -> Occluder | None:
    """The first sample owned by a process outside the PID set, or None (choice 5)."""
    for s in samples:
        if s.pid is not None and s.pid not in pids:
            return Occluder(s.pid, s.title, s.hwnd, (s.x, s.y))
    return None


@dataclass
class ForeignWindowLog:
    """The foreign image names already alerted in this run (5.7: one on-call alert per run per image name)."""

    alerted: set[str] = field(default_factory=set)

    def first(self, image_name: str) -> bool:
        """True the first time `image_name` is seen in the run (case blind), and records it."""
        key = image_name.casefold()
        if key in self.alerted:
            return False
        self.alerted.add(key)
        return True


def occluded(occ: Occluder, *, image_name: str, log: ForeignWindowLog, gate: str = "running") -> ControlResult:
    """`env_error (gate, reason: occluded)` with the foreign window's pid and title for the checkpoint in progress;
    the alert and the `hardening` finding the first time the image name is seen in the run (choice 5)."""
    detail = f"pid {occ.pid}, title {occ.title!r}, image {image_name}, at {occ.point}"
    o = Outcome("env_error", "occluded", gate, detail)
    first = log.first(image_name)
    findings = (FindingSpec("hardening", f"foreign process window over the target: {image_name} "
                                         f"(pid {occ.pid}, title {occ.title!r})", detail=image_name),) if first else ()
    return ControlResult("occlusion", False, gate, checkpoint=o, outcomes=(SubsetOutcome("in_progress", o),),
                         alert=alerts.env_error() if first else None, findings=findings, detail=detail,
                         evidence={"pid": occ.pid, "title": occ.title, "hwnd": occ.hwnd, "point": list(occ.point),
                                   "image_name": image_name, "alerted": first})


def foreign_window_event(occ: Occluder, *, run_id: str, shard_id: str, side: str, seq: int, wall_at: str,
                         mono_ms: int, checkpoint_id: str | None = None, flow_id: str | None = None,
                         step_id: str | None = None, app_uptime_s: int | None = None,
                         app_restart_count: int | None = None) -> StepEvent:
    """The 8.3 `foreign_window` step event of one occlusion (5.7, 7.8): the raw title, the sample point, the pid."""
    return StepEvent(run_id=run_id, shard_id=shard_id, side=side, seq=seq, source="orchestrator", wall_at=wall_at,
                     mono_ms=mono_ms, mode="system", action="foreign_window", flow_id=flow_id, step_id=step_id,
                     checkpoint_id=checkpoint_id, popup_title=occ.title or UNTITLED, screen_x=occ.point[0],
                     screen_y=occ.point[1], reason=f"occluded: pid {occ.pid}", app_uptime_s=app_uptime_s,
                     app_restart_count=app_restart_count)


# -- modal (3.4 rule 4) -------------------------------------------------------------------------------------------

def modal_blocking(windows: Sequence[WindowInfo], *, main_hwnd: int, target_hwnd: int | None = None) -> str | None:
    """The title of an unexpected modal while the main frame is disabled, else None (choice 6). Feed it to the
    settle loop as the frame's `modal_title`."""
    main = next((w for w in windows if w.hwnd == main_hwnd), None)
    if main is None or getattr(main, "enabled", True):
        return None
    up = [w for w in windows if w.hwnd != main_hwnd and getattr(w, "enabled", True)]
    owned = [w for w in up if getattr(w, "owner_hwnd", None) == main_hwnd]
    modal = min(owned or up, key=_z, default=None)
    if modal is not None and modal.hwnd == target_hwnd:
        return None
    return (modal.title.strip() if modal is not None else "") or UNTITLED


# -- DPI self-test (3.4 rule 5, 10.8) -----------------------------------------------------------------------------

def dpi_self_test(value: str | None) -> ControlResult | None:
    """None for Per-Monitor-V2; otherwise `aborted_harness (reason: dpi_awareness)` before any capture (choice 7)."""
    if value is not None and re.sub(r"[\s-]+", "_", value.strip()).upper() == DPI_EXPECTED:
        return None
    run, unreached = harness_abort_outcome("dpi_awareness", f"GetProcessDpiAwareness reported {value!r}")
    return ControlResult("dpi_self_test", False, "provisioned", run=run, unreached=unreached,
                         alert=alerts.aborted_harness(), actions=("teardown",),
                         detail=f"DPI awareness {value!r}, {DPI_EXPECTED} required (3.4 rule 5)")


# -- crops and composite (3.4 rule 2, 5.7 Grab) ---------------------------------------------------------------------

def crop_windows(frame: Frame, windows: Sequence[WindowInfo]) -> dict[int, np.ndarray]:
    """Each window's pixels at its DWM extended frame bounds, by hwnd, from one grab (choice 8)."""
    out: dict[int, np.ndarray] = {}
    for w in windows:
        r = _rect(w)
        crop = None if r is None else _crop_at(frame, r)
        if crop is not None:
            out[w.hwnd] = crop.copy()
    return out


def composite(frame: Frame, windows: Sequence[WindowInfo], redacted: Mapping[int, np.ndarray],
              fill: tuple[int, int, int], sweep: Sweep | None = None) -> np.ndarray:
    """The report composite (5.7): the frame with each window replaced by its redacted crop (filled when it has
    none), its rect drawn and its title, redacted as the metadata's is, written (choices 8, 10)."""
    sweep = sweep or Sweep()
    img = np.array(frame.image, dtype=np.uint8, copy=True)
    ox, oy = frame.origin
    for w in windows:
        r = _rect(w)
        if r is None or _crop_at(frame, r) is None:
            continue
        x0, y0 = max(r[0] - ox, 0), max(r[1] - oy, 0)
        x1, y1 = min(r[0] + r[2] - ox, img.shape[1]), min(r[1] + r[3] - oy, img.shape[0])
        crop = redacted.get(w.hwnd)
        if crop is None:
            img[y0:y1, x0:x1] = np.array(fill, dtype=np.uint8)
        elif crop.shape[:2] != (y1 - y0, x1 - x0):
            raise CaptureError(f"redacted crop of window {w.hwnd} is {crop.shape[:2]}, its rect {(y1 - y0, x1 - x0)}")
        else:
            img[y0:y1, x0:x1] = crop[..., :3]
        cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), OUTLINE_BGR, 2)
        cv2.putText(img, redact_text(w.title or UNTITLED, sweep)[:80], (x0 + 4, y0 + 16), cv2.FONT_HERSHEY_SIMPLEX,
                    TITLE_SCALE, OUTLINE_BGR, 1, cv2.LINE_AA)
    return img


def composite_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    """The composite as JPEG bytes (5.7: composite JPEG, report only)."""
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise CaptureError("JPEG encoding failed")
    return buf.tobytes()


# -- masked_live frames (7.9, 3.5, 5.7) ---------------------------------------------------------------------------

def masked_live_offsets(params: Mapping[str, Any] | None = None) -> tuple[int, ...]:
    """The masked_live frame times in ms after the settle frame, validated against 7.9 (choice 9)."""
    sp = (params or load_params())["settle"]
    ml = sp["masked_live"]
    n, step, settle_step = int(ml["frames"]), int(ml["interval_ms"]), int(sp["frame_interval_ms"])
    offsets = tuple(k * step for k in range(n))
    if not MASKED_LIVE_FRAMES[0] <= n <= MASKED_LIVE_FRAMES[1]:
        raise CaptureError(f"settle.masked_live.frames {n}: 7.9 takes {MASKED_LIVE_FRAMES[0]} to "
                           f"{MASKED_LIVE_FRAMES[1]} frames")
    if not MASKED_LIVE_SPAN_MS[0] <= offsets[-1] <= MASKED_LIVE_SPAN_MS[1]:
        raise CaptureError(f"masked_live frames span {offsets[-1]} ms: 7.9 takes them over 1 to 2 s")
    triple = {k * settle_step for k in range(int(sp["identical_frames"]))}
    if 2 * sum(o in triple for o in offsets) <= n:
        raise CaptureError("fewer than half of the masked_live frames fall on the settled frames: their median "
                           "would not equal the settled frame outside masks (3.5)")
    return offsets


@dataclass(frozen=True)
class MaskedLiveFrames:
    """The masked_live frames of one capture, their times after the settle frame and their per-pixel median."""

    times_ms: tuple[int, ...]
    frames: tuple[np.ndarray, ...]
    median: np.ndarray

    @property
    def span_ms(self) -> int:
        return self.times_ms[-1] - self.times_ms[0]


def masked_live_median(frames: Sequence[np.ndarray], times_ms: Sequence[int], *,
                       ignore: np.ndarray | None = None) -> MaskedLiveFrames:
    """The per-pixel median of 3 to 5 frames over 1 to 2 s, the first being the settled frame (7.9). With `ignore`
    (masks, redactions and the parking rectangle, True where ignored) the median must equal the settled frame
    everywhere else (3.5); a frame set that breaks it is refused."""
    if len(frames) != len(times_ms):
        raise CaptureError("one time per masked_live frame")
    n = len(frames)
    if not MASKED_LIVE_FRAMES[0] <= n <= MASKED_LIVE_FRAMES[1]:
        raise CaptureError(f"{n} masked_live frames; 7.9 takes 3 to 5")
    if any(b <= a for a, b in zip(times_ms, times_ms[1:])):
        raise CaptureError("masked_live frame times must increase")
    span = times_ms[-1] - times_ms[0]
    if not MASKED_LIVE_SPAN_MS[0] <= span <= MASKED_LIVE_SPAN_MS[1]:
        raise CaptureError(f"masked_live frames span {span} ms; 7.9 takes them over 1 to 2 s")
    shape = frames[0].shape
    if any(f.shape != shape for f in frames):
        raise CaptureError("masked_live frames differ in size")
    median = np.rint(np.median(np.stack(frames), axis=0)).astype(np.uint8)
    if ignore is not None and not np.array_equal(median[~ignore], frames[0][~ignore]):
        raise CaptureError("the masked_live median differs from the settled frame outside masks (3.5)")
    return MaskedLiveFrames(tuple(int(t) for t in times_ms), tuple(frames), median)


# -- capture metadata (3.3, 5.7, 8.4) -----------------------------------------------------------------------------

def redact_text(text: str, sweep: Sweep) -> str:
    """`text` with every match of the redaction stage's text patterns replaced by `<redacted:<kind>>` (choice 10).
    All patterns run on the original text; overlapping matches merge into one span named by its widest match, the
    first in the sweep's order on a tie (a name token inside an email is that email, as in the image sweep)."""
    hits = sorted((m.start(), m.end(), order, kind) for order, (kind, rx) in enumerate(sweep.matchers())
                  for m in rx.finditer(text) if m.end() > m.start())
    spans: list[list[Any]] = []                         # [start, end, rank of the naming match, kind]
    for start, end, order, kind in hits:
        rank = (start - end, order)                     # wider first, then sweep order
        if spans and start < spans[-1][1]:
            last = spans[-1]
            last[1] = max(last[1], end)
            if rank < last[2]:
                last[2], last[3] = rank, kind
        else:
            spans.append([start, end, rank, kind])
    out, at = [], 0
    for start, end, _, kind in spans:
        out += [text[at:start], f"<redacted:{kind}>"]
        at = end
    return "".join(out) + text[at:]


def capture_meta(*, sha256: str, run_id: str, checkpoint_id: str, side: str, window: WindowInfo, captured_at: str,
                 sweep: Sweep | None = None, dpi: int | None = None, scale_percent: int | None = None,
                 dpi_awareness: str | None = None, fingerprint_id: str | None = None,
                 app_uptime_s: int | None = None, app_restart_count: int | None = None, mode: str | None = None,
                 action_path: str | None = None, settle_ms: int | None = None, redaction_version: str | None = None,
                 redaction_boxes: int = 0) -> CaptureMeta:
    """The `CaptureMeta` of one capture of `window` (3.3, 5.7): class, redacted title, DWM rect, z-order and monitor
    from the window; the rest as given."""
    r = _rect(window)
    if r is None:
        raise CaptureError(f"window {window.hwnd} has no DWM bounds; nothing was captured")
    return CaptureMeta(sha256=sha256, run_id=run_id, checkpoint_id=checkpoint_id, side=side, window_class=window.cls,
                       title=redact_text(window.title, sweep or Sweep()), rect=tuple(int(v) for v in r),
                       captured_at=captured_at, z_order=getattr(window, "z_order", None),
                       monitor=getattr(window, "monitor", None), dpi=dpi, scale_percent=scale_percent,
                       dpi_awareness=dpi_awareness, fingerprint_id=fingerprint_id, app_uptime_s=app_uptime_s,
                       app_restart_count=app_restart_count, mode=mode, action_path=action_path, settle_ms=settle_ms,
                       redaction_version=redaction_version, redaction_boxes=redaction_boxes)


def meta_to_json(meta: CaptureMeta) -> bytes:
    """Canonical JSON of the metadata (sorted keys, unset fields left out)."""
    return json.dumps(meta.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def meta_from_json(data: bytes) -> CaptureMeta:
    """The `CaptureMeta` back from `meta_to_json` bytes (validated again on construction)."""
    d = json.loads(data.decode("utf-8"))
    d["rect"] = tuple(d["rect"])
    return CaptureMeta(**d)


def put_capture(arts: ArtifactStore, png: bytes, redaction: Redaction, *, name: str,
                meta: CaptureMeta) -> tuple[StoredArtifact, StoredArtifact]:
    """Store a redacted capture and its metadata (8.4): the PNG (kind `capture`) and `<name>.meta.json`. The
    metadata must name the PNG's sha256 and the redaction version of its header."""
    if meta.sha256 != sha256_hex(png):
        raise CaptureError(f"{name}: metadata names {meta.sha256}, the PNG hashes to {sha256_hex(png)}")
    if meta.redaction_version != redaction.version:
        raise CaptureError(f"{name}: metadata redaction version {meta.redaction_version!r}, header "
                           f"{redaction.version!r}")
    image = arts.put(png, name=name, kind="capture", media_type="image/png", redaction=redaction,
                     checkpoint_id=meta.checkpoint_id, side=meta.side, captured_at=meta.captured_at)
    sidecar = arts.put(meta_to_json(meta), name=f"{name}.meta.json", kind=META_KIND, media_type="application/json",
                       redaction=redaction, checkpoint_id=meta.checkpoint_id, side=meta.side,
                       captured_at=meta.captured_at)
    return image, sidecar
