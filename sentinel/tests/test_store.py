"""Tests for sentinel.store.store: schema from taxonomy, refusals, later changes, decisions (8.2, 8.3, 4.13, 4.14)."""
import copy
import dataclasses
import json
import sqlite3
import subprocess
import sys

import pytest

from sentinel.records import CheckpointOutcome, Decision, DiffSummary, Finding, RunOutcome, Signature, StepOutcome
from sentinel.store import (ImportConflict, Review, RunNotComplete, RunPlan, ScopeEntry, ShardPlan, Store, StoreError,
                            StoreRefused, StoreWriteFailed, WriterLockError, apply_inheritance, connect_reader, hamming,
                            render_schema, seed_rows)
from sentinel.taxonomy import Taxonomy, TaxonomyError, load

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"            # tier 1
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"   # tier 2
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
BASE = 0x0123456789ABCDEF


def ph(bits=()):
    """BASE with the given bit positions flipped, as 16 hex characters."""
    v = BASE
    for b in bits:
        v ^= 1 << b
    return f"{v:016x}"


def scope():
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
            ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
            ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"))


def plan(run_id=RUN, shards=(), pr_author=None):
    return RunPlan(run_id=run_id, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope(), shards=shards, pr_author=pr_author)


def out(cid, outcome="identical", run_id=RUN, **kw):
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", **kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.begin_run(plan())
    yield s
    s.close()


def finish_all(store, outcome="identical"):
    for cid in (CENSUS, SWEEP, FLOW):
        if store.get_outcome(RUN, cid) is None:
            store.write_checkpoint(out(cid, outcome))


# -- schema generated from taxonomy.yaml --------------------------------------------------------------------------

def test_schema_lists_come_from_taxonomy(tmp_path):
    data = copy.deepcopy(load().data)
    data["finding_kinds"].append("test_only_kind")
    data["reasons"]["not_run"].append("test_only_reason")
    t = Taxonomy(data)
    assert "'test_only_kind'" in render_schema(t) and "'test_only_kind'" not in render_schema(load())
    assert ("not_run", "test_only_reason", 0, None, "4.13") in seed_rows(t)
    assert ("harness_error", "dead_man", 0, None, "1.10") in seed_rows(t)           # reason_inheritance
    assert ("not_run", "step_failed", 1, "<step>", "4.13") in seed_rows(t)
    assert ("aborted_harness", "preflight", 1, "<item>", "4.13") in seed_rows(t)

    def insert_finding(s, kind):
        s.conn.execute("INSERT INTO findings (natural_key, run_id, kind, description, evidence_json, created_at) "
                       "VALUES (?, ?, ?, 'x', '{}', 'now')", (kind, RUN, kind))

    with Store(tmp_path / "a" / "run.db", taxonomy=t) as s:
        s.begin_run(plan())
        insert_finding(s, "test_only_kind")
    with Store(tmp_path / "b" / "run.db") as s:
        s.begin_run(plan())
        with pytest.raises(sqlite3.IntegrityError):
            insert_finding(s, "test_only_kind")
        n = s.conn.execute("SELECT count(*) FROM outcome_reasons").fetchone()[0]
        assert n == len(seed_rows(load()))


def test_schema_refuses_a_taxonomy_missing_a_named_value():
    data = copy.deepcopy(load().data)
    data["checkpoint_outcomes"].remove("needs_human")
    with pytest.raises(StoreError, match="needs_human"):
        render_schema(Taxonomy(data))


def test_reasons_are_retired_not_deleted_and_lists_need_migration(tmp_path):
    db = tmp_path / "run.db"
    with Store(db) as s:
        s.begin_run(plan())
    data = copy.deepcopy(load().data)
    data["reasons"]["needs_baseline"].remove("explorer_replay")
    with Store(db, taxonomy=Taxonomy(data)) as s:
        row = s.conn.execute("SELECT active FROM outcome_reasons WHERE reason = 'explorer_replay'")
        assert row.fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="retired reason"):
            s.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, reason, recorded_at) "
                           "VALUES (?, ?, 'needs_baseline', 'explorer_replay', 'now')", (RUN, CENSUS))
    data["reasons"]["not_run"].remove("quarantine")         # a reason a generated CHECK lists
    with pytest.raises(StoreError, match="migration"):
        Store(db, taxonomy=Taxonomy(data))
    with Store(db) as s:                                    # the refused open left no lock; the reason is back
        row = s.conn.execute("SELECT active FROM outcome_reasons WHERE reason = 'explorer_replay'")
        assert row.fetchone()[0] == 1


def test_pragmas_reader_and_hamming(tmp_path):
    db = tmp_path / "run.db"
    with Store(db) as s:
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        r = connect_reader(db)
        pair = (bytes.fromhex(ph()), bytes.fromhex(ph((0, 9, 63))))
        assert r.execute("SELECT hamming(?, ?)", pair).fetchone()[0] == 3
        with pytest.raises(sqlite3.OperationalError):
            r.execute("INSERT INTO system_state VALUES ('k', 'v', 'now')")
        r.close()
    assert hamming(ph(), ph((1, 2))) == 2 and hamming(None, ph()) is None


# -- single writer and failure modes (8.2, 8.9) -------------------------------------------------------------------

def test_one_writer_process(tmp_path):
    db = tmp_path / "run.db"
    with Store(db):
        with pytest.raises(WriterLockError):
            Store(db)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (tmp_path / "writer.lock").write_text(dead.stdout.strip())          # a stale lock from a dead writer
    with Store(db):
        assert (tmp_path / "writer.lock").read_text() != dead.stdout.strip()
    assert not (tmp_path / "writer.lock").exists()


def test_store_refuses_a_share():
    with pytest.raises(StoreError, match="UNC"):
        Store(r"\\fileserver\share\run.db")


def test_busy_database_is_store_write_failed(tmp_path):
    db = tmp_path / "run.db"
    with Store(db, busy_timeout_ms=10) as s:
        s.begin_run(plan())
        other = sqlite3.connect(db, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StoreWriteFailed):
                s.write_checkpoint(out(CENSUS))
        finally:
            other.execute("ROLLBACK")
            other.close()
        s.write_checkpoint(out(CENSUS))


def test_batches_go_to_the_ledger_first(store, tmp_path):
    store.write_checkpoint(out(CENSUS))
    lines = [json.loads(x) for x in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert [x["batch"] for x in lines] == ["run_begin", "checkpoint"]
    assert {r["table"] for r in lines[1]["rows"]} == {"outcomes", "outcome_records"}


# -- refusals at write ----------------------------------------------------------------------------------------------

def test_unknown_outcome_refused(store):
    o = out(CENSUS)
    o.outcome = "looks_fine"                     # records are mutable; the store re-validates
    with pytest.raises(TaxonomyError):
        store.write_checkpoint(o)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, recorded_at) "
                           "VALUES (?, ?, 'looks_fine', 'now')", (RUN, CENSUS))


def test_illegal_reason_refused(store):
    o = out(CENSUS, "noise", reason="below_floor")
    o.reason = "timeout"
    with pytest.raises(TaxonomyError):
        store.write_checkpoint(o)
    with pytest.raises(sqlite3.IntegrityError):              # (outcome, reason) is a foreign key
        store.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, reason, recorded_at) "
                           "VALUES (?, ?, 'noise', 'timeout', 'now')", (RUN, CENSUS))
    with pytest.raises(sqlite3.IntegrityError, match="reason_detail"):   # parametric without its parameter
        store.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, reason, recorded_at) "
                           "VALUES (?, ?, 'not_run', 'step_failed', 'now')", (RUN, CENSUS))


def test_env_error_without_gate_refused(store):
    o = out(CENSUS, "env_error", reason="fixture_drift", gate="fixture_verified")
    o.gate = None
    with pytest.raises(TaxonomyError):
        store.write_checkpoint(o)
    with pytest.raises(sqlite3.IntegrityError, match="F65"):
        store.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, reason, recorded_at) "
                           "VALUES (?, ?, 'env_error', 'fixture_drift', 'now')", (RUN, CENSUS))
    store.write_checkpoint(out(CENSUS, "env_error", reason="fixture_drift", gate="fixture_verified"))


def test_pending_only_while_queued(store):
    with pytest.raises(StoreRefused, match="pending"):
        store.write_checkpoint(out(CENSUS, "needs_human", reason="no_manifest", review_state="pending"))


def test_unstored_artifact_refused(store):
    with pytest.raises(StoreRefused, match="not stored"):
        store.write_checkpoint(out(CENSUS, artifacts={"candidate": "e" * 64}))


def test_outcome_mutation_refused(store):
    o = out(CENSUS, "collateral", diff=DiffSummary(whole_pixel_fraction=0.02, whole_ssim=0.97))
    store.write_checkpoint(o)
    store.write_checkpoint(o)                                        # replay of the same record: no-op
    with pytest.raises(ImportConflict):
        store.write_checkpoint(out(CENSUS, "intended"))
    key = (RUN, CENSUS)
    for sql in ("UPDATE outcomes SET outcome = 'intended' WHERE run_id = ? AND checkpoint_id = ?",
                "UPDATE outcomes SET ssim = 1.0 WHERE run_id = ? AND checkpoint_id = ?",
                "UPDATE outcomes SET gate = NULL, recaptures = 3 WHERE run_id = ? AND checkpoint_id = ?",
                "UPDATE outcomes SET review_state = 'done' WHERE run_id = ? AND checkpoint_id = ?",
                "DELETE FROM outcomes WHERE run_id = ? AND checkpoint_id = ?",
                "UPDATE outcome_records SET record_json = '{}' WHERE run_id = ? AND checkpoint_id = ?"):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(sql, key)
    assert store.get_outcome(*key).outcome == "collateral"


def test_round_trip_of_records(store):
    o = out(SWEEP, "landmark_missing", reason="locator:s07", detail="rung jab missed", side="candidate", tier=2,
            diff=DiffSummary(whole_pixel_fraction=0.5))
    store.write_checkpoint(o)
    assert store.get_outcome(RUN, SWEEP) == o
    row = store.conn.execute("SELECT reason, reason_detail, diff_pct FROM outcomes WHERE checkpoint_id = ?",
                             (SWEEP,)).fetchone()
    assert tuple(row) == ("locator", "s07\nrung jab missed", 50.0)


def test_a_steps_gateway_event_is_stored_in_the_gate_columns_and_read_back(tmp_path):
    """4.13 step_outcome.gateway_event as 8.3 step_events gate_decision, gate_reason and gate_class (5.4.4)."""
    flow = "flow.trade.order-ticket.stock.limit"
    with Store(tmp_path / "run.db") as s:
        s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(plan(shards=(ShardPlan(f"{RUN}/1", "rig-a1"),)))
        refused = StepOutcome(RUN, flow, "s07+enter", "blocked_by_gateway",
                              gateway_event={"decision": "refused", "reason": "guarded_key_refused", "class": "safety"})
        dropped = StepOutcome(RUN, flow, "s06", "blocked_by_gateway",
                              gateway_event={"decision": "dropped", "reason": "token_expired", "class": "integrity"})
        emitted = StepOutcome(RUN, flow, "s05", "passed", gateway_event={"decision": "emitted", "class": "allowed"})
        plain = StepOutcome(RUN, flow, "s04", "passed")
        s.write_steps([refused, dropped, emitted, plain], shard_id=f"{RUN}/1", side="candidate")
        rows = s.conn.execute("SELECT step_id, gate_decision, gate_reason, gate_class FROM step_events ORDER BY seq")
        assert [tuple(r) for r in rows] == [("s07+enter", "refused", "guarded_key_refused", "safety"),
                                            ("s06", "dropped", "token_expired", "integrity"),
                                            ("s05", "emitted", None, "allowed"), ("s04", "none", None, None)]
        assert s.step_outcomes(RUN) == [refused, dropped, emitted, plain]
        assert s.gateway_drops(RUN) == 2                          # 9.11: dropped or refused rows
        for bad in ({"decision": "refused"},                                       # a refusal names its code
                    {"decision": "refused", "reason": "blocked", "class": "safety"},   # a coarse class, not a code
                    {"decision": "refused", "reason": "no_token", "class": "fatal"},
                    {"reason": "no_token"}, {"decision": "dropped", "reason": "no_token", "code": "x"}):
            with pytest.raises(StoreRefused):
                s.write_steps([StepOutcome(RUN, flow, "s08", "blocked_by_gateway", gateway_event=bad)],
                              shard_id=f"{RUN}/1", side="candidate")
        assert s.gateway_drops(RUN) == 2


def test_an_orders_sent_proof_blocks_complete(tmp_path):
    """6.6: ORDERS SENT (8.3 verdict orders_sent) makes the run aborted_safety; the store never lets it be complete."""
    with Store(tmp_path / "run.db") as s:
        s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(plan(shards=(ShardPlan(f"{RUN}/1", "rig-a1"),)))
        s.conn.execute("INSERT INTO post_run_proofs (shard_id, run_id, login_alias, verdict, reason, send_policy_ref, "
                       "counts_json, cancel_all, paper_mode_assertions, paper_mode_mismatches, gateway_events, "
                       "gateway_drops, produced_at, verified_by) VALUES (?, ?, 'paper-01', 'orders_sent', NULL, "
                       "'dec-1', '{}', 'performed_verified', 1, 0, 0, 0, '2026-11-09T00:00:00+00:00', 'jdoe')",
                       (f"{RUN}/1", RUN))
        finish_all(s)
        s.end_shard(f"{RUN}/1", "complete")
        s.mark_shard_imported(f"{RUN}/1")
        assert s.complete_blockers(RUN) == ["1 ORDERS SENT post-run proof(s)"]
        with pytest.raises(RunNotComplete, match="ORDERS SENT"):
            s.end_run(RunOutcome(RUN, "pr", "complete", "reported"))
        with pytest.raises(sqlite3.IntegrityError, match="orders-sent"):
            s.conn.execute("UPDATE runs SET lifecycle = 'finished', outcome = 'complete' WHERE run_id = ?", (RUN,))


def test_steps_and_findings_need_a_shard_and_round_trip(tmp_path):
    with Store(tmp_path / "run.db") as s:
        s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(plan(shards=(ShardPlan(f"{RUN}/1", "rig-a1"),)))
        step = StepOutcome(RUN, "flow.trade.order-ticket.stock.limit", "s07", "passed_via_agent_fallback",
                           locator_rung="agent", finding_ref="F-2")
        f = Finding("F-1", RUN, "collateral", "ladder overlaps", RUN, checkpoint_id=FLOW, phash=ph(), tier=2,
                    category="collateral")
        with pytest.raises(StoreRefused, match="shard"):
            s.write_checkpoint(out(FLOW, "collateral"), steps=[step])
        s.write_checkpoint(out(FLOW, "collateral"), steps=[step], findings=[f], shard_id=f"{RUN}/1")
        assert s.step_outcomes(RUN) == [step] and s.findings(RUN) == [f]
        finish_all(s)
        with pytest.raises(RunNotComplete, match="not imported"):
            s.end_run(RunOutcome(RUN, "pr", "complete", "reported"))
        s.end_shard(f"{RUN}/1", "complete")
        s.mark_shard_imported(f"{RUN}/1")
        assert s.end_run(RunOutcome(RUN, "pr", "complete", "reported")).status == "complete"


def test_builds_and_fixed_in_build(store):
    facts = dict(channel="branch", source_commit="7be42d0", pipeline_artifact_url="https://ci/1",
                 installer_sha256="9" * 64)
    store.register_build("branch-1998.3.0-7be42d0", **facts)
    store.register_build("branch-1998.3.0-7be42d0", **facts)
    with pytest.raises(ImportConflict):
        store.register_build("branch-1998.3.0-7be42d0", **{**facts, "source_commit": "other"})
    store.register_build("branch-1998.2.0-1aa2b3c", **{**facts, "source_commit": "1aa2b3c"},
                         first_seen_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(StoreRefused):                       # fixed_in_build needs the build (8.3 CHECK)
        record(store, "dec-000001", "fixed_in_build", "confirmed_bug", build_id="branch-1998.2.0-1aa2b3c")
    with pytest.raises(StoreRefused):                       # ... and the older build it is newer than (L-DEC-08)
        record(store, "dec-000001", "fixed_in_build", "confirmed_bug", fixed_in_build_id="branch-1998.3.0-7be42d0")
    d = record(store, "dec-000001", "fixed_in_build", "confirmed_bug", build_id="branch-1998.2.0-1aa2b3c",
               fixed_in_build_id="branch-1998.3.0-7be42d0")
    assert d.action == "fixed_in_build" and d.ticket == "TOS-1"


# -- run completion (8.1, 8.3, 8.9) ---------------------------------------------------------------------------------

def test_complete_with_a_missing_outcome_refused(store):
    store.write_checkpoint(out(CENSUS))
    store.write_checkpoint(out(SWEEP))
    done = RunOutcome(RUN, "pr", "complete", "reported")
    with pytest.raises(RunNotComplete, match="without an outcome, first " + FLOW):
        store.end_run(done)
    with pytest.raises(sqlite3.IntegrityError, match="complete"):
        store.conn.execute("UPDATE runs SET lifecycle = 'finished', outcome = 'complete' WHERE run_id = ?", (RUN,))
    store.write_checkpoint(out(FLOW, "collateral"))
    result = store.end_run(done)
    assert result.status == "complete" and result.counts["identical"] == 2 and result.counts["collateral"] == 1
    with pytest.raises(StoreRefused, match="finished"):
        store.write_checkpoint(out(FLOW, "intended"))


def test_complete_refused_after_an_artifact_persist_failure(store):
    store.write_checkpoint(out(CENSUS, "harness_error", reason="artifact_persist_failed"))
    finish_all(store)
    with pytest.raises(RunNotComplete, match="store-failure"):
        store.end_run(RunOutcome(RUN, "pr", "complete", "reported"))
    r = store.end_run(RunOutcome(RUN, "pr", "aborted_harness", "teardown", reason="artifact_persist_failed"))
    assert r.reason == "artifact_persist_failed" and store.run_counts(RUN)["harness_error"] == 1


def test_env_error_run_and_unreached_checkpoints(store):
    store.write_checkpoint(out(CENSUS))
    filled = store.fill_unreached(RUN, "env_error", "paper_mode_mismatch", gate="paper_mode_asserted")
    assert filled == [FLOW, SWEEP]
    with pytest.raises(RunNotComplete, match="env_error"):
        store.end_run(RunOutcome(RUN, "pr", "complete", "reported"))
    r = store.end_run(RunOutcome(RUN, "pr", "aborted_safety", "paper_mode_asserted", reason="paper_mode_mismatch"))
    assert store.run_outcome(RUN) == r and r.counts["env_error"] == 2


def test_preflight_reason_renders_with_a_dot(store):
    store.fill_unreached(RUN, "harness_error", "preflight.H7")
    row = store.conn.execute("SELECT reason, reason_detail FROM outcomes WHERE checkpoint_id = ?", (CENSUS,))
    assert tuple(row.fetchone()) == ("preflight", "H7")
    r = store.end_run(RunOutcome(RUN, "pr", "aborted_harness", "provisioned", reason="preflight.H7"))
    assert r.reason == "preflight.H7"


# -- the later changes of 4.13 and F28 ------------------------------------------------------------------------------

def review(cid=CENSUS, mapped="intended", verdict="matches_manifest", diff_hash="h1"):
    return Review(run_id=RUN, checkpoint_id=cid, diff_hash=diff_hash, model_id="model-x", prompt_hash="p1",
                  request_sha256="f" * 64, response_json="{}", verdict=verdict, mapped_outcome=mapped,
                  confidence=0.93)


def write_pending(store, cid=CENSUS):
    store.write_checkpoint(out(cid, "needs_human", reason="reviewer_unavailable", review_state="pending"))


def test_pending_review_transition(store):
    write_pending(store)
    o = store.apply_review(review())
    assert (o.outcome, o.reason, o.review_state) == ("intended", None, "done")
    assert store.conn.execute("SELECT review_id FROM outcomes WHERE checkpoint_id = ?", (CENSUS,)).fetchone()[0]
    with pytest.raises(StoreRefused, match="not a pending review"):
        store.apply_review(review(diff_hash="h2"))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE outcomes SET review_state = 'pending' WHERE checkpoint_id = ?", (CENSUS,))


def test_review_outcomes_are_limited(store):
    write_pending(store)
    with pytest.raises(StoreRefused):
        store.apply_review(review(mapped="noise"))                      # noise only through the A/A floor
    with pytest.raises(StoreRefused):
        store.apply_review(review(mapped="needs_human"), reason="reviewer_cap")
    o = store.apply_review(review(mapped="needs_human", verdict="cannot_judge"), reason="reviewer_uncertain")
    assert (o.outcome, o.reason, o.review_state) == ("needs_human", "reviewer_uncertain", "done")


def test_end_of_review_without_verdict(store):
    write_pending(store)
    write_pending(store, SWEEP)
    o = store.end_review(RUN, CENSUS, "reviewer_cap")
    assert (o.outcome, o.reason, o.detail, o.review_state) == ("needs_human", "reviewer_cap", "cause: dollar_cap",
                                                               "done")
    assert store.end_review(RUN, SWEEP, "reviewer_unavailable").review_state == "done"
    with pytest.raises(StoreRefused):
        store.end_review(RUN, CENSUS, "reviewer_cap")


def record(store, did, action, verdict, phash=ph(), cid=SWEEP, category="collateral", by="alice", **kw):
    ticket = kw.pop("ticket", "TOS-1" if action in ("confirm_bug", "mark_intended", "fixed_in_build") else None)
    tol = kw.pop("hamming_tolerance", 8)
    d = Decision(did, action, verdict, Signature(cid, "rgn.ladder.bid-column", phash, category), decided_by=by,
                 ticket=ticket, reason="because", hamming_tolerance=tol,
                 decided_at=kw.pop("decided_at", "2026-11-09T15:05:32-05:00"))
    return store.record_decision(d, run_id=RUN, **kw)


def test_human_decision_wins_over_a_late_verdict(store):
    write_pending(store, SWEEP)
    f = Finding("F-1", RUN, "needs_human", "unreviewed diff", RUN, checkpoint_id=SWEEP, phash=ph(), tier=2,
                category="needs_human")
    store.write_finding(f)
    record(store, "dec-000001", "confirm_bug", "confirmed_bug", category="needs_human", finding_id="F-1")
    assert store.get_finding("F-1").triage_state == "confirmed_bug"
    o = store.apply_review(review(SWEEP, mapped="intended"))
    assert (o.outcome, o.reason, o.review_state) == ("needs_human", "reviewer_unavailable", "done")
    assert store.conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1   # stored as an annotation


def test_decision_ref_is_set_once(store):
    store.write_checkpoint(out(SWEEP, "invariant_violation", reason="INV-01", invariant_ids=["INV-01"]))
    record(store, "dec-000001", "mark_intended", "intended", category="invariant_violation",
           expires_at="2026-12-01T00:00:00+00:00")                  # a waiver expires within 30 days (L-DEC-07)
    record(store, "dec-000002", "confirm_bug", "confirmed_bug", category="invariant_violation", phash=ph(range(20)))
    o = store.set_decision_ref(RUN, SWEEP, "dec-000001")
    assert (o.outcome, o.decision_ref) == ("invariant_violation", "dec-000001")
    with pytest.raises(StoreRefused, match="set once"):
        store.set_decision_ref(RUN, SWEEP, "dec-000002")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE outcomes SET decision_id = 2 WHERE checkpoint_id = ?", (SWEEP,))


# -- decisions (4.14, 8.3, F25) -------------------------------------------------------------------------------------

def test_ai_decision_refused(store):
    d = Decision("dec-000001", "noise_signature", "noise", Signature(SWEEP, "r", ph(), "collateral"),
                 decided_by="alice", reason="flicker")
    for who in ("reviewer", "ai", "Orchestrator"):
        d.decided_by = who
        with pytest.raises((StoreRefused, TaxonomyError)):
            store.record_decision(d, run_id=RUN)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO decisions (checkpoint_id, region_id, phash, category, action, verdict, source, decided_by, "
            "decided_at, run_id, reason, expires_at) VALUES (?, 'r', ?, 'collateral', 'noise_signature', 'noise', "
            "'report_ui', 'reviewer', 'now', ?, 'x', 'later')", (SWEEP, bytes.fromhex(ph()), RUN))
    with pytest.raises(StoreRefused):
        store.revoke_decision("dec-000001", by="system", reason="x")


def test_decision_defaults_and_tier1_tolerance(store):
    d = record(store, "dec-000007", "noise_signature", "noise")
    assert d.decided_at == "2026-11-09T20:05:32+00:00" and d.hamming_tolerance == 8
    row = store.conn.execute("SELECT expires_at FROM decisions WHERE decision_id = 7").fetchone()
    assert row[0] == "2026-12-09T20:05:32+00:00"
    assert record(store, "dec-000008", "noise_signature", "noise", cid=CENSUS).hamming_tolerance == 6
    store.conn.execute("INSERT INTO regions (checkpoint_id, region_id, config_commit, kind, landmark_id, dx, dy, w, h, "
                       "hamming_tolerance) VALUES (?, 'rgn.ladder.bid-column', 'c0ffee', 'diff', 'lm', 0, 0, 9, 9, 3)",
                       (SWEEP,))
    assert record(store, "dec-000009", "noise_signature", "noise", phash=ph(range(30))).hamming_tolerance == 3


def test_lookup_within_tolerance_and_related(store):
    record(store, "dec-000001", "noise_signature", "noise")
    sig = lambda bits, cat="collateral": Signature(SWEEP, "rgn.ladder.bid-column", ph(bits), cat)  # noqa: E731
    hit = store.lookup_decisions(sig(range(8)))
    assert hit.inherited.decision_id == "dec-000001" and hit.matches[0].distance == 8 and not hit.conflict
    near = store.lookup_decisions(sig(range(9)))
    assert near.inherited is None and [m.distance for m in near.related] == [9]
    assert store.lookup_decisions(sig(range(13))).related == ()
    assert store.lookup_decisions(sig(()), at="2027-01-01T00:00:00+00:00").matches == ()   # expired
    assert store.lookup_decisions(sig((), "invariant_violation")).matches == ()           # category is the key
    assert store.lookup_decisions(sig(()), rescued=True).matches == ()


def test_tier1_lookup_uses_six_bits(store):
    record(store, "dec-000001", "noise_signature", "noise", cid=CENSUS)
    sig = lambda n: Signature(CENSUS, "rgn.ladder.bid-column", ph(range(n)), "collateral")  # noqa: E731
    assert store.lookup_decisions(sig(6)).inherited is not None
    assert store.lookup_decisions(sig(7)).inherited is None


def test_conflicting_verdicts_inherit_nothing(store):
    record(store, "dec-000001", "noise_signature", "noise", phash=ph())
    record(store, "dec-000002", "confirm_bug", "confirmed_bug", phash=ph(range(10)))     # 10 bits from the first
    lk = store.lookup_decisions(Signature(SWEEP, "rgn.ladder.bid-column", ph(range(5)), "collateral"))
    assert {m.decision.decision_id for m in lk.matches} == {"dec-000001", "dec-000002"}
    assert lk.conflict and lk.verdicts == {"noise", "confirmed_bug"} and lk.inherited is None
    computed = out(SWEEP, "collateral")
    assert apply_inheritance(computed, lk) == computed


def test_agreeing_verdicts_inherit_nearest_then_newest(store):
    record(store, "dec-000001", "noise_signature", "noise", phash=ph((1,)), decided_at="2026-11-01T00:00:00+00:00")
    record(store, "dec-000002", "noise_signature", "noise", phash=ph((1,)), decided_at="2026-11-05T00:00:00+00:00")
    record(store, "dec-000003", "noise_signature", "noise", phash=ph((1, 2, 3)))
    lk = store.lookup_decisions(Signature(SWEEP, "rgn.ladder.bid-column", ph(), "collateral"))
    assert [m.decision.decision_id for m in lk.matches] == ["dec-000002", "dec-000001", "dec-000003"]
    new = apply_inheritance(out(SWEEP, "collateral"), lk)
    assert (new.outcome, new.reason, new.decision_ref) == ("noise", "decision:dec-000002", "dec-000002")
    store.write_checkpoint(new)
    assert store.get_outcome(RUN, SWEEP) == new
    assert apply_inheritance(out(SWEEP, "collateral", rescued=True), lk).outcome == "collateral"   # 5.5


def test_confirmed_bug_keeps_the_computed_outcome(store):
    record(store, "dec-000001", "confirm_bug", "confirmed_bug")
    lk = store.lookup_decisions(Signature(SWEEP, "rgn.ladder.bid-column", ph(), "collateral"))
    new = apply_inheritance(out(SWEEP, "collateral"), lk)
    assert (new.outcome, new.reason, new.decision_ref) == ("collateral", "decision:dec-000001", "dec-000001")
    inv = apply_inheritance(out(SWEEP, "invariant_violation", reason="INV-02"), lk)
    assert (inv.outcome, inv.reason, inv.decision_ref) == ("invariant_violation", "INV-02", "dec-000001")


def test_supersede_and_revoke(store):
    record(store, "dec-000001", "noise_signature", "noise")
    record(store, "dec-000002", "confirm_bug", "confirmed_bug", supersedes="dec-000001")
    assert store.get_decision("dec-000001").status == "superseded"
    lk = store.lookup_decisions(Signature(SWEEP, "rgn.ladder.bid-column", ph(), "collateral"))
    assert [m.decision.decision_id for m in lk.matches] == ["dec-000002"]
    revoked = store.revoke_decision("dec-000002", by="bob", reason="wrong ticket")
    assert revoked.status == "revoked"
    assert store.lookup_decisions(Signature(SWEEP, "rgn.ladder.bid-column", ph(), "collateral")).matches == ()
    with pytest.raises(sqlite3.IntegrityError):         # revoke fields come together (8.3)
        store.conn.execute("UPDATE decisions SET revoked_by = NULL WHERE decision_id = 2")


def test_decision_constraints_from_8_3(tmp_path):
    with Store(tmp_path / "run.db") as s:
        s.begin_run(plan(pr_author="carol"))
        with pytest.raises(StoreRefused, match="two-person"):
            record(s, "dec-000001", "mark_intended", "intended", by="carol")
        bad = Decision("dec-000002", "noise_signature", "noise", Signature(SWEEP, "r", "xyz", "collateral"),
                       decided_by="alice", reason="r")
        with pytest.raises(StoreRefused, match="16 hex"):
            s.record_decision(bad, run_id=RUN)
        with pytest.raises(StoreRefused, match="dec-"):
            s.record_decision(dataclasses.replace(bad, decision_id="D7"), run_id=RUN)


# -- canary results (8.3 canary_results, 10.3) ---------------------------------------------------------------------

def test_canary_results_hold_one_row_per_mutant(tmp_path):
    from sentinel.canary import CanaryResult

    night = "run-n1"
    rows = [
        CanaryResult("M01", "cat-v3", CENSUS, "erased_control", False, 11, ["collateral"], "detected",
                     base_sha256="a" * 64, mutant_sha256="b" * 64, produced_outcome="collateral"),
        CanaryResult("M04", "cat-v3", CENSUS, "swapped_digit", True, 12, ["collateral", "assertion_failed"],
                     "detector_blind", produced_outcome="identical", absorbed_by="msk-7"),   # same checkpoint as M01
        CanaryResult("M25", "cat-v3", SWEEP, "aa_control", False, 13, ["identical"], "detected",
                     produced_outcome="identical"),
        CanaryResult("M25", "cat-v3", FLOW, "aa_control", False, 13, ["identical"], "detector_degraded",
                     produced_outcome="noise", class_rate=0.5, class_floor=0.9, model_id="claude-x",
                     prompt_hash="c" * 64),                                                   # one id, two checkpoints
    ]
    with Store(tmp_path / "run.db") as s:
        s.begin_run(dataclasses.replace(plan(night), run_type="nightly"))
        s.write_canary_results(night, rows, created_at="2026-11-10T03:12:00-05:00")
        assert s.canary_results(night) == rows
        assert [r[0] for r in s.conn.execute("SELECT DISTINCT created_at FROM canary_results")] == [
            "2026-11-10T08:12:00+00:00"]
        s.write_canary_results(night, rows[:2])                       # replay: a no-op (8.2)
        assert len(s.canary_results(night)) == 4
        with pytest.raises(ImportConflict):
            s.write_canary_results(night, [dataclasses.replace(rows[0], state="missed", produced_outcome="identical")])
        bad = dataclasses.replace(rows[2])
        bad.state = "detector_blind"                                  # mutable; a non-money row is never blind (8.3)
        with pytest.raises(TaxonomyError):
            s.write_canary_results(night, [bad])
        with pytest.raises(StoreRefused, match="unknown run"):
            s.write_canary_results("run-x", rows)
        # the money-cell miss still writes its synthetic harness_error row to outcomes, beside its canary row
        s.write_checkpoint(out(CENSUS, "harness_error", run_id=night, reason="detector_blind", synthetic=True))
        assert s.get_outcome(night, CENSUS).reason == "detector_blind"
        assert s.run_counts(night)["harness_error"] == 0               # synthetic rows are not the run's counts
        assert len(s.canary_results(night)) == 4
    lines = [json.loads(x) for x in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert [x["batch"] for x in lines] == ["run_begin", "canary_results", "checkpoint"]
    assert {r["table"] for r in lines[1]["rows"]} == {"canary_results"} and len(lines[1]["rows"]) == 4
