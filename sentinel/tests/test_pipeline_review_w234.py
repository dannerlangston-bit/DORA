"""Regression tests for the waves 2-4 review findings on the simulated run (sentinel.pipeline.run): frozen base
reuse reads the full capture (3.1, 7.9)."""
from __future__ import annotations

import dataclasses
from datetime import date

import sentinel.pipeline.run as pipeline_run
from sentinel.demo import EXAMPLE

TODAY = date(2026, 11, 10)
CHART = "charts.flexible-grid.chart-cell"


def test_a_reused_base_whose_source_outcome_was_listed_is_compared_against_the_full_capture(tmp_path):
    """pipeline-pr:0 (3.1, 7.9, 3.11 B): the merge run's main build carries a declared change, so its chart cells are
    `intended` and their outcome rows point at the evidence crop. The PR run on that main reuses the merge run's
    full redacted capture (`capture-candidate.png`), never the crop: the cells are compared (identical), name the
    source run and none is `needs_baseline (reuse_missing)`."""
    from sentinel import demo
    from sentinel.pipeline.sim import SimBuild
    merge, pr = demo.base_reuse_scenarios(TODAY)
    merge = dataclasses.replace(merge, candidate=SimBuild(demo.BASE_ID, frozenset({"chart-header-period"})),
                                merged=("branch-1998.3.0-a41c7e9",))
    m = pipeline_run.run_pipeline(EXAMPLE, merge, tmp_path, TODAY)
    listed = [o for o in m.outcomes if o.surface_id == CHART and o.test_kind == "census" and o.data_mode == "frozen"
              and o.outcome == "intended"]
    assert listed                                     # the source outcomes are listed: their artifacts are crops
    pr = dataclasses.replace(pr, base=SimBuild(demo.BASE_ID, frozenset({"chart-header-period"})))
    r = pipeline_run.run_pipeline(EXAMPLE, pr, tmp_path, TODAY)
    cells = {o.checkpoint_id: o for o in r.outcomes if o.surface_id == CHART}
    for o in listed:
        got = cells[o.checkpoint_id]
        assert (got.outcome, got.reason, got.base_source_run_id) == ("identical", None, m.run_id)
    assert not [o for o in r.outcomes if o.reason == "reuse_missing"]


def test_with_the_base_absent_a_candidate_invariant_violation_is_still_invariant_violation(tmp_path):
    """pipeline-pr:1 (3.11 row 7 before row 9, SD-07, 3.10): the Active Trader cell's x360 sweep steps fire INV-01 on
    the candidate. With their base not settling twice (input B absent, `base_not_settled`), the candidate's
    violations are confirmed by one re-capture and the steps stay `invariant_violation`, never `needs_baseline`."""
    from sentinel.demo import AT, flexgrid_scenario
    from sentinel.pipeline.sim import SimBuild
    steps = {f"sweep/{AT}/flexgrid/{w}x360" for w in (320, 480, 640, 960)}
    sc = flexgrid_scenario(TODAY)
    sc = dataclasses.replace(sc, base=SimBuild(sc.base.build_id, sc.base.changes, unsettled=frozenset(steps),
                                               minimums=sc.base.minimums))
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    got = {o.checkpoint_id: o for o in r.outcomes if o.checkpoint_id in steps}
    assert set(got) == steps
    for o in got.values():
        assert (o.outcome, o.reason, o.stability, o.recaptures) == ("invariant_violation", "INV-01", "stable", 1)


def _summary(out, run_id: str) -> dict:
    import json
    import sqlite3
    with sqlite3.connect(out / "sentinel.db") as c:
        return json.loads(c.execute("SELECT summary_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])


def _run_row(out, run_id: str) -> tuple:
    """The stored run outcome, its reason and the last gate (`summary_json.last_gate`, the store's own key)."""
    import sqlite3
    with sqlite3.connect(out / "sentinel.db") as c:
        return c.execute("SELECT outcome, abort_reason, json_extract(summary_json, '$.last_gate') FROM runs "
                         "WHERE run_id = ?", (run_id,)).fetchone()


def test_the_abort_block_comes_from_the_shard_whose_outcome_is_the_runs(tmp_path):
    """pipeline-pr:3 (8.2, 9.2): shard /1 ends aborted_harness (artifact_persist_failed) and a later shard ends
    aborted_safety (attempted_send). The run is aborted_safety, and its abort block, last gate and commit status come
    from the safety shard: trigger attempted_send, its gate and the incident id, never the harness abort's."""
    from sentinel.demo import attempted_send_scenario
    sc = attempted_send_scenario(TODAY)
    cp = "flow/trade.active-trader.ladder/flow.trade.active-trader.read-back/cp-ladder"
    sc = dataclasses.replace(sc, rig=dataclasses.replace(sc.rig, faults=(("artifact_persist", cp, "enospc"),)))
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "attempted_send")
    verdicts = {s["verdict"]: s for s in r.shards}
    assert {"aborted_harness", "aborted_safety"} <= set(verdicts)
    safety = verdicts["aborted_safety"]
    abort = _summary(tmp_path, r.run_id)["abort"]
    assert (abort["trigger"], abort["gate"], abort["incident"]) == ("attempted_send", safety["last_gate"], "1")
    assert _run_row(tmp_path, r.run_id) == ("aborted_safety", "attempted_send", safety["last_gate"])
    assert "incident 1" in r.commit_status["description"]


def test_a_ladder_step_not_reached_leaves_the_rest_of_its_ladder_not_run_step_failed(tmp_path, monkeypatch):
    """pipeline-pr:4 (2.5.1, 4.13 L-OUT-02, 1.10): the Active Trader cell's first flexgrid step measures 100 px short
    on both tries: it is `assertion_failed (assertion: size_reached)` and every later step of that ladder is stored
    `not_run (step_failed:<that step's checkpoint id>)`, never `harness_error (store_write_failed)` (a store
    failure that would hold the run back from complete)."""
    from sentinel.capture.sweep import SizeLadder
    from sentinel.demo import AT, flexgrid_scenario
    first = f"sweep/{AT}/flexgrid/960x720"

    class Short(SizeLadder):
        def measure(self, checkpoint_id, first_size, retry=None):
            if checkpoint_id == first:
                short = (first_size[0] - 100, first_size[1])
                return super().measure(checkpoint_id, short, short)
            return super().measure(checkpoint_id, first_size, retry)

    monkeypatch.setattr(pipeline_run, "SizeLadder", Short)
    r = pipeline_run.run_pipeline(EXAMPLE, flexgrid_scenario(TODAY), tmp_path, TODAY)
    ladder = [o for o in r.outcomes if o.test_kind == "sweep" and o.surface_id == AT and "/flexgrid/" in o.checkpoint_id]
    (failed,) = [o for o in ladder if o.checkpoint_id == first]
    assert (failed.outcome, failed.reason) == ("assertion_failed", "assertion:size_reached")
    rest = [o for o in ladder if o.checkpoint_id != first]
    assert rest and {(o.outcome, o.reason) for o in rest} == {("not_run", f"step_failed:{first}")}
    assert not [o for o in r.outcomes if o.reason == "store_write_failed"]


S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
S07_REGIONS = "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml"


def _nightly(out, *mutants: str, overlay=None, run_id: str = "run-2026-11-10-nightly-01", today: date = TODAY):
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.sim import SimBuild
    return pipeline_run.run_nightly(EXAMPLE, pipeline_run.NightlyScenario(
        "nightly", run_id, SimBuild(BASE_ID), overlay=dict([_main_build()], **(overlay or {})), mutants=mutants),
        out, today)


def _calibrate(out, *cids: str) -> None:
    """The weekend Calibration run on the simulated rig for `cids` (3.9): floors in the store."""
    import json
    from datetime import datetime, timezone

    from sentinel.calibration_run import CalibrationItem, CalibrationPlan, CalibrationShard, main
    from sentinel.pipeline.config import SentinelConfig
    cfg = SentinelConfig.load(EXAMPLE)
    plan = CalibrationPlan("cal-20261106", "main-1998.2.0-4f1c9a2",
                           (CalibrationShard("rig-01", tuple(CalibrationItem(pipeline_run.planned_for(cfg, c),
                                                                             "tos-trade-ui") for c in cids)),),
                           datetime(2026, 11, 7, 3, 15, tzinfo=timezone.utc),
                           datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc),
                           datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc))
    (out / "plan.json").write_text(json.dumps(plan.to_dict()))
    assert main([str(EXAMPLE), str(out / "plan.json"), "--out", str(out)]) == 0


def _spy_store_calibration(monkeypatch) -> dict[tuple[str, str], str]:
    """What every `StoreCalibration.lookup` the run makes answers, by (checkpoint, region)."""
    seen: dict[tuple[str, str], str] = {}

    class Spy(pipeline_run.StoreCalibration):
        def lookup(self, checkpoint_id, region_id):
            th, source = super().lookup(checkpoint_id, region_id)
            seen[(checkpoint_id, region_id)] = source
            return th, source
    monkeypatch.setattr(pipeline_run, "StoreCalibration", Spy)
    return seen


def test_the_nightly_aa_pass_judges_a_floor_the_live_config_invalidated_as_uncalibrated(tmp_path, monkeypatch):
    """calibration:3 (7.13, 10.6, SD-69): after the Calibration run measured S07, its cost region's rect is redrawn
    (a 7.13 Region Registry change). The Nightly A/A pass reads the floors under the live config, as a PR run does,
    so that region's floor is no longer valid: the region runs at threshold 0 (`uncalibrated`), never against the
    stale floor, while the untouched regions stay `calibrated`."""
    _calibrate(tmp_path, S07)
    seen = _spy_store_calibration(monkeypatch)
    text = (EXAMPLE / S07_REGIONS).read_text()
    old = "rgn.confirm.cost, type: diff, landmark: lm.order-confirmation-title, panel: order_confirmation, " \
          "rect: {dx: 24, dy: 96, w: 200, h: 20}"
    assert text.count(old) == 1
    _nightly(tmp_path, "M01", overlay={S07_REGIONS: text.replace(old, old.replace("w: 200", "w: 120"))})
    assert seen[(S07, "rgn.confirm.cost")] == "uncalibrated"
    assert seen[(S07, "rgn.confirm.title")] == "calibrated"


def test_a_nightly_on_a_rendering_key_with_no_carry_over_names_the_changed_field(tmp_path, monkeypatch):
    """pipeline-nightly:5 (10.6, 7.13): the store holds floors for the rig's old rendering key; tonight the GPU
    driver differs, which no floor carries over. The A/A pairs still run at threshold 0 with a Calibration queued,
    and the morning report and the request name the changed field (`gpu`), never just "no Calibration"."""
    from sentinel.orchestrator import simrig
    _calibrate(tmp_path, S07)
    monkeypatch.setitem(simrig.SIM_OS_READS, "gpu", {"name": "Hyper-V Video", "driver_version": "10.0.26100.9"})
    n = _nightly(tmp_path, "M01")
    health = n.canary["run_health"]["aa"]
    assert health["floors_from"] is None
    assert any(line.startswith("rendering key changed (gpu)") for line in n.canary["morning"])
    assert not any(line.startswith("no Calibration for this rendering key") for line in n.canary["morning"])


def test_the_nightly_canary_pass_judges_mutants_against_the_stores_floors(tmp_path):
    """pipeline-nightly:0 (10.3 Purpose, 5.15, 7.13): the Calibration run wrote floors for the Position Statement and
    the deployed floors now swallow the whole grid (raised in the store). The canary pass reads them as merge runs do
    (`StoreCalibration`), so M09 is absorbed (`noise`) and missed, never scored detected against floors the canary
    measured itself; its row's threshold version is the store's floors."""
    import sqlite3
    pos = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    _calibrate(tmp_path, pos)
    with sqlite3.connect(tmp_path / "sentinel.db") as c:
        c.execute("UPDATE calibration_floors SET threshold_changed_px = 1000000, threshold_component_area = 1000000, "
                  "threshold_shift_px = 1000, threshold_delta_e = 1000, diff_pixels_max = 1000000, "
                  "components_max = 1000 WHERE checkpoint_id = ?", (pos,))
    n = _nightly(tmp_path, "M09")
    (r,) = [r for r in n.canary["report"].results if r.mutant_id == "M09"]
    assert r.state != "detected" and r.produced_outcome == "noise"
    assert r.threshold_version and r.threshold_version.startswith("sha256:")


def test_a_diverged_path_equivalence_pair_keeps_its_aa_captures_and_outcome(tmp_path, monkeypatch):
    """pipeline-nightly:4 (SD-91, 10.6, 9.1, SD-09): the day's merge run pair re-run through the canary entry point
    diverges, and the same checkpoint drifted in tonight's A/A pass. The checkpoint's one `outcomes` row stays the
    `harness_error (canary_path_divergence)` row, and it now carries the A/A pair's two captures and its A/A outcome
    (`lower_results` and detail) instead of dropping them."""
    import sqlite3

    from sentinel.demo import clean_scenario
    from sentinel.store import Store
    real = pipeline_run.aa_pass.run

    def drifting(*a, **kw):
        r = real(*a, **kw)
        rows = tuple(dataclasses.replace(o, outcome="not_settled", reason="aa_drift", side="both", detail="regions: x")
                     for o in r.rows)
        return dataclasses.replace(r, rows=rows, drifted=tuple(o.checkpoint_id for o in rows))
    monkeypatch.setattr(pipeline_run.aa_pass, "run", drifting)
    day = date(2026, 11, 9)
    sc = clean_scenario(day, clock="01:10:00")
    merge = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, today=day)
    with sqlite3.connect(tmp_path / "sentinel.db") as c:
        c.execute("UPDATE runs SET run_type = 'merge' WHERE run_id = ?", (merge.run_id,))
    n = _nightly_for(tmp_path, day, sc)
    pe = n.canary["path_equivalence"]
    assert pe.diverged and pe.stored.checkpoint_id in n.canary["aa"].drifted
    cid = pe.stored.checkpoint_id
    with Store(tmp_path / "sentinel.db") as store:
        (o,) = [o for o in store.outcomes(n.run_id) if o.checkpoint_id == cid]
        stored = {r[0] for r in store.query("SELECT sha256 FROM artifacts")}
    assert (o.outcome, o.reason) == ("harness_error", "canary_path_divergence")
    assert o.lower_results == [{"outcome": "not_settled", "reason": "aa_drift", "detail": "regions: x"}]
    assert "A/A pass: not_settled (aa_drift)" in o.detail
    assert o.artifacts["base"] in stored and o.artifacts["candidate"] in stored


def _nightly_for(out, day: date, sc):
    """The Nightly of `day` on the example's main build, with the day's merge run candidate re-rendered as `sc`'s
    candidate build without its change (so the path-equivalence pair diverges)."""
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.sim import SimBuild
    return pipeline_run.run_nightly(EXAMPLE, pipeline_run.NightlyScenario(
        "nightly", f"run-{day.isoformat()}-nightly-01", SimBuild(BASE_ID), overlay=dict([_main_build()]),
        mutants=("M01",), merge_builds={sc.candidate.build_id: SimBuild(sc.candidate.build_id)}), out, day)


def _secret_hit_once(monkeypatch) -> None:
    """The run's first teardown scan finds a login's plaintext in a run row (6.9)."""
    from sentinel.store.secret_scan import SecretScanner, check_upload
    real, calls = pipeline_run._teardown_scan, []

    def scan(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return check_upload(SecretScanner({"x#password": "s3cret-value"}), b"... s3cret-value ...",
                                location="runs.summary")
        return real(*a, **kw)
    monkeypatch.setattr(pipeline_run, "_teardown_scan", scan)


def test_a_teardown_secret_hit_keeps_an_aborted_safety_runs_abort_block_and_gate(tmp_path, monkeypatch):
    """pipeline-pr:6 (8.2, 9.2, 6.9): an aborted_safety (attempted_send) run whose teardown scan finds a secret stays
    aborted_safety with its own last gate and abort block (trigger, gate, proof, incident, rig); the secret-scan facts
    are stored beside them and the report is withheld."""
    from sentinel.demo import attempted_send_scenario
    _secret_hit_once(monkeypatch)
    r = pipeline_run.run_pipeline(EXAMPLE, attempted_send_scenario(TODAY), tmp_path, TODAY)
    s = _summary(tmp_path, r.run_id)
    assert _run_row(tmp_path, r.run_id) == ("aborted_safety", "attempted_send", "running")
    abort = s["abort"]
    assert (abort["trigger"], abort["gate"], abort["incident"], abort["rig"]) == (
        "attempted_send", "running", "1", ["rig-03"])
    assert abort["proof"].startswith("PROVEN")
    assert s["secret_scan"]["result"] == "secret_leaked" and s["secret_scan"]["report_withheld"] is True


def test_a_teardown_secret_hit_on_a_complete_run_decides_its_abort_block(tmp_path, monkeypatch):
    """pipeline-pr:6: when the secret result is the run's outcome (a clean run), the abort block and last gate are
    the teardown scan's."""
    from sentinel.demo import clean_scenario
    _secret_hit_once(monkeypatch)
    r = pipeline_run.run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, TODAY)
    s = _summary(tmp_path, r.run_id)
    assert _run_row(tmp_path, r.run_id)[1:] == ("secret_leaked", "teardown")
    assert (s["abort"]["trigger"], s["abort"]["gate"]) == ("secret_leaked", "teardown")
