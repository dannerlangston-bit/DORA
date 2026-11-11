"""Storage preflight: the free-space floors of R7 and the store checks of H7 (8.2, 8.6, 8.8, 8.9; 12.7 R7 and H7;
11.4 and 8.8 the control-host disk alarm).

- Floors (8.6, 12.7 R7): the rig needs free space above the larger of 20 GB and twice the projected shard size; the
  control host above the larger of 100 GB and twice the projected run size, the scope count times two sides times the
  mean capture size from the last calibration run (`mean_capture_bytes`). `rig_floor_bytes`, `control_floor_bytes`,
  `projected_bytes` and `r7_failures` (both halves and the NTP offset under 2 s) are the one definition preflight R7,
  preflight H7's free-space line and the Watchdog's rig floor use.
- `storage_preflight(db_path, artifact_root, role=..., checkpoints=..., mean_capture_bytes=...)`: H7 first (both paths
  on a fixed local volume, `PRAGMA quick_check`, the WAL writable, the single-writer lock, a write probe under
  `objects/`), then this host's half of R7. The result names the failing item (`H7` before `R7`, since an H item ends
  the run `aborted_harness` and an R item only marks checkpoints `env_error`) and converts to the
  `ports.PreflightRead` that `orchestrator.preflight` evaluates (`as_read`).
- `write_refusal(path, result, at)`: a process whose H7 fails refuses to start and writes the reason to its heartbeat
  file, so the dead-man alert fires at the next deadline with the cause on disk (8.2, 8.9).
- `usage_alarm(total, free)`: the control-host store volume alarms at 70 percent use (8.8, 11.4), the 9.10 store
  health row.

Choices where the spec is ambiguous or silent (reported):

1. Units: GB is 1024**3 bytes, as `orchestrator.watchdog.DISK_ABORT_BYTES` and `orchestrator.boundary.DISK_FLOOR_BYTES`
   use; a floor is passed only by free space strictly above it ("above the larger of", 12.7).
2. The projected shard size is the shard's checkpoints times the sides that rig captures (two for a pair on one rig,
   one for a lockstep half) times the mean capture size; the projected run size is the scope count times two sides
   times the mean capture size (8.6).
3. The mean capture size is the mean `bytes` of the `capture` artifacts in the set `runs/<run_id>` of the most recent
   finished calibration run that stored captures. Before any calibration run has measured one, the 8.8 planning figure
   SA-01 plus SA-02 (1.6 MB per capture with its sidecars) stands in and the source says so, because the first
   calibration run itself needs a floor; 8.8 forbids the table only once a measurement exists.
4. H7 runs in the writer process: the lock check passes when `writer.lock` holds this process's PID or the lock can be
   taken (and is released at once); another live writer fails it. "WAL writable" opens a second connection and takes
   and rolls back a write transaction. The git checkout at the pinned commit with the CI lint hash (also G1) is not a
   storage read: the caller adds `config_commit_ok` and `lint_hash_ok` to the H7 read, and H7 fails without them.
5. The usage alarm fires at 70 percent of the volume used or more; it routes as the 9.10 store health row, P2 to the
   on-call channel.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..orchestrator.alerts import ON_CALL_CHANNEL, P2, AlertRoute
from ..orchestrator.ports import PreflightRead
from .store import StoreError, WriterLock, WriterLockError, check_local_volume

GB = 1024 ** 3
MB = 1024 ** 2
RIG_FLOOR_MIN_BYTES = 20 * GB          # 12.7 R7, 8.6
CONTROL_FLOOR_MIN_BYTES = 100 * GB     # 12.7 R7, 8.6
SIDES = 2                              # candidate and base
NTP_MAX_OFFSET_S = 2.0                 # 12.7 R7
USAGE_ALARM_FRACTION = 0.70            # 8.8, 11.4
PLANNING_CAPTURE_BYTES = int(1.6 * MB)  # 8.8 SA-01 (1.5 MB) plus SA-02 (0.1 MB), choice 3
ROLES = ("rig", "control")

DiskUsage = Callable[[str], Any]       # shutil.disk_usage's shape: .total, .used, .free


# -- the floors (8.6, 12.7 R7) ----------------------------------------------------------------------------------
def projected_bytes(checkpoints: int, mean_capture_bytes: int, sides: int = SIDES) -> int:
    """The projected size of a shard or a run (choice 2): checkpoints times sides times the mean capture size."""
    if checkpoints < 0 or mean_capture_bytes < 0 or sides not in (1, 2):
        raise ValueError(f"checkpoints {checkpoints}, mean {mean_capture_bytes}, sides {sides}")
    return checkpoints * sides * mean_capture_bytes


def rig_floor_bytes(shard_bytes: int) -> int:
    """The rig floor of R7 (8.6): the larger of 20 GB and twice the projected shard size."""
    return max(RIG_FLOOR_MIN_BYTES, 2 * shard_bytes)


def control_floor_bytes(run_bytes: int) -> int:
    """The control-host floor of R7 (8.6): the larger of 100 GB and twice the projected run size."""
    return max(CONTROL_FLOOR_MIN_BYTES, 2 * run_bytes)


def _gb(n: float) -> str:
    return f"{n / GB:.1f} GB"


def free_space_failure(free_bytes: int, floor_bytes: int, where: str) -> str | None:
    """None when `free_bytes` is above the floor (choice 1), else the failure naming the floor."""
    if free_bytes > floor_bytes:
        return None
    return f"{where} free space {_gb(free_bytes)} is not above the floor {_gb(floor_bytes)}"


def ntp_failure(offset_s: float) -> str | None:
    """None when the clock offset is under 2 s (12.7 R7), else the failure."""
    return None if abs(offset_s) < NTP_MAX_OFFSET_S else f"NTP offset {offset_s:+.1f} s is not under 2 s"


def r7_failures(*, rig_free_bytes: int, shard_bytes: int, control_free_bytes: int, run_bytes: int,
                ntp_offset_s: float) -> tuple[str, ...]:
    """The failing parts of R7 (12.7, 8.6): rig floor, control-host floor and NTP offset; () when R7 passes."""
    out = [free_space_failure(rig_free_bytes, rig_floor_bytes(shard_bytes), "rig"),
           free_space_failure(control_free_bytes, control_floor_bytes(run_bytes), "control host"),
           ntp_failure(ntp_offset_s)]
    return tuple(x for x in out if x)


# -- the mean capture size (8.6, 8.8) --------------------------------------------------------------------------
_MEAN_SQL = """SELECT r.run_id, AVG(a.bytes) AS mean, COUNT(*) AS n FROM runs r
JOIN artifact_sets s ON s.set_path = 'runs/' || r.run_id
JOIN artifacts a ON a.sha256 = s.sha256
WHERE r.run_type = 'calibration' AND r.lifecycle = 'finished' AND a.kind = 'capture'
GROUP BY r.run_id ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1"""


@dataclass(frozen=True)
class MeanCapture:
    """The mean capture size the floors use and where it came from (choice 3)."""

    bytes: int
    source: str                        # "calibration run <id> (<n> captures)" or the planning figure
    run_id: str | None = None


def mean_capture_bytes(conn: sqlite3.Connection) -> MeanCapture:
    """The mean capture size from the last finished calibration run with captures, over a Central Store connection
    (`Store.conn` or `store.connect_reader`); the 8.8 planning figure when none has measured one (choice 3)."""
    row = conn.execute(_MEAN_SQL).fetchone()
    if row is None:
        return MeanCapture(PLANNING_CAPTURE_BYTES, "planning figure 8.8 SA-01 plus SA-02: no calibration run measured")
    run_id, mean, n = row[0], row[1], row[2]
    return MeanCapture(int(round(mean)), f"calibration run {run_id} ({n} captures)", run_id)


# -- H7 (8.2, 10.8, 12.7) --------------------------------------------------------------------------------------
def _local(path: Path) -> str | None:
    try:
        check_local_volume(path)
    except StoreError as e:
        return str(e)
    return None


def quick_check(db_path: str | os.PathLike) -> str:
    """`PRAGMA quick_check` on a read-only connection: `ok`, or what SQLite reported (a corrupt or missing file)."""
    p = Path(db_path)
    if not p.is_file():
        return f"no database at {p}"
    try:
        conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return f"quick_check failed: {e}"
    msgs = [str(r[0]) for r in rows]
    return "ok" if msgs == ["ok"] else "; ".join(msgs)


def wal_writable(db_path: str | os.PathLike) -> str | None:
    """None when the database is in WAL mode and a second connection can take and roll back a write transaction
    (choice 4), else the failure."""
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=1.0)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                return f"journal mode {mode}, not WAL"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"WAL not writable: {e}"
    return None


def writer_lock_ok(db_path: str | os.PathLike) -> str | None:
    """None when this process holds `writer.lock` beside the database or can take it (choice 4), else the failure."""
    path = Path(db_path).parent / "writer.lock"
    with contextlib.suppress(OSError):
        if path.read_text().strip() == str(os.getpid()):
            return None
    lock = WriterLock(path)
    try:
        lock.acquire()
    except WriterLockError as e:
        return str(e)
    lock.release()
    return None


def write_probe(artifact_root: str | os.PathLike) -> str | None:
    """None when a probe file under `<artifact_root>/objects/` is written, read back and removed, else the failure."""
    objects = Path(artifact_root) / "objects"
    probe = objects / f".h7-probe.{os.getpid()}.tmp"
    data = b"sentinel H7 write probe\n"
    try:
        objects.mkdir(parents=True, exist_ok=True)
        with open(probe, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if probe.read_bytes() != data:
            return "the write probe did not read back"
    except OSError as e:
        return f"content store not writable: {e}"
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return None


@dataclass(frozen=True)
class StoragePreflight:
    """What `storage_preflight` found on one host: passed, or the failing item (`H7` or `R7`) and why."""

    role: str
    passed: bool
    item: str | None
    reason: str | None
    checks: Mapping[str, Any] = field(default_factory=dict)
    free_bytes: int = 0
    floor_bytes: int = 0
    projected_bytes: int = 0

    def as_read(self, item: str) -> PreflightRead:
        """This host's part of item `H7` or `R7` as the read `orchestrator.preflight.evaluate` takes; the caller merges
        the other host's R7 half and the H7 config checks (`orchestrator.preflight.merge_reads`)."""
        if item == "H7":
            keys = ("db_local", "artifacts_local", "quick_check", "wal", "lock", "writable")
            values = {k: self.checks[k] for k in keys}
            values.update({"free_bytes": self.free_bytes, "shard_bytes": self.projected_bytes})
            return PreflightRead("H7", values)
        if item == "R7":
            if self.role == "rig":
                return PreflightRead("R7", {"rig_free_bytes": self.free_bytes, "shard_bytes": self.projected_bytes})
            return PreflightRead("R7", {"control_free_bytes": self.free_bytes, "run_bytes": self.projected_bytes})
        raise ValueError(f"storage preflight reads H7 and R7, not {item!r}")


def storage_preflight(db_path: str | os.PathLike, artifact_root: str | os.PathLike, *, role: str, checkpoints: int,
                      mean_capture_bytes: int, sides: int = SIDES,
                      disk_usage: DiskUsage = shutil.disk_usage) -> StoragePreflight:
    """H7 then this host's half of R7 (module docstring). `checkpoints` is the shard's checkpoint count on a rig and the
    scope count on the control host; `sides` is how many sides this rig captures (choice 2; always two for a run)."""
    if role not in ROLES:
        raise ValueError(f"role {role!r} is not one of {ROLES}")
    projected = projected_bytes(checkpoints, mean_capture_bytes, sides if role == "rig" else SIDES)
    floor = rig_floor_bytes(projected) if role == "rig" else control_floor_bytes(projected)
    db, root = Path(db_path), Path(artifact_root)
    db_local, art_local = _local(db), _local(root)
    qc = quick_check(db)
    checks: dict[str, Any] = {
        "db_local": db_local is None, "artifacts_local": art_local is None, "quick_check": qc,
        "wal": None, "lock": None, "writable": None}
    failures = [x for x in (db_local, art_local) if x]
    if qc != "ok":
        failures.append(qc)
    else:
        wal, lock = wal_writable(db), writer_lock_ok(db)
        checks.update(wal=wal is None, lock=lock is None)
        failures += [x for x in (wal, lock) if x]
    probe = write_probe(root) if art_local is None else None
    checks["writable"] = art_local is None and probe is None
    if probe:
        failures.append(probe)
    free = int(disk_usage(str(root if root.exists() else root.parent)).free)

    def result(item: str | None, reason: str | None) -> StoragePreflight:
        return StoragePreflight(role, item is None, item, reason, checks, free, floor, projected)
    if failures:
        return result("H7", "; ".join(failures))
    space = free_space_failure(free, floor, "rig" if role == "rig" else "control host")
    return result("R7", space) if space else result(None, None)


def write_refusal(heartbeat_path: str | os.PathLike, result: StoragePreflight, at: datetime) -> Path:
    """Write the refusal to start into the process's heartbeat file (8.2: the process refuses to start and writes the
    reason there); atomic, as every heartbeat write is (10.2)."""
    if result.passed:
        raise ValueError("a passing storage preflight is not a refusal")
    path = Path(heartbeat_path)
    body = {"state": "refused_to_start", "item": result.item, "reason": result.reason, "role": result.role,
            "ts": at.isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


# -- the control-host disk alarm (8.8, 11.4) --------------------------------------------------------------------
def store_health_alert() -> AlertRoute:
    """The 9.10 store health row: P2 to the on-call channel (choice 5)."""
    return AlertRoute("store_health", P2, (), (ON_CALL_CHANNEL,))


def usage_alarm(total_bytes: int, free_bytes: int) -> AlertRoute | None:
    """The store volume alarm at 70 percent used (8.8, 11.4): the store health route, or None below it."""
    if total_bytes <= 0:
        raise ValueError("a volume has a positive size")
    used = (total_bytes - free_bytes) / total_bytes
    return store_health_alert() if used >= USAGE_ALARM_FRACTION else None
