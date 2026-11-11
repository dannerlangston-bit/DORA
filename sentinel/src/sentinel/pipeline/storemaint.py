"""The Nightly run's store maintenance on the simulated control host (8.6, 8.8, 8.9, 9.10).

`nightly_maintenance(store, out, now, run_id=...)` runs, in order:

1. the backup (`store.backup.run_backup`): `VACUUM INTO` a staging snapshot, the CSV exports, the pinned objects
   mirrored, `last_backup_at` (or `last_backup_error`) written; the weekly `PRAGMA integrity_check` when due;
2. the retention sweep (`store.retention.sweep` over `select_candidates`), which does nothing when tonight's backup
   failed, then the 90-day prune of step events (`prune_step_events`);
3. the weekly artifact audit (`store.audit.run_audit`): every pinned copy and a 5 percent sample re-hashed, a missing
   or corrupt one restored from the mirror and recorded as an `artifact_missing` finding;
4. the store state check (`store.health.store_state_check`): backup age, restore-drill age, the integrity result and
   the store volume's free space.

Each step's run-health rows go to the Nightly summary (`health_rows`, and `health["store"]` for the morning lines);
a failed backup and a failed state check each give the 9.10 Store health alert (`store.health.store_health`, P2 to
the on-call channel and the system owner) as an alert entry the run's alerts collapse and dispatch
(`integrations.after_run`).

Choices (reported):

1. Where: the backup root is `<out>/backup`, the artifact root `<out>/artifacts` (the run directory's 8.4 tree); the
   simulated control host's store volume is `SIM_VOLUME` (2 TiB, a tenth used), since the container's own disk says
   nothing about a control host's (8.8: the 100 GiB floor would fail on any small test disk).
2. When: the audit is weekly (8.6): it runs on the Nightly when `last_artifact_audit_at` is unset or at least 7 days
   old; its sample is seeded with the ISO year and week, as `sentinel audit` seeds it. The backup runs every Nightly
   (its own 02:00 timer is the Reporter service's; the simulated Nightly stands for the night).
3. The state check runs after the backup, so a backup that just succeeded is not reported as overdue.
"""
from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..orchestrator.alerts import AlertRoute
from ..store import Store
from ..store.audit import run_audit
from ..store.backup import run_backup
from ..store.health import store_state_check
from ..store.retention import prune_step_events, select_candidates, sweep

TIB = 1024 ** 4
SIM_VOLUME = shutil._ntuple_diskusage(2 * TIB, 2 * TIB // 10, 2 * TIB - 2 * TIB // 10)   # choice 1
AUDIT_EVERY = timedelta(days=7)                                                            # 8.6, choice 2


@dataclass
class StoreMaintenance:
    """What the night's store maintenance did: run-health rows, the morning lines and the alert entries."""

    rows: list[list[str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    backup_ok: bool = False
    deleted: int = 0
    audited: int | None = None


def _alert(route: AlertRoute, run_id: str, detail: str, control: str) -> dict[str, Any]:
    """An alert entry in the `orchestration.alert_entry` shape, for a result no gate returned."""
    return {"run_id": run_id, "shard_id": None, "rule": route.rule, "priority": route.priority,
            "paged": list(route.paged), "notified": list(route.notified), "safety_table": route.is_safety,
            "control": control, "gate": None, "incident": None, "run_outcome": None, "run_reason": None,
            "detail": detail}


def audit_due(store: Store, now: datetime) -> bool:
    """Choice 2: no audit yet, or the last one at least 7 days before `now`."""
    last = store.get_system_state("last_artifact_audit_at")
    return not last or datetime.fromisoformat(str(last).replace("Z", "+00:00")) <= now - AUDIT_EVERY


def nightly_maintenance(store: Store, out: Path, now: datetime, *, run_id: str) -> StoreMaintenance:
    """Backup, retention, the weekly audit and the store state check (module docstring)."""
    at = now.isoformat()
    m = StoreMaintenance()
    art_root, backup_root = Path(out) / "artifacts", Path(out) / "backup"
    res = run_backup(store, backup_root, at, artifact_root=art_root)
    m.backup_ok = res.ok
    if res.ok:
        m.lines.append(f"Backup ok at {res.at}: {sum(res.exports.values())} rows exported, {res.copied} objects "
                       f"mirrored" + ("; integrity_check passed" if res.integrity_checked else ""))
    else:
        m.lines.append(f"Backup failed: {res.error}")
        if res.alert is not None:
            m.alerts.append(_alert(res.alert, run_id, f"backup failed: {res.error}", "store_backup"))
    swept = sweep(store, art_root, select_candidates(store, at), backup_ok=res.ok, now=at)
    pruned = prune_step_events(store, at)
    m.deleted = len(swept.deleted)
    m.rows += [list(r) for r in swept.health_rows()]
    m.lines.append("Retention skipped: backup failed" if swept.skipped else
                   f"Retention swept {len(swept.deleted)} expired copies; kept {len(swept.kept)} pinned or "
                   f"referenced; {pruned} step events past 90 days pruned")
    if audit_due(store, now):
        year, week, _ = now.isocalendar()
        audit = run_audit(store, art_root, backup_root, random.Random(year * 100 + week), at)
        m.audited = audit.pinned + audit.sampled
        m.rows += [list(r) for r in audit.health_rows()]
        m.lines.append(f"Artifact audit sample: {audit.pinned + audit.sampled} copies re-hashed ({audit.pinned} "
                       f"pinned, {audit.sampled} sampled), {len(audit.failures)} missing or corrupt")
    check = store_state_check(store, at, SIM_VOLUME)
    m.rows += check.summary_rows()
    if check.alert is not None:
        m.alerts.append(_alert(check.alert, run_id, check.detail, "store_health"))
        m.lines.append(f"Store health: {check.detail}")
    return m
