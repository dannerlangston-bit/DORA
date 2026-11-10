"""The 3.12 image policy: 1:1 crops padded 24 px and snapped to the region, tiles with 32 px overlap
for anything longer than the tile edge, a 2x nearest-neighbor copy for text under 12 px.

No image is ever resized by this module or by the API: every crop is cut at 1:1 and every image
block the prompt sends sets `transformations.oversized_image: "error"` (vision docs, read
2026-11-10), so an image the pinned model would downscale is rejected with a 400 (an `unavailable`
item, 3.15) instead of being shown at a size nobody chose. `ReviewerConfig` refuses a tile edge that
does not fit the model's image tier, so this never happens with a valid config.

Choices where the spec is ambiguous (3.12):

- "Snapped to its region" is read as: the padded component box is intersected with the region's
  bounds, so no pixel outside the item's region leaves (3.16 item 7).
- Tiles cover each axis longer than the tile edge with equal steps of (edge - overlap); the last tile
  is aligned to the far edge, so tiles may overlap by more than 32 px but never by less.
- The 2x copy is made of the whole 1:1 crop and tiled by the same rule when it outgrows the edge;
  its labels give the 1:1 coordinates each tile covers, since the 1:1 crop stays the reference.
"""
from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .config import CropPolicy

Box = tuple[int, int, int, int]          # x, y, w, h


@dataclass(frozen=True)
class ImagePart:
    """One image block: PNG bytes plus the label the prompt prints for it."""

    label: str
    role: str                            # base | candidate | heatmap | mock
    png: bytes
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()


def crop_box(component: Box, region: Box, pad: int) -> Box:
    """The component box padded by `pad` px and snapped to the region (3.12)."""
    x, y, w, h = component
    rx, ry, rw, rh = region
    x0, y0 = max(x - pad, rx), max(y - pad, ry)
    x1, y1 = min(x + w + pad, rx + rw), min(y + h + pad, ry + rh)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"component {component} does not intersect region {region}")
    return (x0, y0, x1 - x0, y1 - y0)


def _spans(length: int, edge: int, overlap: int) -> list[tuple[int, int]]:
    if length <= edge:
        return [(0, length)]
    step = edge - overlap
    n = math.ceil((length - overlap) / step)
    starts = [min(i * step, length - edge) for i in range(n)]
    return [(s, edge) for s in starts]


def tile_boxes(width: int, height: int, edge: int, overlap: int) -> list[Box]:
    """Tiles of at most `edge` px per side covering a width x height image, row by row."""
    return [(x, y, w, h) for y, h in _spans(height, edge, overlap) for x, w in _spans(width, edge, overlap)]


def encode_png(a: np.ndarray) -> bytes:
    """Deterministic PNG bytes of an 8-bit gray, RGB or RGBA array (same array, same bytes)."""
    if a.dtype != np.uint8 or a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[2] not in (3, 4)):
        raise ValueError(f"expected an 8-bit gray, RGB or RGBA array, got {a.dtype} {a.shape}")
    if a.shape[0] < 1 or a.shape[1] < 1:
        raise ValueError("empty image")
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(a)).save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def upscale_nearest(a: np.ndarray, k: int) -> np.ndarray:
    """k x nearest-neighbor upscale (3.12 small-text copy)."""
    return np.repeat(np.repeat(a, k, axis=0), k, axis=1)


def image_parts(a: np.ndarray, *, what: str, role: str, policy: CropPolicy, scale: int = 1) -> list[ImagePart]:
    """`a` as one or more tiles, each labelled with the 1:1 crop coordinates it covers."""
    h, w = a.shape[:2]
    boxes = tile_boxes(w, h, policy.max_long_edge_px, policy.tile_overlap_px)
    parts = []
    for i, (x, y, tw, th) in enumerate(boxes, 1):
        label = f"{what}, {tw}x{th} px"
        if len(boxes) > 1:
            label += (f", tile {i} of {len(boxes)} covering x {x // scale}-{(x + tw) // scale - 1}, "
                      f"y {y // scale}-{(y + th) // scale - 1} of the 1:1 crop")
        parts.append(ImagePart(label, role, encode_png(a[y:y + th, x:x + tw]), tw, th))
    return parts
