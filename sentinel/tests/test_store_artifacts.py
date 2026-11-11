"""Tests for sentinel.store.artifacts (8.4) and the store's artifact rows and retention (8.2, 8.6), the plaintext
put hook (6.9) and quarantine (8.4, 8.9)."""
import io
import json

import pytest
from PIL import Image

from sentinel.records import CheckpointOutcome, Finding
from sentinel.store import (ArtifactCorrupt, ArtifactError, ArtifactHashMismatch, ArtifactPersistError, ArtifactStore,
                            ManifestHeader, Redaction, RedactionRefused, RunPlan, ScopeEntry, Store)
from sentinel.store import artifacts as artifacts_mod
from sentinel.store.artifacts import ArtifactQuarantined, SecretLeaked, sha256_hex
from sentinel.store.secret_scan import SecretScanner

CID = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
RED = Redaction("redact-v3")


def png(color=(10, 20, 30), size=(8, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def arts(tmp_path):
    return ArtifactStore(tmp_path / "shard", "run-01/1")


def put_capture(arts, data=None, name="census/x/candidate/capture.png", **kw):
    return arts.put(data or png(), name=name, kind="capture", media_type="image/png", redaction=RED,
                    checkpoint_id=CID, side="candidate", **kw)


def test_round_trip_layout_and_manifest(arts, tmp_path):
    data = png()
    a = put_capture(arts, data)
    assert a.sha256 == sha256_hex(data) and (a.width, a.height) == (8, 6)
    assert a.path == str(tmp_path / "shard" / "objects" / a.sha256[:2] / f"{a.sha256}.png")
    assert arts.get(a.sha256) == data and arts.verify(a.sha256)
    assert put_capture(arts, data) == a                                   # idempotent
    log = arts.put(b"line\n", name="logs/watchdog.txt", kind="log_slice", media_type="text/plain")
    assert log.redaction_version is None                                 # text goes to the plaintext scan (6.9)
    arts.write_manifest(ManifestHeader(config_commit="c0ffee", harness_commit="d00d", run_id="run-01",
                                       shard_id="run-01/1"))
    mf = ArtifactStore.read_manifest(tmp_path / "shard")
    assert mf["header"]["run_id"] == "run-01"
    assert [e["name"] for e in mf["entries"]] == ["census/x/candidate/capture.png", "logs/watchdog.txt"]
    assert mf["entries"][0]["redaction_version"] == "redact-v3"
    again = ArtifactStore(tmp_path / "shard", "run-01/1")                 # a resumed writer keeps the entries
    assert again.entries == arts.entries
    with pytest.raises(ArtifactError):
        put_capture(arts, png((1, 1, 1)))                                # one name, one hash


def test_image_without_redaction_header_refused(arts):
    with pytest.raises(RedactionRefused):
        arts.put(png(), name="a.png", kind="capture", media_type="image/png")
    with pytest.raises(RedactionRefused):
        arts.put(png(), name="a.png", kind="capture", media_type="image/png", redaction=Redaction("v3", passed=False))
    with pytest.raises(RedactionRefused):                                # a PNG declared as text is still an image
        arts.put(png(), name="a.txt", kind="log_slice", media_type="text/plain")
    assert not any(arts.objects.rglob("*.png"))


def test_hash_mismatch_refused_and_corruption_detected(arts):
    data = png()
    with pytest.raises(ArtifactHashMismatch):
        put_capture(arts, data, expected_sha256="0" * 64)
    assert arts.path_of(sha256_hex(data)) is None
    a = put_capture(arts, data, expected_sha256=sha256_hex(data))
    raw = bytearray(open(a.path, "rb").read())
    raw[-5] ^= 0xFF                                                       # one flipped byte on disk
    open(a.path, "wb").write(raw)
    with pytest.raises(ArtifactCorrupt):
        arts.get(a.sha256)
    assert not arts.verify(a.sha256)
    assert put_capture(arts, data).sha256 == a.sha256 and arts.verify(a.sha256)   # the bad copy is replaced


def test_read_back_retried_once_then_persist_error(arts, monkeypatch):
    real, calls = artifacts_mod._read_back, []

    def flaky(path, fail_times):
        calls.append(path)
        return b"garbage" if len(calls) <= fail_times else real(path)

    monkeypatch.setattr(artifacts_mod, "_read_back", lambda p: flaky(p, 1))
    a = put_capture(arts)
    assert arts.verify(a.sha256) and len(calls) == 2
    calls.clear()
    monkeypatch.setattr(artifacts_mod, "_read_back", lambda p: flaky(p, 2))
    with pytest.raises(ArtifactPersistError):
        put_capture(arts, png((5, 5, 5)), name="other.png")
    assert not any(p.name.endswith(".tmp") for p in arts.objects.rglob("*"))


def test_store_indexes_artifacts_and_sweeps_safely(tmp_path):
    arts = ArtifactStore(tmp_path / "objects-set", "runs/run-01")
    with Store(tmp_path / "db" / "run.db") as s:
        s.begin_run(RunPlan("run-01", "merge", "c0ffee", "a" * 64, "b" * 64, "d00d", "auth/v1.md", 2700,
                            scope=(ScopeEntry(CID, "ws.tier-a.active-trader", "active_trader", 1, "frozen"),)))
        cand, base, kept = put_capture(arts), put_capture(arts, png((9, 9, 9)), name="base.png"), \
            put_capture(arts, png((7, 7, 7)), name="evidence.png")
        o = CheckpointOutcome(run_id="run-01", checkpoint_id=CID, outcome="collateral", workspace_id="ws.tier-a."
                              "active-trader", persona="active_trader", data_mode="frozen",
                              artifacts={"candidate": cand.sha256, "base": base.sha256})
        f = Finding("F-1", "run-01", "collateral", "overlap", "run-01", checkpoint_id=CID,
                    evidence={"artifacts": [kept.sha256]})
        s.write_checkpoint(o, artifacts=[cand, base, kept], findings=[f])
        row = s.conn.execute("SELECT kind, redaction_passed, redaction_version, width FROM artifacts WHERE sha256 = ?",
                             (cand.sha256,)).fetchone()
        assert tuple(row) == ("capture", 1, "redact-v3", 8)
        assert s.sweep(arts, [cand.sha256], backup_ok=False).skipped               # 8.6: backup failed
        res = s.sweep(arts, [cand.sha256, kept.sha256], backup_ok=True)
        assert res.deleted == (cand.sha256,) and res.kept == (kept.sha256,)       # open finding pins evidence
        assert arts.path_of(cand.sha256) is None
        deleted_at = s.conn.execute("SELECT deleted_at FROM artifacts WHERE sha256 = ?", (cand.sha256,)).fetchone()
        assert deleted_at[0] is not None
        ledger = [json.loads(x)["batch"] for x in (tmp_path / "db" / "results.jsonl").read_text().splitlines()]
        assert ledger[-1] == "retention"


# -- the plaintext put hook (6.9) and quarantine (8.4, 8.9) ---------------------------------------------------------

SECRET = "hunter2-Qx9!"


def test_a_text_artifact_holding_a_secret_is_refused_before_it_is_written(tmp_path):
    arts = ArtifactStore(tmp_path / "shard", "run-01/1", scanner=SecretScanner({"login.password": SECRET}))
    with pytest.raises(SecretLeaked) as e:
        arts.put(f"request: {SECRET}".encode("utf-16-le"), name="reviewer/req.json", kind="request",
                 media_type="application/json")
    assert e.value.refs == ("login.password",) and SECRET not in str(e.value)
    assert not any(p.is_file() for p in arts.objects.rglob("*")) and arts.entries == []
    assert arts.put(b"clean log\n", name="logs/a.txt", kind="log_slice", media_type="text/plain").bytes == 10
    assert put_capture(arts).kind == "capture"                           # images go through redaction, not the scan


def test_quarantine_moves_the_set_marks_its_rows_and_refuses_reads_and_writes(tmp_path):
    arts = ArtifactStore(tmp_path / "artifacts" / "runs" / "run-01", "runs/run-01")
    with Store(tmp_path / "db" / "run.db") as s:
        s.begin_run(RunPlan("run-01", "merge", "c0ffee", "a" * 64, "b" * 64, "d00d", "auth/v1.md", 2700,
                            scope=(ScopeEntry(CID, "ws.tier-a.active-trader", "active_trader", 1, "frozen"),)))
        cand = put_capture(arts)
        log = arts.put(f"pw={SECRET}".encode(), name="logs/leak.txt", kind="log_slice", media_type="text/plain")
        arts.write_manifest(ManifestHeader(config_commit="c0ffee", harness_commit="d00d", run_id="run-01"))
        (arts.root / "shards" / "1").mkdir(parents=True)
        (arts.root / "shards" / "1" / "results.jsonl").write_text('{"batch": "events"}\n')
        o = CheckpointOutcome(run_id="run-01", checkpoint_id=CID, outcome="identical", workspace_id="ws.tier-a."
                              "active-trader", persona="active_trader", data_mode="frozen",
                              artifacts={"candidate": cand.sha256})
        s.write_checkpoint(o, artifacts=[cand, log])
        for a in (cand, log):
            s.write_artifact_set(a.sha256, "runs/run-01")
        s.write_artifact_set(cand.sha256, "baselines/main/b1")             # another set's copy stays untouched
        qroot = tmp_path / "artifacts" / "quarantine"
        (qroot / "run-01").mkdir(parents=True)
        (qroot / "run-01" / "manifest.json").write_text("{}")             # a different file already there
        dest = arts.quarantine("run-01", qroot, store=s, at="2026-11-10T09:00:00-05:00")
        assert dest == qroot / "run-01"
        moved = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        assert moved == sorted([f"objects/{cand.sha256[:2]}/{cand.sha256}.png",
                                f"objects/{log.sha256[:2]}/{log.sha256}.txt",
                                "manifest.json", "manifest.json.1", "shards/1/results.jsonl"])
        assert [p.name for p in arts.root.rglob("*") if p.is_file()] == ["QUARANTINED.json"]
        assert arts.quarantined["quarantined_at"] == "2026-11-10T14:00:00+00:00"
        rows = dict(s.conn.execute("SELECT set_path || ':' || sha256, quarantined_at FROM artifact_sets").fetchall())
        assert rows[f"runs/run-01:{cand.sha256}"] == rows[f"runs/run-01:{log.sha256}"] == "2026-11-10T14:00:00+00:00"
        assert rows[f"baselines/main/b1:{cand.sha256}"] is None
    with pytest.raises(ArtifactQuarantined):
        arts.get(cand.sha256)
    with pytest.raises(ArtifactQuarantined):
        arts.put(b"more\n", name="logs/b.txt", kind="log_slice", media_type="text/plain")
    with pytest.raises(ArtifactQuarantined):                             # a reopened set is still quarantined
        ArtifactStore(arts.root, "runs/run-01").get(cand.sha256)
    assert arts.path_of(cand.sha256) is None and not arts.unlink(cand.sha256)
