"""Regressions for the logic review of classify, invariants, scope and coverage (L-01 to L-12).

Each test failed before its fix. The finding id and spec section are in each docstring.
"""
from __future__ import annotations

from dataclasses import replace

from sentinel.classify import DecisionMatch, Facts, classify
from sentinel.coverage import Profile, build_matrix, manifest_coverage
from sentinel.invariants import (
    CheckerConfig, GridCell, Rect, RuleRegion, ScreenModel, SurfaceRules, UIComponent, check, compare,
)
from sentinel.records import CheckpointOutcome, Decision, DiffSummary, Finding, RegionDiff, Signature
from sentinel.scope import (
    BaseReuse, DependencyIndex, DependencyMap, Manifest, ManifestEntry, PrRunSummary, Registry, Rig, SoakPlan,
    StoredBase, Surface, Workspace, compute_scope, merge_run_findings,
)

# -- classify (3.11, 4.3, 4.14, F38) ----------------------------------------------------------

SIG = Signature("census/charts.flexible-grid.active-trader-cell/ws-a", "rgn.ladder.viewport", "0123456789abcdef",
                "collateral")
ABOVE = dict(diff="above", stability="stable", primitive="FlexibleGrid", threshold_source="calibrated")


def _decision(i, action, verdict, **kw):
    return Decision(i, action, verdict, SIG, decided_by="alice", decided_at="2026-11-01T00:00:00+00:00", **kw)


def test_l01_intended_decision_made_on_this_build_pair_is_inherited_from_the_decision_record():
    """L-01, 3.11 input M, F38: an `intended` verdict matches on the build pair it was made on. The
    Decision record carries `build_pair_id`; the store builds DecisionMatch(decision, distance)
    without repeating it, so classify reads the decision's own field."""
    d = _decision("dec-7", "mark_intended", "intended", ticket="TOS-1", build_pair_id="pair-7")
    facts = Facts(**ABOVE, manifest="undeclared", decisions=(DecisionMatch(d, hamming=0),),
                  build_pair_id="pair-7", manifest_entry_ref="tos-1-1")
    c = classify(facts)
    assert (c.row, c.outcome, c.decision_ref) == ("14", "intended", "dec-7")


def test_l01_intended_decision_from_another_build_pair_is_not_inherited():
    """L-01: whichever build pair is set (on the match or on the record) must equal this run's."""
    d = _decision("dec-7", "mark_intended", "intended", ticket="TOS-1", build_pair_id="pair-6")
    on_record = Facts(**ABOVE, manifest="undeclared", decisions=(DecisionMatch(d, hamming=0),), build_pair_id="pair-7")
    assert (classify(on_record).row, classify(on_record).decision_ref) == ("16", None)
    both = Facts(**ABOVE, manifest="undeclared", build_pair_id="pair-7",
                 decisions=(DecisionMatch(d, hamming=0, build_pair_id="pair-7"),))
    assert classify(both).row == "16"


def test_l02_no_manifest_ui_run_never_produces_intended_from_decision_memory():
    """L-02, 4.3 Missing manifest: the `intended` category is disabled for the run; without an entry
    no diff can be `intended`. Case (a) diffs above threshold are `collateral (manifest_missing)`."""
    d = _decision("dec-8", "mark_intended", "intended", ticket="TOS-2")
    facts = Facts(**ABOVE, manifest="no_manifest_ui", build_pair_id="pair-7",
                  decisions=(DecisionMatch(d, hamming=0, build_pair_id="pair-7"),))
    c = classify(facts)
    assert c.outcome != "intended"
    assert (c.outcome, c.reason) == ("collateral", "manifest_missing")


def test_l02_no_manifest_nonui_run_never_produces_intended_from_decision_memory():
    """L-02, 4.3 case (b): every above-threshold diff is `needs_human (reason: no_manifest)`."""
    d = _decision("dec-9", "mark_intended", "intended", ticket="TOS-3")
    facts = Facts(**ABOVE, manifest="no_manifest_nonui", build_pair_id="pair-7",
                  decisions=(DecisionMatch(d, hamming=0, build_pair_id="pair-7"),))
    c = classify(facts)
    assert (c.outcome, c.reason) == ("needs_human", "no_manifest")


def test_l03_rescued_capture_ignores_confirmed_bug_memory():
    """L-03, 3.11 notes rows 13 and 14, 4.14: a `rescued` checkpoint ignores decision memory, so a
    rescued collateral is not filed as a known bug with reason `decision:<id>`."""
    bug = _decision("dec-3", "confirm_bug", "confirmed_bug", ticket="TOS-9")
    facts = Facts(**ABOVE, manifest="undeclared", rescued=True, decisions=(DecisionMatch(bug, hamming=1),))
    c = classify(facts)
    assert (c.row, c.outcome) == ("16", "collateral")
    assert c.reason is None and c.decision_ref is None


def test_l03_rescued_invariant_violation_is_not_marked_suppressed_by_a_decision():
    """L-03: the same rule on row 7; a rescued capture never carries a decision suppression."""
    noise = _decision("dec-1", "noise_signature", "noise")
    facts = Facts(invariant_ids=("INV-01",), invariant_stability="stable", rescued=True,
                  decisions=(DecisionMatch(noise, hamming=0),))
    c = classify(facts)
    assert c.outcome == "invariant_violation"
    assert c.decision_ref is None


# -- invariants (2.7, F46) --------------------------------------------------------------------

def _ids(fc):
    return [v.invariant_id for v in fc.violations]


def test_l04_inv04_branch_a_on_masked_live_does_not_use_the_base_and_follows_the_price_rule():
    """L-04, 2.7 INV-04 false-positive controls: on `masked_live` the base comparison is replaced by
    the region's rule, so an ellipsis fires where the rule admits none, whatever the base showed."""
    region = RuleRegion("rgn.ladder.last", Rect(0, 0, 200, 40), "price", decimals=2)
    rules = SurfaceRules("trade.active-trader.ladder", tier=2, data_mode="masked_live", rule_regions=(region,))
    cand = ScreenModel("A", (480, 360), components=(
        UIComponent("last", Rect(10, 10, 60, 20), "text", parent="frame", text="1,234.5…", label="1,234.56"),))
    base = ScreenModel("A", (480, 360), components=(
        UIComponent("last", Rect(10, 10, 60, 20), "text", parent="frame", text="1,23…", label="1,233.10"),))
    assert "INV-04" in _ids(check(cand, rules, reference=base))


def test_l04_inv04_branch_a_inside_a_text_rule_region_that_admits_an_ellipsis_does_not_fire():
    """L-04, 2.7 INV-04: inside a `rule` region the rule decides; a `text` rule with no pattern
    admits an ellipsis."""
    region = RuleRegion("rgn.watchlist.description", Rect(0, 0, 200, 40), "text")
    rules = SurfaceRules("monitor.watchlist", tier=2, data_mode="masked_live", rule_regions=(region,))
    cand = ScreenModel("A", (480, 360), components=(
        UIComponent("desc", Rect(10, 10, 80, 20), "text", parent="frame", text="Apple Comp…", label="Apple Computer Inc"),))
    assert "INV-04" not in _ids(check(cand, rules))


def test_l05_inv01_branch_b_compares_parentless_segmented_panels():
    """L-05, 2.7 INV-01 Branch B: pairwise intersection of panels segmented by border lines. Branch B
    has no tree, so two parentless panels overlapping 40x40 px fire (the 'overlapped panels' mutant)."""
    rules = SurfaceRules("charts.flexible-grid.active-trader-cell", tier=1)
    model = ScreenModel("B", (480, 360), components=(
        UIComponent("order-strip", Rect(0, 100, 200, 60), "panel"),
        UIComponent("ladder-viewport", Rect(0, 120, 200, 200), "panel"),
    ))
    assert "INV-01" in _ids(check(model, rules))


def test_l05_inv01_branch_b_parentless_nesting_and_control_in_panel_do_not_fire():
    """L-05 controls: without a tree, a parentless panel inside another is nesting, and a control
    is compared with controls only."""
    rules = SurfaceRules("charts.flexible-grid.active-trader-cell", tier=1)
    model = ScreenModel("B", (480, 360), components=(
        UIComponent("frame", Rect(0, 0, 480, 360), "panel"),
        UIComponent("order-strip", Rect(0, 100, 200, 60), "panel"),
        UIComponent("buy-mkt", Rect(10, 110, 80, 22), "control"),
    ))
    assert "INV-01" not in _ids(check(model, rules))


def test_l06_inv15_violation_on_both_builds_stays_pre_existing_after_a_layout_shift():
    """L-06, 2.7 Pre-existing: a violation on both builds is pre-existing, one level lower. A 4 px
    layout shift (the Flexible Grid padding change of 1.11) must not make it new."""
    config = CheckerConfig(palette={"positive": (0, 200, 0), "negative": (220, 0, 0), "neutral": (200, 200, 200)})
    rules = SurfaceRules("monitor.activity.position-statement", tier=2, signed_columns=frozenset({"pl"}))

    def frames(dx):
        cells = (GridCell("pl", Rect(100 + dx, 50 + dx, 60, 16), text="+1.25", color=(220, 0, 0)),
                 GridCell("pl", Rect(100 + dx, 70 + dx, 60, 16), text="-0.50", color=(220, 0, 0)))
        m = ScreenModel("A", (480, 360), cells=cells)
        return (m, m, m)

    result = compare(frames(0), frames(4), rules, config=config)
    assert [r.violation.invariant_id for r in result.reported] == ["INV-15"]
    assert result.reported[0].pre_existing and result.reported[0].severity == "S2"


# -- scope (2.11, 3.1, 11.2, 11.3, 1.8) -------------------------------------------------------

WS = [Workspace("ws-a", "A", "active_trader"), Workspace("ws-c", "C", "active_trader"),
      Workspace("ws-b", "B", "active_trader")]
EMPTY_MAP = DependencyMap({}, [])


def _registry(n):
    return Registry.from_entries([Surface(id=f"monitor.s{i:03d}", tier=3, kind="gadget", census_workspaces=("ws-a",),
                                          personas=("active_trader",)) for i in range(n)])


def _union():
    return Manifest.union([], candidate="main-2", base="main-1", author="orchestrator", approved_by="lead")


def test_l07_rc_scope_puts_no_scripted_work_on_the_soak_rig():
    """L-07, 1.9, 11.2: rigs 1 to 4 carry census, flows and sweeps and rig 5 carries the 6 h soak,
    even when the soak rig is also in the pool."""
    rigs = [Rig(f"rig-{i}") for i in range(1, 6)]
    soak = SoakPlan(Rig("rig-5"), "ws-b", "monitor.s000")
    plan = compute_scope("rc", _union(), EMPTY_MAP, _registry(300), run_id="run-rc-1", workspaces=WS, rigs=rigs,
                         soak=soak, explorer_rig="rig-6")
    assert all(it.kind == "soak" for it in plan.by_rig["rig-5"])
    assert [c for c in plan.cuts if c.reason == "budget"] == []


def test_l08_rc_scope_puts_no_scripted_work_on_the_explorer_rig():
    """L-08, 11.3: 'The Explorer login never does scripted work'; 11.2: rig 6 does no scripted work."""
    rigs = [Rig(f"rig-{i}") for i in (1, 2, 3, 4, 6)]
    plan = compute_scope("rc", _union(), EMPTY_MAP, _registry(60), run_id="run-rc-2", workspaces=WS, rigs=rigs,
                         explorer_rig="rig-6")
    assert plan.by_rig.get("rig-6", []) == []


def test_l09_frozen_base_is_not_reused_on_a_rig_with_another_fingerprint():
    """L-09, 3.1: a frozen base capture is reused only when the stored fingerprint equals the live
    one, which is the fingerprint of the rig the item is placed on."""
    reg = _registry(40)
    ids = [f"census/{s.id}/ws-a" for s in reg]
    stored = {cid: StoredBase("run-merge-0755", "fp-a", "E-1") for cid in ids}
    rigs = [Rig("rig-1", "fp-a"), Rig("rig-2", "fp-b")]
    plan = compute_scope("merge", _union(), EMPTY_MAP, reg, run_id="run-m-1", workspaces=WS, rigs=rigs,
                         base_reuse=BaseReuse("fp-a", "E-1", stored))
    on_a = [cp for it in plan.by_rig["rig-1"] for cp in it.checkpoints if cp.checkpoint_id in stored]
    on_b = [cp for it in plan.by_rig["rig-2"] for cp in it.checkpoints]
    assert on_b, "fixture should place some items on rig-2"
    assert all(cp.base_source_run_id is None for cp in on_b)
    assert on_a and all(cp.base_source_run_id == "run-merge-0755" for cp in on_a)
    b_items = [it for it in plan.by_rig["rig-2"] if it.kind == "census"]
    assert all(it.rig_cost_s == 2 * it.estimated_s for it in b_items)


def test_l12_missed_by_static_impact_carries_detail_static_impact():
    """L-12, 4.13 finding kinds and 1.8: 'missed by static impact' is `missed_in_pr_run` with
    `detail: static_impact`."""
    reg = Registry.from_entries([Surface(id="monitor.one", tier=1, roots=("com.x.one",))])
    idx = DependencyIndex.from_depends_on({"com.x.one.View": ["com.x.core.Base"]})
    pr = PrRunSummary("run-pr-1", frozenset(), frozenset(), frozenset(), frozenset({"com.x.core.Base"}), frozenset())
    f = Finding("fnd-1", "run-m-1", "collateral", "ladder overlap", "run-m-1", checkpoint_id="census/monitor.one/ws-a",
                tier=1)
    out = merge_run_findings("run-m-1", [f], [pr], reg, idx)
    missed = [o for o in out if o.kind == "missed_in_pr_run"]
    assert len(missed) == 1 and missed[0].detail == "static_impact"


# -- coverage (2.12, 3.10, 10.5, 1.5, 9.11) ---------------------------------------------------

SID = "trade.active-trader.ladder"


def _out(cid, outcome, reason=None, **kw):
    return CheckpointOutcome(run_id="run-rc-1", checkpoint_id=cid, outcome=outcome, reason=reason,
                             workspace_id="ws-c", persona="active_trader", data_mode="frozen", tier=1, **kw)


def test_l10_quarantine_ceiling_counts_tier1_checkpoints_not_cells():
    """L-10, 3.10 step 6, 10.5, 9.10, 1.5 metric: one of ten tier-1 checkpoints quarantined is 10
    percent. The cell is `covered` by the other nine, so a cell-based share read 0."""
    reg = Registry.from_entries([Surface(id=SID, tier=1)])
    sizes = [(960, 720), (960, 480), (960, 360), (640, 720), (640, 480), (640, 360), (480, 720), (480, 480),
             (480, 360), (320, 720)]
    recs = [_out(f"sweep/{SID}/flexgrid/{w}x{h}", "identical") for w, h in sizes[:9]]
    recs.append(_out(f"sweep/{SID}/flexgrid/{sizes[9][0]}x{sizes[9][1]}", "not_run", "quarantine"))
    m = build_matrix("run-rc-1", reg, [Profile("margin", frozenset())], {"margin": recs}, personas=("active_trader",))
    share = m.quarantine_share()
    assert (share.quarantined, share.checkpoints) == (1, 10) and share.red
    assert m.quarantine_cells() == (0, 1)                       # the cell view, display only


def test_l11_rescued_clean_capture_does_not_count_as_an_observed_intended_change():
    """L-11, 2.12 manifest coverage and 3.11 rows 9a and 11a: a zero or below-floor diff that is
    `needs_human` (rescued_capture) is no change above the noise floor."""
    manifest = Manifest("mf-1", "branch-2", "main-1", "pr", "dev", entries=(
        ManifestEntry("tos-1-1", ("TOS-1",), declared_surfaces=(SID,)),))
    clean = DiffSummary(regions=[RegionDiff("rgn.ladder.viewport", 0.0, 1.0, False)])
    rec = _out(f"census/{SID}/ws-a", "needs_human", "rescued_capture", rescued=True, diff=clean)
    rows, findings = manifest_coverage("run-pr-1", manifest, [rec])
    assert rows[0].status == "not_observed"
    assert [f.kind for f in findings] == ["intended_change_not_observed"]
    shown = DiffSummary(regions=[RegionDiff("rgn.ladder.viewport", 0.03, 0.8, True)])
    rows, findings = manifest_coverage("run-pr-1", manifest, [replace(rec, diff=shown)])
    assert rows[0].status == "verified_visible" and findings == []


def test_waiver_applies_to_every_run_while_active():
    """9.7: a waiver (mark_intended on an invariant_violation signature) is not limited by the ticket
    scope that limits ordinary intended decisions; the outcome stays invariant_violation and the
    decision is linked as the waiver."""
    wsig = Signature(SIG.checkpoint_id, SIG.region_id, SIG.phash, "invariant_violation")
    w = Decision("dec-9", "mark_intended", "intended", wsig, decided_by="alice",
                 decided_at="2026-11-01T00:00:00+00:00", ticket="TOS-9", build_pair_id="pair-1")
    facts = Facts(invariant_ids=("INV-01",), invariant_stability="stable", manifest="undeclared",
                  decisions=(DecisionMatch(w, hamming=0),), build_pair_id="pair-2")
    c = classify(facts)
    assert c.outcome == "invariant_violation"
    assert c.decision_ref == "dec-9"
