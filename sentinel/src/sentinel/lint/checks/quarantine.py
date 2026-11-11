"""L-XREF-11 on `quarantine.yaml` flow records (4.15, 4.16, 10.5, SD-96).

"A quarantine record names exactly one checkpoint or one flow; its reason is one of the quarantine reasons of 10.5"
(4.16), and "lint rule L-XREF-11 checks that the checkpoint or flow exists" (10.5). The schema (`quarantine_record`,
validated under L-XREF-11 by rules.py) refuses a reason outside `quarantine_reasons` and a malformed field; rules.py
checks each `checkpoint` against the 4.1 grammar and the registry. This module adds what neither does:

- a record with both `checkpoint` and `flow`, or with neither, is refused;
- a `flow` must name a flow of the library (`flows/**`, by `flow_id`), and not one under `flows/quarantine/` (those
  are Explorer candidates, 4.7 L-FLW-14, never scheduled, so quarantining one quarantines nothing);
- the same checkpoint or flow quarantined twice is refused (one record per target, so removing it un-quarantines).

Choice (reported): the duplicate-target check is read into "exactly one" (10.5: the record is removed by the PR that
cites the fix; two records would leave the target quarantined after that PR).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

RULE_IDS = ("L-XREF-11",)


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    """Report L-XREF-11 on the records of `quarantine.yaml` (module docstring)."""
    doc = repo.one("quarantine")
    if doc is None or not isinstance(doc.data, dict):
        return
    flows = {d.data.get("flow_id"): d.rel for d in repo.of("flow") if isinstance(d.data, dict)}
    seen: dict[str, int] = {}
    for i, rec in enumerate(doc.data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        at = f"records[{i}]"
        cid, fid = rec.get("checkpoint"), rec.get("flow")
        if (cid is None) == (fid is None):
            add("L-XREF-11", doc.rel, "a quarantine record names exactly one of checkpoint and flow (10.5)", at)
            continue
        if fid is not None:
            if fid not in flows:
                add("L-XREF-11", doc.rel, f"quarantined flow {fid} is not in the flow library", f"{at}.flow")
            elif str(flows[fid]).startswith("flows/quarantine/"):
                add("L-XREF-11", doc.rel, f"{fid} is an Explorer candidate under flows/quarantine/, never scheduled; "
                                          "there is nothing to quarantine", f"{at}.flow")
        target = str(cid if cid is not None else fid)
        if target in seen:
            add("L-XREF-11", doc.rel, f"{target} is already quarantined by records[{seen[target]}]; one record per "
                                      "checkpoint or flow", at)
        else:
            seen[target] = i
