"""The Diff Engine (3.3, 3.7, 3.8, 4.12) on synthetic base/candidate pairs with known changes."""
import copy
import dataclasses
from datetime import datetime, timezone

import numpy as np
import pytest

from sentinel import synth
from sentinel.diff.calibration import CalibrationFile, CheckpointCalibration, RegionFloors, derive_threshold
from sentinel.diff.engine import (
    DiffContext, GlobalShiftSignal, compare_fingerprints, diff_pair, fingerprint_id, global_shift_prepass,
    locate_landmark, max_delta, pixel_diff, yiq_delta,
)
from sentinel.diff import load_params
from sentinel.diff.regions import FloorException, Landmark, Rect, RedactionBox, Region, RegionsFile, park_rect
from sentinel.records import DiffSummary, Finding

CID = "census/trade.order-entry.stock/ws.tier-a.active-trader"
SIZE = (640, 400)
LAYOUT = synth.sample_layout(0)
BASE = synth.render(LAYOUT, SIZE)
P = load_params()


def _landmark(screen, panel, pad=2):
    x, y, _, _ = screen.get(f"{panel}.title.label").bbox
    return Landmark(f"lm.{panel}", screen.crop(f"{panel}.title.label", pad), expected=(x - pad, y - pad))


def _region(rid, component, anchor, kind="diff", pad=2, screen=BASE, **kw):
    """A region over a drawn component, as an offset from the landmark of panel `anchor`."""
    lx, ly, _, _ = screen.get(f"{anchor}.title.label").bbox
    x, y, w, h = screen.get(component).bbox
    return Region(rid, kind, f"lm.{anchor}", Rect(x - lx + pad, y - ly + pad, w, h), **kw)


def registry(whole_positions=True, extra=()):
    positions = (_region("rgn.positions.panel", "positions", "positions", panel="position_statement")
                 if whole_positions else _region("rgn.positions.grid", "positions.grid", "positions",
                                                 panel="position_statement"))
    regions = [
        _region("rgn.watchlist.grid", "watchlist.grid", "watchlist"),
        positions,
        _region("rgn.ticket.price", "ticket.price", "ticket", panel="order_ticket", money_cell=True),
        _region("rgn.ticket.qty", "ticket.qty", "ticket", panel="order_ticket", money_cell=True),
        _region("rgn.ticket.send", "ticket.send", "ticket", panel="order_ticket", money_cell=True),
        _region("rgn.ticket.cost", "ticket.cost", "ticket", panel="order_ticket"),
        *extra,
    ]
    return RegionsFile(CID, [_landmark(BASE, p) for p in ("watchlist", "positions", "ticket")], regions)


def calibrated(reg, missing=()):
    """A complete calibration file with zero floors for every region except `missing`."""
    cal = CheckpointCalibration(CID, "frozen")
    for rid in [r.id for r in reg.regions if r.pixel_diffed and r.id not in missing] + ["_rest", "*"]:
        cal.floors[rid] = RegionFloors()
        cal.thresholds[rid] = derive_threshold(RegionFloors())
    return CalibrationFile("sha256:fp", "run-cal-1", {CID: cal})


def mutated(change):
    layout = copy.deepcopy(LAYOUT)
    change(layout)
    return synth.render(layout, SIZE)


def run(candidate, reg=None, **kw):
    return diff_pair(BASE.image, candidate, reg if reg is not None else registry(),
                     context=DiffContext(run_id="run-t", checkpoint_id=CID, tier=1), **kw)


# -- granularity 2: pixelmatch-style per-pixel diff -------------------------------------------

def _reference_pixelmatch(a, b, t=0.1):
    """A direct port of pixelmatch's loops (antialiased, hasManySiblings), as an oracle."""
    h, w = a.shape[:2]
    md = 35215 * t * t

    def lum(p):
        return p[0] * 0.29889531 + p[1] * 0.58662247 + p[2] * 0.11448223

    def many(img, x1, y1):
        x0, y0, x2, y2 = max(x1 - 1, 0), max(y1 - 1, 0), min(x1 + 1, w - 1), min(y1 + 1, h - 1)
        z = 1 if x1 in (x0, x2) or y1 in (y0, y2) else 0
        for x in range(x0, x2 + 1):
            for y in range(y0, y2 + 1):
                if (x, y) != (x1, y1) and (img[y, x] == img[y1, x1]).all():
                    z += 1
                    if z > 2:
                        return True
        return False

    def aa(img, x1, y1, img2):
        x0, y0, x2, y2 = max(x1 - 1, 0), max(y1 - 1, 0), min(x1 + 1, w - 1), min(y1 + 1, h - 1)
        z = 1 if x1 in (x0, x2) or y1 in (y0, y2) else 0
        mn = mx = 0.0
        mnp = mxp = None
        for x in range(x0, x2 + 1):
            for y in range(y0, y2 + 1):
                if (x, y) == (x1, y1):
                    continue
                d = 0.0 if (img[y1, x1] == img[y, x]).all() else lum(img[y1, x1].astype(float)) - lum(img[y, x].astype(float))
                if d == 0:
                    z += 1
                    if z > 2:
                        return False
                elif d < mn:
                    mn, mnp = d, (x, y)
                elif d > mx:
                    mx, mxp = d, (x, y)
        if mn == 0 or mx == 0:
            return False
        return (many(img, *mnp) and many(img2, *mnp)) or (many(img, *mxp) and many(img2, *mxp))

    changed = np.zeros((h, w), bool)
    aam = np.zeros((h, w), bool)
    delta = yiq_delta(a, b)
    for y in range(h):
        for x in range(w):
            if delta[y, x] > md:
                if aa(a, x, y, b) or aa(b, x, y, a):
                    aam[y, x] = True
                else:
                    changed[y, x] = True
    return changed, aam


def test_yiq_threshold_is_35215_t_squared():
    assert max_delta(P) == pytest.approx(352.15)
    px = np.array([[[10, 200, 30]]], np.uint8)
    assert yiq_delta(px, px)[0, 0] == 0


def test_antialiasing_detection_matches_pixelmatch():
    gamma = synth.render(LAYOUT, SIZE, text_gamma=2.0).image
    for (x, y, w, h) in ((10, 60, 70, 40), (480, 350, 70, 30)):
        a, b = BASE.image[y:y + h, x:x + w], gamma[y:y + h, x:x + w]
        changed, aa = pixel_diff(a, b, P)
        ref_changed, ref_aa = _reference_pixelmatch(a, b)
        assert aa.any() and changed.any()
        assert np.array_equal(changed, ref_changed) and np.array_equal(aa, ref_aa)


def test_edge_tolerance_ignores_a_one_pixel_edge_move():
    a = np.full((20, 20, 3), 20, np.uint8)
    b = a.copy()
    a[:, 8] = 220
    b[:, 9] = 220
    assert pixel_diff(a, b, P)[0].sum() == 40
    assert pixel_diff(a, b, P, edge_tolerance_px=1)[0].sum() == 0


# -- whole results ------------------------------------------------------------------------------

def test_identical_is_zero_with_no_findings():
    res = run(BASE.image.copy(), calibration=calibrated(registry()))
    assert res.level == "zero" and res.terminal is None and res.findings == []
    assert len(res.regions) == 7 and not any(m.above_threshold or m.changed_px for m in res.regions)
    assert res.region("rgn.ticket.send").base_rect == Rect(*BASE.get("ticket.send").bbox)
    assert isinstance(res.summary, DiffSummary) and res.summary.whole_pixel_fraction == 0.0
    assert res.threshold_source == "calibrated"
    assert res.heatmap.shape == BASE.image.shape


def test_sub_threshold_noise_changes_no_pixel():
    res = run(synth.perturb(BASE.image, seed=1, count=3000, amplitude=3))
    assert res.level == "zero" and res.summary.components == []


def test_sub_floor_noise_is_below_threshold_on_a_calibrated_zero_floor():
    noisy = synth.perturb(BASE.image, seed=2, count=3, amplitude=90, rect=(20, 70, 200, 60))
    res = run(noisy, calibration=calibrated(registry()))
    grid = res.region("rgn.watchlist.grid")
    assert grid.changed_px == 3 and grid.components == 0 and not grid.above_threshold
    assert res.level == "below" and res.threshold_source == "calibrated"


@pytest.mark.parametrize("dx", [2, 4, 6])
def test_panel_shift_is_a_landmark_move_not_a_pixel_storm(dx):
    cand = mutated(lambda L: synth.node(L, "positions")["rect"].__setitem__(0, 318 + dx))
    res = run(cand.image, calibration=calibrated(registry()))
    panel = res.region("rgn.positions.panel")
    assert res.level == "above"
    assert panel.shift == (dx, 0) and "translation" in panel.tests and panel.changed_px == 0
    moved = res.findings_of("landmark_moved")
    assert [(f.evidence["landmark"], f.evidence["dx"], f.evidence["dy"]) for f in moved] == [("lm.positions", dx, 0)]
    assert res.summary.components == []


def test_content_shift_inside_a_region_is_a_layout_shift_finding():
    cand = mutated(lambda L: synth.node(L, "positions.grid")["rect"].__setitem__(1, 7))
    res = run(cand.image, reg=registry(whole_positions=False))
    shifts = res.findings_of("layout_shift")
    assert [(f.region_id, f.evidence["dx"], f.evidence["dy"]) for f in shifts] == [("rgn.positions.grid", 0, 3)]
    assert res.region("rgn.positions.grid").above_threshold and res.level == "above"


def test_whole_window_shift_is_one_layout_shift_and_no_region_findings():
    cand = np.empty_like(BASE.image)
    cand[:, 1:] = BASE.image[:, :-1]
    cand[:, 0] = BASE.image[:, 0]
    res = run(cand, calibration=calibrated(registry()))
    assert res.summary.shift == (1, 0) and res.level == "above"
    assert [(f.kind, f.region_id) for f in res.findings] == [("layout_shift", None)]
    assert res.summary.components == [] and not any(m.above_threshold for m in res.regions)


def test_erased_button_is_one_large_component_on_the_money_cell():
    def erase(L):
        t = synth.node(L, "ticket")
        t["children"] = [c for c in t["children"] if c.get("name") != "ticket.send"]
    res = run(mutated(erase).image, calibration=calibrated(registry()))
    send = res.region("rgn.ticket.send")
    assert send.above_threshold and send.components == 1 and send.largest_component > 2000
    assert [c.region_id for c in res.components] == ["rgn.ticket.send"]
    assert res.components[0].phash != res.components[0].base_phash
    assert res.level == "above" and res.threshold_source == "calibrated"


def test_swapped_digit_survives_as_a_small_component():
    res = run(mutated(lambda L: synth.node(L, "ticket.price").__setitem__("text", "150.52")).image,
              calibration=calibrated(registry()))
    comps = [c for c in res.components if c.region_id == "rgn.ticket.price"]
    assert len(comps) >= 1 and all(6 <= c.area <= 120 for c in comps)
    assert res.region("rgn.ticket.price").above_threshold and res.level == "above"


def test_red_green_swap_is_a_color_semantic_flip():
    def swap(L):
        for row in synth.node(L, "positions.grid")["rows"]:
            row[3]["color"] = "red" if row[3]["color"] == "green" else "green"
    res = run(mutated(swap).image, calibration=calibrated(registry()))
    flips = [c for c in res.components if c.color_semantic_flip]
    assert len(flips) == 5 and {c.region_id for c in flips} == {"rgn.positions.panel"}
    assert all(c.mean_delta_e > 20 for c in flips)


def test_color_tint_below_the_yiq_threshold_is_a_color_shift_finding():
    tinted = BASE.image.astype(int)
    tinted[..., 0] += 6
    res = run(np.clip(tinted, 0, 255).astype(np.uint8), calibration=calibrated(registry()))
    shifted = {f.region_id for f in res.findings_of("color_shift")}
    assert {"rgn.watchlist.grid", "rgn.positions.panel", "_rest"} <= shifted
    assert res.summary.components == [] and res.level == "above"
    assert all(f.evidence["delta_e"] > 2.0 for f in res.findings_of("color_shift"))


def _ladder_pair(size):
    b = synth.render(synth.active_trader_cell_layout(size, "base"), size)
    c = synth.render(synth.active_trader_cell_layout(size, "candidate"), size)
    lx, ly, _, _ = b.get("grid.cell00.header.label").bbox
    lm = Landmark("lm.cell-header", b.crop("grid.cell00.header.label", 2), expected=(lx - 2, ly - 2))

    def rel(name):
        x, y, w, h = b.get(name).bbox
        return Rect(x - lx + 2, y - ly + 2, w, h)
    cid = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/%dx%d" % size
    reg = RegionsFile(cid, [lm], [Region("rgn.ladder.order-strip", "diff", "lm.cell-header", rel("ladder.strip")),
                                  Region("rgn.ladder.viewport", "diff", "lm.cell-header", rel("ladder.viewport"))])
    return b, c, reg


def test_ladder_overlap_of_the_worked_example_at_480x360():
    b, c, reg = _ladder_pair((480, 360))
    res = diff_pair(b.image, c.image, reg, context=DiffContext(checkpoint_id=reg.checkpoint_id))
    assert res.level == "above" and res.terminal is None
    strip = res.region("rgn.ladder.order-strip")
    assert strip.above_threshold and strip.components > 0
    # a strip component sits on the lower half of BUY MKT, where the first price row now draws
    bx, by, bw, bh = c.get("ladder.buy").bbox
    lower = Rect(bx, by + bh // 2, bw, bh - bh // 2)
    assert any(Rect(*k.bbox).intersect(lower) for k in res.components if k.region_id == "rgn.ladder.order-strip")
    # the viewport starts 20 px higher (58 to 38) and the cell header lost 6 px
    assert res.region("rgn.ladder.viewport").shift == (0, -20)
    assert [(f.evidence["dx"], f.evidence["dy"]) for f in res.findings_of("landmark_moved")] == [(0, -3)]


def test_ladder_without_overlap_still_exceeds_threshold_on_the_viewport():
    b, c, reg = _ladder_pair((480, 480))
    res = diff_pair(b.image, c.image, reg, context=DiffContext(checkpoint_id=reg.checkpoint_id))
    assert res.region("rgn.ladder.viewport").above_threshold and res.level == "above"


def test_landmark_missing_refuses_the_whole_checkpoint():
    cand = mutated(lambda L: synth.node(L, "ticket").__setitem__("title", ""))
    res = run(cand.image)
    assert res.terminal is not None and res.level is None and res.regions == []
    assert (res.terminal.outcome, res.terminal.reason, res.terminal.side) == ("landmark_missing", "landmark:lm.ticket", "candidate")


def test_an_ambiguous_landmark_is_not_found():
    img = np.full((60, 200, 3), 20, np.uint8)
    img[20:30, 20:40] = 200
    img[20:30, 120:140] = 200
    tpl = img[15:35, 15:45].copy()
    m = locate_landmark(img, Landmark("lm.twice", tpl), P)
    assert not m.found and m.score > 0.99 and m.second >= 0.85


def test_window_size_change_pads_counts_and_reports_geometry():
    taller = synth.render(LAYOUT, (640, 410))
    res = run(taller.image, calibration=calibrated(registry()))
    geo = res.findings_of("window_geometry_changed")
    assert len(geo) == 1 and geo[0].evidence == {"base_size": [640, 400], "candidate_size": [640, 410]}
    assert res.level == "above" and res.region("_rest").changed_px >= 640 * 10 - 50


def test_masked_and_redacted_pixels_are_never_diffed():
    def change_bp(L):
        synth.node(L, "ticket.bp")["text"] = "-15,052.00"
        synth.node(L, "watchlist.grid")["rows"][0][1] = "999.99"
    cand = mutated(change_bp).image
    mask = _region("rgn.ticket.bp", "ticket.bp", "ticket", kind="mask", panel="order_ticket", mask_ref="msk.bp")
    wx, wy, ww, wh = BASE.get("watchlist.grid.r0c1").bbox
    res = run(cand, reg=registry(extra=[mask]), redactions=[RedactionBox(Rect(wx, wy, ww, wh))],
              calibration=calibrated(registry()))
    assert res.terminal is None and res.level == "zero"
    assert run(cand, calibration=calibrated(registry())).level == "above"


def test_parking_rectangle_is_a_system_mask_on_both_sides():
    cursor = BASE.image.copy()
    cursor[388:396, 626:634] = 255                       # a tooltip remnant at the parking coordinate
    assert run(cursor).level == "above"
    res = run(cursor, system_masks=[park_rect((630, 392))])
    assert res.level == "zero" and res.terminal is None


def test_mask_over_budget_is_harness_error():
    big = _region("rgn.ticket.captions", "ticket.bp.caption", "ticket", kind="mask", panel="order_ticket",
                  mask_ref="msk.captions")
    big = dataclasses.replace(big, rect=Rect(big.rect.x, big.rect.y, 300, 60))
    # identical pixels short-circuit before the budget (5.8), so the candidate carries a change
    cand = mutated(lambda L: synth.node(L, "ticket.cost").__setitem__("text", "15,025.01")).image
    res = run(cand, reg=registry(extra=[big]))
    assert res.terminal is not None and res.level is None
    assert (res.terminal.outcome, res.terminal.reason) == ("harness_error", "mask_budget_exceeded")
    assert "order_ticket" in res.terminal.detail
    assert [f.kind for f in res.findings] == ["mask_budget_exceeded"]


def test_mask_on_a_money_cell_is_harness_error_but_identity_redaction_is_not_budgeted():
    on_qty = _region("rgn.ticket.qty-mask", "ticket.qty", "ticket", kind="mask", panel="order_ticket",
                     mask_ref="msk.qty")
    cand = mutated(lambda L: synth.node(L, "ticket.cost").__setitem__("text", "15,025.01")).image
    res = run(cand, reg=registry(extra=[on_qty]))
    assert res.terminal.reason == "mask_budget_exceeded" and "rgn.ticket.qty" in res.terminal.detail
    tx, ty, _, _ = BASE.get("ticket.bp.caption").bbox
    identity = RedactionBox(Rect(tx, ty, 200, 40), identity_field=True, anchor_id="red.account")
    assert run(cand, redactions=[identity]).terminal is None
    plain = RedactionBox(Rect(tx, ty, 200, 40), anchor_id="red.order-id")
    assert run(cand, redactions=[plain]).terminal.reason == "mask_budget_exceeded"


def test_uncalibrated_region_is_diffed_at_threshold_zero():
    added = _region("rgn.ticket.bp", "ticket.bp", "ticket", panel="order_ticket")
    reg = registry(extra=[added])
    cal = calibrated(reg, missing=("rgn.ticket.bp",))
    bx, by, bw, bh = BASE.get("ticket.bp").bbox
    one_px = BASE.image.copy()
    one_px[by + 2, bx + 2] = (255, 255, 255)
    res = run(one_px, reg=reg, calibration=cal)
    bp = res.region("rgn.ticket.bp")
    assert bp.threshold_source == "uncalibrated" and bp.above_threshold and bp.changed_px == 1
    assert res.level == "above" and res.threshold_source == "uncalibrated"
    # the same single pixel in a calibrated zero-floor region is below threshold
    cx, cy, _, _ = BASE.get("ticket.cost").bbox
    other = BASE.image.copy()
    other[cy + 2, cx + 2] = (255, 255, 255)
    res = run(other, reg=reg, calibration=cal)
    assert res.level == "below" and res.threshold_source == "calibrated"
    # no calibration file at all: every region is uncalibrated
    assert run(other, reg=reg).threshold_source == "uncalibrated"
    assert run(other, reg=reg).level == "above"


def test_money_cell_any_changed_pixel_is_above():
    px, py, _, _ = BASE.get("ticket.price").bbox
    img = BASE.image.copy()
    img[py + 1, px + 1] = (255, 255, 255)
    res = run(img, calibration=calibrated(registry()))
    assert res.region("rgn.ticket.price").above_threshold and res.level == "above"


def test_nonzero_floor_applies_only_with_a_live_floor_exception():
    reg = registry()
    cal = calibrated(reg)
    cp = cal.checkpoints[CID]
    floors = RegionFloors(changed_px=5)
    cp.floors["rgn.watchlist.grid"], cp.thresholds["rgn.watchlist.grid"] = floors, derive_threshold(floors)
    noisy = synth.perturb(BASE.image, seed=3, count=4, amplitude=90, rect=(20, 70, 200, 60))
    assert run(noisy, reg=reg, calibration=cal).region("rgn.watchlist.grid").threshold_source == "uncalibrated"
    exc = FloorException("jdoe", "flash", "TOS-9", datetime(2030, 1, 1, tzinfo=timezone.utc))
    regions = [dataclasses.replace(r, floor_exception=exc) if r.id == "rgn.watchlist.grid" else r for r in reg.regions]
    res = run(noisy, reg=RegionsFile(CID, reg.landmarks, regions), calibration=cal)
    grid = res.region("rgn.watchlist.grid")
    assert grid.threshold_source == "calibrated" and grid.threshold.changed_px_min == 10 and not grid.above_threshold


def test_findings_are_taxonomy_records_with_stable_ids():
    cand = mutated(lambda L: synth.node(L, "positions")["rect"].__setitem__(0, 322))
    a, b = run(cand.image), run(cand.image)
    assert all(isinstance(f, Finding) for f in a.findings)
    assert [f.finding_id for f in a.findings] == [f.finding_id for f in b.findings]
    assert all(f.checkpoint_id == CID and f.run_id == "run-t" and f.tier == 1 for f in a.findings)


def test_heatmap_paints_changed_pixels_red_on_a_dimmed_base():
    res = run(mutated(lambda L: synth.node(L, "ticket.price").__setitem__("text", "150.52")).image)
    hm = res.heatmap
    ys, xs = np.nonzero(res.changed_map)
    assert hm.shape == BASE.image.shape and (hm[ys, xs] == (255, 0, 0)).all()
    assert hm[5, 600].max() <= int(BASE.image[5, 600].max() * 0.4) + 1


# -- granularity 0 ---------------------------------------------------------------------------

def test_global_text_rendering_shift_prepass():
    shifted = run(synth.render(LAYOUT, SIZE, text_gamma=2.0).image, calibration=calibrated(registry()))
    sig = shifted.signal
    assert sig.above_floor and sig.text_edge_only and sig.text_edge_px > 0.7 * sig.changed_px
    clean = GlobalShiftSignal("x", "zero", False, 0, 0, True)
    signals = [dataclasses.replace(sig, checkpoint_id=f"census/s{i}/ws.a") for i in range(5)]
    signals += [dataclasses.replace(clean, checkpoint_id=f"census/c{i}/ws.a") for i in range(20)]
    decision = global_shift_prepass(signals)
    assert decision.raised and decision.collapse == tuple(f"census/s{i}/ws.a" for i in range(5))
    finding = decision.finding("run-t")
    assert finding.kind == "global_rendering_shift" and len(finding.evidence["checkpoints"]) == 5
    nightly = global_shift_prepass(signals, nightly=True)
    assert (nightly.terminal.outcome, nightly.terminal.reason) == ("harness_error", "aa_drift_global")
    assert not global_shift_prepass(signals[:19]).evaluated

    def erase(L):
        t = synth.node(L, "ticket")
        t["children"] = [c for c in t["children"] if c.get("name") != "ticket.send"]
    erased = run(mutated(erase).image).signal
    assert not erased.text_edge_only
    real = [dataclasses.replace(erased, checkpoint_id=f"census/e{i}/ws.a") for i in range(5)] + signals[5:]
    assert not global_shift_prepass(real).raised


# -- 3.3, 4.12 comparability -----------------------------------------------------------------

def _fp(**match):
    base = {"build_id": "main-1", "jre_release": {"sha256": "a", "java_version": "17"}, "data_epoch": "E1",
            "render_probe_hash": "e3b0c44298fc1c14", "display": {"width": 1920, "height": 1080}}
    base.update(match)
    return {"match": base}


def test_fingerprints_must_match_except_build_id():
    assert compare_fingerprints(_fp(), _fp(build_id="branch-2")) is None
    t = compare_fingerprints(_fp(), _fp(data_epoch="E2", build_id="branch-2"))
    assert (t.outcome, t.reason) == ("not_comparable", "fingerprint_mismatch") and t.detail == "field: data_epoch"
    t = compare_fingerprints(_fp(), _fp(render_probe_hash="e3b0c44298fc1c15"))
    assert "render_probe_hash" in t.detail
    assert fingerprint_id(_fp()["match"]) == fingerprint_id(_fp(build_id="other")["match"])
    assert fingerprint_id(_fp()["match"]) != fingerprint_id(_fp(data_epoch="E2")["match"])


# -- component boxes in window and region coordinates (choice 16, 9.7) --------------------------

def moved_panel_pair(origin, pl="+412.50", cid=CID):
    """A positions panel at `origin` in an otherwise empty window, base and candidate (the candidate's
    first P/L cell reads `pl`), with the panel's own landmark and region: one census checkpoint."""
    def layout(text):
        L = copy.deepcopy(LAYOUT)
        panel = synth.node(L, "positions")
        panel["rect"][:2] = list(origin)
        synth.node(panel, "positions.grid")["rows"][0][3] = {"text": text, "color": "green"}
        L["children"] = [panel]
        return L
    b, c = synth.render(layout("+12.50"), SIZE), synth.render(layout(pl), SIZE)
    reg = RegionsFile(cid, [_landmark(b, "positions")],
                      [_region("rgn.positions.panel", "positions", "positions", screen=b)])
    return b, c, reg


def test_components_carry_window_and_region_relative_boxes():
    results = []
    for origin in ((318, 6), (120, 190)):
        b, c, reg = moved_panel_pair(origin)
        res = diff_pair(b.image, c.image, reg, context=DiffContext(checkpoint_id=CID))
        assert res.level == "above" and res.components
        results.append(res)
    (a, b) = ([c for c in r.components if c.region_id == "rgn.positions.panel"] for r in results)
    assert [c.bbox_region for c in a] == [c.bbox_region for c in b]           # same place in the panel
    assert a[0].bbox != b[0].bbox
    lx, ly, _, _ = moved_panel_pair((318, 6))[0].get("positions").bbox
    assert a[0].bbox_region == (a[0].bbox[0] - lx, a[0].bbox[1] - ly, a[0].bbox[2], a[0].bbox[3])
    summary = results[0].summary.components[0]
    assert (summary.bbox, summary.bbox_region) == (a[0].bbox, a[0].bbox_region)   # DiffSummary keeps both
    rest = run(mutated(lambda L: synth.node(L, "main").__setitem__("title", "Main@thinkorswim [build 1998.3.1]")).image)
    assert rest.components and all(c.bbox_region == c.bbox for c in rest.components if c.region_id == "_rest")
