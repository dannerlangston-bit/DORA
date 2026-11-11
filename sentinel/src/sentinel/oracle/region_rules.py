"""Rule regions (3.8, 4.8): the Oracle's typed assertion of every `rule` region a checkpoint's regions file
declares, on flow, census and sweep checkpoints alike (2.1 "census and sweeps on rule regions", 2.8.1, SD-29).

A `rule` region is never pixel-diffed; its `rule` record is the assertion. `RegionRuleChecker.check(checkpoint,
side, regions_file, sources, history, clock)` reads each rule region once (OCR declared, then the alternates of
2.8.1 the checkpoint supplies: JAB, export) and returns one `AssertionResult` per declared rule field, in the
regions file's order and, within a region, in `CHECKS` order. The assertion id is `rule.<region_id>.<field>`, the
4.8 rule field it checks (`rule.rgn.ps.mark.decimals`), so through `OracleVerdict.facts_fields()` a failure is
`assertion_failed (assertion: rule.rgn.ps.mark.decimals)` (3.11 H, row 8), an unreadable region `needs_human
(reason: oracle_unreadable)` (row 9a), a read that needed OCR after a calibration-panel miss `harness_error (reason:
ocr_calibration)` and a market-data API failure `env_error` (`preflight.N3`, `connectivity`), as for flow
assertions (`assertions` module docstring).

The checks, one per rule field (4.8):

- `field` (always): every non-empty row is a valid value of the field's format, the R10 test (no NaN, Infinity,
  null, double sign or misplaced separator).
- `pattern`: every non-empty row matches the regex (searched, as the Oracle's `matches` does; the 4.8 example
  patterns anchor with `^` themselves).
- `decimals`: every valid row shows exactly that many digits after the decimal point.
- `min`, `max`: every valid row's value lies within the bound.
- `min_rows`: the region holds at least that many non-empty rows.
- `must_advance` (time fields): the time shown is later than the previous read of the region on the same side.
- `must_change_within_s`: two reads of the region on the same side, at least that many seconds apart with the
  venue open throughout, differ.
- `must_not_change_when_closed`: two reads on the same side with the venue closed throughout are equal.
- `api_window`: every valid row is within `tol_ticks` ticks of the market-data API's point value as of the read
  time minus `delay_s` (2.8.2, SD-70).

`RegionHistory` keeps each side's previous readable read per region; the checker compares with it and then
records the current read. `RegularSession` is a weekday session calendar for the venue checks. `recheck` re-runs
the single-read checks over a stored read (`extraction_log.rerun_offline`). `invariant_rule_regions` turns a
regions file's rule regions into `invariants.RuleRegion`s for `SurfaceRules.rule_regions` (INV-03, INV-04, 2.7).

Choices where the spec is ambiguous (reported):

- The assertion id's `<field>` is the 4.8 rule field checked (`field`, `pattern`, `decimals`, `min`, `max`,
  `min_rows`, `must_advance`, `must_change_within_s`, `must_not_change_when_closed`, `api_window`); `field` is the
  format check every rule region has. `AssertionResult.kind` is `region_rule` (`REGION_RULE_KIND`), outside the
  4.7 kind enum, since a rule region is not a flow assertion; `AssertionResult.region` names the region.
- Empty rows are `min_rows`' business: `field`, `pattern`, `decimals`, `min`, `max` and `api_window` judge the
  non-empty rows, so an empty column passes them. `decimals`, `min`, `max` and `api_window` skip a row that fails
  `field`; that row already fails `field`, which comes first and so names the outcome.
- `decimals` applies to `price`, `money` and `percent` fields (and 0 to `int`); `min` and `max` to the numeric
  fields; `must_advance` to `time` fields (4.8 "time fields"); `api_window` to `price` fields (2.8.2 "Prices
  against the API": a tick is a price tick). Anything else is an `OracleConfigError`, a harness fault at run time.
- The temporal checks compare with the previous readable read of the same region on the same side (the history
  the caller scopes: one flow run, or one checkpoint's captures). With no earlier read, reads closer together
  than `must_change_within_s`, a venue not open (or not closed) throughout the interval, rows that do not pair up,
  or for `must_advance` reads on different New York dates, the check is not judged: it passes with an `expected`
  that says so, as `sign_color_agrees` does for a zero value. The base build's read is never the reference: a
  reused base capture can be from any earlier time, and a lockstep one is simultaneous.
- Rows pair by position; `must_change_within_s` and `must_not_change_when_closed` compare typed values when every
  row parses, normalized text otherwise.
- The venue state of a displayed value is the state at the moment the data shows: the read time minus the
  paperMoney delay (`tables.delay_minutes`, 20 minutes unless the fixture says real time; 0 on a `frozen`
  checkpoint), since a delayed quote keeps moving for the delay after the close (2.8.2, 7.9). The calendar is
  the caller's (7.10: an exchange-calendar library, not vendored here); a region with a venue check and no
  calendar is an `OracleConfigError`, never a guess.
- `api_window`: the symbol is the evaluation context's (one symbol per checkpoint; every row is compared with it).
  The comparison is exact (0 ticks) when the symbol's last session closed more than `delay_s` before the read and
  it is absent from the 24-hour trading list (2.8.2). On a `frozen` checkpoint the API value is taken as of the
  fixture's pinned moment (`tables.api_comparison`), never as of the read time, within `tol_ticks` ticks.
- Sources: no rule field declares a source, so OCR is the declared source (2.8.1: OCR "applies to every
  checkpoint with rule regions") and the other sources the caller passes are the alternates. After a
  calibration-panel miss OCR is excluded; a region another source reads is judged, one that needed OCR is
  `harness_error (reason: ocr_calibration)`.
- Read times are aware datetimes; a naive one is taken as UTC (as `tables` does).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import cached_property
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..invariants import RULE_FIELDS, Rect, RuleRegion
from ..taxonomy import ASSERTION_ID, checkpoint_parts
from .assertions import AssertionResult
from .rules import DEFAULT_RULES, NO_CONTEXT
from .sources import ApiReader, ApiUnavailable, Attempt, Extraction, ValueSource, extract, problem
from .tables import EvalContext, OracleConfigError, RuleConfig, api_comparison, delay_minutes, tick_size
from .values import NUMERIC_FORMATS, FormatError, Value, normalize_text, parse, show

#: The rule fields of 4.8 in the order a region's checks are evaluated and reported.
CHECKS: tuple[str, ...] = ("field", "pattern", "decimals", "min", "max", "min_rows", "must_advance",
                           "must_change_within_s", "must_not_change_when_closed", "api_window")
#: The checks that compare two reads of a region (not re-run offline).
TEMPORAL: tuple[str, ...] = ("must_advance", "must_change_within_s", "must_not_change_when_closed")
REGION_RULE_KIND = "region_rule"
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_DELAY_S = 1200                           # 4.8 api_window.delay_s
DEFAULT_TOL_TICKS = 1                            # 4.8 api_window.tol_ticks
_DECIMAL_FIELDS = frozenset({"price", "money", "percent", "int"})
_API_WINDOW_KEYS = frozenset({"field", "delay_s", "tol_ticks"})
_VENUE_CHECKS = ("must_change_within_s", "must_not_change_when_closed")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _count(rule: Mapping[str, Any], key: str, region_id: str, *, low: int) -> int | None:
    v = rule.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < low:
        raise OracleConfigError(f"{region_id}: rule {key} must be an integer of at least {low} (4.8)")
    return v


def _flag(rule: Mapping[str, Any], key: str, region_id: str) -> bool:
    v = rule.get(key, False)
    if not isinstance(v, bool):
        raise OracleConfigError(f"{region_id}: rule {key} must be true or false (4.8)")
    return v


def _bound(rule: Mapping[str, Any], key: str, region_id: str) -> Decimal | None:
    v = rule.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        raise OracleConfigError(f"{region_id}: rule {key} must be a number (4.8)")
    return Decimal(str(v))


@dataclass(frozen=True)
class ApiWindow:
    """4.8 `api_window`: the API `field` as of the read time minus `delay_s`, within `tol_ticks` ticks (2.8.2)."""

    field: str
    delay_s: int = DEFAULT_DELAY_S
    tol_ticks: int = DEFAULT_TOL_TICKS

    @classmethod
    def from_dict(cls, region_id: str, window: Any) -> "ApiWindow":
        if not isinstance(window, Mapping) or not window.get("field") or set(window) - _API_WINDOW_KEYS:
            raise OracleConfigError(f"{region_id}: api_window is {{field, delay_s, tol_ticks}} (4.8)")
        delay = _count(window, "delay_s", region_id, low=0)
        ticks = _count(window, "tol_ticks", region_id, low=0)
        return cls(str(window["field"]), DEFAULT_DELAY_S if delay is None else delay,
                   DEFAULT_TOL_TICKS if ticks is None else ticks)


@dataclass(frozen=True)
class RegionRule:
    """The `rule` record of one `rule` region (4.8), validated."""

    region_id: str
    field: str
    decimals: int | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    must_advance: bool = False
    must_change_within_s: int | None = None
    must_not_change_when_closed: bool = False
    min_rows: int | None = None
    pattern: str | None = None
    api_window: ApiWindow | None = None

    @classmethod
    def from_dict(cls, region_id: str, rule: Any) -> "RegionRule":
        """Read a 4.8 `rule` mapping; anything the Oracle cannot evaluate is an `OracleConfigError`."""
        if not isinstance(rule, Mapping):
            raise OracleConfigError(f"{region_id}: a rule region carries a rule mapping (L-RGN-03)")
        unknown = set(rule) - set(CHECKS)
        if unknown:
            raise OracleConfigError(f"{region_id}: unknown rule fields {sorted(unknown)} (4.8)")
        fmt = rule.get("field")
        if fmt not in RULE_FIELDS:
            raise OracleConfigError(f"{region_id}: rule field {fmt!r} is not one of {RULE_FIELDS} (4.8)")
        decimals = _count(rule, "decimals", region_id, low=0)
        if decimals is not None and (fmt not in _DECIMAL_FIELDS or (fmt == "int" and decimals != 0)):
            raise OracleConfigError(f"{region_id}: decimals {decimals} does not apply to a {fmt} field")
        lo, hi = _bound(rule, "min", region_id), _bound(rule, "max", region_id)
        if (lo is not None or hi is not None) and fmt not in NUMERIC_FORMATS:
            raise OracleConfigError(f"{region_id}: min and max apply to numeric fields, not {fmt}")
        if lo is not None and hi is not None and lo > hi:
            raise OracleConfigError(f"{region_id}: rule min {lo} is above max {hi}")
        advance = _flag(rule, "must_advance", region_id)
        if advance and fmt != "time":
            raise OracleConfigError(f"{region_id}: must_advance applies to time fields (4.8), not {fmt}")
        pattern = rule.get("pattern")
        if pattern is not None:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise OracleConfigError(f"{region_id}: rule pattern {pattern!r} is not a regex: {exc}") from exc
        api = None
        if rule.get("api_window") is not None:
            if fmt != "price":
                raise OracleConfigError(f"{region_id}: api_window compares prices in ticks (2.8.2), not a {fmt}")
            api = ApiWindow.from_dict(region_id, rule["api_window"])
        out = cls(region_id=region_id, field=str(fmt), decimals=decimals, min=lo, max=hi, must_advance=advance,
                  must_change_within_s=_count(rule, "must_change_within_s", region_id, low=1),
                  must_not_change_when_closed=_flag(rule, "must_not_change_when_closed", region_id),
                  min_rows=_count(rule, "min_rows", region_id, low=0),
                  pattern=None if pattern is None else str(pattern), api_window=api)
        bad = [aid for aid in map(out.assertion_id, out.checks) if not ASSERTION_ID.match(aid)]
        if bad:
            raise OracleConfigError(f"{region_id}: {bad[0]!r} is not an assertion id (4.7 id grammar)")
        return out

    @property
    def checks(self) -> tuple[str, ...]:
        """The rule fields this region declares, in `CHECKS` order; `field` always."""
        declared = {"field": True, "pattern": self.pattern is not None, "decimals": self.decimals is not None,
                    "min": self.min is not None, "max": self.max is not None, "min_rows": self.min_rows is not None,
                    "must_advance": self.must_advance, "must_change_within_s": self.must_change_within_s is not None,
                    "must_not_change_when_closed": self.must_not_change_when_closed,
                    "api_window": self.api_window is not None}
        return tuple(c for c in CHECKS if declared[c])

    def assertion_id(self, check: str) -> str:
        return f"rule.{self.region_id}.{check}"


# --------------------------------------------------------------------------------------------
# Regions files


@dataclass(frozen=True)
class RegionEntry:
    """What the Oracle needs of one declared region: id, type, rule mapping and money-cell flag."""

    id: str
    type: str
    rule: Any
    money_cell: bool


def region_entries(regions_file: Any, checkpoint: str | None = None) -> list[RegionEntry]:
    """Every region of a regions file: a `diff.regions.RegionsFile` or the 4.8 mapping (None: no file, no
    regions). `checkpoint`, when given, must be the file's; a rule on a region not typed `rule` is refused
    (L-RGN-03, SD-29)."""
    if regions_file is None:
        return []
    if isinstance(regions_file, Mapping):
        cid = regions_file.get("checkpoint_id")
        entries = [RegionEntry(str(r.get("id")), str(r.get("type", "diff")), r.get("rule"),
                               bool(r.get("money_cell", False))) for r in regions_file.get("regions") or ()]
    else:
        cid = getattr(regions_file, "checkpoint_id", None)
        entries = [RegionEntry(r.id, r.type, r.rule, bool(r.money_cell)) for r in regions_file.regions]
    if checkpoint is not None and cid is not None and cid != checkpoint:
        raise OracleConfigError(f"regions file of {cid} given for checkpoint {checkpoint} (L-RGN-01)")
    for e in entries:
        if (e.rule is not None) != (e.type == "rule"):
            raise OracleConfigError(f"{e.id}: rule present if and only if type is rule (L-RGN-03, SD-29)")
    return entries


def rule_regions(regions_file: Any, checkpoint: str | None = None) -> tuple[RegionRule, ...]:
    """The validated rule of every `rule` region of a regions file, in file order."""
    return tuple(RegionRule.from_dict(e.id, e.rule) for e in region_entries(regions_file, checkpoint)
                 if e.type == "rule")


def _xywh(rect: Any) -> tuple[int, int, int, int]:
    if hasattr(rect, "x"):
        return int(rect.x), int(rect.y), int(rect.w), int(rect.h)
    x, y, w, h = rect
    return int(x), int(y), int(w), int(h)


def invariant_rule_regions(regions_file: Any, located: Mapping[str, Any]) -> tuple[RuleRegion, ...]:
    """`SurfaceRules.rule_regions` for one capture (INV-03, INV-04, 2.7): every rule region `located` places
    (absolute rects by region id, as `diff.regions.locate` returns them, or (x, y, w, h) tuples), with its format,
    decimals, pattern and range."""
    out = []
    for r in rule_regions(regions_file):
        if r.region_id in located:
            out.append(RuleRegion(r.region_id, Rect(*_xywh(located[r.region_id])), r.field, decimals=r.decimals,
                                  pattern=r.pattern, min=None if r.min is None else float(r.min),
                                  max=None if r.max is None else float(r.max)))
    return tuple(out)


# --------------------------------------------------------------------------------------------
# History, the venue calendar and the clock


@dataclass(frozen=True)
class PriorRead:
    """A readable read of a rule region at an earlier capture on the same side."""

    checkpoint_id: str
    read_at: datetime
    extraction: Extraction


class RegionHistory:
    """The previous readable read of each rule region, per side. Scope one history to what the temporal checks
    may compare: one flow run (its checkpoints in order), or one census or sweep checkpoint's captures."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], PriorRead] = {}

    def last(self, side: str, region_id: str) -> PriorRead | None:
        return self._last.get((side, region_id))

    def record(self, side: str, region_id: str, read: PriorRead) -> None:
        self._last[(side, region_id)] = read


class VenueCalendar(Protocol):
    """Whether the venue was open, or closed, throughout an interval (7.10 exchange calendar)."""

    def open_throughout(self, start: datetime, end: datetime) -> bool: ...

    def closed_throughout(self, start: datetime, end: datetime) -> bool: ...


@dataclass(frozen=True)
class RegularSession:
    """A venue's weekday session in America/New_York, default the XNYS regular session 09:30 to 16:00, with the
    caller's holidays and early closes (7.10: from the pinned exchange calendar)."""

    open: time = time(9, 30)
    close: time = time(16, 0)
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = field(default_factory=dict)

    def session(self, day: date) -> tuple[datetime, datetime] | None:
        """The day's session (open, close); None on a weekend or holiday."""
        if day.weekday() >= 5 or day in self.holidays:
            return None
        return (datetime.combine(day, self.open, NEW_YORK),
                datetime.combine(day, self.early_closes.get(day, self.close), NEW_YORK))

    def open_throughout(self, start: datetime, end: datetime) -> bool:
        start, end = _aware(start), _aware(end)
        s = self.session(start.astimezone(NEW_YORK).date())
        return s is not None and s[0] <= start and end <= s[1]

    def closed_throughout(self, start: datetime, end: datetime) -> bool:
        start, end = _aware(start), _aware(end)
        day, last = start.astimezone(NEW_YORK).date(), end.astimezone(NEW_YORK).date()
        while day <= last:
            s = self.session(day)
            if s is not None and s[0] < end and start < s[1]:
                return False
            day += timedelta(days=1)
        return True


class WallClock(Protocol):
    """A clock with an aware wall time (`orchestrator.clock.Clock` is one)."""

    def now(self) -> datetime: ...


def _read_time(clock: datetime | WallClock) -> datetime:
    return _aware(clock if isinstance(clock, datetime) else clock.now())


# --------------------------------------------------------------------------------------------
# One region on one capture


class _Fail(Exception):
    def __init__(self, expected: str, extracted: str, detail: str):
        super().__init__(detail)
        self.expected, self.extracted, self.detail = expected, extracted, detail


class _Stop(Exception):
    """Ends one check with a status other than passed or failed."""

    def __init__(self, status: str, detail: str, *, unreadable: Sequence[str] = (), reason: str | None = None,
                 gate: str | None = None):
        super().__init__(detail)
        self.status, self.detail, self.unreadable = status, detail, tuple(unreadable)
        self.reason, self.gate = reason, gate


@dataclass(frozen=True)
class _Cell:
    row: int
    text: str
    value: Any                                   # the typed value; None when the row is not a valid value
    error: str | None


def _cells(fmt: str, ext: Extraction) -> list[_Cell]:
    out = []
    for i, text in enumerate(ext.rows(), start=1):
        try:
            out.append(_Cell(i, text, parse(fmt, text), None))
        except FormatError as exc:
            out.append(_Cell(i, text, None, str(exc)))
    return out


def _shown_decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _comparable(fmt: str, ext: Extraction) -> list[Value]:
    """Typed rows when every row parses, normalized text otherwise (the temporal comparisons)."""
    cells = _cells(fmt, ext)
    if all(c.error is None for c in cells):
        return [c.value for c in cells]
    return [normalize_text(c.text) for c in cells]


@dataclass
class _Region:
    """One rule region's evaluation on one capture."""

    rule: RegionRule
    ext: Extraction
    read_at: datetime
    prior: PriorRead | None = None
    calendar: VenueCalendar | None = None
    delay: timedelta = timedelta(0)

    @cached_property
    def cells(self) -> list[_Cell]:
        return _cells(self.rule.field, self.ext)

    def valid(self) -> list[_Cell]:
        return [c for c in self.cells if c.error is None]

    def summary(self) -> str:
        rows = self.ext.rows()
        return repr(rows[0]) if len(rows) == 1 else f"{len(rows)} rows"

    def where(self, cell: _Cell) -> str:
        rid = self.rule.region_id
        return f"{rid} reads {cell.text!r}" if len(self.cells) == 1 else f"{rid} row {cell.row} reads {cell.text!r}"

    # -- single-read checks ------------------------------------------------------------------
    def check_field(self) -> tuple[str, str]:
        fmt = self.rule.field
        want = f"a valid {fmt} in every row"
        bad = next((c for c in self.cells if c.error is not None), None)
        if bad is not None:
            raise _Fail(want, repr(bad.text), f"{self.where(bad)}, not a valid {fmt}: {bad.error} (R10)")
        return want, self.summary()

    def check_pattern(self) -> tuple[str, str]:
        pat = self.rule.pattern or ""
        miss = next((c for c in self.cells if not re.search(pat, normalize_text(c.text))), None)
        if miss is not None:
            raise _Fail(f"/{pat}/", repr(miss.text), f"{self.where(miss)}, which does not match /{pat}/")
        return f"/{pat}/ in every row", self.summary()

    def check_decimals(self) -> tuple[str, str]:
        n = self.rule.decimals
        miss = next((c for c in self.valid() if _shown_decimals(c.value) != n), None)
        if miss is not None:
            shown = _shown_decimals(miss.value)
            raise _Fail(f"{n} decimals", f"{shown} decimals in {miss.text!r}",
                        f"{self.where(miss)} with {shown} decimals; the rule says {n}")
        return f"{n} decimals", self.summary()

    def _bound(self, word: str, bound: Decimal, outside: Callable[[Decimal, Decimal], bool]) -> tuple[str, str]:
        fmt = self.rule.field
        want = f"{word} {show(fmt, bound)}"
        miss = next((c for c in self.valid() if outside(c.value, bound)), None)
        if miss is not None:
            raise _Fail(want, show(fmt, miss.value), f"{self.where(miss)}; the rule says {want}")
        return want, self.summary()

    def check_min(self) -> tuple[str, str]:
        assert self.rule.min is not None
        return self._bound("at least", self.rule.min, lambda v, b: v < b)

    def check_max(self) -> tuple[str, str]:
        assert self.rule.max is not None
        return self._bound("at most", self.rule.max, lambda v, b: v > b)

    def check_min_rows(self) -> tuple[str, str]:
        n, got = self.rule.min_rows or 0, len(self.ext.rows())
        want = f"at least {n} rows"
        if got < n:
            raise _Fail(want, f"{got} rows", f"{self.rule.region_id} holds {got} non-empty rows; the rule says {want}")
        return want, f"{got} rows"

    # -- temporal checks ---------------------------------------------------------------------
    def _not_judged(self, want: str, why: str) -> tuple[str, str]:
        return f"{want} (not judged: {why})", self.summary()

    def _since(self) -> tuple[PriorRead, float]:
        assert self.prior is not None
        return self.prior, (self.read_at - _aware(self.prior.read_at)).total_seconds()

    def _venue(self, state: str) -> bool:
        """Whether the venue was `open` (or `closed`) throughout the interval the two reads show."""
        assert self.prior is not None and self.calendar is not None
        start, end = _aware(self.prior.read_at) - self.delay, self.read_at - self.delay
        if state == "open":
            return self.calendar.open_throughout(start, end)
        return self.calendar.closed_throughout(start, end)

    def _changed(self) -> tuple[bool, str, str]:
        assert self.prior is not None
        before = self.prior.extraction
        return (_comparable(self.rule.field, before) != _comparable(self.rule.field, self.ext),
                repr(before.text), repr(self.ext.text))

    def _no_prior(self) -> str:
        return f"no earlier read of {self.rule.region_id} on this side"

    def check_must_advance(self) -> tuple[str, str]:
        want = "a time later than the previous read"
        if self.prior is None:
            return self._not_judged(want, self._no_prior())
        prior, elapsed = self._since()
        before = _cells("time", prior.extraction)
        if len(before) != len(self.cells) or any(c.error for c in before + self.cells):
            return self._not_judged(want, "the two reads do not pair up row by row")
        if _aware(prior.read_at).astimezone(NEW_YORK).date() != self.read_at.astimezone(NEW_YORK).date():
            return self._not_judged(want, "the reads lie on different New York dates")
        for b, c in zip(before, self.cells, strict=True):
            if not c.value > b.value:
                raise _Fail(f"later than {b.text!r} ({prior.checkpoint_id})", repr(c.text),
                            f"{self.where(c)}, not later than {b.text!r} read at {prior.checkpoint_id} "
                            f"{elapsed:.0f} s earlier")
        return want, self.summary()

    def check_must_change_within_s(self) -> tuple[str, str]:
        n = self.rule.must_change_within_s or 0
        want = f"a change within {n} s while the venue is open"
        if self.prior is None:
            return self._not_judged(want, self._no_prior())
        prior, elapsed = self._since()
        if elapsed < n:
            return self._not_judged(want, f"the reads are {elapsed:.0f} s apart")
        if not self._venue("open"):
            return self._not_judged(want, "the venue was not open throughout the interval the reads show")
        changed, before, now = self._changed()
        if not changed:
            raise _Fail(want, now, f"{self.rule.region_id} still reads {now} {elapsed:.0f} s after "
                                   f"{prior.checkpoint_id} read {before}, with the venue open throughout")
        return want, now

    def check_must_not_change_when_closed(self) -> tuple[str, str]:
        want = "no change while the venue is closed"
        if self.prior is None:
            return self._not_judged(want, self._no_prior())
        prior, elapsed = self._since()
        if not self._venue("closed"):
            return self._not_judged(want, "the venue was open during the interval the reads show")
        changed, before, now = self._changed()
        if changed:
            raise _Fail(f"{before} as at {prior.checkpoint_id}", now,
                        f"{self.rule.region_id} reads {now}; it read {before} at {prior.checkpoint_id} "
                        f"{elapsed:.0f} s earlier, with the venue closed throughout")
        return want, now


# --------------------------------------------------------------------------------------------
# The checker


@dataclass
class _RunState:
    """What one run's checks share: the market-data API lost mid-run (2.8.1)."""

    api_lost: str | None = None


def _readable(operands: Mapping[str, Extraction], needed_ocr: bool) -> None:
    """Stop the check when an operand is unreadable: `harness_error (ocr_calibration)` when the region needed OCR
    after a calibration-panel miss, else `unreadable` (`needs_human (reason: oracle_unreadable)`, 2.8.1)."""
    bad = [e for e in operands.values() if not e.readable]
    if not bad:
        return
    if needed_ocr:
        raise _Stop("harness_error", f"{bad[0].field} needed OCR after a calibration-panel miss (2.8.1)",
                    reason="ocr_calibration")
    reads = " | ".join(f"{e.field}: {e.reads()}" for e in bad)
    raise _Stop("unreadable", f"unreadable on every source: {reads}", unreadable=[e.field for e in bad])


def _result(region: _Region, check: str, operands: Mapping[str, Extraction],
            judge: Callable[[], tuple[str, str]]) -> AssertionResult:
    aid, rid = region.rule.assertion_id(check), region.rule.region_id
    try:
        expected, extracted = judge()
        return AssertionResult(aid, REGION_RULE_KIND, "passed", expected, extracted, None, dict(operands), region=rid)
    except _Fail as f:
        return AssertionResult(aid, REGION_RULE_KIND, "failed", f.expected, f.extracted, f.detail, dict(operands),
                               region=rid)
    except _Stop as s:
        return AssertionResult(aid, REGION_RULE_KIND, s.status, detail=s.detail, operands=dict(operands),
                               unreadable=s.unreadable, reason=s.reason, gate=s.gate, region=rid)


class RegionRuleChecker:
    """The Oracle's rule-region evaluator for one run (module docstring).

    `fixture` is the parsed fixture (4.9: the paperMoney delay, symbols for ticks, a frozen API moment); `config`
    the rule tables (ticks per root); `context` the default evaluation context (data mode, symbol, last session
    close, 24-hour trading list), which `check(context=...)` replaces per checkpoint; `calendar` the venue calendar
    of the venue checks. `ocr_calibrated` is False after a calibration-panel miss at `canary_flow_passed`;
    `api_preflight_ok` is False when preflight N3 failed. A market-data API failure during a check makes the run's
    remaining `api_window` checks `env_error (gate: connectivity)` (2.8.1)."""

    def __init__(self, *, fixture: Mapping[str, Any] | None = None, config: RuleConfig = DEFAULT_RULES,
                 context: EvalContext = NO_CONTEXT, calendar: VenueCalendar | None = None,
                 ocr_calibrated: bool = True, api_preflight_ok: bool = True) -> None:
        self.fixture: Mapping[str, Any] = fixture or {}
        self.config, self.context, self.calendar = config, context, calendar
        self.ocr_calibrated, self.api_preflight_ok = ocr_calibrated, api_preflight_ok
        self.state = _RunState()

    def delay(self, context: EvalContext) -> timedelta:
        """The data delay the venue checks subtract: 0 on a frozen checkpoint, else the fixture's (7.9)."""
        return timedelta(0) if context.data_mode == "frozen" else timedelta(minutes=delay_minutes(self.fixture))

    def check(self, checkpoint: str, side: str, regions_file: Any,
              sources: Mapping[str, ValueSource | ApiReader], history: RegionHistory | None,
              clock: datetime | WallClock, *, context: EvalContext | None = None) -> list[AssertionResult]:
        """Every rule region of `regions_file` (a `diff.regions.RegionsFile` or the 4.8 mapping; None: none) on
        one side's capture of `checkpoint`. `sources` are keyed `ocr`, `jab`, `export` (readers of the region's
        cell) and `api`; `clock` is the read time or a clock whose `now()` is. Each readable read is recorded in
        `history` after its checks. Raises `OracleConfigError` for a rule the Oracle cannot evaluate."""
        checkpoint_parts(checkpoint)
        if side not in ("candidate", "base"):
            raise ValueError(f"side {side!r} is candidate or base")
        ctx = context or self.context
        rules = rule_regions(regions_file, checkpoint)
        for rule in rules:
            if self.calendar is None and any(c in rule.checks for c in _VENUE_CHECKS):
                raise OracleConfigError(f"{rule.region_id}: a venue check needs the exchange calendar (7.10)")
        read_at = _read_time(clock)
        readers = {k: v for k, v in sources.items() if k != "api"}
        out: list[AssertionResult] = []
        for rule in rules:
            ext = extract(rule.region_id, rule.field, readers, "ocr",       # type: ignore[arg-type]
                          exclude=frozenset() if self.ocr_calibrated else frozenset({"ocr"}))
            prior = history.last(side, rule.region_id) if history is not None else None
            region = _Region(rule, ext, read_at, prior, self.calendar, self.delay(ctx))
            needed_ocr = not ext.readable and not self.ocr_calibrated and "ocr" in readers
            for check in rule.checks:
                out.append(self._check(region, check, sources.get("api"), ctx, needed_ocr))
            if ext.readable and history is not None:
                history.record(side, rule.region_id, PriorRead(checkpoint, read_at, ext))
        return out

    def recheck(self, rule: RegionRule, ext: Extraction, read_at: datetime, *, api_read: Extraction | None = None,
                context: EvalContext | None = None) -> list[AssertionResult]:
        """The offline re-run of one stored read (2.8.1, SD-09): the single-read checks of `rule` and, given the
        stored API read, `api_window`. The temporal checks compare captures in order under a calendar and are not
        re-run; an `api_window` with no stored API read is skipped."""
        ctx = context or self.context
        region = _Region(rule, ext, _aware(read_at))
        out = []
        for check in rule.checks:
            if check in TEMPORAL or (check == "api_window" and api_read is None):
                continue
            operands = {"region": ext}
            if check == "api_window" and api_read is not None:
                operands["api"] = api_read
            out.append(_result(region, check, operands, self._judge_fn(region, check, operands, ctx)))
        return out

    # -- one check -----------------------------------------------------------------------------
    def _check(self, region: _Region, check: str, api: Any, context: EvalContext, needed_ocr: bool) -> AssertionResult:
        operands: dict[str, Extraction] = {"region": region.ext}
        prior = region.prior
        if prior is not None and check in TEMPORAL:
            operands["previous"] = replace(prior.extraction, field=f"{prior.checkpoint_id}:{region.rule.region_id}")
        if check == "api_window":
            return _result(region, check, operands, lambda: self._api_window(region, operands, api, context,
                                                                             needed_ocr))
        return _result(region, check, operands, self._judge_fn(region, check, operands, context, needed_ocr))

    def _judge_fn(self, region: _Region, check: str, operands: Mapping[str, Any], context: EvalContext,
                  needed_ocr: bool = False) -> Callable[[], tuple[str, str]]:
        def judge() -> tuple[str, str]:
            if check == "api_window":
                return self._judge_api(region, operands, context, as_of=None)
            _readable({"region": region.ext}, needed_ocr)
            judged: tuple[str, str] = getattr(region, f"check_{check}")()
            return judged
        return judge

    def _api_window(self, region: _Region, operands: dict[str, Extraction], api: Any, context: EvalContext,
                    needed_ocr: bool) -> tuple[str, str]:
        """2.8.1 Market-data API: a failed preflight N3 or an API lost earlier in the run is an environment fault
        ahead of any read problem (1.10 precedence); then the API value as of the modeled moment."""
        rule, window = region.rule, region.rule.api_window
        assert window is not None
        if not self.api_preflight_ok:
            raise _Stop("env_error", "market-data API preflight N3 failed", gate="preflight.N3")
        if self.state.api_lost:
            raise _Stop("env_error", f"market-data API lost mid-run: {self.state.api_lost}", gate="connectivity")
        if api is None:
            raise OracleConfigError(f"{rule.region_id}: api_window needs the market-data API source")
        symbol = context.need_symbol("an api_window rule")
        as_of = (api_comparison(replace(context, capture_time=region.read_at), self.fixture).as_of
                 if context.data_mode == "frozen" else region.read_at - timedelta(seconds=window.delay_s))
        try:
            reading = api.quote(symbol, window.field, as_of)
        except ApiUnavailable as exc:
            self.state.api_lost = str(exc)
            raise _Stop("env_error", f"market-data API failed mid-run: {exc}", gate="connectivity") from exc
        label = f"api:{window.field}"
        api_reading = replace(reading, field=label) if reading is not None else None
        rejected = problem(api_reading, "price")
        operands["api"] = Extraction(label, "price", "api", (Attempt("api", api_reading, rejected),),
                                     api_reading if rejected is None else None)
        if needed_ocr:
            _readable({"region": region.ext}, needed_ocr)
        return self._judge_api(region, operands, context, as_of=as_of)

    def _judge_api(self, region: _Region, operands: Mapping[str, Extraction], context: EvalContext, *,
                   as_of: datetime | None) -> tuple[str, str]:
        """Every valid row within `tol_ticks` ticks of the API value; exact for a symbol whose session closed more
        than `delay_s` before the read and that is off the 24-hour trading list (2.8.2)."""
        window = region.rule.api_window
        assert window is not None
        _readable(operands, False)
        symbol = context.need_symbol("an api_window rule")
        try:
            api_value = parse("price", operands["api"].text)
        except FormatError as exc:
            raise _Stop("unreadable", f"API value {operands['api'].text!r} is not a valid price: {exc}",
                        unreadable=[operands["api"].field]) from exc
        assert isinstance(api_value, Decimal)
        close = context.session_close
        exact = (context.data_mode != "frozen" and symbol not in context.trading_24h and close is not None
                 and region.read_at - _aware(close) > timedelta(seconds=window.delay_s))
        ticks = 0 if exact else window.tol_ticks
        tol = ticks * tick_size(symbol, self.config, self.fixture, api_value)
        moment = f" as of {as_of.isoformat()}" if as_of is not None else ""
        why = (f"{symbol}'s session closed more than {window.delay_s} s before the read: exact" if exact
               else f"within {ticks} ticks")
        want = f"{show('price', api_value)} +/- {tol.normalize():f} (API {window.field}{moment}; {why})"
        miss = next((c for c in region.valid() if abs(c.value - api_value) > tol), None)
        if miss is not None:
            raise _Fail(want, show("price", miss.value), f"{region.where(miss)}; API {window.field}{moment} is "
                                                         f"{show('price', api_value)} ({why})")
        return want, region.summary()
