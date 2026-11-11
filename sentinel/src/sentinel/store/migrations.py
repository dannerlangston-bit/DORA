"""Forward-only schema migrations at service start (8.2, 8.6, 8.9).

`PRAGMA user_version` holds the schema version (8.2); `system_state.schema_sha256` holds the digest of the DDL the
file was generated with, which changes whenever a generated CHECK list of `taxonomy.yaml` changes (8.3: finding
kinds, outcomes and the other lists grow by pull request). `Store` opens a file and, when its version is older than
the code's or its digest is another taxonomy's, takes a pre-migration snapshot with `snapshot` (8.6: migrations run
only after a successful VACUUM INTO of the pre-migration file) and calls `migrate`. A failed migration rolls back
whole, so the file is as it was and the snapshot is intact; the service stops (8.9).

`migrate(conn, target, ddl=, digest=)` runs, in one transaction, the registered step of every version from the
file's up to `target` (`STEPS`: version n to n + 1), or, at the target version with another digest, `rebuild`
alone. Then it checks the foreign keys, records the new version and digest, and commits.

`rebuild(conn, ddl)` is the SQLite table-rebuild pattern applied to the whole schema: drop every trigger, index and
view; rename every table aside; create the target tables; copy each kept table's common columns; drop the old
tables; create the target indexes and triggers. Triggers are created after the copy, so history the triggers
would refuse today (a retired reason, a closed incident's rig) is kept as written; the target CHECKs and NOT NULLs
apply to the copied rows, so a list that lost a value still in use fails the migration instead of losing rows.

Choices where the spec is silent (reported):
- Version 1 (before the foundation's tables and columns) migrates to 2 with `rebuild`: the foundation added tables
  and nullable or defaulted columns only. A future step that must transform data registers its own function.
- A table or column the target schema drops is refused while it holds data (`MigrationError`): forward-only never
  loses facts silently; an empty one goes.
- The snapshot is `pre-migration/<db name>-v<version>-<UTC stamp>.db` beside the database (8.6 does not name it);
  the backup job's mirror is not involved, so a migration needs no backup location.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

Step = Callable[[sqlite3.Connection, str], None]
_PREFIX = "_mig_"


class MigrationError(Exception):
    """A migration cannot run or failed; the database is unchanged (8.9: the service stops)."""


@dataclass(frozen=True)
class MigrationReport:
    """What a migration did: the versions and digests before and after, and the pre-migration snapshot."""
    from_version: int
    to_version: int
    from_digest: str | None
    to_digest: str
    snapshot: Path | None
    tables: tuple[str, ...]                 # tables whose rows were copied


def snapshot_path(db: Path, version: int, at: datetime | None = None) -> Path:
    """Where the pre-migration snapshot of `db` at `version` goes (module docstring): the first free name, so two
    migrations within a second keep both snapshots."""
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = db.parent / "pre-migration" / f"{db.stem}-v{version}-{stamp}"
    out, n = base.with_suffix(".db"), 1
    while out.exists():
        n += 1
        out = base.parent / f"{base.name}-{n}.db"
    return out


def snapshot(conn: sqlite3.Connection, dest: Path) -> Path:
    """`VACUUM INTO dest`: the consistent pre-migration copy 8.6 requires before any migration. An existing file
    at `dest` is never overwritten."""
    if dest.exists():
        raise MigrationError(f"{dest} exists; a pre-migration snapshot is never overwritten")
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(dest),))
    return dest


def _master(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return [(t, n, sql) for t, n, sql in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY rowid")]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def rebuild(conn: sqlite3.Connection, ddl: str) -> tuple[str, ...]:
    """Rebuild every table to the target DDL inside the caller's transaction (module docstring); returns the
    tables whose rows were copied."""
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(ddl)
        target = _master(scratch)
    finally:
        scratch.close()
    old = _master(conn)
    for kind, name, sql in old:
        if kind in ("trigger", "view") or (kind == "index" and sql is not None):
            conn.execute(f'DROP {kind.upper()} "{name}"')
    old_tables = [n for k, n, _ in old if k == "table"]
    for name in old_tables:
        conn.execute(f'ALTER TABLE "{name}" RENAME TO "{_PREFIX}{name}"')
    new_tables = [(n, sql) for k, n, sql in target if k == "table"]
    for _, sql in new_tables:
        conn.execute(sql)
    copied = []
    for name, _ in new_tables:
        if name not in old_tables:
            continue
        before, after = _columns(conn, _PREFIX + name), _columns(conn, name)
        for col in (c for c in before if c not in after):
            if conn.execute(f'SELECT 1 FROM "{_PREFIX}{name}" WHERE "{col}" IS NOT NULL LIMIT 1').fetchone():
                raise MigrationError(f"{name}.{col} holds data and the target schema drops it; register a step "
                                     "that moves it (forward-only migrations never lose facts)")
        common = ",".join(f'"{c}"' for c in before if c in after)
        conn.execute(f'INSERT INTO "{name}" ({common}) SELECT {common} FROM "{_PREFIX}{name}"')
        copied.append(name)
    kept = {n for n, _ in new_tables}
    for name in old_tables:
        if name not in kept and conn.execute(f'SELECT 1 FROM "{_PREFIX}{name}" LIMIT 1').fetchone():
            raise MigrationError(f"table {name} holds data and the target schema drops it; register a step "
                                 "that moves it")
        conn.execute(f'DROP TABLE "{_PREFIX}{name}"')
    for kind, _, sql in target:
        if kind != "table" and sql is not None:
            conn.execute(sql)
    return tuple(copied)


# version n -> the step that migrates it to n + 1 (module docstring)
STEPS: Mapping[int, Step] = {1: rebuild}


def migrate(conn: sqlite3.Connection, target: int, *, ddl: str, digest: str, at: str,
            steps: Mapping[int, Step] = STEPS, snapshot_file: Path | None = None) -> MigrationReport:
    """Migrate the database on `conn` forward to `target` and the schema `ddl` (digest `digest`) in one
    transaction (module docstring). `at` stamps the `system_state` rows. Raises MigrationError, with nothing
    changed, for a newer file, a version with no registered step, a failed step or a foreign key the copy broke."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > target:
        raise MigrationError(f"schema version {version} is newer than {target}; migrations are forward-only")
    if version < 1:
        raise MigrationError(f"schema version {version} has no Sentinel schema to migrate")
    chain = [steps.get(v) for v in range(version, target)]
    if None in chain:
        missing = next(v for v, f in zip(range(version, target), chain) if f is None)
        raise MigrationError(f"no migration from schema version {missing}")
    row = conn.execute("SELECT value FROM system_state WHERE key = 'schema_sha256'").fetchone()
    before = None if row is None else row[0]
    conn.execute("PRAGMA foreign_keys = OFF")          # outside the transaction, as SQLite requires
    conn.execute("PRAGMA legacy_alter_table = ON")     # a rename leaves other tables' references alone
    copied: tuple[str, ...] = ()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for step in chain or [rebuild]:
                copied = tuple(step(conn, ddl) or ())
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise MigrationError(f"the migrated rows break {len(broken)} foreign key(s), first in table "
                                     f"{broken[0][0]}")
            for key, value in (("schema_version", str(target)), ("schema_sha256", digest)):
                conn.execute("INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) "
                             "DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                             (key, value, at))
            conn.execute(f"PRAGMA user_version = {int(target)}")
            conn.execute("COMMIT")
        except BaseException as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(e, sqlite3.Error):
                raise MigrationError(f"migration from version {version} failed: {e}") from e
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    return MigrationReport(version, target, before, digest, snapshot_file, copied)
