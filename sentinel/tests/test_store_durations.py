"""Measured durations from step_events (11 preamble, 11.2, 2.11, 7.10, L-FLW-10; `sentinel.store.durations`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint_repo
from sentinel.records import StepEvent
from sentinel.scope import DependencyMap, Flow, Manifest, Registry, Rig, Surface, Workspace, compute_scope
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.durations import (Durations, durations, flow_budget_s, flow_duration_p50, percentile)

NOW = datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)
CP = "census/monitor.position-statement/ws.tier-a.options-trader"
FLOW = "flow.trade.order-ticket.stock.limit"
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
FIX = Path(__file__).parent / "fixtures" / "scope"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="vault://paper/rig-01", owner="asmith", snapshot_id="snap-1",
                   allowlist_entry=1)
    yield s
    s.close()


class Runs:
    """Begins runs with one shard each and writes timed step events into them."""

    def __init__(self, store: Store):
        self.store, self.n, self.seq = store, 0, {}

    def run(self, started: datetime, scope: tuple[str, ...] = (CP,)) -> str:
        self.n += 1
        run_id = f"run-2026-11-{self.n:02d}-merge-01"
        entries = tuple(ScopeEntry(c, c.split("/")[2], "options_trader", 1, "frozen") for c in scope)
        self.store.begin_run(RunPlan(run_id=run_id, run_type="merge", config_commit="c0ffee",
                                     config_tarball_sha256="a" * 64, snapshot_set_sha256="b" * 64, harness_sha="d00d",
                                     authorization_ref="auth/v1.md", budget_sec=10800, scope=entries,
                                     shards=(ShardPlan(f"{run_id}/1", "rig-01"),),
                                     started_at=started.isoformat()))
        return run_id

    def event(self, run_id: str, at: datetime, ms: int | None, *, checkpoint_id: str | None = None,
              flow_id: str | None = None, side: str = "candidate", drill: bool = False, mode: str = "scripted",
              source: str = "orchestrator") -> None:
        shard = f"{run_id}/1"
        self.seq[shard] = self.seq.get(shard, 0) + 1
        self.store.write_events([StepEvent(run_id, shard, side, self.seq[shard], source, at.isoformat(), 0, mode,
                                           "capture" if checkpoint_id else "click", flow_id=flow_id,
                                           step_id="s01" if flow_id else None, checkpoint_id=checkpoint_id,
                                           duration_ms=ms, drill=drill)])


def test_ten_runs_give_the_nearest_rank_p50_and_p95(store):
    runs = Runs(store)
    for i in range(10):                                   # 10 s .. 19 s, shuffled order of arrival
        rid = runs.run(NOW - timedelta(days=1, hours=i))
        secs = (13, 17, 10, 19, 11, 15, 12, 18, 14, 16)[i]
        runs.event(rid, NOW - timedelta(days=1, hours=i), secs * 1000 - 2000, checkpoint_id=CP)
        runs.event(rid, NOW - timedelta(days=1, hours=i), 2000, checkpoint_id=CP)   # summed per run and side
    d = durations(store, now=NOW)
    assert d.checkpoints[CP].n == 10
    assert (d.checkpoints[CP].p50_s, d.checkpoints[CP].p95_s) == (14.0, 19.0)
    assert d.p50() == {CP: 14.0}
    assert d.lines() == ["measured census duration (30 d): p50 14.0 s, p95 19.0 s per side over 1 census checkpoints"]


def test_each_side_is_an_observation_and_untimed_rows_measure_nothing(store):
    runs = Runs(store)
    rid = runs.run(NOW - timedelta(hours=3))
    runs.event(rid, NOW - timedelta(hours=3), 12000, checkpoint_id=CP, side="base")
    runs.event(rid, NOW - timedelta(hours=3), 20000, checkpoint_id=CP, side="candidate")
    runs.event(rid, NOW - timedelta(hours=3), None, flow_id="flow.untimed")
    d = durations(store, now=NOW)
    assert d.checkpoints[CP].n == 2 and d.checkpoints[CP].p50_s == 12.0
    assert "flow.untimed" not in d.flows


def test_window_drill_explorer_and_hook_rows_are_left_out(store):
    runs = Runs(store)
    old = runs.run(NOW - timedelta(days=40))
    runs.event(old, NOW - timedelta(days=40), 99000, checkpoint_id=CP)            # outside the 30 days
    rid = runs.run(NOW - timedelta(days=2))
    runs.event(rid, NOW - timedelta(days=2), 30000, flow_id=FLOW)
    runs.event(rid, NOW - timedelta(days=2), 50000, flow_id=FLOW, drill=True)
    runs.event(rid, NOW - timedelta(days=2), 50000, flow_id=FLOW, mode="explorer")
    runs.event(rid, NOW - timedelta(days=2), 50000, flow_id=FLOW, source="gateway_hook")
    d = durations(store, now=NOW)
    assert CP not in d.checkpoints
    assert d.flow_p50() == {FLOW: 30.0} and d.flow_p95() == {FLOW: 30.0}
    assert flow_duration_p50(store, FLOW, now=NOW) == 30.0
    assert flow_duration_p50(store, "flow.never.run", now=NOW) is None
    assert durations(store, 60, now=NOW).checkpoints[CP].p50_s == 99.0


def test_flow_budget_is_three_times_p50_and_five_minutes_until_measured(store):
    runs = Runs(store)
    rid = runs.run(NOW - timedelta(days=1))
    runs.event(rid, NOW - timedelta(days=1), 40000, flow_id=FLOW)
    assert flow_budget_s(store, FLOW, now=NOW) == 120.0
    assert flow_budget_s(store, "flow.never.run", now=NOW) == 300.0
    d = durations(store, now=NOW)
    assert (d.flow_budget_s(FLOW), d.flow_budget_s("flow.never.run")) == (120.0, 300.0)


def test_a_measured_p50_of_40_s_makes_a_180_s_budget_fail_l_flw_10(store):
    runs = Runs(store)
    rid = runs.run(NOW - timedelta(days=1))
    runs.event(rid, NOW - timedelta(days=1), 40000, flow_id=FLOW)
    repo = Repo.load(EXAMPLE)
    unmeasured = {v.rule for v in lint_repo(repo, 0, NOW.date()).violations}
    measured = lint_repo(repo, 0, NOW.date(), flow_p50_s=durations(store, now=NOW).flow_p50()).violations
    assert "L-FLW-10" not in unmeasured                                  # 180 s is inside the 5-minute default
    hits = [v for v in measured if v.rule == "L-FLW-10"]
    assert len(hits) == 1 and "180 exceeds 120" in hits[0].message


def test_measured_durations_change_the_scope_cut(store):
    def load(name):
        return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))
    base = Registry.from_entries(load("registry.yaml"))
    smoke = sum(s.smoke for s in base)
    filler = [Surface(id=f"tools.filler-{i:02d}", tier=3, kind="subtab", smoke=True,
                      roots=(f"com.tos.desktop.filler.f{i:02d}",), personas=("beginner",),
                      census_workspaces=("ws.tier-a.beginner",)) for i in range(1, 41 - smoke)]
    registry = Registry.from_entries(list(base) + filler)
    manifest = Manifest.union([Manifest.from_dict(load("manifest-flexgrid.yaml"))], candidate="main-1998.3.0-9a0b1c2",
                              base="main-1998.3.0-2c91ae4", author="orchestrator", approved_by="triage-lead")
    args = dict(manifest=manifest, dep_map=DependencyMap.from_dict(load("dependency-map.yaml")), registry=registry,
                run_id="run-2026-11-10-merge-0001",
                workspaces=[Workspace.from_dict(w) for w in load("workspaces.yaml")],
                flows=[Flow.from_dict(f) for f in load("flows.yaml")], entitlements=("margin", "futures", "options_L1"),
                rigs=[Rig("rig-1"), Rig("rig-2")], budget_s=1500)
    default = compute_scope("merge", **args)
    cut = {c.checkpoint.checkpoint_id for c in default.cuts if c.reason == "budget" and c.checkpoint is not None
           and c.checkpoint.data_mode == "frozen"}
    assert cut
    runs = Runs(store)
    rid = runs.run(NOW - timedelta(days=1), scope=())
    for cid in sorted(cut | set(default.planned_ids)):
        runs.event(rid, NOW - timedelta(days=1), 1000, checkpoint_id=cid)
    measured = compute_scope("merge", **args, durations=durations(store, now=NOW).p50())
    still = {c.checkpoint.checkpoint_id for c in measured.cuts if c.reason == "budget" and c.checkpoint is not None
             and c.checkpoint.data_mode == "frozen"}
    assert still < cut


def test_percentile_is_nearest_rank():
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.0 and percentile([1, 2, 3, 4], 0.95) == 4.0
    with pytest.raises(ValueError):
        percentile([], 0.5)
    assert Durations().lines() == [] and Durations().p50() == {}
