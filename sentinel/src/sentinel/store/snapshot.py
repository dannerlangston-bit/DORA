"""The read-only snapshot set shipped to every shard of a run (8.1, 8.3, 8.5 What lives in git and how a run pins it).

At run creation the Orchestrator ships, beside the config tarball, "a read-only snapshot of the Central Store's
decisions, baselines, regions, masks, calibration floors and quarantine records as of run creation, hashed into
`snapshot_set_sha256`, so every shard applies the same inheritance and the run can name the decision set it used".

- `export_snapshot(store, path, at, config_commit=, run_id=)` writes that snapshot as a SQLite file with the Central
  Store's tables and indexes, and returns its `snapshot_set_sha256` (64 lowercase hex, `RunPlan.snapshot_set_sha256`).
  The rig opens it with `store.connect_reader(path)` and runs the 8.3 signature lookup on it
  (`store.lookup_decisions(conn, signature, ...)`), so a decision written after run creation is never inherited
  by that run.
- `snapshot_sha256(path)` recomputes the hash from a snapshot file (the rig's check of what it received).
- `provenance_row(...)` is the Run health line 8.5 asks for: both commits, the tarball hash and the snapshot hash.

What the snapshot holds (`SNAPSHOT_TABLES`), read in one transaction so it is consistent:
- decisions decided at or before `at` (every status: the 9.7 freeze counts every decision ever made on a
  signature), and the rows the lookup joins: their runs and source findings, every checkpoint (the rename chain),
  every build and build pair (the fix-build order and the run's candidate build), plus the run being created when
  the store already has it (its build pair gives the 9.7 scope of mark_intended);
- baselines proposed at or before `at`;
- regions and masks at the run's config commit (8.5: loaded from the same commit);
- calibration floors current at `at` (`valid_from` at or before it, not superseded by then);
- quarantine records: the open `safety_quarantines` rows at `at` and the checkpoints `flake_stats` holds
  quarantined (the quarantine.yaml mirror of 10.5).

Choices where the spec is ambiguous (reported to the coordinator):
- The hash is over the content, not the file bytes: the canonical JSON (`canonical.canonical_json`) of each
  snapshot table's columns and rows, rows sorted, BLOBs as `{"blob": hex}`. The same content therefore gives the
  same hash whatever SQLite version wrote the file or in which order rows arrived, and rows of tables outside the
  set (outcomes, step events, ...) never change it.
- The file has the Central Store's tables and indexes and no triggers (they guard writes; the snapshot is never
  written) and `user_version` 0, so `Store` refuses to open it as a writable database.
- Rows are as they stand at export: the export runs at run creation, so a decision's status is its status as of
  `at`; `at` also bounds which rows exist, for a clock that runs ahead.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..canonical import canonical_json
from .store import Store, connect_reader, utc

# table -> the rows of the source it takes (named parameters :at, :commit, :run)
SNAPSHOT_TABLES: dict[str, str] = {
    "decisions": "decided_at <= :at",
    "findings": "finding_id IN (SELECT source_finding_id FROM src.decisions WHERE decided_at <= :at)",
    "runs": "run_id IN (SELECT run_id FROM src.decisions WHERE decided_at <= :at) OR run_id = :run",
    "checkpoints": "1",
    "builds": "1",
    "build_pairs": "1",
    "baselines": "proposed_at <= :at",
    "regions": "config_commit = :commit",
    "masks": "config_commit = :commit",
    "calibration_floors": "valid_from <= :at AND (superseded_at IS NULL OR superseded_at > :at)",
    "safety_quarantines": "opened_at <= :at AND (reenabled_at IS NULL OR reenabled_at > :at)",
    "flake_stats": "quarantined = 1",
}
SNAPSHOT_FORMAT = 1


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _value(v: Any) -> Any:
    return {"blob": v.hex()} if isinstance(v, bytes) else v


def _digest(conn: sqlite3.Connection) -> str:
    """The content hash of the snapshot tables on `conn` (module docstring)."""
    doc: dict[str, Any] = {"format": SNAPSHOT_FORMAT, "tables": {}}
    for table in sorted(SNAPSHOT_TABLES):
        cols = _columns(conn, table)
        rows = sorted(canonical_json([_value(v) for v in r])
                      for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}"))
        doc["tables"][table] = {"columns": cols, "rows": rows}
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def export_snapshot(store: Store, path: str | os.PathLike, at: str, *, config_commit: str,
                    run_id: str | None = None) -> str:
    """Write the run's snapshot set to `path` (replaced if present) and return its `snapshot_set_sha256`."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    args = {"at": utc(at), "commit": config_commit, "run": run_id}
    conn = sqlite3.connect(tmp.resolve().as_uri() + "?mode=rwc", uri=True, isolation_level=None)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (store.path.resolve().as_uri() + "?mode=ro",))
        ddl = conn.execute("SELECT type, sql FROM src.sqlite_master WHERE type IN ('table', 'index') AND sql IS NOT "
                           "NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
        conn.execute("BEGIN")
        for _, sql in ddl:                         # tables first ('table' > 'index'), then their indexes
            conn.execute(sql)
        for table, where in SNAPSHOT_TABLES.items():
            cols = ", ".join(_columns(conn, table, "src"))
            conn.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM src.{table} WHERE {where} "
                         f"ORDER BY {cols}", args)
        conn.execute("COMMIT")
        digest = _digest(conn)
        conn.execute("DETACH DATABASE src")
        conn.execute("VACUUM")
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)                # no half-written snapshot is left beside the shipped one
        raise
    conn.close()
    os.replace(tmp, dest)
    return digest


def snapshot_sha256(path: str | os.PathLike) -> str:
    """The `snapshot_set_sha256` of a snapshot file, recomputed from its content."""
    conn = connect_reader(path)
    try:
        return _digest(conn)
    finally:
        conn.close()


def provenance_row(*, config_commit: str, harness_commit: str, config_tarball_sha256: str,
                   snapshot_set_sha256: str) -> list[str]:
    """The Run health row of 8.5 ([label, text], `runs.summary_json` `health_rows`)."""
    return ["Provenance", f"config {config_commit}; harness {harness_commit}; config tarball "
                          f"{config_tarball_sha256}; snapshot set {snapshot_set_sha256}"]
