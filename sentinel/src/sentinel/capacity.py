"""Capacity: the 11.2 rig-minute tables recomputed, measured rig-minutes per run type, pool utilization over the
operating day with the R15 growth trigger, and the R22 masked_live starvation check (11.1 PA-09, PA-10, PA-12; 11.2
"Rig-minutes per pair", "Daily capacity"; 11.6 "P2 when over 5 percent of a day's masked_live checkpoints starve";
11.10 R15, R22; 9.10 "Coverage loss" and "Pool saturated").

The system owner recomputes the 11.2 tables at every phase exit from measured numbers (11 preamble); this module is
that arithmetic, fed by the planning assumptions or by the store:

- `pair_rig_minutes(S)` is the scheduling rule of 11.1 for per-side time S: without reuse a pair costs 2 S (frozen
  twice on one rig, masked_live once on each rig of a pair); with base reuse at PA-09 it costs 1.3 S (frozen once,
  masked_live twice). `wall_minutes` is "wall on n rigs = rig-minutes / n + startup + 10 min report", the startup
  8 min without reuse (a frozen shard opens two sessions) and 4 with it. `plan_table()` gives the 11.2 table rows
  (PR 80 and 52 rig-min, 38 and 27 min wall; merge 550 and 358, 156 and 104; RC 1,236 and 803, 327 and 215; see
  choice 6 for the RC worst case).
- `daily_need()` is "Daily capacity (PA-10)": the PR and merge runs of a day, startups counted, in rig-hours and as
  a share of the 4-rig pool's 64 rig-hours (about 37 rig-hours, 57 percent, with reuse; about 56, 87 percent,
  without).
- `measured(store, since, until)` reads the runs of a window: per run type the runs, the mean rig-minutes (shard
  sessions), the mean wall minutes and the base-reuse share of frozen checkpoints.
- `utilization(store, day)` is one operating day's pool utilization; `growth_trigger(store, day)` is R15: the pool
  grows by one rig pair when two weeks of measured utilization exceed 70 percent.
- `r22_check(store, day)` counts a day's masked_live checkpoints that starved (`not_run (reason: budget)`) and
  returns the P2 `coverage_loss` alert of 9.10 above 5 percent (11.6, R22).
- `report(store, day)` puts them together as `CapacityReport` with the lines the Reporter prints.

Choices where the spec is ambiguous (each is reported):

1. Session startups in rig-minutes (daily capacity): a run on the pool opens one session per rig; the four pool
   rigs run one lockstep pair (masked_live, one 4-minute session per rig) and two frozen rigs, whose sessions cost
   8 minutes without reuse (base then candidate) and 4 with it. That is 16 rig-minutes per run with reuse and 24
   without, which reproduces 11.2's "about 70 rig-minutes" per PR run and "about 375" per merge run with reuse and
   its "about 56 rig-hours, 87 percent" without.
2. Utilization of a day: the busy minutes of the active pool rigs (`rigs.role = pool`) inside the operating day
   (06:00 to 22:00 America/New_York, PA-12), a rig's overlapping shard sessions counted once, over the active pool
   rigs times the operating day. A shard still running counts until `now` is not known here, so only finished
   sessions (`started_at` and `finished_at` set) count. With no pool rig registered the 11.2 fleet of 4 is assumed.
3. R15 "two weeks of measured utilization exceed 70 percent": the busy minutes over the capacity minutes of the
   business days (XNYS) in the 14 days ending `day`, above 0.70; no business day, no trigger.
4. R22: a day's masked_live checkpoints are the non-synthetic outcome rows of masked_live checkpoints in the PR,
   merge and RC runs that started on that America/New_York date (the pool's runs; Nightly and Calibration are not
   pool-scheduled, 11.2); the starved ones are `not_run (reason: budget)`, as 11.10 R22 and 9.10 define them.
   "Over 5 percent" is strictly above.
5. Measured rig-minutes of a run: the sum of its shards' session lengths (`finished_at - started_at`), one session
   per shard; the base-reuse share is the frozen checkpoints' outcome rows with `base_source_run_id` over the frozen
   checkpoints' outcome rows that captured both sides or reused a base (non-synthetic, run type's runs).
6. The wall formula takes one startup rule for every run type: 8 minutes without reuse, 4 with. 11.2's RC row writes
   "1,236 / 4 + 4 + 10 = 323 min worst" with a 4-minute startup, while its PR and merge rows use 8 for the worst
   case; by the stated rule the RC worst case is 327 minutes (reported as a spec inconsistency; both are inside the
   8-hour budget).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import calendar as cal
from .orchestrator import alerts
from .report.alerting import Alert
from .store import Store

PER_SIDE_MIN: Mapping[str, float] = {"pr": 40.0, "merge": 275.0, "rc": 618.0}   # 11.2 "Per side: PR 40, merge 275"
BUDGET_MIN: Mapping[str, float] = {"pr": 45.0, "merge": 180.0, "rc": 480.0}     # 11.2 wall budgets
FROZEN_SHARE = 0.70                       # PA-09
POOL_RIGS = 4                             # 11.2 fleet: rigs 1 to 4; RC pair work also runs on rigs 1 to 4
PAIR_RIGS = 2                             # one lockstep pair for masked_live (choice 1)
SESSION_MIN = 4.0                         # PA-04
FROZEN_NO_REUSE_SESSION_MIN = 8.0         # 11.2: a frozen shard without reuse opens two sessions on its rig
REPORT_MIN = 10.0                         # 11.2: the report margin
RUNS_PER_DAY: Mapping[str, int] = {"pr": 10, "merge": 4}   # PA-10 (the Nightly runs on rig 5, outside the pool)
OPENS, CLOSES = time(6, 0), time(22, 0)   # PA-12 operating day
GROWTH_TRIGGER = 0.70                     # 11.2, 11.10 R15
GROWTH_DAYS = 14
R22_SHARE = 0.05                          # 11.6, 11.10 R22
POOL_RUN_TYPES = ("pr", "merge", "rc")


def pair_rig_minutes(per_side_min: float, frozen_share: float = FROZEN_SHARE) -> tuple[float, float]:
    """(no reuse, reuse) rig-minutes of one pair for per-side time S (11.1 scheduling rule, 11.2): 2 S, and
    S x (frozen + 2 x masked_live) with the frozen base reused."""
    return 2 * per_side_min, per_side_min * (frozen_share + 2 * (1 - frozen_share))


def wall_minutes(rig_minutes: float, rigs: int, *, reuse: bool) -> float:
    """11.2: rig-minutes / n + startup (8 min without reuse, 4 with) + 10 min report."""
    return rig_minutes / rigs + (SESSION_MIN if reuse else FROZEN_NO_REUSE_SESSION_MIN) + REPORT_MIN


def startup_rig_minutes(rigs: int = POOL_RIGS, *, reuse: bool) -> float:
    """The session startups of one pool run in rig-minutes (choice 1)."""
    pair = min(PAIR_RIGS, rigs)
    return pair * SESSION_MIN + (rigs - pair) * (SESSION_MIN if reuse else FROZEN_NO_REUSE_SESSION_MIN)


@dataclass(frozen=True)
class PlanRow:
    """One row of the 11.2 rig-minutes table."""

    run_type: str
    per_side_min: float
    pair_no_reuse: float
    pair_reuse: float
    rigs: int
    wall_worst: float
    wall_expected: float

    @property
    def inside_budget(self) -> bool:
        """The worst-case wall fits the run type's budget (11.2)."""
        return self.wall_worst <= BUDGET_MIN[self.run_type]

    def line(self) -> str:
        return (f"{self.run_type}: per side {self.per_side_min:.0f} min; pair {self.pair_no_reuse:.0f} rig-min without "
                f"reuse, {self.pair_reuse:.0f} with; wall on {self.rigs} rigs {self.wall_worst:.0f} min worst, "
                f"{self.wall_expected:.0f} expected (budget {BUDGET_MIN[self.run_type]:.0f})")


def plan_table(per_side: Mapping[str, float] = PER_SIDE_MIN, frozen_share: float = FROZEN_SHARE,
               rigs: int = POOL_RIGS) -> tuple[PlanRow, ...]:
    """The 11.2 rig-minutes table for per-side times (the PA values, or measured ones at a phase exit)."""
    out = []
    for rt, s in per_side.items():
        worst, expected = pair_rig_minutes(s, frozen_share)
        out.append(PlanRow(rt, s, worst, expected, rigs, wall_minutes(worst, rigs, reuse=False),
                           wall_minutes(expected, rigs, reuse=True)))
    return tuple(out)


@dataclass(frozen=True)
class DailyNeed:
    """Daily capacity (11.2, PA-10): rig-hours a day needs and their share of the pool's operating day."""

    rig_hours_reuse: float
    rig_hours_no_reuse: float
    pool_rig_hours: float

    @property
    def share_reuse(self) -> float:
        return self.rig_hours_reuse / self.pool_rig_hours

    @property
    def share_no_reuse(self) -> float:
        return self.rig_hours_no_reuse / self.pool_rig_hours

    def line(self) -> str:
        return (f"daily need at PA-10: {self.rig_hours_reuse:.0f} rig-hours with reuse ({100 * self.share_reuse:.0f} "
                f"percent of {self.pool_rig_hours:.0f}), {self.rig_hours_no_reuse:.0f} without "
                f"({100 * self.share_no_reuse:.0f} percent); the growth trigger is {100 * GROWTH_TRIGGER:.0f} percent")


def daily_need(rows: Sequence[PlanRow] = (), runs_per_day: Mapping[str, int] = RUNS_PER_DAY, rigs: int = POOL_RIGS,
               opens: time = OPENS, closes: time = CLOSES) -> DailyNeed:
    """The day's PR and merge runs in rig-hours, startups counted (choice 1), against the pool's operating day."""
    by_type = {r.run_type: r for r in (rows or plan_table(rigs=rigs))}
    reuse = no_reuse = 0.0
    for rt, n in runs_per_day.items():
        row = by_type[rt]
        reuse += n * (row.pair_reuse + startup_rig_minutes(rigs, reuse=True))
        no_reuse += n * (row.pair_no_reuse + startup_rig_minutes(rigs, reuse=False))
    day_h = (datetime.combine(date.min, closes) - datetime.combine(date.min, opens)).total_seconds() / 3600
    return DailyNeed(reuse / 60, no_reuse / 60, rigs * day_h)


# -- measured -------------------------------------------------------------------------------------------------------

def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _iso(at: datetime) -> str:
    return cal.to_utc(at).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Measured:
    """One run type's measured cost over a window (choice 5)."""

    run_type: str
    runs: int
    rig_minutes: float | None             # mean per run
    wall_minutes: float | None            # mean per finished run
    reuse_share: float | None             # frozen rows with a reused base over frozen rows

    def line(self) -> str:
        def f(v: float | None, unit: str) -> str:
            return "not measured" if v is None else f"{v:.0f} {unit}"
        reuse = "not measured" if self.reuse_share is None else f"{100 * self.reuse_share:.0f} percent"
        return (f"{self.run_type}: {self.runs} runs, {f(self.rig_minutes, 'rig-min')} per run, wall "
                f"{f(self.wall_minutes, 'min')}, base reuse {reuse}")


def measured(store: Store, since: datetime, until: datetime) -> tuple[Measured, ...]:
    """Per run type, the runs that started in (`since`, `until`]: count, mean rig-minutes, mean wall, reuse share."""
    window = (_iso(since), _iso(until))
    runs = store.query("SELECT run_id, run_type, started_at, finished_at FROM runs WHERE started_at > ? AND "
                       "started_at <= ?", window)
    shards: dict[str, float] = defaultdict(float)
    for r in store.query("SELECT s.run_id, s.started_at, s.finished_at FROM shards s JOIN runs r ON r.run_id = "
                         "s.run_id WHERE r.started_at > ? AND r.started_at <= ? AND s.started_at IS NOT NULL AND "
                         "s.finished_at IS NOT NULL", window):
        shards[r["run_id"]] += (_t(r["finished_at"]) - _t(r["started_at"])).total_seconds() / 60
    reuse = {r["run_type"]: (r["reused"], r["n"]) for r in store.query(
        "SELECT r.run_type, SUM(o.base_source_run_id IS NOT NULL) AS reused, COUNT(*) AS n FROM outcomes o "
        "JOIN runs r ON r.run_id = o.run_id JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
        "WHERE r.started_at > ? AND r.started_at <= ? AND o.synthetic = 0 AND c.data_mode = 'frozen' AND "
        "(o.base_capture_sha256 IS NOT NULL OR o.base_source_run_id IS NOT NULL) GROUP BY r.run_type", window)}
    out = []
    for rt in cal.RUN_TYPES:
        mine = [r for r in runs if r["run_type"] == rt]
        if not mine:
            continue
        rig = [shards[r["run_id"]] for r in mine if r["run_id"] in shards]
        wall = [(_t(r["finished_at"]) - _t(r["started_at"])).total_seconds() / 60 for r in mine if r["finished_at"]]
        reused, n = reuse.get(rt, (0, 0))
        out.append(Measured(rt, len(mine), sum(rig) / len(rig) if rig else None,
                            sum(wall) / len(wall) if wall else None, reused / n if n else None))
    return tuple(out)


@dataclass(frozen=True)
class DayUtilization:
    """One operating day of the pool (choice 2)."""

    day: date
    busy_min: float
    capacity_min: float

    @property
    def share(self) -> float:
        return self.busy_min / self.capacity_min if self.capacity_min else 0.0


def _merged(spans: list[tuple[datetime, datetime]]) -> float:
    """Minutes covered by the union of one rig's sessions (overlaps counted once, choice 2)."""
    total, cur = 0.0, None
    for s, e in sorted(sp for sp in spans if sp[1] > sp[0]):
        if cur is not None and s <= cur[1]:
            cur = (cur[0], max(cur[1], e))
            continue
        if cur is not None:
            total += (cur[1] - cur[0]).total_seconds() / 60
        cur = (s, e)
    return total + ((cur[1] - cur[0]).total_seconds() / 60 if cur else 0.0)


def utilization(store: Store, day: date, *, opens: time = OPENS, closes: time = CLOSES) -> DayUtilization:
    """The pool's busy minutes inside `day`'s operating day over its capacity minutes (choice 2)."""
    start, end = cal.wall_time(day, opens), cal.wall_time(day, closes)
    pool = [r[0] for r in store.query("SELECT rig_id FROM rigs WHERE role = 'pool' AND active = 1")]
    spans: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for r in store.query("SELECT s.rig_id, s.started_at, s.finished_at FROM shards s JOIN rigs g ON g.rig_id = "
                         "s.rig_id WHERE g.role = 'pool' AND s.started_at IS NOT NULL AND s.finished_at IS NOT NULL "
                         "AND s.started_at < ? AND s.finished_at > ?", (_iso(end), _iso(start))):
        spans[r["rig_id"]].append((max(_t(r["started_at"]), start), min(_t(r["finished_at"]), end)))
    busy = sum(_merged(v) for v in spans.values())
    return DayUtilization(day, busy, (len(pool) or POOL_RIGS) * (end - start).total_seconds() / 60)


def growth_trigger(store: Store, day: date, *, opens: time = OPENS, closes: time = CLOSES) -> tuple[bool, float | None]:
    """R15 (choice 3): (triggered, utilization) over the business days of the 14 days ending `day`."""
    days = cal.business_days(day - timedelta(days=GROWTH_DAYS - 1), day)
    if not days:
        return False, None
    us = [utilization(store, d, opens=opens, closes=closes) for d in days]
    cap = sum(u.capacity_min for u in us)
    share = sum(u.busy_min for u in us) / cap if cap else 0.0
    return share > GROWTH_TRIGGER, share


@dataclass(frozen=True)
class R22Result:
    """A day's masked_live starvation (choice 4) and the P2 alert above 5 percent (11.6, R22)."""

    day: date
    masked_live: int
    starved: int
    alert: Alert | None = None

    @property
    def share(self) -> float:
        return self.starved / self.masked_live if self.masked_live else 0.0

    def line(self) -> str:
        return (f"masked_live starvation {self.day}: {self.starved} of {self.masked_live} checkpoints not_run "
                f"(budget), {100 * self.share:.1f} percent (R22 posts P2 above {100 * R22_SHARE:.0f})")


def r22_check(store: Store, day: date) -> R22Result:
    """R22 on `day` (choice 4): the P2 `coverage_loss` alert when over 5 percent of the day's masked_live checkpoints
    starved."""
    start, end = cal.wall_time(day, time(0, 0)), cal.wall_time(day + timedelta(days=1), time(0, 0))
    marks = ",".join("?" * len(POOL_RUN_TYPES))
    row = store.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(o.outcome = 'not_run' AND o.reason = 'budget'), 0) AS starved "
        "FROM outcomes o JOIN runs r ON r.run_id = o.run_id JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
        f"WHERE o.synthetic = 0 AND c.data_mode = 'masked_live' AND r.run_type IN ({marks}) AND r.started_at >= ? "
        "AND r.started_at < ?", (*POOL_RUN_TYPES, _iso(start), _iso(end)))[0]
    res = R22Result(day, row["n"], row["starved"])
    if res.share > R22_SHARE:
        alert = Alert.of(alerts.coverage_loss(above_r22=True), key=f"r22:{day.isoformat()}",
                         detail=f"{res.starved} of {res.masked_live} masked_live checkpoints starved on {day} "
                                f"({100 * res.share:.1f} percent, above 5; 11.10 R22, counts toward R15)")
        res = R22Result(day, res.masked_live, res.starved, alert)
    return res


@dataclass(frozen=True)
class CapacityReport:
    """The capacity panel of a day (module docstring)."""

    day: date
    plan: tuple[PlanRow, ...]
    need: DailyNeed
    measured: tuple[Measured, ...]
    utilization: DayUtilization
    growth: bool
    growth_share: float | None
    r22: R22Result
    alerts: tuple[Alert, ...] = field(default=())

    def lines(self) -> list[str]:
        out = [f"capacity {self.day}: pool utilization {100 * self.utilization.share:.0f} percent of the operating day"]
        if self.growth_share is not None:
            verdict = "above" if self.growth else "within"
            out.append(f"two-week utilization {100 * self.growth_share:.0f} percent, {verdict} the R15 trigger of "
                       f"{100 * GROWTH_TRIGGER:.0f}" + ("; grow the pool by one rig pair" if self.growth else ""))
        out += [r.line() for r in self.plan] + [self.need.line()] + [m.line() for m in self.measured]
        out.append(self.r22.line())
        return out


def report(store: Store, day: date, *, window_days: int = 30, per_side: Mapping[str, float] = PER_SIDE_MIN,
           opens: time = OPENS, closes: time = CLOSES) -> CapacityReport:
    """The capacity panel for `day`: the 11.2 tables from `per_side`, the measured runs of the `window_days` days
    ending `day`, the day's utilization, the R15 trigger and the R22 check with its alert."""
    until = cal.wall_time(day + timedelta(days=1), time(0, 0))
    plan = plan_table(per_side)
    growth, share = growth_trigger(store, day, opens=opens, closes=closes)
    r22 = r22_check(store, day)
    return CapacityReport(day, plan, daily_need(plan), measured(store, until - timedelta(days=window_days), until),
                          utilization(store, day, opens=opens, closes=closes), growth, share, r22,
                          (r22.alert,) if r22.alert else ())
