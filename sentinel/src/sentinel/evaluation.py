"""Evaluation inputs: settle parameters from config, the step-failure rules, input B and re-captures.

Four pieces the per-checkpoint evaluation (5.17) reads before it classifies, kept out of the pipeline so the
simulated run and a real rig share them:

- `settle_spec_for(cfg, checkpoint)` builds the 3.5 `SettleSpec` of one checkpoint from the configuration: the
  checkpoint's `max_settle_ms`, else its surface's `settle.max_wait_ms`, else the 3.5 default (long for chart and
  scan checkpoints); `keep_focus` (4.7, SD-27); the surface's `must_see` landmarks (4.5, SD-37) and the regions
  file's landmarks as must-see anchors; the 3.5 anti-anchor texts, the surface's `anti_anchors` and the platform's
  spinner and empty-grid templates; the regions file's masks (a `keep_focus` caret mask among them); the flow's
  neutral control.
- `base_side_outcome(step_results, manifest, surface)` applies the step-failure rules of 3.1, 2.4 and 2.6.1 to
  one checkpoint from the step records of both builds (SD-07): a candidate failure gives the candidate rule
  (`landmark_missing (locator:<step>)` on the checkpoint the step led to, `not_run (step_failed:<step>)` after
  it, labeled `both` when the base failed the same step); a base-only failure gives `needs_baseline
  (new_surface)` when an entry declaring the surface has `new_surface: true`, else `not_run
  (base_step_failed)`, and the candidate capture still runs the Invariant Checker and the Oracle.
- `settle_base(capture, spec)` and `b_input_reason(evidence, new_surface=...)` give input B of 3.11: a base
  capture that fails (`not_settled`, `env_error`, `harness_error`) is re-captured once while its session is at
  the checkpoint, and only the second failure sets B `absent` (`base_not_settled`, `base_env_error`,
  `base_harness_error`); the other `absent` reasons (`new_surface`, `lineage_missing`, `reuse_missing`,
  `explorer_replay`) and `removed` come from the evidence the caller holds.
- `recapture_plan`, `take_recaptures` and `rerun_stability` are the targeted re-run of 3.10: how many
  re-captures each side takes, each through the settle rule with one retry of a `not_settled` re-capture, and the
  resulting stability; `flake_event` is the `flake_events` row of an `unstable` result (3.10 step 5).

Choices where the spec is ambiguous (reported):

1. Max wait (3.5, 4.5, 4.7). 3.5, 5.7 and 12.x give a default of 15 s (30 s for chart and scan checkpoints);
   4.5's notation gives `settle: {max_wait_ms: int = 8000}`, and `settle` is required on every surface, so
   applying the 8000 default would leave 3.5's defaults unreachable. The checkpoint's `max_settle_ms` wins; else
   a surface file that writes `settle.max_wait_ms` sets the wait; else 3.5's default applies (the settle
   parameters `max_wait_ms` and `max_wait_long_ms`). 4.5's 8000 is not applied.
2. "Chart and scan checkpoints" (3.5): a checkpoint whose surface has `kind: chart`, or whose surface id's area
   segment is `scan` (4.5 has no scan kind; thinkorswim's Scan tab is an area).
3. Must-see anchors (3.5 condition 3, SD-37): the regions file's landmarks (searched at their `search_rect`, else
   their position plus or minus the 3.7 search margin, else the whole frame) and the surface's landmarks with
   `must_see: true` (searched in the whole frame); a surface landmark whose id a regions-file landmark already
   has is one anchor. Every anchor matches at the 3.5 confidence of 0.92: a landmark's `confidence_min` is a 3.7
   (landmark location) override and is not read by settle.
4. Anti-anchors (3.5 condition 4): the three 3.5 texts, the surface's `anti_anchors`, and the spinner and
   empty-grid templates, which no configuration file declares, so the caller passes them; duplicate texts are
   compared case-insensitively.
5. The neutral control (3.5 condition 2) is the `target` of the flow's `normalize` `focus_neutral` step (4.7);
   a census, sweep or split checkpoint, or a flow without one, keeps the generic label. It only names the
   control in a `not_settled` detail.
6. Masks are placed at the landmark positions the caller passes (the platform locates them on the first frame),
   else at the landmarks' `expected` positions; a mask whose landmark has neither is not applied.
7. The base-only rule is decided per checkpoint by that checkpoint's surface: any manifest entry declaring the
   surface with `new_surface: true` counts (4.3 "the entry that declares the surface"; several entries may).
   A base step that ends `blocked_by_gateway` is a base step failure like `failed`.
8. When the candidate failed a step before the checkpoint, the candidate rule decides, whatever the base did; the
   failure is labeled `both` only when the base failed that same step (2.6.1 "fails on both builds"); a base
   step that failed earlier is named in `detail`. A step record without `side` is the candidate's.
9. Input B, first applicable (3.11): `removed`; `absent (explorer_replay)`; a base step failure (`absent
   (new_surface)`, or G `not_run (base_step_failed)`, which is not a B value); `present` for a settled base
   capture; the last base capture failure (`base_not_settled`, `base_env_error`, `base_harness_error`);
   `absent (new_surface)` for a declared new surface the base never captured; `lineage_missing`;
   `reuse_missing`. A base capture failure whose session could not re-capture (it moved on) sets B `absent` at
   once, with that failure's reason.
10. Re-captures (3.10 steps 2 and 3): an `invariant_violation` re-captures the candidate only (the invariant is
    judged on the candidate capture); the base is re-captured for a diff when it is not reused and is a
    `masked_live` lockstep session or a frozen base session still open; two `not_settled` base re-captures
    are `unstable` like two candidate ones. One run-budget unit is taken per re-captured checkpoint, and none
    for a checkpoint that is `recapturable: false`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import cv2
import numpy as np

from .classify import Condition, EnvFailure
from .diff import load_params
from .diff.regions import Rect, RegionsFile
from .diff.settle import (
    DEFAULT_ANTI_ANCHOR_TEXTS, Anchor, Frame, Recapture, RerunBudget, SettleResult, SettleSpec, Stability,
    diff_stability, invariant_stability, recaptures_allowed, settle,
)
from .records import StepOutcome
from .scope import Manifest, PlannedCheckpoint

FAILED_STEP_STATUSES = ("failed", "blocked_by_gateway")      # 2.6.1: a step that ends the flow
CAPTURE_FAILURES = {"not_settled": "base_not_settled", "env_error": "base_env_error",
                    "harness_error": "base_harness_error"}   # 3.11 input B, row 9
LONG_WAIT_KINDS = frozenset({"chart"})                       # 3.5 chart checkpoints (choice 2)
LONG_WAIT_AREAS = frozenset({"scan"})                        # 3.5 scan checkpoints (choice 2)


# --------------------------------------------------------------------------------------------
# 3.5 settle parameters from the configuration
# --------------------------------------------------------------------------------------------

class ConfigView(Protocol):
    """What `settle_spec_for` reads from a configuration (`pipeline.config.SentinelConfig` has it): registry
    entries by surface id (4.5), flow documents by flow id (4.7) and the repository root templates live under."""

    surfaces: Mapping[str, Mapping[str, Any]]
    flow_docs: Mapping[str, Mapping[str, Any]]
    root: Path


TemplateResolver = Callable[[str, str], "np.ndarray | None"]


def read_template(root: Path) -> TemplateResolver:
    """The default template resolver: the PNG at `root / path` as RGB (L-SRF-06 guarantees it exists)."""
    def resolve(landmark_id: str, path: str) -> np.ndarray:
        bgr = cv2.imread(str(Path(root) / path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"landmark {landmark_id}: template {path} is not readable under {root}")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return resolve


def flow_step(cfg: ConfigView, checkpoint: PlannedCheckpoint) -> Mapping[str, Any]:
    """The checkpoint step of a flow checkpoint (4.7), or an empty mapping for any other checkpoint."""
    if not checkpoint.flow_id:
        return {}
    for st in cfg.flow_docs.get(checkpoint.flow_id, {}).get("steps") or ():
        if st.get("id") == checkpoint.step_id:
            return st
    return {}


def long_wait(surface: Mapping[str, Any]) -> bool:
    """A chart or scan checkpoint takes the long 3.5 wait (choice 2)."""
    area = str(surface.get("id", "")).split(".", 1)[0]
    return surface.get("kind") in LONG_WAIT_KINDS or area in LONG_WAIT_AREAS


def max_wait_ms(cfg: ConfigView, checkpoint: PlannedCheckpoint) -> int | None:
    """The declared max wait (4.7 `max_settle_ms`, else 4.5 `settle.max_wait_ms` as written), or None for the 3.5
    default (choice 1)."""
    declared = flow_step(cfg, checkpoint).get("max_settle_ms")
    if declared is None:
        declared = (cfg.surfaces.get(checkpoint.surface_id, {}).get("settle") or {}).get("max_wait_ms")
    return int(declared) if declared is not None else None


def neutral_control(cfg: ConfigView, checkpoint: PlannedCheckpoint) -> str | None:
    """The `target` of the flow's `normalize` `focus_neutral` step (choice 5), or None."""
    if not checkpoint.flow_id:
        return None
    for st in cfg.flow_docs.get(checkpoint.flow_id, {}).get("normalize") or ():
        if st.get("type") == "focus_neutral" and st.get("target"):
            return str(st["target"])
    return None


def _regions_anchor(lm: Any, pos: tuple[int, int] | None, margin: int) -> Anchor:
    th, tw = lm.template.shape[:2]
    if lm.search_rect is not None:
        box = lm.search_rect
    elif pos is not None:
        box = Rect(pos[0] - margin, pos[1] - margin, tw + 2 * margin, th + 2 * margin)
    else:
        box = None
    return Anchor(lm.id, template=lm.template, search=box)


def settle_spec_for(cfg: ConfigView, checkpoint: PlannedCheckpoint, *, regfile: RegionsFile | None = None,
                    park: tuple[int, int] | None = None, positions: Mapping[str, tuple[int, int]] | None = None,
                    template: TemplateResolver | None = None, anti_anchor_templates: Sequence[Anchor] = (),
                    params: Mapping[str, Any] | None = None) -> SettleSpec:
    """The 3.5 settle parameters of one checkpoint (module docstring, choices 1 to 6).

    `regfile` is the checkpoint's Region Registry entry (its landmarks become must-see anchors and its masks are
    ignored by frame stability); `park` the parking coordinate; `positions` where each landmark sits on the frame
    (default: its `expected` position); `template(landmark_id, path)` resolves a surface landmark's template
    (default: the PNG under `cfg.root`; a resolver may return None to leave that anchor out where the platform
    cannot show it, as the simulation does); `anti_anchor_templates` are the platform's spinner and empty-grid
    templates."""
    p = params or load_params()
    margin = int(p["landmarks"]["search_margin_px"])
    surface = cfg.surfaces.get(checkpoint.surface_id, {})
    step = flow_step(cfg, checkpoint)
    resolve = template or read_template(cfg.root)
    pos = {lm.id: lm.expected for lm in (regfile.landmarks if regfile else ()) if lm.expected is not None}
    pos.update(positions or {})

    anchors: list[Anchor] = [_regions_anchor(lm, pos.get(lm.id), margin) for lm in (regfile.landmarks if regfile else ())]
    seen = {a.name for a in anchors}
    for lm in surface.get("landmarks") or ():
        if lm.get("must_see") is True and str(lm["id"]) not in seen:
            img = resolve(str(lm["id"]), str(lm["template"]))
            if img is not None:
                anchors.append(Anchor(str(lm["id"]), template=img))
                seen.add(str(lm["id"]))

    anti: list[Anchor] = []
    texts: set[str] = set()
    for t in list(DEFAULT_ANTI_ANCHOR_TEXTS) + [str(x) for x in surface.get("anti_anchors") or ()]:
        if t.lower() not in texts:
            texts.add(t.lower())
            anti.append(Anchor(t, text=t))
    anti.extend(anti_anchor_templates)

    masks: list[Rect] = []
    for r in (regfile.regions if regfile else ()):
        at = pos.get(r.landmark)
        if r.type == "mask" and at is not None:
            masks.append(r.rect.offset(*at))

    control = neutral_control(cfg, checkpoint)
    return SettleSpec(anchors=anchors, anti_anchors=anti, ignore=masks, park=park,
                      neutral_control=control or SettleSpec.neutral_control, long_wait=long_wait(surface),
                      max_wait_ms=max_wait_ms(cfg, checkpoint), keep_focus=step.get("keep_focus") is True)


# --------------------------------------------------------------------------------------------
# 3.1, 2.4, 2.6.1 step failures and 3.11 input B
# --------------------------------------------------------------------------------------------

def declares_new_surface(manifest: Manifest | None, surface: str) -> bool:
    """True when a loaded manifest entry declaring `surface` carries `new_surface: true` (4.3, choice 7)."""
    return manifest is not None and any(surface in e.declared_surfaces and e.new_surface for e in manifest.entries)


def _side(s: StepOutcome) -> str:
    return s.side or "candidate"


def first_failed_step(step_results: Iterable[StepOutcome], side: str) -> StepOutcome | None:
    """The first step of `side` that ended the flow (`failed` after its retry, or `blocked_by_gateway`)."""
    return next((s for s in step_results if _side(s) == side and s.status in FAILED_STEP_STATUSES), None)


@dataclass(frozen=True)
class BaseEvidence:
    """What the run knows about the base side of one checkpoint (3.11 input B).

    `captured`: a settled base capture exists (fresh, re-captured, reused or from the lineage set).
    `failures`: the failed base capture attempts in order (`not_settled`, `env_error`, `harness_error`), as
    `settle_base` records them. `step_failed`: the base step that failed after its retry while the candidate
    passed it. `explorer_replay`: a replayed Explorer candidate flow (2.9). `lineage_missing`: an RC run whose
    lineage set lacks the checkpoint. `reuse_missing`: frozen reuse was expected and the capture was not found.
    `removed`: the 2.2.3 label probe found the surface's label on the base and absent, unpaired, on the
    candidate."""

    captured: bool = False
    failures: tuple[str, ...] = ()
    step_failed: str | None = None
    explorer_replay: bool = False
    lineage_missing: bool = False
    reuse_missing: bool = False
    removed: bool = False

    def __post_init__(self) -> None:
        for f in self.failures:
            if f not in CAPTURE_FAILURES:
                raise ValueError(f"base capture failure {f!r} is not one of {tuple(CAPTURE_FAILURES)}")


@dataclass(frozen=True)
class BInput:
    """Input B (3.11), or the G `not_run (base_step_failed)` a base step failure on a surface that is not new
    sets instead. `facts` are the `classify.Facts` keyword arguments it contributes."""

    b: str | None                       # present | absent | removed; None when G not_run decides
    reason: str | None = None           # the needs_baseline reason of an absent base
    not_run: Condition | None = None

    @property
    def facts(self) -> dict[str, Any]:
        if self.not_run is not None:
            return {"not_run": self.not_run}
        if self.b == "absent":
            return {"base": "absent", "base_reason": self.reason}
        return {"base": self.b}


def b_input_reason(evidence: BaseEvidence, *, new_surface: bool) -> BInput:
    """Input B for one checkpoint (3.11, 2.4, SD-07; first applicable, choice 9). `new_surface`: an entry
    declaring the checkpoint's surface carries `new_surface: true` (`declares_new_surface`)."""
    e = evidence
    if e.removed:
        return BInput("removed")
    if e.explorer_replay:
        return BInput("absent", "explorer_replay")
    if e.step_failed is not None:
        if new_surface:
            return BInput("absent", "new_surface")
        return BInput(None, not_run=Condition("base_step_failed", side="base",
                                              detail=f"base step {e.step_failed} failed after its retry; "
                                                     "the candidate passed it"))
    if e.captured:
        return BInput("present")
    if e.failures:
        return BInput("absent", CAPTURE_FAILURES[e.failures[-1]])
    if new_surface:
        return BInput("absent", "new_surface")
    if e.lineage_missing:
        return BInput("absent", "lineage_missing")
    if e.reuse_missing:
        return BInput("absent", "reuse_missing")
    raise ValueError("no base capture and no recorded reason for its absence (3.11 input B)")


@dataclass(frozen=True)
class SideVerdict:
    """What the step records of both builds decide for one checkpoint (3.1 Step failures, 2.4, 2.6.1).

    `case`: `passed` (no step failed before it on either side), `candidate`, `both` (the candidate rule) or `base`
    (base only). `facts` are the `classify.Facts` keyword arguments the verdict sets. `candidate_checks`: the base
    failed only, and the candidate capture still runs the Invariant Checker and the Oracle, whose findings are
    listed in Unexpected (3.1). `evidence` is the step finding's: the failed step, its side label, the candidate's
    pre-attempt screenshot and the base build's screenshot at the same step (3.1: "so a control that moved stays
    visible")."""

    case: str
    step_id: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    candidate_checks: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)


def _screens(step_results: Sequence[StepOutcome], step_id: str, side: str) -> dict[str, Any]:
    cand = next((s for s in step_results if _side(s) == "candidate" and s.step_id == step_id), None)
    base = next((s for s in step_results if _side(s) == "base" and s.step_id == step_id), None)
    return {"step_id": step_id, "side": side,
            "pre_screenshot": cand.pre_screenshot if cand else None,
            "base_screenshot": (base.pre_screenshot or base.post_screenshot) if base else None}


def base_side_outcome(step_results: Sequence[StepOutcome], manifest: Manifest | None, surface: str, *,
                      led_to: bool = False, assertion_ids: Sequence[str] = (),
                      env: EnvFailure | None = None) -> SideVerdict:
    """The step-failure rules for one checkpoint (module docstring; choices 7 and 8).

    `step_results` are the step records of the flow (or census path) executed before the checkpoint, both
    builds, in execution order. `led_to`: the checkpoint is the one the failing candidate step led to, which is
    `landmark_missing (locator:<step>)` for a `failed` step, unless the Executor saw an assertion fail
    (`assertion_ids`: `assertion_failed`, row 8) or an environment cause (`env`: `env_error`, row 1), and
    `not_run (step_failed:<step>)` for a `blocked_by_gateway` step; every later checkpoint is `not_run
    (step_failed:<step>)`."""
    cand = first_failed_step(step_results, "candidate")
    base = first_failed_step(step_results, "base")
    if cand is None and base is None:
        return SideVerdict("passed")
    if cand is not None:
        both = base is not None and base.step_id == cand.step_id
        side = "both" if both else "candidate"
        step = cand.step_id
        detail = f"step {step} failed on {'both builds' if both else 'the candidate'}"
        if base is not None and not both:
            detail += f"; the base failed step {base.step_id} earlier"
        facts: dict[str, Any]
        if led_to and cand.status == "failed" and env is not None:
            facts = {"env": env}
        elif led_to and cand.status == "failed" and assertion_ids:
            facts = {"assertion_ids": tuple(assertion_ids)}
        elif led_to and cand.status == "failed":
            facts = {"landmark": Condition(f"locator:{step}", side=side, detail=detail)}
        else:
            facts = {"not_run": Condition(f"step_failed:{step}", side=side, detail=detail)}
        return SideVerdict("both" if both else "candidate", step, facts, evidence=_screens(step_results, step, side))
    b = b_input_reason(BaseEvidence(step_failed=base.step_id), new_surface=declares_new_surface(manifest, surface))
    return SideVerdict("base", base.step_id, b.facts, candidate_checks=True,
                       evidence=_screens(step_results, base.step_id, "base"))


# --------------------------------------------------------------------------------------------
# 3.11 row 9: the base capture and its one re-capture
# --------------------------------------------------------------------------------------------

class CaptureFailure(Exception):
    """A capture attempt that ended in `env_error` or `harness_error` at the capture (3.11 row 9)."""

    def __init__(self, kind: str, detail: str | None = None):
        if kind not in ("env_error", "harness_error"):
            raise ValueError(f"capture failure kind {kind!r}: env_error or harness_error")
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class BaseCapture:
    """The base side's settle: the settled result, or the failed attempts (`failures`, with `details`)."""

    result: SettleResult | None
    failures: tuple[str, ...] = ()
    details: tuple[str | None, ...] = ()

    @property
    def settled(self) -> bool:
        return self.result is not None and self.result.settled

    @property
    def attempts(self) -> int:
        return len(self.failures) + (1 if self.settled else 0)

    def evidence(self, **kw: Any) -> BaseEvidence:
        """The `BaseEvidence` of this capture, with the caller's other facts (`removed`, `explorer_replay`...)."""
        return BaseEvidence(captured=self.settled, failures=self.failures, **kw)


def settle_base(capture: Callable[[], Sequence[Frame]], spec: SettleSpec, params: Mapping[str, Any] | None = None,
                *, recapturable: bool = True) -> BaseCapture:
    """Settle the base capture; a failed attempt (`not_settled`, or a `CaptureFailure`) is re-captured once while
    the base session is still at the checkpoint (`recapturable`), so only a second failure leaves B absent (3.11
    row 9, 2.4, SD-07). `capture()` runs the settle loop's frame grab after the normalize routine."""
    failures: list[str] = []
    details: list[str | None] = []
    last: SettleResult | None = None
    for _ in range(2 if recapturable else 1):
        try:
            last = settle(capture(), spec, params)
        except CaptureFailure as exc:
            failures.append(exc.kind)
            details.append(exc.detail)
            continue
        if last.settled:
            return BaseCapture(last, tuple(failures), tuple(details))
        failures.append("not_settled")
        details.append(f"{last.reason}: {last.detail}")
    return BaseCapture(last, tuple(failures), tuple(details))


# --------------------------------------------------------------------------------------------
# 3.10 targeted re-run
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RecapturePlan:
    """How a checkpoint is re-captured (3.10 steps 1, 2 and 4): `candidate` and `base` re-captures to take, or
    `unknown_cause` (`not_recapturable`, `budget`) when none are and stability is `unknown`."""

    trigger: str
    candidate: int = 0
    base: int = 0
    unknown_cause: str | None = None


def recapture_plan(trigger: str, *, data_mode: str, recapturable: bool = True, budget: RerunBudget | None = None,
                   base_reused: bool = False, base_session_open: bool = False,
                   params: Mapping[str, Any] | None = None) -> RecapturePlan:
    """The re-capture plan for one trigger (`diff`, `invariant` or `assertion`; module docstring choice 10).

    An `assertion_failed` is final without a re-run. A `recapturable: false` checkpoint takes nothing and no budget
    (`not_recapturable`); otherwise one unit of the run budget is taken, and an exhausted budget is `budget`. The
    base side is re-captured for a diff when it is not reused and is a `masked_live` lockstep session or a frozen
    base session still open (`base_session_open`)."""
    allowed = recaptures_allowed(trigger, params)
    if allowed == 0:
        return RecapturePlan(trigger)
    if not recapturable:
        return RecapturePlan(trigger, unknown_cause="not_recapturable")
    if budget is not None and not budget.take():
        return RecapturePlan(trigger, unknown_cause="budget")
    base_open = not base_reused and (data_mode == "masked_live" or base_session_open)
    return RecapturePlan(trigger, candidate=allowed, base=allowed if trigger == "diff" and base_open else 0)


T = TypeVar("T")


@dataclass(frozen=True)
class Taken(Generic[T]):
    """One re-capture: what the capture call returned, its settle result, and the `Recapture` stability reads."""

    source: T
    result: SettleResult
    recapture: Recapture


def take_recaptures(count: int, capture: Callable[[], T], spec: SettleSpec, *,
                    frames: Callable[[T], Sequence[Frame]] | None = None, fired: Callable[[T], bool] | None = None,
                    params: Mapping[str, Any] | None = None) -> tuple[Taken[T], ...]:
    """Take up to `count` settled re-captures, each through the settle rule (3.10 step 2). A re-capture that ends
    `not_settled` is retried once more; a second `not_settled` ends the series, which stability reads as
    `unstable` (3.10 step 3). `frames(source)` gives the frames of what `capture()` returned (default: it returned
    the frames); `fired(source)` says whether the invariant fired again on a settled re-capture (F49). Every
    re-capture is returned so the caller keeps and stores it ("the first capture is always kept")."""
    out: list[Taken[T]] = []
    retried = False
    settled = 0
    while settled < count:
        src = capture()
        res = settle(frames(src) if frames is not None else src, spec, params)    # type: ignore[arg-type]
        if res.settled:
            settled += 1
            out.append(Taken(src, res, Recapture(True, res.capture, fired(src) if fired is not None else None)))
            continue
        out.append(Taken(src, res, Recapture(False)))
        if retried:
            break
        retried = True
    return tuple(out)


def rerun_stability(plan: RecapturePlan, first: np.ndarray, taken: Sequence[Taken[Any]],
                    above: Callable[[np.ndarray, np.ndarray], bool] | None = None, *,
                    base_first: np.ndarray | None = None, base_taken: Sequence[Taken[Any]] = (),
                    params: Mapping[str, Any] | None = None) -> Stability:
    """Stability of a re-run checkpoint (3.10 step 3): `unknown` with the plan's cause; for an invariant, whether
    it fired again; for a diff, every settled candidate capture against every other and the base captures
    (`base_first` and the settled base re-captures) likewise when more than one exists; two `not_settled`
    re-captures on either side are `unstable`."""
    if plan.unknown_cause is not None:
        return Stability("unknown", plan.unknown_cause)
    recs = [t.recapture for t in taken]
    if plan.trigger == "invariant":
        return invariant_stability(recs, params=params)
    if plan.trigger != "diff":
        raise ValueError(f"an {plan.trigger!r} trigger is final without a re-run (3.10 step 1)")
    if above is None:
        raise ValueError("a diff re-run needs the region-threshold comparison `above(a, b)`")
    base_caps = [t.recapture.image for t in base_taken if t.recapture.settled]
    if base_first is not None and base_caps:
        base_caps.insert(0, base_first)
    st = diff_stability(first, recs, above, base_captures=base_caps if len(base_caps) > 1 else (), params=params)
    if st.stability == "stable" and sum(1 for t in base_taken if not t.recapture.settled) >= 2:
        return Stability("unstable", recaptures=st.recaptures, pairs_above=st.pairs_above)
    return st


def flake_event(stability: Stability, captures: Sequence[str], *, region_id: str | None = None) -> dict[str, Any] | None:
    """The keyword arguments of `Store.write_flake_event` for an `unstable` re-run (3.10 step 5): both (or all)
    capture hashes, the region and the metrics; None for any other stability."""
    if stability.stability != "unstable":
        return None
    if len(captures) < 2:
        raise ValueError("an unstable re-run keeps at least the first capture and one re-capture (3.10)")
    return {"captures": list(captures), "region_id": region_id,
            "metrics": {"stability": "unstable", "recaptures": stability.recaptures,
                        "pairs_above": [list(p) for p in stability.pairs_above]}}
