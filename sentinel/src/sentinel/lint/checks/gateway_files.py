"""L-XREF-17 and L-XREF-18: the gateway files of 4.16 (5.4.3, 5.4.7, 6.2, 6.13 item 10).

- L-XREF-17: `gateway/default_order_hotkeys.yaml` exists (a missing file fails closed at `settings_attested`); every
  `chord` parses as modifiers (Ctrl, Alt, Shift, Win) plus one key; no chord repeats; at most sixteen chords (the
  segment's chord table, 5.4.2); `verified: false` warns from Phase 1.
- L-XREF-18: `gateway/dismiss_allowlist.yaml` `labels` are exact strings, never patterns, and none is send-like;
  `Edit` appears only when `phase0/branches.yaml` records safety item 10 with an observed `Edit` control; an empty
  list is valid. Labels without the Phase 0 read recorded (`read_on_build`, `read_by`, `read_at`) warn, because the
  gateway ignores them until that record exists (gateway_config choice 1).

The label and chord checks are `gateway_config.label_problems` and `gateway_config.chord_problems`, the same code the
runtime loaders apply. Schema failures of both files are reported under the same rule ids by rules.py.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...orchestrator.gateway_config import (DEFAULT_ORDER_HOTKEYS_PATH, DISMISS_ALLOWLIST_PATH, PIN_FIELDS,
                                            chord_problems, label_problems)
from ..repo import Repo

RULE_IDS = ("L-XREF-17", "L-XREF-18")
EDIT_LABEL = "Edit"
DIALOG_ITEM = 10                     # 6.13 item 10: the dialog's labels and titles


def _observed_labels(repo: Repo) -> set[str]:
    """The labels `phase0/branches.yaml` records as observed for safety item 10."""
    doc = repo.one("branches")
    data = doc.data if doc is not None and isinstance(doc.data, dict) else {}
    out: set[str] = set()
    for item in data.get("safety") or ():
        if isinstance(item, dict) and item.get("item") == DIALOG_ITEM:
            out |= {str(x) for x in item.get("observed") or ()}
    return out


def _hotkeys(repo: Repo, add: Callable[..., None], phase: int) -> None:
    doc = repo.one("default_order_hotkeys")
    if doc is None:
        add("L-XREF-17", DEFAULT_ORDER_HOTKEYS_PATH, "gateway/default_order_hotkeys.yaml is missing; a missing file "
            "fails closed at settings_attested (5.4.7)")
        return
    data: Any = doc.data
    if doc.error or not isinstance(data, dict):
        return                                   # the parse or schema failure is reported by rules.py
    entries = data.get("chords") if isinstance(data.get("chords"), list) else []
    for i, message in chord_problems(entries):
        add("L-XREF-17", doc.rel, message, f"chords[{i}].chord")
    if data.get("verified") is not True and phase >= 1:
        add("L-XREF-17", doc.rel, "verified: false: the chords are placeholders until the Phase 0 read of the pinned "
            "build's default hotkey table", "verified", "warn")


def _allowlist(repo: Repo, add: Callable[..., None]) -> None:
    doc = repo.one("dismiss_allowlist")
    if doc is None or doc.error or not isinstance(doc.data, dict) or not isinstance(doc.data.get("labels"), list):
        return                                   # absent is the empty allowlist; shape failures are rules.py's
    data = doc.data
    labels = data["labels"]
    for i, label in enumerate(labels):
        for message in label_problems(label):
            add("L-XREF-18", doc.rel, message, f"labels[{i}]")
    if EDIT_LABEL in labels and EDIT_LABEL not in _observed_labels(repo):
        add("L-XREF-18", doc.rel, "Edit is allowlisted but phase0/branches.yaml does not record an observed Edit "
            "control for safety item 10 (6.13)", f"labels[{labels.index(EDIT_LABEL)}]")
    if labels and not all(data.get(k) for k in PIN_FIELDS):
        add("L-XREF-18", doc.rel or DISMISS_ALLOWLIST_PATH, "labels without the Phase 0 read recorded (read_on_build, "
            "read_by, read_at) are ignored by the gateway, so no dialog click is tokened", "labels", "warn")


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Run L-XREF-17 and L-XREF-18 over the repository (module docstring)."""
    _hotkeys(repo, add, phase)
    _allowlist(repo, add)
