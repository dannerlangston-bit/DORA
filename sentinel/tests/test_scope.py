"""Scope computation (2.10, 2.11, 2.5.3, 3.1, 4.3, 4.4) on the fixtures in tests/fixtures/scope/."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from sentinel.checkpoints import SweepStep
from sentinel.lint import Repo
from sentinel.lint.rules import _Linter
from sentinel.records import Finding
from sentinel.scope import (
    KIND_ORDER, LADDER_S, NIGHTLY_RIG5_WORK_S, Attribution, BaseReuse, DependencyIndex, DependencyMap, Edge, Flow,
    LadderDef, Manifest, PlanItem, PlannedCheckpoint, PrRunSummary, Registry, Rig, ScopeConfigError, SoakPlan,
    Splitter, StoredBase, Surface, Workspace, capture_time, class_matches, classes_in, compute_scope, load_ladders,
    measured_sizes, merge_run_findings, plan_calibration, plan_nightly, work_area,
)
from sentinel.shards import SESSION_STARTUP_S
from sentinel.taxonomy import is_checkpoint_id

FIX = Path(__file__).parent / "fixtures" / "scope"
RUN = "run-2026-11-10-pr-0001"
CANDIDATE = "branch-1998.3.0-7be42d0"
E = ("margin", "futures", "options_L1")
RIGS = [Rig(f"rig-{i}", "fp-9e1f03") for i in range(1, 5)]
CHANGED = {"com.tos.desktop.ui.grid.GridCellLayout", "com.tos.desktop.ui.grid.GridCellHeader"}
CHART = "charts.flexible-grid.chart-cell"
AT_CELL = "charts.flexible-grid.active-trader-cell"
L2_CELL = "charts.flexible-grid.level2-cell"
TS_CELL = "charts.flexible-grid.time-sales-cell"
GRID_CELLS = {CHART, AT_CELL, L2_CELL, TS_CELL}
LADDER = "trade.active-trader.ladder"


def _yaml(name: str):
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def _filler(first: int, last: int) -> list[Surface]:
    """Tier-3 smoke surfaces that fill the smoke subset up to the cap."""
    return [Surface(id=f"tools.filler-{i:02d}", tier=3, kind="subtab", smoke=True, roots=(f"com.tos.desktop.filler.f{i:02d}",),
                    personas=("beginner",), census_workspaces=("ws.tier-a.beginner",)) for i in range(first, last + 1)]


@pytest.fixture
def inputs() -> dict:
    base = Registry.from_entries(_yaml("registry.yaml"))
    smoke = sum(s.smoke for s in base)
    registry = Registry.from_entries(list(base) + _filler(1, 40 - smoke))
    return {
        "registry": registry,
        "dep_map": DependencyMap.from_dict(_yaml("dependency-map.yaml")),
        "dep_index": DependencyIndex.from_depends_on(_yaml("dependency-index.yaml")),
        "workspaces": [Workspace.from_dict(w) for w in _yaml("workspaces.yaml")],
        "flows": [Flow.from_dict(f) for f in _yaml("flows.yaml")],
        "manifest": Manifest.from_dict(_yaml("manifest-flexgrid.yaml")),
    }


def pr(inputs: dict, **kw):
    args = dict(manifest=inputs["manifest"], dep_map=inputs["dep_map"], registry=inputs["registry"],
                dep_index=inputs["dep_index"], changed_classes=CHANGED, run_id=RUN, workspaces=inputs["workspaces"],
                flows=inputs["flows"], entitlements=E, rigs=RIGS, candidate_build_id=CANDIDATE)
    args.update(kw)
    return compute_scope("pr", **args)


def union_manifest(inputs: dict) -> Manifest:
    return Manifest.union([inputs["manifest"]], candidate="main-1998.3.0-9a0b1c2", base="main-1998.3.0-2c91ae4",
                          author="orchestrator", approved_by="triage-lead")


def full(inputs: dict, run_type: str, **kw):
    args = dict(manifest=union_manifest(inputs), dep_map=inputs["dep_map"], registry=inputs["registry"],
                run_id=f"run-2026-11-10-{run_type}-0001", workspaces=inputs["workspaces"], flows=inputs["flows"],
                entitlements=E, rigs=RIGS)
    args.update(kw)
    return compute_scope(run_type, **args)


def ids(plan, surface=None, kind=None) -> list[str]:
    return [cp.checkpoint_id for cp in plan.planned
            if (surface is None or cp.surface_id == surface) and (kind is None or cp.test_kind == kind)]


# -- the Flexible Grid PR of 1.11 ------------------------------------------------------------

def test_flexgrid_pr_scope_has_the_four_grid_cells_and_no_findings(inputs):
    plan = pr(inputs)
    assert plan.manifest_state == "loaded" and plan.intended_enabled
    assert plan.findings == []                                   # the map carries every edge: no dependency_map_gap
    assert GRID_CELLS <= plan.scope_surfaces <= plan.census_surfaces
    census = sorted(cid for s in GRID_CELLS for cid in ids(plan, s, "census"))
    assert census == sorted([                                    # every change surface: all its census workspaces
        f"census/{CHART}/ws.tier-a.active-trader", f"census/{CHART}/ws.tier-a.swing-trader",   # declared
        f"census/{AT_CELL}/ws.tier-a.active-trader", f"census/{AT_CELL}/ws.tier-c.sweep-harness",   # map-expanded
        f"census/{L2_CELL}/ws.tier-a.active-trader", f"census/{TS_CELL}/ws.tier-a.active-trader"])
    assert ids(plan, LADDER, "census") == [f"census/{LADDER}/{w}" for w in (   # map-expanded, undeclared (1.11)
        "ws.tier-a.active-trader", "ws.tier-a.futures-trader", "ws.tier-c.sweep-harness")]
    ps = "monitor.activity.position-statement"                  # smoke, outside the change scope: canonical only
    assert ps not in plan.scope_surfaces and ids(plan, ps, "census") == [f"census/{ps}/ws.tier-a.active-trader"]
    assert plan.x_input(CHART) == "declared" and plan.x_input(AT_CELL) == "undeclared"
    assert plan.attribution[AT_CELL] == Attribution("FlexibleGrid", "tos-48120-1")
    assert plan.attribution[CHART].entry_id == "tos-48120-1"


def test_flexgrid_pr_sweeps_the_active_trader_cell_on_ladder_s(inputs):
    plan = pr(inputs)
    expected = [f"sweep/{AT_CELL}/flexgrid/{w}x{h}" for w, h in LADDER_S]
    assert ids(plan, AT_CELL, "sweep") == expected
    assert len(expected) == 12 and f"sweep/{AT_CELL}/flexgrid/480x360" in expected
    assert expected[:3] == [f"sweep/{AT_CELL}/flexgrid/960x720", f"sweep/{AT_CELL}/flexgrid/960x480",
                            f"sweep/{AT_CELL}/flexgrid/960x360"]          # descending; 960x360 is the first height-360 step
    sweep = [cp for cp in plan.planned if cp.checkpoint_id in expected]
    assert {(cp.workspace_id, cp.persona, cp.data_mode) for cp in sweep} == {("ws.tier-c.sweep-harness", "active_trader", "frozen")}
    at_480x360 = next(cp for cp in sweep if cp.checkpoint_id.endswith("/480x360"))
    assert dict(at_480x360.size_step) == {"host": "flexgrid", "w": 480, "h": 360}
    rigs = {it.rigs for it in plan.items if it.kind == "sweep" and it.checkpoints[0].surface_id == AT_CELL}
    assert len(rigs) == 1                                        # one ladder, one session, in order
    on_rig = [cp.checkpoint_id for it in plan.by_rig[rigs.pop()[0]] for cp in it.checkpoints if cp.checkpoint_id in expected]
    assert on_rig == expected
    for tier2 in (CHART, L2_CELL, TS_CELL):                      # tier 2: no sweep in a PR run (2.5.3)
        assert ids(plan, tier2, "sweep") == [] and ids(plan, tier2, "split") == []
    assert ids(plan, "monitor.activity.position-statement", "sweep") == []   # smoke adds census only (choice 1)


def _ladders(plan) -> dict[str, list[str]]:
    """Sweep and split checkpoint ids by ladder (shard key), in the order the plan runs them."""
    out: dict[str, list[str]] = {}
    for it in plan.items:
        if it.kind in ("sweep", "split"):
            out.setdefault(it.shard_key, []).extend(cp.checkpoint_id for cp in it.checkpoints)
    return out


def _contiguous(plan) -> bool:
    keys = [it.shard_key for it in plan.items if it.kind in ("sweep", "split")]
    runs = [k for i, k in enumerate(keys) if i == 0 or keys[i - 1] != k]
    return len(runs) == len(set(runs))


def test_measured_p50s_keep_each_ladder_whole_and_in_ladder_order(inputs):
    """scope-pairs:1, 2.5.2 ("the descending order is mandatory"), SD-04 (`-asc` after the descending pass), 2.5.1
    (one ladder, then the next host or surface): per-step p50s (SD-03) never reorder a ladder's steps or interleave
    ladders; ladders are ordered by their total duration, longest first (SD-14)."""
    default = pr(inputs)
    steps = [cp.checkpoint_id for cp in default.planned if cp.test_kind in ("sweep", "split")]

    def p50(cid: str) -> float:                                  # small cells settle slower
        size = cid.rsplit("/", 1)[1].removesuffix("-asc")
        if "x" not in size:
            return 8.0
        w, h = map(int, size.split("x"))
        return 7 + (960 - w) / 320 + (720 - h) / 360

    measured = pr(inputs, durations={cid: p50(cid) for cid in steps})
    assert _ladders(measured) == _ladders(default) and _contiguous(measured)
    at = _ladders(measured)[f"sweep:{AT_CELL}:flexgrid"]
    assert at == [f"sweep/{AT_CELL}/flexgrid/{w}x{h}" for w, h in LADDER_S]      # descending, 960x720 first
    on_rig = [cp.checkpoint_id for it in measured.items for cp in it.checkpoints if cp.checkpoint_id in at]
    assert on_rig == at
    ladder = _ladders(measured)
    totals = [sum(p50(c) for c in ladder[k]) for k in dict.fromkeys(
        it.shard_key for it in measured.items if it.kind == "sweep" and it.tier == 1 and not it.declared)]
    assert totals == sorted(totals, reverse=True)                # longest ladder first

    rc_default = full(inputs, "rc")
    asc = [cp.checkpoint_id for cp in rc_default.planned if cp.checkpoint_id.endswith("-asc")]
    assert asc
    rc = full(inputs, "rc", durations={cid: 8.5 if cid.endswith("-asc") else 8.0
                                       for cid in [cp.checkpoint_id for cp in rc_default.planned]})
    assert _ladders(rc) == _ladders(rc_default) and _contiguous(rc)
    for ids_ in _ladders(rc).values():
        marks = [c.endswith("-asc") for c in ids_]
        assert marks == sorted(marks)                            # the ascending pass after the descending one


def test_flexgrid_pr_smoke_census_capped_at_40(inputs):
    registry = inputs["registry"]
    smoke = {s.id for s in registry if s.smoke}
    assert len(smoke) == 40
    plan = pr(inputs)
    assert smoke <= {cp.surface_id for cp in plan.planned if cp.test_kind == "census"}
    over = Registry.from_entries(list(registry) + _filler(41, 41))
    with pytest.raises(ScopeConfigError, match="cap is 40"):
        pr(inputs, registry=over)


def test_flexgrid_pr_marks_the_rest_out_of_scope_and_picks_touching_tier1_flows(inputs):
    plan = pr(inputs)
    scope_cuts = [c for c in plan.cuts if c.reason == "scope"]
    assert {c.surface_id for c in scope_cuts} == {"analyze.risk-profile", "sidebar.level2", "tools.thinklog"}
    assert all(c.checkpoint is None for c in scope_cuts)
    flows = {cp.flow_id for cp in plan.planned if cp.test_kind == "flow"}
    assert flows == {"flow.trade.active-trader.ticket"}
    cp2 = next(cp for cp in plan.planned if cp.checkpoint_id.endswith("flow.trade.active-trader.ticket/cp2"))
    assert (cp2.persona, cp2.workspace_id, cp2.step_id) == ("active_trader", "ws.tier-a.active-trader", "cp2")


def test_plan_order_and_ids_follow_2_11_and_the_4_1_grammar(inputs):
    plan = pr(inputs)
    keys = [(it.tier, KIND_ORDER[it.kind]) for it in plan.items]
    assert keys == sorted(keys) and plan.items[0].kind == "flow"
    assert all(is_checkpoint_id(c) for c in plan.planned_ids)
    assert len(plan.planned_ids) == len(set(plan.planned_ids))
    assert ids(plan, "trade.all-products.option-chain", "census") == [
        "census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=delta,strikes=8",
        "census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=delta,strikes=16",
        "census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=theo,strikes=8",
        "census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=theo,strikes=16"]


# -- the no-manifest cases (4.3, 2.11, F5) ---------------------------------------------------

def test_backend_only_pr_without_manifest_is_case_b(inputs):
    flows = inputs["flows"] + [
        replace(inputs["flows"][0], flow_id="flow.trade.fixture-reset", flow_class="fixture_reset"),   # not standard
        replace(inputs["flows"][0], flow_id="flow.trade.tier2-ticket", tier=2),                        # not tier 1
    ]
    plan = pr(inputs, manifest=None, changed_classes={"com.tos.desktop.net.Codec"}, flows=flows)
    assert plan.manifest_state == "no_manifest_nonui" and not plan.intended_enabled
    assert plan.x_input(CHART) == "no_manifest_nonui"
    assert "manifest_missing" not in [f.kind for f in plan.findings]
    # Codec reaches QuoteClient and no surface: a backend class is not an unowned-root gap (2.11)
    assert "dependency_map_gap" not in [f.kind for f in plan.findings]
    assert plan.scope_surfaces == frozenset()
    smoke = {s.id for s in inputs["registry"] if s.smoke}
    assert {cp.surface_id for cp in plan.planned} == smoke   # the smoke census plus flows on smoke surfaces
    assert {cp.test_kind for cp in plan.planned} == {"census", "flow"}   # no sweep: smoke adds census only
    # case (b): the tier-1 standard flows whose surfaces meet the smoke subset still run (2.11)
    assert {cp.flow_id for cp in plan.planned if cp.test_kind == "flow"} == {
        "flow.trade.active-trader.ticket", "flow.trade.order-ticket.futures.single-leg"}
    assert all(it.protected for it in plan.items if it.kind == "flow")   # on smoke surfaces: never budget-cut


def test_case_b_flows_need_a_smoke_surface_and_pass_the_common_filters(inputs):
    plan = pr(inputs, manifest=None, changed_classes={"com.tos.desktop.net.Codec"}, entitlements=("margin",))
    assert {cp.flow_id for cp in plan.planned if cp.test_kind == "flow"} == {"flow.trade.active-trader.ticket"}
    ent = {c.checkpoint.flow_id for c in plan.cuts if c.reason == "entitlement" and c.checkpoint.flow_id}
    assert ent == {"flow.trade.order-ticket.futures.single-leg"}          # requires futures (common_filters)
    off_smoke = replace(inputs["flows"][0], flow_id="flow.analyze.risk", surfaces=("analyze.risk-profile",))
    plan = pr(inputs, manifest=None, changed_classes={"com.tos.desktop.net.Codec"}, flows=[off_smoke])
    assert [cp for cp in plan.planned if cp.test_kind == "flow"] == []


def test_ui_pr_without_manifest_is_case_a(inputs):
    plan = pr(inputs, manifest=None)
    assert plan.manifest_state == "no_manifest_ui" and not plan.intended_enabled
    assert plan.x_input(CHART) == "no_manifest_ui" and plan.declared == frozenset()
    missing = [f for f in plan.findings if f.kind == "manifest_missing"]
    assert len(missing) == 1 and missing[0].tier == 1
    derived = set(missing[0].evidence["raw_values"]["derived_surfaces"].split(","))
    assert GRID_CELLS | {LADDER} <= derived
    assert "dependency_map_gap" not in [f.kind for f in plan.findings]
    assert ids(plan, CHART, "census") == [f"census/{CHART}/ws.tier-a.active-trader",   # undeclared but derived:
                                          f"census/{CHART}/ws.tier-a.swing-trader"]    # a change surface (2.11)
    assert plan.attribution[AT_CELL] == Attribution("FlexibleGrid", None)


def test_missing_manifest_and_missing_index_is_case_a_with_one_gap(inputs):
    plan = pr(inputs, manifest=None, dep_index=None, changed_classes={"com.tos.desktop.net.Codec"})
    assert plan.manifest_state == "no_manifest_ui"
    gaps = [f for f in plan.findings if f.kind == "dependency_map_gap"]
    assert [g.evidence["raw_values"]["reason"] for g in gaps] == ["no_dependency_index"]
    assert "manifest_missing" in [f.kind for f in plan.findings]


def test_manifest_naming_another_candidate_is_treated_as_missing(inputs):
    plan = pr(inputs, candidate_build_id="branch-1998.3.0-0000000")
    assert plan.manifest_state == "no_manifest_ui"


# -- the static impact step (2.10.2) ---------------------------------------------------------

def test_static_impact_edge_missing_from_the_map_is_a_dependency_map_gap(inputs):
    dm = inputs["dep_map"]
    without = DependencyMap(dm.primitives, [e for e in dm.edges if not (e.primitive == "FlexibleGrid" and e.surface == L2_CELL)])
    plan = pr(inputs, dep_map=without, changed_classes=CHANGED | {"com.tos.desktop.util.Strings"})
    gaps = [f for f in plan.findings if f.kind == "dependency_map_gap"]
    raws = [g.evidence["raw_values"] for g in gaps]
    assert raws[0]["reason"] == "missing_edge" and raws[0]["primitive"] == "FlexibleGrid" and raws[0]["surface"] == L2_CELL
    assert raws[0]["evidence"] == ("com.tos.desktop.ui.grid.GridCellHeader -> com.tos.desktop.ui.grid.FlexibleGridCell -> "
                                   "com.tos.desktop.ui.grid.CellContentAdapter -> "
                                   "com.tos.desktop.charts.flexgrid.level2cell.Level2CellView")
    assert gaps[0].primitive == "FlexibleGrid" and gaps[0].tier == 2
    assert raws[1:] == []                         # util.Strings is unowned but reaches no surface: no gap (2.11)
    assert L2_CELL in plan.scope_surfaces and ids(plan, L2_CELL, "census")    # still looked at
    assert plan.proposed_edges == [Edge("FlexibleGrid", L2_CELL, "static_analysis", 1.0, True, CANDIDATE, raws[0]["evidence"])]


def test_unowned_class_reaching_a_surface_names_the_root_of_the_missing_edge(inputs):
    index = DependencyIndex.from_depends_on({**_yaml("dependency-index.yaml"),
                                             "com.tos.desktop.sidebar.level2.Level2Gadget": ["com.tos.desktop.util.Pricing"]})
    plan = pr(inputs, dep_index=index, changed_classes={"com.tos.desktop.util.Pricing"})
    raws = [f.evidence["raw_values"] for f in plan.findings if f.kind == "dependency_map_gap"]
    assert {"reason": "missing_edge", "unowned_root": "com.tos.desktop.util.Pricing", "surface": "sidebar.level2",
            "evidence": "com.tos.desktop.util.Pricing -> com.tos.desktop.sidebar.level2.Level2Gadget"} in raws
    assert {"reason": "unowned_root", "unowned_root": "com.tos.desktop.util.Pricing"} in raws
    assert plan.proposed_edges == []                              # no primitive to draw an edge from


def test_reverse_closure_is_bounded_at_depth_6():
    chain = {f"c{i}": [f"c{i - 1}"] for i in range(1, 9)}        # c1 depends on c0, c2 on c1, ...
    reach = DependencyIndex.from_depends_on(chain).reach({"c0"})["c0"]
    assert "c6" in reach and "c7" not in reach
    assert reach["c3"] == ("c0", "c1", "c2", "c3")


def test_class_matching_and_classes_in():
    assert class_matches("com.tos.desktop.ui.grid.sub.Cell$Inner", "com.tos.desktop.ui.grid.**")
    assert class_matches("com.tos.desktop.trade.activetrader.LadderPanel", "com.tos.desktop.trade.activetrader")
    assert not class_matches("com.tos.desktop.trade.activetraderx.Panel", "com.tos.desktop.trade.activetrader")
    assert not class_matches("com.tos.desktop.ui.gridx.Cell", "com.tos.desktop.ui.grid.*")
    assert classes_in(["desktop/ui/grid/src/main/java/com/tos/desktop/ui/grid/GridCellLayout.java", "README.md"]) == {
        "com.tos.desktop.ui.grid.GridCellLayout"}


# -- merge and RC scopes ----------------------------------------------------------------------

def test_merge_scope(inputs):
    plan = full(inputs, "merge", map_scan_failed=True)
    assert plan.budget_s == capture_time("merge") == 3 * 3600 - 10 * 60          # before session startups (SD-03)
    for rig, b in plan.rig_budget_s.items():                                     # each rig's own startups come off
        assert b == capture_time("merge", sum(s.sessions for s in plan.shards if rig in s.rigs))
    tier_a = ["ws.tier-a.active-trader", "ws.tier-a.beginner", "ws.tier-a.futures-trader", "ws.tier-a.options-trader",
              "ws.tier-a.swing-trader"]
    assert ids(plan, "dialog.order-confirmation", "census") == [f"census/dialog.order-confirmation/{w}" for w in tier_a]
    assert not [c for c in plan.planned_ids if "/ws.tier-b." in c]
    scope = {c.checkpoint.checkpoint_id for c in plan.cuts if c.reason == "scope"}
    assert "census/analyze.risk-profile/ws.tier-a.active-trader" in scope       # not hosted there
    assert len(ids(plan, "tools.thinklog", "census")) == 5                       # a sub-tab is global
    assert len(ids(plan, CHART, "sweep")) == 12                  # tier 2: Ladder S in merge
    assert len(ids(plan, "analyze.risk-profile", "split")) == 3  # short set
    assert len(ids(plan, LADDER, "sweep")) == 24 and ids(plan, LADDER, "split") == [   # flexgrid, detached (SD-05)
        f"split/{LADDER}/side-panel/0.15", f"split/{LADDER}/side-panel/0.50", f"split/{LADDER}/side-panel/0.85"]
    assert ids(plan, "tools.thinklog", "sweep") == []            # tier 3: RC only
    assert {cp.flow_id for cp in plan.planned if cp.test_kind == "flow"} == {
        "flow.trade.active-trader.ticket", "flow.trade.order-ticket.futures.single-leg"}
    assert [f.evidence["raw_values"]["reason"] for f in plan.findings] == ["scan_failed"]
    assert plan.x_input(CHART) == "declared" and plan.x_input(AT_CELL) == "undeclared"


def test_rc_scope(inputs):
    soak = SoakPlan(Rig("rig-5", "fp-9e1f03"), "ws.tier-b.power-1", LADDER)
    plan = full(inputs, "rc", soak=soak, explorer_rig="rig-6", corpus_sizes={CHART: [(512, 384)]})
    assert plan.budget_s == 8 * 3600 - 10 * 60 - 90 * 60 and plan.explorer_rig == "rig-6"
    assert f"census/{CHART}/ws.tier-b.power-1" in plan.planned_ids
    chain = ids(plan, "trade.all-products.option-chain", "census")
    assert "census/trade.all-products.option-chain/ws.tier-b.power-1" in chain and len(chain) == 5
    sweep = ids(plan, CHART, "sweep")
    assert len(sweep) == 36 + 4 + 1 + 6                          # grid, corners, corpus, then the ascending pass
    assert sweep[0].endswith("/960x720") and sweep[35].endswith("/320x300")
    assert [s.rsplit("/", 1)[1] for s in sweep[36:41]] == ["240x160", "240x1000", "1904x160", "1904x1000", "512x384"]
    # SD-04: Ladder F's ascending pass has its own `-asc` ids, planned after the descending pass in ladder order
    assert [s.rsplit("/", 1)[1] for s in sweep[41:]] == ["320x480-asc", "400x480-asc", "480x480-asc", "640x480-asc",
                                                          "800x480-asc", "960x480-asc"]
    assert all(is_checkpoint_id(s) for s in sweep)
    assert [s.rsplit("/", 1)[1] for s in ids(plan, LADDER, "split")] == ["0.15", "0.30", "0.50", "0.70", "0.85"]
    assert [s.rsplit("/", 1)[1] for s in ids(plan, "analyze.risk-profile", "split")] == [
        "0.00", "0.15", "0.30", "0.50", "0.70", "0.85", "1.00"]  # collapsible
    assert ids(plan, "tools.thinklog", "sweep") == [] and ids(plan, "tools.thinklog", "split") == []   # a tab host (SD-05)
    assert {cp.flow_id for cp in plan.planned if cp.test_kind == "flow" and cp.flow_id != "soak"} == {
        f.flow_id for f in inputs["flows"]}
    soak_ids = [f"flow/{LADDER}/soak/c{n:02d}" for n in range(1, 25)]
    assert [cp.checkpoint_id for it in plan.by_rig["rig-5"] for cp in it.checkpoints] == soak_ids
    assert "rig-5" not in plan.projected_overrun_s


def test_nightly_and_calibration_compute_no_scope(inputs):
    for run_type in ("nightly", "calibration"):
        with pytest.raises(ValueError, match="compute no scope"):
            full(inputs, run_type)


# -- filters, scheduling, budget (2.11 common_filters and order_and_cut, 3.1) -----------------

def test_common_filters_cut_entitlement_then_quarantine(inputs):
    q = ["census/settings.order-defaults/ws.tier-a.active-trader", "flow.trade.active-trader.ticket"]
    plan = pr(inputs, entitlements=("margin",), quarantine=q)
    ent = {c.checkpoint.checkpoint_id for c in plan.cuts if c.reason == "entitlement"}
    assert "census/trade.order-entry.futures/ws.tier-a.futures-trader" in ent
    assert sum(cid.startswith("census/trade.all-products.option-chain/") for cid in ent) == 4
    quar = {c.checkpoint.checkpoint_id for c in plan.cuts if c.reason == "quarantine"}
    assert quar == {q[0], "flow/trade.active-trader.ladder/flow.trade.active-trader.ticket/cp1",
                    "flow/dialog.order-confirmation/flow.trade.active-trader.ticket/cp2"}
    assert not (ent | quar) & set(plan.planned_ids)
    out = next(c for c in plan.cuts if c.reason == "quarantine").outcome(RUN)
    assert (out.outcome, out.reason) == ("not_run", "quarantine")


def test_frozen_base_reuse_halves_the_rig_cost(inputs):
    sweep = [f"sweep/{AT_CELL}/flexgrid/{w}x{h}" for w, h in LADDER_S]
    stored = {cid: StoredBase("run-merge-0755", "fp-9e1f03", "E-2026-11-09") for cid in sweep}
    stored[sweep[0]] = StoredBase("run-merge-0755", "fp-9e1f03", "E-2026-11-08")              # stale fixture epoch
    stored["census/dialog.order-confirmation/ws.tier-a.active-trader"] = StoredBase("run-merge-0755", "fp-9e1f03", "E-2026-11-09")
    plan = pr(inputs, base_reuse=BaseReuse("fp-9e1f03", "E-2026-11-09", stored))
    cps = {cp.checkpoint_id: cp for cp in plan.planned}
    assert all(cps[c].base_source_run_id == "run-merge-0755" for c in sweep[1:])
    assert cps[sweep[0]].base_source_run_id is None
    assert cps["census/dialog.order-confirmation/ws.tier-a.active-trader"].base_source_run_id is None   # masked_live: never
    items = {it.item_id: it for it in plan.items}
    assert items[sweep[1]].rig_cost_s == items[sweep[1]].estimated_s
    assert items[sweep[0]].rig_cost_s == 2 * items[sweep[0]].estimated_s


def test_masked_live_needs_a_lockstep_pair(inputs):
    plan = pr(inputs, rigs=[Rig("rig-1", "fp-a"), Rig("rig-2", "fp-b")])
    live = {c.checkpoint.checkpoint_id for c in plan.cuts if c.reason == "budget"}
    assert "census/dialog.order-confirmation/ws.tier-a.active-trader" in live
    assert all(c.checkpoint.data_mode == "masked_live" for c in plan.cuts if c.reason == "budget")
    assert not [cp for cp in plan.planned if cp.data_mode == "masked_live"]
    paired = pr(inputs, rigs=[Rig("rig-1", "fp-a"), Rig("rig-2", "fp-a"), Rig("rig-3", "fp-a")])
    assert {it.rigs for it in paired.items if it.data_mode == "masked_live"} == {("rig-1", "rig-2")}
    assert all(len(it.rigs) == 1 for it in paired.items if it.data_mode == "frozen")


def test_budget_cuts_take_the_highest_tier_first_and_never_declared_or_smoke(inputs):
    plan = full(inputs, "merge", rigs=[Rig("rig-1")], budget_s=600)
    lockstep = [c for c in plan.cuts if c.reason == "budget" and c.checkpoint.data_mode == "masked_live"]
    assert lockstep                                               # one rig: no lockstep pair (3.1)
    cuts = [c for c in plan.cuts if c.reason == "budget" and c.checkpoint.data_mode == "frozen"]
    smoke = {s.id for s in inputs["registry"] if s.smoke}
    assert cuts and not {c.surface_id for c in cuts} & (smoke | plan.declared)
    cut_tiers = [c.tier for c in cuts]
    assert cut_tiers == sorted(cut_tiers, reverse=True) and cut_tiers[0] == 3
    assert all(it.protected for it in plan.items)                 # protected work alone still overruns
    assert plan.projected_overrun_s["rig-1"] > 0


def test_pr_overrun_proceeds_and_aborts_budget_at_the_deadline(inputs):
    plan = pr(inputs, budget_s=120)
    assert plan.projected_overrun_s
    assert {c.surface_id for c in plan.cuts if c.reason == "budget"} == {L2_CELL, TS_CELL}
    assert set(ids(plan, CHART, "census")) == {f"census/{CHART}/ws.tier-a.active-trader", f"census/{CHART}/ws.tier-a.swing-trader"}
    outs, status = plan.deadline_outcomes(plan.planned_ids[:5])
    assert status == ("aborted_budget", "wall_clock")
    assert len(outs) == len(plan.planned_ids) - 5
    assert {(o.outcome, o.reason) for o in outs} == {("not_run", "budget")}
    assert plan.deadline_outcomes(plan.planned_ids) == ([], None)
    safety = plan.unreached_outcomes([], "aborted_safety", "kill_switch", gate="running")
    assert {(o.outcome, o.gate) for o in safety} == {("env_error", "running")}
    harness = plan.unreached_outcomes([], "aborted_harness", "hook_lost")
    assert {(o.outcome, o.reason) for o in harness} == {("harness_error", "hook_lost")}


# -- merge run: missed_in_pr_run (1.8) ---------------------------------------------------------

def test_merge_run_records_what_pr_runs_missed(inputs):
    pr_plan = pr(inputs)
    summary = pr_plan.pr_summary(reported_surfaces={CHART, AT_CELL})
    assert isinstance(summary, PrRunSummary) and summary.changed_classes == frozenset(CHANGED)
    other = PrRunSummary("run-2026-11-10-pr-0002", frozenset(), frozenset(), frozenset(), frozenset(),
                         frozenset({"ChartEngine"}))
    merge_index = DependencyIndex.from_depends_on({**_yaml("dependency-index.yaml"),
                                                   "com.tos.desktop.sidebar.level2.Level2Gadget": ["com.tos.desktop.ui.grid.FlexibleGridCell"]})

    def finding(n: int, kind: str, cid: str) -> Finding:
        return Finding(f"fnd-run-m-{n:06d}", "run-m", kind, "merge finding", "run-m", checkpoint_id=cid, tier=2)

    found = [finding(1, "collateral", f"census/{L2_CELL}/ws.tier-a.active-trader"),
             finding(2, "collateral", "census/sidebar.level2/ws.tier-a.active-trader"),
             finding(3, "invariant_violation", "census/analyze.risk-profile/ws.tier-a.options-trader"),
             finding(4, "intended", f"census/{CHART}/ws.tier-a.swing-trader")]
    out = merge_run_findings("run-m", found, [summary, other], inputs["registry"], merge_index)
    got = [(f.kind, f.checkpoint_id.split("/")[1], f.evidence["raw_values"]["pr_run_id"], f.evidence["raw_values"].get("detail"))
           for f in out]
    assert got == [
        ("missed_in_pr_run", L2_CELL, RUN, None),                              # in PR scope, not reported
        ("missed_in_pr_run", "sidebar.level2", RUN, "static_impact"),          # not_run (scope); the index reaches it
        ("dependency_map_gap", "analyze.risk-profile", "run-2026-11-10-pr-0002", None),   # consumes a touched primitive
    ]
    assert out[0].description.startswith("missed in PR run") and out[1].description.startswith("missed by static impact")
    assert all(f.finding_id.startswith("fnd-run-m-merge-") for f in out)


def test_surface_from_dict_reads_the_4_5_fields():
    s = Surface.from_dict(_yaml("registry.yaml")[4])
    assert s.id == LADDER and s.smoke and s.tier == 1 and s.hosts == ("tab", "flexgrid", "detached")
    assert s.splitters[0].steps(full=False) == (0.15, 0.50, 0.85)
    assert replace(s.splitters[0], fractions=(0.15, 0.30)).steps(full=True) == (0.15, 0.30)


def test_merge_and_rc_never_lack_a_manifest(inputs):
    empty = Manifest.union([], candidate="main-1998.3.0-9a0b1c2", base="main-1998.3.0-2c91ae4", author="orchestrator")
    assert empty.entries == () and empty.empty_reason == "no PR manifests in range" and empty.source == "union"
    assert full(inputs, "merge", manifest=empty).declared == frozenset()
    with pytest.raises(ScopeConfigError, match="never lack a manifest"):
        full(inputs, "rc", manifest=None)


# -- lint and scope enumerate the same census ids (review lint-oracle:3) ---------------------------

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"


def _example_scope(run_type: str, **kw):
    repo = Repo.load(EXAMPLE)
    registry = Registry.from_entries(d.data for d in repo.of("surface"))
    union = Manifest.union([], candidate="main-1998.3.0-9a0b1c2", base="main-1998.3.0-2c91ae4", author="orchestrator")
    args = dict(run_id=f"run-2026-11-10-{run_type}-0001", workspaces=[Workspace.from_dict(d.data) for d in repo.of("workspace")],
                flows=[Flow.from_dict(d.data) for d in repo.of("flow")], entitlements=("margin", "ira"), rigs=RIGS)
    args.update(kw)
    plan = compute_scope(run_type, union, DependencyMap.from_dict(repo.one("dependency_map").data), registry, **args)
    return plan, _Linter(repo, 0, date(2026, 11, 10), None)


@pytest.mark.parametrize("run_type", ["merge", "rc"])
def test_merge_and_rc_census_ids_are_the_ids_the_lint_defines(run_type):
    """A merge or RC census visit of a hosting workspace that `census.workspaces` does not list (2.11 `s hosted_by w`)
    is a checkpoint the lint defines too, so L-XREF-03 asks its masked_live visits for a rule or mask region and
    L-RGN-01 accepts their regions files (review lint-oracle:3)."""
    plan, linter = _example_scope(run_type)
    census = [cp for cp in plan.planned if cp.test_kind == "census"]
    assert "census/monitor.activity.position-statement/ws.tier-a.futures-trader" in {cp.checkpoint_id for cp in census}
    assert [cp.checkpoint_id for cp in census if cp.checkpoint_id not in linter.defined] == []
    for cp in census:
        if cp.data_mode == "masked_live":
            assert linter._has_rule_or_mask(cp.checkpoint_id), cp.checkpoint_id


def test_a_corpus_size_is_swept_only_on_the_host_it_was_measured_on():
    """corpus/measured_sizes.yaml names a host per size (4.16); the lint defines the step on that host only, and so
    does the scope (review lint-oracle:3, SD-06)."""
    plan, linter = _example_scope("rc", corpus_sizes={LADDER: [SweepStep(700, 500, host="detached")]})
    planned = set(ids(plan, LADDER, "sweep"))
    assert f"sweep/{LADDER}/detached/700x500" in planned and f"sweep/{LADDER}/flexgrid/700x500" not in planned
    assert f"sweep/{LADDER}/flexgrid/320x300" in planned                      # the ladder itself is on every host


# -- budget B (2.11, 11.2, SD-03) ------------------------------------------------------------------

def test_capture_time_is_the_wall_budget_less_the_report_margin_and_session_startups():
    assert capture_time("pr") == 45 * 60 - 600
    assert capture_time("pr", 2) == 45 * 60 - 600 - 2 * 240                      # a frozen shard without reuse: 8 min
    assert capture_time("merge", [True, False]) == 3 * 3600 - 600 - 480 - 240    # one session with a fixture reset
    with pytest.raises(ValueError):
        capture_time("calibration")


def test_each_rig_cuts_against_its_own_capture_time_and_its_load_counts_startups(inputs):
    plan = pr(inputs)
    assert plan.budget_s == capture_time("pr")
    for rig in plan.by_rig:
        sessions = sum(s.sessions for s in plan.shards if rig in s.rigs)
        assert plan.rig_budget_s[rig] == capture_time("pr", sessions)
        work = plan.rig_load_s[rig] - sessions * SESSION_STARTUP_S
        assert work <= plan.rig_budget_s[rig]
    tight = pr(inputs, budget_s=60)                                               # B given: every rig gets it
    assert set(tight.rig_budget_s.values()) == {60.0} and tight.projected_overrun_s


def test_flow_durations_default_by_tier(inputs):
    flows = [replace(f, tier=t, flow_id=f"{f.flow_id}.t{t}") for f in inputs["flows"][:1] for t in (1, 2, 3)]
    plan = full(inputs, "rc", flows=flows)
    est = {it.item_id[-2:]: it.estimated_s for it in plan.items if it.kind == "flow"}
    assert est == {"t1": 90.0, "t2": 75.0, "t3": 60.0}                           # 11.1 PA-06
    timed = full(inputs, "rc", flows=flows, durations={flows[1].flow_id: 33.0})
    assert {it.estimated_s for it in timed.items if it.item_id == flows[1].flow_id} == {33.0}


# -- sweeps: override, streaming, charts, corpus sizes, RC tier 3 (2.5.2, 2.5.3, SD-04, SD-06, SD-17) -------------

LADDERS = load_ladders(yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "example" / "registry"
                                       / "ladders.yaml").read_text()))


def _with(inputs: dict, sid: str, **kw) -> Registry:
    return Registry.from_entries([replace(s, **kw) if s.id == sid else s for s in inputs["registry"]])


def test_surface_from_dict_reads_the_override_streaming_and_parent_fields():
    s = Surface.from_dict({"id": "x.y", "tier": 2, "size_ladder": "ladder-s", "ladder_reason": "clamps below 400 px",
                           "streaming": True, "parent": "x"})
    assert (s.size_ladder, s.ladder_reason, s.streaming, s.parent) == ("ladder-s", "clamps below 400 px", True, "x")


def test_size_ladder_override_replaces_the_tier_ladder(inputs):
    plan = full(inputs, "rc", registry=_with(inputs, CHART, size_ladder="ladder-s", ladder_reason="r"), ladders=LADDERS)
    assert ids(plan, CHART, "sweep") == [f"sweep/{CHART}/flexgrid/{w}x{h}" for w, h in LADDER_S]   # Ladder S in RC
    narrow = {**LADDERS, "ladder-narrow": LadderDef("ladder-narrow", "S", ((640, 480, "grid"), ("MAXW", 360, "grid")))}
    plan = full(inputs, "merge", registry=_with(inputs, CHART, size_ladder="ladder-narrow"), ladders=narrow)
    assert ids(plan, CHART, "sweep") == [f"sweep/{CHART}/flexgrid/640x480", f"sweep/{CHART}/flexgrid/1904x360"]
    tier3 = _with(inputs, CHART, size_ladder="ladder-narrow", tier=3)
    assert ids(full(inputs, "merge", registry=tier3, ladders=narrow), CHART, "sweep") == []   # 2.5.3 still decides when
    with pytest.raises(ScopeConfigError, match="L-SRF-03"):
        full(inputs, "merge", registry=_with(inputs, CHART, size_ladder="ladder-gone"), ladders=LADDERS)


def test_ladder_f_from_the_ladders_file_takes_the_corpus_sizes_before_the_ascending_pass():
    lad = LADDERS["ladder-f"]
    steps = lad.sweep_steps((1904, 1000), (SweepStep(512, 384, host="flexgrid"),))
    sizes = [(st.w, st.h, st.ascending, st.host) for st in steps]
    assert sizes[36:41] == [(240, 160, False, None), (240, 1000, False, None), (1904, 160, False, None),
                            (1904, 1000, False, None), (512, 384, False, "flexgrid")]
    assert sizes[41:] == [(w, 480, True, None) for w in (320, 400, 480, 640, 800, 960)]


def test_a_streaming_surface_plans_no_sweep_and_its_sweeps_are_not_run_scope(inputs):
    plan = full(inputs, "merge", registry=_with(inputs, CHART, streaming=True))
    assert ids(plan, CHART, "sweep") == [] and ids(plan, CHART, "census")       # still visited by the census
    cut = sorted(c.checkpoint.checkpoint_id for c in plan.cuts if c.surface_id == CHART and c.reason == "scope"
                 and c.checkpoint and c.checkpoint.test_kind == "sweep")
    assert cut == sorted(f"sweep/{CHART}/flexgrid/{w}x{h}" for w, h in LADDER_S)
    assert plan.chart_sweeps_not_run == 0


def test_chart_sweeps_run_frozen_and_are_not_run_scope_without_frozen_data(inputs):
    live = _with(inputs, CHART, data_mode="masked_live")
    plan = full(inputs, "merge", registry=live)
    assert {cp.data_mode for cp in plan.planned if cp.surface_id == CHART and cp.test_kind == "sweep"} == {"frozen"}
    assert {cp.data_mode for cp in plan.planned if cp.surface_id == CHART and cp.test_kind == "census"} == {"masked_live"}
    branch_b = full(inputs, "merge", registry=live, frozen_available=False)
    assert ids(branch_b, CHART, "sweep") == [] and branch_b.chart_sweeps_not_run == 12
    assert len([c for c in branch_b.cuts if c.reason == "scope" and c.checkpoint
                and c.checkpoint.checkpoint_id.startswith(f"sweep/{CHART}/")]) == 12
    assert ids(branch_b, AT_CELL, "sweep")                                       # not a chart: still swept
    assert any(line.startswith("chart sweeps not run (scope): 12 checkpoints") for line in branch_b.health_lines())


def test_ladder_f_corpus_sizes_missing_print_in_run_health(inputs):
    sizes = measured_sizes({"measured_on_run": "run-x", "sizes": [
        {"surface": CHART, "workspace": "ws.tier-a.swing-trader", "host": "flexgrid", "w": 512, "h": 384}]})
    plan = full(inputs, "rc", corpus_sizes=sizes, max_client=work_area({"configs": [{"id": "c", "work_area": {"w": 1904, "h": 1000}}]}))
    assert f"sweep/{CHART}/flexgrid/512x384" in ids(plan, CHART, "sweep")
    assert CHART not in plan.corpus_sizes_missing and {AT_CELL, L2_CELL, TS_CELL, LADDER} <= set(plan.corpus_sizes_missing)
    assert "tools.thinklog" not in plan.corpus_sizes_missing                       # tier 3: Ladder S in RC
    assert any("corpus/measured_sizes.yaml lists no size for" in line for line in plan.health_lines())
    assert work_area(None) is None


def test_rc_sweeps_tier_3_splitters_on_the_short_set(inputs):
    reg = _with(inputs, "tools.thinklog", splitters=(Splitter("log-split", "tab"),))
    assert [s.rsplit("/", 1)[1] for s in ids(full(inputs, "rc", registry=reg), "tools.thinklog", "split")] == [
        "0.15", "0.50", "0.85"]                                                  # SD-04
    assert ids(full(inputs, "merge", registry=reg), "tools.thinklog", "split") == []   # tier 3: RC only


def test_sweeps_count_toward_the_primary_persona(inputs):
    harness = [replace(w, persona="beginner") if w.tier == "C" else w for w in inputs["workspaces"]]
    plan = pr(inputs, workspaces=harness)
    assert {cp.persona for cp in plan.planned if cp.test_kind == "sweep" and cp.surface_id == AT_CELL} == {"active_trader"}


# -- order and shards (2.11 order_and_cut, 11.3, SD-14) --------------------------------------------

def test_declared_surfaces_come_first_within_a_tier(inputs):
    plan = pr(inputs)
    keys = [(it.tier, not it.declared, KIND_ORDER[it.kind]) for it in plan.items]
    assert keys == sorted(keys)
    tier2 = [it.checkpoints[0].surface_id for it in plan.items if it.tier == 2]
    assert tier2[0] == CHART and all(it.declared for it in plan.items if it.checkpoints[0].surface_id == CHART)
    for group in {(it.tier, it.declared) for it in plan.items}:              # census items grouped by workspace
        seq = [it.checkpoints[0].workspace_id for it in plan.items if it.kind == "census" and (it.tier, it.declared) == group]
        runs = [w for i, w in enumerate(seq) if i == 0 or seq[i - 1] != w]
        assert len(runs) == len(set(runs)), seq


def test_the_census_of_one_tab_and_data_mode_lands_in_one_shard(inputs):
    plan = full(inputs, "merge")
    by_key: dict[str, set] = {}
    for it in plan.items:
        if it.kind == "census":
            by_key.setdefault(it.shard_key, set()).add(it.rigs)
    assert by_key and all(len(r) == 1 for r in by_key.values())
    assert {"census:charts:frozen", "census:trade:frozen", "census:trade:masked_live", "census:monitor:masked_live"} <= set(by_key)
    assert inputs["registry"].tab("dialog.order-confirmation") == "dialog"
    nested = Registry.from_entries([Surface("trade", 1, kind="tab"), Surface("trade.x", 1, parent="trade"),
                                    Surface("dialog.y", 1, kind="dialog", parent="trade.x")])
    assert nested.tab("dialog.y") == "trade"                                     # a dialog joins its tab's shard


def test_flows_sharing_a_state_scope_or_a_fixture_share_a_shard(inputs):
    f1, f2, f3 = inputs["flows"]
    plan = full(inputs, "rc")
    key = {it.item_id: it.shard_key for it in plan.items if it.kind == "flow"}
    assert key[f1.flow_id] == key[f2.flow_id] != key[f3.flow_id]                 # fx.rig-01 pins two; fx.rig-02 alone
    scoped = [replace(f1, state_scope="watchlists"), f2, replace(f3, state_scope="watchlists")]
    plan = full(inputs, "rc", flows=scoped)
    items = {it.item_id: it for it in plan.items if it.kind == "flow"}
    assert len({it.shard_key for it in items.values()}) == 1
    frozen = {it.rigs for it in items.values() if it.data_mode == "frozen"}
    assert len(frozen) <= 1


def test_a_flow_checkpoint_step_overrides_the_surface_tier(inputs):
    doc = _yaml("flows.yaml")[0]
    doc["steps"] = [dict(s, tier=2) if s.get("type") == "checkpoint" else s for s in doc["steps"]]
    plan = full(inputs, "rc", flows=[Flow.from_dict(doc)])
    assert {cp.tier for cp in plan.planned if cp.flow_id == doc["flow_id"]} == {2}


# -- the Nightly A/A pass and Calibration runs (10.6, 11.2, 3.9) -----------------------------------

NIGHT_RIGS = [Rig("rig-4", "fp-9e1f03"), Rig("rig-5", "fp-9e1f03")]


def nightly(inputs: dict, day: date, **kw):
    return plan_nightly(inputs["registry"], day, NIGHT_RIGS, workspaces=inputs["workspaces"], flows=inputs["flows"],
                        entitlements=E, **kw)


def test_nightly_covers_tier_1_every_night_and_the_rest_in_five_nights(inputs):
    days = [date(2026, 11, 10 + i) for i in range(5)]
    plans = [nightly(inputs, d) for d in days]
    tier1 = [{cp.checkpoint_id for cp in p.aa_checkpoints if cp.tier == 1} for p in plans]
    assert tier1[0] and all(t == tier1[0] for t in tier1)
    rest = [{cp.checkpoint_id for cp in p.aa_checkpoints if cp.tier != 1} for p in plans]
    assert sorted(p.rotation_slice for p in plans) == [0, 1, 2, 3, 4]
    assert all(not (a & b) for i, a in enumerate(rest) for b in rest[i + 1:])    # one fifth per night
    week = nightly(inputs, days[0])
    every = {cp.checkpoint_id for p in plans for cp in p.aa_checkpoints}
    universe = full(inputs, "merge")
    assert {cp.checkpoint_id for cp in universe.planned if cp.test_kind != "flow"} <= every
    assert nightly(inputs, days[0] + timedelta(days=5)).rotation_slice == week.rotation_slice


def test_nightly_captures_each_checkpoint_twice_in_one_session_on_one_rig(inputs):
    plan = nightly(inputs, date(2026, 11, 10))
    assert all(len(it.rigs) == 1 for it in plan.items)
    assert {s.data_mode for s in plan.shards} == {"any"} and all(s.sessions == 1 for s in plan.shards)
    rig5 = sum(s for _, s in NIGHTLY_RIG5_WORK_S)
    assert plan.canary_rig == "rig-5" and rig5 == 50 * 60
    per_side = {r: sum(it.estimated_s for it in plan.by_rig[r]) for r in plan.by_rig}
    assert plan.rig_load_s["rig-4"] >= 2 * per_side["rig-4"] + SESSION_STARTUP_S
    assert plan.rig_load_s["rig-5"] >= 2 * per_side["rig-5"] + rig5
    assert plan.fits
    weekend = nightly(inputs, date(2026, 11, 13), aa_pass=False)                 # the A/A pass pauses (1.6)
    assert weekend.items == [] and weekend.rig_load_s["rig-5"] == rig5 + SESSION_STARTUP_S


def _items(n: int, per_side: float, tier: int = 1) -> list[PlanItem]:
    out = []
    for i in range(n):
        cid = f"census/area.s{i:03d}/ws.tier-a.active-trader"
        cp = PlannedCheckpoint(cid, f"area.s{i:03d}", "census", tier, "ws.tier-a.active-trader", "active_trader", "frozen")
        out.append(PlanItem(cid, "census", tier, [cp], "frozen", f"census:k{i % 40}:frozen", False, per_side, i))
    return out


def test_weekend_calibration_keeps_every_pass_of_a_checkpoint_on_one_rig_and_fits_before_saturday_11_10():
    rigs = [Rig(f"rig-{i}", "fp") for i in range(1, 5)]
    items = _items(200, 180.0)                                                   # 600 min per side, measured
    plan = plan_calibration(items, rigs, "weekend", day=date(2026, 11, 13))
    assert plan.passes == (5, 1) and all(len(it.rigs) == 1 for it in plan.items)
    assert all(it.rigs == () for it in items)                                    # the scope's items are not touched
    assert max(plan.ends(r.id)[0] for r in rigs) <= datetime(2026, 11, 14, 11, 10)
    assert plan.starts == (datetime(2026, 11, 13, 22, 15), datetime(2026, 11, 14, 22, 15))
    assert (plan.start_deadline, plan.deadline) == (datetime(2026, 11, 14, 2, 0), datetime(2026, 11, 15, 17, 0))
    for r in rigs:
        night1, night2 = plan.night_load_s[r.id]
        per_side = sum(it.estimated_s for it in plan.by_rig[r.id])
        assert night1 == 5 * per_side + SESSION_STARTUP_S and night2 == per_side + SESSION_STARTUP_S
    assert plan.fits and plan.uncalibrated == ()
    with pytest.raises(ValueError, match="Friday"):
        plan_calibration(items, rigs, "weekend", day=date(2026, 11, 12))


def test_emergency_calibration_is_tier_1_only_on_rigs_1_to_3_by_06_00(inputs):
    rc = full(inputs, "rc")
    plan = plan_calibration(rc, [Rig(f"rig-{i}", "fp-9e1f03") for i in range(1, 5)], "emergency", day=date(2026, 11, 10))
    assert plan.passes == (5,) and set(plan.by_rig) == {"rig-1", "rig-2", "rig-3"}
    assert all(any(cp.tier == 1 for cp in it.checkpoints) for it in plan.items)
    left = set(plan.uncalibrated)
    assert left and left == {cp.checkpoint_id for it in rc.items if not any(c.tier == 1 for c in it.checkpoints)
                             for cp in it.checkpoints}                            # tiers 2 and 3 stay uncalibrated
    assert (plan.deadline, plan.start_deadline) == (datetime(2026, 11, 11, 6, 0), datetime(2026, 11, 11, 1, 0))
    assert plan.fits
    big = plan_calibration(_items(300, 300.0), [Rig(f"rig-{i}", "fp") for i in range(1, 4)], "emergency",
                           day=date(2026, 11, 10))
    assert not big.fits                                                          # 25 h of passes cannot end by 06:00
