"""Per-component model usage, the cost alarms and the Reviewer backlog alarm (11.4 Cost model, 11.6, 9.10 "Preflight
failures and cost alarm" and "Reviewer backlog" rows).

11.4: "the Orchestrator stores every response's usage block per component and a 2x week-over-week change or a cap hit
posts to the on-call channel". The `model_usage` table (8.3) holds one row per model response:

- `record(store, component=..., model_id=..., usage=..., cost_usd=..., at=...)` writes one response's usage block
  (`reviewer.transport.Usage` or the API's usage mapping) for the Reviewer, agent rescue or the Explorer; with
  `prices` instead of `cost_usd` the dollars come from `reviewer.budget.cost_usd`, the one pricing rule.
- `record_step_usage(store, run_id, prices)` copies the usage blocks agent and Explorer steps carry in
  `step_events.usage_json` (11.4, SD-16) into `model_usage`, once per step event.
- `spend(store, since, until)` sums dollars per component.
- `weekly_alarms(store, now, caps=...)` returns the 9.10 P2 alerts (`orchestrator.alerts.model_degraded`, the
  "Preflight failures and cost alarm" row) for a 2x week-over-week spend change and for every cap hit of the week.
- `backlog_alarms(store, now, ...)` returns the 9.10 P2 "Reviewer backlog" alerts (`orchestrator.alerts.
  reviewer_backlog`, 4-hour response, 11.6).

Each alarm is a `report.alerting.Alert` for `report.alerting.Dispatcher.dispatch`; its key is stable for a week (cost)
or a run (caps, backlog), so dispatching the same alarm twice is visible as one key.

Choices where the spec is ambiguous (each is reported):

1. Week over week: "this week" is the 7 days ending `now`, "last week" the 7 days before. The ratio is compared per
   component and for the total; a ratio of 2 or more raises the alarm (a fall is not a "2x change" that costs
   money). A component with no spend last week has no ratio and raises nothing: there is no baseline to double.
2. Cap hits inside this week, each from the store (caps from `policy/budgets.yaml`, `Caps.from_budgets`): the weekly
   cap (this week's spend at or above `usd.per_week`); the per-run ceiling (a run's spend at or above `usd.per_run`,
   or a run ended `aborted_budget (reason: dollar_cap)`); the Reviewer call cap (a run with `needs_human (reason:
   reviewer_cap)` items, 3.15); the rescue cap (a run whose agent-mode steps reached `rescue.
   max_rescued_steps_per_run`, counting one rescue per distinct flow step, 5.5); the Explorer session cap (a run whose
   Explorer spend reached `usd.per_explorer_session`: an RC run has one Explorer session, 2.9). Runs count by
   `started_at`.
3. Reviewer backlog (9.10): a pending review item (`outcomes.review_state = pending`, non-synthetic) whose row was
   recorded more than 4 hours before `now` on a merge run, more than 1 hour on a PR run when the commit status is
   blocking (`pr_blocking`), or any pending item on the RC runs whose report is being written (`rc_reporting`). One
   alert per run, naming the count and the oldest item's age. Nightly and Calibration runs have no backlog alarm.
4. `record_step_usage` keys each row by `request_id = step:<shard_id>:<seq>` so a second call writes nothing new;
   mode `agent` is the `agent_rescue` component and mode `explorer` the `explorer` component; a step's usage block
   carries no response model id, so the row takes the step's `model_id` column.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .orchestrator import alerts
from .records import MODEL_COMPONENTS, ModelUsage
from .report.alerting import Alert, RunRef
from .reviewer.budget import cost_usd as priced
from .reviewer.config import Prices
from .reviewer.transport import Usage
from .store import Store

WEEK = timedelta(days=7)
WOW_RATIO = 2.0                                  # 11.4, 9.10: a 2x week-over-week change
BACKLOG_MERGE = timedelta(hours=4)               # 9.10 Reviewer backlog
BACKLOG_PR_BLOCKING = timedelta(hours=1)
STEP_COMPONENTS = {"agent": "agent_rescue", "explorer": "explorer"}   # choice 4
TOTAL = "total"


@dataclass(frozen=True)
class Caps:
    """The 11.4 caps of `policy/budgets.yaml` the alarms watch (choice 2)."""

    per_run_usd: float = 50.0
    per_week_usd: float = 500.0
    per_explorer_session_usd: float = 25.0
    rescued_steps_per_run: int = 25

    @classmethod
    def from_budgets(cls, doc: Mapping[str, Any]) -> Caps:
        """From the parsed `policy/budgets.yaml` (`usd`, `rescue`); a missing key keeps the 11.4 default."""
        usd, rescue = doc.get("usd") or {}, doc.get("rescue") or {}
        return cls(float(usd.get("per_run", 50.0)), float(usd.get("per_week", 500.0)),
                   float(usd.get("per_explorer_session", 25.0)), int(rescue.get("max_rescued_steps_per_run", 25)))


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _usage(usage: Usage | Mapping[str, Any]) -> Usage:
    if isinstance(usage, Usage):
        return usage
    return Usage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                 int(usage.get("cache_creation_input_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0))


def record(store: Store, *, component: str, model_id: str, usage: Usage | Mapping[str, Any], at: str,
           cost_usd: float | None = None, prices: Prices | None = None, batch: bool = False,
           run_id: str | None = None, request_id: str | None = None) -> int:
    """Store one response's usage block for its component (11.4); returns the row id. Give `cost_usd`, or `prices`
    (and `batch`) to price it with `reviewer.budget.cost_usd`."""
    u = _usage(usage)
    if cost_usd is None:
        if prices is None:
            raise ValueError("record needs cost_usd or prices (11.4)")
        cost_usd = priced(u, prices, batch=batch)
    return store.write_model_usage(ModelUsage(component, model_id, at, run_id, request_id, u.input_tokens,
                                              u.output_tokens, u.cache_read_input_tokens, float(cost_usd)))


def record_step_usage(store: Store, run_id: str, prices: Prices) -> int:
    """Copy the usage block of every agent and Explorer step event of the run into `model_usage` (choice 4); returns
    how many rows were written."""
    done = {r[0] for r in store.query("SELECT request_id FROM model_usage WHERE run_id = ? AND request_id LIKE "
                                      "'step:%'", (run_id,))}
    rows = store.query("SELECT shard_id, seq, mode, model_id, wall_at, usage_json FROM step_events WHERE run_id = ? "
                       "AND usage_json IS NOT NULL AND mode IN ('agent', 'explorer') ORDER BY shard_id, seq",
                       (run_id,))
    written = 0
    for r in rows:
        key = f"step:{r['shard_id']}:{r['seq']}"
        if key in done:
            continue
        record(store, component=STEP_COMPONENTS[r["mode"]], model_id=r["model_id"] or "unknown",
               usage=json.loads(r["usage_json"]), at=r["wall_at"], prices=prices, run_id=run_id, request_id=key)
        written += 1
    return written


def spend(store: Store, since: datetime, until: datetime) -> dict[str, float]:
    """Dollars per component, and `total`, over `since` (exclusive) to `until` (inclusive)."""
    out = {c: 0.0 for c in MODEL_COMPONENTS}
    for r in store.query("SELECT component, SUM(cost_usd) FROM model_usage WHERE at > ? AND at <= ? GROUP BY "
                         "component", (_iso(since), _iso(until))):
        out[r[0]] = float(r[1] or 0.0)
    out[TOTAL] = sum(out[c] for c in MODEL_COMPONENTS)
    return out


def _alert(route: alerts.AlertRoute, key: str, detail: str, store: Store, run_id: str | None = None) -> Alert:
    return Alert.of(route, detail=detail, key=key, run=RunRef.from_store(store, run_id) if run_id else None)


def _wow(store: Store, now: datetime) -> list[Alert]:
    this, last = spend(store, now - WEEK, now), spend(store, now - 2 * WEEK, now - WEEK)
    out = []
    for c in (*MODEL_COMPONENTS, TOTAL):
        if last[c] > 0 and this[c] / last[c] >= WOW_RATIO:
            out.append(_alert(alerts.model_degraded(), f"cost_wow:{c}:{now.date().isoformat()}",
                              f"model spend {this[c] / last[c]:.1f}x week over week ({c}: {this[c]:.2f} USD this "
                              f"week, {last[c]:.2f} USD last week; 11.4)", store))
    return out


def _caps(store: Store, now: datetime, caps: Caps) -> list[Alert]:
    since, until = _iso(now - WEEK), _iso(now)
    week = spend(store, now - WEEK, now)[TOTAL]
    out = []
    if week >= caps.per_week_usd:
        out.append(_alert(alerts.model_degraded(), f"cap:week:{now.date().isoformat()}",
                          f"weekly model spend cap hit: {week:.2f} of {caps.per_week_usd:g} USD; model calls stop for "
                          "the rest of the week (11.4)", store))
    runs = store.query("SELECT r.run_id, r.outcome, r.abort_reason, "
                       "(SELECT COALESCE(SUM(u.cost_usd), 0) FROM model_usage u WHERE u.run_id = r.run_id) AS usd, "
                       "(SELECT COALESCE(SUM(u.cost_usd), 0) FROM model_usage u WHERE u.run_id = r.run_id "
                       " AND u.component = 'explorer') AS explorer_usd, "
                       "(SELECT COUNT(*) FROM outcomes o WHERE o.run_id = r.run_id AND o.synthetic = 0 "
                       " AND o.outcome = 'needs_human' AND o.reason = 'reviewer_cap') AS capped, "
                       "(SELECT COUNT(DISTINCT e.shard_id || '/' || COALESCE(e.flow_id, '') || '/' || "
                       " COALESCE(e.step_id, '')) FROM step_events e WHERE e.run_id = r.run_id AND e.mode = 'agent' "
                       " AND e.drill = 0) AS rescues "
                       "FROM runs r WHERE r.started_at > ? AND r.started_at <= ? ORDER BY r.started_at, r.run_id",
                       (since, until))
    for r in runs:
        rid = r["run_id"]
        if r["usd"] >= caps.per_run_usd or (r["outcome"] == "aborted_budget" and r["abort_reason"] == "dollar_cap"):
            out.append(_alert(alerts.model_degraded(), f"cap:run:{rid}", f"per-run model spend ceiling hit: "
                              f"{r['usd']:.2f} of {caps.per_run_usd:g} USD (11.4)", store, rid))
        if r["explorer_usd"] >= caps.per_explorer_session_usd:
            out.append(_alert(alerts.model_degraded(), f"cap:explorer:{rid}", f"Explorer session cap hit: "
                              f"{r['explorer_usd']:.2f} of {caps.per_explorer_session_usd:g} USD (11.4, 2.9)", store,
                              rid))
        if r["capped"]:
            out.append(_alert(alerts.model_degraded(), f"cap:reviewer_items:{rid}", f"Reviewer call cap hit: "
                              f"{r['capped']} items needs_human (reason: reviewer_cap) (3.15, 11.4)", store, rid))
        if r["rescues"] >= caps.rescued_steps_per_run:
            out.append(_alert(alerts.model_degraded(), f"cap:rescue:{rid}", f"agent rescue cap hit: {r['rescues']} "
                              f"rescued steps of {caps.rescued_steps_per_run} (5.5, 11.4)", store, rid))
    return out


def weekly_alarms(store: Store, now: datetime, caps: Caps = Caps()) -> tuple[Alert, ...]:
    """The cost alarms of 11.4 and 9.10 at `now`: a 2x week-over-week spend change per component or in total, and
    every cap hit of the week (choices 1 and 2). P2 to the on-call channel, same day."""
    return tuple(_wow(store, now) + _caps(store, now, caps))


def backlog_alarms(store: Store, now: datetime, *, pr_blocking: bool = False,
                   rc_reporting: Iterable[str] = ()) -> tuple[Alert, ...]:
    """The 9.10 Reviewer backlog alerts at `now` (choice 3): pending items past 4 h on a merge run, past 1 h on a PR
    run in blocking mode, any on an RC run in `rc_reporting`. P2, 4-hour response (11.6)."""
    rc = frozenset(rc_reporting)
    rows = store.query("SELECT r.run_id, r.run_type, COUNT(*) AS n, MIN(o.recorded_at) AS oldest FROM outcomes o "
                       "JOIN runs r ON r.run_id = o.run_id WHERE o.review_state = 'pending' AND o.synthetic = 0 "
                       "GROUP BY r.run_id, r.run_type ORDER BY r.run_id")
    out = []
    for r in rows:
        age = now - datetime.fromisoformat(r["oldest"])
        limit = {"merge": BACKLOG_MERGE, "pr": BACKLOG_PR_BLOCKING if pr_blocking else None}.get(r["run_type"])
        due = (r["run_type"] == "rc" and r["run_id"] in rc) or (limit is not None and age > limit)
        if not due:
            continue
        hours = age.total_seconds() / 3600
        out.append(_alert(alerts.reviewer_backlog(), f"reviewer_backlog:{r['run_id']}",
                          f"{r['n']} review items pending on {r['run_type']} run {r['run_id']}, oldest {hours:.1f} h "
                          "(9.10)", store, r["run_id"]))
    return tuple(out)
