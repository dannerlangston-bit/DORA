"""The Reviewer's eval set, metrics and gates; weekly sampling; the nightly drift switch (3.14, 3.15, 10.4, 4.16,
8.3 `reviewer_evals`, 9.11).

The labeled eval set is external data: `eval/<eval_set_version>/labels.jsonl` in `sentinel-config`, one JSON object
per line (10.4), its images content-addressed in a blob directory (`<pixel sha256>.png`, `canary.image_sha256`).

- Items: `EvalItem` (`parse_labels`, `load_labels`) with its source row of 3.14 (`mutant`, `aa`, `real_intended`,
  `wrong_manifest`, `real_collateral`), task, truth label, defect class, tier, money-cell flag, labeler ids, the
  `hard` tag and its adjudicator, and the Reviewer inputs (`inputs`, read by `review_item`). `composition` lists
  where the set departs from 3.14 (size, shares, tier-1 share).
- Split: `split_of` and `split`, 70 percent development and 30 percent held-out by a seeded hash of the item id;
  an item marked `split: development` (the weekly additions of 3.15) is always development. `routing_threshold`
  reads the development split only; the gates read the held-out split only.
- Mutants: `eval_mutant` applies one of the seven mutations of 3.14 with the canary recipes (`canary`), on the eval
  stream `EVAL_STREAM` of the catalog seed (never a canary mutant's stream), pairs it with an unrelated manifest
  entry and returns the item and its crops.
- Running: `review_item` builds the 3.12 `ReviewItem` of an item; `predict` runs a `ReviewerJob` (fake or live
  transport) over items and returns each answered verdict as a `Prediction`.
- Metrics and gates: `compute_metrics` (precision and recall per verdict, `matches_manifest` precision and recall at
  the routing threshold, tier-1 mutant recall, overall `does_not_match_manifest` recall, hallucination and
  rubber-stamp rates, ECE over 10 bins with the reliability table, defect-class accuracy, missed money-cell mutants),
  `gates` (the 3.14 table), `change_rule` (against the previous pin), `report_doc` (the 4.16 eval report),
  `report_problems` (what L-XREF-29 checks in a report), `summary_text` (the weekly reviewer report).
- Store: `record` writes one `reviewer_evals` row; `routing_threshold_in_force` reads the derived threshold back
  for the classifier (0.80 until a passing evaluation exists); `drift_check` disables the Reviewer after two failing nightly
  30-item subsets or a failing weekly full evaluation (`system_state.reviewer_disabled`), `reviewer_disabled` reads
  it (the `ReportContext.reviewer_disabled` value), `disabled_review` is the classifier input R while it holds, and
  `reenable` clears it on a passing eval report.
- Weekly: `week_rows` reads the week's verdicts and human decisions, `weekly_dev_sample` picks the items that join
  the development split (3.15), `override_rates` is the override rate per verdict.
- CLI: `main` is `sentinel eval`, `gate_main` is `sentinel eval-gate` (the CI gate of 10.4 with the canary gate).

Choices where the spec is ambiguous (reported):

1. The split is per item, by `sha256(seed:item_id)` below 0.70, so an item never moves between splits as the set
   grows weekly and the held-out split is never used to choose a prompt or threshold (3.14); the shares are 70/30 in
   expectation, not exactly.
2. 3.14 computes the routing threshold "on the held-out split" in its metric list and forbids using the held-out
   split to choose a threshold; the threshold is read off the development split's curve (the smallest confidence
   among `matches_manifest` answers at which precision reaches 0.95) and the gates are measured on the held-out split
   at that threshold. Without such a confidence (or without development answers) the 0.80 default of 3.11 applies
   and the metrics say `routing_threshold_derived: false`.
3. Recall and precision are on the verdict alone, except `matches_manifest` at the routing threshold. An item the
   Reviewer did not answer counts against recall, hallucination (an A/A item "not labeled `no_visible_difference`")
   and defect-class accuracy, and is left out of precision, rubber-stamp rate and ECE. ECE bins are [k/10, (k+1)/10)
   with 1.0 in the last bin, over answered `manifest_match` items, correct when the verdict equals the label.
4. "Overall precision" of the change rule is the `matches_manifest` precision at the routing threshold (the
   precision gate); a drop of exactly 1 or 2 points does not block. "Any money-cell mutant missed" is the gate row
   `money_cell_mutant_misses` with threshold 0 (also an eval report gate, so L-XREF-29 holds it). A gate whose metric
   is undefined (no item of its kind in the split) fails, written with value 0 for an at-least gate and 1 for an
   at-most gate, and is listed under `undefined`.
5. Composition (3.14 shares within 5 points, 300 to 500 items, tier 1 at least 40 percent) is reported, not
   enforced: the gates decide. Real items need two distinct labelers and a `hard` item its adjudicator (3.14).
   `real_collateral` items are `describe_collateral` items labeled `collateral` or `needs_human`; defect-class
   accuracy is over those with a labeled defect class (the closest change type of 3.13).
6. Gate failure mid-release: 3.14 disables the Reviewer after the nightly 30-item subset fails two nights running;
   10.4 also disables it when a weekly full evaluation fails, at once. Both are applied, over the `reviewer_evals`
   rows of the deployed tuple written since the last re-enable. The nightly subset is the first 30 held-out items by
   a seeded hash (fixed, so night-to-night drift compares the same items). The state is `system_state.
   reviewer_disabled` as JSON `{since, since_at, gate, reviewer_version_sha256}`, cleared to an empty value; `since`
   is the date of the evaluation that disabled it and `gate` its first failing gate (the 9.2 line). Only a passing
   eval report run after `since_at` re-enables it (3.14: "a PR carrying a passing eval report").
7. Weekly sampling: the week's verdicts are `reviews` rows of non-Nightly runs (canary mutants count in no metric
   but the canary pass rate, 10.3); a verdict a human overturned and a late verdict that disagrees with an earlier
   human decision are those whose latest non-revoked decision on the same run and checkpoint disagrees with it by the
   3.11 rule (`classify._agrees`: `intended` with a confident match, `confirmed_bug` with a confident no-match, `noise`
   with no visible difference), `overturned` when the decision came after the verdict, `late_disagrees` before. The
   5 percent sample is `ceil(0.05 n)` of the other `intended` verdicts, seeded by the week. The override rate per
   verdict is overturned over decided (verdicts a human decided on). The candidates are written for labeling; a
   labeled candidate joins `labels.jsonl` with `split: development`.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import yaml
from PIL import Image

from .. import canary
from ..change_control import ReviewerTuple, sha256_of, tuple_in_force
from ..classify import ROUTING_THRESHOLD, Review, _agrees, reviewer_input
from ..diff.engine import diff_pair
from ..orchestrator.alerts import AlertRoute, reviewer_unavailable
from ..store import Store
from .config import ReviewerConfig
from .images import crop_box
from .job import ReviewerJob
from .prompt import DependencyEdge, DiffMetrics, FlowContext, ManifestEntryRef, ReviewItem, StepCrops
from .schema import CHANGE_TYPES, MATCHING_VERDICTS, TASK_A, TASK_B, VERDICTS
from .transport import Transport

EVAL_SEED = canary.SEED                  # the catalog seed of 12.5 ...
EVAL_STREAM = 314                        # ... on its own spawn-key stream: canary mutants use a 1-tuple key
SOURCES = {"mutant": 0.40, "aa": 0.15, "real_intended": 0.25, "wrong_manifest": 0.10, "real_collateral": 0.10}
REAL_SOURCES = frozenset({"real_intended", "wrong_manifest", "real_collateral"})
SHARE_TOLERANCE = 0.05
SET_SIZE = (300, 500)
TIER1_MIN = 0.40
DEV_SHARE = 0.70
DEVELOPMENT, HELD_OUT = "development", "held_out"
ECE_BINS = 10
PRECISION_TARGET = 0.95
NIGHTLY_SUBSET = 30
DRIFT_NIGHTS = 2
WEEKLY_SHARE = 0.05
TIER1_DROP_MAX = 0.01
PRECISION_DROP_MAX = 0.02
DNM, NVD = "does_not_match_manifest", "no_visible_difference"
TASK_A_LABELS = tuple(v for v in VERDICTS if v != "not_applicable")
TASK_B_LABELS = ("collateral", "needs_human")
SOURCE_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mutant": (TASK_A, (DNM,)), "aa": (TASK_A, (NVD,)), "real_intended": (TASK_A, ("matches_manifest",)),
    "wrong_manifest": (TASK_A, (DNM,)), "real_collateral": (TASK_B, TASK_B_LABELS)}
KINDS = ("held_out", "nightly_subset", "weekly")
DISABLED_KEY = "reviewer_disabled"
DISABLED_CAUSE = "reviewer_disabled"     # the cause the run's unreviewed adapter uses (pipeline.run)
MUTATIONS = {                            # the seven mutations of 3.14 -> the defect class they make (3.13 change type)
    "shift": "shifted", "erase": "missing_element", "digit": "text_change", "clip": "clipped_text",
    "overlap": "overlap", "hue": "color_semantic_flip", "swap_headers": "text_change"}
PAIRED = frozenset({"overlap", "swap_headers"})            # need the neighbor's box
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))


class EvalSetError(ValueError):
    """A labels file or an item that breaks the 3.14 or 10.4 rules."""


# -- items ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalItem:
    """One labeled eval item (3.14, 10.4)."""

    item_id: str
    source: str
    task: str
    label: str
    tier: int
    labelers: tuple[str, ...]
    defect_class: str | None = None
    money_cell: bool = False
    hard: bool = False
    adjudicator: str | None = None
    split: str | None = None                 # "development": a weekly addition, never held out (3.15)
    checkpoint_id: str | None = None
    inputs: Mapping[str, Any] | None = None  # the Reviewer inputs (`review_item`)

    def __post_init__(self) -> None:
        where = self.item_id or "(no item_id)"
        if not self.item_id:
            raise EvalSetError("an item needs an item_id")
        if self.source not in SOURCES:
            raise EvalSetError(f"{where}: source {self.source!r} not in {sorted(SOURCES)}")
        task, labels = SOURCE_LABELS[self.source]
        if self.task != task or self.label not in labels:
            raise EvalSetError(f"{where}: a {self.source} item is a {task} item labeled one of {list(labels)}")
        if self.tier not in (1, 2, 3):
            raise EvalSetError(f"{where}: tier {self.tier!r}")
        if not self.labelers or not all(isinstance(x, str) and x for x in self.labelers):
            raise EvalSetError(f"{where}: labeler ids are required (10.4)")
        if self.source in REAL_SOURCES and len(set(self.labelers)) < 2:
            raise EvalSetError(f"{where}: a real item is labeled by two people independently (3.14)")
        if self.hard and not self.adjudicator:
            raise EvalSetError(f"{where}: a hard item names the third person who adjudicated it (3.14)")
        if self.defect_class is not None and self.defect_class not in CHANGE_TYPES:
            raise EvalSetError(f"{where}: defect class {self.defect_class!r} not in {list(CHANGE_TYPES)}")
        if self.defect_class is None and (self.source == "mutant" or self.label == "collateral"):
            raise EvalSetError(f"{where}: mutant and collateral items carry their defect class (3.14)")
        if self.money_cell and self.source != "mutant":
            raise EvalSetError(f"{where}: only a mutant item is a money-cell mutant")
        if self.split not in (None, DEVELOPMENT):
            raise EvalSetError(f"{where}: split is assigned by seed; only `development` may be forced (3.15)")

    def doc(self) -> dict[str, Any]:
        """The labels.jsonl object of this item (optional keys omitted)."""
        d: dict[str, Any] = {"item_id": self.item_id, "source": self.source, "task": self.task, "label": self.label,
                             "tier": self.tier, "labelers": list(self.labelers)}
        for k in ("defect_class", "adjudicator", "split", "checkpoint_id"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.money_cell:
            d["money_cell"] = True
        if self.hard:
            d["hard"] = True
        if self.inputs is not None:
            d["inputs"] = dict(self.inputs)
        return d


ITEM_KEYS = frozenset(f.name for f in dataclasses.fields(EvalItem))


def parse_item(d: Any, where: str = "item") -> EvalItem:
    """One labels.jsonl object as an `EvalItem`; raises `EvalSetError`."""
    if not isinstance(d, Mapping):
        raise EvalSetError(f"{where}: not a JSON object")
    unknown = set(d) - ITEM_KEYS
    missing = {"item_id", "source", "task", "label", "tier", "labelers"} - set(d)
    if unknown or missing:
        raise EvalSetError(f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
    try:
        return EvalItem(**{**d, "labelers": tuple(d["labelers"]), "tier": int(d["tier"]),
                           "money_cell": bool(d.get("money_cell", False)), "hard": bool(d.get("hard", False))})
    except EvalSetError as exc:
        raise EvalSetError(f"{where}: {exc}") from None
    except (TypeError, ValueError) as exc:
        raise EvalSetError(f"{where}: {exc}") from None


def parse_labels(text: str) -> list[EvalItem]:
    """Every item of a labels.jsonl text, ids unique; raises `EvalSetError`."""
    items: list[EvalItem] = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise EvalSetError(f"line {n}: not JSON: {exc}") from None
            items.append(parse_item(raw, f"line {n}"))
    ids = [i.item_id for i in items]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise EvalSetError(f"duplicate item ids {dupes}")
    return items


def load_labels(path: str | Path) -> list[EvalItem]:
    return parse_labels(Path(path).read_text(encoding="utf-8"))


def composition(items: Sequence[EvalItem]) -> list[str]:
    """Where the set departs from 3.14: size, source shares (within 5 points), tier-1 share (choice 5)."""
    n = len(items)
    if n == 0:
        return ["the eval set is empty"]
    out = []
    if not SET_SIZE[0] <= n <= SET_SIZE[1]:
        out.append(f"{n} items; 3.14 targets {SET_SIZE[0]} to {SET_SIZE[1]}")
    for src, share in SOURCES.items():
        got = sum(i.source == src for i in items) / n
        if abs(got - share) > SHARE_TOLERANCE + 1e-9:
            out.append(f"source {src}: {got:.0%} of the set; 3.14 asks {share:.0%}")
    t1 = sum(i.tier == 1 for i in items) / n
    if t1 < TIER1_MIN:
        out.append(f"tier-1 items are {t1:.0%} of the set; 3.14 asks at least {TIER1_MIN:.0%}")
    return out


# -- split ---------------------------------------------------------------------------------------------------------

def _unit(seed: int, *parts: str) -> float:
    """A number in [0, 1) from a seeded sha256."""
    return int(hashlib.sha256(":".join((str(seed), *parts)).encode()).hexdigest()[:16], 16) / 16 ** 16


def split_of(item: EvalItem, seed: int = EVAL_SEED) -> str:
    """`development` or `held_out` for one item (choice 1)."""
    if item.split == DEVELOPMENT:
        return DEVELOPMENT
    return DEVELOPMENT if _unit(seed, item.item_id) < DEV_SHARE else HELD_OUT


@dataclass(frozen=True)
class Split:
    development: tuple[EvalItem, ...]
    held_out: tuple[EvalItem, ...]


def split(items: Iterable[EvalItem], seed: int = EVAL_SEED) -> Split:
    """The 70/30 split of 3.14 (choice 1), each side in item order."""
    dev, held = [], []
    for i in items:
        (dev if split_of(i, seed) == DEVELOPMENT else held).append(i)
    return Split(tuple(dev), tuple(held))


def nightly_subset(held_out: Sequence[EvalItem], seed: int = EVAL_SEED, n: int = NIGHTLY_SUBSET) -> list[EvalItem]:
    """The fixed nightly subset of the held-out split (3.14, 10.4; choice 6)."""
    return sorted(held_out, key=lambda i: _unit(seed, "nightly", i.item_id))[:n]


# -- blobs and the Reviewer inputs ---------------------------------------------------------------------------------

def write_blob(blobs: str | Path, image: np.ndarray) -> str:
    """Store an RGB image as `<pixel sha256>.png` (content-addressed, 10.4); returns its sha256."""
    sha = canary.image_sha256(image)
    path = Path(blobs) / f"{sha}.png"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.ascontiguousarray(image), "RGB").save(path, format="PNG")
    return sha


def read_blob(blobs: str | Path, sha: str) -> np.ndarray:
    """The RGB image stored under `sha`, checked against its name."""
    img = np.asarray(Image.open(Path(blobs) / f"{sha}.png").convert("RGB"))
    if canary.image_sha256(img) != sha:
        raise EvalSetError(f"blob {sha} does not hash to its name")
    return img


def entry_doc(e: ManifestEntryRef) -> dict[str, Any]:
    return {"entry_id": e.entry_id, "tickets": list(e.tickets), "summary": e.summary,
            "declared_surfaces": list(e.declared_surfaces), "expected_visual": e.expected_visual,
            "feature_flags": list(e.feature_flags)}


def _entry(d: Mapping[str, Any]) -> ManifestEntryRef:
    return ManifestEntryRef(d["entry_id"], tuple(d["tickets"]), d["summary"], tuple(d["declared_surfaces"]),
                            d["expected_visual"], tuple(d.get("feature_flags", ())))


def review_item(item: EvalItem, blobs: str | Path) -> ReviewItem:
    """The 3.12 Reviewer item of an eval item, from its `inputs` and the blob directory. `inputs` carries `region`,
    `workspace_id`, `data_mode`, `data_epoch`, `window_title`, `crops` ({base, candidate, heatmap} sha256 of the 1:1
    crops), `diff` ({changed_px, components, translation, color_flags} in crop coordinates), `entry` (task A) or
    `edge` (task B), and optionally `small_text`, `ocr_text` and `flow`."""
    if item.inputs is None or item.checkpoint_id is None:
        raise EvalSetError(f"{item.item_id}: no Reviewer inputs or checkpoint id")
    x = item.inputs
    crops = {k: read_blob(blobs, x["crops"][k]) for k in ("base", "candidate", "heatmap")}
    d = x["diff"]
    flow = x.get("flow") or {}
    return ReviewItem(
        task=item.task, queue_key=item.item_id, checkpoint_ids=(item.checkpoint_id,), region=x["region"],
        tier=item.tier, data_mode=x["data_mode"], data_epoch=x["data_epoch"], workspace_id=x["workspace_id"],
        window_title=x.get("window_title", ""),
        diff=DiffMetrics(int(d["changed_px"]), tuple(tuple(b) for b in d.get("components", ())),
                         tuple(d.get("translation", (0, 0))), tuple(d.get("color_flags", ()))),
        flow=FlowContext(tuple(flow.get("last_actions", ())), flow.get("expected_screen", ""),
                         tuple(flow.get("must_see", ())), tuple(flow.get("masks_and_rules", ()))),
        steps=(StepCrops(None, crops["base"], crops["candidate"], crops["heatmap"], bool(x.get("small_text"))),),
        entry=_entry(x["entry"]) if x.get("entry") else None,
        edge=DependencyEdge(x["edge"]["primitive"], x["edge"]["entry_id"]) if x.get("edge") else None,
        ocr_text=tuple(x.get("ocr_text", ())), base_hash=x["crops"]["base"], candidate_hash=x["crops"]["candidate"])


# -- eval mutants (3.14 row 1) -------------------------------------------------------------------------------------

def mutant_rng(index: int, seed: int = EVAL_SEED) -> np.random.Generator:
    """The rng of eval mutant `index`: the seed on the eval stream, never a canary mutant's (`canary.mutant_rng`)."""
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(EVAL_STREAM, index)))


def mutate(image: np.ndarray, op: str, box: canary.Box, rng: np.random.Generator, *,
           neighbor: canary.Box | None = None) -> np.ndarray:
    """One mutation of 3.14 with the canary recipes: `shift` 2 to 6 px, `erase` with sampled background, `digit` by
    glyph copy from the same row, `clip` (crop or ellipsis), `overlap` of the box onto `neighbor`, `hue` (red and green
    swapped), `swap_headers` of the box and `neighbor`."""
    if op not in MUTATIONS:
        raise canary.GenerationError(f"eval mutation {op!r} not in {sorted(MUTATIONS)}")
    if op in PAIRED and neighbor is None:
        raise canary.GenerationError(f"{op} needs the neighbor's box")
    if op == "shift":
        ux, uy = DIRECTIONS[int(rng.integers(len(DIRECTIONS)))]
        mag = int(rng.integers(2, 7))
        return canary.shift_region(image, box, rng, dx=ux * mag, dy=uy * mag)
    if op == "erase":
        return canary.erase(image, box, rng)
    if op == "digit":
        return canary.replace_glyph(image, box, rng)
    if op == "clip":
        return canary.clip_label(image, box, rng, style=("crop", "ellipsis")[int(rng.integers(2))])
    if op == "hue":
        return canary.channel_swap(image, box, rng)
    assert neighbor is not None
    if op == "overlap":
        return canary.overlap_panels(image, neighbor, rng, layer_box=box)
    one = canary.transplant(image, box, rng, source_box=neighbor)
    other = canary.transplant(image, neighbor, rng, source_box=box)
    x, y, w, h = neighbor
    one[y:y + h, x:x + w] = other[y:y + h, x:x + w]
    return one


def unrelated_entry(entries: Sequence[ManifestEntryRef], surface_id: str, rng: np.random.Generator) -> ManifestEntryRef:
    """A plausible manifest entry that does not declare the mutant's surface (3.14 row 1)."""
    pool = [e for e in entries if surface_id not in e.declared_surfaces]
    if not pool:
        raise canary.GenerationError(f"no manifest entry unrelated to {surface_id}")
    return pool[int(rng.integers(len(pool)))]


@dataclass(frozen=True, eq=False)
class EvalMutant:
    """A generated eval mutant: its item (with inputs) and the crops to store under their sha256."""

    item: EvalItem
    frame: np.ndarray
    crops: Mapping[str, np.ndarray]


def eval_mutant(base: np.ndarray, *, op: str, box: canary.Box, index: int, checkpoint_id: str, surface_id: str,
                region: str, tier: int, entries: Sequence[ManifestEntryRef], workspace_id: str, data_epoch: str,
                neighbor: canary.Box | None = None, money_cell: bool = False, data_mode: str = "frozen",
                window_title: str = "", pad: int | None = None, seed: int = EVAL_SEED) -> EvalMutant:
    """Eval mutant `index` of an approved capture: the mutation (`mutate`), the unrelated entry, the 1:1 crops around
    the changed components (padded by the crop policy's pad, 3.12) and the diff metrics in crop coordinates."""
    rng = mutant_rng(index, seed)
    frame = mutate(base, op, box, rng, neighbor=neighbor)
    entry = unrelated_entry(entries, surface_id, rng)
    res = diff_pair(base, frame)
    if not res.components:
        raise canary.GenerationError(f"{op} on {checkpoint_id} made no reportable change")
    x0 = min(c.bbox[0] for c in res.components)
    y0 = min(c.bbox[1] for c in res.components)
    x1 = max(c.bbox[0] + c.bbox[2] for c in res.components)
    y1 = max(c.bbox[1] + c.bbox[3] for c in res.components)
    h, w = base.shape[:2]
    cx, cy, cw, ch = crop_box((x0, y0, x1 - x0, y1 - y0), (0, 0, w, h), ReviewerConfig().crop.pad_px
                              if pad is None else pad)
    crops = {"base": base[cy:cy + ch, cx:cx + cw].copy(), "candidate": frame[cy:cy + ch, cx:cx + cw].copy(),
             "heatmap": res.heatmap[cy:cy + ch, cx:cx + cw].copy()}
    shas = {k: canary.image_sha256(v) for k, v in crops.items()}
    inputs = {"region": region, "workspace_id": workspace_id, "data_mode": data_mode, "data_epoch": data_epoch,
              "window_title": window_title, "crops": shas, "entry": entry_doc(entry),
              "diff": {"changed_px": sum(c.area for c in res.components),
                       "components": [[c.bbox[0] - cx, c.bbox[1] - cy, c.bbox[2], c.bbox[3]] for c in res.components],
                       "translation": [0, 0],
                       "color_flags": ["color_semantic_flip"] if any(c.color_semantic_flip for c in res.components)
                       else []},
              "mutation": {"op": op, "seed": seed, "stream": EVAL_STREAM, "index": index}}
    item = EvalItem(f"ev-mut-{index:04d}", "mutant", TASK_A, DNM, tier, ("generator",), defect_class=MUTATIONS[op],
                    money_cell=money_cell, checkpoint_id=checkpoint_id, inputs=inputs)
    return EvalMutant(item, frame, crops)


# -- predictions ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prediction:
    """An answered verdict: verdict, confidence and the closest defect class (3.13)."""

    verdict: str
    confidence: float
    defect_class: str | None = None


def predict(job: ReviewerJob, items: Sequence[EvalItem], blobs: str | Path) -> dict[str, Prediction | None]:
    """Run the job over `items` (one review call; the job's run kind and caps apply) and return each answered
    verdict as a `Prediction`, None for an item that got no answer."""
    result = job.review([review_item(i, blobs) for i in items])
    out: dict[str, Prediction | None] = {}
    for i in items:
        v = result.verdicts[i.item_id]
        out[i.item_id] = None if v.answer is None else Prediction(v.answer["verdict"], float(v.answer["confidence"]),
                                                                   v.defect_class)
    return out


def read_predictions(path: str | Path) -> dict[str, Prediction]:
    """Recorded verdicts, one JSON object per line: `item_id`, `verdict`, `confidence`, optional `defect_class`."""
    out: dict[str, Prediction] = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            d = json.loads(line)
            if d["verdict"] not in VERDICTS or not 0.0 <= float(d["confidence"]) <= 1.0:
                raise EvalSetError(f"{path} line {n}: verdict or confidence out of the 3.13 range")
            out[str(d["item_id"])] = Prediction(d["verdict"], float(d["confidence"]), d.get("defect_class"))
    return out


# -- metrics -------------------------------------------------------------------------------------------------------

def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


@dataclass(frozen=True)
class Metrics:
    """The 3.14 metrics of one split at one routing threshold."""

    items: int
    threshold: float
    threshold_derived: bool
    per_verdict: Mapping[str, Mapping[str, Any]]
    matches_precision: float | None
    matches_recall: float | None
    tier1_mutant_recall: float | None
    dnm_recall: float | None
    hallucination_rate: float | None
    rubber_stamp_rate: float | None
    ece: float | None
    reliability: tuple[Mapping[str, Any], ...]
    defect_class_accuracy: float | None
    money_cell_misses: tuple[str, ...]
    unanswered: int

    def value(self, gate: str) -> float | None:
        """The metric a gate of `GATES` reads."""
        return {"matches_manifest_precision": self.matches_precision, "matches_manifest_recall": self.matches_recall,
                "tier1_mutant_recall": self.tier1_mutant_recall, "does_not_match_manifest_recall": self.dnm_recall,
                "hallucination_rate": self.hallucination_rate, "rubber_stamp_rate": self.rubber_stamp_rate,
                "expected_calibration_error": self.ece,
                "money_cell_mutant_misses": float(len(self.money_cell_misses))}[gate]

    def doc(self) -> dict[str, Any]:
        """The metrics as JSON (`reviewer_evals.metrics_json`, the weekly report)."""
        return {"items": self.items, "routing_threshold": self.threshold,
                "routing_threshold_derived": self.threshold_derived,
                "per_verdict": {k: dict(v) for k, v in self.per_verdict.items()},
                "matches_manifest_at_threshold": {"precision": self.matches_precision, "recall": self.matches_recall},
                "tier1_mutant_recall": self.tier1_mutant_recall, "does_not_match_manifest_recall": self.dnm_recall,
                "hallucination_rate": self.hallucination_rate, "rubber_stamp_rate": self.rubber_stamp_rate,
                "expected_calibration_error": self.ece, "reliability": [dict(b) for b in self.reliability],
                "defect_class_accuracy": self.defect_class_accuracy, "money_cell_misses": list(self.money_cell_misses),
                "unanswered": self.unanswered,
                "undefined": [g.name for g in GATES if self.value(g.name) is None]}


def reliability(items: Sequence[EvalItem], preds: Mapping[str, Prediction | None],
                bins: int = ECE_BINS) -> tuple[float | None, tuple[dict[str, Any], ...]]:
    """ECE over answered `manifest_match` items and the reliability table (choice 3): per bin its bounds, count,
    mean confidence and accuracy."""
    rows: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for i in items:
        p = preds.get(i.item_id)
        if i.task == TASK_A and p is not None:
            rows[min(int(p.confidence * bins), bins - 1)].append((p.confidence, p.verdict == i.label))
    n = sum(len(r) for r in rows)
    table, ece = [], 0.0
    for k, r in enumerate(rows):
        conf = sum(c for c, _ in r) / len(r) if r else None
        acc = sum(ok for _, ok in r) / len(r) if r else None
        table.append({"bin": [k / bins, (k + 1) / bins], "count": len(r), "confidence": conf, "accuracy": acc})
        if r:
            ece += len(r) / n * abs(acc - conf)
    return (ece if n else None), tuple(table)


def compute_metrics(items: Sequence[EvalItem], preds: Mapping[str, Prediction | None], threshold: float, *,
                    threshold_derived: bool = True) -> Metrics:
    """The 3.14 metrics of `items` (choices 3 and 4)."""
    a = [i for i in items if i.task == TASK_A]

    def says(i: EvalItem, *verdicts: str) -> bool:
        p = preds.get(i.item_id)
        return p is not None and p.verdict in verdicts

    def confident(i: EvalItem) -> bool:
        p = preds.get(i.item_id)
        return p is not None and p.verdict == "matches_manifest" and p.confidence >= threshold

    def right_class(i: EvalItem) -> bool:
        p = preds.get(i.item_id)
        return p is not None and p.defect_class == i.defect_class

    per = {}
    for v in TASK_A_LABELS:
        support = sum(i.label == v for i in a)
        predicted = sum(says(i, v) for i in a)
        tp = sum(i.label == v and says(i, v) for i in a)
        per[v] = {"support": support, "predicted": predicted, "precision": _ratio(tp, predicted),
                  "recall": _ratio(tp, support)}
    hi = [i for i in a if confident(i)]
    hi_ok = sum(i.label == "matches_manifest" for i in hi)
    dnm = [i for i in a if i.label == DNM]
    t1 = [i for i in a if i.source == "mutant" and i.tier == 1]
    aa = [i for i in a if i.source == "aa"]
    wm = [i for i in a if i.source == "wrong_manifest"]
    col = [i for i in items if i.source == "real_collateral" and i.defect_class is not None]
    ece, table = reliability(items, preds)
    return Metrics(
        items=len(items), threshold=threshold, threshold_derived=threshold_derived, per_verdict=per,
        matches_precision=_ratio(hi_ok, len(hi)), matches_recall=_ratio(hi_ok, per["matches_manifest"]["support"]),
        tier1_mutant_recall=_ratio(sum(says(i, DNM) for i in t1), len(t1)),
        dnm_recall=_ratio(sum(says(i, DNM) for i in dnm), len(dnm)),
        hallucination_rate=_ratio(sum(not says(i, NVD) for i in aa), len(aa)),
        rubber_stamp_rate=_ratio(sum(says(i, *MATCHING_VERDICTS) for i in wm), len(wm)),
        ece=ece, reliability=table, defect_class_accuracy=_ratio(sum(right_class(i) for i in col), len(col)),
        money_cell_misses=tuple(i.item_id for i in a if i.source == "mutant" and i.money_cell and not says(i, DNM)),
        unanswered=sum(preds.get(i.item_id) is None for i in items))


def routing_threshold(development: Sequence[EvalItem], preds: Mapping[str, Prediction | None],
                      target: float = PRECISION_TARGET) -> float | None:
    """The smallest confidence among the development split's `matches_manifest` answers at which
    `matches_manifest` precision reaches `target` (3.14, choice 2); None when none does."""
    answers = []
    for i in development:
        p = preds.get(i.item_id)
        if i.task == TASK_A and p is not None and p.verdict == "matches_manifest":
            answers.append((p.confidence, i.label == "matches_manifest"))
    for t in sorted({c for c, _ in answers}):
        kept = [ok for c, ok in answers if c >= t]
        if sum(kept) / len(kept) >= target:
            return t
    return None


# -- gates ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    """One row of the 3.14 gate table."""

    name: str
    op: str                                  # ">=" or "<="
    threshold: float

    def passes(self, value: float) -> bool:
        return value >= self.threshold - 1e-12 if self.op == ">=" else value <= self.threshold + 1e-12


GATES = (Gate("matches_manifest_precision", ">=", 0.95), Gate("matches_manifest_recall", ">=", 0.80),
         Gate("tier1_mutant_recall", ">=", 0.98), Gate("does_not_match_manifest_recall", ">=", 0.95),
         Gate("hallucination_rate", "<=", 0.02), Gate("rubber_stamp_rate", "<=", 0.03),
         Gate("expected_calibration_error", "<=", 0.08), Gate("money_cell_mutant_misses", "<=", 0.0))
GATE_BY_NAME = {g.name: g for g in GATES}


@dataclass(frozen=True)
class GateResult:
    gate: str
    value: float
    threshold: float
    passed: bool
    defined: bool = True

    def doc(self) -> dict[str, Any]:
        """The 4.16 `gates` entry."""
        return {"gate": self.gate, "value": self.value, "threshold": self.threshold, "passed": self.passed}


def gates(metrics: Metrics) -> list[GateResult]:
    """Every gate of 3.14 over `metrics` (an undefined metric fails, choice 4)."""
    out = []
    for g in GATES:
        v = metrics.value(g.name)
        if v is None:
            out.append(GateResult(g.name, 0.0 if g.op == ">=" else 1.0, g.threshold, False, False))
        else:
            out.append(GateResult(g.name, round(float(v), 6), g.threshold, g.passes(float(v))))
    return out


def _values(gs: Iterable[Mapping[str, Any] | GateResult]) -> dict[str, float]:
    return {(g.gate if isinstance(g, GateResult) else g["gate"]): float(g.value if isinstance(g, GateResult)
                                                                         else g["value"]) for g in gs}


def change_rule(new: Sequence[GateResult | Mapping[str, Any]],
                previous: Sequence[GateResult | Mapping[str, Any]] | None = None) -> list[str]:
    """Why the 3.14 change rule blocks the new tuple (empty: it does not): a gate that fails (no gate may fall, a
    missed money-cell mutant included), a tier-1 recall drop of more than 1 point or a precision drop of more than
    2 points against the previous pin, even while the gates hold (choice 4)."""
    out = []
    for g in new:
        name, value, passed = (g.gate, g.value, g.passed) if isinstance(g, GateResult) else \
            (g["gate"], float(g["value"]), bool(g["passed"]))
        if not passed:
            gate = GATE_BY_NAME[name]
            out.append(f"gate {name} fails: {value:g} (needs {gate.op} {gate.threshold:g})")
    if previous is not None:
        now, then = _values(new), _values(previous)
        for name, limit in (("tier1_mutant_recall", TIER1_DROP_MAX), ("matches_manifest_precision", PRECISION_DROP_MAX)):
            drop = then.get(name, 0.0) - now.get(name, 0.0)
            if drop > limit + 1e-9:
                out.append(f"{name} drops {100 * drop:.1f} points ({then[name]:g} -> {now[name]:g}); the change rule "
                           f"allows {100 * limit:.0f}")
    return out


# -- eval reports (4.16) -------------------------------------------------------------------------------------------

def report_doc(t: ReviewerTuple, results: Sequence[GateResult], *, eval_set_version: str, run_at: str,
               canary_run_id: str | None = None) -> dict[str, Any]:
    """The eval report of 4.16 for one tuple on the held-out split."""
    doc: dict[str, Any] = {"schema_version": 1, "reviewer_version": t.doc(), "reviewer_version_sha256": t.sha256,
                           "eval_set_version": eval_set_version, "split": HELD_OUT, "run_at": run_at,
                           "gates": [g.doc() for g in results]}
    if canary_run_id:
        doc["canary_run_id"] = canary_run_id
    return doc


def report_path(t: ReviewerTuple) -> str:
    """`eval/reports/<hex>.yaml` (4.1, 4.16)."""
    return f"eval/reports/{t.hex}.yaml"


def report_problems(doc: Any, expected: ReviewerTuple | None = None, rel: str | None = None) -> list[str]:
    """What L-XREF-29 finds in one eval report beyond its schema: the hash over `reviewer_version`, the file name,
    the tuple in force (`expected`), and every gate of 3.14 present once at its value and passing."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("reviewer_version"), Mapping):
        return ["not an eval report (4.16)"]
    out = []
    stated = doc.get("reviewer_version_sha256")
    actual = sha256_of(dict(doc["reviewer_version"]))
    if stated != actual:
        out.append(f"reviewer_version_sha256 {stated} is not the hash of its reviewer_version ({actual})")
    if rel is not None and Path(rel).stem != str(stated).removeprefix("sha256:"):
        out.append(f"the file name is not the tuple hash {stated} without its prefix (4.16)")
    if expected is not None and actual != expected.sha256:
        try:
            diff = ", ".join(expected.differences(ReviewerTuple.from_doc(doc["reviewer_version"])))
        except (KeyError, TypeError, ValueError):
            diff = "its reviewer_version"
        out.append(f"the report is for another reviewer version tuple (differs in {diff})")
    if doc.get("split") != HELD_OUT:
        out.append("the report is not on the held-out split (3.14)")
    seen: dict[str, Mapping[str, Any]] = {}
    for g in doc.get("gates") or ():
        if not isinstance(g, Mapping) or g.get("gate") not in GATE_BY_NAME:
            out.append(f"gate {g.get('gate') if isinstance(g, Mapping) else g!r} is not a 3.14 gate")
        elif g["gate"] in seen:
            out.append(f"gate {g['gate']} appears twice")
        else:
            seen[g["gate"]] = g
    for gate in GATES:
        g = seen.get(gate.name)
        if g is None:
            out.append(f"gate {gate.name} is missing (every gate of 3.14)")
            continue
        try:
            value, threshold = float(g["value"]), float(g["threshold"])
        except (KeyError, TypeError, ValueError):
            out.append(f"gate {gate.name} has no numeric value and threshold")
            continue
        if abs(threshold - gate.threshold) > 1e-9:
            out.append(f"gate {gate.name} threshold {threshold:g} is not the 3.14 value {gate.threshold:g}")
        ok = gate.passes(value)
        if bool(g.get("passed")) != ok:
            out.append(f"gate {gate.name} says passed: {g.get('passed')} but {value:g} {gate.op} {gate.threshold:g} "
                       f"is {str(ok).lower()}")
        elif not ok:
            out.append(f"gate {gate.name} fails: {value:g} (needs {gate.op} {gate.threshold:g})")
    return out


def report_passes(doc: Any) -> bool:
    """Every 3.14 gate of the report passes and its hash is consistent."""
    return not report_problems(doc)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def summary_text(t: ReviewerTuple, metrics: Metrics, results: Sequence[GateResult], *, eval_set_version: str,
                 split_name: str, composition_notes: Sequence[str] = (), blocks: Sequence[str] = ()) -> str:
    """The weekly reviewer report (3.14) as plain text: tuple, gates, metrics per verdict, the reliability diagram
    as a table, composition notes and what blocks the change."""
    lines = [f"Reviewer eval: {t.model_id}, prompt {t.prompt_sha256[:19]}, tuple {t.sha256[:19]}",
             f"eval set {eval_set_version}, {split_name} split, {metrics.items} items, {metrics.unanswered} unanswered, "
             f"routing threshold {metrics.threshold:g}" + ("" if metrics.threshold_derived else " (default 0.80)"),
             "", "Gates (3.14):"]
    lines += [f"  {'PASS' if g.passed else 'FAIL'} {g.gate}: {g.value:g} (threshold {g.threshold:g})"
              + ("" if g.defined else ", undefined") for g in results]
    lines += ["", "Precision and recall per verdict:"]
    for v, m in metrics.per_verdict.items():
        lines.append(f"  {v}: precision {_fmt(m['precision'])}, recall {_fmt(m['recall'])} ({m['support']} labeled, "
                     f"{m['predicted']} answered)")
    lines += [f"Defect-class accuracy on collateral items: {_fmt(metrics.defect_class_accuracy)}",
              "", "Reliability diagram (confidence bin: count, mean confidence, accuracy):"]
    for b in metrics.reliability:
        if b["count"]:
            lines.append(f"  [{b['bin'][0]:.1f}, {b['bin'][1]:.1f}): {b['count']}, {b['confidence']:.3f}, "
                         f"{b['accuracy']:.3f}")
    if composition_notes:
        lines += ["", "Eval set composition (3.14):"] + [f"  {n}" for n in composition_notes]
    if blocks:
        lines += ["", "Blocked by the change rule:"] + [f"  {b}" for b in blocks]
    return "\n".join(lines) + "\n"


# -- store: reviewer_evals rows and the drift switch (3.14, 10.4) ---------------------------------------------------

def record(store: Store, t: ReviewerTuple, metrics: Metrics, results: Sequence[GateResult], *,
           eval_set_version: str, kind: str, run_at: str | None = None) -> int:
    """One `reviewer_evals` row (8.3) for an evaluation of `t`: `kind` is `held_out` (a pre-merge evaluation),
    `nightly_subset` or `weekly` (the deployed tuple)."""
    if kind not in KINDS:
        raise ValueError(f"eval kind {kind!r} not in {KINDS}")
    doc = {**metrics.doc(), "kind": kind, "reviewer_version_sha256": t.sha256, "gates": [g.doc() for g in results]}
    return store.write_reviewer_eval(eval_set_version=eval_set_version, model_id=t.model_id,
                                     prompt_hash=t.prompt_sha256, items=metrics.items, metrics=doc,
                                     passed_gate=all(g.passed for g in results), run_at=run_at)


def routing_threshold_in_force(store: Store, reviewer_version_sha256: str | None) -> float:
    """The `match_hi` routing threshold for the tuple in force (3.11 input R, 3.14): the threshold of its latest
    passing held-out or weekly evaluation, else 0.80 ("0.80 is used until the first eval report exists")."""
    if reviewer_version_sha256 is None:
        return ROUTING_THRESHOLD
    for r in store.query("SELECT metrics_json FROM reviewer_evals WHERE passed_gate = 1 ORDER BY run_at DESC, "
                         "eval_id DESC"):
        m = json.loads(r["metrics_json"])
        if m.get("reviewer_version_sha256") == reviewer_version_sha256 and m.get("kind") in ("held_out", "weekly"):
            return float(m["routing_threshold"])
    return ROUTING_THRESHOLD


def _ts(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def reviewer_disabled(store: Store) -> dict[str, Any] | None:
    """The disabled state (`{since, since_at, gate, reviewer_version_sha256}`, choice 6), None while enabled. Its
    `since` and `gate` are what `report.model.ReportContext.reviewer_disabled` prints."""
    value = store.get_system_state(DISABLED_KEY)
    return json.loads(value) if value else None


def disabled_review(store: Store) -> Review | None:
    """The classifier input R of every row 23 item while the Reviewer is disabled (`needs_human (reason:
    reviewer_unavailable)`, 3.14, 10.4); None while it is enabled."""
    return Review("unavailable", cause=DISABLED_CAUSE) if reviewer_disabled(store) else None


@dataclass(frozen=True)
class DriftResult:
    """What `drift_check` found: whether the Reviewer is disabled, whether this check disabled it, and the alert."""

    disabled: bool
    newly: bool = False
    since: str | None = None
    gate: str | None = None
    alert: AlertRoute | None = None


def drift_check(store: Store, reviewer_version_sha256: str, *, date: str, at: str | None = None) -> DriftResult:
    """After a nightly subset or weekly evaluation of the deployed tuple was recorded: disable the Reviewer when the
    last two nightly subsets both failed or the last weekly evaluation failed, counting rows written since the last
    re-enable (choice 6). `date` is the night's date (the banner's `since`); `at` the write time."""
    current = reviewer_disabled(store)
    if current is not None:
        return DriftResult(True, False, current.get("since"), current.get("gate"))
    cleared = store.query("SELECT updated_at FROM system_state WHERE key = ?", (DISABLED_KEY,))
    after = _ts(cleared[0][0]) if cleared else None
    rows = []
    for r in store.query("SELECT metrics_json, passed_gate, run_at FROM reviewer_evals ORDER BY run_at DESC, "
                         "eval_id DESC"):
        m = json.loads(r["metrics_json"])
        if m.get("reviewer_version_sha256") == reviewer_version_sha256 and (after is None or _ts(r["run_at"]) > after):
            rows.append((m, bool(r["passed_gate"])))
    nightly = [r for r in rows if r[0].get("kind") == "nightly_subset"][:DRIFT_NIGHTS]
    weekly = [r for r in rows if r[0].get("kind") == "weekly"][:1]
    failing = None
    if weekly and not weekly[0][1]:
        failing = weekly[0][0]
    elif len(nightly) == DRIFT_NIGHTS and not any(ok for _, ok in nightly):
        failing = nightly[0][0]
    if failing is None:
        return DriftResult(False)
    gate = next((g["gate"] for g in failing.get("gates", ()) if not g["passed"]), "unknown")
    since_at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.set_system_state(DISABLED_KEY, json.dumps({"since": date, "since_at": since_at, "gate": gate,
                                                     "reviewer_version_sha256": reviewer_version_sha256},
                                                    sort_keys=True), at=at)
    return DriftResult(True, True, date, gate, reviewer_unavailable())


def reenable(store: Store, doc: Mapping[str, Any], *, at: str | None = None) -> bool:
    """Clear the disabled state when `doc` is a passing eval report run after the Reviewer was disabled (3.14, 10.4:
    "re-enabled only by a PR carrying a passing eval report"); True when it did."""
    current = reviewer_disabled(store)
    if current is None or not report_passes(doc) or _ts(str(doc["run_at"])) <= _ts(current["since_at"]):
        return False
    store.set_system_state(DISABLED_KEY, "", at=at)
    return True


# -- weekly sampling into the development split (3.15) -------------------------------------------------------------

@dataclass(frozen=True)
class VerdictRow:
    """One stored Reviewer verdict (8.3 `reviews`)."""

    review_id: int
    run_id: str
    checkpoint_id: str
    verdict: str
    confidence: float
    mapped_outcome: str
    created_at: str


@dataclass(frozen=True)
class HumanDecision:
    """One human decision on a run's checkpoint (8.3 `decisions`)."""

    decision_id: int
    run_id: str
    checkpoint_id: str
    verdict: str
    decided_at: str


@dataclass(frozen=True)
class DevCandidate:
    """A verdict that joins the development split this week, and why (choice 7)."""

    review: VerdictRow
    reason: str                              # overturned | late_disagrees | intended_sample
    decision: HumanDecision | None = None

    def doc(self, week: str) -> dict[str, Any]:
        d = {"week": week, "reason": self.reason, **dataclasses.asdict(self.review)}
        if self.decision is not None:
            d.update(decision_id=self.decision.decision_id, decision_verdict=self.decision.verdict,
                     decided_at=self.decision.decided_at)
        return d


def week_rows(store: Store, since: str, until: str) -> tuple[list[VerdictRow], list[HumanDecision]]:
    """The verdicts stored in [since, until) on non-Nightly runs and the non-revoked human decisions on their runs."""
    verdicts = [VerdictRow(r["review_id"], r["run_id"], r["checkpoint_id"], r["verdict"], float(r["confidence"]),
                           r["mapped_outcome"], r["created_at"])
                for r in store.query("SELECT v.review_id, v.run_id, v.checkpoint_id, v.verdict, v.confidence, "
                                     "v.mapped_outcome, v.created_at FROM reviews v JOIN runs u USING (run_id) "
                                     "WHERE u.run_type <> 'nightly' AND v.created_at >= ? AND v.created_at < ? "
                                     "ORDER BY v.review_id", (since, until))]
    runs = sorted({v.run_id for v in verdicts})
    decisions = [HumanDecision(r["decision_id"], r["run_id"], r["checkpoint_id"], r["verdict"], r["decided_at"])
                 for r in store.query("SELECT decision_id, run_id, checkpoint_id, verdict, decided_at FROM decisions "
                                      f"WHERE status <> 'revoked' AND run_id IN ({','.join('?' * len(runs))}) "
                                      "ORDER BY decision_id", runs)] if runs else []
    return verdicts, decisions


def _latest(decisions: Iterable[HumanDecision]) -> dict[tuple[str, str], HumanDecision]:
    out: dict[tuple[str, str], HumanDecision] = {}
    for d in sorted(decisions, key=lambda d: (_ts(d.decided_at), d.decision_id)):
        out[(d.run_id, d.checkpoint_id)] = d
    return out


def _disagrees(v: VerdictRow, d: HumanDecision, threshold: float) -> bool:
    r = reviewer_input(Review("answered", verdict=v.verdict, confidence=v.confidence, threshold=threshold))
    return not _agrees(d.verdict, r)


def weekly_dev_sample(verdicts: Sequence[VerdictRow], decisions: Sequence[HumanDecision], *, week: str,
                      seed: int = EVAL_SEED, share: float = WEEKLY_SHARE,
                      threshold: float = ROUTING_THRESHOLD) -> list[DevCandidate]:
    """The verdicts that join the development split this week (3.15, choice 7): every verdict a human overturned,
    every late verdict that disagrees with an earlier human decision, and a seeded 5 percent of the other
    `intended` verdicts."""
    latest = _latest(decisions)
    out, taken = [], set()
    for v in sorted(verdicts, key=lambda v: v.review_id):
        d = latest.get((v.run_id, v.checkpoint_id))
        if d is not None and _disagrees(v, d, threshold):
            late = _ts(v.created_at) > _ts(d.decided_at)
            out.append(DevCandidate(v, "late_disagrees" if late else "overturned", d))
            taken.add(v.review_id)
    intended = [v for v in sorted(verdicts, key=lambda v: v.review_id)
                if v.mapped_outcome == "intended" and v.review_id not in taken]
    picked = random.Random(f"{seed}:{week}").sample(intended, math.ceil(share * len(intended)))
    out += [DevCandidate(v, "intended_sample", latest.get((v.run_id, v.checkpoint_id)))
            for v in sorted(picked, key=lambda v: v.review_id)]
    return out


def override_rates(verdicts: Sequence[VerdictRow], decisions: Sequence[HumanDecision], *,
                   threshold: float = ROUTING_THRESHOLD) -> dict[str, dict[str, Any]]:
    """The override rate per verdict (3.15): of the verdicts a human decided on, the share the decision overturned."""
    latest = _latest(decisions)
    out: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        d = latest.get((v.run_id, v.checkpoint_id))
        if d is None:
            continue
        row = out.setdefault(v.verdict, {"decided": 0, "overturned": 0})
        row["decided"] += 1
        row["overturned"] += _disagrees(v, d, threshold)
    for row in out.values():
        row["rate"] = row["overturned"] / row["decided"]
    return dict(sorted(out.items()))


# -- CLI -----------------------------------------------------------------------------------------------------------

def _settings(path: str | None) -> ReviewerConfig:
    from ..pipeline.live import load_reviewer_config

    return load_reviewer_config(path)


def _job(settings: ReviewerConfig, pin: str, n: int, transport: Transport | None, gate_root: Path,
         db: Store | None) -> ReviewerJob:
    """A synchronous job for `n` items on the pinned model: the PR path with its cap raised to `n` (an eval sends every
    item); the live transport goes through an egress gate over the config's allowlist, logged to `db`."""
    cfg = dataclasses.replace(settings, model_id=pin, call_caps={**settings.call_caps, "pr": max(n, 1)})
    if transport is None:
        from ..egress import EgressAllowlist, EgressGate
        from .transport import AnthropicTransport

        if db is None:
            raise EvalSetError("a live eval logs its egress: pass --db (6.10)")
        path = gate_root / "policy" / "egress_allowlist.yaml"
        allow = EgressAllowlist.load(path) if path.is_file() else EgressAllowlist.empty()
        transport = AnthropicTransport.from_env(allow_network=True, base_url=cfg.endpoint_base_url,
                                                gate=EgressGate(allow, db))
    return ReviewerJob(cfg, transport, "pr")


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    """`sentinel eval LABELS --config DIR (--verdicts FILE | --blobs DIR [--live])`: the Reviewer over the eval set.
    The threshold is read off the development split and the gates measured on the held-out split (`--nightly`: the
    fixed 30-item subset at `--threshold`); writes `<out>/eval/reports/<hex>.yaml` (not for `--nightly`), the metrics
    JSON and the weekly report text; with `--db` records the `reviewer_evals` row and, for `--nightly` and `--weekly`,
    runs the drift check. Exit 0 when every gate passes, 1 when one fails, 2 on bad input."""
    from ..branches import BranchesError
    from .transport import NetworkNotAllowed

    p = argparse.ArgumentParser(prog="sentinel eval", description=main.__doc__)
    p.add_argument("labels")
    p.add_argument("--config", required=True, help="the sentinel-config checkout whose reviewer tuple is evaluated")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--verdicts", help="recorded verdicts, one JSON object per line")
    src.add_argument("--blobs", help="the content-addressed image directory; the Reviewer runs over the items")
    p.add_argument("--live", action="store_true", help="send the items to the real Reviewer (ANTHROPIC_API_KEY)")
    p.add_argument("--reviewer-config", help="a YAML file whose `reviewer:` block sets the Reviewer (3.15)")
    p.add_argument("--eval-set-version", default="unversioned")
    p.add_argument("--seed", type=int, default=EVAL_SEED)
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--nightly", action="store_true", help="the nightly 30-item subset of the deployed tuple")
    kind.add_argument("--weekly", action="store_true", help="the weekly full evaluation of the deployed tuple")
    p.add_argument("--threshold", type=float, default=ROUTING_THRESHOLD, help="the deployed routing threshold "
                   "(--nightly)")
    p.add_argument("--previous", help="the previous pin's eval report, for the change rule")
    p.add_argument("--canary-run-id")
    p.add_argument("--run-at", default=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p.add_argument("--date", help="the night's date for the drift check (default: the date of --run-at)")
    p.add_argument("--db", help="the Central Store: records the reviewer_evals row (and the live egress log)")
    p.add_argument("--out", default="out")
    a = p.parse_args(argv)
    try:
        items = load_labels(a.labels)
        settings = _settings(a.reviewer_config)
        t = tuple_in_force(a.config, config=settings)
    except (EvalSetError, BranchesError, OSError, ValueError) as exc:
        print(f"sentinel eval: {exc}", file=sys.stderr)
        return 2
    if t is None:
        print("sentinel eval: no reviewer version tuple is in force in the config (DR-07 undecided, no pinned model, "
              "or the offline stand-in)", file=sys.stderr)
        return 2
    sp = split(items, a.seed)
    measured = nightly_subset(sp.held_out, a.seed) if a.nightly else list(sp.held_out)
    db = Store(a.db) if a.db else None
    try:
        if a.verdicts:
            preds: dict[str, Prediction | None] = dict(read_predictions(a.verdicts))
        else:
            if not a.live and transport is None:
                print("sentinel eval: --blobs needs --live (or recorded --verdicts)", file=sys.stderr)
                return 2
            wanted = measured if a.nightly else list(sp.development) + measured
            preds = predict(_job(settings, t.model_id, len(wanted), transport, Path(a.config), db), wanted, a.blobs)
        if a.nightly:
            threshold, derived = a.threshold, True
        else:
            derived_t = routing_threshold(sp.development, preds)
            threshold, derived = (derived_t, True) if derived_t is not None else (ROUTING_THRESHOLD, False)
        metrics = compute_metrics(measured, preds, threshold, threshold_derived=derived)
        results = gates(metrics)
        previous = yaml.safe_load(Path(a.previous).read_text(encoding="utf-8"))["gates"] if a.previous else None
        blocks = change_rule(results, previous)
        out = Path(a.out)
        (out / "eval" / "reports").mkdir(parents=True, exist_ok=True)
        stem = out / "eval" / "reports" / t.hex
        name = "nightly_subset" if a.nightly else "weekly" if a.weekly else "held_out"
        if not a.nightly:
            doc = report_doc(t, results, eval_set_version=a.eval_set_version, run_at=a.run_at,
                             canary_run_id=a.canary_run_id)
            stem.with_suffix(".yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        metrics_path = Path(f"{stem}.{name}.metrics.json")
        metrics_path.write_text(json.dumps({**metrics.doc(), "kind": name, "reviewer_version": t.doc(),
                                            "reviewer_version_sha256": t.sha256, "gates": [g.doc() for g in results],
                                            "change_rule": blocks, "composition": composition(items)},
                                           indent=1) + "\n", encoding="utf-8")
        text = summary_text(t, metrics, results, eval_set_version=a.eval_set_version, split_name=name,
                            composition_notes=composition(items), blocks=blocks)
        Path(f"{stem}.{name}.txt").write_text(text, encoding="utf-8")
        print(text, end="")
        if db is not None:
            record(db, t, metrics, results, eval_set_version=a.eval_set_version, kind=name, run_at=a.run_at)
            if a.nightly or a.weekly:
                drift = drift_check(db, t.sha256, date=a.date or a.run_at[:10], at=a.run_at)
                if drift.newly:
                    print(f"Reviewer disabled since {drift.since}: eval gate {drift.gate} failed "
                          f"(alert {drift.alert.rule if drift.alert else 'none'})")
    except (EvalSetError, NetworkNotAllowed, OSError, KeyError, ValueError) as exc:
        print(f"sentinel eval: {exc}", file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()
    return 0 if not blocks else 1


def gate_main(argv: list[str] | None = None) -> int:
    """`sentinel eval-gate REPORT --canary-result FILE [--previous REPORT] [--config DIR]`: the CI gate of 10.4 for a
    change to the reviewer tuple. Blocks when a 3.14 gate falls, when the change rule blocks against the previous
    pin's report, when the report is not for the tuple in force of `--config`, or when the canary gate result
    (`sentinel canary-gate`) did not pass (a missed critical mutant). Exit 0 to merge, 1 blocked, 2 on bad input."""
    p = argparse.ArgumentParser(prog="sentinel eval-gate", description=gate_main.__doc__)
    p.add_argument("report")
    p.add_argument("--canary-result", required=True)
    p.add_argument("--previous")
    p.add_argument("--config")
    a = p.parse_args(argv)
    try:
        doc = yaml.safe_load(Path(a.report).read_text(encoding="utf-8"))
        previous = yaml.safe_load(Path(a.previous).read_text(encoding="utf-8")) if a.previous else None
        canary_result = json.loads(Path(a.canary_result).read_text(encoding="utf-8"))
        expected = tuple_in_force(a.config) if a.config else None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"sentinel eval-gate: {exc}", file=sys.stderr)
        return 2
    blocks = report_problems(doc, expected, a.report)
    if not blocks:
        blocks = change_rule(doc["gates"], previous.get("gates") if isinstance(previous, Mapping) else None)
    if not canary_result.get("passed"):
        blocks.append("the canary gate did not pass: " + str(canary_result.get("message") or "no result"))
    for b in blocks:
        print(f"BLOCKED: {b}")
    if not blocks:
        print(f"eval gate passed: {doc['reviewer_version_sha256']} with a passing canary gate result")
    return 1 if blocks else 0
