"""Store health: the Nightly run's state check and the restore drill record (8.6, 8.8, 8.9, 9.10 Store health).

- `store_state_check(store, now, disk, projected_run_bytes=)` is the Nightly run's second line of defence (8.6,
  8.9): it reads `system_state` (`last_backup_at`, `last_backup_error`, `last_restore_drill_at`) and the
  control host's store volume, and returns the red Run health rows and, when there is any, the 9.10 Store health
  alert (`store_health()`, P2). A backup job failure, a backup older than 36 hours, an integrity check failure on
  the weekly snapshot, a restore drill older than 100 days, the volume at or above the 70 percent alarm of 8.8 and
  free space below the control host's preflight threshold (the larger of 100 GB and twice the projected run size,
  8.6) each give a row. Everything fresh gives no row and no alert.
- `record_restore_drill(store, by, compared=, matched=)` records the quarterly drill (8.6): after the system
  owner restored the latest snapshot and `baselines\\` to a scratch host and re-diffed at least ten checkpoints of
  one RC run with every outcome matching the original rows, `last_restore_drill_at` is set; a drill with fewer
  checkpoints or any mismatch is refused and nothing is written.
- `main(argv)` is `sentinel store restore-drill --db PATH --by NAME --compared N --matched N [--at TS]` when no service
  holds the Central Store's writer; with it held (8.2) nothing is written and it exits 4, and the service records
  the drill through its writer (`record_restore_drill` as one unit of writer work).

Choices where the spec is ambiguous (reported to the coordinator):
- `last_backup_error` is current while it was written at or after the last successful backup (`updated_at`
  against `last_backup_at`); the key is never deleted, so the last error stays readable after a good night.
- An integrity check failure on the weekly snapshot is recorded by the backup job as its error, prefixed
  `INTEGRITY_PREFIX` (8.3 has no key of its own for it): the backup is not good, so the alert fires, the retention
  sweep is skipped and this check prints it as its own "Integrity check" row.
- A key never written counts as infinitely old: no backup or no restore drill on record is a red row.
- 9.10 lists the restore drill among the Store health alert's triggers, 8.9 only gives the red row; the alert is
  raised for it too. The 06:00 after-hours posting of 11.6 belongs to the alert sender, not to this route.
- Recipients: the on-call channel and the system owner (8.9 "Who sees it"); P2 posts and pages nobody (9.10).
- Sizes are binary gigabytes (1024**3 bytes), as the rig's R7 floor in `orchestrator.boundary`.
- The drill's `by` is a person (the system owner owns it, 8.6); service identities are refused. Who ran it goes
  into the writer batch's results.jsonl entry, since `system_state` holds a value and a time only.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from ..orchestrator import alerts
from ..orchestrator.alerts import AlertRoute
from ..records import now as utc_now
from .retention import ts
from .store import SERVICE_IDENTITIES, Store, StoreError, StoreRefused, utc

BACKUP_MAX_AGE = timedelta(hours=36)
RESTORE_DRILL_MAX_AGE = timedelta(days=100)
DISK_ALARM_SHARE = 0.70                       # 8.8: the alarm at 70 percent of the control-host volume
CONTROL_FLOOR_BYTES = 100 * 1024 ** 3         # 8.6 storage preflight: the larger of 100 GB and twice the run size
DRILL_CHECKPOINTS = 10                        # 8.6: re-diff ten checkpoints of one RC run
INTEGRITY_PREFIX = "integrity_check: "
EXIT_NOT_OPENED = 4                           # backup, audit, restore-drill: the store would not open for writing
NOT_OPENED_HINT = ("nothing was written. While the Reporter service runs it is the Central Store's only writer "
                   "(8.2): the service runs this job through its writer")


def store_health() -> AlertRoute:
    """The 9.10 Store health row: P2 to the on-call channel, the system owner told (8.9; `alerts.store_health`)."""
    return alerts.store_health()


class DiskUsage(Protocol):
    """What `shutil.disk_usage` returns for the control host's store volume."""
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class StateValue:
    """One `system_state` row."""
    value: str
    updated_at: str


def state(store: Store, key: str) -> StateValue | None:
    """The `system_state` row of `key`, or None when it was never written."""
    rows = store.query("SELECT value, updated_at FROM system_state WHERE key = ?", (key,))
    return StateValue(rows[0]["value"], rows[0]["updated_at"]) if rows else None


def backup_error(store: Store) -> str | None:
    """The last backup error while it is current: written at or after the last successful backup."""
    err, ok = state(store, "last_backup_error"), state(store, "last_backup_at")
    if err is None or (ok is not None and ts(ok.updated_at) > ts(err.updated_at)):
        return None
    return err.value


@dataclass(frozen=True)
class HealthRow:
    """A red Run health row of the store state check."""
    label: str
    text: str

    def summary_row(self) -> list[str]:
        """The row as `runs.summary_json` `health_rows` holds it ([label, text], `report.model`)."""
        return [self.label, f"red: {self.text}"]


@dataclass(frozen=True)
class StateCheck:
    """The red rows and the Store health alert (None when every row is fresh)."""
    rows: tuple[HealthRow, ...] = ()
    alert: AlertRoute | None = None

    def summary_rows(self) -> list[list[str]]:
        return [r.summary_row() for r in self.rows]

    @property
    def detail(self) -> str:
        """The alert's one line of detail (9.10 message format)."""
        return "; ".join(f"{r.label}: {r.text}" for r in self.rows)


def _gib(n: float) -> str:
    return f"{n / 1024 ** 3:.0f} GB"


def _age_row(store: Store, key: str, at, limit: timedelta, label: str, what: str) -> HealthRow | None:
    v = state(store, key)
    if v is None:
        return HealthRow(label, f"no {what} recorded")
    age = at - ts(v.value)
    if age <= limit:
        return None
    hours = age.total_seconds() / 3600
    span = f"{hours:.0f} h" if limit < timedelta(days=2) else f"{age.days} days"
    lim = f"{limit.total_seconds() / 3600:.0f} h" if limit < timedelta(days=2) else f"{limit.days} days"
    return HealthRow(label, f"last {what} at {v.value}, {span} ago (limit {lim})")


def store_state_check(store: Store, now: str, disk: DiskUsage | None = None, *,
                      projected_run_bytes: int = 0) -> StateCheck:
    """The Nightly run's store state check (8.6, 8.8, 8.9, 9.10); `disk` is the store volume's usage."""
    at = ts(now)
    rows: list[HealthRow] = []
    err = backup_error(store)
    if err is not None and err.startswith(INTEGRITY_PREFIX):
        rows.append(HealthRow("Integrity check", f"weekly snapshot failed PRAGMA integrity_check: "
                                                 f"{err[len(INTEGRITY_PREFIX):]}; stop the service and restore (8.9)"))
    elif err is not None:
        rows.append(HealthRow("Backup", f"backup job failed: {err}"))
    for row in (_age_row(store, "last_backup_at", at, BACKUP_MAX_AGE, "Backup age", "backup"),
                _age_row(store, "last_restore_drill_at", at, RESTORE_DRILL_MAX_AGE, "Restore drill", "restore drill")):
        if row is not None:
            rows.append(row)
    if disk is not None and disk.total > 0:
        share = disk.used / disk.total
        if share >= DISK_ALARM_SHARE:
            rows.append(HealthRow("Disk", f"store volume {share:.0%} used (alarm at {DISK_ALARM_SHARE:.0%})"))
        floor = max(CONTROL_FLOOR_BYTES, 2 * projected_run_bytes)
        if disk.free < floor:
            rows.append(HealthRow("Disk free", f"{_gib(disk.free)} free, below the preflight threshold "
                                               f"{_gib(floor)}"))
    return StateCheck(tuple(rows), store_health() if rows else None)


def record_restore_drill(store: Store, by: str, *, compared: int, matched: int, at: str | None = None) -> str:
    """Record a passing restore drill (8.6): `compared` checkpoints of one RC run re-diffed against the restored
    baselines, `matched` of them equal to the original rows. Returns the recorded time; StoreRefused (nothing
    written) for a service identity, fewer than ten checkpoints or any mismatch."""
    if not isinstance(by, str) or by.strip().lower() in SERVICE_IDENTITIES:
        raise StoreRefused(f"a restore drill is recorded by a person, not {by!r} (8.6)")
    if compared < DRILL_CHECKPOINTS:
        raise StoreRefused(f"a restore drill re-diffs at least {DRILL_CHECKPOINTS} checkpoints, not {compared} (8.6)")
    if matched != compared:
        raise StoreRefused(f"{compared - matched} of {compared} re-diffed outcomes differ from the original rows: "
                           "the drill failed and is not recorded (8.6)")
    when = utc(at) or utc_now()
    with store.batch("restore_drill") as b:
        b.insert("system_state", {"key": "last_restore_drill_at", "value": when, "updated_at": when},
                 upsert="(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
        b.rows.append({"op": "restore_drill", "by": by, "compared": compared, "matched": matched, "at": when})
    return when


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel store restore-drill", description="Record a passing restore drill (8.6).")
    p.add_argument("--db", required=True, help="the Central Store database")
    p.add_argument("--by", required=True, help="the person who ran the drill (the system owner)")
    p.add_argument("--compared", type=int, required=True, help="checkpoints of one RC run re-diffed")
    p.add_argument("--matched", type=int, required=True, help="re-diffed outcomes equal to the original rows")
    p.add_argument("--at", help="when the drill ended (default now)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when recorded, 1 when refused (reason on stderr), 2 on a usage error or a missing database, 4 when the
    Central Store would not open for writing (the Reporter service holds writer.lock, 8.2: the service records the
    drill through its writer, `record_restore_drill` as one unit of writer work); nothing is written then."""
    a = _parser().parse_args(argv)
    if not Path(a.db).is_file():
        print(f"sentinel store restore-drill: no database at {a.db}", file=sys.stderr)
        return 2
    try:
        store = Store(a.db)
    except StoreError as e:
        print(f"sentinel store restore-drill: not recorded: {e}; {NOT_OPENED_HINT}", file=sys.stderr)
        return EXIT_NOT_OPENED
    try:
        with store:
            when = record_restore_drill(store, a.by, compared=a.compared, matched=a.matched, at=a.at)
    except StoreRefused as e:
        print(f"sentinel store restore-drill: refused: {e}", file=sys.stderr)
        return 1
    print(f"restore drill recorded at {when} by {a.by}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
