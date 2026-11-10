"""The Invariant Checker: the layout invariant catalog INV-01 to INV-15 (spec 2.7, 12.3, 5.9).

Invariants are baseline-free rules about bounds, text, labels and colors. They run on every
capture of both builds, masks never apply to them, and the Reviewer never reclassifies them.

The input is a `ScreenModel`: what one settled frame looks like to the bounds source chosen by
DR-01. Under Branch A it carries the JAB tree (role, accessible name, rect, showing state); under
Branch B it carries template-located controls, panels segmented by border lines and OCR word
boxes (3x upscale, confidence floor 70). Containers marked `opaque` under Branch A expose no
children, so their interior falls to the Branch B rules (2.7 common rules, Bounds).

Pipeline:

1. `check(model, rules)` evaluates every enabled invariant on one frame.
2. `persist(frames)` keeps a violation only when it holds on all 3 settled frames; the rest are
   discarded and returned for flake statistics (2.7 Persistence).
3. `compare(base_frames, candidate_frames, rules)` runs 1 and 2 on both builds, marks a violation
   present on both builds as pre-existing at one severity level lower, and gives candidate-only
   violations full severity (2.7 Pre-existing, Severity).
4. `confirm(comparison, recapture)` applies the one confirming re-capture of 3.10.
5. `enablement(pairs, rules)` is the A/A enablement test of 2.7 and F46; `invariants_on_aa` finds
   an enabled invariant firing on a Nightly A/A pair (`harness_error (reason: invariant_on_aa)`).

Choices where the spec is ambiguous (each cites the section it reads):

- Capture frame (3.5, 5.17). 3.5 names the first frame of the settled triple as the capture and
  5.17 the third; the frames are identical outside masks, so evidence is taken from the first.
- Severity order (2.7 Severity, Pre-existing). The tier-1 bump is applied first and the
  pre-existing lowering second, so a pre-existing violation is always one level below the same
  violation candidate-only (tier-1 S1 stays S1 when new, S2 when pre-existing). S3 is the floor.
- Violation identity. A violation is keyed by (invariant id, subject), where the subject is the
  build-stable name of what fired (component names, word anchor, label text, region id). The
  pre-existing rule and the 3-frame rule match on this key; word boxes without an `anchor` fall
  back to text and position, which is not stable across a layout shift.
- INV-02 viewports. 2.7 says both "parent not a viewport with a showing scrollbar" (Branch A) and
  "viewports excluded unless INV-06 fires" (controls). A child outside a viewport is excluded only
  when that viewport shows a scrollbar and INV-06 did not fire on it.
- INV-03 Branch A OCR cross-check. The metrics test fires unless OCR inside the component reads
  the whole accessible name. A word box with no word at the same anchor on the other build cannot
  be compared, so only the declared-label prefix test applies to it.
- INV-03 and INV-04 on `masked_live` (2.7, F46). The base comparison is never used there. Inside
  a `rule` region the region's rule decides. Outside every rule region INV-03 keeps only the
  declared-label prefix test and INV-04 does not fire, since neither a base nor a rule exists.
  A `text` rule admits an ellipsis when it has no pattern or its pattern matches the token;
  numeric rules admit none.
- INV-04 needs a word at the same anchor on both builds (its "same anchor on both builds"
  control); under Branch A it is suppressed when the base's displayed text already ended in an
  ellipsis.
- INV-05 Branch B matches a label against a run of words on one line, joined by single spaces,
  within one edit (Levenshtein distance at most 1).
- INV-06. An allowed-scrollbar entry gives the size at and above which no scrollbar is expected;
  the model's `size` is compared against it. A scrollbar in an unlisted container fires.
- INV-07. Without declared text regions the whole capture is one region. Branch B uses a word's
  `cap_height` and falls back to its box height.
- INV-08. The placeholder set is 2.7's; "empty-grid strings" are declared per surface and default
  to the registry example's "No data" (4.5).
- INV-10 counts template-located controls at or above `template_floor` (0.92, the 3.7 default).
- INV-11 Branch B allowlists a word pair when the components that contain the two words are an
  allowlisted pair. An allowlist entry may use "*" for "any component".
- INV-12. A cell is aligned when its edge is within 2 px of the column's median edge; the header
  spans the cells when it covers their horizontal extent within the same 2 px.
- INV-13 fires when the observed focus sequence does not start with the declared sequence.
- INV-15. A cell's color is classed as the nearest pinned palette entry within
  `palette_tolerance`; a nonzero value fires when that class is not its sign's class. A color far
  from every palette entry is not judged.
- Bounds unreadable (2.7 Bounds source failure). Elements whose bounds could not be read are
  returned per frame and unioned across frames, so the checkpoint is `needs_human (reason:
  bounds_unreadable)` unless a higher-precedence outcome applies (3.11 row 9a).
- Re-capture (3.10). The re-capture is stable when at least one first-capture violation fires
  again; violations that do not reproduce are kept as evidence. None reproducing is `unstable`.
- Missing configuration fails loudly, never silently off (2.7): an error-template window with no
  error title patterns, a signed-column value with no palette, or windows with no virtual screen
  raise `ValueError`.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations

INVARIANT_IDS: tuple[str, ...] = tuple(f"INV-{i:02d}" for i in range(1, 16))

#: Catalog severity of each invariant (2.7, column Sev).
SEVERITY: dict[str, str] = {
    "INV-01": "S1", "INV-02": "S1", "INV-03": "S1", "INV-04": "S2", "INV-05": "S1",
    "INV-06": "S2", "INV-07": "S2", "INV-08": "S1", "INV-09": "S2", "INV-10": "S1",
    "INV-11": "S1", "INV-12": "S2", "INV-13": "S2", "INV-14": "S1", "INV-15": "S1",
}

#: Statement of each invariant (2.7, column Statement).
STATEMENTS: dict[str, str] = {
    "INV-01": "No two sibling components' bounds intersect",
    "INV-02": "No component extends outside its parent",
    "INV-03": "No text clipped at a boundary",
    "INV-04": "No ellipsis where the base had none",
    "INV-05": "Required labels present",
    "INV-06": "No unexpected scrollbar",
    "INV-07": "Minimum legible font height",
    "INV-08": "No placeholder, loading or error text in a value region",
    "INV-09": "Every top-level window is inside the virtual screen; every owned dialog inside its "
              "owner's screen; main frame rect unchanged since run start",
    "INV-10": "Every required control present exactly once",
    "INV-11": "No two text runs overlap",
    "INV-12": "Grid columns aligned",
    "INV-13": "Focus traversal covers declared controls in order (ticket, confirm dialog)",
    "INV-14": "No error dialog or exception window present at capture",
    "INV-15": "Sign and color agree in P/L and change columns",
}

SETTLED_FRAMES = 3                      # 2.7 Persistence, 3.5 condition 5
OCR_CONFIDENCE_FLOOR = 70.0             # 2.7 Bounds, Branch B
OVERLAP_MIN_PX = 4                      # INV-01
OVERLAP_MIN_FRACTION = 0.02             # INV-01, of the smaller rect
OUTSIDE_TOLERANCE_PX = 1                # INV-02
EDGE_TOUCH_PX = 1                       # INV-03 Branch B
ERODE_PX = 1                            # INV-11 Branch B
ALIGN_TOLERANCE_PX = 2                  # INV-12
MIN_GRID_ROWS = 3                       # INV-12
MIN_FONT_PX = 9.0                       # INV-07 Branch A
MIN_CAP_HEIGHT_PX = 7.0                 # INV-07 Branch B (12.1: 7 px cap height at 1920x1080, 100% DPI)
LABEL_TEMPLATE_FLOOR = 0.9              # INV-05 Branch B
TEMPLATE_FLOOR = 0.92                   # INV-10 Branch B (3.7 default)
PLACEHOLDERS: tuple[str, ...] = ("Loading", "Reconnecting", "--", "N/A", "NaN", "null", "Infinity")
ELLIPSES: tuple[str, ...] = ("...", "…")

ENABLED = "enabled"
CALIBRATION_PENDING = "disabled: calibration pending"

COMPONENT_KINDS = ("window", "panel", "container", "viewport", "control", "text", "cell", "scrollbar", "icon")
RULE_FIELDS = ("price", "int", "money", "time", "text", "percent")


# --------------------------------------------------------------------------------------------
# Geometry


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box in native pixels covering [x, x+w) by [y, y+h)."""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def overlap(self, other: Rect) -> tuple[int, int]:
        """Extent of the intersection on each axis; zero or negative means disjoint on that axis."""
        return (min(self.right, other.right) - max(self.x, other.x),
                min(self.bottom, other.bottom) - max(self.y, other.y))

    def contains_point(self, point: tuple[float, float]) -> bool:
        px, py = point
        return self.x <= px < self.right and self.y <= py < self.bottom

    def contains(self, other: Rect) -> bool:
        return (self.x <= other.x and self.y <= other.y
                and other.right <= self.right and other.bottom <= self.bottom)

    def excess_over(self, outer: Rect) -> int:
        """How many pixels this box extends beyond `outer` on its worst side (0 when inside)."""
        return max(0, outer.x - self.x, self.right - outer.right, outer.y - self.y, self.bottom - outer.bottom)

    def eroded(self, px: int) -> Rect:
        return Rect(self.x + px, self.y + px, max(0, self.w - 2 * px), max(0, self.h - 2 * px))

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    @staticmethod
    def union(rects: Iterable[Rect]) -> Rect:
        rs = list(rects)
        if not rs:
            raise ValueError("union of no rects")
        x, y = min(r.x for r in rs), min(r.y for r in rs)
        return Rect(x, y, max(r.right for r in rs) - x, max(r.bottom for r in rs) - y)


# --------------------------------------------------------------------------------------------
# The screen model (one settled frame, from either bounds source)


@dataclass(frozen=True)
class WordBox:
    """One OCR word box (Branch B, or the OCR cross-check and opaque interiors under Branch A)."""

    text: str
    bbox: Rect
    confidence: float                   # OCR confidence 0..100; below the floor of 70 it is ignored
    anchor: str | None = None           # landmark-relative slot, equal on both builds for the same text
    cap_height: float | None = None     # INV-07 Branch B
    icon: bool = False                  # matched an icon template; excluded from INV-07
    oracle_digits: int | None = None    # digit count the Oracle extracted for this value (INV-03)


@dataclass(frozen=True)
class UIComponent:
    """A component with bounds: a JAB context (Branch A) or a template-located control or a
    segmented panel (Branch B)."""

    name: str                           # unique in the frame and stable across builds
    bbox: Rect
    kind: str                           # one of COMPONENT_KINDS
    parent: str | None = None           # name of the containing component
    text: str | None = None             # displayed text
    label: str | None = None            # accessible name (Branch A)
    role: str | None = None             # JAB role (Branch A)
    showing: bool = True                # Branch A: showing and visible by state
    opaque: bool = False                # Branch A: custom-painted container exposing no children
    control_id: str | None = None       # Branch B: registry control a template match located
    confidence: float | None = None     # Branch B: template match score
    font_px: float | None = None        # Branch A: font size attribute (INV-07)
    name_width_px: float | None = None  # Branch A: accessible name rendered with the pinned font metrics (INV-03)
    insets: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom (INV-03)
    scrollbar_showing: bool = False     # viewport with a showing scrollbar (INV-02)

    def __post_init__(self) -> None:
        if self.kind not in COMPONENT_KINDS:
            raise ValueError(f"component kind {self.kind!r}")


@dataclass(frozen=True)
class GridCell:
    """A grid cell of a declared column (INV-12, INV-15): a JAB table cell, or word boxes the
    Branch B pipeline assigned to the column."""

    column: str
    bbox: Rect
    text: str | None = None
    header: bool = False
    wrapped: bool = False
    color: tuple[int, int, int] | None = None   # dominant foreground color of the cell crop (INV-15)


@dataclass(frozen=True)
class Window:
    """A top-level window of the process tree (INV-09, INV-14)."""

    name: str
    rect: Rect
    kind: str                           # main | top | owned
    title: str = ""
    owner_screen: Rect | None = None    # owned dialogs: the screen of the owner window
    in_process_tree: bool = True
    error_template: bool = False        # Java exception dialog template or a popup catalog error entry matched

    def __post_init__(self) -> None:
        if self.kind not in ("main", "top", "owned"):
            raise ValueError(f"window kind {self.kind!r}")


@dataclass(frozen=True)
class ScreenModel:
    """What one settled frame looks like to the bounds source of DR-01 (2.7 Bounds)."""

    branch: str                                     # "A" (JAB) or "B" (OCR and templates)
    size: tuple[int, int]                           # captured surface size in native pixels
    components: tuple[UIComponent, ...] = ()
    words: tuple[WordBox, ...] = ()
    cells: tuple[GridCell, ...] = ()
    windows: tuple[Window, ...] = ()
    virtual_screen: Rect | None = None
    pinned_frame_rect: Rect | None = None           # main frame rect recorded at run start (INV-09)
    label_templates: Mapping[str, float] = field(default_factory=dict)   # label text -> best template score (INV-05 B)
    spinners: tuple[Rect, ...] = ()                 # spinner template matches (INV-08)
    focus_trace: tuple[str, ...] | None = None      # control ids that held focus after each Tab (INV-13)
    unreadable: tuple[str, ...] = ()                # elements whose bounds could not be read

    def __post_init__(self) -> None:
        if self.branch not in ("A", "B"):
            raise ValueError(f"branch {self.branch!r}; DR-01 decides A or B")
        names = [c.name for c in self.components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique in a frame")

    @property
    def client_rect(self) -> Rect:
        return Rect(0, 0, self.size[0], self.size[1])


# --------------------------------------------------------------------------------------------
# What the surface declares (4.5 Surface Registry entry, 4.8 Region Registry)


@dataclass(frozen=True)
class RequiredLabel:
    text: str
    region: Rect | None = None


@dataclass(frozen=True)
class RequiredControl:
    id: str
    role: str | None = None             # JAB role (Branch A)
    name: str | None = None             # JAB accessible name (Branch A)
    region: Rect | None = None
    ocr_text: str | None = None         # the registry's OCR anchor; also a declared label for INV-03


@dataclass(frozen=True)
class TextRegion:
    """A region INV-07 judges as a whole; thresholds come from the base distribution at calibration."""

    id: str
    rect: Rect
    min_cap_height_px: float | None = None
    min_font_px: float | None = None


@dataclass(frozen=True)
class ValueRegion:
    id: str
    rect: Rect
    allowed_placeholders: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleRegion:
    """A `rule` region of 4.8: INV-03 and INV-04 judge text inside it by the rule, not the base."""

    id: str
    rect: Rect
    field: str                          # one of RULE_FIELDS
    decimals: int | None = None
    pattern: str | None = None
    min: float | None = None
    max: float | None = None

    def __post_init__(self) -> None:
        if self.field not in RULE_FIELDS:
            raise ValueError(f"rule field {self.field!r}")

    def accepts(self, text: str) -> bool:
        """True when `text` satisfies the rule's format, decimals and range."""
        t = text.strip()
        if self.pattern is not None:
            return re.fullmatch(self.pattern, t) is not None
        if self.field == "text":
            return bool(t)
        if self.field == "time":
            return re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?", t) is not None
        if self.field == "int":
            m = re.fullmatch(r"[+\-−]?\d{1,3}(?:,\d{3})*|[+\-−]?\d+", t)
            return m is not None and self._in_range(t)
        number = r"\(?[+\-−]?\$?[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\)?"
        m = re.fullmatch(number + ("%" if self.field == "percent" else ""), t)
        if m is None:
            return False
        if self.decimals is not None and len(m.group(1) or "") != self.decimals:
            return False
        return self._in_range(t)

    def admits_ellipsis(self, text: str) -> bool:
        return self.field == "text" and (self.pattern is None or re.fullmatch(self.pattern, text.strip()) is not None)

    def _in_range(self, text: str) -> bool:
        value = _parse_signed(text)
        if value is None:
            return self.min is None and self.max is None
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


@dataclass(frozen=True)
class InvariantException:
    """A registry exception for an invariant that is wrong for one surface (4.5, 1.10)."""

    invariant_id: str
    owner: str
    reason: str
    expires_at: str                     # ISO timestamp


@dataclass(frozen=True)
class SurfaceRules:
    """Everything the surface declares that the invariants read."""

    surface_id: str
    tier: int
    data_mode: str = "frozen"
    labels: tuple[RequiredLabel, ...] = ()
    controls: tuple[RequiredControl, ...] = ()
    allowlist: tuple[tuple[str, str], ...] = ()                 # component name pairs; "*" is any
    clipped_by_design: tuple[str | Rect, ...] = ()              # component names or regions (INV-02)
    allowed_scrollbars: Mapping[str, tuple[int, int]] = field(default_factory=dict)  # container -> (w, h) from which none is expected
    text_regions: tuple[TextRegion, ...] = ()
    min_font_px: float = MIN_FONT_PX
    min_cap_height_px: float = MIN_CAP_HEIGHT_PX
    value_regions: tuple[ValueRegion, ...] = ()
    empty_grid_strings: tuple[str, ...] = ("No data",)
    rule_regions: tuple[RuleRegion, ...] = ()
    grid_columns: Mapping[str, str] = field(default_factory=dict)  # column -> "left" (text) | "right" (numeric)
    signed_columns: frozenset[str] = frozenset()
    focus_order: tuple[str, ...] | None = None                  # keyboard-only ticket flows only
    calibration_pending: frozenset[str] = frozenset()           # not yet enabled by the A/A test
    exceptions: tuple[InvariantException, ...] = ()

    def __post_init__(self) -> None:
        if self.tier not in (1, 2, 3):
            raise ValueError(f"tier {self.tier!r}")
        if self.data_mode not in ("frozen", "masked_live"):
            raise ValueError(f"data_mode {self.data_mode!r}")
        for col, align in self.grid_columns.items():
            if align not in ("left", "right"):
                raise ValueError(f"column {col} alignment {align!r}")
        for i in self.calibration_pending | {e.invariant_id for e in self.exceptions}:
            if i not in SEVERITY:
                raise ValueError(f"unknown invariant {i!r}")


@dataclass(frozen=True)
class CheckerConfig:
    """Run-wide settings: floors, the popup catalog's error title patterns, the pinned palette."""

    ocr_confidence_floor: float = OCR_CONFIDENCE_FLOOR
    template_floor: float = TEMPLATE_FLOOR
    label_template_floor: float = LABEL_TEMPLATE_FLOOR
    error_title_patterns: tuple[str, ...] = ()                  # title_regex of the catalog's app_failure entries (4.10)
    palette: Mapping[str, tuple[int, int, int]] = field(default_factory=dict)  # positive, negative, neutral (pinned per theme)
    palette_tolerance: float = 60.0


DEFAULT_CONFIG = CheckerConfig()


# --------------------------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class Violation:
    """One fired invariant with its evidence: the rects involved and the native crop to cut."""

    invariant_id: str
    subject: str
    rects: tuple[Rect, ...]
    detail: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.invariant_id, self.subject)

    @property
    def crop(self) -> Rect:
        return Rect.union(self.rects)


@dataclass(frozen=True)
class FrameCheck:
    """The result of `check` on one frame."""

    violations: tuple[Violation, ...]
    unreadable: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()      # listed `disabled: calibration pending` in run health
    excepted: tuple[str, ...] = ()      # registry exception in force


@dataclass(frozen=True)
class InvariantResult:
    """Violations that held on all 3 settled frames of one capture (2.7 Persistence)."""

    violations: tuple[Violation, ...]
    discarded: tuple[Violation, ...] = ()   # seen on some frames only: flake statistics
    unreadable: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    excepted: tuple[str, ...] = ()

    @property
    def keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(v.key for v in self.violations)


@dataclass(frozen=True)
class Reported:
    """A candidate violation as reported: severity after tier and pre-existing rules."""

    violation: Violation
    severity: str
    pre_existing: bool


@dataclass(frozen=True)
class Comparison:
    """The candidate's violations against the base's (2.7 Pre-existing)."""

    reported: tuple[Reported, ...]
    base_only: tuple[Violation, ...] = ()
    discarded: tuple[Violation, ...] = ()
    unreadable: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    excepted: tuple[str, ...] = ()

    @property
    def fired(self) -> bool:
        return bool(self.reported)

    @property
    def invariant_ids(self) -> tuple[str, ...]:
        """Ids that fired on the candidate, in catalog order (4.13: the reason names the first)."""
        return _catalog_order(r.violation.invariant_id for r in self.reported)


@dataclass(frozen=True)
class Persistence:
    """Outcome of the confirming re-capture of 3.10 for an invariant violation."""

    stability: str                      # stable | unstable | unknown
    stability_cause: str | None
    confirmed: tuple[Reported, ...]
    unconfirmed: tuple[Reported, ...] = ()

    @property
    def invariant_ids(self) -> tuple[str, ...]:
        return _catalog_order(r.violation.invariant_id for r in self.confirmed)


@dataclass(frozen=True)
class AAPair:
    """Two captures of the same build at one checkpoint (3.9), each as its 3 settled frames."""

    data_mode: str
    first: tuple[ScreenModel, ...]
    second: tuple[ScreenModel, ...]


def _catalog_order(ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(ids), key=INVARIANT_IDS.index))


def effective_severity(invariant_id: str, *, tier: int, pre_existing: bool) -> str:
    """2.7 Severity: tier 1 bumps S2 to S1 and S3 to S2; pre-existing is one level lower."""
    level = int(SEVERITY[invariant_id][1])
    if tier == 1:
        level = max(1, level - 1)
    if pre_existing:
        level += 1
    return f"S{min(level, 3)}"


# --------------------------------------------------------------------------------------------
# Text helpers


def _ends_with_ellipsis(text: str | None) -> bool:
    return bool(text) and text.rstrip().endswith(ELLIPSES)


def _levenshtein_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


def _strict_label_prefix(text: str, labels: Iterable[str]) -> bool:
    """True when `text` is a strict prefix of a declared label, or of one of its words, and is not
    itself a whole word of that label (2.7 INV-03 Branch B)."""
    t = text.strip()
    if not t:
        return False
    for label in labels:
        parts = label.split()
        if t == label or t in parts:
            continue
        if label.startswith(t) or any(p.startswith(t) for p in parts):
            return True
    return False


def _placeholder(text: str, extra: Iterable[str]) -> str | None:
    """The placeholder `text` reads as, if any (2.7 INV-08)."""
    t = text.strip()
    core = t.rstrip(".…").strip() or t
    folded = core.casefold()
    for p in (*PLACEHOLDERS, *extra):
        pf = p.casefold()
        if folded == pf or (p in ("Loading", "Reconnecting") and folded.startswith(pf)):
            return p
        if p == "Infinity" and folded.lstrip("+-−") == pf:
            return p
    return None


_SIGNED = re.compile(r"^\(?([+\-−])?\$?([+\-−])?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)%?\)?$")


def _parse_signed(text: str) -> float | None:
    """A signed number from a cell's text: '+1.25', '-$12.00', '(3.10)', '2.5%'; None when unreadable."""
    t = text.strip().replace(" ", "")
    m = _SIGNED.match(t)
    if not m:
        return None
    value = float(m.group(3).replace(",", ""))
    negative = (m.group(1) in ("-", "−") or m.group(2) in ("-", "−")
                or (t.startswith("(") and t.endswith(")")))
    return -value if negative else value


def _reading_order(words: Iterable[WordBox]) -> list[list[WordBox]]:
    """Group word boxes into lines (centers within half a line height) in reading order."""
    lines: list[list[WordBox]] = []
    for w in sorted(words, key=lambda w: (w.bbox.center[1], w.bbox.x)):
        cy = w.bbox.center[1]
        if lines and abs(cy - statistics.fmean(x.bbox.center[1] for x in lines[-1])) <= max(
                max(x.bbox.h for x in lines[-1]), w.bbox.h) / 2:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.bbox.x) for line in lines]


def _pair(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _overlapping_pairs(items: Sequence[tuple[str, Rect]]) -> Iterable[tuple[tuple[str, Rect], tuple[str, Rect]]]:
    """Pairs of items whose rects intersect with positive area (sweep over x)."""
    active: list[tuple[str, Rect]] = []
    for item in sorted(items, key=lambda it: it[1].x):
        active = [a for a in active if a[1].right > item[1].x]
        for a in active:
            ox, oy = a[1].overlap(item[1])
            if ox > 0 and oy > 0:
                yield a, item
        active.append(item)


# --------------------------------------------------------------------------------------------
# The checker


class _Frame:
    """One frame plus the derived lookups every rule shares."""

    def __init__(self, model: ScreenModel, rules: SurfaceRules, config: CheckerConfig,
                 reference: ScreenModel | None) -> None:
        self.model, self.rules, self.config, self.reference = model, rules, config, reference
        self.by_name = {c.name: c for c in model.components}
        self.opaque = [c for c in model.components if c.opaque]
        self.words = [w for w in model.words if w.confidence >= config.ocr_confidence_floor]
        self.b_words = [w for w in self.words if self.word_is_b(w)]
        self.declared_labels = [lb.text for lb in rules.labels] + [c.ocr_text for c in rules.controls if c.ocr_text]

    def ancestors(self, comp: UIComponent) -> Iterable[UIComponent]:
        seen: set[str] = set()
        parent = comp.parent
        while parent is not None and parent in self.by_name and parent not in seen:
            seen.add(parent)
            node = self.by_name[parent]
            yield node
            parent = node.parent

    def comp_is_b(self, comp: UIComponent) -> bool:
        return self.model.branch == "B" or any(a.opaque for a in self.ancestors(comp))

    def word_is_b(self, word: WordBox) -> bool:
        return self.model.branch == "B" or any(o.bbox.contains_point(word.bbox.center) for o in self.opaque)

    def a_components(self) -> list[UIComponent]:
        return [c for c in self.model.components if c.showing and not self.comp_is_b(c)]

    def smallest_containing(self, rect: Rect, kinds: Sequence[str], *, include_opaque: bool = False) -> UIComponent | None:
        point = rect.center
        found = [c for c in self.model.components
                 if (c.kind in kinds or (include_opaque and c.opaque)) and c.showing and c.bbox.contains_point(point)]
        return min(found, key=lambda c: c.bbox.area, default=None)

    def allowlisted(self, a: str, b: str) -> bool:
        for x, y in self.rules.allowlist:
            if (x in (a, "*") and y in (b, "*")) or (x in (b, "*") and y in (a, "*")):
                return True
        return False

    def clipped_by_design(self, name: str | None, rect: Rect) -> bool:
        for entry in self.rules.clipped_by_design:
            if isinstance(entry, Rect):
                if entry.contains_point(rect.center):
                    return True
            elif entry == name:
                return True
        return False

    def rule_region(self, rect: Rect) -> RuleRegion | None:
        return next((r for r in self.rules.rule_regions if r.rect.contains_point(rect.center)), None)

    def words_in(self, rect: Rect, words: Iterable[WordBox] | None = None) -> list[WordBox]:
        return [w for w in (self.words if words is None else words) if rect.contains_point(w.bbox.center)]

    @staticmethod
    def word_key(word: WordBox) -> str:
        return word.anchor or f"{word.text}@{word.bbox.x},{word.bbox.y}"

    def base_word(self, word: WordBox) -> WordBox | None:
        """The other build's word at the same anchor (by `anchor`, else by largest overlap)."""
        if self.reference is None:
            return None
        if word.anchor is not None:
            return next((w for w in self.reference.words if w.anchor == word.anchor), None)
        best, best_area = None, 0
        for w in self.reference.words:
            ox, oy = w.bbox.overlap(word.bbox)
            if ox > 0 and oy > 0 and ox * oy > best_area:
                best, best_area = w, ox * oy
        return best


def _inv01(f: _Frame) -> list[Violation]:
    groups: dict[tuple[str, bool], list[UIComponent]] = defaultdict(list)
    for c in f.model.components:
        if c.parent is None or not c.showing:
            continue
        is_b = f.comp_is_b(c)
        if is_b and c.kind not in ("panel", "control"):
            continue
        groups[(c.parent, is_b)].append(c)
    out = []
    for members in groups.values():
        for a, b in combinations(members, 2):
            ox, oy = a.bbox.overlap(b.bbox)
            if ox < OVERLAP_MIN_PX or oy < OVERLAP_MIN_PX:
                continue
            if ox * oy < OVERLAP_MIN_FRACTION * min(a.bbox.area, b.bbox.area):
                continue
            if f.allowlisted(a.name, b.name):
                continue
            out.append(Violation("INV-01", _pair(a.name, b.name), (a.bbox, b.bbox),
                                 f"{a.name} and {b.name} intersect by {ox}x{oy} px"))
    return out


def _inv06(f: _Frame) -> list[Violation]:
    w, h = f.model.size
    out = []
    for c in f.model.components:
        if c.kind != "scrollbar" or not c.showing:
            continue
        container = c.parent or ""
        allowed = f.rules.allowed_scrollbars.get(container)
        if allowed is not None and not (w >= allowed[0] and h >= allowed[1]):
            continue
        why = "not allowed to scroll" if allowed is None else f"none expected at {w}x{h} (from {allowed[0]}x{allowed[1]})"
        out.append(Violation("INV-06", container or c.name, (c.bbox,), f"scrollbar {c.name} in {container or 'no container'}: {why}"))
    return out


def _inv02(f: _Frame, unexpected_scroll: set[str]) -> list[Violation]:
    out = []
    client = f.model.client_rect

    def excluded_viewport(parent: UIComponent) -> bool:
        return parent.kind == "viewport" and parent.scrollbar_showing and parent.name not in unexpected_scroll

    for c in f.model.components:
        if not c.showing:
            continue
        is_b = f.comp_is_b(c)
        if is_b and c.kind not in ("control", "panel"):
            continue
        parent = f.by_name.get(c.parent) if c.parent else None
        if parent is not None:
            excess = c.bbox.excess_over(parent.bbox)
            if (excess > OUTSIDE_TOLERANCE_PX and not excluded_viewport(parent)
                    and not f.allowlisted(c.name, parent.name) and not f.clipped_by_design(c.name, c.bbox)):
                out.append(Violation("INV-02", c.name, (c.bbox, parent.bbox), f"{c.name} extends {excess} px outside {parent.name}"))
                continue
        if is_b:
            excess = c.bbox.excess_over(client)
            if excess > OUTSIDE_TOLERANCE_PX and not f.clipped_by_design(c.name, c.bbox):
                out.append(Violation("INV-02", c.name, (c.bbox, client), f"{c.name} crosses the client rect by {excess} px"))
    for w in f.b_words:
        key = f.word_key(w)
        panel = f.smallest_containing(w.bbox, ("panel", "container", "viewport"), include_opaque=True)
        if panel is not None:
            excess = w.bbox.excess_over(panel.bbox)
            if (excess > OUTSIDE_TOLERANCE_PX and not excluded_viewport(panel)
                    and not f.clipped_by_design(panel.name, w.bbox) and not f.clipped_by_design(None, w.bbox)):
                out.append(Violation("INV-02", key, (w.bbox, panel.bbox), f"word {w.text!r} crosses {panel.name} by {excess} px"))
                continue
        excess = w.bbox.excess_over(client)
        if excess > OUTSIDE_TOLERANCE_PX and not f.clipped_by_design(None, w.bbox):
            out.append(Violation("INV-02", key, (w.bbox, client), f"word {w.text!r} crosses the client rect by {excess} px"))
    return out


def _touches_edge(inner: Rect, outer: Rect) -> bool:
    return min(inner.x - outer.x, outer.right - inner.right, inner.y - outer.y, outer.bottom - inner.bottom) <= EDGE_TOUCH_PX


def _inv03(f: _Frame) -> list[Violation]:
    out = []
    for c in f.a_components():
        if not c.label or c.name_width_px is None:
            continue
        inner = c.bbox.w - c.insets[0] - c.insets[2]
        if c.name_width_px <= inner:
            continue
        seen = " ".join(w.text for line in _reading_order(f.words_in(c.bbox)) for w in line)
        if seen == c.label:
            continue                                     # OCR cross-check reads the whole name
        out.append(Violation("INV-03", c.name, (c.bbox,),
                             f"accessible name {c.label!r} needs {c.name_width_px:g} px; inner width {inner} px"))
    for w in f.b_words:
        box = f.smallest_containing(w.bbox, ("cell", "control"))
        if box is None or not _touches_edge(w.bbox, box.bbox):
            continue
        prefix = _strict_label_prefix(w.text, f.declared_labels)
        region = f.rule_region(w.bbox)
        if region is not None:
            fires = prefix or not region.accepts(w.text)
            why = f"fails rule {region.id}" if not prefix else "strict prefix of a declared label"
        elif f.rules.data_mode == "masked_live":
            fires, why = prefix, "strict prefix of a declared label"
        else:
            base = f.base_word(w)
            digits_differ = (base is not None and w.oracle_digits is not None and base.oracle_digits is not None
                             and w.oracle_digits != base.oracle_digits)
            differs = base is not None and base.text != w.text and not digits_differ
            fires = prefix or differs
            why = f"base reads {base.text!r}" if differs and base is not None else "strict prefix of a declared label"
        if fires:
            out.append(Violation("INV-03", f.word_key(w), (w.bbox, box.bbox),
                                 f"word {w.text!r} at the edge of {box.name}: {why}"))
    return out


def _inv04(f: _Frame) -> list[Violation]:
    out = []
    ref_by_name = {c.name: c for c in f.reference.components} if f.reference is not None else {}
    for c in f.a_components():
        if not _ends_with_ellipsis(c.text) or not c.label or _ends_with_ellipsis(c.label):
            continue
        ref = ref_by_name.get(c.name)
        if ref is not None and _ends_with_ellipsis(ref.text):
            continue
        out.append(Violation("INV-04", c.name, (c.bbox,), f"displays {c.text!r}; accessible name {c.label!r}"))
    for w in f.b_words:
        if not _ends_with_ellipsis(w.text):
            continue
        region = f.rule_region(w.bbox)
        if region is not None:
            fires, why = not region.admits_ellipsis(w.text), f"rule {region.id} admits no ellipsis"
        elif f.rules.data_mode == "masked_live":
            fires, why = False, ""
        else:
            base = f.base_word(w)
            fires = base is not None and not _ends_with_ellipsis(base.text)
            why = f"base reads {base.text!r}" if base is not None else ""
        if fires:
            out.append(Violation("INV-04", f.word_key(w), (w.bbox,), f"word {w.text!r} ends in an ellipsis: {why}"))
    return out


def _inv05(f: _Frame) -> list[Violation]:
    out = []
    for label in f.rules.labels:
        def inside(rect: Rect, region: Rect | None = label.region) -> bool:
            return region is None or region.contains_point(rect.center)

        found = any((c.label or "").strip() == label.text or (c.text or "").strip() == label.text
                    for c in f.a_components() if inside(c.bbox))
        if not found and f.model.branch == "B":
            found = f.model.label_templates.get(label.text, 0.0) > f.config.label_template_floor
        if not found:
            n = len(label.text.split())
            for line in _reading_order(w for w in f.b_words if inside(w.bbox)):
                runs = [" ".join(w.text for w in line[i:i + n]) for i in range(max(1, len(line) - n + 1))]
                if any(_levenshtein_at_most_one(run, label.text) for run in runs):
                    found = True
                    break
        if not found:
            out.append(Violation("INV-05", label.text, (label.region or f.model.client_rect,),
                                 f"required label {label.text!r} not found"))
    return out


def _inv07(f: _Frame) -> list[Violation]:
    regions = f.rules.text_regions or (TextRegion("_all", f.model.client_rect),)
    out = []
    for region in regions:
        min_font = region.min_font_px if region.min_font_px is not None else f.rules.min_font_px
        min_cap = region.min_cap_height_px if region.min_cap_height_px is not None else f.rules.min_cap_height_px
        fonts = [c.font_px for c in f.a_components()
                 if c.font_px is not None and region.rect.contains_point(c.bbox.center)]
        caps = [w.cap_height if w.cap_height is not None else float(w.bbox.h)
                for w in f.words_in(region.rect, f.b_words) if not w.icon]
        problems = []
        if fonts and statistics.median(fonts) < min_font:
            problems.append(f"median font {statistics.median(fonts):g} px < {min_font:g} px")
        if caps and statistics.median(caps) < min_cap:
            problems.append(f"median cap height {statistics.median(caps):g} px < {min_cap:g} px")
        if problems:
            out.append(Violation("INV-07", region.id, (region.rect,), "; ".join(problems)))
    return out


def _inv08(f: _Frame) -> list[Violation]:
    out = []
    for region in f.rules.value_regions:
        allowed = {p.casefold() for p in region.allowed_placeholders}
        texts: list[tuple[str, Rect]] = [(c.text, c.bbox) for c in f.a_components()
                                         if c.text and region.rect.contains_point(c.bbox.center)]
        for line in _reading_order(f.words_in(region.rect, f.b_words)):
            texts += [(w.text, w.bbox) for w in line]
            if len(line) > 1:
                texts.append((" ".join(w.text for w in line), Rect.union(w.bbox for w in line)))
        seen: set[str] = set()
        for text, rect in texts:
            hit = _placeholder(text, f.rules.empty_grid_strings)
            if hit is None or hit.casefold() in allowed or hit in seen:
                continue
            seen.add(hit)
            out.append(Violation("INV-08", f"{region.id}:{hit}", (rect,), f"{text!r} in value region {region.id}"))
        for spinner in f.model.spinners:
            if region.rect.contains_point(spinner.center) and "spinner" not in seen:
                seen.add("spinner")
                out.append(Violation("INV-08", f"{region.id}:spinner", (spinner,), f"spinner in value region {region.id}"))
    return out


def _inv09(f: _Frame) -> list[Violation]:
    windows = [w for w in f.model.windows if w.in_process_tree]
    if not windows:
        return []
    screen = f.model.virtual_screen
    if screen is None:
        raise ValueError("INV-09 needs the virtual screen rect")
    out = []
    for w in windows:
        bounds = screen if w.kind != "owned" else (w.owner_screen or screen)
        where = "the virtual screen" if w.kind != "owned" else "its owner's screen"
        if not bounds.contains(w.rect):
            out.append(Violation("INV-09", w.name, (w.rect,), f"window {w.name} is not inside {where}"))
        elif w.kind == "main" and f.model.pinned_frame_rect is not None and w.rect != f.model.pinned_frame_rect:
            out.append(Violation("INV-09", w.name, (w.rect, f.model.pinned_frame_rect),
                                 f"main frame rect {w.rect.as_tuple()} moved from {f.model.pinned_frame_rect.as_tuple()}"))
    return out


def _inv10(f: _Frame) -> list[Violation]:
    out = []
    for ctrl in f.rules.controls:
        def inside(rect: Rect, region: Rect | None = ctrl.region) -> bool:
            return region is None or region.contains_point(rect.center)

        a_matches = b_matches = []
        if ctrl.role and ctrl.name:
            a_matches = [c for c in f.a_components() if c.role == ctrl.role and c.label == ctrl.name and inside(c.bbox)]
        templated = [c for c in f.model.components if c.showing and c.control_id == ctrl.id
                     and (c.confidence or 0.0) >= f.config.template_floor and inside(c.bbox)]
        b_matches = [c for c in templated if f.comp_is_b(c)] if (ctrl.role and ctrl.name) else templated
        matches = a_matches + [c for c in b_matches if c not in a_matches]
        if len(matches) != 1:
            rects = tuple(c.bbox for c in matches) or (ctrl.region or f.model.client_rect,)
            out.append(Violation("INV-10", ctrl.id, rects, f"required control {ctrl.id} found {len(matches)} times"))
    return out


def _inv11(f: _Frame) -> list[Violation]:
    out = []
    texts = [(c.name, c.bbox) for c in f.a_components() if c.kind == "text"]
    for (a, ra), (b, rb) in _overlapping_pairs(texts):
        if not f.allowlisted(a, b):
            out.append(Violation("INV-11", _pair(a, b), (ra, rb), f"text runs {a} and {b} overlap"))
    owner: dict[str, str] = {}
    items = []
    for w in f.b_words:
        key = f.word_key(w)
        comp = f.smallest_containing(w.bbox, COMPONENT_KINDS)
        owner[key] = comp.name if comp is not None else key
        items.append((key, w.bbox.eroded(ERODE_PX)))
    text_of = {f.word_key(w): w for w in f.b_words}
    for (a, _), (b, _) in _overlapping_pairs(items):
        if a == b or f.allowlisted(owner[a], owner[b]):
            continue
        wa, wb = text_of[a], text_of[b]
        out.append(Violation("INV-11", _pair(a, b), (wa.bbox, wb.bbox), f"word boxes {wa.text!r} and {wb.text!r} overlap"))
    return out


def _inv12(f: _Frame) -> list[Violation]:
    out = []
    for column, align in f.rules.grid_columns.items():
        cells = [c for c in f.model.cells if c.column == column]
        body = [c for c in cells if not c.header and not c.wrapped]
        if len(body) < MIN_GRID_ROWS:
            continue
        edges = [c.bbox.x if align == "left" else c.bbox.right for c in body]
        median = statistics.median(edges)
        off = [c for c, e in zip(body, edges) if abs(e - median) > ALIGN_TOLERANCE_PX]
        problems = [f"{len(off)} cells off the {align} edge {median:g} by more than {ALIGN_TOLERANCE_PX} px"] if off else []
        lo, hi = min(c.bbox.x for c in body), max(c.bbox.right for c in body)
        headers = [c for c in cells if c.header]
        bad_headers = [h for h in headers if h.bbox.x > lo + ALIGN_TOLERANCE_PX or h.bbox.right < hi - ALIGN_TOLERANCE_PX]
        if bad_headers:
            problems.append("header does not span the cells")
        if problems:
            rects = tuple(c.bbox for c in off + bad_headers)
            out.append(Violation("INV-12", column, rects, f"column {column}: " + "; ".join(problems)))
    return out


def _inv13(f: _Frame) -> list[Violation]:
    order, trace = f.rules.focus_order, f.model.focus_trace
    if order is None or trace is None:
        return []
    observed = tuple(trace[:len(order)])
    if observed == tuple(order):
        return []
    at = next((i for i, (a, b) in enumerate(zip(observed, order)) if a != b), len(observed))
    got = observed[at] if at < len(observed) else "nothing"
    return [Violation("INV-13", "focus", (f.model.client_rect,),
                      f"Tab {at + 1} focused {got}; declared {order[at]}")]


def _inv14(f: _Frame) -> list[Violation]:
    out = []
    for w in f.model.windows:
        if not (w.in_process_tree and w.error_template):
            continue
        if not f.config.error_title_patterns:
            raise ValueError("INV-14 needs the popup catalog's error title patterns")
        if any(re.search(p, w.title) for p in f.config.error_title_patterns):
            out.append(Violation("INV-14", w.name, (w.rect,), f"error dialog {w.title!r} present at capture"))
    return out


def _palette_class(color: tuple[int, int, int], config: CheckerConfig) -> str | None:
    best, best_d = None, float("inf")
    for name, ref in config.palette.items():
        d = sum((a - b) ** 2 for a, b in zip(color, ref)) ** 0.5
        if d < best_d:
            best, best_d = name, d
    return best if best_d <= config.palette_tolerance else None


def _inv15(f: _Frame) -> list[Violation]:
    out = []
    for c in f.model.cells:
        if c.column not in f.rules.signed_columns or c.header or c.text is None or c.color is None:
            continue
        value = _parse_signed(c.text)
        if value is None or value == 0:
            continue                                     # unreadable or zero: excluded
        if not f.config.palette:
            raise ValueError("INV-15 needs the palette pinned for the theme")
        cls = _palette_class(c.color, f.config)
        expected = "positive" if value > 0 else "negative"
        if cls is not None and cls != expected:
            out.append(Violation("INV-15", f"{c.column}:{c.bbox.x},{c.bbox.y}", (c.bbox,),
                                 f"{c.text!r} in {c.column} is drawn {cls}; its sign is {expected}"))
    return out


def check(model: ScreenModel, rules: SurfaceRules, *, reference: ScreenModel | None = None,
          config: CheckerConfig = DEFAULT_CONFIG) -> FrameCheck:
    """Evaluate every enabled invariant of 2.7 on one frame.

    `reference` is the other build's capture at the same checkpoint, which INV-03 and INV-04
    compare against; without it those two fire only on their rule-based and label-prefix tests.
    Invariants pending calibration or under a registry exception are not evaluated and are
    returned in `disabled` and `excepted`, never silently off.
    """
    f = _Frame(model, rules, config, reference)
    excepted = {e.invariant_id for e in rules.exceptions}
    skip = excepted | set(rules.calibration_pending)
    inv06 = _inv06(f) if "INV-06" not in skip else []
    unexpected_scroll = {v.subject for v in inv06}
    rules_by_id = {
        "INV-01": lambda: _inv01(f), "INV-02": lambda: _inv02(f, unexpected_scroll),
        "INV-03": lambda: _inv03(f), "INV-04": lambda: _inv04(f), "INV-05": lambda: _inv05(f),
        "INV-06": lambda: inv06, "INV-07": lambda: _inv07(f), "INV-08": lambda: _inv08(f),
        "INV-09": lambda: _inv09(f), "INV-10": lambda: _inv10(f), "INV-11": lambda: _inv11(f),
        "INV-12": lambda: _inv12(f), "INV-13": lambda: _inv13(f), "INV-14": lambda: _inv14(f),
        "INV-15": lambda: _inv15(f),
    }
    found: dict[tuple[str, str], Violation] = {}
    for inv in INVARIANT_IDS:
        if inv in skip:
            continue
        for v in rules_by_id[inv]():
            found.setdefault(v.key, v)
    ordered = sorted(found.values(), key=lambda v: (INVARIANT_IDS.index(v.invariant_id), v.subject))
    return FrameCheck(tuple(ordered), unreadable=tuple(model.unreadable),
                      disabled=_catalog_order(rules.calibration_pending - excepted), excepted=_catalog_order(excepted))


def persist(frames: Sequence[FrameCheck]) -> InvariantResult:
    """2.7 Persistence: keep violations seen on all 3 settled frames; discard the rest for flake
    statistics. Evidence comes from the first frame, the capture of 3.5."""
    if len(frames) != SETTLED_FRAMES:
        raise ValueError(f"persistence needs the {SETTLED_FRAMES} settled frames, got {len(frames)}")
    keyed = [{v.key: v for v in fc.violations} for fc in frames]
    held = set(keyed[0]).intersection(*keyed[1:])
    kept = tuple(v for v in frames[0].violations if v.key in held)
    discarded: dict[tuple[str, str], Violation] = {}
    for k in keyed:
        for key, v in k.items():
            if key not in held:
                discarded.setdefault(key, v)
    unreadable = tuple(dict.fromkeys(e for fc in frames for e in fc.unreadable))
    return InvariantResult(kept, tuple(discarded.values()), unreadable, frames[0].disabled, frames[0].excepted)


def check_capture(frames: Sequence[ScreenModel], rules: SurfaceRules, *, reference: ScreenModel | None = None,
                  config: CheckerConfig = DEFAULT_CONFIG) -> InvariantResult:
    """`check` on each of the 3 settled frames of one capture, then `persist`."""
    return persist([check(m, rules, reference=reference, config=config) for m in frames])


def compare(base: Sequence[ScreenModel] | None, candidate: Sequence[ScreenModel], rules: SurfaceRules, *,
            config: CheckerConfig = DEFAULT_CONFIG) -> Comparison:
    """Invariants on both builds (2.7): a violation on both is reported once as pre-existing at
    one severity level lower and still makes the candidate `invariant_violation`; candidate-only
    violations get full severity. `base` is None when no base capture exists."""
    base_capture = base[0] if base else None
    cand = check_capture(candidate, rules, reference=base_capture, config=config)
    base_result = check_capture(base, rules, reference=candidate[0], config=config) if base else None
    base_keys = base_result.keys if base_result is not None else frozenset()
    reported = tuple(Reported(v, effective_severity(v.invariant_id, tier=rules.tier, pre_existing=v.key in base_keys),
                              v.key in base_keys) for v in cand.violations)
    base_only = tuple(v for v in base_result.violations if v.key not in cand.keys) if base_result else ()
    return Comparison(reported, base_only, cand.discarded, cand.unreadable, cand.disabled, cand.excepted)


def confirm(comparison: Comparison, recapture: InvariantResult | None, *, cause: str | None = None) -> Persistence:
    """3.10 for an invariant violation: re-captured once. Firing again is `stable`; not firing is
    `unstable` (the checkpoint becomes `not_settled (reason: unstable)` with the violation kept as
    evidence). Without a re-capture the stability is `unknown` with cause `not_recapturable` or
    `budget`, and the violation stands."""
    if not comparison.fired:
        raise ValueError("no invariant violation to confirm")
    if recapture is None:
        if cause not in ("not_recapturable", "budget"):
            raise ValueError("an unconfirmed violation needs cause not_recapturable or budget (3.10)")
        return Persistence("unknown", cause, comparison.reported)
    again = recapture.keys
    confirmed = tuple(r for r in comparison.reported if r.violation.key in again)
    unconfirmed = tuple(r for r in comparison.reported if r.violation.key not in again)
    return Persistence("stable" if confirmed else "unstable", None, confirmed, unconfirmed)


def active_exceptions(rules: SurfaceRules, now: str) -> tuple[SurfaceRules, tuple[InvariantException, ...]]:
    """Drop registry exceptions expired at run start (L-SRF-11); the dropped ones go to run health
    as `invariant_exception_expired` findings."""
    live = tuple(e for e in rules.exceptions if e.expires_at > now)
    expired = tuple(e for e in rules.exceptions if e.expires_at <= now)
    return replace(rules, exceptions=live), expired


def _aa_fired(pair: AAPair, rules: SurfaceRules, config: CheckerConfig) -> tuple[str, ...]:
    r = replace(rules, data_mode=pair.data_mode)
    a = check_capture(pair.first, r, reference=pair.second[0], config=config)
    b = check_capture(pair.second, r, reference=pair.first[0], config=config)
    return _catalog_order(v.invariant_id for v in a.violations + b.violations)


def enablement(pairs: Sequence[AAPair], rules: SurfaceRules, *, config: CheckerConfig = DEFAULT_CONFIG) -> dict[str, str]:
    """The A/A enablement test of 2.7 and F46.

    An invariant is enabled for the surface when it fires on none of the surface's frozen A/A
    pairs. A surface with no frozen pairs uses its masked_live pairs, checked with masks and rule
    regions in force (INV-03 and INV-04 then use the rule-region control). With no pairs at all
    every invariant stays `disabled: calibration pending`.
    """
    frozen = [p for p in pairs if p.data_mode == "frozen"]
    pool = frozen or [p for p in pairs if p.data_mode == "masked_live"]
    if not pool:
        return {i: CALIBRATION_PENDING for i in INVARIANT_IDS}
    everything = replace(rules, calibration_pending=frozenset(), exceptions=())
    fired = {i for p in pool for i in _aa_fired(p, everything, config)}
    return {i: CALIBRATION_PENDING if i in fired else ENABLED for i in INVARIANT_IDS}


def with_enablement(rules: SurfaceRules, statuses: Mapping[str, str]) -> SurfaceRules:
    """The surface's rules with every invariant not `enabled` marked calibration pending."""
    return replace(rules, calibration_pending=frozenset(i for i, s in statuses.items() if s != ENABLED))


def invariants_on_aa(pair: AAPair, rules: SurfaceRules, *, config: CheckerConfig = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Enabled invariants that fired on a Nightly A/A pair. Any is a harness bug recorded as
    `harness_error (reason: invariant_on_aa)` on the Nightly run (2.7, F54)."""
    return _aa_fired(pair, rules, config)
