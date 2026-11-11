"""The locator ladder (5.5; 4.7 step common fields; 5.4.3 stage 3): a named target to physical screen coordinates.

`ladder(target, capture, ocr, jab, branches, ctx) -> LadderResult` runs, for one step:

1. attempt 1, rungs 1 to 3 on `capture`, first accepted rung wins:
   - rung 1, JAB (DR-01 Branch A only; otherwise the rung is recorded `rung1_skipped`): the accessible context with
     the target's role, name and path under the window; accepted when found, unique, visible and inside the window,
     else `locator_failed(jab)` (not found, ambiguous, stale, invisible, outside the window, bridge unavailable);
   - rung 2, image anchor: `TM_CCOEFF_NORMED` within the declared region; accepted when the best score is at least
     the floor (default 0.92) and the second peak at least 0.05 below it, else `locator_failed(image)` or
     `locator_ambiguous(image)`; the evidence carries the score, both peaks and the 40x20 crop at the match;
   - rung 3, OCR text anchor on the region upscaled 3x and inverted: exactly one word box (or run of word boxes on
     one line, for a target text of several words) whose normalized text matches exactly or at fuzzy ratio >= 0.9,
     else `locator_failed(ocr)` or `locator_ambiguous(ocr)`; the evidence carries the boxes considered;
2. when all fail, `ctx.retry()` runs `reset_input_state`, re-enumerates and re-captures, and attempt 2 tries the
   scripted rungs once more: success is `passed_after_retry`, the first attempt's screenshot kept;
3. then rung 4, the agent rescue, only when nothing disables it (`agent_disabled`): the target and the flow allow
   it, the step is not a `secret`, `login` or teardown step, the post-login checkpoint has passed, preflight
   enabled the agent, `phase0/branches.yaml` has DR-07 on Branch A, the per-run rescue cap is not spent and the
   flag (read fresh) is clear; else `agent_failed(disabled)`. Success within 25 actions and 90 s is
   `passed_via_agent_fallback` with a `locator_failure` finding and `rescued` set (the next checkpoint is captured
   after the flow's normalize steps); failure is `agent_failed(cap|refusal|vocabulary|abort_flag)`.

An exhausted ladder is step `failed`, the checkpoint `landmark_missing (reason: locator:<step>)`, the rest of the flow
`not_run (reason: step_failed:<step>)` and a `locator_failure` finding (`LadderResult.consequence`). The resolved
rung is recorded on every step (`LadderResult.rung`, `step_outcome_fields`). `RescueCap` counts rescues per run and
gives the run-health line once spent. `Ladder` implements the `ports.Locator` protocol over a `Frame`.

Choices where the spec is ambiguous or silent (reported):

1. Coordinates: the image anchor is the centre of the matched template, the target is the anchor plus the declared
   offset (5.17: "anchor (1412, 618) + offset (64, 0) -> target (1476, 618)"); the OCR anchor is the centre of the
   word box; the JAB target the centre of the context's bounds. Every point is physical screen pixels (the capture's
   origin added) and must lie inside the window, else the rung fails.
2. Second peak: the best score outside `max(5, min(template w, h) // 2)` px of the best, so the shoulder of the
   best peak is not mistaken for a second control.
3. A rung the target declares no definition for is not tried and not recorded, except rung 1 off Branch A, which
   records `rung1_skipped`. A declared region the capture does not resolve fails the rung (no silent whole-window
   search); with no region the whole window is searched.
4. DR-01 and DR-07 still `undecided` count as Branch B: rung 1 is skipped and the agent rung is disabled until the
   branch is decided.
5. The rescue cap counts rescue sessions started in the run (`policy/budgets.yaml` `max_rescued_steps_per_run`); a
   rescue that reports success after more than `max_actions` actions or `max_seconds` is `agent_failed(cap)`.
   The run-health line appears once the cap is reached.
6. OCR matches: every word box (or run) at ratio >= the fuzzy floor counts, exact or not; two of them are
   `locator_ambiguous(ocr)`. Text is compared after NFKC normalization, case folding and whitespace collapsing.
7. A rung result's `result` is the spec's code (`accepted`, `locator_failed(<rung>)`, `locator_ambiguous(<rung>)`,
   `rung1_skipped`, `agent_failed(<why>)`); the cause and the measurements go in `RungResult.evidence`, which the
   step event carries, and `to_record` keeps the 4.13 keys only.
8. The finding's crop is PNG-encoded and hashed (`LadderResult.crop_png`, evidence `crop_sha256` and `artifacts`) so
   the caller stores exactly the bytes the finding names; it is cut from the capture the rescue started from.
"""
from __future__ import annotations

import difflib
import hashlib
import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import cv2
import numpy as np

from ..capture.redaction import OcrWord, WordOcr
from ..diff.engine import match_template
from ..diff.regions import Rect
from ..orchestrator.gateway import Flag
from ..orchestrator.ports import AccessNode, AccessPort, Frame, LocateResult
from ..orchestrator.results import ControlResult, FindingSpec, Outcome, SubsetOutcome

IMAGE_FLOOR = 0.92                 # 5.5 rung 2 default floor
SECOND_PEAK_MARGIN = 0.05          # 5.5 rung 2: the second peak at least 0.05 below the best
MIN_PEAK_RADIUS = 5                # choice 2
OCR_FUZZY = 0.9                    # 5.5 rung 3 default fuzzy ratio
OCR_UPSCALE = 3                    # 5.5 rung 3, 2.8.1: upscaled 3x and inverted
CROP_W, CROP_H = 40, 20            # 5.5: the 40x20 crop at the match and around the final target
RESCUE_STEPS, RESCUE_ACTIONS, RESCUE_SECONDS = 25, 25, 90.0     # 5.5, 5.6, policy/budgets.yaml rescue
AGENT_FAILURES = ("cap", "refusal", "vocabulary", "abort_flag", "disabled")   # 5.5 rung 4 failure codes
ACCEPTED = "accepted"


# -- inputs -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowCapture:
    """The target window's capture for one attempt: the image (H x W x 3), the virtual-screen position of its pixel
    (0, 0), the named regions resolved in window pixels, the screenshot hash and the window handle (for JAB)."""

    image: Any
    origin: tuple[int, int] = (0, 0)
    regions: Mapping[str, Rect] = field(default_factory=dict)
    sha256: str | None = None
    hwnd: int | None = None

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.image.shape[:2]
        return int(w), int(h)

    def screen(self, x: float, y: float) -> tuple[int, int]:
        """A window pixel in physical screen coordinates."""
        return int(round(self.origin[0] + x)), int(round(self.origin[1] + y))

    def inside(self, point: tuple[int, int]) -> bool:
        """True when a screen point lies inside the window."""
        w, h = self.size
        return self.origin[0] <= point[0] < self.origin[0] + w and self.origin[1] <= point[1] < self.origin[1] + h

    @classmethod
    def from_frame(cls, frame: Frame, regions: Mapping[str, Rect] | None = None, *, sha256: str | None = None,
                   hwnd: int | None = None) -> "WindowCapture":
        return cls(frame.image, tuple(frame.origin), dict(regions or {}), sha256, hwnd)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Branches:
    """The two Phase 0 branches the ladder reads from `phase0/branches.yaml` (4.16): DR-01 `jab` and DR-07
    `computer_use`, each `A`, `B` or `undecided` (choice 4)."""

    jab: str = "undecided"
    computer_use: str = "undecided"

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "Branches":
        """From the parsed `phase0/branches.yaml` (its top-level `jab` and `computer_use` branch choices)."""
        def branch(key: str) -> str:
            v = doc.get(key)
            b = v.get("branch") if isinstance(v, Mapping) else None
            return b if b in ("A", "B") else "undecided"
        return cls(branch("jab"), branch("computer_use"))


@dataclass
class RescueCap:
    """The agent rescue limits (5.5, 5.6, policy/budgets.yaml `rescue`): rescued steps per run, actions and wall
    seconds per rescue. `used` counts the rescues started this run (choice 5)."""

    max_steps: int = RESCUE_STEPS
    max_actions: int = RESCUE_ACTIONS
    max_seconds: float = RESCUE_SECONDS
    used: int = 0

    @classmethod
    def from_budgets(cls, doc: Mapping[str, Any]) -> "RescueCap":
        """From the parsed `policy/budgets.yaml` (`rescue: {max_actions, max_seconds, max_rescued_steps_per_run}`);
        a missing key keeps the 5.5 default."""
        r = doc.get("rescue") or {}
        return cls(int(r.get("max_rescued_steps_per_run", RESCUE_STEPS)), int(r.get("max_actions", RESCUE_ACTIONS)),
                   float(r.get("max_seconds", RESCUE_SECONDS)))

    @property
    def spent(self) -> bool:
        return self.used >= self.max_steps

    def take(self) -> bool:
        """Count one rescue; False (and nothing counted) once the cap is spent."""
        if self.spent:
            return False
        self.used += 1
        return True

    def health_line(self) -> str | None:
        """The run-health line of a spent cap (5.5), or None."""
        if not self.spent:
            return None
        return f"agent rescue cap reached after {self.used} steps; later locator failures are failed"


@dataclass(frozen=True)
class RescueRequest:
    """What rung 4 asks the agent (5.5): click the control named `target` in window `window`, within the limits."""

    step_id: str
    target: str
    window: str
    max_actions: int
    max_seconds: float
    pre_screenshot: str | None = None


@dataclass(frozen=True)
class RescueOutcome:
    """What a rescue session returned: passed (the step's post-condition held) or the 5.5 failure code, the final
    target in physical screen coordinates, the actions and seconds it used and its action trace with rationale."""

    passed: bool
    failure: str | None = None
    point: tuple[int, int] | None = None
    actions: int = 0
    seconds: float = 0.0
    trace: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.passed == (self.failure is not None):
            raise ValueError("a rescue either passed or names its failure code")
        if self.failure is not None and self.failure not in AGENT_FAILURES:
            raise ValueError(f"agent failure {self.failure!r}; one of {AGENT_FAILURES} (5.5)")
        if self.passed and self.point is None:
            raise ValueError("a passed rescue names the final target")


class AgentRescuer(Protocol):
    """The agent session of rung 4 (5.6 toolset, through the gateway under profile `agent`)."""

    def rescue(self, request: RescueRequest) -> RescueOutcome:
        """Run one rescue session."""


@dataclass
class LadderContext:
    """The step and run state the ladder reads. `retry` runs `reset_input_state`, re-enumerates and re-captures for
    the second scripted attempt; `flag` reads the flag fresh; `templates` maps a template path to its image;
    `post_login` is False from launch until the post-login checkpoint (6.9, 7.7); `teardown` marks a step of the
    flow's teardown block; `flow_agent_fallback` is the flow header's `agent_fallback`."""

    step_id: str
    step_type: str
    cap: RescueCap
    retry: Callable[[], WindowCapture]
    flag: Callable[[], Flag]
    flow_class: str | None = None
    templates: Mapping[str, Any] = field(default_factory=dict)
    flow_agent_fallback: str = "allowed"
    agent_available: bool = True
    post_login: bool = True
    teardown: bool = False
    rescuer: AgentRescuer | None = None
    window: str = "main"


# -- results ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RungResult:
    """One rung tried on one attempt (choice 7)."""

    rung: str
    result: str
    score: float | None = None
    attempt: int = 1
    point: tuple[int, int] | None = None
    rect: tuple[int, int, int, int] | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    crop: Any = field(default=None, repr=False, compare=False)

    @property
    def accepted(self) -> bool:
        return self.result == ACCEPTED

    def to_record(self) -> dict[str, Any]:
        """The 4.13 `rung_results` entry: rung, result, score when measured."""
        out: dict[str, Any] = {"rung": self.rung, "result": self.result}
        if self.score is not None:
            out["score"] = round(float(self.score), 4)
        return out


@dataclass(frozen=True)
class LadderResult:
    """The ladder's answer for one step (module docstring)."""

    status: str                                   # passed | passed_after_retry | passed_via_agent_fallback | failed
    rung: str                                     # jab | image | ocr | agent | none
    rung_results: tuple[RungResult, ...]
    attempts: int
    point: tuple[int, int] | None = None
    rect: tuple[int, int, int, int] | None = None
    pre_screenshot: str | None = None
    rescued: bool = False
    finding: FindingSpec | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    crop_png: bytes | None = field(default=None, repr=False)
    consequence: ControlResult | None = None
    health_line: str | None = None

    def records(self) -> list[dict[str, Any]]:
        return [r.to_record() for r in self.rung_results]

    def step_outcome_fields(self) -> dict[str, Any]:
        """The `records.StepOutcome` fields the ladder decides (4.13)."""
        return {"status": self.status, "locator_rung": self.rung, "rung_results": self.records(),
                "pre_screenshot": self.pre_screenshot, "attempt": self.attempts}

    def to_locate_result(self) -> LocateResult:
        """The `ports.LocateResult` view."""
        return LocateResult(self.rung, self.point, self.rect, tuple(self.records()),
                            "locator_failure" if self.finding is not None else None)


# -- the ladder -------------------------------------------------------------------------------------------------
def ladder(target: Mapping[str, Any], capture: WindowCapture, ocr: WordOcr | None, jab: AccessPort | None,
           branches: Branches | Mapping[str, Any], ctx: LadderContext) -> LadderResult:
    """Resolve one step's target (module docstring)."""
    br = branches if isinstance(branches, Branches) else Branches.from_doc(branches)
    first, hit = _attempt(target, capture, ocr, jab, br, ctx, 1)
    if hit is not None:
        return _passed("passed", hit, tuple(first), 1, capture.sha256)
    again = ctx.retry()
    second, hit = _attempt(target, again, ocr, jab, br, ctx, 2)
    tried = tuple(first + second)
    if hit is not None:
        return _passed("passed_after_retry", hit, tried, 2, capture.sha256)
    why = agent_disabled(target, br, ctx)
    if why is None:
        if ctx.flag() != Flag.CLEAR:
            why = "flag_not_clear"
        elif not ctx.cap.take():
            why = "rescue_cap_spent"
    if why is not None or ctx.rescuer is None:
        agent = RungResult("agent", "agent_failed(disabled)", attempt=2, evidence={"cause": why or "no_rescuer"})
        return _failed(target, tried + (agent,), capture.sha256, ctx)
    out = ctx.rescuer.rescue(RescueRequest(ctx.step_id, str(target.get("name", "")), ctx.window,
                                           ctx.cap.max_actions, ctx.cap.max_seconds, capture.sha256))
    trace = {"trace": list(out.trace), "actions": out.actions, "seconds": out.seconds}
    over = out.actions > ctx.cap.max_actions or out.seconds > ctx.cap.max_seconds
    if not out.passed or over or out.point is None:
        agent = RungResult("agent", f"agent_failed({out.failure or 'cap'})", attempt=2, evidence=trace)
        return _failed(target, tried + (agent,), capture.sha256, ctx, trace=out.trace)
    agent = RungResult("agent", ACCEPTED, attempt=2, point=out.point, evidence=trace)
    crop = _crop(again.image, (out.point[0] - again.origin[0], out.point[1] - again.origin[1]))
    png = _png(crop)
    sha = hashlib.sha256(png).hexdigest() if png is not None else None
    results = tried + (agent,)
    evidence = _evidence(target, results, capture.sha256, ctx, out.trace, out.point, sha)
    return LadderResult("passed_via_agent_fallback", "agent", results, 2, out.point, None, capture.sha256,
                        rescued=True, finding=FindingSpec("locator_failure",
                                                          f"scripted locator broke at step {ctx.step_id}"),
                        evidence=evidence, crop_png=png, health_line=ctx.cap.health_line())


def agent_disabled(target: Mapping[str, Any], branches: Branches, ctx: LadderContext) -> str | None:
    """Why rung 4 may not run for this step (5.5, 4.7, 6.9), or None; the flag and the rescue cap are read by
    `ladder` at the moment rung 4 would start."""
    if target.get("agent_fallback", True) is False:
        return "target_forbids"
    if ctx.flow_agent_fallback != "allowed":
        return "flow_forbids"
    if ctx.step_type == "secret":
        return "secret_step"
    if ctx.flow_class == "login":
        return "login_flow"
    if ctx.teardown or ctx.step_type == "teardown":
        return "teardown_step"
    if not ctx.post_login:
        return "before_post_login"
    if not ctx.agent_available:
        return "agent_unavailable"
    if branches.computer_use != "A":
        return f"computer_use_branch_{branches.computer_use}"
    if ctx.rescuer is None:
        return "no_rescuer"
    return None


class Ladder:
    """`ports.Locator` over the ladder for one step: `locate(target, frame)`; `last` keeps the full result."""

    def __init__(self, ocr: WordOcr | None, jab: AccessPort | None, branches: Branches | Mapping[str, Any],
                 ctx: LadderContext, *, regions: Mapping[str, Rect] | None = None, hwnd: int | None = None) -> None:
        self.ocr, self.jab, self.branches, self.ctx = ocr, jab, branches, ctx
        self.regions, self.hwnd = dict(regions or {}), hwnd
        self.last: LadderResult | None = None

    def locate(self, target: Mapping[str, Any], frame: Frame) -> LocateResult:
        cap = WindowCapture.from_frame(frame, self.regions, hwnd=self.hwnd)
        self.last = ladder(target, cap, self.ocr, self.jab, self.branches, self.ctx)
        return self.last.to_locate_result()


def target_from_control(surface_id: str, control: Mapping[str, Any], *, region: str | None = None,
                        agent_fallback: bool = True) -> dict[str, Any]:
    """A Surface Registry control (4.5: `jab`, `template`, `ocr_text`, `click_offset`) as a 5.5 target."""
    off = control.get("click_offset") or {}
    offset = [int(off.get("dx", 0)), int(off.get("dy", 0))]
    image: dict[str, Any] = {"template": control["template"], "offset": offset}
    target: dict[str, Any] = {"name": f"{surface_id}.{control['id']}", "image": image,
                              "agent_fallback": agent_fallback}
    if control.get("jab"):
        target["jab"] = {"role": control["jab"]["role"], "name": control["jab"]["name"]}
    if control.get("ocr_text"):
        target["ocr"] = {"text": control["ocr_text"], "offset": list(offset)}
    if region is not None:
        image["region"] = region
        if "ocr" in target:
            target["ocr"]["region"] = region
    return target


# -- attempts and rungs -----------------------------------------------------------------------------------------
def _attempt(target: Mapping[str, Any], cap: WindowCapture, ocr: WordOcr | None, jab: AccessPort | None,
             br: Branches, ctx: LadderContext, attempt: int) -> tuple[list[RungResult], RungResult | None]:
    out: list[RungResult] = []
    for rung in (_rung_jab(target, cap, jab, br, attempt), _rung_image(target, cap, ctx, attempt),
                 _rung_ocr(target, cap, ocr, attempt)):
        if rung is None:
            continue
        out.append(rung)
        if rung.accepted:
            return out, rung
    return out, None


def _rung_jab(target: Mapping[str, Any], cap: WindowCapture, jab: AccessPort | None, br: Branches,
              attempt: int) -> RungResult | None:
    if br.jab != "A":
        return RungResult("jab", "rung1_skipped", attempt=attempt, evidence={"branch": br.jab})
    d = target.get("jab")
    if not isinstance(d, Mapping):
        return None

    def fail(cause: str, **ev: Any) -> RungResult:
        return RungResult("jab", "locator_failed(jab)", attempt=attempt, evidence={"cause": cause, **ev})

    if jab is None or cap.hwnd is None or not jab.available():
        return fail("bridge_unavailable")
    root = jab.tree(cap.hwnd)
    if root is None:
        return fail("bridge_unavailable")
    path = [p for p in str(d.get("path", "")).split("/") if p] or None
    found = [n for n, chain in _walk(root, ()) if n.role.lower() == str(d.get("role", "")).lower()
             and n.name == d.get("name") and (path is None or _path_matches(chain, path))]
    if not found:
        return fail("not_found")
    if len(found) > 1:
        return fail("ambiguous", count=len(found))
    node = found[0]
    if node.rect is None:
        return fail("stale")
    if "visible" not in node.states:
        return fail("invisible")
    x, y, w, h = node.rect
    win_w, win_h = cap.size
    if x < cap.origin[0] or y < cap.origin[1] or x + w > cap.origin[0] + win_w or y + h > cap.origin[1] + win_h:
        return fail("outside_window", rect=list(node.rect))
    return RungResult("jab", ACCEPTED, attempt=attempt, point=(x + w // 2, y + h // 2), rect=(x, y, w, h))


def _walk(node: AccessNode, chain: tuple[str, ...]):
    here = chain + (node.name,)
    yield node, here
    for c in node.children:
        yield from _walk(c, here)


def _path_matches(chain: Sequence[str], pattern: Sequence[str]) -> bool:
    """The node's name chain ends with the path, `...` standing for any number of intermediate contexts."""
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(chain)
        if pattern[i] == "...":
            return any(match(i + 1, k) for k in range(j, len(chain) + 1))
        return j < len(chain) and chain[j] == pattern[i] and match(i + 1, j + 1)
    return any(match(0, s) for s in range(len(chain)))


def _region(cap: WindowCapture, d: Mapping[str, Any]) -> Rect | None | str:
    """The declared region in window pixels, the whole window when none is declared, or the error text."""
    name = d.get("region")
    w, h = cap.size
    if name is None:
        return Rect(0, 0, w, h)
    r = cap.regions.get(str(name))
    if r is None:
        return f"region {name!r} is not resolved on this capture"
    return r.intersect(Rect(0, 0, w, h))


def _offset(d: Mapping[str, Any]) -> tuple[int, int]:
    off = d.get("offset") or (0, 0)
    return int(off[0]), int(off[1])


def _rung_image(target: Mapping[str, Any], cap: WindowCapture, ctx: LadderContext, attempt: int) -> RungResult | None:
    d = target.get("image")
    if not isinstance(d, Mapping):
        return None

    def fail(result: str, cause: str, score: float | None = None, **ev: Any) -> RungResult:
        return RungResult("image", result, score, attempt, evidence={"cause": cause, **ev})

    tpl = ctx.templates.get(str(d.get("template")))
    if tpl is None:
        return fail("locator_failed(image)", "template_missing")
    region = _region(cap, d)
    if not isinstance(region, Rect):
        return fail("locator_failed(image)", "region_unresolved" if region else "region_outside_window")
    th, tw = tpl.shape[:2]
    best, second, pos = match_template(cap.image, tpl, region, max(MIN_PEAK_RADIUS, min(tw, th) // 2))
    floor = float(d.get("floor", IMAGE_FLOOR))
    peaks = {"best": round(best, 4), "second": round(second, 4), "floor": floor}
    if pos is None:
        return fail("locator_failed(image)", "region_smaller_than_template", 0.0, **peaks)
    anchor = (pos[0] + tw / 2.0, pos[1] + th / 2.0)
    crop = _crop(cap.image, anchor)
    if best < floor:
        return RungResult("image", "locator_failed(image)", best, attempt,
                          evidence={"cause": "below_floor", **peaks}, crop=crop)
    if second > best - SECOND_PEAK_MARGIN:
        return RungResult("image", "locator_ambiguous(image)", best, attempt,
                          evidence={"cause": "second_peak", **peaks}, crop=crop)
    dx, dy = _offset(d)
    point = cap.screen(anchor[0] + dx, anchor[1] + dy)
    if not cap.inside(point):
        return RungResult("image", "locator_failed(image)", best, attempt,
                          evidence={"cause": "outside_window", **peaks}, crop=crop)
    return RungResult("image", ACCEPTED, best, attempt, point=point,
                      rect=(*cap.screen(pos[0], pos[1]), tw, th), evidence=peaks, crop=crop)


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def _ratio(a: str, b: str) -> float:
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def prepare_ocr(image: Any, region: Rect) -> Any:
    """The OCR input of rung 3: the region upscaled 3x and inverted (5.5, 2.8.1)."""
    crop = np.ascontiguousarray(image[region.y:region.y + region.h, region.x:region.x + region.w])
    big = cv2.resize(crop, (region.w * OCR_UPSCALE, region.h * OCR_UPSCALE), interpolation=cv2.INTER_CUBIC)
    return 255 - big


def _rung_ocr(target: Mapping[str, Any], cap: WindowCapture, ocr: WordOcr | None, attempt: int) -> RungResult | None:
    d = target.get("ocr")
    if not isinstance(d, Mapping):
        return None

    def fail(result: str, cause: str, score: float | None = None, **ev: Any) -> RungResult:
        return RungResult("ocr", result, score, attempt, evidence={"cause": cause, **ev})

    want = _norm(str(d.get("text", "")))
    if not want:
        return fail("locator_failed(ocr)", "no_text")
    if ocr is None:
        return fail("locator_failed(ocr)", "ocr_unavailable")
    region = _region(cap, d)
    if not isinstance(region, Rect):
        return fail("locator_failed(ocr)", "region_unresolved" if region else "region_outside_window")
    words = [_unscale(w, region) for w in ocr.words(prepare_ocr(cap.image, region))]
    fuzzy = float(d.get("fuzzy", OCR_FUZZY))
    considered = [(text, box, _ratio(_norm(text), want)) for text, box in _candidates(words, len(want.split()))]
    boxes = [{"text": t, "rect": [b.x, b.y, b.w, b.h], "ratio": round(r, 4)} for t, b, r in considered]
    matches = [(t, b, r) for t, b, r in considered if r >= fuzzy]
    best = max((r for _, _, r in considered), default=0.0)
    if not matches:
        return fail("locator_failed(ocr)", "no_match", best, words=boxes)
    if len(matches) > 1:
        return fail("locator_ambiguous(ocr)", "several_matches", best, words=boxes)
    _, box, ratio = matches[0]
    dx, dy = _offset(d)
    cx, cy = box.center()
    point = cap.screen(cx + dx, cy + dy)
    if not cap.inside(point):
        return fail("locator_failed(ocr)", "outside_window", ratio, words=boxes)
    return RungResult("ocr", ACCEPTED, ratio, attempt, point=point, rect=(*cap.screen(box.x, box.y), box.w, box.h),
                      evidence={"words": boxes}, crop=_crop(cap.image, (cx, cy)))


def _unscale(word: OcrWord, region: Rect) -> tuple[str, Rect]:
    """A word box of the 3x OCR input back in window pixels."""
    x0, y0 = region.x + word.rect.x / OCR_UPSCALE, region.y + word.rect.y / OCR_UPSCALE
    x1 = region.x + (word.rect.x + word.rect.w) / OCR_UPSCALE
    y1 = region.y + (word.rect.y + word.rect.h) / OCR_UPSCALE
    return word.text, Rect(math.floor(x0), math.floor(y0), math.ceil(x1) - math.floor(x0), math.ceil(y1) - math.floor(y0))


def _candidates(words: list[tuple[str, Rect]], n: int) -> list[tuple[str, Rect]]:
    """Single word boxes for a one-word text; runs of `n` neighbours on one line otherwise (choice 6)."""
    if n <= 1:
        return words
    lines: list[list[tuple[str, Rect]]] = []
    for w in sorted(words, key=lambda t: (t[1].center()[1], t[1].x)):
        for line in lines:
            ref = line[0][1]
            if abs(ref.center()[1] - w[1].center()[1]) <= max(ref.h, w[1].h) / 2:
                line.append(w)
                break
        else:
            lines.append([w])
    out = []
    for line in lines:
        line.sort(key=lambda t: t[1].x)
        for i in range(len(line) - n + 1):
            run = line[i:i + n]
            out.append((" ".join(t for t, _ in run), Rect.bounding([b for _, b in run])))
    return out


def _crop(image: Any, center: tuple[float, float]) -> Any:
    """The 40x20 crop centred on a window point, clipped to the image."""
    h, w = image.shape[:2]
    x0, y0 = int(round(center[0])) - CROP_W // 2, int(round(center[1])) - CROP_H // 2
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(w, x0 + CROP_W), min(h, y0 + CROP_H)
    if xb <= xa or yb <= ya:
        return None
    return np.ascontiguousarray(image[ya:yb, xa:xb])


def _png(crop: Any) -> bytes | None:
    if crop is None:
        return None
    ok, buf = cv2.imencode(".png", crop)
    return bytes(buf) if ok else None


# -- outcomes ---------------------------------------------------------------------------------------------------
def _passed(status: str, hit: RungResult, results: tuple[RungResult, ...], attempts: int,
            pre: str | None) -> LadderResult:
    return LadderResult(status, hit.rung, results, attempts, hit.point, hit.rect, pre)


def _evidence(target: Mapping[str, Any], results: tuple[RungResult, ...], pre: str | None, ctx: LadderContext,
              trace: Sequence[Mapping[str, Any]], point: tuple[int, int] | None, crop_sha: str | None) -> dict[str, Any]:
    """The `locator_failure` finding's evidence (5.5): step id, every rung's result and score, the pre-attempt
    screenshot hash, the agent trace, the final target and the crop."""
    ev: dict[str, Any] = {"step_id": ctx.step_id, "target": target.get("name"),
                          "rung_results": [dict(r.to_record(), attempt=r.attempt) for r in results],
                          "pre_screenshot": pre, "agent_trace": list(trace),
                          "final_target": {"x": point[0], "y": point[1]} if point is not None else None}
    if crop_sha is not None:
        ev.update(crop_sha256=crop_sha, artifacts=[crop_sha])
    return ev


def _failed(target: Mapping[str, Any], results: tuple[RungResult, ...], pre: str | None, ctx: LadderContext, *,
            trace: Sequence[Mapping[str, Any]] = ()) -> LadderResult:
    """An exhausted ladder: step failed, checkpoint landmark_missing, the rest of the flow not_run (5.5, 4.7)."""
    agent = results[-1]
    finding = FindingSpec("locator_failure", f"scripted locator failed at step {ctx.step_id}; {agent.result}"
                          + (f" ({agent.evidence['cause']})" if "cause" in agent.evidence else ""))
    consequence = ControlResult(
        "locator", False, "running", step="failed", checkpoint=Outcome("landmark_missing", f"locator:{ctx.step_id}"),
        outcomes=(SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{ctx.step_id}")),),
        findings=(finding,), actions=("end_flow",), detail=agent.result)
    return LadderResult("failed", "none", results, 2, None, None, pre, finding=finding,
                        evidence=_evidence(target, results, pre, ctx, trace, None, None), consequence=consequence,
                        health_line=ctx.cap.health_line())
