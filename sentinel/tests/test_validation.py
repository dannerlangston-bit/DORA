"""Registry validation (2.2.3, 1.4 G1, 4.5, SD-15) and its Nightly self-test (10.3): `sentinel.validation`."""
from __future__ import annotations

import pytest

from sentinel import required_fields
from sentinel import validation as v
from sentinel.classify import Facts, classify
from sentinel.evaluation import BInput
from sentinel.records import CheckpointOutcome
from sentinel.scope import Manifest, ManifestEntry, PlannedCheckpoint
from sentinel.validation import ProbeListing as L

RUN = "run-2026-11-10-pr-4812"
WS = "ws.tier-a.active-trader"
PERSONA = "active_trader"
LOGIN = frozenset({"margin", "options_L1"})
STAMPS = {"fingerprint_id": "fp-0001", "data_epoch_id": "ep-0001"}
TABS = ("Monitor", "Trade", "Analyze", "Scan")
SUBTABS = ("All Products", "Active Trader", "Risk Profile")


def entry(sid: str, title: str, probe: str, *, parent: str | None = None, aliases: tuple[str, ...] = (),
          entitlements: tuple[str, ...] = (), kind: str = "tab", owner: str = "tos-shell-ui") -> dict:
    return {"id": sid, "title": title, "kind": kind, "tier": 2, "owner": owner, "enumerable_by": probe,
            "parent": parent, "aliases": list(aliases), "entitlements": list(entitlements)}


def registry(*extra: dict) -> dict[str, dict]:
    docs = [
        {"id": "shell.tab-bar", "title": "Tab bar", "kind": "panel", "tier": 2, "owner": "tos-shell-ui",
         "enumerable_by": "none"},
        {"id": "registry.validation-p1", "title": "Top tab row", "kind": "probe", "tier": 1,
         "owner": "sentinel-registry", "parent": "shell.tab-bar", "enumerable_by": "none"},
        entry("monitor.tab", "Monitor", "P1", parent="shell.tab-bar"),
        entry("trade.tab", "Trade", "P1", parent="shell.tab-bar", aliases=("Trading",)),
        entry("analyze.tab", "Analyze", "P1", parent="shell.tab-bar"),
        entry("scan.tab", "Scan", "P1", parent="shell.tab-bar", owner="tos-scan-ui"),
        entry("trade.subtab.all-products", "All Products", "P2", parent="trade.tab", kind="subtab"),
        entry("trade.subtab.active-trader", "Active Trader", "P2", parent="trade.tab", kind="subtab"),
        entry("analyze.subtab.risk-profile", "Risk Profile", "P2", parent="analyze.tab", kind="subtab"),
        *extra,
    ]
    return {d["id"]: d for d in docs}


def listing(*labels: str, side: str = "c", located: bool = True) -> L:
    return L(tuple(labels), located, f"{side}{len(labels):063d}")


def run(probe: str, cand: tuple[str, ...], base: tuple[str, ...], reg: dict | None = None,
        manifest: Manifest | None = None, **kw) -> v.ProbeResult:
    return v.validate(probe, listing(*cand, side="c"), listing(*base, side="b"), reg or registry(), LOGIN, manifest,
                      workspace_id=WS, persona=PERSONA, **kw)


def manifest(*declared: str) -> Manifest:
    return Manifest("mf-1", "cand-1", "base-1", "pr", "dev", (ManifestEntry("tos-1-1", ("TOS-1",),
                                                                             declared_surfaces=declared),))


def lists(**over: tuple[tuple[str, ...], tuple[str, ...]]) -> dict[str, tuple[L, L]]:
    out = {p: (listing(side="c"), listing(side="b")) for p in v.PROBES}
    out["P1"] = (listing(*TABS, side="c"), listing(*TABS, side="b"))
    out["P2"] = (listing(*SUBTABS, side="c"), listing(*SUBTABS, side="b"))
    for p, (c, b) in over.items():
        out[p] = (listing(*c, side="c"), listing(*b, side="b"))
    return out


def cp(sid: str, kind: str = "census") -> PlannedCheckpoint:
    cid = {"census": f"census/{sid}/{WS}", "flow": f"flow/{sid}/flow.x/s01"}[kind]
    return PlannedCheckpoint(cid, sid, kind, 2, WS, PERSONA, "frozen")


# -- labels -------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("_Trade (3)", "trade"), ("Positions (3)", "positions"), ("  Active \t Trader ", "active trader"),
    ("Tr_ade", "trade"), ("ＴＲＡＤＥ", "trade"), ("Level (2) II", "level (2) ii"), ("Trade ( 12 )", "trade"),
])
def test_normalize_label(raw, want):
    assert v.normalize_label(raw) == want


def test_label_distance_is_normalized_levenshtein():
    assert v.label_distance("Chart", "_Chart (2)") == 0.0
    assert v.label_distance("Charts", "Chart") == pytest.approx(1 / 6)
    assert v.label_distance("", "") == 0.0
    assert v.label_distance("abc", "") == 1.0


def test_ids_follow_the_checkpoint_grammar():
    assert v.probe_surface_id("P3") == "registry.validation-p3"
    assert v.probe_checkpoint_id("P3", WS) == f"census/registry.validation-p3/{WS}"
    with pytest.raises(ValueError):
        v.probe_surface_id("P9")


# -- classify_windows ---------------------------------------------------------------------------

def test_classify_windows_matches_titles_and_aliases_of_the_probes_entries_only():
    got = v.classify_windows({"P1": ("Monitor", "_Trading", "Risk Profile", "Monitor (2)", "Futures")}, registry())
    p1 = got["P1"]
    assert p1.registered == (("Monitor", ("monitor.tab",)), ("_Trading", ("trade.tab",)))
    assert p1.unregistered == ("Risk Profile", "Monitor (2)", "Futures")   # a P2 title is not a P1 entry; the
    assert p1.present == {"monitor.tab", "trade.tab"}                      # second "Monitor" has no entry left
    assert p1.matched == {"monitor.tab", "trade.tab"} and p1.unresolved == frozenset()


def test_classify_windows_refuses_an_unknown_probe():
    with pytest.raises(ValueError):
        v.classify_windows({"P0": ()}, registry())


# -- pair_renames -------------------------------------------------------------------------------

def p1_entries(reg: dict | None = None) -> list[dict]:
    return [d for d in (reg or registry()).values() if d.get("enumerable_by") == "P1"]


def test_pairing_by_edit_distance():
    pairs = v.pair_renames(("Monitor", "Trades", "Analyze", "Scan"), TABS, p1_entries())
    assert [(p.label, p.entry_id, p.base_label, p.by) for p in pairs] == [("Trades", "trade.tab", "Trade",
                                                                           "edit_distance")]
    assert pairs[0].distance == pytest.approx(1 / 6)


def test_pairing_by_position():
    pairs = v.pair_renames(("Monitor", "Orders", "Analyze", "Scan"), TABS, p1_entries())
    assert [(p.label, p.entry_id, p.by) for p in pairs] == [("Orders", "trade.tab", "position")]
    assert pairs[0].distance > v.RENAME_DISTANCE_MAX


def test_edit_distance_wins_over_a_shifted_position():
    pairs = v.pair_renames(("Monitor", "New", "Trades", "Analyze", "Scan"), TABS, p1_entries())
    assert [(p.label, p.entry_id) for p in pairs] == [("Trades", "trade.tab")]


def test_unpaired_when_neither_criterion_holds():
    assert v.pair_renames(("Monitor", "Analyze", "Scan", "Zeta"), TABS, p1_entries()) == ()


def test_edit_distance_bound_is_inclusive():
    reg = registry(entry("x.tab", "abcdefghij", "P1", parent="shell.tab-bar"))
    base = (*TABS, "abcdefghij")
    near = v.pair_renames(("Monitor", "Trade", "abcdefgxyz", "Analyze", "Scan"), base, p1_entries(reg))
    assert [(p.label, p.by) for p in near] == [("abcdefgxyz", "edit_distance")]          # 3 / 10
    far = v.pair_renames(("Monitor", "Trade", "abcdefwxyz", "Analyze", "Scan"), base, p1_entries(reg))
    assert far == ()                                                                    # 4 / 10, other position


def test_an_entry_on_the_base_by_its_alias_can_pair():
    pairs = v.pair_renames(("Monitor", "Tradeing", "Analyze", "Scan"), ("Monitor", "Trading", "Analyze", "Scan"),
                           p1_entries())
    assert [(p.entry_id, p.base_label) for p in pairs] == [("trade.tab", "Trading")]


def test_substitute_label_changes_the_ocr_rung_only():
    pairs = v.pair_renames(("Monitor", "Trades", "Analyze", "Scan"), TABS, p1_entries())
    target = {"name": "shell.tab-bar.trade", "image": {"template": "t.png", "offset": [0, 0]},
              "jab": {"role": "page tab", "name": "Trade"}, "ocr": {"text": "Trade", "offset": [0, 0]}}
    out = v.substitute_label(target, pairs)
    assert out["ocr"] == {"text": "Trades", "offset": [0, 0]}
    assert out["jab"] == target["jab"] and out["image"] == target["image"] and target["ocr"]["text"] == "Trade"
    other = dict(target, ocr={"text": "Monitor"})
    assert v.substitute_label(other, pairs) == other
    no_ocr = {"name": "x", "image": {"template": "t.png"}}
    assert v.substitute_label(no_ocr, pairs) == no_ocr


# -- validate: the probe outcome ----------------------------------------------------------------

def test_identical_lists_with_every_label_registered():
    r = run("P1", TABS, TABS)
    assert r.record.outcome.outcome == "identical"
    assert r.record.checkpoint.checkpoint_id == f"census/registry.validation-p1/{WS}"
    assert r.record.checkpoint.tier == 1 and r.complete and r.health is None
    assert (r.unregistered, r.removed, r.appeared, r.subsets, r.proposals) == ((), (), (), (), ())
    assert r.record.artifacts == {"candidate": "c" + "4".zfill(63), "base": "b" + "4".zfill(63)}


def test_different_lists_are_collateral_without_a_declared_row_parent():
    r = run("P1", (*TABS, "Futures"), TABS)
    o = r.record
    assert o.outcome.outcome == "collateral" and o.classification.by == "rule" and o.manifest_entry_ref is None
    rec = o.to_outcome(RUN, **STAMPS)
    assert required_fields.errors(rec) == []          # the two probe screenshots stand in for the diff


def test_different_lists_are_intended_when_the_manifest_declares_the_row_parent():
    r = run("P1", (*TABS, "Futures"), TABS, manifest=manifest("shell.tab-bar"))
    assert r.record.outcome.outcome == "intended"
    assert r.record.manifest_entry_ref == "tos-1-1" and r.record.classification.category == "intended"


def test_row_parents_per_label_on_a_multi_row_probe():
    reg = registry(entry("analyze.subtab.thinkback", "thinkBack", "P2", parent="analyze.tab", kind="subtab"))
    trade_only = ("All Products", "Risk Profile")                     # Active Trader (under Trade) removed
    assert run("P2", trade_only, SUBTABS, reg, manifest("trade.tab")).record.outcome.outcome == "intended"
    both = ("All Products", "Risk Profile", "thinkBack")              # and thinkBack (under Analyze) added
    assert run("P2", both, SUBTABS, reg, manifest("trade.tab")).record.outcome.outcome == "collateral"
    assert run("P2", both, SUBTABS, reg, manifest("trade.tab", "analyze.tab")).record.outcome.outcome == "intended"


DEPTH_TRADE = entry("trade.all-products.product-depth", "Product Depth", "P2", parent="trade.subtab.all-products",
                    kind="subtab")
DEPTH_CHARTS = entry("charts.product-depth", "Product Depth", "P2", parent="charts.tab", kind="subtab")


def test_a_new_surface_repeating_a_registered_label_is_unregistered():
    """2.2.3 row 1, G1, choice 2 (review validation:0): a second "Product Depth" (a Charts sub-tab) beside the
    registered All Products section is an unregistered surface on every run until the registry changes, and the
    probe is not intended because the manifest declares the registered row's parent."""
    reg = registry(DEPTH_TRADE)
    base = (*SUBTABS, "Product Depth")
    cand = (*SUBTABS, "Product Depth", "Product Depth")
    r = run("P2", cand, base, reg, manifest("trade.subtab.all-products"))
    (u,) = r.unregistered
    assert u.surface_id == "unregistered.p2.product-depth" and "candidate only" in u.record.outcome.detail
    assert [(p.kind, p.surface_id) for p in r.proposals] == [("stub", "unregistered.p2.product-depth")]
    assert r.record.outcome.outcome == "collateral" and r.removed == () and r.appeared == ()
    again = run("P2", cand, cand, reg)
    assert again.record.outcome.outcome == "identical" and [u.surface_id for u in again.unregistered] == [
        "unregistered.p2.product-depth"]
    assert v.ValidationReport((again,)).control().alert.rule == "unregistered_surface"
    twice = run("P2", (*cand, "Product Depth"), base, reg)                   # each extra row has its own id
    assert len({u.surface_id for u in twice.unregistered}) == 2


def test_a_removed_surface_sharing_its_label_is_never_silently_accepted():
    """2.2.3, choices 2 and 5 (review validation:0): with both "Product Depth" entries registered and one row gone,
    the flat list cannot say which: neither is guessed removed, run health says validation is incomplete, the census
    visits both, and the probe is intended only when the manifest declares both rows' parents."""
    reg = registry(DEPTH_TRADE, DEPTH_CHARTS)
    base = (*SUBTABS, "Product Depth", "Product Depth")
    cand = (*SUBTABS, "Product Depth")
    r = run("P2", cand, base, reg, manifest("trade.subtab.all-products"))
    assert r.record.outcome.outcome == "collateral" and r.unregistered == () and r.removed == ()
    assert r.notes == (('validation incomplete for P2: "product depth" is the label of 2 entries '
                        '(charts.product-depth, trade.all-products.product-depth) but is listed 1 time(s) on the '
                        'candidate and 2 on the base, so the probe cannot tell which; the census visits each'),)
    report = v.ValidationReport((r,))
    assert report.health == r.notes and report.b_input(cp("charts.product-depth")) is None
    both = run("P2", cand, base, reg, manifest("trade.subtab.all-products", "charts.tab"))
    assert both.record.outcome.outcome == "intended"
    assert run("P2", base, base, reg).notes == ()                             # every entry has its row


def test_a_pure_reorder_is_judged_on_the_moved_labels():
    moved = ("Active Trader", "All Products", "Risk Profile")
    assert run("P2", moved, SUBTABS, manifest=manifest("trade.tab")).record.outcome.outcome == "intended"
    assert run("P2", moved, SUBTABS, manifest=manifest("analyze.tab")).record.outcome.outcome == "collateral"


def test_anchor_not_located_is_landmark_missing_with_the_side_and_leaves_the_registry_as_truth():
    r = v.validate("P1", listing(*TABS, "Futures"), listing(*TABS, side="b", located=False), registry(), LOGIN,
                   None, workspace_id=WS, persona=PERSONA)
    assert (r.record.outcome.outcome, r.record.outcome.reason, r.record.side) == ("landmark_missing", "probe:P1",
                                                                                  "base")
    assert not r.complete and r.unregistered == () and r.subsets == ()
    assert r.health == "validation incomplete for P1: landmark_missing (probe:P1) on base"
    assert required_fields.errors(r.record.to_outcome(RUN, **STAMPS)) == []
    both = v.validate("P1", listing(located=False), listing(located=False), registry(), LOGIN, None,
                      workspace_id=WS, persona=PERSONA)
    assert both.record.side == "both"


def test_ocr_calibration_failure_is_harness_error_and_outranks_the_rest():
    r = v.validate("P1", listing(located=False), listing(located=False), registry(), LOGIN, None,
                   workspace_id=WS, persona=PERSONA, ocr_calibrated=False)
    assert (r.record.outcome.outcome, r.record.outcome.reason) == ("harness_error", "ocr_calibration")
    assert r.health == "validation incomplete for P1: harness_error (ocr_calibration)"
    rec = r.record.to_outcome(RUN, detail="watchdog_event: wd-1; " + r.record.outcome.detail, **STAMPS)
    assert required_fields.errors(rec) == []


def test_probe_host_without_the_entitlement_is_not_run():
    reg = registry({"id": "registry.validation-p3", "title": "Gadget picker", "kind": "probe", "tier": 1,
                    "owner": "sentinel-registry", "parent": "shell.sidebar", "enumerable_by": "none"},
                   {"id": "shell.sidebar", "title": "Sidebar", "kind": "panel", "tier": 2, "owner": "x",
                    "enumerable_by": "none", "entitlements": ["futures"]},
                   entry("sidebar.watchlist", "Watchlist", "P3", kind="gadget"))
    r = v.validate("P3", listing(located=False), listing(), reg, LOGIN, None, workspace_id=WS, persona=PERSONA)
    assert (r.record.outcome.outcome, r.record.outcome.reason) == ("not_run", "entitlement")
    assert r.health is None and r.subsets == () and not r.complete
    entitled = v.validate("P3", listing(located=False), listing(), reg, LOGIN | {"futures"}, None,
                          workspace_id=WS, persona=PERSONA)
    assert entitled.record.outcome.outcome == "landmark_missing"


def test_unknown_probe_raises():
    with pytest.raises(ValueError):
        run("P0", TABS, TABS)


# -- validate: the result table -----------------------------------------------------------------

def test_new_label_on_the_candidate_is_an_unregistered_surface_with_a_stub_pr():
    r = run("P1", ("Monitor", "Trade", "Futures", "Analyze", "Scan"), TABS)
    (u,) = r.unregistered
    assert u.surface_id == "unregistered.p1.futures" and u.rename_candidate is None
    rec = u.record.to_outcome(RUN, **STAMPS)
    assert rec.checkpoint_id == f"census/unregistered.p1.futures/{WS}"
    assert (rec.outcome, rec.reason, rec.tier) == ("unregistered_surface", "probe:P1", 1)
    assert '"Futures"' in rec.detail and "candidate only" in rec.detail
    assert set(rec.artifacts) == {"candidate", "base"} and not rec.synthetic
    assert required_fields.errors(rec) == []
    (p,) = r.proposals
    assert (p.kind, p.surface_id, p.path) == ("stub", u.surface_id,
                                              "registry/surfaces/unregistered/unregistered.p1.futures.yaml")
    assert p.document == {"schema_version": 1, "id": u.surface_id, "title": "Futures", "kind": "tab", "tier": 1,
                          "enumerable_by": "P1", "parent": "shell.tab-bar", "owner": "sentinel-registry"}
    f = u.finding()
    assert (f.kind, f.tier) == ("unregistered_surface", 1)
    assert r.removed == () and r.appeared == () and r.renames == ()


def test_label_on_both_builds_without_an_entry_is_unregistered_too():
    r = run("P1", (*TABS, "Futures"), (*TABS, "Futures"))
    assert r.record.outcome.outcome == "identical"
    (u,) = r.unregistered
    assert "candidate and base" in u.record.outcome.detail


def test_rename_candidate_keeps_the_record_proposes_an_alias_and_still_visits_the_entry():
    r = run("P1", ("Monitor", "Orders", "Analyze", "Scan"), TABS)
    (u,) = r.unregistered
    assert u.rename_candidate == "trade.tab"
    assert u.record.outcome.reason == "probe:P1"                      # the reason stays the code and probe id
    assert "rename_candidate: trade.tab" in u.record.outcome.detail
    (p,) = r.proposals
    assert (p.kind, p.surface_id, p.document) == ("alias", "trade.tab", {"id": "trade.tab",
                                                                         "aliases": ["Trading", "Orders"]})
    assert p.path is None
    assert r.removed == () and [x.entry_id for x in r.renames] == ["trade.tab"]
    report = v.ValidationReport((r,))
    assert report.b_input(cp("trade.tab")) is None                    # visited and classified by the normal rules


def test_base_only_unpaired_entry_is_removed_for_its_census_checkpoints():
    r = run("P1", ("Monitor", "Trade", "Analyze"), TABS)
    assert r.removed == ("scan.tab",) and r.unregistered == () and r.subsets == ()
    report = v.ValidationReport((r,))
    assert report.b_input(cp("scan.tab")) == BInput("removed")
    assert report.b_input(cp("scan.tab", "flow")) is None
    assert report.b_input(cp("monitor.tab")) is None
    undeclared = classify(Facts(**report.b_input(cp("scan.tab")).facts, manifest="undeclared"))
    assert (undeclared.row, undeclared.outcome) == ("26", "collateral")       # 3.11 row 26


def test_candidate_only_entry_takes_needs_baseline():
    r = run("P1", TABS, ("Monitor", "Trade", "Analyze"))
    assert r.appeared == ("scan.tab",) and r.removed == ()
    report = v.ValidationReport((r,))
    for kind in ("census", "flow"):
        assert report.b_input(cp("scan.tab", kind)) == BInput("absent", "new_surface")
    c = classify(Facts(**report.b_input(cp("scan.tab")).facts))
    assert (c.outcome, c.reason) == ("needs_baseline", "new_surface")


def test_absent_on_both_without_the_entitlement_is_not_run():
    reg = registry(entry("forex.tab", "Forex", "P1", parent="shell.tab-bar", entitlements=("forex",)))
    r = run("P1", TABS, TABS, reg)
    assert [(s.selector, s.outcome.outcome, s.outcome.reason) for s in r.subsets] == [
        ("surface:forex.tab", "not_run", "entitlement")]
    assert r.missing == ()


def test_absent_on_both_with_the_entitlement_is_registry_entry_not_found_with_a_task():
    r = run("P1", ("Monitor", "Trade", "Analyze"), ("Monitor", "Trade", "Analyze"))
    assert [(s.selector, s.outcome.outcome, s.outcome.reason) for s in r.subsets] == [
        ("surface:scan.tab", "harness_error", "registry_entry_not_found")]
    report = v.ValidationReport((r,))
    (task,) = report.maintenance("2026-11-10T13:15:00+00:00", RUN)
    assert (task.kind, task.ref, task.owner, task.run_id) == ("registry_entry_not_found", "scan.tab", "tos-scan-ui",
                                                              RUN)
    ctl = report.control()
    assert ctl.passed and ctl.gate == "running" and ctl.has("maintenance_task") and ctl.outcomes == r.subsets
    assert ctl.alert.rule == "harness_error" and "system_owner" in ctl.alert.notified


def test_control_routes_new_entries_to_the_system_owner_and_is_quiet_otherwise():
    new = v.ValidationReport((run("P1", (*TABS, "Futures"), TABS),)).control()
    assert (new.alert.priority, new.alert.notified, new.actions) == ("P3", ("system_owner",), ())
    clean = v.ValidationReport((run("P1", TABS, TABS),)).control()
    assert clean.alert is None and clean.outcomes == () and clean.detail is None


def test_synthetic_ids_are_slugs_and_unique_within_a_probe():
    r = run("P1", (*TABS, "P/L", "P L", "★", "Café", "Trade History"), TABS)
    ids = [u.surface_id for u in r.unregistered]
    assert ids[0].startswith("unregistered.p1.p-l-") and len(ids[0]) == 28 and ids[1] == "unregistered.p1.p-l"
    assert ids[2].startswith("unregistered.p1.label-") and ids[3].startswith("unregistered.p1.cafe-")
    assert ids[4] == "unregistered.p1.trade-history"
    assert len(set(ids)) == 5
    for u in r.unregistered:                                          # each id is a valid checkpoint id
        assert isinstance(u.record.to_outcome(RUN), CheckpointOutcome)


def test_a_synthetic_id_depends_on_its_label_alone():
    """Choice 10, 2.2.3 row 1, 10.3 (review validation:3): a label's id does not move when another label folding to
    the same slug is listed before it, so the self-test that removes "P&L" while "P/L" is unregistered passes in
    either list order."""
    def ids(*labels):
        return {u.label: u.surface_id for u in run("P1", (*TABS, *labels), TABS).unregistered}
    assert ids("P/L")["P/L"] == ids("P&L", "P/L")["P/L"] != ids("P&L", "P/L")["P&L"]
    reg = registry(entry("monitor.account-statement.pl", "P&L", "P2", parent="monitor.tab", kind="subtab"))
    for order in (("P&L", *SUBTABS, "P/L"), ("P/L", *SUBTABS, "P&L")):
        r = self_test(reg, lists(P2=(order, order)), "monitor.account-statement.pl")
        assert r.passed and len(r.added) == 1, order


def test_an_alias_match_is_not_flagged():
    r = run("P1", ("Monitor", "Trading", "Analyze", "Scan"), TABS)
    assert (r.unregistered, r.removed, r.appeared, r.subsets) == ((), (), (), ())
    assert r.record.outcome.outcome == "collateral"                   # the label change itself is a list difference


def test_a_base_only_label_without_an_entry_produces_nothing():
    r = run("P1", TABS, (*TABS, "Futures"))
    assert (r.unregistered, r.removed, r.subsets, r.proposals) == ((), (), (), ())


# -- validate_all and the selector --------------------------------------------------------------

def test_validate_all_runs_every_probe_and_aggregates():
    ls = lists(P1=(("Monitor", "Trade", "Futures", "Analyze", "Scan"), TABS))
    ls["P4"] = (listing(located=False), listing(side="b"))
    report = v.validate_all(ls, registry(), LOGIN, None, workspace_id=WS, persona=PERSONA)
    assert [p.probe for p in report.probes] == list(v.PROBES)
    assert [u.surface_id for u in report.unregistered] == ["unregistered.p1.futures"]
    assert report.health == ("validation incomplete for P4: landmark_missing (probe:P4) on candidate",)
    assert [r.checkpoint.checkpoint_id for r in report.records][:2] == [
        f"census/registry.validation-p1/{WS}", f"census/registry.validation-p2/{WS}"]
    assert report.records[-1].checkpoint.surface_id == "unregistered.p1.futures"
    assert "validation incomplete for P4" in report.control().detail


def test_validate_all_refuses_a_missing_probe():
    ls = lists()
    del ls["P5"]
    with pytest.raises(ValueError, match="P5"):
        v.validate_all(ls, registry(), LOGIN, None, workspace_id=WS, persona=PERSONA)


def test_surface_selector():
    cps = [cp("scan.tab"), cp("scan.tab", "flow"), cp("monitor.tab")]
    assert v.handles("surface:scan.tab") and not v.handles("surface:") and not v.handles("probe:P1")
    assert v.select_subset("surface:scan.tab", cps) == cps[:2]
    with pytest.raises(ValueError):
        v.select_subset("account_state", cps)


# -- the Nightly self-test (10.3) ---------------------------------------------------------------

def self_test(reg: dict | None = None, ls: dict | None = None, surface: str | None = "scan.tab", **kw):
    return v.self_test(reg or registry(), ls or lists(), surface, entitlements=LOGIN, workspace_id=WS,
                       persona=PERSONA, **kw)


def test_self_test_passes_with_exactly_one_unregistered_surface():
    r = self_test()
    assert r.passed and r.added == ("unregistered.p1.scan",) and r.record is None and r.banner is None
    assert r.control().passed and r.control().alert is None


def test_self_test_picks_a_surface_when_none_is_designated():
    r = self_test(surface=None)
    assert r.passed and r.surface == "analyze.subtab.risk-profile" and r.probe == "P2"


def test_self_test_ignores_surfaces_already_unregistered_on_the_main_build():
    r = self_test(ls=lists(P1=((*TABS, "Futures"), (*TABS, "Futures"))))
    assert r.passed and r.added == ("unregistered.p1.scan",)


def blind(listings, reg, entitlements, manifest, **kw) -> v.ValidationReport:
    """A validator that matches every label: it never finds an unregistered surface."""
    return v.ValidationReport(())


def doubled(listings, reg, entitlements, manifest, **kw) -> v.ValidationReport:
    """A validator that reports every unregistered surface twice."""
    report = v.validate_all(listings, reg, entitlements, manifest, **kw)
    extra = []
    for u in report.unregistered:
        sid = u.surface_id + "-twin"
        c = PlannedCheckpoint(f"census/{sid}/{WS}", sid, "census", 1, WS, PERSONA, "frozen")
        extra.append(v.Unregistered(u.probe, u.label, v.ValidationRecord(c, u.record.outcome)))
    twin = v.ProbeResult("P1", report.probes[0].record, tuple(extra))
    return v.ValidationReport((*report.probes, twin))


@pytest.mark.parametrize("validator, count", [(blind, 0), (doubled, 2)])
def test_self_test_fails_on_a_blind_or_noisy_validator(validator, count):
    r = self_test(validator=validator)
    assert not r.passed and len(r.added) == count
    rec = r.record
    assert rec.checkpoint.checkpoint_id == f"census/registry.validation-p1/{WS}"
    assert (rec.outcome.outcome, rec.outcome.reason) == ("harness_error", "validation_self_test")
    assert "registry validation is blind" in r.banner.lower()
    ctl = r.control()
    assert not ctl.passed and ctl.alert.rule == "harness_error"
    out = rec.to_outcome(RUN, detail="watchdog_event: wd-7; " + rec.outcome.detail)
    assert required_fields.errors(out) == []


def test_self_test_fails_when_the_removed_surface_is_not_listed():
    ls = lists(P1=(("Monitor", "Trade", "Analyze"), ("Monitor", "Trade", "Analyze")))
    r = self_test(ls=ls)
    assert not r.passed and r.added == ()


def test_self_test_fails_loudly_when_no_surface_can_be_removed():
    ls = {p: (listing(), listing(side="b")) for p in v.PROBES}
    r = self_test(ls=ls, surface=None)
    assert not r.passed and r.surface is None and r.probe == "P1"
    assert "no registered surface" in r.record.outcome.detail


def test_self_test_refuses_a_surface_it_cannot_remove():
    with pytest.raises(ValueError):
        self_test(surface="nope.tab")
    with pytest.raises(ValueError):
        self_test(surface="shell.tab-bar")
