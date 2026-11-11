"""The simulated pipeline's parts: the synthetic app, the simulated Reviewer, the config abort and the case (b)
smoke flows the scope plans (sentinel.pipeline)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from sentinel import invariants as inv
from sentinel.demo import EXAMPLE, clean_scenario, config_invalid_scenario
from sentinel.diff.settle import settle
from sentinel.evaluation import settle_spec_for
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.orchestration import RigScript
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
    # 10.5: the run-start mirror puts the record into flake_stats; the next run's config no longer names it, so the
    # mirror releases it, and with no Calibration run showing the exit rule the release is a run-health line
    with Store(tmp_path / "out" / "sentinel.db") as s:
        assert [tuple(x) for x in s.query("SELECT checkpoint_id, quarantined, quarantine_ticket_ref FROM flake_stats")
                ] == [(cid, 1, "TOS-4410")]
    r2 = run_pipeline(EXAMPLE, clean_scenario(TODAY, seq=2, clock="12:00:00"), tmp_path / "out", today=TODAY)
    with Store(tmp_path / "out" / "sentinel.db") as s:
        assert s.query("SELECT quarantined FROM flake_stats WHERE checkpoint_id = ?", (cid,))[0][0] == 0
        rows = json.loads(s.query("SELECT summary_json FROM runs WHERE run_id = ?", (r2.run_id,))[0][0])["health_rows"]
    assert ["Quarantine exits without evidence", cid] in rows


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


def test_a_rule_region_the_oracle_cannot_build_is_a_harness_error_not_a_crash(tmp_path, monkeypatch):
    """lint-oracle:4 (2.7, 2.8.2): the Invariant Checker's rule regions are built inside the OracleConfigError guard;
    a rule region the Oracle cannot evaluate makes the checkpoint `harness_error (exception:OracleConfigError)`."""
    from datetime import datetime, timezone

    import sentinel.pipeline.run as pipeline_run
    from sentinel.demo import BASE_ID, PS_CENSUS
    from sentinel.oracle.tables import OracleConfigError
    from sentinel.pipeline.config import materialize

    def broken(reg, placed):
        raise OracleConfigError("rgn.ps.mark: a rule the Oracle cannot evaluate")

    monkeypatch.setattr(pipeline_run, "invariant_rule_regions", broken)
    cfg = SentinelConfig.load(materialize(EXAMPLE, {}, tmp_path / "cfg"))
    cp = pipeline_run.planned_for(cfg, PS_CENSUS)
    run = pipeline_run._Run(cfg, "run-rule-guard", "pr", SimBuild(BASE_ID), SimBuild(BASE_ID),
                            datetime(2026, 11, 10, 15, 31, tzinfo=timezone.utc), None, None, lambda s: "undeclared")
    base, cand = run.capture(cp, "base"), run.capture(cp, "candidate")
    ev = pipeline_run.evaluate(run, cp, base, cand, x="undeclared", primitive=None, entry_ref=None,
                               recapture=lambda: cand)
    assert (ev.classified.outcome, ev.classified.reason) == ("harness_error", "exception:OracleConfigError")


# -- w3 integration: scope, shards, lockstep, coverage profiles, the map patch ---------------------------------------

PS_CENSUS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"


def _summary(out, run_id):
    with Store(out / "sentinel.db") as s:
        return json.loads(s.query("SELECT summary_json FROM runs WHERE run_id = ?", (run_id,))[0][0])


def test_the_shards_follow_the_scope_plan_and_each_rig_of_a_lockstep_pair_gets_one(tmp_path):
    """11.3, SD-14: the run's shards are `plan.shards`; the masked_live pair (rig-01, rig-02) is two shards, the
    candidate side running the pair's checkpoints and the base side standing in lockstep through every gate, its own
    paper-mode assertions before each unit and its own post-run proof."""
    from sentinel.pipeline.run import shard_specs
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    assert [(s["rig"], s["side"]) for s in r.shards] == [("rig-01", "candidate"), ("rig-02", "base"),
                                                         ("rig-03", "both"), ("rig-04", "both")]
    specs = shard_specs(r.run_id, r.plan)
    (cand,), (base,) = ([s for s in specs if s[4] == side] for side in ("candidate", "base"))
    assert [cp.checkpoint_id for cp in cand[3]] == [cp.checkpoint_id for cp in base[3]] == [PS_CENSUS]
    assert base[5] == cand[0]                               # the base side names its candidate side
    with Store(tmp_path / "sentinel.db") as s:
        sides = dict(s.query("SELECT shard_id, sides FROM shards WHERE run_id = ?", (r.run_id,)))
        proofs = {p.shard_id: p for p in s.post_run_proofs(r.run_id)}
    assert sides[cand[0]] == "candidate" and sides[base[0]] == "base" and len(proofs) == 4
    assert proofs[base[0]].paper_mode_assertions > 1        # asserted at the gate and before the pair's unit (6.4)


def test_a_lockstep_slip_is_requeued_once_and_a_second_slip_is_not_run_budget(tmp_path):
    """3.1: the pair's base rig captured 75 s after the candidate rig: the checkpoint re-runs once at the end of the
    shard and is compared; slipping again is `not_run (reason: budget)`, counted as lockstep_slip in run health."""
    once = replace(clean_scenario(TODAY), rig=RigScript(lockstep_slip_s=((PS_CENSUS, (75.0,)),)))
    r1 = run_pipeline(EXAMPLE, once, tmp_path / "once", today=TODAY)
    (o,) = [o for o in r1.outcomes if o.checkpoint_id == PS_CENSUS]
    assert o.outcome == "identical" and r1.status == "complete"
    rows = dict(map(tuple, _summary(tmp_path / "once", r1.run_id)["health_rows"]))
    assert rows["Lockstep"].startswith("lockstep_slip 0; 1 pairs re-queued once")
    twice = replace(clean_scenario(TODAY), rig=RigScript(lockstep_slip_s=((PS_CENSUS, (75.0, 80.0)),)))
    r2 = run_pipeline(EXAMPLE, twice, tmp_path / "twice", today=TODAY)
    (o,) = [o for o in r2.outcomes if o.checkpoint_id == PS_CENSUS]
    assert (o.outcome, o.reason, o.detail) == ("not_run", "budget", "lockstep_slip: 80 s apart twice")
    rows = dict(map(tuple, _summary(tmp_path / "twice", r2.run_id)["health_rows"]))
    assert rows["Lockstep"].startswith(f"lockstep_slip 1 ({PS_CENSUS}: not_run, budget)")


def test_each_record_counts_in_the_entitlement_profile_of_its_account(tmp_path):
    """2.12, SD-75: a flow runs on its `account_role` (empty: the IRA account), a census visit on the golden margin
    account; each record's `profile_id` is that account's kind, the matrix has one column per kind, and with no
    profile carrying paper_send the order-lifecycle surfaces are counted not_run (entitlement) (B01, 2.13)."""
    from sentinel.demo import wrong_number_scenario
    r = run_pipeline(EXAMPLE, wrong_number_scenario(TODAY), tmp_path, today=TODAY)
    assert {o.profile_id for o in r.outcomes if o.test_kind == "flow"} == {"ira"}
    assert {o.profile_id for o in r.outcomes if o.test_kind == "census"} == {"margin"}
    from sentinel.coverage import order_lifecycle_not_run
    cfg = SentinelConfig.load(EXAMPLE)
    cov = _summary(tmp_path, r.run_id)["coverage"]
    assert cov["matrix"]["profiles"] == 2 and [p.id for p in cfg.profiles] == ["margin", "ira"]
    assert cov["scope"]["order_surfaces_entitlement"] == len(order_lifecycle_not_run(cfg.registry, cfg.flows,
                                                                                      cfg.profiles))


def test_a_pr_run_writes_its_proposed_map_edges_as_the_map_prs_patch(tmp_path):
    """2.10.2, 4.4: a changed primitive class that reaches the Position Statement through the candidate's dependency
    index, with no map edge, is a proposed edge; the run writes the map PR's patch (`depscan.map_update(...).patch()`)
    into its output and the scope line names the file."""
    from sentinel.demo import GRID_SRC
    sc = replace(clean_scenario(TODAY), changed_paths=(f"{GRID_SRC}/FlexibleGridCell.java",),
                 depends_on={"com.tos.desktop.monitor.positions.PositionTable": ("com.tos.desktop.ui.grid.FlexibleGridCell",)})
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    name = f"dependency-map-patch-{r.run_id}.json"
    patch = json.loads((tmp_path / name).read_text())
    assert patch["build"] == sc.candidate.build_id
    assert [e["surface"] for e in patch["add"]] == ["monitor.activity.position-statement"]
    assert _summary(tmp_path, r.run_id)["coverage"]["scope"]["map_patch"] == name
    assert f"proposed map edges in {name}" in (tmp_path / "pr-comment.md").read_text()


def test_the_scope_takes_the_measured_durations_and_its_health_lines_reach_run_health(tmp_path):
    """2.11, 11.1: `Scenario.durations` (the 30-day per-side p50 by id) feeds `compute_scope`; a smoke census that
    takes 50 minutes cannot be cut, so its rigs overrun their capture budget and the scope's run-health line
    (`ScopePlan.health_lines`) names each of them."""
    sc = replace(clean_scenario(TODAY), durations={PS_CENSUS: 3000.0})
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.plan.projected_overrun_s and set(r.plan.projected_overrun_s) <= {"rig-01", "rig-02"}
    scope_rows = [t for k, t in _summary(tmp_path, r.run_id)["health_rows"] if k == "Scope"]
    assert scope_rows == r.plan.health_lines() and all(t.startswith("projected overrun on rig-0") for t in scope_rows)


def test_a_flow_unit_asserts_paper_mode_against_the_account_of_its_role(tmp_path):
    """SD-75, 6.3 A2: the Executor passes the flow's `account_role` account to `flow_boundary` and `step_boundary`; a
    flow on the golden account is asserted, and its records stamped, with the golden account, while the census
    visits keep the run's fixture (empty) account."""
    from sentinel.demo import wrong_number_scenario
    from sentinel.orchestrator.identity import account_id_sha256
    rel = "flows/tier-1/flow.trade.active-trader.read-back.yaml"
    text = (EXAMPLE / rel).read_text().replace("\nclass: ", "\naccount_role: golden\nclass: ", 1)
    sc = wrong_number_scenario(TODAY)
    sc = replace(sc, overlay=dict(sc.overlay, **{rel: text}))
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    cfg = SentinelConfig.load(EXAMPLE)
    golden, empty = (next(str(a["account_id"]) for a in cfg.fixture["accounts"] if a["role"] == role)
                     for role in ("golden", "empty"))
    flow = [o for o in r.outcomes if o.flow_id == "flow.trade.active-trader.read-back" and o.paper_assertion]
    census = [o for o in r.outcomes if o.test_kind == "census" and o.paper_assertion]
    assert flow and {o.paper_assertion["account_id_sha256"] for o in flow} == {account_id_sha256(golden)}
    assert census and {o.paper_assertion["account_id_sha256"] for o in census} == {account_id_sha256(empty)}
    assert {o.profile_id for o in flow} == {"margin"}         # the golden account is the margin account (2.12)


def test_the_logins_rate_counters_are_seeded_with_its_last_24_hours_from_the_store(tmp_path, monkeypatch):
    """6.4 rate caps across runs: each shard's `RateCounters` start with the login's logins and staged orders of the
    last 24 hours (`Store.login_rate_events`), so a second run on the same logins counts the first run's sessions."""
    import sentinel.pipeline.run as pipeline_run
    from sentinel.orchestrator.budgets import RateCounters
    seeded: list[list] = []                                         # each shard's logins as seeded

    class Recording(RateCounters):
        def __init__(self, **kw):
            super().__init__(**kw)
            seeded.append(list(self.logins))

    monkeypatch.setattr(pipeline_run, "RateCounters", Recording)
    run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    assert seeded and all(not logins for logins in seeded)          # a fresh store: nothing before this run
    seeded.clear()
    run_pipeline(EXAMPLE, clean_scenario(TODAY, seq=2, clock="11:30:00"), tmp_path, today=TODAY)
    assert seeded and all(len(logins) >= 1 for logins in seeded)    # the first run's session on each login
    with Store(tmp_path / "sentinel.db") as s:
        from sentinel.pipeline.orchestration import login_alias
        logins, orders = s.login_rate_events(login_alias("rig-01"), "2026-11-09T16:00:00+00:00")
        assert len(logins) >= 2 and orders == []                      # both runs' sessions; no order-stage steps
        assert s.login_rate_events(login_alias("rig-01"), "2026-11-11T00:00:00+00:00") == ([], [])


def test_a_pr_run_takes_the_detector_banner_and_canary_row_from_the_last_nightlys_state(tmp_path):
    """10.3, 9.2: PR runs build the DETECTOR banner and the Canary run-health row from `system_state.detector_state`
    (`canary.read_detector_state`), which the last Nightly wrote; the scenario passes neither."""
    from sentinel.canary import DetectorState
    banner = "DETECTOR BLIND since 2026-11-10 03:12 ET: money-cell canary M04 was not detected"
    health = {"money_detected": 3, "money_total": 4, "other_detected": 10, "other_total": 10,
              "detector_state": "detector_blind", "date": "2026-11-10", "since": "2026-11-10 03:12 ET"}
    state = DetectorState("detector_blind", "run-2026-11-10-nightly-02", "2026-11-10T08:12:00+00:00",
                          "2026-11-10 03:12 ET", banner, "detector_blind", health=health)
    with Store(tmp_path / "sentinel.db") as s:
        s.set_system_state("detector_state", state.to_json())
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    assert "DETECTOR BLIND" in r.comment and banner.split(":")[0] in r.comment
    assert _summary(tmp_path, r.run_id)["health"]["canary"]["detector_state"] == "detector_blind"
