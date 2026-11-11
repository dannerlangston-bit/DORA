"""The control host's timed work beside the Reporter service (5.1, 8.2, 8.6, 9.1, 9.2, 9.9, 9.10, 9.12): what
`sentinel serve` runs every minute on a timer thread, through the Central Store's single writer.

`ControlHost.tick(now)` runs whatever is due at `now`:

| When | What |
|---|---|
| every tick (each minute) | the outbox: every due job through its sender (`outbox.dispatch`, `report.senders`), the 9.10 outage alert at 15 minutes, a SENT task for each job abandoned at 24 hours (9.9); the `error` status of PR runs with no comment by budget plus 15 minutes (`prhost.post_overdue`); the 5-minute undelivered-page watch (`Dispatcher.check_undelivered`, `alert_undelivered`) |
| every 15 minutes | the triage reminders (`digest.reminders`) |
| hourly in business hours | the 9.12 escalation steps (`sla.escalate`), each alert dispatched |
| hourly | the Jira fix poll (`jira_sync.poll`) |
| 08:00 New York, business days | the needs_human daily digest (`digest.daily`), to the triage channel and the digest email |
| Mondays 08:00 New York | the weekly metrics summary (`digest.weekly`) |
| first tick of each New York day | the commit status mode announcement (`prhost.announce_check_mode`) |
| 02:00 New York | the Central Store backup (`store.backup.run_backup`) |
| Sundays 03:00 New York | the weekly artifact audit (`store.audit.run_audit`) |

`after_action(result, request)` is the Reporter service's hook after a report UI action: each PR run a decision
was written on is re-posted (`prhost.repost`, 9.2: within one minute) and the outbox runs at once. `raise_alert`
dispatches one alert (`alerting.Dispatcher`) and stores its payloads. `restore_drill` records a passing drill.

Choices where the spec is ambiguous or silent (reported):
1. The backup and the audit run on their own thread, never inside a writer unit: they hold the writer only for
   their two short units (`store.backup.writer_call`), so the service keeps writing meanwhile (8.6). A failed
   backup or an audit with failures raises the 9.10 Store health alert (`store.health.store_health`); a failed
   weekly integrity check also asks the service to stop (`stop_requested`, 8.9). The audit is Sundays 03:00 New
   York, after the backup (8.6 names the week, not the day); its sample is seeded with the ISO year and week, as
   `sentinel store audit` seeds it.
2. A job is due once per period: the first tick at or after its time, then not again until the next period. A tick
   that finds a daily or weekly time already past (the service started late) runs the job once at once.
3. An abandoned job of no run opens no SENT task (9.9 keys a task to a run); the digest and the run list still
   list it until a human closes it. A forbidden Jira reopen raises the P2 `jira_reopen_forbidden` route.
4. The dispatcher (pending pages, threads, rate limits) is built on the writer's thread and used only inside writer
   units, as the store it reads; it is in memory, so a restart forgets pending pages (alerting choice).
"""
from __future__ import annotations

import random
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TypeVar

from .. import calendar as cal
from ..egress import EgressAllowlist
from ..orchestrator.alerts import jira_reopen_forbidden
from ..report import digest, jira, jira_sync, outbox, prhost, slack
from ..report.actions import ActionRequest, ActionResult
from ..report.alerting import Alert, Dispatched, Dispatcher, RunRef
from ..report.reporting_config import ReportingConfig
from ..report.senders import SlackSender, Transports, jira_alerts, senders_for
from ..sla import BusinessClock, OnCall, escalate, iso
from ..store import audit as store_audit
from ..store import backup as store_backup
from ..store import health as store_health
from ..store.store import Store, decision_int

T = TypeVar("T")
REMINDER_EVERY = timedelta(minutes=15)
HOURLY = timedelta(hours=1)
BACKUP_AT = time(2, 0)                     # 8.6: 02:00 America/New_York
AUDIT_AT = (6, time(3, 0))                 # choice 1: Sundays (ISO weekday 7 - 1) 03:00 America/New_York
WEEKLY_AT = (0, digest.DIGEST_AT)          # choice 2: Mondays 08:00 America/New_York


class WriterAdapter:
    """The store package's `WriterService` as the Reporter service's synchronous `Writer` (8.2)."""

    def __init__(self, service: Any):
        self.service = service

    def submit(self, fn: Callable[[Store], T]) -> T:
        return self.service.call(fn)


@dataclass
class TickReport:
    """What one tick ran and what it did."""
    ran: list[str] = field(default_factory=list)
    dispatch: outbox.DispatchReport | None = None
    alerts: list[Dispatched] = field(default_factory=list)
    undelivered: tuple[Dispatched, ...] = ()
    overdue: list[int] = field(default_factory=list)
    sent_tasks: list[int] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)
    maintenance: list[str] = field(default_factory=list)


class ControlHost:
    """The timed work of the control host (module docstring)."""

    def __init__(self, writer: Any, payloads: outbox.PayloadStore, *, allowlist: EgressAllowlist,
                 transports: Transports | None = None, reporting: ReportingConfig | None = None,
                 oncall: Mapping[str, Any] | None = None, base_url: str = "https://sentinel.internal",
                 artifact_root: str | Path | None = None, backup_root: str | Path | None = None,
                 windows: cal.WindowsConfig | None = None, holidays: Iterable[date] = (),
                 background: bool = True):
        self.writer, self.payloads, self.allowlist = writer, payloads, allowlist
        self.transports = transports or Transports()
        self.reporting, self.base_url = reporting, base_url
        self.oncall = OnCall.from_doc(oncall) if oncall else OnCall()
        self.channels = slack.SlackChannels.from_reporting({"slack": reporting.slack}) if reporting else \
            slack.SlackChannels()
        self.alert_to = outbox.alert_destinations(reporting) if reporting else ()
        self.artifact_root = Path(artifact_root) if artifact_root else None
        self.backup_root = Path(backup_root) if backup_root else None
        self.windows, self.clock = windows, BusinessClock(holidays=frozenset(holidays))
        self.background = background
        self.last: dict[str, datetime] = {}
        self.stop_requested: str | None = None
        self.maintenance_log: list[str] = []
        self._dispatcher: Dispatcher | None = None
        self._slack = SlackSender(self.transports.slack) if self.transports.slack is not None else None
        self._jobs: dict[str, threading.Thread] = {}

    # -- plumbing -----------------------------------------------------------------------------------------------
    def _w(self, fn: Callable[[Store], T]) -> T:
        return store_backup.writer_call(self.writer)(fn)

    def _keep(self, items: Iterable[Any]) -> None:
        """Store the payload bytes of queued jobs as the artifacts their hashes name (8.3 outbox)."""
        for q in items:
            self.payloads.put(q.payload, name=q.payload_sha256, kind="request", media_type="application/json")

    def _disp(self, s: Store) -> Dispatcher:
        if self._dispatcher is None:
            self._dispatcher = Dispatcher(s, channels=self.channels, oncall=self.oncall, windows=self.windows,
                                          paging_service=self.reporting.paging_service if self.reporting else
                                          "paging")
        return self._dispatcher

    def raise_alert(self, alert: Alert, now: datetime) -> Dispatched:
        """Dispatch one alert through the outbox (9.10) and keep its payloads."""
        d = self._w(lambda s: self._disp(s).dispatch(alert, now))
        self._keep(d.jobs)
        return d

    # -- the outbox ---------------------------------------------------------------------------------------------
    def deliver(self, now: datetime) -> tuple[outbox.DispatchReport, list[Dispatched], list[int]]:
        """One outbox pass at `now` (9.1): the sends, then a SENT task per job abandoned in the pass (9.9) and the
        P2 of each forbidden Jira reopen (choice 3)."""
        at = iso(now) or ""

        def work(s: Store) -> tuple[outbox.DispatchReport, list[Any], list[jira_sync.Queued]]:
            senders = senders_for(s, self.transports, report_base=self.base_url, at=at, slack_sender=self._slack)
            rep = outbox.dispatch(s, self.payloads, senders, at, allowlist=self.allowlist, alert_to=self.alert_to)
            tasks = []
            for jid in rep.abandoned:
                job = s.outbox_job(jid)
                if job is None or job.run_id is None:
                    continue
                q = jira_sync.sent_task(s, jira.SentEvent("abandoned_outbox_job", job.kind, job.run_id,
                                                          detail=job.last_error or ""),
                                        report_base=self.base_url, at=at)
                if q is not None:
                    tasks.append(q)
            return rep, jira_alerts(senders), tasks

        rep, forbidden, tasks = self._w(work)
        self._keep(tasks)
        raised = [self.raise_alert(Alert.of(jira_reopen_forbidden(), detail=detail, key=f"jira_reopen:{job.job_id}",
                                            run=self._run_ref(job.run_id)), now)
                  for _route, job, detail in forbidden]
        return rep, raised, [q.job_id for q in tasks]

    def _run_ref(self, run_id: str | None) -> RunRef | None:
        if run_id is None:
            return None
        return self._w(lambda s: RunRef.from_store(s, run_id, base_url=self.base_url))

    def after_action(self, result: ActionResult, request: ActionRequest, now: datetime) -> list[prhost.Posted]:
        """After a report UI action: re-post every PR run a decision was written on (9.2) and run the outbox."""
        ids = [decision_int(d) for d in result.decisions]

        def runs(s: Store) -> list[str]:
            found = {r[0] for r in s.query(f"SELECT run_id FROM decisions WHERE decision_id IN "
                                           f"({','.join('?' * len(ids))})", tuple(ids))} if ids else set()
            return sorted(found | ({request.run} if request.run else set()))

        at = iso(now) or ""
        posted = [p for r in self._w(runs) if (p := self._w(lambda s, r=r: prhost.repost(
            s, self.payloads, r, self.transports.pr_host or prhost.FakePrHost(), allowlist=self.allowlist,
            reporting=self.reporting, now_=at))) is not None]
        if posted:
            self.deliver(now)
        return posted

    # -- the schedule (choice 2) --------------------------------------------------------------------------------
    def _every(self, name: str, now: datetime, period: timedelta) -> bool:
        last = self.last.get(name)
        if last is not None and now - last < period:
            return False
        self.last[name] = now
        return True

    def _at(self, name: str, now: datetime, wall: time, weekday: int | None = None) -> bool:
        """Once per New York day (or week, on `weekday`) at the first tick at or after `wall` (choice 2)."""
        local = cal.ny(now)
        day = local.date()
        if weekday is not None:
            day = day - timedelta(days=(day.weekday() - weekday) % 7)
        due = cal.wall_time(day, wall)
        last = self.last.get(name)
        if cal.to_utc(now) < due or (last is not None and cal.to_utc(last) >= due):
            return False
        self.last[name] = now
        return True

    def tick(self, now: datetime) -> TickReport:
        """Run what is due at `now` (module docstring)."""
        out = TickReport()
        out.dispatch, raised, out.sent_tasks = self.deliver(now)
        out.alerts += raised
        out.ran.append("outbox")
        out.overdue = self._overdue(now)
        out.undelivered = self._w(lambda s: self._disp(s).check_undelivered(now))
        if self._every("reminders", now, REMINDER_EVERY):
            out.ran.append("reminders")
            since = self.last.get("reminders_since")
            self._keep(self._w(lambda s: digest.post_reminders(
                s, digest.reminders(s, now, since=since, clock=self.clock, base_url=self.base_url), self.channels,
                now)))
            self.last["reminders_since"] = now
        if self._business_hours(now) and self._every("sla", now, HOURLY):
            out.ran.append("sla")
            res = self._w(lambda s: escalate(s, now, oncall=self.oncall, clock=self.clock))
            out.alerts += [self.raise_alert(Alert.of(e.route, detail=e.detail, key=e.key, tier=e.tier,
                                                     people=e.people, run=self._run_ref(e.run_id)), now)
                           for e in res.alerts]
        if self.transports.jira is not None and self._every("jira_poll", now, HOURLY):
            out.ran.append("jira_poll")
            since = iso(now - HOURLY)
            self._w(lambda s: jira_sync.poll(self.transports.jira, s, iso(now) or "", since=since))
        if self._at("digest_daily", now, digest.DIGEST_AT):
            d = self._w(lambda s: digest.daily(s, now, clock=self.clock, oncall=self.oncall, base_url=self.base_url))
            if d is not None:
                out.ran.append("digest_daily")
                out.digests.append(d.title)
                self._post_digest(d, now)
        if self._at("digest_weekly", now, WEEKLY_AT[1], WEEKLY_AT[0]):
            out.ran.append("digest_weekly")
            d = self._w(lambda s: digest.weekly(s, now, clock=self.clock))
            out.digests.append(d.title)
            self._post_digest(d, now)
        if self.reporting is not None and self._at("announce", now, time(0, 0)):
            out.ran.append("announce")
            self._w(lambda s: prhost.announce_check_mode(s, self.payloads, self.reporting, now_=iso(now)))
        if self.backup_root is not None and self.artifact_root is not None:
            if self._at("backup", now, BACKUP_AT):
                out.ran.append("backup")
                self._start("backup", self.backup, now)
            if self._at("audit", now, AUDIT_AT[1], AUDIT_AT[0]):
                out.ran.append("audit")
                self._start("audit", self.audit, now)
        out.maintenance = list(self.maintenance_log)
        return out

    def _business_hours(self, now: datetime) -> bool:
        local = cal.ny(now)
        return self.clock.is_business_day(local.date()) and self.clock.opens <= local.time() < self.clock.closes

    def _post_digest(self, d: digest.Digest, now: datetime) -> None:
        email = self.reporting.email if self.reporting else ()
        self._keep(self._w(lambda s: digest.post(s, d, self.channels, now, email_to=email)))

    def _overdue(self, now: datetime) -> list[int]:
        at = iso(now) or ""

        def work(s: Store) -> list[int]:
            jobs = [prhost.post_overdue(s, self.payloads, r, base_url=self.base_url, reporting=self.reporting,
                                        now_=at) for r in prhost.overdue_runs(s, at)]
            return [j for j in jobs if j is not None]
        return self._w(work)

    # -- store maintenance (choice 1) ---------------------------------------------------------------------------
    def _start(self, name: str, job: Callable[[datetime], Any], now: datetime) -> None:
        running = self._jobs.get(name)
        if running is not None and running.is_alive():
            self.maintenance_log.append(f"{name}: still running from the last start; skipped at {iso(now)}")
            return
        if not self.background:
            job(now)
            return
        t = threading.Thread(target=job, args=(now,), name=f"sentinel-{name}", daemon=True)
        self._jobs[name] = t
        t.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the maintenance threads (tests, shutdown)."""
        for t in list(self._jobs.values()):
            t.join(timeout)

    def backup(self, now: datetime) -> store_backup.BackupResult:
        """The 02:00 backup through the writer (8.6); a failure raises the Store health alert (9.10)."""
        assert self.backup_root is not None and self.artifact_root is not None
        res = store_backup.run_backup(self.writer, self.backup_root, iso(now) or "", artifact_root=self.artifact_root)
        self.maintenance_log.append(f"backup {res.stamp}: " + ("ok" if res.ok else f"failed: {res.error}"))
        if not res.ok:
            self.raise_alert(Alert.of(store_health.store_health(), detail=f"backup failed: {res.error}",
                                      key=f"store_health:backup:{res.stamp}"), now)
        if res.stop_service:
            self.stop_requested = f"weekly integrity check failed: {res.error}"
        return res

    def audit(self, now: datetime) -> store_audit.AuditResult:
        """The weekly artifact audit through the writer (8.6); failures raise the Store health alert (9.10)."""
        assert self.backup_root is not None and self.artifact_root is not None
        year, week, _ = cal.ny(now).isocalendar()
        res = store_audit.run_audit(self.writer, [self.artifact_root], self.backup_root, random.Random(year * 100 + week),
                                    iso(now) or "")
        self.maintenance_log.append(f"audit {year}-W{week:02d}: {len(res.failures)} failure(s)")
        if res.failures:
            self.raise_alert(Alert.of(store_health.store_health(), detail=f"artifact audit: {len(res.failures)} "
                                      "copies missing or corrupt", key=f"store_health:audit:{year}-W{week:02d}"), now)
        return res

    def restore_drill(self, by: str, *, compared: int, matched: int, at: datetime) -> str:
        """Record a passing restore drill through the writer (8.6; `store.health.record_restore_drill`)."""
        return self._w(lambda s: store_health.record_restore_drill(s, by, compared=compared, matched=matched,
                                                                   at=iso(at)))


__all__ = ["ControlHost", "TickReport", "WriterAdapter"]
