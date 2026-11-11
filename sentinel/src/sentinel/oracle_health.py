"""Oracle health across runs (2.8.1 Sources and Extraction confidence, 5.10): the OCR calibration task, the
fallback counts per source and the grid-copy flow task.

- On a tier-1 money cell, the same cell unreadable in three consecutive runs on one build opens an OCR calibration
  task for the flow owner (a larger crop, a per-field whitelist change or a `rule` region re-declaration).
- Every source fallback is a step event `oracle_source_fallback`; run health counts fallbacks per source, and three
  grid-copy fallbacks on one grid in a week open a flow task.

API:

- `fallback_event(fb, run_id=..., shard_id=..., side=..., seq=..., wall_at=..., mono_ms=...)`: the 8.3 `step_events`
  row of one `oracle.sources.SourceFallback` (action `oracle_source_fallback`, `reason` its `event()` text), for
  `Store.write_events`; `parse_fallback(reason)` reads one back.
- `evaluate(store, run_id, money_cells=..., owner_of=...)`: after a run's extractions and step events are stored,
  the maintenance items to open (`records.MaintenanceItem`, for `Store.write_maintenance_item`) and the run-health
  counts (`OracleHealth.health()`).

Readability is judged from the stored extractions (8.3 `oracle_extractions`) exactly as the Oracle judged them:
`oracle.extraction_log.reads` rebuilds each field's read and `oracle.rules.rebind` re-accepts it under the money
floor (85, with the 3x and 2x digit-count check, 2.8.1), so a run whose outcome took a higher precedence than
`oracle_unreadable` still counts.

Choices where the spec is ambiguous (reported to the coordinator):

1. A money cell is a Region Registry region with `money_cell: true` (4.8); the caller passes them as
   (checkpoint id, region id) pairs from the effective registry. The cell is (checkpoint, region, field), tier 1
   from the store's `checkpoints` row. Every money role reads at the money floor (85, as price, integer and percent).
2. "Consecutive runs on one build" are the runs, in start order, that read the cell on that build; a read on the
   candidate side is of the candidate build and on the base side of the base build (a run with no build pair takes
   its fingerprint's build for both). A run is unreadable for the cell when any of its reads of it on that build is
   unreadable; a readable run ends the streak. A task opens when the streak reaching the run under evaluation is at
   least 3 and no task for the cell is open; after a task closes, only runs that started after its closing count.
3. Grid copy is read through the export reader (`copy_all`), so a grid-copy fallback is an `oracle_source_fallback`
   whose from-source is `export`. "One grid" is one checkpoint; "three in a week" are three distinct runs with such
   a fallback on it in the 7 days ending at the run's start (a single run that falls back on several fields counts
   once). The task's ref is the checkpoint's flow (the checkpoint itself for a census or sweep checkpoint).
4. Maintenance item kinds (8.3 `maintenance_items.kind` is open text): `ocr_calibration` for the OCR calibration
   task, ref `<checkpoint id>#<region id>`, and `flow_task` for the grid-copy flow task.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .oracle.extraction_log import reads
from .oracle.rules import rebind
from .oracle.sources import SourceFallback
from .records import Extraction, MaintenanceItem, StepEvent
from .store.store import Store

OCR_STREAK = 3                        # 2.8.1: unreadable in three consecutive runs on one build
GRID_FALLBACKS = 3                    # 2.8.1: three grid-copy fallbacks on one grid ...
GRID_WINDOW = timedelta(days=7)       # ... in a week
GRID_SOURCE = "export"                # the reader grid copy goes through (oracle.sources ExportReader, copy_all)
MONEY_FORMAT = "money"                # the money floor (85) and its digit-count check
OCR_TASK, FLOW_TASK = "ocr_calibration", "flow_task"
ACTION = "oracle_source_fallback"     # the 8.3 step_events action
_EVENT = re.compile(r"^oracle_source_fallback:(?P<field>.+):(?P<src>[a-z]+)->(?P<dst>[a-z]+)$")


@dataclass(frozen=True)
class Fallback:
    """One `oracle_source_fallback` read back from its step event."""

    field: str
    from_source: str
    to_source: str


def fallback_event(fb: SourceFallback, *, run_id: str, shard_id: str, side: str, seq: int, wall_at: str,
                   mono_ms: int, checkpoint_id: str | None = None, flow_id: str | None = None,
                   step_id: str | None = None) -> StepEvent:
    """The `step_events` row of one source fallback (2.8.1): action `oracle_source_fallback`, mode `system`, the
    fallback's `event()` text as `reason`."""
    return StepEvent(run_id=run_id, shard_id=shard_id, side=side, seq=seq, source="orchestrator", wall_at=wall_at,
                     mono_ms=mono_ms, mode="system", action=ACTION, checkpoint_id=checkpoint_id, flow_id=flow_id,
                     step_id=step_id, reason=fb.event())


def parse_fallback(reason: str | None) -> Fallback | None:
    """The fallback a step event's `reason` records, or None when it is not one."""
    m = _EVENT.match(reason or "")
    return None if m is None else Fallback(m["field"], m["src"], m["dst"])


@dataclass(frozen=True)
class OracleHealth:
    """What `evaluate` found for one run: maintenance items to open and the run-health counts."""

    items: tuple[MaintenanceItem, ...]
    fallbacks_by_source: Mapping[str, int] = field(default_factory=dict)
    unreadable_streaks: Mapping[str, int] = field(default_factory=dict)    # cell ref -> streak, this run's cells

    def health(self) -> dict[str, Any]:
        """The run-health block: fallbacks per source (2.8.1), unreadable money-cell streaks and tasks opened."""
        return {"oracle_fallbacks": dict(sorted(self.fallbacks_by_source.items())),
                "money_cell_unreadable_streaks": dict(sorted(self.unreadable_streaks.items())),
                "maintenance_opened": [{"kind": i.kind, "ref": i.ref, "owner": i.owner} for i in self.items]}


@dataclass(frozen=True)
class _RunBuilds:
    run_id: str
    started_at: str
    candidate: str
    base: str


_RUN_SQL = ("SELECT r.run_id, r.started_at, bp.candidate_build_id AS cand, bp.base_build_id AS base, "
            "(SELECT f.build_id FROM env_fingerprints f WHERE f.run_id = r.run_id AND f.side = 'candidate' "
            " ORDER BY f.fingerprint_id LIMIT 1) AS fp_build "
            "FROM runs r LEFT JOIN build_pairs bp ON bp.pair_id = r.pair_id")


def _builds(row: Mapping[str, Any]) -> _RunBuilds:
    cand = row["cand"] or row["fp_build"] or f"run:{row['run_id']}"
    return _RunBuilds(row["run_id"], row["started_at"], cand, row["base"] or cand)


def _extraction(row: Mapping[str, Any]) -> Extraction:
    return Extraction(run_id=row["run_id"], checkpoint_id=row["checkpoint_id"], side=row["side"],
                      assertion_id=row["assertion_id"], field=row["field"], source=row["source"],
                      read_at=row["read_at"], region_id=row["region_id"], text=row["text"], value=row["value"],
                      confidence=row["confidence"], second_pass=bool(row["second_pass"]),
                      crop_sha256=row["crop_sha256"])


def _unreadable(rows: list[Extraction]) -> bool:
    """True when any stored read among `rows` (one run, one cell) is unreadable at the money floor."""
    return any(not rebind(r.extraction, MONEY_FORMAT).readable for by in reads(rows).values() for r in by.values())


def _open_or_closed(store: Store, kind: str, ref: str) -> tuple[bool, str | None]:
    """Whether an item of `kind` for `ref` is open, and when the latest closed one closed."""
    rows = store.query("SELECT closed_at FROM maintenance_items WHERE kind = ? AND ref = ?", (kind, ref))
    closed = [r[0] for r in rows if r[0] is not None]
    return any(r[0] is None for r in rows), max(closed, default=None)


def _ocr_items(store: Store, run: _RunBuilds, money: Collection[tuple[str, str]],
               owner_of: Callable[[str], str]) -> tuple[list[MaintenanceItem], dict[str, int]]:
    tiers = {r[0]: r[1] for r in store.query("SELECT checkpoint_id, tier FROM checkpoints")}
    mine = [x for x in store.extractions(run.run_id)
            if (x.checkpoint_id, x.region_id) in money and tiers.get(x.checkpoint_id) == 1]
    cells: dict[tuple[str, str, str, str], list[Extraction]] = defaultdict(list)
    for x in mine:
        build = run.candidate if x.side == "candidate" else run.base
        cells[(x.checkpoint_id, x.region_id or "", x.field, build)].append(x)
    items: list[MaintenanceItem] = []
    streaks: dict[str, int] = {}
    for (cid, region, fld, build), rows in sorted(cells.items()):
        if not _unreadable(rows):
            continue
        ref = f"{cid}#{region}"
        is_open, closed_at = _open_or_closed(store, OCR_TASK, ref)
        history: dict[str, list[Extraction]] = defaultdict(list)
        started: dict[str, str] = {}
        for r in store.query(
                "SELECT e.*, b.started_at, b.cand, b.base, b.fp_build FROM oracle_extractions e "
                f"JOIN ({_RUN_SQL}) b ON b.run_id = e.run_id WHERE e.checkpoint_id = ? AND e.region_id IS ? "
                "AND e.field = ? AND b.started_at <= ?", (cid, region or None, fld, run.started_at)):
            rb = _builds(r)
            if (rb.candidate if r["side"] == "candidate" else rb.base) != build:
                continue
            if closed_at is not None and rb.started_at <= closed_at:
                continue
            history[rb.run_id].append(_extraction(r))
            started[rb.run_id] = rb.started_at
        streak = 0
        for run_id in sorted(history, key=lambda k: (started[k], k), reverse=True):
            if not _unreadable(history[run_id]):
                break
            streak += 1
        streaks[ref] = streak
        if streak >= OCR_STREAK and not is_open and all(i.ref != ref for i in items):
            items.append(MaintenanceItem(
                OCR_TASK, ref, owner_of(cid), run.started_at, run_id=run.run_id,
                detail=f"money cell {region} ({fld}) unreadable in {streak} consecutive runs on build {build} "
                       "(2.8.1): a larger crop, a per-field whitelist change or a rule region re-declaration"))
    return items, streaks


def _flow_items(store: Store, run: _RunBuilds, owner_of: Callable[[str], str],
                events: list[tuple[str | None, Fallback]]) -> list[MaintenanceItem]:
    flows = {r[0]: r[1] for r in store.query("SELECT checkpoint_id, flow_id FROM checkpoints")}
    since = (datetime.fromisoformat(run.started_at) - GRID_WINDOW).isoformat()
    items: list[MaintenanceItem] = []
    for cid in sorted({c for c, fb in events if c is not None and fb.from_source == GRID_SOURCE}):
        hits = {r["run_id"] for r in store.query(
            "SELECT s.run_id, s.reason FROM step_events s JOIN runs r ON r.run_id = s.run_id WHERE s.action = ? "
            "AND s.checkpoint_id = ? AND r.started_at > ? AND r.started_at <= ?", (ACTION, cid, since, run.started_at))
            if (fb := parse_fallback(r["reason"])) is not None and fb.from_source == GRID_SOURCE}
        ref = flows.get(cid) or cid
        if len(hits) < GRID_FALLBACKS or _open_or_closed(store, FLOW_TASK, ref)[0] or any(i.ref == ref for i in items):
            continue
        items.append(MaintenanceItem(
            FLOW_TASK, ref, owner_of(cid), run.started_at, run_id=run.run_id,
            detail=f"grid copy fell back to another source on {cid} in {len(hits)} runs within 7 days (2.8.1)"))
    return items


def evaluate(store: Store, run_id: str, *, money_cells: Collection[tuple[str, str]],
             owner_of: Callable[[str], str]) -> OracleHealth:
    """The run's Oracle health (module docstring): OCR calibration tasks for tier-1 money cells unreadable in three
    consecutive runs on one build, flow tasks for grid copy falling back three times on one grid in a week, and the
    fallback counts per source. `money_cells` are (checkpoint id, region id) pairs; `owner_of(checkpoint_id)` names
    the flow owner. Reads only; the caller writes the items (`Store.write_maintenance_item`)."""
    row = next(iter(store.query(_RUN_SQL + " WHERE r.run_id = ?", (run_id,))), None)
    if row is None:
        raise ValueError(f"unknown run {run_id}")
    run = _builds(row)
    events = [(r["checkpoint_id"], fb) for r in store.query(
        "SELECT checkpoint_id, reason FROM step_events WHERE run_id = ? AND action = ? ORDER BY shard_id, seq",
        (run_id, ACTION)) if (fb := parse_fallback(r["reason"])) is not None]
    money = frozenset(money_cells)
    ocr, streaks = _ocr_items(store, run, money, owner_of)
    flow = _flow_items(store, run, owner_of, events)
    return OracleHealth(tuple(ocr + flow), dict(Counter(fb.from_source for _, fb in events)), streaks)
