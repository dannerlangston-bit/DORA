"""L-XREF-23 (4.15): `policy/release_gate.yaml` has unique train names, `default_business_days` and every
`business_days` at least 1, and every `rc_cut_date` earlier than its `ga_date` minus N business days of the XNYS
calendar (7.10), N being the train's `business_days` or the default.

The content checks are `release_gate.config_problems`, the same check `release_gate.from_doc` applies when the
Reporter loads the file; schema failures report under L-XREF-23 through rules.py. Choice (reported): a missing file
is not a violation, since 4.16 and 11.7 row 12 define what happens without it (N = 3 and an RC cut at G-7 are
assumed and printed in the RC report header).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...release_gate import POLICY_PATH, config_problems

RULE_IDS = ("L-XREF-23",)
RULE = "L-XREF-23"


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    doc = repo.one("release_gate")
    if doc is None or doc.error is not None:
        return                                                  # missing: assumed (choice); parse errors: rules.py
    for path, message in config_problems(doc.data):
        add(RULE, doc.rel or POLICY_PATH, message, path)
