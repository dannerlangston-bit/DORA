"""One negative test per implemented lint rule: a copy of the example with one defect, and the
rule's id must fire (4.2 to 4.12, 4.15). Most defects are made in memory on a deep copy of the
loaded repository; defects that live in files (templates, hashes, stray text) use a copied tree."""
from __future__ import annotations

import copy
import shutil
from datetime import date
from pathlib import Path
from typing import Callable

import pytest
import yaml

from sentinel.lint import Repo, lint, lint_fingerprint, lint_fingerprint_pair, lint_fingerprint_rig_spec, lint_repo
from sentinel.lint import rules as R
from sentinel.lint import schemas
from sentinel.lint.repo import Doc
from sentinel.taxonomy import load

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)

STOCK = "registry/surfaces/trade/trade.order-entry.stock.yaml"
DIALOG = "registry/surfaces/dialog/dialog.order-confirmation.yaml"
LADDER = "registry/surfaces/trade/trade.active-trader.ladder.yaml"
ATCELL = "registry/surfaces/charts/charts.flexible-grid.active-trader-cell.yaml"
CHART = "registry/surfaces/charts/charts.flexible-grid.chart-cell.yaml"
LEVEL2 = "registry/surfaces/charts/charts.flexible-grid.level2-cell.yaml"
PS = "registry/surfaces/monitor/monitor.activity.position-statement.yaml"
DEPMAP = "registry/dependency-map.yaml"
LADDERS = "registry/ladders.yaml"
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
READBACK = "flows/tier-1/flow.trade.active-trader.read-back.yaml"
CANARY = "flows/tier-1/flow.canary.ticket-stock.yaml"
RENAMES = "flows/RENAMES.yaml"
S06 = "regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06.yaml"
S07 = "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml"
PS_CENSUS = "regions/census/monitor.activity.position-statement/ws.tier-a.active-trader.yaml"
CHART_CENSUS = "regions/census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader.yaml"
MASK = "masks/census/monitor.activity.position-statement/ws.tier-a.active-trader/msk.ps.last-update-01.yaml"
MANIFEST = "manifests/branch-1998.3.0-5d3e812.yaml"
BRANCH_BUILD = "builds/branch-1998.3.0-5d3e812.yaml"
MAIN_BUILD = "builds/main-1998.2.0-4f1c9a2.yaml"
FIXTURE = "fixtures/fx.rig-01.paper.yaml"
ALLOWLIST = "gateway/allowlist.yaml"
SETTINGS = "profiles/profile.app-settings.sentinel-pinned.yaml"
POPUPS = "popups/catalog.yaml"
MUTANTS = "canaries/mutants.yaml"
BRANCHES = "phase0/branches.yaml"
EXPLORER = "policy/explorer_budget.yaml"
QUARANTINE = "quarantine.yaml"
WS_AT = "corpus/workspaces/ws.tier-a.active-trader/entry.yaml"
WS_OPT = "corpus/workspaces/ws.tier-a.options-trader/entry.yaml"
WS_BEG = "corpus/workspaces/ws.tier-a.beginner/entry.yaml"
WS_C = "corpus/workspaces/ws.tier-c.sweep-harness/entry.yaml"


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def step(flow: dict, sid: str) -> dict:
    return next(s for sec in ("normalize", "preconditions", "steps", "teardown") for s in flow.get(sec, []) if s["id"] == sid)


def mutant(cat: list, mid: str) -> dict:
    return next(m for m in cat if m["mutant_id"] == mid)


def popup(cat: list | dict, pid: str) -> dict:
    return next(p for p in entries(cat) if p["popup_id"] == pid)


def entries(cat: list | dict) -> list:
    """The catalog's entries: the example is in the `{schema_version, entries}` form (SD-47)."""
    return cat["entries"] if isinstance(cat, dict) else cat


def region(rf: dict, rid: str) -> dict:
    return next(r for r in rf["regions"] if r["id"] == rid)


def edge(dm: dict, prim: str, surf: str) -> dict:
    return next(e for e in dm["edges"] if e["primitive"] == prim and e["surface"] == surf)


def add_released_builds(r: Repo) -> None:
    for i in range(5):
        bid = f"released-1997.{i}.0-00000{i}0"
        r.docs[f"builds/{bid}.yaml"] = Doc(f"builds/{bid}.yaml", "build", {"schema_version": 1, "build": {
            "build_id": bid, "channel": "released", "version_string": f"1997.{i}.0", "commit_sha": f"00000{i}0" + "a" * 33,
            "pipeline_run_id": "x", "artifact": {"uri": "x", "sha256": "sha256:" + "b" * 64}, "install_method": "artifact",
            "built_at": f"2026-0{i + 1}-01T00:00:00-05:00", "released_at": f"2026-0{i + 1}-02T00:00:00-05:00"}})


def many_smoke_surfaces(r: Repo) -> None:
    src = r.docs[LEVEL2].data
    for i in range(36):
        d = copy.deepcopy(src)
        d["id"] = f"charts.flexible-grid.extra-{i}"
        d["smoke"] = True
        rel = f"registry/surfaces/charts/{d['id']}.yaml"
        r.docs[rel] = Doc(rel, "surface", d)


# (rule, file to mutate, mutation) -- the mutation gets the parsed data (or the repo for file None)
MUTATIONS: list[tuple[str, str | None, Callable]] = [
    ("L-BLD-00", MAIN_BUILD, lambda d: d["build"].update(channel="nightly")),
    ("L-BLD-01", BRANCH_BUILD, lambda d: d["build"].update(build_id="branch-1998.3.0-0000000")),
    ("L-BLD-02", MAIN_BUILD, lambda d: d["build"]["artifact"].pop("sha256")),
    ("L-BLD-03", MAIN_BUILD, lambda d: d["build"].update(branch="feature/x")),
    ("L-BLD-04", BRANCH_BUILD, lambda d: d["build_pair"].update(base_rule="previous_main")),
    ("L-BLD-05", BRANCH_BUILD, lambda d: d["build_pair"].update(base="branch-1998.3.0-5d3e812")),
    ("L-BLD-06", MAIN_BUILD, lambda d: d["build"].update(released_at="2026-11-09T00:00:00-05:00")),
    ("L-MAN-00", MANIFEST, lambda d: d.update(source="email")),
    ("L-MAN-01", MANIFEST, lambda d: d["entries"][0]["declared_surfaces"].append("charts.unknown")),
    ("L-MAN-02", MANIFEST, lambda d: d["entries"][0]["touched_primitives"].append("Docking")),
    ("L-MAN-03", MANIFEST, lambda d: d["entries"][0].update(expected_visual=d["entries"][0]["summary"])),
    ("L-MAN-04", MANIFEST, lambda d: d["entries"][0].update(mock_image="manifests/mocks/missing.png")),
    ("L-MAN-05", MANIFEST, lambda d: d["entries"][0].update(tickets=["tos-48120"])),
    ("L-MAN-06", MANIFEST, lambda d: d.update(entries=[])),
    ("L-MAN-07", MANIFEST, lambda d: d["entries"][0].update(changed_paths=[], touched_primitives=["OrderTicketCore"])),
    ("L-MAN-08", MANIFEST, lambda d: d["entries"][0]["feature_flags"][0].update(name="Bad-Name")),
    ("L-DEP-00", DEPMAP, lambda d: d["edges"][0].update(source="guess")),
    ("L-DEP-02", DEPMAP, lambda d: d["edges"][1].update(surface="charts.nothing")),
    ("L-DEP-03", DEPMAP, lambda d: edge(d, "OrderTicketCore", "dialog.order-confirmation").update(confirmed_from_source=False)),
    ("L-DEP-04", DEPMAP, lambda d: d["edges"].append(copy.deepcopy(d["edges"][0]))),
    ("L-DEP-05", CHART, lambda d: d["primitives"].remove("FlexibleGrid")),
    ("L-DEP-06", DEPMAP, lambda d: d["edges"].remove(edge(d, "Theme", "*"))),
    ("L-DEP-07", DEPMAP, lambda d: d["edges"][0].update(last_verified="2026-01-01T00:00:00-05:00")),
    ("L-DEP-08", DEPMAP, lambda d: d["edges"].remove(edge(d, "FlexibleGrid", "trade.active-trader.ladder"))),
    ("L-SRF-00", STOCK, lambda d: d.update(tier=4)),
    ("L-SRF-01", LEVEL2, lambda d: d.update(id="charts.flexible-grid.level-two-cell")),
    ("L-SRF-02", PS, lambda d: d.update(anti_anchors=[])),
    ("L-SRF-03", CHART, lambda d: d.update(enumerable_by="P2")),
    ("L-SRF-04", DIALOG, lambda d: d["target_window"].pop("title_regex")),
    ("L-SRF-05", LADDER, lambda d: d["path"][-1]["anchor"].update(landmark="lm.ladder-buy-column-x")),
    ("L-SRF-06", PS, lambda d: d["controls"][0].update(template="registry/templates/monitor/missing.png")),
    ("L-SRF-07", PS, lambda d: d.update(roots=[])),
    ("L-SRF-08", PS, lambda d: d["census"]["workspaces"].append("ws.tier-a.futures-trader")),
    ("L-SRF-09", LADDER, lambda d: d["controls"].append(copy.deepcopy(d["controls"][0]))),
    ("L-SRF-10", CHART, lambda d: d["census"].update(workspaces=[])),
    ("L-SRF-11", PS, lambda d: d.update(invariant_exceptions=[{"invariant_id": "INV-06", "owner": "o", "reason": "r",
                                                               "expires_at": "2026-01-01T00:00:00Z"}])),
    ("L-SRF-13", LADDER, lambda d: d["splitters"][0].update(host="sidebar")),
    ("L-SRF-14", WS_AT, lambda d: d["gadgets_present"].remove("charts.flexible-grid.level2-cell")),
    ("L-SRF-15", None, many_smoke_surfaces),
    ("L-WSP-00", WS_OPT, lambda d: d.update(tier="D")),
    ("L-WSP-01", WS_OPT, lambda d: d.update(file_sha256="sha256:" + "0" * 64)),
    ("L-WSP-02", WS_OPT, lambda d: d.update(persona="swing_trader")),
    ("L-WSP-03", WS_BEG, lambda d: d.update(tier="B")),
    ("L-WSP-04", WS_C, lambda d: d["size_sweep_cells"][0].update(hosts="trade.order-entry.stock")),
    ("L-WSP-05", WS_OPT, lambda d: d["display_config"].update(scale_percent=125)),
    ("L-WSP-06", WS_AT, lambda d: d["active_trader_panels"].pop()),
    ("L-WSP-07", WS_OPT, lambda d: d["link_groups"][0].update(symbol_ref="tsla")),
    ("L-FLW-00", FLOW, lambda d: d.update(data_mode="live")),
    ("L-FLW-01", FLOW, lambda d: step(d, "s01").update(type="park_mouse")),
    ("L-FLW-02", FLOW, lambda d: step(d, "s06").pop("target_window")),
    ("L-FLW-03", FLOW, lambda d: step(d, "s06").update(checkpoint_id="flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s6")),
    ("L-FLW-04", FLOW, lambda d: step(d, "s03").update(regions="regions/flow/missing.yaml")),
    ("L-FLW-05", READBACK, lambda d: step(d, "cp-ladder").update(data_mode="masked_live")),
    ("L-FLW-06", FLOW, lambda d: step(d, "s04").update(value="pwd=Tr0ub4dor")),
    ("L-FLW-07", FLOW, lambda d: step(d, "open-confirm").update(order_stage=False)),
    ("L-FLW-08", CANARY, lambda d: d.update({"class": "paper_send_ok"})),
    ("L-FLW-09", FLOW, lambda d: d["steps"].insert(6, {"id": "d1", "type": "drag", "from": {"x": 1, "y": 1},
                                                       "to": {"x": 9, "y": 9}, "steps": 5, "hold_ms": 100})),
    ("L-FLW-10", FLOW, lambda d: d["budget"].update(max_duration_s=600)),
    ("L-FLW-11", FLOW, lambda d: step(d, "s06")["expect"][2].update(region="rgn.confirm.qty")),
    ("L-FLW-12", FLOW, lambda d: d["preconditions"].remove(step(d, "p3"))),
    ("L-FLW-13", READBACK, lambda d: d["steps"].insert(1, {"id": "x1", "type": "click_named",
                                                           "target": "trade.active-trader.ladder.buy-mkt-button"})),
    ("L-FLW-14", CANARY, lambda d: d.update(origin={"kind": "explorer"})),
    ("L-FLW-15", FLOW, lambda d: d["steps"].remove(step(d, "s03"))),
    ("L-FLW-16", CANARY, lambda d: d["teardown"].insert(1, {"id": "cp-after", "type": "checkpoint",
        "checkpoint_id": "flow/trade.order-entry.stock/flow.canary.ticket-stock/cp-after", "surface_id": "trade.order-entry.stock",
        "target_window": {"kind": "main"}, "data_mode": "frozen", "regions": "default"})),
    ("L-FLW-17", STOCK, lambda d: d["controls"][1].pop("ocr_text")),
    ("L-FLW-18", CANARY, lambda d: (d.update({"class": "explorer_candidate"}), d["budget"].update(max_steps=500))),
    ("L-FLW-19", None, lambda r: (r.docs[LADDER].data["controls"].append(
        {"id": "cancel-all", "template": "registry/templates/active-trader/flatten.png", "ocr_text": "Cancel All"}),
        r.docs[READBACK].data["steps"].insert(1, {"id": "x1", "type": "click_named", "target": "trade.active-trader.ladder.cancel-all"}))),
    ("L-RGN-00", S06, lambda d: d["regions"][0].update(landmark="lm.unknown")),
    ("L-RGN-01", S06, lambda d: d["regions"].append(copy.deepcopy(d["regions"][0]))),
    ("L-RGN-02", S06, lambda d: d["regions"].append({"id": "rgn.order-entry.cover", "type": "freeze", "landmark": "lm.order-entry-header",
                                                    "rect": {"dx": 60, "dy": 30, "w": 30, "h": 10}, "fixture_dependency": "x"})),
    ("L-RGN-03", S07, lambda d: region(d, "rgn.confirm.timestamp").pop("rule")),
    ("L-RGN-04", FLOW, lambda d: step(d, "s07")["expect"].pop()),
    ("L-RGN-05", S07, lambda d: region(d, "rgn.confirm.cost").update(floor_exception={
        "approved_by": "a", "reason": "r", "ticket": "TOS-1", "expires_at": "2026-01-01T00:00:00Z"})),
    ("L-RGN-07", S06, lambda d: d["regions"].append({"id": "rgn.order-entry.tooltip", "type": "mask", "landmark": "lm.order-entry-header",
                                                    "rect": {"dx": 0, "dy": 100, "w": 10, "h": 10}, "mask_ref": "msk.x"})),
    ("L-RGN-08", S07, lambda d: region(d, "rgn.confirm.qty").update(money_role="qty", panel="other")),
    ("L-MSK-00", MASK, lambda d: d.pop("reason")),
    ("L-MSK-01", MASK, lambda d: d.update(created_by="reviewer")),
    ("L-MSK-02", MASK, lambda d: d.update(expires_at="2026-11-01T00:00:00-05:00")),
    ("L-MSK-03", PS_CENSUS, lambda d: region(d, "rgn.ps.last-update").update(rect={"dx": 0, "dy": 166, "w": 600, "h": 60})),
    ("L-FIX-00", FIXTURE, lambda d: d["accounts"][0].update(kind="cash")),
    ("L-FIX-01", ALLOWLIST, lambda d: d["rigs"][0]["account_ids"].pop()),
    ("L-FIX-02", FIXTURE, lambda d: d["accounts"][1].update(role="golden")),
    ("L-FIX-03", FIXTURE, lambda d: d["symbols"][0].update(roll_date="2026-11-01")),
    ("L-FIX-04", FIXTURE, lambda d: d.update(login_ref="plain-text-login")),
    ("L-FIX-05", SETTINGS, lambda d: d["settings"].update(order_confirmation=False)),
    ("L-FIX-06", FIXTURE, lambda d: d["epoch"]["front_month"].pop("es")),
    ("L-FIX-07", FIXTURE, lambda d: d["churn_calendar"].pop(1)),
    ("L-FIX-09", FIXTURE, lambda d: d["positions"].pop()),
    ("L-FIX-10", FIXTURE, lambda d: d["positions"].append({"account_ref": "D-20481134", "symbol_ref": "aapl", "qty": 1,
                                                          "avg_price": 1.0, "seeded_by": "x", "seeded_at": "2026-10-06T10:15:00-04:00"})),
    ("L-POP-00", POPUPS, lambda d: entries(d)[0].update({"class": "nuisance"})),
    ("L-POP-01", POPUPS, lambda d: popup(d, "POP-16").update(popup_id="POP-99")),
    ("L-POP-02", POPUPS, lambda d: popup(d, "POP-07").update(key="enter")),
    ("L-POP-03", POPUPS, lambda d: popup(d, "POP-14").update({"class": "dismissable"})),
    ("L-POP-04", POPUPS, lambda d: popup(d, "POP-02").update(action="click")),
    ("L-POP-06", None, lambda r: (add_released_builds(r),
                                  popup(r.docs[POPUPS].data, "POP-08").update(last_seen_build="released-1997.0.0-0000000"))),
    ("L-MUT-00", MUTANTS, lambda d: d[0].update(mode="video")),
    ("L-MUT-01", MUTANTS, lambda d: mutant(d, "M03").update(region_ref="rgn.confirm.nothing")),
    ("L-MUT-02", MUTANTS, lambda d: mutant(d, "M01")["expected"].update(outcomes=["needs_human"])),
    ("L-MUT-04", MUTANTS, lambda d: mutant(d, "M26").update(canary_flow="flow.trade.order-ticket.stock.limit")),
    ("L-MUT-05", MUTANTS, lambda d: mutant(d, "M07")["expected"].update(invariant_id="INV-01")),
    ("L-MUT-06", MUTANTS, lambda d: mutant(d, "M08")["expected"].update(invariant_id="INV-99")),
    ("L-XREF-01", LEVEL2, lambda d: d["census"].update(enabled=False)),
    ("L-XREF-02", LEVEL2, lambda d: d.update(tier=1)),
    ("L-XREF-03", CHART_CENSUS, lambda d: d["regions"].append({"id": "rgn.chart.last", "type": "rule", "landmark": "lm.chart-cell-header",
                                                              "rect": {"dx": 0, "dy": 400, "w": 50, "h": 10},
                                                              "rule": {"field": "price", "must_change_within_s": 30}})),
    ("L-XREF-04", MUTANTS, lambda d: mutant(d, "M14").update(checkpoint_id="census/charts.flexible-grid.chart-cell/ws.tier-a.nobody")),
    ("L-XREF-06", MANIFEST, lambda d: d["entries"][0]["declared_surfaces"].append("charts.renamed-away")),
    ("L-XREF-11", QUARANTINE, lambda d: d["records"].append({"checkpoint": "flow/Bad Id", "owner": "o", "ticket": "TOS-1",
                                                            "entered": "2026-11-01", "reason": "flaky"})),
    ("L-XREF-12", MUTANTS, lambda d: d.remove(mutant(d, "M33"))),
    ("L-XREF-14", EXPLORER, lambda d: d.update(max_actions=500)),
    ("L-XREF-15", BRANCHES, lambda d: d.update(data_fixture="B")),
]


def _apply(base: Repo, rel: str | None, fn: Callable, phase: int = 0) -> set[str]:
    r = copy.deepcopy(base)
    fn(r if rel is None else r.docs[rel].data)
    return lint_repo(r, phase, TODAY).rules()


def test_every_checkable_rule_has_a_negative_test():
    covered = {rule for rule, _, _ in MUTATIONS} | {"L-DEP-01", "L-SRF-06", "L-WSP-01", "L-XREF-07", "L-XREF-08",
                                                    "L-XREF-09", "L-FPR-00", "L-FPR-01", "L-FPR-02", "L-FPR-03", "L-FPR-05"}
    checkable = {rid for rid, r in R.RULES.items() if not r.skipped} | {"L-FPR-02", "L-FPR-03", "L-FPR-05"}
    assert checkable - covered == set()


@pytest.mark.parametrize("rule,rel,fn", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_rule_fires(base, rule, rel, fn):
    assert rule in _apply(base, rel, fn)


def test_tbd_glob_warns_in_phase0_and_errors_after(base):
    def tbd(d: dict) -> None:
        d["primitives"][0]["source_packages"] = ["TBD.flexgrid.**"]
    for phase, sev in ((0, "warn"), (1, "error")):
        r = copy.deepcopy(base)
        tbd(r.docs[DEPMAP].data)
        found = [v for v in lint_repo(r, phase, TODAY).violations if v.rule == "L-DEP-01"]
        assert [v.severity for v in found] == [sev]


def test_unresolved_branch_warns_past_its_deadline_in_phase0_and_errors_after(base):
    """The coordinator's decision (4.15 row with 11.8 and R23): in Phase 0 an undecided record warns only once its
    deadline passed; from Phase 1 any undecided record is an error."""
    for phase, deadline, sevs in ((0, "2026-12-01", []), (0, "2026-11-01", ["warn"]), (1, "2026-12-01", ["error"])):
        r = copy.deepcopy(base)
        r.docs[BRANCHES].data["session_rule"] = {"branch": "undecided", "deadline": deadline}   # 4.16 branch_choice
        found = [v.severity for v in lint_repo(r, phase, TODAY).violations if v.rule == "L-XREF-15"]
        assert sorted(set(found)) == sevs, (phase, deadline, found)


def test_golden_list_is_warn_until_phase2(base):
    for phase, sev in ((1, "warn"), (2, "error")):
        r = copy.deepcopy(base)
        r.docs[FIXTURE].data["positions"].pop()
        assert [v.severity for v in lint_repo(r, phase, TODAY).violations if v.rule == "L-FIX-09"] == [sev]


def test_money_cell_rules(base):
    """L-RGN-04: a money cell is never rule or mask; L-MSK-03: no mask on a money cell (budget 0)."""
    r = copy.deepcopy(base)
    q = region(r.docs[S07].data, "rgn.confirm.qty")
    q.update(type="rule", rule={"field": "int"})
    found = {v.rule for v in lint_repo(r, 0, TODAY).violations if "rgn.confirm.qty" in v.message}
    assert "L-RGN-04" in found
    r = copy.deepcopy(base)
    region(r.docs[PS_CENSUS].data, "rgn.ps.last-update").update(rect={"dx": 0, "dy": 50, "w": 20, "h": 20})
    region(r.docs[PS_CENSUS].data, "rgn.ps.qty").update(money_cell=True)     # SD-28: the example's qty is no money cell
    assert any(v.rule == "L-MSK-03" and "money cell rgn.ps.qty" in v.message for v in lint_repo(r, 0, TODAY).violations)


def test_flow_checkpoint_fields_required(base):
    """Every checkpoint carries surface_id (existing and listed), target_window and data_mode (L-FLW-02)."""
    for change in ({"surface_id": "trade.unknown"}, {"surface_id": "monitor.activity.position-statement"}):
        r = copy.deepcopy(base)
        step(r.docs[FLOW].data, "s06").update(change)
        assert "L-FLW-02" in lint_repo(r, 0, TODAY).rules()
    for field in ("surface_id", "target_window", "data_mode"):
        r = copy.deepcopy(base)
        step(r.docs[FLOW].data, "s07").pop(field)
        assert any(v.rule == "L-FLW-02" and field in v.message for v in lint_repo(r, 0, TODAY).violations)


def test_renames_file(base):
    r = copy.deepcopy(base)
    r.docs[RENAMES].data["renames"][0]["old"] = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06"
    r.docs[RENAMES].data["renames"][0]["new"] = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s99"
    found = [v.message for v in lint_repo(r, 0, TODAY).violations if v.rule == "L-FLW-03"]
    assert len(found) == 2


def test_mutant_catalog_shape(base):
    r = copy.deepcopy(base)
    m = mutant(r.docs[MUTANTS].data, "M04")
    m["generator"]["seed"] = 42
    m["money_cell"] = False
    found = lint_repo(r, 0, TODAY).violations
    assert any(v.rule == "L-MUT-01" and "seed" in v.message for v in found)
    assert any(v.rule == "L-XREF-12" and "M04" in v.message for v in found)


def test_mask_budget_refuses_large_masks(base):
    r = copy.deepcopy(base)
    region(r.docs[PS_CENSUS].data, "rgn.ps.last-update").update(rect={"dx": 0, "dy": 166, "w": 600, "h": 60})
    found = [v for v in lint_repo(r, 0, TODAY).violations if v.rule == "L-MSK-03"]
    assert found and "position_statement panel on lm.position-statement-header is" in found[0].message
    assert found[0].severity == "error"


# -- file-level defects ----------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root



def test_xref_02_money_cell_only_where_an_order_panel_shows(tree):
    """SD-28: a tier-1 surface needs a money cell only when its regions show an order panel."""
    import yaml
    path = tree / "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/cp2.yaml"
    if not path.exists():
        path = next((tree / "regions/flow/dialog.order-confirmation").rglob("*.yaml"))
    doc = yaml.safe_load(path.read_text())
    for r in doc["regions"]:
        r.pop("money_cell", None)
        r.pop("money_role", None)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    hits = [v for v in lint(tree, today=TODAY).violations if v.rule == "L-XREF-02"]
    assert any("dialog.order-confirmation" in v.message and "order panel" in v.message for v in hits)
    assert not any("position-statement" in v.message and "money_cell" in v.message for v in hits)

def test_template_size_and_format(tree):
    from PIL import Image
    Image.new("RGB", (10, 10)).save(tree / "registry/templates/trade/qty-field.png", format="PNG")
    Image.new("RGB", (40, 20)).save(tree / "registry/templates/trade/price-field.png", format="JPEG")
    found = [v for v in lint(tree, today=TODAY).violations if v.rule == "L-SRF-06"]
    assert {v.path for v in found} == {"controls[1].template", "controls[2].template"}


def test_workspace_hash_mismatch(tree):
    (tree / "corpus/workspaces/ws.tier-a.beginner/workspace.sentinel-beginner").write_text("changed\n")
    assert "L-WSP-01" in lint(tree, today=TODAY).rules()


def test_secret_in_any_file(tree):
    p = tree / "policy/release_gate.yaml"
    line = len(p.read_text().splitlines()) + 1
    p.write_text(p.read_text() + "api_token: AbC9dEf1GhI2jKl3MnO4pQr5\n")
    (tree / "notes.md").write_text("password: hunter2\n")
    found = {(v.file, v.path) for v in lint(tree, today=TODAY).violations if v.rule == "L-XREF-07"}
    assert ("policy/release_gate.yaml", f"line {line}") in found and ("notes.md", "line 1") in found


def test_credential_pattern_spares_ids_and_vault_refs():
    assert R.credential_hits("login_ref: vault://sentinel/rigs/rig-01/tos-login") == []
    assert R.credential_hits("branch: feature/TOS-48120-flexgrid-chart-cell-padding") == []
    assert R.credential_hits("evidence: com.tos.desktop.trade.confirm.OrderConfirmationDialog2") == []
    assert R.credential_hits("type: secret, vault_ref: vault://x#password") == []
    assert R.credential_hits("secret: s3cr3t-value") and R.credential_hits("key: Zq8Lm2Xv9Rt4Bn7Kc1Wp5Yd")


def test_taxonomy_copy_must_match(tree):
    data = copy.deepcopy(load().data)
    (tree / "taxonomy.yaml").write_text(yaml.safe_dump(data))
    assert "L-XREF-08" not in lint(tree, today=TODAY).rules()
    data["checkpoint_outcomes"] = data["checkpoint_outcomes"][:-1]
    (tree / "taxonomy.yaml").write_text(yaml.safe_dump(data))
    assert "L-XREF-08" in lint(tree, today=TODAY).rules()


def test_schemas_must_regenerate(tree):
    schemas.write_schemas(tree / "schemas")
    assert "L-XREF-09" not in lint(tree, today=TODAY).rules()
    (tree / "schemas/mask.schema.json").write_text("{}")
    assert [v.file for v in lint(tree, today=TODAY).violations if v.rule == "L-XREF-09"] == ["schemas/mask.schema.json"]


def test_unreadable_yaml_is_a_schema_error(tree):
    (tree / "registry/surfaces/charts/charts.flexible-grid.chart-cell.yaml").write_text("id: [unclosed\n")
    assert "L-SRF-00" in lint(tree, today=TODAY).rules()


# -- fingerprint (4.12) ----------------------------------------------------------------------------

def _fingerprint() -> dict:
    h = "sha256:" + "c" * 64
    fp = {
        "schema_version": 2, "fingerprint_id": "", "run_id": "run-2026-11-09-pr-0931", "side": "candidate", "rig_id": "rig-01",
        "read_at": "2026-11-09T14:02:11-05:00",
        "match": {
            "build_id": "branch-1998.3.0-5d3e812", "jre_release": {"sha256": h, "java_version": "17.0.12"}, "vmoptions_hash": h,
            "java2d_pipeline": {"pinned": "software", "confirmed": "software"},
            "windows_build": {"current_build": "26100", "ubr": "2314"},
            "display": {"width": 1920, "height": 1080, "bits": 32, "frequency": 60, "scale_percent": 100, "dpi": 96,
                        "awareness": "PER_MONITOR_AWARE_V2", "monitors": 1},
            "cleartype": {"smoothing": True, "type": "FE_FONTSMOOTHINGCLEARTYPE", "gamma": 1800, "contrast": 1400, "orientation": "RGB"},
            "gpu": {"name": "Hyper-V Video", "driver_version": "10.0.26100.1"},
            "os_theme": {"apps_light": False, "system_light": False, "transparency": False, "animations": False},
            "app_theme": {"scheme": "dark", "font_size": "medium"}, "workspace_hash": {"ws.tier-a.futures-trader": h},
            "settings_profile_hash": h,
            "time_zone": {"os": "Eastern Standard Time", "app_display": "America/New_York", "chart": "America/New_York"},
            "keyboard": {"layout": "0x0409", "layout_count": 1, "toggle_keys": {"caps": False, "num": True, "scroll": False},
                         "locale": "en-US"},
            "data_epoch": {"epoch_id": h, "data_mode": "backend_fixture"}, "input_desktop": "Default",
            "harness": {"version": "1.4.2", "ocr_engine": "tesseract-5.3.4", "ocr_model": "eng-best-4.1", "capture_lib": "mss-9.0.1",
                        "settle_params_sha256": h, "diff_engine_params_sha256": h, "redaction_version": "r3",
                        "region_registry_version": "rr-12", "config_commit": "c41a0f2", "config_tarball_sha256": h,
                        "allowlist_sha256": h},
            "render_probe_hash": "e3b0c44298fc1c14"},
        "record": {"jar_listing_hash": h, "account": {"account_id_sha256": h, "login_sha256": h, "role": "empty"},
                   "rig": {"hostname": "tos-rig-01", "hypervisor": "hyperv", "hypervisor_version": "10.0.26100",
                           "snapshot_ids": ["snap-2026-10-28-base"], "vm_image_id": "tos-rig-win11-2026-10-28"},
                   "run_date": "2026-11-09"},
    }
    fp["fingerprint_id"] = R.fingerprint_id(fp)
    return fp


def test_fingerprint_record():
    fp = _fingerprint()
    assert lint_fingerprint(fp) == []
    fp["match"]["gpu"]["driver_version"] = "10.0.26100.2"
    assert [v.rule for v in lint_fingerprint(fp)] == ["L-FPR-01"]
    fp["match"]["extra"] = 1
    assert "L-FPR-00" in {v.rule for v in lint_fingerprint(fp)}


def test_fingerprint_field_set_matches_7_4(base, monkeypatch):
    monkeypatch.setattr(schemas, "FINGERPRINT_FIELDS_7_4", schemas.FINGERPRINT_FIELDS_7_4 + ("bounds_source",))
    assert "L-FPR-01" in lint_repo(copy.deepcopy(base), 0, TODAY).rules()


def test_fingerprint_rig_spec_and_pair():
    fp = _fingerprint()
    assert lint_fingerprint_rig_spec(fp, "sha256:" + "c" * 64) == []
    bad = copy.deepcopy(fp)
    bad["match"]["display"]["width"] = 1366
    assert [v.rule for v in lint_fingerprint_rig_spec(bad)] == ["L-FPR-02"]
    base = copy.deepcopy(fp)
    base["match"]["build_id"] = "main-1998.2.0-4f1c9a2"
    assert lint_fingerprint_pair(fp, base, {"candidate": "branch-1998.3.0-5d3e812", "base": "main-1998.2.0-4f1c9a2"}) == []
    base["match"]["cleartype"]["gamma"] = 2200
    base["match"]["render_probe_hash"] = "e3b0c44298fc1c15"
    assert {v.rule for v in lint_fingerprint_pair(fp, base)} == {"L-FPR-03", "L-FPR-05"}
    assert [v.rule for v in lint_fingerprint_pair(fp, fp, {"base": "main-1998.2.0-4f1c9a2"})] == ["L-FPR-03"]


@pytest.mark.parametrize("path,value", [
    ("cleartype.smoothing", False),
    ("cleartype.type", "FE_FONTSMOOTHINGSTANDARD"),
    ("display.awareness", "SYSTEM_AWARE"),
    ("display.bits", 16),
    ("java2d_pipeline.pinned", "d3d"),
    ("os_theme.transparency", True),
    ("os_theme.animations", True),
    ("keyboard.locale", "de-DE"),
    ("keyboard.toggle_keys.caps", True),
])
def test_fingerprint_rig_spec_covers_every_pinned_value(path, value):
    # 7.1 and 7.12 pins; java2d confirmation left "unknown" so the pinned pipeline alone is checked
    fp = _fingerprint()
    fp["match"]["java2d_pipeline"]["confirmed"] = "unknown"
    assert R.lint_fingerprint_rig_spec(fp) == []
    *parents, leaf = path.split(".")
    node = fp["match"]
    for p in parents:
        node = node[p]
    node[leaf] = value
    assert [(v.rule, v.path) for v in R.lint_fingerprint_rig_spec(fp)] == [("L-FPR-02", f"match.{path}")]


def test_fingerprint_rig_spec_by_rig_kind():
    fp = _fingerprint()
    d3d = copy.deepcopy(fp)
    d3d["match"]["java2d_pipeline"] = {"pinned": "d3d", "confirmed": "d3d"}
    assert R.lint_fingerprint_rig_spec(d3d, rig_kind="physical") == []
    assert [v.path for v in R.lint_fingerprint_rig_spec(fp, rig_kind="physical")] == ["match.java2d_pipeline.pinned"]
    fp["match"]["java2d_pipeline"]["confirmed"] = "opengl"
    assert [v.path for v in R.lint_fingerprint_rig_spec(fp)] == ["match.java2d_pipeline.confirmed"]
    with pytest.raises(ValueError, match="rig kind"):
        R.lint_fingerprint_rig_spec(fp, rig_kind="laptop")


def test_fingerprint_rig_spec_follows_the_declared_display_config():
    fp = _fingerprint()
    cfg = {"id": "cfg.1920x1080-125.light-large", "width": 1920, "height": 1080, "scale_percent": 125, "theme": "light",
           "font_size": "large", "work_area": {"w": 1536, "h": 807}}
    assert {v.path for v in R.lint_fingerprint_rig_spec(fp, display_config=cfg)} == {
        "match.display.scale_percent", "match.display.dpi", "match.app_theme.scheme", "match.app_theme.font_size"}
    fp["match"]["display"].update(scale_percent=125, dpi=120)
    fp["match"]["app_theme"] = {"scheme": "light", "font_size": "large"}
    assert R.lint_fingerprint_rig_spec(fp, display_config=cfg) == []
    assert {v.path for v in R.lint_fingerprint_rig_spec(fp)} == {"match.display.scale_percent", "match.display.dpi"}


def test_fingerprint_rig_spec_against_the_snapshot_manifest():
    fp = _fingerprint()
    manifest = copy.deepcopy(fp["match"])
    assert R.lint_fingerprint_rig_spec(fp, manifest=manifest) == []
    manifest["windows_build"]["ubr"] = "2400"
    manifest["cleartype"]["gamma"] = 2200
    manifest["gpu"]["driver_version"] = "10.0.26100.2"
    assert {v.path for v in R.lint_fingerprint_rig_spec(fp, manifest=manifest)} == {
        "match.windows_build.ubr", "match.cleartype.gamma", "match.gpu.driver_version"}
    del manifest["vmoptions_hash"]
    with pytest.raises(ValueError, match="vmoptions_hash"):
        R.lint_fingerprint_rig_spec(fp, manifest=manifest)


def test_fingerprint_differences_is_the_one_pair_comparison():
    m = _fingerprint()["match"]
    other = copy.deepcopy(m)
    other.update(build_id="main-1998.2.0-4f1c9a2", render_probe_hash="E3B0C44298FC1C14")
    assert R.fingerprint_differences(m, other) == []                       # build id aside; one 64-bit value
    other["gpu"] = {"name": "other", "driver_version": "1"}
    other["keyboard"] = {**m["keyboard"], "locale": "en-GB"}
    assert R.fingerprint_differences(m, other) == ["gpu", "keyboard"]
    assert R.fingerprint_differences(m, other, ["gpu", "display"]) == ["gpu"]
    with pytest.raises(ValueError, match="cleartype"):
        R.fingerprint_differences(m, {**other, "cleartype": None})
    with pytest.raises(ValueError):
        lint_fingerprint_pair({"match": m}, {"match": {k: v for k, v in m.items() if k != "gpu"}})


@pytest.mark.parametrize("rel,path,bad,rule", [
    (DEPMAP, ("edges", 0, "surface"), ["x"], "L-DEP-00"),
    (FLOW, ("steps", 1, "target"), {"a": 1}, "L-FLW-00"),
    (FIXTURE, ("accounts", 0, "account_id"), [], "L-FIX-00"),
    (MUTANTS, (0, "checkpoint_id"), [], "L-MUT-00"),
    (POPUPS, ("entries", 0, "popup_id"), {}, "L-POP-00"),
    (WS_AT, ("gadgets_present",), "trade.active-trader.ladder", "L-WSP-00"),
    (S07, ("regions", 0, "landmark"), [], "L-RGN-00"),
    (STOCK, ("parent",), [], "L-SRF-00"),
    (POPUPS, ("entries", 0, "on_abort", "reason"), 7, "L-POP-04"),
])
def test_malformed_values_do_not_crash(base, rel, path, bad, rule):
    """A list or map where a scalar belongs fails the schema; the rules still run (schemas.coerce_shapes)."""
    r = copy.deepcopy(base)
    cur = r.docs[rel].data
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = bad
    assert rule in lint_repo(r, 1, TODAY).rules()


def test_flw07_type_fixture_committing_with_enter_after_confirm_and_send(base):
    """6.2, L-FLW-07: a type_fixture step that commits with Enter while the order is staged presses
    Enter just as a `press: enter` step would."""
    def fn(d):
        i = next(k for k, st in enumerate(d["steps"]) if st.get("id") == "w-confirm")
        d["steps"].insert(i + 1, {"id": "t-enter", "type": "type_fixture", "target": "trade.order-entry.stock.qty-field",
                                  "fixture_ref": "fixture.order.qty", "commit": "enter"})
    assert "L-FLW-07" in _apply(base, FLOW, fn)


# -- w1 lint safety and correctness: pairs at run creation ------------------------------------------------------------

def _violations(base: Repo, rel: str | None, fn: Callable, phase: int = 0) -> list:
    r = copy.deepcopy(base)
    fn(r if rel is None else r.docs[rel].data)
    return lint_repo(r, phase, TODAY).violations


def test_check_pair_refuses_at_run_creation():
    """04-pair-refusal-at-creation (4.2): the pair record alone gives the rule ids, no repository needed."""
    builds = {"branch-1998.3.0-5d3e812": {"channel": "branch", "merge_base_sha": "4f1c9a2" + "e" * 33},
              "main-1998.2.0-4f1c9a2": {"channel": "main", "commit_sha": "4f1c9a2" + "e" * 33}}
    good = {"run_type": "pr", "candidate": "branch-1998.3.0-5d3e812", "base": "main-1998.2.0-4f1c9a2",
            "base_rule": "merge_base_main"}
    assert R.check_pair(good, builds) == []
    refused = R.check_pair({**good, "base_rule": "same_build"}, builds)
    assert [v.rule for v in refused] == ["L-BLD-04"] and refused[0].file == "build_pair"
    same = R.check_pair({**good, "base": good["candidate"]}, builds, "builds/x.yaml")
    assert "L-BLD-05" in {v.rule for v in same} and {v.file for v in same} == {"builds/x.yaml"}


# -- 04-lint-not-oracle-config: assertions the Oracle refuses --------------------------------------------------------

def _s07_expect(a: dict) -> Callable:
    return lambda d: step(d, "s07")["expect"].append(a)


ORACLE_PROBES = [
    ("L-FLW-11", "r03", {"kind": "rule", "rule_id": "R03", "inputs": {"bid": "rgn.confirm.price", "mark": "rgn.confirm.price",
                                                                      "ask": "rgn.confirm.price", "last": "rgn.confirm.price"}}),
    ("L-FLW-11", "r10", {"kind": "rule", "rule_id": "R10", "inputs": {"value": "rgn.confirm.cost"}}),
    ("L-FLW-11", "r04", {"kind": "rule", "rule_id": "R04", "inputs": {"count": "rgn.confirm.qty", "selector": 16}}),
    ("L-FLW-11", "r01", {"kind": "rule", "rule_id": "R01", "inputs": {"qty": "rgn.confirm.qty", "price": "rgn.confirm.price",
                                                                      "multiplier": 1.5, "total": "rgn.confirm.cost"}}),
    ("L-FLW-00", "eq", {"kind": "equals", "region": "rgn.confirm.side"}),
    ("L-FLW-00", "mt", {"kind": "matches", "region": "rgn.confirm.side"}),
    ("L-FLW-00", "ct", {"kind": "count", "region": "rgn.confirm.side"}),
    ("L-FLW-00", "ct2", {"kind": "count", "region": "rgn.confirm.side", "expected": "abc"}),
    ("L-FLW-00", "ex", {"kind": "export_equals", "region": "rgn.confirm.cost"}),
    ("L-FLW-00", "api", {"kind": "api_within", "region": "rgn.confirm.price"}),
    ("L-FLW-00", "src", {"kind": "equals", "region": "rgn.confirm.side", "expected": "BUY", "source": "api"}),
    ("L-FLW-00", "app_alive", {"kind": "present", "region": "rgn.confirm.side"}),
    ("L-FLW-00", "ref", {"kind": "equals", "region": "rgn.confirm.price", "value_ref": "fixture.prices.nope.value"}),
    ("L-FLW-00", "pat", {"kind": "matches", "region": "rgn.confirm.side", "pattern": "[0-9"}),
    ("L-FLW-00", "fmt", {"kind": "equals", "region": "rgn.confirm.qty", "format": "int", "expected": "abc"}),
    ("L-FLW-11", "qty", {"kind": "equals", "region": "rgn.confirm.qty", "expected": 100}),       # a second `qty` at s07
]


@pytest.mark.parametrize("rule,aid,body", ORACLE_PROBES, ids=[p[1] for p in ORACLE_PROBES])
def test_assertions_the_oracle_refuses_fail_the_lint(base, rule, aid, body):
    found = _violations(base, FLOW, _s07_expect({"id": aid, **body}))
    assert any(v.rule == rule and f"[{aid}]" in v.path for v in found), [v.line() for v in found]


def test_every_accepted_assertion_builds_in_the_oracle(base):
    """The property behind the probes: an assertion the lint accepts is one the Oracle's parser accepts. Every
    assertion of the example (which lints clean) builds, and so does a within_tol without tol (2.8.2 default)."""
    from sentinel.oracle.assertions import Assertion
    for doc in base.of("flow"):
        for st in R._Linter._seq(doc.data):
            expect = st.get("expect", []) if st.get("type") == "checkpoint" else []      # a teardown expect is a count
            for a in expect + st.get("assertions", []) + ([st["verify"]] if "verify" in st else []):
                Assertion.from_dict(a)
    tol = {"id": "tw", "kind": "within_tol", "region": "rgn.confirm.price", "format": "price",
           "value_ref": "fixture.prices.aapl_far_below.value"}
    assert not [v for v in _violations(base, FLOW, _s07_expect(tol)) if "[tw]" in v.path]
    Assertion.from_dict(tol)


# -- 04-mask-budget-panel-bypass --------------------------------------------------------------------------------------

def test_unlabeled_mask_over_a_panel_counts_toward_its_budget(base):
    """A 670x34 mask (15 percent of the s07 Order Confirmation bounds) with the default panel `other` is L-MSK-03."""
    clock = {"id": "rgn.confirm.clock", "type": "mask", "landmark": "lm.order-confirmation-title",
             "rect": {"dx": 0, "dy": 166, "w": 670, "h": 34}, "mask_ref": "msk.confirm.clock-01"}
    found = _violations(base, S07, lambda d: d["regions"].append(clock))
    assert any(v.rule == "L-MSK-03" and "order_confirmation panel" in v.message and "15.2%" in v.message for v in found)


def test_panel_share_counts_overlap_once_and_adds_a_proposed_mask():
    regs = [{"id": "rgn.a", "type": "diff", "landmark": "lm", "panel": "order_ticket", "rect": {"dx": 0, "dy": 0, "w": 50, "h": 100}},
            {"id": "rgn.b", "type": "diff", "landmark": "lm", "panel": "order_ticket", "rect": {"dx": 50, "dy": 0, "w": 50, "h": 100},
             "money_role": "qty", "money_cell": True},
            {"id": "rgn.m1", "type": "mask", "landmark": "lm", "rect": {"dx": 0, "dy": 0, "w": 10, "h": 80}, "mask_ref": "m1"},
            {"id": "rgn.m2", "type": "mask", "landmark": "lm", "rect": {"dx": 0, "dy": 0, "w": 10, "h": 80}, "mask_ref": "m2"}]
    budgets = {"order_ticket": 0.10}
    (share,) = R.panel_share(regs, budgets=budgets)
    assert (share.panel, share.landmark, share.share, share.exceeded) == ("order_ticket", "lm", 0.08, False)
    box = {"id": "rgn.redact.x", "landmark": "lm", "rect": {"dx": 5, "dy": 0, "w": 10, "h": 80}}
    assert R.panel_share(regs, [box], budgets=budgets)[0].share == pytest.approx(0.12)
    assert R.panel_share(regs, [{**box, "identity_field": True}], budgets=budgets)[0].share == pytest.approx(0.08)
    proposed = {"landmark": "lm", "rect": {"dx": 20, "dy": 0, "w": 10, "h": 50}}
    assert R.panel_share(regs, (), proposed, budgets)[0].share == pytest.approx(0.13)
    assert R.money_cells_covered(regs) == []
    assert R.money_cells_covered(regs, (), {"landmark": "lm", "rect": {"dx": 60, "dy": 10, "w": 5, "h": 5}}) == [
        ("rgn.b", "proposed mask")]


@pytest.fixture
def diff_params(tmp_path, monkeypatch):
    """A Diff Engine parameters file the lint reads its mask budgets from; restored after the test."""
    from sentinel import diff
    data = yaml.safe_load(diff.DEFAULT_PARAMS_PATH.read_text())

    def write(**budgets: float) -> None:
        data["mask_budget"].update(budgets)
        p = tmp_path / "diff_engine.yaml"
        p.write_text(yaml.safe_dump(data))
        monkeypatch.setenv("SENTINEL_DIFF_PARAMS", str(p))
        diff.load_params.cache_clear()

    yield write
    monkeypatch.delenv("SENTINEL_DIFF_PARAMS", raising=False)
    diff.load_params.cache_clear()


def test_mask_budget_comes_from_the_diff_engine_parameters(base, diff_params):
    def big(d: dict) -> None:
        region(d, "rgn.ps.last-update").update(rect={"dx": 0, "dy": 166, "w": 600, "h": 60})
    assert "L-MSK-03" in {v.rule for v in _violations(base, PS_CENSUS, big)}
    diff_params(position_statement=0.5)
    assert R.mask_budgets()["position_statement"] == 0.5
    assert "L-MSK-03" not in {v.rule for v in _violations(base, PS_CENSUS, big)}


# -- 04-regions-landmark-templates-unchecked --------------------------------------------------------------------------

@pytest.mark.parametrize("change,needle", [
    ({"template": "regions/landmarks/missing.png"}, "does not exist"),
    ({"confidence_min": 1.5}, "confidence_min"),
])
def test_regions_landmark_fields(base, change, needle):
    found = _violations(base, S07, lambda d: d["landmarks"][0].update(change))
    assert any(v.rule == "L-RGN-00" and needle in v.message and v.path.startswith("landmarks[0]") for v in found)


def test_regions_landmark_ids_unique_and_redaction_templates_checked(base):
    found = _violations(base, S07, lambda d: d["landmarks"].append(copy.deepcopy(d["landmarks"][0])))
    assert any(v.rule == "L-RGN-00" and "landmark id lm.order-confirmation-title is not unique" in v.message for v in found)
    found = _violations(base, "redaction/anchors.yaml", lambda d: d["landmarks"][0].update(template="regions/landmarks/nope.png"))
    assert any(v.rule == "L-MSK-03" and v.path == "landmarks[0].template" for v in found)


@pytest.mark.parametrize("size,fmt", [((800, 600), "PNG"), ((10, 10), "PNG"), ((150, 22), "JPEG")])
def test_regions_landmark_template_size_and_format(tree, size, fmt):
    from PIL import Image
    Image.new("RGB", size).save(tree / "regions/landmarks/lm.chart-cell-header.png", format=fmt)
    found = [v for v in lint(tree, today=TODAY).violations if v.rule == "L-RGN-00"]
    assert [(v.file, v.path) for v in found] == [(CHART_CENSUS, "landmarks[0].template")]


# -- 04-money-cell-closed-list (L-RGN-08) and 04-census-money-cell-unasserted (L-RGN-04 census half) --------------

def test_money_role_needs_money_cell_and_an_order_panel(base):
    found = _violations(base, S07, lambda d: region(d, "rgn.confirm.max-loss").update(money_role="max_loss", money_cell=False))
    assert any(v.rule == "L-RGN-08" and "without money_cell" in v.message for v in found)
    found = _violations(base, PS_CENSUS, lambda d: region(d, "rgn.ps.qty").update(money_role="qty"))
    assert any(v.rule == "L-RGN-08" and "panel position_statement" in v.message for v in found)
    assert not [v for v in _violations(base, S07, lambda d: region(d, "rgn.confirm.qty").update(money_role="qty"))
                if v.rule == "L-RGN-08"]


def test_census_money_cell_is_a_rule_region(base):
    """SD-29: on a census checkpoint every money cell of the closed list is a rule region, never diff-only."""
    cell = {"id": "rgn.ps.order-qty", "type": "diff", "landmark": "lm.position-statement-header", "panel": "order_ticket",
            "money_role": "qty", "money_cell": True, "rect": {"dx": 0, "dy": 300, "w": 60, "h": 20}}
    found = _violations(base, PS_CENSUS, lambda d: d["regions"].append(cell))
    assert any(v.rule == "L-RGN-04" and "rgn.ps.order-qty" in v.message and "census" in v.message for v in found)
    rule = {**cell, "type": "rule", "rule": {"field": "int"}}
    assert not [v for v in _violations(base, PS_CENSUS, lambda d: d["regions"].append(rule))
                if v.rule == "L-RGN-04" and "rgn.ps.order-qty" in v.message]


# -- SD-01, SD-27 -------------------------------------------------------------------------------------------------------

def test_tier1_does_not_imply_smoke(base):
    assert "L-SRF-02" not in {v.rule for v in _violations(base, PS, lambda d: d.update(smoke=False))}


def test_caret_mask_on_a_frozen_checkpoint_needs_keep_focus(base):
    caret = {"id": "rgn.order-entry.caret", "type": "mask", "landmark": "lm.order-entry-header",
             "rect": {"dx": 0, "dy": 100, "w": 4, "h": 14}, "mask_ref": "msk.x"}
    found = _violations(base, S06, lambda d: d["regions"].append(caret))
    assert any(v.rule == "L-RGN-07" and "keep_focus" in v.message for v in found)

    def focused(r: Repo) -> None:
        r.docs[S06].data["regions"].append(caret)
        step(r.docs[FLOW].data, "s06")["keep_focus"] = True
    assert "L-RGN-07" not in {v.rule for v in _violations(base, None, focused)}


# -- 04-cancel-confirm-by-jab-name ---------------------------------------------------------------------------------

def test_cancel_all_and_confirm_and_send_by_jab_name(base):
    def cancel_all(r: Repo) -> None:
        r.docs[LADDER].data["controls"].append({"id": "ca-btn", "template": "registry/templates/active-trader/flatten.png",
                                                "jab": {"name": "Cancel All", "role": "push button"}})
        r.docs[READBACK].data["steps"].insert(1, {"id": "x1", "type": "click_named", "target": "trade.active-trader.ladder.ca-btn"})
    assert "L-FLW-19" in {v.rule for v in _violations(base, None, cancel_all)}

    def confirm(r: Repo) -> None:
        r.docs[STOCK].data["controls"].append({"id": "cs-btn", "template": "registry/templates/trade/confirm-and-send.png",
                                               "jab": {"name": "Confirm and Send", "role": "push button"}})
        r.docs[CANARY].data["steps"].insert(4, {"id": "x1", "type": "click_named", "target": "trade.order-entry.stock.cs-btn"})
    assert any(v.rule == "L-FLW-07" and "x1" in v.message for v in _violations(base, None, confirm))


# -- 06-paper-send-ok-flow-lint --------------------------------------------------------------------------------------

SEND_POLICY = {"schema_version": 1, "decision": "approved", "enabled": True, "risk_owner": "risk.owner",
               "signed_at": "2026-11-01T00:00:00-05:00", "signature": "sig-risk-owner"}


def send_ok(r: Repo) -> None:
    """The stock limit flow as a conforming paper_send_ok flow under a signed send policy (6.7)."""
    r.docs["policy/send_policy.yaml"].data.clear()
    r.docs["policy/send_policy.yaml"].data.update(SEND_POLICY)
    r.docs[PS].data["controls"].append({"id": "cancel-order", "template": "registry/templates/active-trader/flatten.png",
                                        "ocr_text": "Cancel", "deny": True})
    fl = r.docs[FLOW].data
    fl["class"] = "paper_send_ok"
    step(fl, "s03")["expect"][0]["expected"] = 1
    step(fl, "s04")["value"] = "1"
    for cp in ("s06", "s07"):
        next(a for a in step(fl, cp)["expect"] if a["id"] == "qty")["expected"] = 1
    i = fl["steps"].index(step(fl, "s07"))
    fl["steps"][i + 1:i + 1] = [
        {"id": "send", "type": "click_named", "target": "dialog.order-confirmation.send-button"},
        {"id": "a-working", "type": "assert",
         "assertions": [{"id": "working_row", "kind": "matches", "region": "rgn.confirm.side", "pattern": "BUY \\+1 AAPL .* WORKING"}]},
        {"id": "cancel", "type": "click_named", "target": "monitor.activity.position-statement.cancel-order"},
    ]


def _send_rules(base: Repo, *changes: Callable) -> list:
    def fn(r: Repo) -> None:
        send_ok(r)
        for c in changes:
            c(r)
    return [v for v in _violations(base, None, fn) if v.rule in ("L-FLW-08", "L-FLW-13")]


def test_conforming_paper_send_ok_flow_lints_clean(base):
    assert _send_rules(base) == []


def test_paper_send_ok_send_click_is_denied_without_the_policy(base):
    found = _send_rules(base, lambda r: r.docs["policy/send_policy.yaml"].data.update(enabled=False))
    assert {v.rule for v in found} == {"L-FLW-08", "L-FLW-13"}


def _flow_step(r: Repo, sid: str) -> dict:
    return step(r.docs[FLOW].data, sid)


@pytest.mark.parametrize("name,change,needle", [
    ("qty", lambda r: _flow_step(r, "s04").update(value="2"), "order qty '2'"),
    ("market", lambda r: _flow_step(r, "s03")["expect"][1].update(expected="MKT"), "a limit order"),
    ("gtc", lambda r: _flow_step(r, "s03")["expect"][2].update(expected="GTC"), "DAY time in force"),
    ("near", lambda r: r.docs[FIXTURE].data["prices"][1].update(rule="last_delayed x 0.9, rounded to 0.01"), "at least 20 percent"),
    ("literal", lambda r: (_flow_step(r, "s05").pop("value_ref"), _flow_step(r, "s05").update(value="150.25")),
     "not a fixture.prices"),
    ("symbol", lambda r: (_flow_step(r, "s02").pop("value_ref"), _flow_step(r, "s02").update(value="TSLA")),
     "'TSLA' is not in fixture"),
    ("row", lambda r: r.docs[FLOW].data["steps"].remove(_flow_step(r, "a-working")), "Working row"),
    ("cancel", lambda r: r.docs[FLOW].data["steps"].remove(_flow_step(r, "cancel")), "no cancel step"),
    ("two-sends", lambda r: r.docs[FLOW].data["steps"].insert(
        r.docs[FLOW].data["steps"].index(_flow_step(r, "cancel")),
        {"id": "send2", "type": "click_named", "target": "dialog.order-confirmation.send-button"}), "exactly one Send click"),
    ("futures", lambda r: _flow_step(r, "s05").update(value_ref="fixture.prices.mes_far_below.value"), "tick_distance_futures"),
    ("teardown", lambda r: r.docs[FLOW].data["teardown"].remove(_flow_step(r, "t4")), "working_orders_check"),
])
def test_paper_send_ok_order_constraints(base, name, change, needle):
    found = _send_rules(base, change)
    assert any(v.rule == "L-FLW-08" and needle in v.message for v in found), [v.message for v in found]


def test_paper_send_ok_flows_stay_within_the_per_run_send_cap(base):
    def six(r: Repo) -> None:
        for i in range(5):
            rel = f"flows/tier-1/copy-{i}.yaml"
            r.docs[rel] = Doc(rel, "flow", copy.deepcopy(r.docs[FLOW].data))
    found = _send_rules(base, six)
    assert [(v.file, v.message) for v in found if v.file == "policy/send_policy.yaml"] == [
        ("policy/send_policy.yaml", "6 paper_send_ok flows send 6 orders; the per-run send cap is 5")]


# -- 04-flow-field-checks ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rule,rel,change,needle", [
    ("L-FLW-00", FLOW, lambda d: d["preconditions"].append({"id": "px", "type": "press", "key": "escape"}), "only type precondition"),
    ("L-FLW-06", FLOW, lambda d: d["steps"].insert(1, {"id": "sx", "type": "secret", "target": "trade.order-entry.stock.qty-field",
                                                        "vault_ref": "plain", "commit": "tab"}), "vault://<path>#<field>"),
    ("L-FLW-00", FLOW, lambda d: step(d, "t3")["assertions"][0].update(pattern="[0-9"), "does not compile"),
    ("L-FLW-00", FLOW, lambda d: step(d, "s07")["target_window"].update(title_regex="(^Order"), "does not compile"),
    ("L-FLW-00", FLOW, lambda d: step(d, "w-confirm")["anchor"].update(window_title_regex="(^Order"), "does not compile"),
    ("L-FLW-00", FLOW, lambda d: step(d, "w-ticket")["anchor"].update(landmark="lm.nope"), "wait_for landmark lm.nope"),
    ("L-RGN-00", S07, lambda d: region(d, "rgn.confirm.timestamp")["rule"].update(pattern="([0-9"), "rule pattern does not compile"),
    ("L-RGN-00", S07, lambda d: region(d, "rgn.confirm.timestamp")["rule"].update(min=5, max=1), "rule min 5 is above max 1"),
    ("L-RGN-00", PS_CENSUS, lambda d: region(d, "rgn.ps.mark")["rule"].update(must_advance=True), "must_advance"),
])
def test_flow_and_region_field_checks(base, rule, rel, change, needle):
    found = _violations(base, rel, change)
    assert any(v.rule == rule and needle in v.message for v in found), [v.line() for v in found]


# -- 04b: mutant seed, expired contracts, popup actions ---------------------------------------------------------------

def test_mutant_without_seed_is_l_mut_01(base):
    found = _violations(base, MUTANTS, lambda d: mutant(d, "M01")["generator"].pop("seed"))
    assert any(v.rule == "L-MUT-01" and "seed is missing" in v.message for v in found)


@pytest.mark.parametrize("symbol,roll,needle", [
    ("/MESZ24", "2027-01-01", "expired contract"),
    ("/MESZ26", "2027-03-01", "falls after the contract month 2026-12"),
])
def test_expired_or_late_rolled_contracts(base, symbol, roll, needle):
    found = _violations(base, FIXTURE, lambda d: d["symbols"][0].update(symbol=symbol, roll_date=roll))
    assert any(v.rule == "L-FIX-03" and needle in v.message for v in found)


@pytest.mark.parametrize("key", ["Control+Return", "ctrl + numpad enter", "Spacebar", " ", "return"])
def test_enter_key_spellings_are_l_pop_02(base, key):
    assert "L-POP-02" in {v.rule for v in _violations(base, POPUPS, lambda d: popup(d, "POP-07").update(key=key))}


def test_escape_is_a_dismiss_key():
    assert R._chord("Escape") == "escape" and R._chord("Shift+Ctrl+Tab") == "ctrl+shift+tab"


@pytest.mark.parametrize("pid,change,needle", [
    ("POP-07", lambda p: p.pop("key"), "action dismiss_key requires key"),
    ("POP-04", lambda p: p.update(action="wait"), "action wait requires wait_timeout_ms"),
    ("POP-04", lambda p: p.update(key="escape"), "key belongs to action dismiss_key, not abort"),
    ("POP-02", lambda p: p.update(wait_timeout_ms=100), "wait_timeout_ms belongs to action wait"),
])
def test_popup_action_fields(base, pid, change, needle):
    found = _violations(base, POPUPS, lambda d: change(popup(d, pid)))
    assert any(v.rule == "L-POP-04" and needle in v.message for v in found), [v.message for v in found]
