"""A/A calibration and threshold derivation (3.9, 4.8 "Threshold source", 10.6).

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
zero changed-pixel floor; a non-zero floor is a fix-list item with a cause class, closed only by a
fix or a human-approved `floor_exception`. Until every item is closed the fingerprint has no
thresholds: every region is `uncalibrated` (threshold 0). A region missing from a complete file is
`uncalibrated` on its own.

Choices where the spec is ambiguous (reported to the coordinator):

- The SSIM "floor" is the minimum over the pairs (the worst similarity), stored for ranking only.
- A non-zero translation floor on a pixel-diffed region, or on the whole window (`*`), is also a
  fix-list item (cause `landmark_jitter`): 3.2 expects a zero floor on every `diff` and `freeze`
  region. A non-zero CIEDE2000 floor alone is not an item; it only raises the color threshold.
- The cause class is the caller's hint when given; otherwise `landmark_jitter` when the region's
  translation floor is non-zero, else `unknown` (the spec has a human or a heuristic read it off
  the pair). Failed captures map `landmark_missing` to `landmark_jitter` and every other failure
  (`not_settled`, `env_error`, `harness_error`) to `settle`.
- 3.9 lists the cause classes `caret`, `hover`, ...; 8.3's `calibration_fixes.cause_class` CHECK
  lists a different set (`timing`, `state`, ...). This module uses 3.9's list.
- Every non-zero-floor item carries a proposed region (the dilated union of the changing pixels,
  snapped to the region's landmark), not only `live_data` and `flash` items; nothing is applied.
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import cv2
import numpy as np
import yaml

from . import load_params
from .engine import DiffContext, DiffResult, Terminal, diff_pair
from .regions import (
    REST_ID, UNCALIBRATED, WHOLE_ID, FloorException, Rect, RedactionBox, RegionsFile, RegionThreshold,
)

CAUSE_CLASSES = ("caret", "hover", "tooltip", "flash", "scroll", "sort", "live_data", "settle",
                 "landmark_jitter", "unknown")
FAILED_CAPTURE_OUTCOMES = ("not_settled", "landmark_missing", "env_error", "harness_error")


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
            self.largest_bbox = row["largest_bbox"]
        self.delta_e = max(self.delta_e, float(row["delta_e"]))
        self.shift_px = max(self.shift_px, int(row["shift_px"]))
        self.ssim_min = min(self.ssim_min, float(row["ssim"]))
        self.ssim_block_min = min(self.ssim_block_min, float(row["ssim_min_block"]))


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


@dataclass
class FixItem:
    """One fix-list item of 3.9 step 5 (`calibration_fixes`, 8.3)."""

    checkpoint_id: str
    region_id: str | None
    cause: str
    floors: RegionFloors | None = None
    outcome: str | None = None
    reason: str | None = None
    proposed_landmark: str | None = None
    proposed_rect: Rect | None = None
    floor_exception: FloorException | None = None

    def __post_init__(self) -> None:
        if self.cause not in CAUSE_CLASSES:
            raise ValueError(f"cause class {self.cause!r} is not one of {CAUSE_CLASSES}")

    @property
    def closed(self) -> bool:
        """Closed by a human-approved floor_exception (a fixed item leaves the list on re-calibration)."""
        return self.floor_exception is not None


@dataclass
class CheckpointCalibration:
    """The calibration of one checkpoint on one fingerprint."""

    checkpoint_id: str
    data_mode: str
    pair_stats: dict[str, list[dict]] = field(default_factory=dict)
    floors: dict[str, RegionFloors] = field(default_factory=dict)
    thresholds: dict[str, RegionThreshold] = field(default_factory=dict)
    fix_list: list[FixItem] = field(default_factory=list)
    settle_p50_ms: float | None = None

    @property
    def accepted(self) -> bool:
        return all(item.closed for item in self.fix_list)

    def floors_sorted(self) -> list[tuple[str, RegionFloors]]:
        """Floors published sorted by changed-pixel floor descending (3.9 step 4)."""
        return sorted(self.floors.items(), key=lambda kv: (-kv[1].changed_px, -kv[1].component_area, kv[0]))


def _row(metrics: Any) -> dict:
    return {"changed_px": metrics.changed_px, "pixel_fraction": metrics.pixel_fraction,
            "components": metrics.components, "largest_component": metrics.largest_component,
            "largest_bbox": metrics.largest_bbox, "ssim": metrics.ssim, "ssim_min_block": metrics.ssim_min_block,
            "delta_e": metrics.delta_e, "shift": metrics.shift, "shift_px": metrics.shift_px}


def calibrate_checkpoint(captures: Sequence[Capture], registry: RegionsFile, *, data_mode: str,
                         redactions: Sequence[RedactionBox] = (), now: datetime | None = None,
                         cause_hints: Mapping[str, str] | None = None,
                         params: Mapping[str, Any] | None = None) -> CheckpointCalibration:
    """Run 3.9 steps 3 to 5 for one checkpoint over its six captures (r1..r5, r6).

    `registry` is the effective Region Registry entry; a region's live `floor_exception` (not
    expired at `now`) closes its non-zero-floor item. Region floors and thresholds are kept for
    every pixel-diffed region plus `_rest` and the whole window (`*`).
    """
    p = params or load_params()
    cp = p["calibration"]
    n_s, n_n = int(cp["session_captures"]), int(cp["next_session_captures"])
    if len(captures) != n_s + n_n:
        raise ValueError(f"calibration needs {n_s + n_n} captures, got {len(captures)}")
    cid = registry.checkpoint_id
    cal = CheckpointCalibration(cid, data_mode)
    hints = cause_hints or {}
    for c in captures:
        if c.outcome is not None:
            cause = "landmark_jitter" if c.outcome == "landmark_missing" else "settle"
            cal.fix_list.append(FixItem(cid, None, cause, outcome=c.outcome, reason=c.reason))
    times = [c.time_to_settle_ms for c in captures if c.time_to_settle_ms is not None]
    cal.settle_p50_ms = float(statistics.median(times)) if times else None

    region_ids = [r.id for r in registry.regions if r.pixel_diffed] + [REST_ID]
    floors = {rid: RegionFloors() for rid in region_ids + [WHOLE_ID]}
    unions: dict[str, np.ndarray] = {}
    anchor_pos: dict[str, tuple[int, int]] = {}
    for i, j in aa_pairs(n_s, n_n):
        a, b = captures[i], captures[j]
        if a.image is None or b.image is None:
            continue
        res = diff_pair(a.image, b.image, registry, redactions=redactions,
                        context=DiffContext(run_id="calibration", checkpoint_id=cid), params=p)
        if res.terminal is not None:
            cause = "landmark_jitter" if res.terminal.outcome == "landmark_missing" else "settle"
            cal.fix_list.append(FixItem(cid, None, cause, outcome=res.terminal.outcome, reason=res.terminal.reason))
            continue
        if res.window_shift is not None and res.window_shift.accepted:
            floors[WHOLE_ID].shift_px = max(floors[WHOLE_ID].shift_px,
                                            max(abs(res.window_shift.dx), abs(res.window_shift.dy)))
        for lm_id, (mb, _) in res.landmarks.items():
            anchor_pos.setdefault(lm_id, mb.pos)
        for m in res.regions:
            row = _row(m)
            row["pair"] = (i, j)
            cal.pair_stats.setdefault(m.region_id, []).append(row)
            floors[m.region_id].add(row)
        if res.changed_map is not None and res.changed_map.any():
            cmap = res.changed_map
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
    for rid, fl in floors.items():
        cal.floors[rid] = fl
        cal.thresholds[rid] = derive_threshold(fl, p)
        if fl.changed_px == 0 and fl.shift_px == 0:
            continue
        region = by_id.get(rid)
        cause = hints.get(rid) or ("landmark_jitter" if fl.shift_px > 0 else "unknown")
        exc = region.floor_exception if region is not None else None
        if exc is not None and now is not None and exc.expired(now):
            exc = None
        landmark, rect = _proposal(unions.get(rid), region.landmark if region else None, anchor_pos, p)
        cal.fix_list.append(FixItem(cid, rid, cause, floors=fl, proposed_landmark=landmark,
                                    proposed_rect=rect, floor_exception=exc))
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


@dataclass
class CalibrationFile:
    """`calibration/<fingerprint_id>.yaml` (4.8): per checkpoint and region, the four derived
    thresholds plus the measured floors, for one fingerprint."""

    fingerprint_id: str
    calibration_run_id: str
    checkpoints: dict[str, CheckpointCalibration] = field(default_factory=dict)

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
        cps: dict[str, Any] = {}
        for cid, c in sorted(self.checkpoints.items()):
            regions = {}
            for rid, th in c.thresholds.items():
                fl = c.floors[rid]
                regions[rid] = {
                    "thresholds": {"component_area_min": th.component_area_min, "changed_px_min": th.changed_px_min,
                                   "delta_e_min": float(th.delta_e_min), "shift_px_min": th.shift_px_min},
                    "floors": {"changed_px": fl.changed_px, "pixel_fraction": float(fl.pixel_fraction),
                               "components": fl.components, "component_area": fl.component_area,
                               "largest_bbox": list(fl.largest_bbox) if fl.largest_bbox else None,
                               "delta_e": float(fl.delta_e), "shift_px": fl.shift_px,
                               "ssim_min": float(fl.ssim_min), "ssim_block_min": float(fl.ssim_block_min)},
                }
            fixes = [{"region_id": f.region_id, "cause": f.cause, "outcome": f.outcome, "reason": f.reason,
                      "floor_exception": _exception_dict(f.floor_exception)} for f in c.fix_list]
            cps[cid] = {"data_mode": c.data_mode, "settle_p50_ms": c.settle_p50_ms, "regions": regions,
                        "fix_list": fixes}
        return {"schema_version": 1, "fingerprint_id": self.fingerprint_id,
                "calibration_run_id": self.calibration_run_id, "complete": self.complete, "checkpoints": cps}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "CalibrationFile":
        out = CalibrationFile(str(d["fingerprint_id"]), str(d["calibration_run_id"]))
        for cid, c in d.get("checkpoints", {}).items():
            cal = CheckpointCalibration(cid, str(c["data_mode"]), settle_p50_ms=c.get("settle_p50_ms"))
            for rid, rec in c.get("regions", {}).items():
                f = rec["floors"]
                fl = RegionFloors(changed_px=int(f["changed_px"]), pixel_fraction=float(f["pixel_fraction"]),
                                  components=int(f["components"]), component_area=int(f["component_area"]),
                                  largest_bbox=tuple(f["largest_bbox"]) if f.get("largest_bbox") else None,
                                  delta_e=float(f["delta_e"]), shift_px=int(f["shift_px"]),
                                  ssim_min=float(f["ssim_min"]), ssim_block_min=float(f["ssim_block_min"]))
                t = rec["thresholds"]
                cal.floors[rid] = fl
                cal.thresholds[rid] = RegionThreshold(
                    int(t["component_area_min"]), int(t["changed_px_min"]), float(t["delta_e_min"]),
                    int(t["shift_px_min"]), fl.changed_px, fl.component_area, fl.delta_e, fl.shift_px)
            for fx in c.get("fix_list", []):
                exc = fx.get("floor_exception")
                cal.fix_list.append(FixItem(cid, fx.get("region_id"), str(fx["cause"]), outcome=fx.get("outcome"),
                                            reason=fx.get("reason"),
                                            floor_exception=FloorException.from_dict(exc) if exc else None))
            out.checkpoints[cid] = cal
        return out

    def save(self, directory: str | os.PathLike) -> Path:
        path = Path(directory) / f"{self.fingerprint_id.replace(':', '_')}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        return path

    @staticmethod
    def load(directory: str | os.PathLike, fingerprint_id: str) -> "CalibrationFile | None":
        """The file for a fingerprint, or None when none exists (every region `uncalibrated`, L-FPR-04)."""
        path = Path(directory) / f"{fingerprint_id.replace(':', '_')}.yaml"
        if not path.exists():
            return None
        return CalibrationFile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _exception_dict(exc: FloorException | None) -> dict | None:
    if exc is None:
        return None
    return {"approved_by": exc.approved_by, "reason": exc.reason, "ticket": exc.ticket,
            "expires_at": exc.expires_at.isoformat(), "edge_tolerance_px": exc.edge_tolerance_px}


def aa_false_positive_rate(results: Iterable[DiffResult]) -> float:
    """3.9 step 7: region pairs over threshold divided by region pairs compared."""
    over = compared = 0
    for res in results:
        for m in res.regions:
            compared += 1
            over += int(m.above_threshold)
    return over / compared if compared else 0.0


@dataclass(frozen=True)
class AADrift:
    """The Nightly A/A verdict (3.9 step 7, 10.6)."""

    drifted: dict[str, Terminal]
    global_drift: Terminal | None
    above_floor_fraction: float


def aa_drift(results: Mapping[str, DiffResult], frozen: Iterable[str],
             params: Mapping[str, Any] | None = None) -> AADrift:
    """Nightly A/A on one build. A frozen checkpoint whose calibrated zero-floor region shows any
    change is `not_settled (reason: aa_drift)`; more than 2 percent of checkpoints above floor is
    `harness_error (reason: aa_drift_global)`. Uncalibrated results are left to
    `needs_human (reason: uncalibrated)` (10.6) and never drift."""
    p = params or load_params()
    frozen_set = set(frozen)
    drifted: dict[str, Terminal] = {}
    above = 0
    for cid, res in results.items():
        if res.terminal is not None:
            continue
        if res.signal is not None and res.signal.above_floor and res.threshold_source == "calibrated":
            above += 1
        if cid not in frozen_set:
            continue
        hits = [m.region_id for m in res.regions if m.threshold_source == "calibrated"
                and m.threshold.floor_changed_px == 0 and m.threshold.floor_shift_px == 0
                and (m.changed_px > 0 or m.shift_px > 0)]
        if hits:
            drifted[cid] = Terminal("not_settled", "aa_drift", detail="regions: " + ", ".join(hits))
    frac = above / len(results) if results else 0.0
    glob = None
    if frac > float(p["calibration"]["aa_drift_global_fraction"]):
        glob = Terminal("harness_error", "aa_drift_global", detail=f"{above} of {len(results)} checkpoints above floor")
    return AADrift(drifted, glob, frac)
