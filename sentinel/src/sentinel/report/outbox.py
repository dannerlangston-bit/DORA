"""Outbox delivery: every outbound post is a job before it is a send (8.3 `outbox`, 9.1, 9.9, 9.10, 9.12).

The Reporter never posts directly. A PR comment, a commit status, a Jira create, comment or transition, a Slack post,
a page or the digest is first written through the single writer as a queued `outbox` job whose payload is a
canonical-JSON artifact named by the job's `payload_sha256` (`enqueue_job`). `dispatch` then sends the due jobs
through pluggable senders (one per job kind), each send going through the run's `EgressGate`, so every attempt is
one `egress_log` row (`sent`, `failed`, or `refused` by the allowlist; 6.10). A failing job is retried at 1, 2, 5,
15 and 60 minutes, then hourly until 24 hours (`RETRY_MINUTES`, `next_attempt_at`); a job still failing after 15
minutes raises the integration outage alert of 9.10 on every other alert channel (`alert_destinations`); after 24
hours it is `abandoned` and listed in the daily digest and on the run page (`abandoned_jobs`) until a human closes
it (`close_job`). A crash between `enqueue_job` and `dispatch` leaves the job queued; nothing is lost.

- `enqueue_job(store, payloads, kind=, destination=, payload=, run_id=)` stores the payload, then the job.
- `due_jobs(store, now)` lists the jobs `dispatch` would attempt now; `dispatch(store, payloads, senders, now,
  allowlist=, alert_to=)` attempts them and returns a `DispatchReport`.
- `Sender`: `host` (the allowlisted host the post goes to) and `send(job, payload) -> Delivery`, raising on failure
  (`DeliveryError` carries the HTTP status when there was one).
- `Payloads(*sets)`: payloads read from several artifact sets in order (the outbox's own set first, where new
  payloads go, then the run sets that hold payloads other writers stored, such as the Nightly's `quarantine_pr`).

Choices where the spec is ambiguous (reported):

1. The schedule counts from the job's `created_at`: the retries fall 1, 2, 5, 15, 60, 120, ... 1440 minutes after
   the job was written (the post is written when it is due, 9.1), each failed attempt taking the first point after
   it, so a late dispatcher skips the points it missed instead of retrying in a burst. The attempt at 24 hours that
   fails abandons the job. "Still failing after 15 minutes" is the first failed attempt at least 15 minutes after
   `created_at`.
2. The outage alert is raised once per outage of a channel (PR host, Jira, Slack, paging, email): at the failed
   attempt where a job crosses 15 minutes, unless an older job of the same channel is still failing past its own
   15 minutes (that job raised it). "Every other channel" is every alert destination whose channel differs from the
   failing one: the on-call Slack channel and the paging service (`alert_destinations`); email has no outbox kind.
   The alert is itself an outbox job, written in the same batch as the failed attempt; an outage alert that fails
   raises no further alert, so two channels down do not alert each other in a loop.
3. Posts that set a state (`pr_comment`, `commit_status`) go out in order per destination: a job waits while an
   older job to the same destination is queued or failing, so a retried stale status can never overwrite a newer one
   (a stale `success` after a revoked decision would unblock a merge in blocking mode, 9.2).
4. Egress purposes (6.10): PR host posts (`pr_comment`, `commit_status` and every `*_pr` pull request) log
   `pr_comment`, Jira kinds `jira`, `slack` `slack`, `digest` `digest`. 6.10 lists no paging purpose, so a `page` job
   is sent only through a sender that names its own `purpose`; without one the attempt fails without a request
   (reported: the purpose list needs a paging entry).
5. One attempt is one egress row, whatever the sender does inside it (a PR comment edit lists the comments, then
   edits). A sender that raises is a failed attempt; an allowlist refusal is a failed attempt too, its `refused` row
   and `harness_error` finding written by the gate's rules, the finding returned in `DispatchReport.refusals` (the
   run has usually ended, so the outbox does not write it).
6. Due jobs with no sender for their kind wait, unattempted (`DispatchReport.waiting`), as do jobs held by choice 3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence

from ..canonical import SHA256_PREFIX, canonical_json, sha256_id
from ..egress import EgressAllowlist, EgressGate, EgressRefused
from ..records import OUTBOX_KINDS, EgressRow, Finding, OutboxJob, now
from ..store.artifacts import ArtifactError, ArtifactMissing
from ..store.store import Store, StoreRefused, utc
from .reporting_config import ReportingConfig

RETRY_MINUTES = (1, 2, 5, 15) + tuple(range(60, 24 * 60 + 1, 60))   # 9.1: then hourly until 24 hours
OUTAGE_AFTER = timedelta(minutes=15)                                 # 9.1, 9.10 Integration outage
OUTAGE_ALERT = "integration_outage"
OUTAGE_SEVERITY = "P2"                                               # 9.10
ORDERED_KINDS = frozenset({"pr_comment", "commit_status"})           # choice 3
_CHANNELS = {"pr_comment": "pr_host", "commit_status": "pr_host", "jira_create": "jira", "jira_comment": "jira",
             "jira_transition": "jira", "slack": "slack", "page": "paging", "digest": "email"}
_PURPOSES = {"pr_host": "pr_comment", "jira": "jira", "slack": "slack", "email": "digest"}   # 6.10; choice 4


def channel_of(kind: str) -> str:
    """The integration a job kind posts to: pr_host, jira, slack, paging or email (a `*_pr` kind is a pull request
    on the PR host)."""
    if kind.endswith("_pr"):
        return "pr_host"
    if kind not in _CHANNELS:
        raise ValueError(f"outbox kind {kind!r}; one of {OUTBOX_KINDS}")
    return _CHANNELS[kind]


def egress_purpose(kind: str) -> str | None:
    """The 6.10 `egress_log` purpose of a job kind's posts; None for `page` (choice 4)."""
    return _PURPOSES.get(channel_of(kind))


@dataclass(frozen=True)
class Delivery:
    """What a sender returns for a post the host accepted."""
    status: int | None = None          # HTTP status
    request_id: str | None = None
    ref: str | None = None             # what the host made or changed: a comment id, a ticket key, a message id


class DeliveryError(RuntimeError):
    """A post the host did not accept; `status_code` is the HTTP status when there was a response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class Sender(Protocol):
    """Delivers the jobs of one kind. `host` is the allowlisted host name the post goes to; a sender for a kind with
    no 6.10 purpose (`page`) also names its `purpose` attribute."""
    host: str

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery: ...


class PayloadStore(Protocol):
    """Where payloads are kept: `store.artifacts.ArtifactStore` (or `Payloads`) has both methods."""

    def put(self, data: bytes, *, name: str, kind: str, media_type: str) -> Any: ...

    def get(self, sha: str) -> bytes: ...


class Payloads:
    """Payloads across artifact sets: `put` writes to the first set, `get` reads the first set that holds it."""

    def __init__(self, *sets: PayloadStore):
        if not sets:
            raise ValueError("Payloads needs at least one artifact set")
        self.sets = sets

    def put(self, data: bytes, *, name: str, kind: str, media_type: str) -> Any:
        return self.sets[0].put(data, name=name, kind=kind, media_type=media_type)

    def get(self, sha: str) -> bytes:
        for s in self.sets:
            try:
                return s.get(sha)
            except ArtifactMissing:
                continue
        raise ArtifactMissing(sha)


@dataclass(frozen=True)
class AlertDestination:
    """Where an outage alert can go: an alert channel, as an outbox kind and destination."""
    channel: str
    kind: str
    destination: str


def alert_destinations(cfg: ReportingConfig) -> tuple[AlertDestination, ...]:
    """The alert channels of `policy/reporting.yaml` (9.9, 9.10): the on-call Slack channel and the paging service."""
    return (AlertDestination("slack", "slack", cfg.channel("on_call")),
            AlertDestination("paging", "page", cfg.paging_service))


@dataclass(frozen=True)
class Attempt:
    """One send attempt of `dispatch`."""
    job_id: int
    kind: str
    ok: bool
    state: str                         # the job's state after the attempt: sent, failed or abandoned
    error: str | None = None
    next_attempt_at: str | None = None
    ref: str | None = None


@dataclass
class DispatchReport:
    """What one `dispatch` pass did."""
    attempts: list[Attempt] = field(default_factory=list)
    alerts: list[int] = field(default_factory=list)          # outage alert jobs enqueued (9.10)
    abandoned: list[int] = field(default_factory=list)       # jobs abandoned this pass (9.1)
    waiting: list[int] = field(default_factory=list)         # due, not attempted (choices 3 and 6)
    refusals: list[Finding] = field(default_factory=list)    # allowlist refusals' harness_error findings (choice 5)


def _dt(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hex(payload_sha256: str) -> str:
    return payload_sha256[len(SHA256_PREFIX):] if payload_sha256.startswith(SHA256_PREFIX) else payload_sha256


def next_attempt_at(created_at: str, failed_at: str) -> str | None:
    """9.1: when to retry a job written at `created_at` whose attempt at `failed_at` failed: the first point of
    `RETRY_MINUTES` after `created_at` later than the attempt; None once the schedule is spent (abandon; choice 1)."""
    created = _dt(created_at)
    elapsed = _dt(failed_at) - created
    return next((_iso(created + timedelta(minutes=m)) for m in RETRY_MINUTES if timedelta(minutes=m) > elapsed),
                None)


def _row(job: OutboxJob) -> dict[str, Any]:
    values = {k: v for k, v in job.to_dict().items() if k != "job_id"}
    values.update(created_at=utc(job.created_at), next_attempt_at=utc(job.next_attempt_at))
    return values


def _store_payload(payloads: PayloadStore, payload: Mapping[str, Any]) -> tuple[bytes, str]:
    data = canonical_json(payload).encode("utf-8")
    sha = sha256_id(payload)
    payloads.put(data, name=f"outbox/{_hex(sha)}.json", kind="request", media_type="application/json")
    return data, sha


def enqueue_job(store: Store, payloads: PayloadStore, *, kind: str, destination: str, payload: Mapping[str, Any],
                run_id: str | None = None, at: str | None = None) -> int:
    """Store `payload` as a canonical-JSON artifact, then write the queued job naming its hash (9.1: a job before a
    send); returns the job id. An identical job (same run, kind, destination and payload) still queued or failing is
    not written twice: its id is returned."""
    sha = sha256_id(payload)
    same = store.query("SELECT job_id FROM outbox WHERE run_id IS ? AND kind = ? AND destination = ? AND "
                       "payload_sha256 = ? AND state IN ('queued','failed') ORDER BY job_id LIMIT 1",
                       (run_id, kind, destination, sha))
    if same:
        return int(same[0][0])
    _store_payload(payloads, payload)
    return store.enqueue(OutboxJob(kind, destination, sha, created_at=utc(at) or now(), run_id=run_id))


def _held(store: Store, job: OutboxJob) -> bool:
    """An older job to the same destination is still open (choice 3)."""
    if job.kind not in ORDERED_KINDS:
        return False
    return bool(store.query(
        "SELECT 1 FROM outbox WHERE kind = ? AND destination = ? AND state IN ('queued','failed') AND "
        "(created_at < ? OR (created_at = ? AND job_id < ?)) LIMIT 1",
        (job.kind, job.destination, job.created_at, job.created_at, job.job_id)))


def due_jobs(store: Store, now_: str) -> list[OutboxJob]:
    """The jobs `dispatch` would attempt at `now_`: queued or failing, due, and not held behind an older post to the
    same destination (choice 3); oldest first."""
    return [j for j in store.due(utc(now_)) if not _held(store, j)]


def abandoned_jobs(store: Store, run_id: str | None = None) -> list[OutboxJob]:
    """Abandoned jobs no human has closed, oldest first: the daily digest's and the run page's list (9.1, 9.10)."""
    sql = "SELECT * FROM outbox WHERE state = 'abandoned' AND closed_by IS NULL"
    args: tuple = ()
    if run_id is not None:
        sql, args = sql + " AND run_id = ?", (run_id,)
    return [OutboxJob(**dict(r)) for r in store.query(sql + " ORDER BY created_at, job_id", args)]


def close_job(store: Store, job_id: int, *, by: str, at: str | None = None) -> OutboxJob | None:
    """A human closes an abandoned job (9.1, 9.12: within one business day of its digest line); returns it. The
    store refuses a job that is not abandoned, one already closed and a closer who is not a person."""
    store.close_job(job_id, by=by, at=at)
    return store.outbox_job(job_id)


class ReporterSink:
    """The egress sink of the Reporter's requests (outbox sends, the PR comment lookup): rows go to `egress_log`; a
    refusal's finding is not written, since the run has usually ended: the gate keeps it in `findings` and the
    caller returns it (choice 5)."""

    def __init__(self, store: Store):
        self._store = store

    def log_egress(self, row: EgressRow) -> int:
        return self._store.log_egress(row)

    def write_finding(self, finding: Finding, *, synthetic: bool = False) -> None:
        return None


def _load(payloads: PayloadStore, job: OutboxJob) -> tuple[bytes, dict]:
    data = payloads.get(_hex(job.payload_sha256))
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("an outbox payload is a JSON object")
    return data, payload


def _is_outage_alert(payload: Mapping[str, Any] | None) -> bool:
    return isinstance(payload, Mapping) and payload.get("alert") == OUTAGE_ALERT


def _channel_alerted(store: Store, payloads: PayloadStore, job: OutboxJob, at: datetime) -> bool:
    """An older job of the same channel is still failing past its own 15 minutes, so it raised this outage's alert
    (choice 2); outage alerts themselves raise none and so do not count."""
    channel = channel_of(job.kind)
    kinds = [k for k in OUTBOX_KINDS if channel_of(k) == channel]
    rows = store.query(
        f"SELECT * FROM outbox WHERE kind IN ({','.join('?' * len(kinds))}) AND state = 'failed' AND job_id <> ? AND "
        "(created_at < ? OR (created_at = ? AND job_id < ?)) AND created_at <= ? ORDER BY created_at, job_id",
        (*kinds, job.job_id, job.created_at, job.created_at, job.job_id, _iso(at - OUTAGE_AFTER)))
    for r in rows:
        try:
            _, payload = _load(payloads, OutboxJob(**dict(r)))
        except (ArtifactError, ValueError):
            return True
        if not _is_outage_alert(payload):
            return True
    return False


def _run_facts(store: Store, run_id: str | None) -> str:
    if run_id is None:
        return "no run"
    r = store.query("SELECT r.run_type, r.outcome, p.candidate_build_id, p.base_build_id FROM runs r "
                    "LEFT JOIN build_pairs p ON p.pair_id = r.pair_id WHERE r.run_id = ?", (run_id,))
    if not r:
        return f"run {run_id}"
    row = r[0]
    pair = f" | {row['candidate_build_id']} vs {row['base_build_id']}" if row["candidate_build_id"] else ""
    return f"{row['run_type']} {run_id}{pair} | {row['outcome'] or 'running'}"


def outage_alert(store: Store, job: OutboxJob, error: str, at: str) -> dict[str, Any]:
    """The integration outage alert payload for a job failing 15 minutes (9.10), its `text` in the 9.10 message
    format: severity and alert, run type and id, build pair, run outcome, then one line of detail."""
    channel = channel_of(job.kind)
    detail = (f"{channel}: outbox job {job.job_id} ({job.kind} to {job.destination}) failing since {job.created_at}: "
              f"{error}; retrying until 24 hours")
    return {"alert": OUTAGE_ALERT, "severity": OUTAGE_SEVERITY, "channel": channel, "raised_at": at,
            "job": {"job_id": job.job_id, "kind": job.kind, "destination": job.destination, "run_id": job.run_id,
                    "created_at": job.created_at},
            "text": f"[Sentinel] {OUTAGE_SEVERITY} integration outage | {_run_facts(store, job.run_id)} | {detail}"}


def _fail(store: Store, payloads: PayloadStore, jid: int, job: OutboxJob, payload: dict | None, error: str, at: str,
          alert_to: Sequence[AlertDestination], report: DispatchReport) -> Attempt:
    created, t = _dt(job.created_at), _dt(at)
    retry = next_attempt_at(job.created_at, at)
    crossing = t - created >= OUTAGE_AFTER and (
        job.attempts == 0 or (job.next_attempt_at is not None and _dt(job.next_attempt_at) - created <= OUTAGE_AFTER))
    alerts: list[OutboxJob] = []
    if crossing and not _is_outage_alert(payload) and not _channel_alerted(store, payloads, job, t):
        body = outage_alert(store, job, error, at)
        _, sha = _store_payload(payloads, body)
        alerts = [OutboxJob(d.kind, d.destination, sha, created_at=at, run_id=job.run_id)
                  for d in alert_to if d.channel != channel_of(job.kind)]
    state = "failed" if retry is not None else "abandoned"
    with store.batch("outbox_attempt") as b:
        report.alerts.extend(b.insert("outbox", _row(a)) for a in alerts)
        changed = b.update("outbox", {"job_id": jid, "state": job.state},
                           {"state": state, "attempts": job.attempts + 1, "last_error": error[:2000],
                            "next_attempt_at": retry})
        if changed != 1:
            raise StoreRefused(f"outbox job {jid} changed during its attempt (9.1)")
    if state == "abandoned":
        report.abandoned.append(jid)
    return Attempt(jid, job.kind, False, state, error, retry)


def _error_text(e: BaseException) -> str:
    code = getattr(e, "status_code", None)
    return f"{type(e).__name__}{f' (HTTP {code})' if code else ''}: {e}"


def _attempt(store: Store, payloads: PayloadStore, sender: Sender, jid: int, job: OutboxJob, at: str,
             allowlist: EgressAllowlist, alert_to: Sequence[AlertDestination], report: DispatchReport) -> Attempt:
    payload: dict | None = None
    try:
        data, payload = _load(payloads, job)
    except (ArtifactError, ValueError) as e:
        return _fail(store, payloads, jid, job, None, f"payload {job.payload_sha256} unavailable: {e}", at, alert_to,
                     report)
    purpose = egress_purpose(job.kind) or getattr(sender, "purpose", None)
    if purpose is None:
        return _fail(store, payloads, jid, job, payload, f"no egress purpose for {job.kind} posts (6.10)", at, alert_to,
                     report)
    gate = EgressGate(allowlist, ReporterSink(store), run_id=job.run_id, clock=lambda: at)
    try:
        delivery = gate.request(sender.host, lambda: sender.send(job, payload), purpose=purpose, nbytes=len(data),
                                status_of=lambda d: getattr(d, "status", None),
                                request_id_of=lambda d: getattr(d, "request_id", None))
    except EgressRefused as e:
        report.refusals.extend(gate.findings)
        return _fail(store, payloads, jid, job, payload, str(e), at, alert_to, report)
    except Exception as e:                       # any sender failure is a failed attempt (choice 5)
        return _fail(store, payloads, jid, job, payload, _error_text(e), at, alert_to, report)
    store.mark_sent(jid, at=at)
    return Attempt(jid, job.kind, True, "sent", ref=getattr(delivery, "ref", None))


def dispatch(store: Store, payloads: PayloadStore, senders: Mapping[str, Sender], now_: str, *,
             allowlist: EgressAllowlist, alert_to: Sequence[AlertDestination] = ()) -> DispatchReport:
    """Attempt every due job at `now_`, oldest first (9.1): a delivered job is `sent`; a failed one is retried on the
    schedule, raises the outage alert to `alert_to` at 15 minutes and is `abandoned` at 24 hours (module docstring).
    A post held behind an older one to the same destination is attempted in the same pass once that one is sent, and
    the outage alerts the pass raises are attempted at its end."""
    at = utc(now_) or now_
    report = DispatchReport()
    for queued in store.due(at):
        _one(store, payloads, senders, queued.job_id, at, allowlist, alert_to, report)
    for alert_id in list(report.alerts):        # an outage alert raises no further alert (choice 2): no loop
        _one(store, payloads, senders, alert_id, at, allowlist, alert_to, report)
    return report


def _one(store: Store, payloads: PayloadStore, senders: Mapping[str, Sender], job_id: int | None, at: str,
         allowlist: EgressAllowlist, alert_to: Sequence[AlertDestination], report: DispatchReport) -> None:
    job = None if job_id is None else store.outbox_job(job_id)
    if job_id is None or job is None or job.state not in ("queued", "failed"):
        return
    sender = senders.get(job.kind)
    if sender is None or _held(store, job):
        report.waiting.append(job_id)
        return
    report.attempts.append(_attempt(store, payloads, sender, job_id, job, at, allowlist, alert_to, report))


__all__ = ["AlertDestination", "Attempt", "Delivery", "DeliveryError", "DispatchReport", "OUTAGE_AFTER",
           "OUTAGE_ALERT", "ORDERED_KINDS", "PayloadStore", "Payloads", "RETRY_MINUTES", "ReporterSink", "Sender",
           "abandoned_jobs",
           "alert_destinations", "channel_of", "close_job", "dispatch", "due_jobs", "egress_purpose", "enqueue_job",
           "next_attempt_at", "outage_alert"]
