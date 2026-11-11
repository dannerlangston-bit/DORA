"""The live Reviewer on a simulated run (3.12, 3.15): `reviewer.ModelReviewer` behind the pipeline's adapter.

The default Reviewer of every run is the offline `SimulatedReviewer`. `--reviewer live` (`sentinel demo`,
`sentinel run`) gives `run.run_pipeline` a PR-run `reviewer.ReviewerJob` from `live_job`, which exists only
when `ANTHROPIC_API_KEY` is set (`reviewer.NetworkNotAllowed` otherwise, before any client is built), with
the settings of a `reviewer:` block (`load_reviewer_config`, `--reviewer-config FILE`; the example config
has no such block, so the defaults of `reviewer.config` apply unless a file is given). The
run then builds `ModelReviewer(job, ItemBuilder(...))`: the pipeline's `ReviewRequest` carries no pixels,
so `ItemBuilder` turns each request into the full 3.12 `ReviewItem`:

- the crops: both builds' settled captures are put in the run's artifact store under the run's redaction
  header (synthetic screens carry no account data) and read back from it, and each step's base, candidate
  and heatmap crop is cut at 1:1 with `reviewer.crop_box`: the diff components' bounding box padded by the
  crop policy's pad and snapped to the item's region (the whole capture when the item names no declared
  region); a sweep cluster sends its smallest and largest step (3.12);
- the diff metrics in crop coordinates, the flow context (the last three step actions before the
  checkpoint, its must-see landmark, its masks and rule regions), the OCR words inside the crop as
  untrusted screen text, the manifest entry (task A) or dependency edge (task B), and the version fields
  of the verdict cache (base and candidate crop hashes, mask, redaction and registry versions).

`CollectingReviewer` answers a run offline and keeps every request's `ReviewItem`; `sentinel review --live`
hands them to `reviewer.review_live` (network only with `allow_network=True` and a key).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

import cv2
import numpy as np
import yaml

from ..egress import EgressGate
from ..reviewer import ConfigError, ReviewerConfig, ReviewerJob
from ..reviewer.images import crop_box
from ..reviewer.prompt import DependencyEdge, DiffMetrics, FlowContext, ManifestEntryRef, ReviewItem, StepCrops
from ..reviewer.transport import AnthropicTransport
from ..store.artifacts import ArtifactStore, Redaction
from .reviewer import ReviewRequest, SimulatedReviewer

Box = tuple[int, int, int, int]


def load_reviewer_config(path: str | Path | None) -> ReviewerConfig:
    """The `reviewer:` block of a YAML file through `ReviewerConfig.from_mapping` (unknown keys refused);
    the defaults (the model pin of `reviewer.config`) without a file."""
    if path is None:
        return ReviewerConfig()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    block = data.get("reviewer") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: no `reviewer:` mapping")
    return ReviewerConfig.from_mapping(block)


def live_job(env: Mapping[str, str] | None = None, config: ReviewerConfig | None = None,
             gate: EgressGate | None = None) -> ReviewerJob:
    """A PR-run `ReviewerJob` over the real API; raises `NetworkNotAllowed` unless `ANTHROPIC_API_KEY` is set. With
    `gate` (the run's `egress.EgressGate`, built by `run.run_pipeline`) every request is checked against the signed
    allowlist and logged to `egress_log` (6.10)."""
    cfg = config or ReviewerConfig()
    transport = AnthropicTransport.from_env(allow_network=True, env=env, base_url=cfg.endpoint_base_url, gate=gate)
    return ReviewerJob(cfg, transport, "pr")


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def _decode(data: bytes) -> np.ndarray:
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


class ItemBuilder:
    """`item_for(ReviewRequest) -> ReviewItem` over one run's evaluated checkpoints (module docstring)."""

    def __init__(self, run_id: str, arts: ArtifactStore, redaction: Redaction, evals: Mapping[str, Any], *,
                 manifest: Any, epoch: str, flow_docs: Mapping[str, Mapping[str, Any]], pad: int):
        self.run_id, self.arts, self.redaction, self.evals = run_id, arts, redaction, evals
        self.manifest, self.epoch, self.flow_docs, self.pad = manifest, epoch, flow_docs, pad

    def _stored(self, img: np.ndarray, cid: str, side: str) -> np.ndarray:
        """The capture as the artifact store holds it (written once, read back)."""
        a = self.arts.put(_png(img), name=f"{self.run_id}/{cid}/review/{side}.png", kind="capture",
                          media_type="image/png", redaction=self.redaction, checkpoint_id=cid, side=side)
        return _decode(self.arts.get(a.sha256))

    def _region(self, ev: Any, region_id: str | None, shape: tuple[int, ...]) -> Box:
        h, w = shape[:2]
        for r in ev.registry.regions:
            if r.id == region_id:
                lm = ev.registry.landmark(r.landmark).expected
                if lm is not None:
                    x0, y0 = max(0, r.rect.x + lm[0]), max(0, r.rect.y + lm[1])
                    return (x0, y0, max(1, min(w, r.rect.x + lm[0] + r.rect.w) - x0),
                            max(1, min(h, r.rect.y + lm[1] + r.rect.h) - y0))
        return (0, 0, w, h)

    def _step(self, ev: Any, region_id: str | None, size_step: str | None) -> tuple[StepCrops, Box, list[Box]]:
        cid = ev.cp.checkpoint_id
        bimg, cimg = ev.images()                     # the redacted pair (3.6): nothing unredacted is sent
        base = self._stored(bimg if bimg is not None else cimg, cid, "base")
        cand = self._stored(cimg, cid, "candidate")
        region = self._region(ev, region_id, cand.shape)
        comps = [tuple(c.bbox) for c in (ev.diff.summary.components if ev.diff is not None else ())]
        rx, ry, rw, rh = region
        inside = [c for c in comps if c[0] < rx + rw and rx < c[0] + c[2] and c[1] < ry + rh and ry < c[1] + c[3]]
        if inside:
            x0, y0 = min(c[0] for c in inside), min(c[1] for c in inside)
            x1, y1 = max(c[0] + c[2] for c in inside), max(c[1] + c[3] for c in inside)
            box = crop_box((x0, y0, x1 - x0, y1 - y0), region, self.pad)
        else:
            box = region
        x, y, w, h = box
        heat = ev.diff.heatmap if ev.diff is not None and ev.diff.heatmap is not None else np.zeros_like(cand)
        crops = StepCrops(size_step, base[y:y + h, x:x + w].copy(), cand[y:y + h, x:x + w].copy(),
                          heat[y:y + h, x:x + w].copy())
        local = [(max(c[0], x) - x, max(c[1], y) - y, min(c[0] + c[2], x + w) - max(c[0], x),
                  min(c[1] + c[3], y + h) - max(c[1], y)) for c in inside]
        return crops, box, [c for c in local if c[2] > 0 and c[3] > 0]

    def _flow(self, ev: Any) -> FlowContext:
        cp = ev.cp
        steps = list(self.flow_docs.get(cp.flow_id or "", {}).get("steps") or ()) if cp.flow_id else []
        before: list[str] = []
        for st in steps:
            if st.get("id") == cp.step_id:
                break
            if st.get("type") not in ("checkpoint", "wait_for"):
                before.append(f"{st.get('type')} {st.get('target') or st.get('key') or ''}".strip())
        rules = tuple(f"{r.type}: {r.id}" for r in ev.registry.regions if r.type in ("mask", "rule"))
        return FlowContext(tuple(before[-3:]), f"{cp.surface_id} ({cp.test_kind})",
                           tuple(lm.id for lm in ev.registry.landmarks), rules)

    def __call__(self, req: ReviewRequest) -> ReviewItem:
        evs = [self.evals[c] for c in req.checkpoint_ids]
        ends = [evs[0], evs[-1]] if len(evs) > 1 and req.sizes else [evs[0]]
        sizes = list(req.sizes) if len(ends) == 2 else [None]
        built = [self._step(ev, req.region_id, size) for ev, size in zip(ends, sizes)]
        rep, (_, box, comps) = ends[0], built[0]
        words = [w.text for w in rep.cand.model.words
                 if box[0] <= w.bbox.center[0] < box[0] + box[2] and box[1] <= w.bbox.center[1] < box[1] + box[3]]
        entry = edge = None
        if req.entry is not None:
            declared = ()
            if self.manifest is not None:
                e = next((x for x in self.manifest.entries if x.entry_id == req.entry["entry_id"]), None)
                declared = tuple(sorted(e.declared_surfaces)) if e is not None else ()
            entry = ManifestEntryRef(str(req.entry["entry_id"]), tuple(req.entry.get("tickets") or ()),
                                     str(req.entry.get("summary") or ""), declared,
                                     str(req.entry.get("expected_visual") or ""))
        if req.edge is not None:
            edge = DependencyEdge(str(req.edge["primitive"]), str(req.edge["entry_id"]))
        title = next((d.text for d in rep.cand.screen.components if d.kind == "titlebar" and d.text), "")
        hashes = [hashlib.sha256(s.base.tobytes()).hexdigest() for s, _, _ in built]
        chashes = [hashlib.sha256(s.candidate.tobytes()).hexdigest() for s, _, _ in built]
        masks = ",".join(sorted(str(r.mask_ref) for r in rep.registry.regions if r.mask_ref))
        registry = hashlib.sha256("|".join(f"{r.id}:{r.rect}" for r in rep.registry.regions).encode()).hexdigest()[:16]
        return ReviewItem(
            task=req.task, queue_key=f"{self.run_id}|{req.checkpoint_ids[0]}|{req.region_id or '_window'}|{req.task}",
            checkpoint_ids=tuple(req.checkpoint_ids), region=req.region_id or "_window", tier=req.tier,
            data_mode=req.data_mode, data_epoch=self.epoch, workspace_id=req.workspace_id, window_title=title,
            diff=DiffMetrics(int(req.diff.get("changed_px", 0)), tuple(comps),
                             tuple(int(v) for v in req.diff.get("shift", (0, 0)))),
            flow=self._flow(rep), steps=tuple(s for s, _, _ in built), entry=entry, edge=edge,
            ocr_text=tuple(words), base_hash="|".join(hashes), candidate_hash="|".join(chashes),
            mask_version=masks or "none", redaction_version=self.redaction.version, registry_version=registry)


class CollectingReviewer:
    """The offline `SimulatedReviewer` answering a run, keeping the `ReviewItem` of every request it got, for
    `sentinel review --live` to hand to `reviewer.review_live`. The run sets `item_for` (an `ItemBuilder`)."""

    def __init__(self) -> None:
        self.sim = SimulatedReviewer()
        self.model_id, self.prompt_hash = self.sim.model_id, self.sim.prompt_hash
        self.item_for: Callable[[ReviewRequest], ReviewItem] | None = None
        self.items: list[ReviewItem] = []

    def review(self, request: ReviewRequest) -> dict:
        if self.item_for is None:
            raise RuntimeError("the run has not bound an ItemBuilder to this reviewer")
        self.items.append(self.item_for(request))
        return self.sim.review(request)
