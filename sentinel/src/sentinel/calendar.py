"""The exchange calendar, the operating day, the quiet and maintenance windows and DST (7.10, 7.11, 10.7, 11.1 PA-12,
11.2, 12.7 G4; SD-67, SD-76).

No network and no new dependency (7.10 "vendored and pinned in the harness"): the NYSE (XNYS) and CME Globex (CMES)
holiday and early-close rules are pure Python here, pinned by `CALENDAR_HORIZON`, and time zones come from the
standard library's `zoneinfo`. Every instant this module returns is an aware datetime in UTC, which compares and adds
correctly against any aware datetime; `ny()` gives the America/New_York wall clock for display. Inputs must be
aware (a naive datetime is refused): the Orchestrator's clock is tz-aware America/New_York (7.10).

What it provides, and who calls it:

- `WindowsConfig.from_doc` reads `schedule/windows.yaml` (4.16); `config_problems` is the same check for lint
  L-XREF-24 (`lint/checks/windows.py`).
- The calendar: `is_session_day`, `is_business_day`, `business_days`, `holidays` (the `holidays=` argument of
  churn, quarantine, the fix list and the store lifecycle), `sessions` and `symbol_calendar`.
- Windows (7.10): `window(name, at, config, symbol=..., overnight=...)` resolves a named window around an instant;
  `next_all_quiet` is the Calibration run's window (11.2, 7.13); `is_crypto_future` is the always-live profile.
- The operating day (11.1 PA-12, SD-76): `is_operating_time`, `operating_start` (a PR, merge or RC run triggered
  outside it waits until 06:00; Nightly and Calibration runs are exempt) and `operating_day_end`.
- The windowed-flow scheduler (7.10): `plan_windowed(items, now, p95, config, run_type=...)` refuses a windowed
  flow whose p95 cannot finish before its window, or the operating day, ends: `not_run (reason: budget)`.
- Preflight: `g4_horizon_check` (12.7 G4) and `maintenance_reschedule` (7.11, 10.7, 12.7: an N1, N3 or N4 failure
  inside a `maintenance_windows` entry reschedules the run once to the window end plus 15 minutes).
- `lockstep_pause` (10.7 Network, 11.3): a paused masked_live checkpoint pauses its lockstep partner.
- DST (7.10, 7.1): `dst_transitions(year)`, `time_axis_check_due` (the Nightly that follows a transition re-runs the
  time-axis label check) and `time_axis_check` (the label comparison itself).

Choices where the spec is ambiguous (each is reported):

1. Sessions are the hours quotes move in the app, which is what a quiet window is cut from (7.10): XNYS from the
   07:00 pre-market open to 4 hours after the close (16:00, or 13:00 on an early close: the extended order sessions
   7.10 reports end at 20:00); CMES Globex from 18:00 the evening before the trade date to 17:00 (13:00 on the
   holiday halts, 12:00 CT; 13:15 on the day after Thanksgiving, Christmas Eve and July 3, 12:15 CT); forex 24/5
   from Sunday 17:00 to Friday 17:00; CME crypto futures 24/7 less the Saturday 2:00 to 4:00 CT break.
2. Holiday rules are the current ones: XNYS closes on New Year's Day (Saturday: not observed), Martin Luther King
   Jr. Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day,
   Thanksgiving and Christmas (Saturday to Friday, Sunday to Monday), closes at 13:00 on July 3 (Monday to
   Thursday), the day after Thanksgiving and December 24 (Monday to Thursday), and was closed on 2025-01-09. CMES
   closes on New Year's Day, Good Friday and Christmas and halts early on the other XNYS holidays and early closes.
   `CALENDAR_HORIZON` is the last day these were checked against the exchanges' published calendars; preflight G4
   fails when the run date plus 90 days passes it, so the rules are re-verified before they run out.
3. A window `start` with a `calendar` moves back over that calendar's holidays to the last session day of the week;
   an `end` with a `calendar` moves forward over the holidays that follow it. The time of day stays as configured,
   but a move never carries the window into a session of `QUIET_CALENDARS` (XNYS, CMES or forex, choice 1): a quiet
   window is one in which "equities, futures, forex [are] closed" (7.10), so a moved start is no earlier than the
   last of those sessions to close before the configured start, and a moved end no later than the first to open
   after the configured end. On Juneteenth, July 3 observed, Good Friday and Christmas Fridays all_quiet therefore
   starts Friday 17:00 (Globex halts at 13:00, forex trades to 17:00), not Thursday evening.
4. `equities_quiet` is the time between XNYS extended sessions (choice 1), never for a symbol on the 24-hour list
   read this run; `futures_break` is the time between CMES sessions; no window exists for a CME crypto future
   (`CRYPTO_FUTURE_ROOTS`: /BTC and /MBT, 7.10), so `window` returns None for one and the scheduler refuses it.
5. The scheduler: `p95` is keyed by item id (the flow id); an item with no measured p95 uses its declared
   `max_duration_s`, else 5 minutes (11.2 "5 min until measured"). `plan_windowed` models one queue (one rig or
   pair): the windowed items run in the order given, each from the later of the previous item's p95 end and its
   window's opening. A window that opens after the run deadline, an end past the window, past 22:00 for a PR, merge
   or RC run, or past the deadline are all `not_run (reason: budget)` with the cause in `detail`.
6. Maintenance reschedule: the failure time is the second attempt's (one retry per item, 7.11); the run is
   rescheduled only when every failed item is a network item (N1 to N7) and one of them is N1, N3 or N4, since a
   rig, build, harness or registry failure keeps its own outcome; "once" counts the run's earlier maintenance
   reschedules (`previous_reschedules`). Windows are half open: a failure at the end minute is outside.
7. Lockstep pause: "in the same minute" is the 60 s of 3.1 (`orchestrator.lockstep.LOCKSTEP_WINDOW_S`); the pause
   charged to the wall budget runs from the pause to the later of the two resumes; a partner that never resumes, or
   resumes more than 60 s after the paused rig, makes the checkpoint `not_run (reason: budget)`.
8. DST: the Nightly that follows a transition is the first whose start has a different New York UTC offset from the
   previous Nightly's start (default: 24 hours earlier). The time-axis label check compares each OCR label (H:MM
   or HH:MM) with the New York wall time of the bar it marks; a mismatch is the app chart time-zone mismatch of the
   7.1 Time zone row, `env_error (gate: settings_attested, reason: fixture_drift)`.
9. G4 (12.7) resolves each fixture symbol's calendar by its 4.6 `asset` (equity, option and index on XNYS; future
   and future_option on CMES, or the crypto profile; forex 24/5) over the run date plus 90 days, needs the 24-hour
   list read this run (`overnight` not None) and the America/New_York zone data; any miss is `env_error (gate:
   preflight.G4)` for every scheduled checkpoint, fed to the gate as `PreflightReport.items["G4"]`.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .orchestrator.lockstep import LOCKSTEP_WINDOW_S, slip_seconds
from .orchestrator.results import Outcome
from .records import DATA_MODES

NEW_YORK = "America/New_York"
UTC = timezone.utc
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WINDOW_NAMES = ("all_quiet", "equities_quiet", "futures_break", "none")       # 4.7, 7.10, L-XREF-24
NO_WINDOW = "none"
CONFIG_CALENDARS = ("XNYS", "CMES")                                            # 4.16 quiet_window.calendar
CALENDARS = CONFIG_CALENDARS + ("FX", "CRYPTO")                                # choice 1: the session profiles
QUIET_CALENDARS = CONFIG_CALENDARS + ("FX",)       # choice 3: closed in a quiet window; crypto is excluded by profile
RUN_TYPES = ("pr", "merge", "rc", "nightly", "calibration")
EXEMPT_RUN_TYPES = frozenset({"nightly", "calibration"})                       # 7.10, 11.1 PA-12, SD-76
RESCHEDULE_ITEMS = ("N1", "N3", "N4")                                          # 7.11, 12.7
RESCHEDULE_DELAY = timedelta(minutes=15)                                       # "window end plus 15 minutes"
G4_HORIZON_DAYS = 90                                                           # 7.10, 12.7 G4
CALENDAR_HORIZON = date(2028, 12, 31)                                          # choice 2
DEFAULT_FLOW_S = 300.0                                                         # 11.2: 5 min until measured
CRYPTO_FUTURE_ROOTS = ("/BTC", "/MBT")                                         # 7.10 CME crypto futures
MONTH_CODES = "FGHJKMNQUVXZ"

PRE_MARKET_OPEN = time(7, 0)                                                   # 7.10 extended hours (choice 1)
XNYS_CLOSE, XNYS_EARLY_CLOSE = time(16, 0), time(13, 0)
AFTER_HOURS = timedelta(hours=4)                                               # 16:00 -> 20:00, 13:00 -> 17:00
CMES_OPEN, CMES_CLOSE = time(18, 0), time(17, 0)                               # 17:00 to 18:00 daily break, ET
CMES_HOLIDAY_HALT, CMES_SHORT_HALT = time(13, 0), time(13, 15)                 # 12:00 CT, 12:15 CT
FX_ROLL = time(17, 0)                                                          # forex 24/5, Sunday 17:00 to Fri 17:00
CRYPTO_BREAK = (time(3, 0), time(5, 0))                                        # Saturday 2:00 to 4:00 CT

RULE_EQUITIES_QUIET = "XNYS closed and symbol not in overnight list"           # 7.10 windows.yaml
RULE_FUTURES_BREAK = "CMES daily break 17:00 to 18:00, or CMES weekend close"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_CRYPTO = re.compile(r"^/(?:BTC|MBT)(?:(?![A-Z])|[" + MONTH_CODES + r"]\d{2})")
_LABEL = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_LOOKBACK, _LOOKAHEAD = timedelta(days=10), timedelta(days=21)                 # session span around a window lookup


class WindowsConfigError(ValueError):
    """`schedule/windows.yaml` cannot drive the scheduler; `problems` lists (path, message) as L-XREF-24 reports."""

    def __init__(self, problems: Sequence[tuple[str, str]]):
        super().__init__("schedule/windows.yaml: " + "; ".join(f"{p or '(root)'}: {m}" for p, m in problems))
        self.problems = tuple(problems)


# -- time ---------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _zone() -> ZoneInfo:
    return ZoneInfo(NEW_YORK)


def ny(at: datetime) -> datetime:
    """The America/New_York wall clock of an aware instant (for display and health lines)."""
    return to_utc(at).astimezone(_zone())


def to_utc(at: datetime) -> datetime:
    """The same instant in UTC; a naive datetime is refused (7.10: tz-aware datetimes throughout)."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{at!r} is naive; the scheduler works on tz-aware datetimes (7.10)")
    return at.astimezone(UTC)


def wall_time(d: date, t: time) -> datetime:
    """The instant (UTC) of New York wall time `t` on date `d`; a time skipped by the spring change reads as EST."""
    return datetime.combine(d, t, tzinfo=_zone()).astimezone(UTC)


def _hhmm(text: str) -> time:
    m = _HHMM.match(text)
    if not m:
        raise ValueError(f"{text!r} is not HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def fmt_ny(at: datetime) -> str:
    """`Sat 2026-11-07 23:10 EST`: an instant as the run-health lines print it."""
    local = ny(at)
    return f"{WEEKDAYS[local.weekday()]} {local:%Y-%m-%d %H:%M} {local.tzname()}"


@dataclass(frozen=True)
class Span:
    """A half-open interval [start, end) of aware UTC instants."""

    start: datetime
    end: datetime

    def __contains__(self, at: datetime) -> bool:
        return self.start <= to_utc(at) < self.end

    def __str__(self) -> str:
        return f"{fmt_ny(self.start)} to {fmt_ny(self.end)}"


# -- holiday rules (choice 2) -------------------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (0 = Monday) of a month; n = -1 is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (the anonymous Gregorian algorithm)."""
    a, (b, c) = year % 19, divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, *, saturday: bool = True) -> date | None:
    """A fixed-date holiday's weekday: Saturday to Friday (none when `saturday` is False), Sunday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


XNYS_ADHOC_CLOSURES = frozenset({date(2025, 1, 9)})                            # national day of mourning


@lru_cache(maxsize=None)
def _rules(calendar: str, year: int) -> Mapping[date, time | None]:
    """Date -> None (closed all day) or the early close / halt time (New York) of one calendar year."""
    mon, thu = 0, 3
    new_year = _observed(date(year, 1, 1), saturday=False)
    good_friday = _easter(year) - timedelta(days=2)
    christmas = _observed(date(year, 12, 25))
    others = [_nth_weekday(year, 1, mon, 3), _nth_weekday(year, 2, mon, 3), _nth_weekday(year, 5, mon, -1),
              _observed(date(year, 7, 4)), _nth_weekday(year, 9, mon, 1), _nth_weekday(year, 11, thu, 4)]
    if year >= 2022:
        others.append(_observed(date(year, 6, 19)))
    short = [_nth_weekday(year, 11, thu, 4) + timedelta(days=1)]
    short += [d for d in (date(year, 7, 3), date(year, 12, 24)) if d.weekday() <= thu]
    out: dict[date, time | None] = {d: (XNYS_EARLY_CLOSE if calendar == "XNYS" else CMES_SHORT_HALT) for d in short}
    for d in others:
        if d is not None:
            out[d] = None if calendar == "XNYS" else CMES_HOLIDAY_HALT
    for d in (new_year, good_friday, christmas):
        if d is not None:
            out[d] = None
    if calendar == "XNYS":
        out.update({d: None for d in XNYS_ADHOC_CLOSURES if d.year == year})
    return out


def _check_calendar(calendar: str, allowed: Sequence[str] = CALENDARS) -> None:
    if calendar not in allowed:
        raise ValueError(f"unknown calendar {calendar!r}; one of {', '.join(allowed)}")


def is_session_day(calendar: str, d: date) -> bool:
    """True when `d` is a trade date of the calendar: a weekday that is not a full holiday (every day for CRYPTO)."""
    _check_calendar(calendar)
    if calendar == "CRYPTO":
        return True
    if d.weekday() >= 5:
        return False
    if calendar == "FX":
        return True
    rules = _rules(calendar, d.year)
    return d not in rules or rules[d] is not None


def is_business_day(d: date, calendar: str = "XNYS") -> bool:
    """A business day of the exchange calendar (XNYS by default): the business days of 1.4, 9.12, 10.5 and 10.9."""
    _check_calendar(calendar, CONFIG_CALENDARS)
    return is_session_day(calendar, d)


def _days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def business_days(start: date, end: date, calendar: str = "XNYS") -> tuple[date, ...]:
    """The business days from `start` to `end`, both included."""
    return tuple(d for d in _days(start, end) if is_business_day(d, calendar))


def holidays(start: date, end: date, calendar: str = "XNYS") -> tuple[date, ...]:
    """The weekdays from `start` to `end` on which the calendar is closed all day: the `holidays` argument of the
    business-day helpers (store lifecycle, quarantine, the fix list) and of `churn.generate`."""
    _check_calendar(calendar, CONFIG_CALENDARS)
    return tuple(d for d in _days(start, end) if d.weekday() < 5 and not is_session_day(calendar, d))


def _session(calendar: str, d: date) -> list[Span]:
    """The quote-moving session(s) of trade date `d` (choice 1); [] on a non-session day."""
    if not is_session_day(calendar, d):
        return []
    if calendar == "XNYS":
        close = _rules("XNYS", d.year).get(d) or XNYS_CLOSE
        return [Span(wall_time(d, PRE_MARKET_OPEN), wall_time(d, close) + AFTER_HOURS)]
    if calendar == "CMES":
        close = _rules("CMES", d.year).get(d) or CMES_CLOSE
        return [Span(wall_time(d - timedelta(days=1), CMES_OPEN), wall_time(d, close))]
    if calendar == "FX":
        return [Span(wall_time(d - timedelta(days=1), FX_ROLL), wall_time(d, FX_ROLL))]
    day = Span(wall_time(d, time(0)), wall_time(d + timedelta(days=1), time(0)))
    if d.weekday() != 5:
        return [day]
    return [Span(day.start, wall_time(d, CRYPTO_BREAK[0])), Span(wall_time(d, CRYPTO_BREAK[1]), day.end)]


def sessions(calendar: str, start: date, end: date) -> tuple[Span, ...]:
    """The sessions of the trade dates from `start` to `end` (choice 1), in order."""
    _check_calendar(calendar)
    return tuple(s for d in _days(start, end) for s in _session(calendar, d))


def is_crypto_future(symbol: str) -> bool:
    """A CME crypto future or future option (/BTC, /MBT; 7.10): always live, no quiet window."""
    return bool(_CRYPTO.match(symbol.strip().removeprefix(".")))


def symbol_calendar(symbol: str, asset: str) -> str:
    """The session calendar of a fixture symbol by its 4.6 `asset` (choice 9)."""
    if asset in ("equity", "option", "index"):
        return "XNYS"
    if asset in ("future", "future_option"):
        return "CRYPTO" if is_crypto_future(symbol) else "CMES"
    if asset == "forex":
        return "FX"
    raise ValueError(f"symbol {symbol!r}: asset {asset!r} has no calendar")


# -- windows.yaml -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Anchor:
    """A quiet window edge (4.16 `quiet_window.start` / `end`)."""

    weekday: str
    time: time
    calendar: str | None = None


@dataclass(frozen=True)
class QuietWindow:
    """One named window of 7.10: weekly edges, or a calendar rule the scheduler resolves; `none` has neither."""

    name: str
    start: Anchor | None = None
    end: Anchor | None = None
    rule: str | None = None


@dataclass(frozen=True)
class MaintenanceWindow:
    """A known backend window (4.16 `maintenance_windows`); an `end` at or before `start` ends the next day."""

    weekday: str
    start: time
    end: time
    source: str

    def span_on(self, d: date) -> Span:
        """The occurrence that starts on date `d`."""
        return Span(wall_time(d, self.start), wall_time(d + timedelta(days=self.end <= self.start), self.end))

    def __str__(self) -> str:
        return f"{self.weekday} {self.start:%H:%M}-{self.end:%H:%M} ({self.source})"


@dataclass(frozen=True)
class WindowsConfig:
    """`schedule/windows.yaml` (4.16, 7.10)."""

    operating_start: time
    operating_end: time
    windows: Mapping[str, QuietWindow]
    maintenance: tuple[MaintenanceWindow, ...] = ()

    @classmethod
    def from_doc(cls, doc: Any) -> "WindowsConfig":
        """Read the loaded YAML; raises `WindowsConfigError` with the problems of `config_problems`."""
        found = config_problems(doc)
        if found:
            raise WindowsConfigError(found)
        od = doc["operating_day"]
        wins = {name: _quiet(name, spec) for name, spec in doc["windows"].items()}
        maint = tuple(MaintenanceWindow(m["weekday"], _hhmm(m["start"]), _hhmm(m["end"]), m["source"])
                      for m in doc.get("maintenance_windows") or ())
        return cls(_hhmm(od["start"]), _hhmm(od["end"]), wins, maint)


def _norm_rule(rule: str) -> str:
    return " ".join(rule.lower().split())


def _quiet(name: str, spec: Mapping[str, Any]) -> QuietWindow:
    def anchor(a: Mapping[str, Any] | None) -> Anchor | None:
        return None if a is None else Anchor(a["weekday"], _hhmm(a["time"]), a.get("calendar"))
    return QuietWindow(name, anchor(spec.get("start")), anchor(spec.get("end")), spec.get("rule"))


def _time_problem(path: str, value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, str) or not _HHMM.match(value):
        return [(path, f"{value!r} is not a time HH:MM (00:00 to 23:59, America/New_York)")]
    return []


def config_problems(doc: Any) -> list[tuple[str, str]]:
    """What makes `schedule/windows.yaml` unusable beyond its schema (L-XREF-24, 7.10): the four named windows,
    `HH:MM` times, an operating day that starts before it ends, every quiet window either weekly edges (both) or a
    rule the scheduler resolves (`RULE_EQUITIES_QUIET`, `RULE_FUTURES_BREAK`), `none` empty, and maintenance windows
    that do not start and end at the same minute."""
    if not isinstance(doc, Mapping):
        return [("", "schedule/windows.yaml is not a mapping")]
    out: list[tuple[str, str]] = []
    od = doc.get("operating_day")
    if not isinstance(od, Mapping):
        out.append(("operating_day", "operating_day {start, end} is required (11.1 PA-12)"))
    else:
        found = _time_problem("operating_day.start", od.get("start")) + _time_problem("operating_day.end", od.get("end"))
        out += found
        if not found and _hhmm(od["start"]) >= _hhmm(od["end"]):
            out.append(("operating_day", f"the operating day starts at {od['start']}, not before it ends at {od['end']}"))
    wins = doc.get("windows")
    wins = wins if isinstance(wins, Mapping) else {}
    for name in WINDOW_NAMES:
        if not isinstance(wins.get(name), Mapping):
            out.append((f"windows.{name}", f"the named window {name} is not defined (7.10)"))
    known = {_norm_rule(RULE_EQUITIES_QUIET), _norm_rule(RULE_FUTURES_BREAK)}
    for name, spec in wins.items():
        if not isinstance(spec, Mapping):
            continue
        at = f"windows.{name}"
        start, end, rule = spec.get("start"), spec.get("end"), spec.get("rule")
        if name == NO_WINDOW:
            if start is not None or end is not None or rule is not None:
                out.append((at, "none is the absence of a window: it takes no start, end or rule"))
            continue
        if (start is None) != (end is None):
            out.append((at, "a weekly window gives both start and end"))
        elif start is None and rule is None:
            out.append((at, "the window gives start and end, or a rule"))
        elif start is not None and rule is not None:
            out.append((at, "the window gives start and end or a rule, not both"))
        if rule is not None and (not isinstance(rule, str) or _norm_rule(rule) not in known):
            out.append((f"{at}.rule", f"rule {rule!r} is not one the scheduler resolves: {RULE_EQUITIES_QUIET!r} or "
                                      f"{RULE_FUTURES_BREAK!r}"))
        edges = []
        for edge, a in (("start", start), ("end", end)):
            if a is None:
                continue
            if not isinstance(a, Mapping):
                out.append((f"{at}.{edge}", "an edge is {weekday, time, calendar?}"))
                continue
            found = _time_problem(f"{at}.{edge}.time", a.get("time"))
            if a.get("weekday") not in WEEKDAYS:
                found.append((f"{at}.{edge}.weekday", f"{a.get('weekday')!r} is not one of {', '.join(WEEKDAYS)}"))
            if a.get("calendar") is not None and a["calendar"] not in CONFIG_CALENDARS:
                found.append((f"{at}.{edge}.calendar", f"{a['calendar']!r} is not one of {', '.join(CONFIG_CALENDARS)}"))
            out += found
            if not found:
                edges.append((a["weekday"], a["time"]))
        if len(edges) == 2 and edges[0] == edges[1]:
            out.append((at, "the window starts and ends at the same weekday and time"))
    for i, m in enumerate(doc.get("maintenance_windows") or ()):
        if not isinstance(m, Mapping):
            continue
        at = f"maintenance_windows[{i}]"
        found = _time_problem(f"{at}.start", m.get("start")) + _time_problem(f"{at}.end", m.get("end"))
        out += found
        if not found and m["start"] == m["end"]:
            out.append((at, "a maintenance window that starts and ends at the same minute has no length"))
        if m.get("weekday") not in WEEKDAYS:
            out.append((f"{at}.weekday", f"{m.get('weekday')!r} is not one of {', '.join(WEEKDAYS)}"))
        if not isinstance(m.get("source"), str) or not m["source"].strip():
            out.append((f"{at}.source", "a maintenance window names its source (7.10)"))
    return out


# -- window resolution --------------------------------------------------------------------------------------------

def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _live_between(a: datetime, b: datetime) -> tuple[Span, ...]:
    """The sessions of `QUIET_CALENDARS` that overlap [a, b) (choice 3)."""
    d0, d1 = ny(a).date() - timedelta(days=1), ny(b).date() + timedelta(days=1)
    return tuple(s for c in QUIET_CALENDARS for s in sessions(c, d0, d1) if s.start < b and s.end > a)


def _weekly_span(qw: QuietWindow, monday: date) -> Span:
    """The occurrence of a weekly window anchored in the week starting `monday` (choice 3)."""
    assert qw.start is not None and qw.end is not None
    configured = monday + timedelta(days=WEEKDAYS.index(qw.start.weekday))
    sd = configured
    if qw.start.calendar:
        while sd > monday and sd.weekday() < 5 and not is_session_day(qw.start.calendar, sd):
            sd -= timedelta(days=1)
    start = wall_time(sd, qw.start.time)
    if sd != configured:
        anchor = wall_time(configured, qw.start.time)
        start = max([start] + [min(s.end, anchor) for s in _live_between(start, anchor)])
    ed = sd + timedelta(days=(WEEKDAYS.index(qw.end.weekday) - sd.weekday()) % 7)
    if wall_time(ed, qw.end.time) <= start:
        ed += timedelta(days=7)
    anchor = wall_time(ed, qw.end.time)
    if qw.end.calendar:
        while (ed + timedelta(days=1)).weekday() < 5 and not is_session_day(qw.end.calendar, ed + timedelta(days=1)):
            ed += timedelta(days=1)
    end = wall_time(ed, qw.end.time)
    if end != anchor:
        end = min([end] + [max(s.start, anchor) for s in _live_between(anchor, end)])
    return Span(start, end)


def _gap_at(calendar: str, t: datetime) -> Span | None:
    """The time between two sessions of `calendar` that contains `t` or comes next (choice 4)."""
    d = t.astimezone(_zone()).date()
    merged: list[Span] = []
    for s in sessions(calendar, d - _LOOKBACK, d + _LOOKAHEAD):
        if merged and s.start <= merged[-1].end:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, s.end))
        else:
            merged.append(s)
    for a, b in zip(merged, merged[1:]):
        if b.start > t:
            return Span(a.end, b.start)
    return None


def window(name: str, at: datetime, config: WindowsConfig, *, symbol: str | None = None,
           overnight: frozenset[str] = frozenset()) -> Span | None:
    """The occurrence of named window `name` that contains `at`, or else the next one (7.10); None when the window
    never occurs for `symbol` (a crypto future; a symbol on the 24-hour list `overnight` for equities_quiet)."""
    t = to_utc(at)
    if name not in config.windows:
        raise ValueError(f"window {name!r} is not defined in schedule/windows.yaml")
    qw = config.windows[name]
    if name == NO_WINDOW:
        raise ValueError("none is the absence of a window; nothing to resolve")
    if symbol is not None and is_crypto_future(symbol):
        return None
    if qw.rule is not None:
        if _norm_rule(qw.rule) == _norm_rule(RULE_EQUITIES_QUIET):
            return None if symbol is not None and symbol in overnight else _gap_at("XNYS", t)
        return _gap_at("CMES", t)
    monday = _monday(t.astimezone(_zone()).date())
    for k in range(-1, 3):
        span = _weekly_span(qw, monday + timedelta(weeks=k))
        if span.end > t:
            return span
    return None


def next_all_quiet(at: datetime, config: WindowsConfig) -> Span:
    """The `all_quiet` window the Calibration run uses (11.2, 7.13): the one in progress at `at`, else the next."""
    span = window("all_quiet", at, config)
    if span is None:
        raise ValueError("all_quiet does not resolve")
    return span


# -- the operating day --------------------------------------------------------------------------------------------

def _check_run_type(run_type: str) -> None:
    if run_type not in RUN_TYPES:
        raise ValueError(f"unknown run type {run_type!r}; one of {', '.join(RUN_TYPES)}")


def is_operating_time(at: datetime, config: WindowsConfig, run_type: str) -> bool:
    """True when a run of `run_type` may start at `at`: inside the operating day (06:00 to 22:00 America/New_York,
    11.1 PA-12) for PR, merge and RC runs; always for Nightly and Calibration runs (SD-76)."""
    _check_run_type(run_type)
    if run_type in EXEMPT_RUN_TYPES:
        return True
    local = ny(at).time()
    return config.operating_start <= local < config.operating_end


def operating_start(at: datetime, config: WindowsConfig, run_type: str) -> datetime:
    """When a run of `run_type` triggered at `at` may start: `at` itself inside the operating day or for an exempt
    run, else the next 06:00 (7.10 "one that would start outside it waits until 06:00")."""
    if is_operating_time(at, config, run_type):
        return to_utc(at)
    local = ny(at)
    day = local.date() if local.time() < config.operating_start else local.date() + timedelta(days=1)
    return wall_time(day, config.operating_start)


def operating_day_end(at: datetime, config: WindowsConfig) -> datetime:
    """22:00 America/New_York on the New York date of `at`."""
    return wall_time(ny(at).date(), config.operating_end)


def maintenance_hit(at: datetime, config: WindowsConfig) -> tuple[MaintenanceWindow, Span] | None:
    """The `maintenance_windows` entry and occurrence that contain `at`, or None (7.10, 7.11; choice 6)."""
    t = to_utc(at)
    d = ny(t).date()
    for entry in config.maintenance:
        for day in (d - timedelta(days=1), d):
            if day.weekday() == WEEKDAYS.index(entry.weekday) and t in entry.span_on(day):
                return entry, entry.span_on(day)
    return None


def in_maintenance_window(at: datetime, config: WindowsConfig) -> bool:
    """True when `at` falls inside a listed maintenance window (7.10)."""
    return maintenance_hit(at, config) is not None


# -- the windowed-flow scheduler (7.10) ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowedItem:
    """A schedulable item that may declare a window: its id (the flow id p95 is keyed by), `requires_window` (4.7,
    the checkpoint override applied by the caller), the fixture symbol it shows, its checkpoint ids and its declared
    `budget.max_duration_s`."""

    item_id: str
    requires_window: str = NO_WINDOW
    symbol: str | None = None
    checkpoints: tuple[str, ...] = ()
    max_duration_s: float | None = None


@dataclass(frozen=True)
class Admission:
    """The scheduler's decision on one windowed item: a start time inside its window, or a refusal."""

    item_id: str
    window: str
    start_at: datetime | None
    ends_by: datetime | None
    span: Span | None
    detail: str
    checkpoints: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.start_at is not None

    @property
    def outcome(self) -> Outcome | None:
        """`not_run (reason: budget)` for a refused item (7.10: it shows in the coverage matrix)."""
        return None if self.admitted else Outcome("not_run", "budget", detail=self.detail)


@dataclass(frozen=True)
class WindowPlan:
    """The windowed items of one queue in order (choice 5); items without a window are listed in `unwindowed`."""

    admissions: tuple[Admission, ...]
    unwindowed: tuple[str, ...] = ()

    @property
    def admitted(self) -> tuple[Admission, ...]:
        return tuple(a for a in self.admissions if a.admitted)

    @property
    def refused(self) -> tuple[Admission, ...]:
        return tuple(a for a in self.admissions if not a.admitted)

    def outcomes(self) -> dict[str, Outcome]:
        """Checkpoint id -> `not_run (reason: budget)` for every checkpoint of a refused item."""
        return {cp: a.outcome for a in self.refused for cp in a.checkpoints if a.outcome is not None}


def _refuse(item: WindowedItem, span: Span | None, detail: str) -> Admission:
    return Admission(item.item_id, item.requires_window, None, None, span, detail, item.checkpoints)


def plan_windowed(items: Iterable[WindowedItem], now: datetime, p95: Mapping[str, float], config: WindowsConfig, *,
                  run_type: str, overnight: frozenset[str] = frozenset(),
                  deadline: datetime | None = None) -> WindowPlan:
    """Start times for the windowed items of one queue, or `not_run (reason: budget)` for each that cannot finish
    by its p95 before its window ends or, in a PR, merge or RC run, before the operating day ends (7.10; choice 5).
    `p95` holds the measured p95 flow durations in seconds from the run store; `deadline` is the run's capture
    deadline."""
    _check_run_type(run_type)
    cursor = to_utc(now)
    end_by = to_utc(deadline) if deadline is not None else None
    out: list[Admission] = []
    unwindowed: list[str] = []
    for item in items:
        name = item.requires_window
        if name == NO_WINDOW:
            unwindowed.append(item.item_id)
            continue
        span = window(name, cursor, config, symbol=item.symbol, overnight=overnight)
        if span is None:
            why = "crypto futures are always live" if item.symbol and is_crypto_future(item.symbol) else \
                "the symbol is on the 24-hour list read this run"
            out.append(_refuse(item, None, f"no {name} window exists for {item.symbol} ({why}, 7.10)"))
            continue
        start = max(cursor, span.start)
        if end_by is not None and start >= end_by:
            out.append(_refuse(item, span, f"the next {name} window opens {fmt_ny(span.start)}, after the run "
                                           f"deadline {fmt_ny(end_by)}"))
            continue
        dur = p95.get(item.item_id)
        source = "p95"
        if dur is None:
            dur, source = (item.max_duration_s, "declared max_duration_s") if item.max_duration_s else \
                (DEFAULT_FLOW_S, "unmeasured default")
        ends = start + timedelta(seconds=float(dur))
        cost = f"{source} {float(dur):.0f} s from {fmt_ny(start)} ends {fmt_ny(ends)}"
        if ends > span.end:
            out.append(_refuse(item, span, f"{cost}, after {name} closes {fmt_ny(span.end)}"))
            continue
        if run_type not in EXEMPT_RUN_TYPES:
            if not is_operating_time(start, config, run_type):
                out.append(_refuse(item, span, f"{name} opens {fmt_ny(start)}, outside the operating day "
                                               f"{config.operating_start:%H:%M} to {config.operating_end:%H:%M}"))
                continue
            day_end = operating_day_end(start, config)
            if ends > day_end:
                out.append(_refuse(item, span, f"{cost}, after the operating day ends {fmt_ny(day_end)}"))
                continue
        if end_by is not None and ends > end_by:
            out.append(_refuse(item, span, f"{cost}, after the run deadline {fmt_ny(end_by)}"))
            continue
        out.append(Admission(item.item_id, name, start, ends, span, cost, item.checkpoints))
        cursor = ends
    return WindowPlan(tuple(out), tuple(unwindowed))


# -- preflight: G4 and the maintenance reschedule -----------------------------------------------------------------

@dataclass(frozen=True)
class G4Check:
    """Preflight G4 (12.7): the exchange calendar resolved for every fixture symbol over the run date plus 90 days.
    Feed it to the gate as `PreflightReport.items["G4"] = ok` and `detail["G4"] = detail`."""

    ok: bool
    detail: str | None = None
    sessions: Mapping[str, int] | None = None

    def outcome(self) -> Outcome | None:
        """`env_error (gate: preflight.G4)` for every scheduled checkpoint when it failed (7.10, 12.7)."""
        return None if self.ok else Outcome("env_error", "gate", "preflight.G4", self.detail)


def g4_horizon_check(run_date: date, symbols: Iterable[Mapping[str, Any]], *, overnight: frozenset[str] | None,
                     horizon_days: int = G4_HORIZON_DAYS, data_horizon: date = CALENDAR_HORIZON) -> G4Check:
    """Resolve the sessions of every fixture symbol (4.6 `symbols` entries: `symbol`, `asset`) from `run_date` to
    `run_date + horizon_days`; fails on missing zone data, a calendar horizon shorter than that, a symbol with no
    calendar or no session, or a 24-hour list not read this run (`overnight` None) (choice 9)."""
    try:
        _zone()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return G4Check(False, f"time zone data for {NEW_YORK} unavailable: {exc}")
    end = run_date + timedelta(days=horizon_days)
    if end > data_horizon:
        return G4Check(False, f"exchange calendar data ends {data_horizon}, before the run date plus {horizon_days} "
                              f"days ({end})")
    if overnight is None:
        return G4Check(False, "the 24-hour trading list was not read this run (7.4, 7.10)")
    counts: dict[str, int] = {}
    problems: list[str] = []
    for s in symbols:
        sym, asset = str(s.get("symbol", "")), str(s.get("asset", ""))
        try:
            cal = symbol_calendar(sym, asset)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        counts[sym] = len(sessions(cal, run_date, end))
        if not counts[sym]:
            problems.append(f"symbol {sym!r}: no {cal} session from {run_date} to {end}")
    if problems:
        return G4Check(False, "; ".join(problems), counts)
    return G4Check(True, None, counts)


class _Checklist(Protocol):
    """The 12.7 checklist after the one retry per item (`orchestrator.rig.PreflightReport`)."""

    items: Mapping[str, bool]


@dataclass(frozen=True)
class Reschedule:
    """A run moved once to the window end plus 15 minutes after an N1, N3 or N4 failure inside a maintenance
    window (7.11), with its run-health notice; the Reporter's maintenance calendar takes it with POP-09 hits."""

    run_id: str
    items: tuple[str, ...]
    failed_at: datetime
    window: MaintenanceWindow
    span: Span
    start_at: datetime

    def health_line(self) -> str:
        return (f"preflight {', '.join(self.items)} failed at {fmt_ny(self.failed_at)} inside maintenance window "
                f"{self.window}; run rescheduled once to {fmt_ny(self.start_at)} instead of env_error (7.11)")


def maintenance_reschedule(report: _Checklist, now: datetime, config: WindowsConfig, *, run_id: str,
                           previous_reschedules: int = 0) -> Reschedule | None:
    """The reschedule of 7.11 and 12.7, or None when the usual preflight outcome applies (`gates.check_provisioned`):
    no N1, N3 or N4 failure, a non-network item failed too, the run was already rescheduled once, or `now` (the
    second attempt's time) is outside every maintenance window (choice 6)."""
    failed = [i for i, ok in report.items.items() if not ok]
    trigger = [i for i in RESCHEDULE_ITEMS if i in failed]
    if not trigger or any(not i.startswith("N") for i in failed) or previous_reschedules >= 1:
        return None
    hit = maintenance_hit(now, config)
    if hit is None:
        return None
    entry, span = hit
    return Reschedule(run_id, tuple(trigger), to_utc(now), entry, span, span.end + RESCHEDULE_DELAY)


# -- the lockstep pause (10.7, 11.3) ------------------------------------------------------------------------------

@dataclass(frozen=True)
class PauseDecision:
    """What a network pause of one checkpoint means for its lockstep partner (10.7; choice 7). `pause_s` counts
    against the wall budget; `outcome` is `not_run (reason: budget)` when the partner missed the minute."""

    checkpoint: str
    partner_paused: bool
    pause_s: float
    lag_s: float | None
    outcome: Outcome | None


def lockstep_pause(checkpoint: str, data_mode: str, paused_at: datetime | float, resumed_at: datetime | float,
                   partner_resumed_at: datetime | float | None) -> PauseDecision:
    """A paused masked_live checkpoint pauses its lockstep partner; a partner that cannot resume within 60 s of the
    paused rig makes the checkpoint `not_run (reason: budget)` (10.7, 11.3). A frozen checkpoint has no partner.
    Times are aware datetimes or seconds on one clock (`orchestrator.lockstep.slip_seconds`)."""
    if data_mode not in DATA_MODES:
        raise ValueError(f"unknown data mode {data_mode!r}")
    own = slip_seconds(paused_at, resumed_at)
    if data_mode != "masked_live":
        return PauseDecision(checkpoint, False, own, None, None)
    if partner_resumed_at is None:
        return PauseDecision(checkpoint, True, own, None,
                             Outcome("not_run", "budget", detail="lockstep partner did not resume after the pause"))
    lag = slip_seconds(resumed_at, partner_resumed_at)
    pause = max(own, slip_seconds(paused_at, partner_resumed_at))
    if lag <= LOCKSTEP_WINDOW_S:
        return PauseDecision(checkpoint, True, pause, lag, None)
    return PauseDecision(checkpoint, True, pause, lag,
                         Outcome("not_run", "budget", detail=f"lockstep partner resumed {lag:.0f} s after the paused "
                                                             "rig, not in the same minute (10.7, 11.3)"))


# -- DST and the time-axis label check (7.10, 7.1) ----------------------------------------------------------------

def dst_transitions(year: int) -> tuple[date, date]:
    """The two dates America/New_York changes offset in `year`: (start of daylight time, end of daylight time)."""
    zone = _zone()
    found: list[date] = []
    prev = datetime(year, 1, 1, 12, tzinfo=zone).utcoffset()
    for d in _days(date(year, 1, 2), date(year, 12, 31)):
        off = datetime.combine(d, time(12), tzinfo=zone).utcoffset()
        if off != prev:
            found.append(d)
            prev = off
    if len(found) != 2:
        raise ValueError(f"{NEW_YORK} has {len(found)} offset changes in {year}, not 2")
    return found[0], found[1]


def time_axis_check_due(nightly_start: datetime, previous_nightly: datetime | None = None) -> bool:
    """True for the Nightly run that follows a DST transition: it re-runs the time-axis label check (7.10; choice
    8). `previous_nightly` defaults to 24 hours before `nightly_start`."""
    t = to_utc(nightly_start)
    prev = to_utc(previous_nightly) if previous_nightly is not None else t - timedelta(days=1)
    return ny(prev).utcoffset() != ny(t).utcoffset()


@dataclass(frozen=True)
class TimeAxisCheck:
    """The chart time-axis label check (7.1 Time zone row): each OCR label against the New York time of its bar."""

    mismatches: tuple[tuple[int, str, str], ...] = ()       # (label index, read, expected)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def outcome(self) -> Outcome | None:
        """`env_error (gate: settings_attested, reason: fixture_drift)`: the app chart time zone is not New York."""
        if self.ok:
            return None
        detail = "chart time-axis labels off New York time: " + ", ".join(
            f"#{i} read {read!r} expected {want}" for i, read, want in self.mismatches)
        return Outcome("env_error", "fixture_drift", "settings_attested", detail)


def time_axis_check(labels: Sequence[str], instants: Sequence[datetime]) -> TimeAxisCheck:
    """Compare the OCR'd time-axis labels with the New York wall time (H:MM) of the bars they mark (choice 8)."""
    if len(labels) != len(instants):
        raise ValueError(f"{len(labels)} labels for {len(instants)} bar instants")
    bad = []
    for i, (label, at) in enumerate(zip(labels, instants)):
        local = ny(at)
        m = _LABEL.match(label)
        if m is None or (int(m.group(1)), int(m.group(2))) != (local.hour, local.minute):
            bad.append((i, label, f"{local:%H:%M}"))
    return TimeAxisCheck(tuple(bad))
