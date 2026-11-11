"""A/A calibration and threshold derivation (3.9, 4.8 "Threshold source", 7.13, 9.7, 10.6).

Calibration measures how much two captures of one build differ when nothing changed. Per
checkpoint: five captures back to back in one session (r1 to r5) and one the following night in a
fresh session (r6); the 15 pairs (10 among r1 to r5, 5 between r6 and each of them) go through the
Diff Engine; the floor per region and metric is the maximum over the pairs; thresholds are derived
mechanically with margin 2:

    component area      max(4, ceil(2 x floor_component_area))
    changed pixels      max(4, ceil(2 x floor_count))
    mean color shift    max(2.0, 2 x floor_deltaE)
    translation         max(1, ceil(2 x floor_shift))

Acceptance: every `diff` and `freeze` region (and `_rest`, the implicit `diff` region) must have a
zero changed-pixel floor; a non-zero floor is a fix-list item with a cause class read off the pair
(`classify_cause`) and a standard fix (`STANDARD_FIX`), closed only by a fix or a human-approved
`floor_exception`. Until every item of a calibration key is closed the key has no thresholds: every
region is `uncalibrated` (threshold 0). A region missing from a complete calibration is
`uncalibrated` on its own (3.9 step 6, 4.8).

What this module holds besides the measurement (`calibrate_checkpoint`):

- Persistence (3.9 steps 3 to 5, 8.3, SD-19, SD-20): `write_calibration` writes one `calibration_floors` row per
  (calibration key, checkpoint, region), the 15 per-pair rows (time to settle included) in `pairs_json`, and one
  `calibration_fixes` row per fix item with its cause, owner, opened date and ticket. `lookup` and
  `StoreCalibration` read them back; `StoreCalibration.lookup(checkpoint, region)` is what the Diff Engine calls,
  so a PR run reads its floors from the store and never calibrates inline. `CalibrationFile` is the
  `calibration/<hex>.yaml` export of 4.16 (`calibration.schema.json`), `<hex>` the calibration key without its
  `sha256:` prefix (SD-36, SD-69); `export_file` builds it from the store.
- Scoped validity (7.13, SD-69): floors are keyed on the rendering key (`canonical.calibration_key`); every floor row
  records what it was measured under (`MeasuredUnder`: the region's definition hash, the masks that touch it, the
  redaction version, the workspace hash, the epoch definition hash), and `invalidation` / `is_valid` compare that
  with the live config (`LiveVersions`, built by `checkpoint_versions`) row by row, so a changed region, mask or
  redaction version uncalibrates that region, a changed workspace or epoch definition that checkpoint, and nothing
  else. `carry_over` copies the floors of a key that differs only in `jre_release` or `render_probe_hash`, with
  their open fix items, once the Nightly A/A on the new key passes (`carry_over_source` finds it).
- Queueing (7.13, 12.7 G5, L-FPR-04): `has_calibration` answers G5's `calibration_record` (and `calibrated_keys` is
  the container `fingerprint.g5` reads); `StoreCalibration` collects every region it answered `uncalibrated` and
  `CalibrationRequest` is the request to queue.
- The Hamming tolerance of 9.7 and 4.14: `region_tolerance` from the A/A noise spread and the mutants' fault
  separation, `bbox_q4` and `exact_match` for the exact fallback, `retune_tolerances` for the Nightly with its
  report lines (`tolerance_changes`).
- The fix list in run health and the digest (3.9 step 6): `open_fixes`, `fix_list_lines`, `fix_list_item`, ages in
  business days (`business_days`), and `overdue` for the 5-business-day quarantine of 10.5.
- `settle_regression_finding` (3.5): the run p50 against the calibration p50, a finding for the flow owner.
- `revoke_floor_exception` (3.9 factor-2 paragraph): a canary miss on a floor-excepted region revokes the exception;
  `revoked_exceptions` reads the revocations back so the region stays `uncalibrated` until it is re-declared.

Choices where the spec is ambiguous (reported to the coordinator):

1. The SSIM "floor" is the minimum over the pairs (the worst similarity), stored for ranking only.
2. A non-zero translation floor on a pixel-diffed region, or on the whole window (`*`), is also a fix-list item:
   3.2 expects a zero floor on every `diff` and `freeze` region. A non-zero CIEDE2000 floor alone is not an item;
   it only raises the color threshold.
3. Cause classes (3.9 step 5), read off the region's pairs in this order, first match wins: the whole window (`*`)
   shifted, `landmark_jitter`; a region translation of at most 2 px, `landmark_jitter` (the template matched a
   pixel off), above 2 px, `scroll`; the two crops hold the same pixel rows in another order, `sort`; every changed
   component a 1 to 2 px wide vertical bar at least 5 px tall, `caret`; the changes within 40 px of the parking
   coordinate, `tooltip` when they form a solid box (at least 20 px wide, 10 to 48 px tall, half filled), else
   `hover`; one capture differs from all the others (every changed pair holds it, at least two pairs, and every
   pair holding it changed) with a solid blob of at least 16 px, `flash`; a `masked_live` checkpoint whose changes
   are glyph-sized (a component 5 to 40 px tall with at least 6 changed pixels) in pairs of more than one capture,
   `live_data`; one capture differs from all the others otherwise, `settle` (captured before it settled);
   anything else, `unknown`. A caller's hint wins. Failed captures map `landmark_missing` to `landmark_jitter` and
   every other failure to `settle`.
4. The standard fix per class follows 3.9's list in order: caret mask or focus drop; parking coordinate; tooltip
   wait; settle anchor (a flash); explicit scroll or sort step; human re-declaration as `rule` or `mask` (live
   data); longer max wait (settle); sharper template (landmark jitter); `unknown` has none and is read by hand.
5. Every non-zero-floor item carries a proposed region (the dilated union of the changing pixels, snapped to the
   region's landmark), not only `live_data` and `flash` items; nothing is applied.
6. The floor row's `regions_file_sha256` is the hash of the region's own definition (type, rect, panel, money
   cell, its landmark's id, template pixels, confidence and search rect, mask ref), so editing one region
   uncalibrates that region only (7.13 "that region"); for `_rest` and `*` it is the hash of every landmark and
   pixel-diffed region, since any of them changes what `_rest` covers. `mask_versions_json` holds the masks that
   touch the region: on the same landmark when the rects meet, on another landmark always (no capture to place
   them), every mask for `_rest` and `*`; a mask added, removed, moved or re-recorded uncalibrates the regions it
   touches. `redaction_version` is recorded only on regions a redaction box touched during calibration (`_rest`
   and `*` when any box was applied): a new redaction list version uncalibrates those regions, since a region no
   box touched measured no redacted pixel.
7. The epoch definition of a checkpoint (7.13 "Data mode or epoch definition") is its data mode plus the fixture
   epoch's `data_delay`; `window_date`, `dataset_id`, roll months and expirations are the dated epoch, outside it.
8. A stored calibration's fix items are linked to its key through the floor they name or, for a failed-capture
   item without a floor, through the fingerprints its Calibration run recorded (their calibration keys). A key is
   complete when none of its open items lacks a live `floor_exception` (with the same ticket or not). The store
   never closes an item for an exception: an item an exception covered at calibration is written open with the
   exception's ticket (`floor_exception_ref`), so it is back on the fix list (and the key incomplete) once the
   exception expires or is removed; only a re-calibration closes a stored item.
9. `CalibrationFile` (the 4.16 export) marks a checkpoint whose calibration had a failed capture with an empty
   region list, and its `settle_p50_ms` is 0 when no time was measured (a real settle takes at least three 250 ms
   frames): the schema has no other place for either. A loaded file rebuilds its fix items from the regions (a
   non-zero floor without `floor_exception: true` is open), with cause `unknown`; the store holds the real cause.
10. Noise spread is the largest pHash distance between the region's crops at its anchored bounds over the A/A
    pairs; fault separation the smallest distance between the pHashes of two distinct mutants of the region, None
    with fewer than two. Without A/A pairs the spread counts as 0. `bbox_q4` snaps the box outward to the 4 px grid
    (x and y down, right and bottom edges up), relative to the region's anchored bounds (the component's
    `bbox_region`).
11. Business days are Monday to Friday minus the holidays the caller passes; the age counts the business days
    after the opened date up to today, so an item opened today is 0 days old; "older than 5 business days" is an
    age above 5.
12. A canary miss revokes a floor exception when the mutant's `region_ref` names the region or its footprint meets
    the region's located rect; the revocation is a `canary_miss` finding (evidence `revokes_floor_exception`, the
    ticket) and holds while the region carries an exception with that ticket.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import cv2
import numpy as np
import yaml

from ..canonical import CALIBRATION_KEY_FIELDS, calibration_filename, calibration_key, sha256_id
from ..records import DATA_MODES, Finding
from . import load_params
from .engine import DiffContext, DiffResult, Terminal, diff_pair
from .phash import hamming, phash, tolerance as default_tolerance
from .regions import (
    REST_ID, UNCALIBRATED, WHOLE_ID, FloorException, Landmark, Rect, RedactionBox, Region, RegionsFile,
    RegionThreshold,
)
from .settle import settle_regression

CAUSE_CLASSES = ("caret", "hover", "tooltip", "flash", "scroll", "sort", "live_data", "settle",
                 "landmark_jitter", "unknown")
FAILED_CAPTURE_OUTCOMES = ("not_settled", "landmark_missing", "env_error", "harness_error")
STANDARD_FIX = {                                   # 3.9 step 5, choice 4
    "caret": "caret mask or focus drop",
    "hover": "parking coordinate",
    "tooltip": "tooltip wait",
    "flash": "settle anchor",
    "scroll": "explicit scroll step",
    "sort": "explicit sort step",
    "live_data": "human re-declaration as rule or mask",
    "settle": "longer max wait",
    "landmark_jitter": "sharper template",
    "unknown": "read the pair by hand",
}
FIX_QUARANTINE_BUSINESS_DAYS = 5                   # 3.9 step 6, 10.5: an older frozen item quarantines
BBOX_QUANTUM_PX = 4                                # 9.7, 4.14 bbox_q4
CARRY_OVER_FIELDS = ("jre_release", "render_probe_hash")   # 7.13, 4.12: floors carry over after a passing A/A
JITTER_MAX_PX = 2                                  # choice 3: a larger region translation is a scroll
NEAR_PARK_PX = 40
TOOLTIP_MIN_W, TOOLTIP_H = 20, (10, 48)
SOLID_FILL = 0.5
FLASH_MIN_AREA = 16
GLYPH_H, GLYPH_MIN_PX = (5, 40), 6
CARET_MAX_W, CARET_MIN_H = 2, 5


def aa_pairs(session: int = 5, next_session: int = 1) -> list[tuple[int, int]]:
    """Index pairs of 3.9 step 3: all pairs among r1..r5, then r6 against each of them (15)."""
    pairs = list(combinations(range(session), 2))
    pairs += [(i, session + j) for j in range(next_session) for i in range(session)]
    return pairs


def _ceil(value: float, margin: float) -> int:
    return int(math.ceil(margin * value - 1e-9))


@dataclass
class RegionFloors:
    """Floors of one region: the maximum over the A/A pairs (minimum for SSIM)."""

    changed_px: int = 0
    pixel_fraction: float = 0.0
    components: int = 0
    component_area: int = 0
    largest_bbox: tuple[int, int, int, int] | None = None
    delta_e: float = 0.0
    shift_px: int = 0
    ssim_min: float = 1.0
    ssim_block_min: float = 1.0

    def add(self, row: Mapping[str, Any]) -> None:
        self.changed_px = max(self.changed_px, int(row["changed_px"]))
        self.pixel_fraction = max(self.pixel_fraction, float(row["pixel_fraction"]))
        self.components = max(self.components, int(row["components"]))
        if int(row["largest_component"]) > self.component_area:
            self.component_area = int(row["largest_component"])
            box = row["largest_bbox"]
            self.largest_bbox = tuple(int(v) for v in box) if box is not None else None
        self.delta_e = max(self.delta_e, float(row["delta_e"]))
        self.shift_px = max(self.shift_px, int(row["shift_px"]))
        self.ssim_min = min(self.ssim_min, float(row["ssim"]))
        self.ssim_block_min = min(self.ssim_block_min, float(row["ssim_min_block"]))

    @property
    def nonzero(self) -> bool:
        return self.changed_px > 0 or self.shift_px > 0

    @staticmethod
    def of(rows: Iterable[Mapping[str, Any]]) -> "RegionFloors":
        """The floors of a list of per-pair rows (3.9 step 4)."""
        fl = RegionFloors()
        for row in rows:
            fl.add(row)
        return fl


def derive_threshold(floors: RegionFloors, params: Mapping[str, Any] | None = None) -> RegionThreshold:
    """The mechanical threshold rule of 3.9 (margin 2); no human picks a number."""
    t = (params or load_params())["thresholds"]
    m = float(t["margin"])
    return RegionThreshold(
        component_area_min=max(int(t["component_area_min"]), _ceil(floors.component_area, m)),
        changed_px_min=max(int(t["changed_px_min"]), _ceil(floors.changed_px, m)),
        delta_e_min=max(float(t["delta_e_min"]), m * floors.delta_e),
        shift_px_min=max(int(t["shift_px_min"]), _ceil(floors.shift_px, m)),
        floor_changed_px=floors.changed_px, floor_component_area=floors.component_area,
        floor_delta_e=floors.delta_e, floor_shift_px=floors.shift_px,
    )


@dataclass(frozen=True)
class Capture:
    """One calibration capture. `image` is None when the capture failed with `outcome`."""

    image: np.ndarray | None
    outcome: str | None = None
    reason: str | None = None
    time_to_settle_ms: int | None = None

    def __post_init__(self) -> None:
        if (self.image is None) != (self.outcome is not None):
            raise ValueError("a capture has an image or a failure outcome, not both")
        if self.outcome is not None and self.outcome not in FAILED_CAPTURE_OUTCOMES:
            raise ValueError(f"calibration capture outcome {self.outcome!r}")


# ------------------------------------------------------------------------------------------------
# Business days and the fix list (3.9 steps 5 and 6, 10.5)

def business_days(start: date, end: date, holidays: Iterable[date] = ()) -> int:
    """Business days after `start` up to and including `end` (choice 11); 0 when `end` is not after `start`."""
    off = frozenset(holidays)
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in off:
            n += 1
    return n


def _day(ts: datetime | date) -> date:
    return ts.astimezone(timezone.utc).date() if isinstance(ts, datetime) else ts


@dataclass
class FixItem:
    """One fix-list item of 3.9 step 5 (one `calibration_fixes` row of 8.3)."""

    checkpoint_id: str
    region_id: str | None
    cause: str
    floors: RegionFloors | None = None
    outcome: str | None = None
    reason: str | None = None
    proposed_landmark: str | None = None
    proposed_rect: Rect | None = None
    floor_exception: FloorException | None = None
    owner: str | None = None
    opened_at: datetime | None = None
    ticket: str | None = None
    exception_ref: str | None = None              # the ticket of the floor exception that closed a stored item
    data_mode: str | None = None
    fix_id: int | None = None

    def __post_init__(self) -> None:
        if self.cause not in CAUSE_CLASSES:
            raise ValueError(f"cause class {self.cause!r} is not one of {CAUSE_CLASSES}")

    @property
    def closed(self) -> bool:
        """Closed by a human-approved floor_exception (a fixed item leaves the list on re-calibration)."""
        return self.floor_exception is not None or self.exception_ref is not None

    @property
    def standard_fix(self) -> str:
        return STANDARD_FIX[self.cause]

    def age_business_days(self, today: date | datetime, holidays: Iterable[date] = ()) -> int | None:
        """Age in business days (choice 11); None when the opened date is unknown."""
        if self.opened_at is None:
            return None
        return business_days(_day(self.opened_at), _day(today), holidays)

    def overdue(self, today: date | datetime, holidays: Iterable[date] = ()) -> bool:
        """3.9 step 6, 10.5: an open item on a `frozen` checkpoint older than 5 business days."""
        age = self.age_business_days(today, holidays)
        return (not self.closed and self.data_mode == "frozen" and age is not None
                and age > FIX_QUARANTINE_BUSINESS_DAYS)

    def label(self) -> str:
        """The item as run health names it: checkpoint, then region or the failed capture's outcome."""
        what = self.region_id if self.region_id is not None else \
            f"failed capture {self.outcome}" + (f" ({self.reason})" if self.reason else "")
        return f"{self.checkpoint_id} {what}"

    def text(self, today: date | datetime, holidays: Iterable[date] = ()) -> str:
        """`item, cause, owner, age in business days` (3.9 step 6) plus the standard fix."""
        age = self.age_business_days(today, holidays)
        aged = "age unknown" if age is None else f"{age} business day{'s' if age != 1 else ''}"
        return (f"{self.label()}: {self.cause}, owner {self.owner or 'unassigned'}, {aged} "
                f"(standard fix: {self.standard_fix})" + (f", ticket {self.ticket}" if self.ticket else ""))


def fix_list_lines(items: Iterable[FixItem], today: date | datetime, holidays: Iterable[date] = ()) -> list[str]:
    """The open fix list for run health and the daily digest (3.9 step 6), oldest first."""
    hol = tuple(holidays)
    open_ = [i for i in items if not i.closed]
    open_.sort(key=lambda i: (-(i.age_business_days(today, hol) or 0), i.checkpoint_id, i.region_id or ""))
    return [i.text(today, hol) for i in open_]


def fix_list_item(items: Iterable[FixItem], checkpoint_id: str, region_id: str | None = None,
                  today: date | datetime | None = None) -> str | None:
    """The fix item a `needs_human (reason: uncalibrated)` outcome names (`classify.Facts.fix_list_item`): the open
    item of the region, else the checkpoint's first open item; None when it has none."""
    mine = [i for i in items if i.checkpoint_id == checkpoint_id and not i.closed]
    pick = next((i for i in mine if i.region_id == region_id), mine[0] if mine else None)
    if pick is None:
        return None
    return pick.text(today) if today is not None else f"{pick.label()}: {pick.cause}, owner {pick.owner or 'unassigned'}"


def overdue(items: Iterable[FixItem], today: date | datetime, holidays: Iterable[date] = ()) -> list[FixItem]:
    """Items that quarantine their checkpoint (3.9 step 6, 10.5 reason `aa_floor`): open, frozen, older than 5
    business days."""
    hol = tuple(holidays)
    return [i for i in items if i.overdue(today, hol)]


# ------------------------------------------------------------------------------------------------
# The Hamming tolerance of 9.7 and 4.14

def _hash(value: Any) -> str:
    return value if isinstance(value, str) else phash(np.asarray(value))


def noise_spread(pairs: Iterable[tuple[Any, Any]]) -> int | None:
    """The largest pHash distance among a region's A/A pairs (9.7); None without pairs. Each side is a 16-hex
    pHash or a crop."""
    ds = [hamming(_hash(a), _hash(b)) for a, b in pairs]
    return max(ds) if ds else None


def fault_separation(mutants: Iterable[Any]) -> int | None:
    """The smallest pHash distance between two distinct mutants of a region (9.7); None with fewer than two."""
    hs = [_hash(m) for m in mutants]
    ds = [hamming(a, b) for a, b in combinations(hs, 2)]
    return min(ds) if ds else None


@dataclass(frozen=True)
class Tolerance:
    """A region's signature-matching tolerance (9.7, 4.14) and the inputs it came from."""

    hamming_tolerance: int
    exact_fallback: bool
    noise_spread: int | None = None
    fault_separation: int | None = None

    def describe(self) -> str:
        how = "exact pHash plus the 4 px box" if self.exact_fallback else f"Hamming {self.hamming_tolerance}"
        return f"{how} (noise spread {self.noise_spread}, fault separation {self.fault_separation})"


def region_tolerance(aa_pairs: Iterable[tuple[Any, Any]] = (), mutant_crops: Iterable[Any] = (), *,
                     tier: int | None = None, spread: int | None = None) -> Tolerance:
    """9.7: the smaller of the default (8, or 6 for tier 1) and fault separation minus 1; at or below the noise
    spread it is 0 and matching falls back to pHash equality plus the 4 px box (`exact_match`). `spread` passes a
    stored noise spread instead of the pairs (the Nightly re-tune)."""
    ns = noise_spread(aa_pairs) if spread is None else spread
    fs = fault_separation(mutant_crops)
    tol = default_tolerance(tier) if fs is None else min(default_tolerance(tier), fs - 1)
    if tol <= (ns or 0):
        return Tolerance(0, True, ns, fs)
    return Tolerance(tol, False, ns, fs)


def bbox_q4(bbox: Sequence[int], quantum: int = BBOX_QUANTUM_PX) -> tuple[int, int, int, int]:
    """The largest component's box snapped outward to the 4 px grid (9.7, 4.14 `bbox_q4`; choice 10)."""
    x, y, w, h = (int(v) for v in bbox)
    x0, y0 = quantum * (x // quantum), quantum * (y // quantum)
    x1, y1 = quantum * -(-(x + w) // quantum), quantum * -(-(y + h) // quantum)
    return x0, y0, x1 - x0, y1 - y0


def exact_match(decision_phash: str, decision_bbox_q4: Sequence[int] | None, finding_phash: str,
                finding_bbox: Sequence[int] | None) -> bool:
    """The exact fallback of 9.7: equal pHash and equal 4 px-quantized boxes (tolerance 0)."""
    if decision_bbox_q4 is None or finding_bbox is None:
        return False
    return decision_phash == finding_phash and tuple(decision_bbox_q4) == bbox_q4(finding_bbox)


def tolerance_changes(before: Mapping[tuple[str, str], Tolerance | None],
                      after: Mapping[tuple[str, str], Tolerance]) -> list[str]:
    """The Nightly report lines of 9.7 ("Changes print in the Nightly report"), one per region whose tolerance or
    fallback changed."""
    lines = []
    for (cid, rid), new in sorted(after.items()):
        old = before.get((cid, rid))
        if old is not None and (old.hamming_tolerance, old.exact_fallback) == (new.hamming_tolerance,
                                                                               new.exact_fallback):
            continue
        was = "default" if old is None else ("exact" if old.exact_fallback else str(old.hamming_tolerance))
        lines.append(f"{cid} {rid}: tolerance {was} -> {new.describe()}")
    return lines


# ------------------------------------------------------------------------------------------------
# Cause classes (3.9 step 5, choice 3)

@dataclass(frozen=True)
class PairEvidence:
    """What one A/A pair shows in one region: the changed pixels (region-local), the two crops, the translation,
    where the region sits on the frame and the parking coordinate. `changed` None is a clean pair."""

    pair: tuple[int, int]
    changed: np.ndarray | None = None
    base: np.ndarray | None = None
    candidate: np.ndarray | None = None
    shift_px: int = 0
    delta_e: float = 0.0
    origin: tuple[int, int] = (0, 0)
    park: tuple[int, int] | None = None
    data_mode: str = "frozen"
    whole: bool = False

    @property
    def is_changed(self) -> bool:
        return self.shift_px > 0 or (self.changed is not None and bool(self.changed.any()))


def _components(mask: np.ndarray, dilate: int = 0) -> list[tuple[int, int, int, int, int]]:
    """(x, y, w, h, changed px) of each 8-connected component, after an optional square dilation."""
    m = mask.astype(np.uint8)
    if dilate:
        m = cv2.dilate(m, np.ones((dilate, dilate), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, _ = (int(v) for v in stats[k])
        out.append((x, y, w, h, int(mask[labels == k].sum())))
    return out


def _union_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def _permuted(a: np.ndarray | None, b: np.ndarray | None) -> bool:
    """The same pixel rows in another order (a sorted grid)."""
    if a is None or b is None or a.shape != b.shape or a.shape[0] < 2:
        return False
    ra, rb = [r.tobytes() for r in a], [r.tobytes() for r in b]
    return ra != rb and sorted(ra) == sorted(rb)


def _caret(ev: PairEvidence) -> bool:
    comps = _components(ev.changed) if ev.changed is not None else []
    return bool(comps) and len(comps) <= 2 and all(w <= CARET_MAX_W and h >= max(CARET_MIN_H, 3 * w)
                                                    for _, _, w, h, _ in comps)


def _near_park(ev: PairEvidence) -> tuple[int, int, int, int] | None:
    """The changed union in frame coordinates when it lies within 40 px of the parking coordinate."""
    if ev.park is None or ev.changed is None:
        return None
    box = _union_box(ev.changed)
    if box is None:
        return None
    x, y, w, h = box
    x, y = x + ev.origin[0], y + ev.origin[1]
    px, py = ev.park
    dx, dy = max(x - px, 0, px - (x + w - 1)), max(y - py, 0, py - (y + h - 1))
    return (x, y, w, h) if math.hypot(dx, dy) <= NEAR_PARK_PX else None


def _solid(mask: np.ndarray, box: tuple[int, int, int, int]) -> float:
    x, y, w, h = box
    return float(mask[y:y + h, x:x + w].sum()) / float(max(1, w * h))


def _odd_capture(evidence: Sequence[PairEvidence]) -> int | None:
    """The one capture that differs from all the others (choice 3), or None."""
    changed = [e.pair for e in evidence if e.is_changed]
    if len(changed) < 2:
        return None
    common = set(changed[0]).intersection(*map(set, changed[1:]))
    if len(common) != 1:
        return None
    k = next(iter(common))
    holding = [e for e in evidence if k in e.pair]
    return k if all(e.is_changed for e in holding) else None


def _glyphs(ev: PairEvidence) -> bool:
    if ev.changed is None:
        return False
    return any(GLYPH_H[0] <= h <= GLYPH_H[1] and n >= GLYPH_MIN_PX for _, _, _, h, n in _components(ev.changed, 3))


def classify_cause(pairs: PairEvidence | Sequence[PairEvidence]) -> str:
    """The cause class of a region's non-zero floor read off its A/A pairs (3.9 step 5, choice 3). Pass every pair
    the region was compared in, clean ones included, so a capture that differs alone can be told apart."""
    evidence = [pairs] if isinstance(pairs, PairEvidence) else list(pairs)
    changed = [e for e in evidence if e.is_changed]
    if not changed:
        return "unknown"
    if any(e.whole for e in changed):
        return "landmark_jitter"
    shifted = [e.shift_px for e in changed if e.shift_px > 0]
    if shifted:
        return "scroll" if max(shifted) > JITTER_MAX_PX else "landmark_jitter"
    if any(_permuted(e.base, e.candidate) for e in changed):
        return "sort"
    if all(_caret(e) for e in changed):
        return "caret"
    near = [(e, _near_park(e)) for e in changed]
    if all(box is not None for _, box in near):
        tooltip = all(box[2] >= TOOLTIP_MIN_W and TOOLTIP_H[0] <= box[3] <= TOOLTIP_H[1]
                      and _solid(e.changed, (box[0] - e.origin[0], box[1] - e.origin[1], box[2], box[3])) >= SOLID_FILL
                      for e, box in near)
        return "tooltip" if tooltip else "hover"
    odd = _odd_capture(evidence)
    if odd is not None and any(w * h >= FLASH_MIN_AREA and n >= SOLID_FILL * w * h
                               for e in changed if e.changed is not None for _, _, w, h, n in _components(e.changed)):
        return "flash"
    if odd is None and changed[0].data_mode == "masked_live" and all(_glyphs(e) for e in changed) and \
            len({i for e in changed for i in e.pair}) > 2:
        return "live_data"
    if odd is not None:
        return "settle"
    return "unknown"


# ------------------------------------------------------------------------------------------------
# What a floor was measured under, and the live config it is checked against (7.13, SD-69)

@dataclass(frozen=True)
class MeasuredUnder:
    """The 7.13 inputs one floor row records (8.3 calibration_floors): the region's definition hash, the masks
    that touch it, the redaction version (only on a region a redaction box touched), the workspace hash and the
    epoch definition hash of its checkpoint (choices 6 and 7)."""

    region_sha256: str | None = None
    mask_versions: Mapping[str, str] = field(default_factory=dict)
    redaction_version: str | None = None
    workspace_sha256: str | None = None
    epoch_definition_sha256: str | None = None


@dataclass(frozen=True)
class LiveVersions:
    """The live config of one checkpoint for `invalidation`: per region its definition hash and touching masks,
    the run's redaction version, the workspace and epoch definition hashes, and the floor exceptions in force
    (region id -> ticket)."""

    regions: Mapping[str, str] = field(default_factory=dict)
    masks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    redaction_version: str | None = None
    workspace_sha256: str | None = None
    epoch_definition_sha256: str | None = None
    floor_exceptions: Mapping[str, str] = field(default_factory=dict)

    def measured(self, region_id: str, *, redacted: bool) -> MeasuredUnder:
        """What a floor of `region_id` measured now records (`redacted`: a redaction box touched the region)."""
        return MeasuredUnder(self.regions.get(region_id), dict(self.masks.get(region_id, {})),
                             self.redaction_version if redacted else None, self.workspace_sha256,
                             self.epoch_definition_sha256)


def _template_sha(tpl: np.ndarray) -> str:
    arr = np.ascontiguousarray(tpl)
    return hashlib.sha256(repr((arr.shape, str(arr.dtype))).encode() + arr.tobytes()).hexdigest()


def _landmark_def(lm: Landmark) -> dict:
    return {"id": lm.id, "template": _template_sha(lm.template), "confidence_min": lm.confidence_min,
            "search_rect": None if lm.search_rect is None else list(lm.search_rect.as_tuple())}


def _region_def(reg: RegionsFile, r: Region) -> dict:
    lm = next((x for x in reg.landmarks if x.id == r.landmark), None)
    return {"id": r.id, "type": r.type, "rect": list(r.rect.as_tuple()), "panel": r.panel, "money_cell": r.money_cell,
            "mask_ref": r.mask_ref, "landmark": None if lm is None else _landmark_def(lm)}


def mask_version(record: Any) -> str:
    """A mask record's version: the hash of its fields (a renewal or an edit is a new version)."""
    if isinstance(record, Mapping):
        body = {k: str(v) for k, v in record.items()}
    else:
        body = {k: str(getattr(record, k)) for k in ("mask_id", "checkpoint_id", "region_id", "created_at",
                                                     "expires_at", "decision_ref")}
    return sha256_id(body)


def epoch_definition_sha256(data_mode: str, epoch: Mapping[str, Any] | None = None) -> str:
    """The epoch definition of a checkpoint (7.13, choice 7): its data mode and the epoch's data delay."""
    return sha256_id({"data_mode": data_mode, "data_delay": str((epoch or {}).get("data_delay"))})


def checkpoint_versions(registry: RegionsFile, *, mask_versions: Mapping[str, str] | None = None,
                        redaction_version: str | None = None, workspace_sha256: str | None = None,
                        epoch_definition_sha256: str | None = None) -> LiveVersions:
    """The 7.13 versions of one checkpoint's effective registry (choice 6), the same function at calibration and at
    run time. `mask_versions` maps a mask id to its record's version (`mask_version`); a mask without one is
    versioned by its id."""
    mv = mask_versions or {}
    masks = [r for r in registry.regions if r.type == "mask"]
    diffed = [r for r in registry.regions if r.pixel_diffed]
    defs = {r.id: sha256_id(_region_def(registry, r)) for r in diffed}
    whole = sha256_id({"landmarks": sorted((_landmark_def(lm) for lm in registry.landmarks), key=lambda d: d["id"]),
                       "regions": sorted(defs.values())})

    def version(m: Region) -> str:
        return sha256_id({"region": _region_def(registry, m), "record": mv.get(m.mask_ref or "", m.mask_ref)})

    touching: dict[str, dict[str, str]] = {}
    for r in diffed:
        touching[r.id] = {m.id: version(m) for m in masks
                          if m.landmark != r.landmark or m.rect.intersect(r.rect) is not None}
    every = {m.id: version(m) for m in masks}
    for rid in (REST_ID, WHOLE_ID):
        defs[rid] = whole
        touching[rid] = every
    exceptions = {r.id: r.floor_exception.ticket for r in registry.regions if r.floor_exception is not None}
    return LiveVersions(defs, touching, redaction_version, workspace_sha256, epoch_definition_sha256, exceptions)


def invalidation(measured: MeasuredUnder, live: LiveVersions, region_id: str) -> str | None:
    """The 7.13 row that invalidates a floor under the live config, or None when the floor is valid: `workspace`,
    `epoch_definition` (the checkpoint), `region`, `mask` or `redaction_version` (the region). A version neither
    side records is not compared."""
    def differs(old: str | None, new: str | None) -> bool:
        return old is not None and new is not None and old != new

    if differs(measured.workspace_sha256, live.workspace_sha256):
        return "workspace"
    if differs(measured.epoch_definition_sha256, live.epoch_definition_sha256):
        return "epoch_definition"
    if measured.region_sha256 is not None and measured.region_sha256 != live.regions.get(region_id):
        return "region"
    if dict(measured.mask_versions) != dict(live.masks.get(region_id, {})):
        return "mask"
    if measured.redaction_version is not None and measured.redaction_version != live.redaction_version:
        return "redaction_version"
    return None


# ------------------------------------------------------------------------------------------------
# One checkpoint's calibration (3.9 steps 2 to 5)

@dataclass
class CheckpointCalibration:
    """The calibration of one checkpoint on one calibration key."""

    checkpoint_id: str
    data_mode: str | None
    pair_stats: dict[str, list[dict]] = field(default_factory=dict)
    floors: dict[str, RegionFloors] = field(default_factory=dict)
    thresholds: dict[str, RegionThreshold] = field(default_factory=dict)
    fix_list: list[FixItem] = field(default_factory=list)
    settle_p50_ms: float | None = None
    tolerances: dict[str, Tolerance] = field(default_factory=dict)
    redacted: set[str] = field(default_factory=set)         # regions a redaction box touched (choice 6)

    @property
    def accepted(self) -> bool:
        return all(item.closed for item in self.fix_list)

    @property
    def failed(self) -> bool:
        """A capture of the six failed (3.9 step 2): the checkpoint has no valid floors."""
        return any(item.region_id is None for item in self.fix_list)

    def floors_sorted(self) -> list[tuple[str, RegionFloors]]:
        """Floors published sorted by changed-pixel floor descending (3.9 step 4)."""
        return sorted(self.floors.items(), key=lambda kv: (-kv[1].changed_px, -kv[1].component_area, kv[0]))

    def excepted(self, region_id: str) -> bool:
        """The region's non-zero floor is closed by a floor exception (4.16 `floor_exception: true`)."""
        return any(i.region_id == region_id and i.closed for i in self.fix_list)


def _row(metrics: Any, settle_ms: Sequence[int | None]) -> dict:
    return {"changed_px": metrics.changed_px, "pixel_fraction": metrics.pixel_fraction,
            "components": metrics.components, "largest_component": metrics.largest_component,
            "largest_bbox": list(metrics.largest_bbox) if metrics.largest_bbox else None,
            "ssim": metrics.ssim, "ssim_min_block": metrics.ssim_min_block, "delta_e": metrics.delta_e,
            "shift": list(metrics.shift), "shift_px": metrics.shift_px, "settle_ms": list(settle_ms)}


def _whole_row(shift: tuple[int, int], settle_ms: Sequence[int | None]) -> dict:
    return {"changed_px": 0, "pixel_fraction": 0.0, "components": 0, "largest_component": 0, "largest_bbox": None,
            "ssim": 1.0, "ssim_min_block": 1.0, "delta_e": 0.0, "shift": list(shift),
            "shift_px": max(abs(shift[0]), abs(shift[1])), "settle_ms": list(settle_ms)}


def _crop(img: np.ndarray, r: Rect) -> np.ndarray | None:
    c = r.intersect(Rect(0, 0, img.shape[1], img.shape[0]))
    return None if c is None or c.area == 0 else img[c.y:c.y + c.h, c.x:c.x + c.w]


def calibrate_checkpoint(captures: Sequence[Capture], registry: RegionsFile, *, data_mode: str,
                         redactions: Sequence[RedactionBox] = (), system_masks: Sequence[Rect] = (),
                         panel_bounds: Mapping[str, Mapping[str, Rect]] | None = None,
                         now: datetime | None = None, cause_hints: Mapping[str, str] | None = None,
                         params: Mapping[str, Any] | None = None, park: tuple[int, int] | None = None,
                         tier: int | None = None, owner: str | None = None,
                         mutant_crops: Mapping[str, Sequence[Any]] | None = None) -> CheckpointCalibration:
    """Run 3.9 steps 3 to 5 for one checkpoint over its six captures (r1..r5, r6).

    `registry` is the effective Region Registry entry; a region's live `floor_exception` (not
    expired at `now`) closes its non-zero-floor item. Region floors and thresholds are kept for
    every pixel-diffed region plus `_rest` and the whole window (`*`). `redactions`,
    `system_masks` (the parking rectangle, `regions.park_rect`) and `panel_bounds` are passed to
    every `diff_pair` exactly as a product run passes them (3.9 step 2, 3.5). `park` is the parking
    coordinate the cause classes read (hover, tooltip); `tier` and `mutant_crops` (region id -> the
    region's crops or pHashes of its distinct canary mutants) give each region's Hamming tolerance
    (9.7); fix items carry `owner` and are opened at `now`.
    """
    p = params or load_params()
    cp = p["calibration"]
    n_s, n_n = int(cp["session_captures"]), int(cp["next_session_captures"])
    if len(captures) != n_s + n_n:
        raise ValueError(f"calibration needs {n_s + n_n} captures, got {len(captures)}")
    if data_mode not in DATA_MODES:
        raise ValueError(f"data_mode {data_mode!r}")
    cid = registry.checkpoint_id
    cal = CheckpointCalibration(cid, data_mode)
    hints = cause_hints or {}

    def item(region_id: str | None, cause: str, **kw: Any) -> FixItem:
        return FixItem(cid, region_id, cause, owner=owner, opened_at=now, data_mode=data_mode, **kw)

    for c in captures:                         # one item per failure, however many captures it took (3.9 step 2)
        if c.outcome is not None and not any((f.outcome, f.reason) == (c.outcome, c.reason) for f in cal.fix_list):
            cause = "landmark_jitter" if c.outcome == "landmark_missing" else "settle"
            cal.fix_list.append(item(None, cause, outcome=c.outcome, reason=c.reason))
    times = [c.time_to_settle_ms for c in captures if c.time_to_settle_ms is not None]
    cal.settle_p50_ms = float(statistics.median(times)) if times else None

    region_ids = [r.id for r in registry.regions if r.pixel_diffed] + [REST_ID]
    floors = {rid: RegionFloors() for rid in region_ids + [WHOLE_ID]}
    unions: dict[str, np.ndarray] = {}
    anchor_pos: dict[str, tuple[int, int]] = {}
    evidence: dict[str, list[PairEvidence]] = {}
    hashes: dict[str, list[tuple[str, str]]] = {}
    for i, j in aa_pairs(n_s, n_n):
        a, b = captures[i], captures[j]
        if a.image is None or b.image is None:
            continue
        res = diff_pair(a.image, b.image, registry, redactions=redactions, system_masks=system_masks,
                        panel_bounds=panel_bounds, context=DiffContext(run_id="calibration", checkpoint_id=cid),
                        params=p)
        if res.terminal is not None:
            t = res.terminal
            cause = "landmark_jitter" if t.outcome == "landmark_missing" else "settle"
            if not any((f.outcome, f.reason) == (t.outcome, t.reason) for f in cal.fix_list):
                cal.fix_list.append(item(None, cause, outcome=t.outcome, reason=t.reason))
            continue
        settle_ms = (a.time_to_settle_ms, b.time_to_settle_ms)
        ws = res.window_shift
        shift = (ws.dx, ws.dy) if ws is not None and ws.accepted else (0, 0)
        cal.pair_stats.setdefault(WHOLE_ID, []).append({**_whole_row(shift, settle_ms), "pair": [i, j]})
        floors[WHOLE_ID].add(cal.pair_stats[WHOLE_ID][-1])
        evidence.setdefault(WHOLE_ID, []).append(PairEvidence((i, j), shift_px=max(map(abs, shift)), whole=True))
        for lm_id, (mb, _) in res.landmarks.items():
            anchor_pos.setdefault(lm_id, mb.pos)
        cmap = res.changed_map
        for m in res.regions:
            row = {**_row(m, settle_ms), "pair": [i, j]}
            cal.pair_stats.setdefault(m.region_id, []).append(row)
            floors[m.region_id].add(row)
            if redactions and (m.region_id == REST_ID or any(
                    bx.rect.intersect(m.base_rect) is not None for bx in redactions)):
                cal.redacted.add(m.region_id)
            ca, cb = _crop(a.image, m.base_rect), _crop(b.image, m.candidate_rect)
            if m.region_id != REST_ID and ca is not None and cb is not None:
                hashes.setdefault(m.region_id, []).append((phash(ca), phash(cb)))
            local = None
            if cmap is not None and (m.changed_px or m.shift_px):
                local = _crop(cmap, m.base_rect)
            evidence.setdefault(m.region_id, []).append(PairEvidence(
                (i, j), local, ca if local is not None else None, cb if local is not None else None, m.shift_px,
                m.delta_e, (m.base_rect.x, m.base_rect.y), park, data_mode))
        if redactions:
            cal.redacted.add(WHOLE_ID)
        if cmap is not None and cmap.any():
            frame = Rect(0, 0, cmap.shape[1], cmap.shape[0])
            declared = np.zeros_like(cmap)
            for m in res.regions:
                r = m.base_rect.intersect(frame)
                if m.region_id == REST_ID or r is None:
                    continue
                local = np.zeros_like(cmap)
                local[r.y:r.y + r.h, r.x:r.x + r.w] = cmap[r.y:r.y + r.h, r.x:r.x + r.w]
                declared[r.y:r.y + r.h, r.x:r.x + r.w] = True
                unions[m.region_id] = unions.get(m.region_id, np.zeros_like(cmap)) | local
            unions[REST_ID] = unions.get(REST_ID, np.zeros_like(cmap)) | (cmap & ~declared)

    by_id = {r.id: r for r in registry.regions}
    crops = mutant_crops or {}
    for rid, fl in floors.items():
        cal.floors[rid] = fl
        cal.thresholds[rid] = derive_threshold(fl, p)
        if rid != WHOLE_ID:
            cal.tolerances[rid] = region_tolerance(hashes.get(rid, ()), crops.get(rid, ()), tier=tier)
        if not fl.nonzero:
            continue
        region = by_id.get(rid)
        cause = hints.get(rid) or classify_cause(evidence.get(rid, []))
        exc = region.floor_exception if region is not None else None
        if exc is not None and now is not None and exc.expired(now):
            exc = None
        landmark, rect = _proposal(unions.get(rid), region.landmark if region else None, anchor_pos, p)
        cal.fix_list.append(item(rid, cause, floors=fl, proposed_landmark=landmark, proposed_rect=rect,
                                 floor_exception=exc))
    return cal


def _proposal(union: np.ndarray | None, landmark: str | None, anchor_pos: Mapping[str, tuple[int, int]],
              params: Mapping[str, Any]) -> tuple[str | None, Rect | None]:
    """The dilated union of changing pixels, as an offset from the region's landmark (3.9 step 5)."""
    if union is None or not union.any():
        return landmark, None
    kw, kh = params["components"]["dilate_kernel_wh"]
    dil = cv2.dilate(union.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (int(kw), int(kh)))) > 0
    ys, xs = np.nonzero(dil)
    r = Rect(int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    if landmark is not None and anchor_pos.get(landmark) is not None:
        lx, ly = anchor_pos[landmark]
        r = r.offset(-lx, -ly)
    return landmark, r


# ------------------------------------------------------------------------------------------------
# The 4.16 export (calibration/<hex>.yaml)

def _utc_text(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CalibrationFile:
    """`calibration/<hex>.yaml` (4.8, 4.12, 4.16): per checkpoint and region, the four derived thresholds plus the
    measured floors, for one calibration key; `<hex>` is the key without its `sha256:` prefix
    (`canonical.calibration_filename`, SD-36 with SD-69). `fingerprint_id` is the fingerprint the Calibration run
    captured on (the key when unknown)."""

    calibration_key: str
    calibration_run_id: str
    checkpoints: dict[str, CheckpointCalibration] = field(default_factory=dict)
    fingerprint_id: str | None = None
    exported_at: datetime | None = None

    @property
    def complete(self) -> bool:
        """3.9 step 5: complete when every fix-list item is fixed or carries an approved exception."""
        return all(c.accepted for c in self.checkpoints.values())

    def fix_list(self) -> list[FixItem]:
        return [item for c in self.checkpoints.values() for item in c.fix_list if not item.closed]

    def lookup(self, checkpoint_id: str, region_id: str) -> tuple[RegionThreshold, str]:
        """Threshold and source for one region (4.8 "Threshold source"): `calibrated` when the file
        is complete and has the region; otherwise `UNCALIBRATED` (threshold 0, `uncalibrated`)."""
        if not self.complete:
            return UNCALIBRATED, "uncalibrated"
        cal = self.checkpoints.get(checkpoint_id)
        if cal is None or region_id not in cal.thresholds:
            return UNCALIBRATED, "uncalibrated"
        return cal.thresholds[region_id], "calibrated"

    def settle_p50_ms(self, checkpoint_id: str) -> float | None:
        cal = self.checkpoints.get(checkpoint_id)
        return cal.settle_p50_ms if cal else None

    # -- persistence --------------------------------------------------------------------------
    def to_dict(self) -> dict:
        """The 4.16 document (`calibration.schema.json`); regions sorted by floor descending (3.9 step 4)."""
        cps = []
        for cid, c in sorted(self.checkpoints.items()):
            regions = []
            for rid, fl in ([] if c.failed else c.floors_sorted()):
                th = c.thresholds[rid]
                rec: dict[str, Any] = {
                    "region_id": rid,
                    "floors": {"changed_px": fl.changed_px, "component_area": fl.component_area,
                               "delta_e": float(fl.delta_e), "shift_px": fl.shift_px},
                    "thresholds": {"component_area_min": th.component_area_min, "changed_px_min": th.changed_px_min,
                                   "delta_e_min": float(th.delta_e_min), "shift_px_min": th.shift_px_min},
                    "ssim_min": float(fl.ssim_min)}
                tol = c.tolerances.get(rid)
                if tol is not None:
                    for k in ("noise_spread", "fault_separation", "hamming_tolerance"):
                        if getattr(tol, k) is not None:
                            rec[k] = int(getattr(tol, k))
                    rec["exact_fallback"] = tol.exact_fallback
                rec["floor_exception"] = c.excepted(rid)
                regions.append(rec)
            p50 = c.settle_p50_ms
            cps.append({"checkpoint_id": cid, "settle_p50_ms": int(round(p50)) if p50 else 0, "regions": regions})
        return {"schema_version": 1, "calibration_key": self.calibration_key,
                "fingerprint_id": self.fingerprint_id or self.calibration_key,
                "calibration_run_id": self.calibration_run_id,
                "exported_at": _utc_text(self.exported_at or datetime.now(timezone.utc)), "checkpoints": cps}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "CalibrationFile":
        out = CalibrationFile(str(d["calibration_key"]), str(d["calibration_run_id"]),
                              fingerprint_id=d.get("fingerprint_id"),
                              exported_at=datetime.fromisoformat(str(d["exported_at"])) if d.get("exported_at") else None)
        for c in d.get("checkpoints", []):
            cid = str(c["checkpoint_id"])
            cal = CheckpointCalibration(cid, None, settle_p50_ms=float(c["settle_p50_ms"]) or None)
            regions = c.get("regions") or []
            if not regions:
                cal.fix_list.append(FixItem(cid, None, "unknown", reason="failed capture (calibration_fixes holds it)"))
            for rec in regions:
                rid, f, t = str(rec["region_id"]), rec["floors"], rec["thresholds"]
                fl = RegionFloors(changed_px=int(f["changed_px"]), component_area=int(f["component_area"]),
                                  delta_e=float(f["delta_e"]), shift_px=int(f["shift_px"]),
                                  ssim_min=float(rec.get("ssim_min", 1.0)))
                cal.floors[rid] = fl
                cal.thresholds[rid] = RegionThreshold(
                    int(t["component_area_min"]), int(t["changed_px_min"]), float(t["delta_e_min"]),
                    int(t["shift_px_min"]), fl.changed_px, fl.component_area, fl.delta_e, fl.shift_px)
                if "hamming_tolerance" in rec:
                    cal.tolerances[rid] = Tolerance(int(rec["hamming_tolerance"]), bool(rec.get("exact_fallback")),
                                                    rec.get("noise_spread"), rec.get("fault_separation"))
                if fl.nonzero:
                    cal.fix_list.append(FixItem(cid, rid, "unknown", floors=fl, exception_ref=(
                        "floor_exception" if rec.get("floor_exception") else None)))
            out.checkpoints[cid] = cal
        return out

    def save(self, directory: str | os.PathLike) -> Path:
        path = Path(directory) / calibration_filename(self.calibration_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        return path

    @staticmethod
    def load(directory: str | os.PathLike, calibration_key: str) -> "CalibrationFile | None":
        """The file for a calibration key, or None when none exists (every region `uncalibrated`, L-FPR-04)."""
        path = Path(directory) / calibration_filename(calibration_key)
        if not path.exists():
            return None
        return CalibrationFile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------------------
# The store: calibration_floors and calibration_fixes (3.9, 8.3, SD-19, SD-20)

def _pct(values: Sequence[int], q: float) -> int:
    s = sorted(values)
    return int(s[min(len(s) - 1, int(math.ceil(q * len(s))) - 1)]) if s else 0


def floor_rows(cal: CheckpointCalibration, *, calibration_key: str, fingerprint_id: str | None, run_id: str,
               live: LiveVersions | None, valid_from: datetime) -> list[dict]:
    """The `calibration_floors` rows of one checkpoint (3.9 steps 3 and 4, 8.3): every region with pairs, its four
    thresholds, the 15 per-pair rows in `pairs_json`, its tolerance and what it was measured under (7.13). A
    checkpoint with a failed capture writes none (it has no valid floor)."""
    if cal.failed:
        return []
    lv = live or LiveVersions()
    rows = []
    for rid, fl in cal.floors_sorted():
        pairs = cal.pair_stats.get(rid, [])
        if not pairs:
            continue
        th, tol = cal.thresholds[rid], cal.tolerances.get(rid)
        mu = lv.measured(rid, redacted=rid in cal.redacted)
        px = [int(r["changed_px"]) for r in pairs]
        rows.append({
            "checkpoint_id": cal.checkpoint_id, "region_id": rid, "rendering_key_sha256": calibration_key,
            "fingerprint_sha256": fingerprint_id, "calibration_run_id": run_id, "samples": len(pairs),
            "diff_pixels_p50": _pct(px, 0.5), "diff_pixels_p95": _pct(px, 0.95), "diff_pixels_max": max(px),
            "ssim_min": float(fl.ssim_min), "components_max": fl.components,
            "largest_bbox_max": ",".join(map(str, fl.largest_bbox)) if fl.largest_bbox else None,
            "noise_spread": tol.noise_spread if tol else None, "fault_separation": tol.fault_separation if tol else None,
            "hamming_tolerance": tol.hamming_tolerance if tol else None,
            "exact_fallback": int(bool(tol and tol.exact_fallback)),
            "pairs_json": json.dumps(pairs, sort_keys=True, separators=(",", ":")),
            "settle_p50_ms": int(round(cal.settle_p50_ms)) if cal.settle_p50_ms is not None else None,
            "threshold_component_area": th.component_area_min, "threshold_changed_px": th.changed_px_min,
            "threshold_delta_e": float(th.delta_e_min), "threshold_shift_px": th.shift_px_min,
            "regions_file_sha256": mu.region_sha256,
            "mask_versions_json": json.dumps(dict(mu.mask_versions), sort_keys=True),
            "redaction_version": mu.redaction_version, "workspace_sha256": mu.workspace_sha256,
            "epoch_definition_sha256": mu.epoch_definition_sha256,
            "nonzero_on_frozen": int(cal.data_mode == "frozen" and fl.nonzero), "valid_from": _utc_text(valid_from)})
    return rows


def fix_rows(cal: CheckpointCalibration, *, run_id: str) -> list[dict]:
    """The `calibration_fixes` rows of one checkpoint (3.9 step 5, SD-20): one per item, written open. An item a floor
    exception covers carries the exception's ticket (`floor_exception_ref`) but stays open: the exception closes it
    only while it is in force in the config (`live_exceptions`, choice 8), so an expired or removed exception
    reopens it (4.8, L-RGN-05). A checkpoint with a failed capture writes only its failed-capture items: it has no
    valid floor for a region item to name."""
    out = []
    for f in cal.fix_list:
        if cal.failed and f.region_id is not None:
            continue
        row: dict[str, Any] = {
            "calibration_run_id": run_id, "checkpoint_id": f.checkpoint_id, "region_id": f.region_id,
            "cause_class": f.cause, "outcome": f.outcome, "reason": f.reason, "owner": f.owner or "unassigned",
            "ticket_ref": f.ticket, "opened_at": _utc_text(f.opened_at) if f.opened_at else None}
        if f.floor_exception is not None:
            row["floor_exception_ref"] = f.floor_exception.ticket
        out.append(row)
    return out


def write_calibration(store: Any, checkpoints: Iterable[CheckpointCalibration], *, calibration_key: str,
                      fingerprint_id: str | None, run_id: str, now: datetime,
                      live: Mapping[str, LiveVersions] | None = None) -> list[int]:
    """Write the floors and fix items of calibrated checkpoints (`Store.write_calibration`): each checkpoint's earlier
    open items on the key are closed by this run, and an item that is still there keeps its earliest opened date and
    a ticket, so its age counts from the day it was first opened (3.9 step 6, 10.5: the clock is on the region's
    floor). An item is the same item by checkpoint and region, or for a failed capture by checkpoint and outcome; the
    cause class read off the pairs can change between calibrations and is taken from the new one. Returns the floor
    ids."""
    cals = list(checkpoints)
    floors: list[dict] = []
    fixes: list[dict] = []
    mine = {c.checkpoint_id for c in cals}
    earlier = sorted((i for i in open_fixes(store, calibration_key) if i.checkpoint_id in mine),
                     key=lambda i: (i.opened_at or now, i.fix_id or 0))
    for cal in cals:
        for f in cal.fix_list:
            same = [i for i in earlier if (i.checkpoint_id, i.region_id) == (f.checkpoint_id, f.region_id)
                    and (f.region_id is not None or i.outcome == f.outcome)]
            if same:
                f.opened_at = same[0].opened_at
                f.ticket = f.ticket or next((i.ticket for i in same if i.ticket), None)
        floors += floor_rows(cal, calibration_key=calibration_key, fingerprint_id=fingerprint_id, run_id=run_id,
                             live=(live or {}).get(cal.checkpoint_id), valid_from=now)
        fixes += fix_rows(cal, run_id=run_id)
    ids = store.write_calibration(floors, fixes)
    for i in earlier:                     # after the new rows: an interruption leaves an item open twice, never lost
        store.update("calibration_fixes", {"fix_id": i.fix_id},
                     {"closed_at": _utc_text(now), "closed_by": f"calibration run {run_id}"})
    return ids


@dataclass(frozen=True)
class FloorEntry:
    """One current `calibration_floors` row as the Diff Engine and 7.13 read it."""

    checkpoint_id: str
    region_id: str
    calibration_key: str
    calibration_run_id: str
    floors: RegionFloors
    threshold: RegionThreshold
    settle_p50_ms: float | None
    tolerance: Tolerance | None
    measured: MeasuredUnder
    nonzero_on_frozen: bool = False
    floor_id: int | None = None


def _entry(r: Any) -> FloorEntry:
    pairs = json.loads(r["pairs_json"] or "[]")
    fl = RegionFloors.of(pairs)
    th = RegionThreshold(int(r["threshold_component_area"]), int(r["threshold_changed_px"]),
                         float(r["threshold_delta_e"]), int(r["threshold_shift_px"]), fl.changed_px,
                         fl.component_area, fl.delta_e, fl.shift_px)
    tol = None if r["hamming_tolerance"] is None else Tolerance(int(r["hamming_tolerance"]), bool(r["exact_fallback"]),
                                                                r["noise_spread"], r["fault_separation"])
    mu = MeasuredUnder(r["regions_file_sha256"], json.loads(r["mask_versions_json"] or "{}"), r["redaction_version"],
                       r["workspace_sha256"], r["epoch_definition_sha256"])
    return FloorEntry(r["checkpoint_id"], r["region_id"], r["rendering_key_sha256"], r["calibration_run_id"], fl, th,
                      None if r["settle_p50_ms"] is None else float(r["settle_p50_ms"]), tol, mu,
                      bool(r["nonzero_on_frozen"]), int(r["floor_id"]))


def lookup(store: Any, key: str, checkpoint: str) -> dict[str, FloorEntry]:
    """The current floors of one checkpoint on one calibration key (8.3 `ux_floor_current`), by region id; empty
    when it has none."""
    rows = store.query("SELECT * FROM calibration_floors WHERE rendering_key_sha256 = ? AND checkpoint_id = ? "
                       "AND superseded_at IS NULL", (key, checkpoint))
    return {r["region_id"]: _entry(r) for r in rows}


def is_valid(entry: FloorEntry, live: LiveVersions | None) -> bool:
    """7.13: the floor applies under the live config (`invalidation` names no row); no live config, valid."""
    return live is None or invalidation(entry.measured, live, entry.region_id) is None


def has_calibration(store: Any, key: str, match: Mapping[str, Any] | None = None) -> bool:
    """L-FPR-04 and 12.7 G5's `calibration_record`: floors exist for the key, or (with the fingerprint's `match`) for
    a key it may take over after a passing Nightly A/A (`carry_over_source`)."""
    if store.query("SELECT 1 FROM calibration_floors WHERE rendering_key_sha256 = ? AND superseded_at IS NULL "
                   "LIMIT 1", (key,)):
        return True
    return match is not None and carry_over_source(match, calibrated_matches(store)) is not None


def calibrated_keys(store: Any, match: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The keys with current floors, plus `match`'s own key when it may take over a calibrated key's floors (L-FPR-04):
    the `calibrated_keys` container `fingerprint.g5` reads."""
    keys = frozenset(r[0] for r in store.query("SELECT DISTINCT rendering_key_sha256 FROM calibration_floors "
                                               "WHERE superseded_at IS NULL"))
    if match is not None and carry_over_source(match, calibrated_matches(store)) is not None:
        keys |= {calibration_key(match)}
    return keys


def _run_keys(store: Any, run_id: str, cache: dict[str, frozenset[str]]) -> frozenset[str]:
    """The calibration keys of the fingerprints a Calibration run recorded (choice 8)."""
    if run_id not in cache:
        keys = set()
        for r in store.query("SELECT fields_json FROM env_fingerprints WHERE run_id = ?", (run_id,)):
            try:
                keys.add(calibration_key(json.loads(r["fields_json"])["match"]))
            except (KeyError, ValueError, TypeError):
                continue
        cache[run_id] = frozenset(keys)
    return cache[run_id]


def open_fixes(store: Any, key: str | None = None) -> list[FixItem]:
    """Open `calibration_fixes` rows (3.9 step 6), with their checkpoint's data mode; with `key`, only the items of
    that calibration key (choice 8)."""
    rows = store.query(
        "SELECT x.*, f.rendering_key_sha256 AS floor_key, c.data_mode FROM calibration_fixes x "
        "LEFT JOIN calibration_floors f ON f.floor_id = x.floor_id "
        "LEFT JOIN checkpoints c ON c.checkpoint_id = x.checkpoint_id "
        "WHERE x.closed_at IS NULL ORDER BY x.fix_id")
    cache: dict[str, frozenset[str]] = {}
    out = []
    for r in rows:
        if key is not None:
            if r["floor_key"] is not None and r["floor_key"] != key:
                continue
            if r["floor_key"] is None and (r["calibration_run_id"] is None or
                                           key not in _run_keys(store, r["calibration_run_id"], cache)):
                continue
        out.append(FixItem(r["checkpoint_id"], r["region_id"], r["cause_class"], outcome=r["outcome"],
                           reason=r["reason"], owner=r["owner"], ticket=r["ticket_ref"],
                           opened_at=datetime.fromisoformat(r["opened_at"]), data_mode=r["data_mode"],
                           fix_id=int(r["fix_id"])))
    return out


def live_exceptions(regions_docs: Mapping[str, Mapping[str, Any]], now: datetime) -> dict[str, dict[str, str]]:
    """The floor exceptions in force at `now` in 4.8 regions documents: checkpoint id -> region id -> ticket."""
    out: dict[str, dict[str, str]] = {}
    for cid, doc in regions_docs.items():
        for r in (doc or {}).get("regions") or ():
            fe = r.get("floor_exception") if isinstance(r, Mapping) else None
            if not isinstance(fe, Mapping) or not fe.get("ticket") or not fe.get("expires_at"):
                continue
            exp = fe["expires_at"]
            exp = exp if isinstance(exp, datetime) else datetime.fromisoformat(str(exp))
            exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
            if exp > now:
                out.setdefault(cid, {})[str(r["id"])] = str(fe["ticket"])
    return out


@dataclass(frozen=True)
class CalibrationRequest:
    """A Calibration run to queue (7.13, 4.8, L-FPR-04, 12.7 G5): the key, the checkpoints and regions answered
    `uncalibrated`, and why (`no_calibration`, `incomplete`, `missing`, a 7.13 row or `revoked`)."""

    calibration_key: str
    regions: tuple[tuple[str, str, str], ...]            # (checkpoint, region, reason)
    requested_by: str | None = None

    @property
    def checkpoints(self) -> tuple[str, ...]:
        return tuple(sorted({c for c, _, _ in self.regions}))

    def to_dict(self) -> dict:
        reasons: dict[str, int] = {}
        for _, _, why in self.regions:
            reasons[why] = reasons.get(why, 0) + 1
        return {"calibration_key": self.calibration_key, "requested_by": self.requested_by,
                "checkpoints": list(self.checkpoints), "regions": len(self.regions), "reasons": reasons}


class StoreCalibration:
    """The calibration of one key as the Diff Engine reads it from the store (4.8 Threshold source, 3.9 step 6,
    7.13): `lookup(checkpoint, region)` is `calibrated` only when the key is complete, the region has a current
    floor, the floor is valid under the live config and its floor exception was not revoked. Every other answer
    is `UNCALIBRATED` and is collected for `request()` (the Calibration run to queue).

    `live` maps a checkpoint id to its `LiveVersions` (`checkpoint_versions`); a checkpoint without one is not
    checked for 7.13 changes. `exceptions` (checkpoint -> region -> ticket, `live_exceptions`) are the floor
    exceptions in force across the config, which close open items for completeness; `revoked` the
    (checkpoint, region, ticket) revocations of `revoked_exceptions`."""

    def __init__(self, store: Any, key: str, *, live: Mapping[str, LiveVersions] | None = None,
                 exceptions: Mapping[str, Mapping[str, str]] | None = None,
                 revoked: Iterable[tuple[str, str, str]] = ()):
        self.store, self.key = store, key
        self.live = dict(live or {})
        self.exceptions = {c: dict(v) for c, v in (exceptions or {}).items()}
        self.revoked = frozenset(revoked)
        self._entries: dict[str, dict[str, FloorEntry]] = {}
        self.uncalibrated: dict[tuple[str, str], str] = {}
        self.calibrated = has_calibration(store, key)
        self.items = open_fixes(store, key)

    @property
    def complete(self) -> bool:
        """3.9 step 5: every open item of the key carries a live floor exception."""
        return self.calibrated and not self.fix_list()

    def fix_list(self) -> list[FixItem]:
        return [i for i in self.items
                if i.region_id is None or i.region_id not in self.exceptions.get(i.checkpoint_id, {})]

    def entries(self, checkpoint_id: str) -> dict[str, FloorEntry]:
        if checkpoint_id not in self._entries:
            self._entries[checkpoint_id] = lookup(self.store, self.key, checkpoint_id)
        return self._entries[checkpoint_id]

    def _why(self, checkpoint_id: str, region_id: str) -> tuple[FloorEntry | None, str | None]:
        if not self.calibrated:
            return None, "no_calibration"
        if not self.complete:
            return None, "incomplete"
        e = self.entries(checkpoint_id).get(region_id)
        if e is None:
            return None, "missing"
        live = self.live.get(checkpoint_id)
        if live is not None:
            row = invalidation(e.measured, live, region_id)
            if row is not None:
                return e, row
            ticket = live.floor_exceptions.get(region_id)
            if ticket is not None and (checkpoint_id, region_id, ticket) in self.revoked:
                return e, "revoked"
        return e, None

    def lookup(self, checkpoint_id: str, region_id: str) -> tuple[RegionThreshold, str]:
        """Threshold and source for one region (the `CalibrationFile.lookup` contract the Diff Engine calls)."""
        e, why = self._why(checkpoint_id, region_id)
        if why is not None or e is None:
            self.uncalibrated[(checkpoint_id, region_id)] = why or "missing"
            return UNCALIBRATED, "uncalibrated"
        return e.threshold, "calibrated"

    def settle_p50_ms(self, checkpoint_id: str) -> float | None:
        """The checkpoint's calibration p50 time to settle (3.5), the same on every region row."""
        return next((e.settle_p50_ms for e in self.entries(checkpoint_id).values() if e.settle_p50_ms is not None),
                    None)

    def tolerance(self, checkpoint_id: str, region_id: str) -> Tolerance | None:
        e = self.entries(checkpoint_id).get(region_id)
        return e.tolerance if e is not None else None

    def request(self, requested_by: str | None = None) -> CalibrationRequest | None:
        """The Calibration run to queue for every region answered `uncalibrated` so far, or None."""
        if not self.uncalibrated:
            return None
        return CalibrationRequest(self.key, tuple(sorted((c, r, w) for (c, r), w in self.uncalibrated.items())),
                                  requested_by)


def export_file(store: Any, key: str, *, exported_at: datetime,
                exceptions: Mapping[str, Mapping[str, str]] | None = None) -> CalibrationFile | None:
    """The `calibration/<hex>.yaml` export of a key from `calibration_floors` (4.16: "written by the Calibration
    Service from calibration_floors"), or None when the key has no floors. Open fix items (from the store) keep
    their regions' non-zero floors open; a failed-capture item empties its checkpoint (choice 9)."""
    rows = store.query("SELECT * FROM calibration_floors WHERE rendering_key_sha256 = ? AND superseded_at IS NULL "
                       "ORDER BY checkpoint_id, region_id", (key,))
    if not rows:
        return None
    exc = exceptions or {}
    items = open_fixes(store, key)
    entries = [_entry(r) for r in rows]
    latest = max(entries, key=lambda e: e.floor_id or 0)                # the last floor written
    out = CalibrationFile(key, latest.calibration_run_id, exported_at=exported_at,
                          fingerprint_id=next((r["fingerprint_sha256"] for r in rows if r["fingerprint_sha256"]), None))
    for e in entries:
        cal = out.checkpoints.setdefault(e.checkpoint_id, CheckpointCalibration(e.checkpoint_id, None,
                                                                              settle_p50_ms=e.settle_p50_ms))
        cal.floors[e.region_id], cal.thresholds[e.region_id] = e.floors, e.threshold
        if e.tolerance is not None:
            cal.tolerances[e.region_id] = e.tolerance
        if e.floors.nonzero:
            open_ = any(i.checkpoint_id == e.checkpoint_id and i.region_id == e.region_id for i in items)
            closed_by = None if open_ and e.region_id not in exc.get(e.checkpoint_id, {}) else "floor_exception"
            cal.fix_list.append(FixItem(e.checkpoint_id, e.region_id, "unknown", floors=e.floors,
                                        exception_ref=closed_by))
    for i in items:
        if i.region_id is None and i.checkpoint_id in out.checkpoints:
            out.checkpoints[i.checkpoint_id].fix_list.append(i)
    return out


# ------------------------------------------------------------------------------------------------
# Carry-over to a new JRE or render probe (7.13, 4.12, SD-69)

def calibrated_matches(store: Any) -> dict[str, Mapping[str, Any]]:
    """Calibration key -> the fingerprint `match` its current floors were measured on (8.3 env_fingerprints)."""
    out: dict[str, Mapping[str, Any]] = {}
    rows = store.query("SELECT DISTINCT f.rendering_key_sha256 AS k, e.fields_json FROM calibration_floors f "
                       "JOIN env_fingerprints e ON e.fingerprint_sha256 = f.fingerprint_sha256 "
                       "WHERE f.superseded_at IS NULL")
    for r in rows:
        out.setdefault(r["k"], json.loads(r["fields_json"])["match"])
    return out


def carry_over_source(match: Mapping[str, Any], calibrated: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The calibrated key whose match differs from `match` only in `jre_release` or `render_probe_hash` among the
    rendering-key fields (7.13), or None. The harness hashes must be equal too."""
    want = calibration_key(match)
    for key, other in sorted(calibrated.items()):
        if key == want:
            continue
        same = [f for f in CALIBRATION_KEY_FIELDS if f not in CARRY_OVER_FIELDS]
        if all(match.get(f) == other.get(f) for f in same) and calibration_key(
                {**match, **{f: other.get(f) for f in CARRY_OVER_FIELDS}}) == key:
            return key
    return None


def carry_over(store: Any, from_key: str, to_key: str, *, aa_passed: bool, now: datetime,
               fingerprint_id: str | None = None) -> int:
    """Copy the current floors of `from_key` to `to_key` once the Nightly A/A on the new key passed (7.13); returns
    how many floors were carried (0 when the A/A did not pass: the new key stays at threshold zero, and a failing
    A/A is `aa_drift_global` with a full calibration, 10.6). The carried rows keep their measurements and their
    Calibration run, and the open fix items of the carried floors come with them (same cause, owner, opened date
    and ticket, linked to the new key's floor), so the new key is complete exactly when the old one is (3.9 steps 5
    and 6) and an item's age is unchanged. A failed-capture item has no floor to carry; a key holding one is never
    complete, so its floors never carry over."""
    if not aa_passed:
        return 0
    rows = store.query("SELECT * FROM calibration_floors WHERE rendering_key_sha256 = ? AND superseded_at IS NULL",
                       (from_key,))
    skip = ("floor_id", "superseded_at", "exported_commit")
    carried = [{**{k: r[k] for k in r.keys() if k not in skip}, "rendering_key_sha256": to_key,
                "fingerprint_sha256": fingerprint_id or r["fingerprint_sha256"], "valid_from": _utc_text(now)}
               for r in rows]
    items = store.query("SELECT x.* FROM calibration_fixes x JOIN calibration_floors f ON f.floor_id = x.floor_id "
                        "WHERE f.rendering_key_sha256 = ? AND f.superseded_at IS NULL AND x.closed_at IS NULL "
                        "ORDER BY x.fix_id", (from_key,))
    fixes = [{k: r[k] for k in r.keys() if k not in ("fix_id", "floor_id")} for r in items]   # relinked by region
    store.write_calibration(carried, fixes)
    return len(carried)


def retune_tolerances(store: Any, key: str, mutants: Mapping[tuple[str, str], Sequence[Any]],
                      tiers: Mapping[str, int]) -> list[str]:
    """The Nightly re-tune of 9.7: for each region with canary mutants, the tolerance from its stored noise spread and
    the mutants' fault separation, written to its current floor row (which the regions mirror reads); returns the
    report lines of the regions whose tolerance changed (`tolerance_changes`)."""
    before: dict[tuple[str, str], Tolerance | None] = {}
    after: dict[tuple[str, str], Tolerance] = {}
    for (cid, rid), crops in sorted(mutants.items()):
        e = lookup(store, key, cid).get(rid)
        if e is None:
            continue
        spread = e.tolerance.noise_spread if e.tolerance is not None else None
        new = region_tolerance(mutant_crops=crops, tier=tiers.get(cid), spread=spread)
        before[(cid, rid)], after[(cid, rid)] = e.tolerance, new
        if e.tolerance != new:
            store.update("calibration_floors", {"floor_id": e.floor_id},
                         {"fault_separation": new.fault_separation, "hamming_tolerance": new.hamming_tolerance,
                          "exact_fallback": int(new.exact_fallback)})
    return tolerance_changes(before, after)


# ------------------------------------------------------------------------------------------------
# settle_regression (3.5) and floor-exception revocation (3.9)

def _finding_id(*parts: Any) -> str:
    return "fnd-" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def settle_regression_finding(run_id: str, checkpoint_id: str, times_ms: Sequence[int | float],
                              calibration_p50_ms: float | None, *, owner: str | None, tier: int | None = None,
                              params: Mapping[str, Any] | None = None) -> Finding | None:
    """3.5: the run's p50 time to settle of a checkpoint above twice its calibration p50 is a `settle_regression`
    finding in run health for the flow owner (`diff.settle.settle_regression` compares); None otherwise."""
    if not times_ms or calibration_p50_ms is None:
        return None
    p50 = float(statistics.median(times_ms))
    if not settle_regression(p50, calibration_p50_ms, params):
        return None
    return Finding(finding_id=_finding_id(run_id, checkpoint_id, "settle_regression"), run_id=run_id,
                   kind="settle_regression", first_seen_run_id=run_id, checkpoint_id=checkpoint_id, tier=tier,
                   description=f"time to settle p50 {p50:.0f} ms is above twice the calibration p50 "
                               f"{calibration_p50_ms:.0f} ms (3.5)",
                   evidence={"p50_ms": p50, "calibration_p50_ms": float(calibration_p50_ms),
                             "captures": len(times_ms), "owner": owner})


@dataclass(frozen=True)
class CanaryMiss:
    """A canary mutant the path missed (10.3 states `missed`, `detector_blind`, `detector_degraded`): its checkpoint,
    the region the catalog binds it to and its footprint in frame coordinates."""

    checkpoint_id: str
    mutant_id: str
    region_ref: str | None = None
    footprint: tuple[int, int, int, int] | None = None


MISSED_STATES = ("missed", "detector_blind", "detector_degraded")


def canary_misses(results: Iterable[Any], mutants: Iterable[Any]) -> list[CanaryMiss]:
    """The misses of one night from `canary.score`'s rows (`CanaryResult`) and the generated `Mutant`s."""
    by_key = {(m.mutant_id, m.checkpoint_id): m for m in mutants}
    out = []
    for r in results:
        if r.state not in MISSED_STATES:
            continue
        m = by_key.get((r.mutant_id, r.checkpoint_id))
        out.append(CanaryMiss(r.checkpoint_id, r.mutant_id, m.entry.region_ref if m is not None else None,
                              tuple(m.footprint) if m is not None and m.footprint is not None else None))
    return out


@dataclass(frozen=True)
class Revocation:
    """A floor exception revoked by a canary miss on its region (3.9): the region becomes `rule` or `mask` by a human
    PR and is `uncalibrated` until then."""

    checkpoint_id: str
    region_id: str
    ticket: str
    approved_by: str
    mutant_ids: tuple[str, ...]

    def finding(self, run_id: str) -> Finding:
        """The `canary_miss` finding that carries the revocation and the PR proposal."""
        return Finding(
            finding_id=_finding_id(run_id, self.checkpoint_id, self.region_id, "floor_exception_revoked"),
            run_id=run_id, kind="canary_miss", first_seen_run_id=run_id, checkpoint_id=self.checkpoint_id,
            region_id=self.region_id,
            description=f"canary {', '.join(self.mutant_ids)} missed on {self.region_id}: its floor_exception "
                        f"{self.ticket} is revoked; re-declare the region as rule or mask (3.9)",
            evidence={"revokes_floor_exception": True, "ticket": self.ticket, "approved_by": self.approved_by,
                      "mutants": list(self.mutant_ids),
                      "proposal": f"remove floor_exception {self.ticket} from {self.region_id} in the regions file "
                                  f"of {self.checkpoint_id} and declare it as a rule or mask region"})


def revoke_floor_exception(canary_miss: Iterable[CanaryMiss], regions: Mapping[str, RegionsFile],
                           located: Mapping[str, Mapping[str, Rect]] | None = None) -> list[Revocation]:
    """3.9: "the canary catalog must still detect every mutant bound to it, else the exception is revoked". A miss
    lands on a floor-excepted region when its `region_ref` names it or its footprint meets the region's located
    rect (choice 12)."""
    hits: dict[tuple[str, str], list[str]] = {}
    for miss in canary_miss:
        reg = regions.get(miss.checkpoint_id)
        if reg is None:
            continue
        rects = (located or {}).get(miss.checkpoint_id, {})
        for r in reg.regions:
            if r.floor_exception is None:
                continue
            on = miss.region_ref == r.id or (miss.footprint is not None and r.id in rects and
                                             rects[r.id].intersect(Rect(*miss.footprint)) is not None)
            if on:
                hits.setdefault((miss.checkpoint_id, r.id), []).append(miss.mutant_id)
    out = []
    for (cid, rid), mids in sorted(hits.items()):
        exc = regions[cid].region(rid).floor_exception
        assert exc is not None
        out.append(Revocation(cid, rid, exc.ticket, exc.approved_by, tuple(sorted(set(mids)))))
    return out


def revoked_exceptions(store: Any) -> frozenset[tuple[str, str, str]]:
    """Every revocation recorded in the store, as (checkpoint, region, ticket) for `StoreCalibration(revoked=)`."""
    out = set()
    for r in store.query("SELECT checkpoint_id, region_id, evidence_json FROM findings WHERE kind = 'canary_miss' "
                         "AND checkpoint_id IS NOT NULL AND region_id IS NOT NULL"):
        ev = json.loads(r["evidence_json"] or "{}")
        if ev.get("revokes_floor_exception") and ev.get("ticket"):
            out.add((r["checkpoint_id"], r["region_id"], str(ev["ticket"])))
    return frozenset(out)


# ------------------------------------------------------------------------------------------------
# The Nightly A/A (3.9 step 7, 10.6)

def aa_false_positive_rate(results: Iterable[DiffResult]) -> float:
    """3.9 step 7: region pairs over floor divided by region pairs compared."""
    over = compared = 0
    for res in results:
        for m in res.regions:
            compared += 1
            over += int(m.above_floor)
    return over / compared if compared else 0.0


@dataclass(frozen=True)
class AADrift:
    """The Nightly A/A verdict (3.9 step 7, 10.6)."""

    drifted: dict[str, Terminal]
    global_drift: Terminal | None
    above_floor_fraction: float


def aa_drift(results: Mapping[str, DiffResult], frozen: Iterable[str],
             params: Mapping[str, Any] | None = None) -> AADrift:
    """Nightly A/A on one build. A frozen checkpoint with any calibrated diff above its floor is
    `not_settled (reason: aa_drift)` (10.6): a calibrated region above its floor or its threshold,
    or an accepted whole-window shift (regions are measured relative to it, so they alone miss a
    shifted window; a shift is a change, never noise, 3.7). More than 2 percent of checkpoints above
    floor is `harness_error (reason: aa_drift_global)`. Uncalibrated results are left to
    `needs_human (reason: uncalibrated)` (10.6) and never drift. Synthetic results (canary
    mutants, `DiffContext(synthetic=True)`) are left out entirely, the denominator included."""
    p = params or load_params()
    frozen_set = set(frozen)
    drifted: dict[str, Terminal] = {}
    above = 0
    real = {cid: res for cid, res in results.items()
            if not (res.synthetic or (res.signal is not None and res.signal.synthetic))}
    for cid, res in real.items():
        if res.terminal is not None:
            continue
        if res.signal is not None and res.signal.above_floor and res.threshold_source == "calibrated":
            above += 1
        if cid not in frozen_set:
            continue
        hits = [m.region_id for m in res.regions if m.threshold_source == "calibrated"
                and (m.above_floor or m.above_threshold)]
        ws = res.window_shift
        if ws is not None and ws.accepted and res.threshold_source == "calibrated":
            hits.append(f"window shift ({ws.dx}, {ws.dy})")
        if hits:
            drifted[cid] = Terminal("not_settled", "aa_drift", detail="regions: " + ", ".join(hits))
    frac = above / len(real) if real else 0.0
    glob = None
    if frac > float(p["calibration"]["aa_drift_global_fraction"]):
        glob = Terminal("harness_error", "aa_drift_global", detail=f"{above} of {len(real)} checkpoints above floor")
    return AADrift(drifted, glob, frac)
