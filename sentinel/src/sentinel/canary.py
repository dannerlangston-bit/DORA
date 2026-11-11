"""Canary Service: mutant generation, scoring and the Nightly's canary records (10.3, 12.5, 4.11, 5.15, 8.3, 9.2;
decisions F7, F31, F58, SD-42 to SD-45, SD-47, SD-81, SD-88, SD-89, SD-91).

The catalog (`canaries/mutants.yaml`, the `{schema_version, entries}` file of 4.11 or the old bare list, SD-47) is
loaded with `load_catalog` or `parse_catalog`, each entry validated by the lint's generated schema (L-MUT-00).
`generate` turns each image entry into a mutant of the approved capture of its checkpoint, deterministically from
the catalog seed 20260101, and records a sha256 manifest that `check_determinism` compares with the previous night's
and with the hash the G3 preflight checked (10.3 Determinism). The mutants are injected by the caller through
`evaluate_checkpoint` with `synthetic: true` (10.3 Injection); behavioral mutants run their canary flow and hand
in its checkpoint record and step status. `score` takes what the path produced for each mutant and returns the
`canary_results` rows of 8.3, the run's canary state (SD-88), the `synthetic_outcomes` rows (SD-91), every signal
id that fired, the coverage-gap lines and the morning banner of 10.3. `persist` writes them with the night's
manifest, `system_state.detector_state` and the run's summary; `read_detector_state`, `class_history` and
`previous_manifest` read them back for later runs. The path-equivalence self-test is `canary_paths`; the pages and
SENT Bugs are `canary_integrations`.

One recipe per class of the 10.3 class table, each a pure function
``recipe(image, box, rng, **params) -> image`` over an 8-bit RGB frame (the channel order of the Diff
Engine, `synth` and the landmark-template loader) and an ``(x, y, w, h)`` box from the Region Registry
located by landmark. The money-cell flag comes from the catalog only: money cells
are a closed list (Qty, Price, Buying Power Effect, Max Loss, Send, Cancel, Delete; F58, 3.8), so the
ladder, P/L, balances, BUY and SELL labels and dialog titles are non-money and count toward
`detector_degraded`, M12 among them.

Choices where the spec is ambiguous (reported to the coordinator):

1. Glyph transplant (`swapped_digit`, `header_change`, 10.3): glyphs are columns of ink (pixels more
   than 48 levels from the box's interior background); a run of touching glyphs, as wide as
   `round(width / (0.75 x ink height))` glyphs (a digit's advance is about three quarters of its
   height), is split at the column of least ink nearest each even division, and a run with no clear
   valley there (more than 60 percent of its peak column) cannot be isolated: never a target
   (`GenerationError`) and never a donor, so one glyph is replaced, never several. The donor is a
   different glyph of the same row band across the whole frame, chosen by the seeded rng. There is
   no OCR here, so the catalog's `from`/`to` params (M03) are descriptive only, and `header_change`
   cannot guarantee that the new date is exactly one day later; it transplants a different glyph
   into the header. Touching glyphs narrower together than 1.125 x their height (a "1" against a
   "0" in some fonts) still read as one glyph.
2. Parameter ranges the table does not give: `shifted_panel` without `dx`/`dy` moves 8 to 12 px (the
   M08 and M09 values) in an rng-chosen direction; `scrollbar` draws a 12 px track with a thumb of 20 to
   50 percent of the box height; `error_dialog` synthesizes the Java exception dialog with OpenCV
   (no template asset ships with the core); `redaction` plants 9 rng digits unless `text` is given;
   `window_offscreen` moves the frame to `x` (default -200, M30) and fills the uncovered strip with
   black desktop; `study_removed` needs the study's RGB `color` and inpaints it (Telea, radius 3).
   Erase and translate fills use the 4 px border background of 10.3; the value-region recipes (glyphs,
   clipping, rows, placeholder, font height, scrollbar, typed value, cell shift) use the region's most
   common interior color, so a white field stays white around the new text.
3. Region params: `layer` (overlapped_panels, M12), `source` (swapped_label) and `parent`
   (outside_parent) name region ids resolved from the checkpoint's located boxes; `donor_checkpoint`
   (focus_order) names the checkpoint whose approved capture is the after-frame of step n+1. A name
   that cannot be resolved is `generation_failed`, like a missing landmark.
4. The synthetic batch (`bound_checkpoints`) is the concrete checkpoints the night's image entries bind. An
   `aa_control` entry bound to ``"*"`` (M25, SD-43) and every `global_shift` entry (M21, M23, SD-89) expand to one
   mutant per batch checkpoint. A global-shift mutant is one judgment over its members: detected only when every
   generated member ended `needs_human (reason: global_shift)`, the one cluster of the 3.7 pre-pass the caller ran
   over the batch; every member's row takes that verdict.
5. Behavioral entries (M26 to M28, M30) run their `canary_flow` on the rig. `generate` renders their image
   simulation only when the checkpoint has an approved capture (for the CI gate run of 10.3 and, for `gateway`,
   the non-matching Send template); otherwise it returns them unrendered, since their stimulus is the flow. `score`
   takes the flow's checkpoint record and step status; a gateway mutant whose step was blocked may come without a
   record (no click, nothing captured), any other miss keeps the record (8.3: a miss keeps the produced outcome).
   A behavioral mutant with neither a record nor a step status did not run: that mutant's `generation_failed`
   (10.3: a detector that was not tested is not trusted).
6. Hashes are over the decoded pixels (shape header plus raw bytes), not an encoded file, so they
   do not depend on the PNG encoder. A mutant whose base hash and seed equal last night's but whose mutant
   hash differs is `generation_failed` (nondeterministic generator); a catalog whose sha256 is not the one the
   G3 preflight checked fails every mutant before rendering.
7. Detection is on the final outcome (10.3: "in its expected list"), and a classification confidence below
   `expected.min_confidence` (4.11) does not count. The expected signal is matched against every id that fired
   (`fired_ids`: `invariant_ids`, `assertion_ids`, the reason and the lower-precedence results of 9.1, plus the
   caller's `fired_ids`): `invariant_id` for an `invariant_violation`, `assertion_id` for an `assertion_failed`,
   and `expected.reason` (SD-44) wherever the taxonomy allows it for the produced outcome. An outcome in the list
   without its signal is detected under 10.3's rule and listed in `unproven` and as a proof-gap line (12.3:
   every invariant has a mutant proving it fires); `score(strict_signals=True)` scores it a miss instead.
   `collateral` or `needs_human` counts for non-controls when it lands on the mutated region: a component whose
   region is the `region_ref` or whose box meets the mutant's footprint, a region above threshold that is the
   `region_ref`, or the classification's region pointer. The footprint is in frame coordinates, so it is tested
   against the component's window box `bbox`, never the region-relative `bbox_region` the Diff Engine also records
   for clustering (its choice 16, 9.7). `gateway` is detected on its step status (`expected.step_status`, default
   `blocked_by_gateway`); `redaction` on the redaction hit (12.5 M19). Borderline Reviewer verdicts must already
   be re-sampled (3.15) before `score`. The below-threshold control `sub_floor` (M20) is detected whenever the
   path absorbed it, that is on any outcome without a finding (`identical` or `noise`, 10.3 class table and 12.5
   M20), whatever its `expected` list says: under the zero floor of a frozen region a 1 px divider shift is either
   under the YIQ threshold everywhere (`identical`) or above it, never `below` (`noise`). The identical control
   `aa_control` (M25) still needs its expected `identical`.
8. Rolling floor: the lower of the median of the class's last 14 nightly rates minus 5 points and
   90 percent; 90 percent with no history; never below 95 percent once Phase 2 exits (read literally,
   the floor is then 95 percent). Tonight's rate is detected over scored non-money mutants of the class,
   with generation failures excluded. `class_history` reads each class's stored rate per earlier Nightly run.
9. Run canary state (SD-88): `generation_failed` when nothing was generated or a checkpoint of the caller's
   `critical_checkpoints` has no generated mutant; otherwise `detector_blind` when any row is, else
   `detector_degraded` when any row is, else `ok`. One mutant's generation failure and `missed` rows leave the
   run `ok`. A missed `redaction` mutant (M19) writes `harness_error (reason: safety_canary_blind:redaction)` and
   raises the run to `detector_blind` (same P1 and banner as 10.3 safety canaries), while its own row
   follows its non-money class (the 8.3 CHECK forbids `detector_blind` on a non-money row).
10. Banner: the 10.3 text for one money-cell miss; further misses add "Also missed:" blocks;
    `generation_failed` uses the blind header with the line "canaries did not run", the reason, and one line per
    failed mutant; `detector_degraded` lists the classes with rate and floor. Every non-`ok` state
    ends "RC sign-off is blocked." (12.2: any non-ok state blocks RC sign-off). `banner_for` restamps the first
    line with an earlier start time.
11. The catalog validates through `schemas/mutant.schema.json`, so the parser and the lint read one rule set (no
    `description` key; the banner prints the 12.5 Transformation text for M01 to M33, else the name). Parsing
    tolerates two schema errors: `checkpoint_id: "*"`, which SD-43 allows for `aa_control` and the generated
    pattern does not yet accept (the class rule is checked here), and a missing `name`, which 10.3's own M12
    example omits.
12. What absorbed a miss (`absorbed_by`, 10.3 Scoring): the suppressing decision, the masks that meet the mutant's
    footprint or are its region, a non-zero calibrated floor on such a region with its fingerprint, fix-list item
    and owner, the landmark of a `landmark_missing` record, the manifest entry of an `intended` record, and for the
    controls the finding they over-fired with; a signal that did not fire comes first. The caller passes the
    registry's masks and the calibration floors; without them the record alone is read.
13. Coverage gaps (10.3 Gate and governance, L-MUT-05, SD-42) are counted over the generated mutants for the
    critical checkpoints and the tier-1 checkpoints with an approved baseline the caller names: fewer than 20 live
    mutants, or a missing brief class, is one line each.
14. `system_state.detector_state` is JSON (`DetectorState`): the banner state (the mutants' state, raised to
    `detector_blind` by a blind extra row: a diverged path-equivalence pair, a blind safety canary or drill item),
    its start time (kept while the state is unchanged, 8.3 "with its start time"), the banner, the degraded classes
    and the run-health Canary row. A green night writes state `ok`, which clears the banner (8.3 has no delete for
    the key). The banner adds a line per diverged pair; safety canaries and drill items keep their own 6.5 banner.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Mapping, Sequence

import cv2
import numpy as np
import yaml
from numpy.typing import NDArray

from .lint import schemas
from .lint.rules import BRIEF_CLASSES
from .records import CheckpointOutcome, Finding, SyntheticOutcome
from .taxonomy import TaxonomyError, load

if TYPE_CHECKING:
    from .store.store import Store

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
ABSORBED_OUTCOMES = ("identical", "noise")                  # no finding: what detects the sub_floor control (choice 7)
WHOLE_WINDOW_CLASSES = frozenset({"global_shift", "window_offscreen", "focus_order", "aa_control"})
OPS = ("erase", "translate", "replace_glyph", "channel_swap", "crop_right", "overlay", "remove_row", "type_value")
BATCH_CLASSES = frozenset({"global_shift"})               # SD-89: applied to every bound checkpoint, judged as a batch
REGION_PARAMS = {"layer": "layer_box", "source": "source_box", "parent": "parent_box"}
DESCRIPTIVE_PARAMS = frozenset({"from", "to"})        # choice 1: no OCR, recorded only
MUTANT_ID = re.compile(r"^M[0-9]{2,}$")
CATALOG_SCHEMA_VERSION = 1                            # 4.11 mutant_catalog.schema_version
MIN_LIVE_MUTANTS = 20                                 # 10.3 Gate and governance: the coverage-gap line
BLIND_TAIL = ("Reports for the affected checkpoints cannot be trusted until this clears.",   # 10.3 Morning message
              "RC sign-off is blocked.")
ROLE_FOR_ID = {"invariant_violation": "invariant_id", "assertion_failed": "assertion_id"}
# 12.5 Transformation column: what the banner prints after the id and class for M01 to M33 (choice 11).
TRANSFORMATIONS_12_5 = {
    "M01": "Confirm and Send rect filled with sampled background", "M02": "Send rect filled with dialog background",
    "M03": "Quantity 100 -> 108 by glyph transplant", "M04": "One digit of Buying Power Effect",
    "M05": "Price 150.25 -> 150.52", "M06": "SELL transplanted over BUY",
    "M07": "Red and green channels swapped in the P/L Open column", "M08": "Ladder translated 12 px right into its neighbor",
    "M09": "Grid translated 8 px down", "M10": '"Buying Power" cut to "Buying Pow" at the cell edge',
    "M11": "Ellipsis inserted in a column header", "M12": "Ladder viewport translated 14 px up into the order-control strip",
    "M13": "One strike row removed, gap closed", "M14": "Expiration date moved one day",
    "M15": '"Loading..." pasted over the body', "M16": "One-row text region downscaled to 5 px cap height",
    "M17": "Scrollbar drawn on the empty grid", "M18": '"Net Liq" label erased',
    "M19": "Synthetic account id left unredacted in the input frame", "M20": "One divider shifted 1 px",
    "M21": "Whole window translated 1 px", "M22": "Dialog title erased", "M23": "Red channel +6 everywhere",
    "M24": "SMA line erased", "M25": "Approved capture injected unchanged",
    "M26": "Quantity 10 typed where the flow expects 100", "M27": "Price left blank, Confirm and Send clicked",
    "M28": "Send template swapped for a non-matching image for one step",
    "M29": "BUY MKT button translated 6 px across the panel border", "M30": "Detached ladder window moved to x = -200",
    "M31": "One numeric cell shifted 3 px right within its column",
    "M32": "After-frame of Tab step 3 replaced by that of Tab step 4", "M33": "Java exception dialog composited over the body",
}

BORDER_PX = 4          # 10.3 erased_control: background sampled from the 4 px border
INK_DELTA = 48         # a channel this far from the background is ink
CHROMA = 40            # max minus min channel above this is a colored pixel (color_swap)
GLYPH_PITCH = 0.75     # a glyph's advance as a share of its ink height: splits touching glyphs (choice 1)
GLYPH_VALLEY = 0.6     # a split column holds at most this share of the run's peak column ink (choice 1)
FLOOR_CAP = 0.90       # 10.3 rolling floor
FLOOR_MARGIN = 0.05
FLOOR_TARGET = 0.95    # 9.11 rolling target once Phase 2 exits
FLOOR_WINDOW = 14


class CatalogError(ValueError):
    """A catalog entry breaks the 4.11 schema."""


class GenerationError(RuntimeError):
    """A mutant could not be generated: `harness_error (reason: canary_generation_failed)` (10.3)."""


# -- catalog (4.11) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Expected:
    """4.11 `expected`: the outcomes that count as detection, the signal ids they must carry, and the SD-44 forms."""
    outcomes: tuple[str, ...]
    min_confidence: float | None = None
    invariant_id: str | None = None
    assertion_id: str | None = None
    reason: str | None = None                 # SD-44: fixed by class (L-MUT-02)
    step_status: str | None = None            # SD-44: gateway mutants (M28)
    redaction_hit: bool | None = None         # SD-44: redaction mutants (M19)


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
    catalog_version: str = ""

    @property
    def label(self) -> str:
        """What the banner prints after the id and class: the 12.5 Transformation, else the name (choice 11)."""
        return TRANSFORMATIONS_12_5.get(self.mutant_id) or self.name or self.op


@dataclass(frozen=True)
class Catalog:
    """The parsed catalog and its version (the sha256 of the file, recorded per result row, 10.3). `bare_list`
    is True for a file in the old bare-list form, which the lint warns on in Phase 0 and refuses from Phase 1
    (SD-47)."""
    entries: tuple[MutantEntry, ...]
    version: str
    bare_list: bool = False


def _need(cond: bool, mid: str, msg: str) -> None:
    if not cond:
        raise CatalogError(f"{mid}: {msg}")


def _relaxed(raw: Mapping[str, Any], location: str, message: str) -> bool:
    """The two schema errors parsing tolerates (choice 11): `checkpoint_id: "*"`, which SD-43 allows for class
    `aa_control` and the generated pattern does not yet accept (the class rule is checked here instead), and a
    missing `name`, which 10.3's own M12 example omits."""
    if location == "checkpoint_id" and raw.get("checkpoint_id") == "*":
        return True
    return location == "" and message == "'name' is a required property"


def parse_entry(raw: Mapping[str, Any], version: str = "") -> MutantEntry:
    """Validate one 4.11 entry against `schemas/mutant.schema.json` (the lint's rule set, L-MUT-00) and the entry
    rules that need two fields, and return it."""
    if not isinstance(raw, Mapping):
        raise CatalogError(f"catalog entry {raw!r} is not a mapping")
    mid = str(raw.get("mutant_id", "?"))
    _need(bool(MUTANT_ID.match(mid)), mid, "mutant_id must match ^M[0-9]{2,}$")
    errors = [f"{loc or 'entry'}: {msg}" for loc, msg in schemas.validate("mutant#mutant", dict(raw))
              if not _relaxed(raw, loc, msg)]
    _need(not errors, mid, "; ".join(errors) + " (schemas/mutant.schema.json, L-MUT-00)")
    cls, mode, flow, cid = raw["class"], raw["mode"], raw.get("canary_flow"), raw["checkpoint_id"]
    _need((mode == "behavioral") == (cls in BEHAVIORAL_CLASSES), mid,
          f"class {cls} is {'not ' if mode == 'behavioral' else ''}behavioral (10.3)")
    _need((flow is not None) == (mode == "behavioral"), mid,
          "canary_flow is required for behavioral and forbidden for image (L-MUT-04)")
    _need(cid != "*" or cls == "aa_control", mid, 'checkpoint_id "*" is only for class aa_control (SD-43, L-MUT-01)')
    _need(cid == "*" or raw.get("region_ref") is not None, mid, 'region_ref is required unless checkpoint_id is "*" (4.11)')
    gen, exp, tax = raw["generator"], raw["expected"], load()
    _need(bool(exp["outcomes"]), mid, "expected.outcomes must be a non-empty list")
    inv = exp.get("invariant_id")
    _need(inv is None or inv in tax.data["reasons"]["invariant_violation"], mid, f"invariant_id {inv!r} (L-MUT-06)")
    conf = exp.get("min_confidence")
    _need(conf is None or 0 <= conf <= 1, mid, "min_confidence must be in [0, 1]")
    reason = exp.get("reason")
    _need(reason is None or any(tax.reason_allowed(o, reason) for o in exp["outcomes"]), mid,
          f"expected.reason {reason!r} is not a reason of any expected outcome (SD-44, L-MUT-02)")
    _need(exp.get("step_status") is None or cls == "gateway", mid, "expected.step_status is for class gateway (SD-44)")
    _need(exp.get("redaction_hit") is None or cls == "redaction", mid,
          "expected.redaction_hit is for class redaction (SD-44)")
    return MutantEntry(
        mutant_id=mid, checkpoint_id=cid, defect_class=cls, mode=mode, money_cell=raw["money_cell"],
        op=gen["op"], params=dict(gen["params"]), owner=raw["owner"], region_ref=raw.get("region_ref"),
        seed=gen.get("seed"), name=raw.get("name"), canary_flow=flow, catalog_version=version,
        expected=Expected(tuple(exp["outcomes"]), conf, inv, exp.get("assertion_id"), reason,
                          exp.get("step_status"), exp.get("redaction_hit")),
    )


def parse_catalog(text: str | bytes) -> Catalog:
    """Parse `canaries/mutants.yaml`: the `{schema_version, entries}` file of 4.11, or the old bare list (SD-47:
    both forms parse; the lint grades the bare list by phase). The version is the sha256 of the text."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    version = hashlib.sha256(data).hexdigest()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise CatalogError(f"canaries/mutants.yaml is not valid YAML: {exc}") from exc
    bare = isinstance(raw, list)
    if not bare:
        if not (isinstance(raw, Mapping) and set(raw) <= {"schema_version", "entries"}
                and isinstance(raw.get("entries"), list)):
            raise CatalogError("canaries/mutants.yaml must be {schema_version, entries: [...]} or a list of mutant "
                               "entries (4.11, SD-47)")
        if raw.get("schema_version", CATALOG_SCHEMA_VERSION) != CATALOG_SCHEMA_VERSION:
            raise CatalogError(f"canaries/mutants.yaml schema_version {raw.get('schema_version')!r}; this reader "
                               f"knows {CATALOG_SCHEMA_VERSION} (4.11)")
        raw = raw["entries"]
    entries = tuple(parse_entry(r, version) for r in raw)
    ids = [e.mutant_id for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CatalogError(f"duplicate mutant ids {dupes} (L-MUT-01)")
    return Catalog(entries, version, bare)


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
        raise GenerationError("approved capture must be an 8-bit RGB frame (H x W x 3)")
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


def _glyphs(ink: NDArray[np.bool_]) -> tuple[dict[tuple[int, int], tuple[int, int]], set[tuple[int, int]]]:
    """The glyphs of a row band (choice 1) as {(start, end) column: (top, bottom) row}, left to right, and
    the ones that could not be isolated. Ink cut by the band's top or bottom is not a glyph of this row."""
    h = ink.shape[0]
    counts = ink.sum(axis=0)
    glyphs: dict[tuple[int, int], tuple[int, int]] = {}
    unsure: set[tuple[int, int]] = set()
    for s, e in _runs(counts > 0):
        rows = np.flatnonzero(ink[:, s:e].any(axis=1))
        if rows[0] == 0 or rows[-1] == h - 1:
            continue
        n = max(1, round((e - s) / (GLYPH_PITCH * (rows[-1] + 1 - rows[0]))))
        cuts, clear = [s], True
        for k in range(1, n):                         # touching glyphs: cut at the ink valley nearest k/n
            want = s + k * (e - s) / n
            half = max(1, int((e - s) / n / 3))
            cols = range(max(cuts[-1] + 1, int(want) - half), min(e - 1, int(want) + half + 1))
            if not cols:
                clear = False
                continue
            c = min(cols, key=lambda c: (counts[c], abs(c + 0.5 - want)))
            clear = clear and counts[c] <= GLYPH_VALLEY * counts[s:e].max()
            cuts.append(c + 1)                        # the valley column ends the left glyph
        cuts.append(e)
        for a, b in zip(cuts, cuts[1:]):
            r = np.flatnonzero(ink[:, a:b].any(axis=1))
            if r.size:
                glyphs[(a, b)] = (int(r[0]), int(r[-1]) + 1)
                if not clear:
                    unsure.add((a, b))
    return glyphs, unsure


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


def _box_crop(image: Image, box: Box) -> Image:
    """A copy of the box's pixels, for a recipe that needs no background (so a whole-frame box works)."""
    x, y, w, h = box
    return image[y:y + h, x:x + w].copy()


def _box_view(image: Image, box: Box, interior: bool = False) -> tuple[Image, NDArray[np.uint8]]:
    """A copy of the box's pixels and its background: the 4 px border, or the interior's dominant color."""
    crop = _box_crop(image, box)
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
    extent, unsure = _glyphs(_ink(band, bg))
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
    if t in unsure:
        raise GenerationError("cannot isolate one glyph of the target's run of touching glyphs")
    tbit, th = band[:, t[0]:t[1]], extent[t][1] - extent[t][0]
    donors = [g for g, (r0, r1) in extent.items() if g != t and g not in unsure and 0.6 * th <= r1 - r0 <= 1.4 * th
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
    crop = _box_crop(image, b)
    c = crop.astype(np.int16)
    colored = (c.max(axis=2) - c.min(axis=2)) > CHROMA
    if not colored.any():
        raise GenerationError("no colored pixels in the target box")
    crop[colored] = crop[colored][:, [1, 0, 2]]          # RGB: red and green trade places
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
        idx = {"red": 0, "green": 1, "blue": 2}[channel]          # RGB
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
    """study_removed (M24): erase the study line of RGB `color` from the chart region by inpainting."""
    b = _check_box(image, box)
    crop = _box_crop(image, b)
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
    crop = _box_crop(image, b)
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
    crop = _box_crop(image, b)
    h, w = crop.shape[:2]
    if w < 60 or h < 40:
        raise GenerationError("body too small for the exception dialog")
    dw, dh = min(w, 320), min(h, 140)
    ox, oy = int(rng.integers(0, w - dw + 1)), int(rng.integers(0, h - dh + 1))
    dlg = np.full((dh, dw, 3), 240, np.uint8)
    bar = max(10, dh // 6)
    dlg[:bar] = (30, 90, 160)                         # RGB: a blue title bar
    dlg[1:bar - 1, 3:dw // 2] = _with_text(dlg[1:bar - 1, 3:dw // 2], title, (255, 255, 255))
    r = max(3, min(10, (dh - bar) // 5))
    cv2.circle(dlg, (6 + r, bar + 6 + r), r, (210, 0, 0), -1)   # RGB: the red error icon
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
    """What `generate` returns: every mutant in catalog order, failures included, and the catalog's sha256 (the
    `catalog_sha256` of the night's `canary_manifests` row, 8.3)."""
    mutants: list[Mutant]
    catalog_sha256: str = ""

    @property
    def manifest(self) -> dict[str, dict[str, Any]]:
        """The sha256 manifest kept for the next night's check (`canary_manifests.manifest_json`, 8.3)."""
        return {m.key: {"base_sha256": m.base_sha256, "mutant_sha256": m.mutant_sha256, "seed": m.seed}
                for m in self.mutants if m.image is not None}

    @property
    def failed(self) -> list[Mutant]:
        return [m for m in self.mutants if m.failed]


def bound_checkpoints(catalog_entries: Iterable[MutantEntry]) -> list[str]:
    """The concrete checkpoints the night's image entries bind, sorted: the synthetic batch that `"*"` (M25,
    SD-43) and the global-shift class (M21, M23, SD-89) expand over, and what a Nightly plans to capture."""
    return sorted({e.checkpoint_id for e in catalog_entries if e.mode == "image" and e.checkpoint_id != "*"})


def check_determinism(manifest: Mapping[str, Mapping[str, Any]], previous: Mapping[str, Mapping[str, Any]] | None,
                      g3_hash: str | None, catalog_sha256: str) -> dict[str, str]:
    """The keys of tonight's manifest that fail 10.3 Determinism, with the reason (choice 6).

    Every key fails when the catalog's sha256 is not the hash the G3 preflight checked (`g3_hash`, 12.7 G3; None
    when the run recorded none). A key whose base sha256 and seed equal last night's but whose mutant sha256
    differs fails as a nondeterministic generator; a changed base regenerates and passes.
    """
    if g3_hash is not None and g3_hash != catalog_sha256:
        return {k: "the catalog failed the G3 preflight hash" for k in manifest}
    out: dict[str, str] = {}
    for k, cur in manifest.items():
        prev = (previous or {}).get(k)
        if prev and prev.get("base_sha256") == cur.get("base_sha256") and prev.get("seed") == cur.get("seed") \
                and prev.get("mutant_sha256") != cur.get("mutant_sha256"):
            out[k] = "nondeterministic generator: same base and seed, different mutant sha256"
    return out


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
             g3_hash: str | None = None) -> Generation:
    """Generate every mutant of the catalog from the approved captures (10.3 Determinism, 12.5).

    `approved_images` maps checkpoint id to the approved RGB capture of the main build; `region_boxes`
    maps checkpoint id to {region id: (x, y, w, h)} located by landmark. An entry's own generator seed
    wins over `seed`. An `aa_control` entry bound to `"*"` (M25) and every `global_shift` entry (M21, M23,
    SD-89) are applied to every checkpoint of `bound_checkpoints`. A missing capture, region or donor, a
    degenerate box, a generator exception, a mutant equal to its base, a sha256 that differs from
    `previous_manifest` (last night's `Generation.manifest`) for the same base and seed, or a catalog whose
    sha256 is not `g3_hash` (the hash the G3 preflight checked, 12.7) leaves that mutant with `error` set:
    `harness_error (reason: canary_generation_failed)`, never skipped (`check_determinism`).
    """
    entries = list(catalog_entries)
    bound = bound_checkpoints(entries)
    catalog_sha = next((e.catalog_version for e in entries), "")
    g3_failed = check_determinism({"catalog": {}}, None, g3_hash, catalog_sha)
    mutants: list[Mutant] = []
    for e in entries:
        for cid in (bound if e.checkpoint_id == "*" or e.defect_class in BATCH_CLASSES else [e.checkpoint_id]):
            m = Mutant(entry=e, checkpoint_id=cid, seed=e.seed if e.seed is not None else seed)
            mutants.append(m)
            if g3_failed:
                m.error = g3_failed["catalog"]
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
            m.image, m.footprint, m.base_sha256, m.mutant_sha256 = image, footprint, image_sha256(base), image_sha256(image)
    gen = Generation(mutants, catalog_sha)
    failures = check_determinism(gen.manifest, previous_manifest, None, catalog_sha)
    for m in mutants:
        if m.key in failures:
            m.error, m.image, m.footprint, m.mutant_sha256 = failures[m.key], None, None, None
    return gen


# -- scoring (10.3 Scoring, 8.3 canary_results) ----------------------------------------------------

@dataclass
class MutantResult:
    """What the path produced for one mutant, as `score` takes it."""
    mutant: Mutant
    produced: CheckpointOutcome | None = None   # the synthetic record from evaluate_checkpoint, or the canary flow's
    absorbed_by: str | None = None              # what swallowed a miss (`absorbed_by`); None: derived from the record
    on_mutated_region: bool | None = None       # None: derived from the record's diff (choice 7)
    step_status: str | None = None              # behavioral: the canary flow's step status (gateway)
    redaction_hit: bool | None = None           # redaction: the stage masked the id and the egress log shows it
    fired_ids: Collection[str] = ()             # signal ids that fired beyond those the record names (choice 7)


@dataclass
class RowContext:
    """Fields for a synthetic row when the path produced no record (a generation failure)."""
    run_id: str
    workspace_id: str
    persona: str
    data_mode: str


@dataclass(frozen=True)
class RowVersions:
    """The version columns of the night's `canary_results` rows (8.3): the mask and threshold versions (the
    calibration file hash) and the pinned model and prompt, which a Reviewer classification overrides per row."""
    mask_version: str | None = None
    threshold_version: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None


@dataclass(frozen=True)
class MaskArea:
    """A mask region of the mutant's checkpoint in frame coordinates: a candidate absorber of a miss (10.3)."""
    mask_id: str
    region_id: str
    rect: Box


@dataclass(frozen=True)
class CalibratedFloor:
    """A calibrated floor on a region of the mutant's checkpoint (3.9 fix list): a candidate absorber (10.3)."""
    region_id: str
    fingerprint: str                            # the calibration fingerprint, as the banner prints it
    floor: float
    rect: Box | None = None
    fix_item: str | None = None
    owner: str | None = None


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
    reviewer_verdict: str | None = None         # the Reviewer's category and confidence on the produced record
    reviewer_confidence: float | None = None
    mask_version: str | None = None
    threshold_version: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None

    def __post_init__(self) -> None:
        load().check_member("canary_states", self.state)
        load().check_member("canary_kinds", self.kind)
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
    """The night's canary result: rows, run state, synthetic outcome rows, banner, signals and coverage."""
    state: str                                  # ok | detector_blind | generation_failed | detector_degraded
    results: list[CanaryResult]
    outcome_rows: list[CheckpointOutcome]       # the synthetic harness_error records that replace a produced row
    synthetic: list[SyntheticOutcome]           # one `synthetic_outcomes` row per mutant that ran or failed (SD-91)
    class_rates: dict[str, float]
    class_floors: dict[str, float]
    degraded_classes: list[str]
    banner: str | None                          # the morning message with the `since` given to `score`
    page: str | None                            # P1 for blind and generation_failed, P2 for degraded (9.10)
    rc_signoff_blocked: bool
    no_canary_ran: bool                         # the run is then aborted_harness (reason: canary_generation_failed)
    fired: dict[str, tuple[str, ...]] = field(default_factory=dict)   # mutant key: every signal id that fired
    coverage_gaps: list[str] = field(default_factory=list)            # the morning report's coverage-gap lines
    generation_reason: str | None = None        # why the run state is generation_failed (SD-88)
    generation_errors: dict[str, str] = field(default_factory=dict)   # mutant key: why it was not generated
    unproven: dict[str, str] = field(default_factory=dict)            # mutant key: the expected signal that did not fire
    banner_body: tuple[str, ...] = ()           # the banner after its first line (`banner_for`)

    @property
    def detector_state(self) -> str:
        """The mutants' canary state for the Nightly run's summary_json (`persist` writes `system_state`)."""
        return self.state

    def banner_for(self, since: str | None) -> str | None:
        """The morning message with its start time `since` (10.3); None when the state is ok."""
        return _banner(self.state, since, self.banner_body)


@dataclass
class _Graded:
    r: MutantResult
    hit: bool | None                            # None: not generated (or the behavioral flow did not run)
    error: str | None = None
    fired: frozenset[str] = frozenset()
    signal: str | None = None                   # why the produced outcome did not count (choice 7)
    unproven: str | None = None                 # detected, but the expected signal did not fire (choice 7)


def rolling_floor(history: Sequence[float], phase2_exited: bool = False) -> float:
    """The class floor of 10.3: min(14-night median - 5 points, 90 percent), at least 95 percent after Phase 2."""
    recent = list(history)[-FLOOR_WINDOW:]
    floor = min(statistics.median(recent) - FLOOR_MARGIN, FLOOR_CAP) if recent else FLOOR_CAP
    return max(floor, FLOOR_TARGET) if phase2_exited else floor


def fired_ids(record: CheckpointOutcome) -> frozenset[str]:
    """Every invariant id and assertion id the record shows firing: `invariant_ids`, `assertion_ids`, its reason
    and the reasons of its lower-precedence results (9.1), so a signal hidden by precedence still counts."""
    ids = set(record.invariant_ids or ()) | set(record.assertion_ids or ())
    pairs = [(record.outcome, record.reason)] + [(x["outcome"], x.get("reason")) for x in record.lower_results or ()]
    for outcome, reason in pairs:
        if outcome == "invariant_violation" and reason:
            ids.add(reason)
        elif outcome == "assertion_failed" and reason and reason.startswith("assertion:"):
            ids.add(reason.split(":", 1)[1])
    return frozenset(ids)


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


def _missing_signal(e: Expected, p: CheckpointOutcome, fired: frozenset[str]) -> str | None:
    """Why an outcome in the expected list does not prove the class (choice 7): the expected invariant or assertion
    did not fire, or the record's reason is not the expected reason."""
    role = ROLE_FOR_ID.get(p.outcome)
    want = getattr(e, role) if role else None
    if want is not None and want not in fired:
        return f"signal: expected {want} did not fire (fired {', '.join(sorted(fired)) or 'none'})"
    if e.reason is not None and load().reason_allowed(p.outcome, e.reason) and p.reason != e.reason:
        return f"signal: expected reason {e.reason}, produced {p.reason or 'none'}"
    return None


def _batch_verdicts(results: Sequence[MutantResult]) -> dict[str, tuple[bool, str | None]]:
    """SD-89: a global-shift mutant is one judgment over its batch: detected when every generated member ended
    `needs_human (reason: global_shift)`, the one cluster of the whole-window pre-pass of 3.7."""
    members: dict[str, list[MutantResult]] = {}
    for r in results:
        if r.mutant.defect_class in BATCH_CLASSES and not r.mutant.failed and r.produced is not None:
            members.setdefault(r.mutant.mutant_id, []).append(r)
    out: dict[str, tuple[bool, str | None]] = {}
    for mid, rs in members.items():
        outside = [r for r in rs if r.produced is not None
                   and (r.produced.outcome, r.produced.reason) != ("needs_human", "global_shift")]
        first = outside[0].produced if outside else None
        out[mid] = (not outside, None if first is None else
                    f"global shift pre-pass: {len(outside)} of {len(rs)} batch checkpoints outside the global_shift "
                    f"cluster (first {first.checkpoint_id}: {first.outcome})")
    return out


def _detected(m: Mutant, r: MutantResult, fired: frozenset[str], batch: Mapping[str, tuple[bool, str | None]],
              strict: bool) -> tuple[bool, str | None, str | None]:
    """(detected, why it missed, the expected signal that did not fire although the outcome is in the list)."""
    e = m.entry.expected
    if m.defect_class == "redaction":
        return bool(r.redaction_hit), None if r.redaction_hit else "redaction: no hit on the planted identifier", None
    if m.defect_class == "gateway":
        want = e.step_status or "blocked_by_gateway"
        return r.step_status == want, None if r.step_status == want else \
            f"gateway: step {r.step_status or 'without a status'}, expected {want}", None
    if m.defect_class in BATCH_CLASSES:
        return (*batch[m.mutant_id], None)
    assert r.produced is not None
    outcome, cls = r.produced.outcome, r.produced.classification
    floor = e.min_confidence
    if floor is not None and cls is not None and cls.confidence is not None and cls.confidence < floor:
        return False, f"signal: confidence {cls.confidence:g} below min_confidence {floor:g}", None
    signal = None
    if outcome in e.outcomes:
        signal = _missing_signal(e, r.produced, fired)
        if signal is None or not strict:
            return True, None, signal
    if m.defect_class == "sub_floor":
        return outcome in ABSORBED_OUTCOMES, signal, None     # absorbed below threshold; any finding is the miss
    if m.defect_class not in CONTROL_CLASSES and outcome in ("collateral", "needs_human") and _on_region(m, r):
        return True, None, None
    return False, signal, None


def _check_result(r: MutantResult) -> None:
    m, p = r.mutant, r.produced
    if r.step_status is not None:
        load().check_step(r.step_status)
    if p is None:
        want = m.entry.expected.step_status or "blocked_by_gateway"
        if not (m.defect_class == "gateway" and r.step_status == want):
            raise ValueError(f"{m.key}: a generated mutant needs the outcome the path produced (a gateway mutant may "
                             "omit it only when its step was blocked)")
        return
    if not p.synthetic:
        raise ValueError(f"{m.key}: canary records carry synthetic: true (10.3)")
    if p.checkpoint_id != m.checkpoint_id:
        raise ValueError(f"{m.key}: produced record is for {p.checkpoint_id}")


def _grade(r: MutantResult, batch: Mapping[str, tuple[bool, str | None]], strict: bool) -> _Graded:
    m = r.mutant
    if m.failed:
        return _Graded(r, None, m.error)
    if m.entry.mode == "behavioral" and r.produced is None and r.step_status is None:
        return _Graded(r, None, f"canary flow {m.entry.canary_flow} did not run")     # choice 5
    _check_result(r)
    fired = (fired_ids(r.produced) if r.produced is not None else frozenset()) | frozenset(r.fired_ids)
    hit, signal, unproven = _detected(m, r, fired, batch, strict)
    return _Graded(r, hit, None, fired, signal, unproven)


def absorbed_by(mutant: Mutant, produced: CheckpointOutcome | None, *, masks: Iterable[MaskArea] = (),
                floors: Iterable[CalibratedFloor] = ()) -> str | None:
    """What swallowed a missed mutant, as its row and the banner name it (10.3 Scoring, choice 12).

    A `decision:<id>` reason names the decision; a mask of `masks` that meets the mutant's footprint (or is its
    `region_ref`) names the mask; a non-zero floor of `floors` on such a region names the threshold with its
    calibration fingerprint, fix-list item and owner; a `landmark_missing` record names the landmark; an
    `intended` record names its manifest entry; and for the controls, which miss by over-firing, the finding.
    None when nothing is known (the banner prints "not recorded")."""
    if produced is None:
        return None
    ref, fp = mutant.entry.region_ref, mutant.footprint

    def meets(region_id: str, rect: Box | None) -> bool:
        return (ref is not None and region_id == ref) or (fp is not None and rect is not None and _intersects(rect, fp))

    reason = produced.reason or ""
    parts: list[str] = []
    if reason.startswith("decision:"):
        parts.append(f"decision: {produced.decision_ref or reason.split(':', 1)[1]}")
    parts += [f"mask {k.mask_id} (region {k.region_id})" for k in masks if meets(k.region_id, k.rect)]
    for f in floors:
        if f.floor and meets(f.region_id, f.rect):
            fix = ", ".join(x for x in (f"fix-list item {f.fix_item}" if f.fix_item else "",
                                        f"owner {f.owner}" if f.owner else "") if x)
            parts.append(f"threshold: calibration for fingerprint {f.fingerprint} carries a non-zero floor on "
                         f"{f.region_id}" + (f" ({fix})" if fix else ""))
    if produced.outcome == "landmark_missing" and mutant.defect_class != "landmark_loss":
        parts.append(f"landmark: {reason.split(':', 1)[-1] if reason else 'not located'} not located")
    if produced.outcome == "intended" and not reason.startswith("decision:"):
        parts.append(f"intended: manifest entry {produced.manifest_entry_ref}" if produced.manifest_entry_ref
                     else "intended: Reviewer classification")
    if mutant.defect_class in CONTROL_CLASSES and produced.outcome not in ABSORBED_OUTCOMES:
        parts.append(f"finding: {produced.outcome}" + (f" ({reason})" if reason else ""))
    return "; ".join(parts) or None


def _absorbed_text(g: _Graded) -> str | None:
    base = g.r.absorbed_by if g.r.absorbed_by is not None else absorbed_by(g.r.mutant, g.r.produced)
    return "; ".join(x for x in (g.signal, base) if x) or None


def _harness_row(g: _Graded, reason: str, context: RowContext | None, watchdog_event: str | None) -> CheckpointOutcome:
    m, p = g.r.mutant, g.r.produced
    produced = f"; produced {p.outcome}" + (f" ({p.reason})" if p.reason else "") if p else ""
    detail = f"mutant {m.mutant_id} {m.defect_class}{produced}" + (f": {g.error}" if g.error else "")
    if watchdog_event:
        detail = f"watchdog_event: {watchdog_event}; {detail}"      # 4.13 harness_error required field
    if p is not None:
        return CheckpointOutcome(run_id=p.run_id, checkpoint_id=p.checkpoint_id, outcome="harness_error",
                                 reason=reason, detail=detail, workspace_id=p.workspace_id, persona=p.persona,
                                 data_mode=p.data_mode, surface_id=p.surface_id, test_kind=p.test_kind,
                                 tier=p.tier, flow_id=p.flow_id, step_id=p.step_id, size_step=p.size_step,
                                 artifacts=p.artifacts, synthetic=True, mutant_id=m.mutant_id)
    if context is None:
        raise ValueError(f"{m.key}: a generation failure needs a RowContext for its synthetic row")
    return CheckpointOutcome(run_id=context.run_id, checkpoint_id=m.checkpoint_id, outcome="harness_error",
                             reason=reason, detail=detail, workspace_id=context.workspace_id,
                             persona=context.persona, data_mode=context.data_mode, synthetic=True,
                             mutant_id=m.mutant_id)


def _synthetic_row(m: Mutant, src: CheckpointOutcome | None) -> SyntheticOutcome | None:
    """The mutant's `synthetic_outcomes` row (SD-91): its harness_error row when one replaces the produced
    record, else the produced record; none for a blocked gateway step that left no record."""
    if src is None:
        return None
    return SyntheticOutcome(run_id=src.run_id, mutant_id=m.mutant_id, checkpoint_id=m.checkpoint_id,
                            outcome=src.outcome, reason=src.reason, detail=src.detail, gate=src.gate,
                            mutant_sha256=m.mutant_sha256, recorded_at=src.recorded_at)


def _versions(p: CheckpointOutcome | None, v: RowVersions) -> dict[str, Any]:
    rev = p.classification if p is not None and p.classification is not None and p.classification.by == "reviewer" else None
    return {"reviewer_verdict": rev.category if rev else None, "reviewer_confidence": rev.confidence if rev else None,
            "mask_version": v.mask_version, "threshold_version": v.threshold_version,
            "model_id": (rev.model_id if rev and rev.model_id else v.model_id),
            "prompt_hash": (rev.prompt_hash if rev and rev.prompt_hash else v.prompt_hash)}


def _coverage_gaps(generated: Sequence[_Graded], checkpoints: Iterable[str]) -> list[str]:
    """10.3 Gate and governance and L-MUT-05 counted over the generated mutants (SD-42): a checkpoint with fewer
    than 20 live mutants, or without one of the six brief classes, is a coverage-gap line (choice 13); a mutant
    detected without its expected signal is a proof-gap line (12.3, choice 7)."""
    live: dict[str, set[str]] = {}
    count: dict[str, int] = {}
    for g in generated:
        cid = g.r.mutant.checkpoint_id
        count[cid] = count.get(cid, 0) + 1
        live.setdefault(cid, set()).add(g.r.mutant.defect_class)
    out = []
    for cid in sorted(set(checkpoints)):
        n = count.get(cid, 0)
        if n < MIN_LIVE_MUTANTS:
            out.append(f"coverage gap: {cid} has {n} live mutant{'' if n == 1 else 's'}, fewer than "
                       f"{MIN_LIVE_MUTANTS} (10.3)")
        missing = [c for c in BRIEF_CLASSES if c not in live.get(cid, set())]
        if missing:
            out.append(f"coverage gap: {cid} lacks the brief classes {', '.join(missing)} (L-MUT-05)")
    out += [f"proof gap: {g.r.mutant.mutant_id} {g.r.mutant.defect_class} on {g.r.mutant.checkpoint_id} was detected "
            f"but its {g.unproven.removeprefix('signal: ')} (12.3)" for g in generated if g.unproven]
    return out


def score(results: Sequence[MutantResult], *, history: Mapping[str, Sequence[float]] | None = None,
          phase2_exited: bool = False, since: str | None = None, context: RowContext | None = None,
          behavioral_flows_started: bool | None = None, critical_checkpoints: Iterable[str] = (),
          tier1_checkpoints: Iterable[str] = (), versions: RowVersions | None = None,
          watchdog_event: str | None = None, strict_signals: bool = False) -> CanaryReport:
    """Score one night's mutants (10.3 Scoring, F31, F58, SD-88, SD-89).

    Money-cell mutants need 100 percent: a miss is `detector_blind` and its synthetic row is
    `harness_error (reason: detector_blind)`. Other mutants count toward their class rate against
    `rolling_floor(history[class])`; a miss in a class below the floor is `detector_degraded` with the
    row `harness_error (reason: detector_degraded)`, otherwise `missed` and the produced row stands.
    A generation failure, or a behavioral mutant whose canary flow did not run, is `generation_failed` with
    `harness_error (reason: canary_generation_failed)` on its row only. The run state is `generation_failed`
    when nothing was generated or a checkpoint of `critical_checkpoints` has no generated mutant, otherwise the
    worst of `detector_blind` and `detector_degraded`, else `ok` (SD-88); `no_canary_ran` (the run is then
    `aborted_harness`) when nothing was generated and no behavioral flow started (`behavioral_flows_started`;
    None derives it from the results). `history` maps class to its previous nightly rates (oldest first,
    `class_history`); `since` is the banner's start time as the Reporter prints it (for example
    "2026-11-09 03:12 ET"); `versions` fills the version columns; `watchdog_event` is the run's Watchdog event
    id that every synthetic `harness_error` names in its detail (4.13). `critical_checkpoints` and
    `tier1_checkpoints` (with an approved baseline) give the coverage-gap lines. A produced outcome in the
    expected list whose expected invariant, assertion or reason did not fire is detected (10.3) and listed in
    `unproven` and as a proof-gap line (12.3); with `strict_signals` it is a miss instead (choice 7).
    """
    history = history or {}
    batch = _batch_verdicts(results)
    graded = [_grade(r, batch, strict_signals) for r in results]
    per_class: dict[str, list[bool]] = {}
    for g in graded:
        if g.hit is not None and not g.r.mutant.money_cell:
            per_class.setdefault(g.r.mutant.defect_class, []).append(g.hit)
    rates = {c: sum(v) / len(v) for c, v in per_class.items()}
    floors = {c: rolling_floor(history.get(c, ()), phase2_exited) for c in per_class}
    below = sorted(c for c in rates if rates[c] < floors[c])

    rows: list[CanaryResult] = []
    outcome_rows: list[CheckpointOutcome] = []
    synthetic: list[SyntheticOutcome] = []
    blind: list[CanaryResult] = []
    redaction_blind: list[CanaryResult] = []
    degraded: list[CanaryResult] = []
    failed: list[_Graded] = []
    v = versions or RowVersions()
    for g in graded:
        m, p = g.r.mutant, g.r.produced
        if g.hit is None:
            state, reason = "generation_failed", "canary_generation_failed"
            failed.append(g)
        elif g.hit:
            state, reason = "detected", None
        elif m.money_cell:
            state, reason = "detector_blind", "detector_blind"
        elif m.defect_class in below:
            state, reason = "detector_degraded", "detector_degraded"
        else:
            state, reason = "missed", None
        if g.hit is False and m.defect_class == "redaction":
            reason = "safety_canary_blind:redaction"
        row = CanaryResult(
            mutant_id=m.mutant_id, catalog_version=m.entry.catalog_version, checkpoint_id=m.checkpoint_id,
            defect_class=m.defect_class, money_cell=m.money_cell, seed=m.seed,
            expected=list(m.entry.expected.outcomes), state=state, base_sha256=m.base_sha256,
            mutant_sha256=m.mutant_sha256, produced_outcome=p.outcome if p else None,
            produced_reason=p.reason if p else None, absorbed_by=None if g.hit is not False else _absorbed_text(g),
            class_rate=None if m.money_cell else rates.get(m.defect_class),
            class_floor=None if m.money_cell else floors.get(m.defect_class), **_versions(p, v),
        )
        rows.append(row)
        harness = _harness_row(g, reason, context, watchdog_event) if reason is not None else None
        if harness is not None:
            outcome_rows.append(harness)
        so = _synthetic_row(m, harness or p)
        if so is not None:
            synthetic.append(so)
        if state == "detector_blind":
            blind.append(row)
        if reason == "safety_canary_blind:redaction":
            redaction_blind.append(row)
        elif state == "detector_degraded":
            degraded.append(row)

    generated = [g for g in graded if g.hit is not None]
    critical = sorted(set(critical_checkpoints))
    empty = [c for c in critical if not any(g.r.mutant.checkpoint_id == c for g in generated)]
    gen_reason = ("the stage generated no mutants" if not generated else
                  f"no generated mutant for critical checkpoint {', '.join(empty)}" if empty else None)
    state = ("generation_failed" if gen_reason else "detector_blind" if blind or redaction_blind else
             "detector_degraded" if degraded else "ok")
    flows = behavioral_flows_started if behavioral_flows_started is not None else \
        any(g.r.mutant.entry.mode == "behavioral" for g in generated)
    money_total = sum(1 for g in generated if g.r.mutant.money_cell)
    labels = {g.r.mutant.mutant_id: g.r.mutant.entry.label for g in graded}
    body = _banner_body(state, gen_reason, labels, money_total, blind, redaction_blind, failed, below, rates, floors,
                        degraded)
    return CanaryReport(
        state=state, results=rows, outcome_rows=outcome_rows, synthetic=synthetic, class_rates=rates,
        class_floors=floors, degraded_classes=below, banner=_banner(state, since, body),
        page={"detector_blind": "P1", "generation_failed": "P1", "detector_degraded": "P2"}.get(state),
        rc_signoff_blocked=state != "ok", no_canary_ran=not generated and not flows,
        fired={g.r.mutant.key: tuple(sorted(g.fired)) for g in generated},
        coverage_gaps=_coverage_gaps(generated, set(critical) | set(tier1_checkpoints)), generation_reason=gen_reason,
        unproven={g.r.mutant.key: g.unproven for g in generated if g.unproven},
        generation_errors={g.r.mutant.key: g.error or "" for g in failed}, banner_body=body,
    )


def _banner(state: str, since: str | None, body: Sequence[str]) -> str | None:
    """The morning message of 10.3 (choice 10): the state's first line with its start time, then `body`."""
    if state == "ok":
        return None
    head = "DETECTOR BLIND" if state in ("detector_blind", "generation_failed") else "DETECTOR DEGRADED"
    return "\n".join([f"{head} since {since}." if since else f"{head}.", *body])


def _banner_body(state: str, gen_reason: str | None, labels: Mapping[str, str], money_total: int,
                 blind: list[CanaryResult], redaction_blind: list[CanaryResult], failed: list[_Graded],
                 below: list[str], rates: Mapping[str, float], floors: Mapping[str, float],
                 degraded: list[CanaryResult]) -> tuple[str, ...]:
    """The banner's lines after the first (choice 10)."""
    if state == "ok":
        return ()
    lines: list[str] = []
    for i, c in enumerate(blind):
        lead = f"Missed {len(blind)} of {money_total} money-cell mutants: " if i == 0 else "Also missed: "
        lines += [f"{lead}{c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}",
                  f"  (checkpoint {c.checkpoint_id}).", f"Absorbed by: {c.absorbed_by or 'not recorded'}."]
    for c in redaction_blind:
        lines += [f"Redaction canary blind: {c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}",
                  f"  (checkpoint {c.checkpoint_id})."]
    if state == "generation_failed":
        lines += ["canaries did not run", f"  {gen_reason}."]
        lines += [f"  {g.r.mutant.mutant_id} {g.r.mutant.defect_class} (checkpoint {g.r.mutant.checkpoint_id}): "
                  f"{g.error}." for g in failed]
    if below:
        lines.append("Classes below their rolling floor: "
                     + ", ".join(f"{c} {rates[c]:.0%} (floor {floors[c]:.0%})" for c in below) + ".")
        lines += [f"  Missed {c.mutant_id} {c.defect_class}, {labels[c.mutant_id]}"
                  f" (checkpoint {c.checkpoint_id}); absorbed by: {c.absorbed_by or 'not recorded'}." for c in degraded]
    lines += BLIND_TAIL if state in ("detector_blind", "generation_failed") else BLIND_TAIL[1:]
    return tuple(lines)


# -- the Nightly's records (8.3 canary_results, synthetic_outcomes, canary_manifests, system_state; 9.2) ------------

@dataclass(frozen=True)
class DetectorState:
    """`system_state.detector_state` (8.3, 9.2): the banner state of the last Nightly run, which every later
    report reads until a green Nightly clears it (choice 14)."""
    state: str                                  # ok | detector_blind | generation_failed | detector_degraded
    run_id: str                                 # the Nightly run that wrote it
    since: str | None = None                    # UTC time the state began, kept while it stays the same
    since_text: str | None = None               # the same time as the banner prints it ("2026-11-09 03:12 ET")
    banner: str | None = None
    canary_state: str = "ok"                    # the mutants' own state (`CanaryReport.state`)
    divergence: bool = False                    # the path-equivalence pair did not reproduce (canary_path_divergence)
    classes: tuple[str, ...] = ()               # the degraded classes
    health: Mapping[str, Any] = field(default_factory=dict)    # the run-health Canary row (9.2, `health_row`)

    def __post_init__(self) -> None:
        for s in (self.state, self.canary_state):
            if s not in ("ok", "detector_blind", "generation_failed", "detector_degraded"):
                raise TaxonomyError(f"detector state {s!r} (10.3)")

    def to_json(self) -> str:
        return json.dumps({**dataclasses.asdict(self), "classes": list(self.classes), "health": dict(self.health)},
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "DetectorState":
        d = json.loads(text)
        return cls(**{**d, "classes": tuple(d.get("classes", ()))})


def health_row(report: CanaryReport, *, date: str, since_text: str | None, state: str | None = None) -> dict[str, Any]:
    """The run-health Canary row of 9.2 as the report model reads it: money-cell and other detection counts,
    the detector state (`state`, else the mutants' own), the date, the banner's start time and the degraded
    classes (`detector_degraded: {class}`)."""
    money = [r for r in report.results if r.money_cell and r.state != "generation_failed"]
    other = [r for r in report.results if not r.money_cell and r.state != "generation_failed"]
    return {"money_detected": sum(r.state == "detected" for r in money), "money_total": len(money),
            "other_detected": sum(r.state == "detected" for r in other), "other_total": len(other),
            "detector_state": state or report.state, "date": date, "since": since_text,
            "classes": list(report.degraded_classes)}


def read_detector_state(store: Store) -> DetectorState | None:
    """The detector state the last Nightly wrote (`system_state.detector_state`); None before any. PR and merge
    runs build their banner (`DetectorState.banner`) and run-health Canary row (`DetectorState.health`) from it."""
    text = store.get_system_state("detector_state")
    return None if text is None else DetectorState.from_json(text)


def _path_lines(rows: Sequence[CanaryResult]) -> list[str]:
    return [f"Path equivalence: the merge-run pair {r.checkpoint_id} of {r.mutant_id} did not reproduce through the "
            f"canary entry point: harness_error (reason: canary_path_divergence); {r.absorbed_by or 'no detail'}."
            for r in rows if r.kind == "path_equivalence" and r.state == "detector_blind"]


def persist(store: Store, run_id: str, report: CanaryReport, *, since_at: str, since_text: str, date: str,
            generation: Generation | None = None, extra_rows: Sequence[CanaryResult] = (),
            at: str | None = None) -> DetectorState:
    """Write one Nightly run's canary pass to the store and return the detector state it set (8.3, 9.2, 10.3).

    Writes the `canary_results` rows of `report` and `extra_rows` (the path-equivalence row of
    `canary_paths.path_equivalence`, and the safety canary and drill rows), one `synthetic_outcomes` row per
    mutant, linked from its result row (SD-91), the night's manifest when `generation` is given
    (`canary_manifests`, read back by `previous_manifest`), `system_state.detector_state` and the `canary` key of
    the run's summary_json. The run must be open and the mutants' checkpoints registered. The detector state is
    the mutants' state, raised to `detector_blind` by a blind extra row (a diverged pair, a blind safety canary or
    drill item: the same banner, 10.3); its start time is the previous state's while the state is unchanged,
    else `since_at` (UTC) and `since_text` (as the banner prints it); `ok` clears it. `date` is the run-health
    date; `at` the write time of every row.
    """
    store.write_canary_results(run_id, [*report.results, *extra_rows], created_at=at)
    for so in report.synthetic:
        store.write_synthetic_outcome(so if at is None else dataclasses.replace(so, recorded_at=at))
    if generation is not None:
        store.write_canary_manifest(run_id, catalog_sha256=generation.catalog_sha256, manifest=generation.manifest,
                                    created_at=at)
    divergence = any(r.kind == "path_equivalence" and r.state == "detector_blind" for r in extra_rows)
    state = report.state
    if state in ("ok", "detector_degraded") and any(r.state == "detector_blind" for r in extra_rows):
        state = "detector_blind"
    previous = read_detector_state(store)
    keep = previous is not None and previous.state == state != "ok"
    since, text = (previous.since, previous.since_text) if keep and previous else (since_at, since_text)
    banner = None
    if state != "ok":
        tail = BLIND_TAIL if state in ("detector_blind", "generation_failed") else BLIND_TAIL[1:]
        body = [x for x in report.banner_body if x not in BLIND_TAIL] + _path_lines(extra_rows) + list(tail)
        banner = _banner(state, text, body)
    ds = DetectorState(state, run_id, None if state == "ok" else since, None if state == "ok" else text, banner,
                       report.state, divergence, tuple(report.degraded_classes),
                       health_row(report, date=date, since_text=None if state == "ok" else text, state=state))
    store.set_system_state("detector_state", ds.to_json(), at=at)
    store.update_summary(run_id, {"canary": {
        "detector_state": state, "canary_state": report.state, "since": ds.since, "since_text": ds.since_text,
        "path_divergence": divergence, "class_rates": report.class_rates, "class_floors": report.class_floors,
        "degraded_classes": report.degraded_classes, "coverage_gaps": report.coverage_gaps,
        "generation_reason": report.generation_reason, "no_canary_ran": report.no_canary_ran}})
    return ds


def class_history(store: Store, *, exclude_run_id: str | None = None, nights: int = FLOOR_WINDOW) -> dict[str, list[float]]:
    """Each class's nightly detection rates from the stored `canary_results` of earlier Nightly runs, oldest
    first, at most the last `nights` (the 14-night window of the rolling floor, 10.3)."""
    rows = store.query(
        "SELECT c.run_id, c.defect_class, max(c.class_rate) AS rate FROM canary_results c JOIN runs r USING (run_id) "
        "WHERE r.run_type = 'nightly' AND c.kind = 'mutant' AND c.money_cell = 0 AND c.class_rate IS NOT NULL "
        "AND c.run_id IS NOT ? GROUP BY c.run_id, c.defect_class ORDER BY r.started_at, c.run_id", (exclude_run_id,))
    out: dict[str, list[float]] = {}
    for row in rows:
        out.setdefault(row["defect_class"], []).append(float(row["rate"]))
    return {c: rates[-nights:] for c, rates in out.items()}


def previous_manifest(store: Store, *, exclude_run_id: str | None = None) -> dict[str, dict[str, Any]] | None:
    """The manifest of the latest earlier Nightly run (`canary_manifests`, 8.3), for `generate`'s night-to-night
    sha256 check (10.3 Determinism); None before any."""
    rows = store.query(
        "SELECT m.manifest_json FROM canary_manifests m JOIN runs r USING (run_id) WHERE r.run_type = 'nightly' "
        "AND m.run_id IS NOT ? ORDER BY r.started_at DESC, m.created_at DESC LIMIT 1", (exclude_run_id,))
    return json.loads(rows[0][0]) if rows else None


def findings(report: CanaryReport, run_id: str, *, created_at: str) -> list[Finding]:
    """The night's canary findings (4.13): a `canary_miss` for every missed mutant whose produced outcome is
    `identical`, `noise` or a decision-suppressed row, and a `harness_error` for every mutant that could not be
    generated (its synthetic row is `harness_error (reason: canary_generation_failed)`)."""
    out = []
    for r in report.results:
        if r.kind != "mutant":
            continue
        key = f"{r.mutant_id}@{r.checkpoint_id}"
        fid = f"fnd-{run_id}-canary-{r.mutant_id}-{hashlib.sha256(r.checkpoint_id.encode()).hexdigest()[:12]}"
        raw = {"state": r.state, "money_cell": str(r.money_cell).lower(), "mutant_id": r.mutant_id}
        if r.state == "generation_failed":
            out.append(Finding(fid, run_id, "harness_error", f"canary mutant {r.mutant_id} {r.defect_class} was not "
                               f"generated: {report.generation_errors.get(key) or 'no reason recorded'}", run_id,
                               checkpoint_id=r.checkpoint_id, evidence={"raw_values": raw}, created_at=created_at))
        elif r.state != "detected" and (r.produced_outcome in ABSORBED_OUTCOMES
                                        or (r.produced_reason or "").startswith("decision:")):
            produced = r.produced_outcome + (f" ({r.produced_reason})" if r.produced_reason else "")
            out.append(Finding(fid, run_id, "canary_miss", f"{r.mutant_id} {r.defect_class} {r.state}: produced "
                               f"{produced}; absorbed by {r.absorbed_by or 'not recorded'}", run_id,
                               checkpoint_id=r.checkpoint_id, evidence={"raw_values": raw}, created_at=created_at))
    return out
