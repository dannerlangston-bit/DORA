"""The size-sweep step measurement of 2.5.1: the three closed cases, the one retry, `assertion_failed (assertion:
size_reached)` with the rest of the (surface, host) ladder `not_run (reason: step_failed:<step>)`, the detach control
or splitter not found, the achieved minimum per gadget and host, and the `size_step` event."""
from __future__ import annotations

import pytest

from sentinel.capture import (MinSize, SizeLadder, SweepError, check_size_reached, classify_axes, classify_size_step,
                              compare_minimums, minimums_summary, size_step_event)

SURFACE = "trade.active-trader.ladder"
PREV = (480, 360)          # what the previous smaller-or-equal request achieved: a larger size is no clamp


def cid(size: str, host: str = "flexgrid") -> str:
    return f"sweep/{SURFACE}/{host}/{size}"


# -- the three cases ------------------------------------------------------------------------------------------------

def test_within_4_px_on_both_axes_is_taken():
    assert classify_size_step((480, 360), (482, 359), (None, None)) == "taken"
    assert classify_size_step((480, 360), (484, 356), (480, 360)) == "taken"


def test_larger_at_the_smallest_request_or_near_the_previous_smaller_is_clamped():
    assert classify_size_step((240, 160), (320, 300), (None, None)) == "clamped"       # smallest on both axes
    assert classify_size_step((240, 160), (320, 300), (320, 300)) == "clamped"
    assert classify_axes((320, 480), (362, 481), (360, 480)) == ("clamped", "taken")
    assert classify_size_step((320, 480), (362, 481), (360, 480)) == "clamped"


def test_any_other_result_is_not_reached():
    assert classify_size_step((400, 480), (520, 480), (400, 480)) == "not_reached"     # larger, far from previous
    assert classify_size_step((640, 480), (600, 480), (None, None)) == "not_reached"   # smaller than requested
    assert classify_axes((320, 300), (360, 250), (None, None)) == ("clamped", "not_reached")


# -- one retry -------------------------------------------------------------------------------------------------------

def test_not_reached_then_reached_on_the_retry_passes_after_retry():
    r = check_size_reached((480, 360), (640, 480), (481, 361), PREV, step_id="480x360")
    assert (r.status, r.result, r.attempts, r.achieved) == ("passed_after_retry", "taken", 2, (481, 361))
    assert r.checkpoint is None and r.rest_of_ladder is None


def test_ignored_twice_is_assertion_failed_size_reached_and_the_rest_not_run():
    r = check_size_reached((480, 360), (640, 480), (640, 480), PREV, step_id="480x360", side="candidate")
    assert r.status == "failed" and r.result == "not_reached"
    assert (r.checkpoint.outcome, r.checkpoint.reason) == ("assertion_failed", "assertion:size_reached")
    assert "requested 480x360" in r.checkpoint.detail and "640x480" in r.checkpoint.detail
    assert (r.rest_of_ladder.outcome, r.rest_of_ladder.reason) == ("not_run", "step_failed:480x360")


def test_the_retry_is_exactly_one_and_only_when_needed():
    with pytest.raises(SweepError):
        check_size_reached((480, 360), (640, 480), None, PREV, step_id="480x360")
    with pytest.raises(SweepError):
        check_size_reached((480, 360), (480, 360), (480, 360), PREV, step_id="480x360")


# -- a ladder ---------------------------------------------------------------------------------------------------------

def test_descending_ladder_clamps_at_the_gadget_minimum_and_records_it():
    lad = SizeLadder(SURFACE, "flexgrid")
    gadget_min = (360, 330)

    def achieved(w, h):
        return max(w, gadget_min[0]), max(h, gadget_min[1])

    for w in (960, 640, 480, 320):
        for h in (720, 480, 360, 300):
            r = lad.measure(cid(f"{w}x{h}"), achieved(w, h))
            assert r.status == "passed", (w, h)
            assert r.clamped == (w < 360 or h < 330), (w, h)
    corner = lad.measure(cid("240x160"), (360, 330))
    assert corner.clamped and corner.axes == ("clamped", "clamped")
    m = lad.minimum()
    assert m == MinSize(SURFACE, "flexgrid", 360, 330, True, True)
    assert m.report_line() == f"{SURFACE} (flexgrid): achieved minimum 360x330, clamped on width and height"
    assert minimums_summary([lad, SizeLadder(SURFACE, "detached")]) == [m.to_dict()]


def test_reference_is_the_most_recent_smaller_or_equal_request():
    lad = SizeLadder(SURFACE, "flexgrid")
    lad.measure(cid("960x300"), (960, 330))                       # first meets the minimum height: clamped
    assert lad.reference((800, 300)) == (None, 330)
    assert lad.reference((960, 720)) == (960, 330)
    ok = lad.measure(cid("800x300"), (800, 331))
    assert ok.clamped


def test_a_drag_that_did_not_take_after_the_minimum_is_found_fails_the_ladder():
    lad = SizeLadder(SURFACE, "flexgrid")
    lad.measure(cid("960x300"), (960, 330))
    assert not lad.needs_retry(cid("800x300"), (800, 332))
    assert lad.needs_retry(cid("800x300"), (800, 600))
    r = lad.measure(cid("800x300"), (800, 600), retry=(800, 600))
    assert r.status == "failed" and r.checkpoint.reason == "assertion:size_reached"
    assert lad.blocked() == r.rest_of_ladder and lad.blocked().reason == "step_failed:800x300"
    with pytest.raises(SweepError):
        lad.measure(cid("640x720"), (640, 720))
    with pytest.raises(SweepError):
        lad.needs_retry(cid("640x720"), (640, 720))
    assert lad.minimum() == MinSize(SURFACE, "flexgrid", 960, 330, False, True)


def test_a_window_clamped_where_a_smaller_request_was_taken_is_not_a_clamp():
    lad = SizeLadder(SURFACE, "flexgrid")
    lad.measure(cid("320x300"), (320, 300))                        # the host took 320 wide earlier
    r = lad.measure(cid("320x480-asc"), (360, 480), retry=(360, 480))
    assert r.status == "failed" and r.rest_of_ladder.reason == "step_failed:320x480-asc"


def test_detach_control_not_found_ends_the_first_step_and_the_ladder():
    lad = SizeLadder(SURFACE, "detached", side="base")
    r = lad.control_missing(cid("960x720", "detached"), "detach control")
    assert r.status == "failed" and r.side == "base"
    assert (r.checkpoint.outcome, r.checkpoint.reason) == ("landmark_missing", "locator:960x720")
    assert "detach control" in r.checkpoint.detail and "base" in r.checkpoint.detail
    assert lad.blocked().reason == "step_failed:960x720"
    assert lad.minimum() is None


def test_a_step_of_another_ladder_is_refused():
    lad = SizeLadder(SURFACE, "flexgrid")
    with pytest.raises(SweepError):
        lad.measure(cid("960x720", "detached"), (960, 720))
    with pytest.raises(SweepError):
        lad.measure(f"split/{SURFACE}/sp.side/0.50", (100, 100))


# -- minimums across builds -------------------------------------------------------------------------------------------

def test_a_moved_minimum_is_window_geometry_changed():
    base = MinSize(SURFACE, "flexgrid", 360, 330, True, True)
    assert compare_minimums(base, MinSize(SURFACE, "flexgrid", 363, 327, True, True)) is None
    f = compare_minimums(base, MinSize(SURFACE, "flexgrid", 400, 330, True, True))
    assert f.kind == "window_geometry_changed" and "360x330" in f.description and "400x330" in f.description
    with pytest.raises(SweepError):
        compare_minimums(base, MinSize(SURFACE, "detached", 360, 330, True, True))
    assert MinSize(SURFACE, "dialog", 320, 300, False, False).report_line().endswith("320x300, no clamp")


# -- the step event ---------------------------------------------------------------------------------------------------

EV = {"run_id": "run-1", "shard_id": "s1", "side": "candidate", "wall_at": "2026-11-10T12:00:00+00:00",
      "mono_ms": 5000}


def test_size_step_events_carry_request_achieved_attempt_and_reason():
    lad = SizeLadder(SURFACE, "flexgrid")
    taken = lad.measure(cid("960x720"), (960, 720))
    clamp = lad.measure(cid("240x160"), (320, 300))
    retried = check_size_reached((480, 360), (500, 500), (480, 360), PREV, step_id="480x360")
    failed = check_size_reached((480, 360), (500, 500), (500, 500), PREV, step_id="480x360")
    e1 = size_step_event(taken, checkpoint_id=cid("960x720"), seq=1, **EV)
    assert (e1.action, e1.step_outcome, e1.reason, e1.rect_w, e1.rect_h) == ("size_step", "passed", None, 960, 720)
    e2 = size_step_event(clamp, checkpoint_id=cid("240x160"), seq=2, **EV)
    assert (e2.reason, e2.target_text, e2.rect_w, e2.rect_h) == ("size_clamped", "requested 240x160", 320, 300)
    e3 = size_step_event(retried, checkpoint_id=cid("480x360"), seq=3, **EV)
    assert (e3.attempt, e3.step_outcome, e3.reason) == (2, "passed_after_retry", "retry: size not reached")
    e4 = size_step_event(failed, checkpoint_id=cid("480x360"), seq=4, **EV)
    assert (e4.attempt, e4.step_outcome, e4.reason) == (2, "failed", "assertion:size_reached")
    gone = SizeLadder(SURFACE, "detached").control_missing(cid("960x720", "detached"), "detach control")
    e5 = size_step_event(gone, checkpoint_id=cid("960x720", "detached"), seq=5, **EV)
    assert (e5.reason, e5.rect_w, e5.attempt) == ("locator:960x720", None, 1)
