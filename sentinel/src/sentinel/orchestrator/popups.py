"""The popup sentinel: catalog matching, the fixed action effects, unknown windows, hit counts and the per-build rate
comparison (4.10, 7.8, 12.8, 5.9, 2.7 INV-14).

Platform independent. The desktop port enumerates the top-level windows of the process tree
(`ports.DesktopPort.enumerate_windows`) and reports the foreground window; sending WM_CLOSE, the dismiss key (through
the gateway, `Rig.send_input`), the agent's dismiss-only session and the screenshot are the driver's. Everything decided
over those observations is here, and every decision is a `ControlResult` in the closed taxonomy.

- `load_catalog(path)` / `parse_catalog(data)`: `popups/catalog.yaml` in the wrapper form `{schema_version, entries}`
  and the bare-list form (SD-47; `PopupCatalog.bare_list` says which, the lint owns the Phase 0 warning and the Phase 1
  error of L-POP-01). Entries are checked enough to act on safely: the class and action vocabularies, a compiling
  `title_regex`, `on_abort` where the action needs it, a dismiss key that is one named key and never Enter or Space
  (L-POP-02), and no input action on a `safety`, `order_event` or `app_failure` entry (L-POP-03, 7.8 policy 3-4).
- `unexpected_windows(windows, ...)`: what 7.8 policy 1 calls a popup: every visible enumerated window except the main
  frame, the checkpoint's declared target window and the declared detached windows, and except the transient windows
  of policy 2 (owned, untitled SunAwtWindow instances).
- `PopupCatalog.match(window, phase, template_ok=...)`: the catalog entry for one window, or None.
- `decide(entry, ctx, window)`: what the entry's action and class produce (4.10 "What each action produces is fixed",
  7.8 policy 3, the 12.8 outcome column); `wait_expired` is the `on_abort` of a `wait` that timed out.
- `unknown_window(window, ctx, catalog)`: an unmatched window (4.10, 7.8 policy 5); `persisting(window, ctx, entry)`:
  a window still present after its dismissal or close (7.8 policy 5).
- `foreground_check(foreground, tree_pids, ctx)`: a foreign foreground window before an input batch (7.8 policy 1).
- `PopupSentinel`: one shard's sentinel over the above: `sweep` before every step and capture, `wait_over` when a
  `wait` poll ends, `foreground` before an input batch; it keeps the run's `PopupCounter` and the shard's streaks.
- `popup_event(hit, ...)`: the 8.3 `step_events` row (action `popup`) of one counted hit; `PopupCounter.from_events`
  rebuilds a run's counts from those rows and `BuildCounts` aggregates runs of one build; `popup_rate_findings(history)`
  gives the `popup_rate_increase` findings (7.8 policy 3, 4.13).
- `error_title_patterns(catalog)`: the INV-14 error title regexes (the `title_regex` of the matchable `app_failure`
  entries, 5.9, 2.7) for `invariants.CheckerConfig.error_title_patterns`; `order_dialog_regexes`,
  `DETACHED_TICKET_REGEXES` and `order_event_regexes` feed `windows.TitleSets.compile` (7.8 policy 5, 5.4.6, 6.6).
- `dismiss_action(entry)`: the gateway action of a `dismiss_key` entry; `TemplateMatcher`: the small-template half of
  the match on a grab.

`ControlResult.evidence` of every popup decision carries: `popup_id` (None for an unmatched window), `title`, `hwnd`,
`popup_action` (the catalog action, or `unknown`), `input` (None, `{"kind": "key", "key": ...}` or `{"kind": "click",
"target": ...}`) and `close` (None, `wm_close` or `agent_dismiss`, with the agent's `dismiss_labels` and
`refused_labels`, 7.8 policy 5). Some carry
`wait_timeout_ms` (a `wait`, or the 30 s of an unowned Order Confirmation), `capture_first` (INV-14: capture the
checkpoint with the dialog on screen before WM_CLOSE), `handoff: login_recovery` (POP-21 inside the LOGIN flow),
`proactive_relaunch` (POP-11), `ondemand_timeouts` (POP-12) or `proposed_entry`.

Choices where the spec is ambiguous or silent (reported):

1. Phases restrict input (4.10 `expected_phases`, 7.8 policy 3). An entry whose action emits input (`dismiss_key`,
   `click`) matches only in its expected phases; outside them the window takes the unmatched path, so no catalog key or
   click is ever sent where the entry does not expect its window. Every other entry matches in any phase, because its
   effect emits no input and 7.8 and 12.8 state its outcome outside its phases too (POP-21 "anywhere else", POP-01
   "after a relaunch", app error dialogs "never on a dismiss list" in any phase). Disabled and retired entries never
   match.
2. Precedence: when several entries match one title (L-POP-05 warns), `safety`, `order_event` and `app_failure`
   entries win over the others, then file order, so an ambiguous title is never dismissed when it may be an order or
   error dialog. With a `template_ok` check an entry that carries a template matches only when its template does too;
   without one (no grab, or Phase 0 drafts) the title decides.
3. One hit per appearance (7.8 policy 3 "every hit is counted"): `PopupSentinel` counts a window the first sweep that
   sees it (by hwnd and the entry it matched; an unmatched window that retitles itself stays one appearance) and
   forgets it once a sweep no longer sees it; an `ignore` entry (product state) is never counted. Unmatched windows are
   counted under their raw title, which is the 8.3 `popup_title` of an unknown popup; matched windows under their
   `popup_id`. A window seen again keeps a POP-21 hand-off while the LOGIN flow's recovery runs, so the recovery is
   never charged twice.
4. Abort scope by class. `abort` and an expired `wait` are flow-level as 4.10 states (step `failed`, the checkpoint in
   progress takes `on_abort`, `rest_of_flow` is `not_run (reason: step_failed:<step>)`, `end_flow`), except: an
   `env_error` of the `env` and `login` classes ends the shard (7.8 policy 3 "abort the shard", 7.7 POP-04 "every
   remaining checkpoint of the shard"): run `complete_with_env_errors`, the checkpoint in progress and every unreached
   one `env_error` with the gate and reason, `teardown`; `occluded` is checkpoint-level and the input batch is not
   emitted (7.8 policy 1, 5.7); `ondemand_timeout` stays flow-level, so three consecutive timeouts in one shard can
   happen and abandon OnDemand for the run: the `frozen` checkpoints (the OnDemand checkpoints under Branch B) become
   `env_error (gate: fixture_verified, reason: ondemand_timeout)` (7.9). `aborted_run` ends the run with `run_outcome`
   (`aborted_safety` through the kill switch sequence with an S2 incident, as the gateway's unknown dialog does;
   `aborted_harness` with `on_abort.reason`). An `env_error` without `on_abort.gate` takes the gate in force.
5. Starter rows whose 12.8 outcome goes beyond their class and action are keyed by data where the data says it:
   `version_mismatch` becomes `version_changed_mid_run` after a relaunch (POP-01), and a `version_mismatch` left as
   such ends the shard whatever the entry's class and asks for `disable_rig`, the scheduler's hold (7.3 "nothing
   else runs on that rig until a human looks", 7.7 step 1, as `gates.check_launched` does for the update progress);
   a change within the run holds nothing; `occluded` is the foreign-window path
   (POP-19); `ondemand_timeout` is the OnDemand stall (POP-12: before a capture it is `not_settled (reason:
   anti_anchor_present)`, at setup it waits); a `relogin` entry whose `on_abort.reason` is `authentication_challenge`
   is the challenge prompt (POP-21: the LOGIN flow's Branch B recovery once, inside phase `login`; anywhere else or a
   second time its `on_abort`). Rows whose extra effect the data cannot say are keyed by their fixed starter id
   (L-XREF-12 keeps them): POP-11 adds a `memory_warning` finding with the Watchdog's heap and GDI counts and asks for
   the proactive relaunch at the next flow boundary (`proactive_relaunch`); POP-07 and POP-15 add a `fixture_finding`
   finding (4.13) whose `detail` is its reason, `fixture_drift` (7.6) and `fixture_maintenance_needed` (7.9), unless
   the flow expects the popup.
   Every other `relogin` entry (POP-05) draws on the run's re-login budget K (SD-66, `budgets.ReloginBudget`).
6. App error dialogs (`app_failure`, 7.8 policy 4, 5.9): never dismissed; the checkpoint in progress is `invariant_
   violation` with `on_abort.invariant_id` (INV-14) and the decision asks the driver to capture it with the dialog on
   screen first (`capture_first`, so the Invariant Checker's crop and the Watchdog's log slice attach to the finding),
   then close it with WM_CLOSE as every `abort` does (4.10); WM_CLOSE is a window message, not input. A process that is
   gone or an EDT that is frozen is never decided here: it is the Watchdog's `app_alive` path (10.2).
7. `safety` (POP-14) and `order_event` (POP-22) windows are never closed and never get input: an unowned Order
   Confirmation waits with no input until the kill switch's 30 s rule (6.6; `wait_expired` gives the same decision);
   an order event is the kill switch at once. An unmatched window whose title matches either set, or the detached
   Order Entry window, is never handed to the agent and never closed (7.8 policy 5). 5.4.6 source 1 and 6.2 put the
   detached Order Entry window in the order-dialog set, but the 12.8 starter catalog has no entry for it, so its
   title regex is `DETACHED_TICKET_REGEXES` (`^Order Entry\b`, a draft until Phase 0 reads the title, 5.19), the
   Window Watcher's detached-ticket set (`windows.TitleSets.detached_ticket`); `unknown_window` and `PopupSentinel`
   take it as `detached_ticket`, so the two sets agree.
8. The proposed catalog entry of an `unknown_popup` (4.10, 7.8 policy 5) is drafted from the window: the next free id
   (from POP-23, 12.8), a slug name, an anchored escaped `title_regex`, `seed_titles`, `window_class_hint`, the phase,
   `status: unverified`, the build, and the registry owner from the context (`sentinel-ops` by default, the example
   catalog's owner). A window over a staged order or with an order-dialog title is drafted as `safety` with the L-POP-03
   kill-switch `on_abort`; any other as `dismissable` with `dismiss_key: escape`. It is the finding's `detail` as
   canonical JSON.
9. The rate (4.10 "a per-build rise in hits"): hits per run of the newest build against the previous build, for catalog
   ids only (4.13: "catalog hits per build"); any rise after a build change is a finding, none when both entries name
   one build.
10. A window that persists (7.8 policy 5): at a census checkpoint `not_settled (reason: unexpected_window:<title>)`;
    inside a flow the step is `failed`, the checkpoint the step led to and the rest of the flow are `not_run (reason:
    step_failed:<step>)`, `reset_input_state` runs and the flow ends; the `unknown_popup` finding was already raised, so
    none is raised again. A catalog window still present after its dismissal or WM_CLOSE takes the same path (the spec
    is silent).
11. No foreground window at all (GetForegroundWindow NULL) before an input batch counts as foreign: the batch is not
    emitted (fail closed). The on-call alert of an occlusion is sent once per run per foreign image name (5.7).
12. Template floor 0.90, the floor of the 5.4.6 detector; the spec gives popup templates none.
13. The order-dialog and order-event title sets include disabled entries (not retired ones): they only raise the
    ticket flag or keep a window from being closed, so a draft or disabled regex can only make the guard stricter.
14. A `dismiss_key` is one named key of `gateway.NAMED_VK`; a chord is refused at load, so no catalog entry can send
    Alt+F4 or Ctrl+Enter to the app.
15. The 12.8 "fixture finding" is the 4.13 finding kind `fixture_finding`, its reason in `detail`; the unknown popup's
    flow quarantine is the `quarantine_flow` action, persisted with the 10.5 reason `unknown_popup`
    (`store.safety.quarantine_reason_for`).
16. A dismiss or WM_CLOSE found at the slow half's modal check (5.4.3 stage 2) goes through the same sweep; failing that
    step is the emitter's rule. A rising `focus_correction` count is a reason to sweep (5.4.3 stage 1); the driver runs
    `sweep` after a correction.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from ..canonical import canonical_json
from ..records import StepEvent
from ..taxonomy import load
from . import alerts
from .budgets import ReloginBudget
from .gates import GATES
from .gateway import NAMED_VK, VK_RETURN, VK_SPACE, Action
from .killswitch import DIALOG_OPEN_MAX_S
from .results import (ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, harness_abort_outcome,
                      safety_abort_outcome)

CONTROL = "popup"
CLASSES = ("dismissable", "env", "login", "app_failure", "safety", "order_event")     # 4.10, 7.8 policy 3
PHASES = ("launch", "login", "workspace_load", "running", "teardown")                 # 4.10 expected_phases
STATUSES = ("verified", "unverified", "disabled")
ON_ABORT_OUTCOMES = ("env_error", "invariant_violation", "aborted_run")
RUN_OUTCOMES = ("aborted_safety", "aborted_harness")
POP_ID = re.compile(r"^POP-[0-9]{2,}$")
FIRST_FREE_ID = 23                               # 12.8: further ids from POP-23 on
INPUT_ACTIONS = frozenset({"dismiss_key", "click"})
NO_INPUT_CLASSES = frozenset({"safety", "order_event", "app_failure"})               # 7.8 policy 3-4, 12.8 "no input"
SAFETY_FIRST = ("safety", "order_event", "app_failure")                              # choice 2
NEVER_CLOSED = frozenset({"safety", "order_event"})                                  # choice 7
SHARD_CLASSES = frozenset({"env", "login"})                                          # choice 4
TRANSIENT_CLASS = "SunAwtWindow"                                                     # 7.8 policy 2
ONDEMAND_ABANDON = 3                             # 7.9: three consecutive buffering timeouts abandon OnDemand
MEMORY_WARNING_ID = "POP-11"                     # 12.8: a per-build memory_warning finding
FIXTURE_FINDINGS = {"POP-07": "fixture_drift", "POP-15": "fixture_maintenance_needed"}   # 7.6, 7.9, 12.8
REFUSED_KEYS = frozenset({VK_RETURN, VK_SPACE})  # L-POP-02
AGENT_DISMISS_LABELS = ("OK", "Close", "Later", "Not now", "Dismiss", "Cancel")       # 7.8 policy 5
AGENT_NEVER_LABELS = ("Accept", "Agree", "Confirm", "Send", "Yes", "Save")
POPUP_TEMPLATE_FLOOR = 0.90                      # choice 12
DRAFT_OWNER = "sentinel-ops"                     # choice 8
DETACHED_TICKET_REGEXES = (r"^Order Entry\b",)   # the detached Order Entry window (5.4.6 source 1, 6.2; choice 7)


class CatalogError(ValueError):
    """`popups/catalog.yaml` cannot be acted on safely."""


# -- the catalog (4.10) -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OnAbort:
    """An entry's `on_abort` (4.10): what an `abort`, an expired `wait` or a refused `relogin` gives."""

    outcome: str
    reason: str | None = None
    gate: str | None = None
    invariant_id: str | None = None
    run_outcome: str | None = None


@dataclass(frozen=True)
class PopupEntry:
    """One catalog entry (4.10), with its `title_regex` compiled."""

    popup_id: str
    name: str
    cls: str
    title_regex: str
    action: str
    severity: str
    expected_phases: tuple[str, ...]
    status: str
    key: str | None = None
    target: str | None = None
    template: str | None = None
    wait_timeout_ms: int | None = None
    on_abort: OnAbort | None = None
    retired: bool = False
    owner: str | None = None
    first_seen_build: str | None = None
    window_class_hint: str | None = None
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "pattern", re.compile(self.title_regex))
        except re.error as exc:
            raise CatalogError(f"{self.popup_id}: title_regex does not compile: {exc}") from exc

    @property
    def matchable(self) -> bool:
        """Disabled and retired entries are kept but never matched (4.10)."""
        return self.status != "disabled" and not self.retired

    @property
    def emits_input(self) -> bool:
        return self.action in INPUT_ACTIONS

    @property
    def abort_reason(self) -> str | None:
        return self.on_abort.reason if self.on_abort else None


@dataclass(frozen=True)
class PopupCatalog:
    """The parsed catalog: its entries in file order and its `schema_version` (None: the bare-list form, SD-47)."""

    entries: tuple[PopupEntry, ...]
    schema_version: int | None = None

    @property
    def bare_list(self) -> bool:
        return self.schema_version is None

    def match(self, window: Any, phase: str, *,
              template_ok: Callable[[PopupEntry, Any], bool] | None = None) -> PopupEntry | None:
        """The entry for `window` in `phase` (choices 1 and 2), or None. `template_ok(entry, window)` is the template
        half of the match (`TemplateMatcher`); without it the title decides."""
        if phase not in PHASES:
            raise ValueError(f"phase {phase!r}; one of {PHASES}")
        title = window.title or ""
        ranked = sorted(self.entries, key=lambda e: SAFETY_FIRST.index(e.cls) if e.cls in SAFETY_FIRST else 3)
        for e in ranked:
            if not e.matchable or (e.emits_input and phase not in e.expected_phases):
                continue
            if not e.pattern.search(title):
                continue
            if e.template and template_ok is not None and not template_ok(e, window):
                continue
            return e
        return None

    def next_popup_id(self, taken: Iterable[str] = ()) -> str:
        """The next free popup id for a proposed entry: past every id in the catalog and in `taken`, from POP-23."""
        nums = [int(i[4:]) for i in (*(e.popup_id for e in self.entries), *taken) if POP_ID.match(i)]
        return f"POP-{max([FIRST_FREE_ID - 1, *nums]) + 1:02d}"


def _need(ok: bool, msg: str) -> None:
    if not ok:
        raise CatalogError(msg)


def _on_abort(pid: str, raw: Any) -> OnAbort | None:
    if raw is None:
        return None
    _need(isinstance(raw, Mapping), f"{pid}: on_abort is a mapping")
    oa = OnAbort(raw.get("outcome"), raw.get("reason"), raw.get("gate"), raw.get("invariant_id"),
                 raw.get("run_outcome"))
    _need(oa.outcome in ON_ABORT_OUTCOMES, f"{pid}: on_abort.outcome {oa.outcome!r}; one of {ON_ABORT_OUTCOMES}")
    t = load()
    if oa.outcome == "aborted_run":
        _need(oa.run_outcome in RUN_OUTCOMES, f"{pid}: aborted_run needs run_outcome, one of {RUN_OUTCOMES}")
        _need(oa.run_outcome == "aborted_safety" or bool(oa.reason), f"{pid}: aborted_harness needs a reason")
        _need(oa.reason is None or t.reason_allowed(oa.run_outcome, oa.reason),
              f"{pid}: {oa.reason!r} is not a reason of {oa.run_outcome}")
    elif oa.outcome == "invariant_violation":
        _need(t.reason_allowed("invariant_violation", oa.invariant_id), f"{pid}: invariant_id {oa.invariant_id!r}")
    else:
        _need(oa.gate is None or oa.gate in t.gates, f"{pid}: unknown gate {oa.gate!r}")
        _need(t.reason_allowed("env_error", oa.reason or "gate"), f"{pid}: {oa.reason!r} is not an env_error reason")
    return oa


def _entry(raw: Any, at: int) -> PopupEntry:
    _need(isinstance(raw, Mapping), f"entry [{at}] is a mapping")
    pid = raw.get("popup_id")
    _need(isinstance(pid, str) and bool(POP_ID.match(pid)), f"entry [{at}]: popup_id {pid!r} is not POP-nn")
    cls, action = raw.get("class"), raw.get("action")
    _need(cls in CLASSES, f"{pid}: class {cls!r}; one of {CLASSES}")
    _need(action in load().popup_actions, f"{pid}: action {action!r}; one of {load().popup_actions}")
    _need(isinstance(raw.get("title_regex"), str) and bool(raw["title_regex"]), f"{pid}: title_regex is required")
    status = raw.get("status", "unverified")
    _need(status in STATUSES, f"{pid}: status {status!r}; one of {STATUSES}")
    phases = tuple(raw.get("expected_phases") or ())
    _need(set(phases) <= set(PHASES), f"{pid}: expected_phases {list(phases)}; each one of {PHASES}")
    oa = _on_abort(pid, raw.get("on_abort"))
    _need(not (cls in NO_INPUT_CLASSES and action in INPUT_ACTIONS),
          f"{pid}: a {cls} entry never emits input (7.8 policy 3-4, L-POP-03)")
    _need(action not in ("abort", "relogin", "wait") or oa is not None, f"{pid}: action {action} requires on_abort")
    key = raw.get("key")
    if action == "dismiss_key":
        vk = NAMED_VK.get(str(key).strip().lower()) if isinstance(key, str) else None
        _need(vk is not None, f"{pid}: dismiss key {key!r} is not one named key (choice 14)")
        _need(vk not in REFUSED_KEYS, f"{pid}: dismiss key {key!r} is Enter or Space (L-POP-02)")
    _need(action != "click" or bool(raw.get("target")), f"{pid}: action click requires target")
    wait_ms = raw.get("wait_timeout_ms")
    _need(action != "wait" or (isinstance(wait_ms, int) and not isinstance(wait_ms, bool) and wait_ms > 0),
          f"{pid}: action wait requires a positive wait_timeout_ms")
    return PopupEntry(popup_id=pid, name=str(raw.get("name") or pid), cls=cls, title_regex=raw["title_regex"],
                      action=action, severity=str(raw.get("severity") or "info"), expected_phases=phases,
                      status=status, key=key, target=raw.get("target"), template=raw.get("template"),
                      wait_timeout_ms=wait_ms, on_abort=oa, retired=bool(raw.get("retired", False)),
                      owner=raw.get("owner"), first_seen_build=raw.get("first_seen_build"),
                      window_class_hint=raw.get("window_class_hint"))


def parse_catalog(data: Any) -> PopupCatalog:
    """A catalog from its parsed YAML: `{schema_version, entries}` or a bare list (SD-47)."""
    if isinstance(data, list):
        version, items = None, data
    else:
        _need(isinstance(data, Mapping) and isinstance(data.get("entries"), list),
              "the catalog is {schema_version, entries: [...]} or a bare list (SD-47)")
        version, items = data.get("schema_version"), data["entries"]
        _need(isinstance(version, int) and not isinstance(version, bool), "schema_version is an int")
    entries = tuple(_entry(raw, i) for i, raw in enumerate(items))
    ids = [e.popup_id for e in entries]
    _need(len(set(ids)) == len(ids), f"popup ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}")
    return PopupCatalog(entries, version)


def load_catalog(path: str | Path) -> PopupCatalog:
    """Read and parse `popups/catalog.yaml` (4.10, SD-47)."""
    return parse_catalog(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def error_title_patterns(catalog: PopupCatalog) -> tuple[str, ...]:
    """The INV-14 error title regexes: `title_regex` of the matchable `app_failure` entries (5.9, 2.7, 4.10)."""
    return tuple(e.title_regex for e in catalog.entries if e.cls == "app_failure" and e.matchable)


def order_dialog_regexes(catalog: PopupCatalog) -> tuple[str, ...]:
    """The order-dialog regex set (7.8 policy 5, 5.4.6 source 1): the `safety` entries that are not retired (choice
    13); `windows.TitleSets.compile(order_dialog=...)`."""
    return tuple(e.title_regex for e in catalog.entries if e.cls == "safety" and not e.retired)


def order_event_regexes(catalog: PopupCatalog) -> tuple[str, ...]:
    """The order-event windows (6.6 `order_event_window`): the `order_event` entries that are not retired (choice
    13); `windows.TitleSets.compile(order_event=...)`."""
    return tuple(e.title_regex for e in catalog.entries if e.cls == "order_event" and not e.retired)


def dismiss_action(entry: PopupEntry) -> Action:
    """The gateway action that sends a `dismiss_key` entry's key (`Rig.send_input`, 5.4.3)."""
    if entry.action != "dismiss_key" or entry.key is None:
        raise ValueError(f"{entry.popup_id} is not a dismiss_key entry")
    return Action("press", keys=(NAMED_VK[entry.key.strip().lower()],))


class TemplateMatcher:
    """The small-template half of the match (7.8 policy 3): the entry's template searched inside the window's bounds
    on one grab (`ports.Frame`) at `floor` (choice 12). `root` is the config repository root the template paths are
    relative to; templates are read once."""

    def __init__(self, root: str | Path, frame: Any, *, floor: float = POPUP_TEMPLATE_FLOOR):
        self.root = Path(root)
        self.frame = frame
        self.floor = floor
        self._templates: dict[str, np.ndarray] = {}

    def _template(self, rel: str) -> np.ndarray:
        if rel not in self._templates:
            img = cv2.imread(str(self.root / rel), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise FileNotFoundError(f"popup template {rel} is missing or not an image")
            self._templates[rel] = img
        return self._templates[rel]

    def __call__(self, entry: PopupEntry, window: Any) -> bool:
        tpl = self._template(entry.template or "")
        img = np.asarray(self.frame.image)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        rect = getattr(window, "rect", None)
        if rect is not None:
            ox, oy = self.frame.origin
            x, y, w, h = rect
            gray = gray[max(0, y - oy):max(0, y - oy + h), max(0, x - ox):max(0, x - ox + w)]
        if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
            return False
        res = np.nan_to_num(cv2.matchTemplate(np.ascontiguousarray(gray), tpl, cv2.TM_CCOEFF_NORMED), nan=0.0)
        return float(res.max()) >= self.floor


# -- which windows are popups (7.8 policy 1-2) --------------------------------------------------------------------

def unexpected_windows(windows: Iterable[Any], *, expected_hwnds: Collection[int] = (),
                       expected_titles: Iterable[str] = ()) -> tuple[Any, ...]:
    """The visible windows that are popups: not the main frame, the declared target or a declared detached window
    (by hwnd or by title regex), and not a transient window (owned, untitled SunAwtWindow, 7.8 policy 2)."""
    pats = [re.compile(p) for p in expected_titles]
    out = []
    for w in windows:
        if not w.visible or w.hwnd in expected_hwnds or any(p.search(w.title or "") for p in pats):
            continue
        if w.cls == TRANSIENT_CLASS and not w.title and getattr(w, "owner_hwnd", None) is not None:
            continue
        out.append(w)
    return tuple(out)


# -- decisions (4.10, 7.8, 12.8) ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class PopupContext:
    """Where the sentinel runs: the phase and the gate in force, the flow, step and checkpoint kind, and what the
    run knows that changes an outcome (choices 4 and 5)."""

    phase: str = "running"
    gate: str = "running"
    flow_id: str | None = None
    step_id: str | None = None
    order_stage: bool = False                  # the current step's `order_stage` (4.7, 6.2)
    at_capture: bool = False                   # the sweep before a capture (False: before a step)
    census: bool = False                       # a census checkpoint (7.8 policy 5)
    agent_enabled: bool = True                 # agent mode on (DR-07 Branch A and preflight N2 passed)
    relaunched: bool = False                   # the app was relaunched in this shard (POP-01)
    ondemand_timeouts: int = 0                 # consecutive OnDemand buffering timeouts so far in the shard (7.9)
    challenge_recoveries: int = 0              # POP-21 recoveries already run inside the LOGIN flow (7.7)
    flow_expects: frozenset[str] = frozenset()  # popup ids the flow expects (POP-15 "unless the flow expects it")
    relogin_budget: ReloginBudget | None = None  # the run's re-login budget K (7.7, SD-66)
    memory: Mapping[str, int] | None = None    # the Watchdog's heap and GDI counts (POP-11)
    build_id: str | None = None                # the build under test (a proposed entry's first_seen_build)
    registry_owner: str = DRAFT_OWNER          # owner of a proposed entry (choice 8)

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"phase {self.phase!r}; one of {PHASES}")
        if self.gate not in load().gates:
            raise ValueError(f"unknown gate {self.gate!r}")


def _evidence(entry: PopupEntry | None, window: Any, **extra: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {"popup_id": entry.popup_id if entry else None,
                          "title": getattr(window, "title", None), "hwnd": getattr(window, "hwnd", None),
                          "popup_action": entry.action if entry else "unknown", "input": None, "close": None}
    ev.update(extra)
    return ev


def _gate_or_running(gate: str) -> str:
    """`running`, or the gate in force when it comes before `running` (7.8 policy 1)."""
    return gate if gate in GATES and GATES.index(gate) < GATES.index("running") else "running"


def _env_alert(gate: str, reason: str) -> alerts.AlertRoute:
    """9.10 `env_error`; the accounts owner hears of a login-caused exit, and a challenge pages both owners (7.7)."""
    route = alerts.env_error(notified=("accounts_owner",) if gate == "authenticated" else ())
    if reason in ("authentication_challenge", "authentication_failed"):
        route = route.plus(paged=("login_owner", "accounts_owner"))
    return route


def _occlusion(ctx: PopupContext, window: Any, ev: dict[str, Any]) -> ControlResult:
    """A foreign window in front before an input batch: the batch is not emitted (7.8 policy 1, POP-19, 5.7)."""
    gate = _gate_or_running(ctx.gate)
    pid, title = getattr(window, "pid", None), getattr(window, "title", "") or ""
    image = getattr(window, "process_name", None)
    o = Outcome("env_error", "occluded", gate, detail=f"pid {pid} {title!r}")
    what = f"foreign window {title!r} (pid {pid}, {image or 'unknown image'})" if window is not None \
        else "no foreground window"
    return ControlResult(CONTROL, False, gate, checkpoint=o, alert=alerts.env_error(),
                         findings=(FindingSpec("hardening", f"{what} in front of the frame before an input batch"),),
                         evidence={**ev, "emit": False, "pid": pid, "process_name": image, "close": None},
                         detail="foreign-process occlusion: the input batch is not emitted")


def _kill(entry: PopupEntry, ctx: PopupContext, window: Any, trigger: str) -> ControlResult:
    """`aborted_safety` through the kill switch sequence (6.6, 7.8 policy 3): no input, the window never closed."""
    oa = entry.on_abort
    reason = oa.reason if oa and oa.outcome == "aborted_run" and oa.run_outcome == "aborted_safety" and oa.reason \
        else "kill_switch"
    run, un = safety_abort_outcome(ctx.gate, reason, detail=f"trigger {trigger}; popup {entry.popup_id}")
    acts = ("end_flow",) + (("quarantine_flow",) if ctx.flow_id else ()) + ("kill_switch",)
    return ControlResult(CONTROL, False, ctx.gate, run=run, unreached=un, step="failed", actions=acts,
                         alert=alerts.safety_abort(), incident="S2", evidence=_evidence(entry, window, trigger=trigger),
                         detail=f"{entry.popup_id} ({entry.cls}): kill switch, {trigger}")


def _abort(entry: PopupEntry, ctx: PopupContext, window: Any) -> ControlResult:
    """The `on_abort` of an `abort`, an expired `wait` or a refused challenge (4.10, choice 4)."""
    oa = entry.on_abort
    if oa is None:
        raise ValueError(f"{entry.popup_id} has no on_abort")
    ev = _evidence(entry, window, close="wm_close")
    step = ctx.step_id or "unknown"
    rest = SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{step}"))
    if oa.outcome == "aborted_run":
        if oa.run_outcome == "aborted_safety":
            return _kill(entry, ctx, window, "popup_on_abort")
        run, un = harness_abort_outcome(str(oa.reason), detail=f"popup {entry.popup_id}")
        return ControlResult(CONTROL, False, ctx.gate, run=run, unreached=un, step="failed",
                             actions=("end_flow", "teardown"), alert=alerts.aborted_harness(), evidence=ev,
                             detail=f"{entry.popup_id}: aborted_harness ({oa.reason})")
    if oa.outcome == "invariant_violation":
        o = Outcome("invariant_violation", oa.invariant_id)
        return ControlResult(CONTROL, False, ctx.gate, checkpoint=o, outcomes=(rest,), step="failed",
                             actions=("end_flow",), evidence={**ev, "capture_first": True},
                             detail=f"{entry.popup_id}: app error dialog is {oa.invariant_id} on the checkpoint being "
                                    "captured, never dismissed (7.8 policy 4)")
    reason, gate = oa.reason or "gate", oa.gate or ctx.gate
    if reason == "version_mismatch" and ctx.relaunched:
        reason = "version_changed_mid_run"
    if reason == "occluded":
        return _occlusion(ctx, window, _evidence(entry, window))
    o = Outcome("env_error", reason, gate)
    if reason == "ondemand_timeout":
        streak = ctx.ondemand_timeouts + 1
        outs = (rest,) + ((SubsetOutcome("frozen", o),) if streak >= ONDEMAND_ABANDON else ())
        return ControlResult(CONTROL, False, gate, checkpoint=o, outcomes=outs, step="failed", actions=("end_flow",),
                             alert=_env_alert(gate, reason), evidence={**ev, "ondemand_timeouts": streak},
                             detail=f"OnDemand buffering timeout {streak} in a row"
                                    + ("; OnDemand abandoned for the run (7.9)" if streak >= ONDEMAND_ABANDON else ""))
    if entry.cls in SHARD_CLASSES or reason == "version_mismatch":
        hold = ("disable_rig",) if reason == "version_mismatch" else ()
        return ControlResult(CONTROL, False, gate, run=RunVerdict("complete_with_env_errors"), checkpoint=o,
                             unreached=o, step="failed", actions=("end_flow", "teardown") + hold,
                             alert=_env_alert(gate, reason), evidence=ev,
                             detail=f"{entry.popup_id} ({entry.cls}): the shard ends env_error ({gate}, {reason})"
                                    + ("; the rig is held until a human looks (7.3)" if hold else ""))
    return ControlResult(CONTROL, False, gate, checkpoint=o, outcomes=(rest,), step="failed", actions=("end_flow",),
                         alert=_env_alert(gate, reason), evidence=ev,
                         detail=f"{entry.popup_id}: the flow ends env_error ({gate}, {reason})")


def decide(entry: PopupEntry, ctx: PopupContext, window: Any = None) -> ControlResult:
    """What a matched window's entry produces (4.10, 7.8 policy 3, 12.8; choices 4 to 7). `window` names the window in
    the evidence. A `relogin` entry other than the challenge prompt spends `ctx.relogin_budget`."""
    if entry.cls == "order_event":
        return _kill(entry, ctx, window, "order_event_window")
    if entry.cls == "safety":
        wait_ms = entry.wait_timeout_ms or int(DIALOG_OPEN_MAX_S * 1000)
        return ControlResult(CONTROL, False, ctx.gate, evidence=_evidence(entry, window, wait_timeout_ms=wait_ms),
                             detail=f"{entry.popup_id}: no key or click is emitted; the kill switch fires after "
                                    f"{wait_ms // 1000} s without an owning step (6.6)")
    if entry.action == "ignore":
        return ControlResult(CONTROL, True, ctx.gate, evidence=_evidence(entry, window),
                             detail=f"{entry.popup_id}: product state, never closed")
    if entry.emits_input:
        inp = {"kind": "key", "key": entry.key} if entry.action == "dismiss_key" else \
            {"kind": "click", "target": entry.target}
        extra: dict[str, Any] = {"input": inp}
        findings: tuple[FindingSpec, ...] = ()
        if entry.popup_id == MEMORY_WARNING_ID:
            findings = (FindingSpec("memory_warning", f"low memory dialog {getattr(window, 'title', '')!r}",
                                    detail=canonical_json(dict(ctx.memory or {}))),)
            extra["proactive_relaunch"] = True
        fixture = FIXTURE_FINDINGS.get(entry.popup_id)
        if fixture and entry.popup_id not in ctx.flow_expects:
            findings += (FindingSpec("fixture_finding", f"{entry.popup_id} {getattr(window, 'title', '')!r} appeared "
                                     "and the flow does not expect it: the fixture should rule it out (12.8)",
                                     detail=fixture),)
        return ControlResult(CONTROL, True, ctx.gate, findings=findings, evidence=_evidence(entry, window, **extra),
                             detail=f"{entry.popup_id}: dismissed and counted")
    if entry.action == "wait":
        if entry.abort_reason == "ondemand_timeout" and ctx.at_capture:
            return ControlResult(CONTROL, False, ctx.gate, checkpoint=Outcome("not_settled", "anti_anchor_present"),
                                 evidence=_evidence(entry, window),
                                 detail=f"{entry.popup_id}: an OnDemand stall mid-checkpoint is an anti-anchor (7.9)")
        return ControlResult(CONTROL, True, ctx.gate,
                             evidence=_evidence(entry, window, wait_timeout_ms=entry.wait_timeout_ms),
                             detail=f"{entry.popup_id}: wait up to {entry.wait_timeout_ms} ms, then on_abort")
    if entry.action == "relogin":
        if entry.abort_reason == "authentication_challenge":
            if ctx.phase == "login" and ctx.challenge_recoveries == 0:
                return ControlResult(CONTROL, True, "authenticated",
                                     evidence=_evidence(entry, window, handoff="login_recovery"),
                                     detail=f"{entry.popup_id}: Branch B recovery inside the LOGIN flow, once (7.7)")
            return _abort(entry, ctx, window)
        if ctx.relogin_budget is None:
            raise ValueError(f"{entry.popup_id} draws on the run's re-login budget K: pass ctx.relogin_budget (SD-66)")
        r = ctx.relogin_budget.environment_relogin()
        return replace(r, control=CONTROL, evidence={**r.evidence, **_evidence(entry, window)},
                       detail=f"{entry.popup_id}: {r.detail}")
    return _abort(entry, ctx, window)


def wait_expired(entry: PopupEntry, ctx: PopupContext, window: Any = None) -> ControlResult:
    """A `wait` that polled `wait_timeout_ms` without the window leaving falls through to `on_abort` (4.10); an unowned
    Order Confirmation past its 30 s is the kill switch (6.6). `ctx.ondemand_timeouts` counts the earlier ones."""
    if entry.cls == "safety":
        return _kill(entry, ctx, window, "dialog_open_30s")
    if entry.action != "wait":
        raise ValueError(f"{entry.popup_id} is not a wait entry")
    return _abort(entry, ctx, window)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "untitled"


def proposed_entry(window: Any, ctx: PopupContext, popup_id: str, *, safety: bool) -> dict[str, Any]:
    """The draft catalog entry of an unmatched window (choice 8)."""
    title = window.title or ""
    draft: dict[str, Any] = {"popup_id": popup_id, "name": f"popup.unknown-{_slug(title)}",
                             "title_regex": f"^{re.escape(title)}$", "seed_titles": [title],
                             "window_class_hint": window.cls}
    if safety:
        draft.update({"class": "safety", "action": "wait", "wait_timeout_ms": int(DIALOG_OPEN_MAX_S * 1000),
                      "on_abort": {"outcome": "aborted_run", "run_outcome": "aborted_safety", "reason": "kill_switch"},
                      "severity": "critical"})
    else:
        draft.update({"class": "dismissable", "action": "dismiss_key", "key": "escape", "severity": "medium"})
    draft.update({"expected_phases": [ctx.phase], "owner": ctx.registry_owner, "status": "unverified",
                  "notes": "proposed by the popup sentinel; confirm the title, class and action"})
    if ctx.build_id:
        draft["first_seen_build"] = ctx.build_id
    return draft


def unknown_window(window: Any, ctx: PopupContext, catalog: PopupCatalog, *, popup_id: str | None = None,
                   detached_ticket: Sequence[str] = DETACHED_TICKET_REGEXES) -> ControlResult:
    """An unmatched window (4.10, 7.8 policy 5): `aborted_safety (reason: unknown_dialog_in_order_stage)` while the
    step's `order_stage` is set; otherwise the flow is quarantined and the window goes to the agent's dismiss-only
    toolset, or WM_CLOSE with agent mode off, unless its title is in the order-dialog set (the catalog's `safety`
    entries and the detached Order Entry window, `detached_ticket`, choice 7) or the order-event set, which is never
    handed to the agent and never closed. Either way an `unknown_popup` finding carries the proposed entry."""
    title = window.title or ""
    safety_title = any(re.search(p, title) for p in (*order_dialog_regexes(catalog), *detached_ticket,
                                                       *order_event_regexes(catalog)))
    draft = proposed_entry(window, ctx, popup_id or catalog.next_popup_id(), safety=ctx.order_stage or safety_title)
    finding = FindingSpec("unknown_popup", f"unknown window {title!r} ({window.cls}) in phase {ctx.phase}",
                          detail=canonical_json(draft))
    ev = _evidence(None, window, proposed_entry=draft)
    if ctx.order_stage:
        run, un = safety_abort_outcome(ctx.gate, "unknown_dialog_in_order_stage", detail=f"window {title!r}")
        return ControlResult(CONTROL, False, ctx.gate, run=run, unreached=un, step="failed",
                             actions=("end_flow", "kill_switch"), alert=alerts.safety_abort(), incident="S2",
                             findings=(finding,), evidence=ev,
                             detail="an unknown dialog over a staged order may be a send path (4.10)")
    if safety_title:
        close = None
    else:
        close = "agent_dismiss" if ctx.agent_enabled else "wm_close"
    if close == "agent_dismiss":
        ev.update(dismiss_labels=list(AGENT_DISMISS_LABELS), refused_labels=list(AGENT_NEVER_LABELS))
    return ControlResult(CONTROL, False, ctx.gate, actions=("quarantine_flow",) if ctx.flow_id else (),
                         alert=alerts.flow_quarantined() if ctx.flow_id else None, findings=(finding,),
                         evidence={**ev, "close": close},
                         detail=f"unknown window {title!r}: "
                                + ("order-dialog title, never handed to the agent or closed" if close is None
                                   else f"closed by {close}"))


def persisting(window: Any, ctx: PopupContext, entry: PopupEntry | None = None) -> ControlResult:
    """A window still present after its dismissal or close (7.8 policy 5, choice 10)."""
    title = window.title or ""
    ev = _evidence(entry, window, persisted=True)
    if ctx.census:
        return ControlResult(CONTROL, False, ctx.gate, checkpoint=Outcome("not_settled",
                                                                           f"unexpected_window:{title or 'untitled'}"),
                             evidence=ev, detail="window persisted at a census checkpoint; the census moves on")
    o = Outcome("not_run", f"step_failed:{ctx.step_id or 'unknown'}")
    return ControlResult(CONTROL, False, ctx.gate, checkpoint=o, outcomes=(SubsetOutcome("rest_of_flow", o),),
                         step="failed", actions=("end_flow", "reset_input_state"), evidence=ev,
                         detail=f"window {title!r} persisted: the step fails, the next flow's preconditions decide")


def foreground_check(foreground: Any | None, tree_pids: Collection[int], ctx: PopupContext) -> ControlResult | None:
    """GetForegroundWindow before an input batch (7.8 policy 1): None when it belongs to the process tree; otherwise
    `env_error (gate: running, reason: occluded)` (or the gate in force before `running`), the batch not emitted, and a
    `hardening` finding (choice 11)."""
    if foreground is not None and foreground.pid in tree_pids:
        return None
    return _occlusion(ctx, foreground, _evidence(None, foreground, popup_action="foreground"))


# -- the sentinel of one shard ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PopupHit:
    """One unexpected window of a sweep: its entry (None: unmatched), the decision, and whether this sweep is the
    first to see it (a new hit: write its step event, act on the decision)."""

    window: Any
    entry: PopupEntry | None
    result: ControlResult
    new: bool


@dataclass
class PopupCounter:
    """Hits per run (7.8 policy 3): by `popup_id`, or by raw title for an unmatched window (8.3 `popup_title`)."""

    counts: Counter[str] = field(default_factory=Counter)

    def add(self, key: str) -> None:
        self.counts[key] += 1

    @classmethod
    def from_events(cls, events: Iterable[StepEvent]) -> PopupCounter:
        """A run's counts from its `step_events` rows of action `popup`, one row per hit."""
        return cls(Counter(e.popup_title for e in events if e.action == "popup" and e.popup_title))


def _passive(entry: PopupEntry) -> bool:
    """Entries whose window stays by design while the sentinel waits or the kill switch acts (choice 3)."""
    return entry.cls in NEVER_CLOSED or entry.action in ("wait", "ignore")


class PopupSentinel:
    """One shard's popup sentinel (7.8): the catalog, the run's hit counter, the OnDemand and challenge streaks, the
    windows present at the last sweep and the proposed ids of unknown titles. `detached_ticket` is the detached Order
    Entry title set `unknown_window` keeps from the agent and WM_CLOSE (choice 7): the Window Watcher's."""

    def __init__(self, catalog: PopupCatalog, counter: PopupCounter | None = None, *,
                 detached_ticket: Sequence[str] = DETACHED_TICKET_REGEXES):
        self.catalog = catalog
        self.detached_ticket = tuple(detached_ticket)
        self.counter = counter if counter is not None else PopupCounter()
        self.ondemand_timeouts = 0
        self.challenge_recoveries = 0
        self._active: dict[int, str | None] = {}           # hwnd -> the popup_id it matched (None: unmatched)
        self._handoffs: dict[int, ControlResult] = {}       # hwnd -> a LOGIN-flow recovery in progress
        self._proposed: dict[str, str] = {}
        self._occluders: set[str] = set()

    def sweep(self, windows: Iterable[Any], ctx: PopupContext, *, expected_hwnds: Collection[int] = (),
              expected_titles: Iterable[str] = (),
              template_ok: Callable[[PopupEntry, Any], bool] | None = None) -> tuple[PopupHit, ...]:
        """Before every step and every capture (4.10, 7.8 policy 1): one decision per unexpected window. A window is
        one appearance while its hwnd stays matched to the same entry (an unmatched one may retitle itself). Seen again,
        a window its dismissal or close should have removed is `persisting`; a waiting or kill-switch window is decided
        again with no new count, and a LOGIN-flow recovery in progress keeps its hand-off."""
        ctx = replace(ctx, ondemand_timeouts=self.ondemand_timeouts, challenge_recoveries=self.challenge_recoveries)
        hits: list[PopupHit] = []
        active: dict[int, str | None] = {}
        for w in unexpected_windows(windows, expected_hwnds=expected_hwnds, expected_titles=expected_titles):
            entry = self.catalog.match(w, ctx.phase, template_ok=template_ok)
            ident = entry.popup_id if entry else None
            new = w.hwnd not in self._active or self._active[w.hwnd] != ident
            active[w.hwnd] = ident
            key = ident or (w.title or "")
            if not new and w.hwnd in self._handoffs:
                hits.append(PopupHit(w, entry, self._handoffs[w.hwnd], new=False))
                continue
            if not new and (entry is None or not _passive(entry)):
                hits.append(PopupHit(w, entry, persisting(w, ctx, entry), new=False))
                continue
            if entry is None:
                pid = self._proposed.get(key) or self.catalog.next_popup_id(self._proposed.values())
                self._proposed[key] = pid
                result = unknown_window(w, ctx, self.catalog, popup_id=pid, detached_ticket=self.detached_ticket)
            else:
                result = decide(entry, ctx, w)
            if new and (entry is None or entry.action != "ignore"):
                self.counter.add(key)
            if new and result.evidence.get("handoff") == "login_recovery":
                self.challenge_recoveries += 1
                self._handoffs[w.hwnd] = result
            hits.append(PopupHit(w, entry, result, new=new))
        self._active = active
        self._handoffs = {h: r for h, r in self._handoffs.items() if h in active}
        return tuple(hits)

    def wait_over(self, hit: PopupHit, ctx: PopupContext, *, cleared: bool) -> ControlResult | None:
        """The end of a `wait` poll (4.10): None when the window left (an OnDemand success resets the streak, 7.9),
        else `wait_expired`."""
        entry = hit.entry
        if entry is None or not (entry.action == "wait" or entry.cls == "safety"):
            raise ValueError("wait_over is for a wait or safety hit")
        ondemand = entry.abort_reason == "ondemand_timeout"
        if cleared:
            if ondemand:
                self.ondemand_timeouts = 0
            return None
        r = wait_expired(entry, replace(ctx, ondemand_timeouts=self.ondemand_timeouts), hit.window)
        if ondemand:
            self.ondemand_timeouts += 1
        return r

    def foreground(self, foreground: Any | None, tree_pids: Collection[int], ctx: PopupContext) -> ControlResult | None:
        """`foreground_check`, with one on-call alert per run per foreign image name (5.7, choice 11)."""
        r = foreground_check(foreground, tree_pids, ctx)
        if r is None:
            return None
        image = getattr(foreground, "process_name", None) or getattr(foreground, "title", None) or "unknown"
        if image in self._occluders:
            return replace(r, alert=None)
        self._occluders.add(image)
        return r


def popup_event(hit: PopupHit, *, run_id: str, shard_id: str, side: str, seq: int, wall_at: str, mono_ms: int,
                mode: str = "system", flow_id: str | None = None, step_id: str | None = None,
                checkpoint_id: str | None = None, screenshot_sha256: str | None = None) -> StepEvent:
    """The 8.3 `step_events` row of one new hit: action `popup`, `popup_title` the matched `popup_id` or the raw title
    of an unknown popup, the key or click target sent, the catalog action (or `unknown:<close>`) as `reason`, and the
    hit's screenshot as `pre_hash` (7.8 policy 3: every hit logged with a screenshot)."""
    ev = hit.result.evidence
    inp = ev.get("input") or {}
    entry = hit.entry
    return StepEvent(run_id=run_id, shard_id=shard_id, side=side, seq=seq, source="orchestrator", wall_at=wall_at,
                     mono_ms=mono_ms, mode=mode, action="popup", flow_id=flow_id, step_id=step_id,
                     checkpoint_id=checkpoint_id, keys=inp.get("key"), target_text=inp.get("target"),
                     popup_title=entry.popup_id if entry else (hit.window.title or ""),
                     reason=entry.action if entry else f"unknown:{ev.get('close') or 'none'}",
                     pre_hash=screenshot_sha256)


# -- per-build counts and the rate finding (7.8 policy 3, 4.13) ---------------------------------------------------

@dataclass(frozen=True)
class BuildCounts:
    """One build's popup hits over `runs` runs (or run sides): the per-build count of 7.8."""

    build_id: str
    runs: int
    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError(f"{self.build_id}: a build's counts cover at least one run")

    def rate(self, key: str) -> float:
        """Hits per run (choice 9)."""
        return self.counts.get(key, 0) / self.runs

    @classmethod
    def from_events(cls, build_id: str, events: Iterable[StepEvent], *, side: str | None = None,
                    runs: int | None = None) -> BuildCounts:
        """The counts of every popup row among `events` (all the step events of the build's runs, filtered to `side`
        when given, the side of a PR run that ran this build); `runs` defaults to the distinct run ids seen."""
        evs = [e for e in events if side is None or e.side == side]
        n = runs if runs is not None else len({e.run_id for e in evs})
        return cls(build_id, n, dict(PopupCounter.from_events(evs).counts))


def popup_rate_findings(history: Sequence[BuildCounts]) -> tuple[FindingSpec, ...]:
    """`popup_rate_increase` findings for the newest build of `history` (oldest first) against the build before it: one
    per catalog id whose hits per run rose after the build change (7.8 policy 3, choice 9)."""
    if len(history) < 2 or history[-1].build_id == history[-2].build_id:
        return ()
    prev, cur = history[-2], history[-1]
    out = []
    for key in sorted(k for k in cur.counts if POP_ID.match(k)):
        before, after = prev.rate(key), cur.rate(key)
        if after > before:
            detail = {"popup_id": key, "build": cur.build_id, "hits": cur.counts[key], "runs": cur.runs,
                      "previous_build": prev.build_id, "previous_hits": prev.counts.get(key, 0),
                      "previous_runs": prev.runs}
            out.append(FindingSpec("popup_rate_increase", f"{key}: {after:.2f} hits per run on {cur.build_id}, "
                                                          f"{before:.2f} on {prev.build_id}",
                                   detail=canonical_json(detail)))
    return tuple(out)
