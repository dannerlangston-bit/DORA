"""`policy/reporting.yaml`: the Reporter's settings (4.16, 9.2, 9.9, 9.10, 9.12; L-XREF-21).

The file holds what 9.9 says is set in configuration: the commit status mode of `sentinel/pr-run` with the date and
the author of every change (9.2), the Jira product and harness project keys, the four Slack channels (on-call,
triage, release and `#sentinel-safety`), the paging service and the digest email recipients (9.10), and the
auto-filing switch per tier, off by default (9.9).

- `load(path)` / `from_dict(data)` validate the file against its 4.16 schema (`lint.schemas`) and against
  `config_problems`, and return a `ReportingConfig`; a file that fails either raises `ReportingConfigError`.
- `config_problems(data, today=)` is the one content check of L-XREF-21 that the loader and the lint
  (`lint/checks/reporting.py`) both apply, as `egress.allowlist_problems` is for L-XREF-19.
- `ReportingConfig.check_mode(on)` is the mode in force on a date: the last dated entry at or before it, and
  `informational` (the Phase 1 default of 9.2) before the first entry. `mode_change(on)` is the entry in force.
- `auto_file_turned_on(base, head)` lists the tiers whose switch a change turns on, the pull-request half of the
  L-XREF-21 auto-file clause (choice 4).

Choices where the spec is ambiguous (reported):

1. "In date order" allows two entries on one date (a switch and a revert the same day): file order then decides,
   and the last entry is the mode in force. The lint (given `today`) refuses a last entry dated after today, since
   the last entry must be the mode in force; the runtime loader takes no `today` and reads a future entry as not
   yet in force. At least one entry is required: the rule's "last entry" has to exist.
2. "Names who changed it": `by` is a non-empty name of a person, never a service identity (`records.NON_HUMAN`):
   the system owner switches modes (9.12), and the Reporter never does.
3. The two Jira keys differ: tickets for the harness triage queue never reach the product backlog (9.9). The safety
   channel differs from the other three: nothing but safety events is posted there (9.9 "the channel is the safety
   record").
4. "An `auto_file` switch turns on only in a pull request the system owner approves" is a property of the pull
   request, which a lint of one tree cannot see. `config_problems` does not check it; `auto_file_turned_on(base,
   head)` gives the tiers a change turns on, for the history lint of a pull request to compare with the PR host's
   approvers (the integrator's wiring, reported).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from ..orchestrator.alerts import SAFETY_CHANNEL
from ..records import NON_HUMAN

DEFAULT_CHECK_MODE = "informational"            # 9.2: Phase 1 and the default
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9]+$")       # L-XREF-21
SLACK_ROLES = ("on_call", "triage", "release", "safety")   # the four channels of 9.9
TIERS = (1, 2, 3)
SCHEMA = "reporting"


class ReportingConfigError(ValueError):
    """`policy/reporting.yaml` breaks its 4.16 schema or L-XREF-21."""


@dataclass(frozen=True)
class ModeChange:
    """One dated entry of `check_mode_history` (9.2): the mode from `since` on, and who switched it."""
    mode: str
    since: date
    by: str


@dataclass(frozen=True)
class ReportingConfig:
    """The validated `policy/reporting.yaml` (4.16)."""
    check_mode_history: tuple[ModeChange, ...]
    jira_product: str
    jira_harness: str
    slack: Mapping[str, str]                   # SLACK_ROLES -> channel
    paging_service: str
    escalation_policy: str | None = None
    email: tuple[str, ...] = ()
    auto_file: Mapping[int, bool] = field(default_factory=lambda: dict.fromkeys(TIERS, False))   # off by default (9.9)

    def mode_change(self, on: date) -> ModeChange | None:
        """The history entry in force on `on`: the last one dated at or before it; None before the first."""
        return next((m for m in reversed(self.check_mode_history) if m.since <= on), None)

    def check_mode(self, on: date) -> str:
        """The commit status mode in force on `on` (9.2); `informational` before the first entry."""
        m = self.mode_change(on)
        return m.mode if m is not None else DEFAULT_CHECK_MODE

    def auto_files(self, tier: int) -> bool:
        """Whether `collateral` of this tier is filed without a human (9.9; off unless the system owner enabled it)."""
        if tier not in TIERS:
            raise ValueError(f"tier {tier!r}; one of {TIERS}")
        return bool(self.auto_file.get(tier, False))

    def channel(self, role: str) -> str:
        """The Slack channel of a 9.9 role: on_call, triage, release or safety."""
        if role not in SLACK_ROLES:
            raise ValueError(f"Slack role {role!r}; one of {SLACK_ROLES}")
        return self.slack[role]


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _history_problems(history: Any, today: date | None) -> list[tuple[str, str]]:
    if not isinstance(history, list):
        return []                                          # the schema reports the shape
    if not history:
        return [("check_mode_history", "check_mode_history records the mode in force: at least one dated entry "
                                       "(9.2)")]
    out: list[tuple[str, str]] = []
    last: date | None = None
    for i, entry in enumerate(history):
        where = f"check_mode_history[{i}]"
        if not isinstance(entry, Mapping):
            continue
        by = entry.get("by")
        if not isinstance(by, str) or by.strip().lower() in NON_HUMAN:
            out.append((f"{where}.by", f"every mode change names the person who made it, not {by!r} (9.2, 9.12)"))
        since = _date(entry.get("since"))
        if since is None:
            out.append((f"{where}.since", f"since {entry.get('since')!r} is not a date (YYYY-MM-DD)"))
            continue
        if last is not None and since < last:
            out.append((f"{where}.since", f"check_mode_history is in date order: {since} comes after {last}"))
        last = since if last is None else max(last, since)
    final = _date(history[-1].get("since")) if isinstance(history[-1], Mapping) else None
    if today is not None and final is not None and final > today:
        out.append((f"check_mode_history[{len(history) - 1}].since",
                    f"the last entry is the mode in force, but it starts {final}, after today ({today})"))
    return out


def config_problems(data: Any, *, today: date | None = None) -> list[tuple[str, str]]:
    """(path, message) for every L-XREF-21 content problem of a `policy/reporting.yaml` document (module
    docstring, choices 1 to 3). Shapes the 4.16 schema rejects are skipped here; `from_dict` reports them."""
    if not isinstance(data, Mapping):
        return [("", "policy/reporting.yaml is a mapping (4.16)")]
    out = _history_problems(data.get("check_mode_history"), today)
    jira = data.get("jira")
    if isinstance(jira, Mapping):
        for k in ("product_project", "harness_project"):
            if isinstance(jira.get(k), str) and not JIRA_KEY.match(jira[k]):
                out.append((f"jira.{k}", f"Jira key {jira[k]!r} does not match {JIRA_KEY.pattern}"))
        if _text(jira.get("product_project")) and jira.get("product_project") == jira.get("harness_project"):
            out.append(("jira.harness_project", "the harness project differs from the product project: harness "
                                                "tickets never reach the product backlog (9.9)"))
    slack = data.get("slack")
    if isinstance(slack, Mapping):
        for role in SLACK_ROLES:
            if role in slack and not _text(slack[role]):
                out.append((f"slack.{role}", f"the {role} channel is set (9.9: four channels)"))
        if isinstance(slack.get("safety"), str) and slack["safety"] != SAFETY_CHANNEL:
            out.append(("slack.safety", f"the safety channel is {SAFETY_CHANNEL}, not {slack['safety']!r} (9.9)"))
        for role in SLACK_ROLES[:3]:
            if _text(slack.get(role)) and slack[role] == slack.get("safety"):
                out.append((f"slack.{role}", f"the {role} channel is not the safety channel: nothing but safety "
                                             "events is posted there (9.9)"))
    return out


def _schema_problems(data: Any) -> list[tuple[str, str]]:
    from ..lint.schemas import validate                     # lazy: the lint package is heavy and imports report
    return validate(SCHEMA, data)


def from_dict(data: Any) -> ReportingConfig:
    """Validate a parsed `policy/reporting.yaml` (schema and `config_problems`) and return its settings."""
    problems = _schema_problems(data) or config_problems(data)
    if problems:
        raise ReportingConfigError("; ".join(f"{p}: {m}" if p else m for p, m in problems))
    auto = data.get("auto_file") or {}
    return ReportingConfig(
        check_mode_history=tuple(ModeChange(e["mode"], date.fromisoformat(e["since"]), e["by"])
                                 for e in data["check_mode_history"]),
        jira_product=data["jira"]["product_project"], jira_harness=data["jira"]["harness_project"],
        slack={r: data["slack"][r] for r in SLACK_ROLES}, paging_service=data["paging"]["service"],
        escalation_policy=data["paging"].get("escalation_policy"), email=tuple(data.get("email") or ()),
        auto_file={t: bool(auto.get(f"tier{t}", False)) for t in TIERS})


def load(path: str | Path) -> ReportingConfig:
    """Read and validate `policy/reporting.yaml` (dates kept as text, as the lint reads them, 4.1)."""
    from ..lint.repo import load_yaml_text
    try:
        data = load_yaml_text(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ReportingConfigError(f"cannot read {path}: {e}") from e
    return from_dict(data)


def auto_file_turned_on(base: Any, head: Any) -> tuple[int, ...]:
    """The tiers whose `auto_file` switch is off in `base` (or absent) and on in `head` (choice 4): the change a
    pull request may make only with the system owner's approval (9.9, L-XREF-21)."""
    def switches(doc: Any) -> Mapping[str, Any]:
        auto = doc.get("auto_file") if isinstance(doc, Mapping) else None
        return auto if isinstance(auto, Mapping) else {}
    b, h = switches(base), switches(head)
    return tuple(t for t in TIERS if h.get(f"tier{t}") is True and b.get(f"tier{t}") is not True)


__all__ = ["DEFAULT_CHECK_MODE", "JIRA_KEY", "ModeChange", "ReportingConfig", "ReportingConfigError", "SLACK_ROLES",
           "TIERS", "auto_file_turned_on", "config_problems", "from_dict", "load"]
