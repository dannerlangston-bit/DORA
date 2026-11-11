"""The read-only snapshot set of a run (8.1, 8.3, 8.5; gap 08-snapshot-set): what it holds as of run creation, its
content hash, and the signature lookup on it."""
from __future__ import annotations

import sqlite3

import pytest

from sentinel.records import Decision, Finding, Signature
from sentinel.store import RunPlan, ScopeEntry, Store, StoreError, connect_reader, lookup_decisions
from sentinel.store.snapshot import SNAPSHOT_TABLES, export_snapshot, provenance_row, snapshot_sha256

RUN0 = "run-00"
WS = "ws.tier-a.active-trader"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
REGION = "rgn.ladder.bid-column"
TS = "2026-11-01T12:00:00+00:00"
AT = "2026-11-02T00:00:00+00:00"
LATER = "2026-11-03T00:00:00+00:00"
PH1, PH2 = "0123456789abcdef", "fedcba9876543210"


def decide(s, did, phash, decided_at, finding=None):
    d = Decision(did, "noise_signature", "noise", Signature(SWEEP, REGION, phash, "collateral"), decided_by="alice",
                 reason="clock", decided_at=decided_at, finding_ref=finding)
    return s.record_decision(d, run_id=RUN0)


def floor(**kw):
    row = {"checkpoint_id": SWEEP, "region_id": REGION, "rendering_key_sha256": "sha256:k1",
           "calibration_run_id": RUN0, "samples": 15, "threshold_component_area": 4, "threshold_changed_px": 9,
           "threshold_delta_e": 2.5, "threshold_shift_px": 1, "valid_from": TS}
    return {**row, **kw}


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "central.db")
    for b, at in (("m0", "2026-10-01T00:00:00+00:00"), ("b1", "2026-11-01T00:00:00+00:00")):
        st.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                          first_seen_at=at)
    st.begin_run(RunPlan(run_id=RUN0, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                         snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                         budget_sec=2700, scope=(ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),),
                         pair_id=st.register_pair("b1", "m0", "pr"), started_at=TS))
    st.write_finding(Finding("F-0", RUN0, "collateral", "clock digits", RUN0, checkpoint_id=SWEEP, tier=2,
                             region_id=REGION, phash=PH1, category="collateral", created_at=TS))
    decide(st, "dec-000001", PH1, TS, finding="F-0")
    for commit in ("c0ffee", "other"):
        st.insert("regions", {"checkpoint_id": SWEEP, "region_id": REGION, "config_commit": commit, "kind": "diff",
                              "landmark_id": "lm", "dx": 0, "dy": 0, "w": 40, "h": 20})
        st.insert("masks", {"mask_id": "m-1", "config_commit": commit, "checkpoint_id": SWEEP, "landmark_id": "lm",
                            "dx": 0, "dy": 0, "w": 10, "h": 10, "owner": "qa", "reason": "clock",
                            "created_by": "carol", "created_by_kind": "human", "created_at": TS,
                            "expires_at": "2026-12-01T12:00:00+00:00"})
    for proposed in (TS, LATER):
        st.insert("baselines", {"checkpoint_id": SWEEP, "lineage": "released", "build_id": "b1",
                                "image_sha256": "c" * 64, "state": "candidate", "proposed_at": proposed})
    st.insert("calibration_floors", floor(region_id="*", valid_from="2026-10-01T00:00:00+00:00",
                                          superseded_at="2026-10-20T00:00:00+00:00"))
    st.insert("calibration_floors", floor())
    st.insert("calibration_floors", floor(rendering_key_sha256="sha256:k2", valid_from=LATER))
    st.insert("safety_quarantines", {"target_kind": "flow", "target": "flow.a", "reason": "flake", "opened_at": TS})
    st.insert("safety_quarantines", {"target_kind": "flow", "target": "flow.b", "reason": "flake", "opened_at": TS,
                                     "reenabled_by": "dana", "reenabled_at": "2026-11-01T18:00:00+00:00",
                                     "reenable_reason": "fixed", "ticket_ref": "TOS-9"})
    st.insert("flake_stats", {"checkpoint_id": SWEEP, "quarantined": 1, "quarantine_commit": "c0ffee",
                              "quarantine_owner": "qa", "quarantine_ticket_ref": "TOS-8", "quarantine_reason": "flake",
                              "updated_at": TS})
    yield st
    st.close()


def test_the_same_content_gives_the_same_hash(s, tmp_path):
    h1 = export_snapshot(s, tmp_path / "a" / "snapshot.db", AT, config_commit="c0ffee", run_id=RUN0)
    h2 = export_snapshot(s, tmp_path / "b" / "snapshot.db", AT, config_commit="c0ffee", run_id=RUN0)
    assert h1 == h2 and len(h1) == 64 and int(h1, 16) >= 0
    assert snapshot_sha256(tmp_path / "a" / "snapshot.db") == h1
    # rows outside the snapshot set never change it
    s.insert("incidents", {"run_id": RUN0, "severity": "S3", "trigger": "drop", "opened_at": TS})
    s.set_system_state("detector_state", "ok")
    assert export_snapshot(s, tmp_path / "c.db", AT, config_commit="c0ffee", run_id=RUN0) == h1
    # another config commit is another region and mask set
    assert export_snapshot(s, tmp_path / "d.db", AT, config_commit="other", run_id=RUN0) != h1


def test_it_holds_the_decision_set_as_of_run_creation(s, tmp_path):
    path = tmp_path / "shipped" / "snapshot.db"
    export_snapshot(s, path, AT, config_commit="c0ffee", run_id=RUN0)
    conn = connect_reader(path)
    count = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in SNAPSHOT_TABLES}
    assert count == {"decisions": 1, "findings": 1, "runs": 1, "checkpoints": 1, "builds": 2, "build_pairs": 1,
                     "baselines": 1, "regions": 1, "masks": 1, "calibration_floors": 1, "safety_quarantines": 1,
                     "flake_stats": 1}
    assert conn.execute("SELECT config_commit FROM regions").fetchone()[0] == "c0ffee"
    assert conn.execute("SELECT target FROM safety_quarantines").fetchone()[0] == "flow.a"
    assert conn.execute("SELECT rendering_key_sha256, region_id FROM calibration_floors").fetchone()[:] == (
        "sha256:k1", REGION)
    assert conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    with pytest.raises(StoreError, match="no Sentinel schema version"):
        Store(path)                                       # never opened as a writable store


def test_a_decision_written_after_the_snapshot_is_not_seen_by_a_lookup_on_it(s, tmp_path):
    path = tmp_path / "snapshot.db"
    before = export_snapshot(s, path, AT, config_commit="c0ffee", run_id=RUN0)
    decide(s, "dec-000002", PH2, LATER)
    snap = connect_reader(path)
    old, new = Signature(SWEEP, REGION, PH1, "collateral"), Signature(SWEEP, REGION, PH2, "collateral")
    at = "2026-11-04T00:00:00+00:00"
    on_snapshot = lookup_decisions(snap, old, at=at, run_id=RUN0)
    live = s.lookup_decisions(old, at=at, run_id=RUN0)
    assert [m.decision.decision_id for m in on_snapshot.applicable] == ["dec-000001"]
    assert on_snapshot.inherited == live.inherited
    assert on_snapshot.inherited.finding_ref == "F-0" and on_snapshot.inherited.build_pair_id == "1"
    assert lookup_decisions(snap, new, at=at, run_id=RUN0).applicable == ()
    assert [m.decision.decision_id for m in s.lookup_decisions(new, at=at, run_id=RUN0).applicable] == ["dec-000002"]
    # the set as of AT is unchanged by the later decision; a later run's set holds it
    assert export_snapshot(s, tmp_path / "again.db", AT, config_commit="c0ffee", run_id=RUN0) == before
    assert export_snapshot(s, tmp_path / "later.db", at, config_commit="c0ffee", run_id=RUN0) != before


def test_a_failed_export_leaves_no_file(s, tmp_path, monkeypatch):
    monkeypatch.setitem(SNAPSHOT_TABLES, "decisions", "no_such_column = 1")
    with pytest.raises(sqlite3.OperationalError):
        export_snapshot(s, tmp_path / "out" / "snapshot.db", AT, config_commit="c0ffee")
    assert list((tmp_path / "out").iterdir()) == []


def test_the_provenance_row_names_both_commits_and_both_hashes():
    assert provenance_row(config_commit="c0ffee", harness_commit="d00d", config_tarball_sha256="a" * 64,
                          snapshot_set_sha256="e" * 64) == [
        "Provenance", f"config c0ffee; harness d00d; config tarball {'a' * 64}; snapshot set {'e' * 64}"]
