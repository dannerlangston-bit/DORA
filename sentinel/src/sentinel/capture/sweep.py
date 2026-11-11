"""Size-sweep step measurement (2.5.1, 2.5.2; the built-in assertion `size_reached` of 1.10 and 4.7).

Every step of a w x h ladder measures the achieved size after the set or drag and compares it with the request
(2.5.1). Three cases, closed:

- within 4 px of the request on both axes: the size was taken; capture;
- larger than the request on an axis and within 4 px of the size achieved by the previous smaller request on that
  axis (or this is the smallest request on that axis), every other axis taken: the gadget's minimum clamps the
  request; a `size_step` event with reason `size_clamped`, requested and achieved sizes; capture anyway under
  the requested step id;
- anything else: the size was not reached; one retry of the set or drag, and still not reached the step is
  `failed`, its checkpoint `assertion_failed (assertion: size_reached)` with requested and measured sizes, and
  every later step of that (surface, host) ladder `not_run (reason: step_failed:<step>)`.

`classify_size_step(requested, measured, reference)` is the table; `check_size_reached(...)` adds the one retry and
the outcomes; `SizeLadder` keeps one (surface, host) ladder: the reference sizes for each step, the stop after a
failed step, the detach control or splitter the locator ladder cannot find (2.5.1: the step `failed`, its
checkpoint `landmark_missing`, the rest `not_run`), and the achieved minimum stored with the run (`MinSize`), which
the Reporter prints per gadget and host (`MinSize.report_line`) and which, moved between builds by more than
4 px, raises `window_geometry_changed` (`compare_minimums`). `size_step_event` is the 8.3 `step_events` row of a
step.

Choices where the spec is ambiguous or silent (reported):

1. "The previous smaller request on that axis". The ladders run in descending order (2.5.2), so the request just
   below this one on an axis has usually not been measured yet when this step needs its answer; read literally the
   first step that meets a gadget's minimum could never clamp. The reference is therefore the achieved size, on
   that axis, of the most recent earlier step of the same ladder whose request on that axis was smaller than or
   equal to this one; "the smallest request on that axis" is a request with no such earlier step (the smallest so
   far). A gadget whose minimum is met at 960x300 clamps there; 800x300 must then land within 4 px of what
   960x300 achieved, so a drag that did not take is still caught. Failed steps are no reference.
2. A measurement smaller than the request by more than 4 px on any axis is "any other result": not reached.
3. The `<step>` of `step_failed:<step>` for a sweep step is the size segment of its checkpoint id (`480x360`,
   `480x360-asc`), the step that failed.
4. A detach control or splitter not found ends the step it was needed for (2.5.1 says the first step; a splitter
   is dragged at every step, so any step can lose it): the checkpoint is `landmark_missing (reason:
   locator:<step>)` with the side, and the rest of the ladder `not_run (reason: step_failed:<step>)`.
5. The achieved minimum is the smallest achieved size on each axis over the ladder's steps that passed, marked
   clamped on an axis when a clamped step achieved it (within 4 px); a ladder with no passed step has none.
6. The `size_step` event carries the achieved size in `rect_w`/`rect_h` (the target window's rect size), the
   request in `target_text`, the attempt (2 after the retry), the step outcome, and `reason`: `size_clamped` for a
   clamp, `assertion:size_reached` for a failure, `retry: size not reached` after a successful retry.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..orchestrator.results import FindingSpec, Outcome
from ..records import StepEvent
from ..taxonomy import checkpoint_parts

TOLERANCE_PX = 4                                # 2.5.1: within 4 px on both axes
STEP_RESULTS = ("taken", "clamped", "not_reached")
AXES = ("width", "height")
Size = tuple[int, int]                          # (w, h) in client-area pixels (2.5.2)


class SweepError(ValueError):
    """A sweep call that breaks the procedure of 2.5.1 (a retry across a capture, a step after a failed one)."""


def _wh(s: Size) -> str:
    return f"{s[0]}x{s[1]}"


def classify_axes(requested: Size, measured: Size, reference: tuple[int | None, int | None]) -> tuple[str, str]:
    """The 2.5.1 case per axis (choices 1, 2): `taken`, `clamped` or `not_reached`."""
    out = []
    for r, m, ref in zip(requested, measured, reference):
        if abs(m - r) <= TOLERANCE_PX:
            out.append("taken")
        elif m > r and (ref is None or abs(m - ref) <= TOLERANCE_PX):
            out.append("clamped")
        else:
            out.append("not_reached")
    return out[0], out[1]


def classify_size_step(requested: Size, measured: Size, reference: tuple[int | None, int | None]) -> str:
    """The 2.5.1 case of one measurement: `taken` on both axes, `clamped` when every axis is taken or clamped and
    one is clamped, else `not_reached`. `reference` is, per axis, the achieved size of the previous smaller
    request (None when this is the smallest request on that axis; choice 1)."""
    axes = classify_axes(requested, measured, reference)
    if "not_reached" in axes:
        return "not_reached"
    return "clamped" if "clamped" in axes else "taken"


@dataclass(frozen=True)
class SizeStepResult:
    """One measured step: the final 2.5.1 case, the step outcome and, for a failure, the outcomes 2.5.1 gives."""

    step_id: str
    requested: Size
    measured: tuple[Size, ...]                  # the first measurement, then the retry's when there was one
    result: str                                 # STEP_RESULTS, of the last measurement
    axes: tuple[str, str]                       # per axis, of the last measurement
    status: str                                 # passed | passed_after_retry | failed (4.13 step outcomes)
    checkpoint: Outcome | None = None           # the failed step's checkpoint outcome
    rest_of_ladder: Outcome | None = None       # every later step of the (surface, host) ladder
    side: str | None = None

    @property
    def achieved(self) -> Size | None:
        return self.measured[-1] if self.measured else None

    @property
    def attempts(self) -> int:
        return max(1, len(self.measured))

    @property
    def clamped(self) -> bool:
        return self.result == "clamped"


def check_size_reached(requested: Size, first: Size, retry: Size | None, reference: tuple[int | None, int | None],
                       *, step_id: str, side: str | None = None) -> SizeStepResult:
    """2.5.1 with its one retry: taken or clamped on the first measurement passes; not reached needs the retry's
    measurement (`retry`), after which the step passes after retry or fails with `assertion_failed (assertion:
    size_reached)` and the rest of the ladder `not_run (reason: step_failed:<step>)` (choice 3)."""
    axes = classify_axes(requested, first, reference)
    result = classify_size_step(requested, first, reference)
    if result != "not_reached":
        if retry is not None:
            raise SweepError(f"step {step_id}: one retry per step, and only when the size was not reached (2.5.1)")
        return SizeStepResult(step_id, requested, (first,), result, axes, "passed", side=side)
    if retry is None:
        raise SweepError(f"step {step_id}: {_wh(requested)} not reached ({_wh(first)}); retry the set or drag once "
                         "before failing (2.5.1)")
    axes = classify_axes(requested, retry, reference)
    result = classify_size_step(requested, retry, reference)
    if result != "not_reached":
        return SizeStepResult(step_id, requested, (first, retry), result, axes, "passed_after_retry", side=side)
    detail = f"requested {_wh(requested)}, measured {_wh(first)}, after one retry {_wh(retry)}"
    return SizeStepResult(step_id, requested, (first, retry), result, axes, "failed",
                          checkpoint=Outcome("assertion_failed", "assertion:size_reached", detail=detail),
                          rest_of_ladder=Outcome("not_run", f"step_failed:{step_id}"), side=side)


@dataclass(frozen=True)
class MinSize:
    """The smallest achieved size of one gadget in one host for a run (2.5.1, choice 5)."""

    surface_id: str
    host: str
    w: int
    h: int
    clamped_w: bool
    clamped_h: bool

    def to_dict(self) -> dict:
        return {"surface_id": self.surface_id, "host": self.host, "w": self.w, "h": self.h,
                "clamped_w": self.clamped_w, "clamped_h": self.clamped_h}

    def report_line(self) -> str:
        """The Reporter's line for the achieved minimum (2.5.1)."""
        axes = [a for a, c in zip(AXES, (self.clamped_w, self.clamped_h)) if c]
        how = f"clamped on {' and '.join(axes)}" if axes else "no clamp"
        return f"{self.surface_id} ({self.host}): achieved minimum {self.w}x{self.h}, {how}"


def compare_minimums(base: MinSize, candidate: MinSize) -> FindingSpec | None:
    """`window_geometry_changed` when the achieved minimum moved by more than 4 px on an axis between builds
    (2.5.1: "a build that moves it shows up as a finding"); None otherwise."""
    if (base.surface_id, base.host) != (candidate.surface_id, candidate.host):
        raise SweepError("compare the minimums of one gadget in one host")
    if abs(base.w - candidate.w) <= TOLERANCE_PX and abs(base.h - candidate.h) <= TOLERANCE_PX:
        return None
    return FindingSpec("window_geometry_changed",
                       f"{base.surface_id} ({base.host}): achieved minimum {base.w}x{base.h} on base, "
                       f"{candidate.w}x{candidate.h} on candidate",
                       detail=f"base {base.w}x{base.h}; candidate {candidate.w}x{candidate.h}")


@dataclass
class SizeLadder:
    """One (surface, host) w x h ladder of one side, in the order its steps run (2.5.1, 2.5.2)."""

    surface_id: str
    host: str
    side: str = "candidate"
    passed: list[SizeStepResult] = field(default_factory=list)
    failed: SizeStepResult | None = None

    def _step(self, checkpoint_id: str) -> tuple[str, Size]:
        p = checkpoint_parts(checkpoint_id)
        if p["kind"] != "sweep" or (p["surface"], p["host"]) != (self.surface_id, self.host):
            raise SweepError(f"{checkpoint_id} is not a step of the {self.surface_id} {self.host} ladder")
        return p["qualifier"].split("/", 1)[1], (p["w"], p["h"])

    def reference(self, requested: Size) -> tuple[int | None, int | None]:
        """Per axis, the achieved size of the most recent passed step whose request on that axis was smaller than or
        equal to this one; None when there is none (choice 1)."""
        ref: list[int | None] = [None, None]
        for axis in (0, 1):
            for s in reversed(self.passed):
                if s.requested[axis] <= requested[axis] and s.measured:
                    ref[axis] = s.measured[-1][axis]
                    break
        return ref[0], ref[1]

    def blocked(self) -> Outcome | None:
        """`not_run (reason: step_failed:<step>)` for every step after a failed one; None while the ladder runs."""
        return self.failed.rest_of_ladder if self.failed is not None else None

    def _record(self, r: SizeStepResult) -> SizeStepResult:
        if r.status == "failed":
            self.failed = r
        else:
            self.passed.append(r)
        return r

    def _check_open(self, checkpoint_id: str) -> None:
        if self.failed is not None:
            raise SweepError(f"{checkpoint_id}: the ladder stopped at {self.failed.step_id}; later steps are "
                             f"{self.failed.rest_of_ladder.reason if self.failed.rest_of_ladder else 'not_run'}")

    def needs_retry(self, checkpoint_id: str, first: Size) -> bool:
        """True when the first measurement of the step did not reach the size: retry the set or drag once, then
        `measure` with both measurements (2.5.1)."""
        self._check_open(checkpoint_id)
        _, requested = self._step(checkpoint_id)
        return classify_size_step(requested, first, self.reference(requested)) == "not_reached"

    def measure(self, checkpoint_id: str, first: Size, retry: Size | None = None) -> SizeStepResult:
        """Classify the step `checkpoint_id` (`sweep/<surface>/<host>/<w>x<h>[-asc]`) from its measurement(s)."""
        self._check_open(checkpoint_id)
        step_id, requested = self._step(checkpoint_id)
        return self._record(check_size_reached(requested, first, retry, self.reference(requested), step_id=step_id,
                                               side=self.side))

    def control_missing(self, checkpoint_id: str, control: str) -> SizeStepResult:
        """The detach control or splitter the locator ladder cannot find (2.5.1, choice 4)."""
        self._check_open(checkpoint_id)
        step_id, requested = self._step(checkpoint_id)
        return self._record(SizeStepResult(
            step_id, requested, (), "not_reached", ("not_reached", "not_reached"), "failed",
            checkpoint=Outcome("landmark_missing", f"locator:{step_id}",
                               detail=f"{control} not found by the locator ladder on {self.side}"),
            rest_of_ladder=Outcome("not_run", f"step_failed:{step_id}"), side=self.side))

    def minimum(self) -> MinSize | None:
        """The achieved minimum of this gadget in this host (choice 5)."""
        done = [(s.axes, s.measured[-1]) for s in self.passed if s.measured]
        if not done:
            return None
        lows = [min(size[a] for _, size in done) for a in (0, 1)]
        clamped = [any(axes[a] == "clamped" and abs(size[a] - lows[a]) <= TOLERANCE_PX for axes, size in done)
                   for a in (0, 1)]
        return MinSize(self.surface_id, self.host, lows[0], lows[1], clamped[0], clamped[1])


def size_step_event(result: SizeStepResult, *, checkpoint_id: str, run_id: str, shard_id: str, side: str, seq: int,
                    wall_at: str, mono_ms: int, mode: str = "scripted", app_uptime_s: int | None = None,
                    app_restart_count: int | None = None) -> StepEvent:
    """The 8.3 `size_step` row of one step (choice 6)."""
    if result.checkpoint is not None:
        reason: str | None = result.checkpoint.reason           # assertion:size_reached or locator:<step>
    elif result.clamped:
        reason = "size_clamped"
    else:
        reason = "retry: size not reached" if result.status == "passed_after_retry" else None
    achieved = result.achieved
    return StepEvent(run_id=run_id, shard_id=shard_id, side=side, seq=seq, source="orchestrator", wall_at=wall_at,
                     mono_ms=mono_ms, mode=mode, action="size_step", step_id=result.step_id,
                     checkpoint_id=checkpoint_id, attempt=result.attempts, step_outcome=result.status,
                     target_text=f"requested {_wh(result.requested)}", reason=reason,
                     rect_w=achieved[0] if achieved else None, rect_h=achieved[1] if achieved else None,
                     app_uptime_s=app_uptime_s, app_restart_count=app_restart_count)


def minimums_summary(ladders: Sequence[SizeLadder]) -> list[dict]:
    """The per-run table of achieved minimums (2.5.1: stored with the run), one row per ladder that has one."""
    return [m.to_dict() for m in (lad.minimum() for lad in ladders) if m is not None]
