"""Tests for sentinel.store.importer: the shard import into the Central Store, upload and ledger verification, the
restart-budget fill, lost and re-queued rigs and the dead-man re-evaluation (8.2, 8.4, 8.9, 10.7)."""
import dataclasses
import hashlib
import io
import json
import sqlite3

import pytest
from PIL import Image

from sentinel.records import CheckpointOutcome, Finding, RunOutcome, StepEvent
from sentinel.store import (ArtifactStore, ImportConflict, ManifestHeader, Redaction, RunPlan, ScopeEntry,
                            ShardPlan, Store, StoreRefused, StoreWriteFailed)
from sentinel.store import importer
from sentinel.store.store import read_ledger

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
SCOPE = {CENSUS: ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
         SWEEP: ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
         FLOW: ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard")}
RIGS = {"rig-a1": "paper-01", "rig-a2": "paper-02", "rig-b1": "paper-03"}


def plan(scope, shards):
    return RunPlan(run_id=RUN, run_type="merge", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=tuple(SCOPE[c] for c in scope), shards=tuple(shards), started_at="2026-11-10T14:00:00Z")


def rigs(s):
    for n, (rig, login) in enumerate(RIGS.items(), 1):
        s.register_rig(rig, login_alias=login, owner="qa", snapshot_id=f"snap-{n}", allowlist_entry=n)


def png(seed):
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (seed % 256, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def out(cid, outcome="identical", **kw):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=outcome, workspace_id=WS, persona="active_trader",
                             data_mode="frozen", **kw)


class World:
    """A Central Store with the run begun, and shard directories built by Run Stores as rigs build them."""

    def __init__(self, tmp_path, shards=(("1", "rig-a1"),)):
        self.tmp = tmp_path
        self.root = tmp_path / "artifacts"
        self.central = Store(tmp_path / "control" / "central.db")
        rigs(self.central)
        self.central.begin_run(plan(SCOPE, [ShardPlan(f"{RUN}/{n}", rig, scope_count=0) for n, rig in shards]))
        self.n = 0

    def shard(self, n, scope, *, rig="rig-a1", attempt=1, outcome="identical", write=None, end="complete",
              name=None):
        """Build `incoming/<run>/<n>[-name]/` with a Run Store of `scope`, a capture per written checkpoint."""
        sid = f"{RUN}/{n}"
        d = self.tmp / "incoming" / RUN / (name or n)
        arts = ArtifactStore(d, sid)
        with Store(d / "run.db") as s:
            rigs(s)
            s.begin_run(plan(scope, [ShardPlan(sid, rig, attempt=attempt)]))
            for cid in scope if write is None else write:
                self.n += 1
                a = arts.put(png(self.n), name=f"{cid}/candidate/capture.png", kind="capture", media_type="image/png",
                             redaction=Redaction("redact-v3"), checkpoint_id=cid, side="candidate")
                step = StepEvent(run_id=RUN, shard_id=sid, side="candidate", seq=self.n, source="orchestrator",
                                 wall_at="2026-11-10T14:05:00+00:00", mono_ms=self.n, mode="scripted", action="capture",
                                 checkpoint_id=cid)
                s.write_events([step])
                findings = []
                if outcome == "collateral":
                    findings = [Finding(f"F-{n}-{attempt}-{cid}", RUN, "collateral", "moved", RUN, checkpoint_id=cid,
                                        phash="0123456789abcdef", tier=2, category="collateral")]
                s.write_checkpoint(out(cid, outcome, artifacts={"candidate": a.sha256}), artifacts=[a],
                                   findings=findings, shard_id=sid)
            if end is not None:
                s.end_shard(sid, end)
        arts.write_manifest(ManifestHeader(config_commit="c0ffee", harness_commit="d00d", run_id=RUN, shard_id=sid))
        return d

    def run_import(self, d, **kw):
        return importer.import_shard(self.central, d, RUN, artifact_root=self.root, **kw)

    def close(self):
        self.central.close()


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def objects(d):
    return sorted(p for p in (d / "objects").rglob("*") if p.is_file())


def test_a_shard_imports_its_rows_and_objects_and_is_imported(w):
    d = w.shard("1", [CENSUS, SWEEP, FLOW])
    shipped = objects(d)
    with Store.reader(d / "run.db") as rs:
        want = rs.outcomes(RUN)
        events = rs.step_events(RUN)
    r = w.run_import(d)
    assert (r.state, r.reason, r.rerequests) == ("imported", None, 0)
    c = w.central
    assert c.outcomes(RUN) == want and c.step_events(RUN) == events
    assert objects(d) == []                                           # renamed into the central tree
    for p in shipped:
        moved = w.root / "runs" / RUN / "objects" / p.parent.name / p.name
        assert hashlib.sha256(moved.read_bytes()).hexdigest() == p.name.split(".")[0]
    row = c.query("SELECT state, run_store_sha256, ledger_sha256, ledger_rows, shard_outcome FROM shards")[0]
    assert row["state"] == "imported" and row["shard_outcome"] == "complete"
    assert row["run_store_sha256"] == hashlib.sha256((d / "run.db").read_bytes()).hexdigest()
    assert row["ledger_rows"] == sum(len(b["rows"]) for b, _ in read_ledger(d / "results.jsonl"))
    kept = w.root / "runs" / RUN / "shards" / "1" / "results.jsonl"
    assert kept.read_bytes() == (d / "results.jsonl").read_bytes()    # the ledger kept verbatim (8.4)
    manifest = json.loads((w.root / "runs" / RUN / "manifest.json").read_text())
    assert manifest["header"]["shards"] == [f"{RUN}/1"] and len(manifest["entries"]) == 3
    assert {r[0] for r in c.query("SELECT DISTINCT set_path FROM artifact_sets")} == {f"runs/{RUN}"}
    assert w.run_import(d).state == "replayed"                       # a rig retrying: idempotent
    assert c.end_run(RunOutcome(RUN, "merge", "complete", "reported")).status == "complete"


def test_an_upload_hash_mismatch_is_re_requested_three_times_then_fails_the_run(w):
    d = w.shard("1", [CENSUS, SWEEP, FLOW])
    bad = objects(d)[0]
    good = bad.read_bytes()
    bad.write_bytes(b"corrupted in transit")
    asked = []
    r = w.run_import(d, rehash_request=lambda sha, n: asked.append((sha, n)))
    assert asked == [(bad.name.split(".")[0], n) for n in (1, 2, 3)]
    assert (r.state, r.reason, r.rerequests) == ("import_failed", "upload_hash_mismatch", 3)
    assert (r.run_outcome.status, r.run_outcome.reason) == ("aborted_harness", "artifact_persist_failed")
    c = w.central
    assert {o.reason for o in c.outcomes(RUN)} == {"artifact_persist_failed"}   # nothing of the shard imported
    assert c.query("SELECT count(*) FROM artifacts")[0][0] == 0 and len(objects(d)) == 3   # the rig keeps its copy
    sh = c.query("SELECT state, shard_outcome, abort_reason, abort_detail FROM shards")[0]
    assert tuple(sh)[:3] == ("import_failed", "aborted_harness", "artifact_persist_failed")
    assert sh["abort_detail"].startswith("upload_hash_mismatch")
    assert c.run_summary(RUN)["health"]["import_failed"][0]["reason"] == "upload_hash_mismatch"
    bad.write_bytes(good)                                # shipped again: the run has ended, nothing changes
    again = w.run_import(d)
    assert again.state == "import_failed" and again.run_outcome == r.run_outcome and len(objects(d)) == 3


def test_a_re_request_that_brings_the_right_bytes_imports(w):
    d = w.shard("1", [CENSUS])
    bad = objects(d)[0]
    good = bad.read_bytes()
    bad.write_bytes(b"torn upload")
    r = w.run_import(d, rehash_request=lambda sha, n: bad.write_bytes(good) if n == 2 else None)
    assert (r.state, r.rerequests) == ("imported", 2)


def test_a_ledger_that_disagrees_is_an_import_conflict_with_nothing_imported(w):
    d = w.shard("1", [CENSUS, SWEEP, FLOW])
    ledger = d / "results.jsonl"
    lines = ledger.read_text().splitlines()
    i = next(n for n, x in enumerate(lines) if json.loads(x)["batch"] == "checkpoint")
    lines[i] = lines[i].replace('"identical"', '"collateral"')
    ledger.write_text("\n".join(lines) + "\n")
    r = w.run_import(d)
    assert (r.state, r.reason) == ("import_failed", "import_conflict")
    assert "outcomes" in r.detail
    assert (r.run_outcome.status, r.run_outcome.reason) == ("aborted_harness", "import_conflict")
    c = w.central
    assert [o.outcome for o in c.outcomes(RUN)] == ["harness_error"] * 3
    assert c.query("SELECT count(*) FROM step_events")[0][0] == 0 and len(objects(d)) == 3
    assert not (w.root / "runs" / RUN / "manifest.json").exists()


def test_a_ledger_holding_one_uncommitted_batch_is_imported_from_the_ledger(w):
    """8.9: the Orchestrator appended a batch and died before its commit; the ledger holds the rows for replay."""
    d = w.shard("1", [CENSUS, SWEEP, FLOW])
    lines = (d / "results.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["batch"] == "shard_end"
    i = max(n for n, x in enumerate(lines) if json.loads(x)["batch"] == "checkpoint")
    head = d.parent / "head.jsonl"
    head.write_text("".join(x + "\n" for x in lines[:i]))             # the Run Store lacks the last checkpoint
    (d / "run.db").unlink()
    with Store(d / "run.db", ledger=False) as s:
        s.replay(head)
    (d / "results.jsonl").write_text("".join(x + "\n" for x in lines[:i + 1]))
    head.unlink()
    r = w.run_import(d, restart_budget_spent=True)
    assert r.state == "imported" and len(w.central.outcomes(RUN)) == 3
    assert {o.outcome for o in w.central.outcomes(RUN)} == {"identical"}


def test_the_restart_budget_fills_only_the_shards_own_scope(tmp_path):
    w = World(tmp_path, shards=(("1", "rig-a1"), ("2", "rig-a2")))
    try:
        d = w.shard("2", [SWEEP, FLOW], rig="rig-a2", write=[SWEEP], end=None)   # died for good after SWEEP
        r = w.run_import(d, restart_budget_spent=True)
        assert r.state == "imported"
        c = w.central
        flow = c.get_outcome(RUN, FLOW)
        assert (flow.outcome, flow.reason) == ("harness_error", "orchestrator_restart_budget")
        assert c.query("SELECT shard_id FROM outcomes WHERE checkpoint_id = ?", (FLOW,))[0][0] == f"{RUN}/2"
        assert c.get_outcome(RUN, SWEEP).outcome == "identical" and c.get_outcome(RUN, CENSUS) is None
        sh = c.query("SELECT shard_outcome, abort_reason FROM shards WHERE shard_id = ?", (f"{RUN}/2",))[0]
        assert tuple(sh) == ("aborted_harness", "orchestrator_restart_budget")
        assert importer.re_evaluate_run(c, RUN) is None                 # shard 1 has not shipped yet
        assert w.run_import(w.shard("1", [CENSUS])).state == "imported"
        done = importer.re_evaluate_run(c, RUN)
        assert (done.status, done.reason) == ("aborted_harness", "orchestrator_restart_budget")
    finally:
        w.close()


def test_a_failed_transaction_leaves_nothing_visible(w):
    """8.2: a partially imported shard is never visible: a row that conflicts with the Central Store's mid-import
    rolls back every row and puts the objects back."""
    w.central.write_checkpoint(out(FLOW, "collateral"))                # the Central Store already has FLOW
    d = w.shard("1", [CENSUS, SWEEP, FLOW])
    r = w.run_import(d)
    assert (r.state, r.reason) == ("import_failed", "import_conflict")
    c = w.central
    assert c.query("SELECT count(*) FROM artifacts")[0][0] == 0
    assert c.query("SELECT count(*) FROM step_events")[0][0] == 0
    assert c.get_outcome(RUN, CENSUS).reason == "import_conflict"     # settle_run's fill, not the shard's row
    assert len(objects(d)) == 3 and not (w.root / "runs" / RUN / "manifest.json").exists()
    assert not (w.root / "runs" / RUN / "shards" / "1" / "results.jsonl").exists()


def test_a_central_commit_failure_retries_with_backoff_and_leaves_the_shard_shipping(w, monkeypatch):
    d = w.shard("1", [CENSUS])
    real = w.central.import_shard_rows
    calls, sleeps = [], []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise StoreWriteFailed("disk I/O error")
        return real(*a, **kw)

    monkeypatch.setattr(w.central, "import_shard_rows", flaky)
    assert w.run_import(d, sleep=sleeps.append).state == "imported" and sleeps == [1.0, 2.0]
    w2 = World(w.tmp / "second")
    try:
        d2 = w2.shard("1", [CENSUS])
        monkeypatch.setattr(w2.central, "import_shard_rows", lambda *a, **kw: (_ for _ in ()).throw(
            StoreWriteFailed("disk full")))
        with pytest.raises(StoreWriteFailed):
            w2.run_import(d2, sleep=lambda s: None, retries=2)
        assert w2.central.query("SELECT state FROM shards")[0][0] == "shipping"
        assert len(objects(d2)) == 1                                     # back in incoming for the retry
    finally:
        w2.close()


def test_a_returning_rigs_attempt_is_kept_superseded_and_never_counted(w):
    """10.7: lose attempt 1, re-queue it as attempt 2 on another rig and import that; the returning rig's attempt-1
    rows land in outcomes_superseded only."""
    c = w.central
    importer.mark_shard_lost(c, f"{RUN}/1")
    assert importer.mark_shard_superseded(c, f"{RUN}/1", rig_id="rig-b1", at="2026-11-10T15:00:00Z") == 2
    assert w.run_import(w.shard("1", [CENSUS, SWEEP, FLOW], rig="rig-b1", attempt=2, name="1-a2")).state == "imported"
    counts = c.run_counts(RUN)
    assert counts["identical"] == 3
    old = w.shard("1", [CENSUS, SWEEP, FLOW], attempt=1, outcome="collateral", name="1-a1")
    r = w.run_import(old)
    assert (r.state, r.attempt) == ("superseded", 1)
    assert c.run_counts(RUN) == counts and c.findings(RUN) == []
    rows = c.query("SELECT checkpoint_id, outcome, attempt, shard_id FROM outcomes_superseded ORDER BY checkpoint_id")
    assert [tuple(x) for x in rows] == [(cid, "collateral", 1, f"{RUN}/1") for cid in sorted([CENSUS, SWEEP, FLOW])]
    assert (w.root / "runs" / RUN / "shards" / "1" / "attempt-1" / "results.jsonl").exists()
    assert w.run_import(old).state == "superseded"                  # again: write-once, a no-op
    assert c.query("SELECT count(*) FROM outcomes_superseded")[0][0] == 3
    assert c.end_run(RunOutcome(RUN, "merge", "complete", "reported")).status == "complete"


def test_a_shard_is_re_queued_once(w):
    c = w.central
    with pytest.raises(StoreRefused, match="only a lost shard"):
        importer.mark_shard_superseded(c, f"{RUN}/1", rig_id="rig-b1")
    importer.mark_shard_lost(c, f"{RUN}/1")
    importer.mark_shard_superseded(c, f"{RUN}/1", rig_id="rig-b1")
    importer.mark_shard_lost(c, f"{RUN}/1")
    with pytest.raises(StoreRefused, match="rig_lost"):
        importer.mark_shard_superseded(c, f"{RUN}/1", rig_id="rig-a2")
    with pytest.raises(StoreRefused):
        importer.mark_shard_lost(c, f"{RUN}/1")                      # already lost


def test_a_lost_shard_of_a_dead_man_run_is_imported_when_the_rig_returns_and_the_run_re_evaluated(w):
    c = w.central
    importer.mark_shard_lost(c, f"{RUN}/1")
    dead = importer.abort_dead_man(c, RUN, at="2026-11-10T17:00:00Z")
    assert (dead.status, dead.reason) == ("aborted_harness", "dead_man")
    assert c.run_summary(RUN)["health"]["lost_shards"] == [{"shard_id": f"{RUN}/1", "detail": importer.LOST_DETAIL}]
    assert c.run_counts(RUN)["harness_error"] == 3
    r = w.run_import(w.shard("1", [CENSUS, SWEEP, FLOW]), at="2026-11-10T19:00:00Z")
    assert r.state == "lost_returned" and r.run_outcome.status == "complete"
    assert c.run_outcome(RUN).status == "complete" and c.run_counts(RUN)["identical"] == 3
    assert c.query("SELECT count(*) FROM outcomes_superseded WHERE reason = 'dead_man'")[0][0] == 3
    assert c.run_summary(RUN)["lost_rig_returned"][0]["shard_id"] == f"{RUN}/1"


def test_a_returning_shard_re_evaluates_but_the_run_stays_dead_man_while_another_shard_is_lost(tmp_path):
    w = World(tmp_path, shards=(("1", "rig-a1"), ("2", "rig-a2")))
    try:
        c = w.central
        importer.mark_shard_lost(c, f"{RUN}/1")
        importer.mark_shard_lost(c, f"{RUN}/2")
        importer.abort_dead_man(c, RUN)
        r = w.run_import(w.shard("1", [CENSUS]))
        assert r.state == "lost_returned" and (r.run_outcome.status, r.run_outcome.reason) == ("aborted_harness",
                                                                                               "dead_man")
        assert c.get_outcome(RUN, CENSUS).outcome == "identical" and c.get_outcome(RUN, FLOW).reason == "dead_man"
        r = w.run_import(w.shard("2", [SWEEP, FLOW], rig="rig-a2"))
        assert r.run_outcome.status == "complete"
    finally:
        w.close()


def test_a_run_store_of_another_run_or_identity_is_refused(w):
    d = w.shard("1", [CENSUS])
    with pytest.raises(StoreRefused, match="run-02"):
        importer.import_shard(w.central, d, "run-02", artifact_root=w.root)
    with Store.reader(d / "run.db") as rs:                          # a run identity of another configuration
        rows = rs.shard_rows(f"{RUN}/1", objects={p.name.split(".")[0]: p for p in objects(d)},
                             set_path=f"runs/{RUN}")
    with pytest.raises(ImportConflict, match="config_commit"):
        w.central.import_shard_rows(dataclasses.replace(rows, run={**rows.run, "config_commit": "other"}))
    conn = sqlite3.connect(d / "run.db")
    conn.execute("UPDATE runs SET config_commit = 'other'")
    conn.commit()
    conn.close()
    r = w.run_import(d)
    assert (r.state, r.reason) == ("import_failed", "import_conflict")   # the Run Store disagrees with its ledger


def test_mark_import_failed_takes_only_the_8_9_reasons(w):
    with pytest.raises(StoreRefused, match="import_conflict"):
        w.central.mark_import_failed(f"{RUN}/1", "dead_man", "x")


def test_the_shards_journals_are_ingested_with_it_and_a_foreign_journal_line_is_a_conflict(w):
    d = w.shard("1", [CENSUS])
    drop = StepEvent(run_id=RUN, shard_id=f"{RUN}/1", side="candidate", seq=50, source="gateway_hook",
                     wall_at="2026-11-10T14:06:00+00:00", mono_ms=50, mode="scripted", action="gateway_decision",
                     gate_decision="dropped", gate_reason="no_token", gate_class="integrity")
    (d / "gateway_drops.jsonl").write_text(json.dumps(drop.to_dict()) + "\n")
    (d / "watchdog_events.jsonl").write_text(json.dumps({"event_id": "wd-1", "action": "watchdog_hang",
                                                         "at": "2026-11-10T14:07:00Z", "mono_ms": 60}) + "\n")
    other = World(w.tmp / "other")
    try:
        d2 = other.shard("1", [CENSUS])
        (d2 / "gateway_drops.jsonl").write_text(json.dumps({**drop.to_dict(), "shard_id": f"{RUN}/2"}) + "\n")
        r = other.run_import(d2)
        assert (r.state, r.reason) == ("import_failed", "import_conflict") and len(objects(d2)) == 1
    finally:
        other.close()
    assert w.run_import(d).state == "imported"
    c = w.central
    assert c.gateway_drops(RUN) == 1
    assert [e.reason for e in c.step_events(RUN) if e.source == "watchdog"] == ["wd-1"]
    assert (w.root / "runs" / RUN / "shards" / "1" / "watchdog_events.jsonl").exists()

