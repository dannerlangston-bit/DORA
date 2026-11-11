"""A/A calibration and threshold derivation (3.9, 4.8 threshold source, 7.13, 9.7, 10.6)."""
import copy
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import numpy as np
import pytest
import yaml

from sentinel import synth
from sentinel.canonical import calibration_key
from sentinel.diff.calibration import (
    STANDARD_FIX, CalibrationFile, CanaryMiss, Capture, CheckpointCalibration, FixItem, PairEvidence,
    RegionFloors, StoreCalibration, Tolerance, aa_drift, aa_false_positive_rate, aa_pairs, bbox_q4, business_days,
    calibrate_checkpoint, calibrated_keys, canary_misses, carry_over, carry_over_source, checkpoint_versions,
    classify_cause, derive_threshold, epoch_definition_sha256, exact_match, export_file, fault_separation,
    fix_list_item, fix_list_lines, has_calibration, invalidation, is_valid, live_exceptions, lookup, mask_version,
    noise_spread, open_fixes, overdue, region_tolerance, retune_tolerances, revoke_floor_exception,
    revoked_exceptions, settle_regression_finding, tolerance_changes, write_calibration,
)
from sentinel.diff.engine import DiffContext, diff_pair
from sentinel.diff.regions import UNCALIBRATED, FloorException, Landmark, RedactionBox, Rect, Region, RegionsFile
from sentinel.records import Fingerprint
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

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
    assert CalibrationFile("sha256:fp1", "run-cal-1", {CID: cal}).settle_p50_ms(CID) == 1150
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
    with pytest.raises(ValueError):
        calibrate_checkpoint(caps, registry(), data_mode="live")
    # a landmark lost in r6 is one fix-list item, not one per pair
    lost = copy.deepcopy(LAYOUT)
    synth.node(lost, "watchlist")["title"] = ""
    caps = [Capture(BASE.image.copy()) for _ in range(5)] + [Capture(synth.render(lost, SIZE).image)]
    cal = calibrate_checkpoint(caps, registry(), data_mode="frozen", now=NOW)
    assert [(i.cause, i.reason) for i in cal.fix_list] == [("landmark_jitter", "landmark:lm.watchlist")]


def test_lookup_cases_and_round_trip(tmp_path, noisy_calibration):
    zero = CheckpointCalibration(CID, "frozen")
    for rid in ("rgn.watchlist.grid", "_rest", "*"):
        zero.floors[rid], zero.thresholds[rid] = RegionFloors(), derive_threshold(RegionFloors())
    key2, key3 = "sha256:" + "2" * 64, "sha256:" + "3" * 64      # calibration keys (4.12)
    cf = CalibrationFile(key2, "run-cal-2", {CID: zero})
    assert cf.lookup(CID, "rgn.new-region") == (UNCALIBRATED, "uncalibrated")     # added since calibration
    assert cf.lookup("census/other/ws.a", "_rest") == (UNCALIBRATED, "uncalibrated")
    path = cf.save(tmp_path)
    back = CalibrationFile.load(tmp_path, key2)
    assert back.complete and back.lookup(CID, "rgn.watchlist.grid") == cf.lookup(CID, "rgn.watchlist.grid")
    assert path.name == "2" * 64 + ".yaml"                      # calibration/<hex>.yaml (SD-36, SD-69)
    assert CalibrationFile.load(tmp_path, "sha256:" + "0" * 64) is None
    noisy = CalibrationFile(key3, "run-cal-3", {CID: noisy_calibration})
    noisy.save(tmp_path)
    again = CalibrationFile.load(tmp_path, key3)
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


# -- cause classes (3.9 step 5) ---------------------------------------------------------------------------------

GX, GY, GW, GH = BASE.get("watchlist.grid").bbox
IMG = BASE.image
G, T = "rgn.watchlist.grid", "rgn.watchlist.title"


def _cal(images, mode="frozen", reg=None, **kw):
    return calibrate_checkpoint([Capture(i.copy(), time_to_settle_ms=1000) for i in images], reg or registry(),
                                data_mode=mode, now=NOW, **kw)


def _causes(images, mode="frozen", **kw):
    return [(i.region_id, i.cause) for i in _cal(images, mode, **kw).fix_list]


def test_cause_classes_read_off_the_pairs():
    """3.9 step 5: synthetic A/A captures of each pattern give their cause class and standard fix (choice 3)."""
    caret = IMG.copy()
    caret[GY + 20:GY + 32, GX + 60] = 250                                  # a blinking caret
    flash = IMG.copy()
    flash[GY + 30:GY + 36, GX + 40:GX + 46] = 255                          # one capture flashes a cell
    rows = IMG.copy()
    a, b = GY + 20, GY + 40
    rows[a:a + 14], rows[b:b + 14] = IMG[b:b + 14].copy(), IMG[a:a + 14].copy()   # two grid rows swap places
    park = (GX + 100, GY + 60)
    hover = IMG.copy()
    hover[park[1] + 10:park[1] + 14, park[0] + 10:park[0] + 14] = 200
    tip = IMG.copy()
    tip[park[1] + 12:park[1] + 30, park[0] + 4:park[0] + 50] = (250, 250, 210)
    live = [IMG.copy() for _ in range(6)]
    for k, c in enumerate(live):                                           # a quote that ticks every capture
        c[GY + 30:GY + 40, GX + 150 + 3 * k:GX + 154 + 3 * k] = 240
    odd = IMG.copy()
    for y, x in ((10, 20), (50, 90), (70, 200), (100, 30), (120, 250)):     # r6 alone differs, scattered
        odd[GY + y, GX + x] = 255 - odd[GY + y, GX + x]
    [blink] = _cal([IMG, caret] * 3).fix_list
    assert (blink.region_id, blink.cause, blink.standard_fix) == (G, "caret", "caret mask or focus drop")
    assert _causes([IMG, IMG, flash, IMG, IMG, IMG]) == [(G, "flash")]
    assert _causes([IMG, rows] + [IMG] * 4) == [(G, "sort")]
    assert _causes([IMG, hover, IMG, hover, IMG, IMG], park=park) == [(G, "hover")]
    assert _causes([IMG, tip, IMG, IMG, IMG, tip], park=park) == [(G, "tooltip")]
    assert _causes(live, "masked_live") == [(G, "live_data")]
    assert _causes([IMG] * 5 + [odd]) == [(G, "settle")]
    assert _causes([IMG] * 5 + [odd], cause_hints={G: "hover"}) == [(G, "hover")]       # a caller's hint wins
    assert set(STANDARD_FIX) == {"caret", "hover", "tooltip", "flash", "scroll", "sort", "live_data", "settle",
                                 "landmark_jitter", "unknown"}


def test_classify_cause_on_bare_evidence():
    """Translation, a shifted window and ambiguous noise (choice 3)."""
    m = np.zeros((40, 80), bool)
    assert classify_cause(PairEvidence((0, 5), m, shift_px=8)) == "scroll"
    assert classify_cause(PairEvidence((0, 5), m, shift_px=1)) == "landmark_jitter"
    assert classify_cause(PairEvidence((0, 5), whole=True, shift_px=1)) == "landmark_jitter"
    assert classify_cause([PairEvidence((0, 1))]) == "unknown"
    rng = np.random.default_rng(1)
    noisy = []
    for i, j in aa_pairs():                                                # scattered pixels in every pair
        c = np.zeros((40, 80), bool)
        c[rng.integers(0, 40, 4), rng.integers(0, 80, 4)] = True
        noisy.append(PairEvidence((i, j), c, data_mode="masked_live"))
    assert classify_cause(noisy) == "unknown"


# -- Hamming tolerance (9.7, 4.14) ------------------------------------------------------------------------------

def _h(bits):
    """A pHash with the low `bits` bits set: hamming(_h(0), _h(k)) == k."""
    return f"{(1 << bits) - 1:016x}"


def test_region_tolerance_from_noise_spread_and_fault_separation():
    """9.7: min(default, fault separation - 1); at or below the noise spread, 0 with the exact fallback."""
    assert noise_spread([(_h(0), _h(3))]) == 3 and fault_separation([_h(0), _h(5), _h(12)]) == 5
    assert region_tolerance([(_h(0), _h(3))], [_h(0), _h(5), _h(12)]) == Tolerance(4, False, 3, 5)
    assert region_tolerance([(_h(0), _h(2))], [_h(0), _h(6)]).hamming_tolerance == 5
    assert region_tolerance([(_h(0), _h(5))], [_h(0), _h(6)]) == Tolerance(0, True, 5, 6)
    assert region_tolerance([(_h(0), _h(1))], tier=1) == Tolerance(6, False, 1, None)   # no mutants: the default
    assert region_tolerance([(_h(0), _h(1))]).hamming_tolerance == 8
    assert region_tolerance([(_h(0), _h(9))]).exact_fallback                            # noisier than the default
    crop = IMG[GY:GY + 40, GX:GX + 80]
    assert noise_spread([(crop, crop.copy())]) == 0 and fault_separation([crop]) is None
    clean = _cal([IMG] * 6, tier=1)
    assert clean.tolerances[G] == Tolerance(6, False, 0, None) and "*" not in clean.tolerances
    t = _cal([IMG] * 6, mutant_crops={G: [_h(0), _h(5)]}).tolerances[G]
    assert (t.hamming_tolerance, t.fault_separation) == (4, 5)


def test_exact_fallback_matches_phash_and_the_4px_box():
    """9.7: at tolerance 0 two equal pHashes with different 4 px-quantized boxes do not match."""
    assert bbox_q4((5, 9, 10, 3)) == (4, 8, 12, 4)
    ph = "c3a5e1f0b4d29876"
    assert exact_match(ph, (4, 8, 12, 4), ph, (6, 10, 9, 2))
    assert not exact_match(ph, (4, 8, 12, 4), ph, (9, 10, 9, 2))
    assert not exact_match(ph, (4, 8, 12, 4), "c3a5e1f0b4d29877", (6, 10, 9, 2))
    assert not exact_match(ph, None, ph, (6, 10, 9, 2))
    lines = tolerance_changes({("c", "r"): Tolerance(8, False, 1, None)},
                              {("c", "r"): Tolerance(4, False, 3, 5), ("c", "s"): Tolerance(0, True, 5, 6)})
    assert len(lines) == 2 and "tolerance 8 -> Hamming 4" in lines[0] and "default -> exact pHash" in lines[1]
    assert tolerance_changes({("c", "r"): Tolerance(4, False, 3, 5)}, {("c", "r"): Tolerance(4, False, 3, 5)}) == []


# -- scoped validity (7.13, SD-69) ------------------------------------------------------------------------------

def two_regions(rect_t=None, mask=False):
    """The grid plus a title region on the same landmark; `mask` adds a mask inside the grid."""
    reg = registry()
    grid = reg.regions[0]
    regions = [grid, Region(T, "diff", "lm.watchlist", rect_t or Rect(0, 0, 40, 10))]
    if mask:
        regions.append(Region("rgn.watchlist.clock", "mask", "lm.watchlist",
                              Rect(grid.rect.x + 4, grid.rect.y + 4, 10, 10), mask_ref="mask-0001"))
    return RegionsFile(CID, list(reg.landmarks), regions)


WS_SHA = "sha256:" + "a" * 64
EPOCH = epoch_definition_sha256("frozen", {"data_delay": "delayed_20", "window_date": "2026-11-09"})


def vers(reg, **kw):
    return checkpoint_versions(reg, **{"redaction_version": "redaction-1-aaa", "workspace_sha256": WS_SHA,
                                       "epoch_definition_sha256": EPOCH, **kw})


def test_scoped_invalidation_follows_the_7_13_rows():
    """7.13: a region, mask or redaction change uncalibrates that region; a workspace or epoch definition change that
    checkpoint; the epoch's window date nothing (choices 6 and 7)."""
    base = vers(two_regions())
    measured = {rid: base.measured(rid, redacted=rid == T) for rid in (G, T, "_rest")}
    assert all(invalidation(m, base, rid) is None for rid, m in measured.items())
    moved = vers(two_regions(rect_t=Rect(0, 0, 44, 10)))
    assert invalidation(measured[T], moved, T) == "region" and invalidation(measured[G], moved, G) is None
    assert invalidation(measured["_rest"], moved, "_rest") == "region"
    masked = vers(two_regions(mask=True))
    assert invalidation(measured[G], masked, G) == "mask" and invalidation(measured[T], masked, T) is None
    v1, v2 = (vers(two_regions(mask=True), mask_versions={"mask-0001": v}) for v in ("v1", "v2"))
    assert invalidation(v1.measured(G, redacted=False), v2, G) == "mask"          # a renewed mask record
    red2 = vers(two_regions(), redaction_version="redaction-1-bbb")
    assert invalidation(measured[T], red2, T) == "redaction_version" and invalidation(measured[G], red2, G) is None
    assert invalidation(measured[G], dataclasses.replace(base, workspace_sha256="sha256:" + "b" * 64), G) == "workspace"
    live = epoch_definition_sha256("masked_live", {"data_delay": "delayed_20"})
    assert invalidation(measured[G], dataclasses.replace(base, epoch_definition_sha256=live), G) == "epoch_definition"
    assert epoch_definition_sha256("frozen", {"data_delay": "delayed_20", "window_date": "2026-11-10"}) == EPOCH
    assert mask_version({"mask_id": "m", "expires_at": "2026-12-01"}) != \
        mask_version({"mask_id": "m", "expires_at": "2026-12-31"})
    boxed = _cal([IMG] * 6, redactions=[RedactionBox(Rect(GX + 5, GY + 5, 20, 8), identity_field=True)])
    assert boxed.redacted == {G, "_rest", "*"} and _cal([IMG] * 6).redacted == set()


# -- the store (3.9 steps 3 to 6, 8.3) --------------------------------------------------------------------------

BUILD = "main-1998.2.0-4f1c9a2"
RUN = "cal-20261113"
KEY = "sha256:" + "4" * 64
TS = "2026-11-13T22:15:00+00:00"
MATCH = {
    "build_id": BUILD, "jre_release": {"sha256": "sha256:" + "1" * 64, "java_version": "17.0.12"},
    "vmoptions_hash": "sha256:" + "2" * 64, "java2d_pipeline": {"pinned": "software", "confirmed": "software"},
    "windows_build": {"current_build": "26100", "ubr": "2314"}, "display": {"width": 1920, "height": 1080, "dpi": 96},
    "cleartype": {"smoothing": True}, "gpu": {"name": "Hyper-V Video", "driver_version": "10.0"},
    "os_theme": {"apps_light": False}, "app_theme": {"scheme": "dark", "font_size": "medium"},
    "workspace_hash": {"ws.tier-a.swing-trader": WS_SHA}, "data_epoch": {"epoch_id": "sha256:" + "5" * 64},
    "harness": {"capture_lib": "mss-9.0.1", "settle_params_sha256": "sha256:" + "6" * 64,
                "diff_engine_params_sha256": "sha256:" + "7" * 64},
    "render_probe_hash": "e3b0c44298fc1c14",
}
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "calibration.schema.json").read_text())


def _plan(run_id):
    return RunPlan(run_id=run_id, run_type="calibration", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=3600,
                   scope=(), shards=(ShardPlan(f"{run_id}/1", "rig-01", sides="candidate", scope_count=0),),
                   started_at=TS)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-01", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.register_build(BUILD, channel="main", source_commit="4f1c9a2", pipeline_artifact_url="sim://b",
                     installer_sha256="c" * 64)
    s.begin_run(_plan(RUN))
    s.register_checkpoints([ScopeEntry(CID, "ws.tier-a.swing-trader", "swing_trader", 2, "frozen")],
                           config_commit="c0ffee")
    yield s
    s.close()


def _write(store, cals, key=KEY, run_id=RUN, now=NOW, **kw):
    return write_calibration(store, cals, calibration_key=key, fingerprint_id=kw.pop("fingerprint_id", None),
                             run_id=run_id, now=now, **kw)


def test_floors_and_fix_items_round_trip_through_the_store(store):
    """3.9 steps 3 to 6: one noisy region writes its 15 pair rows and one fix row with owner and opened date; the
    stored floors reproduce the thresholds; the key has no threshold until the item is closed."""
    cal = calibrate_checkpoint(noisy_captures(), registry(), data_mode="frozen", now=NOW, owner="jdoe")
    live = {CID: vers(registry())}
    _write(store, [cal], live=live)
    [row] = store.query("SELECT * FROM calibration_floors WHERE region_id = ?", (G,))
    pairs = json.loads(row["pairs_json"])
    assert row["samples"] == len(pairs) == 15 and row["diff_pixels_max"] == 6 and row["nonzero_on_frozen"] == 1
    assert all(len(p["settle_ms"]) == 2 for p in pairs) and row["settle_p50_ms"] == 1750
    assert row["workspace_sha256"] == WS_SHA and row["hamming_tolerance"] is not None
    entries = lookup(store, KEY, CID)
    assert set(entries) == {G, "_rest", "*"} and entries[G].threshold == cal.thresholds[G]
    [item] = open_fixes(store, KEY)
    assert (item.region_id, item.cause, item.owner, item.data_mode, item.opened_at) == (G, "unknown", "jdoe", "frozen",
                                                                                        NOW)
    sc = StoreCalibration(store, KEY, live=live)
    assert not sc.complete and sc.lookup(CID, "_rest") == (UNCALIBRATED, "uncalibrated")
    assert sc.request().to_dict()["reasons"] == {"incomplete": 1}
    sc = StoreCalibration(store, KEY, live=live, exceptions={CID: {G: "TOS-48200"}})   # a floor exception landed
    assert sc.complete and sc.lookup(CID, G) == (cal.thresholds[G], "calibrated")
    assert sc.settle_p50_ms(CID) == 1750 and sc.request() is None and sc.tolerance(CID, G) == cal.tolerances[G]
    res = diff_pair(IMG, IMG.copy(), registry(), calibration=sc, context=DiffContext(checkpoint_id=CID))
    assert res.threshold_source == "calibrated"                   # the Diff Engine reads the store
    assert sc.lookup(CID, "rgn.new") == (UNCALIBRATED, "uncalibrated")
    assert sc.request().regions == ((CID, "rgn.new", "missing"),)
    none = StoreCalibration(store, "sha256:" + "0" * 64)
    assert none.lookup(CID, G) == (UNCALIBRATED, "uncalibrated") and none.request().regions[0][2] == "no_calibration"
    assert has_calibration(store, KEY) and not has_calibration(store, "sha256:" + "0" * 64)


def test_a_stored_floor_is_uncalibrated_where_a_7_13_row_invalidates_it(store):
    """7.13, 05: change one region rect after calibration: that region is uncalibrated, the others keep theirs."""
    reg = two_regions()
    _write(store, [_cal([IMG] * 6, reg=reg)], live={CID: vers(reg)})
    moved = vers(two_regions(rect_t=Rect(0, 0, 44, 10)))
    sc = StoreCalibration(store, KEY, live={CID: moved})
    assert sc.lookup(CID, T) == (UNCALIBRATED, "uncalibrated") and sc.lookup(CID, G)[1] == "calibrated"
    assert sc.request().regions == ((CID, T, "region"),)
    entries = lookup(store, KEY, CID)
    assert is_valid(entries[G], moved) and is_valid(entries[T], None) and not is_valid(entries[T], moved)


def test_recalibration_keeps_the_opened_date_and_closes_fixed_items(store):
    """3.9 step 6: an item still there after a re-calibration keeps its age; a fixed one leaves the list."""
    _write(store, [calibrate_checkpoint(noisy_captures(), registry(), data_mode="frozen", now=NOW, owner="jdoe")])
    later = NOW + timedelta(days=7)
    store.begin_run(_plan("cal-20261120"))
    _write(store, [calibrate_checkpoint(noisy_captures(), registry(), data_mode="frozen", now=later, owner="jdoe")],
           run_id="cal-20261120", now=later)
    [item] = open_fixes(store, KEY)
    assert item.opened_at == NOW and item.age_business_days(later) == 5
    store.begin_run(_plan("cal-20261121"))
    _write(store, [_cal([IMG] * 6)], run_id="cal-20261121", now=later + timedelta(days=1))
    assert open_fixes(store, KEY) == [] and StoreCalibration(store, KEY).complete
    assert len(store.query("SELECT 1 FROM calibration_floors WHERE superseded_at IS NULL")) == 3


def test_a_failed_capture_item_belongs_to_the_key_of_its_runs_fingerprint(store):
    """Choice 8: an item without a floor is linked to its key through the run's recorded fingerprint."""
    store.write_fingerprint(Fingerprint(run_id=RUN, side="candidate", rig_id="rig-01", read_at=TS, match=MATCH,
                                        shard_id=f"{RUN}/1"))
    caps = [Capture(None, outcome="not_settled", reason="frames_unstable")] + noisy_captures()[1:]
    failed = calibrate_checkpoint(caps, registry(), data_mode="frozen", now=NOW, owner="jdoe")
    assert {i.region_id for i in failed.fix_list} == {None, G}            # the noisy region is not written: no floor
    _write(store, [failed], key=calibration_key(MATCH))
    assert store.query("SELECT count(*) FROM calibration_floors")[0][0] == 0      # no valid floor
    assert [i.outcome for i in open_fixes(store, calibration_key(MATCH))] == ["not_settled"]
    assert open_fixes(store, KEY) == [] and len(open_fixes(store)) == 1


def test_floors_carry_over_to_a_new_jre_after_a_passing_aa(store):
    """7.13, 4.12: a key that differs only in jre_release takes over the floors once the Nightly A/A passes."""
    fp = Fingerprint(run_id=RUN, side="candidate", rig_id="rig-01", read_at=TS, match=MATCH, shard_id=f"{RUN}/1")
    store.write_fingerprint(fp)
    new = {**MATCH, "jre_release": {"sha256": "sha256:" + "8" * 64, "java_version": "17.0.13"}}
    k_old, k_new = calibration_key(MATCH), calibration_key(new)
    _write(store, [_cal([IMG] * 6)], key=k_old, fingerprint_id=fp.fingerprint_id)
    assert carry_over_source(new, {k_old: MATCH}) == k_old
    assert carry_over_source({**MATCH, "gpu": {"name": "other"}}, {k_old: MATCH}) is None
    assert has_calibration(store, k_new, new) and not has_calibration(store, k_new)
    assert calibrated_keys(store) == {k_old} and calibrated_keys(store, new) == {k_old, k_new}   # fingerprint.g5
    assert carry_over(store, k_old, k_new, aa_passed=False, now=NOW) == 0
    assert not StoreCalibration(store, k_new).calibrated
    assert carry_over(store, k_old, k_new, aa_passed=True, now=NOW) == 3
    assert StoreCalibration(store, k_new).lookup(CID, G)[1] == "calibrated"
    assert StoreCalibration(store, k_old).lookup(CID, G)[1] == "calibrated"


def test_the_nightly_retunes_a_regions_tolerance(store):
    """9.7: the stored noise spread and new mutants' separation lower the tolerance, with a Nightly report line."""
    _write(store, [_cal([IMG] * 6)])
    crops = {(CID, G): [_h(0), _h(5)]}
    assert retune_tolerances(store, KEY, crops, {CID: 2}) == [
        f"{CID} {G}: tolerance 8 -> Hamming 4 (noise spread 0, fault separation 5)"]
    assert lookup(store, KEY, CID)[G].tolerance == Tolerance(4, False, 0, 5)
    assert retune_tolerances(store, KEY, crops, {CID: 2}) == []


def test_the_export_follows_the_4_16_schema(tmp_path, store):
    """4.16 calibration/<hex>.yaml: the export validates, sorts regions by floor, marks a failed checkpoint and is
    rebuilt from calibration_floors."""
    clean = _cal([IMG] * 6)
    doc = CalibrationFile(KEY, RUN, {CID: clean}, exported_at=NOW).to_dict()
    jsonschema.validate(doc, SCHEMA)
    assert doc["checkpoints"][0]["settle_p50_ms"] == 1000 and doc["exported_at"] == "2026-11-10T12:00:00+00:00"
    caps = [Capture(None, outcome="not_settled", reason="timeout")] + [Capture(IMG.copy())] * 5
    failed = CalibrationFile(KEY, RUN, {CID: calibrate_checkpoint(caps, registry(), data_mode="frozen")}).to_dict()
    assert failed["checkpoints"][0]["regions"] == [] and not CalibrationFile.from_dict(failed).complete
    _write(store, [calibrate_checkpoint(noisy_captures(), registry(), data_mode="frozen", now=NOW, owner="jdoe")])
    out = export_file(store, KEY, exported_at=NOW)
    jsonschema.validate(out.to_dict(), SCHEMA)
    assert not out.complete and out.to_dict()["checkpoints"][0]["regions"][0]["region_id"] == G   # largest floor first
    excepted = export_file(store, KEY, exported_at=NOW, exceptions={CID: {G: "TOS-48200"}})
    assert excepted.complete and excepted.to_dict()["checkpoints"][0]["regions"][0]["floor_exception"] is True
    path = excepted.save(tmp_path)
    assert yaml.safe_load(path.read_text())["calibration_key"] == KEY
    back = CalibrationFile.load(tmp_path, KEY)
    assert back.complete and back.lookup(CID, G) == excepted.lookup(CID, G)
    assert export_file(store, "sha256:" + "0" * 64, exported_at=NOW) is None


# -- the fix list in run health (3.9 step 6, 10.5) --------------------------------------------------------------

def test_fix_list_ages_in_business_days_and_the_frozen_quarantine():
    """3.9 step 6: item, cause, owner and age in business days; a frozen item older than 5 business days is due for
    quarantine (choice 11)."""
    friday = datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
    item = FixItem(CID, G, "live_data", owner="jdoe", opened_at=friday, data_mode="frozen")
    assert business_days(date(2026, 11, 13), date(2026, 11, 16)) == 1          # Friday to Monday
    assert item.age_business_days(date(2026, 11, 20)) == 5 and not item.overdue(date(2026, 11, 20))
    assert item.overdue(date(2026, 11, 23)) and overdue([item], date(2026, 11, 23)) == [item]
    assert not item.overdue(date(2026, 11, 23), holidays=[date(2026, 11, 19)])
    assert not dataclasses.replace(item, data_mode="masked_live").overdue(date(2026, 11, 23))
    closed = dataclasses.replace(item, region_id="rgn.other", exception_ref="TOS-1")
    assert fix_list_lines([item, closed], date(2026, 11, 17)) == [
        f"{CID} {G}: live_data, owner jdoe, 2 business days (standard fix: human re-declaration as rule or mask)"]
    assert fix_list_item([item], CID, G) == f"{CID} {G}: live_data, owner jdoe"
    assert fix_list_item([item], "census/other/ws.a") is None
    failed = FixItem(CID, None, "settle", outcome="not_settled", reason="frames_unstable")
    assert failed.label() == f"{CID} failed capture not_settled (frames_unstable)"
    docs = {CID: {"regions": [
        {"id": "rgn.a", "floor_exception": {"ticket": "T-1", "expires_at": "2026-12-01T00:00:00Z"}},
        {"id": "rgn.b", "floor_exception": {"ticket": "T-2", "expires_at": "2026-11-01T00:00:00Z"}}]}}
    assert live_exceptions(docs, NOW) == {CID: {"rgn.a": "T-1"}}


# -- settle_regression (3.5) and floor-exception revocation (3.9) -----------------------------------------------

def test_settle_regression_against_the_calibration_p50():
    """3.5: a run p50 of 2.5 s against a 1 s calibration p50 raises settle_regression for the owner; 1.9 s does not."""
    f = settle_regression_finding("run-01", CID, [2500, 2400, 2600], 1000.0, owner="jdoe", tier=2)
    assert f.kind == "settle_regression" and f.evidence["owner"] == "jdoe" and f.evidence["p50_ms"] == 2500
    assert settle_regression_finding("run-01", CID, [1900], 1000.0, owner="jdoe") is None
    assert settle_regression_finding("run-01", CID, [2500], None, owner="jdoe") is None


def test_a_canary_miss_on_a_floor_excepted_region_revokes_the_exception(store):
    """3.9: a mutant bound to a floor-excepted region that the path missed revokes the exception; the region stays
    uncalibrated while it carries that exception."""
    exc = FloorException("jdoe", "quote flash", "TOS-48200", NOW + timedelta(days=30))
    reg = registry(exc)
    located = {CID: {G: Rect(GX, GY, GW, GH)}}
    misses = [CanaryMiss(CID, "M05", region_ref=G), CanaryMiss(CID, "M07", footprint=(GX + 5, GY + 5, 4, 4))]
    [rev] = revoke_floor_exception(misses, {CID: reg}, located)
    assert (rev.region_id, rev.ticket, rev.mutant_ids) == (G, "TOS-48200", ("M05", "M07"))
    assert revoke_floor_exception(misses, {CID: registry()}, located) == []
    assert revoke_floor_exception([CanaryMiss(CID, "M07", footprint=(0, 0, 2, 2))], {CID: reg}, located) == []
    rows = [SimpleNamespace(state="missed", mutant_id="M05", checkpoint_id=CID),
            SimpleNamespace(state="detected", mutant_id="M06", checkpoint_id=CID)]
    muts = [SimpleNamespace(mutant_id="M05", checkpoint_id=CID, entry=SimpleNamespace(region_ref=G),
                            footprint=(1, 2, 3, 4))]
    assert canary_misses(rows, muts) == [CanaryMiss(CID, "M05", G, (1, 2, 3, 4))]
    f = rev.finding(RUN)
    assert f.kind == "canary_miss" and f.evidence["revokes_floor_exception"] and f.region_id == G
    store.write_finding(f)
    assert revoked_exceptions(store) == {(CID, G, "TOS-48200")}
    _write(store, [calibrate_checkpoint(noisy_captures(), reg, data_mode="masked_live", now=NOW)])
    sc = StoreCalibration(store, KEY, live={CID: checkpoint_versions(reg)}, revoked=revoked_exceptions(store))
    assert sc.complete and sc.lookup(CID, G) == (UNCALIBRATED, "uncalibrated")
    assert sc.lookup(CID, "_rest")[1] == "calibrated" and sc.request().regions == ((CID, G, "revoked"),)
    redeclared = checkpoint_versions(registry(dataclasses.replace(exc, ticket="TOS-48999")))   # a new approval
    assert StoreCalibration(store, KEY, live={CID: redeclared},
                            revoked=revoked_exceptions(store)).lookup(CID, G)[1] == "calibrated"
