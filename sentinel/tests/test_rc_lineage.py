"""The RC base (1.9, 3.1, 9.8) and the lineage soak (10.11), read from a Central Store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel import rc_lineage
from sentinel.records import CheckpointOutcome, Fingerprint, RunOutcome
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

RELEASED = "released-1998.1.0-8b2d4f6"
OLDER = "released-1998.0.0-3c9e7a1"
RC = "rc-1998.2.0-4f1c9a2"
TS = "2026-10-01T10:00:00Z"
WS = "ws.tier-a.active-trader"
CENSUS = ("census/trade.order-entry.stock/ws.tier-a.active-trader",
          "census/monitor.activity.position-statement/ws.tier-a.active-trader")
SOAK = tuple(f"flow/trade.active-trader.ladder/soak/c{n:02d}" for n in (1, 2))
MATCH = {"build_id": RELEASED, "display": {"w": 1920, "h": 1080}, "data_epoch": "e-2026-10-01"}
FP = Fingerprint(run_id="x", side="candidate", rig_id="rig-01", read_at=TS, match=MATCH).fingerprint_id
OTHER = Fingerprint(run_id="x", side="candidate", rig_id="rig-01", read_at=TS,
                    match={**MATCH, "data_epoch": "e-2026-11-11"}).fingerprint_id


def plan(run_id, scope, pair_id, run_type="rc"):
    entries = tuple(ScopeEntry(c, WS, "active_trader", 1, "masked_live",
                               flow_class="standard" if c.startswith("flow/") else None) for c in scope)
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                   budget_sec=8 * 3600, scope=entries, shards=(ShardPlan(f"{run_id}/1", "rig-05"),), pair_id=pair_id,
                   started_at=TS)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-05", login_alias="paper-05", owner="ops", snapshot_id="snap-5", allowlist_entry=5)
    for b, ch, rel in ((OLDER, "released", "2026-07-01T00:00:00Z"), (RELEASED, "released", "2026-10-15T00:00:00Z"),
                       ("main-1998.0.9-1111111", "main", None), (RC, "rc", None)):
        s.register_build(b, channel=ch, source_commit=b[-7:] * 5 + "x" * 5, pipeline_artifact_url=f"https://a/{b}",
                         installer_sha256="c" * 64, released_at=rel)
    yield s
    s.close()


def source_run(store, run_id, build, match, scope=CENSUS):
    """The run that captured `build` as its candidate on the fingerprint `match`, with its scope."""
    pair = store.register_pair(build, "main-1998.0.9-1111111" if build != "main-1998.0.9-1111111" else OLDER, "rc")
    store.begin_run(plan(run_id, scope, pair))
    store.write_fingerprint(Fingerprint(run_id=run_id, side="candidate", rig_id="rig-05", read_at=TS,
                                        match={**match, "build_id": build}, shard_id=f"{run_id}/1"))
    return pair


def approve(store, cid, build, run_id, image="1" * 64):
    store.insert("baselines", {"checkpoint_id": cid, "lineage": "released", "build_id": build,
                               "image_sha256": image, "state": "approved", "source_run_id": run_id,
                               "proposed_at": TS, "approved_by": "jdoe", "approved_at": TS,
                               "approval_reason": "release sign-off"})


def test_the_last_released_build():
    with Store(":memory:") as empty:
        assert rc_lineage.released_build(empty) is None


def test_a_set_on_the_current_fingerprint_is_the_base_and_a_missing_checkpoint_is_lineage_missing(store):
    source_run(store, "rc-prev", RELEASED, MATCH)
    approve(store, CENSUS[0], RELEASED, "rc-prev", image="2" * 64)
    base = rc_lineage.resolve(store, fingerprint_id=FP, scope=CENSUS)
    assert base.mode == "stored" and base.released_build_id == RELEASED
    assert base.base_image(CENSUS[0]) == "2" * 64 and base.missing == (CENSUS[1],)
    [(cid, o)] = base.outcomes().items()
    assert cid == CENSUS[1] and (o.outcome, o.reason) == ("needs_baseline", "lineage_missing")
    assert "1 lineage_missing" in base.health_line() and base.to_summary()["mode"] == "stored"


def test_a_fingerprint_mismatch_triggers_a_recapture(store):
    source_run(store, "rc-prev", RELEASED, MATCH)
    approve(store, CENSUS[0], RELEASED, "rc-prev")
    approve(store, CENSUS[1], RELEASED, "rc-prev")
    base = rc_lineage.resolve(store, fingerprint_id=OTHER, scope=CENSUS)
    assert base.mode == "recapture" and base.recapture == CENSUS and base.outcomes() == {}
    assert "another fingerprint" in base.detail and RELEASED in base.health_line()


def test_an_older_releases_image_counts_and_an_unrecorded_fingerprint_forces_a_recapture(store):
    source_run(store, "rc-old", OLDER, MATCH)
    source_run(store, "rc-prev", RELEASED, MATCH, scope=(CENSUS[1],))
    approve(store, CENSUS[0], OLDER, "rc-old")
    approve(store, CENSUS[1], RELEASED, "rc-prev")
    assert rc_lineage.resolve(store, fingerprint_id=FP, scope=CENSUS).mode == "stored"
    store.insert("baselines", {"checkpoint_id": CENSUS[0], "lineage": "beta", "build_id": OLDER, "image_sha256": "3" * 64,
                               "state": "approved", "source_run_id": None, "proposed_at": TS, "approved_by": "jdoe",
                               "approved_at": TS, "approval_reason": "x"})
    beta = rc_lineage.resolve(store, "beta", fingerprint_id=FP, scope=CENSUS)
    assert beta.mode == "recapture" and "none recorded" in beta.detail


def test_no_approved_set_is_a_recapture_not_lineage_missing_everywhere(store):
    base = rc_lineage.resolve(store, fingerprint_id=FP, scope=CENSUS)
    assert base.mode == "recapture" and base.missing == () and "no approved baseline set" in base.detail


# -- the lineage soak --------------------------------------------------------------------------------------------
def lineage_rc(store, match=MATCH, signatures=("com.tos.A.a",)):
    source_run(store, "rc-prev", RELEASED, match, scope=SOAK)
    start = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    store.write_soak_samples("rc-prev", [{"sampled_at": (start + timedelta(minutes=m)).isoformat(), "pid": 7,
                                          "metric": "working_set", "value": 1e9 + m} for m in (1, 2, 3)])
    for n, cid in enumerate(SOAK, 1):
        sha = f"{n}" * 64
        store.insert("artifacts", {"sha256": sha, "kind": "capture", "media_type": "image/png", "bytes": 10,
                                   "redaction_passed": 1, "set_path": "runs/rc-prev", "created_at": TS})
        store.write_checkpoint(CheckpointOutcome(run_id="rc-prev", checkpoint_id=cid, outcome="identical",
                                                 workspace_id=WS, persona="active_trader", data_mode="masked_live",
                                                 artifacts={"candidate": sha}), shard_id="rc-prev/1")
    store.update_summary("rc-prev", {"rc": {"soak": {"log_signatures": list(signatures)}}})
    store.end_run(RunOutcome(run_id="rc-prev", run_type="rc", status="complete_with_env_errors", last_gate="reported"))


def test_the_lineage_soak_is_comparable_with_a_matching_fingerprint(store):
    lineage_rc(store)
    ls = rc_lineage.lineage_soak(store, RELEASED, fingerprint_id=FP)
    assert ls.run_id == "rc-prev" and ls.comparable and ls.fingerprint_id == FP
    assert [s.t_s for s in ls.samples] == [60.0, 120.0, 180.0]
    assert ls.capture(2) == "2" * 64 and ls.log_signatures == frozenset({"com.tos.A.a"})


def test_a_lineage_soak_on_another_fingerprint_is_not_comparable(store):
    lineage_rc(store)
    ls = rc_lineage.lineage_soak(store, RELEASED, fingerprint_id=OTHER)
    assert not ls.comparable and ls.capture(1) is None and ls.samples
    assert not rc_lineage.lineage_soak(store, OLDER, fingerprint_id=FP).comparable
    assert rc_lineage.lineage_soak(store, None, fingerprint_id=FP).run_id is None
