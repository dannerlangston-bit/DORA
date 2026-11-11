"""The Reporter's alert dispatcher (9.10 Alerting model; 9.9 Dedup; 10.1 "one alert per run per gate"; 11.6; SD-62,
SD-84, SD-85).

The Orchestrator's controls each carry a 9.10 route (`orchestrator.alerts`); a run writes one entry per control
result and shard (`pipeline.orchestration.alert_entry`). This module turns them into what people receive:

- `raise_run_alerts(run, entries)` collapses a run's entries to one `Alert` per run, rule and reason (the gate for
  `env_error`: 10.1's one alert per run per gate), the priority the highest and the recipients the union of the
  entries, with the shards listed and each shard's detail kept for the thread.
- `escalate_repeats(store, run, alerts)` is the repeat detector of 9.10 and 11.6: an `env_error` on a gate that also
  raised `env_error` in the previous run of the same type, and a `harness_error` after a `harness_error` in that
  run, become P1. `harness_freeze(store, now)` sets the `harness_freeze` key of `system_state` at the second
  `aborted_harness` run within 7 days (9.10 harness_error row, 1.5).
- `Dispatcher(store, ...).dispatch(alert, now)` sends one alert through the outbox (9.1): a `page` job to the paging
  service for the route's paged recipients; for P1 an email in parallel (a `page` job to `EMAIL_DESTINATION`, see
  choice 4) and the Slack posts; for P2 the Slack posts mentioning the rotation; P3 waits for the daily digest
  (`report.digest`). Between 22:00 and 06:00 America/New_York only the `AFTER_HOURS_RULES` page and post at once;
  every other alert's jobs are held to 06:00 (`next_attempt_at`). `pool_saturated` is sent at most once an hour.
  Slack posts thread by run and alert kind (`report.slack.thread_key`), with one reply per shard detail.
- `Dispatcher.check_undelivered(now)`: a dispatch with a page that no channel accepted (no job `sent`) within 5
  minutes of its due time writes `alert_undelivered` to `system_state` (shown on the run list page).
- `record_ack(store, run_id, key, by=..., at=...)` mirrors an acknowledgement from the paging service into the run's
  `summary_json` (`alert_acks`) through the writer, for the Alerts panel.
- `Pager` and `Mailer` are the protocols the outbox sender calls for `page` jobs (`send_page_job`); `MemoryPager`
  and `MemoryMailer` are the in-memory fakes until the paging service is named (Phase 0, 11.8).

Choices where the spec is ambiguous (each is reported):

1. Collapse key: rule plus reason, the reason being the gate for `env_error` and the run reason otherwise (an
   `unproven_proof` entry has none, so a run has one UNPROVEN alert). The status field of the message is
   `env_error (gate: <gate>)`, `<run outcome> (reason: <reason>)`, or the gate. The detail is the first distinct
   detail, with "(+N more)" when shards differ, then the shards (`shards 1, 2, 3, 4`, the part after the run id).
2. Paging: everyone a route lists as `paged` is paged, at any priority (SD-62: an `authentication_failed` P2 pages
   the login owner and the accounts owner); P1 adds the email and posts in parallel; P2 posts and mentions the
   rotation (the harness on-call in the on-call channel, the triage rotation in triage) and the route's notified
   people. Recipients are named by role in the jobs, with the login of each role the roster names on the day.
3. After hours (9.10, 11.6, SD-84): the operating day comes from `schedule/windows.yaml` (default 06:00 to 22:00);
   outside it `canary_failure` pages at once only at P1 (`detector_blind`, `generation_failed`), and an alert held
   to 06:00 keeps its pages for 06:00.
4. Email has no outbox kind (8.3 lists none), so the P1 email is a `page` job with destination `email`; the sender
   routes it to the `Mailer` (`send_page_job`). An `email` outbox kind would make this explicit (reported).
5. Undelivered: the 5 minutes count from the dispatch's due time (06:00 for a held alert). The `system_state` value
   is a JSON list of `{key, rule, run_id, due_at, detected_at}`, appended to. The dispatcher keeps its pending
   pages, thread parents and rate limits in memory, like the run queue (a control-host restart forgets them).
6. Repeats: "the previous run" is the run of the same type that started last before this one; its gates come from
   its `summary_json` alerts and its `env_error` outcome rows, its `harness_error` from either too. The freeze is
   set once (a human clears it after the root cause); runs count by `started_at` within the 7 days up to `now`.
7. A safety-table alert names the run's `incidents` rows (`incident 12 (S1)`) in its detail; the link to the
   incident row is the run's report link, whose Safety proof section lists the open incidents (9.4 item 8).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from .. import calendar as cal
from ..orchestrator.alerts import CHANNELS, P1, P2, P3, PRIORITIES, TRIAGE_CHANNEL, AlertRoute
from ..sla import OnCall, iso
from ..store import Store
from . import slack
from .inline import Urls

UNDELIVERED_AFTER = timedelta(minutes=5)       # 9.10: a page no channel accepts within 5 minutes
RATE_LIMITS = {"pool_saturated": timedelta(hours=1)}   # 9.10, 11.6: one per hour
FREEZE_WINDOW = timedelta(days=7)              # 9.10: two aborted_harness in a week
FREEZE_COUNT = 2
EMAIL_DESTINATION = "email"                    # choice 4
UNDELIVERED_KEY = "alert_undelivered"          # 9.10 system_state key
FREEZE_KEY = "harness_freeze"
ACKS_KEY = "alert_acks"                        # run summary_json key the Alerts panel reads
AFTER_HOURS_RULES = frozenset({                # 9.10 preamble, 11.6, SD-84: these page between 22:00 and 06:00
    "safety:aborted_safety", "aborted_harness", "gateway_safety_drop", "safety:explorer_order_stage",
    "safety:drill_failure", "safety:unproven_proof", "canary_path_divergence", "redaction_miss", "dead_man"})
ALERT_NAMES = {                                # the 9.10 "Alert" names printed in messages
    "safety:aborted_safety": "aborted_safety", "safety:explorer_order_stage": "explorer_order_stage",
    "safety:unproven_proof": "UNPROVEN post-run proof", "safety:drill_failure": "fire drill failure",
    "dead_man": "dead-man", "preflight_or_cost": "preflight failure or cost alarm",
    "new_collateral": "new high-confidence collateral", "sla_escalation": "SLA escalation",
    "needs_human_digest": "needs_human daily digest", "nightly_skipped_for_rc": "Nightly skipped for RC",
    "rc_start_deadline": "RC start deadline", "aa_drift": "A/A drift"}
_INCIDENT_ORDER = ("S1", "S2", "S3")
_HANDOVER = ("account", "working_orders_screenshot", "instruction")   # the 6.6 Cancel All failure hand-over


def alert_name(rule: str) -> str:
    """The 9.10 name of a rule, as messages print it."""
    return ALERT_NAMES.get(rule, rule.removeprefix("safety:"))


def pages_after_hours(route: AlertRoute) -> bool:
    """True for an alert that pages between 22:00 and 06:00 America/New_York (9.10, 11.6, SD-84; choice 3)."""
    return route.rule in AFTER_HOURS_RULES or (route.rule == "canary_failure" and route.priority == P1)


def _merge(a: tuple[str, ...], b: Iterable[str]) -> tuple[str, ...]:
    return a + tuple(x for x in dict.fromkeys(b) if x not in a)


# -- the run and its alerts ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRef:
    """The run facts every alert message prints (9.10 format)."""

    run_id: str
    run_type: str
    build_pair: str | None = None
    started_at: str | None = None
    base_url: str = "https://sentinel.internal"

    @classmethod
    def from_store(cls, store: Store, run_id: str, *, base_url: str = "https://sentinel.internal") -> "RunRef":
        """The run row and its build pair (`<candidate> vs <base>`)."""
        r = store.query("SELECT r.run_type, r.started_at, p.candidate_build_id, p.base_build_id FROM runs r "
                        "LEFT JOIN build_pairs p ON p.pair_id = r.pair_id WHERE r.run_id = ?", (run_id,))
        if not r:
            raise ValueError(f"unknown run {run_id}")
        pair = f"{r[0]['candidate_build_id']} vs {r[0]['base_build_id']}" if r[0]["candidate_build_id"] else None
        return cls(run_id, r[0]["run_type"], pair, r[0]["started_at"], base_url)

    @property
    def report_link(self) -> str:
        return Urls(self.base_url.rstrip("/"), self.run_id).report


@dataclass(frozen=True)
class Alert:
    """One alert to dispatch: a run, rule and reason with its merged 9.10 route (choice 1)."""

    route: AlertRoute
    key: str
    detail: str
    status: str | None = None
    run_id: str | None = None
    run_type: str | None = None
    build_pair: str | None = None
    report_link: str | None = None
    reason: str | None = None
    shards: tuple[str, ...] = ()
    shard_details: tuple[tuple[str, str], ...] = ()
    incident: str | None = None
    control: str | None = None
    gate: str | None = None
    run_outcome: str | None = None
    run_reason: str | None = None
    tier: int | None = None
    surface: str | None = None
    crop_link: str | None = None
    people: Mapping[str, str] = field(default_factory=dict)     # recipient -> login, given by the caller
    handover: Mapping[str, Any] = field(default_factory=dict)    # 6.6 account, Working Orders screenshot, instruction

    @property
    def rule(self) -> str:
        return self.route.rule

    @property
    def priority(self) -> str:
        return self.route.priority

    @classmethod
    def of(cls, route: AlertRoute, *, detail: str, key: str, run: RunRef | None = None, status: str | None = None,
           tier: int | None = None, people: Mapping[str, str] | None = None) -> "Alert":
        """An alert that no run's control results raised: an SLA escalation, the queue's pool saturated, ..."""
        return cls(route, key, detail, status, run.run_id if run else None, run.run_type if run else None,
                   run.build_pair if run else None, run.report_link if run else None, tier=tier,
                   people=dict(people or {}))

    def fields(self, detail: str | None = None) -> slack.MessageFields:
        """The 9.10 message fields; `detail` replaces the alert's own (a shard's reply)."""
        return slack.MessageFields(self.priority, alert_name(self.rule), self.run_type, self.run_id, self.build_pair,
                                   self.status, self.detail if detail is None else detail, self.report_link,
                                   self.surface, self.crop_link)

    def entry(self) -> dict[str, Any]:
        """The alert in the `alerts.json` and summary `alerts` shape (`pipeline.orchestration.alert_entry`, which the
        Alerts panel prints), plus its key and shards."""
        out = {"run_id": self.run_id, "shard_id": None, "shards": list(self.shards), "key": self.key,
               "rule": self.rule, "priority": self.priority, "paged": list(self.route.paged),
               "notified": list(self.route.notified), "safety_table": self.route.is_safety, "control": self.control,
               "gate": self.gate, "incident": self.incident, "run_outcome": self.run_outcome,
               "run_reason": self.run_reason, "detail": self.detail}
        out.update(self.handover)
        return out


def _reason(e: Mapping[str, Any]) -> str | None:
    return e.get("gate") if e["rule"] == "env_error" else e.get("run_reason")


def _status(e: Mapping[str, Any]) -> str | None:
    if e["rule"] == "env_error" and e.get("gate"):
        return f"env_error (gate: {e['gate']})"
    if e.get("run_outcome"):
        return f"{e['run_outcome']} (reason: {e['run_reason']})" if e.get("run_reason") else e["run_outcome"]
    return e.get("gate")


def _shard_name(run_id: str, shard: str) -> str:
    return shard[len(run_id) + 1:] if shard.startswith(run_id + "/") else shard


def raise_run_alerts(run: RunRef, entries: Iterable[Mapping[str, Any]]) -> tuple[Alert, ...]:
    """One alert per run, rule and reason from a run's alert entries, in first-seen order (choice 1). Entries without
    a rule (the 10.2 late-arrival closure) are not alerts and are skipped."""
    groups: dict[tuple[str, str | None], list[Mapping[str, Any]]] = {}
    for e in entries:
        if "rule" in e:
            groups.setdefault((e["rule"], _reason(e)), []).append(e)
    out = []
    for (rule, reason), es in groups.items():
        first = es[0]
        prio = min((e["priority"] for e in es), key=PRIORITIES.index)
        paged: tuple[str, ...] = ()
        notified: tuple[str, ...] = ()
        for e in es:
            paged, notified = _merge(paged, e.get("paged") or ()), _merge(notified, e.get("notified") or ())
        shards = tuple(sorted({e["shard_id"] for e in es if e.get("shard_id")}))
        details = list(dict.fromkeys(str(e.get("detail") or "") for e in es if e.get("detail")))
        detail = (details[0] if details else "") + (f" (+{len(details) - 1} more)" if len(details) > 1 else "")
        if shards:
            names = ", ".join(_shard_name(run.run_id, s) for s in shards)
            detail = f"{detail}; shard{'s' if len(shards) > 1 else ''} {names}".lstrip("; ")
        incidents = [e["incident"] for e in es if e.get("incident") in _INCIDENT_ORDER]
        handover = next(({k: e[k] for k in _HANDOVER if e.get(k)} for e in es if any(e.get(k) for k in _HANDOVER)),
                        {})
        out.append(Alert(AlertRoute(rule, prio, paged, notified), f"{run.run_id}:{rule}:{reason or '-'}", detail,
                         _status(first), run.run_id, run.run_type, run.build_pair, run.report_link, reason, shards,
                         tuple((_shard_name(run.run_id, e["shard_id"]), str(e.get("detail") or ""))
                               for e in es if e.get("shard_id")),
                         min(incidents, key=_INCIDENT_ORDER.index) if incidents else None, first.get("control"),
                         first.get("gate"), first.get("run_outcome"), first.get("run_reason"), handover=handover))
    return tuple(out)


# -- repeats and the harness freeze (9.10, 11.6; choice 6) ----------------------------------------------------------

def previous_run(store: Store, run: RunRef) -> str | None:
    """The run of the same type that started last before `run` (choice 6)."""
    started = run.started_at
    if started is None:
        row = store.query("SELECT started_at FROM runs WHERE run_id = ?", (run.run_id,))
        started = row[0][0] if row else None
    if started is None:
        return None
    r = store.query("SELECT run_id FROM runs WHERE run_type = ? AND run_id <> ? AND started_at < ? "
                    "ORDER BY started_at DESC, run_id DESC LIMIT 1", (run.run_type, run.run_id, started))
    return r[0][0] if r else None


def _previous_failures(store: Store, run_id: str) -> tuple[frozenset[str], bool]:
    """The env_error gates and whether a harness_error was raised in `run_id`."""
    alerts_ = store.run_summary(run_id).get("alerts") or ()
    gates = {a["gate"] for a in alerts_ if a.get("rule") == "env_error" and a.get("gate")}
    gates |= {r[0] for r in store.query("SELECT DISTINCT gate FROM outcomes WHERE run_id = ? AND outcome = 'env_error'"
                                        " AND gate IS NOT NULL AND synthetic = 0", (run_id,))}
    harness = any(a.get("rule") == "harness_error" for a in alerts_) or bool(store.query(
        "SELECT 1 FROM outcomes WHERE run_id = ? AND outcome = 'harness_error' AND synthetic = 0 LIMIT 1", (run_id,)))
    return frozenset(gates), harness


def escalate_repeats(store: Store, run: RunRef, alerts: Sequence[Alert]) -> tuple[Alert, ...]:
    """The 9.10 repeat escalation (choice 6): `env_error` on a gate that failed in the previous run of the same type,
    and `harness_error` after a `harness_error` there, become P1, for every gate (11.6)."""
    prev = previous_run(store, run)
    if prev is None:
        return tuple(alerts)
    gates, harness = _previous_failures(store, prev)
    out = []
    for a in alerts:
        repeat = (a.rule == "env_error" and a.gate in gates) or (a.rule == "harness_error" and harness)
        if repeat and a.priority != P1:
            a = replace(a, route=replace(a.route, priority=P1),
                        detail=f"{a.detail}; repeat of the previous {run.run_type} run {prev}".lstrip("; "))
        out.append(a)
    return tuple(out)


def harness_freeze(store: Store, now: datetime) -> str | None:
    """Set `system_state.harness_freeze` at the second `aborted_harness` run within 7 days up to `now` (9.10, 1.5);
    returns the value when this call set it, None otherwise (already frozen, or fewer runs)."""
    if store.get_system_state(FREEZE_KEY):
        return None
    since = iso(cal.to_utc(now) - FREEZE_WINDOW)
    ids = [r[0] for r in store.query("SELECT run_id FROM runs WHERE outcome = 'aborted_harness' AND started_at > ? "
                                     "AND started_at <= ? ORDER BY started_at, run_id", (since, iso(now)))]
    if len(ids) < FREEZE_COUNT:
        return None
    value = (f"frozen {iso(now)}: {len(ids)} aborted_harness runs within 7 days ({', '.join(ids)}); harness changes "
             "frozen until root-caused (1.5, 9.10)")
    store.set_system_state(FREEZE_KEY, value, at=iso(now))
    return value


# -- delivery (9.10; choices 2 to 5) --------------------------------------------------------------------------------

class Pager(Protocol):
    """The paging service (a Phase 0 item, 11.8): pages people and returns the page id."""

    def page(self, *, key: str, severity: str, text: str, people: Sequence[str], roles: Sequence[str]) -> str: ...


class Mailer(Protocol):
    """The mail relay: sends a plain-text email and returns the message id."""

    def send(self, *, to: Sequence[str], subject: str, body: str) -> str: ...


@dataclass
class MemoryPager:
    """An in-memory `Pager` that records each page (for the demo and tests until the service is named)."""

    pages: list[dict[str, Any]] = field(default_factory=list)

    def page(self, *, key: str, severity: str, text: str, people: Sequence[str], roles: Sequence[str]) -> str:
        self.pages.append({"key": key, "severity": severity, "text": text, "people": list(people),
                           "roles": list(roles)})
        return f"page-{len(self.pages)}"


@dataclass
class MemoryMailer:
    """An in-memory `Mailer` that records each email."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, *, to: Sequence[str], subject: str, body: str) -> str:
        self.sent.append({"to": list(to), "subject": subject, "body": body})
        return f"mail-{len(self.sent)}"


def send_page_job(payload: bytes, *, pager: Pager, mailer: Mailer) -> str:
    """Deliver one `page` outbox job (the sender the outbox calls for kind `page`): the email of a P1 to the `Mailer`
    (choice 4), every other page to the `Pager`. Returns the page or message id."""
    p = json.loads(payload)
    if p["channel"] == EMAIL_DESTINATION:
        return mailer.send(to=p["to"] or p["roles"], subject=p["subject"], body=p["text"])
    return pager.page(key=p["key"], severity=p["severity"], text=p["text"], people=p["to"], roles=p["roles"])


@dataclass(frozen=True)
class Dispatched:
    """What one dispatch did: `posted`, `held` (to 06:00), `digest` (P3), `rate_limited` or `duplicate`."""

    alert: Alert
    disposition: str
    due_at: datetime
    jobs: tuple[slack.Queued, ...] = ()
    pages: bool = False

    @property
    def job_ids(self) -> tuple[int, ...]:
        return tuple(q.job_id for q in self.jobs)


class Dispatcher:
    """Sends alerts through the outbox (9.10). Holds the pending pages, the thread parents and the rate limits."""

    def __init__(self, store: Store, *, channels: slack.SlackChannels | None = None, oncall: OnCall | None = None,
                 windows: cal.WindowsConfig | None = None, paging_service: str = "paging"):
        self.store = store
        self.channels = channels or slack.SlackChannels()
        self.oncall = oncall or OnCall()
        self.windows = windows or cal.WindowsConfig(time(6, 0), time(22, 0), {})
        self.paging_service = paging_service
        self.pending: list[Dispatched] = []
        self._threads: set[str] = set()
        self._keys: set[str] = set()
        self._last: dict[str, datetime] = {}

    def release_at(self, route: AlertRoute, now: datetime) -> datetime:
        """When the alert goes out: now inside the operating day or for an after-hours page, else 06:00 (9.10)."""
        if pages_after_hours(route):
            return cal.to_utc(now)
        return cal.operating_start(now, self.windows, "pr")

    def _people(self, alert: Alert, roles: Iterable[str], day: date) -> dict[str, str]:
        found = {r: p for r in roles if r not in CHANNELS and (p := self.oncall.person(r, day, tier=alert.tier))}
        found.update({r: p for r, p in alert.people.items() if r in roles})
        return found

    def _pages(self, alert: Alert, people: Mapping[str, str]) -> list[tuple[str, str, dict[str, Any], str | None]]:
        r = alert.route
        if not r.paged:
            return []
        to = [people[x] for x in r.paged if x in people]
        text = alert.fields().render()
        page = {"channel": self.paging_service, "key": alert.key, "severity": r.priority, "rule": r.rule,
                "text": text, "roles": list(r.paged), "to": to, "run_id": alert.run_id}
        out = [("page", self.paging_service, page, alert.run_id)]
        if r.priority == P1:
            out.append(("page", EMAIL_DESTINATION, {
                "channel": EMAIL_DESTINATION, "key": alert.key, "severity": r.priority, "rule": r.rule, "text": text,
                "subject": f"[Sentinel] {r.priority} {alert_name(r.rule)}", "roles": list(r.paged), "to": to,
                "run_id": alert.run_id}, alert.run_id))
        return out

    def _posts(self, alert: Alert, people: Mapping[str, str], day: date) -> list[slack.SlackMessage]:
        r = alert.route
        parent = slack.thread_key(alert.run_id, r.rule)
        notified = [people[x] for x in r.notified if x in people]
        out = []
        for ch in slack.route(r):
            mentions = list(notified)
            if r.priority == P2:
                rota = self.oncall.person("triage_rotation" if ch == TRIAGE_CHANNEL else "harness_on_call", day)
                mentions += [rota] if rota else []
            m = tuple(dict.fromkeys(mentions))
            out.append(slack.SlackMessage(ch, alert.fields(), parent, parent in self._threads, m, alert.run_id,
                                          r.rule))
            if len(alert.shard_details) > 1:
                out += [slack.SlackMessage(ch, alert.fields(f"shard {s}: {d or alert.detail}"), parent, True, (),
                                           alert.run_id, r.rule) for s, d in alert.shard_details]
        return out

    def _with_incidents(self, alert: Alert) -> Alert:
        """A safety-table alert names the run's incident rows (9.9 `#sentinel-safety`: "each with the incident id";
        choice 7)."""
        if not alert.route.is_safety or alert.run_id is None:
            return alert
        rows = self.store.query("SELECT incident_id, severity FROM incidents WHERE run_id = ? ORDER BY incident_id",
                                (alert.run_id,))
        if not rows:
            return alert
        ids = ", ".join(f"{r['incident_id']} ({r['severity']})" for r in rows)
        return replace(alert, detail=f"{alert.detail}; incident {ids}".lstrip("; "))

    def dispatch(self, alert: Alert, now: datetime) -> Dispatched:
        """Send one alert (choices 2 to 4); see the module docstring."""
        if alert.key in self._keys:
            return Dispatched(alert, "duplicate", cal.to_utc(now))
        limit = RATE_LIMITS.get(alert.rule)
        last = self._last.get(alert.rule)
        if limit is not None and last is not None and cal.to_utc(now) - last < limit:
            return Dispatched(alert, "rate_limited", cal.to_utc(now))
        self._keys.add(alert.key)
        if limit is not None:
            self._last[alert.rule] = cal.to_utc(now)
        if alert.priority == P3:
            return Dispatched(alert, "digest", cal.to_utc(now))
        alert = self._with_incidents(alert)
        due = self.release_at(alert.route, now)
        day = cal.ny(due).date()
        r = alert.route
        people = self._people(alert, (*r.paged, *r.notified), day)
        items = self._pages(alert, people)
        items += [("slack", self.channels.name(m.channel), slack.payload(m, self.channels), m.run_id)
                  for m in self._posts(alert, people, day)]
        jobs = slack.queue_payloads(self.store, items, now, release_at=due, label="alert_dispatch")
        self._threads.add(slack.thread_key(alert.run_id, r.rule))
        d = Dispatched(alert, "held" if due > cal.to_utc(now) else "posted", due, jobs, bool(r.paged))
        if d.pages:
            self.pending.append(d)
        return d

    def check_undelivered(self, now: datetime) -> tuple[Dispatched, ...]:
        """Pending pages whose 5 minutes have passed (choice 5): those with no job `sent` are written to
        `system_state.alert_undelivered` and returned; every checked dispatch leaves the pending list."""
        t = cal.to_utc(now)
        undelivered, keep = [], []
        for d in self.pending:
            if t < d.due_at + UNDELIVERED_AFTER:
                keep.append(d)
                continue
            ids = d.job_ids
            sent = bool(ids) and bool(self.store.query(
                f"SELECT 1 FROM outbox WHERE state = 'sent' AND job_id IN ({','.join('?' * len(ids))})", ids))
            if not sent:
                undelivered.append(d)
        self.pending = keep
        if undelivered:
            old = json.loads(self.store.get_system_state(UNDELIVERED_KEY) or "[]")
            old += [{"key": d.alert.key, "rule": d.alert.rule, "run_id": d.alert.run_id, "due_at": iso(d.due_at),
                     "detected_at": iso(t)} for d in undelivered]
            self.store.set_system_state(UNDELIVERED_KEY, json.dumps(old, sort_keys=True), at=iso(t))
        return tuple(undelivered)


def record_ack(store: Store, run_id: str, key: str, *, by: str, at: datetime, via: str = "paging_service"
               ) -> dict[str, Any]:
    """Mirror an acknowledgement recorded in the paging service into the run's `summary_json` (`alert_acks`), so
    the Alerts panel shows who acknowledged what and when (9.10). Returns the stored summary."""
    acks = list(store.run_summary(run_id).get(ACKS_KEY) or [])
    acks.append({"key": key, "by": by, "at": iso(at), "via": via})
    return store.update_summary(run_id, {ACKS_KEY: acks})

