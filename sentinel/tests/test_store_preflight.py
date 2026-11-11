"""Storage preflight: the R7 floors, the H7 store checks, the mean capture size and the 70 percent alarm (8.2, 8.6,
8.8, 8.9; 12.7 R7 and H7)."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from sentinel.store import Store
from sentinel.store.preflight import (CONTROL_FLOOR_MIN_BYTES, GB, MB, PLANNING_CAPTURE_BYTES, control_floor_bytes,
                                      mean_capture_bytes, projected_bytes, quick_check, r7_failures, rig_floor_bytes,
                                      storage_preflight, usage_alarm, write_refusal)

Usage = namedtuple("Usage", "total used free")


def disk(free: int, total: int = 2000 * GB):
    return lambda _path: Usage(total, total - free, free)


# -- floors ----------------------------------------------------------------------------------------------------
def test_control_floor_is_100_gb_for_a_merge_scope_and_the_run_term_wins_at_scale():
    merge = projected_bytes(1380, int(1.5 * MB))
    assert round(merge / GB, 1) == 4.0 and control_floor_bytes(merge) == CONTROL_FLOOR_MIN_BYTES
    big = projected_bytes(20_000, int(1.5 * MB))
    assert control_floor_bytes(big) == 2 * big > CONTROL_FLOOR_MIN_BYTES


def test_control_floor_for_1000_checkpoints_at_1_6_mb():
    assert control_floor_bytes(projected_bytes(1000, int(1.6 * MB))) == 100 * GB


def ok_r7(**kw):
    base = dict(rig_free_bytes=40 * GB, shard_bytes=5 * GB, control_free_bytes=500 * GB, run_bytes=10 * GB,
                ntp_offset_s=0.3)
    return r7_failures(**{**base, **kw})


def test_r7_passes_with_room_everywhere():
    assert ok_r7() == ()


def test_r7_shard_floor_doubles_the_projected_shard():
    (f,) = ok_r7(rig_free_bytes=25 * GB, shard_bytes=15 * GB)
    assert "rig free space 25.0 GB" in f and "30.0 GB" in f


def test_r7_rig_floor_is_at_least_20_gb():
    (f,) = ok_r7(rig_free_bytes=19 * GB, shard_bytes=5 * GB)
    assert "20.0 GB" in f
    assert rig_floor_bytes(5 * GB) == 20 * GB


def test_r7_ntp_offset_and_control_floor():
    assert ok_r7(ntp_offset_s=2.1) == ("NTP offset +2.1 s is not under 2 s",)
    assert ok_r7(ntp_offset_s=-1.9) == ()
    (f,) = ok_r7(control_free_bytes=120 * GB, run_bytes=70 * GB)
    assert "control host" in f and "140.0 GB" in f


def test_free_space_equal_to_the_floor_fails():
    assert ok_r7(rig_free_bytes=20 * GB, shard_bytes=1 * GB)


# -- H7 and the storage preflight ------------------------------------------------------------------------------
@pytest.fixture
def stores(tmp_path):
    db = tmp_path / "shard" / "run.db"
    s = Store(db)
    yield s, db, tmp_path / "shard"
    s.close()


def test_storage_preflight_passes_on_a_healthy_rig(stores):
    s, db, root = stores
    r = storage_preflight(db, root, role="rig", checkpoints=600, mean_capture_bytes=int(1.6 * MB),
                          disk_usage=disk(40 * GB))
    assert r.passed and r.item is None
    assert r.checks == {"db_local": True, "artifacts_local": True, "quick_check": "ok", "wal": True, "lock": True,
                        "writable": True}
    assert not any(p.name.startswith(".h7-probe") for p in (root / "objects").iterdir())
    h7 = r.as_read("H7")
    assert h7.values["quick_check"] == "ok" and h7.values["shard_bytes"] == r.projected_bytes
    assert r.as_read("R7").values == {"rig_free_bytes": 40 * GB, "shard_bytes": r.projected_bytes}


def test_storage_preflight_names_r7_when_the_disk_is_short(stores):
    s, db, root = stores
    r = storage_preflight(db, root, role="control", checkpoints=1380, mean_capture_bytes=int(1.5 * MB),
                          disk_usage=disk(90 * GB))
    assert not r.passed and r.item == "R7" and "control host" in r.reason and r.floor_bytes == 100 * GB
    assert r.as_read("R7").values == {"control_free_bytes": 90 * GB, "run_bytes": r.projected_bytes}


def test_a_corrupt_database_fails_h7(tmp_path):
    db = tmp_path / "run.db"
    db.write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
    assert quick_check(db) != "ok"
    r = storage_preflight(db, tmp_path / "arts", role="rig", checkpoints=10, mean_capture_bytes=MB,
                          disk_usage=disk(100 * GB))
    assert not r.passed and r.item == "H7" and r.checks["quick_check"] != "ok" and r.checks["wal"] is None


def test_a_unc_artifact_root_is_refused(stores):
    s, db, _root = stores
    r = storage_preflight(db, "//fileserver/sentinel/run", role="rig", checkpoints=10, mean_capture_bytes=MB,
                          disk_usage=disk(100 * GB))
    assert r.item == "H7" and r.checks["artifacts_local"] is False and r.checks["writable"] is False
    assert "UNC" in r.reason


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user for chmod")
def test_a_read_only_artifact_dir_fails_h7(stores):
    s, db, root = stores
    objects = root / "objects"
    objects.mkdir(exist_ok=True)
    objects.chmod(0o500)
    try:
        r = storage_preflight(db, root, role="rig", checkpoints=10, mean_capture_bytes=MB, disk_usage=disk(100 * GB))
    finally:
        objects.chmod(0o700)
    assert r.item == "H7" and r.checks["writable"] is False and "not writable" in r.reason


def test_a_failing_write_probe_fails_h7(stores, monkeypatch):
    """The same check as the read-only directory, for a root user whom chmod does not stop."""
    s, db, root = stores
    real_open = open

    def refuse(path, *a, **kw):
        if ".h7-probe" in str(path):
            raise PermissionError(13, "read-only", str(path))
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", refuse)
    r = storage_preflight(db, root, role="rig", checkpoints=10, mean_capture_bytes=MB, disk_usage=disk(100 * GB))
    assert r.item == "H7" and r.checks["writable"] is False


def test_another_live_writer_fails_the_lock(stores):
    s, db, root = stores
    (db.parent / "writer.lock").write_text("1")          # PID 1 is alive and is not this process
    r = storage_preflight(db, root, role="rig", checkpoints=10, mean_capture_bytes=MB, disk_usage=disk(100 * GB))
    assert r.item == "H7" and r.checks["lock"] is False


def test_write_refusal_puts_the_reason_in_the_heartbeat_file(tmp_path):
    db = tmp_path / "missing.db"
    r = storage_preflight(db, tmp_path / "a", role="control", checkpoints=1, mean_capture_bytes=MB,
                          disk_usage=disk(500 * GB))
    path = write_refusal(tmp_path / "heartbeat.json", r, datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc))
    body = json.loads(path.read_text())
    assert body["state"] == "refused_to_start" and body["item"] == "H7" and "no database" in body["reason"]


# -- the mean capture size (8.8) -------------------------------------------------------------------------------
def _calibration(conn: sqlite3.Connection, run_id: str, started: str, sizes: list[int], *, kind: str = "capture"):
    conn.execute(
        "INSERT INTO runs (run_id, run_type, lifecycle, outcome, config_commit, config_tarball_sha256, "
        "snapshot_set_sha256, harness_sha, authorization_ref, budget_sec, started_at, finished_at) "
        "VALUES (?, 'calibration', 'finished', 'complete_with_env_errors', 'c', 'a', 'b', 'h', 'auth', 1, ?, ?)",
        (run_id, started, started))
    for i, n in enumerate(sizes):
        sha = f"{run_id}-{i}".encode().hex().ljust(64, "0")[:64]
        conn.execute("INSERT INTO artifacts (sha256, kind, media_type, bytes, redaction_passed, set_path, created_at) "
                     "VALUES (?, ?, 'image/png', ?, 1, ?, ?)", (sha, kind, n, f"runs/{run_id}", started))
        conn.execute("INSERT INTO artifact_sets (sha256, set_path, created_at) VALUES (?, ?, ?)",
                     (sha, f"runs/{run_id}", started))


def test_mean_capture_size_comes_from_the_last_calibration_run(stores):
    s, _db, _root = stores
    assert mean_capture_bytes(s.conn).bytes == PLANNING_CAPTURE_BYTES          # nothing measured yet
    _calibration(s.conn, "cal-1", "2026-10-03T02:00:00+00:00", [1_000_000, 3_000_000])
    _calibration(s.conn, "cal-2", "2026-11-07T02:00:00+00:00", [1_500_000, 1_700_000])
    _calibration(s.conn, "cal-3", "2026-11-08T02:00:00+00:00", [9_000_000], kind="video")   # no captures
    m = mean_capture_bytes(s.conn)
    assert (m.bytes, m.run_id) == (1_600_000, "cal-2") and "2 captures" in m.source


# -- the 70 percent alarm --------------------------------------------------------------------------------------
def test_usage_alarm_at_70_percent():
    assert usage_alarm(1000, 290).priority == "P2"        # 71 percent used
    assert usage_alarm(1000, 310) is None                 # 69 percent used
