"""L-XREF-15 beyond rules.py: `phase0/branches.yaml` deadlines, its runtime problems and the files that depend on its
safety items (4.15, 4.16 Phase 0 reads, 6.13, 11.8).

rules.py reports a missing or off-schema file and the dependents it already knew (JAB fields through L-SRF-09, the
frozen provider, the OnDemand account entry, agent fallback under DR-07 B). This module adds:

- Every problem `sentinel.branches.problems` finds, the same check the run's loader refuses: an `undecided` record
  without a `deadline`, a regex that does not compile, two different title regexes, a safety item outside 1 to 10
  or listed twice, item 5 `different` without its `paper:` and `live:` formats.
- An `undecided` record past its `deadline` warns (the 11.8 default is in force; the row's "warns").
- The dismiss allowlist of L-XREF-18 agrees with safety item 10: no label until item 10 is read, and then only
  labels it records as observed (6.13 item 10).
- Each `app_settings` profile agrees with items 3 and 2: `order_confirmation` and `shift_click_send` read `absent`
  exactly when the item is `different` (6.13: A3 and A4 then expect "absent").
- Under item 5 `different`, every account id of `gateway/allowlist.yaml` and of the fixtures matches the recorded
  paperMoney format (6.13: "the lint regex ... replaced"); under branch A rules.py's L-FIX-01 checks the assumed one.

The checks run only on a file that passed its schema, so a shape failure is reported once, by rules.py.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...branches import CONFIRMATION_ITEM, SHIFT_CLICK_ITEM, Branches, parse, problems
from ...orchestrator.attestation import ABSENT
from .. import schemas
from ..repo import Repo
from ..rules import BRANCH_KEYS

RULE_IDS = ("L-XREF-15",)
RULE = RULE_IDS[0]


def _dicts(x: Any) -> list[dict]:
    return [v for v in x if isinstance(v, dict)] if isinstance(x, list) else []


def _dismiss_allowlist(repo: Repo, b: Branches, add: Callable[..., None]) -> None:
    doc = repo.one("dismiss_allowlist")
    labels = doc.data.get("labels") if doc is not None and isinstance(doc.data, dict) else None
    if not isinstance(labels, list) or not labels:
        return
    gw = b.gateway()
    if not gw.dialog_read:
        add(RULE, doc.rel, "labels are listed but phase0/branches.yaml has not recorded safety item 10; until the "
            "dialog is read the dismiss allowlist is empty (6.13 item 10)", "labels")
        return
    for i, label in enumerate(labels):
        if label not in gw.observed_labels:
            add(RULE, doc.rel, f"label {label!r} is not among the dialog labels phase0/branches.yaml records as "
                "observed for safety item 10 (6.13)", f"labels[{i}]")


def _profiles(repo: Repo, b: Branches, add: Callable[..., None]) -> None:
    att = b.attestation()
    rows = (("order_confirmation", att.order_confirmation, CONFIRMATION_ITEM, "A3"),
            ("shift_click_send", att.shift_click_send, SHIFT_CLICK_ITEM, "A4"))
    for doc in repo.of("profile"):
        data = doc.data if isinstance(doc.data, dict) else {}
        if data.get("kind") != "app_settings" or not isinstance(data.get("settings"), dict):
            continue
        for key, expected, item, row in rows:
            got = data["settings"].get(key)
            if (expected == ABSENT) != (got == ABSENT):
                want = "reads absent" if expected == ABSENT else "is not absent"
                add(RULE, doc.rel, f"{key} {got!r} disagrees with safety item {item} of phase0/branches.yaml: {row} "
                    f"{want} on this branch (6.13 item {item})", f"settings.{key}")


def _account_ids(repo: Repo, b: Branches, add: Callable[..., None]) -> None:
    ids = b.identity()
    if not ids.replaced:
        return
    found: list[tuple[str, str, Any]] = []
    al = repo.one("allowlist")
    if al is not None and isinstance(al.data, dict):
        for i, rig in enumerate(_dicts(al.data.get("rigs"))):
            found += [(al.rel, f"rigs[{i}].account_ids[{j}]", a) for j, a in enumerate(rig.get("account_ids") or ())]
    for doc in repo.of("fixture"):
        data = doc.data if isinstance(doc.data, dict) else {}
        found += [(doc.rel, f"accounts[{i}].account_id", a.get("account_id"))
                  for i, a in enumerate(_dicts(data.get("accounts")))]
    for rel, path, aid in found:
        if not (isinstance(aid, str) and ids.paper.fullmatch(aid)):
            add(RULE, rel, f"account id {aid!r} does not match the paperMoney format {ids.paper.pattern} that "
                "phase0/branches.yaml records for safety item 5 (6.13)", path)


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Run the L-XREF-15 checks of the module docstring."""
    doc = repo.one("branches")
    if doc is None or doc.error or not isinstance(doc.data, dict) or schemas.validate("branches", doc.data):
        return                                   # missing, unreadable or off-schema: rules.py reports it
    found = problems(doc.data)
    for path, message in found:
        add(RULE, doc.rel, message, path)
    if found:
        return
    b = parse(doc.data)
    for key in BRANCH_KEYS:
        c = b.choices[key]
        if c.past_deadline(today):
            add(RULE, doc.rel, f"{c.record} {key} is still undecided after its deadline {c.deadline.isoformat()}; "
                f"its default branch {c.resolved} is in force (11.8)", key, "warn")
    _dismiss_allowlist(repo, b, add)
    _profiles(repo, b, add)
    _account_ids(repo, b, add)
