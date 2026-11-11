"""Tests for the store's record integrity: L-OUT-02 and L-OUT-06 at write, the 8.3 outcome, step and finding
columns, decisions under 4.14 and 9.7, build pairs, the run summary, the post-run check and the late arrival."""
import dataclasses
import hashlib
import json
import sqlite3

import pytest

from sentinel.records import (ACTION_VERDICT, CheckpointOutcome, Decision, DiffSummary, Extraction, Finding,
                              Fingerprint, RunOutcome, Signature, StepEvent, StepOutcome)
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store, StoreRefused
from sentinel.store.artifacts import StoredArtifact
from sentinel.store.store import (ActiveDecisionExists, PaperAssertionMissing, PostRunProof, RecordIncomplete,
                                  RunNotComplete)

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"            # tier 1
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"   # tier 2
FLOW_ID = "flow.trade.order-ticket.stock.limit"
FLOW = f"flow/dialog.order-confirmation/{FLOW_ID}/s07"
SHARD = f"{RUN}/1"
D = DiffSummary(whole_pixel_fraction=0.02, whole_ssim=0.97)
ACCOUNT = "sha256:51f20a0a"
ASSERTED = "2026-11-09T14:21:02-05:00"
TS = "2026-11-09T15:00:00+00:00"


def scope():
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
            ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
            ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"))


def plan(run_id=RUN, shards=(), **kw):
    return RunPlan(run_id=run_id, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope(), shards=shards, **kw)


def out(cid, outcome="identical", run_id=RUN, **kw):
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", **kw)


def stamp(account=ACCOUNT, at=ASSERTED):
    return {"account_id_sha256": account, "mode": "paperMoney", "asserted_at": at}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(plan(shards=(ShardPlan(SHARD, "rig-a1"),)))
    yield s
    s.close()


@pytest.fixture
def strict(tmp_path):
    s = Store(tmp_path / "strict.db", strict_records=True)
    s.begin_run(plan(flow_steps={FLOW_ID: ("s03", "s07")}))
    yield s
    s.close()


# -- L-OUT-02 at write ---------------------------------------------------------------------------------------------

def test_a_strict_store_refuses_an_incomplete_record(strict):
    with pytest.raises(RecordIncomplete) as e:
        strict.write_checkpoint(out(CENSUS, "invariant_violation", reason="INV-01"))
    assert any("invariant_ids" in x for x in e.value.errors)
    with pytest.raises(RecordIncomplete, match="Watchdog"):
        strict.write_checkpoint(out(CENSUS, "harness_error", reason="hook_lost"))
    assert strict.get_outcome(RUN, CENSUS) is None
    strict.write_checkpoint(out(CENSUS, "harness_error", reason="hook_lost", detail="watchdog_event: wd-1"))
    strict.write_checkpoint(out(SWEEP, diff=D))


def test_step_failed_must_name_a_step_of_the_flow(strict):
    with pytest.raises(RecordIncomplete, match="zz99"):
        strict.write_checkpoint(out(FLOW, "not_run", reason="step_failed:zz99", flow_id=FLOW_ID))
    strict.write_checkpoint(out(FLOW, "not_run", reason="step_failed:s03", flow_id=FLOW_ID))
    assert strict.record_errors(out(FLOW, "not_run", reason="step_failed:s07", flow_id=FLOW_ID)) == []


def test_steps_of_registered_flow_checkpoints_count_without_a_plan(store):
    assert store.record_errors(out(FLOW, "not_run", reason="step_failed:s07", flow_id=FLOW_ID)) == []
    assert store.record_errors(out(FLOW, "not_run", reason="step_failed:s03", flow_id=FLOW_ID))


def test_a_lax_store_accepts_what_a_strict_one_refuses(store):
    store.write_checkpoint(out(CENSUS, "harness_error", reason="hook_lost"))
    assert store.get_outcome(RUN, CENSUS).reason == "hook_lost"


def test_the_orchestrator_repairs_once_then_writes_store_write_failed(strict):
    seen = []

    def repair(o, errs):
        seen.append(("repair", tuple(errs)))
        return dataclasses.replace(o, detail="watchdog_event: wd-7")

    def on_refused(o, errs):
        seen.append(("refused", tuple(errs)))
        return "wd-9"

    written = strict.write_checkpoint_or_repair(out(CENSUS, "harness_error", reason="hook_lost"), repair=repair,
                                                on_refused=on_refused)
    assert written.detail == "watchdog_event: wd-7" and [k for k, _ in seen] == ["repair"]
    seen.clear()
    bad = out(SWEEP, "invariant_violation", reason="INV-01", diff=D)
    written = strict.write_checkpoint_or_repair(bad, repair=lambda o, e: o, on_refused=on_refused)
    assert [k for k, _ in seen] == ["refused"]
    got = strict.get_outcome(RUN, SWEEP)
    assert (got.outcome, got.reason) == ("harness_error", "store_write_failed") == (written.outcome, written.reason)
    assert got.detail.startswith("watchdog_event: wd-9; refused: invariant_violation")
    assert strict.write_checkpoint_or_repair(out(FLOW, "not_run", reason="scope"), repair=repair,
                                             on_refused=on_refused).reason == "scope"


# -- L-OUT-06: the paper-mode stamp ---------------------------------------------------------------------------------

def _capture(store, cid, **kw):
    """A record naming one stored (non-image) artifact as its candidate capture."""
    data = b"not an image " + cid.encode()
    path = store.path.parent / "objects.bin"
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    art = StoredArtifact(sha256=sha, name="objects.bin", kind="log", media_type="text/plain", bytes=len(data),
                         set_path="runs/run-01", path=str(path))
    return out(cid, diff=D, artifacts={"candidate": sha}, fingerprint_id="sha256:fp", data_epoch_id="sha256:ep",
               **kw), [art]


def test_the_session_stamp_is_required_on_a_captured_record(store):
    store.set_session_context(SHARD, [ACCOUNT], [ASSERTED])
    o, arts = _capture(store, CENSUS)
    with pytest.raises(PaperAssertionMissing):
        store.write_checkpoint(o, artifacts=arts, shard_id=SHARD)
    with pytest.raises(PaperAssertionMissing, match="allowlisted"):
        store.write_checkpoint(dataclasses.replace(o, paper_assertion=stamp(account="sha256:live")), artifacts=arts,
                               shard_id=SHARD)
    with pytest.raises(PaperAssertionMissing, match="current session"):
        store.write_checkpoint(dataclasses.replace(o, paper_assertion=stamp(at="2026-11-08T09:00:00+00:00")),
                               artifacts=arts, shard_id=SHARD)
    assert isinstance(PaperAssertionMissing("x"), StoreRefused)
    store.write_checkpoint(dataclasses.replace(o, paper_assertion=stamp()), artifacts=arts, shard_id=SHARD)
    row = store.conn.execute("SELECT paper_asserted_at FROM outcomes WHERE checkpoint_id = ?", (CENSUS,)).fetchone()
    assert row[0] == "2026-11-09T19:21:02+00:00"


def test_the_repair_path_never_replaces_a_paper_mode_refusal(store):
    # store:0, L-OUT-06: a captured record that also fails L-OUT-02 is refused for its stamp before any repair; it is
    # never swapped for an exempt harness_error (store_write_failed) with its capture still indexed
    store.set_session_context(SHARD, [ACCOUNT], [ASSERTED])
    o, arts = _capture(store, CENSUS, outcome="invariant_violation", reason="INV-01")     # no invariant_ids: L-OUT-02
    calls = []

    def repair(r, errs):
        calls.append("repair")
        return r

    def on_refused(r, errs):
        calls.append("refused")
        return "wd-1"

    for bad in (None, stamp(account="sha256:live"), stamp(at="2026-11-08T09:00:00+00:00")):
        rec = dataclasses.replace(o, paper_assertion=bad)
        assert store.record_errors(rec)
        with pytest.raises(PaperAssertionMissing):
            store.write_checkpoint_or_repair(rec, repair=repair, on_refused=on_refused, artifacts=arts, shard_id=SHARD)
    assert calls == [] and store.get_outcome(RUN, CENSUS) is None
    assert store.conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    with pytest.raises(StoreRefused, match="not stored"):     # every other non-L-OUT-02 refusal propagates too
        store.write_checkpoint_or_repair(out(SWEEP, "invariant_violation", reason="INV-01", diff=D,
                                             artifacts={"candidate": "e" * 64}), repair=repair, on_refused=on_refused)
    assert calls == [] and store.get_outcome(RUN, SWEEP) is None
    written = store.write_checkpoint_or_repair(dataclasses.replace(o, paper_assertion=stamp()), repair=repair,
                                               on_refused=on_refused, artifacts=arts, shard_id=SHARD)
    assert (written.outcome, written.reason) == ("harness_error", "store_write_failed") and calls == ["repair",
                                                                                                     "refused"]


def test_records_without_a_capture_are_exempt(store):
    store.set_session_context(SHARD, [ACCOUNT], [ASSERTED])
    store.write_checkpoint(out(CENSUS, "env_error", gate="authenticated", reason="session_kicked"), shard_id=SHARD)
    store.write_checkpoint(out(FLOW, "not_run", reason="scope"), shard_id=SHARD)
    with pytest.raises(PaperAssertionMissing):              # a stamp that is there must be the session's
        store.write_checkpoint(out(SWEEP, "harness_error", reason="hook_lost", paper_assertion=stamp(at=TS)),
                               shard_id=SHARD)


def test_a_new_session_replaces_the_earlier_times_and_other_shards_are_unaffected(store):
    store.set_session_context(SHARD, [ACCOUNT], [ASSERTED])
    store.set_session_context(SHARD, [ACCOUNT], ["2026-11-09T20:00:00+00:00"])
    o, arts = _capture(store, CENSUS, paper_assertion=stamp())
    with pytest.raises(PaperAssertionMissing):
        store.write_checkpoint(o, artifacts=arts, shard_id=SHARD)
    store.write_checkpoint(o, artifacts=arts)              # no context for writes without a shard
    with pytest.raises(StoreRefused):
        store.set_session_context(SHARD, [], [ASSERTED])


# -- 8.3 columns of outcomes, steps and findings --------------------------------------------------------------------

def test_outcome_columns_hold_the_record_fields(store):
    store.begin_run(plan(run_id="run-00"))
    o, arts = _capture(store, CENSUS, paper_assertion=stamp(), base_source_run_id="run-00",
                       captured_at="2026-11-09T14:21:40-05:00", settle_ms=1840, cand_rig_id="rig-01",
                       base_rig_id="rig-02", diff_pixels=4120, masked_pct=3.5)
    store.write_checkpoint(o, artifacts=arts)
    row = store.conn.execute("SELECT data_epoch_id, paper_asserted_at, base_source_run_id, captured_at, settle_ms, "
                             "cand_rig_id, base_rig_id, diff_pixels, masked_pct FROM outcomes WHERE run_id = ? "
                             "AND checkpoint_id = ?", (RUN, CENSUS)).fetchone()
    assert tuple(row) == ("sha256:ep", "2026-11-09T19:21:02+00:00", "run-00", "2026-11-09T19:21:40+00:00", 1840,
                          "rig-01", "rig-02", 4120, 3.5)
    with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
        store.conn.execute("UPDATE outcomes SET captured_at = ? WHERE run_id = ? AND checkpoint_id = ?",
                           (TS, RUN, CENSUS))
    with pytest.raises(StoreRefused, match="not a run"):
        store.write_checkpoint(out(SWEEP, diff=D, base_source_run_id="run-99"))


def test_a_step_keeps_its_side_shard_and_screenshots(store):
    step = StepOutcome(RUN, FLOW_ID, "s07", "passed", side="base", shard_id=SHARD, pre_screenshot="p" * 64,
                       post_screenshot="q" * 64, click_crop="r" * 64, mode="scripted", app_uptime_s=12,
                       app_restart_count=1, reason="export_timeout", finding_ref="F-9", action="export")
    store.write_steps([step])
    assert store.step_outcomes(RUN) == [step]
    row = store.conn.execute("SELECT side, shard_id, pre_hash FROM step_events").fetchone()
    assert tuple(row) == ("base", SHARD, "p" * 64)
    with pytest.raises(StoreRefused, match="side"):
        store.write_steps([dataclasses.replace(step, step_id="s08")], side="candidate")
    with pytest.raises(StoreRefused, match="shard"):
        store.write_checkpoint(out(FLOW, "collateral"), steps=[dataclasses.replace(step, step_id="s09")],
                               shard_id=f"{RUN}/2")


def test_a_finding_keeps_severity_side_step_and_detail(store):
    f = Finding("F-1", RUN, "invariant_violation", "ladder overlaps", RUN, checkpoint_id=CENSUS, severity="S1",
                side="both", step_id="s07", detail="dd", pre_existing=True, cluster_id="cl-1", category="collateral")
    store.write_finding(f)
    assert store.get_finding("F-1") == f
    row = store.conn.execute("SELECT severity, side, step_id, detail FROM findings").fetchone()
    assert tuple(row) == ("S1", "both", "s07", "dd")


def test_the_run_row_keeps_catalog_versions_and_the_data_window(tmp_path):
    with Store(tmp_path / "r.db") as s:
        s.begin_run(plan(catalog_versions={"popups": "v12", "mutants": "v7"},
                         data_window_start="2026-11-09T09:30:00-05:00", data_window_end="2026-11-09T16:00:00-05:00"))
        row = s.conn.execute("SELECT catalog_versions_json, data_window_start, data_window_end FROM runs").fetchone()
        assert json.loads(row[0]) == {"popups": "v12", "mutants": "v7"}
        assert (row[1], row[2]) == ("2026-11-09T14:30:00+00:00", "2026-11-09T21:00:00+00:00")


# -- decisions: the 4.14 record fields, L-DEC-03 and 9.7 two humans, one signature ---------------------------------

PH = "0123456789abcdef"


def finding(store, fid, run_id=RUN, cid=SWEEP):
    store.write_finding(Finding(fid, run_id, "collateral", "ladder overlaps", run_id, checkpoint_id=cid, phash=PH,
                                tier=2, category="collateral"))
    return fid


def decision(did, action="noise_signature", by="alice", phash=PH, category="collateral", region="rgn.ladder.bid-column",
             **kw):
    ticket = kw.pop("ticket", "TOS-1" if action in ("confirm_bug", "mark_intended", "fixed_in_build") else None)
    return Decision(did, action, ACTION_VERDICT[action], Signature(SWEEP, region, phash, category), decided_by=by,
                    ticket=ticket, reason="because", decided_at=kw.pop("decided_at", TS), **kw)


def decide(store, did, action="noise_signature", *, run_id=RUN, call=None, **kw):
    return store.record_decision(decision(did, action, **kw), run_id=run_id, **(call or {}))


def test_record_fields_on_the_decision_are_stored_and_read_back(store):
    finding(store, "F-1")
    decide(store, "dec-000001", finding_ref="F-1")
    got = decide(store, "dec-000002", by="bob", expires_at="2026-11-15T00:00:00+00:00", confirmed_by="carol",
                 supersedes="dec-000001", finding_ref="F-1", role="triage_rotation", bbox_q4=(12, 388, 208, 20))
    assert (got.expires_at, got.confirmed_by, got.supersedes, got.finding_ref, got.role, got.bbox_q4) == (
        "2026-11-15T00:00:00+00:00", "carol", "dec-000001", "F-1", "triage_rotation", (12, 388, 208, 20))
    assert store.get_decision("dec-000002") == got
    with pytest.raises(StoreRefused, match="confirmed_by"):      # a keyword and the record disagree
        decide(store, "dec-000003", region="rgn.other", confirmed_by="carol", call={"confirmed_by": "dave"})


def test_a_waiver_with_its_expiry_on_the_record_is_accepted(store):
    w = decide(store, "dec-000001", "mark_intended", category="invariant_violation",
               expires_at="2026-11-19T15:00:00+00:00")
    assert w.expires_at == "2026-11-19T15:00:00+00:00"
    with pytest.raises(StoreRefused, match="L-DEC-07"):
        decide(store, "dec-000002", "mark_intended", category="assertion_failed",
               expires_at="2027-01-19T15:00:00+00:00")


def test_a_second_plain_decision_on_the_signature_is_refused(store):
    finding(store, "F-1")
    first = decide(store, "dec-000001", finding_ref="F-1")
    with pytest.raises(ActiveDecisionExists, match="dec-000001") as e:
        decide(store, "dec-000002", "confirm_bug", by="bob", finding_ref="F-1")
    assert [a.decision_id for a in e.value.active] == [first.decision_id]
    assert store.get_decision("dec-000002") is None
    second = decide(store, "dec-000002", "confirm_bug", by="bob", finding_ref="F-1", supersedes="dec-000001")
    assert second.status == "active" and store.get_decision("dec-000001").status == "superseded"
    assert store.get_finding("F-1").contested                    # 9.7: a supersession by another human


def test_an_out_of_scope_intended_decision_does_not_block(store):
    store.begin_run(plan(run_id="run-00"))
    decide(store, "dec-000001", "mark_intended", run_id="run-00", ticket="TOS-9")
    finding(store, "F-2")
    with pytest.raises(ActiveDecisionExists):                    # the run's manifest carries the ticket: in scope
        decide(store, "dec-000002", "confirm_bug", by="bob", finding_ref="F-2", call={"manifest_tickets": ["TOS-9"]})
    assert decide(store, "dec-000002", "confirm_bug", by="bob", finding_ref="F-2").status == "active"


def test_the_same_click_again_returns_the_same_decision(store):
    finding(store, "F-1")
    first = decide(store, "dec-000001", finding_ref="F-1")
    again = decide(store, "dec-000005", finding_ref="F-1")
    assert again.decision_id == first.decision_id
    assert store.conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


FAR = "fedcba9876543210"                    # 64 bits from PH: outside every tolerance


def test_supersedes_names_a_decision_on_the_same_signature(store):
    # store:2, 9.7: Supersede replaces the active row of THIS signature; SD-79: fixed_in_build supersedes confirm_bug
    finding(store, "F-1")
    decide(store, "dec-000001", by="alice", finding_ref="F-1")
    decide(store, "dec-000002", by="carol", region="rgn.unrelated")
    decide(store, "dec-000003", by="carol", phash=FAR)
    decide(store, "dec-000004", by="carol", category="invariant_violation")
    for other in ("dec-000002", "dec-000003", "dec-000004"):
        with pytest.raises(StoreRefused, match="another signature"):
            decide(store, "dec-000005", "confirm_bug", by="bob", finding_ref="F-1", supersedes=other)
        assert store.get_decision(other).status == "active"
    assert store.get_decision("dec-000005") is None and not store.get_finding("F-1").contested
    for b, at in (("b1", "2026-11-01T00:00:00+00:00"), ("b2", "2026-11-05T00:00:00+00:00")):
        store.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                             first_seen_at=at)
    with pytest.raises(StoreRefused, match="SD-79"):
        decide(store, "dec-000005", "fixed_in_build", by="bob", supersedes="dec-000001",
               call={"fixed_in_build_id": "b2", "build_id": "b1"})
    assert store.get_decision("dec-000001").status == "active"
    sup = decide(store, "dec-000005", "confirm_bug", by="bob", finding_ref="F-1", supersedes="dec-000001")
    assert sup.supersedes == "dec-000001" and store.get_decision("dec-000001").status == "superseded"


def test_superseding_your_own_decision_writes_a_new_row(store):
    # store:3, 9.6 Supersede decision, 9.7: the idempotent return is for the same click again, never a supersession
    finding(store, "F-1")
    decide(store, "dec-000001", "confirm_bug", finding_ref="F-1", ticket="TOS-1")
    with pytest.raises(ActiveDecisionExists, match="dec-000001"):          # a plain change is no repeat: refused
        decide(store, "dec-000002", "confirm_bug", finding_ref="F-1", ticket="TOS-2")
    new = decide(store, "dec-000002", "confirm_bug", finding_ref="F-1", ticket="TOS-2", supersedes="dec-000001")
    assert (new.decision_id, new.ticket, new.supersedes) == ("dec-000002", "TOS-2", "dec-000001")
    assert store.get_decision("dec-000001").status == "superseded"
    assert store.get_finding("F-1").ticket == "TOS-2" and not store.get_finding("F-1").contested


@pytest.fixture
def frozen(tmp_path):
    """A signature with two supersessions within 30 days (9.7), on a store whose system owner is olga."""
    with Store(tmp_path / "f.db", system_owners=["olga"]) as s:
        s.begin_run(plan())
        decide(s, "dec-000001", decided_at="2026-11-01T00:00:00+00:00")
        decide(s, "dec-000002", by="bob", supersedes="dec-000001", decided_at="2026-11-05T00:00:00+00:00")
        decide(s, "dec-000003", by="carl", supersedes="dec-000002", decided_at="2026-11-07T00:00:00+00:00")
        yield s


SIG = Signature(SWEEP, "rgn.ladder.bid-column", PH, "collateral")


def test_a_frozen_signature_needs_the_system_owner(frozen):
    s = frozen
    assert s.lookup_decisions(SIG, at=TS).frozen
    with pytest.raises(StoreRefused, match="system owner"):
        decide(s, "dec-000004", "confirm_bug", by="dana", supersedes="dec-000003")
    with pytest.raises(StoreRefused, match="system owner"):            # store:4: the role is not a key to the freeze
        decide(s, "dec-000004", "confirm_bug", by="dana", supersedes="dec-000003", role="system_owner")
    with pytest.raises(StoreRefused, match="system_owner"):
        decide(s, "dec-000004", "confirm_bug", by="olga", supersedes="dec-000003", role="flow_owner")
    owner = decide(s, "dec-000004", "confirm_bug", by="olga", supersedes="dec-000003")
    assert (owner.decided_by, owner.role) == ("olga", "system_owner")
    for at in (TS, "2027-06-01T00:00:00+00:00"):                       # store:4: the owner's decision ends the freeze
        look = s.lookup_decisions(SIG, at=at)
        assert not look.frozen and look.inherited.decision_id == "dec-000004"
    decide(s, "dec-000005", "confirm_bug", by="bob", supersedes="dec-000004", decided_at="2026-11-10T00:00:00+00:00")
    assert not s.lookup_decisions(SIG, at=TS).frozen                   # one supersession since the owner's decision
    decide(s, "dec-000006", by="carl", supersedes="dec-000005", decided_at="2026-11-12T00:00:00+00:00")
    assert s.lookup_decisions(SIG, at=TS).frozen                       # two more within 30 days freeze it again


def test_a_revoke_does_not_lift_the_freeze(frozen):
    # store:4, 9.7: the freeze is the signature's; revoking its active row (open to the 9.6 roles) leaves it frozen
    s = frozen
    s.revoke_decision("dec-000003", by="dana", reason="disputed", at="2026-11-08T00:00:00+00:00")
    look = s.lookup_decisions(SIG, at=TS)
    assert look.frozen and look.applicable == ()
    with pytest.raises(StoreRefused, match="system owner"):
        decide(s, "dec-000004", "confirm_bug", by="dana")
    assert s.get_decision("dec-000004") is None
    assert decide(s, "dec-000004", "confirm_bug", by="olga").role == "system_owner"
    assert s.lookup_decisions(SIG, at=TS).inherited.decision_id == "dec-000004"


def test_fixed_in_build_is_not_a_contest(store):
    finding(store, "F-1")
    decide(store, "dec-000001", "confirm_bug", finding_ref="F-1")
    for b, at in (("b1", "2026-11-01T00:00:00+00:00"), ("b2", "2026-11-05T00:00:00+00:00")):
        store.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                             first_seen_at=at)
    decide(store, "dec-000002", "fixed_in_build", by="bob", supersedes="dec-000001", finding_ref="F-1",
           call={"fixed_in_build_id": "b2", "build_id": "b1"})
    assert not store.get_finding("F-1").contested


def by_decision(cid, outcome, did, **kw):
    return out(cid, outcome, reason=f"decision:{did}", decision_ref=did, diff=D, recorded_at=TS, **kw)


def test_a_decision_reason_names_a_live_decision_that_acts_on_this_checkpoint(store):
    # store:8, 4.13 required fields: "decision:<id> and a decision_ref to an approved noise signature or an active
    # mask decision"; 9.7: a decision matches its own signature only and noise_signature ends after 30 days
    decide(store, "dec-000001", decided_at="2026-09-01T00:00:00+00:00", expires_at="2026-10-01T00:00:00+00:00")
    decide(store, "dec-000002", region="rgn.b")
    refused = {"dec-000001": "expired", "dec-000002": "checkpoint"}
    for n, action in enumerate(("agent_mistake", "expected_behavior", "request_mask"), start=3):
        decide(store, f"dec-00000{n}", action, region=f"rgn.{n}")
        refused[f"dec-00000{n}"] = action
    for did, why in refused.items():
        with pytest.raises(StoreRefused, match=why):
            store.write_checkpoint(by_decision(CENSUS if did == "dec-000002" else SWEEP, "noise", did))
    store.record_decision(Decision("dec-000006", "reject_baseline", "confirmed_bug",
                                   Signature(SWEEP, "rgn.6", PH, "collateral"), decided_by="alice", ticket="TOS-6",
                                   reason="because", decided_at=TS), run_id=RUN, baseline_id=1)
    with pytest.raises(StoreRefused, match="reject_baseline"):            # it moves a baseline, not the signature
        store.write_checkpoint(by_decision(SWEEP, "collateral", "dec-000006", primitive="ladder"))
    assert store.outcomes(RUN) == []
    store.set_mask_ref("dec-000005", "mask-1")                              # the mask merged: it acts now
    store.write_checkpoint(by_decision(SWEEP, "noise", "dec-000005"))
    decide(store, "dec-000007", "confirm_bug", region="rgn.7")
    decide(store, "dec-000008", "mark_intended", region="rgn.8")
    for run_id, outcome, did in (("run-02", "collateral", "dec-000007"), ("run-03", "intended", "dec-000008")):
        store.begin_run(plan(run_id=run_id))
        store.write_checkpoint(by_decision(SWEEP, outcome, did, run_id=run_id))
        assert store.get_outcome(run_id, SWEEP).reason == f"decision:{did}"


# -- the run summary, review state and late verdicts (8.3, 8.7, 4.13, SD-23) ----------------------------------------

def review(cid, mapped="intended", verdict="matches_manifest", diff_hash="h1", confidence=0.93):
    from sentinel.store import Review
    return Review(run_id=RUN, checkpoint_id=cid, diff_hash=diff_hash, model_id="model-x", prompt_hash="p1",
                  request_sha256="f" * 64, response_json="{}", verdict=verdict, mapped_outcome=mapped,
                  confidence=confidence, created_at=TS)


def pending(store, cid):
    store.write_checkpoint(out(cid, "needs_human", reason="reviewer_unavailable", review_state="pending", diff=D))


def end_complete(store, **kw):
    store.end_shard(SHARD, "complete")
    store.mark_shard_imported(SHARD)
    return store.end_run(RunOutcome(RUN, "pr", "complete", "reported"), **kw)


def review_state(store):
    return store.conn.execute("SELECT review_state FROM runs WHERE run_id = ?", (RUN,)).fetchone()[0]


def test_the_teardown_summary_holds_coverage_health_and_detector_state(store):
    store.update_summary(RUN, {"alerts": [{"id": "a1", "ack": None}]})
    for cid in (CENSUS, SWEEP, FLOW):
        store.write_checkpoint(out(cid, diff=D))
    coverage = {"cells": [{"surface_id": "trade.active-trader.ladder", "persona": "active_trader",
                           "entitlement": "paper-margin", "state": "covered"}]}
    end_complete(store, summary={"coverage": coverage, "health": {"wall_clock": "31:04"}, "detector_state": "ok"})
    summ = store.run_summary(RUN)
    assert summ["coverage"] == coverage and summ["health"] == {"wall_clock": "31:04"}
    assert summ["detector_state"] == "ok" and summ["counts"]["identical"] == 3 and summ["last_gate"] == "reported"
    assert summ["alerts"] == [{"id": "a1", "ack": None}]                 # written before teardown, kept
    acked = store.update_summary(RUN, {"alerts": [{"id": "a1", "ack": {"by": "rkim", "at": TS}}]})
    assert store.run_summary(RUN) == acked and acked["alerts"][0]["ack"]["by"] == "rkim"
    assert store.run_outcome(RUN).counts["identical"] == 3
    with pytest.raises(StoreRefused, match="store's own"):
        store.update_summary(RUN, {"counts": {}})
    with pytest.raises(StoreRefused, match="JSON"):
        store.update_summary(RUN, {"bad": object()})


def test_review_state_follows_the_pending_items(store):
    pending(store, CENSUS)
    pending(store, SWEEP)
    store.write_checkpoint(out(FLOW, diff=D))
    end_complete(store)
    assert review_state(store) == "unreviewed"
    store.apply_review(review(CENSUS))
    assert review_state(store) == "partial"
    store.end_review(RUN, SWEEP, "reviewer_unavailable")
    assert review_state(store) == "reviewed"


def test_a_run_with_nothing_pending_is_reviewed(store):
    for cid in (CENSUS, SWEEP, FLOW):
        store.write_checkpoint(out(cid, diff=D))
    end_complete(store)
    assert review_state(store) == "reviewed"


def test_a_late_verdict_never_produces_collateral_or_noise(store):
    pending(store, CENSUS)
    for mapped, reason in (("collateral", None), ("noise", "below_floor")):
        with pytest.raises(StoreRefused, match="SD-23"):
            store.apply_review(review(CENSUS, mapped=mapped, verdict="does_not_match_manifest"), reason=reason)
    assert store.get_outcome(RUN, CENSUS).review_state == "pending"
    o = store.apply_review(review(CENSUS, mapped="needs_human", verdict="does_not_match_manifest"),
                           reason="reviewer_no_match")
    assert (o.outcome, o.reason, o.review_state) == ("needs_human", "reviewer_no_match", "done")


def test_a_late_verdict_is_idempotent_on_the_reviews_key(store):
    pending(store, CENSUS)
    first = store.apply_review(review(CENSUS))
    assert store.apply_review(review(CENSUS)) == first                     # the drainer delivered it twice
    assert store.conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
    from sentinel.store import ImportConflict
    with pytest.raises(ImportConflict):
        store.apply_review(review(CENSUS, confidence=0.5))


# -- the post-run check of 1.4 G2 and L-OUT-07 ------------------------------------------------------------------------

def cells(surfaces=("s.a", "s.b"), personas=("active_trader",), profiles=("paper-margin", "paper-ira")):
    return [{"surface_id": s, "persona": p, "entitlement": f, "state": "covered"}
            for s in surfaces for p in personas for f in profiles]


def test_a_clean_run_passes_the_post_run_check(store):
    from sentinel.store.checks import post_run_check
    for cid in (CENSUS, SWEEP, FLOW):
        store.write_checkpoint(out(cid, diff=D))
    assert post_run_check(store, RUN, 2, 1, 2, cells=cells()) == []
    store.update_summary(RUN, {"coverage": {"cells": cells()}})           # a PR run keeps its matrix in the summary
    assert post_run_check(store, RUN, 2, 1, 2) == []


def test_a_silent_checkpoint_ends_the_run_aborted_harness_with_the_report_withheld(store):
    from sentinel.store.checks import end_taxonomy_check_failed, post_run_check
    store.write_checkpoint(out(CENSUS, diff=D))
    store.write_checkpoint(out(SWEEP, diff=D))
    failures = post_run_check(store, RUN, 2, 1, 2, cells=cells())
    assert failures == [f"L-OUT-07: 1 scoped checkpoint(s) without an outcome record, first {FLOW}"]
    r = end_taxonomy_check_failed(store, RUN, failures, last_gate="teardown", watchdog_event_id="wd-31")
    assert (r.status, r.reason) == ("aborted_harness", "taxonomy_check_failed")
    filled = store.get_outcome(RUN, FLOW)
    assert (filled.outcome, filled.reason) == ("harness_error", "taxonomy_check_failed")
    assert filled.detail.startswith("watchdog_event: wd-31")
    assert store.run_summary(RUN)["post_run_check"] == {"failures": failures, "report_withheld": True}
    assert post_run_check(store, RUN, 2, 1, 2, cells=cells()) == []


def test_a_retired_reason_fails_the_taxonomy_check(tmp_path):
    import copy
    from sentinel.store.checks import post_run_check
    from sentinel.taxonomy import Taxonomy, load
    db = tmp_path / "t.db"
    with Store(db) as s:
        s.begin_run(plan())
        s.write_checkpoint(out(CENSUS, "needs_baseline", reason="explorer_replay", side="base"))
        s.write_checkpoint(out(SWEEP, diff=D))
        s.write_checkpoint(out(FLOW, diff=D))
        assert post_run_check(s, RUN, 2, 1, 2, cells=cells()) == []
    data = copy.deepcopy(load().data)
    data["reasons"]["needs_baseline"].remove("explorer_replay")            # retired in the seed at the next open
    with Store(db, taxonomy=Taxonomy(data)) as s:
        failures = post_run_check(s, RUN, 2, 1, 2, cells=cells())
        assert len(failures) == 1 and failures[0].startswith("taxonomy: 1 outcome reason(s) retired")


def test_a_matrix_whose_cells_do_not_sum_is_refused(store):
    from sentinel.store.checks import matrix_failures, post_run_check
    for cid in (CENSUS, SWEEP, FLOW):
        store.write_checkpoint(out(cid, diff=D))
    short = cells()[:-1]
    assert matrix_failures(short, 2, 1, 2) == ["G2 matrix: 3 cells, expected 2 surfaces x 1 personas x 2 profiles = 4"]
    doubled = short + short[:1]
    assert any("more than once" in f for f in matrix_failures(doubled, 2, 1, 2))
    assert post_run_check(store, RUN, 2, 1, 2, cells=short) == matrix_failures(short, 2, 1, 2)
    assert post_run_check(store, RUN, 2, 1, 2) == ["G2 matrix: the run has no coverage matrix"]


# -- 10.2 late arrival -------------------------------------------------------------------------------------------------

DEADLINE = "2026-11-09T16:00:00+00:00"


def dead_man(store):
    store.fill_unreached(RUN, "harness_error", "dead_man", detail="watchdog_event: wd-40")
    store.end_run(RunOutcome(RUN, "pr", "aborted_harness", "running", reason="dead_man", ended_at=DEADLINE))
    store.end_shard(SHARD, "complete")
    store.mark_shard_imported(SHARD)


def test_late_rows_replace_the_dead_man_run(store):
    dead_man(store)
    rows = [out(cid, diff=D) for cid in (CENSUS, SWEEP, FLOW)]
    r = store.replace_dead_man_run(RUN, rows, outcome=RunOutcome(RUN, "pr", "complete", "reported"),
                                   arrived_at="2026-11-09T16:05:00+00:00", proofs=[late_proof()])
    assert (r.status, r.reason) == ("complete", None) and r.counts["identical"] == 3
    assert {o.outcome for o in store.outcomes(RUN)} == {"identical"}
    kept = store.conn.execute("SELECT checkpoint_id, outcome, reason FROM outcomes_superseded ORDER BY 1").fetchall()
    assert [tuple(k) for k in kept] == [(cid, "harness_error", "dead_man") for cid in sorted((CENSUS, SWEEP, FLOW))]
    summ = store.run_summary(RUN)
    assert summ["late_arrival"] == {"dead_man_at": DEADLINE, "arrived_at": "2026-11-09T16:05:00+00:00",
                                    "delay_s": 300} == summ["health"]["late_arrival"]
    assert summ["alerts"][-1]["closed_as"] == "late arrival"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):        # the keep triggers are back
        store.conn.execute("DELETE FROM outcomes WHERE run_id = ?", (RUN,))
    with pytest.raises(StoreRefused, match="dead_man"):                   # once only
        store.replace_dead_man_run(RUN, rows, outcome=RunOutcome(RUN, "pr", "complete", "reported"))


def late_proof(shard=SHARD):
    return PostRunProof(shard_id=shard, run_id=RUN, login_alias="paper-01", verdict="proven_no_sends",
                        send_policy_ref="p", counts={}, cancel_all="not_needed", paper_mode_assertions=1,
                        paper_mode_mismatches=0, gateway_events=2, gateway_drops=1, produced_at=TS)


def test_the_late_arrival_carries_the_rigs_proof_events_and_each_rows_shard(store):
    # store:1, 10.2: the rows the rig ships after a dead-man deadline (its post-run proof and step events among them)
    # arrive in the one late batch, each row under its shard; 1.10: a complete late run needs every shard's proof
    store.set_session_context(SHARD, [ACCOUNT], [ASSERTED])
    dead_man(store)
    store.register_build("b1", channel="branch", source_commit="b1", pipeline_artifact_url="u", installer_sha256="b1")
    rows = [out(cid, diff=D) for cid in (CENSUS, SWEEP, FLOW)]
    late = {"outcome": RunOutcome(RUN, "pr", "complete", "reported"), "arrived_at": "2026-11-09T16:05:00+00:00"}
    with pytest.raises(RunNotComplete, match="post-run proof"):
        store.replace_dead_man_run(RUN, rows, **late)
    live, arts = _capture(store, CENSUS, paper_assertion=stamp(account="sha256:deadbeef"))
    with pytest.raises(PaperAssertionMissing):                      # L-OUT-06 under the row's shard
        store.replace_dead_man_run(RUN, [live, *rows[1:]], artifacts=arts, proofs=[late_proof()], **late)
    with pytest.raises(StoreRefused, match="not a shard"):
        store.replace_dead_man_run(RUN, rows, shard_of={CENSUS: "run-01/9"}, proofs=[late_proof()], **late)
    assert (store.run_outcome(RUN).status, store.post_run_proofs(RUN)) == ("aborted_harness", [])
    event = StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=1, source="gateway_hook", wall_at=TS,
                      mono_ms=4242, mode="scripted", action="click", gate_decision="dropped",
                      gate_reason="no_token", gate_class="safety")
    step = StepOutcome(RUN, FLOW_ID, "s07", "passed", shard_id=SHARD)
    x = Extraction(RUN, CENSUS, "candidate", "rule.rgn.qty.qty", "qty", "ocr", TS, region_id="rgn.qty", text="100",
                   value="100", confidence=91.5)
    fp = Fingerprint(RUN, "candidate", "rig-a1", TS, {"build_id": "b1"}, record={}, shard_id=SHARD)
    r = store.replace_dead_man_run(RUN, rows, proofs=[late_proof()], events=[event], steps=[step], extractions=[x],
                                   fingerprints=[fp], **late)
    assert r.status == "complete" and store.post_run_proofs(RUN) == [late_proof()]
    assert [(e.seq, e.gate_decision) for e in store.step_events(RUN)] == [(1, "dropped"), (2, "none")]
    assert store.gateway_drops(RUN) == 1 and store.extractions(RUN) == [x] and store.fingerprint(SHARD, "candidate")
    shards = store.conn.execute("SELECT DISTINCT shard_id FROM outcomes WHERE run_id = ?", (RUN,)).fetchall()
    assert [row[0] for row in shards] == [SHARD]


def test_a_late_row_of_a_run_with_several_shards_names_its_shard(tmp_path):
    with Store(tmp_path / "two.db") as s:
        for rig in ("rig-a1", "rig-a2"):
            s.register_rig(rig, login_alias=f"paper-{rig[-1]}", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        two = (f"{RUN}/1", f"{RUN}/2")
        s.begin_run(plan(shards=(ShardPlan(two[0], "rig-a1"), ShardPlan(two[1], "rig-a2"))))
        s.fill_unreached(RUN, "harness_error", "dead_man", detail="watchdog_event: wd-40")
        s.end_run(RunOutcome(RUN, "pr", "aborted_harness", "running", reason="dead_man", ended_at=DEADLINE))
        for sid in two:
            s.end_shard(sid, "complete")
            s.mark_shard_imported(sid)
        rows = [out(cid, diff=D) for cid in (CENSUS, SWEEP, FLOW)]
        proofs = [dataclasses.replace(late_proof(sid), login_alias=f"paper-{sid[-1]}") for sid in two]
        done = RunOutcome(RUN, "pr", "complete", "reported")
        where = {CENSUS: two[0], SWEEP: two[0], FLOW: two[1]}
        with pytest.raises(StoreRefused, match="name the shard"):
            s.replace_dead_man_run(RUN, rows, proofs=proofs, outcome=done)
        with pytest.raises(RunNotComplete, match="post-run proof"):
            s.replace_dead_man_run(RUN, rows, proofs=proofs[:1], outcome=done, shard_of=where)
        s.replace_dead_man_run(RUN, rows, proofs=proofs, outcome=done, shard_of=where)
        got = s.conn.execute("SELECT checkpoint_id, shard_id FROM outcomes ORDER BY checkpoint_id").fetchall()
        assert [tuple(g) for g in got] == [(CENSUS, two[0]), (FLOW, two[1]), (SWEEP, two[0])]


def test_a_partial_late_arrival_is_refused(store):
    # store:5, 10.2 and 1.10: harness_error (dead_man) belongs only to an aborted_harness (dead_man) run, so the late
    # arrival replaces every placeholder or none; the run stays dead_man until all its rows arrive
    dead_man(store)
    for status in ("complete", "complete_with_env_errors"):
        with pytest.raises(StoreRefused, match="every dead_man placeholder"):
            store.replace_dead_man_run(RUN, [out(CENSUS, diff=D)], outcome=RunOutcome(RUN, "pr", status, "reported"),
                                       arrived_at="2026-11-09T16:05:00+00:00")
    run = store.run_outcome(RUN)
    assert (run.status, run.reason) == ("aborted_harness", "dead_man")
    assert {o.reason for o in store.outcomes(RUN)} == {"dead_man"}
    assert store.conn.execute("SELECT count(*) FROM outcomes_superseded").fetchone()[0] == 0
    rows = [out(cid, diff=D) for cid in (CENSUS, SWEEP, FLOW)]
    assert store.replace_dead_man_run(RUN, rows, outcome=RunOutcome(RUN, "pr", "complete", "reported"),
                                      proofs=[late_proof()]).status == "complete"


def test_a_run_that_did_not_end_dead_man_is_not_replaced(store):
    store.fill_unreached(RUN, "harness_error", "watchdog_dead", detail="watchdog_event: wd-41")
    store.end_run(RunOutcome(RUN, "pr", "aborted_harness", "running", reason="watchdog_dead"))
    with pytest.raises(StoreRefused, match="dead_man"):
        store.replace_dead_man_run(RUN, [out(CENSUS, diff=D)], outcome=RunOutcome(RUN, "pr", "complete", "reported"))


def test_a_refused_late_row_leaves_the_dead_man_run_as_it_was(store):
    dead_man(store)
    bad = out(SWEEP, diff=D, artifacts={"candidate": "e" * 64})           # names an artifact nobody stored
    with pytest.raises(StoreRefused, match="not stored"):
        store.replace_dead_man_run(RUN, [out(CENSUS, diff=D), bad, out(FLOW, diff=D)],
                                   outcome=RunOutcome(RUN, "pr", "complete", "reported"))
    run = store.run_outcome(RUN)
    assert (run.status, run.reason) == ("aborted_harness", "dead_man")
    assert {o.reason for o in store.outcomes(RUN)} == {"dead_man"}
    assert store.conn.execute("SELECT count(*) FROM outcomes_superseded").fetchone()[0] == 0
