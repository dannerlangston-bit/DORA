"""Retention (8.6, 8.9, SD-82, SD-83; gap 08-retention-policy): class candidates per set, the safety sweep with its
kept count, pins, the 180-day release, step_events pruning and the rig-side release rule."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from sentinel.records import CheckpointOutcome
from sentinel.store import ArtifactStore, Redaction, Store
from sentinel.store.retention import (PinResult, ensure_set_rows, prune_step_events, refresh_pins, rig_releasable,
                                      select_candidates, sweep)

NOW_DT = datetime(2026, 11, 11, 7, 30, tzinfo=timezone.utc)
NOW = NOW_DT.isoformat()
WS_ID = "ws.tier-a.active-trader"
CENSUS_ID = f"census/trade.active-trader.ladder/{WS_ID}"
CHART_ID = f"census/trade.active-trader.chart/{WS_ID}"


def ago(days: float) -> str:
    return (NOW_DT - timedelta(days=days)).isoformat()


class World:
    """A Central Store and artifact root with runs of every type and objects in their sets."""

    def __init__(self, tmp_path):
        self.arts = tmp_path / "artifacts"
        self.s = Store(tmp_path / "central.db")
        self.n = 0
        self.objs: dict[str, tuple[str, str]] = {}     # label -> (sha256, set_path)

    def build(self, b):
        if not self.s.query("SELECT 1 FROM builds WHERE build_id = ?", (b,)):
            self.s.register_build(b, channel="main", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                                  first_seen_at=ago(400))

    def run(self, run_id, run_type, days, *, outcome="complete", finished=True, base=None, pr_ref=None,
            abort_reason=None):
        pair = None
        if base is not None:
            self.build(base)
            self.build(f"cand-{run_id}")
            pair = self.s.register_pair(f"cand-{run_id}", base, run_type)
        self.s.insert("runs", {"run_id": run_id, "run_type": run_type, "pair_id": pair,
                               "lifecycle": "finished" if finished else "running",
                               "outcome": outcome if finished else None, "abort_reason": abort_reason,
                               "config_commit": "c0ffee", "config_tarball_sha256": "a" * 64,
                               "snapshot_set_sha256": "b" * 64, "harness_sha": "d00d", "authorization_ref": "auth",
                               "budget_sec": 2700, "started_at": ago(days), "finished_at": ago(days) if finished
                               else None, "pr_ref": pr_ref})

    def obj(self, label, set_path, kind="capture", *, also=()):
        self.n += 1
        a = ArtifactStore.at(self.arts, set_path).put(json.dumps({"n": self.n}).encode(), name=label,
                                                      kind="ocr_json", media_type="application/json")
        self.s.insert("artifacts", {"sha256": a.sha256, "kind": kind, "media_type": a.media_type, "bytes": a.bytes,
                                    "redaction_passed": 1, "set_path": set_path, "created_at": ago(400)})
        self.s.write_artifact_set(a.sha256, set_path, created_at=ago(400))
        for other in also:
            ArtifactStore.at(self.arts, other).put(json.dumps({"n": self.n}).encode(), name=label,
                                                   kind="ocr_json", media_type="application/json")
            self.s.write_artifact_set(a.sha256, other, created_at=ago(400))
        self.objs[label] = (a.sha256, set_path)
        return a.sha256

    def finding(self, key, run_id, shas, state="open", closed_at=None, created_at=None, checkpoint=None):
        self.s.insert("findings", {"natural_key": key, "run_id": run_id, "kind": "collateral", "description": "x",
                                   "evidence_json": json.dumps({"artifacts": list(shas)}), "triage_state": state,
                                   "closed_at": closed_at, "created_at": created_at or ago(300),
                                   "checkpoint_id": checkpoint})

    def path(self, label):
        sha, set_path = self.objs[label]
        return ArtifactStore.at(self.arts, set_path).path_of(sha)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.s.close()


def labels(w, cands):
    by_sha = {v: k for k, v in w.objs.items()}
    return sorted(by_sha[(c.sha256, c.set_path)] for c in cands)


def test_select_candidates_returns_exactly_the_expired_classes(w):
    for rid, days, pr in (("p1", 15, "pr-1"), ("p2", 10, "pr-2"), ("p3", 10, "pr-3")):
        w.run(rid, "pr", days, pr_ref=pr)
        w.obj(f"{rid}.capture", f"runs/{rid}")
    w.obj("p1.crash", "runs/p1", "crash")                                # crash and log slices: 180 days
    w.obj("p1.quarantined", "runs/p1")
    w.s.update("artifact_sets", {"sha256": w.objs["p1.quarantined"][0]}, {"quarantined_at": ago(14)})
    for rid, days in (("m1", 20), ("m2", 18), ("m3", 17), ("m4", 16)):
        w.run(rid, "merge", days)
        w.obj(f"{rid}.capture", f"runs/{rid}")
    w.obj("m1.ledger", "runs/m1", "ledger")                              # the whole set expires: its ledger too
    w.obj("m2.video", "runs/m2", "video")                                # the last 3 complete merge runs: kept
    w.run("m5", "merge", 15, outcome="aborted_harness", abort_reason="disk_full")
    w.obj("m5.capture", "runs/m5")
    for rid, days in (("n1", 8), ("n2", 6)):
        w.run(rid, "nightly", days)
        w.obj(f"{rid}.capture", f"runs/{rid}")
    w.obj("n1.request", "runs/n1", "request")                            # reviewer requests: 30 days
    w.obj("n1.ledger", "runs/n1", "ledger")                              # the set lives on: the ledger stays
    for rid, days in (("c1", 60), ("c2", 40), ("c3", 5)):
        w.run(rid, "calibration", days)
        w.obj(f"{rid}.capture", f"runs/{rid}")
    w.obj("c2.video", "runs/c2", "video")                                # run video: 14 days
    for rid, days, base in (("r1", 30, "rel-1"), ("r2", 20, "rel-1"), ("r3", 10, "rel-1"), ("r4", 25, "rel-2")):
        w.run(rid, "rc", days, base=base)
        w.obj(f"{rid}.capture", f"runs/{rid}")
    w.run("u1", "merge", 30, finished=False)
    w.obj("u1.capture", "runs/u1")
    w.obj("baseline.image", "baselines/released/b1")
    w.obj("eval.item", "eval/v3", "request")
    cands = select_candidates(w.s, NOW, pr_closed_at={"pr-3": ago(8)})
    assert labels(w, cands) == ["c1.capture", "c2.video", "m1.capture", "m1.ledger", "m5.capture", "n1.capture",
                                "p1.capture", "p3.capture", "r1.capture"]
    rules = {(c.sha256, c.set_path): c.rule for c in cands}
    assert rules[w.objs["p3.capture"]] == "pr run: 7 days after close"
    assert rules[w.objs["r1.capture"]] == "rc run: superseded"
    assert rules[w.objs["c2.video"]] == "video: 14 days"
    assert rules[w.objs["c1.capture"]] == "calibration run: next calibration plus 30 days"


def test_the_sweep_keeps_and_counts_a_pinned_object_and_deletes_the_rest(w):
    w.run("p1", "pr", 20)
    image = w.obj("p1.image", "runs/p1")
    w.obj("p1.plain", "runs/p1")
    shared = w.obj("p1.shared", "runs/p1", also=("eval/v3",))
    w.build("b1")
    w.s.insert("checkpoints", {"checkpoint_id": "census/trade.ladder/ws.a", "kind": "census", "surface_id": "s",
                               "workspace_id": "ws.a", "tier": 1, "data_mode": "frozen", "first_seen_commit": "c"})
    w.s.insert("baselines", {"checkpoint_id": "census/trade.ladder/ws.a", "lineage": "released", "build_id": "b1",
                             "image_sha256": image, "state": "candidate", "proposed_at": ago(19)})
    assert refresh_pins(w.s).pinned == 1
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert labels(w, res.deleted) == ["p1.plain", "p1.shared"]
    assert [(labels(w, [k.candidate])[0], k.reason) for k in res.kept] == [("p1.image", "pinned")]
    assert res.health_rows() == [("Retention", "2 deleted; 1 kept: pinned or referenced")]
    assert w.path("p1.image") is not None and w.path("p1.plain") is None
    sets = {(r["sha256"], r["set_path"]): r["deleted_at"] for r in w.s.query("SELECT * FROM artifact_sets")}
    assert sets[w.objs["p1.plain"]] == NOW and sets[(shared, "eval/v3")] is None
    deleted = {r["sha256"]: r["deleted_at"] for r in w.s.query("SELECT sha256, deleted_at FROM artifacts")}
    assert deleted[w.objs["p1.plain"][0]] == NOW
    assert deleted[shared] is None                                       # its eval copy lives on (SD-82)
    assert ArtifactStore.at(w.arts, "eval/v3").verify(shared)


def test_a_finding_closed_100_days_ago_keeps_its_evidence_until_day_181(w):
    w.run("p1", "pr", 300)
    sha = w.obj("p1.capture", "runs/p1")
    w.finding("F-1", "p1", [sha], state="noise", closed_at=ago(100))
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert res.deleted == () and len(res.kept) == 1 and res.kept[0].reason.startswith("released by its finding")
    later = (NOW_DT + timedelta(days=81)).isoformat()                    # 181 days after closed_at
    res = sweep(w.s, w.arts, select_candidates(w.s, later), backup_ok=True, now=later)
    assert labels(w, res.deleted) == ["p1.capture"] and res.kept == ()


@pytest.mark.parametrize("state, pins", [("open", True), ("confirmed_bug", True), ("fixed_pending_verify", True),
                                         ("intended", True), ("noise", False), ("closed", False)])
def test_pinning_states(w, state, pins):
    w.run("p1", "pr", 300)
    sha = w.obj("p1.capture", "runs/p1")
    w.finding("F-1", "p1", [sha], state=state, created_at=ago(300))
    assert refresh_pins(w.s).pinned == int(pins)
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert bool(res.kept) is pins and bool(res.deleted) is not pins     # unpinned: released 300 days ago


def test_an_intended_finding_is_released_when_its_baseline_is_approved(w):
    cp = "census/trade.ladder/ws.a"
    w.run("p1", "pr", 300)
    w.s.insert("checkpoints", {"checkpoint_id": cp, "kind": "census", "surface_id": "s", "workspace_id": "ws.a",
                               "tier": 1, "data_mode": "frozen", "first_seen_commit": "c"})
    w.build("b1")
    sha = w.obj("p1.capture", "runs/p1")
    w.finding("F-1", "p1", [sha], state="intended", created_at=ago(300), checkpoint=cp)
    w.s.insert("baselines", {"checkpoint_id": cp, "lineage": "released", "build_id": "b1", "image_sha256": "c" * 64,
                             "state": "approved", "proposed_at": ago(400), "approved_by": "bob",
                             "approved_at": ago(350), "approval_reason": "old"})   # approved before the finding
    assert refresh_pins(w.s).pinned == 1
    w.s.update("baselines", {"approved_at": ago(350)}, {"state": "retired"})
    w.s.insert("baselines", {"checkpoint_id": cp, "lineage": "released", "build_id": "b1", "image_sha256": "d" * 64,
                             "state": "approved", "proposed_at": ago(200), "approved_by": "bob",
                             "approved_at": ago(100), "approval_reason": "new look"})
    assert refresh_pins(w.s) == PinResult(pinned=0, changed=1)
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert res.deleted == () and "released by its finding" in res.kept[0].reason   # 100 of 180 days


def test_refresh_pins_follows_baselines_and_findings_and_writes_only_changes(w):
    w.run("p1", "pr", 5)
    a, b = w.obj("a", "runs/p1"), w.obj("b", "runs/p1")
    w.finding("F-1", "p1", [a])
    assert refresh_pins(w.s) == PinResult(pinned=1, changed=1)
    assert refresh_pins(w.s) == PinResult(pinned=1, changed=0)
    w.s.update("findings", {"natural_key": "F-1"}, {"triage_state": "closed", "closed_at": ago(1)})
    w.finding("F-2", "p1", [b], state="confirmed_bug")
    result = refresh_pins(w.s)
    assert (result.pinned, result.changed) == (1, 2)
    assert {r[0]: r[1] for r in w.s.query("SELECT sha256, pinned FROM artifacts")} == {a: 0, b: 1}


def test_an_open_explorer_findings_crop_and_trace_are_pinned_and_kept(w):
    """w4-store-maint:2: the 4.13 explorer block names the crop (signature.crop_sha256) and the trace (trace_ref);
    an open explorer_candidate finding pins them like its evidence."""
    w.run("r1", "merge", 20)
    for rid in ("m1", "m2", "m3"):                                       # r1 is no longer among the last three
        w.run(rid, "merge", 2)
    crop, trace = w.obj("r1.crop", "runs/r1", "crop"), w.obj("r1.trace", "runs/r1", "agent_frame")
    capture = w.obj("r1.capture", "runs/r1")
    explorer = {"explorer_run_id": "ex-1", "signature": {"surface_id": "s", "observation": "o", "crop_sha256": crop},
                "confidence": "high", "score": 0.9, "trace_ref": trace}
    w.s.insert("findings", {"natural_key": "F-X", "run_id": "r1", "kind": "explorer_candidate", "description": "x",
                            "evidence_json": json.dumps({"artifacts": [capture]}), "triage_state": "open",
                            "created_at": ago(20), "explorer_json": json.dumps(explorer)})
    assert refresh_pins(w.s) == PinResult(pinned=3, changed=3)
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert res.deleted == () and labels(w, [k.candidate for k in res.kept]) == ["r1.capture", "r1.crop", "r1.trace"]
    assert w.path("r1.crop") is not None and w.path("r1.trace") is not None


def test_a_run_set_that_empties_loses_its_ledgers_and_manifest(w):
    w.run("n1", "nightly", 30)
    w.obj("n1.capture", "runs/n1")
    w.obj("n1.journal", "runs/n1", "journal")
    shard_ledger = w.arts / "runs" / "n1" / "shards" / "1" / "results.jsonl"
    shard_ledger.parent.mkdir(parents=True)
    shard_ledger.write_text("{}\n")
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert labels(w, res.deleted) == ["n1.capture", "n1.journal"]
    assert not (w.arts / "runs" / "n1").exists()


def test_a_ledger_waits_while_a_kept_copy_keeps_its_set_alive(w):
    w.run("n1", "nightly", 30)
    pinned = w.obj("n1.capture", "runs/n1")
    w.obj("n1.journal", "runs/n1", "journal")
    w.finding("F-1", "n1", [pinned])
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert res.deleted == () and labels(w, [k.candidate for k in res.kept]) == ["n1.capture"]
    assert w.path("n1.journal") is not None


def _capture(w, run_id, checkpoint_id=CENSUS_ID):
    """A byte-identical frozen capture stored through the real write path (`Store.write_checkpoint`)."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(buf, format="PNG")
    a = ArtifactStore.at(w.arts, f"runs/{run_id}").put(buf.getvalue(), name=f"{checkpoint_id}/capture.png",
                                                     kind="capture", media_type="image/png",
                                                     redaction=Redaction("redact-v3"), checkpoint_id=checkpoint_id,
                                                     side="candidate")
    w.s.write_checkpoint(CheckpointOutcome(run_id=run_id, checkpoint_id=checkpoint_id, outcome="identical",
                                           workspace_id=WS_ID, persona="active_trader", data_mode="frozen", tier=1,
                                           artifacts={"candidate": a.sha256}), artifacts=[a])
    return a.sha256


def _finish(w, run_id, days):
    w.s.update("runs", {"run_id": run_id}, {"lifecycle": "finished", "outcome": "complete",
                                            "finished_at": ago(days)})


def test_a_new_copy_of_a_hash_retention_removed_is_live_again(w):
    """w4-store-maint:1: the import's copy of a hash whose every earlier copy was swept makes the hash live again
    (8.7: "retention removed it" only while every copy is deleted), and a copy placed again in its set too."""
    w.run("pA", "pr", 20, finished=False)
    sha = _capture(w, "pA")
    _finish(w, "pA", 20)
    assert [c.set_path for c in sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW).deleted
            ] == ["runs/pA"]
    w.run("pZ", "pr", 1, finished=False)
    _capture(w, "pZ")
    assert w.s.query("SELECT deleted_at FROM artifacts WHERE sha256 = ?", (sha,))[0][0] is None
    copies = {r["set_path"]: r["deleted_at"] for r in w.s.query("SELECT * FROM artifact_sets WHERE sha256 = ?", (sha,))}
    assert copies == {"runs/pA": NOW, "runs/pZ": None}
    w.s.update("artifact_sets", {"sha256": sha, "set_path": "runs/pZ"}, {"deleted_at": NOW})
    w.s.update("artifacts", {"sha256": sha}, {"deleted_at": NOW})
    _capture(w, "pZ", CHART_ID)                                          # the same bytes placed in pZ again
    assert w.s.query("SELECT deleted_at FROM artifacts WHERE sha256 = ?", (sha,))[0][0] is None
    assert w.s.query("SELECT deleted_at FROM artifact_sets WHERE sha256 = ? AND set_path = 'runs/pZ'",
                     (sha,))[0][0] is None


def test_the_sweep_records_every_copy_on_disk_before_it_decides(w):
    """w4-store-maint:1: a store whose import recorded only the first set's copy (before SD-82's per-copy rows): the
    sweep finds the copy in the protected merge run on disk, so the hash stays live and, once a finding pins it,
    the merge run's set is not removed with its last copy."""
    w.run("pA", "pr", 20)
    w.run("mB", "merge", 1)
    sha = w.obj("H", "runs/pA")
    ArtifactStore.at(w.arts, "runs/mB").put(json.dumps({"n": w.n}).encode(), name="H", kind="ocr_json",
                                            media_type="application/json")          # no artifact_sets row
    res = sweep(w.s, w.arts, select_candidates(w.s, NOW), backup_ok=True, now=NOW)
    assert [c.set_path for c in res.deleted] == ["runs/pA"]
    assert w.s.query("SELECT deleted_at FROM artifacts WHERE sha256 = ?", (sha,))[0][0] is None
    assert [tuple(r) for r in w.s.query("SELECT set_path, deleted_at FROM artifact_sets WHERE sha256 = ? ORDER BY "
                                        "set_path", (sha,))] == [("runs/mB", None), ("runs/pA", NOW)]
    w.finding("F-1", "mB", [sha])
    refresh_pins(w.s)
    for rid in ("m1", "m2", "m3"):                                       # mB is superseded
        w.run(rid, "merge", 0.5)
    later = (NOW_DT + timedelta(days=20)).isoformat()
    res = sweep(w.s, w.arts, select_candidates(w.s, later), backup_ok=True, now=later)
    assert res.deleted == () and [(k.candidate.set_path, k.reason) for k in res.kept] == [("runs/mB", "pinned")]
    assert ArtifactStore.at(w.arts, "runs/mB").verify(sha)


def test_ensure_set_rows_clears_a_deleted_mark_while_a_copy_lives(w):
    w.run("pA", "pr", 20)
    w.run("mB", "merge", 1)
    sha = w.obj("H", "runs/pA")
    ArtifactStore.at(w.arts, "runs/mB").put(json.dumps({"n": w.n}).encode(), name="H", kind="ocr_json",
                                            media_type="application/json")
    w.s.update("artifact_sets", {"sha256": sha}, {"deleted_at": ago(1)})   # an earlier sweep that missed mB
    w.s.update("artifacts", {"sha256": sha}, {"deleted_at": ago(1)})
    assert ensure_set_rows(w.s) == 0                                     # the database alone knows no other copy
    assert ensure_set_rows(w.s, w.arts) == 1 and ensure_set_rows(w.s, w.arts) == 0
    assert w.s.query("SELECT deleted_at FROM artifacts WHERE sha256 = ?", (sha,))[0][0] is None


def test_ensure_set_rows_records_the_first_sets_copy(w):
    w.run("p1", "pr", 5)
    w.s.insert("artifacts", {"sha256": "e" * 64, "kind": "capture", "media_type": "image/png", "bytes": 1,
                             "redaction_passed": 1, "set_path": "runs/p1", "created_at": ago(5)})
    w.s.insert("artifacts", {"sha256": "f" * 64, "kind": "capture", "media_type": "image/png", "bytes": 1,
                             "redaction_passed": 1, "set_path": "runs/p1", "created_at": ago(5),
                             "deleted_at": ago(1)})
    assert ensure_set_rows(w.s) == 2 and ensure_set_rows(w.s) == 0
    rows = {r["sha256"]: (r["set_path"], r["deleted_at"]) for r in w.s.query("SELECT * FROM artifact_sets")}
    assert rows == {"e" * 64: ("runs/p1", None), "f" * 64: ("runs/p1", ago(1))}


# -- step events -----------------------------------------------------------------------------------------------

def _events(w, run_id, step_outcome=None):
    shard = f"{run_id}/1"
    if not w.s.query("SELECT 1 FROM rigs WHERE rig_id = 'rig-1'"):
        w.s.register_rig("rig-1", login_alias="paper-01", owner="qa", snapshot_id="snap", allowlist_entry=1)
    w.s.insert("shards", {"shard_id": shard, "run_id": run_id, "rig_id": "rig-1", "sides": "both",
                          "scope_count": 1, "state": "finished"})
    for seq in (1, 2):
        w.s.insert("step_events", {"run_id": run_id, "shard_id": shard, "side": "candidate", "seq": seq,
                                   "source": "orchestrator", "wall_at": ago(100), "mono_ms": seq, "mode": "scripted",
                                   "action": "click", "gate_decision": "none",
                                   "step_outcome": step_outcome if seq == 2 else None})


def test_step_events_go_after_90_days_except_for_the_four_exemptions(w):
    w.run("old", "merge", 100)
    w.run("safety", "merge", 100, outcome="aborted_safety", abort_reason="kill_switch")
    w.run("blocked", "merge", 100)
    w.run("incident", "merge", 100)
    w.run("finding", "merge", 100)
    w.run("recent", "merge", 30)
    for rid in ("old", "safety", "incident", "finding", "recent"):
        _events(w, rid)
    _events(w, "blocked", "blocked_by_gateway")
    w.s.insert("incidents", {"run_id": "incident", "severity": "S3", "trigger": "drop", "opened_at": ago(100)})
    w.finding("F-1", "finding", [])
    assert prune_step_events(w.s, NOW) == 2
    left = {r[0] for r in w.s.query("SELECT DISTINCT run_id FROM step_events")}
    assert left == {"safety", "blocked", "incident", "finding", "recent"}
    assert prune_step_events(w.s, NOW) == 0
    w.s.update("findings", {"natural_key": "F-1"}, {"triage_state": "closed", "closed_at": ago(10)})
    assert prune_step_events(w.s, NOW) == 2                              # the finding closed: the exemption ends


# -- the rig side ----------------------------------------------------------------------------------------------

def test_a_shard_leaves_its_rig_after_import_plus_7_days_and_a_revert(w):
    w.run("r", "merge", 10)
    w.s.register_rig("rig-1", login_alias="paper-01", owner="qa", snapshot_id="snap", allowlist_entry=1)
    for n, state, imported in ((1, "imported", ago(8)), (2, "imported", ago(8)), (3, "imported", ago(3)),
                               (4, "import_failed", None), (5, "lost", None), (6, "shipping", None)):
        w.s.insert("shards", {"shard_id": f"r/{n}", "run_id": "r", "rig_id": "rig-1", "sides": "both",
                              "scope_count": 1, "state": state, "imported_at": imported})
    assert rig_releasable(w.s, NOW, last_revert_at={"rig-1": ago(1)}) == ("r/1", "r/2")
    assert rig_releasable(w.s, NOW, last_revert_at={"rig-1": ago(9)}) == ()          # no revert since the import
    assert rig_releasable(w.s, NOW, last_revert_at={}) == ()
    assert rig_releasable(w.s, NOW, last_revert_at={"rig-1": ago(1)}, released=("r/5", "r/6")) == (
        "r/1", "r/2", "r/5")
