"""Tests for Store.replay, Store.load_export and `sentinel store rebuild` (8.1, 8.2, 8.7, 8.9): a lost database is
rebuilt from its ledger row for row, replay is idempotent, and a replayed row that differs is an import conflict."""
import json
import sqlite3

import pytest

from sentinel.records import CheckpointOutcome, Decision, Finding, RunOutcome, Signature, StepOutcome
from sentinel.store import ImportConflict, Review, RunPlan, ScopeEntry, ShardPlan, Store, StoreError
from sentinel.store import store as store_mod
from sentinel.store.store import decisions_ledger_path, read_ledger

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
PH = "0123456789abcdef"
KEY = "sha256:" + "a" * 64
SKIP = ("outcome_reasons", "system_state")        # seeded at open from taxonomy.yaml, not by the ledger


def out(cid, outcome="identical", **kw):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=outcome, workspace_id=WS, persona="active_trader",
                             data_mode="frozen", **kw)


def decision(did, action, verdict, by="alice", **kw):
    return Decision(did, action, verdict, Signature(SWEEP, "rgn.ladder.bid-column", PH, kw.pop("category",
                                                                                                "collateral")),
                    decided_by=by, ticket=kw.pop("ticket", "TOS-1"), reason="because",
                    decided_at="2026-11-09T15:05:32-05:00", **kw)


def history(s: Store) -> None:
    """A run with outcomes, steps, a finding with a decision that is later superseded, a review landing on a
    pending row, calibration floors superseded, a triage assignment released and a revoke: every kind of op."""
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(RunPlan(run_id=RUN, run_type="merge", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, shards=(ShardPlan(f"{RUN}/1", "rig-a1"),),
                        scope=(ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
                               ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
                               ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"))))
    s.write_checkpoint(out(CENSUS, "needs_human", reason="reviewer_unavailable", review_state="pending"))
    f = Finding("F-1", RUN, "collateral", "ladder overlaps", RUN, checkpoint_id=SWEEP, phash=PH, tier=2,
                category="collateral")
    step = StepOutcome(RUN, "flow.trade.order-ticket.stock.limit", "s07", "passed", action="click")
    s.write_checkpoint(out(SWEEP, "collateral"), findings=[f], steps=[step], shard_id=f"{RUN}/1")
    s.write_checkpoint(out(FLOW))
    s.record_decision(decision("dec-000001", "confirm_bug", "confirmed_bug"), run_id=RUN, finding_id="F-1")
    s.record_decision(decision("dec-000002", "mark_intended", "intended", supersedes="dec-000001"), run_id=RUN,
                      finding_id="F-1")
    s.apply_review(Review(run_id=RUN, checkpoint_id=CENSUS, diff_hash="h1", model_id="model-x", prompt_hash="p1",
                          request_sha256="f" * 64, response_json="{}", verdict="matches_manifest",
                          mapped_outcome="intended", confidence=0.93))
    floor = {"region_id": "_rest", "calibration_run_id": RUN, "samples": 15, "threshold_component_area": 4,
             "threshold_changed_px": 4, "threshold_delta_e": 2.0, "threshold_shift_px": 1,
             "checkpoint_id": SWEEP, "rendering_key_sha256": KEY}
    s.write_calibration([{**floor, "valid_from": "2026-11-01T00:00:00Z"}])
    s.write_calibration([{**floor, "samples": 20, "valid_from": "2026-11-02T00:00:00Z"}])
    s.write_triage_assignment("F-1", assignee="ann", assigned_by="system", at="2026-11-09T21:00:00Z")
    s.write_triage_assignment("F-1", assignee="bob", assigned_by="amy", at="2026-11-09T22:00:00Z")
    s.set_system_state("detector_state", "ok")
    s.revoke_decision("dec-000002", by="carol", reason="wrong build")
    s.end_shard(f"{RUN}/1", "complete")
    s.mark_shard_imported(f"{RUN}/1")
    s.end_run(RunOutcome(RUN, "merge", "complete", "reported"))


def tables(path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                            "'sqlite_%' ORDER BY name") if n not in SKIP]
        return {n: sorted(conn.execute(f"SELECT * FROM {n}").fetchall(), key=repr) for n in names}
    finally:
        conn.close()


@pytest.fixture
def original(tmp_path):
    db = tmp_path / "orig" / "central.db"
    with Store(db) as s:
        history(s)
    return db


def test_a_lost_database_is_rebuilt_row_for_row_and_replay_twice_is_a_no_op(original, tmp_path):
    ledger = original.parent / "results.jsonl"
    want = tables(original)
    assert want["outcomes_superseded"] == [] and len(want["calibration_floors"]) == 2
    rebuilt = tmp_path / "new" / "central.db"
    with Store(rebuilt) as s:
        r = s.replay(ledger)
        assert r.batches == len(read_ledger(ledger)) and r.applied == r.batches
        assert s.get_decision("dec-000002").status == "revoked"
        assert s.get_finding("F-1").triage_state == "open"
    assert tables(rebuilt) == want                       # rowids included: the ledger carries them (8.2)
    assert [json.loads(x) for x in (rebuilt.parent / "results.jsonl").read_text().splitlines()] == [
        b for b, _ in read_ledger(ledger)]                 # the rebuilt store's ledger holds the same batches
    with Store(rebuilt) as s:
        assert s.replay(ledger).applied == 0             # idempotent: every row is already there
        assert s.replay(rebuilt.parent / "results.jsonl").applied == 0
    with Store(original) as s:                           # the original against its own ledger: a no-op too
        assert s.replay(ledger).applied == 0
    assert tables(rebuilt) == want and len(read_ledger(rebuilt.parent / "results.jsonl")) == len(read_ledger(ledger))


def test_replay_onto_an_older_snapshot_brings_it_forward(original, tmp_path):
    """8.9: restore the last good snapshot, then replay the later ledger: the rows the snapshot holds in an older
    state are moved on by the later ops, never reported as conflicts."""
    ledger = original.parent / "results.jsonl"
    lines = read_ledger(ledger)
    cut = next(i for i, (b, _) in enumerate(lines) if b["batch"] == "review")
    snap_ledger = tmp_path / "snap.jsonl"
    snap_ledger.write_text("".join(line + "\n" for _, line in lines[:cut]))
    snap = tmp_path / "snap" / "central.db"
    with Store(snap, ledger=False) as s:
        s.replay(snap_ledger)                            # the snapshot: everything before the review landed
        assert s.get_outcome(RUN, CENSUS).review_state == "pending"
        s.replay(ledger)
    assert tables(snap) == tables(original)


def test_a_tampered_ledger_line_is_an_import_conflict(original, tmp_path):
    ledger = original.parent / "results.jsonl"
    lines = ledger.read_text().splitlines()
    i = next(n for n, x in enumerate(lines) if json.loads(x)["batch"] == "checkpoint")
    batch = json.loads(lines[i])
    for op in batch["rows"]:
        if op["table"] == "outcomes":
            op["row"]["recorded_at"] = "2020-01-01T00:00:00+00:00"
    lines[i] = json.dumps(batch, sort_keys=True)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n")
    before = tables(original)
    with Store(original) as s:
        with pytest.raises(ImportConflict, match="recorded_at"):
            s.replay(tampered)
    assert tables(original) == before                    # nothing of the conflicting batch was written
    upd = tmp_path / "update.jsonl"                      # an update of a row the store lacks
    upd.write_text(json.dumps({"batch": "x", "at": "2026-11-10T00:00:00+00:00", "rows": [
        {"op": "update", "table": "findings", "key": {"natural_key": "F-404"}, "set": {"triage_state": "open"}}]})
                   + "\n")
    with Store(original) as s:
        with pytest.raises(ImportConflict, match="lacks"):
            s.replay(upd)


def test_torn_lines_and_annotations(tmp_path):
    ledger = tmp_path / "l.jsonl"
    good = json.dumps({"batch": "note", "at": "2026-11-10T00:00:00+00:00",
                       "rows": [{"op": "human_action", "action": "clear_proof"}]})
    ledger.write_text(good + "\n" + '{"batch": "torn"')           # the writer died mid-append
    with Store(tmp_path / "db" / "x.db") as s:
        assert s.replay(ledger) == store_mod.ReplayResult(1, 0, 0)  # the annotation changes nothing
    ledger.write_text('{"batch": "torn"\n' + good + "\n")
    with Store(tmp_path / "db" / "x.db") as s, pytest.raises(StoreError, match="line 1"):
        s.replay(ledger)


def test_decision_batches_also_go_to_the_days_decisions_ledger(tmp_path):
    db = tmp_path / "central.db"
    with Store(db, decisions_ledger=True) as s:
        history(s)
    files = sorted((tmp_path / "ledgers").iterdir())
    kinds = [b["batch"] for f in files for b, _ in read_ledger(f)]
    assert kinds == ["decision", "decision", "decision_revoke"]
    assert decisions_ledger_path(tmp_path / "ledgers", "2026-11-10T03:00:00+00:00").name == "decisions-2026-11-09.jsonl"
    rebuilt = tmp_path / "re" / "central.db"
    assert store_mod.main(["rebuild", str(rebuilt), "--ledger", str(tmp_path / "results.jsonl"),
                           *[x for f in files for x in ("--ledger", str(f))]]) == 0
    assert tables(rebuilt) == tables(db)


def test_rebuild_cli_loads_the_nightly_exports(original, tmp_path, capsys):
    exports = tmp_path / "exports"
    exports.mkdir()
    conn = sqlite3.connect(original)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)")]
    with open(exports / "decisions.jsonl", "w") as f:
        for row in conn.execute("SELECT * FROM decisions ORDER BY decision_id"):
            f.write(json.dumps({c: v.hex() if isinstance(v, bytes) else v for c, v in zip(cols, row)}) + "\n")
    conn.close()
    rebuilt = tmp_path / "re" / "central.db"
    assert store_mod.main(["rebuild", str(rebuilt), "--ledger", str(original.parent / "results.jsonl"),
                           "--exports", str(exports)]) == 0
    assert "0 inserted, 2 already stored, 0 kept" in capsys.readouterr().out
    # the ledger lost its decision batches: the exports put the decisions back
    partial = tmp_path / "partial.jsonl"
    partial.write_text("".join(line + "\n" for b, line in read_ledger(original.parent / "results.jsonl")
                               if not b["batch"].startswith("decision")))
    alone = tmp_path / "alone" / "central.db"
    with Store(alone) as s:
        s.replay(partial)
        assert s.load_export(exports / "decisions.jsonl") == store_mod.ExportResult("decisions", 2, 0, 0)
        assert s.get_decision("dec-000002").status == "revoked"
    assert store_mod.main(["rebuild"]) == 2
    tampered = tmp_path / "t.jsonl"
    tampered.write_text((original.parent / "results.jsonl").read_text().replace('"merge"', '"pr"', 1))
    assert store_mod.main(["rebuild", str(rebuilt), "--ledger", str(tampered)]) == 1
