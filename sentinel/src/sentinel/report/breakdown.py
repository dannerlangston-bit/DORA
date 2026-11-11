"""Run health's breakdown (5.13, 5.4.8, 5.5), read from the Central Store for one run.

5.13 lists what run health shows beyond the PR comment's totals: "gateway drops by reason and mode, focus corrections,
popups, time to settle, agent spend, hook reinstalls, rescue cap state"; 5.4.8: "Run health prints drops by reason and
mode". `query(store, run_id)` reads them from `step_events`, `outcomes` and `model_usage` (8.3) into a `Breakdown`, and
`Breakdown.rows()` gives the (label, text) rows the HTML report appends to its Run health table (9.4 item 7, 9.5).
The PR comment keeps the 9.2 drop total (`model.health_rows`).

Choices where the spec is ambiguous (reported):

1. Drops are the drainer's `gateway_decision` rows the hook dropped or the slow half refused, the rows
   `Store.gateway_drops` counts, grouped by mode, decision and reason code (`gate_reason`, never a coarse class,
   4.13), most frequent first. Rows tagged `drill` (6.5) are counted apart as "matched by the fire drill": the drill
   expects them and they count toward no threshold (5.4.8).
2. Focus corrections and hook reinstalls are the `focus_correction` and `hook_reinstalled` step events; popups the
   `popup` step events grouped by `popup_title` (the catalog entry matched, or the raw title of an unknown popup).
3. Time to settle is the nearest-rank p50 and p95 of the run's non-synthetic outcome rows' `settle_ms` (3.5, 5.7);
   no row carrying it prints "not recorded".
4. Agent spend is the run's `model_usage.cost_usd` of components `agent_rescue` and `explorer` (11.4); the Reviewer's
   spend has its own run-health row (9.2).
5. Rescued steps are the distinct (shard, flow, step) whose step events show rung 4: mode `agent`, locator level
   `agent` or the step outcome `passed_via_agent_fallback` (5.5); the cap is `max_rescued_steps_per_run` of
   `policy/budgets.yaml` (default 25), and a spent cap prints the 5.5 line (`executor.locator.RescueCap.health_line`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..executor.locator import RESCUE_STEPS, RescueCap
from .inline import plural

if TYPE_CHECKING:
    from ..store.store import Store

AGENT_COMPONENTS = ("agent_rescue", "explorer")       # 11.4 model_usage components of agent spend
_COMPONENT_SAID = {"agent_rescue": "agent rescue", "explorer": "Explorer"}

_DROPS_SQL = ("SELECT mode, gate_decision, gate_reason, drill, count(*) AS n FROM step_events WHERE run_id = ? "
              "AND action = 'gateway_decision' AND gate_decision IN ('dropped','refused') "
              "GROUP BY mode, gate_decision, gate_reason, drill")
_COUNT_SQL = "SELECT action, count(*) AS n FROM step_events WHERE run_id = ? AND action IN " \
             "('focus_correction','hook_reinstalled') GROUP BY action"
_POPUP_SQL = ("SELECT coalesce(popup_title, '(no title)') AS title, count(*) AS n FROM step_events "
              "WHERE run_id = ? AND action = 'popup' GROUP BY title")
_SETTLE_SQL = "SELECT settle_ms FROM outcomes WHERE run_id = ? AND synthetic = 0 AND settle_ms IS NOT NULL"
_SPEND_SQL = "SELECT component, sum(cost_usd) AS usd FROM model_usage WHERE run_id = ? GROUP BY component"
_RESCUE_SQL = ("SELECT count(*) FROM (SELECT DISTINCT shard_id, flow_id, step_id FROM step_events WHERE run_id = ? "
               "AND step_id IS NOT NULL AND (mode = 'agent' OR locator_level = 'agent' "
               "OR step_outcome = 'passed_via_agent_fallback'))")


@dataclass(frozen=True)
class DropCount:
    """Gateway drops of one mode, decision and reason code (5.4.8)."""

    mode: str
    decision: str                  # dropped (by the hook) | refused (by the slow half)
    reason: str
    count: int

    def text(self) -> str:
        return f"{self.count} {self.reason} {self.decision} ({self.mode})"


def percentile(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile of `values` (choice 3); None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


@dataclass(frozen=True)
class Breakdown:
    """The 5.13 run-health facts of one run (module docstring)."""

    drops: tuple[DropCount, ...] = ()
    drill_drops: int = 0
    focus_corrections: int = 0
    popups: tuple[tuple[str, int], ...] = ()
    settle_ms: tuple[int, ...] = ()
    agent_spend: tuple[tuple[str, float], ...] = ()
    hook_reinstalls: int = 0
    rescued_steps: int = 0
    rescue_cap: int = RESCUE_STEPS

    @property
    def rescue(self) -> RescueCap:
        return RescueCap(max_steps=self.rescue_cap, used=self.rescued_steps)

    def _drops_text(self) -> str:
        text = "; ".join(d.text() for d in self.drops) if self.drops else "0"
        if self.drill_drops:
            text += f"; {plural(self.drill_drops, 'drop')} matched by the fire drill, not counted"
        return text

    def _settle_text(self) -> str:
        values = list(self.settle_ms)
        if not values:
            return "not recorded"
        p50, p95 = percentile(values, 50), percentile(values, 95)
        return f"p50 {p50 / 1000:.2f} s, p95 {p95 / 1000:.2f} s over {plural(len(values), 'checkpoint')}"

    def _spend_text(self) -> str:
        spent = dict(self.agent_spend)
        parts = [f"{_COMPONENT_SAID[c]} {spent.get(c, 0.0):.2f}" for c in AGENT_COMPONENTS]
        return f"{sum(spent.get(c, 0.0) for c in AGENT_COMPONENTS):.2f} USD ({', '.join(parts)})"

    def rows(self) -> list[tuple[str, str]]:
        """The run-health rows of 5.13, in its order."""
        popups = sum(n for _, n in self.popups)
        popup_text = str(popups) + (f" ({', '.join(f'{t} {n}' for t, n in self.popups)})" if popups else "")
        cap = self.rescue.health_line() or f"{self.rescued_steps} of {self.rescue_cap} rescued steps used"
        return [("Gateway drops by reason and mode", self._drops_text()),
                ("Focus corrections", str(self.focus_corrections)),
                ("Popups", popup_text),
                ("Time to settle", self._settle_text()),
                ("Agent spend", self._spend_text()),
                ("Hook reinstalls", str(self.hook_reinstalls)),
                ("Agent rescue cap", cap)]


def query(store: "Store", run_id: str, *, rescue_cap: int = RESCUE_STEPS) -> Breakdown:
    """The breakdown of `run_id` from the store (module docstring); `rescue_cap` is the run's
    `max_rescued_steps_per_run` (policy/budgets.yaml, 5.5)."""
    drops: list[DropCount] = []
    drill = 0
    for r in store.query(_DROPS_SQL, (run_id,)):
        if r["drill"]:
            drill += r["n"]
        else:
            drops.append(DropCount(r["mode"], r["gate_decision"], r["gate_reason"], r["n"]))
    drops.sort(key=lambda d: (-d.count, d.mode, d.decision, d.reason))
    counts = {r["action"]: r["n"] for r in store.query(_COUNT_SQL, (run_id,))}
    popups = sorted(((r["title"], r["n"]) for r in store.query(_POPUP_SQL, (run_id,))), key=lambda x: (-x[1], x[0]))
    settle = tuple(int(r["settle_ms"]) for r in store.query(_SETTLE_SQL, (run_id,)))
    spend = tuple((r["component"], float(r["usd"] or 0.0)) for r in store.query(_SPEND_SQL, (run_id,))
                  if r["component"] in AGENT_COMPONENTS)
    rescued = store.query(_RESCUE_SQL, (run_id,))[0][0]
    return Breakdown(tuple(drops), drill, counts.get("focus_correction", 0), tuple(popups), settle, spend,
                     counts.get("hook_reinstalled", 0), rescued, rescue_cap)
