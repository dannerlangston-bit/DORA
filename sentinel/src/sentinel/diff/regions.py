"""Region Registry records and semantics (3.8, 4.8), the mask budget (3.6, 3.8, F33, F34, F41).

A regions file declares, per checkpoint, landmarks (one per compared panel) and regions cut as
offsets from a landmark. Every pixel of the target window belongs to exactly one comparison kind:

- `diff` and `freeze`: pixel-diffed (3.7);
- `rule`: never pixel-diffed, asserted by the Oracle;
- `mask`: never diffed, human-created, expiring; a mask may lie inside any region and then
  excludes those pixels;
- pixels no declared region covers belong to the implicit `diff` region `_rest` (`REST_ID`).

Money cells (Qty, Price, Buying Power Effect, Max Loss, Send, Cancel, Delete; a closed list, F58,
SD-28) name their role with `money_role` (`taxonomy.yaml` `money_roles`). On a flow checkpoint a money
cell is always `diff` or `freeze` with a zero floor; on a census, sweep or split checkpoint it may also
be a `rule` region, which the Oracle asserts (SD-29, the census half of L-RGN-04); it is never a mask
and never carries a `floor_exception` (L-RGN-04, F41). Identity fields (account number, account holder
name) are redaction boxes, never regions, excluded from the mask budget (F33).

Choices where the spec is silent (reported to the coordinator):

- Panel bounds for the mask budget (3.8 "the panel's located bounds"): when the caller supplies no
  bounds for a panel class, the bounds are the bounding box of every located region of that class.
- The money-cell rule counts every redaction box, identity or not ("any mask or redaction box onto
  a money_cell", 3.8); only the share computation excludes identity boxes.
- A mask region whose mask record is missing is a configuration error (`RegionError`); an expired
  one is dropped with an `ExpiryNotice` and its pixels diff as their enclosing region (3.8).
- An expired `floor_exception` is dropped at run start with an `ExpiryNotice` (L-RGN-05).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

KINDS = ("freeze", "rule", "mask", "diff")
PIXEL_DIFFED = ("diff", "freeze")
PANELS = ("order_confirmation", "order_ticket", "position_statement", "account_info", "other")
MONEY_ROLES = ("qty", "price", "bp_effect", "max_loss", "send", "cancel", "delete")    # 4.8 money_role, SD-28
REST_ID = "_rest"
WHOLE_ID = "*"          # the whole window, as `calibration_floors.region_id` names it (8.3)
SIDES = ("base", "candidate")


class RegionError(ValueError):
    """A regions file breaks a Region Registry rule (3.8, 4.8)."""


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle: top-left (x, y), width w, height h, in pixels."""

    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def offset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def intersect(self, other: "Rect") -> "Rect | None":
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.x + self.w, other.x + other.w), min(self.y + self.h, other.y + other.h)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    @staticmethod
    def bounding(rects: Sequence["Rect"]) -> "Rect":
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.w for r in rects)
        y1 = max(r.y + r.h for r in rects)
        return Rect(x0, y0, x1 - x0, y1 - y0)


def park_rect(coord: tuple[int, int], size: int = 16) -> Rect:
    """The system mask around the mouse parking coordinate (3.5 condition 1): `size` x `size` px,
    centered on the coordinate, applied on both sides."""
    x, y = coord
    return Rect(x - size // 2, y - size // 2, size, size)


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Landmark:
    """A landmark template (cut 1:1 from the approved base) and where to look for it (3.7, 4.8).

    `confidence_min` overrides the configured 0.92 for this landmark. `expected` is the template's top-left in the approved base; the search box is that position
    plus or minus the configured margin (200 px). `search_rect` overrides it; with neither, the
    whole image is searched (choice).
    """

    id: str
    template: np.ndarray
    confidence_min: float | None = None
    search_rect: Rect | None = None
    expected: tuple[int, int] | None = None

    def search_box(self, width: int, height: int, margin: int) -> Rect:
        th, tw = self.template.shape[:2]
        if self.search_rect is not None:
            box = self.search_rect
        elif self.expected is not None:
            ex, ey = self.expected
            box = Rect(ex - margin, ey - margin, tw + 2 * margin, th + 2 * margin)
        else:
            box = Rect(0, 0, width, height)
        return box.intersect(Rect(0, 0, width, height)) or Rect(0, 0, 0, 0)


@dataclass(frozen=True)
class FloorException:
    """A human-approved non-zero floor on a `diff` region (3.9 step 5, 4.8)."""

    approved_by: str
    reason: str
    ticket: str
    expires_at: datetime
    edge_tolerance_px: int = 1

    def expired(self, now: datetime) -> bool:
        return _ts(now) >= self.expires_at

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "FloorException":
        for k in ("approved_by", "reason", "ticket", "expires_at"):
            if not d.get(k):
                raise RegionError(f"floor_exception lacks {k} (L-RGN-05)")
        return FloorException(str(d["approved_by"]), str(d["reason"]), str(d["ticket"]),
                              _ts(d["expires_at"]), int(d.get("edge_tolerance_px", 1)))


@dataclass(frozen=True)
class Region:
    """One declared region: kind, landmark, offset rect (dx, dy, w, h) and budget class (4.8)."""

    id: str
    type: str
    landmark: str
    rect: Rect
    panel: str = "other"
    money_cell: bool = False
    fixture_dependency: str | None = None
    floor_exception: FloorException | None = None
    rule: Mapping[str, Any] | None = None
    mask_ref: str | None = None
    money_role: str | None = None        # SD-28: qty, price, bp_effect, max_loss, send, cancel, delete

    @property
    def pixel_diffed(self) -> bool:
        return self.type in PIXEL_DIFFED

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "Region":
        r = d["rect"]
        fe = d.get("floor_exception")
        role = d.get("money_role")
        return Region(
            id=str(d["id"]), type=str(d["type"]), landmark=str(d["landmark"]),
            rect=Rect(int(r["dx"]), int(r["dy"]), int(r["w"]), int(r["h"])),
            panel=str(d.get("panel", "other")), money_cell=bool(d.get("money_cell", False)),
            fixture_dependency=d.get("fixture_dependency"),
            floor_exception=FloorException.from_dict(fe) if fe else None,
            rule=d.get("rule"), mask_ref=d.get("mask_ref"), money_role=str(role) if role is not None else None,
        )


@dataclass(frozen=True)
class MaskRecord:
    """A mask record (4.8 mask schema). Only a named human creates one; it expires (3.8)."""

    mask_id: str
    checkpoint_id: str
    region_id: str
    owner: str
    reason: str
    created_by: str
    created_run: str
    created_at: datetime
    expires_at: datetime
    decision_ref: str
    ticket: str | None = None

    def expired(self, now: datetime) -> bool:
        return _ts(now) >= self.expires_at

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "MaskRecord":
        if d.get("created_by") != "human":
            raise RegionError(f"mask {d.get('mask_id')}: created_by must be human (L-MSK-01)")
        return MaskRecord(
            mask_id=str(d["mask_id"]), checkpoint_id=str(d["checkpoint_id"]),
            region_id=str(d["region_id"]), owner=str(d["owner"]), reason=str(d["reason"]),
            created_by="human", created_run=str(d["created_run"]),
            created_at=_ts(d["created_at"]), expires_at=_ts(d["expires_at"]),
            decision_ref=str(d["decision_ref"]), ticket=d.get("ticket"),
        )


@dataclass(frozen=True)
class RedactionBox:
    """A redaction box applied by Capture (3.6), in absolute window coordinates, same on both sides
    (the union of both captures' boxes). `identity_field` boxes (account number, account holder
    name) are excluded from the mask budget (F33)."""

    rect: Rect
    identity_field: bool = False
    anchor_id: str | None = None


@dataclass(frozen=True)
class ExpiryNotice:
    """A config record that stopped applying at run start: finding kind `mask_expired` or
    `floor_exception_expired` (4.13 finding table)."""

    kind: str
    record_id: str
    region_id: str


@dataclass
class RegionsFile:
    """One regions file per checkpoint (4.8)."""

    checkpoint_id: str
    landmarks: list[Landmark] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)

    def landmark(self, landmark_id: str) -> Landmark:
        for lm in self.landmarks:
            if lm.id == landmark_id:
                return lm
        raise KeyError(landmark_id)

    def region(self, region_id: str) -> Region:
        for r in self.regions:
            if r.id == region_id:
                return r
        raise KeyError(region_id)

    @staticmethod
    def from_dict(d: Mapping[str, Any], templates: Mapping[str, np.ndarray] | None = None,
                  template_root: str | os.PathLike | None = None) -> "RegionsFile":
        """Build from the 4.8 YAML shape. Templates come from `templates[landmark_id]`, else are
        read (RGB) from `template_root / landmark.template`."""
        lms = []
        for lm in d.get("landmarks", []):
            tpl = (templates or {}).get(lm["id"])
            if tpl is None:
                path = Path(template_root or ".") / lm["template"]
                bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if bgr is None:
                    raise RegionError(f"landmark {lm['id']}: template {path} not readable")
                tpl = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            sr = lm.get("search_rect")
            ex = lm.get("expected")
            lms.append(Landmark(
                id=str(lm["id"]), template=tpl,
                confidence_min=float(lm["confidence_min"]) if "confidence_min" in lm else None,
                search_rect=Rect(sr["x"], sr["y"], sr["w"], sr["h"]) if sr else None,
                expected=(int(ex["x"]), int(ex["y"])) if ex else None,
            ))
        rf = RegionsFile(str(d["checkpoint_id"]), lms, [Region.from_dict(r) for r in d.get("regions", [])])
        rf.validate()
        return rf

    def validate(self) -> None:
        """Static Region Registry rules the engine depends on (L-RGN-01 ids, L-RGN-02 same-landmark
        overlap, L-RGN-03, L-RGN-04; 4.8). Raises RegionError."""
        lm_ids = [lm.id for lm in self.landmarks]
        if len(set(lm_ids)) != len(lm_ids):
            raise RegionError("landmark ids must be unique")
        ids = [r.id for r in self.regions]
        if len(set(ids)) != len(ids):
            raise RegionError("region ids must be unique (L-RGN-01)")
        for r in self.regions:
            if r.id in (REST_ID, WHOLE_ID):
                raise RegionError(f"{r.id} is reserved")
            if r.type not in KINDS:
                raise RegionError(f"{r.id}: type {r.type!r} is not one of {KINDS}")
            if r.landmark not in lm_ids:
                raise RegionError(f"{r.id}: landmark {r.landmark!r} is not declared")
            if r.panel not in PANELS:
                raise RegionError(f"{r.id}: panel {r.panel!r} is not one of {PANELS}")
            if r.rect.w <= 0 or r.rect.h <= 0:
                raise RegionError(f"{r.id}: empty rect")
            census_rule = r.type == "rule" and not self.checkpoint_id.startswith("flow/")
            if r.money_cell and r.type not in PIXEL_DIFFED and not census_rule:
                raise RegionError(f"{r.id}: a money cell is diff or freeze (or rule on a census, sweep or split "
                                  f"checkpoint), never {r.type} (L-RGN-04, F41, SD-29)")
            if r.money_role is not None and (not r.money_cell or r.money_role not in MONEY_ROLES):
                raise RegionError(f"{r.id}: money_role {r.money_role!r} needs money_cell and one of {MONEY_ROLES} "
                                  "(L-RGN-08, SD-28)")
            if r.money_cell and r.floor_exception is not None:
                raise RegionError(f"{r.id}: a money cell keeps a zero floor; floor_exception refused (L-RGN-04)")
            if (r.rule is not None) != (r.type == "rule"):
                raise RegionError(f"{r.id}: rule present if and only if type is rule (L-RGN-03)")
            if (r.mask_ref is not None) != (r.type == "mask"):
                raise RegionError(f"{r.id}: mask_ref present if and only if type is mask (L-RGN-03)")
            if (r.fixture_dependency is not None) != (r.type == "freeze"):
                raise RegionError(f"{r.id}: fixture_dependency present if and only if type is freeze (L-RGN-03)")
            if r.floor_exception is not None and r.type != "diff":
                raise RegionError(f"{r.id}: floor_exception is allowed on diff regions only (4.8)")
        for i, a in enumerate(self.regions):
            for b in self.regions[i + 1:]:
                if a.landmark != b.landmark or a.type == b.type or "mask" in (a.type, b.type):
                    continue
                if a.rect.intersect(b.rect) is not None:
                    raise RegionError(f"{a.id} ({a.type}) overlaps {b.id} ({b.type}) (L-RGN-02)")


def effective_registry(regfile: RegionsFile, masks: Mapping[str, MaskRecord],
                       now: datetime) -> tuple[RegionsFile, list[ExpiryNotice]]:
    """The registry as it applies at run start (3.8, L-MSK-02, L-RGN-05).

    A mask region whose record has expired is dropped (its pixels diff as the enclosing region);
    an expired `floor_exception` is removed. Each produces an ExpiryNotice for run health.
    """
    out: list[Region] = []
    notices: list[ExpiryNotice] = []
    for r in regfile.regions:
        if r.type == "mask":
            rec = masks.get(r.mask_ref or "")
            if rec is None:
                raise RegionError(f"{r.id}: mask record {r.mask_ref!r} not found (L-RGN-03)")
            if rec.region_id != r.id:
                raise RegionError(f"{r.id}: mask record {rec.mask_id} names region {rec.region_id}")
            if rec.expired(now):
                notices.append(ExpiryNotice("mask_expired", rec.mask_id, r.id))
                continue
        if r.floor_exception is not None and r.floor_exception.expired(now):
            notices.append(ExpiryNotice("floor_exception_expired", r.floor_exception.ticket, r.id))
            r = replace(r, floor_exception=None)
        out.append(r)
    return RegionsFile(regfile.checkpoint_id, list(regfile.landmarks), out), notices


def locate(regfile: RegionsFile, positions: Mapping[str, tuple[int, int]]) -> dict[str, Rect]:
    """Absolute rects of every region whose landmark has a position (3.7: cut relative to its own
    landmark in each image). Rects may extend past the image; the engine clips and pads."""
    located: dict[str, Rect] = {}
    for r in regfile.regions:
        if r.landmark in positions:
            lx, ly = positions[r.landmark]
            located[r.id] = r.rect.offset(lx, ly)
    return located


@dataclass(frozen=True)
class RegionThreshold:
    """The threshold record a region carries for the current fingerprint (3.9, 4.8): the four
    derived values plus the measured floors they came from."""

    component_area_min: int
    changed_px_min: int
    delta_e_min: float
    shift_px_min: int
    floor_changed_px: int = 0
    floor_component_area: int = 0
    floor_delta_e: float = 0.0
    floor_shift_px: int = 0

    @property
    def nonzero_floor(self) -> bool:
        return self.floor_changed_px > 0 or self.floor_component_area > 0 or self.floor_shift_px > 0


# 3.9 step 6, 4.8: a region with no calibrated floor is diffed at threshold 0, so any changed pixel
# (or any accepted translation) is above. The color test keeps the 2.0 CIEDE2000 definition of a
# color shift (choice: a zero color threshold would flag tints that change no pixel at all).
UNCALIBRATED = RegionThreshold(component_area_min=1, changed_px_min=1, delta_e_min=2.0, shift_px_min=1)


def money_cell_threshold(th: RegionThreshold) -> RegionThreshold:
    """Money cells have a zero floor and no mask, so any changed pixel is above (3.11 note on row 11)."""
    return replace(th, component_area_min=1, changed_px_min=1, shift_px_min=1)


@dataclass(frozen=True)
class PanelBudget:
    panel: str
    side: str
    share: float
    budget: float

    @property
    def exceeded(self) -> bool:
        return self.share > self.budget


@dataclass(frozen=True)
class MoneyCellHit:
    region_id: str
    covered_by: str
    side: str


@dataclass(frozen=True)
class BudgetResult:
    """Per-capture mask budget evaluation (3.8, 4.8 "Mask budget", F34)."""

    panels: tuple[PanelBudget, ...]
    money_cell_hits: tuple[MoneyCellHit, ...]

    @property
    def exceeded(self) -> bool:
        return bool(self.money_cell_hits) or any(p.exceeded for p in self.panels)

    def detail(self) -> str:
        """The `detail` of `harness_error (reason: mask_budget_exceeded)`: panel and share."""
        parts = [f"panel {p.panel} share {p.share:.3f} over budget {p.budget:.2f} ({p.side})"
                 for p in self.panels if p.exceeded]
        parts += [f"money cell {h.region_id} covered by {h.covered_by} ({h.side})" for h in self.money_cell_hits]
        return "; ".join(parts)


def _covered_area(bounds: Rect, rects: Sequence[Rect]) -> int:
    canvas = np.zeros((bounds.h, bounds.w), dtype=bool)
    for r in rects:
        i = r.intersect(bounds)
        if i is not None:
            canvas[i.y - bounds.y:i.y - bounds.y + i.h, i.x - bounds.x:i.x - bounds.x + i.w] = True
    return int(canvas.sum())


def mask_budget(regfile: RegionsFile, located: Mapping[str, Mapping[str, Rect]],
                redactions: Sequence[RedactionBox], budgets: Mapping[str, float],
                panel_bounds: Mapping[str, Mapping[str, Rect]] | None = None, *,
                system_masks: Sequence[Rect] = ()) -> BudgetResult:
    """Evaluate the mask budget per side against the located panel bounds (3.8, 4.8).

    `located[side][region_id]` are the absolute rects of that capture; `budgets` maps a panel class
    to its share (classes absent from it, such as `other`, have no budget). The share is the area
    of masks plus non-identity redaction boxes inside the panel's bounds over the bounds' area.
    Any mask or redaction box intersecting a money cell breaches the money cell's zero budget.
    `system_masks` (the parking rectangle of 3.5, applied on both sides) are masks (3.2) and count
    the same way on each side.
    """
    panels: list[PanelBudget] = []
    hits: list[MoneyCellHit] = []
    by_id = {r.id: r for r in regfile.regions}
    for side in SIDES:
        loc = located.get(side, {})
        masks = [(rid, rect) for rid, rect in loc.items() if by_id[rid].type == "mask"]
        masks += [(f"system_mask@{r.x},{r.y}", r) for r in system_masks]
        cover = [rect for _, rect in masks] + [b.rect for b in redactions if not b.identity_field]
        for panel, budget in budgets.items():
            bounds = (panel_bounds or {}).get(side, {}).get(panel)
            if bounds is None:
                members = [rect for rid, rect in loc.items() if by_id[rid].panel == panel]
                if not members:
                    continue
                bounds = Rect.bounding(members)
            if bounds.area == 0:
                continue
            share = _covered_area(bounds, cover) / bounds.area
            panels.append(PanelBudget(panel, side, share, float(budget)))
        boxes = masks + [(b.anchor_id or f"redaction@{b.rect.x},{b.rect.y}", b.rect) for b in redactions]
        for rid, rect in loc.items():
            if not by_id[rid].money_cell:
                continue
            for cid, crect in boxes:
                if crect.intersect(rect) is not None:
                    hits.append(MoneyCellHit(rid, cid, side))
    return BudgetResult(tuple(panels), tuple(hits))


def sensitivity(regfile: RegionsFile, located: Mapping[str, Rect], redactions: Sequence[RedactionBox],
                width: int, height: int) -> dict[str, float]:
    """Effective sensitivity of one capture (3.8): shares of the window that are pixel-diffed
    (`diff` plus `freeze`, including `_rest`), `rule`, and `mask` plus redaction."""
    kind = np.zeros((height, width), dtype=np.uint8)          # 0 diffed, 1 rule, 2 masked
    window = Rect(0, 0, width, height)
    by_id = {r.id: r for r in regfile.regions}
    for layer, code in (("rule", 1), ("mask", 2)):
        for rid, rect in located.items():
            if by_id[rid].type == layer:
                i = rect.intersect(window)
                if i is not None:
                    kind[i.y:i.y + i.h, i.x:i.x + i.w] = code
    for b in redactions:
        i = b.rect.intersect(window)
        if i is not None:
            kind[i.y:i.y + i.h, i.x:i.x + i.w] = 2
    total = float(width * height)
    return {"diff_freeze": float((kind == 0).sum()) / total, "rule": float((kind == 1).sum()) / total,
            "mask_redaction": float((kind == 2).sum()) / total}
