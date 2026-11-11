"""Tests for sentinel.store.migrations: forward-only migrations at service start after a VACUUM INTO snapshot
(8.2, 8.6, 8.9)."""
import copy
import sqlite3

import pytest

from sentinel.records import CheckpointOutcome, Finding
from sentinel.store import RunPlan, ScopeEntry, Store, StoreError
from sentinel.store import migrations
from sentinel.store.store import SCHEMA_VERSION, render_schema
from sentinel.taxonomy import Taxonomy, load

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"


def plan():
    return RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=(ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),))


def seed(db):
    with Store(db) as s:
        s.begin_run(plan())
        s.write_checkpoint(CheckpointOutcome(run_id=RUN, checkpoint_id=CENSUS, outcome="collateral", workspace_id=WS,
                                             persona="active_trader", data_mode="frozen"),
                           findings=[Finding("F-1", RUN, "collateral", "moved", RUN, checkpoint_id=CENSUS)])


def dump(db):
    conn = sqlite3.connect(db)
    try:
        return list(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def contents(db):
    """Every table's rows, sorted (VACUUM INTO may renumber implicit rowids), and the schema version."""
    conn = sqlite3.connect(db)
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return ({n: sorted(conn.execute(f"SELECT * FROM {n}").fetchall(), key=repr) for n in names},
                conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def with_kind(kind):
    data = copy.deepcopy(load().data)
    data["finding_kinds"].append(kind)
    return Taxonomy(data)


def test_a_new_finding_kind_migrates_keeping_rows_after_a_snapshot(tmp_path):
    db = tmp_path / "central.db"
    seed(db)
    before = contents(db)
    t = with_kind("test_only_kind")
    with Store(db, taxonomy=t) as s:
        m = s.migration
        assert (m.from_version, m.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION) and m.from_digest != m.to_digest
        assert m.snapshot.parent == tmp_path / "pre-migration" and "findings" in m.tables
        assert s.get_outcome(RUN, CENSUS).outcome == "collateral" and s.get_finding("F-1").kind == "collateral"
        s.insert("findings", {"natural_key": "F-2", "run_id": RUN, "kind": "test_only_kind", "description": "new",
                              "evidence_json": "{}", "created_at": "2026-11-10T00:00:00+00:00"})   # now insertable
        assert s.query("SELECT name FROM sqlite_master WHERE name = 'trg_outcome_keep'")
        assert s.get_system_state("schema_version") == str(SCHEMA_VERSION)
    assert contents(m.snapshot) == before                # the pre-migration file, intact (8.6)
    with Store(db, taxonomy=t) as s:                     # opened again: nothing left to migrate
        assert s.migration is None


def test_a_failed_migration_leaves_the_file_untouched_and_stops(tmp_path):
    db = tmp_path / "central.db"
    seed(db)
    before = dump(db)
    data = copy.deepcopy(load().data)
    data["finding_kinds"].remove("collateral")           # a row still uses it: the copy fails the new CHECK
    with pytest.raises(StoreError, match="unchanged.*pre-migration snapshot"):
        Store(db, taxonomy=Taxonomy(data))
    assert dump(db) == before
    assert len(list((tmp_path / "pre-migration").iterdir())) == 1
    with Store(db) as s:                                 # the refused open left no lock and no change
        assert s.migration is None


def test_an_older_version_is_migrated_forward_by_its_registered_step(tmp_path):
    db = tmp_path / "central.db"
    seed(db)
    conn = sqlite3.connect(db)                           # a version-1 file: no proposals table yet
    conn.execute("DROP INDEX ux_proposal_open")
    conn.execute("DROP TABLE proposals")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with Store(db) as s:
        assert (s.migration.from_version, s.migration.to_version) == (1, SCHEMA_VERSION)
        assert s.query("SELECT count(*) FROM proposals")[0][0] == 0 and s.get_finding("F-1") is not None
        assert s.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_forward_only_and_registered_steps_only(tmp_path):
    db = tmp_path / "central.db"
    seed(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    with pytest.raises(StoreError, match="forward-only"):
        Store(db)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    ddl = render_schema(load())
    with pytest.raises(migrations.MigrationError, match="no migration from schema version 1"):
        migrations.migrate(conn, SCHEMA_VERSION, ddl=ddl, digest="d", at="2026-11-10T00:00:00+00:00", steps={})
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_a_dropped_column_or_table_with_data_is_refused(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    conn.executescript("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
                       "CREATE TABLE t (a INTEGER, b TEXT); INSERT INTO t VALUES (1, 'kept');"
                       "PRAGMA user_version = 1;")
    with pytest.raises(migrations.MigrationError, match="t.b holds data"):
        migrations.migrate(conn, 1, ddl="CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at "
                           "TEXT); CREATE TABLE t (a INTEGER);", digest="d", at="now")
    with pytest.raises(migrations.MigrationError, match="table t holds data"):
        migrations.migrate(conn, 1, ddl="CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at "
                           "TEXT);", digest="d", at="now")
    assert conn.execute("SELECT * FROM t").fetchall() == [(1, "kept")]
    report = migrations.migrate(conn, 1, ddl="CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, "
                                "updated_at TEXT); CREATE TABLE t (a INTEGER, b TEXT, c TEXT DEFAULT 'x');",
                                digest="d", at="now")
    assert report.tables == ("system_state", "t") and conn.execute("SELECT * FROM t").fetchall() == [(1, "kept", "x")]
