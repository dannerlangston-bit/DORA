"""The redaction stage of Capture (3.6, 5.7, 6.10): only redacted images are stored, hashed, diffed or sent.

`redact_pair(base, candidate, anchors, ocr, theme)` redacts the base and candidate captures of one checkpoint in
memory and returns a `RedactionResult`:

- Stage 1, anchored boxes (6.10): every landmark of the redaction list (`redaction/anchors.yaml`, regions-file
  shape, SD-31) is searched on both captures with the Diff Engine's landmark rule (3.7: best `TM_CCOEFF_NORMED`
  at the landmark's floor, second peak under 0.85), and each `redaction` region is cut at its offset from its
  landmark wherever the landmark is found. A landmark found on one capture and not at its floor on the other
  ends the checkpoint `landmark_missing (reason: redaction_landmark:<id>)` with the side it is missing on; the
  result then carries no image and no header, so nothing is stored and nothing leaves the rig.
- Stage 2, OCR-regex sweep (6.10): each capture is upscaled 3x nearest-neighbor and read by a `WordOcr`; the
  words are grouped into lines and matched against the literal account ids and display name (from the fixture
  and the vault, always), the paperMoney id pattern, the live id pattern, email and phone. A hit that stage 1
  does not cover is filled and recorded as a `redaction_miss` finding with its crop location and side; a miss on
  the candidate only, absent from the base at the same place, is ranked tier 1 (a suspected identifier leak, 3.6).
- Fill (3.6): the union of both captures' boxes is applied to both, in the theme background color, so a box yields
  identical pixels on both sides. `boxes` are the `diff.regions.RedactionBox` list that `diff.engine.diff_pair`
  takes as `redactions`: stage-1 boxes carry their region's `identity_field`; stage-2 boxes are not identity
  boxes, so they count toward the mask budget, and one over a money cell or over a panel's budget becomes
  `harness_error (reason: mask_budget_exceeded)` in the Diff Engine, which owns the budget (3.8, 6.10).
- `header` is the `store.artifacts.Redaction` header the artifact store requires for an image (8.4), stamped with
  `version` (`redaction_version`); `counts` are the per-side box counts 3.6 stores.

Also here: `verify` re-reads a redacted frame (the re-OCR of the nightly redaction canary must find zero hits,
6.10 Proof), `h6_self_test` is preflight H6 (12.7), `sample_artifacts` picks the 5 percent post-run OCR sample
(3.6), and `SyntheticOcr` is a `WordOcr` over known word boxes, the stand-in for the rig's OCR engine off the rig.

Choices where the spec is ambiguous (reported):

1. Missing landmark: 6.10 and 5.7 say a landmark located on one capture of the pair and not at its floor on the
   other; 3.6's "not found at its floor on either side" is read as that rule. A landmark found on neither capture
   is not on this screen and adds no box. With several landmarks missing, the first in file order is reported.
2. The sweep reads the raw frame. A hit is covered by stage 1 when its box, shrunk by the 1 px that mapping a 3x
   box back can add on each edge, lies inside the union of both captures' stage-1 boxes; otherwise it is a miss
   and its whole box is filled, so a partly covered identifier is never left half visible.
3. Lines: words whose vertical extents overlap by at least half the smaller height form a row; a row splits where
   the horizontal gap between neighbours exceeds twice the smaller height. A line is its words joined with single
   spaces. Patterns match lines, so a phone number or a display name split into words is found; a match boxes
   every word it touches, and a hit inside another hit's box (a name token inside the whole name) is that hit.
4. Patterns (6.10 names them, not their syntax): paperMoney `D-` and 4 to 12 digits; live id 8 or 9 digits not
   part of a longer or decimal number (3.6); email; North American phone. Literal ids match whole words, case
   blind; the display name matches whole and token by token (5.7 "name tokens"), tokens of two characters or more.
5. Candidate-only (3.6, 6.10) applies to misses: a candidate miss that no base hit intersects. A miss at the same
   place on both sides is one finding with side `both`. A finding carries the crop rect, side and pattern kind,
   never the matched text.
6. Version: `redaction-<STAGE_VERSION>-<12 hex>` over the stage version, the 3x scale, the sweep patterns and the
   redaction list (landmark ids, template hashes, regions). Literal ids and the display name are left out: they
   differ by rig, and base and candidate must share one version (6.10).
7. H6 (12.7 "Masked; egress log entry"): passes when the sweep finds the synthetic id, every pixel of its box is
   the fill color, the re-OCR of the output has zero hits, and the result carries the redaction version that every
   upload header and egress row records (8.4, 8.3). No request is sent, so no `egress_log` row is written.
8. Captures of different sizes are redacted with the same boxes, each clipped to its own image (3.3 pads later).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import cv2
import numpy as np
import yaml

from ..diff import load_params
from ..diff.engine import Match, Terminal, locate_landmark
from ..diff.regions import Landmark, Rect, RedactionBox
from ..records import Finding
from ..store.artifacts import Redaction

STAGE_VERSION = "1"
OCR_SCALE = 3                     # 3.6, 6.10: the sweep reads a 3x nearest-neighbor upscale
SAMPLE_FRACTION = 0.05            # 3.6: OCR of a 5 percent sample after the run
SIDES = ("base", "candidate")
# 6.10 stage 2 patterns (choice 4): kind -> regex, in the order a hit's kind is chosen.
DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("paper_id", r"(?<![A-Za-z0-9])D-\d{4,12}(?!\d)"),
    ("live_id", r"(?<!\d)(?<!\d[.,])\d{8,9}(?!\d)(?![.,]\d)"),
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    ("phone", r"(?<![\d+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),
)
_KIND_ORDER = ("literal_id", "display_name") + tuple(k for k, _ in DEFAULT_PATTERNS)
H6_ACCOUNT_ID = "D-48201937"      # the synthetic account id preflight H6 plants (12.7)


class RedactionError(ValueError):
    """The redaction list or the sweep configuration breaks 4.8 or 6.10."""


@dataclass(frozen=True)
class OcrWord:
    """One word an OCR engine read: its text and box, in the coordinates of the image it was given."""
    text: str
    rect: Rect
    confidence: float = 100.0


class WordOcr(Protocol):
    """A whole-frame word reader (Tesseract or PaddleOCR on the rig, `SyntheticOcr` off it)."""

    def words(self, image: np.ndarray) -> Sequence[OcrWord]:
        """Every word of `image` with its box in `image` pixels."""


@dataclass(frozen=True)
class RedactionRegion:
    """One region of type `redaction` (4.8, SD-31): an offset box from a landmark, identity field or not."""
    id: str
    landmark: str
    rect: Rect
    identity_field: bool = False
    panel: str = "other"


@dataclass(frozen=True)
class RedactionList:
    """The redaction list, `redaction/anchors.yaml` (3.6, 6.10): landmarks plus `redaction` regions."""
    landmarks: tuple[Landmark, ...] = ()
    regions: tuple[RedactionRegion, ...] = ()
    owner: str = ""

    @staticmethod
    def from_dict(d: Mapping[str, Any], templates: Mapping[str, np.ndarray] | None = None,
                  template_root: str | os.PathLike | None = None) -> "RedactionList":
        """Build from the 4.8 shape. A landmark's template is `templates[id]`, else read (RGB) from
        `template_root / template`. Raises RedactionError."""
        lms = tuple(_landmark(x, templates, template_root) for x in d.get("landmarks") or [])
        lm_ids = [lm.id for lm in lms]
        if len(set(lm_ids)) != len(lm_ids):
            raise RedactionError("redaction landmark ids must be unique")
        regions = []
        for r in d.get("regions") or []:
            rid = str(r.get("id"))
            if r.get("type") != "redaction":
                raise RedactionError(f"{rid}: a redaction list region has type redaction, not {r.get('type')!r} "
                                     "(SD-31, 6.10)")
            if r.get("landmark") not in lm_ids:
                raise RedactionError(f"{rid}: landmark {r.get('landmark')!r} is not declared")
            rect = r.get("rect") or {}
            box = Rect(int(rect["dx"]), int(rect["dy"]), int(rect["w"]), int(rect["h"]))
            if box.w <= 0 or box.h <= 0:
                raise RedactionError(f"{rid}: empty rect")
            regions.append(RedactionRegion(rid, str(r["landmark"]), box, r.get("identity_field") is True,
                                           str(r.get("panel", "other"))))
        ids = [r.id for r in regions]
        if len(set(ids)) != len(ids):
            raise RedactionError("redaction region ids must be unique")
        return RedactionList(lms, tuple(regions), str(d.get("owner") or ""))

    @staticmethod
    def load(path: str | os.PathLike, config_root: str | os.PathLike | None = None) -> "RedactionList":
        """Read `redaction/anchors.yaml`; templates resolve against `config_root` (default: two levels up)."""
        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return RedactionList.from_dict(data, template_root=config_root or p.parent.parent)

    def digest(self) -> str:
        """sha256 over the list as the stage applies it: landmark ids, floors, search boxes, template bytes and
        regions (part of the redaction version, choice 6)."""
        body = {
            "landmarks": [[lm.id, lm.confidence_min, lm.search_rect.as_tuple() if lm.search_rect else None,
                           hashlib.sha256(np.ascontiguousarray(lm.template).tobytes()).hexdigest(),
                           list(lm.template.shape)] for lm in self.landmarks],
            "regions": [[r.id, r.landmark, r.rect.as_tuple(), r.identity_field] for r in self.regions],
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _landmark(d: Mapping[str, Any], templates: Mapping[str, np.ndarray] | None,
              root: str | os.PathLike | None) -> Landmark:
    lid = str(d["id"])
    tpl = (templates or {}).get(lid)
    if tpl is None:
        path = Path(root or ".") / str(d["template"])
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RedactionError(f"redaction landmark {lid}: template {path} not readable")
        tpl = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    sr = d.get("search_rect")
    return Landmark(id=lid, template=tpl,
                    confidence_min=float(d["confidence_min"]) if "confidence_min" in d else None,
                    search_rect=Rect(int(sr["x"]), int(sr["y"]), int(sr["w"]), int(sr["h"])) if sr else None)


@dataclass(frozen=True)
class Sweep:
    """What stage 2 looks for (6.10): the literal account ids and display name from the fixture and the vault, and
    the generic patterns (`DEFAULT_PATTERNS`, regenerated if Phase 0 finds another id format, 6.13)."""
    literals: tuple[str, ...] = ()
    display_name: str | None = None
    patterns: tuple[tuple[str, str], ...] = DEFAULT_PATTERNS

    def __post_init__(self) -> None:
        if any(not str(lit).strip() for lit in self.literals):
            raise RedactionError("a literal account id is empty")
        for kind, rx in self.patterns:
            if kind not in _KIND_ORDER:
                raise RedactionError(f"sweep pattern kind {kind!r} is not one of {_KIND_ORDER}")
            re.compile(rx)

    def matchers(self) -> list[tuple[str, re.Pattern[str]]]:
        """(kind, compiled pattern) in the order a hit's kind is chosen (choice 4)."""
        out = [("literal_id", re.compile(rf"(?<![A-Za-z0-9]){re.escape(lit.strip())}(?![A-Za-z0-9])", re.I))
               for lit in self.literals]
        if self.display_name and self.display_name.strip():
            tokens = self.display_name.split()
            out.append(("display_name", re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, tokens)) + r"(?!\w)",
                                                   re.I)))
            out += [("display_name", re.compile(rf"(?<!\w){re.escape(t)}(?!\w)", re.I)) for t in tokens if len(t) >= 2]
        out += [(kind, re.compile(rx)) for kind, rx in self.patterns]
        return out

    def digest(self) -> str:
        """sha256 over the generic patterns only (choice 6)."""
        return hashlib.sha256(json.dumps([list(p) for p in self.patterns]).encode()).hexdigest()


def redaction_version(anchors: RedactionList | None = None, sweep: Sweep | None = None) -> str:
    """The redaction version every stored image and upload carries (6.10, 8.4); choice 6."""
    body = json.dumps([STAGE_VERSION, OCR_SCALE, (sweep or Sweep()).digest(),
                       (anchors or RedactionList()).digest()])
    return f"redaction-{STAGE_VERSION}-{hashlib.sha256(body.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class SweepHit:
    """One stage-2 match on one side, in frame pixels; `covered` when stage 1 already boxes it (choice 2)."""
    side: str
    kind: str
    rect: Rect
    covered: bool = False


@dataclass(frozen=True)
class RedactionContext:
    """Who the findings belong to (4.13)."""
    run_id: str = "run-local"
    checkpoint_id: str | None = None
    tier: int | None = None


@dataclass(frozen=True)
class RedactionResult:
    """The redacted pair and what the stage did (module docstring). On a missing redaction landmark `terminal` is
    `landmark_missing (redaction_landmark:<id>)` and `base`, `candidate` and `header` are None: nothing is stored."""
    version: str
    base: np.ndarray | None
    candidate: np.ndarray | None
    boxes: tuple[RedactionBox, ...] = ()
    findings: tuple[Finding, ...] = ()
    counts: Mapping[str, int] = field(default_factory=lambda: {"base": 0, "candidate": 0})
    hits: tuple[SweepHit, ...] = ()
    terminal: Terminal | None = None

    @property
    def stored_ok(self) -> bool:
        """True when the redacted images may be stored and sent (6.10 stage 3)."""
        return self.terminal is None

    @property
    def header(self) -> Redaction | None:
        """The redaction header for `ArtifactStore.put` (8.4); None when nothing may be stored."""
        return Redaction(self.version) if self.terminal is None else None

    def masked(self, rect: Rect) -> bool:
        """True when the union of boxes covers every pixel of `rect` (the M19 and canary check, 12.5, 6.10)."""
        return _covered(rect, [b.rect for b in self.boxes])


def _rgb(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return np.repeat(img[:, :, None], 3, axis=2)
    if img.ndim == 3 and img.shape[2] in (3, 4):
        return np.ascontiguousarray(img[:, :, :3])
    raise RedactionError(f"a capture is H x W, H x W x 3 or H x W x 4; got shape {img.shape}")


def _fill_color(theme: tuple[int, int, int] | Mapping[str, Any]) -> tuple[int, int, int]:
    rgb = theme["bg"] if isinstance(theme, Mapping) else theme
    r, g, b = (int(v) for v in rgb)
    return r, g, b


def _covered(rect: Rect, rects: Sequence[Rect]) -> bool:
    if rect.w <= 0 or rect.h <= 0:
        return True
    canvas = np.zeros((rect.h, rect.w), dtype=bool)
    for r in rects:
        i = r.intersect(rect)
        if i is not None:
            canvas[i.y - rect.y:i.y - rect.y + i.h, i.x - rect.x:i.x - rect.x + i.w] = True
    return bool(canvas.all())


def _down(rect: Rect, scale: int) -> Rect:
    """A box read on the scaled image, back in frame pixels, rounded outward."""
    x0, y0 = rect.x // scale, rect.y // scale
    x1, y1 = -(-(rect.x + rect.w) // scale), -(-(rect.y + rect.h) // scale)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def _lines(words: Sequence[tuple[str, Rect]]) -> list[list[tuple[str, Rect]]]:
    """Group words into lines (choice 3): rows by vertical overlap, then split a row where a gap is too wide."""
    rows: list[list[tuple[str, Rect]]] = []
    for word in sorted(words, key=lambda w: (w[1].y + w[1].h / 2.0, w[1].x)):
        r = word[1]
        for row in rows:
            a = row[0][1]
            if (min(r.y + r.h, a.y + a.h) - max(r.y, a.y)) * 2 >= max(1, min(r.h, a.h)):
                row.append(word)
                break
        else:
            rows.append([word])
    lines: list[list[tuple[str, Rect]]] = []
    for row in rows:
        row.sort(key=lambda w: w[1].x)
        line = [row[0]]
        for prev, word in zip(row, row[1:]):
            if word[1].x - (prev[1].x + prev[1].w) > 2 * max(1, min(prev[1].h, word[1].h)):
                lines.append(line)
                line = []
            line.append(word)
        lines.append(line)
    return lines


def _sweep(image: np.ndarray, ocr: WordOcr, matchers: Sequence[tuple[str, re.Pattern[str]]],
           side: str) -> list[SweepHit]:
    """Stage 2 on one frame (6.10): 3x nearest-neighbor upscale, OCR, line match; one hit per box."""
    up = cv2.resize(image, None, fx=OCR_SCALE, fy=OCR_SCALE, interpolation=cv2.INTER_NEAREST)
    words = [(w.text, _down(w.rect, OCR_SCALE)) for w in ocr.words(up) if w.text.strip()]
    found: dict[Rect, str] = {}
    for line in _lines(words):
        text, spans, pos = "", [], 0
        for i, (t, _) in enumerate(line):
            if i:
                text += " "
                pos += 1
            spans.append((pos, pos + len(t)))
            text += t
            pos += len(t)
        for kind, rx in matchers:
            for m in rx.finditer(text):
                touched = [r for (s, e), (_, r) in zip(spans, line) if s < m.end() and e > m.start()]
                if not touched:
                    continue
                box = Rect.bounding(touched)
                if box not in found or _KIND_ORDER.index(kind) < _KIND_ORDER.index(found[box]):
                    found[box] = kind
    kept = [(box, kind) for box, kind in found.items()          # a token inside a whole-name or id hit is that hit
            if not any(other != box and other.intersect(box) == box for other in found)]
    return [SweepHit(side, kind, box) for box, kind in sorted(kept, key=lambda kv: kv[0].as_tuple())]


def verify(image: np.ndarray, ocr: WordOcr, sweep: Sweep | None = None, *,
           side: str = "candidate") -> tuple[SweepHit, ...]:
    """Re-read a redacted frame: the hits still legible (6.10 Proof: the canary output must contain zero)."""
    return tuple(_sweep(_rgb(image), ocr, (sweep or Sweep()).matchers(), side))


def _finding(ctx: RedactionContext, side: str, hit: SweepHit, candidate_only: bool) -> Finding:
    crop = list(hit.rect.as_tuple())
    key = json.dumps([ctx.run_id, ctx.checkpoint_id, "redaction_miss", side, crop])
    where = "candidate only: a suspected identifier leak" if candidate_only else f"side {side}"
    return Finding(
        finding_id="fnd-" + hashlib.sha256(key.encode()).hexdigest()[:16], run_id=ctx.run_id, kind="redaction_miss",
        description=f"the OCR sweep found a {hit.kind} outside the redaction anchors at {tuple(crop)} ({where}); "
                    "the anchor list is incomplete",
        first_seen_run_id=ctx.run_id, checkpoint_id=ctx.checkpoint_id, tier=1 if candidate_only else ctx.tier,
        side=side, detail="candidate_only" if candidate_only else None,
        evidence={"crop": crop, "side": side, "pattern": hit.kind, "candidate_only": candidate_only})


def redact_pair(base_img: np.ndarray, cand_img: np.ndarray, anchors: RedactionList, ocr: WordOcr,
                theme: tuple[int, int, int] | Mapping[str, Any], *, sweep: Sweep | None = None,
                context: RedactionContext | None = None, params: Mapping[str, Any] | None = None) -> RedactionResult:
    """Redact one base/candidate pair (module docstring). The inputs are not modified; the raw frames never leave
    this call. `theme` is the background RGB or a theme mapping with `bg`; `params` the Diff Engine parameters
    (landmark floors, 3.7)."""
    sw = sweep or Sweep()
    ctx = context or RedactionContext()
    version = redaction_version(anchors, sw)
    p = params or load_params()
    imgs = {"base": _rgb(base_img), "candidate": _rgb(cand_img)}

    # stage 1: anchored boxes wherever the landmark is found; found on one side only refuses the checkpoint
    stage1: dict[str, list[RedactionBox]] = {s: [] for s in SIDES}
    for lm in anchors.landmarks:
        m: dict[str, Match] = {s: locate_landmark(imgs[s], lm, p) for s in SIDES}
        if m["base"].found != m["candidate"].found:
            side = "candidate" if m["base"].found else "base"
            detail = (f"base score {m['base'].score:.3f}/{m['base'].second:.3f}, "
                      f"candidate score {m['candidate'].score:.3f}/{m['candidate'].second:.3f}")
            return RedactionResult(version, None, None, terminal=Terminal(
                "landmark_missing", f"redaction_landmark:{lm.id}", side=side, detail=detail))
        if not m["base"].found:
            continue
        for s in SIDES:
            lx, ly = m[s].pos or (0, 0)
            stage1[s] += [RedactionBox(r.rect.offset(lx, ly), r.identity_field, r.id)
                          for r in anchors.regions if r.landmark == lm.id]
    anchored = [b.rect for s in SIDES for b in stage1[s]]

    # stage 2: the OCR sweep on each raw frame; a hit stage 1 does not cover is a miss (choice 2)
    matchers = sw.matchers()
    hits: list[SweepHit] = []
    for s in SIDES:
        for h in _sweep(imgs[s], ocr, matchers, s):
            inner = Rect(h.rect.x + 1, h.rect.y + 1, h.rect.w - 2, h.rect.h - 2) if min(h.rect.w, h.rect.h) > 2 \
                else h.rect
            hits.append(SweepHit(s, h.kind, h.rect, _covered(inner, anchored)))
    misses = {s: [h for h in hits if h.side == s and not h.covered] for s in SIDES}
    base_hits = [h.rect for h in hits if h.side == "base"]

    findings: list[Finding] = []
    paired: set[SweepHit] = set()
    for h in misses["candidate"]:
        twin = next((b for b in misses["base"] if b.rect.intersect(h.rect) is not None), None)
        if twin is not None:
            paired.add(twin)
            findings.append(_finding(ctx, "both", h, False))
        else:
            only = all(r.intersect(h.rect) is None for r in base_hits)
            findings.append(_finding(ctx, "candidate", h, only))
    findings += [_finding(ctx, "base", h, False) for h in misses["base"] if h not in paired]

    # fill: the union of both sides' boxes on both captures, in the theme background (3.6)
    boxes: list[RedactionBox] = []
    for b in [*stage1["base"], *stage1["candidate"], *(RedactionBox(h.rect) for s in SIDES for h in misses[s])]:
        if b not in boxes:
            boxes.append(b)
    fill = np.array(_fill_color(theme), dtype=np.uint8)
    out = {}
    for s in SIDES:
        img = imgs[s].copy()
        H, W = img.shape[:2]
        for b in boxes:
            i = b.rect.intersect(Rect(0, 0, W, H))
            if i is not None:
                img[i.y:i.y + i.h, i.x:i.x + i.w] = fill
        out[s] = img
    counts = {s: len(stage1[s]) + len(misses[s]) for s in SIDES}
    return RedactionResult(version, out["base"], out["candidate"], tuple(boxes), tuple(findings), counts,
                           tuple(hits))


class SyntheticOcr:
    """A `WordOcr` over known word boxes in frame pixels (`sentinel.synth` ground truth, a planted identifier).

    A word is returned, scaled to the image it is given, only while its box still shows more than one color, so a
    filled box reads as nothing: a redacted frame re-read this way has zero hits (6.10 Proof)."""

    def __init__(self, words: Sequence[tuple[str, Rect]], frame_size: tuple[int, int]):
        self._words = [(str(t), r) for t, r in words]
        self._w, self._h = frame_size

    @staticmethod
    def from_screen(screen: Any) -> "SyntheticOcr":
        """Every drawn component of a `sentinel.synth.Screen` that carries text."""
        h, w = screen.image.shape[:2]
        return SyntheticOcr([(c.text, Rect(*c.bbox)) for c in screen.components if c.text], (w, h))

    def words(self, image: np.ndarray) -> list[OcrWord]:
        sx, sy = image.shape[1] / self._w, image.shape[0] / self._h
        out = []
        for text, r in self._words:
            box = Rect(int(math.floor(r.x * sx)), int(math.floor(r.y * sy)),
                       int(math.ceil(r.w * sx)), int(math.ceil(r.h * sy)))
            i = box.intersect(Rect(0, 0, image.shape[1], image.shape[0]))
            if i is None:
                continue
            crop = image[i.y:i.y + i.h, i.x:i.x + i.w].reshape(-1, image.shape[2] if image.ndim == 3 else 1)
            if (crop != crop[0]).any():
                out.append(OcrWord(text, box))
        return out


@dataclass(frozen=True)
class SelfTest:
    """Preflight H6 (12.7): `passed` feeds `PreflightReport.items["H6"]`."""
    passed: bool
    version: str
    boxes: int
    detail: str | None = None


def h6_frame(theme: tuple[int, int, int] | Mapping[str, Any],
             text: str = H6_ACCOUNT_ID) -> tuple[np.ndarray, tuple[str, Rect]]:
    """The H6 probe frame: the theme background with a synthetic account id in light text, and the id's box."""
    fill = _fill_color(theme)
    img = np.full((120, 400, 3), fill, dtype=np.uint8)
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
    org = (60, 70)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 1.0, (235, 235, 235), 2, cv2.LINE_AA)
    return img, (text, Rect(org[0] - 2, org[1] - th - 2, tw + 4, th + base + 4))


def h6_self_test(theme: tuple[int, int, int] | Mapping[str, Any], ocr: WordOcr | None = None, *,
                 anchors: RedactionList | None = None, sweep: Sweep | None = None,
                 params: Mapping[str, Any] | None = None) -> SelfTest:
    """Preflight H6: a frame with a synthetic account id through the redaction stage (12.7; choice 7). `ocr` is the
    rig's OCR engine; without one the probe's known word box stands in (`SyntheticOcr`)."""
    frame, word = h6_frame(theme)
    reader = ocr or SyntheticOcr([word], (frame.shape[1], frame.shape[0]))
    res = redact_pair(frame, frame.copy(), anchors or RedactionList(), reader, theme, sweep=sweep, params=params)
    rect = word[1]
    fail = None
    if res.terminal is not None or res.base is None:
        fail = f"redaction refused the probe: {res.terminal}"
    elif not res.masked(rect):
        fail = "the synthetic account id was not boxed by the sweep"
    elif not (res.base[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w] == np.array(_fill_color(theme))).all():
        fail = "the synthetic account id's box is not the fill color"
    elif verify(res.base, reader, sweep):
        fail = "the re-read of the redacted probe still finds an identifier"
    elif not res.version:
        fail = "the redacted probe carries no redaction version"
    return SelfTest(fail is None, res.version, len(res.boxes), fail)


def sample_artifacts(shas: Sequence[str], run_id: str, fraction: float = SAMPLE_FRACTION) -> tuple[str, ...]:
    """The post-run OCR sample (3.6): a deterministic `fraction` of the run's stored images (at least one when any
    exists), chosen by sha256 of run id and hash so a re-run of the audit reads the same images."""
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"sample fraction {fraction} outside (0, 1]")
    unique = sorted(set(shas))
    if not unique:
        return ()
    n = max(1, math.ceil(len(unique) * fraction))
    ranked = sorted(unique, key=lambda s: hashlib.sha256(f"{run_id}:{s}".encode()).hexdigest())
    return tuple(sorted(ranked[:n]))
