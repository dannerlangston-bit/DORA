"""Screenshots for the model and the `zoom` tool (5.6 Screenshots, 5.7).

Capture produces the native 1920x1080 frame, redaction runs, then the frame is downscaled by the fixed factor 1.5 to
1280x720 for the model. The model emits no coordinates, so the only mapping is `zoom`: its rectangle, in screenshot
coordinates, is mapped back to physical pixels by the same factor, cropped from the redacted native capture (never
the unredacted frame) and returned at most 1280 px on the long edge. The factor is verified at the
`canary_flow_passed` gate by zooming on a known template region and matching the template in the returned crop
(`verify_scale`; `RigObservations.zoom_scale_ok` is its result, gates.py).

Choices (reported):
1. Downscaling uses `INTER_AREA` (the resampling that keeps text legible when shrinking); a crop over the 1280 px cap
   is shrunk the same way, keeping its aspect ratio.
2. The mapped rectangle is rounded outward to whole native pixels (floor of the origin, ceiling of the far edge)
   and clipped to the frame, so the crop always contains what the model pointed at.
3. `verify_scale` accepts when the template's best `TM_CCOEFF_NORMED` score in the crop reaches 0.92, the image
   anchor floor of 5.5, and the match lies where the mapping predicts within 2 native px.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..diff.engine import match_template

SCALE = 1.5                                        # 5.6: 1920x1080 -> 1280x720
NATIVE = (1920, 1080)
MODEL = (1280, 720)
ZOOM_LONG_EDGE_MAX = 1280                          # 5.6: returned at most 1280 px on the long edge
SCALE_MATCH_FLOOR = 0.92                           # choice 3 (5.5 image anchor floor)
SCALE_MATCH_TOLERANCE_PX = 2                       # choice 3


class ZoomError(ValueError):
    """A zoom request outside the screenshot, or a frame that is not the native capture."""


@dataclass(frozen=True)
class ZoomRect:
    """A rectangle: x, y, width, height."""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0 or self.x < 0 or self.y < 0:
            raise ZoomError(f"rectangle {self} has no area or a negative origin")


def _size(img: Any) -> tuple[int, int]:
    h, w = img.shape[:2]
    return int(w), int(h)


def model_screenshot(native_redacted: np.ndarray) -> np.ndarray:
    """The 1280x720 screenshot the model receives, from the redacted native 1920x1080 frame (5.6, choice 1)."""
    if _size(native_redacted) != NATIVE:
        raise ZoomError(f"the model screenshot is cut from the native {NATIVE[0]}x{NATIVE[1]} capture, got "
                        f"{_size(native_redacted)}")
    return cv2.resize(native_redacted, MODEL, interpolation=cv2.INTER_AREA)


def to_native(rect: ZoomRect) -> ZoomRect:
    """A screenshot rectangle in native physical pixels (factor 1.5, rounded outward, clipped; choice 2)."""
    if rect.x + rect.w > MODEL[0] or rect.y + rect.h > MODEL[1]:
        raise ZoomError(f"zoom region {rect} lies outside the {MODEL[0]}x{MODEL[1]} screenshot")
    x0, y0 = math.floor(rect.x * SCALE), math.floor(rect.y * SCALE)
    x1 = min(NATIVE[0], math.ceil((rect.x + rect.w) * SCALE))
    y1 = min(NATIVE[1], math.ceil((rect.y + rect.h) * SCALE))
    return ZoomRect(x0, y0, x1 - x0, y1 - y0)


def to_model(rect: ZoomRect) -> ZoomRect:
    """A native rectangle in screenshot coordinates (the inverse mapping, rounded outward)."""
    x0, y0 = math.floor(rect.x / SCALE), math.floor(rect.y / SCALE)
    x1 = min(MODEL[0], math.ceil((rect.x + rect.w) / SCALE))
    y1 = min(MODEL[1], math.ceil((rect.y + rect.h) / SCALE))
    return ZoomRect(x0, y0, x1 - x0, y1 - y0)


def zoom(native_redacted: np.ndarray, rect: ZoomRect) -> np.ndarray:
    """The `zoom` tool's result: the native crop of the mapped rectangle, from the redacted native capture, shrunk to
    at most 1280 px on the long edge (5.6, choices 1 and 2)."""
    if _size(native_redacted) != NATIVE:
        raise ZoomError("zoom crops from the redacted native capture only (5.6)")
    n = to_native(rect)
    crop = native_redacted[n.y:n.y + n.h, n.x:n.x + n.w]
    long_edge = max(n.w, n.h)
    if long_edge <= ZOOM_LONG_EDGE_MAX:
        return crop.copy()
    f = ZOOM_LONG_EDGE_MAX / long_edge
    size = (max(1, round(n.w * f)), max(1, round(n.h * f)))
    return cv2.resize(crop, size, interpolation=cv2.INTER_AREA)


def verify_scale(native_redacted: np.ndarray, template: np.ndarray, template_rect: ZoomRect) -> bool:
    """The `canary_flow_passed` scale check (5.6): zoom on the screenshot rectangle covering the template's known
    native rectangle and match the template in the returned crop where the 1.5 mapping puts it (choice 3)."""
    region = to_model(template_rect)
    crop = zoom(native_redacted, region)
    native = to_native(region)
    if crop.shape[:2] != (native.h, native.w):
        return False                          # a shrunk crop cannot hold the template at native scale
    best, _second, at = match_template(crop, template)
    if at is None or best < SCALE_MATCH_FLOOR:
        return False
    want = (template_rect.x - native.x, template_rect.y - native.y)
    return abs(at[0] - want[0]) <= SCALE_MATCH_TOLERANCE_PX and abs(at[1] - want[1]) <= SCALE_MATCH_TOLERANCE_PX
