"""The pre-run and CI anchor lint (11.5 Pre-run anchor lint, 12.7 G1, 12.1 Anchor lint, 12.2 `flow_maintenance_needed`,
5.16 Failure outcomes).

Before every run the Orchestrator matches every anchor of every scheduled flow and every Region Registry landmark
against the latest approved base-build capture of its checkpoint. A miss is a maintenance item posted to the flow
owner in run health (finding `flow_maintenance_needed`), never a checkpoint outcome, and the run continues; the flow
still walks the ladder, and its `locator_failure` findings are pre-labeled maintenance. The same lint runs in CI on
every pull request touching a flow, mask or registry file (`sentinel lint --anchors`).

- `anchor_lint(flows, regions, baselines, artifacts, *, surfaces, templates)` returns an `AnchorLintReport`: the
  misses (`AnchorMiss`, each with its best score, the crop at the best match and the owner), the anchors checked and
  the ones that could not be checked (no approved capture, no checkpoint of the control's surface in the flow).
- `AnchorLintReport.findings(run_id)` gives the `flow_maintenance_needed` findings, `items(opened_at)` the
  `records.MaintenanceItem`s for `maintenance.open_items`, `health_rows()` the run-health rows (`summary
  ["health_rows"]`), and `prelabel(evidence, flow_id, step_id)` the evidence of a `locator_failure` finding with the
  maintenance label added when the pre-run lint already missed that step's anchor.
- `approved_captures(store, checkpoint_ids)` reads the approved baseline image of each checkpoint (8.3 `baselines`,
  along the rename chain), and `SetArtifacts(store, root)` reads an object from whichever artifact set holds it.
- `main(argv)` is `sentinel lint --anchors <config-dir> --store DB --artifacts ROOT [--flow ID ...] [--json]`.

Choices where the spec is ambiguous or silent (reported):

1. The capture an anchor is matched on. A control anchor of a flow step (`target`, `from`, `to`, `at`) is matched on
   the approved capture of the last checkpoint step of the flow at or before the step whose `surface_id` is the
   control's surface (the screen the step acts on), else of the first such checkpoint after it (a step before the
   flow's first capture of that screen); steps of the `normalize` block, which have no place in the sequence, take
   the last such checkpoint. A `wait_for` landmark that is a Surface Registry landmark of one of the flow's surfaces
   is matched on the first checkpoint at or after the step (the screen it waits for). Every landmark of a regions
   file is matched on its own checkpoint's capture; a `wait_for` landmark that is a regions-file landmark is covered
   there, once.
2. Thresholds are the ones the run applies: a control anchor is rung 2 of the ladder (5.5: best at least the 0.92
   floor, second peak at least 0.05 below it, outside `max(5, min(w, h) // 2)` px), searched on the whole capture
   (the registry declares no search region); a regions-file landmark is `diff.engine.locate_landmark` with the
   `landmarks` parameters of `diff_engine.yaml` (3.7: its `confidence_min`, second peak under 0.85, the search box
   its expected position plus or minus 200 px); a Surface Registry landmark the same on the whole capture.
3. An anchor whose checkpoint has no approved capture is not checked and is listed in `skipped` (a new checkpoint
   has nothing to be checked against yet); a missing or unreadable template file is a miss with cause
   `template_missing` (G1 "landmark templates present").
4. Owners: the flow file's `owner` for flow anchors and for the landmarks of a flow checkpoint (`flow/...`), the
   surface's `owner` for the landmarks of a census, sweep or split checkpoint.
5. The maintenance item is of kind `anchor_lint` (8.3 `maintenance_items.kind` is open text), `ref`
   `<flow_id>/<step_id>` for a flow anchor and `<checkpoint_id>#<landmark_id>` for a landmark. The finding names
   the checkpoint, the step and the flow's or checkpoint's tier, and carries the crop (`evidence.artifacts`).
6. "Pre-labeled maintenance": no taxonomy label exists for it, so the label is the evidence key `maintenance` (4.13
   lets a finding's evidence carry the caller's context), holding the lint's miss for that step.
7. `sentinel lint --anchors` exits 1 when any anchor misses (the PR that re-anchors must "pass the lint", 11.5), 0
   when clean and 2 on a usage error; at run start the caller reads the report and the run continues (12.7 G1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from ..diff import load_params
from ..diff.engine import locate_landmark, match_template
from ..diff.regions import Landmark, Rect
from ..executor.locator import CROP_H, CROP_W, IMAGE_FLOOR, MIN_PEAK_RADIUS, SECOND_PEAK_MARGIN
from ..records import Finding, MaintenanceItem, now
from ..taxonomy import checkpoint_parts
from .repo import Repo
from .rules import TARGET_FIELDS

FINDING_KIND = "flow_maintenance_needed"          # 12.2
ITEM_KIND = "anchor_lint"                         # choice 5
LABEL_KEY = "maintenance"                         # choice 6
ROW_LABEL = "Anchor lint"                         # run health (11.5)
SURFACE_LANDMARK_FLOOR = 0.92                     # 4.5 landmarks confidence_min default (3.7)


class ArtifactReader(Protocol):
    """The artifact store as the lint reads it: the bytes of one object by its sha256 (8.4 `ArtifactStore.get`)."""

    def get(self, sha: str) -> bytes:
        """The object's bytes."""


@dataclass(frozen=True)
class AnchorMiss:
    """One anchor that no longer matches the latest approved capture of its checkpoint (11.5): what it is (`control`
    for a flow step's image anchor, `landmark` for a regions-file landmark, `wait_for` for a Surface Registry landmark
    a `wait_for` step waits on), where, the scores against its floor, why it missed, whose it is, and the crop at
    the best match on the approved capture."""

    kind: str
    checkpoint_id: str
    anchor: str
    template: str
    owner: str
    cause: str
    score: float
    second: float
    floor: float
    flow_id: str | None = None
    step_id: str | None = None
    tier: int | None = None
    crop_png: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def crop_sha256(self) -> str | None:
        return hashlib.sha256(self.crop_png).hexdigest() if self.crop_png is not None else None

    @property
    def ref(self) -> str:
        """The maintenance item's `ref` (choice 5)."""
        if self.flow_id is not None and self.step_id is not None:
            return f"{self.flow_id}/{self.step_id}"
        return f"{self.checkpoint_id}#{self.anchor}"

    def subject(self) -> str:
        where = f"{self.flow_id} {self.step_id}" if self.flow_id and self.step_id else self.checkpoint_id
        return f"{where}: {self.kind} {self.anchor}"

    def line(self) -> str:
        """`<flow> <step>: control <name> (<template>) <cause> <score> < <floor> on <checkpoint>; owner <owner>`."""
        scores = (f"{self.score:.2f} < {self.floor:.2f}" if self.cause == "below_floor" else
                  f"second peak {self.second:.2f} within {SECOND_PEAK_MARGIN} of {self.score:.2f}"
                  if self.cause == "second_peak" else self.cause.replace("_", " "))
        return f"{self.subject()} ({self.template}) {scores} on {self.checkpoint_id}; owner {self.owner}"

    def evidence(self) -> dict[str, Any]:
        """The finding's evidence: the anchor, its scores, the checkpoint it was matched on and the crop."""
        ev: dict[str, Any] = {"anchor_kind": self.kind, "anchor": self.anchor, "template": self.template,
                              "checkpoint_id": self.checkpoint_id, "cause": self.cause, "score": round(self.score, 4),
                              "second": round(self.second, 4), "floor": self.floor, "owner": self.owner}
        if self.flow_id is not None:
            ev["flow_id"] = self.flow_id
        if self.crop_sha256 is not None:
            ev.update(crop_sha256=self.crop_sha256, artifacts=[self.crop_sha256])
        return ev

    def finding(self, finding_id: str, run_id: str, *, created_at: str | None = None) -> Finding:
        """The `flow_maintenance_needed` finding of this miss (12.2), a maintenance item, never an outcome."""
        return Finding(finding_id=finding_id, run_id=run_id, kind=FINDING_KIND, first_seen_run_id=run_id,
                       description=f"anchor no longer matches the approved capture: {self.subject()}",
                       checkpoint_id=self.checkpoint_id, step_id=self.step_id, tier=self.tier,
                       evidence=self.evidence(), detail=self.line(), created_at=created_at or now())

    def maintenance_item(self, opened_at: str, *, run_id: str | None = None) -> MaintenanceItem:
        """The 11.5 maintenance item for the owner (choice 5)."""
        return MaintenanceItem(ITEM_KIND, self.ref, self.owner, opened_at, run_id=run_id, detail=self.line())


@dataclass(frozen=True)
class Skipped:
    """An anchor the lint could not check, and why (choice 3)."""

    subject: str
    why: str


@dataclass(frozen=True)
class AnchorLintReport:
    """What one anchor lint found (module docstring)."""

    misses: tuple[AnchorMiss, ...]
    checked: int
    skipped: tuple[Skipped, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.misses

    def missed_step(self, flow_id: str, step_id: str) -> AnchorMiss | None:
        """The lint's miss on that flow step's anchor, if any."""
        return next((m for m in self.misses if (m.flow_id, m.step_id) == (flow_id, step_id)), None)

    def prelabel(self, evidence: Mapping[str, Any], flow_id: str, step_id: str) -> dict[str, Any]:
        """A `locator_failure` finding's evidence with the maintenance label when the pre-run lint missed that step's
        anchor (5.16, choice 6); otherwise the evidence unchanged."""
        out = dict(evidence)
        miss = self.missed_step(flow_id, step_id)
        if miss is not None:
            out[LABEL_KEY] = {"source": ITEM_KIND, "anchor": miss.anchor, "checkpoint_id": miss.checkpoint_id,
                              "cause": miss.cause, "score": round(miss.score, 4)}
        return out

    def findings(self, run_id: str, *, created_at: str | None = None) -> list[Finding]:
        """One `flow_maintenance_needed` finding per miss, ids `fnd-<run>-anchor-NNN`."""
        return [m.finding(f"fnd-{run_id}-anchor-{n:03d}", run_id, created_at=created_at)
                for n, m in enumerate(self.misses, 1)]

    def items(self, opened_at: str, *, run_id: str | None = None) -> list[MaintenanceItem]:
        """One maintenance item per miss, for the owner (11.5)."""
        return [m.maintenance_item(opened_at, run_id=run_id) for m in self.misses]

    def health_rows(self) -> list[tuple[str, str]]:
        """The run-health row: the misses with their owners, or clean, and what was not checked."""
        if self.misses:
            text = (f"{len(self.misses)} of {self.checked} anchors no longer match the approved capture "
                    f"(maintenance, not outcomes): " + "; ".join(m.line() for m in self.misses))
        else:
            text = f"clean ({self.checked} anchors checked)"
        if self.skipped:
            text += f"; {len(self.skipped)} not checked"
        return [(ROW_LABEL, text)]


# ---------------------------------------------------------------------------------------------------------------
# inputs


class TemplateLoader:
    """Template images (RGB) by repository-relative path under a config checkout, cached; None when unreadable."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[str, np.ndarray | None] = {}

    def __call__(self, rel: str) -> np.ndarray | None:
        if rel not in self._cache:
            p = self.root / rel
            img = cv2.imread(str(p), cv2.IMREAD_COLOR) if rel and ".." not in Path(rel).parts and p.is_file() else None
            self._cache[rel] = None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return self._cache[rel]


def decode_png(data: bytes) -> np.ndarray | None:
    """PNG (or any image) bytes as an RGB array, None when they do not decode."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _png(img: np.ndarray | None) -> bytes | None:
    if img is None or img.size == 0:
        return None
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR))
    return bytes(buf) if ok else None


def _crop_at(image: np.ndarray, center: tuple[float, float], w: int = CROP_W, h: int = CROP_H) -> np.ndarray | None:
    H, W = image.shape[:2]
    x0, y0 = int(round(center[0])) - w // 2, int(round(center[1])) - h // 2
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
    return None if xb <= xa or yb <= ya else image[ya:yb, xa:xb]


def controls_of(surfaces: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Every registry control by its flow target name `<surface id>.<control id>` (4.7), with its surface id."""
    out: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for sid, s in surfaces.items():
        for c in s.get("controls") or ():
            if isinstance(c, Mapping) and c.get("id"):
                out[f"{sid}.{c['id']}"] = (sid, c)
    return out


def _checkpoints(flow: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    """(position, checkpoint id, surface id) of every checkpoint step of the flow's steps block."""
    return [(i, str(s["checkpoint_id"]), str(s.get("surface_id", ""))) for i, s in enumerate(flow.get("steps") or ())
            if isinstance(s, Mapping) and s.get("type") == "checkpoint" and s.get("checkpoint_id")]


def _for_control(cps: Sequence[tuple[int, str, str]], pos: int | None, surface: str) -> str | None:
    """Choice 1: the last checkpoint on `surface` at or before `pos`, else the first after it (None: the last)."""
    on = [(i, cid) for i, cid, sid in cps if sid == surface]
    if not on:
        return None
    if pos is None:
        return on[-1][1]
    before = [cid for i, cid in on if i <= pos]
    return before[-1] if before else on[0][1]


def _for_wait(cps: Sequence[tuple[int, str, str]], pos: int) -> str | None:
    return next((cid for i, cid, _ in cps if i >= pos), None)


def _owner_of(cid: str, flows: Mapping[str, Mapping[str, Any]],
              surfaces: Mapping[str, Mapping[str, Any]]) -> tuple[str, int | None]:
    """Choice 4: (owner, tier) of a checkpoint's landmarks."""
    parts = checkpoint_parts(cid)
    if parts["kind"] == "flow":
        flow = flows.get(parts["qualifier"].split("/")[0])
        if flow is not None:
            return str(flow.get("owner", "unknown")), _tier(flow.get("tier"))
    s = surfaces.get(parts["surface"]) or {}
    return str(s.get("owner", "unknown")), _tier(s.get("tier"))


def _tier(v: Any) -> int | None:
    return v if v in (1, 2, 3) else None


# ---------------------------------------------------------------------------------------------------------------
# the lint


class _Lint:
    def __init__(self, baselines: Mapping[str, str | np.ndarray], artifacts: ArtifactReader | None,
                 templates: Callable[[str], np.ndarray | None], params: Mapping[str, Any]):
        self.baselines, self.artifacts, self.templates = baselines, artifacts, templates
        self.params = params
        self.images: dict[str, np.ndarray | None] = {}
        self.misses: list[AnchorMiss] = []
        self.skipped: list[Skipped] = []
        self.checked = 0

    def capture(self, cid: str) -> np.ndarray | None:
        if cid not in self.images:
            b = self.baselines.get(cid)
            img: np.ndarray | None = None
            if isinstance(b, np.ndarray):
                img = b
            elif isinstance(b, str) and self.artifacts is not None:
                try:
                    img = decode_png(self.artifacts.get(b))
                except Exception:                      # a missing or corrupt object: nothing to check against
                    img = None
            self.images[cid] = img
        return self.images[cid]

    def check(self, kind: str, cid: str | None, anchor: str, template: str, owner: str, *, flow_id: str | None = None,
              step_id: str | None = None, tier: int | None = None, floor: float = IMAGE_FLOOR,
              landmark: Mapping[str, Any] | None = None, subject: str = "") -> None:
        if cid is None:
            self.skipped.append(Skipped(subject, "no checkpoint of the anchor's surface in the flow"))
            return
        image = self.capture(cid)
        if image is None:
            self.skipped.append(Skipped(subject, f"no approved capture of {cid}"))
            return
        self.checked += 1
        tpl = self.templates(template) if template else None
        base = dict(kind=kind, checkpoint_id=cid, anchor=anchor, template=template, owner=owner, flow_id=flow_id,
                    step_id=step_id, tier=tier)
        if tpl is None:
            self.misses.append(AnchorMiss(cause="template_missing", score=0.0, second=0.0, floor=floor, **base))
            return
        th, tw = tpl.shape[:2]
        if landmark is not None:
            lm = Landmark(anchor, tpl, landmark.get("confidence_min"), _rect(landmark.get("search_rect")),
                          _point(landmark.get("expected")))
            m = locate_landmark(image, lm, self.params)
            floor = float(self.params["landmarks"]["confidence_min"] if lm.confidence_min is None
                          else lm.confidence_min)
            best, second, pos, ok = m.score, m.second, m.pos, m.found
            if pos is None and m.score > 0:
                H, W = image.shape[:2]
                _, _, pos = match_template(image, tpl, lm.search_box(W, H, int(self.params["landmarks"]
                                                                                 ["search_margin_px"])))
            cause = "below_floor" if best < floor else "second_peak"
        else:
            best, second, pos = match_template(image, tpl, None, max(MIN_PEAK_RADIUS, min(tw, th) // 2))
            ok = pos is not None and best >= floor and second <= best - SECOND_PEAK_MARGIN
            cause = "below_floor" if best < floor else "second_peak"
        if pos is None:
            ok, cause = False, "template_larger_than_capture"
        if ok:
            return
        crop = _crop_at(image, (pos[0] + tw / 2.0, pos[1] + th / 2.0)) if pos is not None else None
        self.misses.append(AnchorMiss(cause=cause, score=float(best), second=float(second), floor=float(floor),
                                      crop_png=_png(crop), **base))


def _rect(d: Any) -> Rect | None:
    return Rect(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"])) if isinstance(d, Mapping) else None


def _point(d: Any) -> tuple[int, int] | None:
    return (int(d["x"]), int(d["y"])) if isinstance(d, Mapping) else None


def anchor_lint(flows: Iterable[Mapping[str, Any]],
                regions: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
                baselines: Mapping[str, str | np.ndarray], artifacts: ArtifactReader | None = None, *,
                surfaces: Mapping[str, Mapping[str, Any]], templates: Callable[[str], np.ndarray | None] | str | Path,
                params: Mapping[str, Any] | None = None) -> AnchorLintReport:
    """Match every anchor of every flow in `flows` (the scheduled flows' documents, 4.7) and every landmark of
    `regions` (regions documents, 4.8, by checkpoint id or as a list) against the approved capture of its checkpoint.

    `baselines` maps a checkpoint id to its approved capture, an RGB array or the sha256 `artifacts` holds it under;
    `surfaces` maps a surface id to its registry entry (4.5); `templates` loads a template by repository-relative
    path (or is the config checkout's root); `params` are the Diff Engine parameters (default `diff_engine.yaml`).
    Reads only; the caller records the findings and items (module docstring)."""
    load = TemplateLoader(templates) if isinstance(templates, (str, Path)) else templates
    lint = _Lint(baselines, artifacts, load, params or load_params())
    flow_docs = {str(f.get("flow_id")): f for f in flows if isinstance(f, Mapping)}
    controls = controls_of(surfaces)
    reg_docs = list(regions.values()) if isinstance(regions, Mapping) else list(regions)
    region_landmarks = {str(lm.get("id")) for d in reg_docs for lm in d.get("landmarks") or ()
                        if isinstance(lm, Mapping)}
    for fid, flow in sorted(flow_docs.items()):
        owner, tier = str(flow.get("owner", "unknown")), _tier(flow.get("tier"))
        cps = _checkpoints(flow)
        surface_lms = {str(lm.get("id")): lm for sid in flow.get("surfaces") or ()
                       for lm in (surfaces.get(str(sid)) or {}).get("landmarks") or () if isinstance(lm, Mapping)}
        blocks = [(i, s) for i, s in enumerate(flow.get("steps") or ())]
        blocks += [(len(blocks) + i, s) for i, s in enumerate(flow.get("teardown") or ())]
        blocks += [(None, s) for s in flow.get("normalize") or ()]
        for pos, step in blocks:
            if not isinstance(step, Mapping):
                continue
            sid = str(step.get("id", "?"))
            for k in TARGET_FIELDS:
                t = step.get(k)
                if not isinstance(t, str) or t not in controls:
                    continue
                surface, control = controls[t]
                lint.check("control", _for_control(cps, pos, surface), t, str(control.get("template", "")), owner,
                           flow_id=fid, step_id=sid, tier=tier, subject=f"{fid} {sid}: control {t}")
            anchor = step.get("anchor") if step.get("type") == "wait_for" else None
            lm_id = str(anchor.get("landmark")) if isinstance(anchor, Mapping) and anchor.get("landmark") else None
            if lm_id and lm_id in surface_lms and lm_id not in region_landmarks and pos is not None:
                lm = surface_lms[lm_id]
                lint.check("wait_for", _for_wait(cps, pos), lm_id, str(lm.get("template", "")), owner, flow_id=fid,
                           step_id=sid, tier=tier, floor=float(lm.get("confidence_min", SURFACE_LANDMARK_FLOOR)),
                           landmark={"confidence_min": lm.get("confidence_min", SURFACE_LANDMARK_FLOOR)},
                           subject=f"{fid} {sid}: landmark {lm_id}")
    for doc in sorted(reg_docs, key=lambda d: str(d.get("checkpoint_id"))):
        cid = str(doc.get("checkpoint_id"))
        owner, tier = _owner_of(cid, flow_docs, surfaces)
        for lm in doc.get("landmarks") or ():
            if isinstance(lm, Mapping) and lm.get("id"):
                lint.check("landmark", cid, str(lm["id"]), str(lm.get("template", "")), owner, tier=tier,
                           landmark=lm, subject=f"{cid}: landmark {lm['id']}")
    return AnchorLintReport(tuple(lint.misses), lint.checked, tuple(lint.skipped))


# ---------------------------------------------------------------------------------------------------------------
# the store side and the CLI


def approved_captures(store: Any, checkpoint_ids: Iterable[str], lineage: str = "released") -> dict[str, str]:
    """The approved baseline image sha256 of each checkpoint that has one (8.3, along the rename chain)."""
    from ..store.mirror import approved_baseline
    out = {}
    for cid in checkpoint_ids:
        row = approved_baseline(store, cid, lineage)
        if row is not None:
            out[cid] = str(row["image_sha256"])
    return out


class SetArtifacts:
    """An `ArtifactReader` over an artifact root: an object is read from a set the store says holds it (8.3
    `artifact_sets`, then `artifacts.set_path`)."""

    def __init__(self, store: Any, root: str | Path):
        self.store, self.root = store, Path(root)

    def get(self, sha: str) -> bytes:
        from ..store.artifacts import ArtifactMissing, ArtifactStore
        sets = [r[0] for r in self.store.query("SELECT set_path FROM artifact_sets WHERE sha256 = ? AND deleted_at "
                                               "IS NULL UNION SELECT set_path FROM artifacts WHERE sha256 = ?",
                                               (sha, sha))]
        for set_path in sets:
            try:
                return ArtifactStore.at(self.root, set_path, require_local=False).get(sha)
            except ArtifactMissing:
                continue
        raise ArtifactMissing(sha)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel lint --anchors",
                                description="Match flow anchors and region landmarks against approved captures (11.5).")
    p.add_argument("config_dir", help="root of the sentinel-config checkout")
    p.add_argument("--store", required=True, help="the Central Store database holding the approved baselines")
    p.add_argument("--artifacts", required=True, help="the artifact root holding the baseline images")
    p.add_argument("--lineage", default="released", help="the baseline lineage (default released)")
    p.add_argument("--flow", action="append", default=[], help="lint only this flow (repeatable; default all)")
    p.add_argument("--json", action="store_true", help="one JSON object per miss")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """`sentinel lint --anchors`: 1 when any anchor misses, 0 when clean, 2 on a usage error (choice 7)."""
    args_in = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--anchors"]
    if args_in[:1] == ["lint"]:
        args_in = args_in[1:]
    try:
        args = _parser().parse_args(args_in)
    except SystemExit as exc:
        return int(exc.code or 0)
    root = Path(args.config_dir)
    if not root.is_dir() or not Path(args.store).is_file():
        print(f"sentinel lint --anchors: {root if not root.is_dir() else args.store} does not exist", file=sys.stderr)
        return 2
    repo = Repo.load(root)
    flows = [d.data for d in repo.of("flow") if isinstance(d.data, dict)
             and (not args.flow or d.data.get("flow_id") in args.flow)]
    surfaces = {d.data["id"]: d.data for d in repo.of("surface") if isinstance(d.data, dict) and d.data.get("id")}
    regions = {d.data["checkpoint_id"]: d.data for d in repo.of("regions")
               if isinstance(d.data, dict) and d.data.get("checkpoint_id")}
    if args.flow:
        cps = {cid for f in flows for _, cid, _ in _checkpoints(f)}
        regions = {k: v for k, v in regions.items() if k in cps}
    from ..store.store import Store
    try:
        with Store(args.store) as store:
            cids = {cid for f in flows for _, cid, _ in _checkpoints(f)} | set(regions)
            report = anchor_lint(flows, regions, approved_captures(store, sorted(cids), args.lineage),
                                 SetArtifacts(store, args.artifacts), surfaces=surfaces, templates=root)
    except (OSError, sqlite3.Error) as exc:
        print(f"sentinel lint --anchors: {exc}", file=sys.stderr)
        return 2
    for m in report.misses:
        print(json.dumps({"rule": FINDING_KIND, "severity": "error", "file": m.ref, "message": m.line()})
              if args.json else f"{FINDING_KIND} {m.line()}")
    for s in report.skipped:
        print(f"not checked: {s.subject} ({s.why})", file=sys.stderr)
    print(f"{len(report.misses)} anchor misses, {report.checked} checked, {len(report.skipped)} not checked",
          file=sys.stderr)
    return 1 if report.misses else 0


__all__ = ["AnchorLintReport", "AnchorMiss", "SetArtifacts", "Skipped", "TemplateLoader", "anchor_lint",
           "approved_captures", "controls_of", "decode_png", "main"]


if __name__ == "__main__":
    sys.exit(main())
