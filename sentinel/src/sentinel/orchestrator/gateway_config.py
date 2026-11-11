"""The two gateway files of 4.16: the dismiss allowlist and the default order hotkeys (5.4.3, 5.4.7, 6.2, 6.13 item 10).

- `load_dismiss_allowlist(path)`: the labels the gateway may click while an order dialog is in front
  (`gateway/dismiss_allowlist.yaml`). Empty when the file is absent or does not record the Phase 0 read, so
  `Confirm and Send` is refused until Phase 0 pins the labels (gateway.py choice 10, 6.13 item 10).
- `load_default_order_hotkeys(path)`: the pinned build's default order-action chords
  (`gateway/default_order_hotkeys.yaml`), loaded at `settings_attested` beside the Hot Keys rows A6 read; a missing
  or invalid file raises `GatewayConfigError`, whose `result` fails the run closed at `settings_attested`
  (`aborted_harness (reason: config_invalid)`, 5.4.7, L-XREF-17).
- `order_hotkey_list(a6_chords, defaults)`: the chord list the segment holds (5.4.7): the A6 bindings plus the file's
  chords, without repeats, at most sixteen.
- `label_problems` and `chord_problems`: the checks of L-XREF-18 and L-XREF-17, which the lint module
  `lint/checks/gateway_files.py` and these loaders share.

Choices where the spec is ambiguous or silent (reported):

1. "Phase 0 signs" (6.13 item 10, 4.16): the allowlist's labels count only when the file records the read, that is
   `read_on_build`, `read_by` and `read_at` are all present; otherwise the loaded allowlist is empty. The lint warns
   about labels without that record (L-XREF-18).
2. A label is a pattern when it carries a regex metacharacter (`^ $ * + ? ( ) [ ] { } | \\`) or surrounding
   whitespace; it is send-like when it matches the never-tokenize regex, the send-like regex of 5.4.3, or `Confirm`
   (L-XREF-18 names `Send`, `Submit`, `Replace` and `Confirm`).
3. Two chords repeat when they parse to the same modifiers and key (`ctrl+shift+b` and `Shift+Ctrl+B`).
4. A missing or invalid hotkeys file is `aborted_harness (reason: config_invalid)` at gate `settings_attested`:
   the harness's own configuration is wrong, not the environment, and the run cannot block the default chords.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import alerts
from .gateway import CHORD_SLOTS, NEVER_TOKENIZE_RE, SEND_LIKE_RE, Chord
from .results import ControlResult, harness_abort_outcome

DISMISS_ALLOWLIST_PATH = "gateway/dismiss_allowlist.yaml"
DEFAULT_ORDER_HOTKEYS_PATH = "gateway/default_order_hotkeys.yaml"
PIN_FIELDS = ("read_on_build", "read_by", "read_at")
GATE = "settings_attested"
_PATTERN_CHARS = re.compile(r"[\^$*+?()\[\]{}|\\]")
_CONFIRM_RE = re.compile(r"confirm", re.I)


class GatewayConfigError(ValueError):
    """A gateway file is missing or breaks its rule; `result` fails the run closed at the gate that loads it."""

    def __init__(self, message: str, *, rule: str, gate: str = GATE):
        super().__init__(message)
        self.rule = rule
        self.gate = gate

    @property
    def result(self) -> ControlResult:
        """`aborted_harness (reason: config_invalid)` at the loading gate (choice 4)."""
        run, un = harness_abort_outcome("config_invalid", detail=f"{self.rule}: {self}")
        return ControlResult("gateway_config", False, self.gate, run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail=f"{self.rule}: {self}", evidence={"rule_ids": [self.rule]})


def label_problems(label: Any) -> list[str]:
    """Why a dismiss-allowlist label breaks L-XREF-18 (choice 2); [] for an exact, non-send-like label."""
    if not isinstance(label, str) or not label.strip():
        return [f"label {label!r} is not a non-empty string"]
    out = []
    if label != label.strip() or _PATTERN_CHARS.search(label):
        out.append(f"label {label!r} is a pattern or not exact; labels are exact strings")
    if NEVER_TOKENIZE_RE.match(label.strip()) or SEND_LIKE_RE.search(label) or _CONFIRM_RE.search(label):
        out.append(f"label {label!r} is send-like; it may never be clicked in an order dialog")
    return out


def chord_problems(entries: Sequence[Any]) -> list[tuple[int, str]]:
    """Why the `chords` of the hotkeys file break L-XREF-17 (choice 3), as (index, message); [] when none do."""
    out: list[tuple[int, str]] = []
    seen: dict[Chord, int] = {}
    for i, e in enumerate(entries):
        text = e.get("chord") if isinstance(e, Mapping) else None
        if not isinstance(text, str):
            out.append((i, f"entry {i} has no chord string"))
            continue
        try:
            c = Chord.parse(text)
        except ValueError as err:
            out.append((i, f"chord {text!r} does not parse as modifiers (Ctrl, Alt, Shift, Win) plus one key: {err}"))
            continue
        if c in seen:
            out.append((i, f"chord {text!r} repeats entry {seen[c]}"))
        else:
            seen[c] = i
    if len(seen) > CHORD_SLOTS:
        out.append((len(entries) - 1, f"{len(seen)} chords; the segment holds {CHORD_SLOTS} with the A6 bindings"))
    return out


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ValueError(f"{path} is not valid YAML: {err}") from err


@dataclass(frozen=True)
class DismissAllowlist:
    """The loaded dismiss allowlist: `labels` is what the slow half uses (empty unless `pinned`, choice 1)."""

    labels: frozenset[str] = frozenset()
    file_labels: tuple[str, ...] = ()
    pinned: bool = False


def load_dismiss_allowlist(path: str | Path) -> DismissAllowlist:
    """Read `gateway/dismiss_allowlist.yaml`: absent or unpinned gives the empty allowlist; a pattern or a send-like
    label raises `GatewayConfigError` (L-XREF-18)."""
    p = Path(path)
    if not p.is_file():
        return DismissAllowlist()
    try:
        data = _read(p)
    except ValueError as err:
        raise GatewayConfigError(str(err), rule="L-XREF-18") from err
    labels = data.get("labels") if isinstance(data, Mapping) else None
    if not isinstance(labels, list):
        raise GatewayConfigError(f"{p}: labels must be a list", rule="L-XREF-18")
    problems = [m for lab in labels for m in label_problems(lab)]
    if problems:
        raise GatewayConfigError(f"{p}: " + "; ".join(problems), rule="L-XREF-18")
    pinned = all(data.get(k) for k in PIN_FIELDS)
    return DismissAllowlist(frozenset(labels) if pinned else frozenset(), tuple(labels), pinned)


@dataclass(frozen=True)
class DefaultOrderHotkeys:
    """The pinned build's default order-action chords (5.4.7, 6.5 item 3)."""

    build: str
    verified: bool
    chords: tuple[Chord, ...]
    actions: tuple[str, ...]


def load_default_order_hotkeys(path: str | Path) -> DefaultOrderHotkeys:
    """Read `gateway/default_order_hotkeys.yaml`; missing or invalid raises `GatewayConfigError` (L-XREF-17), whose
    `result` is the fail-closed outcome at `settings_attested` (choice 4)."""
    p = Path(path)
    if not p.is_file():
        raise GatewayConfigError(f"{p} is missing; the default order chords cannot be blocked", rule="L-XREF-17")
    try:
        data = _read(p)
    except ValueError as err:
        raise GatewayConfigError(str(err), rule="L-XREF-17") from err
    entries = data.get("chords") if isinstance(data, Mapping) else None
    if not isinstance(entries, list) or not isinstance(data.get("build"), str):
        raise GatewayConfigError(f"{p}: needs build and a chords list", rule="L-XREF-17")
    problems = chord_problems(entries)
    if problems:
        raise GatewayConfigError(f"{p}: " + "; ".join(m for _, m in problems), rule="L-XREF-17")
    return DefaultOrderHotkeys(data["build"], data.get("verified") is True,
                               tuple(Chord.parse(e["chord"]) for e in entries),
                               tuple(str(e.get("action", "")) for e in entries))


def order_hotkey_list(a6_chords: Iterable[Chord], defaults: DefaultOrderHotkeys) -> tuple[Chord, ...]:
    """The order-hotkey list loaded at `settings_attested` (5.4.7, 6.2 rule 2): every A6 binding, then the pinned
    defaults, without repeats; more than sixteen raises `GatewayConfigError` (the segment holds sixteen)."""
    chords = tuple(dict.fromkeys((*a6_chords, *defaults.chords)))
    if len(chords) > CHORD_SLOTS:
        raise GatewayConfigError(f"{len(chords)} order chords with the A6 bindings; the segment holds {CHORD_SLOTS}",
                                 rule="L-XREF-17")
    return chords
