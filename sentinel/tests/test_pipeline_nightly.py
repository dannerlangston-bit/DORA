"""The Nightly run's wiring (10.3, 10.5, 10.6, 3.9, 2.2.3 self-test, 9.10): mutants through the real paths, canary
scoring and persistence, the A/A pass, flake and quarantine, the registry self-test and calibration upkeep, each
end to end through `pipeline.run.run_nightly` on the example config."""
from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from sentinel.demo import BASE_ID, EXAMPLE, _main_build, blind_night_overlay, blind_overlay, clean_scenario
from sentinel.pipeline.reviewer import SimulatedReviewer
from sentinel.pipeline.run import NOT_SIMULATED, NightlyScenario, run_nightly, run_pipeline
from sentinel.pipeline.sim import SimBuild
from sentinel.store import Store

TODAY = date(2026, 11, 10)
S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"


def nightly(out: Path, *mutants: str, run_id: str = "run-2026-11-10-nightly-01", overlay=None, today: date = TODAY,
            reviewer=None, **knobs):
    return run_nightly(EXAMPLE, NightlyScenario("nightly", run_id, SimBuild(BASE_ID),
                                                overlay=dict([_main_build()], **(overlay or {})), mutants=mutants,
                                                **knobs), out, today, reviewer=reviewer)


def by_id(n):
    out: dict[str, list] = {}
    for r in n.canary["report"].results:
        out.setdefault(r.mutant_id, []).append(r)
    return out


@pytest.fixture(scope="module")
def night(tmp_path_factory):
    rev = SimulatedReviewer()
    n = nightly(tmp_path_factory.mktemp("night"), "M03", "M09", "M15", "M21", "M23", "M25", reviewer=rev)
    return n, rev


# -- item 1: mutants through the real paths (10.3 Injection) ----------------------------------------------------

def test_every_catalog_mutant_is_simulated():
    assert NOT_SIMULATED == {}


def test_the_oracle_and_the_declared_inputs_read_the_mutants_pixels(night):
    """M03 fails the Oracle's qty assertion on the mutated pixels; M15 fires INV-08 on a declared value region."""
    n, _ = night
    rows = by_id(n)
    (m03,), (m15,) = rows["M03"], rows["M15"]
    assert (m03.state, m03.produced_outcome, m03.produced_reason) == ("detected", "assertion_failed", "assertion:qty")
    assert (m15.state, m15.produced_outcome) == ("detected", "invariant_violation")
    assert "INV-08" in n.canary["report"].fired[f"M15@{m15.checkpoint_id}"]
    assert not n.canary["report"].unproven                  # every expected signal fired (12.3)


def test_the_gateway_mutant_runs_its_step_through_the_rigs_gateway(tmp_path, monkeypatch):
    """M28 (10.3, SD-44): the canary flow's step on the swapped Send template goes through `SimulatedRig.send_input`
    (slow half, then hook): the ambiguous name is refused, no click is emitted, and the mutant is detected; a gateway
    that emitted the click would miss it."""
    from sentinel.orchestrator import simrig
    sent = []
    real = simrig.SimulatedRig.send_input

    def spy(self, action, **kw):
        res = real(self, action, **kw)
        if action.flow_class == "canary" and action.under_point_text == "dneS dna mrifnoC":
            sent.append((action, res))
        return res
    monkeypatch.setattr(simrig.SimulatedRig, "send_input", spy)
    n = nightly(tmp_path / "a", "M28")
    (m28,) = by_id(n)["M28"]
    assert m28.state == "detected"
    ((action, res),) = sent
    assert action.ambiguous and res.refused is not None and res.refused.reason == "deny_profile" and res.events == ()
    from sentinel.orchestrator.gateway import HookDecision, InputEvent
    from sentinel.orchestrator.rig import EmitResult

    def broken(self, action, **kw):                       # a gateway that lets the click through
        if action.under_point_text == "dneS dna mrifnoC":
            return EmitResult(decisions=(HookDecision(True, token_id=1),),
                              events=(InputEvent("button_down", *action.point, button="left"),))
        return real(self, action, **kw)
    monkeypatch.setattr(simrig.SimulatedRig, "send_input", broken)
    n = nightly(tmp_path / "b", "M28", run_id="run-2026-11-10-nightly-02")
    assert by_id(n)["M28"][0].state != "detected"           # the click was emitted: M28 is missed


def test_the_global_shift_mutants_are_judged_over_their_batch(night):
    """SD-89: M21 and M23 are applied to every bound checkpoint of the night's batch; the pre-pass over the batch
    alone puts each member in the needs_human (global_shift) cluster, so both are detected."""
    n, _ = night
    rows = by_id(n)
    batch = {r.checkpoint_id for r in rows["M21"]}
    assert len(batch) > 1 and batch == {r.checkpoint_id for r in rows["M23"]}
    for mid in ("M21", "M23"):
        assert {(r.state, r.produced_outcome, r.produced_reason) for r in rows[mid]} == \
            {("detected", "needs_human", "global_shift")}


def test_mutants_go_through_the_reviewer_queue_one_by_one(night):
    """3.12, 10.3 Injection: a collateral mutant (M09) gets a describe_collateral call; the A/A control (M25,
    identical) none; each request names one checkpoint."""
    n, rev = night
    asked = [req for req, _ in rev.calls]
    assert asked and all(len(req.checkpoint_ids) == 1 for req in asked)
    m09 = next(r for r in by_id(n)["M09"])
    assert any(req.task == "describe_collateral" and req.checkpoint_ids == (m09.checkpoint_id,) for req in asked)
    assert all(req.checkpoint_ids != (S07,) or req.task != "manifest_match" for req in asked)


def test_the_oracle_catches_m04_under_the_mask_until_r02_is_dropped(tmp_path):
    """10.3, nightly-canary night 2: under the pasted mask the R02 assertion still reads the swapped digit; only
    without R02 does M04 end identical and the detector go blind."""
    masked = nightly(tmp_path / "masked", "M04", overlay=blind_overlay(EXAMPLE))
    (r,) = masked.canary["report"].results
    assert (r.state, r.produced_outcome, r.produced_reason) == ("detected", "assertion_failed", "assertion:R02")
    blind = nightly(tmp_path / "blind", "M04", overlay=blind_night_overlay(EXAMPLE))
    (r,) = blind.canary["report"].results
    assert (r.state, r.produced_outcome) == ("detector_blind", "identical")


# -- item 2: canary scoring and persistence (10.3 Scoring, Determinism, Path equivalence; 8.3; 9.9; 9.10) ----------

def test_the_detector_state_carries_across_nights_and_a_green_night_clears_it(tmp_path):
    """8.3 system_state.detector_state: a blind night sets it with its start time, a second blind night keeps that
    start, a green night clears it; every night's manifest is stored for the next night's determinism check, and
    the catalogs' hashes preflight G3 checked are on the run row."""
    blind = blind_night_overlay(EXAMPLE)
    n1 = nightly(tmp_path, "M04", run_id="run-2026-11-08-nightly-01", overlay=blind, today=date(2026, 11, 8))
    n2 = nightly(tmp_path, "M04", run_id="run-2026-11-09-nightly-01", overlay=blind, today=date(2026, 11, 9))
    assert n1.canary["detector"].state == n2.canary["detector"].state == "detector_blind"
    assert n2.canary["detector"].since == n1.canary["detector"].since
    assert n2.canary["detector"].banner.startswith("DETECTOR BLIND since 2026-11-08 03:12 ET.")
    n3 = nightly(tmp_path, "M04", run_id="run-2026-11-10-nightly-01")
    assert n3.canary["detector"].state == "ok" and n3.canary["detector"].banner is None
    with Store(tmp_path / "sentinel.db") as store:
        assert json.loads(store.get_system_state("detector_state"))["state"] == "ok"
        assert [r[0] for r in store.query("SELECT run_id FROM canary_manifests ORDER BY run_id")] == \
            [n1.run_id, n2.run_id, n3.run_id]
        versions = json.loads(store.query("SELECT catalog_versions_json FROM runs WHERE run_id = ?",
                                          (n3.run_id,))[0][0])
    assert {"mutants", "popups"} <= set(versions)
    alerts = json.loads((tmp_path / "alerts.json").read_text())
    assert [(a["run_id"], a["rule"], a["priority"]) for a in alerts                    # less the store check's
            if a["control"] not in ("fingerprint", "store_health")] == \
        [(n1.run_id, "canary_failure", "P1"), (n2.run_id, "canary_failure", "P1")]          # less G5's notices


def test_a_catalog_that_fails_the_g3_hash_runs_no_canary_and_aborts_the_run(tmp_path):
    """10.3, SD-88, 12.7 G3: every mutant of a catalog whose hash is not the one G3 recorded fails generation; with
    nothing generated and no behavioral flow the run is aborted_harness (canary_generation_failed), paged P1."""
    n = run_nightly(EXAMPLE, NightlyScenario("nightly", "run-2026-11-10-nightly-01", SimBuild(BASE_ID),
                                             overlay=dict([_main_build()]), mutants=("M01", "M04"),
                                             g3_catalog_sha256="0" * 64), tmp_path, TODAY)
    assert (n.status, n.reason) == ("aborted_harness", "canary_generation_failed")
    assert {r.state for r in n.canary["report"].results} == {"generation_failed"}
    assert n.canary["report"].no_canary_ran
    assert ("aborted_harness", "P1") in {(a["rule"], a["priority"]) for a in n.alerts}
    with Store(tmp_path / "sentinel.db") as store:
        assert {f.kind for f in store.findings(n.run_id)} == {"harness_error"}
        unreached = store.outcomes(n.run_id)            # the A/A scope carries the abort, with its Watchdog id
        assert unreached and {(o.outcome, o.reason) for o in unreached} == {("harness_error", "canary_generation_failed")}
        assert all(store.record_errors(o) == [] for o in unreached)
        assert tuple(store.query("SELECT outcome, abort_reason FROM runs WHERE run_id = ?", (n.run_id,))[0]) == \
            ("aborted_harness", "canary_generation_failed")


def _merge_from_clean_pr(out: Path):
    """The clean PR run stored as the day's merge run (the simulated pipeline runs no merge run of its own)."""
    sc = clean_scenario(TODAY, clock="01:10:00")
    pr = run_pipeline(EXAMPLE, sc, out, today=TODAY)
    with Store(out / "sentinel.db") as store:
        store.conn.execute("UPDATE runs SET run_type = 'merge' WHERE run_id = ?", (pr.run_id,))
        store.conn.commit()
    return sc, pr


def test_path_equivalence_reproduces_a_merge_run_pair(tmp_path):
    """10.3 Path equivalence: one real pair of the day's merge run, chosen by seed, re-run through the canary entry
    point reproduces exactly; the kind path_equivalence row is detected and the detector stays ok."""
    sc, pr = _merge_from_clean_pr(tmp_path)
    n = nightly(tmp_path, "M01", merge_builds={sc.candidate.build_id: sc.candidate, sc.base.build_id: sc.base})
    pe = n.canary["path_equivalence"]
    assert pe is not None and pe.stored.run_id == pr.run_id
    assert not pe.diverged, pe.differences
    assert (pe.row.kind, pe.row.state) == ("path_equivalence", "detected")
    assert n.canary["detector"].state == "ok" and n.status == "complete"


def test_a_diverged_pair_is_harness_error_and_raises_the_blind_banner(tmp_path):
    """10.3: a pair that does not reproduce is harness_error (canary_path_divergence) on its checkpoint, the
    detector-blind banner with the divergence line, and a P1 page of its own (9.10)."""
    sc, pr = _merge_from_clean_pr(tmp_path)
    n = nightly(tmp_path, "M01", merge_builds={sc.candidate.build_id: SimBuild(sc.candidate.build_id)})
    pe = n.canary["path_equivalence"]
    assert pe is not None and pe.diverged
    rows = [o for o in n.outcomes if o.reason == "canary_path_divergence"]
    assert [(o.checkpoint_id, o.outcome) for o in rows] == [(pe.stored.checkpoint_id, "harness_error")]
    ds = n.canary["detector"]
    assert ds.state == "detector_blind" and ds.divergence and "Path equivalence:" in ds.banner
    assert ("canary_path_divergence", "P1") in {(a["rule"], a["priority"]) for a in n.alerts}


# -- item 3: the A/A pass, flake statistics and quarantine (10.6, 3.9 step 7, 10.5, 2.8.1) ---------------------------

def test_the_aa_pass_writes_its_rows_in_scope_and_queues_a_calibration(tmp_path):
    """10.6: every A/A checkpoint plan_nightly selects is in the run's scope and gets a real outcome row (two captures
    of the main build in one session); with no Calibration floors for the rendering key in the store the pairs are
    diffed at threshold 0 and an emergency Calibration run is queued (7.13, 11.2). The rule checks' reads are stored
    (SD-09) and flake_stats is refreshed over the run's rows (10.5)."""
    n = nightly(tmp_path, "M01")
    aa = n.canary["aa"]
    assert n.status == "complete" and len(aa.rows) > 20
    assert {o.checkpoint_id for o in n.outcomes} == {r.checkpoint_id for r in aa.rows}
    assert {(o.outcome, o.synthetic) for o in n.outcomes} == {("identical", False)}
    with Store(tmp_path / "sentinel.db") as store:
        scope = {r[0] for r in store.query("SELECT checkpoint_id FROM run_scope WHERE run_id = ?", (n.run_id,))}
        summary = store.run_summary(n.run_id)
        extractions = store.extractions(n.run_id)
        stats = {r[0] for r in store.query("SELECT checkpoint_id FROM flake_stats")}
        assert all(store.record_errors(o) == [] for o in store.outcomes(n.run_id))   # L-OUT-02 (strict store)
    assert scope == {r.checkpoint_id for r in aa.rows}
    assert summary["calibration_queued"]["reason"] == "no_calibration"
    assert summary["calibration_queued"]["kind"] == "emergency"
    assert summary["health"]["aa"]["pairs"] == len(aa.rows) and not summary["health"]["aa"]["global"]
    assert any(x.checkpoint_id == S07 for x in extractions)
    assert scope <= stats
    assert any("Calibration queued" in line for line in n.canary["morning"])


def test_a_complete_quarantine_proposal_becomes_a_pr_payload_and_an_outbox_job(tmp_path, monkeypatch):
    """10.5, 9.1: a proposal with its owner and ticket is added to the quarantine.yaml patch, whose PR payload is
    stored as a canonical-JSON artifact with a queued outbox job of kind quarantine_pr; an exempt (drill or
    safety-canary) trigger is paged through the safety routing of 9.10 instead."""
    import sentinel.pipeline.run as pr
    from sentinel.quarantine import Evaluation, Proposal
    real = pr.quarantine.evaluate

    def evaluate(*a, **kw):
        real(*a, **kw)
        return Evaluation(
            (Proposal("checkpoint", S07, "aa_drift", TODAY, "not_settled (aa_drift) on 3 consecutive Nightly runs",
                      ("aa_drift",), owner="tos-trade-ui", ticket="SENT-101"),),
            (Proposal("flow", "flow.canary.ticket-stock", "fire_drill", TODAY, "drill item 3 not blocked",
                      ("fire_drill",)),))
    monkeypatch.setattr(pr.quarantine, "evaluate", evaluate)
    n = nightly(tmp_path, "M01")
    with Store(tmp_path / "sentinel.db") as store:
        (job,) = store.query("SELECT kind, destination, payload_sha256, state FROM outbox WHERE run_id = ? AND "
                             "kind = 'quarantine_pr'", (n.run_id,))   # beside the run's alert pages and posts (9.10)
        block = store.run_summary(n.run_id)["health"]["quarantine"]
    assert (job["kind"], job["destination"], job["state"]) == ("quarantine_pr", "sentinel-config/quarantine.yaml",
                                                               "queued")
    assert [r["checkpoint"] for r in block["added"]] == [S07] and block["paged"] == ["flow.canary.ticket-stock"]
    assert ("safety:drill_failure", "quarantine") in {(a["rule"], a["control"]) for a in n.alerts}
    sha = job["payload_sha256"].removeprefix("sha256:")         # the payload artifact is the job's hash (9.1)
    (blob,) = [p for p in (tmp_path / "artifacts").rglob(f"{sha}*") if p.is_file()]
    body = json.loads(blob.read_text())
    assert body["path"] == "quarantine.yaml" and body["records"][0]["ticket"] == "SENT-101"


# -- item 4: the registry validation self-test and its morning banner (10.3, 1.4 G1, 2.2.3) -------------------------

def test_the_registry_self_test_passes_on_the_main_build(night):
    """10.3: removing one surface from a registry copy gives exactly one unregistered_surface, its label."""
    n, _ = night
    st = n.canary["run_health"]["registry_self_test"]
    assert st["passed"] and st["surface"] and len(st["added"]) == 1 and st["banner"] is None
    assert not [o for o in n.outcomes if o.reason == "validation_self_test"]


def test_a_blind_registry_self_test_is_harness_error_with_the_morning_banner(tmp_path):
    """10.3, G1: when no probe can read its list the self-test removes nothing it can see: harness_error
    (validation_self_test) on the probe's census checkpoint, a P2 harness_error page, and the morning banner
    "registry validation is blind" until a self-test passes."""
    from sentinel.validation import PROBES
    n = nightly(tmp_path, "M01", probes_unread=PROBES)
    (row,) = [o for o in n.outcomes if o.reason == "validation_self_test"]
    assert (row.checkpoint_id, row.outcome) == ("census/registry.validation-p1/ws.tier-a.swing-trader", "harness_error")
    assert "watchdog_event:" in row.detail
    assert any(line.startswith("Registry validation is blind") for line in n.canary["morning"])
    assert ("harness_error", "validation_self_test") in {(a["rule"], a["control"]) for a in n.alerts}
    assert n.status == "complete"


# -- item 5: calibration upkeep in the Nightly (3.9, 7.13, 10.6) ------------------------------------------------------

def test_a_canary_miss_on_a_floor_excepted_region_revokes_the_exception(tmp_path):
    """3.9: "the canary catalog must still detect every mutant bound to it, else the exception is revoked": M04
    missed on the Buying Power Effect region, which carries a floor exception, writes the canary_miss finding that
    revokes it, named in run health and the morning report."""
    from sentinel.demo import S07 as S07_REGIONS
    overlay = blind_night_overlay(EXAMPLE)
    line = "panel: order_confirmation, rect: {dx: 24, dy: 120, w: 200, h: 20}}\n"
    assert overlay[S07_REGIONS].count(line) == 1
    overlay[S07_REGIONS] = overlay[S07_REGIONS].replace(line, (
        "panel: order_confirmation, rect: {dx: 24, dy: 120, w: 200, h: 20}, floor_exception: {approved_by: jdoe, "
        "approved_at: '2026-11-01T10:00:00-05:00', owner: jdoe, reason: quote flash, ticket: TOS-48300, "
        "measured_floor: 6, expires_at: '2026-11-30T10:00:00-05:00'}}\n"))
    n = nightly(tmp_path, "M04", overlay=overlay)
    (r,) = n.canary["report"].results
    assert r.state == "detector_blind"
    with Store(tmp_path / "sentinel.db") as store:
        found = [f for f in store.findings(n.run_id) if (f.evidence or {}).get("revokes_floor_exception")]
    assert [(f.kind, f.checkpoint_id, f.region_id) for f in found] == [("canary_miss", S07, "rgn.confirm.bp-effect")]
    assert any("floor exception revoked" in line and "TOS-48300" in line for line in n.canary["morning"])


def _calibrate(out: Path, *cids: str) -> None:
    """`sentinel calibrate` on the simulated rig for `cids` (the weekend Calibration run, 3.9): floors in the store."""
    from sentinel.calibration_run import CalibrationItem, CalibrationPlan, CalibrationShard, main
    from datetime import datetime, timezone
    from sentinel.pipeline.config import SentinelConfig
    from sentinel.pipeline.run import planned_for
    cfg = SentinelConfig.load(EXAMPLE)
    plan = CalibrationPlan("cal-20261106", "main-1998.2.0-4f1c9a2",
                           (CalibrationShard("rig-01", tuple(CalibrationItem(planned_for(cfg, c), "tos-trade-ui")
                                                             for c in cids)),),
                           datetime(2026, 11, 7, 3, 15, tzinfo=timezone.utc),
                           datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc),
                           datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc))
    (out / "plan.json").write_text(json.dumps(plan.to_dict()))
    assert main([str(EXAMPLE), str(out / "plan.json"), "--out", str(out)]) == 0


def test_the_aa_pass_reads_the_stored_floors_and_a_global_drift_supersedes_them(tmp_path, monkeypatch):
    """10.6, 7.13: with Calibration floors for the rig's rendering key in the store the A/A pass runs against them and
    queues nothing; a global drift (aa_drift_global) supersedes the key's floors until a Calibration run passes."""
    import dataclasses
    import sentinel.pipeline.run as pr
    _calibrate(tmp_path, S07)
    n = nightly(tmp_path, "M01")
    health = n.canary["run_health"]["aa"]
    assert health["floors_from"] == health["calibration_key"] and health["queue_calibration"] is None
    real = pr.aa_pass.run
    monkeypatch.setattr(pr.aa_pass, "run", lambda *a, **kw: dataclasses.replace(real(*a, **kw), invalidate=True))
    nightly(tmp_path, "M01", run_id="run-2026-11-11-nightly-01", today=date(2026, 11, 11))
    with Store(tmp_path / "sentinel.db") as store:
        live = store.query("SELECT count(*) FROM calibration_floors WHERE superseded_at IS NULL")[0][0]
        gone = store.query("SELECT count(*) FROM calibration_floors WHERE superseded_at IS NOT NULL")[0][0]
    assert live == 0 and gone > 0
