"""The PR host side of the Reporter: one comment per PR edited in place, and the `sentinel/pr-run` commit status
(9.1, 9.2, 9.6, 10.7).

Every post goes through the outbox (`report.outbox`): this module renders and enqueues; `outbox.dispatch` with
`senders(host)` delivers.

- `PrHost` is the PR host's API as the Reporter uses it (list, create and edit comments; set a commit status).
  `FakePrHost` is the in-memory host the tests and the simulated runs use; the live GitHub Enterprise transport is
  deferred (no network here). `PrCommentSender` finds the PR's comment by its hidden marker
  `<!-- sentinel:pr:{pr} -->` at send time and edits it in place, or creates it when it is missing (9.1).
- `post_pending(...)`: the `pending` status at T+0:00, from the run row (9.2).
- `post_report(store, payloads, run_id, host, context, ...)`: at `reported`, render the comment from the store
  (`ReportInputs.from_store`) and enqueue the comment and the final status. It fills the facts the caller cannot:
  the history of earlier runs of the same PR from the store, `marker_recreated` when the marker comment is gone
  after an earlier run posted one, `report_late` when the comment is first posted after the budget deadline and
  `comment_overdue` when none was posted by the deadline plus 15 minutes (status `error`, "Sentinel did not
  report"). Call it again after every decision write (the status is re-posted within one minute, 9.2) and every
  Reviewer verdict that lands (the comment is edited in place, 10.7): it re-reads the store and enqueues both again.
- `overdue_runs(store, now)` and `post_overdue(...)`: the Reporter host's timer, the `error` status for a PR run
  with no comment posted by budget plus 15 minutes (9.2, 9.10).
- `announce_check_mode(...)`: a commit status mode change is announced in the release channel once (9.2).
- `repost(store, payloads, run_id, host, ...)`: `post_report` again from what the first one stored (the report
  context and manifest it rendered with, under `summary_json["pr_report"]`), for the Reporter service after a
  decision write and for the review drain after a landed verdict (9.2, 10.7).
- `post_withheld(...)`: 6.9 withholds the report: the run's open PR comment and status jobs are abandoned (a human
  scans the run before anything posts) and the `error` status "Sentinel did not report" is queued in their place.
- `FilePrHost(path)`: the in-memory host kept in a JSON file, the simulated runs' PR host across processes.

Choices where the spec is ambiguous (reported):

1. A run's PR number is the trailing number of `runs.pr_ref` (`PR 4812`); the candidate commit is the candidate
   build's `source_commit` through the run's build pair. A run with no build pair posts no status.
2. Only the latest finished PR run of a PR edits the PR's one comment: re-posting an older run (after a decision on
   its finding) re-posts that run's status on its own commit and leaves the comment to the newer run.
3. The history lists the earlier finished PR runs of the same `pr_ref`, newest first: run id, candidate build,
   result (with its reason), the Unexpected, Intended and Suppressed counts that `post_report` recorded for that run
   (else its non-zero outcome counts) and when its comment was first posted.
4. `marker_recreated` is decided at render time from one lookup of the PR's comments (one `egress_log` row,
   purpose `pr_comment`): the marker is missing and a comment for this PR was sent before (by an earlier run or an
   earlier render of this one); before the PR's first comment there is no lookup. Once recreated,
   the run's later renders keep the note. A lookup that fails assumes the marker is present; the sender still
   creates a missing comment, without the note.
5. `report_late`: the run's first comment was sent after `started_at + budget_sec`, or none was sent and the render
   is after it (the comment rendered now is posted now). `comment_overdue`: no comment was sent by the deadline plus
   15 minutes and the render is past that point; once overdue, the status stays `error` (9.2).
6. The check mode is the `policy/reporting.yaml` mode in force on the America/New_York date of the post (the
   operating day's clock, 9.10); without a config the context's mode is used.
7. The overdue scan looks back 24 hours (the outbox's window, 9.1), so an old run without a comment is not
   re-posted after an outage of the Reporter host itself.
8. The mode announcement is posted once per change: the entry in force, when its mode differs from the entry
   before it (the first entry is no change), as one deterministic payload to the release channel.
9. A re-post renders with the report context and manifest the first `post_report` stored, plus the run row's
   check override (9.6 Override check), so a decision written from the report UI re-renders the comment the run
   posted with its new state, without the pipeline's in-memory facts.
10. A withheld report (6.9) abandons the run's open `pr_comment` and `commit_status` jobs rather than deleting
    them: abandoned jobs stay listed on the digest and the run page until the human who scans the run closes them.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..calendar import ny
from ..canonical import sha256_id
from ..egress import EgressAllowlist, EgressGate
from ..records import OutboxJob, now
from ..store.store import Store, utc
from .inline import Urls
from .model import CHECK_CONTEXT, CommitStatus, ReportContext, ReportInputs, RunReport, pending_status
from .outbox import Delivery, PayloadStore, ReporterSink, Sender, enqueue_job
from .pr_comment import comment_marker, render_report_markdown
from .reporting_config import ReportingConfig

OVERDUE_AFTER = timedelta(minutes=15)            # 9.2: no comment by budget plus 15 minutes
DID_NOT_REPORT = "Sentinel did not report"       # 9.2 error description
LOOKBACK = timedelta(hours=24)                   # choice 7
SUMMARY_KEY = "pr_report"                        # what post_report records on the run's summary_json
_PR_NUMBER = re.compile(r"(\d+)\s*$")


@dataclass(frozen=True)
class Comment:
    """One comment on a PR, as the host lists it."""
    comment_id: str
    body: str


class PrHostError(RuntimeError):
    """The PR host refused or failed a request; `status_code` is the HTTP status when there was a response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PrHost(Protocol):
    """The PR host API the Reporter uses (GitHub Enterprise assumed, 9.1). `host` is its allowlisted host name."""
    host: str

    def list_comments(self, pr: int) -> Sequence[Comment]: ...

    def create_comment(self, pr: int, body: str) -> str: ...

    def edit_comment(self, pr: int, comment_id: str, body: str) -> None: ...

    def set_status(self, commit: str, *, context: str, state: str, description: str,
                   target_url: str | None) -> None: ...


class FakePrHost:
    """An in-memory PR host: comments per PR, the current status per (commit, context) and every status posted.
    `down` fails every request with HTTP 503; `delete_comment` stands for a human or the host losing a comment."""

    def __init__(self, host: str = "scm.example.internal"):
        self.host = host
        self.comments: dict[int, list[Comment]] = {}
        self.statuses: dict[tuple[str, str], dict[str, Any]] = {}
        self.status_log: list[dict[str, Any]] = []
        self.down = False
        self._next_id = 0

    def _up(self) -> None:
        if self.down:
            raise PrHostError("PR host unavailable", status_code=503)

    def list_comments(self, pr: int) -> list[Comment]:
        self._up()
        return list(self.comments.get(pr, []))

    def create_comment(self, pr: int, body: str) -> str:
        self._up()
        self._next_id += 1
        cid = f"c{self._next_id}"
        self.comments.setdefault(pr, []).append(Comment(cid, body))
        return cid

    def edit_comment(self, pr: int, comment_id: str, body: str) -> None:
        self._up()
        found = self.comments.get(pr, [])
        for i, c in enumerate(found):
            if c.comment_id == comment_id:
                found[i] = Comment(comment_id, body)
                return
        raise PrHostError(f"comment {comment_id} not found on PR {pr}", status_code=404)

    def delete_comment(self, pr: int, comment_id: str) -> None:
        self.comments[pr] = [c for c in self.comments.get(pr, []) if c.comment_id != comment_id]

    def set_status(self, commit: str, *, context: str, state: str, description: str,
                   target_url: str | None) -> None:
        self._up()
        posted = {"commit": commit, "context": context, "state": state, "description": description,
                  "target_url": target_url}
        self.statuses[(commit, context)] = posted
        self.status_log.append(posted)


class FilePrHost(FakePrHost):
    """`FakePrHost` kept in a JSON file, so the simulated runs and the demo's control host share one PR host across
    processes (the comment a run's job created is found by the next run's marker lookup)."""

    def __init__(self, path: str | Path, host: str = "scm.example.internal"):
        super().__init__(host)
        self.path = Path(path)
        if self.path.exists():
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.comments = {int(pr): [Comment(**c) for c in cs] for pr, cs in doc["comments"].items()}
            self.status_log = list(doc["status_log"])
            self.statuses = {(s["commit"], s["context"]): s for s in self.status_log}
            self._next_id = int(doc["next_id"])

    def _save(self) -> None:
        doc = {"host": self.host, "next_id": self._next_id, "status_log": self.status_log,
               "comments": {str(pr): [dataclasses.asdict(c) for c in cs] for pr, cs in sorted(self.comments.items())}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def create_comment(self, pr: int, body: str) -> str:
        cid = super().create_comment(pr, body)
        self._save()
        return cid

    def edit_comment(self, pr: int, comment_id: str, body: str) -> None:
        super().edit_comment(pr, comment_id, body)
        self._save()

    def delete_comment(self, pr: int, comment_id: str) -> None:
        super().delete_comment(pr, comment_id)
        self._save()

    def set_status(self, commit: str, *, context: str, state: str, description: str,
                   target_url: str | None) -> None:
        super().set_status(commit, context=context, state=state, description=description, target_url=target_url)
        self._save()


def find_marker(comments: Sequence[Comment], pr: int) -> Comment | None:
    """The PR's Sentinel comment: the first whose body starts with the marker (9.1)."""
    marker = comment_marker(pr)
    return next((c for c in comments if c.body.startswith(marker)), None)


class PrCommentSender:
    """The outbox sender of `pr_comment` jobs: edits the marker comment in place, or creates it (9.1)."""

    def __init__(self, pr_host: PrHost):
        self.pr_host = pr_host
        self.host = pr_host.host

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        pr, body = int(payload["pr"]), str(payload["body"])
        found = find_marker(self.pr_host.list_comments(pr), pr)
        if found is not None:
            self.pr_host.edit_comment(pr, found.comment_id, body)
            return Delivery(ref=found.comment_id)
        return Delivery(ref=self.pr_host.create_comment(pr, body))


class CommitStatusSender:
    """The outbox sender of `commit_status` jobs (9.2)."""

    def __init__(self, pr_host: PrHost):
        self.pr_host = pr_host
        self.host = pr_host.host

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        self.pr_host.set_status(str(payload["commit"]), context=str(payload["context"]), state=str(payload["state"]),
                                description=str(payload["description"]), target_url=payload.get("target_url"))
        return Delivery(ref=str(payload["state"]))


def senders(pr_host: PrHost) -> dict[str, Sender]:
    """The outbox senders of the PR host's two kinds, for `outbox.dispatch`."""
    return {"pr_comment": PrCommentSender(pr_host), "commit_status": CommitStatusSender(pr_host)}


def comment_destination(pr: int) -> str:
    return f"pr:{int(pr)}"


def status_destination(commit: str, context: str = CHECK_CONTEXT) -> str:
    return f"commit:{commit}#{context}"


def pr_number(pr_ref: str | None) -> int | None:
    """The PR number at the end of `runs.pr_ref` (`PR 4812`), or None (choice 1)."""
    m = _PR_NUMBER.search(pr_ref or "")
    return int(m.group(1)) if m else None


def _dt(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PrRun:
    """The run-row facts the PR host posts need (8.3 `runs`, `build_pairs`, `builds`)."""
    run_id: str
    pr_ref: str
    pr: int
    started_at: str
    budget_sec: int
    lifecycle: str
    commit: str | None = None
    candidate_build: str | None = None
    base_build: str | None = None

    @property
    def deadline(self) -> datetime:
        """The PR run's budget deadline (9.1 `report_late`)."""
        return _dt(self.started_at) + timedelta(seconds=self.budget_sec)

    @property
    def overdue_at(self) -> datetime:
        """Budget plus 15 minutes: no comment by then is status `error` (9.2)."""
        return self.deadline + OVERDUE_AFTER


def pr_run(store: Store, run_id: str) -> PrRun:
    """The PR run's facts; ValueError for an unknown run, a run of another type or one without a PR number."""
    rows = store.query("SELECT r.run_id, r.run_type, r.pr_ref, r.started_at, r.budget_sec, r.lifecycle, "
                       "p.candidate_build_id, p.base_build_id, b.source_commit FROM runs r "
                       "LEFT JOIN build_pairs p ON p.pair_id = r.pair_id "
                       "LEFT JOIN builds b ON b.build_id = p.candidate_build_id WHERE r.run_id = ?", (run_id,))
    if not rows:
        raise ValueError(f"unknown run {run_id}")
    r = rows[0]
    pr = pr_number(r["pr_ref"])
    if r["run_type"] != "pr" or pr is None:
        raise ValueError(f"run {run_id} is not a PR run with a PR number (pr_ref {r['pr_ref']!r})")
    return PrRun(run_id, r["pr_ref"], pr, r["started_at"], r["budget_sec"], r["lifecycle"], r["source_commit"],
                 r["candidate_build_id"], r["base_build_id"])


def latest_pr_run(store: Store, pr_ref: str) -> str | None:
    """The latest finished PR run of a PR: the one whose comment the PR shows (choice 2)."""
    rows = store.query("SELECT run_id FROM runs WHERE run_type = 'pr' AND pr_ref = ? AND lifecycle = 'finished' "
                       "ORDER BY started_at DESC, run_id DESC LIMIT 1", (pr_ref,))
    return rows[0][0] if rows else None


def _first_comment_sent(store: Store, run_id: str) -> str | None:
    r = store.query("SELECT min(sent_at) FROM outbox WHERE run_id = ? AND kind = 'pr_comment' AND state = 'sent'",
                    (run_id,))
    return r[0][0] if r else None


def history(store: Store, run_id: str) -> tuple[dict, ...]:
    """The earlier finished PR runs of the same PR, newest first, as `ReportContext.history` rows (choice 3)."""
    run = pr_run(store, run_id)
    rows = store.query("SELECT r.run_id, r.outcome, r.abort_reason, r.abort_detail, r.summary_json, "
                       "p.candidate_build_id FROM runs r LEFT JOIN build_pairs p ON p.pair_id = r.pair_id "
                       "WHERE r.run_type = 'pr' AND r.pr_ref = ? AND r.lifecycle = 'finished' AND r.run_id <> ? "
                       "AND r.started_at <= ? ORDER BY r.started_at DESC, r.run_id DESC",
                       (run.pr_ref, run_id, run.started_at))
    out = []
    for r in rows:
        summary = store.run_summary(r["run_id"])
        posted = (summary.get(SUMMARY_KEY) or {}).get("counts")
        if isinstance(posted, dict):
            counts = ", ".join(f"{posted.get(k, 0)} {k}" for k in ("unexpected", "intended", "suppressed"))
        else:
            counts = ", ".join(f"{v} {k}" for k, v in (summary.get("counts") or {}).items() if v)
        reason = ":".join(x for x in (r["abort_reason"], r["abort_detail"]) if x)
        out.append({"run_id": r["run_id"], "candidate": r["candidate_build_id"] or "",
                    "result": r["outcome"] + (f" (reason: {reason})" if reason else ""), "counts": counts,
                    "reported": _first_comment_sent(store, r["run_id"]) or "not posted"})
    return tuple(out)


def _comment_ever_sent(store: Store, pr: int) -> bool:
    return bool(store.query("SELECT 1 FROM outbox WHERE kind = 'pr_comment' AND destination = ? AND state = 'sent' "
                            "LIMIT 1", (comment_destination(pr),)))


def _marker_missing(store: Store, host: PrHost, run: PrRun, allowlist: EgressAllowlist, at: str) -> bool:
    """Choice 4: a comment for this PR was sent before and the host no longer shows the marker comment."""
    if not _comment_ever_sent(store, run.pr):
        return False
    gate = EgressGate(allowlist, ReporterSink(store), run_id=run.run_id, clock=lambda: at)
    try:
        comments = gate.request(host.host, lambda: host.list_comments(run.pr), purpose="pr_comment")
    except Exception:                           # unknown: the sender creates the comment if it is missing
        return False
    return find_marker(comments, run.pr) is None


def _mode(reporting: ReportingConfig | None, fallback: str, at: str) -> str:
    return reporting.check_mode(ny(_dt(at)).date()) if reporting is not None else fallback


def _status_payload(commit: str, status: CommitStatus) -> dict[str, Any]:
    return {"commit": commit, "context": status.context, "state": status.state, "description": status.description,
            "target_url": status.target_url}


def _enqueue_status(store: Store, payloads: PayloadStore, run: PrRun, status: CommitStatus, at: str) -> int | None:
    if run.commit is None:
        return None
    return enqueue_job(store, payloads, kind="commit_status",
                       destination=status_destination(run.commit, status.context),
                       payload=_status_payload(run.commit, status), run_id=run.run_id, at=at)


def post_pending(store: Store, payloads: PayloadStore, run_id: str, *, base_url: str,
                 reporting: ReportingConfig | None = None, check_mode: str = "informational",
                 now_: str | None = None) -> int | None:
    """9.2 `pending` at T+0:00 from the run row: "Sentinel run started, base = {base_build}"; returns its job id (None
    for a run without a build pair)."""
    at = utc(now_) or now()
    run = pr_run(store, run_id)
    status = pending_status(run.base_build or "unknown", _mode(reporting, check_mode, at),
                            Urls(base_url, run_id).report)
    return _enqueue_status(store, payloads, run, status, at)


@dataclass(frozen=True)
class Posted:
    """What `post_report` enqueued and the facts it computed."""
    run_id: str
    status: CommitStatus
    comment: str
    comment_job: int | None              # None when a later run of the PR owns the comment (choice 2)
    status_job: int | None               # None when the run has no candidate commit (choice 1)
    marker_recreated: bool
    report_late: bool
    comment_overdue: bool
    history: tuple[dict, ...] = field(default_factory=tuple)
    report: RunReport | None = field(default=None, compare=False, repr=False)   # what was rendered (report.html)


def post_report(store: Store, payloads: PayloadStore, run_id: str, host: PrHost, context: ReportContext, *,
                allowlist: EgressAllowlist, manifest: dict | None = None, reporting: ReportingConfig | None = None,
                now_: str | None = None) -> Posted:
    """Render the finished PR run's comment from the store and enqueue it with the final commit status (9.1, 9.2;
    module docstring). `context` carries what no record holds (builds, banners, abort facts, base URL); its PR
    number, check mode, history and the three posting flags are filled here. Call again on every decision write and
    every landed verdict: the comment is edited in place and the status re-posted."""
    at = utc(now_) or now()
    run = pr_run(store, run_id)
    summary = store.run_summary(run_id)
    prior = summary.get(SUMMARY_KEY) or {}
    first = _first_comment_sent(store, run_id)
    t = _dt(at)
    report_late = _dt(first or at) > run.deadline
    comment_overdue = t > run.overdue_at and (first is None or _dt(first) > run.overdue_at)
    owner = latest_pr_run(store, run.pr_ref) == run_id
    recreated = bool(prior.get("marker_recreated")) or (owner and _marker_missing(store, host, run, allowlist, at))
    past = history(store, run_id)
    ctx = replace(context, pr=run.pr, check_mode=_mode(reporting, context.check_mode, at), history=past,
                  marker_recreated=recreated, report_late=report_late, comment_overdue=comment_overdue)
    report = ReportInputs.from_store(store, run_id, at=at).build(manifest, ctx)
    body = render_report_markdown(report)
    comment_job = enqueue_job(store, payloads, kind="pr_comment", destination=comment_destination(run.pr),
                              payload={"pr": run.pr, "marker": comment_marker(run.pr), "body": body},
                              run_id=run_id, at=at) if owner else None
    status_job = _enqueue_status(store, payloads, run, report.status, at)
    store.update_summary(run_id, {SUMMARY_KEY: {
        **prior, "rendered_at": at, "result": report.result, "marker_recreated": recreated,
        "report_late": report_late, "comment_overdue": comment_overdue, "comment_job": comment_job,
        "status_job": status_job, "counts": {"unexpected": report.n_unexpected, "intended": report.n_intended,
                                             "suppressed": report.n_suppressed},
        "context": prior.get("context") or _stored_context(context),
        "manifest": prior["manifest"] if "manifest" in prior else manifest}})
    return Posted(run_id, report.status, body, comment_job, status_job, recreated, report_late, comment_overdue, past,
                  report)


_FILLED = ("pr", "check_mode", "history", "marker_recreated", "report_late", "comment_overdue", "override")


def _stored_context(context: ReportContext) -> dict[str, Any]:
    """The caller's report context without the fields `post_report` fills (choice 9)."""
    return {k: v for k, v in dataclasses.asdict(context).items() if k not in _FILLED}


def repost(store: Store, payloads: PayloadStore, run_id: str, host: PrHost, *, allowlist: EgressAllowlist,
           reporting: ReportingConfig | None = None, now_: str | None = None) -> Posted | None:
    """`post_report` again for a PR run already reported once (choice 9): after a decision write or a landed
    verdict. None for a run that is not a PR run with a PR number or that was never reported."""
    try:
        pr_run(store, run_id)
    except ValueError:
        return None
    prior = store.run_summary(run_id).get(SUMMARY_KEY) or {}
    stored = prior.get("context")
    if not isinstance(stored, dict):
        return None
    known = {f.name for f in dataclasses.fields(ReportContext)}
    kw = {k: v for k, v in stored.items() if k in known}
    row = store.query("SELECT check_override_by, check_override_reason FROM runs WHERE run_id = ?", (run_id,))[0]
    override = {"by": row[0], "reason": row[1]} if row[0] else None
    context = ReportContext(pr=0, override=override, **kw)
    return post_report(store, payloads, run_id, host, context, allowlist=allowlist, manifest=prior.get("manifest"),
                       reporting=reporting, now_=now_)


def post_withheld(store: Store, payloads: PayloadStore, run_id: str, *, description: str, base_url: str,
                  reporting: ReportingConfig | None = None, check_mode: str = "informational",
                  now_: str | None = None) -> int | None:
    """6.9: the report is withheld. The run's open `pr_comment` and `commit_status` jobs are abandoned (choice 10)
    and the `error` status with `description` is queued; returns its job id (None for a run with no PR commit)."""
    at = utc(now_) or now()
    run = pr_run(store, run_id)
    for r in store.query("SELECT job_id FROM outbox WHERE run_id = ? AND kind IN ('pr_comment','commit_status') "
                         "AND state IN ('queued','failed') ORDER BY job_id", (run_id,)):
        store.abandon(r[0])
    status = CommitStatus("error", description, _mode(reporting, check_mode, at), None)
    return _enqueue_status(store, payloads, run, status, at)


def overdue_runs(store: Store, now_: str) -> list[str]:
    """PR runs past budget plus 15 minutes (within the last 24 hours, choice 7) with no comment sent by then and no
    overdue status posted yet: what the Reporter host's timer passes to `post_overdue` (9.2)."""
    t = _dt(utc(now_) or now_)
    out = []
    for r in store.query("SELECT run_id, started_at, budget_sec, summary_json FROM runs WHERE run_type = 'pr' "
                         "AND pr_ref IS NOT NULL AND pair_id IS NOT NULL ORDER BY started_at, run_id"):
        due = _dt(r["started_at"]) + timedelta(seconds=r["budget_sec"]) + OVERDUE_AFTER
        if not (t - LOOKBACK <= due <= t):
            continue
        if (json.loads(r["summary_json"] or "{}").get(SUMMARY_KEY) or {}).get("overdue_job") is not None:
            continue
        first = _first_comment_sent(store, r["run_id"])
        if first is None or _dt(first) > due:
            out.append(r["run_id"])
    return out


def post_overdue(store: Store, payloads: PayloadStore, run_id: str, *, base_url: str,
                 reporting: ReportingConfig | None = None, check_mode: str = "informational",
                 now_: str | None = None) -> int | None:
    """No comment posted by budget plus 15 minutes: enqueue status `error`, "Sentinel did not report", once (9.2,
    9.10); returns the job id, or None when the run is not overdue, was already marked or has no commit."""
    at = utc(now_) or now()
    run = pr_run(store, run_id)
    first = _first_comment_sent(store, run_id)
    prior = store.run_summary(run_id).get(SUMMARY_KEY) or {}
    if _dt(at) < run.overdue_at or (first is not None and _dt(first) <= run.overdue_at) or \
            prior.get("overdue_job") is not None:
        return None
    status = CommitStatus("error", DID_NOT_REPORT, _mode(reporting, check_mode, at), Urls(base_url, run_id).report)
    job = _enqueue_status(store, payloads, run, status, at)
    if job is not None:
        store.update_summary(run_id, {SUMMARY_KEY: {**prior, "overdue_job": job, "comment_overdue": True}})
    return job


def announce_check_mode(store: Store, payloads: PayloadStore, reporting: ReportingConfig, *,
                        now_: str | None = None) -> int | None:
    """Announce the commit status mode in force in the release channel once, when it changed (choice 8, 9.2, 9.9);
    returns the job id, or None when there is nothing to announce or it was announced already."""
    at = utc(now_) or now()
    on: date = ny(_dt(at)).date()
    hist = reporting.check_mode_history
    in_force = [k for k, m in enumerate(hist) if m.since <= on]
    if not in_force or in_force[-1] == 0:
        return None
    i = in_force[-1]
    current = hist[i]
    if hist[i - 1].mode == current.mode:
        return None
    payload = {"announce": "check_mode", "context": CHECK_CONTEXT, "mode": current.mode,
               "since": current.since.isoformat(), "by": current.by,
               "text": f"[Sentinel] commit status {CHECK_CONTEXT} is {current.mode} from {current.since.isoformat()} "
                       f"(was {hist[i - 1].mode}; changed by {current.by})"}
    channel = reporting.channel("release")
    if store.query("SELECT 1 FROM outbox WHERE kind = 'slack' AND destination = ? AND payload_sha256 = ? LIMIT 1",
                   (channel, sha256_id(payload))):
        return None
    return enqueue_job(store, payloads, kind="slack", destination=channel, payload=payload, at=at)


__all__ = ["Comment", "CommitStatusSender", "DID_NOT_REPORT", "FakePrHost", "FilePrHost", "OVERDUE_AFTER",
           "PrCommentSender", "PrHost", "PrHostError", "PrRun", "Posted", "announce_check_mode", "comment_destination",
           "find_marker", "history", "latest_pr_run", "overdue_runs", "post_overdue", "post_pending", "post_report",
           "post_withheld", "pr_number", "pr_run", "repost", "senders", "status_destination"]
