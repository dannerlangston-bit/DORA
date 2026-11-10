"""Canary Service: mutant generation and scoring (10.3, 12.5, 4.11, 5.15; decisions F7, F31, F58).

The catalog (`canaries/mutants.yaml`, a list of 4.11 entries) is loaded with `load_catalog` or
`parse_catalog`. `generate` turns each image entry into a mutant of the approved capture of its
checkpoint, deterministically from the catalog seed 20260101, and records a sha256 manifest that is
checked against the previous night (10.3 Determinism). The mutants are injected by the caller through
`evaluate_checkpoint` with `synthetic: true` (10.3 Injection); `score` takes the outcome the path
produced for each mutant and returns the `canary_results` rows of 8.3, the run's canary state, the
synthetic `harness_error` rows of 4.13 and the morning banner of 10.3.

One recipe per class of the 10.3 class table, each a pure function
``recipe(image, box, rng, **params) -> image`` over an 8-bit BGR frame and an ``(x, y, w, h)`` box from
the Region Registry located by landmark. The money-cell flag comes from the catalog only: money cells
are a closed list (Qty, Price, Buying Power Effect, Max Loss, Send, Cancel, Delete; F58, 3.8), so the
ladder, P/L, balances, BUY and SELL labels and dialog titles are non-money and count toward
`detector_degraded`, M12 among them.

Choices where the spec is ambiguous (reported to the coordinator):

1. Glyph transplant (`swapped_digit`, `header_change`, 10.3): glyphs are columns of ink (pixels more
   than 48 levels from the background sampled on the box's 4 px border); the donor is a different glyph
   of the same row band across the whole frame, chosen by the seeded rng. There is no OCR here, so the
   catalog's `from`/`to` params (M03) are descriptive only, and `header_change` cannot guarantee that
   the new date is exactly one day later; it transplants a different glyph into the header.
2. Parameter ranges the table does not give: `shifted_panel` without `dx`/`dy` moves 8 to 12 px (the
   M08 and M09 values) in an rng-chosen direction; `scrollbar` draws a 12 px track with a thumb of 20 to
   50 percent of the box height; `error_dialog` synthesizes the Java exception dialog with OpenCV
   (no template asset ships with the core); `redaction` plants 9 rng digits unless `text` is given;
   `window_offscreen` moves the frame to `x` (default -200, M30) and fills the uncovered strip with
   black desktop; `study_removed` needs the study's BGR `color` and inpaints it (Telea, radius 3).
   Erase and translate fills use the 4 px border background of 10.3; the value-region recipes (glyphs,
   clipping, rows, placeholder, font height, scrollbar, typed value, cell shift) use the region's most
   common interior color, so a white field stays white around the new text.
3. Region params: `layer` (overlapped_panels, M12), `source` (swapped_label) and `parent`
   (outside_parent) name region ids resolved from the checkpoint's located boxes; `donor_checkpoint`
   (focus_order) names the checkpoint whose approved capture is the after-frame of step n+1. A name
   that cannot be resolved is `generation_failed`, like a missing landmark.
4. `aa_control` (M25, "every bound checkpoint"): an entry whose `checkpoint_id` is ``"*"`` expands to
   one control per checkpoint bound by an image entry of the catalog; a concrete id binds one.
5. Behavioral entries (M26 to M28, M30) run their `canary_flow` on the live rig. `generate` renders
   their image simulation only when the checkpoint has an approved capture (for the CI gate run of
   10.3 and, for `gateway`, the non-matching Send template); otherwise it returns them unrendered and
   never `generation_failed`, since their stimulus is the flow, not an image.
6. Hashes are over the decoded pixels (shape header plus raw bytes), not an encoded file, so they
   do not depend on the PNG encoder. A mutant whose base hash equals last night's but whose mutant
   hash differs is `generation_failed` (nondeterministic generator).
7. Detection is on the final outcome (10.3: "in its expected list"); `invariant_id` and
   `assertion_id` are not matched, and a classification confidence below `expected.min_confidence`
   (4.11) does not count. `collateral` or `needs_human` counts for non-controls when it lands
   on the mutated region: a component whose region is the `region_ref` or whose box meets the mutant's
   footprint, a region above threshold that is the `region_ref`, or the classification's region
   pointer. `gateway` is detected on step status `blocked_by_gateway`; `redaction` on the redaction
   hit (12.5 M19). Borderline Reviewer verdicts must already be re-sampled (3.15) before `score`.
8. Rolling floor: the lower of the median of the class's last 14 nightly rates minus 5 points and
   90 percent; 90 percent with no history; never below 95 percent once Phase 2 exits (read literally,
   the floor is then 95 percent). Tonight's rate is detected over scored non-money mutants of the class,
   with generation failures excluded.
9. Run canary state: the worst of `detector_blind`, `generation_failed`, `detector_degraded`; `missed`
   alone leaves the run `ok`, since the floor tolerates it (8.3 banner states ok, degraded, blind). A
   missed `redaction` mutant (M19) writes `harness_error (reason: safety_canary_blind:redaction)` and
   raises the run to `detector_blind` (same P1 and banner as 10.3 safety canaries), while its own row
   follows its non-money class (the 8.3 CHECK forbids `detector_blind` on a non-money row).
10. Banner: the 10.3 text for one money-cell miss; further misses add "Also missed:" blocks;
    `generation_failed` uses the blind header with the line "canaries did not run" and one line per
    failed mutant; `detector_degraded` lists the classes with rate and floor. Every non-`ok` state
    ends "RC sign-off is blocked." (12.2: any non-ok state blocks RC sign-off).
11. The catalog entry accepts one key beyond 4.11, an optional `description` (the 12.5 Transformation
    text, for example "Price 150.25 -> 150.52"), which the banner prints; `name` is optional because
    10.3's own M12 example omits it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import cv2
import numpy as np
import yaml
from numpy.typing import NDArray

from .records import CheckpointOutcome
from .taxonomy import TaxonomyError, is_checkpoint_id, is_slug, load

Image = NDArray[np.uint8]
Box = tuple[int, int, int, int]                       # x, y, w, h, as records.Component.bbox

SEED = 20260101                                       # 12.5, F7
CLASSES = (                                           # the 26 names of the 10.3 class table (F7, 4.11)
    "erased_control", "swapped_digit", "swapped_label", "color_swap", "shifted_panel", "clipped_label",
    "overlapped_panels", "missing_row", "header_change", "placeholder", "font_height", "scrollbar",
    "redaction", "sub_floor", "global_shift", "landmark_loss", "study_removed", "aa_control",
    "wrong_quantity", "blank_field", "gateway", "outside_parent", "window_offscreen",
    "misaligned_column", "focus_order", "error_dialog",
)
BEHAVIORAL_CLASSES = frozenset({"wrong_quantity", "blank_field", "gateway", "window_offscreen"})
CONTROL_CLASSES = frozenset({"sub_floor", "aa_control"})   # M20, M25: collateral never counts (10.3)
WHOLE_WINDOW_CLASSES = frozenset({"global_shift", "window_offscreen", "focus_order", "aa_control"})
OPS = ("erase", "translate", "replace_glyph", "channel_swap", "crop_right", "overlay", "remove_row", "type_value")
ENTRY_KEYS = frozenset({"mutant_id", "name", "checkpoint_id", "class", "mode", "money_cell", "generator",
                        "region_ref", "canary_flow", "expected", "owner", "description"})
REGION_PARAMS = {"layer": "layer_box", "source": "source_box", "parent": "parent_box"}
DESCRIPTIVE_PARAMS = frozenset({"from", "to"})        # choice 1: no OCR, recorded only
MUTANT_ID = re.compile(r"^M[0-9]{2,}$")

BORDER_PX = 4          # 10.3 erased_control: background sampled from the 4 px border
INK_DELTA = 48         # a channel this far from the background is ink
CHROMA = 40            # max minus min channel above this is a colored pixel (color_swap)
FLOOR_CAP = 0.90       # 10.3 rolling floor
FLOOR_MARGIN = 0.05
FLOOR_TARGET = 0.95    # 9.11 rolling target once Phase 2 exits
FLOOR_WINDOW = 14
STATE_RANK = {"detector_blind": 3, "generation_failed": 2, "detector_degraded": 1}


class CatalogError(ValueError):
    """A catalog entry breaks the 4.11 schema."""


class GenerationError(RuntimeError):
    """A mutant could not be generated: `harness_error (reason: canary_generation_failed)` (10.3)."""


# -- catalog (4.11) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Expected:
    """4.11 `expected`: the outcomes that count as detection, plus optional ids."""
    outcomes: tuple[str, ...]
    min_confidence: float | None = None
    invariant_id: str | None = None
    assertion_id: str | None = None


@dataclass(frozen=True)
class MutantEntry:
    """One entry of `canaries/mutants.yaml` (4.11)."""
    mutant_id: str
    checkpoint_id: str
    defect_class: str
    mode: str
    money_cell: bool
    op: str
    params: Mapping[str, Any]
    expected: Expected
    owner: str
    region_ref: str | None = None
    seed: int | None = None
    name: str | None = None
    canary_flow: str | None = None
    description: str | None = None
    catalog_version: str = ""

    @property
    def label(self) -> str:
        """What the banner prints after the id and class."""
        return self.description or self.name or self.op


@dataclass(frozen=True)
class Catalog:
    """The parsed catalog and its version (the sha256 of the file, recorded per result row, 10.3)."""
    entries: tuple[MutantEntry, ...]
    version: str


def _need(cond: bool, mid: str, msg: str) -> None:
    if not cond:
        raise CatalogError(f"{mid}: {msg}")


def parse_entry(raw: Mapping[str, Any], version: str = "") -> MutantEntry:
    """Validate one 4.11 entry and return it."""
    if not isinstance(raw, Mapping):
        raise CatalogError(f"catalog entry {raw!r} is not a mapping")
    mid = str(raw.get("mutant_id", "?"))
    _need(bool(MUTANT_ID.match(mid)), mid, "mutant_id must match ^M[0-9]{2,}$")
    unknown = set(raw) - ENTRY_KEYS
    _need(not unknown, mid, f"unknown keys {sorted(unknown)}")
    cls = raw.get("class")
    _need(cls in CLASSES, mid, f"class {cls!r} is not one of the 26 classes of 10.3")
    mode = raw.get("mode")
    _need(mode in ("image", "behavioral"), mid, f"mode {mode!r}")
    _need((mode == "behavioral") == (cls in BEHAVIORAL_CLASSES), mid,
          f"class {cls} is {'not ' if mode == 'behavioral' else ''}behavioral (10.3)")
    flow = raw.get("canary_flow")
    _need((flow is not None) == (mode == "behavioral"), mid,
          "canary_flow is required for behavioral and forbidden for image (L-MUT-04)")
    cid = raw.get("checkpoint_id")
    _need(isinstance(cid, str) and (is_checkpoint_id(cid) or (cid == "*" and cls == "aa_control")), mid, f"checkpoint_id {cid!r}")
    _need(isinstance(raw.get("money_cell"), bool), mid, "money_cell must be a bool")
    gen = raw.get("generator")
    _need(isinstance(gen, Mapping) and gen.get("op") in OPS, mid, f"generator.op must be one of {OPS}")
    params = gen.get("params") or {}
    _need(isinstance(params, Mapping), mid, "generator.params must be a map")
    seed = gen.get("seed")
    _need(seed is None or (isinstance(seed, int) and not isinstance(seed, bool)), mid, "generator.seed must be an int")
    region = raw.get("region_ref")
    _need(cls == "aa_control" or isinstance(region, str), mid, "region_ref is required")
    name = raw.get("name")
    _need(name is None or (isinstance(name, str) and is_slug(name)), mid, f"name {name!r} is not a slug")
    owner = raw.get("owner")
    _need(isinstance(owner, str) and bool(owner), mid, "owner is required")
    exp = raw.get("expected")
    _need(isinstance(exp, Mapping) and isinstance(exp.get("outcomes"), list) and bool(exp["outcomes"]), mid,
          "expected.outcomes must be a non-empty list")
    tax = load()
    for o in exp["outcomes"]:
        _need(o in tax.checkpoint_outcomes, mid, f"expected outcome {o!r} is not a checkpoint outcome")
    inv = exp.get("invariant_id")
    _need(inv is None or inv in tax.data["reasons"]["invariant_violation"], mid, f"invariant_id {inv!r} (L-MUT-06)")
    conf = exp.get("min_confidence")
    _need(conf is None or (isinstance(conf, (int, float)) and 0 <= conf <= 1), mid, "min_confidence must be in [0, 1]")
    return MutantEntry(
        mutant_id=mid, checkpoint_id=cid, defect_class=cls, mode=mode, money_cell=raw["money_cell"],
        op=gen["op"], params=dict(params), owner=owner, region_ref=region, seed=seed, name=name,
        canary_flow=flow, description=raw.get("description"), catalog_version=version,
        expected=Expected(tuple(exp["outcomes"]), conf, inv, exp.get("assertion_id")),
    )


def parse_catalog(text: str | bytes) -> Catalog:
    """Parse the YAML list of 4.11 entries; the version is the sha256 of the text."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    version = hashlib.sha256(data).hexdigest()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise CatalogError(f"canaries/mutants.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, list):
        raise CatalogError("canaries/mutants.yaml must be a list of mutant entries (4.11)")
    entries = tuple(parse_entry(r, version) for r in raw)
    ids = [e.mutant_id for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CatalogError(f"duplicate mutant ids {dupes} (L-MUT-01)")
    return Catalog(entries, version)


def load_catalog(path: str | Path) -> Catalog:
    """Read and parse `canaries/mutants.yaml`."""
    return parse_catalog(Path(path).read_bytes())


# -- image helpers ----------------------------------------------------------------------------------

def image_sha256(image: Image) -> str:
    """sha256 of a frame's pixels with its shape (choice 6)."""
    arr = np.ascontiguousarray(image)
    header = f"{'x'.join(str(d) for d in arr.shape)}:{arr.dtype}\n".encode()
    return hashlib.sha256(header + arr.tobytes()).hexdigest()


def _check_image(image: Any) -> Image:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise GenerationError("approved capture must be an 8-bit BGR frame")
    return image


def _check_box(image: Image, box: Sequence[int] | None) -> Box:
    if box is None or len(box) != 4:
        raise GenerationError(f"degenerate target box {box!r}")
    x, y, w, h = (int(v) for v in box)
    height, width = image.shape[:2]
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise GenerationError(f"degenerate target box {tuple(box)} for a {width}x{height} frame")
    return x, y, w, h


def border_background(image: Image, box: Box, width: int = BORDER_PX) -> NDArray[np.uint8]:
    """Median color of the `width` px ring around `box` (10.3 erased_control)."""
    x, y, w, h = box
    height, wd = image.shape[:2]
    x0, y0, x1, y1 = max(0, x - width), max(0, y - width), min(wd, x + w + width), min(height, y + h + width)
    ring = np.ones((y1 - y0, x1 - x0), bool)
    ring[y - y0:y - y0 + h, x - x0:x - x0 + w] = False
    pixels = image[y0:y1, x0:x1][ring]
    if pixels.size == 0:
        raise GenerationError("no border around the target box to sample background from")
    return np.median(pixels, axis=0).round().astype(np.uint8)


def interior_background(crop: Image) -> NDArray[np.uint8]:
    """The most common color inside a value region: the field's own background (choice 2)."""
    packed = (crop[..., 0].astype(np.int32) << 16) | (crop[..., 1].astype(np.int32) << 8) | crop[..., 2]
    values, counts = np.unique(packed.ravel(), return_counts=True)
    v = int(values[int(np.argmax(counts))])
    return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], np.uint8)


def _ink(crop: Image, bg: NDArray[np.uint8]) -> NDArray[np.bool_]:
    return np.abs(crop.astype(np.int16) - bg.astype(np.int16)).max(axis=2) > INK_DELTA


def _runs(flags: NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Start and end (exclusive) of each run of True."""
    d = np.diff(np.concatenate(([0], flags.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _ink_color(crop: Image, bg: NDArray[np.uint8]) -> tuple[int, int, int]:
    ink = _ink(crop, bg)
    if ink.any():
        c = np.median(crop[ink], axis=0).round()
    else:
        c = np.zeros(3) if bg.mean() > 127 else np.full(3, 255)
    return int(c[0]), int(c[1]), int(c[2])


def _with_text(patch: Image, text: str, color: tuple[int, int, int]) -> Image:
    """A copy of `patch` with `text` drawn to fit inside it (Hershey simplex, 8-connected)."""
    out = patch.copy()
    h, w = out.shape[:2]
    if not text:
        return out
    if h < 4 or w < 6:
        raise GenerationError(f"no room for {text!r} in a {w}x{h} box")
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, 1.0, 1)
    scale = min(0.7 * h / (th + base), (w - 4) / tw)
    (tw, th), base = cv2.getTextSize(text, font, scale, 1)
    cv2.putText(out, text, (2, (h + th) // 2), font, scale, color, 1, cv2.LINE_8)
    return out


def _translate_box(image: Image, box: Box, dx: int, dy: int, fill: NDArray[np.uint8]) -> Image:
    """Move the box's pixels by (dx, dy), clipped to the frame; fill the vacated area with `fill`."""
    x, y, w, h = box
    height, width = image.shape[:2]
    out = image.copy()
    patch = image[y:y + h, x:x + w].copy()
    out[y:y + h, x:x + w] = fill
    tx, ty = x + dx, y + dy
    x0, y0, x1, y1 = max(0, tx), max(0, ty), min(width, tx + w), min(height, ty + h)
    if x1 <= x0 or y1 <= y0:
        raise GenerationError("the translated region leaves the frame")
    out[y0:y1, x0:x1] = patch[y0 - ty:y1 - ty, x0 - tx:x1 - tx]
    return out


def _check_fill(fill: str) -> None:
    if fill != "background_sample":
        raise GenerationError(f"unsupported fill {fill!r}; only background_sample (4.11)")


def _direction(rng: np.random.Generator) -> tuple[int, int]:
    return [(1, 0), (-1, 0), (0, 1), (0, -1)][int(rng.integers(4))]


def _box_view(image: Image, box: Box, interior: bool = False) -> tuple[Image, NDArray[np.uint8]]:
    """A copy of the box's pixels and its background: the 4 px border, or the interior's dominant color."""
    x, y, w, h = box
    crop = image[y:y + h, x:x + w].copy()
    return crop, interior_background(crop) if interior else border_background(image, box)


def _paste(image: Image, box: Box, crop: Image) -> Image:
    x, y, w, h = box
    out = image.copy()
    out[y:y + h, x:x + w] = crop
    return out


# -- recipes: one per class of the 10.3 table ----------------------------------------------------

def erase(image: Image, box: Box, rng: np.random.Generator, *, fill: str = "background_sample") -> Image:
    """erased_control (M01, M02, M18) and landmark_loss (M22): fill the box with its border background."""
    _check_fill(fill)
    b = _check_box(image, box)
    crop, bg = _box_view(image, b)
    crop[:] = bg
    return _paste(image, b, crop)


def replace_glyph(image: Image, box: Box, rng: np.random.Generator, *, position: str | int | None = None) -> Image:
    """swapped_digit (M03 to M05), header_change (M14): replace one glyph with another from the same row."""
    x, y, w, h = _check_box(image, box)
    bg = interior_background(image[y:y + h, x:x + w])
    band = image[y:y + h]
    ink = _ink(band, bg)
    extent: dict[tuple[int, int], tuple[int, int]] = {}
    for g in _runs(ink.any(axis=0)):
        rows = np.flatnonzero(ink[:, g[0]:g[1]].any(axis=1))
        if rows[0] > 0 and rows[-1] < h - 1:          # ink cut by the band is not a glyph of this row
            extent[g] = (int(rows[0]), int(rows[-1]) + 1)
    inside = [g for g in extent if g[0] >= x and g[1] <= x + w]
    if not inside:
        raise GenerationError("no glyph inside the target box")
    tallest = max(extent[g][1] - extent[g][0] for g in inside)
    targets = [g for g in inside if extent[g][1] - extent[g][0] >= 0.6 * tallest]   # digits, not punctuation
    if position == "last":
        t = targets[-1]
    elif position == "first":
        t = targets[0]
    elif position is None:
        t = targets[int(rng.integers(len(targets)))]
    else:
        t = targets[int(position)]
    tbit, th = band[:, t[0]:t[1]], extent[t][1] - extent[t][0]
    donors = [g for g, (r0, r1) in extent.items() if g != t and 0.6 * th <= r1 - r0 <= 1.4 * th
              and not (g[1] - g[0] == t[1] - t[0] and np.array_equal(band[:, g[0]:g[1]], tbit))]
    if not donors:
        raise GenerationError("no different glyph in the row to transplant")
    d0, d1 = donors[int(rng.integers(len(donors)))]
    out = image.copy()
    out[y:y + h, t[0]:t[1]] = bg
    left = (t[0] + t[1]) // 2 - (d1 - d0) // 2
    lo, hi = max(x, left), min(x + w, left + d1 - d0)
    out[y:y + h, lo:hi] = band[:, d0 + lo - left:d0 + hi - left]
    return out


def transplant(image: Image, box: Box, rng: np.random.Generator, *, source_box: Box) -> Image:
    """swapped_label (M06): transplant another label of the same dialog (SELL) over the target (BUY)."""
    x, y, w, h = b = _check_box(image, box)
    sx, sy, sw, sh = _check_box(image, source_box)
    patch = image[sy:sy + sh, sx:sx + sw]
    if (sw, sh) != (w, h):
        patch = cv2.resize(patch, (w, h), interpolation=cv2.INTER_AREA)
    return _paste(image, b, patch)


def channel_swap(image: Image, box: Box, rng: np.random.Generator) -> Image:
    """color_swap (M07): swap the red and green channels of the box's colored pixels."""
    b = _check_box(image, box)
    crop, _ = _box_view(image, b)
    c = crop.astype(np.int16)
    colored = (c.max(axis=2) - c.min(axis=2)) > CHROMA
    if not colored.any():
        raise GenerationError("no colored pixels in the target box")
    crop[colored] = crop[colored][:, [0, 2, 1]]
    return _paste(image, b, crop)


def shift_region(image: Image, box: Box, rng: np.random.Generator, *, dx: int | None = None, dy: int | None = None,
                 fill: str = "background_sample") -> Image:
    """shifted_panel (M08, M09): translate the region by (dx, dy); fill the vacated strip (choice 2)."""
    _check_fill(fill)
    b = _check_box(image, box)
    if dx is None and dy is None:
        ux, uy = _direction(rng)
        mag = int(rng.integers(8, 13))
        dx, dy = ux * mag, uy * mag
    dx, dy = int(dx or 0), int(dy or 0)
    if dx == 0 and dy == 0:
        raise GenerationError("shifted_panel needs a non-zero translation")
    return _translate_box(image, b, dx, dy, border_background(image, b))


def clip_label(image: Image, box: Box, rng: np.random.Generator, *, fraction: float | None = None,
               style: str = "crop") -> Image:
    """clipped_label (M10, M11): crop the last 15 to 40 percent of the label, or end it in an ellipsis."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    ink = _ink(crop, bg)
    cols, rows = np.flatnonzero(ink.any(axis=0)), np.flatnonzero(ink.any(axis=1))
    if cols.size == 0:
        raise GenerationError("no label ink in the target box")
    if fraction is None:
        fraction = float(rng.uniform(0.15, 0.40))
    if not 0.15 <= fraction <= 0.40:
        raise GenerationError(f"clip fraction {fraction} outside 15 to 40 percent (10.3)")
    c0, c1 = int(cols[0]), int(cols[-1]) + 1
    cut = c1 - max(1, round(fraction * (c1 - c0)))
    color = _ink_color(crop, bg)
    crop[:, cut:] = bg
    if style == "ellipsis":
        r1 = int(rows[-1]) + 1
        dot = max(1, (r1 - int(rows[0])) // 6)
        for k in range(3):
            left = cut + dot + 2 * dot * k
            cv2.rectangle(crop, (left, r1 - dot), (left + dot - 1, r1 - 1), color, -1)
    elif style != "crop":
        raise GenerationError(f"clipped_label style {style!r}; crop or ellipsis")
    return _paste(image, b, crop)


def overlap_panels(image: Image, box: Box, rng: np.random.Generator, *, layer_box: Box, dx: int | None = None,
                   dy: int | None = None, fill: str = "background_sample") -> Image:
    """overlapped_panels (M12): translate the child panel `layer_box` so it enters the sibling `box` by 4 to 14 px."""
    _check_fill(fill)
    sx, sy, sw, sh = _check_box(image, box)
    lx, ly, lw, lh = lay = _check_box(image, layer_box)
    if dx is None and dy is None:
        gaps = {(0, -1): ly - (sy + sh), (0, 1): sy - (ly + lh), (-1, 0): lx - (sx + sw), (1, 0): sx - (lx + lw)}
        facing = sorted((g, d) for d, g in gaps.items() if g >= 0)
        if not facing:
            raise GenerationError("the layer already intersects its sibling")
        gap, (ux, uy) = facing[0]
        step = gap + int(rng.integers(4, 15))
        dx, dy = ux * step, uy * step
    dx, dy = int(dx or 0), int(dy or 0)
    ix = min(lx + dx + lw, sx + sw) - max(lx + dx, sx)
    iy = min(ly + dy + lh, sy + sh) - max(ly + dy, sy)
    if ix <= 0 or iy <= 0:
        raise GenerationError("the translated layer does not intersect its sibling")
    depth = iy if dx == 0 else ix if dy == 0 else min(ix, iy)
    if not 4 <= depth <= 14:
        raise GenerationError(f"overlap of {depth} px outside 4 to 14 px (10.3)")
    return _translate_box(image, lay, dx, dy, border_background(image, lay))


def remove_row(image: Image, box: Box, rng: np.random.Generator, *, row: int | None = None) -> Image:
    """missing_row (M13): delete one grid row, shift the rows below up, fill the bottom."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    rows = _runs(_ink(crop, bg).any(axis=1))
    if len(rows) < 2:
        raise GenerationError("fewer than two rows in the grid")
    i = int(rng.integers(len(rows))) if row is None else int(row)
    h = crop.shape[0]
    start = rows[i][0]
    end = rows[i + 1][0] if i + 1 < len(rows) else h
    out = crop.copy()
    out[start:h - (end - start)] = crop[end:h]
    out[h - (end - start):] = bg
    return _paste(image, b, out)


def paste_text(image: Image, box: Box, rng: np.random.Generator, *, text: str = "Loading...") -> Image:
    """placeholder (M15): paste "Loading..." over the value region."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    color = _ink_color(crop, bg)
    crop[:] = bg
    return _paste(image, b, _with_text(crop, text, color))


def shrink_row(image: Image, box: Box, rng: np.random.Generator, *, row: int | None = None, cap_height: int = 5) -> Image:
    """font_height (M16): downscale one text row to a 5 px cap height, bottom-aligned on its baseline."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    rows = _runs(_ink(crop, bg).any(axis=1))
    if not rows:
        raise GenerationError("no text row in the target box")
    r0, r1 = rows[int(rng.integers(len(rows))) if row is None else int(row)]
    if r1 - r0 <= cap_height:
        raise GenerationError(f"row is already at or below {cap_height} px")
    cols = np.flatnonzero(_ink(crop[r0:r1], bg).any(axis=0))
    c0, c1 = int(cols[0]), int(cols[-1]) + 1
    sw = max(1, round((c1 - c0) * cap_height / (r1 - r0)))
    small = cv2.resize(crop[r0:r1, c0:c1], (sw, cap_height), interpolation=cv2.INTER_AREA)
    crop[r0:r1, c0:c1] = bg
    crop[r1 - cap_height:r1, c0:c0 + sw] = small
    return _paste(image, b, crop)


def draw_scrollbar(image: Image, box: Box, rng: np.random.Generator, *, width: int = 12) -> Image:
    """scrollbar (M17): draw a scrollbar track and thumb on the right edge of an empty grid (choice 2)."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    h, w = crop.shape[:2]
    if w < width + 4 or h < 16:
        raise GenerationError("grid too small for a scrollbar")
    sign = -1 if bg.mean() > 127 else 1
    track = np.clip(bg.astype(np.int16) + sign * 24, 0, 255).astype(np.uint8)
    thumb = np.clip(bg.astype(np.int16) + sign * 90, 0, 255).astype(np.uint8)
    length = max(8, round(h * float(rng.uniform(0.2, 0.5))))
    top = int(rng.integers(0, h - length + 1))
    crop[:, w - width:] = track
    crop[top:top + length, w - width + 2:w - 2] = thumb
    return _paste(image, b, crop)


def plant_identifier(image: Image, box: Box, rng: np.random.Generator, *, text: str | None = None) -> Image:
    """redaction (M19): leave a synthetic account id unredacted in the input frame (choice 2)."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b)
    ident = text if text is not None else "".join(str(int(d)) for d in rng.integers(0, 10, 9))
    crop[:] = bg
    return _paste(image, b, _with_text(crop, ident, _ink_color(crop, bg)))


def nudge_divider(image: Image, box: Box, rng: np.random.Generator, *, dx: int | None = None, dy: int | None = None,
                  fill: str = "background_sample") -> Image:
    """sub_floor control (M20): shift one divider by 1 px across its length."""
    _check_fill(fill)
    x, y, w, h = b = _check_box(image, box)
    if dx is None and dy is None:
        sign = 1 if rng.integers(2) else -1
        dx, dy = (0, sign) if w >= h else (sign, 0)
    dx, dy = int(dx or 0), int(dy or 0)
    if abs(dx) + abs(dy) != 1:
        raise GenerationError("sub_floor shifts by exactly 1 px")
    return _translate_box(image, b, dx, dy, border_background(image, b))


def global_shift(image: Image, box: Box, rng: np.random.Generator, *, dx: int | None = None, dy: int | None = None,
                 channel: str = "red", delta: int | None = None) -> Image:
    """global_shift (M21, M23): translate the whole window 1 px, or add `delta` (6) to one channel everywhere."""
    _check_image(image)
    if delta is not None:
        idx = {"blue": 0, "green": 1, "red": 2}[channel]
        out = image.copy()
        out[..., idx] = np.clip(image[..., idx].astype(np.int16) + int(delta), 0, 255)
        return out
    dx, dy = (1, 0) if dx is None and dy is None else (int(dx or 0), int(dy or 0))
    if dx == 0 and dy == 0:
        raise GenerationError("global_shift needs a non-zero translation or a channel delta")
    height, width = image.shape[:2]
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(image, m, (width, height), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE)


def remove_study(image: Image, box: Box, rng: np.random.Generator, *, color: Sequence[int], tolerance: int = 40) -> Image:
    """study_removed (M24): erase the study line of BGR `color` from the chart region by inpainting."""
    b = _check_box(image, box)
    crop, _ = _box_view(image, b)
    near = np.abs(crop.astype(np.int16) - np.array(color, np.int16)).max(axis=2) <= tolerance
    if not near.any():
        raise GenerationError(f"no pixels of the study color {list(color)} in the chart region")
    mask = cv2.dilate(near.astype(np.uint8) * 255, np.ones((3, 3), np.uint8))
    return _paste(image, b, cv2.inpaint(crop, mask, 3, cv2.INPAINT_TELEA))


def unchanged(image: Image, box: Box, rng: np.random.Generator) -> Image:
    """aa_control (M25): the approved capture, unchanged; it must end `identical`."""
    return _check_image(image).copy()


def type_value(image: Image, box: Box, rng: np.random.Generator, *, value: str = "10") -> Image:
    """wrong_quantity (M26) simulation: the field shows `value` (10 where the flow expects 100)."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    color = _ink_color(crop, bg)
    crop[:] = bg
    return _paste(image, b, _with_text(crop, value, color))


def blank_value(image: Image, box: Box, rng: np.random.Generator) -> Image:
    """blank_field (M27) simulation: the Price field left blank."""
    return type_value(image, box, rng, value="")


def mismatch_template(image: Image, box: Box, rng: np.random.Generator) -> Image:
    """gateway (M28): the Send control replaced by a non-matching image (mirrored and inverted)."""
    b = _check_box(image, box)
    crop, _ = _box_view(image, b)
    return _paste(image, b, 255 - crop[:, ::-1])


def cross_parent(image: Image, box: Box, rng: np.random.Generator, *, parent_box: Box | None = None,
                 dx: int | None = None, dy: int | None = None, distance: int = 6,
                 fill: str = "background_sample") -> Image:
    """outside_parent (M29): translate the control 6 px toward its nearest panel border so it crosses it."""
    _check_fill(fill)
    x, y, w, h = b = _check_box(image, box)
    if dx is None and dy is None:
        if parent_box is not None:
            px, py, pw, ph = _check_box(image, parent_box)
            room = {(-1, 0): x - px, (1, 0): px + pw - x - w, (0, -1): y - py, (0, 1): py + ph - y - h}
            r, (ux, uy) = min((r, d) for d, r in room.items())
            if r >= distance:
                raise GenerationError(f"a {distance} px move does not cross the panel border")
        else:
            ux, uy = _direction(rng)
        dx, dy = ux * distance, uy * distance
    return _translate_box(image, b, int(dx or 0), int(dy or 0), border_background(image, b))


def move_offscreen(image: Image, box: Box, rng: np.random.Generator, *, x: int = -200) -> Image:
    """window_offscreen (M30) simulation: the window moved to `x` (-200); the uncovered strip is black desktop."""
    _check_image(image)
    width = image.shape[1]
    if not -width < x < 0:
        raise GenerationError(f"x = {x} does not leave the window partly on screen")
    out = np.zeros_like(image)
    out[:, :width + x] = image[:, -x:]
    return out


def shift_within_cell(image: Image, box: Box, rng: np.random.Generator, *, dx: int = 3) -> Image:
    """misaligned_column (M31): shift one numeric cell's content 3 px within its column."""
    b = _check_box(image, box)
    crop, bg = _box_view(image, b, interior=True)
    w = crop.shape[1]
    if dx == 0 or abs(dx) >= w:
        raise GenerationError(f"dx = {dx} for a {w} px cell")
    out = np.empty_like(crop)
    out[:] = bg
    if dx > 0:
        out[:, dx:] = crop[:, :w - dx]
    else:
        out[:, :w + dx] = crop[:, -dx:]
    return _paste(image, b, out)


def swap_frame(image: Image, box: Box, rng: np.random.Generator, *, donor: Image) -> Image:
    """focus_order (M32): the after-frame of Tab step n replaced by that of step n+1 (`donor`)."""
    _check_image(image)
    if not isinstance(donor, np.ndarray) or donor.shape != image.shape or donor.dtype != image.dtype:
        raise GenerationError("the donor after-frame does not match the frame's shape")
    return donor.copy()


def composite_error_dialog(image: Image, box: Box, rng: np.random.Generator, *, title: str = "Error",
                           message: str = "java.lang.NullPointerException") -> Image:
    """error_dialog (M33): composite a Java exception dialog over the body (synthesized, choice 2)."""
    b = _check_box(image, box)
    crop, _ = _box_view(image, b)
    h, w = crop.shape[:2]
    if w < 60 or h < 40:
        raise GenerationError("body too small for the exception dialog")
    dw, dh = min(w, 320), min(h, 140)
    ox, oy = int(rng.integers(0, w - dw + 1)), int(rng.integers(0, h - dh + 1))
    dlg = np.full((dh, dw, 3), 240, np.uint8)
    bar = max(10, dh // 6)
    dlg[:bar] = (160, 90, 30)
    dlg[1:bar - 1, 3:dw // 2] = _with_text(dlg[1:bar - 1, 3:dw // 2], title, (255, 255, 255))
    r = max(3, min(10, (dh - bar) // 5))
    cv2.circle(dlg, (6 + r, bar + 6 + r), r, (0, 0, 210), -1)
    left = 12 + 2 * r
    dlg[bar + 6:bar + 6 + 2 * r, left:dw - 4] = _with_text(dlg[bar + 6:bar + 6 + 2 * r, left:dw - 4], message, (0, 0, 0))
    cv2.rectangle(dlg, (dw // 2 - 15, dh - 16), (dw // 2 + 15, dh - 5), (200, 200, 200), -1)
    cv2.rectangle(dlg, (0, 0), (dw - 1, dh - 1), (90, 90, 90), 1)
    crop[oy:oy + dh, ox:ox + dw] = dlg
    return _paste(image, b, crop)


Recipe = Callable[..., Image]
RECIPES: dict[str, Recipe] = {
    "erased_control": erase, "swapped_digit": replace_glyph, "swapped_label": transplant,
    "color_swap": channel_swap, "shifted_panel": shift_region, "clipped_label": clip_label,
    "overlapped_panels": overlap_panels, "missing_row": remove_row, "header_change": replace_glyph,
    "placeholder": paste_text, "font_height": shrink_row, "scrollbar": draw_scrollbar,
    "redaction": plant_identifier, "sub_floor": nudge_divider, "global_shift": global_shift,
    "landmark_loss": erase, "study_removed": remove_study, "aa_control": unchanged,
    "wrong_quantity": type_value, "blank_field": blank_value, "gateway": mismatch_template,
    "outside_parent": cross_parent, "window_offscreen": move_offscreen,
    "misaligned_column": shift_within_cell, "focus_order": swap_frame, "error_dialog": composite_error_dialog,
}


# -- generation (10.3 Determinism) --------------------------------------------------------------------

@dataclass
class Mutant:
    """One generated mutant (or its generation failure) for one checkpoint."""
    entry: MutantEntry
    checkpoint_id: str
    seed: int
    image: Image | None = None
    base_sha256: str | None = None
    mutant_sha256: str | None = None
    footprint: Box | None = None          # bounding box of the changed pixels
    error: str | None = None              # set: harness_error (reason: canary_generation_failed)

    @property
    def mutant_id(self) -> str:
        return self.entry.mutant_id

    @property
    def defect_class(self) -> str:
        return self.entry.defect_class

    @property
    def money_cell(self) -> bool:
        return self.entry.money_cell

    @property
    def key(self) -> str:
        """Manifest key: the mutant id at its checkpoint (M25 repeats across checkpoints)."""
        return f"{self.mutant_id}@{self.checkpoint_id}"

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class Generation:
    """What `generate` returns: every mutant in catalog order, failures included."""
    mutants: list[Mutant]

    @property
    def manifest(self) -> dict[str, dict[str, Any]]:
        """The sha256 manifest kept for the next night's check."""
        return {m.key: {"base_sha256": m.base_sha256, "mutant_sha256": m.mutant_sha256, "seed": m.seed}
                for m in self.mutants if m.image is not None}

    @property
    def failed(self) -> list[Mutant]:
        return [m for m in self.mutants if m.failed]


def mutant_rng(seed: int, mutant_id: str) -> np.random.Generator:
    """The rng of one mutant: the catalog seed spawned by the mutant number, independent of catalog order."""
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(int(mutant_id[1:]),)))


def _render(entry: MutantEntry, cid: str, base: Image, approved: Mapping[str, Image],
            boxes: Mapping[str, Sequence[int]], rng: np.random.Generator) -> tuple[Image, Box | None]:
    base = _check_image(base)
    height, width = base.shape[:2]
    if entry.defect_class in WHOLE_WINDOW_CLASSES:
        box: Box = (0, 0, width, height)
    else:
        if entry.region_ref not in boxes:
            raise GenerationError(f"region {entry.region_ref} not located on {cid}")
        box = _check_box(base, boxes[entry.region_ref])
    kwargs: dict[str, Any] = {}
    for k, v in entry.params.items():
        if k in DESCRIPTIVE_PARAMS:
            continue
        if k in REGION_PARAMS:
            if v not in boxes:
                raise GenerationError(f"region {v} ({k}) not located on {cid}")
            kwargs[REGION_PARAMS[k]] = boxes[v]
        elif k == "donor_checkpoint":
            if v not in approved:
                raise GenerationError(f"no approved capture for donor checkpoint {v}")
            kwargs["donor"] = approved[v]
        else:
            kwargs[k] = v
    out = RECIPES[entry.defect_class](base, box, rng, **kwargs)
    changed = np.any(out != base, axis=2)
    if entry.defect_class != "aa_control" and not changed.any():
        raise GenerationError("the mutant equals the approved capture")
    if not changed.any():
        return out, None
    ys, xs = np.nonzero(changed)
    return out, (int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))


def generate(catalog_entries: Iterable[MutantEntry], approved_images: Mapping[str, Image],
             region_boxes: Mapping[str, Mapping[str, Sequence[int]]], seed: int = SEED, *,
             previous_manifest: Mapping[str, Mapping[str, Any]] | None = None,
             catalog_hash_ok: bool = True) -> Generation:
    """Generate every mutant of the catalog from the approved captures (10.3 Determinism, 12.5).

    `approved_images` maps checkpoint id to the approved BGR capture of the main build; `region_boxes`
    maps checkpoint id to {region id: (x, y, w, h)} located by landmark. An entry's own generator seed
    wins over `seed`. A missing capture, region or donor, a degenerate box, a generator exception, a
    mutant equal to its base, a sha256 that differs from `previous_manifest` for the same base and
    seed, or a catalog that failed the G3 preflight hash (`catalog_hash_ok=False`) leaves that mutant
    with `error` set: `harness_error (reason: canary_generation_failed)`, never skipped.
    """
    entries = list(catalog_entries)
    bound = sorted({e.checkpoint_id for e in entries if e.mode == "image" and e.checkpoint_id != "*"})
    previous = previous_manifest or {}
    mutants: list[Mutant] = []
    for e in entries:
        for cid in (bound if e.checkpoint_id == "*" else [e.checkpoint_id]):
            m = Mutant(entry=e, checkpoint_id=cid, seed=e.seed if e.seed is not None else seed)
            mutants.append(m)
            if not catalog_hash_ok:
                m.error = "the catalog failed the G3 preflight hash"
                continue
            base = approved_images.get(cid)
            if base is None:
                if e.mode == "image":
                    m.error = f"no approved capture for {cid}"
                continue                      # behavioral without a capture: flow only (choice 5)
            try:
                image, footprint = _render(e, cid, base, approved_images, region_boxes.get(cid, {}),
                                           mutant_rng(m.seed, e.mutant_id))
            except GenerationError as exc:
                m.error = str(exc)
                continue
            except Exception as exc:          # 10.3: a generator exception is a generation failure
                m.error = f"exception:{type(exc).__name__}: {exc}"
                continue
            base_sha, mutant_sha = image_sha256(base), image_sha256(image)
            prev = previous.get(m.key)
            if prev and prev.get("base_sha256") == base_sha and prev.get("seed") == m.seed \
                    and prev.get("mutant_sha256") != mutant_sha:
                m.base_sha256 = base_sha
                m.error = "nondeterministic generator: same base and seed, different mutant sha256"
                continue
            m.image, m.footprint, m.base_sha256, m.mutant_sha256 = image, footprint, base_sha, mutant_sha
    return Generation(mutants)


# -- scoring (10.3 Scoring, 8.3 canary_results) ----------------------------------------------------

@dataclass
class MutantResult:
    """What the path produced for one mutant, as `score` takes it."""
    mutant: Mutant
    produced: CheckpointOutcome | None = None   # the synthetic record from evaluate_checkpoint
    absorbed_by: str | None = None              # the mask, decision, threshold or landmark that swallowed a miss
    on_mutated_region: bool | None = None       # None: derived from the record's diff (choice 7)
    step_status: str | None = None              # behavioral: the canary flow's step status (gateway)
    redaction_hit: bool | None = None           # redaction: the stage masked the id and the egress log shows it


@dataclass
class RowContext:
    """Fields for a synthetic row when the path produced no record (a generation failure)."""
    run_id: str
    workspace_id: str
    persona: str
    data_mode: str


@dataclass
class CanaryResult:
    """One `canary_results` row (8.3), with its CHECK constraints enforced."""
    mutant_id: str
    catalog_version: str
    checkpoint_id: str
    defect_class: str
    money_cell: bool
    seed: int
    expected: list[str]
    state: str
    base_sha256: str | None = None
    mutant_sha256: str | None = None
    produced_outcome: str | None = None
    produced_reason: str | None = None
    absorbed_by: str | None = None
    class_rate: float | None = None
    class_floor: float | None = None
    kind: str = "mutant"

    def __post_init__(self) -> None:
        load().check_member("canary_states", self.state)
        if self.money_cell and self.state == "missed":
            raise TaxonomyError("a money-cell miss is always detector_blind (8.3)")
        if not self.money_cell and self.state == "detector_blind":
            raise TaxonomyError("detector_blind only on a money-cell row (8.3)")
        if self.state == "detector_degraded" and (self.class_rate is None or self.class_floor is None):
            raise TaxonomyError("detector_degraded needs class_rate and class_floor (8.3)")
        if self.state in ("missed", "detector_blind", "detector_degraded") and self.produced_outcome is None:
            raise TaxonomyError("a miss keeps the produced outcome (8.3)")

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclass
class CanaryReport:
    """The night's canary result: rows, run state, synthetic outcome rows and the banner."""
    state: str                                  # ok | detector_blind | generation_failed | detector_degraded
    results: list[CanaryResult]
    outcome_rows: list[CheckpointOutcome]       # synthetic harness_error rows that replace the produced rows
    class_rates: dict[str, float]
    class_floors: dict[str, float]
    degraded_classes: list[str]
    banner: str | None
    page: str | None                            # P1 for blind and generation_failed, P2 for degraded (9.10)
    rc_signoff_blocked: bool
    no_canary_ran: bool                         # the run is then aborted_harness (reason: canary_generation_failed)

    @property
    def detector_state(self) -> str:
        """The value for `system_state.detector_state` and the Nightly run's summary_json."""
        return self.state


def rolling_floor(history: Sequence[float], phase2_exited: bool = False) -> float:
    """The class floor of 10.3: min(14-night median - 5 points, 90 percent), at least 95 percent after Phase 2."""
    recent = list(history)[-FLOOR_WINDOW:]
    floor = min(statistics.median(recent) - FLOOR_MARGIN, FLOOR_CAP) if recent else FLOOR_CAP
    return max(floor, FLOOR_TARGET) if phase2_exited else floor


def _intersects(a: Sequence[int], b: Sequence[int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _on_region(m: Mutant, r: MutantResult) -> bool:
    if r.on_mutated_region is not None:
        return r.on_mutated_region
    p = r.produced
    ref = m.entry.region_ref
    if p is None:
        return False
    if p.classification is not None and ref is not None and p.classification.region_pointer == ref:
        return True
    if p.diff is None:
        return False
    if any(rd.region_id == ref and rd.above_threshold for rd in p.diff.regions):
        return True
    return any((c.region_id is not None and c.region_id == ref) or (m.footprint is not None and _intersects(c.bbox, m.footprint))
               for c in p.diff.components)


def _detected(m: Mutant, r: MutantResult) -> bool:
    if m.defect_class == "redaction":
        return bool(r.redaction_hit)
    if m.defect_class == "gateway":
        return r.step_status == "blocked_by_gateway"
    assert r.produced is not None
    outcome, cls = r.produced.outcome, r.produced.classification
    floor = m.entry.expected.min_confidence
    if floor is not None and cls is not None and cls.confidence is not None and cls.confidence < floor:
        return False
    if outcome in m.entry.expected.outcomes:
        return True
    return m.defect_class not in CONTROL_CLASSES and outcome in ("collateral", "needs_human") and _on_region(m, r)


def _check_result(r: MutantResult) -> None:
    m, p = r.mutant, r.produced
    if p is None:
        raise ValueError(f"{m.key}: a generated mutant needs the outcome the path produced")
    if not p.synthetic:
        raise ValueError(f"{m.key}: canary records carry synthetic: true (10.3)")
    if p.checkpoint_id != m.checkpoint_id:
        raise ValueError(f"{m.key}: produced record is for {p.checkpoint_id}")
    if r.step_status is not None:
        load().check_step(r.step_status)


def _absorbed(r: MutantResult) -> str | None:
    if r.absorbed_by:
        return r.absorbed_by
    reason = r.produced.reason if r.produced else None
    return reason if reason and reason.startswith("decision:") else None


def _harness_row(r: MutantResult, reason: str, context: RowContext | None) -> CheckpointOutcome:
    m, p = r.mutant, r.produced
    produced = f"; produced {p.outcome}" + (f" ({p.reason})" if p.reason else "") if p else ""
    detail = f"mutant {m.mutant_id} {m.defect_class}{produced}" + (f": {m.error}" if m.error else "")
    if p is not None:
        return CheckpointOutcome(run_id=p.run_id, checkpoint_id=p.checkpoint_id, outcome="harness_error",
                                 reason=reason, detail=detail, workspace_id=p.workspace_id, persona=p.persona,
                                 data_mode=p.data_mode, surface_id=p.surface_id, test_kind=p.test_kind,
                                 tier=p.tier, flow_id=p.flow_id, step_id=p.step_id, size_step=p.size_step,
                                 artifacts=p.artifacts, synthetic=True)
    if context is None:
        raise ValueError(f"{m.key}: a generation failure needs a RowContext for its synthetic row")
    return CheckpointOutcome(run_id=context.run_id, checkpoint_id=m.checkpoint_id, outcome="harness_error",
                             reason=reason, detail=detail, workspace_id=context.workspace_id,
                             persona=context.persona, data_mode=context.data_mode, synthetic=True)


def score(results: Sequence[MutantResult], *, history: Mapping[str, Sequence[float]] | None = None,
          phase2_exited: bool = False, since: str | None = None, context: RowContext | None = None) -> CanaryReport:
    """Score one night's mutants (10.3 Scoring, F31, F58).

    Money-cell mutants need 100 percent: a miss is `detector_blind` and its synthetic row is
    `harness_error (reason: detector_blind)`. Other mutants count toward their class rate against
    `rolling_floor(history[class])`; a miss in a class below the floor is `detector_degraded` with the
    row `harness_error (reason: detector_degraded)`, otherwise `missed` and the produced row stands.
    A generation failure is `generation_failed` with `harness_error (reason: canary_generation_failed)`.
    `history` maps class to its previous nightly rates (oldest first); `since` is the banner's start
    time as the Reporter prints it (for example "2026-11-09 03:12 ET").
    """
    history = history or {}
    graded: list[tuple[MutantResult, bool | None]] = []
    per_class: dict[str, list[bool]] = {}
    for r in results:
        if r.mutant.failed:
            graded.append((r, None))
            continue
        _check_result(r)
        hit = _detected(r.mutant, r)
        graded.append((r, hit))
        if not r.mutant.money_cell:
            per_class.setdefault(r.mutant.defect_class, []).append(hit)
    rates = {c: sum(v) / len(v) for c, v in per_class.items()}
    floors = {c: rolling_floor(history.get(c, ()), phase2_exited) for c in per_class}
    below = sorted(c for c in rates if rates[c] < floors[c])

    rows: list[CanaryResult] = []
    outcome_rows: list[CheckpointOutcome] = []
    blind: list[CanaryResult] = []
    redaction_blind: list[CanaryResult] = []
    degraded: list[CanaryResult] = []
    failed: list[Mutant] = []
    run_rank = 0
    for r, hit in graded:
        m, p = r.mutant, r.produced
        if hit is None:
            state, reason = "generation_failed", "canary_generation_failed"
            failed.append(m)
        elif hit:
            state, reason = "detected", None
        elif m.money_cell:
            state, reason = "detector_blind", "detector_blind"
        elif m.defect_class in below:
            state, reason = "detector_degraded", "detector_degraded"
        else:
            state, reason = "missed", None
        if hit is False and m.defect_class == "redaction":
            reason = "safety_canary_blind:redaction"
        row = CanaryResult(
            mutant_id=m.mutant_id, catalog_version=m.entry.catalog_version, checkpoint_id=m.checkpoint_id,
            defect_class=m.defect_class, money_cell=m.money_cell, seed=m.seed,
            expected=list(m.entry.expected.outcomes), state=state, base_sha256=m.base_sha256,
            mutant_sha256=m.mutant_sha256, produced_outcome=p.outcome if p else None,
            produced_reason=p.reason if p else None, absorbed_by=None if hit else _absorbed(r),
            class_rate=None if m.money_cell else rates.get(m.defect_class),
            class_floor=None if m.money_cell else floors.get(m.defect_class),
        )
        rows.append(row)
        run_rank = max(run_rank, STATE_RANK.get(state, 0))
        if reason is not None:
            outcome_rows.append(_harness_row(r, reason, context))
        if state == "detector_blind":
            blind.append(row)
        if reason == "safety_canary_blind:redaction":
            redaction_blind.append(row)
            run_rank = max(run_rank, STATE_RANK["detector_blind"])
        elif state == "detector_degraded":
            degraded.append(row)

    state = next((s for s, k in STATE_RANK.items() if k == run_rank), "ok")
    money_total = sum(1 for r, hit in graded if hit is not None and r.mutant.money_cell)
    labels = {r.mutant.mutant_id: r.mutant.entry.label for r, _ in graded}
    banner = _banner(state, since, labels, money_total, blind, redaction_blind, failed, below, rates, floors, degraded)
    return CanaryReport(
        state=state, results=rows, outcome_rows=outcome_rows, class_rates=rates, class_floors=floors,
        degraded_classes=below, banner=banner,
        page={"detector_blind": "P1", "generation_failed": "P1", "detector_degraded": "P2"}.get(state),
        rc_signoff_blocked=state != "ok", no_canary_ran=all(hit is None for _, hit in graded),
    )


def _banner(state: str, since: str | None, labels: Mapping[str, str], money_total: int,
            blind: list[CanaryResult], redaction_blind: list[CanaryResult], failed: list[Mutant],
            below: list[str], rates: Mapping[str, float], floors: Mapping[str, float],
            degraded: list[CanaryResult]) -> str | None:
    """The morning message of 10.3 (choice 10)."""
    if state == "ok":
        return None
    head = "DETECTOR BLIND" if state in ("detector_blind", "generation_failed") else "DETECTOR DEGRADED"
    lines = [f"{head} since {since}." if since else f"{head}."]
    for i, c in enumerate(blind):
        lead = f"Missed {len(blind)} of {money_total} money-cell mutants: " if i == 0 else "Also missed: "
        lines += [f"{lead}{c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}",
                  f"  (checkpoint {c.checkpoint_id}).", f"Absorbed by: {c.absorbed_by or 'not recorded'}."]
    for c in redaction_blind:
        lines += [f"Redaction canary blind: {c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}",
                  f"  (checkpoint {c.checkpoint_id})."]
    if failed:
        lines.append("canaries did not run")
        lines += [f"  {m.mutant_id} {m.defect_class} (checkpoint {m.checkpoint_id}): {m.error}." for m in failed]
    if below:
        lines.append("Classes below their rolling floor: "
                     + ", ".join(f"{c} {rates[c]:.0%} (floor {floors[c]:.0%})" for c in below) + ".")
        lines += [f"  Missed {c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}"
                  f" (checkpoint {c.checkpoint_id}); absorbed by: {c.absorbed_by or 'not recorded'}." for c in degraded]
    if head == "DETECTOR BLIND":
        lines.append("Reports for the affected checkpoints cannot be trusted until this clears.")
    lines.append("RC sign-off is blocked.")
    return "\n".join(lines)
