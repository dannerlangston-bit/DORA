"""Flake statistics over a rolling 30 days (10.5, 3.10 steps 5 and 6, 9.11), `sentinel.flake`."""
from __future__ import annotations

from datetime import date

import pytest

from sentinel import flake
from sentinel.records import CheckpointOutcome, StepEvent, StepOutcome
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

TODAY = date(2026, 11, 11)
BUILD, OTHER, BASE = "main-2026.45.1-aaa1111", "main-2026.45.2-bbb2222", "main-2026.44.9-ccc3333"
WS = "ws.tier-a.active-trader"
CP = f"census/trade.order-entry.stock/{WS}"
FLOW = "flow.trade.order-ticket.stock.limit"
FCP = f"flow/trade.order-entry.stock/{FLOW}/s06"


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    for b in (BUILD, OTHER, BASE):
        s.register_build(b, channel="main", source_commit=b[-7:], pipeline_artifact_url="u", installer_sha256="0" * 64,
                         first_seen_at="2026-10-01T00:00:00Z")
    yield s
    s.close()


SCOPE = (ScopeEntry(CP, WS, "active_trader", 1, "frozen"),
         ScopeEntry(FCP, WS, "active_trader", 1, "frozen", flow_class="standard"))


def begin(st: Store, run_id: str, day: int, *, cand: str | None = BUILD, base: str = BASE, run_type: str = "merge",
          fp_build: str | None = None) -> str:
    """A run started on November `day` (22:15 UTC); a nightly run has no pair and names its build by fingerprint."""
    at = f"2026-{11 if day > 0 else 10}-{day if day > 0 else 31 + day:02d}T22:15:00+00:00"
    pair = st.register_pair(cand, base, "merge") if cand else None
    st.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=SCOPE, shards=(ShardPlan(f"{run_id}/1", "rig-01"),), pair_id=pair, started_at=at))
    if fp_build:
        st.insert("env_fingerprints", {"run_id": run_id, "shard_id": f"{run_id}/1", "side": "candidate",
                                       "rig_id": "rig-01", "build_id": fp_build, "fingerprint_sha256": "f",
                                       "fields_json": "{}", "read_at": at})
    return run_id


def oc(run_id: str, outcome: str, reason: str | None = None, cid: str = CP, **kw) -> CheckpointOutcome:
    flow = {"flow_id": FLOW, "step_id": "s06"} if cid == FCP else {}
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, reason=reason, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", tier=1, **flow, **kw)


def put(st: Store, run_id: str, outcome: str, reason: str | None = None, cid: str = CP, steps=(), **kw) -> None:
    st.write_checkpoint(oc(run_id, outcome, reason, cid, **kw), shard_id=f"{run_id}/1", steps=steps)


def stats_of(st: Store, cid: str = CP) -> flake.FlakeStats:
    return next(s for s in flake.collect(st, TODAY) if s.checkpoint_id == cid)


def test_one_unstable_outcome_in_six_observations_of_one_build_scores_0_167(st):
    for n in range(5):
        put(st, begin(st, f"m{n}", n + 1), "identical")
    put(st, begin(st, "m5", 6), "not_settled", "unstable", side="candidate", detail="re-capture differed")
    s = stats_of(st)
    assert (s.observations, s.unstable_total, s.flip_total) == (6, 1, 0)
    assert s.flake_score == pytest.approx(1 / 6) and s.by_build[BUILD].score == pytest.approx(1 / 6)
    assert s.by_build[BUILD].observations == 6
    flake.refresh(st, TODAY, at="2026-11-11T04:00:00Z")
    row = st.query("SELECT observations, unstable_total, flake_score FROM flake_stats WHERE checkpoint_id = ?",
                   (CP,))[0]
    assert (row["observations"], row["unstable_total"]) == (6, 1) and row["flake_score"] == pytest.approx(0.166667)


def test_verdict_flips_between_runs_of_one_build_pair_and_aa_drift_hits(st):
    put(st, begin(st, "a", 1), "identical")
    put(st, begin(st, "b", 2), "collateral")
    put(st, begin(st, "c", 3), "noise", "below_floor")
    put(st, begin(st, "d", 4, cand=OTHER), "collateral")             # another build: no flip with BUILD's runs
    put(st, begin(st, "n1", 5, cand=None, run_type="nightly", fp_build=BUILD), "not_settled", "aa_drift",
        side="both", detail="regions: _rest")
    s = stats_of(st)
    assert s.flip_total == 3 and s.flips_same_build == 3 and s.by_build[BUILD].flips == 3
    assert s.by_build[OTHER] == flake.BuildCounts(1, 0, 0)
    assert s.last_flip_run_id == "n1" and s.observations == 5


def test_a_different_base_build_is_a_separate_verdict_sequence(st):
    put(st, begin(st, "a", 1), "identical")
    put(st, begin(st, "b", 2, base=OTHER), "collateral")
    assert stats_of(st).flip_total == 0


def test_unstable_counts_inside_30_and_7_days_from_outcomes_and_flake_events(st):
    put(st, begin(st, "old", -1), "not_settled", "unstable", side="candidate", detail="x")   # October 30: 12 days ago
    put(st, begin(st, "w1", 6), "not_settled", "unstable", side="candidate", detail="x")
    st.write_flake_event("w1", CP, captures=["a" * 64, "b" * 64], metrics={"changed_px": 9})  # the same run: once
    begin(st, "w2", 9)
    st.write_flake_event("w2", CP, captures=["a" * 64, "c" * 64], metrics={})                 # a base re-capture
    put(st, begin(st, "w3", 10), "not_settled", "unstable", side="candidate", detail="x")
    s = stats_of(st)
    assert (s.unstable_total, s.unstable_7d) == (4, 3)


def test_rows_outside_30_days_and_uncompared_or_synthetic_rows_are_not_observations(st):
    put(st, begin(st, "gone", -20), "identical")                     # October 11: outside the window
    put(st, begin(st, "r1", 2), "not_run", "budget")
    put(st, begin(st, "r2", 3), "not_settled", "frames_unstable", side="candidate", detail="x")
    put(st, begin(st, "r3", 4), "harness_error", "aa_drift_global", detail="watchdog_event: wd-1")
    put(st, begin(st, "r4", 5), "identical", synthetic=True)
    put(st, begin(st, "r5", 6), "identical")
    assert stats_of(st).observations == 1


def test_step_retries_and_agent_fallbacks_skip_drill_rows(st):
    rid = begin(st, "s1", 7)
    steps = [StepOutcome(rid, FLOW, "s04", "passed_after_retry", attempt=2),
             StepOutcome(rid, FLOW, "s05", "passed_via_agent_fallback", locator_rung="agent"),
             StepOutcome(rid, FLOW, "s06", "passed")]
    put(st, rid, "identical", cid=FCP, steps=steps)
    st.write_events([StepEvent(rid, f"{rid}/1", "candidate", 40, "orchestrator", "2026-11-07T22:20:00+00:00", 5,
                               "scripted", "click", flow_id=FLOW, step_id="s05", checkpoint_id=FCP,
                               step_outcome="passed_via_agent_fallback", drill=True)])
    st.write_events([StepEvent(rid, f"{rid}/1", "candidate", 41, "orchestrator", "2026-11-07T22:21:00+00:00", 6,
                               "scripted", "click", checkpoint_id=f"census/not.registered/{WS}",
                               step_outcome="passed_after_retry", attempt=2)])
    assert f"census/not.registered/{WS}" not in {x.checkpoint_id for x in flake.collect(st, TODAY)}
    s = stats_of(st, FCP)
    assert (s.step_retries, s.agent_fallbacks) == (1, 1)
    assert (s.flow_id, s.flow_class) == (FLOW, "standard")
    events = flake.window_events(st, TODAY)
    assert sorted(e.step_outcome for e in events) == ["passed", "passed_after_retry", "passed_after_retry",
                                                      "passed_via_agent_fallback", "passed_via_agent_fallback"]


def test_refresh_keeps_the_quarantine_mirror_and_zeroes_a_checkpoint_that_left_the_window(st):
    put(st, begin(st, "gone", -25), "not_settled", "unstable", side="candidate", detail="x")
    st.replace_flake_stats([{"checkpoint_id": CP, "observations": 9, "unstable_total": 3, "flake_score": 0.33,
                             "quarantined": 1, "quarantine_commit": "abc123", "quarantine_owner": "flow-owner-t1",
                             "quarantine_ticket_ref": "SENT-7", "quarantine_reason": "flake",
                             "updated_at": "2026-10-20T00:00:00Z"}])
    [s] = flake.refresh(st, TODAY)
    assert s.quarantined and s.observations == 0
    row = st.query("SELECT * FROM flake_stats WHERE checkpoint_id = ?", (CP,))[0]
    assert (row["observations"], row["unstable_total"], row["flake_score"]) == (0, 0, 0)
    assert (row["quarantined"], row["quarantine_owner"], row["quarantine_ticket_ref"]) == (1, "flow-owner-t1", "SENT-7")


def test_dashboard_and_run_flake_rates(st):
    put(st, begin(st, "a", 1), "identical")
    put(st, begin(st, "b", 2), "not_settled", "unstable", side="candidate", detail="x")
    put(st, begin(st, "c", 3), "identical", cid=FCP)
    put(st, begin(st, "d", 4), "identical", cid=FCP)
    assert flake.flake_rate(flake.collect(st, TODAY)) == pytest.approx(1 / 4)
    assert flake.flake_rate([]) is None
    rows = [oc("n", "identical"), oc("n", "not_settled", "aa_drift"), oc("n", "not_run", "budget", cid=FCP),
            oc("n", "noise", "below_floor", synthetic=True)]
    assert flake.run_flake_rate(rows) == pytest.approx(1 / 2)
    assert flake.run_flake_rate([oc("n", "not_run", "budget")]) is None
