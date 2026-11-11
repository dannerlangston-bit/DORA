"""The nightly backup of the Central Store (8.1, 8.6 Retention and backup, 8.9).

`run_backup(store, backup_root, now, artifact_root=)` is the 02:00 America/New_York job on the control host:

1. `refresh_pins` (`store.retention`) brings `artifacts.pinned` up to date and `ensure_set_rows` records first-set
   copies, so the mirror below takes every pinned object.
2. `VACUUM INTO` a staging file beside the database (`vacuum_into`) gives a consistent snapshot while the service
   keeps writing (WAL readers never block the writer, 8.2).
3. When the last integrity check is a week old or was never recorded, `weekly_integrity` runs `PRAGMA
   integrity_check` on that snapshot; a failure makes the night a failed backup (8.9: a service stop and restore,
   not a warning: `BackupResult.stop_service`).
4. The snapshot's decisions, baselines, masks, flake statistics, incidents and quarantine records go out as JSONL,
   one canonical-JSON line per row (`EXPORT_TABLES`), so a lost database is rebuilt by replay (8.1).
5. The snapshot, the exports, all of `baselines/` and every pinned object under `runs/` (the pinned copies as the
   snapshot lists them) are mirrored to the backup location, each file copied to a temporary name and renamed into
   place (robocopy on Windows, `shutil` here: the same contract, a complete copy or none).
6. Success writes `last_backup_at` (and `last_integrity_check_at` when it ran) and prunes the backup location to 7
   daily and 8 weekly snapshots with their exports (`keep_snapshots`). Any failure writes `last_backup_error` and
   returns the 9.10 Store health alert (`store.health.store_health`, P2); nothing is pruned.

The Central Store has one writer (8.2), and while the Reporter service runs it holds writer.lock: the service runs
the job with its writer (`run_backup(writer, ...)`, `writer_call`), which the job holds only for step 1 and for the
result of step 6, so imports and actions keep writing while it copies (8.6: "while the service runs").

`backup_ok(store, now)` answers the retention sweep's question "did tonight's backup succeed" from `system_state`
(8.6: the sweep is skipped on a night whose backup failed). `vacuum_into(path, dest)` is also the pre-migration
snapshot 8.6 requires before any schema migration. `main(argv)` is
`sentinel backup --db PATH --backup-root DIR --artifact-root DIR [--now TS]` for the scheduler when no service
holds the writer; with the writer held it writes nothing and exits 4 (`EXIT_NOT_OPENED`), a failed night.

Backup location layout (8.1 Backup location):

    <backup_root>/snapshots/central-<YYYYMMDDTHHMMSSZ>.db
    <backup_root>/exports/<YYYYMMDDTHHMMSSZ>/<table>.jsonl
    <backup_root>/baselines/...                      the artifact root's baselines/, file for file
    <backup_root>/runs/<run_id>/objects/<aa>/<sha256>.<ext>   pinned objects (8.4 layout)

Choices where the spec is ambiguous (reported to the coordinator):
- The snapshot is written locally, then copied: VACUUM INTO writes a SQLite database, and 8.2 forbids opening a
  database file over a share. The staging copy is removed once mirrored; a failed night leaves it for diagnosis
  and the next night clears the staging folder first.
- "Weekly" integrity check: on the first backup at least 7 days after the last recorded check (or with none), so
  no separate scheduler is needed; the result is recorded with the backup. A failure is written as
  `last_backup_error` with `store.health.INTEGRITY_PREFIX` and the failing snapshot is not mirrored.
- Daily and weekly are America/New_York calendar days and ISO weeks: the 7 newest days keep their newest snapshot,
  then the 8 newest older ISO weeks keep theirs (8.8: 15 snapshots). A file whose name does not parse is left alone.
- "Quarantine records" are `safety_quarantines` and the quarantine.yaml mirror inside `flake_stats`
  (`quarantine.mirror`), so both tables are exported; BLOBs are exported as lowercase hex.
- A pinned object missing from the artifact tree, or whose file does not hash to its name, fails the night, unless
  the weekly audit already recorded it (`audit_state` not `ok`, an `artifact_missing` finding exists): those are
  skipped and listed in `known_missing`. A failing object never stops the mirror: every other pinned object is
  copied first, then the night fails.
- A mirrored file that already exists with the same size is not copied again (content-addressed names; the audit
  verifies mirror copies before restoring from them).
- `backup_ok` ("did tonight's backup succeed") needs a success in tonight's window: at or after the most recent
  scheduled 02:00 America/New_York before the sweep, less 30 minutes for a scheduler that starts early, and no
  error since. Yesterday's success does not cover a night whose job died before recording anything; the 36-hour
  limit stays the Nightly state check's (`store.health`), the second line of defence.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar
from zoneinfo import ZoneInfo

from ..canonical import canonical_json
from ..orchestrator.alerts import AlertRoute
from ..records import now as utc_now
from .health import EXIT_NOT_OPENED, INTEGRITY_PREFIX, NOT_OPENED_HINT, backup_error, state, store_health
from .retention import ensure_set_rows, live_copies, object_file, refresh_pins, ts
from .store import Store, StoreError, utc

EXPORT_TABLES = ("decisions", "baselines", "masks", "flake_stats", "incidents", "safety_quarantines")
KEEP_DAILY = 7
KEEP_WEEKLY = 8
INTEGRITY_EVERY = timedelta(days=7)
NEW_YORK = ZoneInfo("America/New_York")
BACKUP_HOUR = 2                                   # 8.6: the job runs at 02:00 America/New_York
BACKUP_SLACK = timedelta(minutes=30)              # a scheduler starting the job a little early still counts
SNAPSHOT_PREFIX = "central-"
_STAMP = re.compile(r"^(\d{8}T\d{6}Z)$")
_CHUNK = 1 << 20
T = TypeVar("T")


class StoreWriter(Protocol):
    """The Central Store's single writer as the Reporter service holds it (8.2, `service.app.Writer`): runs
    `fn(store)` on the writer thread and returns its result or raises."""

    def submit(self, fn: Callable[[Store], T]) -> T:
        """Run one unit of writer work."""


def writer_call(store: Store | StoreWriter) -> Callable[[Callable[[Store], Any]], Any]:
    """How a maintenance job runs one short unit of Central Store work (8.2): directly on a `Store` this process
    writes (no service running; tests), else through the service's single writer, a `store.writer.WriterService`
    (`call`) or the Reporter service's `Writer` (`submit`). The backup and the audit hold the writer only for
    such units and do their long file work on the caller's thread, so the service keeps writing meanwhile."""
    if isinstance(store, Store):
        return lambda fn: fn(store)
    call = getattr(store, "call", None)
    return call if call is not None else store.submit


class BackupError(StoreError):
    """A backup step failed (8.6); the message names the step."""


@dataclass(frozen=True)
class IntegrityResult:
    """`PRAGMA integrity_check` on a snapshot: ok, or the lines it reported."""
    ok: bool
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackupResult:
    """What one backup night did."""
    ok: bool
    at: str
    stamp: str
    snapshot: Path | None = None
    exports: dict[str, int] = field(default_factory=dict)
    copied: int = 0
    known_missing: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
    integrity_checked: bool = False
    error: str | None = None
    stop_service: bool = False
    alert: AlertRoute | None = None


def stamp_of(at: str) -> str:
    """The file stamp of a backup time: UTC `YYYYMMDDTHHMMSSZ`."""
    return ts(at).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_stamp(stamp: str) -> datetime | None:
    if not _STAMP.match(stamp):
        return None
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)


def vacuum_into(path: str | os.PathLike, dest: str | os.PathLike) -> Path:
    """`VACUUM INTO dest` from a read-only connection to `path`: a consistent, compact copy taken while the writer
    runs (8.6). `dest` is replaced if it exists."""
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = _ro(Path(path))
    try:
        conn.execute("VACUUM INTO ?", (str(out),))
    finally:
        conn.close()
    return out


def weekly_integrity(path: str | os.PathLike) -> IntegrityResult:
    """`PRAGMA integrity_check` on a snapshot file (8.6); a file SQLite cannot open is a failure."""
    try:
        conn = _ro(Path(path))
        try:
            lines = tuple(str(r[0]) for r in conn.execute("PRAGMA integrity_check"))
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return IntegrityResult(False, (str(e),))
    return IntegrityResult(lines == ("ok",), () if lines == ("ok",) else lines)


def _jsonable(v):
    return v.hex() if isinstance(v, bytes) else v


def export_tables(snapshot: str | os.PathLike, directory: str | os.PathLike) -> dict[str, int]:
    """Write `<table>.jsonl` for every table of `EXPORT_TABLES` from the snapshot: one canonical-JSON object per
    row in rowid order (8.1, 8.6). Returns the row count per table."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    conn = _ro(Path(snapshot))
    try:
        for table in EXPORT_TABLES:
            cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            cols = [d[0] for d in cur.description]
            n = 0
            with open(out / f"{table}.jsonl", "w", encoding="utf-8", newline="\n") as f:
                for row in cur:
                    f.write(canonical_json({c: _jsonable(v) for c, v in zip(cols, row)}) + "\n")
                    n += 1
            counts[table] = n
    finally:
        conn.close()
    return counts


def copy_file(src: Path, dst: Path, *, verify: str | None = None) -> bool:
    """Copy `src` to `dst` through a temporary name (a complete file or none); False when `dst` already exists with
    the same size. `verify` is the sha256 the copy must hash to."""
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    shutil.copyfile(src, tmp)
    if verify is not None and sha256_file(tmp) != verify:
        tmp.unlink()
        raise BackupError(f"copy of {src} to {dst} does not hash to {verify}")
    os.replace(tmp, dst)
    return True


def mirror_tree(src: Path, dst: Path) -> int:
    """Copy every file under `src` to the same place under `dst` (robocopy without purge: the mirror keeps what
    the source no longer has, 8.8). Returns the number of files copied."""
    if not src.is_dir():
        return 0
    return sum(copy_file(p, dst / p.relative_to(src)) for p in sorted(src.rglob("*"))
               if p.is_file() and not p.name.endswith(".tmp"))


def mirror_pinned(store: Store, artifact_root: Path, backup_root: Path) -> tuple[int, tuple[str, ...]]:
    """Copy every pinned object of a live, unquarantined `runs/` copy to `<backup_root>/<set>/objects/...` (8.6).
    A copy that is missing or does not hash to its name is skipped and listed when the audit already recorded it
    (`audit_state` not `ok`); any other such copy is a BackupError, raised only once every other pinned copy has
    been mirrored, so one bad object never keeps the rest out of the backup. Returns (files copied, the
    `<set>/<sha256>` the audit already recorded missing or corrupt)."""
    copied, known, errors = 0, [], []
    for c in live_copies(store):
        if not (c.pinned and c.set_path.startswith("runs/")) or c.quarantined:
            continue
        src = object_file(artifact_root / c.set_path, c.sha256)
        try:
            if src is None:
                raise BackupError(f"pinned object {c.sha256} of {c.set_path} is not in the artifact tree")
            copied += copy_file(src, backup_root / src.relative_to(artifact_root), verify=c.sha256)
        except BackupError as e:
            if c.audit_state != "ok":
                known.append(f"{c.set_path}/{c.sha256}")
            else:
                errors.append(str(e))
    if errors:
        more = f" (and {len(errors) - 1} more pinned objects)" if len(errors) > 1 else ""
        raise BackupError(errors[0] + more)
    return copied, tuple(known)


def keep_snapshots(stamps: list[str]) -> set[str]:
    """The stamps 8.6 keeps: the newest of each of the 7 newest America/New_York days, then the newest of each of
    the 8 newest older ISO weeks."""
    dated = sorted(((d, s) for s in stamps if (d := _parse_stamp(s)) is not None), reverse=True)
    keep, days, weeks = set(), [], []
    for d, s in dated:
        local = d.astimezone(NEW_YORK)
        day = local.date()
        if len(days) < KEEP_DAILY and day not in days:
            days.append(day)
            keep.add(s)
        elif len(days) >= KEEP_DAILY and day < days[-1]:
            week = local.isocalendar()[:2]
            if len(weeks) < KEEP_WEEKLY and week not in weeks:
                weeks.append(week)
                keep.add(s)
    return keep


def prune_backups(backup_root: Path) -> tuple[str, ...]:
    """Remove snapshots and export folders whose stamp `keep_snapshots` does not keep; returns the stamps removed."""
    snaps = {p.name[len(SNAPSHOT_PREFIX):-3]: p for p in (backup_root / "snapshots").glob(f"{SNAPSHOT_PREFIX}*.db")}
    exports = {p.name: p for p in (backup_root / "exports").glob("*") if p.is_dir()}
    stamps = [s for s in set(snaps) | set(exports) if _parse_stamp(s) is not None]
    keep = keep_snapshots(stamps)
    gone = sorted(s for s in stamps if s not in keep)
    for s in gone:
        if s in snaps:
            snaps[s].unlink()
        if s in exports:
            shutil.rmtree(exports[s])
    return tuple(gone)


def _integrity_due(store: Store, at: datetime) -> bool:
    last = state(store, "last_integrity_check_at")
    return last is None or at - ts(last.value) >= INTEGRITY_EVERY


def _prepare(store: Store, at: datetime) -> tuple[Path, bool]:
    """The backup's first writer unit: pins and set rows brought up to date (step 1); returns the database path and
    whether the weekly integrity check is due."""
    refresh_pins(store)
    ensure_set_rows(store)
    return store.path, _integrity_due(store, at)


def _record(store: Store, at: str, checked: bool) -> None:
    """The backup's last writer unit: the success (step 6)."""
    store.set_system_state("last_backup_at", at, at=at)
    if checked:
        store.set_system_state("last_integrity_check_at", at, at=at)


def run_backup(store: Store | StoreWriter, backup_root: str | os.PathLike, now: str, *,
               artifact_root: str | os.PathLike, staging: str | os.PathLike | None = None) -> BackupResult:
    """The nightly backup (module docstring); never raises for a failed step: the result says what happened and
    `last_backup_at` or `last_backup_error` is written. `store` is the Store this process writes or, while the
    Reporter service runs, its writer (`writer_call`, 8.2): the writer is held only to refresh the pins and to
    record the result, while the snapshot (a read-only connection), the exports and the mirror (whose pinned
    copies are read from the snapshot) run on the caller's thread as the service keeps writing (8.6)."""
    write = writer_call(store)
    at = utc(now)
    stamp = stamp_of(at)
    root, arts = Path(backup_root), Path(artifact_root)
    snap: Path | None = None
    checked = stop = False
    try:
        db, due = write(lambda s: _prepare(s, ts(at)))
        stage = Path(staging) if staging is not None else db.parent / "backup-staging"
        if stage.is_dir():
            shutil.rmtree(stage)
        snap = vacuum_into(db, stage / f"{SNAPSHOT_PREFIX}{stamp}.db")
        if due:
            res = weekly_integrity(snap)
            checked = True
            if not res.ok:
                stop = True
                raise BackupError(INTEGRITY_PREFIX + "; ".join(res.messages))
        exports = export_tables(snap, stage / "exports" / stamp)
        copied = int(copy_file(snap, root / "snapshots" / snap.name, verify=sha256_file(snap)))
        copied += mirror_tree(stage / "exports" / stamp, root / "exports" / stamp)
        copied += mirror_tree(arts / "baselines", root / "baselines")
        with Store.reader(snap) as view:
            pinned, known = mirror_pinned(view, arts, root)
        copied += pinned
        write(lambda s: _record(s, at, checked))
        pruned = prune_backups(root)
        shutil.rmtree(stage)
        return BackupResult(True, at, stamp, root / "snapshots" / snap.name, exports, copied, known, pruned, checked)
    except Exception as e:                        # 8.6: any failure writes last_backup_error
        text = str(e) if isinstance(e, BackupError) else f"{type(e).__name__}: {e}"
        try:
            write(lambda s: s.set_system_state("last_backup_error", text, at=at))
        except Exception as again:
            text += f" (and last_backup_error could not be written: {again})"
        return BackupResult(False, at, stamp, snap, integrity_checked=checked, error=text, stop_service=stop,
                            alert=store_health())


def backup_window_start(now: str) -> datetime:
    """The most recent scheduled backup (02:00 America/New_York) at or before `now`, in UTC (8.6)."""
    local = ts(now).astimezone(NEW_YORK)
    start = local.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def backup_ok(store: Store, now: str) -> bool:
    """True when tonight's backup succeeded: a success at or after the most recent scheduled 02:00 America/New_York
    before `now` (less `BACKUP_SLACK`), with no error since (8.6: otherwise the retention sweep is skipped). A night
    whose job left no record (killed, never started, the writer busy) is a failed night."""
    last = state(store, "last_backup_at")
    return (last is not None and backup_error(store) is None
            and ts(last.value) >= backup_window_start(now) - BACKUP_SLACK)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel backup", description="The nightly Central Store backup (8.6).")
    p.add_argument("--db", required=True, help="the Central Store database")
    p.add_argument("--backup-root", required=True, help="the backup location")
    p.add_argument("--artifact-root", required=True, help="the control host's artifact root (8.4)")
    p.add_argument("--now", help="the backup time (default now)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 on a good backup, 1 on a failed one (the error on stderr, `last_backup_error` written), 2 on a usage
    error or a missing database; 3 when the weekly integrity check failed (stop the service and restore, 8.9);
    4 (`EXIT_NOT_OPENED`) when the Central Store would not open for writing (the Reporter service holds
    writer.lock, or the file failed its open checks): a failed backup with nothing written, so the scheduler raises
    the 9.10 Store health alert and `backup_ok` skips tonight's sweep."""
    a = _parser().parse_args(argv)
    if not Path(a.db).is_file():
        print(f"sentinel backup: no database at {a.db}", file=sys.stderr)
        return 2
    try:
        store = Store(a.db)
    except StoreError as e:
        print(f"sentinel backup: failed: {e}; {NOT_OPENED_HINT}", file=sys.stderr)
        return EXIT_NOT_OPENED
    with store:
        res = run_backup(store, a.backup_root, a.now or utc_now(), artifact_root=a.artifact_root)
    if not res.ok:
        print(f"sentinel backup: failed: {res.error}", file=sys.stderr)
        return 3 if res.stop_service else 1
    print(f"backup {res.stamp}: snapshot {res.snapshot}, {res.copied} files copied, {len(res.pruned)} pruned"
          + (", integrity ok" if res.integrity_checked else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
