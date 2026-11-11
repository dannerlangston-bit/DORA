"""The Dependency Map derivation job (sentinel.depscan): jdeps parsing, derived edges, the add-or-confirm map update
of 4.4 and the scan_failed status the next merge run reads (2.10.1, 2.10.2)."""
from __future__ import annotations

import json
import random
import shutil
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from sentinel.depscan import (DerivedEdge, MapUpdateError, ScanError, check_update, derive, last_scan_failed, main,
                              map_update, parse_jdeps)
from sentinel.lint import Repo, lint
from sentinel.scope import DependencyIndex, DependencyMap, Edge, Manifest, Registry, Rig, Workspace, compute_scope

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
BUILD = "main-1998.3.0-9a0b1c2"
NOW = datetime(2026, 11, 10, 3, 10, tzinfo=timezone.utc)
LADDER, CHART, PS = "trade.active-trader.ladder", "charts.flexible-grid.chart-cell", "monitor.activity.position-statement"

JDK9 = """\
app.jar -> java.base
app.jar -> not found
   com.tos.desktop.trade.activetrader.LadderPanel     -> com.tos.desktop.ui.grid.FlexibleGridCell           app.jar
   com.tos.desktop.trade.activetrader.LadderPanel     -> java.lang.Object                                   java.base
   com.tos.desktop.charts.grid.chartcell.ChartCellView -> com.tos.desktop.charts.grid.CellAdapter           app.jar
   com.tos.desktop.charts.grid.CellAdapter            -> com.tos.desktop.ui.grid.GridCell                   app.jar
   com.tos.desktop.monitor.positions.PositionTable    -> com.tos.desktop.trade.order.OrderModel             app.jar
   com.tos.desktop.monitor.positions.PositionTable    -> com.tos.desktop.ui.laf.Colors                      app.jar
Warning: split package: com.tos.desktop.util
"""
JDK8 = """\
app.jar -> /usr/lib/jvm/jre/lib/rt.jar
   com.tos.desktop.trade.activetrader.LadderPanel (app.jar)
      -> com.tos.desktop.ui.grid.FlexibleGridCell
      -> java.lang.Object                                  rt.jar
"""
DOT = """\
digraph "app.jar" {
    "com.tos.desktop.charts.grid.CellAdapter"  -> "com.tos.desktop.ui.grid.GridCell (app.jar)";
}
"""


@pytest.fixture(scope="module")
def example() -> tuple[DependencyMap, Registry, dict]:
    repo = Repo.load(EXAMPLE)
    doc = repo.one("dependency_map").data
    return DependencyMap.from_dict(doc), Registry.from_entries(d.data for d in repo.of("surface")), doc


def test_parse_jdeps_reads_the_three_formats_and_skips_summaries():
    fwd = parse_jdeps(JDK9)
    assert fwd["com.tos.desktop.trade.activetrader.LadderPanel"] == {"com.tos.desktop.ui.grid.FlexibleGridCell",
                                                                     "java.lang.Object"}
    assert "app.jar" not in fwd and len(fwd) == 4
    assert parse_jdeps(JDK8) == {"com.tos.desktop.trade.activetrader.LadderPanel": frozenset(
        {"com.tos.desktop.ui.grid.FlexibleGridCell", "java.lang.Object"})}
    assert parse_jdeps(DOT) == {"com.tos.desktop.charts.grid.CellAdapter": frozenset({"com.tos.desktop.ui.grid.GridCell"})}
    index = DependencyIndex.from_depends_on(fwd)                  # the reverse index of 2.10.2
    assert index.dependents["com.tos.desktop.charts.grid.CellAdapter"] == {
        "com.tos.desktop.charts.grid.chartcell.ChartCellView"}
    with pytest.raises(ScanError):
        parse_jdeps("app.jar -> java.base\nWarning: nothing\n")


def test_derive_proposes_transitive_edges_with_their_chain_and_skips_global_primitives(example):
    dep_map, registry, _ = example
    scan = derive(parse_jdeps(JDK9), dep_map, registry)
    got = {(e.primitive, e.surface): e.evidence for e in scan.edges}
    assert got == {
        ("FlexibleGrid", LADDER): "com.tos.desktop.trade.activetrader.LadderPanel -> com.tos.desktop.ui.grid.FlexibleGridCell",
        ("FlexibleGrid", CHART): ("com.tos.desktop.charts.grid.chartcell.ChartCellView -> "
                                  "com.tos.desktop.charts.grid.CellAdapter -> com.tos.desktop.ui.grid.GridCell"),
        ("OrderTicketCore", PS): "com.tos.desktop.monitor.positions.PositionTable -> com.tos.desktop.trade.order.OrderModel",
    }                                                             # Theme is global: its "*" edge covers every surface
    bounded = derive(parse_jdeps(JDK9), dep_map, registry, max_depth=1)
    assert ("FlexibleGrid", CHART) not in {(e.primitive, e.surface) for e in bounded.edges}


def test_map_update_adds_new_edges_and_confirms_existing_ones(example):
    dep_map, registry, _ = example
    update = map_update(dep_map, derive(parse_jdeps(JDK9), dep_map, registry), BUILD, NOW)
    (added,) = update.added
    assert added == Edge("OrderTicketCore", PS, "static_analysis", 1.0, True, BUILD, added.evidence,
                         first_seen=NOW.isoformat(), last_verified=NOW.isoformat())
    confirmed = {(e.primitive, e.surface): e for e in update.confirmed}
    chart = confirmed[("FlexibleGrid", CHART)]                    # a declared edge the scan now sees
    assert (chart.source, chart.confirmed_from_source, chart.confirmed_build, chart.confidence) == (
        "declared", True, BUILD, 1.0)
    assert chart.last_verified == NOW.isoformat() and chart.first_seen.startswith("2026-10-01")
    assert len(update.edges) == len(dep_map.edges) + 1 and update.edges[-1] == added
    unseen = next(e for e in update.edges if e.primitive == "LadderModel" and e.surface == "charts.flexible-grid.active-trader-cell")
    assert unseen.confirmed_from_source is False and unseen.reason        # keeps its human reason (4.4)
    assert update.first_scan_at == "2026-11-10"


def test_a_static_analysis_edge_seen_again_is_raised_to_confidence_one():
    dm = DependencyMap({}, [Edge("ColumnModel", PS, "static_analysis", 0.82, evidence="a.B imports c.D",
                                 first_seen="2026-11-02T03:10:00-05:00", last_verified="2026-11-02T03:10:00-05:00")])
    update = map_update(dm, [DerivedEdge("ColumnModel", PS, "x.Y -> c.D")], BUILD, NOW)
    (e,) = update.confirmed
    assert (e.confidence, e.confirmed_from_source, e.confirmed_build, e.evidence) == (1.0, True, BUILD, "a.B imports c.D")
    assert update.added == ()


def test_a_glob_edge_covers_the_surfaces_it_matches():
    dm = DependencyMap({}, [Edge("ChartEngine", "charts.*", "declared", 1.0, first_seen="2026-10-01T09:00:00-04:00",
                                 last_verified="2026-10-01T09:00:00-04:00")])
    update = map_update(dm, [DerivedEdge("ChartEngine", CHART, "a -> b"), DerivedEdge("ChartEngine", "charts.x", "c -> d")],
                        BUILD, NOW)
    assert update.added == () and [e.surface for e in update.confirmed] == ["charts.*"]


def test_map_update_never_removes_an_edge_nor_lowers_a_confidence():
    """A property test over random maps and scans (4.4): whatever the scan sees, every old edge survives with a
    confidence at least as high, and the result passes L-DEP-03 and L-DEP-04."""
    rng = random.Random(20261110)
    prims, surfaces = [f"P{i}" for i in range(5)], [f"area.s{i}" for i in range(8)]
    for _ in range(200):
        pairs = rng.sample([(p, s) for p in prims for s in surfaces], rng.randint(0, 12))
        edges = []
        for p, s in pairs:
            if rng.random() < 0.5:
                edges.append(Edge(p, s, "declared", 1.0, first_seen="2026-10-01", last_verified="2026-10-01"))
            else:
                conf = rng.choice([0.5, 0.82, 0.9, 1.0])
                edges.append(Edge(p, s, "static_analysis", conf, conf == 1.0, "main-1" if conf == 1.0 else None, "e",
                                  first_seen="2026-10-01", last_verified="2026-10-01"))
        dm = DependencyMap({}, edges)
        seen = [DerivedEdge(p, s, f"{s} -> {p}") for p, s in rng.sample([(p, s) for p in prims for s in surfaces], 6)]
        update = map_update(dm, seen, BUILD, NOW)
        after = {(e.primitive, e.surface): e for e in update.edges}
        assert all((e.primitive, e.surface) in after and after[(e.primitive, e.surface)].confidence >= e.confidence
                   for e in edges)
        assert all((d.primitive, d.surface) in after and after[(d.primitive, d.surface)].confirmed_from_source for d in seen)
        assert check_update(edges, update.edges) == []


def test_check_update_refuses_a_removal_a_lowered_confidence_and_an_unconfirmed_one():
    old = [Edge("P", "a.b", "static_analysis", 0.9, evidence="e", first_seen="t", last_verified="t")]
    assert any("removed" in p for p in check_update(old, []))
    assert any("lowered" in p for p in check_update(old, [replace(old[0], confidence=0.5)]))
    assert any("confirmed_from_source" in p for p in check_update(old, [replace(old[0], confidence=1.0)]))
    with pytest.raises(MapUpdateError, match="evidence"):
        map_update(DependencyMap({}, []), [DerivedEdge("P", "a.b", "")], BUILD, NOW)


def test_pr_proposed_edges_become_a_map_update_from_the_candidate_build(example):
    """The static impact step's proposals (2.10.2) go through the same add-or-confirm update, so the proposed map
    carries first_seen, last_verified, confirmed_build and evidence (4.4)."""
    dep_map, _registry, _ = example
    proposed = [Edge("FlexibleGrid", PS, "static_analysis", 1.0, True, "branch-1998.3.0-7be42d0", "a.B -> c.D")]
    update = map_update(dep_map, proposed, "branch-1998.3.0-7be42d0", NOW)
    (e,) = update.added
    assert (e.confirmed_build, e.first_seen, e.evidence) == ("branch-1998.3.0-7be42d0", NOW.isoformat(), "a.B -> c.D")
    assert update.patch()["add"] == [e.to_dict()] and update.patch()["confirm"] == []


def test_depmap_writes_a_proposed_map_that_validates_and_passes_l_dep_03(tmp_path):
    cfg = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, cfg)
    (tmp_path / "jdeps.txt").write_text(JDK9, encoding="utf-8")
    out, patch, status = tmp_path / "map.yaml", tmp_path / "patch.yaml", tmp_path / "status.json"
    rc = main(["--jdeps", str(tmp_path / "jdeps.txt"), "--config", str(cfg), "--build", BUILD,
               "--now", "2026-11-10T03:10:00-05:00", "--out", str(out), "--patch", str(patch), "--status", str(status)])
    assert rc == 0 and json.loads(status.read_text())["status"] == "ok" and not last_scan_failed(status)
    assert [e["surface"] for e in yaml.safe_load(patch.read_text())["add"]] == [PS]
    shutil.copy(out, cfg / "registry" / "dependency-map.yaml")
    rules = {v.rule for v in lint(cfg, today=date(2026, 11, 10)).violations}
    assert not rules & {"L-DEP-00", "L-DEP-03", "L-DEP-04"}, rules
    assert "L-DEP-05" in rules              # the new edge still needs its registry entry to list the primitive


def test_a_failed_scan_keeps_the_map_and_opens_scan_failed_on_the_next_merge_run(tmp_path, example):
    dep_map, registry, _ = example
    status, out = tmp_path / "status.json", tmp_path / "map.yaml"
    rc = main(["--jdeps", str(tmp_path / "missing.txt"), "--config", str(EXAMPLE), "--build", BUILD,
               "--now", "2026-11-10T03:10:00-05:00", "--out", str(out), "--status", str(status)])
    assert rc == 2 and not out.exists() and last_scan_failed(status)
    assert json.loads(status.read_text())["status"] == "scan_failed"
    (tmp_path / "empty.txt").write_text("app.jar -> java.base\n", encoding="utf-8")
    assert main(["--jdeps", str(tmp_path / "empty.txt"), "--config", str(EXAMPLE), "--build", BUILD,
                 "--now", "2026-11-10T03:10:00-05:00", "--status", str(status)]) == 2
    repo = Repo.load(EXAMPLE)
    plan = compute_scope("merge", Manifest.union([], candidate=BUILD, base="main-1998.3.0-2c91ae4", author="o"),
                         dep_map, registry, run_id="run-2026-11-10-merge-0001",
                         workspaces=[Workspace.from_dict(d.data) for d in repo.of("workspace")],
                         entitlements=("margin", "ira"), rigs=[Rig(f"rig-{i}", "fp") for i in range(1, 5)],
                         map_scan_failed=last_scan_failed(status))
    assert [f.evidence["raw_values"]["reason"] for f in plan.findings if f.kind == "dependency_map_gap"] == ["scan_failed"]
    assert not last_scan_failed(tmp_path / "never-written.json")
