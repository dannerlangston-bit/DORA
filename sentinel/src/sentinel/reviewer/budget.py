"""Per-run call caps and dollar ceilings (3.15, 10.7, 11.4).

`SpendLedger` is the run's model spend: the Orchestrator shares one ledger between the Reviewer,
agent rescue and the Explorer (11.4: the per-run ceiling counts every model call of the run), seeded
with the week's spend so far. Before every model call the job asks `stop_reason()`: `run` once the
run's spend reached the per-run ceiling (the Orchestrator then ends the run `aborted_budget (reason:
dollar_cap)`), `week` once the week reached the weekly cap (no run is aborted). Calls already in
flight finish and are charged; no new one starts.

Choices where the spec is ambiguous (3.15):

- "Ranked by tier then component area": tier ascending, then the summed area of the changed
  components descending, then the queue key, so the order is total and deterministic.
- An item served from the verdict cache makes no call and does not count against the call cap.
- Cost is priced from the usage block: cache writes at the 1-hour rate (the only TTL this client
  sets), and the batch factor applied to every token class (the discounts stack).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Sequence

from .config import Prices
from .prompt import ReviewItem
from .transport import Usage


def cost_usd(usage: Usage, prices: Prices, *, batch: bool) -> float:
    """Dollars for one usage block."""
    usd = (usage.input_tokens * prices.input + usage.output_tokens * prices.output
           + usage.cache_creation_input_tokens * prices.cache_write
           + usage.cache_read_input_tokens * prices.cache_read) / 1_000_000
    return usd * (prices.batch_factor if batch else 1.0)


@dataclass
class SpendLedger:
    """Model spend of one run and of its week, against the 50 and 500 dollar ceilings (11.4)."""

    run_ceiling_usd: float = 50.0
    weekly_cap_usd: float = 500.0
    run_spent_usd: float = 0.0
    week_spent_usd: float = 0.0
    by_component: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def charge(self, usd: float, component: str = "reviewer") -> None:
        if usd < 0:
            raise ValueError("spend is never negative")
        with self._lock:
            self.run_spent_usd += usd
            self.week_spent_usd += usd
            self.by_component[component] = self.by_component.get(component, 0.0) + usd

    def stop_reason(self) -> str | None:
        """`run` or `week` when that ceiling is reached and no model call may start, else None."""
        with self._lock:
            if self.run_spent_usd >= self.run_ceiling_usd:
                return "run"
            if self.week_spent_usd >= self.weekly_cap_usd:
                return "week"
            return None

    @property
    def run_ceiling_reached(self) -> bool:
        """The trigger of `aborted_budget (reason: dollar_cap)` (3.15)."""
        return self.stop_reason() == "run"


def rank_for_cap(items: Sequence[ReviewItem]) -> list[ReviewItem]:
    """The 3.15 cap order: tier-1 first, larger changes first within a tier."""
    return sorted(items, key=lambda i: (i.tier, -i.diff.component_area, i.queue_key))
