"""Measured durations from the run database (11 preamble "the Reporter prints measured durations from run one", 11.2,
2.11 `order_and_cut`, 7.10 windowed flows, 4.7 L-FLW-10, SD-03).

Every duration the planner and the lint need is measured here from `step_events`, never typed in by hand:

- `durations(store, window_days=30, now=...)` reads the window once and returns `Durations`: the per-side p50 and
  p95 of every checkpoint and every flow measured in it. `Durations.p50()` is the `durations=` argument of
  `scope.compute_scope` (checkpoint ids and flow ids to seconds, SD-03); `flow_p50()` is the `flow_p50_s=` argument
  of `lint.rules.lint_repo` (L-FLW-10: `budget.max_duration_s` within 3 x the measured p50, 5 minutes until
  measured); `flow_p95()` is the `p95` argument of `calendar.plan_windowed` (7.10: a windowed flow whose p95 cannot
  finish before its window ends is `not_run (reason: budget)`); `lines()` are the Reporter's measured-durations
  lines (11 preamble, the 9.11 "p50 and p95 step time" panel).
- `flow_duration_p50(store, flow_id)` is one flow's p50, and `flow_budget_s(store, flow_id)` the 11.2 flow budget:
  3 x the measured p50, 5 minutes (`calendar.DEFAULT_FLOW_S`) until measured.

Choices where the spec is ambiguous (each is reported):

1. An observation is one item on one side of one run (11.2 rig time is "per side"): a checkpoint's duration is the
   sum of `duration_ms` over the `step_events` rows that carry its checkpoint id on that side of that run
   (navigation, settle wait and capture, PA-03); a flow's is the sum over every row that carries its flow id on that
   side of that run (preconditions, steps and teardown, PA-06). A row without a duration counts 0, and an
   observation that sums to 0 measured nothing and is skipped.
2. Rows that are not scheduled work are left out: fire-drill rows (`drill = 1`, 6.5), Explorer rows (`mode =
   explorer`, 2.9; the Explorer has its own 90-minute budget) and the gateway hook's rows (`source = gateway_hook`:
   they echo an input event the Orchestrator already timed, and their `duration_ms` is a `late_up` delay, 5.4.5).
3. The window is the `window_days` days ending at `now`, by the row's `wall_at` (2.11: "the 30-day p50").
4. Percentiles are nearest-rank, as the calibration floors are (`diff.calibration`): the p-th percentile of n
   values is the ceil(p x n)-th smallest, so ten runs give p50 = the 5th and p95 = the 10th smallest.
5. `lines()` pools the observations of every item of one kind (census, sweep, split, flow) for its p50 and p95, and
   names the number of items measured; a kind with no observation prints nothing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..calendar import DEFAULT_FLOW_S

if TYPE_CHECKING:
    from .store import Store

WINDOW_DAYS = 30                                   # 2.11 and 11.2: the 30-day p50
FLOW_BUDGET_FACTOR = 3                             # 11.2: a flow's budget is 3 x its measured p50
KINDS = ("census", "sweep", "split", "flow")       # 4.1 checkpoint kinds, the order `lines()` prints
_LEFT_OUT = "drill = 0 AND mode <> 'explorer' AND source <> 'gateway_hook'"   # choice 2


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (choice 4): the ceil(q x n)-th smallest of `values`; `values` must not be empty."""
    if not values:
        raise ValueError("a percentile of no values")
    s = sorted(values)
    return float(s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))])


@dataclass(frozen=True)
class Stat:
    """The measured per-side duration of one item over the window: observations, p50 and p95 in seconds."""

    n: int
    p50_s: float
    p95_s: float

    @classmethod
    def of(cls, seconds: Sequence[float]) -> Stat:
        return cls(len(seconds), percentile(seconds, 0.50), percentile(seconds, 0.95))


@dataclass(frozen=True)
class Durations:
    """Per-checkpoint and per-flow measured durations over a window (module docstring)."""

    checkpoints: Mapping[str, Stat] = field(default_factory=dict)
    flows: Mapping[str, Stat] = field(default_factory=dict)
    window_days: int = WINDOW_DAYS
    samples: Mapping[str, tuple[float, ...]] = field(default_factory=dict)   # kind -> every observation (choice 5)

    def p50(self) -> dict[str, float]:
        """Checkpoint ids and flow ids to their p50 seconds: `scope.compute_scope(durations=...)` (2.11, SD-03)."""
        return {**{k: s.p50_s for k, s in self.checkpoints.items()}, **self.flow_p50()}

    def flow_p50(self) -> dict[str, float]:
        """Flow id to p50 seconds: `lint.rules.lint_repo(flow_p50_s=...)` (L-FLW-10, 11.2)."""
        return {k: s.p50_s for k, s in self.flows.items()}

    def flow_p95(self) -> dict[str, float]:
        """Flow id to p95 seconds: `calendar.plan_windowed(p95=...)` (7.10)."""
        return {k: s.p95_s for k, s in self.flows.items()}

    def flow_budget_s(self, flow_id: str) -> float:
        """11.2: 3 x the flow's measured p50, 5 minutes until measured."""
        stat = self.flows.get(flow_id)
        return FLOW_BUDGET_FACTOR * stat.p50_s if stat else DEFAULT_FLOW_S

    def lines(self) -> list[str]:
        """The Reporter's measured-durations lines, one per kind with observations (choice 5)."""
        out = []
        for kind in KINDS:
            obs = self.samples.get(kind, ())
            if not obs:
                continue
            items = len(self.flows) if kind == "flow" else sum(1 for c in self.checkpoints if c.startswith(kind + "/"))
            noun = "flows" if kind == "flow" else f"{kind} checkpoints"
            out.append(f"measured {kind} duration ({self.window_days} d): p50 {percentile(obs, 0.5):.1f} s, "
                       f"p95 {percentile(obs, 0.95):.1f} s per side over {items} {noun}")
        return out


def _since(now: datetime | None, window_days: int) -> tuple[str, str]:
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ((end - timedelta(days=window_days)).isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))


def _observations(store: Store, column: str, since: str, until: str,
                  only: str | None = None) -> dict[str, list[float]]:
    """Item id -> per-side, per-run seconds (choice 1) for the `checkpoint_id` or `flow_id` column."""
    sql = (f"SELECT {column} AS item, run_id, side, SUM(COALESCE(duration_ms, 0)) AS ms FROM step_events "
           f"WHERE {column} IS NOT NULL AND {_LEFT_OUT} AND wall_at > ? AND wall_at <= ?"
           + (f" AND {column} = ?" if only is not None else "") + f" GROUP BY {column}, run_id, side")
    args: tuple = (since, until) + ((only,) if only is not None else ())
    out: dict[str, list[float]] = defaultdict(list)
    for r in store.query(sql, args):
        if r["ms"] > 0:
            out[r["item"]].append(r["ms"] / 1000.0)
    return out


def durations(store: Store, window_days: int = WINDOW_DAYS, *, now: datetime | None = None) -> Durations:
    """Measured per-side p50 and p95 of every checkpoint and flow over the `window_days` days ending at `now`."""
    since, until = _since(now, window_days)
    cps = _observations(store, "checkpoint_id", since, until)
    flows = _observations(store, "flow_id", since, until)
    samples: dict[str, list[float]] = defaultdict(list)
    for cid, obs in cps.items():
        samples[cid.split("/", 1)[0]].extend(obs)
    for obs in flows.values():
        samples["flow"].extend(obs)
    return Durations({k: Stat.of(v) for k, v in sorted(cps.items())}, {k: Stat.of(v) for k, v in sorted(flows.items())},
                     window_days, {k: tuple(v) for k, v in samples.items()})


def flow_duration_p50(store: Store, flow_id: str, window_days: int = WINDOW_DAYS, *,
                      now: datetime | None = None) -> float | None:
    """One flow's measured per-side p50 in seconds over the window; None until measured (L-FLW-10, 11.2)."""
    since, until = _since(now, window_days)
    obs = _observations(store, "flow_id", since, until, only=flow_id).get(flow_id)
    return percentile(obs, 0.5) if obs else None


def flow_budget_s(store: Store, flow_id: str, window_days: int = WINDOW_DAYS, *, now: datetime | None = None) -> float:
    """The 11.2 flow budget in seconds: 3 x the measured p50, 5 minutes until measured."""
    p50 = flow_duration_p50(store, flow_id, window_days, now=now)
    return DEFAULT_FLOW_S if p50 is None else FLOW_BUDGET_FACTOR * p50
