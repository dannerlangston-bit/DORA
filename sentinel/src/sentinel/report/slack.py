"""Slack: the four channels, what each post carries, the PR-run filter, threading and the cross-run dedup (9.9;
SD-85, SD-87). Every post is an outbox job before it is a send (9.1); nothing here talks to Slack.

- `SlackChannels.from_reporting(doc)` reads the four channel names of `policy/reporting.yaml` (4.16 `slack`). The
  rest of the module names a channel by its 9.10 recipient key (`alerts.ON_CALL_CHANNEL`, `TRIAGE_CHANNEL`,
  `RELEASE_CHANNEL`, `SAFETY_CHANNEL`) and the outbox job carries the configured name.
- `route(event)`: the channels a 9.10 alert route (`AlertRoute`) or a finding post (`FindingPost`) goes to.
- `FindingPost`, `should_post_pr` (the PR-run filter of 9.9) and `triage_post` (the triage-channel rule of 9.9 that
  the 9.10 "New high-confidence collateral" row uses, SD-87).
- `thread_key(run_id, alert_kind)`: one message per run and alert kind, later posts threaded under it (9.9 Dedup).
- `SlackHistory` and `cross_run_dedup`: a `collateral` checkpoint plus signature at most once per 24 hours across
  runs, and none when an open Jira ticket carries a matching `sentinel-dec-` label, a thread reply on the original
  post instead when it is under 7 days old (9.9 Dedup).
- `plan_finding_posts(posts, history=..., now=...)` applies all of that to a run's findings; `enqueue(store,
  messages, channels, now)` writes the outbox jobs and returns each payload for the caller to store as the artifact
  its hash names (8.3 outbox).
- `MessageFields` is the 9.10 message format of every channel: `[Sentinel] {severity} {alert} | {run_type}
  {run_id} | {build_pair} | {outcome or gate} | {surface and tier, if any} | {one line of detail} | {report link} |
  {crop link, if any}`; `slack_text` is the escaping of screen or model text in a Slack post.

Choices where the spec is ambiguous (each is reported):

1. Channel routing: every alert of 9.10 posts to the on-call channel (SD-85) except the triage-channel rows of 9.9
   (`new_collateral`, `decision_conflict`, `sla_escalation`, `needs_human_digest`), which post to triage only; a
   route of the 9.10 safety table also posts to `#sentinel-safety`, and nothing else does; a route that names the
   release or triage channel also posts there, and the `RELEASE_RULES` (`canary_failure`, `canary_path_divergence`,
   `rc_start_deadline`, 9.9 release row and 11.6) post to the release channel whichever module built their route
   (the run queue's `start_deadline.rc_start_deadline_alert` and `canary_integrations.alert_route` name none).
2. Finding posts are tier 1 only for the three kinds of 9.9 (`collateral`, `invariant_violation`,
   `assertion_failed`) on every run type, with no matching decision; a `collateral` posts only at reviewer
   confidence 0.80 or above, or when no reviewer was needed. On a PR run the post mentions the PR author. The alert
   kind (and so the thread) is `tier1-<kind>`, so the 9.3 items 2 and 3 are one message and its reply, and item 1
   its own message.
3. Cross-run dedup: a signature matches a recorded post with the same checkpoint and region and a pHash within 6
   bits (the tier-1 tolerance of 9.7); the 24 hours and 7 days count from the matching post's enqueue time; only top
   level posts count as posted. The history is read from the `slack` outbox jobs of the last 7 days, whose payload
   carries the dedup block, through the caller's `payload_of(sha256)` (the artifact store's read).
4. Escaping: `slack_text` collapses whitespace, escapes `&`, `<` and `>` (Slack's control characters, so no mention,
   link or command can be formed), puts a zero-width space after `@` and inside `://` and `www.` so that no mention
   or auto-link is formed from screen text, and caps a detail at 300 characters. The links of the format are built
   by `report.inline.Urls`; a crop link must be `/a/<sha256>` (9.9: "only text and /a/<sha256> links leave").
5. Thread replies name the parent by its thread key; the outbox sender keeps the Slack timestamp of each parent and
   answers a reply under it (the payload's `thread` and `reply`). The payload also carries `egress_purpose` and
   `checkpoint_id` for the sender's `egress_log` row (6.10: every post is logged by the gate that sends it).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..calendar import to_utc
from ..canonical import canonical_json, sha256_id
from ..orchestrator.alerts import ON_CALL_CHANNEL, RELEASE_CHANNEL, SAFETY_CHANNEL, TRIAGE_CHANNEL, AlertRoute
from ..records import Finding, OutboxJob
from ..store import Store, hamming
from ..taxonomy import checkpoint_parts
from .inline import Urls

CONFIDENCE_FLOOR = 0.80                         # 9.9 triage channel, 9.10 (SD-87)
POSTED_KINDS = ("collateral", "invariant_violation", "assertion_failed")   # 9.9 PR-run finding posts
TRIAGE_RULES = frozenset({"new_collateral", "decision_conflict", "sla_escalation", "needs_human_digest"})
RELEASE_RULES = frozenset({"rc_start_deadline", "canary_failure", "canary_path_divergence"})   # 9.9, 11.6
DEDUP_WINDOW = timedelta(hours=24)              # 9.9: once per 24 hours
REPLY_WINDOW = timedelta(days=7)                # 9.9: a thread reply on the original if under 7 days old
SIGNATURE_TOLERANCE = 6                         # choice 3: the tier-1 Hamming tolerance of 9.7
DETAIL_CHARS = 300                              # choice 4
ZWSP = chr(0x200B)
_WS = re.compile(r"\s+")
_AUTOLINK = re.compile(r"(?i)(://|www\.)")
_CROP = re.compile(r"/a/[0-9a-f]{64}$")
CONFIG_KEYS = {"on_call": ON_CALL_CHANNEL, "triage": TRIAGE_CHANNEL, "release": RELEASE_CHANNEL,
               "safety": SAFETY_CHANNEL}        # policy/reporting.yaml `slack` -> recipient key


# -- channels (9.9, 4.16 policy/reporting.yaml) ----------------------------------------------------------------------

@dataclass(frozen=True)
class SlackChannels:
    """The configured name of each of the four channels, by recipient key."""

    names: Mapping[str, str] = field(default_factory=lambda: {k: k for k in CONFIG_KEYS.values()})

    @classmethod
    def from_reporting(cls, doc: Mapping[str, Any]) -> "SlackChannels":
        """From a loaded `policy/reporting.yaml` (`reporting.slack` or a bare `slack` block)."""
        block = (doc.get("reporting") or doc).get("slack") or {}
        missing = sorted(k for k in CONFIG_KEYS if not block.get(k))
        if missing:
            raise ValueError(f"policy/reporting.yaml slack: {', '.join(missing)} not set (9.9, L-XREF-21)")
        return cls({CONFIG_KEYS[k]: str(block[k]) for k in CONFIG_KEYS})

    def name(self, key: str) -> str:
        if key not in self.names:
            raise ValueError(f"unknown Slack channel {key!r}; one of {', '.join(sorted(self.names))}")
        return self.names[key]


# -- finding posts (9.9, 9.10; choice 2) -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FindingPost:
    """What the Slack rules read from one finding of a run."""

    run_id: str
    run_type: str
    finding_id: str
    kind: str                                   # the 4.13 finding kind
    tier: int | None
    description: str
    checkpoint_id: str | None = None
    region_id: str | None = None
    signature: str | None = None                # the 16-hex pHash of the anchored crop
    reviewer_confidence: float | None = None
    review_needed: bool = True                  # False when the outcome needed no Reviewer
    decided: bool = False                       # a matching decision was inherited
    open_ticket: str | None = None              # an open Jira ticket carrying a matching sentinel-dec- label
    pr_author: str | None = None
    surface: str | None = None
    build_pair: str | None = None
    crop_sha256: str | None = None
    category: str | None = None

    @property
    def alert_kind(self) -> str:
        """The thread kind (choice 2): `tier1-collateral`, `tier1-invariant_violation`, ..."""
        return f"tier{self.tier}-{self.kind}"

    @classmethod
    def from_finding(cls, f: Finding, *, run_type: str, reviewer_confidence: float | None = None,
                     review_needed: bool = True, open_ticket: str | None = None, pr_author: str | None = None,
                     build_pair: str | None = None, crop_sha256: str | None = None) -> "FindingPost":
        """The post facts of a stored finding; `decided` is its inherited decision, the surface its checkpoint's."""
        surface = checkpoint_parts(f.checkpoint_id)["surface"] if f.checkpoint_id else None
        return cls(f.run_id, run_type, f.finding_id, f.kind, f.tier, f.description, f.checkpoint_id, f.region_id,
                   f.phash, reviewer_confidence, review_needed, f.decision_ref is not None, open_ticket, pr_author,
                   surface, build_pair, crop_sha256, f.category)


def should_post_pr(post: FindingPost) -> bool:
    """The 9.9 PR-run restriction on finding posts: only tier-1 `collateral`, `invariant_violation` and
    `assertion_failed`. It covers finding posts only: 9.10 alerts always post to on-call (SD-85)."""
    return post.tier == 1 and post.kind in POSTED_KINDS


def triage_post(post: FindingPost) -> bool:
    """The triage-channel rule of 9.9 (and the 9.10 "New high-confidence collateral" row, SD-87): a tier-1 finding of
    the three kinds with no matching decision, a `collateral` only at confidence 0.80 or above or with no reviewer."""
    if not should_post_pr(post) or post.decided:
        return False
    if post.kind != "collateral" or not post.review_needed:
        return True
    return post.reviewer_confidence is not None and post.reviewer_confidence >= CONFIDENCE_FLOOR


def route(event: AlertRoute | FindingPost) -> tuple[str, ...]:
    """The channels (recipient keys) a 9.10 alert route or a finding post goes to (choice 1)."""
    if isinstance(event, FindingPost):
        return (TRIAGE_CHANNEL,) if triage_post(event) else ()
    if event.rule in TRIAGE_RULES:
        return (TRIAGE_CHANNEL,)
    out = [ON_CALL_CHANNEL]
    if event.is_safety:
        out.append(SAFETY_CHANNEL)
    if RELEASE_CHANNEL in event.notified or event.rule in RELEASE_RULES:
        out.append(RELEASE_CHANNEL)
    if TRIAGE_CHANNEL in event.notified:
        out.append(TRIAGE_CHANNEL)
    return tuple(out)


def thread_key(run_id: str | None, alert_kind: str) -> str:
    """The parent of a run's posts of one alert kind (9.9 Dedup): `<run_id>/<alert_kind>`."""
    return f"{run_id or 'no-run'}/{alert_kind}"


# -- the message format (9.10; choice 4) -----------------------------------------------------------------------------

def one_line(s: str | None, limit: int = DETAIL_CHARS) -> str:
    """Whitespace collapsed to single spaces and cut at `limit` characters."""
    text = _WS.sub(" ", str(s or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def slack_text(s: str | None) -> str:
    """Screen or model text made inert in a Slack post (choice 4)."""
    out = one_line(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "@" + ZWSP)
    return _AUTOLINK.sub(lambda m: m.group(1)[:-1] + ZWSP + m.group(1)[-1], out)


@dataclass(frozen=True)
class MessageFields:
    """The fields of the 9.10 message format, rendered for Slack (escaped) or for a page or an email (plain)."""

    severity: str
    alert: str
    run_type: str | None
    run_id: str | None
    build_pair: str | None
    status: str | None                          # the outcome or the gate
    detail: str
    report_link: str | None
    surface: str | None = None                  # surface and tier, if any
    crop_link: str | None = None

    def __post_init__(self):
        for link in (self.report_link, self.crop_link):
            if link is not None and _WS.search(link):
                raise ValueError(f"a link is one token: {link!r}")
        if self.crop_link is not None and not _CROP.search(self.crop_link):
            raise ValueError(f"a crop link is /a/<sha256> (9.9), got {self.crop_link!r}")

    def render(self, *, slack: bool = False) -> str:
        """The one-line message; `slack` escapes the text fields (the links stay links)."""
        esc = slack_text if slack else one_line
        run = " ".join(x for x in (self.run_type, self.run_id) if x) or "-"
        parts = [f"[Sentinel] {self.severity} {esc(self.alert)}", esc(run), esc(self.build_pair or "-"),
                 esc(self.status or "-")]
        if self.surface:
            parts.append(esc(self.surface))
        parts += [esc(self.detail) or "-", self.report_link or "-"]
        if self.crop_link:
            parts.append(self.crop_link)
        return " | ".join(parts)


def finding_fields(post: FindingPost, urls: Urls) -> MessageFields:
    """The 9.10 message of a finding post ("New high-confidence collateral", P2)."""
    surface = f"{post.surface} tier {post.tier}" if post.surface else f"tier {post.tier}"
    crop = urls.artifact(post.crop_sha256) if post.crop_sha256 else None
    return MessageFields("P2", f"new tier-{post.tier} {post.kind}", post.run_type, post.run_id, post.build_pair,
                         post.category or post.kind, post.description, urls.finding(post.finding_id), surface, crop)


# -- messages and the cross-run dedup (9.9 Dedup; choice 3) --------------------------------------------------------

@dataclass(frozen=True)
class SlackMessage:
    """One post to queue: its channel (recipient key), the 9.10 fields, and where it threads."""

    channel: str
    fields: MessageFields | None                # None for a multi-line `text` (a digest)
    thread: str
    reply: bool = False
    mentions: tuple[str, ...] = ()
    run_id: str | None = None
    alert_kind: str = ""
    checkpoint_id: str | None = None
    dedup: Mapping[str, str | None] | None = None   # {checkpoint, region, signature} of a collateral post
    purpose: str = "slack"                      # the egress_log purpose: slack, or digest for a digest post
    text: str | None = None                     # a multi-line body (digests) instead of the one-line format

    def __post_init__(self):
        if (self.fields is None) == (self.text is None):
            raise ValueError("a Slack message has the 9.10 fields or a text body, not both")

    def body(self) -> str:
        return self.fields.render(slack=True) if self.fields is not None else str(self.text)


@dataclass(frozen=True)
class PostRecord:
    """A collateral post on record: its signature, thread and enqueue time."""

    checkpoint_id: str
    region_id: str | None
    signature: str
    thread: str
    posted_at: datetime
    reply: bool = False


@dataclass
class SlackHistory:
    """The collateral posts of the last 7 days (choice 3)."""

    records: list[PostRecord] = field(default_factory=list)

    @classmethod
    def from_store(cls, store: Store, now: datetime, payload_of: Callable[[str], bytes | None]) -> "SlackHistory":
        """Read the `slack` outbox jobs enqueued in the 7 days before `now` and the dedup block of their payloads;
        `payload_of(sha256)` returns a payload's bytes, or None when the artifact is missing (that job is skipped)."""
        since = (to_utc(now) - REPLY_WINDOW).isoformat(timespec="seconds")
        out = cls()
        for r in store.query("SELECT payload_sha256, created_at FROM outbox WHERE kind = 'slack' AND created_at >= ? "
                             "ORDER BY created_at, job_id", (since,)):
            data = payload_of(r["payload_sha256"])
            if data is None:
                continue
            p = json.loads(data)
            d = p.get("dedup")
            if d:
                out.records.append(PostRecord(d["checkpoint"], d.get("region"), d["signature"], p["thread"],
                                              datetime.fromisoformat(r["created_at"]), bool(p.get("reply"))))
        return out

    def original(self, checkpoint_id: str, region_id: str | None, signature: str, now: datetime
                 ) -> PostRecord | None:
        """The latest top-level post of the signature within 7 days before `now`."""
        t = to_utc(now)
        hits = [r for r in self.records if not r.reply and r.checkpoint_id == checkpoint_id
                and r.region_id == region_id and t - REPLY_WINDOW < to_utc(r.posted_at) <= t
                and (hamming(r.signature, signature) or 0) <= SIGNATURE_TOLERANCE]
        return max(hits, key=lambda r: r.posted_at) if hits else None


@dataclass(frozen=True)
class Dedup:
    """What the cross-run dedup says about one collateral post: `post`, `skip`, or `reply` on `thread`."""

    action: str
    thread: str | None = None


def cross_run_dedup(history: SlackHistory, checkpoint_id: str, signature: str, now: datetime, *,
                    region_id: str | None = None, open_ticket: bool = False) -> Dedup:
    """The 9.9 cross-run rule for a `collateral` checkpoint plus signature (choice 3)."""
    orig = history.original(checkpoint_id, region_id, signature, now)
    if open_ticket:
        return Dedup("reply", orig.thread) if orig is not None else Dedup("skip")
    if orig is not None and to_utc(now) - to_utc(orig.posted_at) < DEDUP_WINDOW:
        return Dedup("skip")
    return Dedup("post")


def plan_finding_posts(posts: Iterable[FindingPost], *, history: SlackHistory, now: datetime,
                       base_url: str = "https://sentinel.internal") -> list[SlackMessage]:
    """The triage-channel messages for a run's findings (9.9): one parent per run and alert kind, later posts as
    thread replies, the cross-run dedup applied to `collateral`; `history` gains the posts made here."""
    parents: set[str] = set()
    out: list[SlackMessage] = []
    for p in posts:
        if not triage_post(p):
            continue
        fields_ = finding_fields(p, Urls(base_url.rstrip("/"), p.run_id))
        mentions = (p.pr_author,) if p.run_type == "pr" and p.pr_author else ()
        key = thread_key(p.run_id, p.alert_kind)
        dedup = None
        if p.kind == "collateral" and p.checkpoint_id and p.signature:
            dedup = {"checkpoint": p.checkpoint_id, "region": p.region_id, "signature": p.signature}
            d = cross_run_dedup(history, p.checkpoint_id, p.signature, now, region_id=p.region_id,
                                open_ticket=p.open_ticket is not None)
            if d.action == "skip":
                continue
            if d.action == "reply":
                out.append(SlackMessage(TRIAGE_CHANNEL, fields_, str(d.thread), True, mentions, p.run_id,
                                        p.alert_kind, p.checkpoint_id, dedup))
                continue
        reply = key in parents
        parents.add(key)
        out.append(SlackMessage(TRIAGE_CHANNEL, fields_, key, reply, mentions, p.run_id, p.alert_kind,
                                p.checkpoint_id, dedup))
        if dedup is not None:
            history.records.append(PostRecord(p.checkpoint_id or "", p.region_id, p.signature or "", key,
                                              to_utc(now), reply))
    return out


# -- the outbox (9.1, 8.3) ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Queued:
    """An outbox job written for a post, and the payload bytes the caller stores as the artifact its hash names."""

    job_id: int
    kind: str
    destination: str
    payload: bytes
    payload_sha256: str


def queue_payloads(store: Store, items: Sequence[tuple[str, str, Mapping[str, Any], str | None]], now: datetime, *,
                   release_at: datetime | None = None, label: str = "slack_enqueue") -> tuple[Queued, ...]:
    """Write one outbox job per `(kind, destination, payload, run_id)` in one batch (9.1); a job already queued with
    the same run, kind and payload hash is not queued again. `release_at` later than `now` holds the jobs until then
    (an after-hours alert posts at 06:00, 9.10)."""
    created = to_utc(now).isoformat(timespec="seconds")
    hold = None if release_at is None or to_utc(release_at) <= to_utc(now) else \
        to_utc(release_at).isoformat(timespec="seconds")
    todo: list[tuple[OutboxJob, bytes]] = []
    seen: set[tuple[str | None, str, str]] = set()
    for kind, dest, body, run_id in items:
        if run_id is not None and not store.query("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)):
            run_id = None                       # a queued or unknown run has no row to reference (8.3 FK)
        data, sha = canonical_json(body).encode("utf-8"), sha256_id(body)
        if (run_id, kind, sha) in seen or store.query(
                "SELECT 1 FROM outbox WHERE run_id IS ? AND kind = ? AND payload_sha256 = ?", (run_id, kind, sha)):
            continue
        seen.add((run_id, kind, sha))
        todo.append((OutboxJob(kind, dest, sha, created_at=created, run_id=run_id, next_attempt_at=hold), data))
    if not todo:
        return ()
    with store.batch(label) as b:
        return tuple(Queued(b.insert("outbox", {k: v for k, v in asdict(job).items() if k != "job_id"}), job.kind,
                            job.destination, data, job.payload_sha256) for job, data in todo)


def payload(m: SlackMessage, channels: SlackChannels) -> dict[str, Any]:
    """The outbox payload of one post (choice 5): text, channel, thread, mentions and the egress row context."""
    return {"channel": channels.name(m.channel), "text": m.body(), "thread": m.thread, "reply": m.reply,
            "mentions": list(m.mentions), "alert": m.alert_kind, "run_id": m.run_id, "checkpoint_id": m.checkpoint_id,
            "egress_purpose": m.purpose, "dedup": dict(m.dedup) if m.dedup else None}


def enqueue(store: Store, messages: Iterable[SlackMessage], channels: SlackChannels, now: datetime, *,
            release_at: datetime | None = None) -> tuple[Queued, ...]:
    """Queue the posts as outbox jobs of kind `slack` (a digest post: `digest`) to their configured channels."""
    items = [("digest" if m.purpose == "digest" else "slack", channels.name(m.channel), payload(m, channels),
              m.run_id) for m in messages]
    return queue_payloads(store, items, now, release_at=release_at)

