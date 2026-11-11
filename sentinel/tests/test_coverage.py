"""Coverage accounting and the matrix (2.12) on the scope fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sentinel.coverage import (
    QUARANTINE_CEILING, Cell, Profile, QuarantineShare, build_matrix, cell_state, fixture_profiles, manifest_coverage,
    order_lifecycle_not_run, profile_for_role,
)
from sentinel.lint import Repo
from sentinel.records import PERSONAS, CheckpointOutcome, DiffSummary, RegionDiff
from sentinel.scope import (
    Cut, DependencyIndex, DependencyMap, Flow, FlowCheckpoint, Manifest, PlannedCheckpoint, Registry, Rig, Surface,
    Workspace, compute_scope,
)

FIX = Path(__file__).parent / "fixtures" / "scope"
RUN = "run-2026-11-10-merge-0001"
MARGIN = Profile("paper-margin", frozenset({"margin", "options_L1"}))
IRA = Profile("paper-ira", frozenset({"ira"}))
CHART = "charts.flexible-grid.chart-cell"
AT_CELL = "charts.flexible-grid.active-trader-cell"
FUTURES = "trade.order-entry.futures"
WS = {"active_trader": "ws.tier-a.active-trader", "swing_trader": "ws.tier-a.swing-trader",
      "futures_trader": "ws.tier-a.futures-trader", "options_trader": "ws.tier-a.options-trader",
      "beginner": "ws.tier-a.beginner"}


def _yaml(name: str):
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def registry() -> Registry:
    return Registry.from_entries(_yaml("registry.yaml"))


def out(surface: str, outcome: str, persona: str = "active_trader", reason: str | None = None, kind: str = "census",
        n: int = 0, **kw) -> CheckpointOutcome:
    ws = WS[persona]
    cid = {"census": f"census/{surface}/{ws}", "sweep": f"sweep/{surface}/flexgrid/{480 + n}x360",
           "flow": f"flow/{surface}/flow.test.flow/cp{n}"}[kind]
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=outcome, reason=reason, workspace_id=ws,
                             persona=persona, data_mode="frozen", **kw)


def cut(surface: str, reason: str, persona: str | None, tier: int = 1) -> Cut:
    cp = None if persona is None else PlannedCheckpoint(f"census/{surface}/{WS[persona]}", surface, "census", tier,
                                                        WS[persona], persona, "frozen")
    return Cut(reason, surface, tier, cp)


# -- the cell function ------------------------------------------------------------------------

def test_covered_cell_shows_worst_outcome_and_count_never_passed(registry):
    s = registry[AT_CELL]
    same = cell_state(s, "active_trader", MARGIN, [out(AT_CELL, "identical"), out(AT_CELL, "identical", kind="sweep")])
    assert (same.state, same.worst, same.count, same.display) == ("covered", "identical", 2, "identical (2)")
    assert same.kinds == ("census", "sweep")
    mixed = cell_state(s, "active_trader", MARGIN, [
        out(AT_CELL, "identical", kind="sweep", n=1), out(AT_CELL, "collateral", kind="sweep", n=2),
        out(AT_CELL, "invariant_violation", reason="INV-01", kind="sweep", n=3), out(AT_CELL, "noise", reason="below_floor"),
        out(AT_CELL, "env_error", gate="running", reason="occluded", kind="sweep", n=4)])
    assert mixed.display == "invariant_violation (4)"           # env_error is not usable, so n = 4
    assert "passed" not in same.display + mixed.display


def test_needs_baseline_counts_as_covered_with_a_footnote(registry):
    c = cell_state(registry[CHART], "active_trader", MARGIN, [out(CHART, "needs_baseline", reason="new_surface")])
    assert c.state == "covered" and c.worst == "needs_baseline" and c.needs_baseline == 1


def test_empty_cell_reasons_entitlement_quarantine_budget_scope(registry):
    fut = registry[FUTURES]
    assert cell_state(fut, "futures_trader", MARGIN, []).display == "not_run (entitlement)"
    at = registry[AT_CELL]
    cuts = [cut(AT_CELL, "quarantine", "active_trader"), cut(AT_CELL, "budget", "swing_trader")]
    assert cell_state(at, "active_trader", MARGIN, [], cuts).display == "not_run (quarantine)"
    assert cell_state(at, "swing_trader", MARGIN, [], cuts).display == "not_run (budget)"
    assert cell_state(at, "beginner", MARGIN, [], cuts).display == "not_run (scope)"    # not hosted for this persona
    assert cell_state(at, "active_trader", IRA, [], cuts).display == "not_run (entitlement)"   # entitlement first
    assert cell_state(at, "beginner", MARGIN, [], [cut(AT_CELL, "scope", None)]).reason == "scope"


def test_step_failed_and_blocked_cells(registry):
    s = registry[CHART]
    failed = cell_state(s, "swing_trader", MARGIN, [
        out(CHART, "not_run", "swing_trader", "step_failed:s05", kind="flow", n=1),
        out(CHART, "not_run", "swing_trader", "step_failed:s05", kind="flow", n=2),
        out(CHART, "not_run", "swing_trader", "app_crash", kind="flow", n=3)])
    assert (failed.state, failed.reason, failed.display) == ("not_run", "step_failed:s05", "not_run (step_failed:s05)")
    blocked = cell_state(s, "active_trader", MARGIN, [
        out(CHART, "env_error", gate="running", reason="occluded"),
        out(CHART, "not_settled", reason="timeout", kind="sweep", n=1)])
    assert blocked.reason == "blocked:env_error" and blocked.display == "blocked:env_error" and blocked.blocked
    assert blocked.in_denominator                                 # counted in run health, never covered


def test_unregistered_surface_adds_a_not_registered_row(registry):
    rogue = CheckpointOutcome(run_id=RUN, checkpoint_id="census/unregistered.p3.trader-tv-2/ws.tier-a.active-trader",
                              outcome="unregistered_surface", reason="probe:P3", workspace_id="ws.tier-a.active-trader",
                              persona="active_trader", data_mode="frozen")
    m = build_matrix(RUN, registry, [MARGIN], {MARGIN.id: [rogue]})
    assert m.rows[-1] == "unregistered.p3.trader-tv-2"
    cells = [m.cell("unregistered.p3.trader-tv-2", p, MARGIN.id) for p in PERSONAS]
    assert {(c.state, c.display, c.tier) for c in cells} == {("not_registered", "not_registered", 1)}
    assert not any(c.in_denominator for c in cells)


# -- the matrix, coverage percent and the quarantine ceiling ---------------------------------

def test_matrix_from_a_flexgrid_pr_plan(registry):
    plan = compute_scope(
        "pr", Manifest.from_dict(_yaml("manifest-flexgrid.yaml")), DependencyMap.from_dict(_yaml("dependency-map.yaml")),
        registry, DependencyIndex.from_depends_on(_yaml("dependency-index.yaml")),
        {"com.tos.desktop.ui.grid.GridCellLayout"}, run_id="run-pr", workspaces=[Workspace.from_dict(w) for w in _yaml("workspaces.yaml")],
        flows=[Flow.from_dict(f) for f in _yaml("flows.yaml")], entitlements=MARGIN.entitlements, rigs=[Rig("rig-1"), Rig("rig-2")])
    outcomes = [cp.outcome("run-pr", "identical") for cp in plan.planned]
    m = build_matrix("run-pr", registry, [MARGIN], {MARGIN.id: outcomes}, plan.cuts)
    assert m.cell(CHART, "swing_trader", MARGIN.id).display == "identical (1)"
    assert m.cell(AT_CELL, "active_trader", MARGIN.id).display == "identical (13)"   # canonical census + 12 sweep steps
    assert m.cell("analyze.risk-profile", "options_trader", MARGIN.id).display == "not_run (scope)"
    assert m.cell(FUTURES, "futures_trader", MARGIN.id).display == "not_run (entitlement)"
    t1 = m.tier_coverage(1)
    assert t1.denominator == sum(c.in_denominator for c in m.cells.values() if c.tier == 1)
    assert t1.percent == 100.0                                    # PR percent is relative to its computed scope


def test_coverage_percent_drops_entitlement_and_scope_cells_only(registry):
    outcomes = {MARGIN.id: [out(AT_CELL, "identical"), out(CHART, "env_error", gate="running", reason="occluded")]}
    m = build_matrix(RUN, registry, [MARGIN], outcomes, [cut("settings.order-defaults", "budget", "beginner")])
    t1 = m.tier_coverage(1)
    assert (t1.covered, t1.denominator, t1.percent) == (1, 2, 50.0)   # AT cell covered; settings budget-cut
    t2 = m.tier_coverage(2)
    assert (t2.covered, t2.denominator, t2.blocked) == (0, 1, 1)      # the blocked chart cell stays in
    rows = m.to_rows()
    assert len(rows) == len(list(registry)) * len(PERSONAS)
    for r in rows:
        assert (r["state"] == "not_run") == (r["not_run_reason"] is not None)     # the CHECKs of 8.3
        assert (r["state"] == "covered") == (r["worst_outcome"] is not None)


def test_quarantine_ceiling_is_2_percent_of_tier1_checkpoints():
    surfaces = [Surface(id=f"monitor.s{i:02d}", tier=1, kind="section") for i in range(20)]
    reg = Registry.from_entries(surfaces)                       # 20 surfaces x 5 personas = 100 tier-1 checkpoints
    every = [(f"monitor.s{i:02d}", p) for i in range(20) for p in WS]

    def matrix(quarantined, off=()):
        cuts = [cut(s, "quarantine", p) for s, p in quarantined] + [cut(s, "entitlement", p) for s, p in off]
        ran = [out(s, "identical", p) for s, p in every if (s, p) not in quarantined and (s, p) not in off]
        return build_matrix(RUN, reg, [MARGIN], {MARGIN.id: ran}, cuts)

    two = [("monitor.s00", "active_trader"), ("monitor.s01", "active_trader")]
    m = matrix(two)
    assert m.quarantine_share() == QuarantineShare(2, 100) and not m.quarantine_share().red
    assert matrix(two, off=every[-10:]).quarantine_share() == QuarantineShare(2, 90)   # entitlement cuts drop out
    assert m.quarantine_cells() == (2, 100)                     # the cell share, for display
    m3 = matrix(two + [("monitor.s02", "beginner")])
    share = m3.quarantine_share()
    assert share.share == 0.03 > QUARANTINE_CEILING and share.red
    assert m3.cell("monitor.s02", "beginner", MARGIN.id).display == "not_run (quarantine)"


def test_synthetic_canary_records_are_not_coverage(registry):
    fake = out(AT_CELL, "invariant_violation", reason="INV-01", synthetic=True)
    m = build_matrix(RUN, registry, [MARGIN], {MARGIN.id: [fake]})
    assert m.cell(AT_CELL, "active_trader", MARGIN.id).state == "not_run"


# -- the manifest coverage table --------------------------------------------------------------

def test_manifest_coverage_table():
    m = Manifest.from_dict({**_yaml("manifest-flexgrid.yaml"), "entries": [
        {"entry_id": "tos-1-1", "tickets": ["TOS-1"], "declared_surfaces": [CHART]},
        {"entry_id": "tos-2-1", "tickets": ["TOS-2"], "declared_surfaces": [AT_CELL]},
        {"entry_id": "tos-3-1", "tickets": ["TOS-3"], "declared_surfaces": ["tools.thinklog"]},
        {"entry_id": "tos-4-1", "tickets": ["TOS-4"], "declared_surfaces": [FUTURES]},
        {"entry_id": "tos-5-1", "tickets": ["TOS-5"], "declared_surfaces": ["sidebar.level2"], "must_be_observed": False}]})
    above = DiffSummary(regions=[RegionDiff("rgn.ladder.viewport", 0.02, 0.91, True)])
    records = [out(CHART, "intended", reason=None), out(CHART, "identical", "swing_trader"),
               out(AT_CELL, "identical"), out(AT_CELL, "invariant_violation", reason="INV-01", kind="sweep", diff=above),
               out(FUTURES, "identical", "futures_trader"), out("sidebar.level2", "identical")]
    rows, findings = manifest_coverage(RUN, m, records, visibility={"tos-2-1": "not_visible"})
    status = {r.entry_id: r.status for r in rows}
    assert status == {"tos-1-1": "verified_visible", "tos-2-1": "not_observed", "tos-3-1": "no_covering_checkpoint",
                      "tos-4-1": "not_observed", "tos-5-1": "not_observed"}
    assert rows[0].display == "tos-1-1: verified visible on 1 checkpoints"
    assert rows[2].display == "tos-3-1: no covering checkpoint"
    assert [(f.kind, f.manifest_entry_ref) for f in findings] == [
        ("intended_change_not_observed", "tos-2-1"), ("intended_change_not_observed", "tos-4-1")]
    assert findings[0].evidence["raw_values"]["visibility"] == "not_visible"


def test_cell_requires_one_state():
    c = Cell("x.y", "beginner", MARGIN.id, 3, "not_run", reason="scope")
    assert c.to_row(RUN)["not_run_reason"] == "scope" and c.to_row(RUN)["worst_outcome"] is None


# -- entitlement profiles (2.12 columns, 2.13 B01 and B04) ----------------------------------------

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"


def _example():
    repo = Repo.load(EXAMPLE)
    return (repo.one("fixture").data, Registry.from_entries(d.data for d in repo.of("surface")),
            [Flow.from_dict(d.data) for d in repo.of("flow")])


def test_the_example_fixture_yields_a_margin_and_an_ira_profile():
    fixture, registry, _ = _example()
    profiles = fixture_profiles(fixture)
    assert profiles == (Profile("margin", frozenset({"margin"})), Profile("ira", frozenset({"ira"})))
    assert fixture_profiles(fixture, extra=["paper_send"])[1].entitlements == {"ira", "paper_send"}
    assert (profile_for_role(fixture, "golden"), profile_for_role(fixture, "empty")) == ("margin", "ira")
    matrix = build_matrix(RUN, registry, profiles, [])
    assert len(matrix.cells) == len(list(registry)) * len(PERSONAS) * 2 == len(matrix.to_rows())


def test_a_margin_only_surface_is_not_run_entitlement_in_the_ira_column():
    """Outcomes count toward the profile of the account they ran on (4.13 `profile_id`); a surface that needs margin
    has no checkpoint under the IRA profile, so its whole row there is not_run (entitlement) (2.12)."""
    fixture, registry, _ = _example()
    profiles = fixture_profiles(fixture)
    ladder = "trade.active-trader.ladder"
    on_margin = CheckpointOutcome(run_id=RUN, checkpoint_id=f"census/{ladder}/ws.tier-a.active-trader",
                                  outcome="identical", workspace_id="ws.tier-a.active-trader", persona="active_trader",
                                  data_mode="frozen", surface_id=ladder, profile_id="margin")
    untagged = CheckpointOutcome(run_id=RUN, checkpoint_id=f"census/{CHART}/ws.tier-a.swing-trader", outcome="identical",
                                 workspace_id="ws.tier-a.swing-trader", persona="swing_trader", data_mode="frozen",
                                 surface_id=CHART)
    matrix = build_matrix(RUN, registry, profiles, [on_margin, untagged])
    assert matrix.cell(ladder, "active_trader", "margin").display == "identical (1)"
    assert matrix.cell(ladder, "active_trader", "ira").display == "not_run (entitlement)"
    assert {matrix.cell(ladder, p, "ira").reason for p in PERSONAS} == {"entitlement"}
    assert matrix.cell(CHART, "swing_trader", "margin").state == "covered"     # no profile_id: the first column
    assert matrix.cell(CHART, "swing_trader", "ira").display == "not_run (scope)"   # entitled, not run there


def test_the_order_lifecycle_count_names_paper_send_surfaces_until_a_profile_carries_it():
    fixture, registry, flows = _example()
    profiles = fixture_profiles(fixture)
    assert order_lifecycle_not_run(registry, flows, profiles) == ()      # the example has no order-lifecycle surface
    filled = Surface("monitor.activity.filled-orders", 1, entitlements=frozenset({"margin", "paper_send"}))
    sending = Flow("flow.trade.order-ticket.stock.fill", 1, "swing_trader", "ws.tier-a.swing-trader",
                   ("dialog.order-confirmation",), (FlowCheckpoint("flow/dialog.order-confirmation/x/s1",
                                                                   "dialog.order-confirmation", "s1", "frozen"),),
                   requires=frozenset({"paper_send"}))
    reg = Registry.from_entries(list(registry) + [filled])
    assert order_lifecycle_not_run(reg, flows + [sending], profiles) == (
        "dialog.order-confirmation", "monitor.activity.filled-orders")
    assert order_lifecycle_not_run(reg, flows + [sending], fixture_profiles(fixture, ["paper_send"])) == ()
