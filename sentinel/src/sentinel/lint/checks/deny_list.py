"""L-XREF-13: the Explorer deny list and the deny controls against the registry and the denied-action test (4.15).

"every `deny: true` control, and every control or surface an entry of `policy/explorer_deny_list.yaml` names (6.8),
exists in the registry; every deny-list entry has at least one detection (window title, label, control or surface)
and its regexes compile; the nightly denied-action test of 6.8 names each entry" (error).

The checks are `orchestrator.explorer.deny_list_problems`, the same function the Orchestrator's loader refuses a bad
file with, so the lint and the run never disagree. "Named by the test": the test's list is
`explorer.denied_action_plan`, which gives every entry one probe per detection (a regex detection through
`explorer.regex_sample`) and every `deny: true` control no entry names its own probe; so an entry is named exactly
when it has a detection and each of its regexes has a text the test can type, which `deny_list_problems` checks with
the same sampler. A `deny: true` control is a registry control by construction.

Choice (reported): a checkout without `policy/explorer_deny_list.yaml` is not reported here (4.1 and 4.16 do not
make the file mandatory; the Explorer exists only in branch A of 6.11); the deny controls are then the test's
whole list.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...orchestrator.explorer import deny_list_problems, registry_index

RULE_IDS = ("L-XREF-13",)


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    """Report L-XREF-13 on `policy/explorer_deny_list.yaml` (module docstring)."""
    doc = repo.one("explorer_deny_list")
    if doc is None or doc.data is None:
        return
    registry = registry_index(d.data for d in repo.of("surface"))
    for path, message in deny_list_problems(doc.data, registry):
        add("L-XREF-13", doc.rel, message, path)
