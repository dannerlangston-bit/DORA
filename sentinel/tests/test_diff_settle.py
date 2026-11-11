"""The settle rule (3.5) and the targeted re-run stability logic (3.10, F49)."""
import copy

import numpy as np
import pytest

from sentinel import synth
from sentinel.diff.calibration import CalibrationFile, CheckpointCalibration, RegionFloors, derive_threshold
from sentinel.diff.engine import DiffContext, Terminal, diff_pair
from sentinel.diff.regions import Landmark, Rect, Region, RegionsFile
from sentinel.diff.settle import (
    Anchor, Frame, Recapture, RerunBudget, SettleSpec, diff_stability, invariant_stability, recaptures_allowed,
    settle, settle_regression,
)

SIZE = (640, 400)
LAYOUT = synth.sample_layout(0)
SCREEN = synth.render(LAYOUT, SIZE)
IMG = SCREEN.image
CLOCK = Rect(560, 4, 70, 16)                    # a masked clock in the title bar
_TX, _TY, _, _ = SCREEN.get("ticket.title.label").bbox
TITLE_ANCHOR = Anchor("order entry title", template=SCREEN.crop("ticket.title.label", 2),
                      search=Rect(_TX - 40, _TY - 20, 140, 50))


def frames(images, start=0, step=250, **kw):
    return [Frame(start + i * step, im, **kw) for i, im in enumerate(images)]


def ticking(img, n):
    """`n` copies of `img` whose clock area differs every frame (masked, so still settled)."""
    out = []
    for i in range(n):
        f = img.copy()
        f[CLOCK.y:CLOCK.y + CLOCK.h, CLOCK.x:CLOCK.x + CLOCK.w] = 30 + 10 * i
        out.append(f)
    return out


def spec(**kw):
    return SettleSpec(anchors=[TITLE_ANCHOR], ignore=[CLOCK], park=(630, 395), **kw)


def _reason_is_legal(res):
    Terminal("not_settled", res.reason)


def test_settles_on_three_identical_frames_outside_masks_and_park_rect():
    loading = [Frame(0, IMG, texts=("Loading...",)), Frame(250, synth.perturb(IMG, 1, 50, 90))]
    steady = ticking(IMG, 4)
    steady[2][393:397, 628:632] = 255                    # the parking rectangle changes too
    res = settle(loading + frames(steady, start=500), spec())
    assert res.settled and res.outcome is None and res.reason is None
    assert res.capture_t_ms == 500 and res.time_to_settle_ms == 1000
    assert np.array_equal(res.capture, steady[0])


def test_frames_closer_than_250_ms_are_skipped():
    seq = [Frame(0, IMG), Frame(100, synth.perturb(IMG, 2, 50, 90)), Frame(250, IMG), Frame(500, IMG)]
    res = settle(seq, spec())
    assert res.settled and res.time_to_settle_ms == 500


def test_frames_unstable():
    seq = frames([synth.perturb(IMG, s, 20, 90) for s in range(61)])
    res = settle(seq, spec())
    assert not res.settled and res.outcome == "not_settled" and res.reason == "frames_unstable"
    assert res.elapsed_ms == 15000 and res.last_frame is not None
    _reason_is_legal(res)


def test_anchor_missing_names_the_anchor_or_the_neutral_control():
    no_title = copy.deepcopy(LAYOUT)
    synth.node(no_title, "ticket")["title"] = ""
    res = settle(frames([synth.render(no_title, SIZE).image] * 61), spec())
    assert res.reason == "anchor_missing" and "order entry title" in res.detail
    res = settle(frames([IMG] * 61, focus_dropped=False), spec(neutral_control="watchlist header"))
    assert res.reason == "anchor_missing" and "watchlist header" in res.detail
    _reason_is_legal(res)


def test_keep_focus_skips_the_focus_neutral_condition():
    """SD-27, 3.5 condition 2: a keep_focus checkpoint keeps its field focused and still settles."""
    held = frames([IMG] * 4, focus_dropped=False)
    assert settle(held, spec()).reason == "anchor_missing"
    res = settle(held, spec(keep_focus=True))
    assert res.settled and res.capture_t_ms == 0
    # the caret blinks inside its mask, so the frames stay identical outside masks
    caret = Rect(300, 200, 2, 12)
    blink = [IMG.copy() for _ in range(4)]
    for k, f in enumerate(blink):
        f[caret.y:caret.y + caret.h, caret.x:caret.x + caret.w] = 0 if k % 2 else 255
    assert not settle(frames(blink, focus_dropped=False), spec(keep_focus=True)).settled
    assert settle(frames(blink, focus_dropped=False),
                  SettleSpec(anchors=[TITLE_ANCHOR], ignore=[CLOCK, caret], park=(630, 395), keep_focus=True)).settled


def test_anti_anchor_present_by_text_or_template():
    res = settle(frames([IMG] * 61, texts=("Reconnecting",)), spec())
    assert res.reason == "anti_anchor_present" and "Reconnecting" in res.detail
    spinner = Anchor("spinner", template=SCREEN.crop("ticket.send.label", 1))
    res = settle(frames([IMG] * 61), spec(anti_anchors=[spinner]))
    assert res.reason == "anti_anchor_present" and "spinner" in res.detail
    _reason_is_legal(res)


def test_tooltip_delay_of_one_second_after_parking():
    res = settle(frames([IMG] * 8, parked_at_ms=0), spec())
    assert res.settled and res.capture_t_ms == 1000 and res.time_to_settle_ms == 1500


def test_timeout_when_the_mouse_never_parks_frames_unstable_when_frames_are_too_few():
    res = settle(frames([IMG] * 61, parked_at_ms=None), spec())
    assert res.reason == "timeout" and "condition 1" in res.detail
    res = settle([Frame(0, IMG), Frame(250, IMG)], spec())          # SD-26: timeout only when no frame came
    assert res.reason == "frames_unstable" and "fewer than 3 frames" in res.detail
    _reason_is_legal(res)
    res = settle([], spec())
    assert res.reason == "timeout" and "no frame" in res.detail


def test_unexpected_window_and_long_wait():
    res = settle(frames([IMG] * 61, modal_title="Order Rejected"), spec())
    assert res.reason == "unexpected_window:Order Rejected"
    _reason_is_legal(res)
    late = frames([synth.perturb(IMG, s, 20, 90) for s in range(70)]) + frames([IMG] * 3, start=17500)
    assert not settle(late, spec()).settled                 # 15 s default
    assert settle(late, spec(long_wait=True)).settled       # chart and scan checkpoints wait 30 s


def test_settle_regression_is_twice_the_calibration_p50():
    assert settle_regression(2100, 1000) and not settle_regression(2000, 1000)
    assert not settle_regression(5000, None)


# -- 3.10 targeted re-run ---------------------------------------------------------------------

def _above():
    lx, ly, _, _ = SCREEN.get("ticket.title.label").bbox
    lm = Landmark("lm.ticket", SCREEN.crop("ticket.title.label", 2), expected=(lx - 2, ly - 2))
    sx, sy, sw, sh = SCREEN.get("ticket.send").bbox
    reg = RegionsFile("census/trade.order-entry.stock/ws.tier-a.active-trader", [lm],
                      [Region("rgn.ticket.send", "diff", "lm.ticket", Rect(sx - lx + 2, sy - ly + 2, sw, sh))])
    cal = CheckpointCalibration(reg.checkpoint_id, "frozen")
    for rid in ("rgn.ticket.send", "_rest", "*"):
        cal.floors[rid], cal.thresholds[rid] = RegionFloors(), derive_threshold(RegionFloors())
    cf = CalibrationFile("sha256:fp", "run-cal", {reg.checkpoint_id: cal})

    def above(a, b):
        return diff_pair(a, b, reg, calibration=cf, context=DiffContext(checkpoint_id=reg.checkpoint_id)).level == "above"
    return above


ABOVE = _above()
ERASED = synth.render({**LAYOUT, "children": [
    c if c.get("name") != "ticket" else {**c, "children": [k for k in c["children"] if k.get("name") != "ticket.send"]}
    for c in LAYOUT["children"]]}, SIZE).image


def test_recapture_allowances_and_run_budget():
    assert (recaptures_allowed("diff"), recaptures_allowed("invariant"), recaptures_allowed("assertion")) == (2, 1, 0)
    with pytest.raises(ValueError):
        recaptures_allowed("reviewer")
    assert RerunBudget(100).limit == 30 and RerunBudget(500).limit == 50
    b = RerunBudget(10)
    assert all(b.take() for _ in range(30)) and not b.take()


def test_diff_stable_when_every_pair_is_under_threshold():
    sub = synth.perturb(IMG, 9, count=2000, amplitude=3)
    st = diff_stability(ERASED, [Recapture(True, ERASED.copy()), Recapture(True, ERASED.copy())], ABOVE)
    assert (st.stability, st.cause, st.outcome, st.recaptures) == ("stable", None, None, 2)
    assert diff_stability(IMG, [Recapture(True, sub)], ABOVE).stability == "stable"


def test_diff_unstable_when_any_pair_is_above_threshold():
    st = diff_stability(ERASED, [Recapture(True, ERASED.copy()), Recapture(True, IMG)], ABOVE)
    assert st.stability == "unstable" and st.outcome == ("not_settled", "unstable")
    assert ("candidate", 0, 2) in st.pairs_above and ("candidate", 1, 2) in st.pairs_above
    # base captures are compared among themselves when more than one exists
    st = diff_stability(ERASED, [Recapture(True, ERASED.copy())], ABOVE, base_captures=[IMG, ERASED])
    assert st.stability == "unstable" and st.pairs_above == (("base", 0, 1),)


def test_not_settled_recaptures_are_retried_once():
    st = diff_stability(ERASED, [Recapture(False), Recapture(True, ERASED.copy())], ABOVE)
    assert st.stability == "stable"
    assert diff_stability(ERASED, [Recapture(False), Recapture(False)], ABOVE).stability == "unstable"
    with pytest.raises(ValueError, match="retried"):
        diff_stability(ERASED, [Recapture(False)], ABOVE)
    with pytest.raises(ValueError):
        diff_stability(ERASED, [Recapture(True, ERASED)] * 3, ABOVE)


def test_unknown_when_not_recapturable_or_over_budget():
    assert diff_stability(ERASED, [], ABOVE, recapturable=False) == diff_stability(ERASED, [], ABOVE, recapturable=False)
    st = diff_stability(ERASED, [], ABOVE, recapturable=False)
    assert (st.stability, st.cause) == ("unknown", "not_recapturable") and st.outcome is None
    st = invariant_stability([], budget_ok=False)
    assert (st.stability, st.cause) == ("unknown", "budget")


def test_invariant_violation_is_recaptured_once_to_confirm_persistence():
    assert invariant_stability([Recapture(True, IMG, invariant_fired=True)]).stability == "stable"
    st = invariant_stability([Recapture(True, IMG, invariant_fired=False)])
    assert st.stability == "unstable" and st.outcome == ("not_settled", "unstable")
    assert invariant_stability([Recapture(False), Recapture(True, IMG, invariant_fired=True)]).stability == "stable"
    assert invariant_stability([Recapture(False), Recapture(False)]).stability == "unstable"
    with pytest.raises(ValueError):
        invariant_stability([Recapture(True, IMG, invariant_fired=True)] * 2)
    with pytest.raises(ValueError):
        invariant_stability([Recapture(True, IMG)])
