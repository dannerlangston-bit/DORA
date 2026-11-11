"""L-RGN-05, the SD-24 half: a floor exception lasts at most 30 days from its approval (3.9 step 5, 4.8).

4.8 `floor_exception` carries `approved_by`, `approved_at`, `owner`, `reason`, `ticket`, `measured_floor` and
`expires_at` (the schema requires them, L-RGN-00); SD-24 adds to L-RGN-05 that `expires_at` is at most 30 days after
`approved_at` (3.9: "a human-approved floor_exception (owner, reason, 30-day expiry, measured floor)"). rules.py
checks the human fields, the future expiry and the `diff` type; this module checks the cap: an `expires_at` later
than `approved_at` plus 30 days, or not after `approved_at`, is an error. An unreadable timestamp is left to the
schema (L-RGN-00) and to rules.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

RULE_IDS = ("L-RGN-05",)
MAX_DAYS = 30                                       # SD-24, 3.9 step 5


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        t: datetime | None = v
    else:
        try:
            t = datetime.fromisoformat(v) if isinstance(v, str) else None
        except ValueError:
            return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    """Report every floor exception whose expiry is more than 30 days after, or not after, its approval."""
    for doc in repo.of("regions"):
        regions = doc.data.get("regions") if isinstance(doc.data, dict) else None
        for i, x in enumerate(regions if isinstance(regions, list) else []):
            fe = x.get("floor_exception") if isinstance(x, dict) else None
            if not isinstance(fe, dict):
                continue
            approved, expires = _ts(fe.get("approved_at")), _ts(fe.get("expires_at"))
            if approved is None or expires is None:
                continue
            at = f"regions[{i}].floor_exception.expires_at"
            if expires <= approved:
                add("L-RGN-05", doc.rel, f"floor_exception on {x.get('id')} expires at {fe.get('expires_at')}, not after "
                                         f"its approval at {fe.get('approved_at')}", at)
            elif expires - approved > timedelta(days=MAX_DAYS):
                add("L-RGN-05", doc.rel, f"floor_exception on {x.get('id')} expires {(expires - approved).days} days after "
                                         f"its approval; the most is {MAX_DAYS} days (SD-24, 3.9)", at)
