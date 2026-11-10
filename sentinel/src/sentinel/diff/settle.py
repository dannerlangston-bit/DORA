"""The settle rule (3.5, 5.7) and the targeted re-run stability logic (3.10, 10.5, F49).

Settle. Capture happens only when, checked in order within `max_wait` (15 s; 30 s for chart and
scan checkpoints): (1) the mouse is parked at the declared dead-area coordinate and the 1 s
tooltip delay has passed; (2) focus was dropped on the declared neutral control; (3) every
must-see anchor matches at 0.92 or better; (4) no anti-anchor (`Loading`, `Reconnecting`,
`Disconnected`, spinner templates, the empty-grid placeholder) is present anywhere in the target
window; (5) three consecutive frames 250 ms apart are pixel-identical outside masks, redactions
and the 16x16 parking rectangle. The first frame of the triple is the capture (3.5; 5.7 names the
third, which is the same pixels outside masks). An unexpected modal up at `max_wait` is
`unexpected_window:<title>` (3.4 rule 4).

Choice (3.5 is ambiguous about `timeout`): when `max_wait` elapses, the reason is the condition
that held at the end ("whichever held at timeout", 4.7 `wait_for`): `unexpected_window:<title>`,
`anchor_missing` (conditions 2 and 3, the control or anchor named in `detail`),
`anti_anchor_present`, or `frames_unstable`. `timeout` is the remaining case: the last failing
condition was the parking condition (1), which has no code of its own, or nothing failed but too
few frames arrived to form the triple; `detail` names the last failing condition either way.
Frames closer than 250 ms to the previous frame of the run are skipped, never compared.

Re-run (3.10). An above-threshold diff is re-captured up to twice, an `invariant_violation` once
to confirm persistence (F49), an `assertion_failed` never. Every settled candidate capture is
compared with every other (base captures likewise when more than one exists): all pairs under
threshold is `stable`, any pair above is `unstable`. A re-capture that ends `not_settled` is
retried once more; a second `not_settled` is `unstable`. An invariant that fires again on the
re-capture is `stable`; one that does not is `unstable`. `unstable` is
`not_settled (reason: unstable)`. A checkpoint declared `recapturable: false` or beyond the run
budget (max(30, 10 percent of the run's checkpoints)) is `unknown` with cause `not_recapturable`
or `budget`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from . import load_params
from .engine import match_template
from .regions import Rect

DEFAULT_ANTI_ANCHOR_TEXTS = ("Loading", "Reconnecting", "Disconnected")


@dataclass(frozen=True)
class Frame:
    """One frame of the settle loop. `t_ms` counts from the start of the wait.

    The platform side reports what it knows about the frame: whether the mouse is parked with the
    tooltip delay passed, whether focus was dropped on the neutral control (always true for a
    `keep_focus` checkpoint), the title of an unexpected modal window if one is up, and OCR words
    for text anti-anchors.
    """

    t_ms: int
    image: np.ndarray
    parked: bool = True
    focus_dropped: bool = True
    modal_title: str | None = None
    texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Anchor:
    """A must-see anchor or an anti-anchor: a template (RGB) or an OCR text, optionally in a box."""

    name: str
    template: np.ndarray | None = None
    text: str | None = None
    search: Rect | None = None

    def present(self, frame: Frame, confidence: float, seen: dict | None = None) -> bool:
        """Text match in the frame's OCR words, or template match at `confidence` or better.
        `seen` caches template results for a frame pixel-identical to the previous one."""
        if self.text is not None and any(self.text.lower() in t.lower() for t in frame.texts):
            return True
        if self.template is None:
            return False
        if seen is not None and self.name in seen:
            return seen[self.name]
        best, _, _ = match_template(frame.image, self.template, self.search)
        if seen is not None:
            seen[self.name] = best >= confidence
        return best >= confidence


@dataclass
class SettleSpec:
    """The checkpoint's settle parameters (4.7 checkpoint record, 5.7 inputs)."""

    anchors: Sequence[Anchor] = ()
    anti_anchors: Sequence[Anchor] = field(
        default_factory=lambda: [Anchor(t, text=t) for t in DEFAULT_ANTI_ANCHOR_TEXTS])
    ignore: Sequence[Rect] = ()
    park: tuple[int, int] | None = None
    neutral_control: str = "neutral control"
    long_wait: bool = False
    max_wait_ms: int | None = None


@dataclass(frozen=True)
class SettleResult:
    """Settled (with the capture) or `not_settled` with its reason, the last frame and the time."""

    settled: bool
    capture: np.ndarray | None
    capture_t_ms: int | None
    time_to_settle_ms: int | None
    reason: str | None
    detail: str | None
    last_frame: np.ndarray | None
    elapsed_ms: int

    @property
    def outcome(self) -> str | None:
        return None if self.settled else "not_settled"


def _ignore_mask(shape: tuple[int, int], spec: SettleSpec, park_px: int) -> np.ndarray:
    h, w = shape
    m = np.zeros((h, w), dtype=bool)
    rects = list(spec.ignore)
    if spec.park is not None:
        px, py = spec.park
        rects.append(Rect(px - park_px // 2, py - park_px // 2, park_px, park_px))
    for r in rects:
        i = r.intersect(Rect(0, 0, w, h))
        if i is not None:
            m[i.y:i.y + i.h, i.x:i.x + i.w] = True
    return m


def _failing(frame: Frame, spec: SettleSpec, confidence: float, seen: dict) -> tuple[str, str] | None:
    """The first failing condition of 3.5 for one frame, as (reason code, detail), or None."""
    if frame.modal_title:
        return f"unexpected_window:{frame.modal_title}", f"modal window {frame.modal_title!r}"
    if not frame.parked:
        return "park", "condition 1: mouse not parked or tooltip delay not passed"
    if not frame.focus_dropped:
        return "anchor_missing", f"condition 2: focus not dropped on {spec.neutral_control}"
    for a in spec.anchors:
        if not a.present(frame, confidence, seen):
            return "anchor_missing", f"condition 3: anchor {a.name}"
    for a in spec.anti_anchors:
        if a.present(frame, confidence, seen):
            return "anti_anchor_present", f"condition 4: anti-anchor {a.name}"
    return None


def settle(frames: Iterable[Frame], spec: SettleSpec, params: Mapping[str, Any] | None = None) -> SettleResult:
    """Apply the settle rule of 3.5 to a frame sequence (frames at 250 ms, in time order)."""
    sp = (params or load_params())["settle"]
    interval = int(sp["frame_interval_ms"])
    need = int(sp["identical_frames"])
    confidence = float(sp["anchor_confidence_min"])
    max_wait = spec.max_wait_ms or int(sp["max_wait_long_ms"] if spec.long_wait else sp["max_wait_ms"])
    run: list[Frame] = []
    last: Frame | None = None
    last_fail: tuple[str, str] | None = None
    ignore: np.ndarray | None = None
    seen: dict[str, bool] = {}          # template results, valid while frames stay pixel-identical
    for f in frames:
        if f.t_ms > max_wait:
            break
        if last is None or not (last.image is f.image or (last.image.shape == f.image.shape
                                                          and np.array_equal(last.image, f.image))):
            seen = {}
        last = f
        fail = _failing(f, spec, confidence, seen)
        if fail is not None:
            run, last_fail = [], fail
            continue
        if run and f.t_ms - run[-1].t_ms < interval:
            continue
        if ignore is None or ignore.shape != f.image.shape[:2]:
            ignore = _ignore_mask(f.image.shape[:2], spec, int(sp["park_mask_px"]))
        if run and not (run[-1].image.shape == f.image.shape
                        and np.array_equal(run[-1].image[~ignore], f.image[~ignore])):
            run, last_fail = [f], ("frames_unstable", "condition 5: consecutive frames differ outside masks")
            continue
        run.append(f)
        if len(run) == need:
            first = run[0]
            return SettleResult(True, first.image, first.t_ms, run[-1].t_ms, None, None, None, run[-1].t_ms)
    elapsed = last.t_ms if last is not None else 0
    if last_fail is None or last_fail[0] == "park":
        detail = last_fail[1] if last_fail else f"fewer than {need} frames {interval} ms apart within {max_wait} ms"
        reason = "timeout"
    else:
        reason, detail = last_fail
    return SettleResult(False, None, None, None, reason, detail, last.image if last is not None else None, elapsed)


def settle_regression(p50_ms: float, calibration_p50_ms: float | None,
                      params: Mapping[str, Any] | None = None) -> bool:
    """`settle_regression` when the p50 time to settle is above 2 x the calibration p50 (3.5)."""
    if calibration_p50_ms is None:
        return False
    factor = float((params or load_params())["settle"]["regression_factor"])
    return p50_ms > factor * calibration_p50_ms


# --------------------------------------------------------------------------------------------
# 3.10 targeted re-run
# --------------------------------------------------------------------------------------------

TRIGGERS = ("diff", "invariant", "assertion")


def recaptures_allowed(trigger: str, params: Mapping[str, Any] | None = None) -> int:
    """Re-captures per checkpoint (3.10 steps 1 and 4): two for an above-threshold diff, one for
    an `invariant_violation` (F49), none for an `assertion_failed` (final without a re-run)."""
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger {trigger!r} is not one of {TRIGGERS}")
    rp = (params or load_params())["rerun"]
    return {"diff": int(rp["max_recaptures_diff"]), "invariant": int(rp["max_recaptures_invariant"]),
            "assertion": 0}[trigger]


class RerunBudget:
    """The run's re-capture budget: max(30, 10 percent of the run's checkpoints) checkpoints (3.10)."""

    def __init__(self, checkpoints_in_run: int, params: Mapping[str, Any] | None = None):
        rp = (params or load_params())["rerun"]
        self.limit = max(int(rp["run_budget_min"]), math.ceil(float(rp["run_budget_fraction"]) * checkpoints_in_run))
        self.used = 0

    def take(self) -> bool:
        """Reserve one checkpoint's re-captures; False once the budget is spent (`rerun_budget_exhausted`)."""
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass(frozen=True)
class Recapture:
    """One targeted re-capture: settled with an image, or `not_settled`."""

    settled: bool
    image: np.ndarray | None = None
    invariant_fired: bool | None = None


@dataclass(frozen=True)
class Stability:
    """`stability` and `stability_cause` of the outcome record (4.13); `unstable` carries the
    `not_settled (reason: unstable)` outcome."""

    stability: str
    cause: str | None = None
    recaptures: int = 0
    pairs_above: tuple[tuple[str, int, int], ...] = ()

    @property
    def outcome(self) -> tuple[str, str] | None:
        return ("not_settled", "unstable") if self.stability == "unstable" else None


def _unknown(recapturable: bool, budget_ok: bool) -> Stability | None:
    if not recapturable:
        return Stability("unknown", "not_recapturable")
    if not budget_ok:
        return Stability("unknown", "budget")
    return None


def _check_attempts(recaptures: Sequence[Recapture], allowed: int) -> int:
    failed = sum(1 for r in recaptures if not r.settled)
    if failed == 1 and not any(r.settled for r in recaptures):
        raise ValueError("a not_settled re-capture is retried once more before stability is decided (3.10)")
    if len(recaptures) > allowed + (1 if failed else 0):
        raise ValueError(f"at most {allowed} re-captures (plus one retry of a not_settled one), got {len(recaptures)}")
    return failed


def diff_stability(first: np.ndarray, recaptures: Sequence[Recapture],
                   above: Callable[[np.ndarray, np.ndarray], bool], *, base_captures: Sequence[np.ndarray] = (),
                   recapturable: bool = True, budget_ok: bool = True,
                   params: Mapping[str, Any] | None = None) -> Stability:
    """Stability of an above-threshold diff (3.10 step 3). `above(a, b)` runs the Diff Engine on
    two captures of the same side with the region thresholds and says whether any region is over."""
    unknown = _unknown(recapturable, budget_ok)
    if unknown is not None:
        return unknown
    if not recaptures:
        raise ValueError("diff stability needs at least one re-capture")
    failed = _check_attempts(recaptures, recaptures_allowed("diff", params))
    if failed >= 2:
        return Stability("unstable", recaptures=len(recaptures))
    hits: list[tuple[str, int, int]] = []
    for side, caps in (("candidate", [first] + [r.image for r in recaptures if r.settled]),
                       ("base", list(base_captures))):
        for i in range(len(caps)):
            for j in range(i + 1, len(caps)):
                if above(caps[i], caps[j]):
                    hits.append((side, i, j))
    return Stability("unstable" if hits else "stable", recaptures=len(recaptures), pairs_above=tuple(hits))


def invariant_stability(recaptures: Sequence[Recapture], *, recapturable: bool = True, budget_ok: bool = True,
                        params: Mapping[str, Any] | None = None) -> Stability:
    """Stability of an `invariant_violation` (3.10 step 3, F49): re-captured once; firing again is
    `stable` (the outcome stays `invariant_violation`), not firing is `unstable`."""
    unknown = _unknown(recapturable, budget_ok)
    if unknown is not None:
        return unknown
    if not recaptures:
        raise ValueError("invariant stability needs the confirming re-capture")
    failed = _check_attempts(recaptures, recaptures_allowed("invariant", params))
    if failed >= 2:
        return Stability("unstable", recaptures=len(recaptures))
    settled = [r for r in recaptures if r.settled]
    if settled[0].invariant_fired is None:
        raise ValueError("a settled invariant re-capture must say whether the invariant fired")
    return Stability("stable" if settled[0].invariant_fired else "unstable", recaptures=len(recaptures))
