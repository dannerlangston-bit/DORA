"""The credential rotation calendar and the run hold (7.7 Credential rotation calendar, 6.9, 4.16; SD-74).

`ops/credential_rotation.yaml` in `sentinel-config` lists every login a rig uses with its owner, backup owner and,
under DR-03 branch B, the team-owned SMS device, and each rotation of a password or vault token with its window, its
owner and the login-only canary run that validated it (6.9). "A run scheduled inside a rotation window is held, not
started, and the hold is a run health line" (7.7).

- `RotationCalendar.from_doc` reads the file; `calendar_problems` is the L-XREF-25 check `lint/checks/windows.py`
  reports, and `from_doc` refuses the same problems.
- `in_rotation_window(now, calendar, login)` is the queue's check before a run starts on a rig: a `RotationHold` with
  its run-health line while a rotation window of that rig's login is open, else None.
- `pending_validation(calendar, now)` lists the rotations whose window has ended without a validating run: the
  queue schedules the login-only canary run of 6.9 for each.

Choices where the spec is ambiguous (reported):

1. A window is half open, [start, end): a run due at the end minute starts. Window times carry a UTC offset (4.16
   `ts`); a time without one is refused.
2. The lint judges "a rotation whose window has ended" against the start of `today` in America/New_York (the lint
   runs on a date); `pending_validation` uses the exact instant.
3. A backup owner equal to the owner is no backup (7.7 "a named owner, a backup owner"), and a login has one entry.
4. While DR-03 is undecided its 11.8 default (branch B) is in force (`sentinel.branches`), so every login needs its
   SMS device until branch A is recorded.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from .calendar import Span, fmt_ny, to_utc, wall_time

ROTATION_KINDS = ("password", "vault_token")            # 4.16 credential_rotation.rotations[].kind


class RotationCalendarError(ValueError):
    """`ops/credential_rotation.yaml` cannot drive the hold; `problems` lists (path, message) as L-XREF-25 reports."""

    def __init__(self, problems: Iterable[tuple[str, str]]):
        self.problems = tuple(problems)
        super().__init__("ops/credential_rotation.yaml: " + "; ".join(f"{p or '(root)'}: {m}" for p, m in self.problems))


@dataclass(frozen=True)
class LoginOwners:
    """One login's owners (7.7): `login_ref` as in `gateway/allowlist.yaml`."""

    login_ref: str
    owner: str
    backup_owner: str
    sms_device: str | None = None


@dataclass(frozen=True)
class Rotation:
    """One rotation of a login's password or vault token, inside `window`."""

    login_ref: str
    kind: str
    window: Span
    owner: str
    validating_run_id: str | None = None


@dataclass(frozen=True)
class RotationHold:
    """A run not started because its login is inside a rotation window (7.7); `health_line` is the run-health line."""

    login_ref: str
    rotation: Rotation
    at: datetime

    @property
    def until(self) -> datetime:
        """When the hold lifts: the end of the rotation window."""
        return self.rotation.window.end

    def health_line(self) -> str:
        r = self.rotation
        return (f"run held at {fmt_ny(self.at)}: {r.kind} rotation of {self.login_ref} by {r.owner} in window "
                f"{r.window}; not started until the window ends (7.7)")


@dataclass(frozen=True)
class RotationCalendar:
    """`ops/credential_rotation.yaml` (4.16, 7.7)."""

    logins: tuple[LoginOwners, ...]
    rotations: tuple[Rotation, ...]

    @classmethod
    def from_doc(cls, doc: Any) -> "RotationCalendar":
        """Read the loaded YAML; raises `RotationCalendarError` on a problem of `calendar_problems` that does not
        depend on the rigs, the branch or the date."""
        found = calendar_problems(doc)
        if found:
            raise RotationCalendarError(found)
        logins = tuple(LoginOwners(x["login_ref"], x["owner"], x["backup_owner"], x.get("sms_device"))
                       for x in doc.get("logins") or ())
        rotations = tuple(Rotation(r["login_ref"], r["kind"], _window(r["window"]), r["owner"],
                                   r.get("validating_run_id")) for r in doc.get("rotations") or ())
        return cls(logins, rotations)


def _ts(value: Any) -> datetime:
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return to_utc(when)


def _window(w: Mapping[str, Any]) -> Span:
    return Span(_ts(w["start"]), _ts(w["end"]))


def calendar_problems(doc: Any, *, rig_logins: Iterable[str] | None = None, branch_b: bool = False,
                      today: date | None = None) -> list[tuple[str, str]]:
    """L-XREF-25 (4.15): every login in `rig_logins` (the `login_ref` of each rig of `gateway/allowlist.yaml`) has an
    entry with an owner and a different backup owner, plus an SMS device under DR-03 branch B; one entry per login;
    every rotation names a listed login, a known kind and a window that ends after it starts; with `today`, a rotation
    whose window ended before `today` (New York) carries `validating_run_id` (choices 1 to 4)."""
    if not isinstance(doc, Mapping):
        return [("", "ops/credential_rotation.yaml is not a mapping")]
    out: list[tuple[str, str]] = []
    listed: dict[str, int] = {}
    for i, x in enumerate(doc.get("logins") or ()):
        if not isinstance(x, Mapping):
            continue
        at, ref = f"logins[{i}]", x.get("login_ref")
        if not isinstance(ref, str) or not ref:
            out.append((f"{at}.login_ref", "every entry names its login_ref, as in gateway/allowlist.yaml"))
            continue
        if ref in listed:
            out.append((f"{at}.login_ref", f"{ref} already has an entry at logins[{listed[ref]}]; one per login"))
            continue
        listed[ref] = i
        if not x.get("owner") or not x.get("backup_owner"):
            out.append((at, f"{ref} needs a named owner and a backup owner (7.7)"))
        elif x["owner"] == x["backup_owner"]:
            out.append((f"{at}.backup_owner", f"{ref}: the backup owner is the owner; name another person (7.7)"))
        if branch_b and not x.get("sms_device"):
            out.append((f"{at}.sms_device", f"{ref} needs the team-owned SMS device under DR-03 branch B (7.7)"))
    for ref in sorted(set(rig_logins or ()) - set(listed)):
        out.append(("logins", f"{ref} is used by a rig of gateway/allowlist.yaml but has no owner entry (7.7)"))
    cutoff = wall_time(today, time(0)) if today is not None else None
    for i, r in enumerate(doc.get("rotations") or ()):
        if not isinstance(r, Mapping):
            continue
        at = f"rotations[{i}]"
        if r.get("login_ref") not in listed:
            out.append((f"{at}.login_ref", f"{r.get('login_ref')} is not a listed login"))
        if r.get("kind") not in ROTATION_KINDS:
            out.append((f"{at}.kind", f"{r.get('kind')!r} is not one of {', '.join(ROTATION_KINDS)}"))
        if not isinstance(r.get("owner"), str) or not r["owner"]:
            out.append((f"{at}.owner", "every rotation names its owner (7.7)"))
        w = r.get("window")
        try:
            span = _window(w) if isinstance(w, Mapping) else None
        except (TypeError, ValueError) as exc:
            out.append((f"{at}.window", f"the window needs two timestamps with a UTC offset: {exc}"))
            continue
        if span is None:
            out.append((f"{at}.window", "the window {start, end} is required"))
            continue
        if span.end <= span.start:
            out.append((f"{at}.window", "the window ends before it starts"))
        elif cutoff is not None and span.end <= cutoff and not r.get("validating_run_id"):
            out.append((f"{at}.validating_run_id", f"the window ended {fmt_ny(span.end)}; name the login-only canary "
                                                   "run that validated the rotation (6.9)"))
    return out


def in_rotation_window(now: datetime, calendar: RotationCalendar, login: str) -> RotationHold | None:
    """The hold for a run of `login` due at `now`, or None when no rotation window of that login is open (7.7)."""
    t = to_utc(now)
    for r in calendar.rotations:
        if r.login_ref == login and t in r.window:
            return RotationHold(login, r, t)
    return None


def pending_validation(calendar: RotationCalendar, now: datetime) -> tuple[Rotation, ...]:
    """Rotations whose window has ended by `now` with no validating run: each needs the login-only canary (6.9)."""
    t = to_utc(now)
    return tuple(r for r in calendar.rotations if r.window.end <= t and not r.validating_run_id)
