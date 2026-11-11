"""The outbox senders of the control host (9.1, 9.2, 9.6, 9.9, 9.10): one per job kind, each over a transport
protocol, plus the in-memory transports of the simulated integrations.

`report.outbox.dispatch` hands each due job to the sender of its kind; the sender's `host` is the allowlisted host
the attempt is logged against (6.10). A sender also writes back what a delivery taught (the completions):

- `slack`: `SlackSender` over a `SlackClient`; a thread reply goes under the timestamp of its parent post (slack
  choice 5), which the sender keeps per thread key.
- `digest`: `DigestSender`; the email copy (destination `email`) to the `Mailer`, the post to Slack (digest choice 5).
- `page`: `PageSender`; a P1 email (destination `email`) to the `Mailer`, every other page, the outbox's integration
  outage alert included, to the `Pager` (`alerting.send_page_job`).
- `jira_create`, `jira_comment`, `jira_transition`: `JiraSender`, which carries the payload (`jira_sync.send`) and
  writes back at once (`jira_sync.record`: a new Bug's key on its decisions and findings through
  `Store.fill_ticket_ref`, a SENT ticket's key on its ledger row, ticket-not-found); a reopen the workflow forbids
  leaves its P2 route in `alerts` for the dispatcher (9.9).
- `pr_comment`, `commit_status`: `prhost.senders(pr_host)`.
- `manifest_pr`, `mask_pr`, `quarantine_pr`, `flow_pr`, `anchor_pr`: `ConfigPrSender` opens the sentinel-config pull
  request through the `ConfigRepo` (8.5); a delivered `mask_pr` marks its finding "mask requested, PR {n}"
  (`actions.mask_pr_opened`, 9.6).

`senders_for(store, transports, ...)` builds the map for one dispatch pass; a transport left out leaves its kinds'
jobs queued (outbox choice 6). `MemorySlack` and `MemoryConfigRepo` are the in-memory transports `sentinel serve
--integrations simulated` and the demo use beside `prhost.FilePrHost`, `jira.MemoryJira`, `alerting.MemoryPager` and
`alerting.MemoryMailer` (the live transports are Phase 2 items, 11.1).

Choices where the spec is silent (reported):
1. Hosts (6.10): Slack posts and the digest go to `slack.com`, Jira to the Jira host, pages to the paging host, the
   config pull requests to the SCM host, as in `policy/egress_allowlist.yaml`. The digest's email copy and a P1's
   email ride their job's sender, so their attempt is logged against that sender's host (the mail relay has no
   purpose of its own in 6.10).
2. A config pull request's title names the kind, the action and the checkpoint (or finding); its body is the job's
   payload as canonical JSON; its author is the payload's requester (`author`, `owner` or the mask's
   `requested_by`), else the Reporter; its files are the payload's `files` (Promote candidate flow, Accept anchor
   patch), and for a mask PR the mask record as `masks/<checkpoint id>/<region id>.yaml` (4.8).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import yaml

from ..canonical import canonical_json
from ..orchestrator.alerts import AlertRoute
from ..records import OutboxJob
from ..store.store import Store
from . import jira, jira_sync, prhost
from .actions import ConfigRepo, PullRequest, mask_pr_opened
from .alerting import EMAIL_DESTINATION, Mailer, Pager, send_page_job
from .outbox import Delivery, Sender

SLACK_HOST = "slack.com"
JIRA_HOST = "jira.example.internal"
PAGING_HOST = "paging.example.internal"
REPORTER = "reporter"                           # the author of a config PR no person requested (choice 2)
CONFIG_PR_KINDS = ("manifest_pr", "mask_pr", "quarantine_pr", "flow_pr", "anchor_pr")


class SlackClient(Protocol):
    """Slack as the Reporter posts to it: one message, optionally a reply under a parent's timestamp."""

    def post(self, *, channel: str, text: str, thread_ts: str | None = None, mentions: Sequence[str] = ()) -> str:
        """Post and return the message timestamp."""


@dataclass
class MemorySlack:
    """An in-memory `SlackClient` that records each post."""
    posts: list[dict[str, Any]] = field(default_factory=list)

    def post(self, *, channel: str, text: str, thread_ts: str | None = None, mentions: Sequence[str] = ()) -> str:
        ts = f"{len(self.posts) + 1}.000100"
        self.posts.append({"ts": ts, "channel": channel, "text": text, "thread_ts": thread_ts,
                           "mentions": list(mentions)})
        return ts


class SlackSender:
    """`slack` jobs: the post to the job's channel, a reply under its thread's parent post (choice 5 of slack)."""

    def __init__(self, client: SlackClient, host: str = SLACK_HOST):
        self.client, self.host = client, host
        self.parents: dict[str, str] = {}

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        thread = payload.get("thread")
        parent = self.parents.get(str(thread)) if payload.get("reply") and thread else None
        mentions = payload.get("mentions") or ()
        ts = self.client.post(channel=job.destination, text=str(payload.get("text") or canonical_json(payload)),
                              thread_ts=parent, mentions=tuple(mentions))
        if thread and not payload.get("reply"):
            self.parents.setdefault(str(thread), ts)
        return Delivery(ref=ts)


class DigestSender:
    """`digest` jobs: the email copy to the `Mailer`, the post to Slack (digest choice 5)."""

    def __init__(self, slack: SlackSender, mailer: Mailer):
        self.slack, self.mailer, self.host = slack, mailer, slack.host

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        if job.destination == EMAIL_DESTINATION:
            return Delivery(ref=self.mailer.send(to=payload.get("to") or (), subject=str(payload.get("subject")),
                                                 body=str(payload.get("text"))))
        return self.slack.send(job, payload)


class PageSender:
    """`page` jobs: a P1's email to the `Mailer`, every other page to the `Pager`; the outbox's integration outage
    alert (no `key`) pages the harness on-call."""

    def __init__(self, pager: Pager, mailer: Mailer, host: str = PAGING_HOST):
        self.pager, self.mailer, self.host = pager, mailer, host

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        if "key" not in payload:                                    # outbox.outage_alert (9.10 Integration outage)
            ref = self.pager.page(key=f"{payload.get('alert')}:{(payload.get('job') or {}).get('job_id')}",
                                  severity=str(payload.get("severity")), text=str(payload.get("text")), people=(),
                                  roles=("harness_on_call",))
            return Delivery(ref=ref)
        return Delivery(ref=send_page_job(json.dumps(dict(payload)).encode("utf-8"), pager=self.pager,
                                          mailer=self.mailer))


class JiraSender:
    """The three Jira kinds: deliver (`jira_sync.send`) and write back at once (`jira_sync.record`); `alerts` gathers
    the P2 routes of reopens the workflow forbade (9.9)."""

    def __init__(self, client: jira.JiraClient, store: Store, *, report_base: str, at: str, host: str = JIRA_HOST):
        self.client, self.store, self.report_base, self.at, self.host = client, store, report_base, at, host
        self.alerts: list[tuple[AlertRoute, OutboxJob, str]] = []

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        got = jira_sync.send(self.client, payload, store=self.store, report_base=self.report_base)
        if payload.get("op"):
            jira_sync.record(self.store, payload, got, at=self.at)
        if got.alert is not None:
            self.alerts.append((got.alert, job, f"{got.ticket}: the workflow refused the reopen transition"))
        return Delivery(ref=got.ticket)


def _author(payload: Mapping[str, Any]) -> str:
    mask = payload.get("mask") if isinstance(payload.get("mask"), Mapping) else {}
    return str(payload.get("author") or payload.get("owner") or mask.get("requested_by") or REPORTER)


class ConfigPrSender:
    """`*_pr` jobs: the sentinel-config pull request through the `ConfigRepo` (choice 2); a delivered `mask_pr`
    marks its finding "mask requested, PR {n}" (9.6)."""

    def __init__(self, repo: ConfigRepo, store: Store, host: str):
        self.repo, self.store, self.host = repo, store, host

    def send(self, job: OutboxJob, payload: Mapping[str, Any]) -> Delivery:
        mask = payload.get("mask") if isinstance(payload.get("mask"), Mapping) else None
        files = dict(payload.get("files") or {})
        if mask is not None:
            files[f"masks/{mask['checkpoint_id']}/{mask['region_id']}.yaml"] = yaml.safe_dump(dict(mask),
                                                                                             sort_keys=False)
        what = payload.get("checkpoint") or (mask or {}).get("checkpoint_id") or payload.get("finding") or ""
        number = self.repo.open(job.kind, title=f"{job.kind}: {payload.get('action', job.kind)} {what}".strip(),
                                body=canonical_json(payload), author=_author(payload), files=files)
        if job.kind == "mask_pr" and mask is not None and mask.get("decision_ref"):
            mask_pr_opened(self.store, str(mask["decision_ref"]), number)
        return Delivery(ref=str(number))


@dataclass
class MemoryConfigRepo:
    """An in-memory sentinel-config host (`actions.ConfigRepo`): every pull request it opened, merged or closed."""
    pulls: dict[int, PullRequest] = field(default_factory=dict)
    files: dict[int, dict[str, str]] = field(default_factory=dict)
    head: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    def pull(self, number: int) -> PullRequest | None:
        return self.pulls.get(number)

    def merge(self, number: int, *, by: str) -> str:
        self.pulls[number] = replace(self.pulls[number], state="merged")
        return f"merge-{number}"

    def close(self, number: int, *, by: str, reason: str) -> None:
        self.pulls[number] = replace(self.pulls[number], state="closed")

    def open(self, kind: str, *, title: str, body: str, author: str, files: Mapping[str, str]) -> int:
        number = len(self.pulls) + 1
        self.pulls[number] = PullRequest(number, kind, author)
        self.files[number] = dict(files)
        return number

    def head_regions(self) -> Mapping[str, Mapping[str, Any]]:
        return self.head


@dataclass
class Transports:
    """The integrations the control host posts through; None leaves that integration's jobs queued."""
    pr_host: prhost.PrHost | None = None
    jira: jira.JiraClient | None = None
    slack: SlackClient | None = None
    pager: Pager | None = None
    mailer: Mailer | None = None
    config_repo: ConfigRepo | None = None


def senders_for(store: Store, t: Transports, *, report_base: str, at: str,
                slack_sender: SlackSender | None = None) -> dict[str, Sender]:
    """The sender map of one dispatch pass at `at` over the transports `t` (module docstring). Pass the same
    `slack_sender` to every pass so thread replies find their parents."""
    out: dict[str, Sender] = {}
    if t.pr_host is not None:
        out.update(prhost.senders(t.pr_host))
    if t.config_repo is not None:
        host = t.pr_host.host if t.pr_host is not None else prhost.FakePrHost().host
        out.update(dict.fromkeys(CONFIG_PR_KINDS, ConfigPrSender(t.config_repo, store, host)))
    if t.jira is not None:
        js = JiraSender(t.jira, store, report_base=report_base, at=at)
        out.update(dict.fromkeys(jira.JIRA_KINDS, js))
    if t.slack is not None:
        s = slack_sender or SlackSender(t.slack)
        out["slack"] = s
        if t.mailer is not None:
            out["digest"] = DigestSender(s, t.mailer)
    if t.pager is not None and t.mailer is not None:
        out["page"] = PageSender(t.pager, t.mailer)
    return out


def jira_alerts(senders: Mapping[str, Sender]) -> list[tuple[AlertRoute, OutboxJob, str]]:
    """The P2 routes the pass's Jira deliveries raised (a forbidden reopen, 9.9)."""
    found = {id(s): s for s in senders.values() if isinstance(s, JiraSender)}
    return [a for s in found.values() for a in s.alerts]


__all__ = ["CONFIG_PR_KINDS", "ConfigPrSender", "DigestSender", "JIRA_HOST", "JiraSender", "MemoryConfigRepo",
           "MemorySlack", "PAGING_HOST", "PageSender", "SLACK_HOST", "SlackClient", "SlackSender", "Transports",
           "jira_alerts", "senders_for"]
