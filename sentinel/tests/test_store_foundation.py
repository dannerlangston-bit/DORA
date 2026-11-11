"""Tests for the store's foundation tables, columns and helpers (8.3, SD-09, SD-16, SD-19, SD-20, SD-61, SD-78,
SD-80, SD-81, SD-82, SD-91, 11-rig-login-uniqueness, 08-step-events-api, 08-stats-tables, 08-egress-outbox)."""
import copy
import sqlite3

import pytest

from sentinel import canary
from sentinel.canary import CanaryResult
from sentinel.records import (CheckpointOutcome, Decision, EgressRow, Escape, ExplorerFinding, Extraction, Finding,
                              Fingerprint, MaintenanceItem, ModelUsage, OutboxJob, Proposal, RunOutcome, Signature,
                              StepEvent, SyntheticOutcome, outcome_id)
from sentinel.store.store import (ImportConflict, PostRunProof, Review, RunPlan, ScopeEntry, ShardPlan, Store,
                                  StoreRefused, seed_rows)
from sentinel.taxonomy import Taxonomy, TaxonomyError, load

RUN = "run-01"
SHARD = f"{RUN}/1"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
TS = "2026-11-09T14:00:00+00:00"
BUILD = "branch-1998.3.0-7be42d0"


def plan(run_id=RUN, run_type="pr", shards=True):
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=(ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
                          ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen")),
                   shards=(ShardPlan(f"{run_id}/1", "rig-a1"),) if shards else (),
                   base_commit_distance=2, authorization_sha256="sha256:auth", pr_committers=("ann", "bob"))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(plan())
    yield s
    s.close()


def out(cid, outcome="identical", **kw):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=outcome, workspace_id=WS, persona="active_trader",
                             data_mode="frozen", **kw)


def ev(**kw):
    base = dict(run_id=RUN, shard_id=SHARD, side="candidate", seq=1, source="orchestrator", wall_at=TS, mono_ms=4242,
                mode="scripted", action="click")
    return StepEvent(**{**base, **kw})


# -- the public writer path -------------------------------------------------------------------------------------

def test_batch_insert_update_and_read_only_query(store):
    with store.batch("test") as b:
        b.insert("system_state", {"key": "detector_state", "value": "ok", "updated_at": TS})
        assert b.update("system_state", {"key": "detector_state"}, {"value": "blind"}) == 1
    assert store.query("SELECT value FROM system_state WHERE key = ?", ("detector_state",))[0][0] == "blind"
    with pytest.raises(StoreRefused):
        store.query("DELETE FROM system_state")
    assert store.get_system_state("detector_state") == "blind"           # the refused query wrote nothing
    with pytest.raises(StoreRefused, match="no column"):
        store.insert("system_state", {"key": "k", "valu": "v", "updated_at": TS})
    with pytest.raises(StoreRefused, match="unknown table"):
        store.insert("no_such_table", {"a": 1})
    with pytest.raises(StoreRefused):
        store.insert("system_state; DROP TABLE runs", {"a": 1})


def test_system_state_keys(store):
    store.set_system_state("harness_freeze", "1", at=TS)
    store.set_system_state("harness_freeze", "0")
    assert store.get_system_state("harness_freeze") == "0"
    for key in ("schema_sha256", "schema_version", "favourite_color"):
        with pytest.raises(StoreRefused):
            store.set_system_state(key, "x")


# -- step events (08-step-events-api) ---------------------------------------------------------------------------

def test_events_round_trip_with_their_own_mono_ms(store):
    click = ev(screen_x=812, screen_y=440, win_x=12, win_y=40, keys=None, pre_hash="sha256:pre", post_hash="sha256:post",
               click_crop_sha256="sha256:crop", gate_decision="emitted", gate_class="allowed", token_id=7, injected=True,
               rect_w=96, rect_h=24, usage={"input_tokens": 12})
    chord = ev(seq=2, action="key_chord", keys="ctrl+t", mono_ms=4300, gate_decision="dropped", gate_reason="no_token",
               gate_class="safety")
    store.write_events([click, chord])
    store.write_events([click])                                             # replay: no-op
    assert store.step_events(RUN) == [click, chord]
    with pytest.raises(ImportConflict):
        store.write_events([ev(mono_ms=1)])
    assert store.gateway_drops(RUN) == 1


def test_secret_events_never_store_a_plaintext(store):
    store.write_events([ev(action="secret", target_text="<redacted:login.password>")])
    with pytest.raises(TaxonomyError):
        store.write_events([ev(seq=2, action="secret", target_text="hunter2")])
    with pytest.raises(sqlite3.IntegrityError):                            # the schema refuses it too
        store.conn.execute("INSERT INTO step_events (run_id, shard_id, side, seq, source, wall_at, mono_ms, mode, "
                           "action, gate_decision, target_text) VALUES (?, ?, 'candidate', 9, 'orchestrator', ?, 1, "
                           "'scripted', 'secret', 'none', 'hunter2')", (RUN, SHARD, TS))


def test_events_refuse_an_unknown_action_and_a_foreign_shard(store):
    with pytest.raises(TaxonomyError):
        ev(action="step")
    with pytest.raises(StoreRefused, match="not a shard"):
        store.write_events([ev(shard_id="run-02/1")])


# -- extractions, fingerprints ----------------------------------------------------------------------------------

def test_extractions_are_stored_once(store):
    x = Extraction(RUN, CENSUS, "candidate", "rule.rgn.qty.qty", "qty", "ocr", TS, region_id="rgn.qty", text="100",
                   value="100", confidence=91.5, second_pass=True, crop_sha256="sha256:c")
    store.write_extractions([x, x])
    assert store.extractions(RUN) == [x] and store.extractions(RUN, CENSUS) == [x]
    with pytest.raises(ImportConflict):
        store.write_extractions([Extraction(**{**x.__dict__, "value": "10"})])


def test_fingerprint_is_written_once(store):
    store.register_build(BUILD, channel="branch", source_commit="7be42d0", pipeline_artifact_url="sim://b",
                         installer_sha256="c" * 64)
    match = {"build_id": BUILD, "gpu": {"name": "Radeon™ Graphics"}, "render_probe_hash": "e3b0c44298fc1c14"}
    fp = Fingerprint(RUN, "candidate", "rig-a1", TS, match, record={"run_date": "2026-11-09"}, shard_id=SHARD)
    store.write_fingerprint(fp)
    store.write_fingerprint(fp)
    assert store.fingerprint(SHARD, "candidate") == fp
    with pytest.raises(ImportConflict):
        store.write_fingerprint(Fingerprint(RUN, "candidate", "rig-a1", TS, {**match, "gpu": {"name": "other"}},
                                            shard_id=SHARD))
    with pytest.raises(StoreRefused):
        store.write_fingerprint(Fingerprint(RUN, "base", "rig-a1", TS, match))      # no shard


# -- egress and outbox (08-egress-outbox) -----------------------------------------------------------------------

def test_egress_rows(store):
    eid = store.log_egress(EgressRow("api.anthropic.com", "review", "sent", 2048, run_id=RUN, redaction_version="r3",
                                     model_id="claude-x", request_id="req-1"))
    row = store.query("SELECT * FROM egress_log WHERE egress_id = ?", (eid,))[0]
    assert (row["status"], row["bytes"], row["redaction_version"]) == ("sent", 2048, "r3")
    store.log_egress(EgressRow("evil.example", "review", "refused", 0, refusal_reason="destination not allowlisted"))
    with pytest.raises(StoreRefused):
        store.insert("egress_log", {"sent_at": TS, "destination": "h", "purpose": "review", "status": "refused",
                                    "bytes": 0})


def test_outbox_job_lifecycle(store):
    jid = store.enqueue(OutboxJob("pr_comment", "github.internal", "sha256:p", created_at=TS, run_id=RUN))
    assert [j.job_id for j in store.due(TS)] == [jid]
    store.mark_failed(jid, error="502", next_attempt_at="2026-11-09T14:02:00+00:00")
    assert store.due("2026-11-09T14:01:00+00:00") == []
    assert store.due("2026-11-09T14:02:00+00:00")[0].attempts == 1
    store.mark_sent(jid, at="2026-11-09T14:02:05+00:00")
    j = store.outbox_job(jid)
    assert (j.state, j.sent_at, j.attempts) == ("sent", "2026-11-09T14:02:05+00:00", 2)
    with pytest.raises(StoreRefused):
        store.mark_failed(jid, error="late", next_attempt_at=TS)
    with pytest.raises(StoreRefused):
        store.enqueue(OutboxJob("slack", "slack", "sha256:q", state="failed"))


def test_abandoned_job_is_closed_by_a_human(store):
    jid = store.enqueue(OutboxJob("jira_create", "jira", "sha256:p"))
    with pytest.raises(StoreRefused):
        store.close_job(jid, by="amy")                                     # not abandoned yet
    store.abandon(jid)
    with pytest.raises(StoreRefused):
        store.close_job(jid, by="system")
    store.close_job(jid, by="amy", at=TS)
    assert store.outbox_job(jid).closed_by == "amy"
    with pytest.raises(StoreRefused):
        store.close_job(jid, by="bob")


def decision(store, did, action, verdict, ticket=None, **kw):
    sig = Signature(CENSUS, "rgn.ladder.bid-column", "0123456789abcdef", "collateral")
    return store.record_decision(Decision(did, action, verdict, sig, decided_by="amy", ticket=ticket,
                                          reason="because", decided_at=TS), run_id=RUN, **kw)


def test_ticket_fill_in_works_exactly_once(store):
    decision(store, "dec-000001", "confirm_bug", "confirmed_bug")
    assert store.fill_ticket_ref("dec-000001", "TOS-1").ticket == "TOS-1"
    with pytest.raises(StoreRefused):
        store.fill_ticket_ref("dec-000001", "TOS-2")
    decision(store, "dec-000002", "noise_signature", "noise")
    with pytest.raises(StoreRefused):
        store.fill_ticket_ref("dec-000002", "TOS-3")                       # only a confirm_bug


def test_mask_ref_is_set_once_on_request_mask(store):
    decision(store, "dec-000003", "request_mask", "noise")
    store.set_mask_ref("dec-000003", "mask-0001")
    with pytest.raises(StoreRefused):
        store.set_mask_ref("dec-000003", "mask-0002")
    decision(store, "dec-000004", "confirm_bug", "confirmed_bug", ticket="T-1")
    with pytest.raises(StoreRefused):
        store.set_mask_ref("dec-000004", "mask-0003")


# -- stats tables (08-stats-tables) -----------------------------------------------------------------------------

def floor(**kw):
    base = {"checkpoint_id": CENSUS, "region_id": "rgn.qty", "rendering_key_sha256": "sha256:rk",
            "calibration_run_id": RUN, "samples": 15, "threshold_component_area": 4, "threshold_changed_px": 4,
            "threshold_delta_e": 2.0, "threshold_shift_px": 1, "noise_spread": 2, "fault_separation": 9,
            "hamming_tolerance": 8, "settle_p50_ms": 640, "valid_from": TS}
    return {**base, **kw}


def test_calibration_supersedes_the_current_floor(store):
    first = store.write_calibration([floor()])
    second = store.write_calibration([floor(threshold_changed_px=6, valid_from="2026-11-10T00:00:00+00:00")],
                                     [{"checkpoint_id": CENSUS, "region_id": "rgn.qty", "cause_class": "caret",
                                       "owner": "flow-owner"}])
    current = store.query("SELECT floor_id FROM calibration_floors WHERE superseded_at IS NULL")
    assert [r[0] for r in current] == second and first != second
    fix = store.query("SELECT floor_id FROM calibration_fixes")[0]
    assert fix[0] == second[0]
    store.write_calibration([], [{"checkpoint_id": SWEEP, "cause_class": "settle", "outcome": "not_settled",
                                  "reason": "timeout", "owner": "o"}])           # a failed-capture item
    with pytest.raises(StoreRefused):
        store.write_calibration([], [{"checkpoint_id": SWEEP, "cause_class": "timing", "owner": "o",
                                      "outcome": "not_settled"}])                # not a 3.9 class
    with pytest.raises(StoreRefused):
        store.write_calibration([], [{"checkpoint_id": SWEEP, "cause_class": "settle", "owner": "o"}])


def test_flake_events_and_stats(store):
    store.write_flake_event(RUN, CENSUS, captures=["sha256:a", "sha256:b"], metrics={"changed_px": 12})
    store.replace_flake_stats([{"checkpoint_id": CENSUS, "observations": 5, "unstable_total": 1}])
    store.replace_flake_stats([{"checkpoint_id": CENSUS, "observations": 6, "unstable_total": 2}])
    assert store.query("SELECT observations FROM flake_stats")[0][0] == 6
    with pytest.raises(StoreRefused):
        store.replace_flake_stats([{"checkpoint_id": CENSUS, "quarantined": 1, "quarantine_commit": "c1"}])
    with pytest.raises(StoreRefused):
        store.replace_flake_stats([{"checkpoint_id": CENSUS, "quarantine_reason": "boredom"}])
    with pytest.raises(StoreRefused):
        store.write_flake_event(RUN, CENSUS, captures=["sha256:a"], metrics={})


def cell(run_id):
    return {"run_id": run_id, "surface_id": "trade.active-trader.ladder", "persona": "active_trader",
            "entitlement": "paper-margin", "tier": 1, "state": "covered", "worst_outcome": "identical",
            "checkpoint_count": 2, "profile_id": "paper-margin"}


def test_coverage_cells_only_for_merge_rc_and_nightly(store):
    with pytest.raises(StoreRefused):
        store.write_coverage_cells([cell(RUN)])
    store.begin_run(plan("run-n", "nightly", shards=False))
    store.write_coverage_cells([cell("run-n"), cell("run-n")])
    assert store.query("SELECT profile_id FROM coverage_cells")[0][0] == "paper-margin"


def test_evals_soak_and_usage(store):
    store.write_reviewer_eval(eval_set_version="ev-3", model_id="claude-x", prompt_hash="ph", items=200,
                              metrics={"tier1_recall": 0.97}, passed_gate=True)
    sample = {"sampled_at": TS, "pid": 4100, "metric": "handles", "value": 812}
    store.write_soak_samples(RUN, [sample, sample])
    with pytest.raises(ImportConflict):
        store.write_soak_samples(RUN, [{**sample, "value": 900}])
    store.write_model_usage(ModelUsage("explorer", "claude-x", TS, run_id=RUN, input_tokens=10, cost_usd=0.2))
    assert store.query("SELECT component, cost_usd FROM model_usage")[0][:] == ("explorer", 0.2)


# -- canaries (SD-81, SD-91) ------------------------------------------------------------------------------------

def test_synthetic_outcomes_and_the_canary_link(store):
    store.write_canary_results(RUN, [CanaryResult("M04", "cat-v3", CENSUS, "swapped_digit", True, 12,
                                                  ["collateral"], "detected")])
    sid = store.write_synthetic_outcome(SyntheticOutcome(RUN, "M04", CENSUS, "collateral", recorded_at=TS))
    assert store.write_synthetic_outcome(SyntheticOutcome(RUN, "M04", CENSUS, "collateral", recorded_at=TS)) is None
    assert store.query("SELECT synthetic_ref FROM canary_results")[0][0] == sid
    with pytest.raises(ImportConflict):
        store.write_synthetic_outcome(SyntheticOutcome(RUN, "M04", CENSUS, "identical", recorded_at=TS))
    store.write_synthetic_outcome(SyntheticOutcome(RUN, "M05", CENSUS, "not_run", reason="step_failed:s07",
                                                   recorded_at=TS))
    assert store.query("SELECT reason, reason_detail FROM synthetic_outcomes WHERE mutant_id = 'M05'")[0][:] == \
        ("step_failed", "s07")
    assert store.query("SELECT count(*) FROM outcomes")[0][0] == 0          # synthetic rows never enter outcomes


def insert_canary(store, kind, mutant_id, defect_class):
    store.conn.execute("INSERT INTO canary_results (run_id, kind, mutant_id, catalog_version, checkpoint_id, "
                       "defect_class, seed, expected_json, state, created_at) VALUES (?, ?, ?, 'v', ?, ?, 1, '[]', "
                       "'detected', ?)", (RUN, kind, mutant_id, CENSUS, defect_class, TS))


def test_defect_class_check_by_kind(store):
    for cls in canary.CLASSES:                                             # the CHECK list equals the 26 classes
        insert_canary(store, "mutant", f"M-{cls}", cls)
    insert_canary(store, "safety_canary", "attestation", "safety_canary")
    insert_canary(store, "fire_drill", "drill-3", "fire_drill")
    for kind, cls in (("mutant", "safety_canary"), ("mutant", "overlap"), ("safety_canary", "erased_control"),
                      ("path_equivalence", "fire_drill")):
        with pytest.raises(sqlite3.IntegrityError):
            insert_canary(store, kind, "x", cls)


def test_canary_manifest_once(store):
    store.write_canary_manifest(RUN, catalog_sha256="sha256:cat", manifest=[{"mutant_id": "M01", "seed": 1}])
    store.write_canary_manifest(RUN, catalog_sha256="sha256:cat", manifest=[{"mutant_id": "M01", "seed": 1}])
    with pytest.raises(ImportConflict):
        store.write_canary_manifest(RUN, catalog_sha256="sha256:cat", manifest=[{"mutant_id": "M01", "seed": 2}])


# -- triage records ---------------------------------------------------------------------------------------------

def finding(fid="fnd-1", **kw):
    return Finding(finding_id=fid, run_id=RUN, kind="collateral", description="x", first_seen_run_id=RUN,
                   checkpoint_id=CENSUS, **kw)


def test_proposals_need_a_second_human(store):
    store.write_finding(finding())
    pid = store.write_proposal(Proposal("fnd-1", RUN, "mark_intended", "ann", "padding change", ticket="TOS-5",
                                        proposed_at=TS))
    with pytest.raises(StoreRefused):                                      # one open proposal per finding
        store.write_proposal(Proposal("fnd-1", RUN, "mark_intended", "bob", "again"))
    with pytest.raises(TaxonomyError, match="different human"):
        store.update_proposal(pid, state="declined", decided_by="ann", decline_reason="no")
    decision(store, "dec-000009", "mark_intended", "intended", ticket="TOS-5")
    p = store.update_proposal(pid, state="confirmed", decided_by="bob", decision_id="dec-000009")
    assert (p.state, p.decided_by, p.decision_ref) == ("confirmed", "bob", "dec-000009")
    with pytest.raises(StoreRefused):
        store.update_proposal(pid, state="declined", decided_by="cat", decline_reason="late")


def test_escapes_maintenance_and_assignments(store):
    store.write_escape(Escape("1998.3.0", "TOS-9", "covered_missed", "amy", checkpoint_id=CENSUS, tier=1))
    store.write_escape(Escape("1998.3.0", "TOS-10", "not_registered", "amy"))
    with pytest.raises(StoreRefused):
        store.write_escape(Escape("1998.3.0", "TOS-9", "not_run", "amy", checkpoint_id=CENSUS))   # one per ticket
    store.write_maintenance_item(MaintenanceItem("update_scripted_flow", "flow.trade.x/s04", "flow-owner", TS,
                                                 hours=1.5))
    store.write_finding(finding("fnd-2"))
    store.write_triage_assignment("fnd-2", assignee="ann", assigned_by="system", at=TS)
    store.write_triage_assignment("fnd-2", assignee="bob", assigned_by="amy", escalation_step=1)
    rows = store.query("SELECT assignee, released_at FROM triage_assignments ORDER BY assignment_id")
    assert [r[0] for r in rows] == ["ann", "bob"] and rows[0][1] is not None and rows[1][1] is None


def test_artifact_sets_need_an_indexed_object(store):
    with pytest.raises(StoreRefused):
        store.write_artifact_set("d" * 64, f"runs/{RUN}")
    store.conn.execute("INSERT INTO artifacts (sha256, kind, media_type, bytes, redaction_passed, set_path, created_at) "
                       "VALUES (?, 'log_slice', 'text/plain', 3, 1, ?, ?)", ("d" * 64, f"runs/{RUN}", TS))
    store.write_artifact_set("d" * 64, f"runs/{RUN}")
    store.write_artifact_set("d" * 64, f"runs/{RUN}")
    store.write_artifact_set("d" * 64, "baselines/released/1998.2.0")
    assert store.query("SELECT count(*) FROM artifact_sets")[0][0] == 2


# -- the fleet (11-rig-login-uniqueness) ------------------------------------------------------------------------

def test_one_login_is_never_on_two_active_rigs(store):
    with pytest.raises(StoreRefused):
        store.register_rig("rig-a2", login_alias="paper-01", owner="qa", snapshot_id="snap-2", allowlist_entry=1)


def test_explorer_rigs_run_the_explorer_login_only(store):
    with pytest.raises(StoreRefused):
        store.register_rig("rig-06", login_alias="explore-01", owner="qa", snapshot_id="snap-6", allowlist_entry=6,
                           role="explorer")                                # default login kind scripted
    store.register_rig("rig-06", login_alias="explore-01", owner="qa", snapshot_id="snap-6", allowlist_entry=6,
                       role="explorer", login_kind="explorer")
    with pytest.raises(StoreRefused):
        store.register_rig("rig-a3", login_alias="explore-01x", owner="qa", snapshot_id="s", allowlist_entry=7,
                           login_kind="explorer")                          # a pool rig on an explorer login


def test_reassign_login_moves_it_and_records_the_operator(store):
    store.register_rig("rig-06", login_alias="spare-01", owner="qa", snapshot_id="snap-6", allowlist_entry=6)
    with pytest.raises(StoreRefused):
        store.reassign_login("rig-a1", "rig-06", operator="jdoe", snapshot_id="snap-6")    # not re-snapshotted
    store.reassign_login("rig-a1", "rig-06", operator="jdoe", snapshot_id="snap-6-paper-01", at=TS)
    rows = {r["rig_id"]: r for r in store.query("SELECT * FROM rigs")}
    assert rows["rig-a1"]["active"] == 0 and rows["rig-06"]["login_alias"] == "paper-01"
    assert rows["rig-06"]["reassigned_by"] == rows["rig-a1"]["reassigned_by"] == "jdoe"
    with pytest.raises(StoreRefused):
        store.reassign_login("rig-a1", "rig-06", operator="system", snapshot_id="snap-x")


# -- the new outcome, finding, run and proof columns ------------------------------------------------------------

def test_outcome_columns_and_record_round_trip(store):
    o = out(CENSUS, outcome_id=outcome_id(RUN, 1), profile_id="paper-margin", settle_ms=640, cand_rig_id="rig-a1",
            base_rig_id="rig-a1", diff_pixels=0, masked_pct=1.5, data_epoch_id="sha256:ep",
            paper_assertion={"account_id_sha256": "sha256:acc", "mode": "paper", "asserted_at": TS},
            app_uptime_s=100, app_restart_count=0, lower_results=[{"outcome": "collateral"}])
    store.write_checkpoint(o, shard_id=SHARD)
    assert store.get_outcome(RUN, CENSUS) == o
    row = store.query("SELECT outcome_id, profile_id, settle_ms, paper_asserted_at, data_epoch_id FROM outcomes")[0]
    assert row[:] == (o.outcome_id, "paper-margin", 640, TS, "sha256:ep")
    with pytest.raises(StoreRefused):                                      # outcome_id is unique
        store.write_checkpoint(out(SWEEP, outcome_id=outcome_id(RUN, 1)))


def test_fingerprint_mismatch_needs_its_fields(store):
    with pytest.raises(sqlite3.IntegrityError, match="SD-78"):
        store.conn.execute("INSERT INTO outcomes (run_id, checkpoint_id, outcome, reason, reason_detail, recorded_at) "
                           "VALUES (?, ?, 'not_comparable', 'fingerprint_mismatch', ' ', ?)", (RUN, CENSUS, TS))
    store.write_checkpoint(out(CENSUS, "not_comparable", reason="fingerprint_mismatch", detail="font_smoothing"))


def test_finding_columns_round_trip(store):
    f = Finding(finding_id="fnd-x", run_id=RUN, kind="explorer_candidate", description="x", first_seen_run_id=RUN,
                severity="S2", side="candidate", step_id="s04", detail="d", pre_existing=True, cluster_id="cl-1",
                contested=True, held=True, closed_at=TS, created_at=TS,
                explorer=ExplorerFinding("xs-1", {"surface_id": "trade.all-products", "observation": "INV-03",
                                                  "crop_sha256": "sha256:c"}, "high", 0.8, "sha256:t", "reproduced"))
    store.write_finding(f)
    assert store.get_finding("fnd-x") == f
    store.write_finding(f)                                                 # replay
    with pytest.raises(StoreRefused):                                      # the explorer block needs its kind
        store.insert("findings", {"natural_key": "k", "run_id": RUN, "kind": "collateral", "description": "x",
                                  "evidence_json": "{}", "created_at": TS, "explorer_json": "{}"})


def finish(store):
    for cid in (CENSUS, SWEEP):
        if store.get_outcome(RUN, cid) is None:
            store.write_checkpoint(out(cid))
    store.end_shard(SHARD, "complete")
    store.mark_shard_imported(SHARD)


def test_run_outcome_refs_round_trip(store):
    finish(store)
    r = store.end_run(RunOutcome(RUN, "pr", "complete", "reported", summary={"health": ["ok"]},
                                 report_sha256="sha256:r", comment_sha256="sha256:c", egress_log_ref="sha256:e",
                                 coverage_matrix_ref="sha256:m", base_commit_distance=2,
                                 authorization_sha256="sha256:auth"))
    assert (r.summary, r.report_sha256, r.egress_log_ref, r.base_commit_distance) == \
        ({"health": ["ok"]}, "sha256:r", "sha256:e", 2)
    row = store.query("SELECT pr_committers_json, authorization_sha256 FROM runs")[0]
    assert row[:] == ('["ann", "bob"]', "sha256:auth")


def test_run_complete_trigger_holds_the_1_10_conditions(store):
    """SD-80: trg_run_complete itself refuses a fingerprint mismatch, whatever the caller checked."""
    store.write_checkpoint(out(CENSUS, "not_comparable", reason="fingerprint_mismatch", detail="gpu"))
    finish(store)
    with pytest.raises(sqlite3.IntegrityError, match="fingerprint mismatch"):
        store.conn.execute("UPDATE runs SET lifecycle = 'finished', outcome = 'complete', finished_at = ? "
                           "WHERE run_id = ?", (TS, RUN))


def proof(**kw):
    base = dict(shard_id=None, run_id=RUN, login_alias=None, verdict="proven_no_session", send_policy_ref="dec-1",
                counts={}, cancel_all="not_needed", paper_mode_assertions=0, paper_mode_mismatches=0,
                gateway_events=0, gateway_drops=0, produced_at=TS)
    return PostRunProof(**{**base, **kw})


def test_run_level_proof_and_set_once_clearance(store):
    store.write_proof(proof())                                             # the run-level no-session row (SD-61)
    store.write_proof(proof())
    with pytest.raises(ImportConflict):
        store.write_proof(proof(verdict="proven_no_sends"))
    store.write_proof(proof(shard_id=SHARD, login_alias="paper-01", verdict="unproven", reason="monitor_unreadable",
                            unreadable_count=2, refusals_count=1, attestation_ref="sha256:att"))
    with pytest.raises(StoreRefused):
        PostRunProof(**{**proof().__dict__, "shard_id": SHARD})            # a shard's proof names its login
    store.update("post_run_proofs", {"shard_id": SHARD}, {"verified_by": "jdoe", "verified_at": TS})
    with pytest.raises(StoreRefused, match="once"):
        store.update("post_run_proofs", {"shard_id": SHARD}, {"verified_by": "kim"})
    with pytest.raises(StoreRefused):                                      # only an unproven verdict is cleared
        store.update("post_run_proofs", {"run_id": RUN, "verdict": "proven_no_session"}, {"verified_by": "kim",
                                                                                         "verified_at": TS})
    assert [p.unreadable_count for p in store.post_run_proofs(RUN)] == [0, 2]


def test_review_provenance_columns(store):
    store.write_checkpoint(out(CENSUS, "needs_human", reason="reviewer_unavailable", review_state="pending"))
    store.apply_review(Review(RUN, CENSUS, "dh", "claude-x", "ph", "sha256:req", "{}", "matches_manifest", "intended",
                              0.93, task="manifest_match", registry_version="reg-7", cache_read_tokens=100,
                              request_ids_json='["req-1"]', votes_json="[]"))
    row = store.query("SELECT task, registry_version, cache_read_tokens FROM reviews")[0]
    assert row[:] == ("manifest_match", "reg-7", 100)


def test_ladder_f_ascending_sweep_ids_register(store, monkeypatch):
    """SD-04: `<w>x<h>-asc` sweep ids (the taxonomy grammar admits them once regenerated) keep their size."""
    from sentinel.store import store as store_module
    cid = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360-asc"
    kind, surface, host, size = cid.split("/")
    monkeypatch.setattr(store_module, "checkpoint_parts",
                        lambda c: {"kind": kind, "surface": surface, "qualifier": f"{host}/{size}"})
    row = store._checkpoint_row(ScopeEntry(cid, WS, "active_trader", 2, "frozen"), "c0ffee")
    assert (row["host"], row["size_w"], row["size_h"]) == ("flexgrid", 480, 360)


def test_seed_rows_read_reason_metadata():
    data = copy.deepcopy(load().data)
    data["reason_metadata"] = {"not_run": {"step_failed": {"owner_section": "5.2",
                                                           "detail_means": "step id that ended failed"}}}
    rows = {(r[0], r[1]): r for r in seed_rows(Taxonomy(data))}
    assert rows[("not_run", "step_failed")] == ("not_run", "step_failed", 1, "step id that ended failed", "5.2")
    assert rows[("not_run", "budget")][4] == "4.13"                        # not in the map: the fallback
    assert rows[("not_comparable", "fingerprint_mismatch")][2] == 0         # SD-78: plain
