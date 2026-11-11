"""Flow maintenance: health figures per flow, the ticket and task triggers, run health's fallback clusters, the hours
ledger and the budget-overrun cut (11.5 Recorder, Flow fields, the role table and ledger, Budget overrun, "The
morning after a toolbar redesign"; 10.5 last paragraph; 12.2 `passed_after_retry` and `passed_via_agent_fallback`;
5.16 Key internals; SD-55).

Health figures are derived, never flow-file fields (SD-55): the 30-day `agent_fallback_rate` and `flake_rate` come
from `step_events` and outcomes, `last_repaired` from git.

- `rates(store, flow, today=)` and `flow_rates(store, today)`: `FlowRates` per flow (steps, fallbacks, the agent
  fallback rate, compared observations, unstable plus flips, the flake rate); `last_repaired(root, path)` from git.
- `ticket_triggers(store, now)`: two consecutive `passed_via_agent_fallback` on one step on one build open an
  "update scripted flow" ticket (`FallbackTicket`, a SENT task through `report.jira.SentEvent`); three
  `passed_after_retry` on one step in 30 days open a flow maintenance task (`RetryTask`, a `maintenance_items` row).
  `file_triggers(store, triggers, ...)` queues the tickets with `report.jira_sync.sent_task` and writes the tasks.
- `fallback_clusters(store, run_id, anchors=)`: the run's flows in fallback clustered by anchor template ("38 flows
  in fallback, 31 on anchor toolbar_trade_tab"), the run-health row; `step_anchors(flows, surfaces)` names each
  flow step's anchor template.
- The ledger: `open_items(store, items)` writes maintenance items not already open, `log_hours` and `close_item`
  record the hours and the close; `week_hours(store, week, flows=)` sums logged hours per 11.5 role.
- `budget_overrun(store, week, flows=)`: a role over budget two consecutive weeks cuts the lowest-tier flows with the
  highest fallback or flake rate (`quarantine.Proposal`, reason `maintenance_budget`) until the open backlog fits,
  with the report header line "tier 3 coverage reduced: maintenance budget, N flows"; a tier-1 overrun pages the
  system owner, freezes new flows and is reported to the release manager, and cuts nothing.
- `reporter_metrics(store, ...)`: fallback rate per flow, flows repaired in a release window and maintenance items
  past SLA, as run-health rows.

Choices where the spec is ambiguous or silent (reported):

1. A step row is a `step_events` row with `step_outcome` set (one per executed step, 8.3), fire-drill rows left
   out; its build is the run's candidate build on the candidate side and its base build on the base side (8.3
   `build_pairs`), else the side's fingerprint build, else a build of its own (`run:<id>`), as `sentinel.flake`.
   The window is the 30 UTC days ending `today` by run start (`flake.window`).
2. The fallback rate is fallback step rows over step rows (9.11 Harness health, `quarantine` choice 4); the flake rate
   is unstable outcomes plus verdict flips over compared observations summed over the flow's checkpoints
   (`flake.collect`, 10.5). Both are None without a row.
3. "Two consecutive fallbacks on one step on one build" (11.5; 10.5 and 12.2 say "two fallbacks"): two adjacent
   rows of the step's executions on that build, ordered by run start, run and seq, both `passed_via_agent_fallback`;
   the latest such pair gives the trigger, carried by its later run. The SENT key is the trigger and `<flow>/<step>`
   (`report.jira` choice 7: one open ticket per key, one post per run per key), so re-evaluating repeats nothing.
4. "Three in 30 days on one step" counts `passed_after_retry` rows of the step on any build. The task is a
   `maintenance_items` row of kind `maintenance_task` (8.3), `ref` `<flow>/<step>`, written only when no open item of
   that kind and ref exists. Rows of runs that started before the last close of the step's task (or of its SENT
   ticket's ledger row, for fallbacks) do not count again: the repair answered them.
5. The anchor template of a step is the file stem of its target control's `template` (4.5), else the scripted
   target the step's events name, else `unknown`; a flow counts once per anchor it fell back on.
6. Roles are the rows of the 11.5 table (`ROLE_HOURS`). An item whose `ref` names a flow (`<flow>`, `<flow>/<step>`,
   `<trigger>:<flow>/<step>`, or a flow checkpoint's `<checkpoint>#<landmark>`) counts toward the flow owner role of
   that flow's tier; any other item toward its `owner` when that is a role name; the rest is unattributed. An item's
   hours fall in the week (Monday to Sunday, UTC) of its `closed_at`, or of its `opened_at` while it is open: 8.3
   keeps one running total per item, not dated entries.
7. The cut: the open backlog is the hours logged on the open items of the roles over budget (the tier-1 flow owner
   role excepted); flows are taken tier 3 first, then tier 2, each tier by its highest of fallback and flake rate,
   then flow id; a flow is cut only when it holds part of that backlog, and cutting stops once the backlog is at most
   the sum of those roles' weekly budgets. A flow already quarantined is not proposed again.
8. A maintenance item is past SLA five business days after it opened (`sla.BusinessClock`): 11.5 gives the owner of a
   quarantined item five business days; no other SLA is stated for maintenance items.
9. Flows repaired in a release are the distinct flows of maintenance items closed inside the release window the
   caller names (choice 6 for which flow an item names).
"""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import flake
from .lint.rules import TARGET_FIELDS
from .quarantine import Proposal
from .records import MaintenanceItem
from .report import jira
from .sla import BusinessClock
from .store.store import Store, utc
from .taxonomy import TaxonomyError, checkpoint_parts

WINDOW_DAYS = flake.WINDOW_DAYS                    # 11.5: 30 days
FALLBACK = "passed_via_agent_fallback"
RETRY = "passed_after_retry"
FALLBACKS_FOR_TICKET = 2                           # 11.5, 10.5: two (consecutive) fallbacks on one step and build
RETRIES_FOR_TASK = 3                               # 12.2: three in 30 days on one step
TASK_KIND = "maintenance_task"                     # 8.3 maintenance_items.kind (choice 4)
SLA_BUSINESS_DAYS = 5                              # choice 8
#: 11.5 role table: hours per week.
ROLE_HOURS: Mapping[str, float] = {
    "system_owner": 16, "triage_rotation": 5, "tier1_flow_owner": 4, "tier2_flow_owner": 3, "tier3_flow_owner": 2,
    "registry_owner": 2, "accounts_owner": 1, "harness_on_call": 2,
}
TIER1_ROLE = "tier1_flow_owner"
CUT_TIERS = (3, 2)                                 # 11.5: tier 3 first, then tier 2; tier 1 never
_TRIGGER_PREFIX = re.compile(r"^[a-z_]+:(?=\S)")


@dataclass(frozen=True)
class FlowFacts:
    """What the ledger needs of a flow file (4.7): its tier and owner."""

    tier: int
    owner: str

    @classmethod
    def of(cls, docs: Iterable[Mapping[str, Any]]) -> dict[str, "FlowFacts"]:
        """From flow documents."""
        return {str(d["flow_id"]): cls(int(d.get("tier", 3)), str(d.get("owner", "flow_owner")))
                for d in docs if isinstance(d, Mapping) and d.get("flow_id")}


# ---------------------------------------------------------------------------------------------------------------
# step rows (choice 1)

@dataclass(frozen=True)
class _Row:
    run_id: str
    started_at: str
    shard_id: str
    seq: int
    side: str
    build: str
    flow_id: str
    step_id: str
    status: str


def _today(at: date | datetime | str) -> date:
    if isinstance(at, datetime):
        return at.astimezone(timezone.utc).date() if at.tzinfo else at.date()
    if isinstance(at, date):
        return at
    return _today(datetime.fromisoformat(utc(at) or at))


def _rows(store: Store, today: date, *, flow: str | None = None) -> list[_Row]:
    first, last = flake.window(today, WINDOW_DAYS)
    sql = ("SELECT e.run_id, r.started_at, e.shard_id, e.seq, e.side, e.flow_id, e.step_id, e.step_outcome, "
           "bp.candidate_build_id AS cand, bp.base_build_id AS base, "
           "(SELECT f.build_id FROM env_fingerprints f WHERE f.run_id = e.run_id AND f.side = e.side "
           " ORDER BY f.fingerprint_id LIMIT 1) AS fp "
           "FROM step_events e JOIN runs r ON r.run_id = e.run_id LEFT JOIN build_pairs bp ON bp.pair_id = r.pair_id "
           "WHERE e.step_outcome IS NOT NULL AND e.drill = 0 AND e.flow_id IS NOT NULL AND e.step_id IS NOT NULL "
           "AND substr(r.started_at, 1, 10) BETWEEN ? AND ?")
    args: tuple = (first, last)
    if flow is not None:
        sql, args = sql + " AND e.flow_id = ?", args + (flow,)
    out = []
    for r in store.query(sql + " ORDER BY r.started_at, e.run_id, e.shard_id, e.seq", args):
        build = (r["cand"] if r["side"] == "candidate" else r["base"]) or r["fp"] or f"run:{r['run_id']}"
        out.append(_Row(r["run_id"], r["started_at"], r["shard_id"], r["seq"], r["side"], build, r["flow_id"],
                        r["step_id"], r["step_outcome"]))
    return out


# ---------------------------------------------------------------------------------------------------------------
# health figures (SD-55, choice 2)

@dataclass(frozen=True)
class FlowRates:
    """One flow's 30-day health figures (5.16, 11.5 Flow fields), derived, never written in the flow file."""

    flow_id: str
    steps: int = 0
    fallbacks: int = 0
    observations: int = 0
    flaky: int = 0
    last_repaired: str | None = None

    @property
    def agent_fallback_rate(self) -> float | None:
        return self.fallbacks / self.steps if self.steps else None

    @property
    def flake_rate(self) -> float | None:
        return self.flaky / self.observations if self.observations else None

    @property
    def worst(self) -> float:
        """The higher of the two rates (0 without data): the order of the budget cut (11.5)."""
        return max(self.agent_fallback_rate or 0.0, self.flake_rate or 0.0)

    def line(self) -> str:
        """`flow.x: agent_fallback_rate 12.5% (1 of 8 steps), flake_rate 0.0% (0 of 4), last_repaired 2026-10-02`."""
        def pct(rate: float | None, n: int, d: int, unit: str) -> str:
            return "no data" if rate is None else f"{100 * rate:.1f}% ({n} of {d}{unit})"
        fallback = pct(self.agent_fallback_rate, self.fallbacks, self.steps, " steps")
        flaky = pct(self.flake_rate, self.flaky, self.observations, "")
        repaired = self.last_repaired[:10] if self.last_repaired else "unknown"
        return f"{self.flow_id}: agent_fallback_rate {fallback}, flake_rate {flaky}, last_repaired {repaired}"


def flow_rates(store: Store, today: date | datetime | str, *, flows: Iterable[str] | None = None,
               repaired: Mapping[str, str | None] | None = None) -> dict[str, FlowRates]:
    """Every flow's figures over the 30 days ending `today` (every flow seen, plus `flows`); `repaired` maps a flow
    to its `last_repaired` (`last_repaired`)."""
    day = _today(today)
    steps: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in _rows(store, day):
        steps[r.flow_id][0] += 1
        steps[r.flow_id][1] += int(r.status == FALLBACK)
    obs: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in flake.collect(store, day):
        if s.flow_id:
            obs[s.flow_id][0] += s.observations
            obs[s.flow_id][1] += s.unstable_total + s.flip_total
    ids = set(steps) | set(obs) | set(flows or ())
    rep = repaired or {}
    return {f: FlowRates(f, steps[f][0], steps[f][1], obs[f][0], obs[f][1], rep.get(f)) for f in sorted(ids)}


def rates(store: Store, flow: str, *, today: date | datetime | str, last_repaired: str | None = None) -> FlowRates:
    """One flow's 30-day figures (5.16)."""
    return flow_rates(store, today, flows=[flow], repaired={flow: last_repaired})[flow]


def last_repaired(config_root: str | Path, path: str) -> str | None:
    """The commit time (ISO 8601) of the last commit touching `path` in the sentinel-config checkout (11.5: "from git
    history"); None outside git or for a file git does not know."""
    try:
        out = subprocess.run(["git", "-C", str(config_root), "log", "-1", "--format=%cI", "--", path],
                             capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


# ---------------------------------------------------------------------------------------------------------------
# triggers (choices 3 and 4)

def step_ref(flow_id: str, step_id: str) -> str:
    """`<flow>/<step>`: the SENT subject of a fallback ticket and the `ref` of a retry task."""
    return f"{flow_id}/{step_id}"


@dataclass(frozen=True)
class FallbackTicket:
    """Two consecutive agent fallbacks on one step on one build: an "update scripted flow" ticket (11.5, 10.5)."""

    flow_id: str
    step_id: str
    build_id: str
    run_ids: tuple[str, str]

    @property
    def run_id(self) -> str:
        """The run of the second fallback, which the ticket is posted for."""
        return self.run_ids[1]

    def sent_event(self, owner: str | None = None) -> jira.SentEvent:
        """The SENT event (`report.jira`, trigger `passed_via_agent_fallback`, keyed by flow and step id)."""
        return jira.SentEvent(FALLBACK, step_ref(self.flow_id, self.step_id), self.run_id,
                              detail=f"two consecutive agent fallbacks on build {self.build_id}: runs "
                                     f"{self.run_ids[0]} and {self.run_ids[1]}", owner=owner)


@dataclass(frozen=True)
class RetryTask:
    """Three `passed_after_retry` on one step in 30 days: a flow maintenance task (12.2)."""

    flow_id: str
    step_id: str
    count: int
    run_ids: tuple[str, ...]

    def item(self, owner: str, opened_at: str) -> MaintenanceItem:
        return MaintenanceItem(TASK_KIND, step_ref(self.flow_id, self.step_id), owner, opened_at,
                               run_id=self.run_ids[-1],
                               detail=f"{self.count} passed_after_retry in {WINDOW_DAYS} days: "
                                      f"{', '.join(self.run_ids)}")


@dataclass(frozen=True)
class Triggers:
    tickets: tuple[FallbackTicket, ...] = ()
    tasks: tuple[RetryTask, ...] = ()


def _closed(store: Store, kind: str) -> dict[str, datetime]:
    """The latest close of each `ref` of one item kind (choice 4: evidence from before it does not reopen it)."""
    return {r["ref"]: _at(r["closed"]) for r in store.query(
        "SELECT ref, MAX(closed_at) AS closed FROM maintenance_items WHERE kind = ? AND closed_at IS NOT NULL "
        "GROUP BY ref", (kind,))}


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(utc(ts) or ts)


def ticket_triggers(store: Store, now: date | datetime | str) -> Triggers:
    """The tickets and tasks the 30 days ending `now` call for (module docstring); reads only. A step row from a run
    that started before the last close of its ticket or task does not count toward a new one."""
    from .report.jira_sync import LEDGER_KIND
    tickets_closed, tasks_closed = _closed(store, LEDGER_KIND), _closed(store, TASK_KIND)
    seqs: dict[tuple[str, str, str], list[_Row]] = defaultdict(list)
    retries: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in _rows(store, _today(now)):
        ref = step_ref(r.flow_id, r.step_id)
        if (c := tickets_closed.get(jira.task_key(FALLBACK, ref))) is None or _at(r.started_at) > c:
            seqs[(r.flow_id, r.step_id, r.build)].append(r)
        if r.status == RETRY and ((c := tasks_closed.get(ref)) is None or _at(r.started_at) > c):
            retries[(r.flow_id, r.step_id)].append(r.run_id)
    tickets = []
    for (flow, step, build), rows in sorted(seqs.items()):
        pairs = [(a, b) for a, b in zip(rows, rows[1:]) if a.status == FALLBACK and b.status == FALLBACK]
        if pairs:
            a, b = pairs[-1]
            tickets.append(FallbackTicket(flow, step, build, (a.run_id, b.run_id)))
    tasks = [RetryTask(flow, step, len(runs), tuple(dict.fromkeys(runs)))
             for (flow, step), runs in sorted(retries.items()) if len(runs) >= RETRIES_FOR_TASK]
    return Triggers(tuple(tickets), tuple(tasks))


@dataclass(frozen=True)
class Filed:
    """What `file_triggers` queued and wrote."""

    queued: tuple[Any, ...]                      # report.jira_sync.Queued
    tasks: tuple[int, ...]                       # maintenance_items ids


def file_triggers(store: Store, triggers: Triggers, *, report_base: str, at: str,
                  owners: Mapping[str, str] | None = None) -> Filed:
    """Queue each ticket as a SENT task (`report.jira_sync.sent_task`: one open ticket per key, one post per run per
    key) and write each retry task not already open; `owners` maps a flow to its owner (default the flow-owner
    role)."""
    from .report import jira_sync
    owners = owners or {}
    queued = [q for t in triggers.tickets
              if (q := jira_sync.sent_task(store, t.sent_event(owners.get(t.flow_id)), report_base=report_base,
                                           at=at)) is not None]
    ids = open_items(store, [t.item(owners.get(t.flow_id, "flow_owner"), at) for t in triggers.tasks])
    return Filed(tuple(queued), tuple(ids))


# ---------------------------------------------------------------------------------------------------------------
# run health: flows in fallback by anchor (choice 5)

def step_anchors(flows: Iterable[Mapping[str, Any]],
                 surfaces: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """(flow id, step id) -> the anchor template (file stem) of the step's target control (4.5, 4.7)."""
    controls = {f"{sid}.{c['id']}": str(c.get("template", "")) for sid, s in surfaces.items()
                for c in s.get("controls") or () if isinstance(c, Mapping) and c.get("id")}
    out = {}
    for f in flows:
        for blk in ("steps", "normalize", "teardown"):
            for st in f.get(blk) or ():
                t = next((st.get(k) for k in TARGET_FIELDS if isinstance(st, Mapping) and isinstance(st.get(k), str)
                          and st.get(k) in controls), None)
                if t is not None and controls[t]:
                    out[(str(f.get("flow_id")), str(st.get("id")))] = Path(controls[t]).stem
    return out


@dataclass(frozen=True)
class FallbackClusters:
    """A run's flows in fallback (10.5: a run health metric) and their clusters by anchor template (11.5)."""

    flows: tuple[str, ...]
    clusters: tuple[tuple[str, tuple[str, ...]], ...]

    def line(self) -> str:
        """`3 flows in fallback, 2 on anchor qty-field, 1 on anchor price-field`."""
        n = len(self.flows)
        head = f"{n} flow{'s' if n != 1 else ''} in fallback"
        return ", ".join([head] + [f"{len(fs)} on anchor {a}" for a, fs in self.clusters])

    def health_rows(self) -> list[tuple[str, str]]:
        return [("Flows in fallback", self.line())]


def fallback_clusters(store: Store, run_id: str, *,
                      anchors: Mapping[tuple[str, str], str] | None = None) -> FallbackClusters:
    """The run's flows with a `passed_via_agent_fallback` step, clustered by anchor template (choice 5)."""
    anchors = anchors or {}
    steps = store.query("SELECT DISTINCT flow_id, step_id FROM step_events WHERE run_id = ? AND step_outcome = ? "
                        "AND drill = 0 AND flow_id IS NOT NULL AND step_id IS NOT NULL", (run_id, FALLBACK))
    named = {(r["flow_id"], r["step_id"]): r["target_text"] for r in store.query(
        "SELECT flow_id, step_id, target_text FROM step_events WHERE run_id = ? AND mode = 'scripted' AND "
        "target_text IS NOT NULL AND target_text NOT LIKE '<redacted:%' ORDER BY shard_id, seq DESC", (run_id,))}
    by_anchor: dict[str, set[str]] = defaultdict(set)
    for r in steps:
        key = (r["flow_id"], r["step_id"])
        by_anchor[anchors.get(key) or named.get(key) or "unknown"].add(r["flow_id"])
    clusters = tuple(sorted(((a, tuple(sorted(fs))) for a, fs in by_anchor.items()), key=lambda c: (-len(c[1]), c[0])))
    return FallbackClusters(tuple(sorted({r["flow_id"] for r in steps})), clusters)


# ---------------------------------------------------------------------------------------------------------------
# the ledger (choice 6)

def open_items(store: Store, items: Iterable[MaintenanceItem]) -> list[int]:
    """Write each item that has no open item of the same kind and ref; returns the ids written."""
    out = []
    for m in items:
        if store.query("SELECT 1 FROM maintenance_items WHERE kind = ? AND ref = ? AND closed_at IS NULL",
                       (m.kind, m.ref)):
            continue
        out.append(store.write_maintenance_item(m))
    return out


def log_hours(store: Store, item_id: int, hours: float) -> float:
    """Add `hours` to an item's ledger (11.5: hours are logged per maintenance item); returns its new total."""
    if not hours > 0:
        raise ValueError(f"hours logged are positive, not {hours!r}")
    rows = store.query("SELECT hours FROM maintenance_items WHERE item_id = ?", (item_id,))
    if not rows:
        raise ValueError(f"unknown maintenance item {item_id}")
    total = float(rows[0]["hours"]) + float(hours)
    store.update("maintenance_items", {"item_id": item_id}, {"hours": total})
    return total


def close_item(store: Store, item_id: int, *, by: str, at: str, hours: float = 0.0) -> None:
    """Close an open item, logging its last `hours` first."""
    rows = store.query("SELECT closed_at FROM maintenance_items WHERE item_id = ?", (item_id,))
    if not rows:
        raise ValueError(f"unknown maintenance item {item_id}")
    if rows[0]["closed_at"] is not None:
        raise ValueError(f"maintenance item {item_id} is already closed")
    if hours:
        log_hours(store, item_id, hours)
    store.update("maintenance_items", {"item_id": item_id}, {"closed_at": utc(at), "closed_by": by})


def flow_of(ref: str, flows: Mapping[str, Any]) -> str | None:
    """The flow an item's `ref` names (choice 6), or None."""
    text = _TRIGGER_PREFIX.sub("", ref, count=1).split("#", 1)[0]
    candidates = [text, text.split("/", 1)[0]]
    try:
        parts = checkpoint_parts(text)
        if parts["kind"] == "flow":
            candidates.insert(0, parts["qualifier"].split("/", 1)[0])
    except TaxonomyError:
        pass
    return next((c for c in candidates if c in flows), None)


def role_of(item: Mapping[str, Any], flows: Mapping[str, FlowFacts],
            budgets: Mapping[str, float] = ROLE_HOURS) -> str | None:
    """The 11.5 role an item's hours count toward (choice 6)."""
    f = flow_of(str(item["ref"]), flows)
    if f is not None:
        return f"tier{flows[f].tier}_flow_owner"
    return str(item["owner"]) if item["owner"] in budgets else None


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _items(store: Store) -> list[Any]:
    return store.query("SELECT item_id, kind, ref, owner, hours, opened_at, closed_at FROM maintenance_items")


def week_hours(store: Store, week: date, *, flows: Mapping[str, FlowFacts],
               budgets: Mapping[str, float] = ROLE_HOURS) -> dict[str, float]:
    """Logged hours per role in the week holding `week` (choice 6); unattributed hours under `None`."""
    start = monday(week)
    out: dict[Any, float] = defaultdict(float)
    for r in _items(store):
        day = _today(r["closed_at"] or r["opened_at"])
        if monday(day) == start:
            out[role_of(r, flows, budgets)] += float(r["hours"])
    return dict(out)


# ---------------------------------------------------------------------------------------------------------------
# the budget-overrun cut (choice 7)

@dataclass(frozen=True)
class RoleWeek:
    role: str
    previous: float
    this: float
    budget: float


@dataclass(frozen=True)
class Overrun:
    """What `budget_overrun` found and decided (11.5 Budget overrun)."""

    roles: tuple[RoleWeek, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    backlog: float = 0.0
    allowance: float = 0.0
    page_system_owner: bool = False
    freeze_new_flows: bool = False
    tell_release_manager: bool = False
    header_lines: tuple[str, ...] = ()        # "tier 3 coverage reduced: maintenance budget, N flows", per tier cut

    def health_rows(self) -> list[tuple[str, str]]:
        if not self.roles:
            return []
        over = "; ".join(f"{r.role} {r.previous:g} h then {r.this:g} h against {r.budget:g} h" for r in self.roles)
        text = f"over budget two weeks running: {over}"
        if self.proposals:
            text += f"; quarantine proposed (maintenance_budget): {', '.join(p.target for p in self.proposals)}"
        if self.freeze_new_flows:
            text += "; tier-1 overrun: system owner paged, new flows frozen, release manager told"
        if self.backlog > self.allowance:
            text += f"; open backlog {self.backlog:g} h still above {self.allowance:g} h"
        return [("Maintenance budget", text)]


def budget_overrun(store: Store, week: date, *, flows: Mapping[str, FlowFacts],
                   rates: Mapping[str, FlowRates] | None = None, budgets: Mapping[str, float] = ROLE_HOURS,
                   quarantined: Iterable[str] = (), today: date | None = None) -> Overrun:
    """The 11.5 budget-overrun decision for the week holding `week` against the week before (module docstring)."""
    this = week_hours(store, week, flows=flows, budgets=budgets)
    prev = week_hours(store, monday(week) - timedelta(days=7), flows=flows, budgets=budgets)
    over = tuple(RoleWeek(r, prev.get(r, 0.0), this.get(r, 0.0), float(b)) for r, b in budgets.items()
                 if this.get(r, 0.0) > b and prev.get(r, 0.0) > b)
    if not over:
        return Overrun()
    tier1 = any(r.role == TIER1_ROLE for r in over)
    cut_roles = {r.role for r in over if r.role != TIER1_ROLE}
    allowance = float(sum(budgets[r] for r in cut_roles))
    backlog_of: dict[str, float] = defaultdict(float)
    backlog = 0.0
    for r in _items(store):
        if r["closed_at"] is None and role_of(r, flows, budgets) in cut_roles:
            backlog += float(r["hours"])
            f = flow_of(str(r["ref"]), flows)
            if f is not None:
                backlog_of[f] += float(r["hours"])
    rates = rates or {}
    done = set(quarantined)
    entered = today or (monday(week) + timedelta(days=6))
    proposals, headers = [], []
    for tier in CUT_TIERS:
        before = len(proposals)
        ranked = sorted((f for f, x in flows.items() if x.tier == tier and f not in done),
                        key=lambda f: (-(rates[f].worst if f in rates else 0.0), f))
        for f in ranked:
            if backlog <= allowance:
                break
            if backlog_of.get(f, 0.0) <= 0:
                continue
            backlog -= backlog_of[f]
            r = rates.get(f, FlowRates(f))
            proposals.append(Proposal("flow", f, "maintenance_budget", entered,
                                      f"tier {tier} flow cut for the maintenance budget: "
                                      f"{', '.join(sorted(cut_roles))} over budget two weeks running; "
                                      f"{r.line()}", ("maintenance_budget",), owner=flows[f].owner, flow_id=f))
        n = len(proposals) - before
        if n:
            headers.append(f"tier {tier} coverage reduced: maintenance budget, {n} flow{'s' if n != 1 else ''}")
    return Overrun(over, tuple(proposals), backlog, allowance, tier1, tier1, tier1, tuple(headers))


# ---------------------------------------------------------------------------------------------------------------
# what the Reporter prints (11.5, 9.11 operational panels; choices 8 and 9)

@dataclass(frozen=True)
class PastSla:
    item_id: int
    kind: str
    ref: str
    owner: str
    opened_at: str
    due_at: str


def items_past_sla(store: Store, now: datetime | str, *, clock: BusinessClock | None = None,
                   days: int = SLA_BUSINESS_DAYS) -> list[PastSla]:
    """Open maintenance items more than `days` business days old (choice 8)."""
    clock = clock or BusinessClock()
    at = datetime.fromisoformat(utc(now) or now) if isinstance(now, str) else now
    out = []
    for r in store.query("SELECT item_id, kind, ref, owner, opened_at FROM maintenance_items WHERE closed_at IS NULL "
                         "ORDER BY opened_at, item_id"):
        due = clock.add_days(datetime.fromisoformat(r["opened_at"]), days)
        if due < at:
            out.append(PastSla(r["item_id"], r["kind"], r["ref"], r["owner"], r["opened_at"],
                               due.isoformat(timespec="seconds")))
    return out


def flows_repaired(store: Store, since: str, until: str, *, flows: Mapping[str, Any]) -> tuple[str, ...]:
    """The flows with a maintenance item closed in [since, until) (choice 9): flows_repaired_per_release."""
    rows = store.query("SELECT ref FROM maintenance_items WHERE closed_at IS NOT NULL AND closed_at >= ? AND "
                       "closed_at < ?", (utc(since), utc(until)))
    return tuple(sorted({f for r in rows if (f := flow_of(str(r["ref"]), flows)) is not None}))


@dataclass(frozen=True)
class MaintenanceMetrics:
    """The Reporter's maintenance figures (11.5): fallback rate per flow, flows repaired in the release and items
    past SLA."""

    rates: Mapping[str, FlowRates]
    repaired: tuple[str, ...]
    past_sla: tuple[PastSla, ...]

    def health_rows(self) -> list[tuple[str, str]]:
        fall = [r for r in self.rates.values() if r.agent_fallback_rate]
        rows = [("Fallback rate per flow",
                 "; ".join(f"{r.flow_id} {100 * (r.agent_fallback_rate or 0):.1f}% ({r.fallbacks} of {r.steps} steps)"
                           for r in sorted(fall, key=lambda r: (-(r.agent_fallback_rate or 0), r.flow_id)))
                 or "no agent fallback in 30 days"),
                ("Flows repaired this release", f"{len(self.repaired)}" + (f": {', '.join(self.repaired)}"
                                                                         if self.repaired else ""))]
        rows.append(("Maintenance items past SLA", f"{len(self.past_sla)}" + (
            ": " + "; ".join(f"{p.kind} {p.ref} ({p.owner}, due {p.due_at[:10]})" for p in self.past_sla)
            if self.past_sla else "")))
        return rows


def reporter_metrics(store: Store, *, now: datetime | str, release_since: str, flows: Mapping[str, FlowFacts],
                     clock: BusinessClock | None = None,
                     repaired: Mapping[str, str | None] | None = None) -> MaintenanceMetrics:
    """The 11.5 figures as of `now`, the release window starting at `release_since`."""
    at = utc(now) if isinstance(now, str) else now.astimezone(timezone.utc).isoformat()
    return MaintenanceMetrics(flow_rates(store, now, flows=flows, repaired=repaired),
                              flows_repaired(store, release_since, at or "", flows=flows),
                              tuple(items_past_sla(store, now, clock=clock)))


__all__ = ["FallbackClusters", "FallbackTicket", "Filed", "FlowFacts", "FlowRates", "MaintenanceMetrics", "Overrun",
           "PastSla", "ROLE_HOURS", "RetryTask", "RoleWeek", "Triggers", "budget_overrun", "close_item",
           "fallback_clusters", "file_triggers", "flow_of", "flow_rates", "flows_repaired", "items_past_sla",
           "last_repaired", "log_hours", "monday", "open_items", "rates", "reporter_metrics", "role_of",
           "step_anchors", "step_ref", "ticket_triggers", "week_hours"]
