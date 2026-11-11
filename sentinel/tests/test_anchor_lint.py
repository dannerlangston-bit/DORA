"""The pre-run and CI anchor lint (11.5, 12.7 G1, 12.1, 12.2 `flow_maintenance_needed`), `sentinel.lint.anchors`."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from sentinel import synth
from sentinel.lint import anchors
from sentinel.records import CheckpointOutcome
from sentinel.store.artifacts import ArtifactStore, Redaction
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

SURFACE = "trade.order-entry.stock"
FLOW = "flow.trade.order-ticket.stock.limit"
CP = f"flow/{SURFACE}/{FLOW}/s03"
CENSUS = f"census/{SURFACE}/ws.tier-a.swing-trader"
TS = "2026-11-10T22:00:00+00:00"


def layout(*, trade_label: str = "Trade", second_trade: bool = False, header: bool = True) -> dict:
    tools = [{"type": "button", "name": "trade", "label": trade_label, "rect": [8, 4, 70, 22]},
             {"type": "button", "name": "analyze", "label": "Analyze", "rect": [84, 4, 80, 22]},
             {"type": "button", "name": "scan", "label": "Scan", "rect": [170, 4, 60, 22]}]
    if second_trade:
        tools.append({"type": "button", "name": "trade2", "label": trade_label, "rect": [300, 4, 70, 22]})
    ticket = {"type": "panel", "name": "ticket", "rect": [20, 60, 400, 200], "children": [
        {"type": "text", "name": "qty", "text": "Qty", "rect": [10, 10, 60, 16]},
        {"type": "button", "name": "confirm", "label": "Confirm and Send", "style": "primary",
         "rect": [200, 150, 160, 24]}]}
    if header:
        ticket["title"] = "Order Entry"
    return {"type": "window", "name": "main", "title": "Main@thinkorswim", "children": [
        {"type": "panel", "name": "toolbar", "rect": [0, 0, 640, 30], "children": tools}, ticket]}


def screen(**kw) -> synth.Screen:
    return synth.render(layout(**kw), (640, 400))


def crop(s: synth.Screen, name: str, w: int = 40, h: int = 20) -> np.ndarray:
    x, y, bw, bh = s.get(name).bbox
    cx, cy = x + bw // 2, y + bh // 2
    return np.ascontiguousarray(s.image[cy - h // 2:cy - h // 2 + h, cx - w // 2:cx - w // 2 + w])


@pytest.fixture
def base() -> synth.Screen:
    return screen()


@pytest.fixture
def templates(base):
    """The templates cut from the approved base, by repository-relative path."""
    x, y, w, h = base.get("ticket.title").bbox
    return {"registry/templates/trade/trade-tab.png": crop(base, "trade.label"),
            "registry/templates/trade/order-entry-header.png": base.image[y:y + h, x:x + 120].copy(),
            "regions/landmarks/lm.ticket-header.png": base.image[y:y + h, x:x + 120].copy()}


def surfaces() -> dict:
    return {SURFACE: {"id": SURFACE, "owner": "tos-trade-ui", "tier": 1,
                      "landmarks": [{"id": "lm.order-entry-header", "template":
                                     "registry/templates/trade/order-entry-header.png", "must_see": True}],
                      "controls": [{"id": "trade-tab", "template": "registry/templates/trade/trade-tab.png",
                                    "ocr_text": "Trade"}]}}


def flow() -> dict:
    return {"flow_id": FLOW, "owner": "tos-trade-ui", "tier": 1, "surfaces": [SURFACE], "steps": [
        {"id": "s01", "type": "reset_input_state"},
        {"id": "s02", "type": "click_named", "target": f"{SURFACE}.trade-tab"},
        {"id": "w-ticket", "type": "wait_for", "anchor": {"landmark": "lm.order-entry-header"}, "timeout_ms": 8000},
        {"id": "s03", "type": "checkpoint", "checkpoint_id": CP, "surface_id": SURFACE},
        {"id": "s04", "type": "click_named", "target": f"{SURFACE}.trade-tab"}],
        "teardown": [{"id": "t1", "type": "reset_input_state"}]}


def regions(base: synth.Screen) -> dict:
    x, y, _, _ = base.get("ticket.title").bbox
    return {CP: {"checkpoint_id": CP, "landmarks": [{"id": "lm.ticket-header", "template":
                                                      "regions/landmarks/lm.ticket-header.png",
                                                      "expected": {"x": x, "y": y}}], "regions": []}}


def lint(templates, capture, *, flows=None, regs=None, baselines=None) -> anchors.AnchorLintReport:
    return anchors.anchor_lint(flows if flows is not None else [flow()], regs if regs is not None else {},
                               baselines if baselines is not None else {CP: capture}, None, surfaces=surfaces(),
                               templates=templates.get)


def test_anchors_that_still_match_give_no_finding(base, templates):
    r = lint(templates, base.image, regs=regions(base))
    assert r.clean and r.checked == 4 and r.skipped == ()      # two control steps, one wait_for, one landmark
    assert r.health_rows() == [("Anchor lint", "clean (4 anchors checked)")]


def test_a_vanished_toolbar_label_is_one_maintenance_item_per_step_naming_flow_step_and_owner(base, templates):
    moved = screen(trade_label="Trades")                     # the toolbar redesign of 11.5 relabels the tab
    r = lint(templates, moved.image, regs=regions(base))
    assert [(m.kind, m.flow_id, m.step_id, m.cause) for m in r.misses] == [
        ("control", FLOW, "s02", "below_floor"), ("control", FLOW, "s04", "below_floor")]
    m = r.misses[0]
    assert (m.checkpoint_id, m.owner, m.tier, m.floor) == (CP, "tos-trade-ui", 1, 0.92) and m.score < 0.92
    assert m.crop_png is not None and cv2.imdecode(np.frombuffer(m.crop_png, np.uint8), 1).shape[:2] == (20, 40)
    f = r.findings("run-1", created_at=TS)[0]
    assert (f.kind, f.checkpoint_id, f.step_id, f.tier) == ("flow_maintenance_needed", CP, "s02", 1)
    assert f.evidence["artifacts"] == [m.crop_sha256] and f.evidence["cause"] == "below_floor"
    item = r.items(TS, run_id="run-1")[0]
    assert (item.kind, item.ref, item.owner, item.run_id) == ("anchor_lint", f"{FLOW}/s02", "tos-trade-ui", "run-1")
    label, text = r.health_rows()[0]
    assert label == "Anchor lint" and text.startswith("2 of 4 anchors no longer match the approved capture")
    assert f"{FLOW} s02: control {SURFACE}.trade-tab" in text and "owner tos-trade-ui" in text


def test_a_lint_miss_is_never_an_outcome_and_prelabels_the_steps_locator_failure(base, templates):
    r = lint(templates, screen(trade_label="Trades").image)
    assert not hasattr(r, "outcomes") and all(f.kind == "flow_maintenance_needed" for f in r.findings("run-1"))
    ev = {"step_id": "s02", "final_target": {"x": 40, "y": 40}}
    labeled = r.prelabel(ev, FLOW, "s02")
    assert labeled["maintenance"]["source"] == "anchor_lint" and labeled["maintenance"]["checkpoint_id"] == CP
    assert r.prelabel(ev, FLOW, "s99") == ev and "maintenance" not in ev


def test_a_missing_landmark_and_a_missing_wait_for_landmark_are_misses(base, templates):
    gone = screen(header=False)
    r = lint(templates, gone.image, regs=regions(base))
    kinds = {(m.kind, m.anchor, m.ref) for m in r.misses}
    assert ("landmark", "lm.ticket-header", f"{CP}#lm.ticket-header") in kinds
    assert ("wait_for", "lm.order-entry-header", f"{FLOW}/w-ticket") in kinds
    assert not any(m.kind == "control" for m in r.misses)


def test_a_label_that_is_no_longer_unique_misses_on_its_second_peak(base, templates):
    r = lint(templates, screen(second_trade=True).image)
    assert {m.cause for m in r.misses} == {"second_peak"} and len(r.misses) == 2
    assert "second peak" in r.misses[0].line()


def test_no_approved_capture_is_skipped_and_a_missing_template_is_a_miss(base, templates):
    r = lint(templates, base.image, baselines={})
    assert r.clean and r.checked == 0 and len(r.skipped) == 3
    assert r.skipped[0].why == f"no approved capture of {CP}"
    t = {k: v for k, v in templates.items() if "trade-tab" not in k}
    r = lint(t, base.image)
    assert [m.cause for m in r.misses] == ["template_missing", "template_missing"]


def test_a_step_takes_the_last_checkpoint_of_its_surface_before_it_else_the_first_after(base, templates):
    f = flow()
    cp2 = f"flow/{SURFACE}/{FLOW}/s05"
    f["steps"].append({"id": "s05", "type": "checkpoint", "checkpoint_id": cp2, "surface_id": SURFACE})
    f["steps"].append({"id": "s06", "type": "click_named", "target": f"{SURFACE}.trade-tab"})
    r = lint(templates, None, flows=[f], baselines={CP: base.image, cp2: screen(trade_label="Trades").image})
    assert [(m.step_id, m.checkpoint_id) for m in r.misses] == [("s06", cp2)]       # s02 and s04 use s03's capture
    other = dict(f, surfaces=["dialog.order-confirmation"])
    other["steps"] = [{"id": "s1", "type": "click_named", "target": f"{SURFACE}.trade-tab"}]
    r = lint(templates, base.image, flows=[other])
    assert r.checked == 0 and r.skipped[0].why == "no checkpoint of the anchor's surface in the flow"


def test_census_landmarks_belong_to_the_surface_owner(base, templates):
    regs = {CENSUS: dict(regions(base)[CP], checkpoint_id=CENSUS)}
    r = lint(templates, None, flows=[], regs=regs, baselines={CENSUS: screen(header=False).image})
    assert [(m.ref, m.owner, m.tier) for m in r.misses] == [(f"{CENSUS}#lm.ticket-header", "tos-trade-ui", 1)]


# -- the store side and `sentinel lint --anchors` ------------------------------------------------------------------

def _png(img: np.ndarray) -> bytes:
    return bytes(cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))[1])


def _config(tmp_path, base, templates):
    import yaml
    root = tmp_path / "cfg"
    for rel, img in templates.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(_png(img))
    (root / "registry/surfaces/trade").mkdir(parents=True)
    (root / f"registry/surfaces/trade/{SURFACE}.yaml").write_text(yaml.safe_dump(surfaces()[SURFACE]))
    (root / "flows/tier-1").mkdir(parents=True)
    (root / f"flows/tier-1/{FLOW}.yaml").write_text(yaml.safe_dump(flow()))
    (root / "regions/flow").mkdir(parents=True)
    (root / "regions/flow/s03.yaml").write_text(yaml.safe_dump(regions(base)[CP]))
    return root


def _store_with_baseline(tmp_path, image: np.ndarray) -> tuple[str, str]:
    db, arts = tmp_path / "central.db", tmp_path / "arts"
    with Store(db) as s:
        s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
        for b in ("b1", "b0"):
            s.register_build(b, channel="main", source_commit=b, pipeline_artifact_url="u", installer_sha256="0" * 64,
                             first_seen_at=TS)
        s.begin_run(RunPlan(run_id="r1", run_type="merge", config_commit="c", config_tarball_sha256="t",
                            snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                            scope=(ScopeEntry(CP, "ws.tier-a.swing-trader", "swing_trader", 1, "frozen",
                                              flow_class="standard"),),
                            shards=(ShardPlan("r1/1", "rig-01"),), pair_id=s.register_pair("b1", "b0", "merge"),
                            started_at=TS))
        s.write_checkpoint(CheckpointOutcome(run_id="r1", checkpoint_id=CP, outcome="identical",
                                             workspace_id="ws.tier-a.swing-trader", persona="swing_trader",
                                             data_mode="frozen", tier=1, flow_id=FLOW, step_id="s03"),
                           shard_id="r1/1")
        a = ArtifactStore.at(arts, "baselines/released/b1", require_local=False).put(
            _png(image), name="s03.png", kind="capture", media_type="image/png", redaction=Redaction("v1"))
        s.insert("artifacts", {"sha256": a.sha256, "kind": "capture", "media_type": "image/png", "bytes": a.bytes,
                               "redaction_passed": 1, "set_path": "baselines/released/b1", "first_run_id": "r1",
                               "created_at": TS})
        s.write_artifact_set(a.sha256, "baselines/released/b1", created_at=TS)
        s.insert("baselines", {"checkpoint_id": CP, "lineage": "released", "build_id": "b1", "image_sha256": a.sha256,
                               "state": "approved", "source_run_id": "r1", "proposed_at": TS, "approved_by": "alice",
                               "approved_at": TS, "approval_reason": "first approval"})
    return str(db), str(arts)


def test_the_cli_reads_the_approved_capture_from_the_store_and_exits_by_misses(tmp_path, base, templates, capsys):
    root = _config(tmp_path, base, templates)
    db, arts = _store_with_baseline(tmp_path, base.image)
    assert anchors.main(["--anchors", str(root), "--store", db, "--artifacts", arts]) == 0
    assert "0 anchor misses, 4 checked" in capsys.readouterr().err
    db2, arts2 = _store_with_baseline(tmp_path / "moved", screen(trade_label="Trades").image)
    assert anchors.main(["lint", "--anchors", str(root), "--store", db2, "--artifacts", arts2, "--json",
                         "--flow", FLOW]) == 1
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["file"] for x in lines] == [f"{FLOW}/s02", f"{FLOW}/s04"]
    with Store(db2) as s:                                     # the lint read the store and changed no outcome
        assert [tuple(r) for r in s.query("SELECT checkpoint_id, outcome FROM outcomes")] == [(CP, "identical")]
    assert anchors.main(["--anchors", str(tmp_path / "nowhere"), "--store", db, "--artifacts", arts]) == 2
