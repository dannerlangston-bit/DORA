"""The Nightly A/A drift pass (3.9 step 7, 10.6, 2.7 invariant_on_aa, 7.13, SD-25), `sentinel.aa_pass`."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sentinel import aa_pass as A
from sentinel import required_fields, synth
from sentinel.diff.calibration import CalibrationFile, CheckpointCalibration, RegionFloors, derive_threshold
from sentinel.diff.regions import FloorException, Landmark, Rect, Region, RegionsFile
from sentinel.invariants import Rect as IRect
from sentinel.invariants import ScreenModel, SurfaceRules, UIComponent
from sentinel.scope import PlannedCheckpoint
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

WS = "ws.tier-a.swing-trader"
SIZE = (320, 180)
LAYOUT = {"type": "window", "name": "w", "children": [copy.deepcopy(synth.node(synth.sample_layout(0), "watchlist"))]}
LAYOUT["children"][0]["rect"] = [6, 6, 306, 168]
BASE = synth.render(LAYOUT, SIZE)
GRID_NOISE = (20, 70, 150, 50)                       # inside the watchlist grid
SHA1, SHA2 = "1" * 64, "2" * 64


def registry(exception: FloorException | None = None) -> RegionsFile:
    lx, ly, _, _ = BASE.get("watchlist.title.label").bbox
    lm = Landmark("lm.watchlist", BASE.crop("watchlist.title.label", 2), expected=(lx - 2, ly - 2))
    gx, gy, gw, gh = BASE.get("watchlist.grid").bbox
    return RegionsFile(f"census/sidebar.watchlist/{WS}", [lm],
                       [Region("rgn.watchlist.grid", "diff", "lm.watchlist", Rect(gx - lx + 2, gy - ly + 2, gw, gh),
                               floor_exception=exception)])


REG = registry()
EXCEPTED = registry(FloorException("jdoe", "quote flash in the grid", "TOS-48200",
                                   datetime(2026, 12, 10, tzinfo=timezone.utc)))
DRIFTED = synth.perturb(BASE.image, 5, count=8, amplitude=90, rect=GRID_NOISE)
BIG = synth.perturb(BASE.image, 7, count=60, amplitude=90, rect=GRID_NOISE)


def cid(n: int) -> str:
    return f"census/surface.s{n:02d}/{WS}"


def cp(n: int, data_mode: str = "frozen") -> PlannedCheckpoint:
    return PlannedCheckpoint(cid(n), f"surface.s{n:02d}", "census", 1, WS, "swing_trader", data_mode)


def calibration(cps, *, grid_floor: int = 0) -> CalibrationFile:
    cf = CalibrationFile("sha256:" + "a" * 64, "run-cal-1")
    for c in cps:
        cal = CheckpointCalibration(c.checkpoint_id, c.data_mode)
        for rid in ("rgn.watchlist.grid", "_rest", "*"):
            fl = RegionFloors(changed_px=grid_floor, component_area=grid_floor) if rid == "rgn.watchlist.grid" \
                else RegionFloors()
            cal.floors[rid], cal.thresholds[rid] = fl, derive_threshold(fl)
        cf.checkpoints[c.checkpoint_id] = cal
    return cf


def pair(second=None, registry_=REG, **kw) -> A.PassPair:
    second = BASE.image.copy() if second is None else second
    return A.PassPair(BASE.image, second, registry_, artifacts={"base": SHA1, "candidate": SHA2},
                      fingerprint_id="sha256:" + "f" * 64, data_epoch_id="sha256:" + "e" * 64,
                      captured_at="2026-11-11T03:10:00+00:00", rig_id="rig-04", **kw)


def capture(changes: dict | None = None):
    """capture_pair: an identical second pass unless `changes` names another PassPair or image for the checkpoint."""
    changes = changes or {}

    def cap(c):
        got = changes.get(c.checkpoint_id)
        return got if isinstance(got, A.PassPair) else pair(got)
    return cap


def go(cps, changes=None, cal="default", **kw) -> A.AAPassResult:
    cal = calibration(cps) if cal == "default" else cal
    return A.run(cps, capture(changes), run_id="nightly-1", calibration=cal, watchdog_event=lambda c: "wd-7", **kw)


def by_id(res: A.AAPassResult) -> dict:
    return {r.checkpoint_id: r for r in res.rows}


def test_a_drifting_frozen_checkpoint_is_not_settled_aa_drift_and_the_rest_identical():
    cps = [cp(n) for n in range(55)]
    res = go(cps, {cid(3): DRIFTED})
    rows = by_id(res)
    assert (rows[cid(3)].outcome, rows[cid(3)].reason, rows[cid(3)].side) == ("not_settled", "aa_drift", "both")
    assert "rgn.watchlist.grid" in rows[cid(3)].detail
    assert {r.outcome for k, r in rows.items() if k != cid(3)} == {"identical"}
    assert res.drifted == (cid(3),) and not res.invalidate and res.queue_calibration is None
    assert res.fp_rate == pytest.approx(1 / 110)                        # 55 pairs x 2 regions, one over floor
    assert any("A/A drift on 1 checkpoint" in line for line in res.morning)
    assert res.health()["drifted"] == [cid(3)] and res.health()["pairs"] == 55
    assert all(required_fields.errors(r) == [] for r in res.rows)       # 4.13 required fields, SD-48 provenance
    assert rows[cid(3)].artifacts == {"base": SHA1, "candidate": SHA2} and rows[cid(3)].cand_rig_id == "rig-04"


def test_more_than_2_percent_above_floor_is_aa_drift_global_and_invalidates_thresholds():
    cps = [cp(n) for n in range(50)]
    res = go(cps, {cid(n): DRIFTED for n in range(3)})
    rows = by_id(res)
    assert {(r.outcome, r.reason) for r in res.rows} == {("harness_error", "aa_drift_global")}   # every compared pair
    assert "watchdog_event: wd-7" in rows[cid(0)].detail and "3 of 50" in rows[cid(0)].detail
    assert rows[cid(0)].detail.endswith("this checkpoint above floor")
    assert rows[cid(9)].detail.endswith("not above floor") and res.drifted == ()   # no aa_drift streak from it
    assert res.invalidate and res.queue_calibration == A.CalibrationRequest("aa_drift_global")
    assert any(A.MORNING_INVALID in line for line in res.morning)
    assert all(required_fields.errors(r) == [] for r in res.rows)


def test_an_enabled_invariant_firing_on_the_pair_is_harness_error_invariant_on_aa():
    frame = UIComponent("frame", IRect(0, 0, 480, 360), "panel")
    noisy = ScreenModel("A", (480, 360), (frame, UIComponent("a", IRect(0, 0, 100, 100), "panel", parent="frame"),
                                          UIComponent("b", IRect(50, 50, 100, 100), "panel", parent="frame")))
    rules = SurfaceRules("surface.s01", tier=1)
    fired = pair(DRIFTED, frames=((noisy,) * 3, (noisy,) * 3), rules=rules)
    clean_frames = ScreenModel("A", (480, 360), (frame,))
    quiet = pair(frames=((clean_frames,) * 3, (clean_frames,) * 3), rules=rules)
    res = go([cp(n) for n in range(60)], {cid(1): fired, cid(2): quiet})
    rows = by_id(res)
    assert (rows[cid(1)].outcome, rows[cid(1)].reason) == ("harness_error", "invariant_on_aa")
    assert "INV-01" in rows[cid(1)].detail and "watchdog_event: wd-7" in rows[cid(1)].detail
    assert rows[cid(2)].outcome == "identical" and res.invariant_on_aa == (cid(1),) and res.drifted == ()


def test_a_rendering_key_change_runs_at_threshold_zero_and_queues_calibration_naming_the_field():
    cps = [cp(n) for n in range(5)]
    res = go(cps, {cid(0): DRIFTED}, rendering_change=("gpu",))
    rows = by_id(res)
    assert (rows[cid(0)].outcome, rows[cid(0)].reason) == ("needs_human", "uncalibrated")
    assert "gpu" in rows[cid(0)].detail and rows[cid(0)].threshold_source == "uncalibrated"
    assert rows[cid(1)].outcome == "identical"
    assert res.queue_calibration == A.CalibrationRequest("rendering_change", ("gpu",))
    assert res.fp_rate is None and not res.carry_over and not res.invalidate
    assert any("rendering key changed (gpu)" in line for line in res.morning)


def test_floors_carry_over_a_jre_change_only_when_the_pass_passes():
    cps = [cp(n) for n in range(55)]
    ok = go(cps, rendering_change=("jre_release",))
    assert ok.carry_over and ok.queue_calibration is None
    assert any("carried over" in line for line in ok.morning)
    bad = go(cps, {cid(0): DRIFTED}, rendering_change=("jre_release", "render_probe_hash"))
    assert not bad.carry_over and by_id(bad)[cid(0)].reason == "aa_drift"
    assert bad.queue_calibration == A.CalibrationRequest("carry_over_failed", ("jre_release", "render_probe_hash"))


def test_no_calibration_at_all_is_uncalibrated_and_queues_one():
    res = go([cp(0), cp(1)], {cid(0): DRIFTED}, cal=None)
    rows = by_id(res)
    assert (rows[cid(0)].reason, rows[cid(1)].outcome) == ("uncalibrated", "identical")
    assert "no Calibration floors" in rows[cid(0)].detail
    assert res.queue_calibration == A.CalibrationRequest("no_calibration")


def test_zero_floor_regions_of_masked_live_checkpoints_are_excluded_from_drift():
    frozen = [cp(n) for n in range(30)]
    live = [cp(n, "masked_live") for n in range(30, 40)]
    res = go(frozen + live, {c.checkpoint_id: DRIFTED for c in live})
    rows = by_id(res)
    assert {(rows[c.checkpoint_id].outcome, rows[c.checkpoint_id].reason) for c in live} == {("noise", "below_floor")}
    assert "SD-25" in rows[cid(30)].detail
    assert not res.invalidate and res.above_floor_fraction == 0.0 and res.drifted == ()
    assert all(required_fields.errors(rows[c.checkpoint_id]) == [] for c in live)


def test_a_masked_live_region_with_a_nonzero_floor_drifts_above_it_and_is_noise_below_it():
    live = [cp(n, "masked_live") for n in range(55)]
    cal = calibration(live, grid_floor=6)
    small = synth.perturb(BASE.image, 3, count=2, amplitude=90, rect=GRID_NOISE)
    res = go(live, {cid(0): pair(BIG, EXCEPTED), cid(1): pair(small, EXCEPTED)}, cal=cal)
    rows = by_id(res)
    assert (rows[cid(0)].outcome, rows[cid(0)].reason) == ("not_settled", "aa_drift")
    assert (rows[cid(1)].outcome, rows[cid(1)].detail) == ("noise", "A/A change below the floor")


def test_capture_failures_and_engine_terminals_keep_their_outcome():
    stuck = A.PassPair(None, None, failure=A.PassFailure("not_settled", "frames_unstable", side="candidate",
                                                         detail="frames moved for 4000 ms"))
    crashed = A.PassPair(None, None, failure=A.PassFailure("harness_error", "artifact_persist_failed",
                                                           detail="write failed"))
    res = go([cp(0), cp(1), cp(2)], {cid(0): stuck, cid(1): crashed})
    rows = by_id(res)
    assert (rows[cid(0)].outcome, rows[cid(0)].reason, rows[cid(0)].side) == ("not_settled", "frames_unstable",
                                                                              "candidate")
    assert rows[cid(1)].detail == "watchdog_event: wd-7; write failed" and rows[cid(2)].outcome == "identical"
    with pytest.raises(ValueError, match="both captures"):
        A.PassPair(BASE.image, None)
    with pytest.raises(Exception):
        A.PassFailure("not_settled", "made_up_reason")


def test_plan_items_are_flattened_in_order():
    item = SimpleNamespace(checkpoints=[cp(0), cp(1)])
    res = A.run([item, cp(2)], capture(), run_id="nightly-1", calibration=calibration([cp(0), cp(1), cp(2)]),
                watchdog_event=lambda c: "wd-1")
    assert [r.checkpoint_id for r in res.rows] == [cid(0), cid(1), cid(2)]


def test_rendering_changes_names_the_differing_rendering_key_fields():
    match = {f: "x" for f in A.CALIBRATION_KEY_FIELDS} | {"harness": {k: "h" for k in A.CALIBRATION_KEY_HARNESS},
                                                         "build_id": "b1", "data_epoch": "e1"}
    other = {**match, "jre_release": "21.0.5", "build_id": "b2", "data_epoch": "e2",
             "harness": {**match["harness"], "settle_params_sha256": "h2"}}
    assert A.rendering_changes(match, match) == ()
    assert A.rendering_changes(other, match) == ("jre_release", "harness.settle_params_sha256")
    assert "gpu" in A.rendering_changes({k: v for k, v in match.items() if k != "gpu"}, match)


def test_invalidate_floors_supersedes_the_current_floors_of_one_rendering_key(tmp_path):
    key, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    with Store(tmp_path / "c.db") as st:
        st.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="s", allowlist_entry=1)
        st.begin_run(RunPlan(run_id="cal-1", run_type="calibration", config_commit="c", config_tarball_sha256="t",
                             snapshot_set_sha256="s", harness_sha="h", authorization_ref="a", budget_sec=60,
                             scope=(), shards=(ShardPlan("cal-1/1", "rig-01"),)))
        st.register_checkpoints([ScopeEntry(cid(n), WS, "swing_trader", 1, "frozen") for n in (0, 1)],
                               config_commit="c")
        floor = {"region_id": "_rest", "calibration_run_id": "cal-1", "samples": 15, "threshold_component_area": 4,
                 "threshold_changed_px": 4, "threshold_delta_e": 2.0, "threshold_shift_px": 1}
        st.write_calibration([{**floor, "checkpoint_id": cid(0), "rendering_key_sha256": key},
                              {**floor, "checkpoint_id": cid(1), "rendering_key_sha256": key},
                              {**floor, "checkpoint_id": cid(0), "rendering_key_sha256": other}])
        assert A.invalidate_floors(st, key, at="2026-11-11T04:00:00Z") == 2
        rows = st.query("SELECT rendering_key_sha256, superseded_at FROM calibration_floors ORDER BY floor_id")
        assert [(r[0] == key, r[1]) for r in rows] == [(True, "2026-11-11T04:00:00+00:00")] * 2 + [(False, None)]
        assert A.invalidate_floors(st, key) == 0

