"""The one checkpoint-id enumeration (sentinel.checkpoints): the 4.1 grammar, the census dimension rule of 2.4, the
sweep hosts of 2.5.1, the split fractions of 2.5.2, and agreement with the lint and the scope computation."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from sentinel import checkpoints as C
from sentinel.lint import Repo, lint_repo
from sentinel.scope import DependencyMap, Manifest, Registry, Rig, Workspace, compute_scope
from sentinel.taxonomy import is_checkpoint_id

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
CHAIN = "trade.all-products.option-chain"
DIMS = {"layout": ["delta", "theo"], "strikes": [8, 16]}
WS_A, WS_C = "ws.tier-a.options-trader", "ws.tier-c.sweep-harness"


def test_census_dimensions_repeat_only_tier1_in_tier_a():
    got = C.census_ids(CHAIN, WS_A, surface_tier=1, workspace_tier="A", dimensions=DIMS)
    assert got == tuple(f"census/{CHAIN}/{WS_A}/layout={lay},strikes={st}" for lay in ("delta", "theo") for st in (8, 16))
    assert all(is_checkpoint_id(c) for c in got)
    assert C.census_ids(CHAIN, WS_C, surface_tier=1, workspace_tier="C", dimensions=DIMS) == (f"census/{CHAIN}/{WS_C}",)
    assert C.census_ids(CHAIN, WS_A, surface_tier=2, workspace_tier="A", dimensions=DIMS) == (f"census/{CHAIN}/{WS_A}",)
    assert C.census_ids(CHAIN, WS_A, surface_tier=1, workspace_tier="A", dimensions={}) == (f"census/{CHAIN}/{WS_A}",)


def test_sweep_ids_only_on_sized_hosts_with_asc_suffix():
    assert C.sweep_hosts(["tab", "sidebar", "flexgrid", "detached", "dialog"]) == ("flexgrid", "detached", "dialog")
    assert C.sweep_id("trade.active-trader.ladder", "flexgrid", 480, 480, ascending=True) == \
        "sweep/trade.active-trader.ladder/flexgrid/480x480-asc"
    assert is_checkpoint_id(C.sweep_id("trade.active-trader.ladder", "flexgrid", 480, 480, ascending=True))


def test_split_fractions_follow_collapsible_and_narrowing():
    assert C.split_fractions(None, False, full=True) == (0.15, 0.30, 0.50, 0.70, 0.85)
    assert C.split_fractions(None, True, full=True) == C.FULL_FRACTIONS
    assert C.split_fractions(None, True, full=False) == C.SHORT_FRACTIONS
    assert C.split_fractions([0.15, 0.85, 1.0], False, full=True) == (0.15, 0.85)
    assert C.split_id("trade.active-trader.ladder", "side-panel", 0.5) == "split/trade.active-trader.ladder/side-panel/0.50"


def test_surface_checkpoints_kinds_and_order():
    surface = {"id": "trade.active-trader.ladder", "tier": 1, "resizable": True, "hosts": ["tab", "flexgrid"],
               "splitters": [{"id": "side-panel", "host": "tab", "fractions": [0.15, 0.5]}],
               "census": {"workspaces": ["ws.tier-a.active-trader"]}}
    steps = [C.SweepStep(480, 360), C.SweepStep(480, 480, ascending=True), C.SweepStep(640, 480, host="detached")]
    got = C.surface_checkpoints(surface, {"ws.tier-a.active-trader": {"tier": "A"}}, steps)
    assert got == [
        ("census/trade.active-trader.ladder/ws.tier-a.active-trader", "census"),
        ("sweep/trade.active-trader.ladder/flexgrid/480x360", "sweep"),
        ("sweep/trade.active-trader.ladder/flexgrid/480x480-asc", "sweep"),
        ("split/trade.active-trader.ladder/side-panel/0.15", "split"),
        ("split/trade.active-trader.ladder/side-panel/0.50", "split"),
    ]
    assert C.flow_checkpoint_id("dialog.order-confirmation", "flow.x", "cp2") == "flow/dialog.order-confirmation/flow.x/cp2"


def test_pass_visits():
    assert C.pass_visits("a.b", "gadget", [], "ws.x", ["a.b"])
    assert C.pass_visits("a.b", "gadget", ["ws.x"], "ws.x", [])
    assert C.pass_visits("a.b", "dialog", [], "ws.x", [])
    assert not C.pass_visits("a.b", "gadget", ["ws.y"], "ws.x", ["c.d"])


def _chain_surface() -> dict:
    return {"id": CHAIN, "tier": 1, "kind": "chain_layout", "personas": ["options_trader"],
            "census": {"workspaces": [WS_A, WS_C], "dimensions": DIMS}}


def test_lint_and_scope_agree_on_census_dimension_ids():
    """04-census-dimension-id-mismatch: a tier-1 surface with dimensions and a tier C census workspace. The scope plans
    the dimension ids in the tier A workspace and the plain id in the tier C one; the lint defines exactly those."""
    s = _chain_surface()
    workspaces = {WS_A: {"tier": "A", "gadgets_present": [CHAIN]}, WS_C: {"tier": "C", "gadgets_present": [CHAIN]}}
    lint_ids = {cid for cid, kind in C.surface_checkpoints(s, workspaces, []) if kind == "census"}
    entry = {"entry_id": "tos-1-1", "tickets": ["TOS-1"], "summary": "chain", "declared_surfaces": [CHAIN],
             "touched_primitives": [], "expected_visual": "the option chain shows a new column header", "feature_flags": []}
    manifest = Manifest.from_dict({"manifest_id": "mf-x", "candidate": "branch-1998.3.0-7be42d0", "base": "main-1998.2.0-4f1c9a2",
                                   "source": "manual", "author": "a", "entries": [entry]})
    plan = compute_scope("pr", manifest, DependencyMap.from_dict({"primitives": [], "edges": []}), Registry.from_entries([s]),
                         run_id="run-2026-11-10-pr-0001", rigs=[Rig("rig-1", "fp")], candidate_build_id="branch-1998.3.0-7be42d0",
                         workspaces=[Workspace(w, d["tier"], None, frozenset(d["gadgets_present"])) for w, d in workspaces.items()])
    scope_ids = {cp.checkpoint_id for cp in plan.planned if cp.test_kind == "census"}
    assert scope_ids == lint_ids
    assert f"census/{CHAIN}/{WS_C}" in lint_ids and f"census/{CHAIN}/{WS_A}/layout=delta,strikes=8" in lint_ids


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def test_lint_defines_plain_census_id_outside_tier_a(base):
    """L-RGN-01 accepts a regions file for the plain census id of a tier C workspace and refuses the dimension id there."""
    r = copy.deepcopy(base)
    surf = r.docs["registry/surfaces/charts/charts.flexible-grid.chart-cell.yaml"].data
    surf.update(tier=1)
    surf["census"]["dimensions"] = {"layout": ["one", "two"]}
    surf["census"]["workspaces"].append("ws.tier-c.sweep-harness")
    src = r.docs["regions/census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader.yaml"]
    for cid in ("census/charts.flexible-grid.chart-cell/ws.tier-c.sweep-harness",
                "census/charts.flexible-grid.chart-cell/ws.tier-c.sweep-harness/layout=one"):
        rel = f"regions/{cid}.yaml"
        r.docs[rel] = type(src)(rel, "regions", {**copy.deepcopy(src.data), "checkpoint_id": cid})
    l01 = {v.file for v in lint_repo(r, 0, TODAY).violations if v.rule == "L-RGN-01"}
    assert "regions/census/charts.flexible-grid.chart-cell/ws.tier-c.sweep-harness/layout=one.yaml" in l01
    assert "regions/census/charts.flexible-grid.chart-cell/ws.tier-c.sweep-harness.yaml" not in l01
