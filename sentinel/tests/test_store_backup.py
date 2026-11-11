"""The nightly backup (8.1, 8.6, 8.9; gap 08-backup-job): VACUUM INTO, JSONL exports, the mirror, 7 daily and 8
weekly snapshots, the weekly integrity check, last_backup_at and last_backup_error."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.store import ArtifactStore, ManifestHeader, ScopeEntry, Store
from sentinel.store import backup as bk
from sentinel.store.backup import (EXPORT_TABLES, IntegrityResult, backup_ok, keep_snapshots, main, prune_backups,
                                   run_backup, weekly_integrity)
from sentinel.store.health import INTEGRITY_PREFIX, store_health
from sentinel.store.retention import Candidate, sweep
from sentinel.store.writer import WriterService

NOW = "2026-11-11T07:00:00+00:00"                  # 02:00 America/New_York
STAMP = "20261111T070000Z"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
RUN = "run-01"
TS = "2026-11-01T12:00:00+00:00"


def put(arts, set_path, body: bytes, name: str):
    return ArtifactStore.at(arts, set_path).put(body, name=name, kind="ocr_json", media_type="application/json")


def index(s, a, set_path, created_at=TS):
    if not s.query("SELECT 1 FROM artifacts WHERE sha256 = ?", (a.sha256,)):
        s.insert("artifacts", {"sha256": a.sha256, "kind": a.kind, "media_type": a.media_type, "bytes": a.bytes,
                               "redaction_passed": 1, "set_path": set_path, "first_run_id": RUN,
                               "created_at": created_at})
    s.write_artifact_set(a.sha256, set_path, created_at=created_at)


def seed(s, arts):
    """A run with a decision, a baseline (its image in the run set and the baseline set), a pinned finding object,
    an unpinned object, and a row in every exported table."""
    s.register_build("b1", channel="main", source_commit="b1", pipeline_artifact_url="u", installer_sha256="b1",
                     first_seen_at=TS)
    s.register_checkpoints([ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen")], config_commit="c0ffee")
    s.insert("runs", {"run_id": RUN, "run_type": "merge", "lifecycle": "finished", "outcome": "complete",
                      "config_commit": "c0ffee", "config_tarball_sha256": "a" * 64, "snapshot_set_sha256": "b" * 64,
                      "harness_sha": "d00d", "authorization_ref": "auth/v1.md", "budget_sec": 2700, "started_at": TS,
                      "finished_at": TS})
    s.insert("decisions", {"decision_id": 1, "checkpoint_id": CENSUS, "region_id": "rgn.ladder.bid-column",
                           "phash": bytes.fromhex("0123456789abcdef"), "category": "noise",
                           "action": "noise_signature", "verdict": "noise", "source": "report_ui",
                           "decided_by": "alice", "decided_at": TS, "run_id": RUN, "reason": "clock",
                           "expires_at": "2026-12-01T12:00:00+00:00"})
    image = put(arts, f"runs/{RUN}", b'{"image": 1}', "cp/candidate/capture.json")
    put(arts, "baselines/released/b1", b'{"image": 1}', "cp/baseline.json")
    ArtifactStore.at(arts, "baselines/released/b1").write_manifest(
        ManifestHeader(config_commit="c0ffee", harness_commit="d00d", build_id="b1"))
    evidence = put(arts, f"runs/{RUN}", b'{"crop": 1}', "cp/crop.json")
    loose = put(arts, f"runs/{RUN}", b'{"loose": 1}', "cp/sidecar.json")
    for a in (image, evidence, loose):
        index(s, a, f"runs/{RUN}")
    index(s, image, "baselines/released/b1")
    s.insert("baselines", {"checkpoint_id": CENSUS, "lineage": "released", "build_id": "b1",
                           "image_sha256": image.sha256, "state": "approved", "source_run_id": RUN,
                           "proposed_at": TS, "approved_by": "bob", "approved_at": TS, "approval_reason": "ok"})
    s.insert("findings", {"natural_key": "F-1", "run_id": RUN, "checkpoint_id": CENSUS, "kind": "collateral",
                          "description": "overlap", "evidence_json": json.dumps({"artifacts": [evidence.sha256]}),
                          "created_at": TS})
    s.insert("masks", {"mask_id": "m-1", "config_commit": "c0ffee", "checkpoint_id": CENSUS, "landmark_id": "lm",
                       "dx": 0, "dy": 0, "w": 10, "h": 10, "owner": "qa", "reason": "clock", "created_by": "carol",
                       "created_by_kind": "human", "created_at": TS, "expires_at": "2026-12-01T12:00:00+00:00"})
    s.insert("flake_stats", {"checkpoint_id": CENSUS, "observations": 5, "updated_at": TS})
    s.insert("incidents", {"run_id": RUN, "severity": "S3", "trigger": "drop", "opened_at": TS})
    s.insert("safety_quarantines", {"target_kind": "flow", "target": "flow.trade", "reason": "flake",
                                    "opened_at": TS})
    return image, evidence, loose


@pytest.fixture
def env(tmp_path):
    arts, root = tmp_path / "artifacts", tmp_path / "backup"
    s = Store(tmp_path / "db" / "central.db")
    objs = seed(s, arts)
    yield s, arts, root, objs
    s.close()


def dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                        "'sqlite_%' AND name <> 'system_state' ORDER BY name")]
    return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n} ORDER BY 1, 2")] for n in names}


def test_the_snapshot_opens_and_equals_the_live_database(env):
    s, arts, root, _ = env
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert res.ok and res.error is None and res.alert is None and res.integrity_checked
    assert res.snapshot == root / "snapshots" / f"central-{STAMP}.db"
    snap = sqlite3.connect(res.snapshot)
    assert dump(snap) == dump(s.conn)
    assert snap.execute("PRAGMA user_version").fetchone()[0] == s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert s.get_system_state("last_backup_at") == NOW
    assert s.get_system_state("last_integrity_check_at") == NOW
    assert not (s.path.parent / "backup-staging").exists()           # the local staging copy is gone once mirrored


def test_the_exports_have_one_line_per_row(env):
    s, arts, root, _ = env
    res = run_backup(s, root, NOW, artifact_root=arts)
    for table in EXPORT_TABLES:
        lines = (root / "exports" / STAMP / f"{table}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == res.exports[table] == s.query(f"SELECT count(*) FROM {table}")[0][0] == 1
    decision = json.loads((root / "exports" / STAMP / "decisions.jsonl").read_text())
    assert decision["phash"] == "0123456789abcdef" and decision["action"] == "noise_signature"


def test_the_mirror_holds_all_of_baselines_and_only_the_pinned_run_objects(env):
    s, arts, root, (image, evidence, loose) = env
    run_backup(s, root, NOW, artifact_root=arts)
    base = root / "baselines" / "released" / "b1"
    assert (base / "manifest.json").read_bytes() == (arts / "baselines" / "released" / "b1" / "manifest.json"
                                                     ).read_bytes()
    assert (base / "objects" / image.sha256[:2] / f"{image.sha256}.json").exists()
    run_objects = root / "runs" / RUN / "objects"
    assert (run_objects / image.sha256[:2] / f"{image.sha256}.json").exists()       # pinned by the baseline
    assert (run_objects / evidence.sha256[:2] / f"{evidence.sha256}.json").exists()  # pinned by the open finding
    assert not (run_objects / loose.sha256[:2]).exists()                             # not pinned: not mirrored


def test_a_failing_destination_writes_last_backup_error_and_the_sweep_is_skipped(env, tmp_path):
    s, arts, _, (_, _, loose) = env
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the backup location should be")
    res = run_backup(s, blocked, NOW, artifact_root=arts)
    assert not res.ok and res.alert == store_health() and not res.stop_service
    assert s.get_system_state("last_backup_error") == res.error and "Error" in res.error
    assert s.get_system_state("last_backup_at") is None
    assert not backup_ok(s, NOW)
    result = sweep(s, arts, [Candidate(loose.sha256, f"runs/{RUN}", "ocr_json", "merge run: 14 days", NOW)],
                   backup_ok=backup_ok(s, NOW), now=NOW)
    assert result.skipped and result.deleted == () and result.health_rows() == [
        ("Retention", "retention skipped: backup failed")]
    assert ArtifactStore.at(arts, f"runs/{RUN}").verify(loose.sha256)


def test_backup_ok_needs_a_recent_success_and_no_error_since(env):
    s, arts, root, _ = env
    assert not backup_ok(s, NOW)
    run_backup(s, root, NOW, artifact_root=arts)
    assert backup_ok(s, NOW)
    assert not backup_ok(s, "2026-11-12T19:00:01+00:00")                 # 36 h and one second later
    s.set_system_state("last_backup_error", "OSError: x", at="2026-11-11T08:00:00+00:00")
    assert not backup_ok(s, "2026-11-11T09:00:00+00:00")


def test_the_sweep_needs_tonights_backup_not_just_a_recent_one(env):
    """w4-store-maint:5: yesterday's success does not cover a night whose 02:00 job left no record."""
    s, arts, root, (_, _, loose) = env
    assert run_backup(s, root, "2026-11-10T07:00:00+00:00", artifact_root=arts).ok     # yesterday 02:00 New York
    assert backup_ok(s, "2026-11-10T08:00:00+00:00")                                  # yesterday's 03:00 sweep
    tonight = "2026-11-11T08:00:00+00:00"                                             # tonight's job: no record
    assert not backup_ok(s, tonight)
    res = sweep(s, arts, [Candidate(loose.sha256, f"runs/{RUN}", "ocr_json", "merge run: 14 days", tonight)],
                backup_ok=backup_ok(s, tonight), now=tonight)
    assert res.skipped and ArtifactStore.at(arts, f"runs/{RUN}").verify(loose.sha256)
    assert run_backup(s, root, "2026-11-11T06:58:00+00:00", artifact_root=arts).ok     # a scheduler running early
    assert backup_ok(s, tonight)


def test_a_pinned_object_missing_from_the_tree_fails_the_night_unless_the_audit_recorded_it(env):
    s, arts, root, (_, evidence, _) = env
    (arts / "runs" / RUN / "objects" / evidence.sha256[:2] / f"{evidence.sha256}.json").unlink()
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert not res.ok and f"pinned object {evidence.sha256}" in res.error
    s.update("artifacts", {"sha256": evidence.sha256}, {"audit_state": "missing"})
    res = run_backup(s, root, "2026-11-12T07:00:00+00:00", artifact_root=arts)
    assert res.ok and res.known_missing == (f"runs/{RUN}/{evidence.sha256}",)


def test_a_corrupt_pinned_object_never_keeps_the_others_from_the_mirror(env):
    """w4-store-maint:0: a pinned copy that does not hash to its name (never mirrored) fails the night only until
    the audit records it, and every other pinned object is mirrored in any case."""
    s, arts, root, (_, evidence, _) = env
    later = put(arts, "runs/run-02", b'{"later": 1}', "cp/crop.json")          # sorts after runs/run-01
    index(s, later, "runs/run-02")
    s.insert("findings", {"natural_key": "F-2", "run_id": RUN, "checkpoint_id": CENSUS, "kind": "collateral",
                          "description": "x", "evidence_json": json.dumps({"artifacts": [later.sha256]}),
                          "created_at": TS})
    rotted = arts / "runs" / RUN / "objects" / evidence.sha256[:2] / f"{evidence.sha256}.json"
    rotted.write_bytes(b'{"crop": 2}')                                         # same size, one byte flipped
    mirrored_later = root / "runs" / "run-02" / "objects" / later.sha256[:2] / f"{later.sha256}.json"
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert not res.ok and f"does not hash to {evidence.sha256}" in res.error   # not recorded yet: the night fails
    assert mirrored_later.exists()                                             # ... after mirroring the rest
    s.update("artifacts", {"sha256": evidence.sha256}, {"audit_state": "corrupt"})   # the audit recorded it
    res = run_backup(s, root, "2026-11-12T07:00:00+00:00", artifact_root=arts)
    assert res.ok and res.known_missing == (f"runs/{RUN}/{evidence.sha256}",)
    assert not (root / "runs" / RUN / "objects" / evidence.sha256[:2] / f"{evidence.sha256}.json").exists()
    assert backup_ok(s, "2026-11-12T07:30:00+00:00")


def test_the_integrity_check_runs_weekly_on_the_snapshot(env, monkeypatch):
    s, arts, root, _ = env
    assert run_backup(s, root, NOW, artifact_root=arts).integrity_checked
    assert not run_backup(s, root, "2026-11-12T07:00:00+00:00", artifact_root=arts).integrity_checked
    assert s.get_system_state("last_integrity_check_at") == NOW
    monkeypatch.setattr(bk, "weekly_integrity", lambda path: IntegrityResult(False, ("page 7: btree corrupt",)))
    res = run_backup(s, root, "2026-11-18T07:00:00+00:00", artifact_root=arts)
    assert not res.ok and res.integrity_checked and res.stop_service
    assert res.error == INTEGRITY_PREFIX + "page 7: btree corrupt"
    assert s.get_system_state("last_backup_error") == res.error
    assert not (root / "snapshots" / "central-20261118T070000Z.db").exists()   # a failing snapshot is not mirrored
    assert res.snapshot is not None and res.snapshot.exists()                    # kept locally for diagnosis


def test_weekly_integrity_reads_the_snapshot(env, tmp_path):
    s, arts, root, _ = env
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert weekly_integrity(res.snapshot) == IntegrityResult(True)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    assert not weekly_integrity(bad).ok


def _stamps(days: int, start=datetime(2026, 11, 11, 7, tzinfo=timezone.utc)) -> list[str]:
    return [(start - timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ") for i in range(days)]


def test_seven_daily_and_eight_weekly_snapshots_are_kept():
    stamps = _stamps(120)
    keep = keep_snapshots(stamps)
    assert len(keep) == 15
    assert set(stamps[:7]) <= keep                                     # the seven newest days
    weekly = sorted(keep - set(stamps[:7]), reverse=True)
    weeks = [datetime.strptime(w, "%Y%m%dT%H%M%SZ").isocalendar()[:2] for w in weekly]
    assert len(set(weeks)) == 8
    for w in weekly:                                                   # each the newest of its ISO week
        week = datetime.strptime(w, "%Y%m%dT%H%M%SZ").isocalendar()[:2]
        newer = [x for x in stamps[7:] if datetime.strptime(x, "%Y%m%dT%H%M%SZ").isocalendar()[:2] == week]
        assert w == max(newer)
    # a second snapshot on a kept day keeps only the newest of that day
    assert "20261111T050000Z" not in keep_snapshots(stamps + ["20261111T050000Z"])


def test_pruning_removes_snapshots_and_exports_together(tmp_path):
    stamps = _stamps(40)
    for st in stamps:
        (tmp_path / "snapshots").mkdir(exist_ok=True)
        (tmp_path / "snapshots" / f"central-{st}.db").write_bytes(b"x")
        (tmp_path / "exports" / st).mkdir(parents=True)
    (tmp_path / "snapshots" / "central-notes.db").write_bytes(b"left alone")
    gone = prune_backups(tmp_path)
    keep = keep_snapshots(stamps)
    assert set(gone) == set(stamps) - keep
    assert {p.name for p in (tmp_path / "snapshots").iterdir()} == {f"central-{k}.db" for k in keep} | {
        "central-notes.db"}
    assert {p.name for p in (tmp_path / "exports").iterdir()} == keep


def test_the_backup_prunes_the_location_after_a_good_night(env):
    s, arts, root, _ = env
    for st in _stamps(20, datetime(2026, 11, 10, 7, tzinfo=timezone.utc)):
        (root / "snapshots").mkdir(parents=True, exist_ok=True)
        (root / "snapshots" / f"central-{st}.db").write_bytes(b"x")
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert res.ok and len(list((root / "snapshots").iterdir())) == len(keep_snapshots(
        _stamps(20, datetime(2026, 11, 10, 7, tzinfo=timezone.utc)) + [STAMP]))
    assert (root / "snapshots" / f"central-{STAMP}.db").exists()


def test_the_backup_command(tmp_path, capsys):
    arts, root, db = tmp_path / "artifacts", tmp_path / "backup", tmp_path / "db" / "central.db"
    with Store(db) as s:
        seed(s, arts)
    assert main(["--db", str(db), "--backup-root", str(root), "--artifact-root", str(arts), "--now", NOW]) == 0
    assert f"backup {STAMP}" in capsys.readouterr().out
    blocked = tmp_path / "blocked"
    blocked.write_text("x")
    assert main(["--db", str(db), "--backup-root", str(blocked), "--artifact-root", str(arts),
                 "--now", "2026-11-12T07:00:00+00:00"]) == 1
    assert "failed" in capsys.readouterr().err
    assert main(["--db", str(tmp_path / "none.db"), "--backup-root", str(root), "--artifact-root", str(arts)]) == 2


def test_the_backup_command_while_the_service_holds_the_writer(tmp_path, capsys):
    """w4-store-maint:4: the Reporter service holds writer.lock (8.2): the command writes nothing and exits 4 (no
    traceback), a failed night the scheduler alerts on, and tonight's sweep is skipped."""
    arts, root, db = tmp_path / "artifacts", tmp_path / "backup", tmp_path / "db" / "central.db"
    with Store(db) as service:
        seed(service, arts)
        assert main(["--db", str(db), "--backup-root", str(root), "--artifact-root", str(arts), "--now", NOW]) == 4
        assert "nothing was written" in capsys.readouterr().err
        assert service.get_system_state("last_backup_at") is None
        assert not backup_ok(service, "2026-11-11T08:00:00+00:00")


def test_the_backup_runs_through_the_services_writer_and_leaves_it_free_while_copying(tmp_path, monkeypatch):
    """w4-store-maint:4: while the service runs, the backup takes its writer (8.2) only for short units, so the
    writer answers other work during the copy (8.6: "while the service runs")."""
    arts, root, db = tmp_path / "artifacts", tmp_path / "backup", tmp_path / "db" / "central.db"
    with Store(db) as s:
        seed(s, arts)
    with WriterService(lambda: Store(db)) as service:
        copy, answered = bk.mirror_tree, []

        def copying(src, dst):
            answered.append(service.call(lambda st: st.get_system_state("last_backup_at"), timeout=10))
            return copy(src, dst)

        monkeypatch.setattr(bk, "mirror_tree", copying)
        res = run_backup(service, root, NOW, artifact_root=arts)
        assert res.ok and answered == [None, None]
        assert service.call(lambda st: st.get_system_state("last_backup_at")) == NOW
    assert (root / "snapshots" / f"central-{STAMP}.db").exists() and (root / "runs" / RUN / "objects").is_dir()


def test_any_failure_writes_last_backup_error(env, monkeypatch):
    """w4-store-maint:4: 8.6 "on any failure it writes last_backup_error", not only OS and store errors."""
    s, arts, root, _ = env

    def broken(snapshot, directory):
        raise ValueError("a row the export did not expect")

    monkeypatch.setattr(bk, "export_tables", broken)
    res = run_backup(s, root, NOW, artifact_root=arts)
    assert not res.ok and res.error == "ValueError: a row the export did not expect" and res.alert == store_health()
    assert s.get_system_state("last_backup_error") == res.error and not backup_ok(s, NOW)
