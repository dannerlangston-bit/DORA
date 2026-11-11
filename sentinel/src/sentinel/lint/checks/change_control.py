"""L-XREF-29: the reviewer version tuple in force has a passing eval report (3.15, 4.16, 4.15).

The tuple in force is `change_control.tuple_in_force` of the checkout: the model pinned in `phase0/branches.yaml`
with the prompt, schema, few-shot ids, request settings, crop policy and redaction stage version the code and the
config give. It needs `eval/reports/<hex>.yaml` whose `reviewer_version_sha256` is the tuple's hash and whose 3.14
gates all pass on the held-out split (`reviewer.evals.report_problems`). A pull request that changes any element of
the tuple without that report therefore fails this rule on its head. Every eval report in the checkout must also be
consistent: its hash over its `reviewer_version`, and its file name that hash without the prefix. The schema of each
report is checked by the generic 4.16 validation under the same rule id (rules.py `_SCHEMA_OF`).

Choices (reported):

- No tuple is in force while DR-07 is undecided or no model is pinned (`branches.ModelBranches.reviewer_enabled`), or
  when the pin is the simulated run's offline stand-in, which is not a model and has no eval; the rule then checks
  only the consistency of the reports present.
- A `phase0/branches.yaml` or `redaction/anchors.yaml` that cannot be read is reported by its own rules (L-XREF-15,
  the redaction lint); this rule then checks only report consistency.
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from ..repo import Repo

RULE_IDS = ("L-XREF-29",)
RULE = "L-XREF-29"


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Add an L-XREF-29 error for a missing or failing eval report of the tuple in force, and for an inconsistent
    eval report anywhere in the checkout."""
    from ...branches import BranchesError
    from ...capture import RedactionError
    from ...change_control import tuple_in_force
    from ...reviewer.evals import report_path, report_problems

    try:
        t = tuple_in_force(repo.root)
    except (BranchesError, RedactionError, OSError, ValueError):
        t = None
    expected = report_path(t) if t is not None else None
    for doc in repo.of("eval_report"):
        if doc.error is not None or doc.rel == expected:
            continue
        for problem in report_problems(doc.data, None, doc.rel):
            if not problem.startswith("gate "):
                add(RULE, doc.rel, problem)
    if t is None or expected is None:
        return
    found = repo.docs.get(expected)
    if found is None:
        add(RULE, expected, f"the reviewer version tuple in force ({t.model_id}, prompt {t.prompt_sha256[:19]}, tuple "
                            f"{t.sha256[:19]}) has no eval report; a change to the tuple merges only with the eval "
                            "report for the new tuple on the held-out split (3.15, 4.16)")
        return
    if found.error is not None:
        return
    for problem in report_problems(found.data, t, expected):
        add(RULE, expected, problem)
