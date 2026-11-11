"""The snapshot lifecycle: names, revert with retry, R1 quarantine, create, validate, retire, pair plans (7.2, 7.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gates import PREFLIGHT_ITEMS, check_provisioned
from sentinel.orchestrator.ports import PreflightRead
from sentinel.orchestrator.preflight import PreflightPins, evaluate
from sentinel.orchestrator.rig import PreflightReport
from sentinel.orchestrator.snapshots import (SimulatedHypervisor, SnapshotHypervisor, branch_b_pair_problem,
                                             create_snapshot, pair_revert_plan, parse_snapshot_name,
                                             r1_snapshot_actions, retention_plan, revert_with_retry, select_snapshot,
                                             snapshot_name, validate_snapshot)
from sentinel.rigmanager import RigPool, RigSlot, first_launch_agreement

APP = "app.main-1998.2.0-4f1c9a2.g1"


def test_names_round_trip_with_dotted_build_ids():
    s = parse_snapshot_name(APP)
    assert (s.kind, s.build_id, s.gen) == ("app", "main-1998.2.0-4f1c9a2", "g1") and str(s) == APP
    assert parse_snapshot_name("base-os.g1").build_id is None
    assert snapshot_name("upgrade", "g1", "released-1998.1.0-8b2d4f6") == "upgrade.released-1998.1.0-8b2d4f6.g1"
    for bad in ("app.g1", "golden.x.g1", "base-os.g1.x"):
        with pytest.raises(ValueError):
            parse_snapshot_name(bad)


def test_select_by_build_skips_quarantined():
    names = [APP, "base-os.g1"]
    assert select_snapshot(names, kind="app", build_id="main-1998.2.0-4f1c9a2", gen="g1") == APP
    assert select_snapshot(names, kind="app", build_id="main-1998.2.0-4f1c9a2", gen="g1", quarantined=[APP]) is None
    assert select_snapshot(names, kind="app", build_id="main-x", gen="g1") is None


# -- revert ---------------------------------------------------------------------------------------------------------
def test_a_clean_revert_passes_at_once():
    hv, clock = SimulatedHypervisor([APP]), ManualClock()
    t0 = clock.monotonic()
    r = revert_with_retry(hv, APP, clock)
    assert r.passed and r.evidence["attempts"] == 1 and clock.monotonic() == t0 and hv.current == APP
    assert isinstance(hv, SnapshotHypervisor)


def test_a_failed_revert_is_retried_once_after_60_s():
    hv, clock = SimulatedHypervisor([APP], revert_failures=1, raise_on_failure=True), ManualClock()
    t0 = clock.monotonic()
    r = revert_with_retry(hv, APP, clock)
    assert r.passed and r.evidence["attempts"] == 2 and clock.monotonic() - t0 == 60.0
    assert "TimeoutError" in r.detail


def test_two_failures_requeue_when_a_same_generation_rig_is_free():
    pool = RigPool.of([RigSlot("rig-01", "pool", "l1", "g1", APP), RigSlot("rig-02", "pool", "l2", "g1", APP),
                       RigSlot("rig-06", "explorer", "lx", "g1", "explorer.main.g1")])
    target = pool.requeue_target("rig-01")
    r = revert_with_retry(SimulatedHypervisor([APP], revert_failures=2), APP, ManualClock(), requeue_rig=target,
                          budget_allows=True)
    assert not r.passed and r.has("requeue_shard") and r.evidence["requeue_to"] == "rig-02" and r.run is None


def test_two_failures_without_budget_are_snapshot_revert_failed():
    r = revert_with_retry(SimulatedHypervisor([APP], revert_failures=2), APP, ManualClock(), requeue_rig="rig-02",
                          budget_allows=False)
    assert (r.unreached.outcome, r.unreached.reason, r.unreached.gate) == ("env_error", "snapshot_revert_failed",
                                                                           "provisioned")
    assert r.run.status == "complete_with_env_errors" and r.alert.rule == "env_error"
    missing = revert_with_retry(SimulatedHypervisor([]), APP, ManualClock())          # no such snapshot
    assert missing.unreached.reason == "snapshot_revert_failed"


def test_a_wrong_manifest_is_r1_and_quarantines_the_snapshot():
    pins = PreflightPins(snapshot_manifest_sha256="sha256:" + "1" * 64,
                         windows_build={"current_build": "22631", "ubr": "4460"})
    good = {"manifest_sha256": "sha256:" + "1" * 64, "current_build": "22631", "ubr": "4460",
            "pending_restart": False}
    tampered = evaluate("R1", PreflightRead("R1", {**good, "manifest_sha256": "sha256:" + "9" * 64}), pins)
    assert r1_snapshot_actions(tampered.causes) == ("quarantine_snapshot", "rebuild_snapshot")
    restart = evaluate("R1", PreflightRead("R1", {**good, "pending_restart": True}), pins)
    assert not restart.passed and r1_snapshot_actions(restart.causes) == ()
    items = {i: True for i in PREFLIGHT_ITEMS}
    items["R1"] = False
    assert check_provisioned(PreflightReport(items, {"R1": tampered.detail})).unreached.gate == "preflight.R1"


# -- create and validate ----------------------------------------------------------------------------------------------
def test_create_runs_the_steps_and_snapshots():
    hv, done = SimulatedHypervisor(["base-os.g1"]), []
    steps = [(name, (lambda n=name: done.append(n))) for name in ("revert_base_os", "install", "write_vmoptions")]
    res = create_snapshot(hv, APP, steps)
    assert res.control.passed and res.snapshot_id == APP and APP in hv.snapshots() and len(done) == 3


def test_a_failing_step_writes_no_snapshot_and_is_install_failed_with_the_log():
    hv = SimulatedHypervisor(["base-os.g1"])

    def copy_corpus():
        raise OSError("usergui not writable")
    res = create_snapshot(hv, APP, [("install", lambda: None), ("copy_corpus", copy_corpus),
                                    ("first_launch", lambda: None)], installer_log_sha256="sha256:" + "3" * 64)
    assert res.control.unreached.reason == "install_failed" and "copy_corpus" in res.control.detail
    assert res.control.evidence["installer_log"] == "sha256:" + "3" * 64 and APP not in hv.snapshots()
    agreement = create_snapshot(hv, APP, [("first_launch", lambda: first_launch_agreement("a.owner"))])
    assert agreement.control.unreached.reason == "agreement_pending" and APP not in hv.snapshots()


MATCH = {"build_id": "main-1998.2.0-4f1c9a2", "display": {"width": 1920}, "vmoptions_hash": "sha256:" + "a" * 64}


def test_validate_passes_on_an_equal_fingerprint_and_a_passing_smoke_flow():
    hv = SimulatedHypervisor([APP])
    assert validate_snapshot(hv, APP, manifest=MATCH, live=dict(MATCH), smoke_passed=True, owner="rig.owner",
                             at="2026-11-10T03:00:00Z") is None


def test_a_failed_validate_deletes_the_snapshot_and_opens_a_maintenance_item():
    hv = SimulatedHypervisor([APP])
    live = {**MATCH, "vmoptions_hash": "sha256:" + "b" * 64}
    res = validate_snapshot(hv, APP, manifest=MATCH, live=live, smoke_passed=True, owner="rig.owner",
                            at="2026-11-10T03:00:00Z")
    assert res.snapshot_actions == ("delete_snapshot",) and APP not in hv.snapshots()
    assert res.maintenance.owner == "rig.owner" and "vmoptions_hash" in res.maintenance.detail
    assert res.control.unreached.reason == "install_failed"
    smoke = validate_snapshot(SimulatedHypervisor([APP]), APP, manifest=MATCH, live=dict(MATCH), smoke_passed=False,
                              owner="rig.owner", at="2026-11-10T03:00:00Z")
    assert "smoke flow failed" in smoke.maintenance.detail


# -- retire ------------------------------------------------------------------------------------------------------------
BUILDS = {
    "released-1997.10.0-aaaaaaa": {"channel": "released", "built_at": "2026-05-01T20:00:00-04:00",
                                   "released_at": "2026-05-08T06:00:00-04:00"},
    "released-1997.11.0-bbbbbbb": {"channel": "released", "built_at": "2026-07-01T20:00:00-04:00",
                                   "released_at": "2026-07-08T06:00:00-04:00"},
    "released-1998.0.0-3c9e7a1": {"channel": "released", "built_at": "2026-09-14T20:00:00-04:00",
                                  "released_at": "2026-09-21T06:00:00-04:00"},
    "released-1998.1.0-8b2d4f6": {"channel": "released", "built_at": "2026-10-12T20:00:00-04:00",
                                  "released_at": "2026-10-19T06:00:00-04:00"},
    "main-1998.1.5-1111111": {"channel": "main", "built_at": "2026-11-01T21:00:00-05:00"},
    "main-1998.2.0-4f1c9a2": {"channel": "main", "built_at": "2026-11-08T21:10:00-05:00"},
    "main-1998.2.1-2222222": {"channel": "main", "built_at": "2026-11-09T21:10:00-05:00"},
    "branch-1998.3.0-5d3e812": {"channel": "branch", "built_at": "2026-11-09T13:42:10-05:00"},
    "branch-1998.2.9-9999999": {"channel": "branch", "built_at": "2026-11-05T13:42:10-05:00"},
}


def test_retention_keeps_the_release_two_newest_mains_upgrades_and_in_flight_builds():
    names = ["base-os.g1"] + [f"app.{b}.g1" for b in BUILDS] + [
        "upgrade.released-1998.1.0-8b2d4f6.g1", "upgrade.released-1998.0.0-3c9e7a1.g1",
        "upgrade.released-1997.11.0-bbbbbbb.g1", "upgrade.released-1997.10.0-aaaaaaa.g1",
        "explorer.main-1998.2.1-2222222.g1", "app.unknown-build.g1"]
    plan = retention_plan(names, BUILDS, in_flight=["branch-1998.3.0-5d3e812", "main-1998.1.5-1111111"])
    assert set(plan.delete) == {
        "app.released-1997.10.0-aaaaaaa.g1", "app.released-1997.11.0-bbbbbbb.g1", "app.released-1998.0.0-3c9e7a1.g1",
        "app.branch-1998.2.9-9999999.g1", "upgrade.released-1997.11.0-bbbbbbb.g1",
        "upgrade.released-1997.10.0-aaaaaaa.g1"}
    assert plan.keep["app.released-1998.1.0-8b2d4f6.g1"] == "the last released build"
    assert plan.keep["app.main-1998.2.1-2222222.g1"] == plan.keep["explorer.main-1998.2.1-2222222.g1"] \
        == "one of the two newest main builds"
    assert plan.keep["app.main-1998.1.5-1111111.g1"] == "a build of a run in flight"
    assert plan.keep["upgrade.released-1998.0.0-3c9e7a1.g1"].startswith("upgrade snapshot")
    assert "base-os.g1" in plan.keep and "app.unknown-build.g1" in plan.keep


def test_retention_keeps_the_n_minus_1_upgrade_snapshot_the_rc_cut_upgrade_run_needs():
    # w4-rig:0: at an RC cut N is the RC, so N-1 is the last release and N-2 the one before (7.2, 10.9 G-1,
    # 11.8 DR-04 Branch B); the 7.2 retire rule never retires the last release
    names = ["upgrade.released-1998.1.0-8b2d4f6.g1", "upgrade.released-1998.0.0-3c9e7a1.g1",
             "upgrade.released-1997.11.0-bbbbbbb.g1"]
    plan = retention_plan(names, BUILDS)
    assert plan.delete == ("upgrade.released-1997.11.0-bbbbbbb.g1",)
    assert select_snapshot(list(plan.keep), kind="upgrade", build_id="released-1998.1.0-8b2d4f6", gen="g1") == \
        "upgrade.released-1998.1.0-8b2d4f6.g1"
    # an upgrade snapshot a run in flight names is never deleted under it
    flying = retention_plan(names, BUILDS, in_flight=["released-1997.11.0-bbbbbbb"])
    assert flying.delete == () and flying.keep["upgrade.released-1997.11.0-bbbbbbb.g1"] == "a build of a run in flight"


# -- pairs -------------------------------------------------------------------------------------------------------------
def test_a_pair_on_one_rig_reverts_base_then_candidate_and_once_with_reuse():
    base, cand = "app.main-1998.2.0-4f1c9a2.g1", "app.branch-1998.3.0-5d3e812.g1"
    assert pair_revert_plan(base=base, candidate=cand, base_reused=False) == (base, cand)
    assert pair_revert_plan(base=None, candidate=cand, base_reused=True) == (cand,)
    with pytest.raises(ValueError):
        pair_revert_plan(base=None, candidate=cand, base_reused=False)


def test_branch_b_halves_come_from_the_same_night():
    ny = timezone(timedelta(hours=-5))
    late, early = datetime(2026, 11, 9, 22, 0, tzinfo=ny), datetime(2026, 11, 10, 1, 0, tzinfo=ny)
    assert branch_b_pair_problem(late, early) is None
    assert "different nights" in branch_b_pair_problem(late, early + timedelta(days=1))
