"""The weekly artifact audit (8.6, 8.7, 8.9, SD-82; gap 08-artifact-audit): pinned objects and a seeded 5 percent
sample re-hashed per set, artifact_missing findings with the checkpoint's tier, and pinned objects restored."""
from __future__ import annotations

import json
import random

import pytest

from sentinel.store import ArtifactStore, Store
from sentinel.store.audit import main, run_audit
from sentinel.store.backup import run_backup
from sentinel.store.retention import live_copies
from sentinel.store.writer import WriterService

TS = "2026-11-01T12:00:00+00:00"
NOW = "2026-11-15T09:00:00+00:00"
RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"          # tier 1
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"    # tier 2
SEED = 20261115


class Env:
    def __init__(self, tmp_path, loose=40):
        self.arts, self.backup = tmp_path / "artifacts", tmp_path / "backup"
        self.s = s = Store(tmp_path / "db" / "central.db")
        s.insert("runs", {"run_id": RUN, "run_type": "merge", "lifecycle": "finished", "outcome": "complete",
                          "config_commit": "c0ffee", "config_tarball_sha256": "a" * 64,
                          "snapshot_set_sha256": "b" * 64, "harness_sha": "d00d", "authorization_ref": "auth",
                          "budget_sec": 2700, "started_at": TS, "finished_at": TS})
        for cp, kind, tier in ((CENSUS, "census", 1), (SWEEP, "sweep", 2)):
            s.insert("checkpoints", {"checkpoint_id": cp, "kind": kind, "surface_id": "s", "workspace_id": WS,
                                     "tier": tier, "data_mode": "frozen", "first_seen_commit": "c",
                                     **({"host": "flexgrid", "size_w": 480, "size_h": 360} if kind == "sweep"
                                        else {})})
        self.pinned = self.obj(f"runs/{RUN}", "pinned")
        self.loose = [self.obj(f"runs/{RUN}", f"loose-{i}") for i in range(loose)]
        for cp, sha in ((SWEEP, self.pinned), (CENSUS, self.pinned)):   # named by a tier-2 and a tier-1 outcome
            s.insert("outcomes", {"run_id": RUN, "checkpoint_id": cp, "outcome": "identical",
                                  "cand_capture_sha256": sha, "recorded_at": TS})
        s.insert("findings", {"natural_key": "F-1", "run_id": RUN, "checkpoint_id": CENSUS, "kind": "collateral",
                              "description": "x", "evidence_json": json.dumps({"artifacts": [self.pinned]}),
                              "created_at": TS})

    def obj(self, set_path, label):
        a = ArtifactStore.at(self.arts, set_path).put(label.encode(), name=label, kind="ocr_json",
                                                      media_type="application/json")
        if not self.s.query("SELECT 1 FROM artifacts WHERE sha256 = ?", (a.sha256,)):
            self.s.insert("artifacts", {"sha256": a.sha256, "kind": "capture", "media_type": a.media_type,
                                        "bytes": a.bytes, "redaction_passed": 1, "set_path": set_path,
                                        "first_run_id": RUN, "created_at": TS})
        self.s.write_artifact_set(a.sha256, set_path, created_at=TS)
        return a.sha256

    def file(self, sha, set_path=f"runs/{RUN}"):
        return ArtifactStore.at(self.arts, set_path).path_of(sha)

    def expected_sample(self, seed=SEED):
        rest = [c for c in live_copies(self.s) if not c.pinned and not c.quarantined]
        return random.Random(seed).sample(rest, -(-len(rest) * 5 // 100))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.s.close()


def findings(s):
    return {r["natural_key"]: r for r in s.query("SELECT * FROM findings WHERE kind = 'artifact_missing'")}


def test_a_deleted_pinned_object_is_restored_and_a_corrupt_sampled_one_is_recorded(env):
    assert run_backup(env.s, env.backup, "2026-11-15T07:00:00+00:00", artifact_root=env.arts).ok
    sampled = env.expected_sample()[0]
    env.file(env.pinned).unlink()
    env.file(sampled.sha256).write_bytes(b"bit rot")
    res = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)
    assert (res.pinned, res.sampled) == (1, 2)
    by_sha = {f.sha256: f for f in res.failures}
    assert set(by_sha) == {env.pinned, sampled.sha256}
    assert by_sha[env.pinned].state == "missing" and by_sha[env.pinned].restored_from is not None
    assert by_sha[sampled.sha256].state == "corrupt" and by_sha[sampled.sha256].restored_from is None
    assert ArtifactStore.at(env.arts, f"runs/{RUN}").verify(env.pinned)          # restored from the mirror
    state = {r["sha256"]: (r["audit_state"], r["last_audit_at"]) for r in env.s.query(
        "SELECT sha256, audit_state, last_audit_at FROM artifacts")}
    assert state[env.pinned] == ("ok", NOW) and state[sampled.sha256] == ("corrupt", NOW)
    assert env.s.get_system_state("last_artifact_audit_at") == NOW
    got = findings(env.s)
    pinned_f = got[by_sha[env.pinned].finding_id]
    assert (pinned_f["run_id"], pinned_f["checkpoint_id"], pinned_f["tier"]) == (RUN, CENSUS, 1)   # lowest tier
    assert pinned_f["triage_state"] == "open" and "restored from the backup mirror" in pinned_f["description"]
    assert json.loads(pinned_f["evidence_json"])["restored_from"] == by_sha[env.pinned].restored_from
    loose_f = got[by_sha[sampled.sha256].finding_id]
    assert (loose_f["run_id"], loose_f["checkpoint_id"], loose_f["tier"]) == (RUN, None, None)
    assert json.loads(loose_f["evidence_json"])["artifacts"] == [sampled.sha256]
    ledger = [json.loads(line) for line in (env.s.path.parent / "results.jsonl").read_text().splitlines()]
    assert {"op": "restore", "sha256": env.pinned, "set_path": f"runs/{RUN}",
            "from": by_sha[env.pinned].restored_from} in ledger[-2]["rows"]
    assert res.health_rows() == [("Artifact audit", "3 copies re-hashed (1 pinned, 2 sampled); 2 missing or "
                                                    "corrupt; 1 restored from the backup mirror")]


def test_the_sample_is_5_percent_and_deterministic_for_a_seed(tmp_path):
    e = Env(tmp_path, loose=41)
    try:
        first = run_audit(e.s, e.arts, e.backup, random.Random(SEED), NOW)
        checked = {r[0] for r in e.s.query("SELECT sha256 FROM artifacts WHERE last_audit_at IS NOT NULL")}
        assert first.sampled == 3 and len(checked) == 4                    # ceil(41 * 0.05) = 3, plus the pinned one
        assert checked == {e.pinned} | {c.sha256 for c in e.expected_sample()}
        e.s.update("artifacts", {"audit_state": "ok"}, {"last_audit_at": None})
        run_audit(e.s, e.arts, e.backup, random.Random(SEED), NOW)
        assert {r[0] for r in e.s.query("SELECT sha256 FROM artifacts WHERE last_audit_at IS NOT NULL")} == checked
    finally:
        e.s.close()


def test_a_pinned_object_without_a_good_mirror_copy_stays_missing(env):
    env.file(env.pinned).unlink()
    res = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)       # no backup yet: nothing to restore
    assert [(f.state, f.restored_from) for f in res.failures] == [("missing", None)]
    assert env.s.query("SELECT audit_state FROM artifacts WHERE sha256 = ?", (env.pinned,))[0][0] == "missing"
    mirror = env.backup / "runs" / RUN / "objects" / env.pinned[:2] / f"{env.pinned}.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes(b"a mirror copy that rotted")
    res = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)
    assert [(f.state, f.restored_from) for f in res.failures] == [("missing", None)]
    assert env.file(env.pinned) is None
    assert len(findings(env.s)) == 1                                   # the same copy keeps its one finding


def test_a_copy_that_fails_again_after_triage_closed_its_finding_reopens_it(env):
    """w4-store-maint:3: every audit failure leaves an open artifact_missing finding that says what was found."""
    assert run_backup(env.s, env.backup, "2026-11-15T07:00:00+00:00", artifact_root=env.arts).ok
    env.file(env.pinned).unlink()
    week1 = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)
    key = week1.failures[0].finding_id
    assert week1.failures[0].restored_from is not None and findings(env.s)[key]["triage_state"] == "open"
    env.s.update("findings", {"natural_key": key}, {"triage_state": "closed", "closed_at": NOW})   # evidence back
    env.file(env.pinned).unlink()                                      # lost again, and the mirror copy too
    (env.backup / "runs" / RUN / "objects" / env.pinned[:2] / f"{env.pinned}.json").unlink()
    week3 = run_audit(env.s, env.arts, env.backup, random.Random(SEED), "2026-11-29T09:00:00+00:00")
    assert [(f.state, f.restored_from, f.finding_id) for f in week3.failures] == [("missing", None, key)]
    f = findings(env.s)[key]
    assert (f["triage_state"], f["closed_at"]) == ("open", None) and len(findings(env.s)) == 1
    assert "restored" not in f["description"] and json.loads(f["evidence_json"])["restored_from"] is None
    run_audit(env.s, env.arts, env.backup, random.Random(SEED), "2026-12-06T09:00:00+00:00")   # still missing
    assert findings(env.s)[key]["description"] == f["description"]


def test_a_baseline_copy_takes_its_baselines_checkpoint_and_source_run(env):
    env.s.insert("builds", {"build_id": "b1", "channel": "main", "source_commit": "b1", "pipeline_artifact_url": "u",
                            "installer_sha256": "b1", "first_seen_at": TS})
    sha = env.obj("baselines/released/b1", "baseline-image")
    env.s.insert("baselines", {"checkpoint_id": SWEEP, "lineage": "released", "build_id": "b1", "image_sha256": sha,
                               "state": "candidate", "source_run_id": RUN, "proposed_at": TS})
    env.file(sha, "baselines/released/b1").unlink()
    res = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)
    f = findings(env.s)[res.failures[0].finding_id]
    assert (f["run_id"], f["checkpoint_id"], f["tier"]) == (RUN, SWEEP, 2)
    assert json.loads(f["evidence_json"])["set_path"] == "baselines/released/b1"


def test_quarantined_copies_are_not_audited(env):
    env.s.update("artifact_sets", {"sha256": env.pinned}, {"quarantined_at": TS})
    env.file(env.pinned).unlink()
    res = run_audit(env.s, env.arts, env.backup, random.Random(SEED), NOW)
    assert res.pinned == 0 and res.failures == ()


def test_the_audit_command(tmp_path, capsys):
    e = Env(tmp_path, loose=0)
    db = e.s.path
    e.s.close()
    args = ["--db", str(db), "--artifact-root", str(e.arts), "--backup-root", str(e.backup), "--now", NOW]
    assert main(args) == 0
    assert "1 copies re-hashed (1 pinned, 0 sampled); 0 missing" in capsys.readouterr().out
    ArtifactStore.at(e.arts, f"runs/{RUN}").path_of(e.pinned).unlink()
    assert main(args) == 1
    assert f"missing runs/{RUN} {e.pinned} finding fnd-{RUN}-artifact-missing" in capsys.readouterr().out
    assert main(["--db", str(tmp_path / "none.db"), "--artifact-root", "a", "--backup-root", "b"]) == 2


def test_the_audit_runs_through_the_services_writer(tmp_path, capsys):
    """w4-store-maint:4: while the Reporter service holds writer.lock (8.2) the command writes nothing and exits 4;
    the service runs the audit with its writer."""
    e = Env(tmp_path, loose=0)
    db = e.s.path
    e.s.close()
    ArtifactStore.at(e.arts, f"runs/{RUN}").path_of(e.pinned).unlink()
    with WriterService(lambda: Store(db)) as service:
        assert main(["--db", str(db), "--artifact-root", str(e.arts), "--backup-root", str(e.backup),
                     "--now", NOW]) == 4
        assert "nothing was written" in capsys.readouterr().err
        res = run_audit(service, e.arts, e.backup, random.Random(SEED), NOW)
        assert [(f.state, f.finding_id) for f in res.failures] == [
            ("missing", f"fnd-{RUN}-artifact-missing-runs/{RUN}-{e.pinned}")]
        assert service.call(lambda st: st.get_system_state("last_artifact_audit_at")) == NOW
