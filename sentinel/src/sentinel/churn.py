"""The fixture's churn calendar at run time and its generator (4.9 `churn_calendar`, 7.6 stage 2 step 4, 7.9).

At run time (`events_for`, `reseed_due_passed`, `skip_outcomes`, `reapprove`, `reseed_tasks`):

- `skip_checkpoints` makes the affected checkpoints `not_run (reason: scope)` with the event in `detail`
  (`<event> <date>[ <symbol id>]`);
- `reapprove` marks the day's frozen baselines for re-approval instead of scoring (9.8): `reapprove` returns the
  checkpoints and the event, which the baseline lifecycle turns into a re-approval instead of a scored diff;
- `reseed` is a calendar task for the login owner (the fixture's `owner`, who seeds and re-seeds, 4.9);
- once the date of the `reseed_due` event has passed, the golden `account_state` checkpoints are `env_error (gate:
  fixture_verified, reason: fixture_maintenance_needed)` (7.6 step 4, SD-40): `reseed_due_passed` feeds
  `gates.check_fixture(reseed_due_passed=...)`.

`generate` builds the calendar from the exchange calendar and the fixture (7.9): monthly option expirations (the
third Friday, the business day before when that Friday is a holiday), each option symbol's expirations from the epoch,
futures rolls from each symbol's `roll_date`, exchange holidays, earnings and ex-dividend dates, and the next
`reseed_due`. Until the vendored exchange calendar exists (7.10, wave 4), holidays, earnings and ex-dividend dates are
explicit inputs; no network call is made.

Choices (reported):

1. An event with a `symbol_ref` affects the checkpoints using that symbol (`fixture_checks.select_subset`, symbol
   selector). One without (a holiday) affects, for `skip_checkpoints`, the masked_live checkpoints (live data on a
   day the market is shut), and for `reapprove` every frozen checkpoint.
2. A `reseed_due` date has passed when the run's New York date is after it; on the date itself (an expiration day is
   a checkpoint day, 7.6) the fixture still holds.
3. Generated actions: expiration, roll and ex-dividend `reapprove` (each changes a frozen value: the chain's
   expiration list, the continuous contract, an adjusted price); holiday `skip_checkpoints`; earnings `none`
   (nothing frozen changes on the date by itself); reseed_due `reseed`. The monthly expiration names each chain
   underlying (equity or index with purpose `chain`).
4. `reseed_due` is the earliest expiration among the golden account's positions on or after the start date (L-FIX-07):
   an option's from `epoch.option_expirations`, a future's the third Friday of its contract month (the equity index
   convention), read from the month code and year that end its symbol.
5. The horizon is 90 days from the start date (7.10, preflight G4).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .orchestrator.fixture_checks import ConfigView, ScopedCheckpoint, select_subset
from .orchestrator.results import Outcome
from .records import MaintenanceItem

HORIZON_DAYS = 90
ACTIONS = {"expiration": "reapprove", "roll": "reapprove", "ex_dividend": "reapprove", "holiday": "skip_checkpoints",
           "earnings": "none", "reseed_due": "reseed"}
MONTH_CODES = "FGHJKMNQUVXZ"
_FUTURE_MONTH = re.compile(r"([FGHJKMNQUVXZ])(\d{2})$")


@dataclass(frozen=True)
class ChurnEvent:
    """One `churn_calendar` entry (4.9)."""

    date: date
    event: str
    action: str
    symbol_ref: str | None = None

    @property
    def detail(self) -> str:
        """The event as an outcome's `detail` (4.9): `<event> <date>[ <symbol id>]`."""
        return f"{self.event} {self.date.isoformat()}" + (f" {self.symbol_ref}" if self.symbol_ref else "")

    def to_dict(self) -> dict[str, Any]:
        """The entry as the fixture file writes it."""
        d: dict[str, Any] = {"date": self.date, "event": self.event}
        if self.symbol_ref:
            d["symbol_ref"] = self.symbol_ref
        d["action"] = self.action
        return d


def _day(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def calendar(fixture: Mapping[str, Any]) -> tuple[ChurnEvent, ...]:
    """The fixture's `churn_calendar` as events, in file order."""
    return tuple(ChurnEvent(_day(e["date"]), str(e["event"]), str(e.get("action", "none")), e.get("symbol_ref"))
                 for e in fixture.get("churn_calendar") or ())


def events_for(day: date, fixture: Mapping[str, Any]) -> tuple[ChurnEvent, ...]:
    """The events dated `day` (the run's New York date)."""
    return tuple(e for e in calendar(fixture) if e.date == day)


def reseed_due_passed(day: date, fixture: Mapping[str, Any]) -> bool:
    """True when a `reseed_due` event's date is before `day` (choice 2, 7.6 step 4)."""
    return any(e.event == "reseed_due" and e.date < day for e in calendar(fixture))


def _affected(e: ChurnEvent, checkpoints: Sequence[ScopedCheckpoint], cfg: ConfigView,
              no_symbol_mode: str) -> list[ScopedCheckpoint]:
    if e.symbol_ref:
        return select_subset(f"symbol:{e.symbol_ref}", checkpoints, cfg)
    return [cp for cp in checkpoints if cp.data_mode == no_symbol_mode]


def skip_outcomes(day: date, fixture: Mapping[str, Any], checkpoints: Sequence[ScopedCheckpoint],
                  cfg: ConfigView) -> dict[str, Outcome]:
    """checkpoint id -> `not_run (reason: scope)` with the event in `detail`, for the day's `skip_checkpoints` events
    (4.9, choice 1); the first event wins a checkpoint two events name."""
    out: dict[str, Outcome] = {}
    for e in events_for(day, fixture):
        if e.action == "skip_checkpoints":
            for cp in _affected(e, checkpoints, cfg, "masked_live"):
                out.setdefault(cp.checkpoint_id, Outcome("not_run", "scope", detail=e.detail))
    return out


def reapprove(day: date, fixture: Mapping[str, Any], checkpoints: Sequence[ScopedCheckpoint],
              cfg: ConfigView) -> dict[str, str]:
    """checkpoint id -> event detail for the frozen checkpoints whose baselines the day's `reapprove` events mark for
    re-approval instead of scoring (4.9, 7.9, choice 1)."""
    out: dict[str, str] = {}
    for e in events_for(day, fixture):
        if e.action == "reapprove":
            for cp in _affected(e, checkpoints, cfg, "frozen"):
                if cp.data_mode == "frozen":
                    out.setdefault(cp.checkpoint_id, e.detail)
    return out


def reseed_tasks(fixture: Mapping[str, Any], *, opened_at: str) -> tuple[MaintenanceItem, ...]:
    """The calendar tasks of the `reseed` events, for the fixture's owner, who re-seeds the golden account during
    market hours (4.9, 7.6, 11.5)."""
    fid = str(fixture.get("fixture_id", ""))
    return tuple(MaintenanceItem(kind="reseed", ref=f"{fid}/{e.date.isoformat()}", owner=str(fixture["owner"]),
                                 opened_at=opened_at, detail=f"re-seed the golden account by {e.date.isoformat()} "
                                                             f"({e.event})")
                 for e in calendar(fixture) if e.action == "reseed")


# -- the generator (7.9) ------------------------------------------------------------------------------------
def third_friday(year: int, month: int) -> date:
    """The third Friday of a month: the monthly option expiration and the equity index futures expiration."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _business_before(d: date, holidays: frozenset[date]) -> date:
    while d in holidays or d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def monthly_expirations(start: date, end: date, holidays: Iterable[date] = ()) -> list[date]:
    """Monthly option expirations from `start` to `end` inclusive: third Fridays, the business day before when the
    Friday is a holiday."""
    hol = frozenset(holidays)
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        d = _business_before(third_friday(y, m), hol)
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def future_expiration(symbol: str) -> date | None:
    """A futures contract's expiration from the month code and year ending its symbol (/MESZ26: 2026-12-18; choice 4);
    None for a continuous symbol."""
    m = _FUTURE_MONTH.search(symbol)
    if m is None:
        return None
    return third_friday(2000 + int(m[2]), MONTH_CODES.index(m[1]) + 1)


def _next_reseed(fixture: Mapping[str, Any], start: date) -> date | None:
    golden = {a["account_id"] for a in fixture.get("accounts") or () if a.get("role") == "golden"}
    symbols = {s["id"]: s for s in fixture.get("symbols") or ()}
    option_exp = ((fixture.get("epoch") or {}).get("option_expirations") or {})
    dates = []
    for p in fixture.get("positions") or ():
        if p.get("account_ref") not in golden:
            continue
        s = symbols.get(p["symbol_ref"], {})
        if s.get("asset") in ("option", "future_option"):
            dates += [_day(d) for d in option_exp.get(p["symbol_ref"]) or ()]
        elif s.get("asset") == "future":
            d = future_expiration(str(s.get("symbol", "")))
            dates += [d] if d else []
    upcoming = sorted(d for d in dates if d >= start)
    return upcoming[0] if upcoming else None


def generate(fixture: Mapping[str, Any], *, start: date, holidays: Iterable[date] = (),
             earnings: Mapping[str, Iterable[date]] | None = None,
             ex_dividend: Mapping[str, Iterable[date]] | None = None,
             horizon_days: int = HORIZON_DAYS) -> list[ChurnEvent]:
    """The churn calendar from `start` over the horizon (7.9, choices 3 to 5), sorted by date, event and symbol.
    `earnings` and `ex_dividend` map a fixture symbol id to its dates (from the market-data API)."""
    end = start + timedelta(days=horizon_days)
    hol = sorted({_day(h) for h in holidays})
    symbols = fixture.get("symbols") or ()
    found: set[ChurnEvent] = set()

    def add(d: date, event: str, symbol_ref: str | None = None) -> None:
        if start <= d <= end:
            found.add(ChurnEvent(d, event, ACTIONS[event], symbol_ref))

    underlyings = [s["id"] for s in symbols if "chain" in (s.get("purpose") or ()) and s.get("asset") in
                   ("equity", "index")]
    for d in monthly_expirations(start, end, hol):
        for sid in underlyings:
            add(d, "expiration", sid)
    for sid, dates in (((fixture.get("epoch") or {}).get("option_expirations")) or {}).items():
        for d in dates or ():
            add(_day(d), "expiration", sid)
    for s in symbols:
        if s.get("roll_date"):
            add(_day(s["roll_date"]), "roll", s["id"])
    for h in hol:
        add(h, "holiday")
    for event, by_symbol in (("earnings", earnings), ("ex_dividend", ex_dividend)):
        for sid, dates in (by_symbol or {}).items():
            for d in dates:
                add(_day(d), event, sid)
    reseed = _next_reseed(fixture, start)
    if reseed is not None:
        add(reseed, "reseed_due")
    return sorted(found, key=lambda e: (e.date, e.event, e.symbol_ref or ""))
