"""A/A calibration and threshold derivation (3.9, 4.8 threshold source, 10.6)."""
import copy
import dataclasses
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sentinel import synth
from sentinel.diff.calibration import (
    CalibrationFile, Capture, CheckpointCalibration, FixItem, RegionFloors, aa_drift, aa_false_positive_rate, aa_pairs,
    calibrate_checkpoint, derive_threshold,
)
from sentinel.diff.engine import DiffContext, diff_pair
from sentinel.diff.regions import UNCALIBRATED, FloorException, Landmark, Rect, Region, RegionsFile

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
CID = "census/sidebar.watchlist/ws.tier-a.swing-trader"
SIZE = (320, 180)
LAYOUT = {"type": "window", "name": "w", "children": [copy.deepcopy(synth.node(synth.sample_layout(0), "watchlist"))]}
LAYOUT["children"][0]["rect"] = [6, 6, 306, 168]
BASE = synth.render(LAYOUT, SIZE)
NOISE = (20, 70, 150, 50)                       # inside the watchlist grid


def registry(exception=None):
    lx, ly, _, _ = BASE.get("watchlist.title.label").bbox
    lm = Landmark("lm.watchlist", BASE.crop("watchlist.title.label", 2), expected=(lx - 2, ly - 2))
    gx, gy, gw, gh = BASE.get("watchlist.grid").bbox
    grid = Region("rgn.watchlist.grid", "diff", "lm.watchlist", Rect(gx - lx + 2, gy - ly + 2, gw, gh),
                  floor_exception=exception)
    return RegionsFile(CID, [lm], [grid])


def noisy_captures():
    """r1 to r5 and r6: each capture has 3 changed pixels in the grid (a flashing quote cell)."""
    return [Capture(synth.perturb(BASE.image, seed, count=3, amplitude=90, rect=NOISE),
                    time_to_settle_ms=1500 + 100 * seed) for seed in range(6)]


@pytest.fixture(scope="module")
def noisy_calibration():
    return calibrate_checkpoint(noisy_captures(), registry(), data_mode="masked_live", now=NOW)


def test_fifteen_pairs():
    pairs = aa_pairs()
    assert len(pairs) == 15 and len(set(pairs)) == 15
    assert sum(1 for i, j in pairs if j < 5) == 10 and {i for i, j in pairs if j == 5} == {0, 1, 2, 3, 4}


@pytest.mark.parametrize("floors, expected", [
    (RegionFloors(), (4, 4, 2.0, 1)),
    (RegionFloors(component_area=3, changed_px=7, delta_e=1.6, shift_px=1), (6, 14, 3.2, 2)),
    (RegionFloors(component_area=1, changed_px=1, delta_e=0.4, shift_px=0), (4, 4, 2.0, 1)),
    (RegionFloors(component_area=10, changed_px=25, delta_e=0.0, shift_px=3), (20, 50, 2.0, 6)),
])
def test_threshold_derivation_with_margin_two(floors, expected):
    th = derive_threshold(floors)
    assert (th.component_area_min, th.changed_px_min, th.delta_e_min, th.shift_px_min) == pytest.approx(expected)
    assert th.floor_changed_px == floors.changed_px and th.nonzero_floor == bool(floors.changed_px or floors.component_area or floors.shift_px)


def test_clean_captures_calibrate_to_zero_floors_and_complete():
    caps = [Capture(BASE.image.copy(), time_to_settle_ms=t) for t in (1000, 1200, 1100, 1300, 900, 1250)]
    cal = calibrate_checkpoint(caps, registry(), data_mode="frozen", now=NOW)
    assert cal.accepted and cal.fix_list == [] and cal.settle_p50_ms == 1150
    assert set(cal.floors) == {"rgn.watchlist.grid", "_rest", "*"}
    assert all(len(rows) == 15 for rows in cal.pair_stats.values())
    th = cal.thresholds["rgn.watchlist.grid"]
    assert (th.component_area_min, th.changed_px_min, th.delta_e_min, th.shift_px_min) == (4, 4, 2.0, 1)
    cf = CalibrationFile("sha256:fp1", "run-cal-1", {CID: cal})
    assert cf.complete and cf.lookup(CID, "rgn.watchlist.grid") == (th, "calibrated")


def test_floors_from_five_noisy_captures_go_on_the_fix_list(noisy_calibration):
    cal = noisy_calibration
    grid = cal.floors["rgn.watchlist.grid"]
    assert grid.changed_px == 6 and grid.components == 0 and grid.shift_px == 0
    assert cal.floors_sorted()[0][0] == "rgn.watchlist.grid"
    assert max(r["changed_px"] for r in cal.pair_stats["rgn.watchlist.grid"]) == 6
    assert not cal.accepted
    [item] = cal.fix_list
    assert (item.region_id, item.cause, item.closed) == ("rgn.watchlist.grid", "unknown", False)
    # the proposed region is snapped to the landmark and covers the changing pixels
    lx, ly, _, _ = BASE.get("watchlist.title.label").bbox
    p = item.proposed_rect.offset(lx - 2, ly - 2)
    assert item.proposed_landmark == "lm.watchlist"
    assert Rect(*NOISE).intersect(p) is not None and p.x >= NOISE[0] - 3 and p.y >= NOISE[1] - 2
    # until calibration completes, no threshold exists: every region is uncalibrated (3.9 step 6)
    cf = CalibrationFile("sha256:fp1", "run-cal-1", {CID: cal})
    assert not cf.complete and cf.lookup(CID, "rgn.watchlist.grid") == (UNCALIBRATED, "uncalibrated")
    assert cf.lookup(CID, "_rest") == (UNCALIBRATED, "uncalibrated") and len(cf.fix_list()) == 1


def test_floor_exception_accepts_the_floor_and_sub_floor_noise_is_below():
    exc = FloorException("jdoe", "quote flash on the last column", "TOS-48200", NOW + timedelta(days=30))
    reg = registry(exc)
    cal = calibrate_checkpoint(noisy_captures(), reg, data_mode="masked_live", now=NOW)
    assert cal.accepted and cal.fix_list[0].closed
    cf = CalibrationFile("sha256:fp1", "run-cal-1", {CID: cal})
    th, src = cf.lookup(CID, "rgn.watchlist.grid")
    assert src == "calibrated" and th.changed_px_min == 12 and th.floor_changed_px == 6
    later = synth.perturb(BASE.image, 77, count=3, amplitude=90, rect=NOISE)
    res = diff_pair(BASE.image, later, reg, calibration=cf, context=DiffContext(checkpoint_id=CID))
    assert res.level == "below" and res.threshold_source == "calibrated"
    # an expired exception does not close the item
    stale = registry(dataclasses.replace(exc, expires_at=NOW - timedelta(days=1)))
    assert not calibrate_checkpoint(noisy_captures(), stale, data_mode="masked_live", now=NOW).accepted


def test_landmark_jitter_and_failed_captures_are_fix_list_items():
    jitter = copy.deepcopy(LAYOUT)
    jitter["children"][0]["rect"] = [7, 6, 306, 168]            # the content lands 1 px right in r6
    caps = [Capture(BASE.image.copy()) for _ in range(5)] + [Capture(synth.render(jitter, SIZE).image)]
    cal = calibrate_checkpoint(caps, registry(), data_mode="frozen", now=NOW)
    assert [(i.region_id, i.cause) for i in cal.fix_list] == [("*", "landmark_jitter")]
    assert cal.floors["*"].shift_px == 1 and cal.thresholds["*"].shift_px_min == 2 and not cal.accepted
    failed = [Capture(None, outcome="not_settled", reason="frames_unstable")] + [Capture(BASE.image.copy())] * 4 \
        + [Capture(None, outcome="landmark_missing", reason="landmark:lm.watchlist")]
    cal = calibrate_checkpoint(failed, registry(), data_mode="frozen", now=NOW)
    assert [(i.cause, i.outcome) for i in cal.fix_list] == [("settle", "not_settled"), ("landmark_jitter", "landmark_missing")]
    assert not cal.accepted
    with pytest.raises(ValueError):
        Capture(None)
    with pytest.raises(ValueError):
        FixItem(CID, None, "gremlins")
    with pytest.raises(ValueError):
        calibrate_checkpoint(caps[:5], registry(), data_mode="frozen")


def test_lookup_cases_and_round_trip(tmp_path, noisy_calibration):
    zero = CheckpointCalibration(CID, "frozen")
    for rid in ("rgn.watchlist.grid", "_rest", "*"):
        zero.floors[rid], zero.thresholds[rid] = RegionFloors(), derive_threshold(RegionFloors())
    cf = CalibrationFile("sha256:fp2", "run-cal-2", {CID: zero})
    assert cf.lookup(CID, "rgn.new-region") == (UNCALIBRATED, "uncalibrated")     # added since calibration
    assert cf.lookup("census/other/ws.a", "_rest") == (UNCALIBRATED, "uncalibrated")
    path = cf.save(tmp_path)
    back = CalibrationFile.load(tmp_path, "sha256:fp2")
    assert back.complete and back.lookup(CID, "rgn.watchlist.grid") == cf.lookup(CID, "rgn.watchlist.grid")
    assert path.name == "sha256_fp2.yaml"
    assert CalibrationFile.load(tmp_path, "sha256:none") is None
    noisy = CalibrationFile("sha256:fp3", "run-cal-3", {CID: noisy_calibration})
    noisy.save(tmp_path)
    again = CalibrationFile.load(tmp_path, "sha256:fp3")
    assert not again.complete and again.checkpoints[CID].floors["rgn.watchlist.grid"].changed_px == 6


def test_aa_false_positive_rate_and_nightly_drift():
    zero = CheckpointCalibration(CID, "frozen")
    for rid in ("rgn.watchlist.grid", "_rest", "*"):
        zero.floors[rid], zero.thresholds[rid] = RegionFloors(), derive_threshold(RegionFloors())
    cf = CalibrationFile("sha256:fp2", "run-cal-2", {CID: zero})
    clean = diff_pair(BASE.image, BASE.image.copy(), registry(), calibration=cf)
    drift = diff_pair(BASE.image, synth.perturb(BASE.image, 5, count=8, amplitude=90, rect=NOISE), registry(),
                      calibration=cf)
    assert aa_false_positive_rate([clean, drift]) == pytest.approx(1 / 4)
    results = {f"census/c{i}/ws.a": clean for i in range(60)}
    results["census/d/ws.a"] = drift
    verdict = aa_drift(results, frozen=results.keys())
    assert set(verdict.drifted) == {"census/d/ws.a"} and verdict.global_drift is None
    assert verdict.drifted["census/d/ws.a"].reason == "aa_drift"
    many = {f"census/d{i}/ws.a": drift for i in range(3)} | {f"census/c{i}/ws.a": clean for i in range(50)}
    assert aa_drift(many, frozen=[]).global_drift.reason == "aa_drift_global"
    unc = diff_pair(BASE.image, synth.perturb(BASE.image, 5, count=8, amplitude=90, rect=NOISE), registry())
    assert aa_drift({"census/u/ws.a": unc}, frozen=["census/u/ws.a"]).drifted == {}       # uncalibrated: needs_human
