"""Model deprecation alarm input (3.15, 10.7).

Two inputs feed the alarm: `model_deprecations.yaml`, where the system owner records each vendor
deprecation notice (the Models API publishes no retirement dates, 10.7), and the daily
`GET /v1/models/{id}` check of every pinned id, where a missing id is an alarm and a `harness_error`
finding on the run (3.15).

Choices where the spec is ambiguous:

- 4.1 lists `model_deprecations.yaml` without a schema. This module reads
  `{schema_version: 1, notices: [{model_id, announced, retires, source?}]}` with ISO dates.
- 10.7's alarms are "at announcement, T-45, T-30 and T-14 until the migration PR merges". Each fires
  once, on the first daily check after the previous one (`since`) up to and including `today`; a
  milestone that falls before the announcement (a notice shorter than 45 days) is skipped; one more
  alarm fires on the retirement date. A notice for an id that is no longer pinned raises nothing, so
  merging the migration PR (which moves the pin) ends the alarms.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from ..records import Finding
from .transport import Transport

ALARM_OFFSETS_DAYS = (45, 30, 14)


@dataclass(frozen=True)
class DeprecationNotice:
    model_id: str
    announced: date
    retires: date
    source: str = ""


@dataclass(frozen=True)
class Alarm:
    model_id: str
    kind: str                            # announced | t_minus_45 | t_minus_30 | t_minus_14 | retired | missing
    due: date
    message: str


def _date(v: Any, what: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ValueError(f"{what}: {v!r} is not an ISO date") from e


def parse_notices(doc: Mapping[str, Any]) -> list[DeprecationNotice]:
    """The notices of a parsed `model_deprecations.yaml`."""
    if doc.get("schema_version") != 1 or not isinstance(doc.get("notices", []), list):
        raise ValueError("model_deprecations.yaml: schema_version 1 and a notices list")
    out = []
    for i, n in enumerate(doc.get("notices", [])):
        if not n.get("model_id"):
            raise ValueError(f"notices[{i}]: model_id is required")
        notice = DeprecationNotice(str(n["model_id"]), _date(n.get("announced"), f"notices[{i}].announced"),
                                   _date(n.get("retires"), f"notices[{i}].retires"), str(n.get("source", "")))
        if notice.retires < notice.announced:
            raise ValueError(f"notices[{i}]: retires before it was announced")
        out.append(notice)
    return out


def alarms_due(notices: Iterable[DeprecationNotice], pinned: Iterable[str], *, since: date | None,
               today: date) -> list[Alarm]:
    """Alarms whose date falls in (since, today] for notices on pinned ids (every one up to today
    when `since` is None)."""
    pins = set(pinned)
    out = []
    for n in notices:
        if n.model_id not in pins:
            continue
        milestones = [("announced", n.announced)]
        milestones += [(f"t_minus_{d}", n.retires - timedelta(days=d)) for d in ALARM_OFFSETS_DAYS
                       if n.retires - timedelta(days=d) > n.announced]
        milestones.append(("retired", n.retires))
        for kind, due in milestones:
            if (since is None or due > since) and due <= today:
                out.append(Alarm(n.model_id, kind, due, f"pinned model {n.model_id} retires {n.retires.isoformat()}"
                                 f" ({kind}); open the migration ticket, the replacement must pass every eval gate"))
    return sorted(out, key=lambda a: (a.due, a.model_id, a.kind))


def model_check(transport: Transport, pinned: Iterable[str], *, run_id: str,
                today: date) -> tuple[list[Alarm], list[Finding]]:
    """The daily Models API check: an alarm and a `harness_error` finding per pinned id not served."""
    alarms, findings = [], []
    for model_id in sorted(set(pinned)):
        if transport.retrieve_model(model_id):
            continue
        msg = f"pinned model {model_id} is no longer served by the Models API (3.15)"
        alarms.append(Alarm(model_id, "missing", today, msg))
        findings.append(Finding(finding_id=f"fnd-{run_id}-model-{len(findings) + 1:03d}", run_id=run_id,
                                kind="harness_error", description=msg, first_seen_run_id=run_id,
                                detail="pinned_model_missing"))
    return alarms, findings
