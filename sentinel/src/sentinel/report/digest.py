"""Digests and triage reminders (9.9 triage channel; 9.10 "needs_human daily digest" and the digest format; 9.12;
11.6). Each builds text from the store; `post` queues it through the outbox (9.1) with `egress_log` purpose
`digest`, and nothing here sends.

- `daily(store, now, ...)`: the needs_human daily digest, due at `daily_at(day)` (08:00 America/New_York) on business
  days and None on any other day. The 9.10 digest format: one line per tier with count, oldest age and items past
  SLA, then the ten oldest items in the message format, then escalations, pending proposals, decisions without
  tickets and abandoned outbox jobs; then the P3 alerts of the day (9.10: "P3 goes in the daily digest") and the
  queue link.
- `weekly(store, now, ...)`: the weekly metrics summary of the triage channel: contested and suspect decisions, SLA
  breaches (9.12 step 4) and the line of the weekly coverage review.
- `merge_run(store, run_id, now, ...)`: the merge run digest of new, unsuppressed Unexpected findings, headed by the
  items three business days past their SLA (9.9, 9.12 step 3).
- `reminders(store, now, since=..., rc_runs=...)`: pending two-person proposals older than 2 business hours, and the
  baseline queue reminder 3 business days before an RC run (9.9 triage channel).
- `post(store, digest, channels, now, email_to=...)` and `post_reminders(...)` queue the outbox jobs.

Choices where the spec is ambiguous (each is reported):

1. The digest's items are the triage queue of `sla.open_items` (open, non-synthetic, tiered findings without a
   decision); the tier lines print tiers 1 to 3 always. "Oldest age" is wall-clock time since the finding was created
   (`2d 04h`); "past SLA" uses the 9.12 clock of `sla.due`. An item line is the 9.10 message format with severity P3,
   the finding kind as the alert, the category as the outcome, the finding page as the report link and the candidate
   capture (`outcomes.cand_capture_sha256`) as the crop link.
2. Decisions without tickets are active `confirm_bug` decisions with no `ticket_ref` decided 24 hours or more before
   the digest (9.9: "after 24 hours the digest ... list decisions without tickets"; `jira_sync.decisions_without_tickets`
   and its `without_ticket_line`, the Decisions page's list too); abandoned outbox jobs are those no
   human has closed. P3 alerts are those of the runs that started in the 24 hours before the digest (from their
   summary `alerts`, collapsed by `alerting.raise_run_alerts`), plus any the caller passes (a P3 alert with no run).
3. Links: the triage queue is `<base>/triage` and the baseline approval queue `<base>/baselines` (9.5 names the
   pages, not their paths).
4. Reminders fire once, when their moment falls in `(since, now]`: a proposal when it turns 2 business hours old;
   the baseline reminder at 08:00 on the business day 3 business days before the RC run's date. The weekly summary
   counts contested findings created in the 7 days before `now`.
5. The digest is posted to the triage channel as one multi-line message (outbox kind `digest`); with `email_to`, an
   email copy is queued as a `digest` job to destination `email`, which the outbox sender routes to the `Mailer`
   (`alerting.send_page_job` reads the same payload shape). Reminders are `slack` jobs.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from .. import calendar as cal
from ..orchestrator.alerts import TRIAGE_CHANNEL
from ..sla import BusinessClock, OnCall, OpenItem, breaches, business_age, iso, open_items
from ..store import Store
from ..store.lifecycle import suspect_decisions
from . import jira_sync, slack
from .alerting import EMAIL_DESTINATION, Alert, RunRef, raise_run_alerts
from .inline import Urls, is_artifact_hash

DIGEST_AT = time(8, 0)                         # 9.10, 11.6: 08:00 America/New_York
OLDEST = 10                                    # 9.10: the ten oldest items
PROPOSAL_REMINDER = timedelta(hours=2)         # 9.9: pending proposals older than 2 business hours
BASELINE_LEAD = 3                              # 9.9: 3 business days before an RC run
Line = str | slack.MessageFields


def daily_at(day: date) -> datetime:
    """08:00 America/New_York on `day`, in UTC."""
    return cal.wall_time(day, DIGEST_AT)


def _age(start: datetime, end: datetime) -> str:
    secs = max(0, int((cal.to_utc(end) - cal.to_utc(start)).total_seconds()))
    days, rest = divmod(secs, 86400)
    return f"{days}d {rest // 3600:02d}h" if days else f"{rest // 3600}h {rest % 3600 // 60:02d}m"


@dataclass(frozen=True)
class Digest:
    """A digest or summary: its title and lines (text or 9.10 message lines)."""

    kind: str                                   # daily | weekly | merge
    title: str
    lines: tuple[Line, ...]
    created_at: datetime
    run_id: str | None = None

    def render(self, *, slack_post: bool = False) -> str:
        """The text, escaped for Slack when `slack_post` (choice 5); plain for the email copy."""
        esc = slack.slack_text if slack_post else slack.one_line
        out = [esc(self.title)]
        out += [x.render(slack=slack_post) if isinstance(x, slack.MessageFields) else esc(x) for x in self.lines]
        return "\n".join(out)


# -- shared reads ---------------------------------------------------------------------------------------------------

def _pairs(store: Store) -> dict[str, str]:
    return {r[0]: f"{r[1]} vs {r[2]}" for r in store.query(
        "SELECT r.run_id, p.candidate_build_id, p.base_build_id FROM runs r "
        "JOIN build_pairs p ON p.pair_id = r.pair_id")}


def _item_line(store: Store, it: OpenItem, base_url: str, pairs: dict[str, str]) -> slack.MessageFields:
    urls = Urls(base_url.rstrip("/"), it.run_id)
    meta = store.query("SELECT c.surface_id, o.cand_capture_sha256 FROM checkpoints c LEFT JOIN outcomes o ON "
                       "o.checkpoint_id = c.checkpoint_id AND o.run_id = ? WHERE c.checkpoint_id = ?",
                       (it.run_id, it.checkpoint_id)) if it.checkpoint_id else []
    surface = meta[0]["surface_id"] if meta else None
    crop = meta[0]["cand_capture_sha256"] if meta else None
    return slack.MessageFields("P3", it.kind, it.run_type, it.run_id, pairs.get(it.run_id), it.category or it.kind,
                               it.description, urls.finding(it.finding_id),
                               f"{surface} tier {it.tier}" if surface else f"tier {it.tier}",
                               urls.artifact(crop) if crop and is_artifact_hash(crop) and len(crop) == 64 else None)


def _section(title: str, lines: Sequence[Line]) -> list[Line]:
    return [f"{title}:", *(lines or ["none"])]


# -- the daily digest (9.10; choices 1 and 2) ----------------------------------------------------------------------

def _p3_alerts(store: Store, now: datetime, base_url: str) -> list[Alert]:
    since = iso(cal.to_utc(now) - timedelta(hours=24))
    out: list[Alert] = []
    for r in store.query("SELECT run_id FROM runs WHERE started_at > ? AND started_at <= ? ORDER BY started_at, run_id",
                         (since, iso(now))):
        entries = store.run_summary(r[0]).get("alerts") or ()
        run = RunRef.from_store(store, r[0], base_url=base_url)
        out += [a for a in raise_run_alerts(run, entries) if a.priority == "P3"]
    return out


def daily(store: Store, now: datetime, *, clock: BusinessClock | None = None, oncall: OnCall | None = None,
          alerts: Iterable[Alert] = (), base_url: str = "https://sentinel.internal") -> Digest | None:
    """The needs_human daily digest at `now` (9.10 format; choices 1 and 2); None on a day that is not a business
    day. `alerts` are P3 alerts with no run that the dispatcher held for the digest."""
    c = clock or BusinessClock()
    if not c.is_business_day(cal.ny(now).date()):
        return None
    items = open_items(store, clock=c)
    pairs = _pairs(store)
    lines: list[Line] = []
    for tier in (1, 2, 3):
        mine = [i for i in items if i.tier == tier]
        if not mine:
            lines.append(f"Tier {tier}: 0 open")
            continue
        past = sum(1 for i in mine if i.due_at is not None and i.due_at < cal.to_utc(now))
        oldest = min(i.created_at for i in mine)
        lines.append(f"Tier {tier}: {len(mine)} open, oldest {_age(oldest, now)}, {past} past SLA")
    lines += _section("Oldest items", [_item_line(store, i, base_url, pairs) for i in items[:OLDEST]])
    member = (oncall or OnCall()).rotation_member("triage", cal.ny(now).date())
    lines += _section("Escalations", [
        f"{i.finding_id} (tier {i.tier}): step {i.escalation_step}, assigned to {i.assignee}"
        for i in items if i.escalation_step >= 1])
    lines += _section("Pending proposals", [
        f"proposal {i.proposal_id} on {i.finding_id}: {i.proposal_action} by {i.proposed_by}, open "
        f"{business_age(c, i.item.created_at, now)} business, awaiting {member or 'the triage rotation'}"
        for i in items if i.proposal_id is not None])
    at = iso(now) or ""
    lines += _section("Decisions without tickets", [                  # 9.9 (`jira_sync`, one definition)
        jira_sync.without_ticket_line(d, at) for d in jira_sync.decisions_without_tickets(store, at)])
    lines += _section("Abandoned outbox jobs", [
        f"job {r['job_id']}: {r['kind']} to {r['destination']}, last error {r['last_error'] or 'none'}, queued "
        f"{r['created_at']}"
        for r in store.query("SELECT job_id, kind, destination, last_error, created_at FROM outbox WHERE state = "
                             "'abandoned' AND closed_by IS NULL ORDER BY created_at, job_id")])
    lines += _section("P3 alerts", [a.fields() for a in (*_p3_alerts(store, now, base_url), *alerts)])
    lines.append(f"Queue: {base_url.rstrip('/')}/triage")
    return Digest("daily", f"[Sentinel] P3 needs_human daily digest | {cal.fmt_ny(now)}", tuple(lines),
                  cal.to_utc(now))


# -- the weekly metrics summary (9.9, 9.12) -------------------------------------------------------------------------

def weekly(store: Store, now: datetime, *, clock: BusinessClock | None = None, coverage_review: str | None = None
           ) -> Digest:
    """The weekly metrics summary at `now`: contested and suspect decisions, SLA breaches and the coverage review's
    line (9.12: "the review's outcome is a line in the weekly metrics summary")."""
    since = iso(cal.to_utc(now) - timedelta(days=7))
    contested = store.query("SELECT natural_key, checkpoint_id, tier FROM findings WHERE contested = 1 "
                            "AND synthetic = 0 AND created_at > ? ORDER BY created_at, natural_key", (since,))
    suspect = suspect_decisions(store)
    late = breaches(store, now, clock=clock)
    lines: list[Line] = [f"Contested decisions: {len(contested)}",
                         *(f"- {r['natural_key']} on {r['checkpoint_id']} (tier {r['tier']})" for r in contested),
                         f"Suspect decisions: {len(suspect)}",
                         *(f"- {d.decision_id}: {d.action}, reopened {d.reopen_count} times" for d in suspect),
                         f"SLA breaches: {len(late)}",
                         *(f"- {i.finding_id} (tier {i.tier}): five business days past its SLA, assigned to "
                           f"{i.assignee or 'nobody'}" for i in late),
                         f"Coverage review: {coverage_review or 'not recorded this week'}"]
    return Digest("weekly", f"[Sentinel] weekly metrics summary | week ending {cal.fmt_ny(now)}", tuple(lines),
                  cal.to_utc(now))


# -- the merge run digest (9.9, 9.12 step 3) -------------------------------------------------------------------------

def merge_run(store: Store, run_id: str, now: datetime, *, clock: BusinessClock | None = None,
              base_url: str = "https://sentinel.internal") -> Digest:
    """The merge run digest: the items three business days past their SLA first (9.12 step 3), then the run's new,
    unsuppressed Unexpected findings (no decision, first seen in this run, not held)."""
    items = open_items(store, clock=clock)
    pairs = _pairs(store)
    escalated = [i for i in items if i.escalation_step >= 3]
    new = [i for i in items if i.run_id == run_id and store.query(
        "SELECT 1 FROM findings WHERE natural_key = ? AND first_seen_run_id = ?", (i.finding_id, run_id))]
    lines: list[Line] = [*_section("Escalated, three business days past SLA",
                                   [_item_line(store, i, base_url, pairs) for i in escalated]),
                         *_section(f"New Unexpected ({len(new)})",
                                   [_item_line(store, i, base_url, pairs) for i in new])]
    return Digest("merge", f"[Sentinel] P3 merge run digest | merge {run_id} | {pairs.get(run_id, '-')}",
                  tuple(lines), cal.to_utc(now), run_id)


# -- reminders (9.9 triage channel; choice 4) -----------------------------------------------------------------------

@dataclass(frozen=True)
class Reminder:
    """A triage reminder: `proposal` or `baseline_queue`, and its message."""

    kind: str
    ref: str
    fields: slack.MessageFields


def _business_days_before(clock: BusinessClock, d: date, n: int) -> date:
    while n:
        d -= timedelta(days=1)
        n -= clock.is_business_day(d)
    return d


def reminders(store: Store, now: datetime, *, since: datetime | None = None, rc_runs: Iterable[datetime] = (),
              clock: BusinessClock | None = None, base_url: str = "https://sentinel.internal") -> tuple[Reminder, ...]:
    """The reminders whose moment falls in `(since, now]` (choice 4); `since` None takes every proposal already past
    2 business hours. `rc_runs` are the scheduled RC runs (their start times)."""
    c = clock or BusinessClock()
    base = base_url.rstrip("/")
    pairs = _pairs(store)
    out: list[Reminder] = []
    for i in open_items(store, clock=c):
        if i.proposal_id is None:
            continue
        old = c.elapsed(i.item.created_at, now) >= PROPOSAL_REMINDER
        new = since is None or c.elapsed(i.item.created_at, since) < PROPOSAL_REMINDER
        if old and new:
            detail = (f"proposal {i.proposal_id} on {i.finding_id}: {i.proposal_action} by {i.proposed_by}, open "
                      f"{business_age(c, i.item.created_at, now)} business; a second person decides")
            out.append(Reminder("proposal", str(i.proposal_id), slack.MessageFields(
                "P3", "pending two-person proposal", i.run_type, i.run_id, pairs.get(i.run_id), i.proposal_action,
                detail, Urls(base, i.run_id).finding(i.finding_id), f"tier {i.tier}")))
    candidates = store.query("SELECT count(*) FROM baselines WHERE state = 'candidate'")[0][0]
    for rc in rc_runs:
        moment = daily_at(_business_days_before(c, cal.ny(rc).date(), BASELINE_LEAD))
        if moment <= cal.to_utc(now) and (since is None or moment > cal.to_utc(since)):
            out.append(Reminder("baseline_queue", iso(rc) or "", slack.MessageFields(
                "P3", "baseline queue reminder", "rc", None, None, None,
                f"{candidates} baseline candidates await approval; RC run at {cal.fmt_ny(rc)}, in {BASELINE_LEAD} "
                "business days", f"{base}/baselines")))
    return tuple(out)


# -- posting (choice 5) ------------------------------------------------------------------------------------------------

def post(store: Store, digest: Digest, channels: slack.SlackChannels, now: datetime, *,
         email_to: Sequence[str] = ()) -> tuple[slack.Queued, ...]:
    """Queue the digest to the triage channel (kind `digest`) and, with `email_to`, its email copy."""
    key = slack.thread_key(digest.run_id, f"digest-{digest.kind}-{cal.ny(digest.created_at).date()}")
    msg = slack.SlackMessage(TRIAGE_CHANNEL, None, key, run_id=digest.run_id, alert_kind=f"digest-{digest.kind}",
                             purpose="digest", text=digest.render(slack_post=True))
    items: list[tuple[str, str, dict[str, Any], str | None]] = [
        ("digest", channels.name(TRIAGE_CHANNEL), slack.payload(msg, channels), digest.run_id)]
    if email_to:
        items.append(("digest", EMAIL_DESTINATION, {
            "channel": EMAIL_DESTINATION, "key": key, "severity": "P3", "rule": f"digest-{digest.kind}",
            "subject": digest.title, "text": digest.render(), "to": list(email_to), "roles": [],
            "run_id": digest.run_id, "egress_purpose": "digest"}, digest.run_id))
    return slack.queue_payloads(store, items, now, label="digest_enqueue")


def post_reminders(store: Store, items: Iterable[Reminder], channels: slack.SlackChannels, now: datetime
                   ) -> tuple[slack.Queued, ...]:
    """Queue each reminder as its own triage-channel post."""
    msgs = [slack.SlackMessage(TRIAGE_CHANNEL, r.fields, slack.thread_key(None, f"reminder-{r.kind}-{r.ref}"),
                               alert_kind=f"reminder-{r.kind}") for r in items]
    return slack.enqueue(store, msgs, channels, now)
