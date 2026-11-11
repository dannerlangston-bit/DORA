"""The Recorder's platform-independent core (5.16, 11.5 Recorder, 12.1 Recorder, 12.7 H2): the session state behind
the global hooks and the turn of a recorded event log into an anchored 4.7 flow.

The low-level hooks, the screenshots and the authoring UI are the Windows track (`win32`); they feed this module:

- `refuse_rig_host(hostname, pattern=, rig_hostnames=)`: the Recorder refuses to start on a host whose name matches the
  rig pattern (5.16); `RecorderRefused` says why.
- `RecordingSession(hostname, login_titles=, branches=)`: what the hooks observe, in order. `focus(title, role)` pauses
  automatically on the login dialog (title regex) or a password field (JAB role, DR-01 Branch A) and resumes when focus
  leaves it; `pause_hotkey()` toggles the owner's pause before any other secret. While paused the session writes no
  keystroke (`key` returns False) and takes no screenshot (`action` returns False); on resume it asserts that its
  keystroke log did not grow and records a breach if it did. `secret(vault_ref)` records a secret step, only while
  paused. `recording()` hands the result to `recorder.save.check_and_emit`.
- `recorded_events_to_flow(events, screenshots, header=, ocr=)` turns the event log into a `RecordedFlow`: the 4.7
  flow document, the new Surface Registry controls (4.5: a template of about 40x20 px around a stable label or icon,
  the offset to the click point, the OCR text anchor as the next rung, the JAB context under Branch A), the template
  PNGs, the steps whose template is not unique on its own screenshot, and the `order_stage` prompts.
- `suggest_anchors(image, point, words)`: the Recorder's anchor suggestions around one click, label first.

Choices where the spec is ambiguous or silent (reported):

1. Anchors live where 4.5 and 4.7 put them: a flow step's `target` is `<surface id>.<control id>`, and the image
   anchor (`template`, `click_offset`), the OCR text anchor (`ocr_text`) and the JAB context are the control's. A
   recorded click either names an existing control of its surface (the owner picked it in the authoring UI: no new
   anchor) or adds a new control. The window title and the search region 5.16 lists are kept on `RecordedStep` for
   the authoring UI and re-anchor mode: the 4.5 control has no field for them, and the ladder searches the window.
2. Suggestions (5.16 "chosen from the recorder's suggestions"): the 40x20 crop centred on the OCR label under the click
   point (or the nearest within 40 px), then the crop centred on the point, then the point shifted 12 px left, right
   and 6 px up and down. `recorded_events_to_flow` takes the first suggestion that is unique on its own screenshot by
   the ladder's rung-2 rule (best at least 0.92, second peak at least 0.05 below it, 5.5); a step with none is listed
   in `not_unique` (save rejected) with the best-scoring suggestion kept for the UI. A label phrase is the run of word
   boxes on one line with gaps under their height (OCR returns words).
3. The OCR text anchor is the label's text, only when the chosen template is centred on it (one `click_offset` serves
   both rungs, as `executor.locator.target_from_control` reads it) and no other box of the screenshot reads the same.
   The JAB context is kept only under DR-01 Branch A (L-SRF-09).
4. Control ids: the event's `control_id`, else the label's slug, else `<step id>-target`, made unique on the surface
   with a numeric suffix. Templates are stored beside the flow (5.16): `flows/tier-<tier>/templates/<flow id>/<display
   key>/<surface id>.<control id>.png`, one set per theme and display tier (11.5: anchors are per display tier).
5. A `type` or `secret` event without a point acts on the field the last click focused, and takes its target. A
   `secret` step is `{type: secret, target, vault_ref, commit}` (4.7); its keystrokes were never recorded.
6. The `order_stage` prompt: the last action step before the first event whose window title matches the order-dialog
   regex set (when the event before it did not) is the step that opened the dialog (5.4.6); the prompt names it.
   The owner's answer is applied by `recorder.save.check_and_emit`.
7. The flow's first step is `reset_input_state` and its teardown ends with one (4.7); its checkpoints come from the
   owner's checkpoint events, `checkpoint_id` `flow/<surface>/<flow id>/<step id>` (4.1). Flow health figures are not
   written in the flow (SD-55).
8. The rig pattern defaults to `tos-rig-<suffix>` (the example rig host names of 4.13, `tos-rig-01`), case-insensitive;
   the authorization's `rig_hostnames` (6.12) are refused too.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import cv2
import numpy as np

from ..capture.redaction import OcrWord, WordOcr
from ..diff.engine import match_template
from ..diff.regions import Rect
from ..executor.locator import CROP_H, CROP_W, IMAGE_FLOOR, MIN_PEAK_RADIUS, SECOND_PEAK_MARGIN, Branches

DEFAULT_RIG_PATTERN = r"(?i)^tos-rig-[a-z0-9][a-z0-9-]*$"          # choice 8
PASSWORD_ROLES = ("password text",)                                # JAB role of a password field (DR-01 A)
EVENT_KINDS = ("click", "rclick", "dblclick", "type", "press", "key_chord", "wheel", "secret", "checkpoint")
POINTER_KINDS = {"click": "click_named", "rclick": "rclick", "dblclick": "dblclick"}
LABEL_RADIUS = 40                                                  # choice 2
NEAR_SHIFTS = ((-12, 0), (12, 0), (0, -6), (0, 6))                 # choice 2
SEARCH_MARGIN = 200                                                # the 3.7 search margin, for the UI's search region
DEFAULT_DISPLAY = "dark-1920x1080-100"                             # Phase 1: one display configuration (2.12)
_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


class RecorderRefused(Exception):
    """The Recorder may not start or save here (5.16)."""


def refuse_rig_host(hostname: str, *, pattern: str = DEFAULT_RIG_PATTERN, rig_hostnames: Iterable[str] = ()) -> None:
    """Raise `RecorderRefused` on a host whose name matches the rig pattern or is a rig host name (5.16, choice 8)."""
    name = (hostname or "").strip()
    if not name:
        raise RecorderRefused("the Recorder needs the host name to check it is not a run rig (5.16)")
    if re.search(pattern, name) or name.lower() in {h.lower() for h in rig_hostnames}:
        raise RecorderRefused(f"the Recorder refuses to run on {name}: it matches the rig pattern (5.16); record on "
                              "an authoring rig")


# ---------------------------------------------------------------------------------------------------------------
# the event log


@dataclass(frozen=True)
class RecordedEvent:
    """One action the hooks observed (5.16), with what the owner named in the authoring UI.

    `screenshot` is the id of the screenshot taken for the action (before it), `point` the click point in that
    screenshot's pixels, `window_title` the focused window's title. `surface_id` names the Surface Registry surface,
    `control_id` an existing control or the id to give a new one. `checkpoint` carries a checkpoint's
    `target_window`, `data_mode` and `regions` (4.7)."""

    kind: str
    screenshot: str | None = None
    point: tuple[int, int] | None = None
    window_title: str = ""
    surface_id: str | None = None
    control_id: str | None = None
    jab: Mapping[str, str] | None = None
    text: str | None = None
    value_ref: str | None = None
    commit: str = "none"
    key: str | None = None
    keys: tuple[str, ...] = ()
    notches: int = 0
    vault_ref: str | None = None
    checkpoint: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"recorded event kind {self.kind!r}; one of {EVENT_KINDS}")
        if self.kind == "secret" and not self.vault_ref:
            raise ValueError("a secret step records its vault reference, never a value (4.7)")
        if self.kind == "secret" and (self.text or self.screenshot):
            raise ValueError("a secret step carries no typed text and no screenshot: the hook was paused (5.16)")
        if self.kind == "checkpoint" and not self.surface_id:
            raise ValueError("a checkpoint names its surface (4.7)")
        if self.commit not in ("enter", "tab", "none"):
            raise ValueError(f"commit {self.commit!r}; one of enter, tab, none")


@dataclass
class Recording:
    """One recording session's output: the host it ran on, the events, the screenshots by id, the keystroke log,
    the vault references its secret steps used, and the pause breaches. `wipe()` deletes it in place."""

    host: str
    events: list[RecordedEvent] = field(default_factory=list)
    screenshots: dict[str, Any] = field(default_factory=dict)
    keystrokes: list[str] = field(default_factory=list)
    secret_refs: list[str] = field(default_factory=list)
    breaches: list[str] = field(default_factory=list)

    def wipe(self) -> None:
        """Delete everything the recording holds (5.16: "deleted unsaved"); screenshot buffers are zeroed first."""
        for img in self.screenshots.values():
            if isinstance(img, np.ndarray) and img.flags.writeable:
                img.fill(0)
        self.events.clear()
        self.screenshots.clear()
        self.keystrokes.clear()
        self.secret_refs.clear()


class RecordingSession:
    """The session state the hooks drive (module docstring; 5.16 Key internals, secret handling)."""

    def __init__(self, hostname: str, *, login_titles: Iterable[str] = (), branches: Branches | None = None,
                 rig_pattern: str = DEFAULT_RIG_PATTERN, rig_hostnames: Iterable[str] = (),
                 password_roles: Sequence[str] = PASSWORD_ROLES):
        refuse_rig_host(hostname, pattern=rig_pattern, rig_hostnames=rig_hostnames)
        self.rec = Recording(hostname)
        self.login = tuple(re.compile(p) for p in login_titles)
        self.branches = branches or Branches()
        self.password_roles = tuple(r.lower() for r in password_roles)
        self.pause_reason: str | None = None
        self._log_at_pause = 0
        self._shots = 0

    @property
    def paused(self) -> bool:
        return self.pause_reason is not None

    def _pause(self, reason: str) -> None:
        if not self.paused:
            self.pause_reason, self._log_at_pause = reason, len(self.rec.keystrokes)

    def _resume(self) -> None:
        if not self.paused:
            return
        grown = len(self.rec.keystrokes) - self._log_at_pause
        if grown > 0:
            self.rec.breaches.append(f"keystroke log grew by {grown} while paused ({self.pause_reason})")
        self.pause_reason = None

    def focus(self, title: str, role: str | None = None) -> None:
        """The focused window and control changed: pause on the login dialog or a password field, resume when an
        automatic pause's reason is gone (a hotkey pause stays until the hotkey)."""
        login = any(p.search(title or "") for p in self.login)
        password = self.branches.jab == "A" and (role or "").lower() in self.password_roles
        if login or password:
            self._pause("login_dialog" if login else "password_field")
        elif self.pause_reason in ("login_dialog", "password_field"):
            self._resume()

    def pause_hotkey(self) -> None:
        """The owner's pause hotkey: pause before typing any other secret, or resume a hotkey pause."""
        if self.pause_reason == "hotkey":
            self._resume()
        else:
            self._pause("hotkey")

    def key(self, text: str) -> bool:
        """One keystroke from the hook: logged unless paused (False: nothing written)."""
        if self.paused:
            return False
        self.rec.keystrokes.append(text)
        return True

    def action(self, event: RecordedEvent, screenshot: Any | None = None) -> bool:
        """One action and its screenshot: recorded unless paused (False: no event, no screenshot)."""
        if self.paused:
            return False
        if screenshot is not None:
            self._shots += 1
            sid = f"shot-{self._shots:04d}"
            self.rec.screenshots[sid] = screenshot
            event = replace(event, screenshot=sid)
        self.rec.events.append(event)
        return True

    def secret(self, vault_ref: str, *, commit: str = "none", surface_id: str | None = None) -> None:
        """A secret step, typed with the hook paused (5.16, 4.7): only its vault reference is recorded."""
        if not self.paused:
            raise RecorderRefused("a secret is typed with the Recorder paused: press the pause hotkey first (5.16)")
        self.rec.events.append(RecordedEvent("secret", vault_ref=vault_ref, commit=commit, surface_id=surface_id))
        if vault_ref not in self.rec.secret_refs:
            self.rec.secret_refs.append(vault_ref)

    def recording(self) -> Recording:
        """The recording so far; a pause still open is closed first, so its breach check runs."""
        self._resume()
        return self.rec


# ---------------------------------------------------------------------------------------------------------------
# anchors (choices 2 and 3)


@dataclass(frozen=True)
class AnchorSuggestion:
    """One candidate image anchor around a click: its rect on the screenshot, the offset from the template centre to
    the click point, where it came from (`label`, `point`, `near`), its scores on its own screenshot and the label
    text it is centred on."""

    rect: tuple[int, int, int, int]
    offset: tuple[int, int]
    source: str
    best: float
    second: float
    label: str | None = None

    @property
    def unique(self) -> bool:
        """The ladder's rung-2 rule on the template's own screenshot (5.5)."""
        return self.best >= IMAGE_FLOOR and self.second <= self.best - SECOND_PEAK_MARGIN


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def _phrases(words: Sequence[OcrWord]) -> list[tuple[str, Rect]]:
    """Word boxes joined into one-line phrases (gaps under the box height), inner duplicates dropped (a button and
    its label read as the same text)."""
    boxes = sorted(((w.text, w.rect) for w in words if w.text and w.text.strip()), key=lambda t: (t[1].y, t[1].x))
    kept: list[tuple[str, Rect]] = []
    for text, r in boxes:
        same = [i for i, (t, k) in enumerate(kept) if _norm(t) == _norm(text) and (_inside(r, k) or _inside(k, r))]
        if same:
            i = same[0]
            if r.area < kept[i][1].area:
                kept[i] = (text, r)
            continue
        kept.append((text, r))
    lines: list[list[tuple[str, Rect]]] = []
    for text, r in sorted(kept, key=lambda t: (t[1].center()[1], t[1].x)):
        for line in lines:
            last = line[-1][1]
            tall = max(last.h, r.h)
            if abs(last.center()[1] - r.center()[1]) <= tall / 2 and 0 <= r.x - (last.x + last.w) < tall:
                line.append((text, r))
                break
        else:
            lines.append([(text, r)])
    return [(" ".join(t for t, _ in ln), Rect.bounding([b for _, b in ln])) for ln in lines]


def _inside(a: Rect, b: Rect) -> bool:
    return b.x <= a.x and b.y <= a.y and a.x + a.w <= b.x + b.w and a.y + a.h <= b.y + b.h


def _label_at(phrases: Sequence[tuple[str, Rect]], point: tuple[int, int]) -> tuple[str, Rect] | None:
    under = [p for p in phrases if _inside(Rect(point[0], point[1], 1, 1), p[1])]
    if under:
        return min(under, key=lambda p: p[1].area)
    near = [(abs(p[1].center()[0] - point[0]) + abs(p[1].center()[1] - point[1]), p) for p in phrases]
    near = [(d, p) for d, p in near if d <= LABEL_RADIUS]
    return min(near, key=lambda t: t[0])[1] if near else None


def _crop_rect(image: np.ndarray, center: tuple[float, float]) -> tuple[int, int, int, int] | None:
    H, W = image.shape[:2]
    x0, y0 = int(round(center[0])) - CROP_W // 2, int(round(center[1])) - CROP_H // 2
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x0 + CROP_W), min(H, y0 + CROP_H)
    return None if xb - xa < 16 or yb - ya < 16 else (xa, ya, xb - xa, yb - ya)


def _score(image: np.ndarray, rect: tuple[int, int, int, int]) -> tuple[float, float]:
    x, y, w, h = rect
    tpl = np.ascontiguousarray(image[y:y + h, x:x + w])
    best, second, _ = match_template(image, tpl, None, max(MIN_PEAK_RADIUS, min(w, h) // 2))
    return best, second


def suggest_anchors(image: np.ndarray, point: tuple[int, int], words: Sequence[OcrWord] = ()) -> list[AnchorSuggestion]:
    """The anchor suggestions around one click point, label first (choice 2), each scored on `image`."""
    phrases = _phrases(words)
    label = _label_at(phrases, point)
    centers: list[tuple[str, tuple[float, float], str | None]] = []
    if label is not None:
        centers.append(("label", label[1].center(), label[0]))
    centers.append(("point", (float(point[0]), float(point[1])), None))
    centers += [("near", (float(point[0] + dx), float(point[1] + dy)), None) for dx, dy in NEAR_SHIFTS]
    out: list[AnchorSuggestion] = []
    for source, c, text in centers:
        rect = _crop_rect(image, c)
        if rect is None or any(s.rect == rect for s in out):
            continue
        best, second = _score(image, rect)
        cx, cy = rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0
        out.append(AnchorSuggestion(rect, (int(round(point[0] - cx)), int(round(point[1] - cy))), source, best,
                                    second, text))
    return out


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError("template did not encode as PNG")
    return bytes(buf)


def slug(text: str) -> str:
    """A control id from a label (`Confirm and Send` -> `confirm-and-send`), empty when nothing is left."""
    s = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())
    return s.strip("-")


# ---------------------------------------------------------------------------------------------------------------
# the flow


@dataclass(frozen=True)
class FlowHeader:
    """The 4.7 header the owner fills in the authoring UI; `display` keys the anchor set (choice 4). Blocks left
    None take the defaults of `default_blocks`."""

    flow_id: str
    title: str
    tier: int
    owner: str
    persona: str
    surface: str
    workspace: str
    fixture: str
    flow_class: str = "standard"
    data_mode: str = "frozen"
    agent_fallback: str = "allowed"
    budget: Mapping[str, int] = field(default_factory=lambda: {"max_duration_s": 180, "max_steps": 40})
    requires: tuple[str, ...] = ()
    display: str = DEFAULT_DISPLAY
    preconditions: tuple[Mapping[str, Any], ...] | None = None
    normalize: tuple[Mapping[str, Any], ...] | None = None
    teardown: tuple[Mapping[str, Any], ...] | None = None

    def default_blocks(self) -> dict[str, list[dict[str, Any]]]:
        """Preconditions paper_mode, workspace_loaded and no_modal (L-FLW-12); normalize Escape, park, focus;
        teardown ending with reset_input_state (4.7)."""
        return {"preconditions": [{"id": "p1", "type": "precondition", "check": "paper_mode"},
                                  {"id": "p2", "type": "precondition", "check": "workspace_loaded",
                                   "expect": self.workspace},
                                  {"id": "p3", "type": "precondition", "check": "no_modal"}],
                "normalize": [{"id": "n1", "type": "press", "key": "escape"}, {"id": "n2", "type": "park_mouse"},
                              {"id": "n3", "type": "focus_neutral"}],
                "teardown": [{"id": "t1", "type": "reset_input_state"}]}

    def template_dir(self) -> str:
        return f"flows/tier-{self.tier}/templates/{self.flow_id}/{self.display}"


@dataclass(frozen=True)
class RecordedStep:
    """What the Recorder knows of one emitted step beyond the flow file (choice 1): its screenshot, its target and
    whether the Recorder added that control, the anchor chosen and the other suggestions, the window title and the
    search region for the authoring UI."""

    step_id: str
    kind: str
    screenshot: str | None = None
    target: str | None = None
    new_control: bool = False
    anchor: AnchorSuggestion | None = None
    suggestions: tuple[AnchorSuggestion, ...] = ()
    window_title: str = ""
    search_region: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class OrderStagePrompt:
    """The prompt to declare `order_stage: true` on the step that opened an order dialog (5.16, 5.4.6)."""

    step_id: str
    title: str

    def text(self) -> str:
        return (f"step {self.step_id} opened '{self.title}', which matches the order-dialog set: declare "
                "order_stage: true on it? (5.4.6)")


@dataclass
class RecordedFlow:
    """`recorded_events_to_flow`'s result (module docstring)."""

    doc: dict[str, Any]
    controls: dict[str, list[dict[str, Any]]]
    templates: dict[str, bytes]
    steps: tuple[RecordedStep, ...]
    prompts: tuple[OrderStagePrompt, ...] = ()
    not_unique: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        """Where the flow file goes in sentinel-config (4.1)."""
        return f"flows/tier-{self.doc['tier']}/{self.doc['flow_id']}.yaml"

    def answer(self, answers: Mapping[str, bool]) -> list[OrderStagePrompt]:
        """Apply the owner's answers to the order_stage prompts; returns the prompts still unanswered."""
        open_ = [p for p in self.prompts if p.step_id not in answers]
        for p in self.prompts:
            if answers.get(p.step_id):
                for st in self.doc["steps"]:
                    if st["id"] == p.step_id:
                        st["order_stage"] = True
        return open_


class _Builder:
    def __init__(self, header: FlowHeader, screenshots: Mapping[str, np.ndarray], ocr: WordOcr | None,
                 branches: Branches, existing: Mapping[str, Mapping[str, Any]]):
        self.h, self.shots, self.ocr, self.branches = header, screenshots, ocr, branches
        self.existing = existing
        self.taken: dict[str, set[str]] = {sid: {str(c.get("id")) for c in s.get("controls") or ()
                                                 if isinstance(c, Mapping)} for sid, s in existing.items()}
        self.controls: dict[str, list[dict[str, Any]]] = {}
        self.templates: dict[str, bytes] = {}
        self.not_unique: list[str] = []
        self.problems: list[str] = []
        self._words: dict[str, list[OcrWord]] = {}

    def words(self, shot: str) -> list[OcrWord]:
        if shot not in self._words:
            self._words[shot] = list(self.ocr.words(self.shots[shot])) if self.ocr is not None else []
        return self._words[shot]

    def control_id(self, surface: str, wanted: str) -> str:
        base = wanted if _ID.match(wanted) else "target"
        taken = self.taken.setdefault(surface, set())
        cid, n = base, 1
        while cid in taken:
            n += 1
            cid = f"{base}-{n}"
        taken.add(cid)
        return cid

    def target(self, sid: str, ev: RecordedEvent) -> RecordedStep:
        surface = ev.surface_id or self.h.surface
        known = {str(c.get("id")) for c in (self.existing.get(surface) or {}).get("controls") or ()}
        if ev.control_id and ev.control_id in known:
            return RecordedStep(sid, ev.kind, ev.screenshot, f"{surface}.{ev.control_id}", window_title=ev.window_title)
        if ev.screenshot is None or ev.point is None or ev.screenshot not in self.shots:
            self.problems.append(f"step {sid}: no screenshot and click point to cut an anchor from")
            return RecordedStep(sid, ev.kind, ev.screenshot, window_title=ev.window_title)
        image = self.shots[ev.screenshot]
        words = self.words(ev.screenshot)
        sugg = suggest_anchors(image, ev.point, words)
        chosen = next((s for s in sugg if s.unique), None)
        if chosen is None:
            self.not_unique.append(sid)
            chosen = max(sugg, key=lambda s: (s.best - s.second, s.source == "label")) if sugg else None
        if chosen is None:
            self.problems.append(f"step {sid}: the click point is too near the screenshot's edge for a template")
            return RecordedStep(sid, ev.kind, ev.screenshot, window_title=ev.window_title)
        cid = self.control_id(surface, ev.control_id or (slug(chosen.label) if chosen.label else "") or f"{sid}-target")
        x, y, w, hh = chosen.rect
        path = f"{self.h.template_dir()}/{surface}.{cid}.png"
        self.templates[path] = _png(image[y:y + hh, x:x + w])
        control: dict[str, Any] = {"id": cid}
        if ev.jab and self.branches.jab == "A":
            control["jab"] = {"name": str(ev.jab["name"]), "role": str(ev.jab["role"])}
        control["template"] = path
        if chosen.source == "label" and chosen.label and self._unique_text(words, chosen.label):
            control["ocr_text"] = chosen.label
        control["click_offset"] = {"dx": chosen.offset[0], "dy": chosen.offset[1]}
        self.controls.setdefault(surface, []).append(control)
        H, W = image.shape[:2]
        region = Rect(x - SEARCH_MARGIN, y - SEARCH_MARGIN, w + 2 * SEARCH_MARGIN, hh + 2 * SEARCH_MARGIN).intersect(
            Rect(0, 0, W, H))
        return RecordedStep(sid, ev.kind, ev.screenshot, f"{surface}.{cid}", True, chosen, tuple(sugg),
                            ev.window_title, region.as_tuple() if region else None)

    @staticmethod
    def _unique_text(words: Sequence[OcrWord], text: str) -> bool:
        want = _norm(text)
        return sum(1 for t, _ in _phrases(words) if _norm(t) == want) == 1


def recorded_events_to_flow(events: Sequence[RecordedEvent], screenshots: Mapping[str, np.ndarray], *,
                            header: FlowHeader, ocr: WordOcr | None = None, branches: Branches | None = None,
                            existing: Mapping[str, Mapping[str, Any]] | None = None,
                            order_dialog_regexes: Iterable[str] = ()) -> RecordedFlow:
    """The recorded event log as an anchored 4.7 flow (module docstring). `existing` maps a surface id to its registry
    entry (4.5), for the controls a step may name and the ids already taken; `ocr` reads the screenshots' words for
    the label suggestions and the OCR text anchor."""
    b = _Builder(header, screenshots, ocr, branches or Branches(), existing or {})
    order = [re.compile(p) for p in order_dialog_regexes]
    steps: list[dict[str, Any]] = [{"id": "s01", "type": "reset_input_state"}]
    meta: list[RecordedStep] = []
    prompts: list[OrderStagePrompt] = []
    surfaces = [header.surface]
    last_target: str | None = None
    last_action: str | None = None
    in_dialog = False
    for ev in events:
        sid = f"s{len(steps) + 1:02d}"
        hits = any(p.search(ev.window_title or "") for p in order)
        if hits and not in_dialog and last_action is not None:
            prompts.append(OrderStagePrompt(last_action, ev.window_title))
        in_dialog = hits
        if ev.kind == "checkpoint":
            cp = dict(ev.checkpoint or {})
            surface = str(ev.surface_id)
            steps.append({"id": sid, "type": "checkpoint", "checkpoint_id": f"flow/{surface}/{header.flow_id}/{sid}",
                          "surface_id": surface, "target_window": dict(cp.get("target_window") or {"kind": "main"}),
                          "data_mode": cp.get("data_mode", header.data_mode), "regions": cp.get("regions", "default")})
            if surface not in surfaces:
                surfaces.append(surface)
            meta.append(RecordedStep(sid, "checkpoint", ev.screenshot, window_title=ev.window_title))
            continue
        step: dict[str, Any] = {"id": sid}
        if ev.kind in POINTER_KINDS or (ev.kind in ("type", "wheel") and ev.point is not None):
            rs = b.target(sid, ev)
            if ev.kind in POINTER_KINDS:
                last_target = rs.target
        elif ev.kind in ("type", "secret"):
            rs = RecordedStep(sid, ev.kind, ev.screenshot, last_target, window_title=ev.window_title)
            if last_target is None:
                b.problems.append(f"step {sid}: {ev.kind} with no focused field (no click before it)")
        else:
            rs = RecordedStep(sid, ev.kind, ev.screenshot, window_title=ev.window_title)
        if ev.kind in POINTER_KINDS:
            step.update(type=POINTER_KINDS[ev.kind], target=rs.target)
        elif ev.kind == "type":
            step.update(type="type_fixture", target=rs.target)
            step.update({"value_ref": ev.value_ref} if ev.value_ref else {"value": ev.text or ""})
            step["commit"] = ev.commit
        elif ev.kind == "secret":
            step.update(type="secret", target=rs.target, vault_ref=ev.vault_ref, commit=ev.commit)
        elif ev.kind == "press":
            step.update(type="press", key=ev.key)
        elif ev.kind == "key_chord":
            step.update(type="key_chord", keys=list(ev.keys))
        else:
            at = rs.target or ({"x": ev.point[0], "y": ev.point[1]} if ev.point else None)
            if at is None:
                b.problems.append(f"step {sid}: wheel with no point")
            step.update(type="wheel", at=at, notches=ev.notches)
        steps.append(step)
        meta.append(rs)
        last_action = sid
    blocks = header.default_blocks()
    doc: dict[str, Any] = {
        "schema_version": 2, "flow_id": header.flow_id, "title": header.title, "class": header.flow_class,
        "tier": header.tier, "owner": header.owner, "persona": header.persona, "surface": header.surface,
        "surfaces": surfaces, "requires": list(header.requires), "workspace": header.workspace,
        "fixture": header.fixture, "data_mode": header.data_mode, "agent_fallback": header.agent_fallback,
        "budget": dict(header.budget), "origin": {"kind": "recorder"},
        "normalize": [dict(s) for s in header.normalize] if header.normalize is not None else blocks["normalize"],
        "preconditions": ([dict(s) for s in header.preconditions] if header.preconditions is not None
                          else blocks["preconditions"]),
        "steps": steps,
        "teardown": [dict(s) for s in header.teardown] if header.teardown is not None else blocks["teardown"]}
    return RecordedFlow(doc, b.controls, b.templates, tuple(meta), tuple(prompts), tuple(b.not_unique),
                        tuple(b.problems))


__all__ = ["AnchorSuggestion", "DEFAULT_RIG_PATTERN", "FlowHeader", "OrderStagePrompt", "RecordedEvent",
           "RecordedFlow", "RecordedStep", "Recording", "RecordingSession", "RecorderRefused",
           "recorded_events_to_flow", "refuse_rig_host", "slug", "suggest_anchors"]
