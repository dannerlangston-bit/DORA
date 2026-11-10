"""The simulated pipeline's parts: the synthetic app, the simulated Reviewer, the config abort and the case (b)
smoke flows the scope plans (sentinel.pipeline)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from sentinel import invariants as inv
from sentinel.demo import EXAMPLE, clean_scenario
from sentinel.diff.settle import settle
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.reviewer import (SIMULATED_MODEL_ID, ModelReviewerAdapter, ReviewRequest, SimulatedReviewer,
                                        to_review, validate)
from sentinel.pipeline.run import run_pipeline
from sentinel.pipeline.sim import Observation, SimApp, SimBuild, observe
from sentinel.scope import PlannedCheckpoint, Rig, compute_scope
from sentinel.store import Store

AT = "charts.flexible-grid.active-trader-cell"
S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"


@pytest.fixture(scope="module")
def cfg() -> SentinelConfig:
    return SentinelConfig.load(EXAMPLE)


def sweep(w: int, h: int) -> PlannedCheckpoint:
    return PlannedCheckpoint(f"sweep/{AT}/flexgrid/{w}x{h}", AT, "sweep", 1, "ws.tier-c.sweep-harness", "active_trader",
                             "frozen", size_step=(("host", "flexgrid"), ("w", w), ("h", h)))


def s07() -> PlannedCheckpoint:
    return PlannedCheckpoint(S07, "dialog.order-confirmation", "flow", 1, "ws.tier-a.swing-trader", "swing_trader",
                             "frozen", flow_id="flow.trade.order-ticket.stock.limit", step_id="s07")


def test_base_and_candidate_differ_only_by_the_declared_change(cfg):
    base, same = SimApp(SimBuild("b")), SimApp(SimBuild("c"))
    cand = SimApp(SimBuild("c", frozenset({"flexgrid-cell-chrome"})))
    cp = sweep(480, 480)
    assert (base.capture(cp, "base").screen.image == same.capture(cp, "candidate").screen.image).all()
    assert not (base.capture(cp, "base").screen.image == cand.capture(cp, "candidate").screen.image).all()
    assert (base.capture(s07(), "base").screen.image == cand.capture(s07(), "candidate").screen.image).all()
    with pytest.raises(ValueError):
        SimBuild("x", frozenset({"no-such-change"}))


def test_the_bounds_model_sees_the_1_11_overlap_only_at_height_360(cfg):
    ctl = cfg.control_ids(AT)
    rules = cfg.rules(AT, "frozen")
    base, cand = SimApp(SimBuild("b")), SimApp(SimBuild("c", frozenset({"flexgrid-cell-chrome"})))
    for h, fires in ((360, True), (480, False), (720, False)):
        b, c = base.capture(sweep(480, h), "base", controls=ctl), cand.capture(sweep(480, h), "candidate", controls=ctl)
        result = inv.compare(b.models, c.models, replace(rules, calibration_pending=frozenset({"INV-05"})))
        ids = [r.violation for r in result.reported if r.violation.invariant_id == "INV-01"]
        assert bool(ids) is fires
        if fires:
            assert ids[0].subject == "ladder.strip|ladder.viewport" and "480x14" in ids[0].detail
            assert not any(r.pre_existing for r in result.reported)


def test_the_order_confirmation_is_laid_out_to_the_config_regions(cfg):
    cp = s07()
    cap = SimApp(SimBuild("b")).capture(cp, "base")
    reg = SimApp.registry(cp, cap.screen, cap.info, cfg.regions[S07])
    doc = {r["id"]: r["rect"] for r in cfg.regions[S07]["regions"]}
    for r in reg.regions:
        assert (r.rect.x, r.rect.y, r.rect.w, r.rect.h) == (doc[r.id]["dx"], doc[r.id]["dy"], doc[r.id]["w"], doc[r.id]["h"])
    lm = reg.landmarks[0]
    bp = reg.region("rgn.confirm.bp-effect")
    x, y = lm.expected[0] + bp.rect.x, lm.expected[1] + bp.rect.y
    words = [w.text for w in cap.model.words if x <= w.bbox.center[0] < x + bp.rect.w and y <= w.bbox.center[1] < y + bp.rect.h]
    assert "-15,025.00" in words and bp.money_cell


def test_frames_settle_unless_the_build_keeps_them_moving(cfg):
    cp = sweep(480, 360)
    first = SimApp(SimBuild("b")).capture(cp, "base")
    reg = SimApp.registry(cp, first.screen, first.info, cfg.regions[f"sweep/{AT}/flexgrid/480x360"])
    spec = SimApp.settle_spec(reg, (472, 352))
    ok = settle(first.frames, spec)
    assert ok.settled and ok.time_to_settle_ms == 750 and ok.capture_t_ms == 250
    bad = settle(SimApp(SimBuild("b", unsettled=frozenset({cp.checkpoint_id}))).capture(cp, "base").frames, spec)
    assert not bad.settled and bad.reason == "frames_unstable"


def request(task="manifest_match", tickets=("TOS-1",), obs_tickets=frozenset({"TOS-1"}), changed=10):
    obs = (Observation("shifted", "panel starts 6 px higher than base", (0, 0, 10, 10), obs_tickets),) if changed else ()
    return ReviewRequest(task, ("census/charts.flexible-grid.chart-cell/ws.tier-a.beginner",),
                         "charts.flexible-grid.chart-cell", "rgn.chart.header", 2, "frozen", "ws.tier-a.beginner",
                         {"changed_px": changed}, obs, (0, 0, 100, 40),
                         entry={"entry_id": "e-1", "tickets": list(tickets)} if task != "describe_collateral" else None)


def test_the_simulated_reviewer_is_deterministic_and_answers_in_the_3_13_shape():
    rev = SimulatedReviewer()
    assert rev.model_id == SIMULATED_MODEL_ID
    match = rev.review(request())
    assert (match["verdict"], match["confidence"], match["matched_manifest_entry_id"]) == ("matches_manifest", 0.93, "e-1")
    assert rev.review(request()) == match and validate(match, request()) == []
    assert rev.review(request(obs_tickets=frozenset()))["verdict"] == "does_not_match_manifest"
    assert rev.review(request(changed=0))["verdict"] == "no_visible_difference"
    described = rev.review(request("describe_collateral"))
    assert described["verdict"] == "does_not_match_manifest" and described["description"].startswith("Panel starts")
    seen = rev.review(request("manifest_visibility"))
    assert (seen["verdict"], seen["visibility"]) == ("not_applicable", "visible")
    r = to_review(match, rev, "rgn.chart.header")
    assert (r.state, r.verdict, r.model_id, r.defect_class) == ("answered", "matches_manifest", SIMULATED_MODEL_ID, "shifted")


def test_client_side_checks_refuse_a_malformed_answer_and_no_model_is_ever_called():
    good = SimulatedReviewer().review(request())
    assert validate(dict(good, confidence=1.4), request())
    assert validate(dict(good, matched_manifest_entry_id="other"), request())
    assert validate(dict(good, visibility="visible"), request())
    assert validate({"verdict": "matches_manifest"}, request())
    with pytest.raises(RuntimeError):
        ModelReviewerAdapter("any-model", "p").review(request())
    assert ModelReviewerAdapter("m", "p", send=lambda payload: good).review(request()) == good


def test_observations_name_what_moved():
    b = SimApp(SimBuild("b")).capture(sweep(640, 480), "base").screen
    c = SimApp(SimBuild("c", frozenset({"flexgrid-cell-chrome"}))).capture(sweep(640, 480), "candidate").screen
    texts = [o.text for o in observe(b, c)]
    assert "cell header 28 to 22 px" in texts and "ladder viewport starts 6 px higher than base" in texts
    c360 = SimApp(SimBuild("c", frozenset({"flexgrid-cell-chrome"}))).capture(sweep(480, 360), "candidate").screen
    b360 = SimApp(SimBuild("b")).capture(sweep(480, 360), "base").screen
    assert any(o.change_type == "overlap" and "14 px" in o.text for o in observe(b360, c360))


def test_a_lint_error_aborts_the_run_config_invalid(tmp_path):
    sc = clean_scenario(date(2026, 12, 20))
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=date(2026, 12, 20))
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert any("L-MSK-02" in e for e in r.lint_errors)
    text = (tmp_path / "pr-comment.md").read_text()
    assert "**Abort**" in text and "`config_invalid`" in text and "L-MSK-02" in text
    assert r.commit_status["state"] == "error"
    with Store(tmp_path / "sentinel.db") as store:
        run = store.run_outcome(sc.run_id)
        assert (run.status, run.reason, run.last_gate) == ("aborted_harness", "config_invalid", "provisioned")
        assert store.outcomes(sc.run_id) == []


def test_case_b_adds_the_smoke_flows_only_when_the_change_scope_is_empty(cfg):
    """2.11: in case (b) the scope itself plans the tier-1 standard flows that touch the smoke census, so the
    Order Confirmation's money cells are captured; case (a) plans only what the change reaches."""
    from sentinel.scope import DependencyIndex
    common = dict(dep_map=cfg.dep_map, registry=cfg.registry, run_id="run-x", workspaces=cfg.workspaces,
                  flows=cfg.flows, entitlements=cfg.entitlements, rigs=[Rig("rig-01", "fp"), Rig("rig-02", "fp")])
    b = compute_scope("pr", None, dep_index=DependencyIndex({}), changed_classes=(), **common)
    assert b.manifest_state == "no_manifest_nonui" and not b.scope_surfaces
    flows = {cp.flow_id for cp in b.planned if cp.test_kind == "flow"}
    assert S07 in b.planned_ids and all(cfg.flow_docs[f]["class"] == "standard" for f in flows)
    a = compute_scope("pr", None, dep_index=DependencyIndex({}),
                      changed_classes=("com.tos.desktop.charts.grid.level2cell.Level2SizeRenderer",), **common)
    assert a.manifest_state == "no_manifest_ui" and S07 not in a.planned_ids
