"""L-XREF-21 (4.15): `policy/reporting.yaml` has a dated commit status mode history in date order whose last entry is
the mode in force and whose every entry names who changed it; Jira keys match `^[A-Z][A-Z0-9]+$`; the four Slack
channels are set and `safety` is `#sentinel-safety` (9.9); an `auto_file` switch turns on only in a pull request the
system owner approves (9.9).

The content checks are `report.reporting_config.config_problems`, the same check the Reporter's loader applies;
schema failures of the file report under L-XREF-21 through rules.py. Choices (reported):

1. A missing `policy/reporting.yaml` is an error: the Reporter reads its Slack channels, Jira keys and check mode
   there (9.9: "set in configuration"), as a missing `schedule/windows.yaml` is for L-XREF-24.
2. The auto-file clause needs the pull request's approvers, which one tree does not hold; it is left to the history
   lint of a pull request (`reporting_config.auto_file_turned_on`, module docstring choice 4).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...report.reporting_config import config_problems

RULE_IDS = ("L-XREF-21",)
RULE = "L-XREF-21"
PATH = "policy/reporting.yaml"


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    doc = repo.one("reporting")
    if doc is None:
        add(RULE, PATH, f"{PATH} is missing; the Reporter reads its commit status mode, Jira projects and Slack "
                        "channels there (4.16, 9.9)")
        return
    if doc.error is not None:
        return                                                  # the parse error is reported by rules.py
    for path, message in config_problems(doc.data, today=today):
        add(RULE, doc.rel, message, path)
