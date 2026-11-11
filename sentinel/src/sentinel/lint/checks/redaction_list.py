"""The redaction list is required (6.10 Stage 1, 4.1, 4.8 Redaction list): `redaction/anchors.yaml` exists and has
at least one region with `identity_field: true`, the boxes over the account number and the account holder name that
"are always applied". Without the file Capture would run with no anchored box at all, under a redaction version that
still looks valid, so a config without it is refused (the run-start lint then aborts the run `config_invalid`).

Choices (reported):

1. The rule id is L-MSK-03, the row the lint already reports `redaction/anchors.yaml` under when it does not read
   or validate against its schema (`rules._SCHEMA_OF`); 4.15 has no row of its own for the file being present.
2. The identity check counts regions of `type: redaction` whose `identity_field` is exactly true. Whether those
   regions cover every identity field of 6.10's list (account selector, title bar, ticket and confirmation account
   fields, Account Info, Position Statement header, login username) needs the screens and is the nightly redaction
   canary's proof, not the lint's.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping

RULE_IDS = ("L-MSK-03",)
RULE = "L-MSK-03"
FILE = "redaction/anchors.yaml"


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    doc = repo.one("redaction")
    if doc is None:
        add(RULE, FILE, f"{FILE} is missing: Capture's stage-1 redaction list is required (6.10)")
        return
    data = doc.data if isinstance(doc.data, Mapping) else {}
    regions = data.get("regions") if isinstance(data.get("regions"), list) else []
    if not any(isinstance(r, Mapping) and r.get("type") == "redaction" and r.get("identity_field") is True
               for r in regions):
        add(RULE, doc.rel, "no region has identity_field: true; the account number and holder name boxes are "
                           "always applied (6.10 stage 1)", "regions")
