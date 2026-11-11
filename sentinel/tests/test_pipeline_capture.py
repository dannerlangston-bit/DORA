"""Capture wired into the simulated PR run (w2 wiring item 2): the window rules of 3.4 (selection, target choice,
occlusion, modal), every capture stored with its `CaptureMeta` (`capture.put_capture`), the masked_live median's
frames (7.9), sweeps through `capture.sweep.SizeLadder` with gadget minimum clamping (2.5.1), and the bounds source
in the Orchestrator (5.9, 7.5)."""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from sentinel.capture.capture import meta_from_json
from sentinel.demo import AT, EXAMPLE, STOCK_FLOW, app_crash_scenario, wrong_number_scenario
from sentinel.orchestrator.rig import LaunchObservation
from sentinel.orchestrator.simrig import BUILD, MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.clock import ManualClock
from sentinel.pipeline.orchestration import RigScript
from sentinel.pipeline.run import run_pipeline
from sentinel.store import ArtifactStore, Store

TODAY = date(2026, 11, 10)
S03 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s03"
S07 = f"flow/dialog.order-confirmation/{STOCK_FLOW}/s07"
POSITIONS = "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/cp-positions"


def _run(tmp_path, **script):
    sc = dataclasses.replace(wrong_number_scenario(TODAY), rig=RigScript(**script))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    return r, {o.checkpoint_id: o for o in r.outcomes}


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    out = tmp_path_factory.mktemp("capture-clean")
    r, by = _run(out)
    rows = ArtifactStore.read_manifest(out / "artifacts")["entries"]       # 8.4: names to hashes
    return out, r, by, rows


def test_every_capture_is_stored_with_its_metadata(clean):
    out, r, by, rows = clean
    caps = {(x["checkpoint_id"], x["side"]): x for x in rows if x["kind"] == "capture" and "capture-" in x["name"]}
    metas = {(x["checkpoint_id"], x["side"]): x for x in rows if x["kind"] == "bounds_json"}
    assert (S07, "candidate") in caps and (S07, "base") in caps and set(metas) == set(caps)
    arts = ArtifactStore(out / "artifacts", f"runs/{r.run_id}")
    meta = meta_from_json(arts.get(metas[(S07, "candidate")]["sha256"]))
    assert meta.window_class == "SunAwtDialog" and meta.title == "Order Confirmation"       # the dialog target
    assert meta.sha256 == caps[(S07, "candidate")]["sha256"] and meta.fingerprint_id == by[S07].fingerprint_id
    assert meta.redaction_version and meta.app_restart_count == 0 and meta.mode == "scripted"
    main = meta_from_json(arts.get(metas[(S03, "candidate")]["sha256"]))
    assert main.window_class == "SunAwtFrame" and main.rect == (0, 0, 1920, 1080)


def test_a_masked_live_checkpoint_stores_the_later_frames_of_its_median(clean):
    _, _, _, rows = clean
    frames = sorted(x["name"].rsplit("/", 1)[-1] for x in rows if x["checkpoint_id"] == POSITIONS
                    and "masked-live" in x["name"])
    assert frames == ["masked-live-1000ms.png", "masked-live-750ms.png"]


def test_a_foreign_window_over_the_target_is_env_error_occluded(tmp_path):
    r, by = _run(tmp_path, occluded=(S03, S07))
    for cid in (S03, S07):
        assert (by[cid].outcome, by[cid].reason, by[cid].gate) == ("env_error", "occluded", "running")
        assert "notepad.exe" in by[cid].detail
    with Store(tmp_path / "sentinel.db") as store:
        events = [e for e in store.step_events(r.run_id) if e.action == "foreign_window"]
        hardening = [f for f in store.findings(r.run_id) if f.kind == "hardening"]
    assert {e.checkpoint_id for e in events} == {S03, S07} and len(hardening) == 1        # one per image name
    assert len([a for a in r.alerts if a["control"] == "occlusion"]) == 1
    assert r.status == "complete_with_env_errors"


def test_a_target_window_that_is_not_on_screen_is_window_mismatch(tmp_path):
    _, by = _run(tmp_path, window_missing=(S07,))
    assert (by[S07].outcome, by[S07].reason) == ("not_comparable", "window_mismatch")
    assert "Order Confirmation" in by[S07].detail


def test_a_clamped_gadget_is_size_clamped_and_its_minimum_reported(tmp_path):
    sc = app_crash_scenario(TODAY)
    clamp = ((AT, 0, 400),)
    sc = dataclasses.replace(sc, rig=RigScript(), candidate=dataclasses.replace(sc.candidate, minimums=clamp),
                             base=dataclasses.replace(sc.base, minimums=clamp))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    ladder = [o for o in r.outcomes if o.test_kind == "sweep" and o.surface_id == AT]
    assert ladder and {o.outcome for o in ladder} == {"identical"}
    with Store(tmp_path / "sentinel.db") as store:
        steps = [e for e in store.step_events(r.run_id) if e.action == "size_step" and e.side == "candidate"]
    clamped = [e for e in steps if e.reason == "size_clamped"]
    assert clamped and all(e.rect_h == 400 for e in clamped) and all(e.step_outcome == "passed" for e in clamped)
    text = (tmp_path / "pr-comment.md").read_text()
    assert "Sweep minimums" in text and "achieved minimum" in text and "clamped on height" in text


def test_a_build_that_moves_a_gadgets_minimum_is_a_finding(tmp_path):
    sc = app_crash_scenario(TODAY)
    sc = dataclasses.replace(sc, rig=RigScript(),
                             candidate=dataclasses.replace(sc.candidate, minimums=((AT, 0, 400),)))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        moved = [f for f in store.findings(r.run_id) if f.kind == "window_geometry_changed"
                 and "achieved minimum" in f.description]
    assert moved and AT in moved[0].description and "x400 on candidate" in moved[0].description


def _orch(**launch):
    clock = ManualClock()
    obs = LaunchObservation(True, BUILD, waited_s=40.0, **launch)
    rig = SimulatedRig(SimScenario(launches=[obs] * 4), clock=clock)
    ctx = default_context(bounds_branch="A")
    return Orchestrator(rig, ctx, MemorySink(["c1", "c2"]), clock=clock), rig


def test_branch_a_without_the_bridge_marks_the_bounds_invariants():
    orch, _ = _orch(jab_properties_present=False, jab_query_answers=False)
    report = orch.run()
    (r,) = [x for x in report.results if x.control == "jab_enablement"]
    assert r.gate == "launched" and r.outcomes[0].selector == "bounds_invariants"
    assert orch.bounds_outcome(needs_bounds=True).reason == "bounds_source_unavailable"
    assert orch.bounds_outcome(needs_bounds=False) is None


def test_a_bridge_lost_mid_run_relaunches_and_a_second_loss_after_it_aborts():
    orch, rig = _orch(jab_properties_present=True, jab_query_answers=True)
    seen = []

    def work(o):
        o.bounds_lost("c1")
        assert o.bounds_outcome(needs_bounds=False).reason == "bounds_source_unavailable"
        o.flow_boundary()                              # the relaunch at the next boundary (5.9)
        seen.append([x for x in o.results if x.control == "relaunch"][-1].evidence["cause"])
        assert o.bounds_outcome() is None              # the relaunch restored the bridge
    orch.run(work)
    assert seen == ["bounds_source"]
    orch2, rig2 = _orch(jab_properties_present=True, jab_query_answers=True)
    rig2.s.launches = [LaunchObservation(True, BUILD, waited_s=40.0, jab_properties_present=True,
                                         jab_query_answers=True)] + [
        LaunchObservation(True, BUILD, waited_s=40.0, jab_properties_present=True, jab_query_answers=False)] * 3

    def work2(o):
        o.bounds_lost("c1")
        o.flow_boundary()
    report = orch2.run(work2)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_harness", "bounds_source_unavailable")


def test_the_next_sweep_host_starts_after_a_reset_and_the_tier_c_reload(tmp_path):
    from sentinel.demo import flexgrid_scenario
    r = run_pipeline(EXAMPLE, flexgrid_scenario(TODAY), tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        events = store.step_events(r.run_id)
    resets = [e for e in events if e.action == "reset_input_state" and "next sweep host" in (e.reason or "")]
    assert resets and all(e.step_id is None for e in resets)
    sizes = [e for e in events if e.action == "size_step" and e.side == "candidate"]
    assert sizes and {e.reason for e in sizes} <= {None, "size_clamped"} and all(e.rect_w and e.rect_h for e in sizes)
