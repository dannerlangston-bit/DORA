"""The Diff Engine: three-granularity diff of two redacted captures of one checkpoint (3.7, 5.8).

`diff_pair` takes a base and a candidate capture (RGB uint8), the checkpoint's Region Registry
entry, its calibration and the redaction boxes, and returns a `DiffResult`: the 3.11 diff level
(`zero`, `below`, `above`), the threshold source, a `sentinel.records.DiffSummary`, the findings
(`layout_shift`, `landmark_moved`, `color_shift`, `window_geometry_changed`,
`mask_budget_exceeded`), a heatmap, full per-region metrics (the `calibration_stats` row of 3.9
step 3) and components. A checkpoint the engine cannot diff ends in a `Terminal` outcome instead:
`landmark_missing (landmark:<id>)` or `harness_error (mask_budget_exceeded)`. `compare_fingerprints`
refuses a pair before any pixel is read (3.3, 4.12), and `global_shift_prepass` is the run-level
granularity-0 pass.

Order of work (3.7, 5.8): pad to a common size (3.3); exact equality short-circuits to `zero`;
locate landmarks; locate regions and evaluate the mask budget; refuse on a missing landmark;
whole-window translation; per-region YIQ diff with anti-aliasing detection, per-region
translation, SSIM and CIEDE2000; connected components over the whole changed map; thresholds;
findings; heatmap.

Choices where the spec is ambiguous (each reported to the coordinator):

1. Hash equality short-circuits before landmarks and the mask budget (5.8 "exact hash equality
   short-circuits to identical").
2. The mask budget is evaluated before the missing-landmark refusal, on the panels whose landmarks
   were found, so `harness_error` keeps its precedence over `landmark_missing` (3.11 rows 2, 6).
3. A translation counts (for the finding and for the threshold) only when the response, range and
   residual rules of granularity 1 all hold. When a region's translation is accepted, its
   components and pixel counts come from the residual (aligned) map, so a moved gadget is one
   `layout_shift` finding and not a pixel storm. The implicit `_rest` region is compared under the
   accepted whole-window shift and gets no estimate of its own (it is the window estimate).
4. A landmark's displacement is measured relative to the accepted whole-window shift; a non-zero
   relative displacement is a `landmark_moved` finding and is the translation of every region cut
   from that landmark (a moved panel is a change, never noise).
5. `_rest` is compared at absolute coordinates (under the window shift) and excludes every pixel a
   declared region covers on either side.
6. A region is above threshold when any owned component reaches `component_area_min`, its changed
   pixels reach `changed_px_min`, its mean CIEDE2000 reaches `delta_e_min`, or its translation
   reaches `shift_px_min` (3.9 table). `color_shift` is raised when that color test fires while the
   changed-pixel count is under `changed_px_min` ("the per-pixel map is nearly empty").
7. The checkpoint's threshold source is `uncalibrated` when any above-threshold region (or the
   whole window) is uncalibrated, or when no region is calibrated; otherwise `calibrated`.
8. A calibrated record with a non-zero floor applies only while the region carries a live
   `floor_exception`; without one the region is `uncalibrated` (threshold 0), on its own.
9. Pixel diffs and anti-aliasing detection run per region crop, as pixelmatch would on that crop.
   Regions smaller than the 11x11 SSIM window are edge-padded for SSIM.
10. A component is a text-edge component when at least `component_text_edge_share` of its changed
    pixels lie on the dilated Canny edges of the base crop. A checkpoint is text-edge-only when all
    its reportable components are text-edge components (vacuously so with none). In the run-level
    share, a run whose above-floor checkpoints changed no pixel at all meets the text-edge test.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import cv2
import numpy as np
from skimage.color import deltaE_ciede2000, rgb2lab
from skimage.metrics import structural_similarity

from ..records import Component, DiffSummary, Finding, RegionDiff
from ..taxonomy import load as load_taxonomy
from . import load_params
from .phash import component_crop, phash
from .regions import (
    REST_ID, UNCALIBRATED, WHOLE_ID, BudgetResult, Rect, RedactionBox, Region, RegionsFile,
    RegionThreshold, locate, mask_budget, money_cell_threshold,
)

if TYPE_CHECKING:
    from .calibration import CalibrationFile

# YIQ coefficients of 3.7 granularity 2 (pixelmatch).
_Y = np.array([0.29889531, 0.58662247, 0.11448223])
_I = np.array([0.59597799, -0.27417610, -0.32180189])
_Q = np.array([0.21147017, -0.52261711, 0.31114694])
# 8-neighborhood in pixelmatch's iteration order (x outer, y inner), as (dx, dy).
_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
_SSIM_WIN = 11


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffContext:
    """Who the findings belong to."""

    run_id: str = "run-local"
    checkpoint_id: str | None = None
    tier: int | None = None


@dataclass(frozen=True)
class Terminal:
    """An outcome the engine decides by itself (no diff level). Validated against the taxonomy."""

    outcome: str
    reason: str
    side: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        load_taxonomy().check_checkpoint(self.outcome, self.reason)
        if self.side not in (None, "base", "candidate", "both"):
            raise ValueError(f"side {self.side!r}")


@dataclass(frozen=True)
class Match:
    """A template search result (3.7): best score, second peak outside the radius, top-left."""

    found: bool
    score: float
    second: float
    pos: tuple[int, int] | None


@dataclass(frozen=True)
class Shift:
    """A granularity-1 translation estimate. `accepted` means response, range and residual held."""

    dx: int
    dy: int
    response: float
    accepted: bool
    unshifted_px: int = 0
    residual_px: int = 0
    residual_components: int = 0


@dataclass
class RegionMetrics:
    """Per-region metrics of one pair: the `calibration_stats` row of 3.9 step 3 plus decisions."""

    region_id: str
    kind: str
    base_rect: Rect
    candidate_rect: Rect
    compared_px: int
    changed_px: int
    aa_px: int
    pixel_fraction: float
    ssim: float
    ssim_min_block: float
    delta_e: float
    shift: tuple[int, int]
    shift_px: int
    components: int
    largest_component: int
    largest_bbox: tuple[int, int, int, int] | None
    text_edge_px: int
    threshold: RegionThreshold
    threshold_source: str
    above_threshold: bool
    above_floor: bool
    color_shift: bool
    tests: tuple[str, ...]


@dataclass
class ComponentDetail:
    """One reportable connected component (3.7 granularity 3), in base coordinates."""

    index: int
    area: int
    bbox: tuple[int, int, int, int]
    centroid: tuple[float, float]
    region_id: str
    mean_delta_e: float
    color_semantic_flip: bool
    text_edge: bool
    phash: str
    base_phash: str


@dataclass(frozen=True)
class GlobalShiftSignal:
    """What one compared checkpoint contributes to the granularity-0 pre-pass (3.7)."""

    checkpoint_id: str | None
    level: str
    above_floor: bool
    changed_px: int
    text_edge_px: int
    text_edge_only: bool


@dataclass
class DiffResult:
    """Everything one diff produces. `level` is None when `terminal` is set."""

    level: str | None
    threshold_source: str
    summary: DiffSummary
    findings: list[Finding]
    heatmap: np.ndarray
    regions: list[RegionMetrics] = field(default_factory=list)
    components: list[ComponentDetail] = field(default_factory=list)
    landmarks: dict[str, tuple[Match, Match]] = field(default_factory=dict)
    window_shift: Shift | None = None
    terminal: Terminal | None = None
    budget: BudgetResult | None = None
    signal: GlobalShiftSignal | None = None
    changed_map: np.ndarray | None = None

    def region(self, region_id: str) -> RegionMetrics:
        for r in self.regions:
            if r.region_id == region_id:
                return r
        raise KeyError(region_id)

    def findings_of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


# --------------------------------------------------------------------------------------------
# Fingerprint comparability (3.3, 4.12, 7.4)
# --------------------------------------------------------------------------------------------

def fingerprint_id(match: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of the match fields without `build_id` (4.12, 7.4)."""
    body = {k: v for k, v in match.items() if k != "build_id"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def compare_fingerprints(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> Terminal | None:
    """The comparability rule of 3.3 and L-FPR-03/05: every `match` field equal except `build_id`;
    `render_probe_hash` at Hamming distance 0. Returns `not_comparable (fingerprint_mismatch)` with
    the differing field names in `detail`, or None when the pair may be diffed. A missing
    calibration is never a comparability failure (3.3)."""
    mb, mc = base.get("match", {}), candidate.get("match", {})
    fields = sorted((set(mb) | set(mc)) - {"build_id"})
    differing = []
    for f in fields:
        a, b = mb.get(f), mc.get(f)
        if f == "render_probe_hash" and isinstance(a, str) and isinstance(b, str):
            if a.lower() != b.lower():
                differing.append(f)
        elif a != b or a is None:
            differing.append(f)
    if not differing:
        return None
    return Terminal("not_comparable", "fingerprint_mismatch", side="both", detail="field: " + ", ".join(differing))


# --------------------------------------------------------------------------------------------
# Template search (3.7 landmarks; also the settle rule's anchors, 3.5)
# --------------------------------------------------------------------------------------------

def match_template(image: np.ndarray, template: np.ndarray, search: Rect | None = None,
                   radius: int = 5) -> tuple[float, float, tuple[int, int] | None]:
    """`cv2.matchTemplate` with `TM_CCOEFF_NORMED` inside `search`: (best, second peak outside
    `radius` px of the best, best top-left in image coordinates)."""
    H, W = image.shape[:2]
    box = search or Rect(0, 0, W, H)
    th, tw = template.shape[:2]
    if box.w < tw or box.h < th:
        return 0.0, 0.0, None
    sub = image[box.y:box.y + box.h, box.x:box.x + box.w]
    res = cv2.matchTemplate(np.ascontiguousarray(sub), np.ascontiguousarray(template), cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
    _, best, _, (bx, by) = cv2.minMaxLoc(res)
    yy, xx = np.ogrid[:res.shape[0], :res.shape[1]]
    outside = (xx - bx) ** 2 + (yy - by) ** 2 > radius * radius
    second = float(res[outside].max()) if outside.any() else -1.0
    return float(best), second, (box.x + bx, box.y + by)


def locate_landmark(image: np.ndarray, lm: Any, params: Mapping[str, Any]) -> Match:
    """Accept a landmark when the best score is at least its `confidence_min` (0.92 default) and
    the second peak outside 5 px is below 0.85 (3.7)."""
    p = params["landmarks"]
    H, W = image.shape[:2]
    box = lm.search_box(W, H, int(p["search_margin_px"]))
    best, second, pos = match_template(image, lm.template, box, int(p["second_peak_radius_px"]))
    ok = pos is not None and best >= float(lm.confidence_min) and second < float(p["second_peak_max"])
    return Match(ok, best, second, pos if ok else None)


# --------------------------------------------------------------------------------------------
# Pixel diff (3.7 granularity 2)
# --------------------------------------------------------------------------------------------

def yiq_delta(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """pixelmatch's squared YIQ color distance per pixel (3.7): 0.5053 Y^2 + 0.299 I^2 + 0.1957 Q^2."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    y = a @ _Y - b @ _Y
    i = a @ _I - b @ _I
    q = a @ _Q - b @ _Q
    return 0.5053 * y * y + 0.299 * i * i + 0.1957 * q * q


def max_delta(params: Mapping[str, Any]) -> float:
    """35215 x t^2 (352.15 at t = 0.1)."""
    p = params["pixel"]
    return float(p["max_delta_scale"]) * float(p["t"]) ** 2


def _border(ys: np.ndarray, xs: np.ndarray, h: int, w: int) -> np.ndarray:
    return ((xs == 0) | (xs == w - 1) | (ys == 0) | (ys == h - 1)).astype(np.int32)


def _many_siblings(img: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """pixelmatch hasManySiblings: at least three identical 8-neighbors (border counts as one)."""
    h, w = img.shape[:2]
    count = _border(ys, xs, h, w)
    center = img[ys, xs]
    for dx, dy in _OFFSETS:
        nx, ny = xs + dx, ys + dy
        ok = (nx >= 0) & (nx < w) & (ny >= 0) & (ny < h)
        eq = np.zeros(len(ys), dtype=bool)
        eq[ok] = (img[ny[ok], nx[ok]] == center[ok]).all(axis=-1)
        count += eq
    return count > 2


def _antialiased(img: np.ndarray, lum: np.ndarray, other: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """pixelmatch antialiased(img, x, y, ..., other), vectorized over the given pixels."""
    h, w = lum.shape
    n = len(ys)
    zeroes = _border(ys, xs, h, w)
    deltas = np.full((8, n), np.nan)
    for k, (dx, dy) in enumerate(_OFFSETS):
        nx, ny = xs + dx, ys + dy
        ok = (nx >= 0) & (nx < w) & (ny >= 0) & (ny < h)
        deltas[k, ok] = lum[ys[ok], xs[ok]] - lum[ny[ok], nx[ok]]
    zeroes = zeroes + (deltas == 0).sum(axis=0)
    neg = np.where(deltas < 0, deltas, np.inf)
    pos = np.where(deltas > 0, deltas, -np.inf)
    cand = (zeroes <= 2) & np.isfinite(neg).any(axis=0) & np.isfinite(pos).any(axis=0)
    out = np.zeros(n, dtype=bool)
    idx = np.nonzero(cand)[0]
    if idx.size == 0:
        return out
    off = np.array(_OFFSETS)
    kmin, kmax = np.argmin(neg[:, idx], axis=0), np.argmax(pos[:, idx], axis=0)
    result = np.zeros(idx.size, dtype=bool)
    for k in (kmin, kmax):
        px, py = xs[idx] + off[k, 0], ys[idx] + off[k, 1]
        result |= _many_siblings(img, py, px) & _many_siblings(other, py, px)
    out[idx] = result
    return out


def _edge_tolerant(a: np.ndarray, b: np.ndarray, ys: np.ndarray, xs: np.ndarray, r: int, md: float) -> np.ndarray:
    """`edge_tolerance_px`: a changed pixel whose color appears within r px in the other image,
    symmetrically (both directions must hold; "appears" is the YIQ test at threshold t)."""
    h, w = a.shape[:2]

    def appears(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
        found = np.zeros(len(ys), dtype=bool)
        color = src[ys, xs]
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                nx, ny = xs + dx, ys + dy
                ok = (nx >= 0) & (nx < w) & (ny >= 0) & (ny < h) & ~found
                found[ok] = yiq_delta(color[ok], dst[ny[ok], nx[ok]]) <= md
        return found

    return appears(a, b) & appears(b, a)


def pixel_diff(a: np.ndarray, b: np.ndarray, params: Mapping[str, Any], *, valid: np.ndarray | None = None,
               edge_tolerance_px: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Changed and anti-aliased pixel maps of two same-size RGB images (3.7 granularity 2).

    A pixel is changed when its YIQ delta exceeds 35215 t^2 and it is not anti-aliased in the
    pixelmatch sense (checked in both images). Pixels outside `valid` are never changed.
    """
    md = max_delta(params)
    raw = yiq_delta(a, b) > md
    if valid is not None:
        raw &= valid
    aa = np.zeros_like(raw)
    if raw.any() and params["pixel"]["detect_antialiasing"]:
        ys, xs = np.nonzero(raw)
        la, lb = a.astype(np.float64) @ _Y, b.astype(np.float64) @ _Y
        hit = _antialiased(a, la, b, ys, xs) | _antialiased(b, lb, a, ys, xs)
        aa[ys[hit], xs[hit]] = True
    changed = raw & ~aa
    if edge_tolerance_px > 0 and changed.any():
        ys, xs = np.nonzero(changed)
        tol = _edge_tolerant(a, b, ys, xs, edge_tolerance_px, md)
        changed[ys[tol], xs[tol]] = False
    return changed, aa


# --------------------------------------------------------------------------------------------
# Pair geometry helpers
# --------------------------------------------------------------------------------------------

def _pad_common(base: np.ndarray, cand: np.ndarray, pad_rgb: Sequence[int]):
    """Pad both captures to the common size with the sentinel color (3.3). Returns the padded
    images and their validity maps (False on padding)."""
    H = max(base.shape[0], cand.shape[0])
    W = max(base.shape[1], cand.shape[1])

    def pad(img: np.ndarray):
        out = np.empty((H, W, 3), dtype=np.uint8)
        out[:] = np.array(pad_rgb, dtype=np.uint8)
        out[:img.shape[0], :img.shape[1]] = img[:, :, :3]
        valid = np.zeros((H, W), dtype=bool)
        valid[:img.shape[0], :img.shape[1]] = True
        return out, valid

    (b, vb), (c, vc) = pad(base), pad(cand)
    return b, vb, c, vc


def _crop(img: np.ndarray, valid: np.ndarray, r: Rect, pad_rgb: Sequence[int]):
    """Crop `r` from `img`; pixels outside the image take the pad color and are invalid."""
    out = np.empty((r.h, r.w, 3), dtype=np.uint8)
    out[:] = np.array(pad_rgb, dtype=np.uint8)
    v = np.zeros((r.h, r.w), dtype=bool)
    H, W = img.shape[:2]
    i = r.intersect(Rect(0, 0, W, H))
    if i is not None:
        sy, sx = i.y - r.y, i.x - r.x
        out[sy:sy + i.h, sx:sx + i.w] = img[i.y:i.y + i.h, i.x:i.x + i.w]
        v[sy:sy + i.h, sx:sx + i.w] = valid[i.y:i.y + i.h, i.x:i.x + i.w]
    return out, v


def _local_mask(shape: tuple[int, int], origin: tuple[int, int], rects: Sequence[Rect]) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    frame = Rect(origin[0], origin[1], shape[1], shape[0])
    for r in rects:
        i = r.intersect(frame)
        if i is not None:
            m[i.y - frame.y:i.y - frame.y + i.h, i.x - frame.x:i.x - frame.x + i.w] = True
    return m


def _overlap(h: int, w: int, dx: int, dy: int) -> tuple[tuple[slice, slice], tuple[slice, slice]]:
    """Slices so that candidate[c] corresponds to base[b] under shift (dx, dy): cand(p + d) ~ base(p)."""
    b = (slice(max(0, -dy), h - max(0, dy)), slice(max(0, -dx), w - max(0, dx)))
    c = (slice(max(0, dy), h - max(0, -dy)), slice(max(0, dx), w - max(0, -dx)))
    return b, c


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2GRAY)


def _dilate(m: np.ndarray, params: Mapping[str, Any]) -> np.ndarray:
    kw, kh = params["components"]["dilate_kernel_wh"]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (int(kw), int(kh)))
    return cv2.dilate(m.astype(np.uint8), kernel, iterations=int(params["components"]["dilate_iterations"])) > 0


def _count_components(changed: np.ndarray, params: Mapping[str, Any]) -> int:
    if not changed.any():
        return 0
    n, labels = cv2.connectedComponents(_dilate(changed, params).astype(np.uint8),
                                        connectivity=int(params["components"]["connectivity"]))
    areas = np.bincount(labels[changed], minlength=n)
    return int((areas[1:] >= int(params["components"]["min_area_px"])).sum())


@dataclass
class _Pair:
    """One compared pair of crops in a local frame whose (0, 0) is `origin` in base coordinates;
    the candidate pixel for base position p is at p + `offset`."""

    region_id: str
    origin: tuple[int, int]
    offset: tuple[int, int]
    base: np.ndarray
    cand: np.ndarray
    compared: np.ndarray
    changed: np.ndarray
    aa: np.ndarray
    shift: Shift | None = None


def _compare(rid: str, B: np.ndarray, C: np.ndarray, vb: np.ndarray, vc: np.ndarray, excl: np.ndarray,
             origin: tuple[int, int], offset: tuple[int, int], params: Mapping[str, Any], tol: int) -> _Pair:
    both = vb & vc & ~excl
    forced = (vb ^ vc) & ~excl            # padded on one side only: counts as changed (3.3)
    changed, aa = pixel_diff(B, C, params, valid=both, edge_tolerance_px=tol)
    changed |= forced
    return _Pair(rid, origin, offset, B, C, both | forced, changed, aa)


def _try_shift(B: np.ndarray, C: np.ndarray, vb: np.ndarray, vc: np.ndarray, excl_b: np.ndarray, excl_c: np.ndarray,
               unshifted: int, params: Mapping[str, Any], tol: int) -> tuple[Shift, tuple | None]:
    """Granularity 1 on one pair: phase correlation, then the residual rule."""
    p = params["translation"]
    h, w = B.shape[:2]
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), resp = cv2.phaseCorrelate(_gray(B).astype(np.float32), _gray(C).astype(np.float32), win)
    dx, dy = int(round(sx)), int(round(sy))
    mag = max(abs(dx), abs(dy))
    if resp < float(p["response_min"]) or not (int(p["shift_min_px"]) <= mag <= int(p["shift_max_px"])) \
            or unshifted == 0 or abs(dx) >= w or abs(dy) >= h:
        return Shift(dx, dy, float(resp), False, unshifted), None
    sb, sc = _overlap(h, w, dx, dy)
    excl = excl_b[sb] | excl_c[sc]
    both = vb[sb] & vc[sc] & ~excl
    forced = (vb[sb] ^ vc[sc]) & ~excl
    changed, aa = pixel_diff(B[sb], C[sc], params, valid=both, edge_tolerance_px=tol)
    changed |= forced
    residual = int(changed.sum())
    accepted = residual < float(p["residual_max_ratio"]) * unshifted
    shift = Shift(dx, dy, float(resp), accepted, unshifted, residual, _count_components(changed, params))
    return shift, (sb, sc, both | forced, changed, aa)


# --------------------------------------------------------------------------------------------
# Region metrics
# --------------------------------------------------------------------------------------------

def _ssim(pair: _Pair, params: Mapping[str, Any]) -> tuple[float, float]:
    """SSIM per region (3.7): Gaussian sigma 1.5, K1 0.01, K2 0.03, data range 255, population
    covariance; mean over compared pixels and the minimum over 16x16 blocks of the full map."""
    p = params["ssim"]
    gb = _gray(pair.base).astype(np.float64)
    gc = _gray(pair.cand).astype(np.float64)
    gc = np.where(pair.compared, gc, gb)              # excluded pixels are neutral
    h, w = gb.shape
    ph, pw = max(0, _SSIM_WIN - h), max(0, _SSIM_WIN - w)
    if ph or pw:
        gb = np.pad(gb, ((0, ph), (0, pw)), mode="edge")
        gc = np.pad(gc, ((0, ph), (0, pw)), mode="edge")
    _, smap = structural_similarity(gb, gc, gaussian_weights=True, sigma=float(p["sigma"]),
                                    use_sample_covariance=False, data_range=float(p["data_range"]),
                                    K1=float(p["k1"]), K2=float(p["k2"]), full=True)
    smap = smap[:h, :w]
    mean = float(smap[pair.compared].mean()) if pair.compared.any() else 1.0
    bs = int(p["block_px"])
    blocks = [smap[y:y + bs, x:x + bs].mean() for y in range(0, h, bs) for x in range(0, w, bs)]
    return mean, float(min(blocks)) if blocks else 1.0


def _mean_lab(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Mean CIELAB color of the masked pixels (computed over unique colors: UI captures have few)."""
    px = img[mask].astype(np.uint32)
    if px.size == 0:
        return np.zeros(3)
    packed = (px[:, 0] << 16) | (px[:, 1] << 8) | px[:, 2]
    uniq, counts = np.unique(packed, return_counts=True)
    rgb = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=-1).astype(np.float64) / 255.0
    lab = rgb2lab(rgb[:, None, :])[:, 0, :]
    return (lab * counts[:, None]).sum(axis=0) / counts.sum()


def _in_band(hsv: np.ndarray, band: Mapping[str, Any]) -> np.ndarray:
    hue = hsv[:, 0].astype(np.float64) * 2.0
    lo, hi = band["hue_deg"]
    in_hue = (hue >= lo) & (hue <= hi) if lo <= hi else (hue >= lo) | (hue <= hi)
    return in_hue & (hsv[:, 1] / 255.0 >= band["sat_min"]) & (hsv[:, 2] / 255.0 >= band["val_min"])


# --------------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------------

def _finding(ctx: DiffContext, kind: str, description: str, region_id: str | None = None,
             evidence: dict | None = None, phash_value: str | None = None) -> Finding:
    ev = evidence or {}
    key = json.dumps([ctx.run_id, ctx.checkpoint_id, kind, region_id, ev], sort_keys=True, default=str)
    fid = "fnd-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Finding(finding_id=fid, run_id=ctx.run_id, kind=kind, description=description,
                   first_seen_run_id=ctx.run_id, checkpoint_id=ctx.checkpoint_id, tier=ctx.tier,
                   region_id=region_id, phash=phash_value, evidence=ev)


def _threshold(calibration: "CalibrationFile | None", checkpoint_id: str, rid: str,
               region: Region | None) -> tuple[RegionThreshold, str]:
    if calibration is None:
        th, src = UNCALIBRATED, "uncalibrated"
    else:
        th, src = calibration.lookup(checkpoint_id, rid)
    if src == "calibrated" and th.nonzero_floor and (region is None or region.floor_exception is None):
        th, src = UNCALIBRATED, "uncalibrated"
    if region is not None and region.money_cell:
        th = money_cell_threshold(th)
    return th, src


def _heatmap(base: np.ndarray, changed: np.ndarray, aa: np.ndarray, rects: Sequence[Rect],
             comps: Sequence[ComponentDetail], params: Mapping[str, Any]) -> np.ndarray:
    """Base at 40 percent brightness, changed pixels red, anti-aliased pixels yellow, region
    boundaries and numbered component boxes (3.7). A report artifact, never a decision input."""
    p = params["heatmap"]
    out = (base.astype(np.float32) * float(p["base_brightness"])).astype(np.uint8)
    out[aa] = p["aa_rgb"]
    out[changed] = p["changed_rgb"]
    for r in rects:
        cv2.rectangle(out, (r.x, r.y), (r.x + r.w - 1, r.y + r.h - 1), tuple(int(v) for v in p["region_rgb"]), 1)
    for c in comps:
        x, y, w, h = c.bbox
        color = tuple(int(v) for v in p["component_rgb"])
        cv2.rectangle(out, (x - 1, y - 1), (x + w, y + h), color, 1)
        cv2.putText(out, str(c.index), (x, max(8, y - 3)), cv2.FONT_HERSHEY_PLAIN, 0.7, color, 1, cv2.LINE_8)
    return out


def _terminal_result(terminal: Terminal, findings: list[Finding], base: np.ndarray, params: Mapping[str, Any],
                     landmarks: dict, budget: BudgetResult | None, source: str) -> DiffResult:
    blank = np.zeros(base.shape[:2], dtype=bool)
    return DiffResult(level=None, threshold_source=source, summary=DiffSummary(), findings=findings,
                      heatmap=_heatmap(base, blank, blank, [], [], params), landmarks=landmarks,
                      terminal=terminal, budget=budget)


def diff_pair(base: np.ndarray, candidate: np.ndarray, registry: RegionsFile | None = None, *,
              calibration: "CalibrationFile | None" = None, redactions: Sequence[RedactionBox] = (),
              panel_bounds: Mapping[str, Mapping[str, Rect]] | None = None,
              context: DiffContext | None = None, params: Mapping[str, Any] | None = None) -> DiffResult:
    """Diff one redacted base/candidate pair of one checkpoint (3.7). Images are RGB uint8.

    `registry` is the effective Region Registry entry (see `regions.effective_registry`); None means
    no declared regions, so the whole window is `_rest`. `calibration` is the file for the run's
    fingerprint, or None when none exists (every region `uncalibrated`, 3.9 step 6).
    `panel_bounds[side][panel]` overrides the mask-budget panel bounds.
    """
    p = params or load_params()
    ctx = context or DiffContext()
    reg = registry or RegionsFile(checkpoint_id=ctx.checkpoint_id or "")
    cid = ctx.checkpoint_id or reg.checkpoint_id
    pad_rgb = p["geometry"]["pad_rgb"]
    findings: list[Finding] = []

    # 3.3 per-capture geometry
    B, vB, C, vC = _pad_common(base, candidate, pad_rgb)
    H, W = B.shape[:2]
    if base.shape[:2] != candidate.shape[:2]:
        bs, cs = (base.shape[1], base.shape[0]), (candidate.shape[1], candidate.shape[0])
        findings.append(_finding(ctx, "window_geometry_changed",
                                 f"window size changed from {bs[0]}x{bs[1]} to {cs[0]}x{cs[1]}",
                                 evidence={"base_size": list(bs), "candidate_size": list(cs)}))

    pixel_regions = [r for r in reg.regions if r.pixel_diffed]
    thresholds = {r.id: _threshold(calibration, cid, r.id, r) for r in pixel_regions}
    thresholds[REST_ID] = _threshold(calibration, cid, REST_ID, None)
    thresholds[WHOLE_ID] = _threshold(calibration, cid, WHOLE_ID, None)
    any_calibrated = any(src == "calibrated" for _, src in thresholds.values())

    # 5.8: exact hash equality short-circuits (no pixel work; regions report zero metrics)
    if base.shape == candidate.shape and np.array_equal(base, candidate):
        return _identical(cid, reg, B, thresholds, findings, p)

    # 3.7 landmarks, located in both images
    matches = {lm.id: (locate_landmark(B, lm, p), locate_landmark(C, lm, p)) for lm in reg.landmarks}
    pos_b = {k: m[0].pos for k, m in matches.items() if m[0].found}
    pos_c = {k: m[1].pos for k, m in matches.items() if m[1].found}
    loc_b, loc_c = locate(reg, pos_b), locate(reg, pos_c)

    # 3.8 mask budget, before the landmark refusal (choice 2)
    budget = mask_budget(reg, {"base": loc_b, "candidate": loc_c}, redactions, p["mask_budget"], panel_bounds)
    if budget.exceeded:
        detail = budget.detail()
        findings.append(_finding(ctx, "mask_budget_exceeded", f"mask budget exceeded: {detail}",
                                 evidence={"detail": detail}))
        return _terminal_result(Terminal("harness_error", "mask_budget_exceeded", detail=detail),
                                findings, B, p, matches, budget, "calibrated" if any_calibrated else "uncalibrated")
    for lm_id, (mb, mc) in matches.items():
        if not (mb.found and mc.found):
            side = "both" if not (mb.found or mc.found) else ("base" if not mb.found else "candidate")
            return _terminal_result(
                Terminal("landmark_missing", f"landmark:{lm_id}", side=side,
                         detail=f"base score {mb.score:.3f}/{mb.second:.3f}, candidate score {mc.score:.3f}/{mc.second:.3f}"),
                findings, B, p, matches, budget, "calibrated" if any_calibrated else "uncalibrated")

    masks_b = [loc_b[r.id] for r in reg.regions if r.type == "mask"]
    masks_c = [loc_c[r.id] for r in reg.regions if r.type == "mask"]
    red = [b.rect for b in redactions]

    # 3.7 granularity 1: whole window, translation only
    window_shift = _window_shift(B, vB, C, vC, masks_b + red, masks_c + red, p)
    g = (window_shift.dx, window_shift.dy) if window_shift.accepted else (0, 0)

    # landmark displacement relative to the window shift (choice 4)
    rel = {k: (pos_c[k][0] - pos_b[k][0] - g[0], pos_c[k][1] - pos_b[k][1] - g[1]) for k in matches}

    pairs: list[_Pair] = []
    tmin = int(p["translation"]["min_side_px"])
    for r in pixel_regions:
        rb, rc = loc_b[r.id], loc_c[r.id]
        Bc, vb = _crop(B, vB, rb, pad_rgb)
        Cc, vc = _crop(C, vC, rc, pad_rgb)
        excl_b = _local_mask(Bc.shape[:2], (rb.x, rb.y), masks_b + red)
        excl_c = _local_mask(Cc.shape[:2], (rc.x, rc.y), masks_c + red)
        tol = int(p["pixel"]["edge_tolerance_px"]) if r.floor_exception is not None else 0
        pair = _compare(r.id, Bc, Cc, vb, vc, excl_b | excl_c, (rb.x, rb.y), (rc.x - rb.x, rc.y - rb.y), p, tol)
        n0 = int(pair.changed.sum())
        if n0 and min(rb.w, rb.h) >= tmin:
            shift, aligned = _try_shift(Bc, Cc, vb, vc, excl_b, excl_c, n0, p, tol)
            pair.shift = shift
            if shift.accepted and aligned is not None:
                sb, sc, compared, changed, aa = aligned
                pair = _Pair(r.id, (rb.x + sb[1].start, rb.y + sb[0].start),
                             (rc.x - rb.x + shift.dx, rc.y - rb.y + shift.dy),
                             Bc[sb], Cc[sc], compared, changed, aa, shift)
        pairs.append(pair)

    # _rest under the window shift, excluding every declared region on either side (choice 5)
    sb, sc = _overlap(H, W, g[0], g[1])
    declared_b = list(loc_b.values()) + red
    declared_c = list(loc_c.values()) + red
    ob = (sb[1].start, sb[0].start)
    oc = (sc[1].start, sc[0].start)
    Bs, Cs = B[sb], C[sc]
    excl = _local_mask(Bs.shape[:2], ob, declared_b) | _local_mask(Cs.shape[:2], oc, declared_c)
    pairs.append(_compare(REST_ID, Bs, Cs, vB[sb], vC[sc], excl, ob, g, p, 0))
    whole_cmp = vB[sb] & vC[sc] & ~(_local_mask(Bs.shape[:2], ob, masks_b + red)
                                    | _local_mask(Cs.shape[:2], oc, masks_c + red))
    whole = _Pair(WHOLE_ID, ob, g, Bs, Cs, whole_cmp, whole_cmp, whole_cmp)

    return _assemble(ctx, cid, reg, pairs, whole, thresholds, rel, window_shift, matches, loc_b, budget,
                     findings, B, C, p)


def _identical(cid: str, reg: RegionsFile, B: np.ndarray, thresholds: Mapping[str, tuple[RegionThreshold, str]],
               findings: list[Finding], p: Mapping[str, Any]) -> DiffResult:
    H, W = B.shape[:2]
    pos = {}
    for lm in reg.landmarks:
        m = locate_landmark(B, lm, p)
        if m.found:
            pos[lm.id] = m.pos
    loc = locate(reg, pos)
    metrics = []
    for rid in [r.id for r in reg.regions if r.pixel_diffed] + [REST_ID]:
        th, src = thresholds[rid]
        rect = loc.get(rid, Rect(0, 0, W, H) if rid == REST_ID else Rect(0, 0, 0, 0))
        kind = reg.region(rid).type if rid != REST_ID else "diff"
        metrics.append(RegionMetrics(rid, kind, rect, rect, rect.area, 0, 0, 0.0, 1.0, 1.0, 0.0, (0, 0), 0, 0, 0,
                                     None, 0, th, src, False, False, False, ()))
    any_calibrated = any(src == "calibrated" for _, src in thresholds.values())
    blank = np.zeros((H, W), dtype=bool)
    summary = DiffSummary(regions=[RegionDiff(m.region_id, 0.0, 1.0, False) for m in metrics])
    return DiffResult("zero", "calibrated" if any_calibrated else "uncalibrated", summary, findings,
                      _heatmap(B, blank, blank, [], [], p), regions=metrics,
                      signal=GlobalShiftSignal(cid, "zero", False, 0, 0, True), changed_map=blank)


def _window_shift(B: np.ndarray, vB: np.ndarray, C: np.ndarray, vC: np.ndarray, excl_b_rects: list[Rect],
                  excl_c_rects: list[Rect], params: Mapping[str, Any]) -> Shift:
    H, W = B.shape[:2]
    if min(H, W) < int(params["translation"]["min_side_px"]):
        return Shift(0, 0, 0.0, False)
    excl_b = _local_mask((H, W), (0, 0), excl_b_rects)
    excl_c = _local_mask((H, W), (0, 0), excl_c_rects)
    win = cv2.createHanningWindow((W, H), cv2.CV_32F)
    (sx, sy), resp = cv2.phaseCorrelate(_gray(B).astype(np.float32), _gray(C).astype(np.float32), win)
    dx, dy = int(round(sx)), int(round(sy))
    tp = params["translation"]
    if resp < float(tp["response_min"]) or not (int(tp["shift_min_px"]) <= max(abs(dx), abs(dy)) <= int(tp["shift_max_px"])):
        return Shift(dx, dy, float(resp), False)
    whole = _compare(WHOLE_ID, B, C, vB, vC, excl_b | excl_c, (0, 0), (0, 0), params, 0)
    shift, _ = _try_shift(B, C, vB, vC, excl_b, excl_c, int(whole.changed.sum()), params, 0)
    return shift


def _assemble(ctx: DiffContext, cid: str, reg: RegionsFile, pairs: list[_Pair], whole: _Pair,
              thresholds: Mapping[str, tuple[RegionThreshold, str]], rel: Mapping[str, tuple[int, int]],
              window_shift: Shift, matches: dict, loc_b: Mapping[str, Rect], budget: BudgetResult,
              findings: list[Finding], B: np.ndarray, C: np.ndarray, p: Mapping[str, Any]) -> DiffResult:
    H, W = B.shape[:2]
    gp, cp = p["global_shift"], p["color"]
    changed = np.zeros((H, W), dtype=bool)
    aa = np.zeros((H, W), dtype=bool)
    compared = np.zeros((H, W), dtype=bool)
    edge = np.zeros((H, W), dtype=bool)
    src = np.full((H, W), -1, dtype=np.int32)
    de_map = np.zeros((H, W), dtype=np.float64)
    flip_map = np.zeros((H, W), dtype=np.int8)       # +1 green to red, -1 red to green
    owner = np.full((H, W), len(pairs) - 1, dtype=np.int32)   # default: _rest (last pair)
    regions_by_id = {r.id: r for r in reg.regions}
    for idx, pair in enumerate(pairs[:-1]):
        r = loc_b[pair.region_id].intersect(Rect(0, 0, W, H))
        if r is not None:
            owner[r.y:r.y + r.h, r.x:r.x + r.w] = idx

    lab_means: list[float] = []
    edges: list[np.ndarray] = []
    for idx, pair in enumerate(pairs):
        h, w = pair.changed.shape
        fr = Rect(pair.origin[0], pair.origin[1], w, h).intersect(Rect(0, 0, W, H))
        e = _text_edges(pair.base, gp)
        edges.append(e)
        de_local = np.zeros((h, w))
        flip_local = np.zeros((h, w), dtype=np.int8)
        if pair.changed.any():
            ys, xs = np.nonzero(pair.changed)
            pb = pair.base[ys, xs][:, None, :]
            pc = pair.cand[ys, xs][:, None, :]
            de_local[ys, xs] = deltaE_ciede2000(rgb2lab(pb / 255.0), rgb2lab(pc / 255.0))[:, 0]
            hb = cv2.cvtColor(np.ascontiguousarray(pb), cv2.COLOR_RGB2HSV)[:, 0, :]
            hc = cv2.cvtColor(np.ascontiguousarray(pc), cv2.COLOR_RGB2HSV)[:, 0, :]
            gr = _in_band(hb, cp["green_band"]) & _in_band(hc, cp["red_band"])
            rg = _in_band(hb, cp["red_band"]) & _in_band(hc, cp["green_band"])
            flip_local[ys[gr], xs[gr]] = 1
            flip_local[ys[rg], xs[rg]] = -1
        both = pair.compared
        mean_b, mean_c = _mean_lab(pair.base, both), _mean_lab(pair.cand, both)
        lab_means.append(float(deltaE_ciede2000(mean_b[None, None, :], mean_c[None, None, :])[0, 0]) if both.any() else 0.0)
        if fr is None:
            continue
        ly, lx = fr.y - pair.origin[1], fr.x - pair.origin[0]
        sl = (slice(ly, ly + fr.h), slice(lx, lx + fr.w))
        gs = (slice(fr.y, fr.y + fr.h), slice(fr.x, fr.x + fr.w))
        ch = pair.changed[sl]
        changed[gs] |= ch
        aa[gs] |= pair.aa[sl] & ~ch
        compared[gs] |= pair.compared[sl]
        edge[gs] |= e[sl] & pair.compared[sl]
        src[gs] = np.where(ch, idx, src[gs])
        de_map[gs] = np.where(ch, de_local[sl], de_map[gs])
        flip_map[gs] = np.where(ch, flip_local[sl], flip_map[gs])

    # 3.7 granularity 3: connected components over the whole changed map
    comps: list[ComponentDetail] = []
    comp_owner: list[int] = []
    if changed.any():
        n, labels = cv2.connectedComponents(_dilate(changed, p).astype(np.uint8),
                                            connectivity=int(p["components"]["connectivity"]))
        ys, xs = np.nonzero(changed)
        lab = labels[ys, xs]
        order = np.argsort(lab, kind="stable")
        ys, xs, lab = ys[order], xs[order], lab[order]
        starts = np.flatnonzero(np.r_[True, lab[1:] != lab[:-1]])
        ends = np.r_[starts[1:], len(lab)]
        for s, e_ in zip(starts, ends):
            area = int(e_ - s)
            if area < int(p["components"]["min_area_px"]):
                continue
            cy, cx = ys[s:e_], xs[s:e_]
            x0, y0, x1, y1 = int(cx.min()), int(cy.min()), int(cx.max()), int(cy.max())
            bbox = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
            centre_owner = owner[(y0 + y1) // 2, (x0 + x1) // 2]
            srcs = src[cy, cx]
            own = int(centre_owner) if (srcs == centre_owner).any() else int(np.bincount(srcs[srcs >= 0]).argmax())
            pair = pairs[own]
            flips = flip_map[cy, cx]
            share = float(cp["flip_share_min"])
            flip = bool((flips == 1).mean() >= share or (flips == -1).mean() >= share)
            text_edge = float(edge[cy, cx].mean()) >= float(gp["component_text_edge_share"])
            cbox = (bbox[0] + pair.offset[0], bbox[1] + pair.offset[1], bbox[2], bbox[3])
            ccrop = component_crop(C, cbox)
            comps.append(ComponentDetail(
                index=len(comps) + 1, area=area, bbox=bbox,
                centroid=(float(cx.mean()), float(cy.mean())), region_id=pair.region_id,
                mean_delta_e=float(de_map[cy, cx].mean()), color_semantic_flip=flip, text_edge=text_edge,
                phash=phash(ccrop if ccrop.size else component_crop(B, bbox)),
                base_phash=phash(component_crop(B, bbox))))
            comp_owner.append(own)

    # thresholds per region (3.9 table)
    metrics: list[RegionMetrics] = []
    level_above = False
    above_sources: list[str] = []
    for idx, pair in enumerate(pairs):
        rid = pair.region_id
        region = regions_by_id.get(rid)
        th, source = thresholds[rid]
        own_comps = [c for c, o in zip(comps, comp_owner) if o == idx]
        n_changed = int(pair.changed.sum())
        n_cmp = int(pair.compared.sum())
        shift = (0, 0)
        if region is not None:
            lr = rel[region.landmark]
            shift = (lr[0], lr[1])
        if pair.shift is not None and pair.shift.accepted:
            shift = (shift[0] + pair.shift.dx, shift[1] + pair.shift.dy)
        shift_px = max(abs(shift[0]), abs(shift[1]))
        de = lab_means[idx]
        largest = max(own_comps, key=lambda c: c.area, default=None)
        tests = []
        if largest is not None and largest.area >= th.component_area_min:
            tests.append("component")
        if n_changed >= th.changed_px_min:
            tests.append("changed_px")
        if de >= th.delta_e_min:
            tests.append("color")
        if shift_px and shift_px >= th.shift_px_min:
            tests.append("translation")
        color_shift = "color" in tests and n_changed < th.changed_px_min and de > float(cp["color_shift_delta_e"])
        above = bool(tests)
        above_floor = (n_changed > th.floor_changed_px or shift_px > th.floor_shift_px
                       or de > max(th.floor_delta_e, float(cp["color_shift_delta_e"])))
        mean_ssim, block_ssim = _ssim(pair, p)
        h, w = pair.changed.shape
        rb = Rect(pair.origin[0], pair.origin[1], w, h)
        metrics.append(RegionMetrics(
            region_id=rid, kind=region.type if region else "diff", base_rect=rb,
            candidate_rect=rb.offset(pair.offset[0], pair.offset[1]), compared_px=n_cmp, changed_px=n_changed,
            aa_px=int((pair.aa & ~pair.changed).sum()), pixel_fraction=n_changed / n_cmp if n_cmp else 0.0,
            ssim=mean_ssim, ssim_min_block=block_ssim, delta_e=de, shift=shift, shift_px=shift_px,
            components=len(own_comps), largest_component=largest.area if largest else 0,
            largest_bbox=largest.bbox if largest else None,
            text_edge_px=int((pair.changed & edges[idx]).sum()),
            threshold=th, threshold_source=source, above_threshold=above, above_floor=above_floor,
            color_shift=color_shift, tests=tuple(tests)))
        if above:
            level_above = True
            above_sources.append(source)
        if pair.shift is not None and pair.shift.accepted:
            findings.append(_finding(ctx, "layout_shift",
                                     f"{rid} content shifted by ({pair.shift.dx}, {pair.shift.dy}) px",
                                     region_id=rid, evidence={"dx": pair.shift.dx, "dy": pair.shift.dy,
                                                              "residual_components": pair.shift.residual_components}))
        if color_shift:
            findings.append(_finding(ctx, "color_shift", f"{rid} mean color moved by CIEDE2000 {de:.2f}",
                                     region_id=rid, evidence={"delta_e": round(de, 3)}))

    # whole-window translation (granularity 1) and landmark moves
    th_w, src_w = thresholds[WHOLE_ID]
    if window_shift.accepted:
        findings.append(_finding(ctx, "layout_shift",
                                 f"window content shifted by ({window_shift.dx}, {window_shift.dy}) px",
                                 evidence={"dx": window_shift.dx, "dy": window_shift.dy,
                                           "residual_components": window_shift.residual_components}))
        if max(abs(window_shift.dx), abs(window_shift.dy)) >= th_w.shift_px_min:
            level_above = True
            above_sources.append(src_w)
    for lm_id, (dx, dy) in rel.items():
        if (dx, dy) == (0, 0):
            continue
        mins = [thresholds[r.id][0].shift_px_min for r in reg.regions if r.landmark == lm_id and r.pixel_diffed]
        if max(abs(dx), abs(dy)) >= min(mins, default=1):
            findings.append(_finding(ctx, "landmark_moved", f"landmark {lm_id} moved by ({dx}, {dy}) px",
                                     evidence={"landmark": lm_id, "dx": dx, "dy": dy}))

    total_changed = int(changed.sum())
    level = "above" if level_above else ("below" if total_changed else "zero")
    if any(s == "uncalibrated" for s in above_sources):
        source = "uncalibrated"
    elif level_above:
        source = "calibrated"
    else:
        source = "calibrated" if any(s == "calibrated" for _, s in thresholds.values()) else "uncalibrated"

    whole_ssim = _ssim(whole, p)[0] if whole.base.size else 1.0
    n_cmp_total = int(compared.sum())
    summary = DiffSummary(
        whole_pixel_fraction=total_changed / n_cmp_total if n_cmp_total else 0.0,
        whole_ssim=whole_ssim,
        shift=(window_shift.dx, window_shift.dy) if window_shift.accepted else (0, 0),
        regions=[RegionDiff(m.region_id, m.pixel_fraction, m.ssim, m.above_threshold) for m in metrics],
        components=[Component(c.area, c.bbox, c.region_id) for c in comps])
    signal = GlobalShiftSignal(cid, level, any(m.above_floor for m in metrics) or level_above,
                               total_changed, int((changed & edge).sum()), all(c.text_edge for c in comps))
    rects = [m.base_rect for m in metrics[:-1]]
    return DiffResult(level=level, threshold_source=source, summary=summary, findings=findings,
                      heatmap=_heatmap(B, changed, aa, rects, comps, p), regions=metrics, components=comps,
                      landmarks=matches, window_shift=window_shift, budget=budget, signal=signal,
                      changed_map=changed)


def _text_edges(base_crop: np.ndarray, gp: Mapping[str, Any]) -> np.ndarray:
    """Text-edge pixels of 3.7 granularity 0: Canny edges of the base crop dilated by 1 px."""
    k = 2 * int(gp["edge_dilate_px"]) + 1
    e = cv2.Canny(_gray(base_crop), int(gp["canny_low"]), int(gp["canny_high"]))
    return cv2.dilate(e, np.ones((k, k), np.uint8)) > 0


# --------------------------------------------------------------------------------------------
# Granularity 0: run-level global shift pre-pass (3.7, 10.6)
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GlobalShiftDecision:
    """The pre-pass verdict. On a product run `raised` means the run finding
    `global_rendering_shift` and `collapse` lists the checkpoints that become
    `needs_human (reason: global_shift)` in one cluster; on the Nightly run it means
    `harness_error (reason: aa_drift_global)` (`terminal`)."""

    evaluated: bool
    raised: bool
    fraction_above_floor: float
    text_edge_share: float
    collapse: tuple[str, ...] = ()
    terminal: Terminal | None = None

    def finding(self, run_id: str) -> Finding | None:
        if not self.raised or self.terminal is not None:
            return None
        return _finding(DiffContext(run_id=run_id), "global_rendering_shift",
                        f"{len(self.collapse)} checkpoints collapsed into one needs_human (global_shift) cluster",
                        evidence={"fraction_above_floor": round(self.fraction_above_floor, 4),
                                  "text_edge_share": round(self.text_edge_share, 4),
                                  "checkpoints": list(self.collapse)})


def global_shift_prepass(signals: Sequence[GlobalShiftSignal], *, nightly: bool = False,
                         params: Mapping[str, Any] | None = None) -> GlobalShiftDecision:
    """Granularity 0 (3.7): once per run, after at least 20 pairs have been diffed. Raised when more
    than 2 percent of compared checkpoints are above floor and more than 70 percent of their changed
    pixels are text-edge pixels."""
    gp = (params or load_params())["global_shift"]
    if len(signals) < int(gp["min_pairs"]):
        return GlobalShiftDecision(False, False, 0.0, 0.0)
    above = [s for s in signals if s.above_floor]
    frac = len(above) / len(signals)
    changed = sum(s.changed_px for s in above)
    share = sum(s.text_edge_px for s in above) / changed if changed else 1.0
    raised = frac > float(gp["above_floor_fraction"]) and share > float(gp["text_edge_share"])
    if not raised:
        return GlobalShiftDecision(True, False, frac, share)
    if nightly:
        return GlobalShiftDecision(True, True, frac, share,
                                   terminal=Terminal("harness_error", "aa_drift_global",
                                                     detail=f"{len(above)} of {len(signals)} checkpoints above floor"))
    collapse = tuple(s.checkpoint_id or "" for s in signals if s.level == "above" and s.text_edge_only)
    return GlobalShiftDecision(True, True, frac, share, collapse)
