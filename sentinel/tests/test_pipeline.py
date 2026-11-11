"""The simulated pipeline's parts: the synthetic app, the simulated Reviewer, the config abort and the case (b)
smoke flows the scope plans (sentinel.pipeline)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from sentinel import invariants as inv
from sentinel.demo import EXAMPLE, clean_scenario, config_invalid_scenario
from sentinel.diff.settle import settle
from sentinel.evaluation import settle_spec_for
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.reviewer import (SIMULATED_MODEL_ID, ModelReviewerAdapter, ReviewRequest, SimulatedReviewer,
                                        to_review, validate)
from sentinel.pipeline.run import PairRefused, run_pipeline
from sentinel.pipeline.sim import Observation, SimApp, SimBuild, observe
from sentinel.scope import PlannedCheckpoint, Rig, compute_scope
from sentinel.store import Store

AT = "charts.flexible-grid.active-trader-cell"
TODAY = date(2026, 11, 10)
S07 ="flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"


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
    spec = settle_spec_for(cfg, cp, regfile=reg, park=(472, 352), template=lambda i, p: None)   # 3.5
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
    """A broken reference is a run-start lint error (4.1): preflight G1, `config_invalid`, and the run-level proof of
    a run that started no shard (SD-61)."""
    sc = config_invalid_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert any("L-RGN-03" in e for e in r.lint_errors)
    text = (tmp_path / "pr-comment.md").read_text()
    assert "**Abort**" in text and "`config_invalid`" in text and "L-RGN-03" in text
    assert r.commit_status["state"] == "error"
    with Store(tmp_path / "sentinel.db") as store:
        run = store.run_outcome(sc.run_id)
        assert (run.status, run.reason, run.last_gate) == ("aborted_harness", "config_invalid", "provisioned")
        assert store.outcomes(sc.run_id) == []
        (proof,) = store.post_run_proofs(sc.run_id)
        assert (proof.shard_id, proof.verdict) == (None, "proven_no_session")
        assert store.run_summary(sc.run_id)["abort"]["rule_ids"] == ["L-RGN-03"]


def test_an_expired_mask_stops_applying_and_the_run_completes(tmp_path):
    """4.1, 4.8 L-MSK-02: at run start an expired mask is not `config_invalid`; it stops applying, run health lists it
    and the run stores a `mask_expired` finding (the date lies before the /MES roll, so nothing else fails)."""
    day = date(2026, 12, 8)
    sc = clean_scenario(day)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=day)
    assert r.status == "complete" and r.reason is None
    (f,) = [f for f in r.findings if f.kind == "mask_expired"]
    assert f.evidence["raw_values"]["record_id"] == "msk.ps.last-update-01" and f.checkpoint_id is None
    with Store(tmp_path / "sentinel.db") as store:
        health = store.run_summary(sc.run_id)["health"]
    assert health["expired"] == ["mask expired: msk.ps.last-update-01"]
    assert "mask expired: msk.ps.last-update-01" in (tmp_path / "pr-comment.md").read_text()


def test_a_mask_expiring_within_seven_days_warns_its_owner(tmp_path):
    """3.8: the Reporter warns the owner seven days before a mask expires (`expiring_soon` into the summary)."""
    day = date(2026, 12, 1)
    sc = clean_scenario(day)
    run_pipeline(EXAMPLE, sc, tmp_path, today=day)
    with Store(tmp_path / "sentinel.db") as store:
        expiring = store.run_summary(sc.run_id)["expiring"]
    assert expiring == ["mask msk.ps.last-update-01 expires in 4 days (2026-12-05T15:20:00-05:00); owner tos-monitor-ui"]
    assert "Masks expiring within 7 days" in (tmp_path / "pr-comment.md").read_text()


def test_a_manifest_that_fails_its_lint_is_treated_as_missing(tmp_path):
    """4.3: the run's manifest is linted on its own (`lint_manifest`); an error (here L-MAN-09, a candidate that is
    not the pair's) makes it missing for the run, and its lint lines go to the summary and the Intended section."""
    sc = clean_scenario(TODAY)
    cand = sc.candidate.build_id
    rel = f"manifests/{cand}.yaml"
    bad = sc.overlay[rel].replace(f"candidate: {cand}", "candidate: branch-1998.3.0-0000000")
    sc = replace(sc, overlay=dict(sc.overlay, **{rel: bad}))
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.status == "complete" and r.manifest is None
    with Store(tmp_path / "sentinel.db") as store:
        lines = store.run_summary(sc.run_id)["manifest_lint"]
    assert any(line.startswith("L-MAN-09 error") for line in lines)
    assert "L-MAN-09" in (tmp_path / "pr-comment.md").read_text()
    assert {o.outcome for o in r.outcomes if o.surface_id == "charts.flexible-grid.chart-cell"} != {"intended"}


def test_run_creation_refuses_a_bad_build_pair_and_writes_no_run(tmp_path):
    """4.2: `check_pair` at run creation; a refused pair is never queued (no run row), with its rule ids."""
    sc = clean_scenario(TODAY)
    cand = sc.candidate.build_id
    rel = f"builds/{cand}.yaml"
    sc = replace(sc, overlay=dict(sc.overlay, **{rel: sc.overlay[rel].replace("base_rule: merge_base_main",
                                                                              "base_rule: last_green_main")}))
    with pytest.raises(PairRefused) as exc:
        run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert exc.value.rule_ids == ["L-BLD-04"]
    assert not (tmp_path / "sentinel.db").exists()


def test_registry_invariant_exceptions_reach_the_checker_and_expired_ones_drop(tmp_path):
    """4.5 `invariant_exceptions` load with the surface's rules; at run start an expired one is dropped (L-SRF-11,
    `invariants.active_exceptions`) and the run writes its `invariant_exception_expired` finding."""
    from sentinel.pipeline.config import materialize
    rel = "registry/surfaces/charts/charts.flexible-grid.chart-cell.yaml"
    text = (EXAMPLE / rel).read_text() + (
        "invariant_exceptions:\n"
        "  - {invariant_id: INV-06, owner: tos-charts-ui, reason: the plot scrolls by design, expires_at: "
        "'2026-12-31T00:00:00Z'}\n"
        "  - {invariant_id: INV-04, owner: tos-charts-ui, reason: a legacy ellipsis, expires_at: "
        "'2026-11-01T00:00:00Z'}\n")
    root = materialize(EXAMPLE, {rel: text}, tmp_path / "cfg")
    rules = SentinelConfig.load(root).rules("charts.flexible-grid.chart-cell", "frozen")
    assert [x.invariant_id for x in rules.exceptions] == ["INV-06", "INV-04"]
    sc = replace(clean_scenario(TODAY), overlay=dict(clean_scenario(TODAY).overlay, **{rel: text}))
    r = run_pipeline(EXAMPLE, sc, tmp_path / "out", today=TODAY)
    assert r.status == "complete"
    (f,) = [f for f in r.findings if f.kind == "invariant_exception_expired"]
    assert f.evidence["raw_values"] == {"record_id": "INV-04", "scope": "charts.flexible-grid.chart-cell",
                                        "run_health": "invariant exception expired: INV-04"}


def test_a_quarantine_record_in_the_schema_shape_cuts_its_checkpoint(tmp_path):
    """10.5, 4.16 `quarantine_record`: a record names its checkpoint under `checkpoint` (or its flow under `flow`),
    and the scope cuts that checkpoint to `not_run (quarantine)` (2.11 common_filters)."""
    from sentinel.pipeline.config import materialize
    cid = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    text = ("schema_version: 1\nrecords:\n"
            f"  - {{checkpoint: {cid}, owner: tos-monitor-ui, ticket: TOS-4410, entered: 2026-11-02, reason: flake}}\n")
    assert SentinelConfig.load(materialize(EXAMPLE, {"quarantine.yaml": text}, tmp_path / "cfg")).quarantine == {cid}
    sc = replace(clean_scenario(TODAY), overlay=dict(clean_scenario(TODAY).overlay, **{"quarantine.yaml": text}))
    r = run_pipeline(EXAMPLE, sc, tmp_path / "out", today=TODAY)
    (o,) = [o for o in r.outcomes if o.checkpoint_id == cid]
    assert (o.outcome, o.reason) == ("not_run", "quarantine")


def test_a_scope_config_error_names_its_own_rule():
    """SD-01: a ScopeConfigError carries the rule its message names (the smoke cap is L-SRF-15, not L-SRF-10)."""
    from sentinel.scope import ScopeConfigError
    assert ScopeConfigError("41 surfaces carry smoke: true; the cap is 40 (2.2.4, L-SRF-15)").rule_ids == ("L-SRF-15",)
    assert ScopeConfigError("a run needs at least one rig").rule_ids == ()


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
