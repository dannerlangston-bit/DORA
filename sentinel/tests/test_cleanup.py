"""Cross-module checks of the cleanup fixes: region-relative component boxes from the Diff Engine through the
store to report clustering (engine choice 16, 9.7), and a Nightly run's canary rows through the store (8.3, 10.3)."""
from __future__ import annotations

from sentinel import canary as C
from sentinel.diff.engine import DiffContext, diff_pair
from sentinel.records import CheckpointOutcome, Classification, Component, Finding
from sentinel.report.cluster import Member, cluster_members
from sentinel.report.model import COLLATERAL
from sentinel.store import RunPlan, ScopeEntry, Store

from test_canary import CONFIRM, mutant, result
from test_diff_engine import moved_panel_pair

SURFACE = "monitor.activity.position-statement"
PANEL_AT = {"ws.tier-a.active-trader": (318, 6), "ws.tier-a.swing-trader": (120, 190)}   # the panel sits elsewhere


def _plan(run_id: str, run_type: str, scope: list[ScopeEntry]) -> RunPlan:
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=tuple(scope))


def test_a_defect_in_a_moved_panel_still_clusters_after_the_store_round_trip(tmp_path):
    """Two census checkpoints, one defect: the panel sits at (318, 6) in one workspace, (120, 190) in the other."""
    run_id = "run-2026-11-10-pr-0001"
    written = []
    for n, (ws, origin) in enumerate(PANEL_AT.items(), 1):
        cid = f"census/{SURFACE}/{ws}"
        base, cand, reg = moved_panel_pair(origin, cid=cid)
        res = diff_pair(base.image, cand.image, reg, context=DiffContext(run_id=run_id, checkpoint_id=cid, tier=1))
        assert res.level == "above"
        top = max(res.components, key=lambda k: (k.area, k.bbox_region))
        o = CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome="collateral", workspace_id=ws,
                              persona="active_trader", data_mode="frozen", tier=1, primitive="PositionTable",
                              diff=res.summary, classification=Classification("collateral", "rule"))
        f = Finding(f"fnd-{run_id}-{n:03d}", run_id, "collateral", "P/L cell changed", run_id, checkpoint_id=cid,
                    tier=1, region_id=top.region_id, primitive="PositionTable", phash=top.phash, category="collateral")
        written.append((o, f, top))
    (_, _, a), (_, _, b) = written
    assert a.bbox != b.bbox and a.bbox_region == b.bbox_region

    with Store(tmp_path / "run.db") as s:
        s.begin_run(_plan(run_id, "pr", [ScopeEntry(o.checkpoint_id, o.workspace_id, "active_trader", 1, "frozen")
                                         for o, _, _ in written]))
        for o, f, _ in written:
            s.write_checkpoint(o, findings=[f])
        stored = [(s.get_outcome(run_id, o.checkpoint_id), s.get_finding(f.finding_id)) for o, f, _ in written]
    assert [o for o, _ in stored] == [o for o, _, _ in written]            # bbox_region survives the store
    members = [Member(o, f, "collateral", COLLATERAL, f.region_id) for o, f in stored]
    assert [len(c.members) for c in cluster_members(members)] == [2]

    for m in members:                                                      # by window boxes alone they fall apart
        m.outcome.diff.components = [Component(c.area, c.bbox, c.region_id) for c in m.outcome.diff.components]
    assert [len(c.members) for c in cluster_members(members)] == [1, 1]


def test_a_nightly_runs_canary_rows_go_to_canary_results_and_its_blind_rows_to_outcomes(tmp_path):
    """Two money-cell mutants of one checkpoint: two canary rows, one synthetic harness_error outcome (8.3, 10.3)."""
    run_id = "run-2026-11-10-nightly-0001"
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    m06 = mutant("M06", "swapped_digit", money=True, outcomes=("assertion_failed",))
    rep = C.score([result(m05, "identical"), result(m06, "assertion_failed", "assertion:price")])
    assert [(r.mutant_id, r.checkpoint_id, r.state) for r in rep.results] == [
        ("M05", CONFIRM, "detector_blind"), ("M06", CONFIRM, "detected")]
    with Store(tmp_path / "run.db") as s:
        s.begin_run(_plan(run_id, "nightly", [ScopeEntry(CONFIRM, "ws.tier-a.active-trader", "active_trader", 1,
                                                         "frozen", flow_class="standard")]))
        s.write_canary_results(run_id, rep.results)
        for o in rep.outcome_rows:
            s.write_checkpoint(o)
        assert s.canary_results(run_id) == rep.results
        blind = s.get_outcome(run_id, CONFIRM)
        assert (blind.outcome, blind.reason, blind.synthetic) == ("harness_error", "detector_blind", True)
