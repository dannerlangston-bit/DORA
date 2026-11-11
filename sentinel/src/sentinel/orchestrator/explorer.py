"""Explorer boundary: the deny list, the session budgets and the order-stage path (6.8, 2.9; F37).

- The deny list (`DenyEntry`, `DEFAULT_DENY_LIST` from the table of 6.8) matches a target by window
  title, by resolved control name and under-point OCR label, and by landmark-anchored geometry (the
  resolver's `target_kind`). A hit is a `deny_profile` refusal in the slow half (gateway.py).
- Budgets, enforced by the Orchestrator and never the prompt: 400 actions, 90 minutes, 25 dollars per
  session (and the weekly 500 shared with agent rescues, 11.4), early stop after 20 consecutive actions
  with no new screenshot hash, and the RC run's deadline. Exhaustion ends the session, never the run.
- Three denied attempts in one session end it; an attempted-send reason ends it at once with an S3
  ticket and counts toward the per-run trip of ten (`DropLedger`, gateway.py).
- The ticket or dialog flag set or unknown in a session: all input dropped, the window closed by the
  gateway's tokened Escape, the session ended with the finding `explorer_order_stage` (S1, the safety
  routing of 9.10 plus the Explorer owner), counted toward the ten; the run continues. The kill switch
  does not fire for it (the 30 s dialog rule is for scripted flows and agent rescue only).
- The model endpoint down when the session is due (`agent_available: false`): the session is `not_run
  (reason: model_unavailable)` and the RC run is unaffected (10.7).
- Teardown replays the mutation ledger; an item that cannot be deleted or read back blocks the Explorer
  login with `env_error (gate: teardown, reason: precondition:explorer_ledger_clean)` on the Explorer
  stage, the item in `detail`, P2 to the Explorer owner.
- The versioned deny list is `policy/explorer_deny_list.yaml` (4.16, SD-30): `deny_list_problems` checks it
  against the registry (the L-XREF-13 checks, shared with the lint) and `deny_entries` / `load_deny_list`
  turn it into `DenyEntry` rows. `denied_action_plan` is the nightly denied-action test's list (6.8): one
  Explorer-profile action per detection of every entry, plus one per registry control with `deny: true`
  that no entry names; `safety_canaries.denied_action_test` drives it.

Choices (reported): the label patterns of `DEFAULT_DENY_LIST` are this module's reading of the 6.8
table and fail closed (for example every label containing "alert" is denied, not only alert creation);
the versioned list in `sentinel-config` replaces them through `DenyEntry`. Budget checks run after each
action in the order actions, time, no-change, session dollars, weekly dollars.

Choices for the denied-action test (6.8, 4.16; reported):
- A file entry's `control` becomes a geometry target: the resolver reports a registry control by its ref
  (`<surface id>.<control id>`) as the action's `target_kind`, and a `surface` entry hits that surface and
  every control ref under it. The control's registry names (JAB name, OCR text) ride along as the probe's
  labels, as the resolver reports both.
- A regex detection is driven with a sample text the regex matches (`regex_sample`: the first alternative,
  each optional part left out, the first character of each class); a regex no sample can be made for cannot
  be driven, and L-XREF-13 says so. A window-title detection is driven as a click on the control that opens
  the window, named as the window: the Window Watcher's own close-by-Escape path is 5.4.6's.
- An `order_path` entry adds a drag that starts on a splitter and ends on the entry's geometry (6.2: a drag
  that ends on order geometry is an order path).
"""
from __future__ import annotations

import re
import re._parser as _rx  # the stdlib regex parser (3.11+), read-only, for `regex_sample`
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import alerts
from .clock import Clock
from .gateway import Action, DropLedger, GatewayDrop
from .results import ControlResult, FindingSpec, Outcome, SubsetOutcome, safety_abort_outcome

DENY_LIST_PATH = "policy/explorer_deny_list.yaml"     # 4.1, SD-30
DETECTIONS = ("window_title_regex", "label_regex", "control", "surface")   # 4.16 deny_entry


@dataclass(frozen=True)
class DenyEntry:
    """One deny-list row (6.8, 4.16): its id, title regex, label regex, geometry (resolver target kinds and
    registry control refs), a whole surface, whether it is an order path, and the registry names of the control
    it names (what the denied-action test's click carries as labels)."""

    name: str
    title: str | None = None
    label: str | None = None
    geometry: frozenset[str] = frozenset()
    surface: str | None = None
    order_path: bool = False
    control_names: tuple[str, ...] = ()

    def hit(self, *, title: str | None, labels: Sequence[str], target_kind: str | None) -> bool:
        if self.title and title and re.search(self.title, title, re.I):
            return True
        if self.label and any(re.search(self.label, s, re.I) for s in labels if s):
            return True
        if target_kind is None:
            return False
        if target_kind in self.geometry:
            return True
        return self.surface is not None and (target_kind == self.surface or target_kind.startswith(self.surface + "."))


DEFAULT_DENY_LIST: tuple[DenyEntry, ...] = (
    DenyEntry("chat", r"chat|support", r"chat|message"),
    DenyEntry("share", r"share|tos\.mx", r"share|tos\.mx"),
    DenyEntry("setup", r"setup|application settings|order defaults", r"^setup$|application settings|order defaults",
              frozenset({"setup_menu", "application_settings"})),
    DenyEntry("adjust_account", r"adjust account", r"adjust account"),
    DenyEntry("logout_account_switch", None, r"log ?out|log off", frozenset({"account_selector"})),
    DenyEntry("leaves_rig_or_persists", None, r"^help$|browser|export|print|save workspace|delete workspace"),
    DenyEntry("persistent_and_send", None, r"alert|saved orders|blast all|flatten|confirm and send|^send$",
              frozenset({"active_trader_order_column"})),
    DenyEntry("opens_ticket", None, r"^(buy|sell)\b|buy mkt|sell mkt",
              frozenset({"ladder_bid_ask_cell", "level2_bid_ask_cell", "order_entry_button", "ladder_order_cell",
                         "order_bubble"})),
)


def match_deny(*, title: str | None, labels: Sequence[str], target_kind: str | None,
               deny_list: Sequence[DenyEntry] = DEFAULT_DENY_LIST, end_kind: str | None = None) -> str | None:
    """The first deny-list entry the target hits (title, label or geometry), for `SafetyContext.deny_list_hit`.
    `end_kind` is a drag's end target: a gesture that ends on denied geometry is denied too (6.8)."""
    for e in deny_list:
        if e.hit(title=title, labels=labels, target_kind=target_kind) or (
                end_kind is not None and e.hit(title=None, labels=(), target_kind=end_kind)):
            return e.name
    return None


# -- the versioned deny list (4.16) and the denied-action test's plan (6.8) ------------------------------------
class DenyListError(ValueError):
    """`policy/explorer_deny_list.yaml` fails the L-XREF-13 checks; the message lists every problem."""


@dataclass(frozen=True)
class RegistryControl:
    """One registry control as the deny list sees it: its ref, its names (JAB name, OCR text) and `deny`."""

    ref: str
    names: tuple[str, ...] = ()
    deny: bool = False


@dataclass(frozen=True)
class RegistryIndex:
    """The surfaces and controls of the Surface Registry (4.5), keyed by surface id and control ref."""

    surfaces: frozenset[str]
    controls: Mapping[str, RegistryControl]

    @property
    def deny_controls(self) -> dict[str, tuple[str, ...]]:
        """Every control with `deny: true`, ref -> names (6.8: the gateway deny list names them by registry id)."""
        return {ref: c.names for ref, c in self.controls.items() if c.deny}


def registry_index(surfaces: Iterable[Any]) -> RegistryIndex:
    """Index parsed surface files (`registry/surfaces/**/*.yaml`); malformed entries are skipped (the schema
    check reports them)."""
    ids: set[str] = set()
    controls: dict[str, RegistryControl] = {}
    for s in surfaces:
        sid = s.get("id") if isinstance(s, Mapping) else None
        if not isinstance(sid, str):
            continue
        ids.add(sid)
        for c in s.get("controls") or ():
            if not (isinstance(c, Mapping) and isinstance(c.get("id"), str)):
                continue
            jab = c.get("jab") if isinstance(c.get("jab"), Mapping) else {}
            names = tuple(dict.fromkeys(n for n in (jab.get("name"), c.get("ocr_text")) if isinstance(n, str) and n))
            controls.setdefault(f"{sid}.{c['id']}", RegistryControl(f"{sid}.{c['id']}", names, c.get("deny") is True))
    return RegistryIndex(frozenset(ids), controls)


def regex_sample(pattern: str) -> str | None:
    """A text the regex matches (case-insensitively, after the label strip of `Action.labels`), or None when this
    simple construction finds none (choice in the module docstring)."""
    try:
        tree = _rx.parse(pattern)
    except re.error:
        return None
    text = _emit(list(tree))
    if text is None or not text.strip() or not re.search(pattern, text.strip(), re.I):
        return None
    return text.strip()


def _emit(items: list) -> str | None:
    out: list[str] = []
    for op, av in items:
        if op is _rx.LITERAL:
            out.append(chr(av))
        elif op is _rx.AT:
            continue
        elif op is _rx.ANY:
            out.append("x")
        elif op is _rx.IN:
            first = av[0] if av else (None, None)
            if first[0] is _rx.LITERAL:
                out.append(chr(first[1]))
            elif first[0] is _rx.RANGE:
                out.append(chr(first[1][0]))
            elif first[0] is _rx.CATEGORY and first[1] in (_rx.CATEGORY_DIGIT, _rx.CATEGORY_WORD, _rx.CATEGORY_SPACE):
                out.append({_rx.CATEGORY_DIGIT: "0", _rx.CATEGORY_WORD: "a"}.get(first[1], " "))
            else:
                return None
        elif op is _rx.BRANCH:
            alt = next((s for s in (_emit(list(b)) for b in av[1]) if s is not None), None)
            if alt is None:
                return None
            out.append(alt)
        elif op is _rx.SUBPATTERN:
            sub = _emit(list(av[3]))
            if sub is None:
                return None
            out.append(sub)
        elif op in (_rx.MAX_REPEAT, _rx.MIN_REPEAT, _rx.POSSESSIVE_REPEAT):
            sub = _emit(list(av[2]))
            if sub is None:
                return None
            out.append(sub * av[0])
        else:
            return None
    return "".join(out)


def deny_list_problems(data: Any, registry: RegistryIndex) -> list[tuple[str, str]]:
    """The L-XREF-13 checks of one parsed deny list against the registry (4.15): every control and surface an
    entry names exists, every entry has a detection, its regexes compile and the denied-action test can drive
    them, and ids do not repeat. Returns (path, message) pairs; shape errors are the schema's, not repeated."""
    out: list[tuple[str, str]] = []
    entries = data.get("entries") if isinstance(data, Mapping) else None
    seen: set[str] = set()
    for i, e in enumerate(entries if isinstance(entries, list) else ()):
        if not isinstance(e, Mapping):
            continue
        at = f"entries[{i}]"
        eid = e.get("id")
        if isinstance(eid, str):
            if eid in seen:
                out.append((f"{at}.id", f"deny-list entry id {eid} repeats"))
            seen.add(eid)
        if not any(isinstance(e.get(k), str) and e.get(k) for k in DETECTIONS):
            out.append((at, f"deny-list entry {eid} has no detection (window title, label, control or surface)"))
        for k in ("window_title_regex", "label_regex"):
            rx = e.get(k)
            if not isinstance(rx, str):
                continue
            try:
                re.compile(rx)
            except re.error as err:
                out.append((f"{at}.{k}", f"{k} {rx!r} does not compile: {err}"))
                continue
            if regex_sample(rx) is None:
                out.append((f"{at}.{k}", f"the nightly denied-action test cannot drive {k} {rx!r}: no text it can "
                                         "type matches it (6.8)"))
        control = e.get("control")
        if isinstance(control, str) and control not in registry.controls:
            out.append((f"{at}.control", f"control {control} is not a <surface>.<control> of the registry"))
        surface = e.get("surface")
        if isinstance(surface, str) and surface not in registry.surfaces:
            out.append((f"{at}.surface", f"surface {surface} is not in the registry"))
    return out


def deny_entries(data: Any, registry: RegistryIndex) -> tuple[DenyEntry, ...]:
    """The `DenyEntry` rows of a parsed deny list that passes `deny_list_problems` (4.16 -> 6.8)."""
    problems = deny_list_problems(data, registry)
    entries = data.get("entries") if isinstance(data, Mapping) else None
    if not isinstance(entries, list):
        problems.insert(0, ("entries", "the deny list has no entries list (4.16)"))
    else:
        problems[:0] = [(f"entries[{i}]", "not a deny_entry with an id (4.16)") for i, e in enumerate(entries)
                        if not (isinstance(e, Mapping) and isinstance(e.get("id"), str))]
    if problems:
        raise DenyListError("; ".join(f"{p}: {m}" for p, m in problems))
    out = []
    for e in entries:
        control = e.get("control")
        out.append(DenyEntry(
            e["id"], title=e.get("window_title_regex"), label=e.get("label_regex"),
            geometry=frozenset({control}) if control else frozenset(), surface=e.get("surface"),
            order_path=e.get("order_path") is True,
            control_names=registry.controls[control].names if control else ()))
    return tuple(out)


def load_deny_list(config_root: str | Path) -> tuple[tuple[DenyEntry, ...], RegistryIndex]:
    """Read `policy/explorer_deny_list.yaml` and the Surface Registry of a sentinel-config checkout; the entries
    and the registry index (whose `deny_controls` the denied-action test also drives)."""
    root = Path(config_root)
    registry = registry_index(yaml.safe_load(p.read_text(encoding="utf-8"))
                              for p in sorted((root / "registry" / "surfaces").glob("**/*.yaml")))
    data = yaml.safe_load((root / DENY_LIST_PATH).read_text(encoding="utf-8"))
    return deny_entries(data, registry), registry


@dataclass(frozen=True)
class DenyProbe:
    """One scripted attempt of the denied-action test (6.8): the entry it drives (or `control:<ref>` for a
    `deny: true` control no entry names), the detection it exercises and the Explorer-profile action."""

    entry: str
    detection: str                  # title | label | geometry | surface | order_path | control
    action: Action


def _click(**kw: Any) -> Action:
    return Action("click", profile="explorer", **kw)


def _named(names: Sequence[str]) -> dict[str, str | None]:
    """A registry control's names as the resolver reports them: the first as the resolved name, the last as the
    under-point text."""
    return {"resolved_name": names[0] if names else None, "under_point_text": names[-1] if names else None}


def denied_action_plan(entries: Sequence[DenyEntry], deny_controls: Mapping[str, Sequence[str]] | None = None
                       ) -> tuple[DenyProbe, ...]:
    """The denied-action test's list (6.8, L-XREF-13): every detection of every entry, then every `deny: true`
    control that no entry names. An entry whose detections give no probe is absent, which the lint reports."""
    probes: list[DenyProbe] = []
    for e in entries:
        for kind, rx in (("title", e.title), ("label", e.label)):
            sample = regex_sample(rx) if rx else None
            if sample:
                probes.append(DenyProbe(e.name, kind, _click(resolved_name=sample, under_point_text=sample)))
        for g in sorted(e.geometry):
            probes.append(DenyProbe(e.name, "geometry", _click(target_kind=g, **_named(e.control_names))))
        if e.surface:
            probes.append(DenyProbe(e.name, "surface", _click(target_kind=e.surface)))
        end = next(iter(sorted(e.geometry)), e.surface)
        if e.order_path and end:
            probes.append(DenyProbe(e.name, "order_path", Action("drag", profile="explorer", target_kind="splitter",
                                                                 end_kind=end)))
    named = {g for e in entries for g in e.geometry}
    for ref, names in sorted((deny_controls or {}).items()):
        if ref not in named:
            probes.append(DenyProbe(f"control:{ref}", "control", _click(target_kind=ref, **_named(names))))
    return tuple(probes)


@dataclass(frozen=True)
class ExplorerBudget:
    """The session budgets of 6.8 (`policy/explorer_budget.yaml`)."""

    max_actions: int = 400
    max_minutes: float = 90.0
    max_no_change_actions: int = 20
    max_usd_per_session: float = 25.0
    max_usd_per_week: float = 500.0

    @classmethod
    def from_policy(cls, data: Mapping[str, object]) -> "ExplorerBudget":
        return cls(**{k: data[k] for k in ("max_actions", "max_minutes", "max_no_change_actions",
                                           "max_usd_per_session", "max_usd_per_week") if k in data})  # type: ignore[arg-type]


def not_started_model_unavailable() -> ControlResult:
    """The session due with the model endpoint down (2.9, 10.7): `not_run (reason: model_unavailable)`."""
    return ControlResult("explorer", False, "running",
                         outcomes=(SubsetOutcome("explorer_stage", Outcome("not_run", "model_unavailable")),),
                         alert=alerts.coverage_loss(notified=("explorer_owner",)),
                         detail="Explorer: not run, model endpoint unavailable")


class ExplorerSession:
    """One Explorer session's budgets and boundary decisions."""

    def __init__(self, session_id: str, clock: Clock, *, budget: ExplorerBudget = ExplorerBudget(),
                 ledger: DropLedger, deadline_mono: float | None = None, week_spent_usd: float = 0.0):
        self.session_id = session_id
        self.clock = clock
        self.budget = budget
        self.ledger = ledger
        self.deadline = deadline_mono
        self.started = clock.monotonic()
        self.actions = 0
        self.no_change = 0
        self.usd = 0.0
        self.week_usd = week_spent_usd
        self.last_hash: str | None = None
        self.ended: str | None = None

    def _end(self, why: str, **kw) -> ControlResult:
        self.ended = why
        return ControlResult("explorer", False, "running", actions=kw.pop("actions", ("end_session",)),
                             detail=kw.pop("detail", f"Explorer session ended: {why}"), **kw)

    def budget_exhausted(self) -> str | None:
        """The budget that is spent, by name, or None."""
        b = self.budget
        elapsed = self.clock.monotonic() - self.started
        if self.actions >= b.max_actions:
            return "actions"
        if elapsed >= b.max_minutes * 60:
            return "minutes"
        if self.deadline is not None and self.clock.monotonic() >= self.deadline:
            return "rc_deadline"
        if self.no_change >= b.max_no_change_actions:
            return "no_change"
        if self.usd >= b.max_usd_per_session:
            return "usd_session"
        if self.week_usd >= b.max_usd_per_week:
            return "usd_week"
        return None

    def record_action(self, screenshot_sha256: str, usd: float = 0.0) -> ControlResult | None:
        """Count one action, its post-action screenshot hash and its model spend; a result when a budget ends it."""
        if self.ended:
            raise RuntimeError(f"session already ended ({self.ended})")
        self.actions += 1
        self.no_change = self.no_change + 1 if screenshot_sha256 == self.last_hash else 0
        self.last_hash = screenshot_sha256
        self.usd += usd
        self.week_usd += usd
        spent = self.budget_exhausted()
        if spent:
            return self._end(f"budget {spent}", detail=f"Explorer budget hit: {spent}; the run continues",
                             evidence={"budget": spent})
        return None

    def record_drop(self, drop: GatewayDrop) -> ControlResult:
        """A refusal or drop in the session, through the run's ledger (three end it; a send ends it at once)."""
        r = self.ledger.record(drop, unit=self.session_id)
        if r.has("end_session") or r.has("kill_switch"):
            self.ended = drop.reason
        return r

    def order_stage(self) -> ControlResult:
        """The ticket or dialog flag set or unknown in the session (6.8 Order stage in an Explorer session)."""
        self.ended = "order_stage"
        self.ledger.run_count += 1
        finding = FindingSpec("explorer_order_stage", "the ticket or dialog flag was set in an Explorer session",
                              severity="S1")
        if self.ledger.run_count >= self.ledger.per_run:
            run, un = safety_abort_outcome("running", "kill_switch")
            return ControlResult("explorer", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
                                 actions=("end_session", "close_window_escape", "kill_switch"), findings=(finding,),
                                 incident="S1", detail="explorer_order_stage reached the per-run trip of ten")
        return ControlResult("explorer", False, "running", alert=alerts.explorer_order_stage(),
                             actions=("end_session", "close_window_escape"), findings=(finding,), incident="S1",
                             detail="all input dropped, window closed by Escape token; the run continues")


def explorer_teardown(ledger_items: Sequence[str], verified_deleted: Mapping[str, bool]) -> ControlResult:
    """Replay of the server-side mutation ledger at session end (6.8)."""
    left = [i for i in ledger_items if not verified_deleted.get(i)]
    if not left:
        return ControlResult("explorer_teardown", True, "teardown")
    o = Outcome("env_error", "precondition:explorer_ledger_clean", "teardown", "; ".join(left))
    return ControlResult("explorer_teardown", False, "teardown", outcomes=(SubsetOutcome("explorer_stage", o),),
                         alert=alerts.explorer_owner(), actions=("block_login",),
                         detail="Explorer login blocked until the owner cleans it by hand: " + ", ".join(left))
