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


def popup(cat: list, pid: str) -> dict:
    return next(p for p in cat if p["popup_id"] == pid)


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
    ("L-POP-00", POPUPS, lambda d: d[0].update({"class": "nuisance"})),
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


def test_unresolved_branch_warns_in_phase0_and_errors_after(base):
    for phase, sev in ((0, "warn"), (1, "error")):
        r = copy.deepcopy(base)
        r.docs[BRANCHES].data["session_rule"] = "TBD"
        assert [v.severity for v in lint_repo(r, phase, TODAY).violations if v.rule == "L-XREF-15"] == [sev]


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
    assert found and "position_statement panel is" in found[0].message and found[0].severity == "error"


# -- file-level defects ----------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


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
    p.write_text(p.read_text() + "api_token: AbC9dEf1GhI2jKl3MnO4pQr5\n")
    (tree / "notes.md").write_text("password: hunter2\n")
    found = {(v.file, v.path) for v in lint(tree, today=TODAY).violations if v.rule == "L-XREF-07"}
    assert ("policy/release_gate.yaml", "line 5") in found and ("notes.md", "line 1") in found


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
            "app_theme": "dark", "workspace_hash": {"ws.tier-a.futures-trader": h}, "settings_profile_hash": h,
            "time_zone": {"os": "Eastern Standard Time", "app_display": "America/New_York", "chart": "America/New_York"},
            "keyboard": {"layout": "0x0409", "layout_count": 1, "toggle_keys": {"caps": False, "num": True, "scroll": False}},
            "data_epoch": h, "input_desktop": "Default",
            "harness": {"version": "1.4.2", "ocr_engine": "tesseract-5.3.4", "ocr_model": "eng-best-4.1", "capture_lib": "mss-9.0.1",
                        "settle_params_sha256": h, "diff_engine_params_sha256": h, "redaction_version": "r3",
                        "config_commit": "c41a0f2", "allowlist_sha256": h},
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


@pytest.mark.parametrize("rel,path,bad,rule", [
    (DEPMAP, ("edges", 0, "surface"), ["x"], "L-DEP-00"),
    (FLOW, ("steps", 1, "target"), {"a": 1}, "L-FLW-00"),
    (FIXTURE, ("accounts", 0, "account_id"), [], "L-FIX-00"),
    (MUTANTS, (0, "checkpoint_id"), [], "L-MUT-00"),
    (POPUPS, (0, "popup_id"), {}, "L-POP-00"),
    (WS_AT, ("gadgets_present",), "trade.active-trader.ladder", "L-WSP-00"),
    (S07, ("regions", 0, "landmark"), [], "L-RGN-00"),
    (STOCK, ("parent",), [], "L-SRF-00"),
    (POPUPS, (0, "on_abort", "reason"), 7, "L-POP-04"),
])
def test_malformed_values_do_not_crash(base, rel, path, bad, rule):
    """A list or map where a scalar belongs fails the schema; the rules still run (schemas.coerce_shapes)."""
    r = copy.deepcopy(base)
    cur = r.docs[rel].data
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = bad
    assert rule in lint_repo(r, 1, TODAY).rules()
