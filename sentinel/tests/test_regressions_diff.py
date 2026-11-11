"""Regressions for the review of the Diff Engine, synth and canary (D-01 to D-12), plus the synthetic
global-shift signal of the Nightly run (10.3 Injection).

Each test failed before its fix. The finding id and spec section are in each docstring.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from sentinel import canary as C
from sentinel import synth
from sentinel.diff.calibration import CalibrationFile, Capture, aa_drift, aa_false_positive_rate, calibrate_checkpoint
from sentinel.diff.engine import DiffContext, GlobalShiftSignal, diff_pair, global_shift_prepass
from sentinel.diff.regions import (
    FloorException, Landmark, RedactionBox, Rect, Region, RegionsFile, RegionThreshold, park_rect,
)
from sentinel.diff.settle import Anchor, Frame, SettleSpec, settle
from sentinel.records import CheckpointOutcome

POSITIONS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
S06 = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06"
NIGHTLY = "run-2026-11-10-nightly-0001"


def _screen() -> synth.Screen:
    return synth.render(synth.sample_layout(), (640, 400))         # RGB, the Diff Engine's input format


# -- D-01: canary recipes in RGB, the channel order of the engine, synth and the template loader ---------

M07_CATALOG = f"""
- mutant_id: M07
  checkpoint_id: {POSITIONS}
  class: color_swap
  mode: image
  money_cell: false
  generator: {{op: channel_swap, params: {{}}, seed: 20260101}}
  region_ref: rgn.positions.pl-open
  expected: {{outcomes: [invariant_violation], invariant_id: INV-15}}
  owner: sentinel-canaries
"""


def _pl_column(s: synth.Screen) -> tuple[int, int, int, int]:
    cells = [c for c in s.components if c.name.startswith("positions.grid.r") and c.name.endswith("c3")]
    x0, y0 = min(c.bbox[0] for c in cells), min(c.bbox[1] for c in cells)
    x1, y1 = max(c.bbox[0] + c.bbox[2] for c in cells), max(c.bbox[1] + c.bbox[3] for c in cells)
    return x0, y0, x1 - x0, y1 - y0


def test_d01_color_swap_turns_green_pl_red_on_an_rgb_capture():
    """10.3 class table: color_swap (M07) swaps the red and green channels of the P/L column's colored
    pixels. On an RGB capture the formerly green ink must come out red-dominant, not blue."""
    s = _screen()
    box = _pl_column(s)
    out = C.channel_swap(s.image, box, C.mutant_rng(C.SEED, "M07"))
    x, y, w, h = box
    before = s.image[y:y + h, x:x + w].astype(int)
    after = out[y:y + h, x:x + w].astype(int)
    green = (before[..., 1] - before[..., 0] > 100) & (before[..., 1] - before[..., 2] > 60)
    assert green.any()
    assert (after[green][:, 0] > after[green][:, 1]).all(), f"green {before[green][0]} became {after[green][0]}"


def test_d01_m07_mutant_is_seen_by_the_diff_engine_as_a_green_red_flip():
    """3.7 granularity 3: `color_semantic_flip` when base pixels sit in the green band and candidate
    pixels in the red band, or the reverse. The M07 mutant generated from the catalog flips the column."""
    s = _screen()
    gen = C.generate(C.parse_catalog(M07_CATALOG).entries, {POSITIONS: s.image},
                     {POSITIONS: {"rgn.positions.pl-open": _pl_column(s)}})
    m = gen.mutants[0]
    assert not m.failed, m.error
    res = diff_pair(s.image, m.image, context=DiffContext(checkpoint_id=POSITIONS))
    assert res.level == "above"
    assert res.components and all(c.color_semantic_flip for c in res.components), \
        [(c.area, c.color_semantic_flip) for c in res.components]


def test_d01_m07_on_one_green_pl_cell_is_a_green_to_red_flip():
    """The canonical M07 direction: a positive (green) P/L cell turns red. The swapped synth green
    (24, 196, 92) -> (196, 24, 92) sits at hue 330 to 336 degrees, which the red band must take in."""
    s = _screen()
    green_cells = [c for c in s.components if c.name.startswith("positions.grid.r") and c.name.endswith("c3")
                   and s.image[c.bbox[1]:c.bbox[1] + c.bbox[3], c.bbox[0]:c.bbox[0] + c.bbox[2], 1].max() > 120]
    assert green_cells
    for cell in green_cells:
        out = C.channel_swap(s.image, cell.bbox, C.mutant_rng(C.SEED, "M07"))
        res = diff_pair(s.image, out, context=DiffContext(checkpoint_id=POSITIONS))
        assert res.level == "above" and any(c.color_semantic_flip for c in res.components), cell.name


def test_d01_global_shift_red_channel_delta_raises_red_on_an_rgb_capture():
    """10.3: global_shift (M23) adds 6 to the red channel everywhere: channel 0 in RGB, never channel 2."""
    s = _screen()
    out = C.global_shift(s.image, (0, 0, 640, 400), C.mutant_rng(C.SEED, "M23"), channel="red", delta=6)
    d = out.astype(int) - s.image.astype(int)
    assert (d[..., 0] > 0).any() and not (d[..., 1:] != 0).any()


# -- Diff Engine (D-02, D-05, D-06, D-09) ------------------------------------------------------------

H, W = 120, 160


def _landmark() -> np.ndarray:
    lm = np.zeros((12, 12, 3), np.uint8)
    lm[::2, ::3] = 255
    lm[3:9, 3:9, 0] = 200
    return lm


def _base() -> np.ndarray:
    img = np.full((H, W, 3), 20, np.uint8)
    img[4:16, 4:16] = _landmark()
    img[40:80, 60:70] = 220                 # a bar inside the region
    return img


def test_d02_floor_exception_edge_tolerance_value_is_honoured():
    """4.8: floor_exception carries its own `edge_tolerance_px` (default 1). An approved exception with
    edge_tolerance_px: 0 must not get a 1 px tolerance from anywhere else."""
    base = _base()
    cand = base.copy()
    cand[40:80, 60:71] = 220                 # the bar's right edge moves 1 px: 40 changed pixels
    exp = datetime(2099, 1, 1, tzinfo=timezone.utc)
    for tol, changed in ((0, 40), (1, 0)):
        fe = FloorException("jdoe", "aa edge", "T-1", exp, edge_tolerance_px=tol)
        reg = RegionsFile("x", [Landmark("lm", _landmark(), expected=(4, 4))],
                          [Region("r", "diff", "lm", Rect(30, 26, 80, 70), floor_exception=fe)])
        res = diff_pair(base, cand, reg)
        assert res.region("r").changed_px == changed, (tol, res.level, res.region("r").changed_px)
    assert res.level == "zero"


def test_d09_identical_captures_still_refuse_a_redaction_box_on_a_money_cell():
    """3.8: any mask or redaction box onto a money cell is harness_error (mask_budget_exceeded), and
    3.11 puts harness_error (row 2) before identical (row 10). A redaction box over the Price cell
    fills it the same way on both sides: the hash short-circuit must not hide the breach."""
    base = _base()
    reg = RegionsFile("x", [Landmark("lm", _landmark(), expected=(4, 4))],
                      [Region("rgn.price", "diff", "lm", Rect(50, 30, 40, 16), panel="order_ticket", money_cell=True)])
    res = diff_pair(base, base.copy(), reg, redactions=[RedactionBox(Rect(60, 36, 30, 10))])
    assert res.terminal is not None and res.terminal.reason == "mask_budget_exceeded", (res.level, res.terminal)
    clean = diff_pair(base, base.copy(), reg)
    assert clean.level == "zero" and clean.budget is not None and not clean.budget.exceeded


def test_d05_park_rect_over_a_money_cell_is_a_mask_budget_breach():
    """3.2 lists the parking rectangle as a mask and 3.5 applies it on both sides; 3.8: any mask onto a
    money_cell is harness_error (mask_budget_exceeded), never diffed around."""
    base = _base()
    cand = base.copy()
    cand[100:104, 100:104] = 250            # a change inside the money cell, under the park rect
    reg = RegionsFile("x", [Landmark("lm", _landmark(), expected=(4, 4))],
                      [Region("rgn.send", "diff", "lm", Rect(80, 80, 40, 24), panel="order_ticket", money_cell=True)])
    res = diff_pair(base, cand, reg, system_masks=[park_rect((102, 102))])
    assert res.terminal is not None and res.terminal.reason == "mask_budget_exceeded", (res.level, res.terminal)
    assert "system_mask@94,94" in res.terminal.detail


def test_d05_park_rect_counts_toward_the_panel_share():
    """3.8: the panel share is masks plus non-identity redaction over the panel's bounds; the parking
    rectangle is a mask (3.2). 256 px of a 40 x 40 position_statement panel is 16 percent, over 10."""
    base = _base()
    reg = RegionsFile("x", [Landmark("lm", _landmark(), expected=(4, 4))],
                      [Region("rgn.ps", "diff", "lm", Rect(76, 76, 40, 40), panel="position_statement")])
    res = diff_pair(base, base.copy(), reg, system_masks=[park_rect((100, 100))])
    assert res.terminal is not None and res.terminal.reason == "mask_budget_exceeded"
    assert "position_statement" in res.terminal.detail


def test_d06_grayscale_captures_are_diffed_not_crashed():
    """3.7 inputs are two captures; a gray-only PNG decoded unchanged is H x W. phash in the same
    package accepts it; diff_pair raised IndexError in _pad_common."""
    a = np.full((50, 60), 30, np.uint8)
    b = a.copy()
    b[10:20, 10:20] = 200
    assert diff_pair(a, b).level == "above"
    assert diff_pair(a, a.copy()).level == "zero"
    with pytest.raises(ValueError, match="H x W"):
        diff_pair(np.zeros((5, 5, 2), np.uint8), np.zeros((5, 5, 2), np.uint8))


# -- Settle (D-07, D-08) -------------------------------------------------------------------------------

def _tpl() -> np.ndarray:
    t = np.zeros((10, 10, 3), np.uint8)
    t[::2] = 200
    t[:, ::3, 1] = 120
    return t


def test_d07_timeout_reason_is_not_a_condition_that_already_recovered():
    """3.5: when max wait elapses the outcome is `timeout` with the last failing condition. The anchor
    was missing early, then present on the last two frames, and max wait cut the triple short: nothing
    failed at the end, so the reason is `timeout`, never a stale `anchor_missing`."""
    img = np.full((60, 80, 3), 30, np.uint8)
    with_anchor = img.copy()
    with_anchor[20:30, 30:40] = _tpl()
    spec = SettleSpec(anchors=[Anchor("ok-button", template=_tpl())], max_wait_ms=1000)
    frames = [Frame(0, img), Frame(250, img), Frame(500, img),
              Frame(750, with_anchor), Frame(1000, with_anchor), Frame(1250, with_anchor)]
    r = settle(frames, spec)
    assert not r.settled and r.reason == "frames_unstable", (r.reason, r.detail)
    assert "anchor ok-button" in r.detail and "recovered" in r.detail
    still = settle(frames[:3], spec)                     # still missing at the end: the condition stands
    assert still.reason == "anchor_missing"


def test_d07_recovered_frame_instability_is_frames_unstable():   # SD-26: timeout only without frames
    """The same stale value after a recovered run: frames differ once, then two identical frames remain
    before max wait. Condition 5 holds at the end, so the reason is `timeout`, not `frames_unstable`."""
    a = np.full((40, 40, 3), 30, np.uint8)
    b = a.copy()
    b[5:9, 5:9] = 200
    spec = SettleSpec(max_wait_ms=750)
    r = settle([Frame(0, a), Frame(250, b), Frame(500, b), Frame(1000, b)], spec)
    assert not r.settled and r.reason == "frames_unstable", (r.reason, r.detail)
    flicker = settle([Frame(0, a), Frame(250, b), Frame(500, a)], spec)
    assert flicker.reason == "frames_unstable"


def test_d08_settle_accepts_bgra_frames_from_the_screen_grab():
    """3.4 rule 2: the frame is a crop of an `mss` grab, 4-channel; the anchor templates are 3-channel.
    cv2.matchTemplate asserted on the type mismatch and settle raised instead of deciding condition 3."""
    img = np.full((60, 80, 4), 30, np.uint8)
    img[..., 3] = 255
    img[20:30, 30:40, :3] = _tpl()
    spec = SettleSpec(anchors=[Anchor("ok-button", template=_tpl())])
    assert settle([Frame(t, img) for t in (0, 250, 500)], spec).settled
    gray = np.full((60, 80), 30, np.uint8)
    gray[20:30, 30:40] = _tpl()[..., 0]
    gray_spec = SettleSpec(anchors=[Anchor("ok-button", template=np.repeat(_tpl()[..., :1], 3, axis=2))])
    assert settle([Frame(t, gray) for t in (0, 250, 500)], gray_spec).settled


# -- Calibration (D-03, D-04, D-10) --------------------------------------------------------------------

@pytest.fixture(scope="module")
def calibrated():
    s = _screen()
    img = s.image
    grid = s.get("positions.grid").bbox
    lab = s.get("positions.title.label").bbox
    tpl = img[lab[1]:lab[1] + lab[3], lab[0]:lab[0] + lab[2]].copy()
    lx, ly = lab[0], lab[1]
    reg = RegionsFile(POSITIONS, [Landmark("lm.title", tpl, expected=(lx, ly))],
                      [Region("rgn.grid", "diff", "lm.title", Rect(grid[0] - lx, grid[1] - ly, grid[2], grid[3]),
                              panel="position_statement")])
    cal = calibrate_checkpoint([Capture(img)] * 6, reg, data_mode="frozen")
    cf = CalibrationFile("sha256:x", "cal-1", {POSITIONS: cal})
    assert cf.complete
    return img, grid, reg, cf


def test_d03_aa_drift_catches_a_whole_window_shift_on_a_frozen_checkpoint(calibrated):
    """10.6: a frozen checkpoint with any diff above its floor is not_settled (aa_drift). A 1 px
    whole-window shift is above threshold (3.7 granularity 1: a shift is a change, never noise), yet
    every region reports shift 0, measured relative to the accepted window shift."""
    img, _, reg, cf = calibrated
    pass2 = C.global_shift(img, (0, 0, 640, 400), None, dx=1)
    res = diff_pair(img, pass2, reg, calibration=cf, context=DiffContext(checkpoint_id=POSITIONS))
    assert res.level == "above" and res.threshold_source == "calibrated"
    drift = aa_drift({POSITIONS: res}, [POSITIONS])
    assert POSITIONS in drift.drifted and "window shift (1, 0)" in drift.drifted[POSITIONS].detail, drift


def test_d03_aa_drift_catches_a_change_above_a_non_zero_floor(calibrated):
    """10.6 again: the old test only looked at zero-floor regions. A region whose approved floor is
    non-zero drifts when the A/A diff goes above that floor."""
    img, grid, reg, cf = calibrated
    exc = FloorException("jdoe", "aa edge", "T-1", datetime(2099, 1, 1, tzinfo=timezone.utc), edge_tolerance_px=0)
    region = reg.regions[0]
    reg2 = RegionsFile(reg.checkpoint_id, reg.landmarks,
                       [Region(region.id, region.type, region.landmark, region.rect, panel=region.panel,
                               floor_exception=exc)])
    cal = cf.checkpoints[POSITIONS]
    floor8 = RegionThreshold(40, 16, 2.0, 1, floor_changed_px=8)          # an approved floor of 8 changed pixels
    cal2 = type(cal)(POSITIONS, "frozen", floors=cal.floors, thresholds={**cal.thresholds, region.id: floor8})
    cf2 = CalibrationFile("sha256:y", "cal-2", {POSITIONS: cal2})
    p2 = img.copy()
    p2[grid[1] + 50:grid[1] + 53, grid[0] + 100:grid[0] + 104] = 255     # 12 px: over the floor of 8, under 16
    res = diff_pair(img, p2, reg2, calibration=cf2, context=DiffContext(checkpoint_id=POSITIONS))
    m = res.region(region.id)
    assert m.threshold_source == "calibrated" and m.changed_px == 12 and m.above_floor and not m.above_threshold
    assert POSITIONS in aa_drift({POSITIONS: res}, [POSITIONS]).drifted


def test_d04_aa_false_positive_rate_counts_region_pairs_over_floor(calibrated):
    """3.9 step 7: 'region pairs over floor divided by region pairs compared'. Two changed pixels in a
    zero-floor region are over its floor (but under the 4 px threshold): 1 of 2 region pairs."""
    img, grid, reg, cf = calibrated
    p2 = img.copy()
    p2[grid[1] + 50, grid[0] + 100] = 255
    p2[grid[1] + 50, grid[0] + 101] = 255
    res = diff_pair(img, p2, reg, calibration=cf, context=DiffContext(checkpoint_id=POSITIONS))
    assert [m.above_floor for m in res.regions] == [True, False]
    assert aa_false_positive_rate([res]) == pytest.approx(0.5)


def test_d10_calibration_applies_the_parking_rectangle_like_a_product_run(calibrated):
    """3.9 step 2: calibration captures go 'through the same settle, redaction and landmark steps as a
    product run'; 3.5: the 16x16 parking mask is applied on both sides. Captures that differ only inside
    the parking rectangle must not put a non-zero floor on the fix list."""
    img, grid, reg, _ = calibrated
    park = (grid[0] + 150, grid[1] + 60)
    hover = img.copy()
    hover[park[1] - 3:park[1] + 3, park[0] - 3:park[0] + 3] = 240
    caps = [Capture(img), Capture(hover), Capture(img), Capture(hover), Capture(img), Capture(img)]
    cal = calibrate_checkpoint(caps, reg, data_mode="frozen", system_masks=[park_rect(park)])
    assert cal.fix_list == [] and cal.accepted
    assert calibrate_checkpoint(caps, reg, data_mode="frozen").fix_list      # without it: a hover floor


# -- Canary scoring and generation (D-11, D-12) --------------------------------------------------------

def _m20(expected: tuple[str, ...], image: np.ndarray, divider: tuple[int, int, int, int]) -> C.Mutant:
    entry = C.MutantEntry(mutant_id="M20", checkpoint_id=S06, defect_class="sub_floor", mode="image",
                          money_cell=False, op="translate", params={}, owner="sentinel-canaries",
                          expected=C.Expected(expected), region_ref="rgn.ticket.divider", catalog_version="v1")
    return C.Mutant(entry=entry, checkpoint_id=S06, seed=C.SEED, image=image, base_sha256="b" * 64,
                    mutant_sha256="m" * 64, footprint=(divider[0], divider[1], divider[2], 2))


def _row(outcome: str, reason: str | None = None) -> CheckpointOutcome:
    return CheckpointOutcome(run_id=NIGHTLY, checkpoint_id=S06, outcome=outcome, reason=reason,
                             workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="frozen",
                             synthetic=True)


def test_d11_m20_clean_result_is_scored_detected():
    """10.3: the sub_floor control M20 is absorbed (`noise` or `identical`); any finding is the miss.
    Under the zero floor of a frozen region a 1 px divider shift is either under the YIQ threshold
    everywhere (D = zero, 3.11 row 10 `identical`) or above; D = below (`noise`) cannot happen. A clean
    `identical` was scored a miss, which pushed sub_floor to 0 percent every night."""
    s = _screen()
    r0 = s.get("positions.grid.r0").bbox
    divider = (r0[0], r0[1] + r0[3] - 1, r0[2], 1)
    mut = C.nudge_divider(s.image, divider, C.mutant_rng(C.SEED, "M20"))
    assert np.any(mut != s.image)
    res = diff_pair(s.image, mut)
    assert res.level == "zero" and not res.findings
    for expected in (("noise",), ("identical", "noise")):
        for outcome, reason in (("identical", None), ("noise", "below_floor")):
            rep = C.score([C.MutantResult(mutant=_m20(expected, mut, divider), produced=_row(outcome, reason))])
            assert rep.results[0].state == "detected", (expected, outcome, rep.results[0].state)
    over = C.score([C.MutantResult(mutant=_m20(("identical", "noise"), mut, divider),
                                   produced=_row("collateral"), on_mutated_region=True)])
    assert over.results[0].state != "detected"                              # any finding is the miss


def test_d11_catalog_m20_expects_identical_or_noise():
    """12.5 M20 and L-MUT-02: the control expects `identical` or `noise` by design."""
    from pathlib import Path
    cat = C.load_catalog(Path(__file__).resolve().parents[1] / "config/example/canaries/mutants.yaml")
    m20 = next(e for e in cat.entries if e.mutant_id == "M20")
    assert set(m20.expected.outcomes) == {"identical", "noise"}


def test_d12_position_last_replaces_only_the_last_digit_of_100():
    """10.3 swapped_digit replaces one digit glyph; 12.5 M03 is Quantity 100 -> 108. In the synth Qty
    cell the three digits touch, and the whole "100" was overwritten by a donor run."""
    s = _screen()
    x, y, w, h = s.get("ticket.qty").bbox
    box = (x - 1, y - 3, w + 2, h + 6)
    out = C.replace_glyph(s.image, box, C.mutant_rng(C.SEED, "M03"), position="last")
    bx, by, bw, bh = box
    changed = np.flatnonzero(np.any(out[by:by + bh, bx:bx + bw] != s.image[by:by + bh, bx:bx + bw], axis=(0, 2)))
    assert changed.size
    assert changed.min() >= (x - bx) + int(0.6 * w), f"changed columns {changed.min()}..{changed.max()} of {bw}"


def test_d12_a_run_with_no_valley_is_never_transplanted():
    """10.3: replace one glyph, never several. A solid run as wide as two glyphs has no ink valley to cut
    at, so it cannot be isolated: GenerationError, not a two-glyph transplant."""
    img = np.full((30, 80, 3), 240, np.uint8)
    img[10:20, 10:30] = 0                        # a solid 20 x 10 block: two glyph widths, no valley
    img[10:20, 50:56] = 0                        # a lone glyph elsewhere in the row
    with pytest.raises(C.GenerationError, match="isolate"):
        C.replace_glyph(img, (6, 6, 28, 18), C.mutant_rng(C.SEED, "M03"), position="last")


# -- Nightly: synthetic signals never reach the run-level passes (10.3 Injection, 10.6) ------------------

def test_synthetic_signals_are_ignored_by_the_global_shift_prepass():
    """One injected global shift (M21) among 20 real pairs is 5 percent above floor, over the 2 percent
    of 3.7 granularity 0: with nightly=True that was harness_error (aa_drift_global)."""
    clean = [GlobalShiftSignal(f"census/c{i}/ws.a", "zero", False, 0, 0, True) for i in range(20)]
    m21 = GlobalShiftSignal(POSITIONS, "above", True, 5000, 4000, True, synthetic=True)
    decision = global_shift_prepass(clean + [m21], nightly=True)
    assert decision.evaluated and not decision.raised and decision.terminal is None
    assert decision.fraction_above_floor == 0.0
    assert not global_shift_prepass(clean[:19] + [m21], nightly=True).evaluated       # 19 real pairs: too few
    real = GlobalShiftSignal(POSITIONS, "above", True, 5000, 4000, True)
    assert global_shift_prepass(clean + [real], nightly=True).terminal.reason == "aa_drift_global"


def test_injected_m21_and_m23_never_trip_aa_drift_global(calibrated):
    """10.3 Injection: mutants are diffed with `DiffContext(synthetic=True)`; the result and its signal
    carry `synthetic`, and aa_drift leaves them out, the denominator included."""
    img, _, reg, cf = calibrated
    clean = diff_pair(img, img.copy(), reg, calibration=cf, context=DiffContext(checkpoint_id=POSITIONS))
    ctx = DiffContext(run_id=NIGHTLY, checkpoint_id=POSITIONS, synthetic=True)
    m21 = diff_pair(img, C.global_shift(img, (0, 0, 640, 400), None, dx=1), reg, calibration=cf, context=ctx)
    m23 = diff_pair(img, C.global_shift(img, (0, 0, 640, 400), None, channel="red", delta=6), reg,
                    calibration=cf, context=ctx)
    for res in (m21, m23):
        assert res.synthetic and res.signal.synthetic and res.signal.above_floor
    results = {f"census/c{i}/ws.a": clean for i in range(20)} | {"canary/M21": m21, "canary/M23": m23}
    verdict = aa_drift(results, frozen=results.keys())
    assert verdict.global_drift is None and verdict.drifted == {} and verdict.above_floor_fraction == 0.0
    signals = [r.signal for r in results.values()]
    assert global_shift_prepass(signals, nightly=True).terminal is None
    real = {f"census/c{i}/ws.a": clean for i in range(20)}
    real["census/shifted/ws.a"] = diff_pair(img, C.global_shift(img, (0, 0, 640, 400), None, dx=1), reg,
                                            calibration=cf)
    assert aa_drift(real, frozen=[]).global_drift.reason == "aa_drift_global"   # the same shift, not synthetic
