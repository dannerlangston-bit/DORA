"""Tests for the store's record integrity: L-OUT-02 and L-OUT-06 at write, the 8.3 outcome, step and finding
columns, decisions under 4.14 and 9.7, build pairs, the run summary, the post-run check and the late arrival."""
import dataclasses
import hashlib
import json
import sqlite3

import pytest

from sentinel.records import (CheckpointOutcome, Classification, Decision, DiffSummary, Finding, RunOutcome, Signature,
                              StepOutcome)
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store, StoreRefused
from sentinel.store.artifacts import StoredArtifact
from sentinel.store.store import PaperAssertionMissing, RecordIncomplete

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
                       app_restart_count=1, reason="export_timeout", finding_ref="F-9")
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
