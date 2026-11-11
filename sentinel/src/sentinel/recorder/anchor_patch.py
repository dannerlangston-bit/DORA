"""Anchor patches and re-anchor mode (12.1 Anchor patch, 11.5 Flow fields and Recorder, 5.5, 12.2
`locator_failure`, 5.16 re-anchor mode, 11.9 Phase 3).

A successful agent fallback records the final click target and the 40x20 crop around it on its `locator_failure`
finding (`executor.locator`: evidence `final_target`, `crop_sha256`, `artifacts`). Phase 3 turns that into an
anchor-patch pull request to the flow owner, accepted in re-anchor mode, which shows each broken anchor beside the
agent's final click target.

- `anchor_patch(finding, flow, registry, crop_png=|artifacts=)` builds the `AnchorPatch` of one rescued step: the new
  template (the crop) and the new offset on the control the step targets, as the changed surface file, its unified
  diff and the template bytes; None when the step was not rescued (nothing to propose: re-record it).
- `AnchorPatch.apply(root)` writes the patch into a sentinel-config checkout (the owner's acceptance).
- `reanchor_items(findings, flows, registry, ...)` lists the re-anchor view: every broken anchor (the run's
  `locator_failure` findings, and the anchor lint's misses) with its proposed patch, or `rerecord` when there is none.

Choices where the spec is ambiguous or silent (reported):

1. Where the patch lands: anchors are Surface Registry controls (4.5; 4.7 flows hold `<surface>.<control>` targets
   and "never a pixel threshold"), so the patch edits the control the step's target names: `template` becomes the
   crop, stored beside the old template as `<old stem>.<first 8 hex of the crop's sha256>.png`, and `click_offset`
   becomes `{dx: 0, dy: 0}` because the crop is centred on the final target. Exactly one control of one surface file
   changes; every flow step that targets that control follows it, which is what a repaired anchor means.
2. The surface file is edited in place on the control's own line when the control is a one-line flow mapping (the
   rest of the file, comments included, is kept); a block-style control makes the whole file re-dumped.
3. A rescue whose crop is clipped below 16 px on a side (a target at the window edge) cannot be a template (L-SRF-06):
   no patch, re-record. The crop bytes must hash to the finding's `crop_sha256`.
4. With the rescue's screenshot given, the patch says whether the crop is unique on it (the ladder's rung-2 rule);
   without it `unique` is None and the lint and the next run decide.
5. A finding's flow is the flow of its checkpoint id (`flow/<surface>/<flow id>/<step>`), else its evidence's
   `flow_id`.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import cv2
import numpy as np
import yaml

from ..diff.engine import match_template
from ..executor.locator import IMAGE_FLOOR, MIN_PEAK_RADIUS, SECOND_PEAK_MARGIN
from ..lint.anchors import AnchorLintReport, ArtifactReader
from ..lint.repo import Repo
from ..lint.rules import TARGET_FIELDS
from ..records import Finding
from ..taxonomy import TaxonomyError, checkpoint_parts

_SHA_SUFFIX = re.compile(r"\.[0-9a-f]{8}$")
MIN_SIDE = 16                                       # L-SRF-06: templates are 16x16 to 400x400


@dataclass(frozen=True)
class AnchorPatch:
    """One proposed anchor change (12.1): which step, which control in which surface file, the old and new template
    and offset, the final target it came from and the surface file before and after."""

    flow_id: str
    step_id: str
    target: str
    surface_id: str
    control_id: str
    surface_path: str
    old_template: str
    new_template: str
    old_offset: tuple[int, int]
    new_offset: tuple[int, int]
    final_target: tuple[int, int]
    crop_sha256: str
    crop_png: bytes
    old_text: str
    new_text: str
    unique: bool | None = None

    def diff(self) -> str:
        """The unified diff of the surface file (the YAML side of the pull request)."""
        return "".join(difflib.unified_diff(self.old_text.splitlines(keepends=True),
                                            self.new_text.splitlines(keepends=True),
                                            fromfile=f"a/{self.surface_path}", tofile=f"b/{self.surface_path}"))

    def files(self) -> dict[str, bytes]:
        """The files the pull request changes, by repository-relative path."""
        return {self.surface_path: self.new_text.encode("utf-8"), self.new_template: self.crop_png}

    def apply(self, root: str | Path) -> list[str]:
        """Accept the patch (re-anchor mode): write its files into the checkout at `root`; returns their paths."""
        base = Path(root)
        current = (base / self.surface_path).read_text(encoding="utf-8")
        if current != self.old_text:
            raise ValueError(f"{self.surface_path} changed since the patch was built; build it again")
        for rel, data in self.files().items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_bytes(data)
        return sorted(self.files())

    def summary(self) -> str:
        return (f"{self.flow_id} {self.step_id}: {self.target} template {self.old_template} -> {self.new_template}, "
                f"offset {self.old_offset} -> {self.new_offset} (agent's final target {self.final_target})")


def _repo(registry: Repo | str | Path) -> Repo:
    return registry if isinstance(registry, Repo) else Repo.load(registry)


def _flow_id(finding: Finding) -> str | None:
    """Choice 5."""
    if finding.checkpoint_id:
        try:
            parts = checkpoint_parts(finding.checkpoint_id)
            if parts["kind"] == "flow":
                return parts["qualifier"].split("/", 1)[0]
        except TaxonomyError:
            pass
    fid = finding.evidence.get("flow_id")
    return str(fid) if fid else None


def _step(flow: Mapping[str, Any], step_id: str) -> Mapping[str, Any] | None:
    return next((s for blk in ("steps", "teardown", "normalize") for s in flow.get(blk) or ()
                 if isinstance(s, Mapping) and str(s.get("id")) == step_id), None)


def _decode(png: bytes) -> np.ndarray | None:
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _new_template(old: str, sha: str) -> str:
    p = PurePosixPath(old)
    return str(p.with_name(f"{_SHA_SUFFIX.sub('', p.stem)}.{sha[:8]}.png"))


def _edit(text: str, control_id: str, template: str) -> tuple[str, tuple[int, int], str]:
    """The surface file with the control's template and offset replaced (choice 2), the old offset and template."""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)- (\{.*\})\s*(#.*)?$", ln.rstrip("\n"))
        if not m:
            continue
        try:
            c = yaml.safe_load(m.group(2))
        except yaml.YAMLError:
            continue
        if not isinstance(c, dict) or str(c.get("id")) != control_id:
            continue
        old_off, old_tpl = _offset(c), str(c.get("template", ""))
        new = _patched(c, template)
        body = yaml.safe_dump(new, default_flow_style=True, sort_keys=False, width=10_000).strip()
        tail = f"  {m.group(3)}" if m.group(3) else ""
        lines[i] = f"{m.group(1)}- {body}{tail}\n"
        return "".join(lines), old_off, old_tpl
    return _redump(yaml.safe_load(text), control_id, template)


def _redump(doc: Mapping[str, Any], control_id: str, template: str) -> tuple[str, tuple[int, int], str]:
    """A block-style control: the whole file re-dumped (choice 2)."""
    doc = dict(doc)
    doc["controls"] = list(doc.get("controls") or ())
    for i, c in enumerate(doc["controls"]):
        if isinstance(c, dict) and str(c.get("id")) == control_id:
            old_off, old_tpl = _offset(c), str(c.get("template", ""))
            doc["controls"][i] = _patched(c, template)
            return yaml.safe_dump(doc, sort_keys=False, width=120), old_off, old_tpl
    raise ValueError(f"control {control_id} is not in its surface file")


def _offset(c: Mapping[str, Any]) -> tuple[int, int]:
    off = c.get("click_offset") or {}
    return int(off.get("dx", 0)), int(off.get("dy", 0))


def _patched(c: Mapping[str, Any], template: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in c.items():
        if k == "click_offset":
            continue
        out[k] = template if k == "template" else v
        if k == "template":
            out["click_offset"] = {"dx": 0, "dy": 0}
    return out


def anchor_patch(finding: Finding, flow: Mapping[str, Any], registry: Repo | str | Path, *,
                 crop_png: bytes | None = None, artifacts: ArtifactReader | None = None,
                 screenshot: np.ndarray | None = None) -> AnchorPatch | None:
    """The anchor patch of one `locator_failure` finding (module docstring); None when the step was not rescued or
    its crop cannot be a template (choice 3). ValueError for another finding kind, a finding of another flow, a step
    or control that does not exist, or crop bytes that do not match the finding."""
    if finding.kind != "locator_failure":
        raise ValueError(f"an anchor patch is built from a locator_failure finding, not {finding.kind} (12.1)")
    fid = str(flow.get("flow_id"))
    of = _flow_id(finding)
    if of is not None and of != fid:
        raise ValueError(f"the finding is about flow {of}, not {fid}")
    ev = finding.evidence
    final, sha = ev.get("final_target"), ev.get("crop_sha256")
    if not isinstance(final, Mapping) or not sha:
        return None
    step_id = finding.step_id or str(ev.get("step_id") or "")
    step = _step(flow, step_id)
    if step is None:
        raise ValueError(f"step {step_id} is not in flow {fid}")
    target = str(ev.get("target") or next((step[k] for k in TARGET_FIELDS if isinstance(step.get(k), str)), ""))
    repo = _repo(registry)
    found = [(d, c) for d in repo.of("surface") if isinstance(d.data, dict)
             for c in d.data.get("controls") or () if isinstance(c, Mapping)
             and f"{d.data.get('id')}.{c.get('id')}" == target]
    if not found:
        raise ValueError(f"target {target} of step {step_id} is not a registry control")
    doc, control = found[0]
    data = crop_png if crop_png is not None else (artifacts.get(str(sha)) if artifacts is not None else None)
    if data is None:
        raise ValueError(f"the crop {sha} was not given and no artifact store holds it")
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("the crop bytes do not hash to the finding's crop_sha256")
    crop = _decode(data)
    if crop is None or min(crop.shape[:2]) < MIN_SIDE:
        return None
    unique = None
    if screenshot is not None:
        h, w = crop.shape[:2]
        best, second, _ = match_template(screenshot, crop, None, max(MIN_PEAK_RADIUS, min(w, h) // 2))
        unique = best >= IMAGE_FLOOR and second <= best - SECOND_PEAK_MARGIN
    old_tpl = str(control.get("template", ""))
    new_tpl = _new_template(old_tpl, str(sha))
    old_text = (repo.root / doc.rel).read_text(encoding="utf-8")
    new_text, old_off, _ = _edit(old_text, str(control["id"]), new_tpl)
    return AnchorPatch(fid, step_id, target, str(doc.data["id"]), str(control["id"]), doc.rel, old_tpl, new_tpl,
                       old_off, (0, 0), (int(final["x"]), int(final["y"])), str(sha), data, old_text, new_text, unique)


@dataclass(frozen=True)
class ReanchorItem:
    """One broken anchor in re-anchor mode (5.16): where, the broken template, the agent's final target and the
    proposed patch, and what the owner can do (`accept_or_rerecord`, or `rerecord` with no proposal)."""

    flow_id: str
    step_id: str
    target: str | None
    template: str | None
    final_target: tuple[int, int] | None
    patch: AnchorPatch | None
    source: str                                      # locator_failure | anchor_lint

    @property
    def action(self) -> str:
        return "accept_or_rerecord" if self.patch is not None else "rerecord"


def reanchor_items(findings: Iterable[Finding], flows: Mapping[str, Mapping[str, Any]], registry: Repo | str | Path, *,
                   artifacts: ArtifactReader | None = None, crops: Mapping[str, bytes] | None = None,
                   lint: AnchorLintReport | None = None) -> list[ReanchorItem]:
    """The re-anchor view (module docstring): one item per `locator_failure` finding of a known flow, then one per
    anchor-lint miss on a flow step no finding covers."""
    repo = _repo(registry)
    crops = crops or {}
    out: list[ReanchorItem] = []
    for f in findings:
        fid = _flow_id(f)
        if f.kind != "locator_failure" or fid not in flows:
            continue
        sha = f.evidence.get("crop_sha256")
        patch = anchor_patch(f, flows[fid], repo, crop_png=crops.get(str(sha)) if sha else None,
                             artifacts=artifacts) if sha else None
        final = f.evidence.get("final_target")
        step_id = f.step_id or str(f.evidence.get("step_id") or "")
        out.append(ReanchorItem(fid, step_id, patch.target if patch else f.evidence.get("target"),
                                patch.old_template if patch else None,
                                (int(final["x"]), int(final["y"])) if isinstance(final, Mapping) else None, patch,
                                "locator_failure"))
    covered = {(i.flow_id, i.step_id) for i in out}
    for m in (lint.misses if lint is not None else ()):
        if m.flow_id and m.step_id and (m.flow_id, m.step_id) not in covered:
            out.append(ReanchorItem(m.flow_id, m.step_id, m.anchor, m.template, None, None, "anchor_lint"))
    return out


__all__ = ["AnchorPatch", "ReanchorItem", "anchor_patch", "reanchor_items"]
