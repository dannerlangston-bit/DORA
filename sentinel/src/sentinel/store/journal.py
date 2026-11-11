"""Ingest of the rig's two journals into `step_events` (8.2, 8.3, 8.4, 10.2).

Neither the Gateway hook host nor the Watchdog opens the Run Store (8.2): the Orchestrator's drop-log drainer
journals every dropped and refused gateway row to `gateway_drops.jsonl` (`gateway.drainer`), and the Watchdog
appends one JSON line per reaction to `watchdog_events.jsonl` (10.2 reaction step 5). On resume the Orchestrator
ingests both into `step_events`, and the shard import does the same for a shard shipped "as it is" after the
restart budget was spent (8.2). `ingest_journal(store, shard_id, path, source)` does it for one journal; it is
idempotent, so a journal ingested on resume and again at import adds nothing the second time.

- `source="gateway_hook"`: the drop journal holds whole `StepEvent` rows with the seq the shard-wide allocator gave
  them (drainer choice 7), read by `gateway.drainer.ingest_journal`; they are written through
  `Store.write_events`, keyed (shard_id, seq). Their own `source` stays as the drainer wrote it: `gateway_hook`
  for the hook's ring records, `orchestrator` for the slow half's stage 4 refusals.
- `source="watchdog"`: the Watchdog has no seq; each line is keyed by its `event_id` and takes the shard's next seq
  (`Store.write_keyed_events`). Rows are `source watchdog`, `mode system`.

Watchdog journal line (choice; 10.2 says only "a JSON line"; the Watchdog writer follows it):

    {"event_id": "wd-0003", "action": "watchdog_hang", "at": "2026-11-09T19:03:21Z", "mono_ms": 184233118,
     "side": "candidate", "flow_id": "...", "step_id": "s07", "checkpoint_id": "...",
     "detail": "cause: hang", "app_uptime_s": 1820, "app_restart_count": 1}

`action` is one of `WATCHDOG_ACTIONS` (the 8.3 actions of the 10.2 reaction: hang and crash, the kill switch's
second half, `reset_input_state`, the relaunch and the Orchestrator's resume); `event_id`, `action`, `at` and
`mono_ms` are required. The stored `reason` is `<event_id>` or `<event_id>: <detail>` (8.3: "Watchdog event id").
A torn last line (the writer died mid-append) is skipped; a torn line elsewhere raises.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..gateway import drainer
from ..records import StepEvent
from .store import Store, StoreRefused

GATEWAY_JOURNAL = drainer.JOURNAL_NAME            # gateway_drops.jsonl (8.4)
WATCHDOG_JOURNAL = "watchdog_events.jsonl"        # 8.4, 10.2
JOURNALS = {"gateway_hook": GATEWAY_JOURNAL, "watchdog": WATCHDOG_JOURNAL}
WATCHDOG_ACTIONS = ("watchdog_hang", "watchdog_crash", "kill_switch", "reset_input_state", "relaunch", "resume")
_REQUIRED = ("event_id", "action", "at", "mono_ms")


@dataclass(frozen=True)
class IngestResult:
    """What one ingest did: the journal's events and how many of them were new to the store."""
    source: str
    read: int
    written: int


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            if i == len(lines) - 1 and not text.endswith("\n"):
                break
            raise StoreRefused(f"{path}: line {i + 1} is torn ({e}); only the last line may be") from e
    return out


def watchdog_event(entry: dict[str, Any], *, run_id: str, shard_id: str) -> StepEvent:
    """One Watchdog journal line as its `step_events` row (module docstring); the seq is assigned at write."""
    missing = [k for k in _REQUIRED if entry.get(k) in (None, "")]
    if missing:
        raise StoreRefused(f"a Watchdog journal line lacks {', '.join(missing)} (10.2): {entry}")
    if entry["action"] not in WATCHDOG_ACTIONS:
        raise StoreRefused(f"Watchdog action {entry['action']!r}; one of {WATCHDOG_ACTIONS} (8.3, 10.2)")
    detail = entry.get("detail")
    return StepEvent(run_id=run_id, shard_id=shard_id, side=entry.get("side") or "candidate", seq=1, source="watchdog",
                     wall_at=entry["at"], mono_ms=int(entry["mono_ms"]), mode="system", action=entry["action"],
                     flow_id=entry.get("flow_id"), step_id=entry.get("step_id"),
                     checkpoint_id=entry.get("checkpoint_id"),
                     reason=f"{entry['event_id']}: {detail}" if detail else str(entry["event_id"]),
                     app_uptime_s=entry.get("app_uptime_s"), app_restart_count=entry.get("app_restart_count"))


def read_journal(path: str | os.PathLike, source: str, *, run_id: str, shard_id: str) -> list[StepEvent]:
    """The `step_events` rows of one journal of `shard_id` (module docstring); a gateway row of another shard or
    run is refused."""
    p = Path(path)
    if source == "gateway_hook":
        events = drainer.ingest_journal(p)
        foreign = [e for e in events if (e.run_id, e.shard_id) != (run_id, shard_id)]
        if foreign:
            raise StoreRefused(f"{p}: a row of {foreign[0].shard_id} in the journal of {shard_id} (8.4)")
        return events
    if source == "watchdog":
        return [watchdog_event(entry, run_id=run_id, shard_id=shard_id) for entry in _lines(p)]
    raise StoreRefused(f"journal source {source!r}; one of {tuple(JOURNALS)} (8.3 step_events.source)")


def ingest_journal(store: Store, shard_id: str, path: str | os.PathLike, source: str) -> IngestResult:
    """Ingest one journal of `shard_id` into `store` (module docstring); idempotent. The shard must be in the
    store and its run open."""
    row = store.query("SELECT run_id FROM shards WHERE shard_id = ?", (shard_id,))
    if not row:
        raise StoreRefused(f"unknown shard {shard_id} (8.3)")
    events = read_journal(path, source, run_id=row[0]["run_id"], shard_id=shard_id)
    if source == "watchdog":
        return IngestResult(source, len(events), store.write_keyed_events(events))
    have = {r[0] for r in store.query("SELECT seq FROM step_events WHERE shard_id = ?", (shard_id,))}
    store.write_events(events)
    return IngestResult(source, len(events), sum(1 for e in events if e.seq not in have))
