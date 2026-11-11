"""The Reporter's side of Jira over the Central Store (9.9, 9.6 Record fixed in build, 9.4 item 2, 9.11, 9.12): it
enqueues the jobs of `report.jira` through the single writer with the 9.9 dedup, carries a job out and writes back
what Jira answered, polls Jira for fixes, and reads what the report, the digest and the Decisions page print while
Jira is down.

Enqueueing (every post is an outbox job, 9.1; nothing in a run waits on Jira):

- `enqueue(store, job, run_id=, at=)` writes one job (`report.jira.bug_job`, `link_job`, `auto_file_job`); the
  same payload in the same run again is a no-op. The returned bytes are the payload the outbox row's hash names;
  the caller stores them as that artifact (8.3 outbox).
- `seen_again(store, decision, finding, ...)`: the comment on the open ticket of an inherited confirm_bug, at most
  once per ticket per 24 hours.
- `intended_evidence(store, decision, findings, ...)`: mark intended with a ticket, one evidence comment per run
  per ticket; a key off the Jira regex is refused at once.
- `sent_task(store, event, ...)` and `canary_bugs(store, bugs, ...)`: SENT tasks and canary Bugs, one open ticket
  per key, one post per run per key.

Delivery and write-back (the outbox's Jira sender):

- `send(client, payload, store=, report_base=)` carries any Jira payload: the jobs of `report.jira` and the ticket
  reopen `store.lifecycle.verify_fixes` queues (`reopen`: by transition with a comment naming run, build and crop;
  where the workflow forbids it, the comment, the `sentinel-reopened` label and a P2 alert).
- `record(store, payload, delivered, at=)` writes back: the key of a new product Bug on every decision and finding
  it covers (`Store.fill_ticket_ref`), the key of a SENT ticket on its ledger row, and the 9.9 ticket-not-found
  path (`store.lifecycle.ticket_not_found`) for a named ticket Jira does not know.

Fix verification: `poll(client, store, now)` reads Done tickets labelled `sentinel` (hourly; `sync_ticket` is the
same for one ticket, the webhook path) and writes a fixed_in_build decision for every decision on the ticket, by the
resolver, `source: jira_sync`, superseding the confirm_bug (SD-79). It also closes the ledger row of a Done SENT
ticket.

Display: `jira_unreachable_since`, `ticket_note` and `pending_notes` give "ticket pending (Jira unreachable since
10:02 ET)"; `decisions_without_tickets` and `without_ticket_line` give the digest's and the Decisions page's list
after 24 hours; `link_ticket` is the hand link that clears it; `record_collisions` puts the label collisions in Run
health; `flow_tasks` (with `report.jira.flow_task_phrase`) and `jira_queued` feed the reconciliation line and the
Integrations row.

Choices where the spec is ambiguous or silent (reported):

1. The SENT ledger is `maintenance_items` (the harness's maintenance tasks, 9.6 "SENT maintenance task", 11.5): one
   row of kind `sent_ticket` per opened ticket, `ref` the `report.jira.sent_task_key`, `ticket_ref` filled when the
   create lands, closed (`closed_by` the resolver, else `jira_sync`) when the poller sees the ticket Done. A key with
   an open row gets a comment job; one without opens a row and a create job, in one writer batch.
2. "Jira unreachable since" is the `created_at` of the oldest Jira job (create, comment or transition) that is
   failed or abandoned and not closed, among those created after the newest Jira send; none such means Jira is
   answering and a confirm_bug without a key reads "ticket pending". The time prints as America/New_York `HH:MM
   ET`, with the date in front when it is not the day of `now`.
3. "Decisions without tickets" are active confirm_bug rows with no `ticket_ref` decided 24 hours or more before
   `now`; a hand link (`link_ticket`) or the create landing removes them. A hand link checks the key against the
   Jira regex, refuses a service identity and a decision that is not a confirm_bug without a ticket, queues the link
   job (labels and identity block) before it writes the key, so a retry after a failure repeats nothing; a key Jira
   then says does not exist takes the 9.9 ticket-not-found path, as a mark-intended key does.
4. The fix build. The ticket's build id field wins when it names a registered build; otherwise the earliest
   registered build (by `first_seen_at`) among its fixVersions, each mapped through `build_of` (default: the
   fixVersion is the build id). A Done ticket with neither, or without a resolver login, writes nothing and is
   listed in `PollResult.skipped`.
5. The decision a ticket names is followed along its supersessions to the current row: an active confirm_bug is
   fixed; a reopened fixed_in_build is fixed again when the build differs (a later fix, SD-79); an active
   fixed_in_build of the same build is already recorded (a re-poll writes nothing); anything else, and any store
   refusal (L-DEC-08 build order, a frozen signature), is skipped with its reason. The new row is
   `dec-<max + 1>`, made on the confirm_bug's run, `role: ticket_resolver`, `decided_at` the resolution time.
6. The poll JQL is `labels = "sentinel" AND statusCategory = Done`, plus `updated >= "<since>"` when `since` is
   given (the service account's time zone is UTC); re-reading a ticket is a no-op, so the window only bounds cost.
7. The reopen comment names the decision, the run, the build, the run's findings and their 1:1 crop links
   (`/a/<sha256>` of each finding's evidence artifacts when the store and the report base are given). A ticket
   already open is not transitioned again (a retried delivery), and the P2 is the route `jira_reopen_forbidden` to
   the on-call channel; raising it is the alert dispatcher's.
8. Collisions go to the run's summary: `jira_collisions` [{ticket, checkpoint}] and the "Jira label collisions"
   row of `health_rows`, which the report prints as it is (report.model).
9. Synthetic records never post (9.9, 10.3): `enqueue` also asks the store, since a job names its findings and
   decisions and a finding read back from the store does not carry its `synthetic` flag (`Store._finding`). The
   seen-again and intended paths go through `enqueue`, so the check covers them; SENT tasks and canary Bugs report
   on the harness, not on a finding, and are not filtered.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..calendar import ny
from ..canonical import canonical_json, sha256_id
from ..orchestrator import alerts
from ..records import NON_HUMAN, Decision, Finding, MaintenanceItem, OutboxJob
from ..store.lifecycle import ticket_not_found
from ..store.store import Store, StoreRefused, decision_int, decision_ref, utc
from ..taxonomy import TaxonomyError
from . import jira
from .inline import Urls, is_artifact_hash

LEDGER_KIND = "sent_ticket"                       # choice 1
REOPEN_TRANSITION = "Reopen"                      # 9.9: the workflow transition back to open
COLLISION_ROW = "Jira label collisions"           # choice 8
WITHOUT_TICKET_HOURS = 24                         # 9.9: decisions without tickets after 24 hours
POLL_JQL = 'labels = "sentinel" AND statusCategory = Done'          # choice 6
REOPEN_FORBIDDEN = alerts.jira_reopen_forbidden()                                    # 9.9, choice 7
_JIRA_IN = ",".join(f"'{k}'" for k in jira.JIRA_KINDS)


# ---------------------------------------------------------------------------------------------------------------
# enqueueing

@dataclass(frozen=True)
class Queued:
    """One Jira job written to the outbox: its id, and the payload bytes its hash names (store them as the
    artifact, 8.3)."""
    job_id: int
    payload: bytes
    payload_sha256: str
    destination: str


def _outbox_row(job: OutboxJob) -> dict:
    values = {k: v for k, v in dataclasses.asdict(job).items() if k != "job_id"}
    values.update(created_at=utc(job.created_at), next_attempt_at=utc(job.next_attempt_at))
    return values


def _exists(store: Store, run_id: str, kind: str, sha: str) -> bool:
    return bool(store.query("SELECT 1 FROM outbox WHERE run_id IS ? AND kind = ? AND payload_sha256 = ?",
                            (run_id, kind, sha)))


def _posted(store: Store, destination: str, run_id: str | None = None) -> list:
    """The jobs to `destination` that posted or still may (an abandoned job never posted; choice 5 of report.jira)."""
    sql = ("SELECT job_id, created_at FROM outbox WHERE destination = ? AND state <> 'abandoned' AND kind IN "
           f"({_JIRA_IN})")
    if run_id is None:
        return store.query(sql + " ORDER BY created_at DESC", (destination,))
    return store.query(sql + " AND run_id = ?", (destination, run_id))


def _synthetic(store: Store, payload: Mapping) -> bool:
    """True when the payload names a synthetic finding, directly or through a decision's finding (choice 9)."""
    keys = ([payload["finding"]] if payload.get("finding") else []) + list(payload.get("findings") or ())
    decisions = [decision_int(d) for d in payload.get("decisions") or ()]
    return bool(store.query(
        "SELECT 1 FROM findings WHERE synthetic = 1 AND (natural_key IN (SELECT value FROM json_each(?)) OR "
        "finding_id IN (SELECT source_finding_id FROM decisions WHERE decision_id IN (SELECT value FROM "
        "json_each(?)))) LIMIT 1", (json.dumps(keys), json.dumps(decisions))))


def enqueue(store: Store, job: jira.JiraJob | None, *, run_id: str, at: str) -> Queued | None:
    """Write `job` as a queued outbox job of `run_id` (9.1). None for no job, for a job about a synthetic finding
    (synthetic records never post, 9.9, 10.3), or for the same payload already queued for the run."""
    if job is None or _synthetic(store, job.payload):
        return None
    data, sha = canonical_json(job.payload).encode("utf-8"), sha256_id(job.payload)
    if _exists(store, run_id, job.kind, sha):
        return None
    jid = store.enqueue(OutboxJob(job.kind, job.destination, sha, created_at=at, run_id=run_id))
    return Queued(jid, data, sha, job.destination)


def seen_again(store: Store, decision: Decision, finding: Finding, *, run_id: str, build_id: str | None,
               report_base: str, at: str) -> Queued | None:
    """The 9.9 seen-again comment for a finding of `run_id` that inherited `decision`: only for an active
    confirm_bug with a ticket, a real finding, and no seen-again on that ticket in the last 24 hours."""
    if decision.action != "confirm_bug" or decision.status != "active" or not decision.ticket:
        return None
    job = jira.seen_again_job(decision.ticket, finding, run_id=run_id, build_id=build_id, report_base=report_base)
    if job is None:
        return None
    last = _posted(store, job.destination)
    if not jira.seen_again_due(last[0]["created_at"] if last else None, utc(at)):
        return None
    return enqueue(store, job, run_id=run_id, at=at)


def intended_evidence(store: Store, decision: Decision, findings: Sequence[Finding], *, run_id: str,
                      report_base: str, at: str, manifest_pr: str | None = None) -> Queued | None:
    """Mark intended with ticket (9.9): the first decision of the run on the ticket posts the evidence comment,
    later ones only their labels. ValueError for a key off the Jira regex (checked at once)."""
    if decision.action != "mark_intended" or not decision.ticket:
        raise ValueError(f"{decision.decision_id} is not a mark_intended decision with a ticket (9.9)")
    first = not _posted(store, f"{decision.ticket}#intended", run_id)
    job = jira.intended_job(decision.ticket, decision, findings, run_id=run_id, report_base=report_base,
                            comment=first, manifest_pr=manifest_pr)
    return enqueue(store, job, run_id=run_id, at=at)


def _open_item(store: Store, key: str):
    rows = store.query("SELECT item_id, ticket_ref FROM maintenance_items WHERE kind = ? AND ref = ? AND closed_at "
                       "IS NULL ORDER BY item_id LIMIT 1", (LEDGER_KIND, key))
    return rows[0] if rows else None


def _sent(store: Store, key: str, owner: str, run_id: str, at: str, detail: str,
          build: Callable[[str | None, bool], jira.JiraJob]) -> Queued | None:
    """One post per run per key, one open ticket per key (choice 1): a comment job when the key has an open
    ledger row, else a new row and a create job in one batch."""
    item = _open_item(store, key)
    job = build(item["ticket_ref"] if item is not None else None, item is not None)
    if _posted(store, job.destination, run_id):
        return None
    data, sha = canonical_json(job.payload).encode("utf-8"), sha256_id(job.payload)
    with store.batch("jira_sent") as b:
        if item is None:
            m = MaintenanceItem(LEDGER_KIND, key, owner, at, run_id=run_id, detail=detail or None)
            row = {k: v for k, v in dataclasses.asdict(m).items() if k != "item_id"}
            b.insert("maintenance_items", {**row, "opened_at": utc(at)})
        jid = b.insert("outbox", _outbox_row(OutboxJob(job.kind, job.destination, sha, created_at=at,
                                                       run_id=run_id)))
    return Queued(jid, data, sha, job.destination)


def sent_task(store: Store, event: jira.SentEvent, *, report_base: str, at: str) -> Queued | None:
    """A SENT task for `event` (9.9 row 4, 9.4 item 2), or a comment with the run link on its open task."""
    return _sent(store, jira.sent_task_key(event), event.role, event.run_id, at, event.detail,
                 lambda ticket, expect: jira.task_job(event, report_base=report_base, ticket=ticket,
                                                      expect_open=expect))


def canary_bugs(store: Store, bugs: Iterable[Mapping], *, run_id: str, report_base: str, at: str) -> list[Queued]:
    """The night's canary SENT Bugs (`canary_integrations.sent_bug_payload`, 9.9 row 5), one open Bug per mutant
    id, class or stage."""
    out = []
    for bug in bugs:
        key = jira.task_key("canary", bug["dedup_key"])
        q = _sent(store, key, jira.SENT_TRIGGERS["canary"][1], run_id, at, "",
                  lambda ticket, expect, bug=bug: jira.canary_job(bug, run_id=run_id, report_base=report_base,
                                                                  ticket=ticket, expect_open=expect))
        if q is not None:
            out.append(q)
    return out


# ---------------------------------------------------------------------------------------------------------------
# delivery and write-back

def crop_links(store: Store, finding_keys: Iterable[str], report_base: str) -> tuple[str, ...]:
    """The 1:1 crop links (`/a/<sha256>`) of the findings' evidence artifacts, in order, once each."""
    urls = Urls(report_base.rstrip("/"), "")
    out: list[str] = []
    for key in finding_keys:
        f = store.get_finding(key)
        for a in (f.evidence.get("artifacts", ()) if f is not None else ()):
            if is_artifact_hash(a) and urls.artifact(a) not in out:
                out.append(urls.artifact(a))
    return tuple(out)


def reopen_comment(payload: Mapping, crops: Sequence[str] = (), report_base: str | None = None) -> str:
    """The comment of a reopen (9.9): the run, the build and the crop."""
    run = payload.get("run")
    findings = list(payload.get("findings") or ())
    links = [Urls(report_base.rstrip("/"), run or "").finding(f) for f in findings] if report_base else findings
    return "\n".join([f"Sentinel: the signature fixed by {payload.get('decision')} returned in run {run} on build "
                      f"{payload.get('build') or 'unknown'}.",
                      "Findings: " + (" ".join(links) or "none listed"),
                      "Crop (1:1): " + (" ".join(crops) or "see the findings")])


def reopen(client: jira.JiraClient, payload: Mapping, *, crops: Sequence[str] = (),
           report_base: str | None = None) -> jira.Delivered:
    """Deliver a `jira_transition` reopen job of `store.lifecycle.verify_fixes` (9.9, choice 7)."""
    key = payload["ticket"]
    t = client.get(key)
    if t is None:
        return jira.Delivered("reopen", key, not_found=True)
    ok = client.transition(key, REOPEN_TRANSITION) if t.done else True
    client.comment(key, reopen_comment(payload, crops, report_base))
    if ok:
        return jira.Delivered("reopen", key, commented=True, transitioned=True)
    if jira.REOPENED_LABEL not in t.labels:
        client.add_labels(key, [jira.REOPENED_LABEL])
    return jira.Delivered("reopen", key, commented=True, transitioned=False, alert=REOPEN_FORBIDDEN)


def send(client: jira.JiraClient, payload: Mapping, *, store: Store | None = None,
         report_base: str | None = None) -> jira.Delivered:
    """The outbox's Jira sender: carry one payload (a `report.jira` job or a lifecycle reopen). `JiraUnavailable`
    and `JiraPending` propagate, so the outbox marks the attempt failed and retries (9.1)."""
    if payload.get("action") == "reopen":
        crops = (crop_links(store, payload.get("findings") or (), report_base)
                 if store is not None and report_base else ())
        return reopen(client, payload, crops=crops, report_base=report_base)
    return jira.deliver(client, payload)


def record(store: Store, payload: Mapping, delivered: jira.Delivered, *, at: str) -> None:
    """Write back what a delivery learned (9.9): a new Bug's key on its decisions and their findings, a SENT
    ticket's key on its ledger row, and ticket-not-found for a named ticket Jira does not know."""
    if delivered.op == "bug" and delivered.ticket:
        for did in payload.get("decisions", ()):
            d = store.get_decision(did)
            if d is not None and d.ticket is None:
                store.fill_ticket_ref(did, delivered.ticket)
    elif delivered.op in ("task", "canary") and delivered.ticket:
        item = _open_item(store, payload["key"])
        if item is None:
            m = MaintenanceItem(LEDGER_KIND, payload["key"], jira.SENT_TRIGGERS[payload["key"].split(":", 1)[0]][1],
                                at, run_id=payload.get("run"), ticket_ref=delivered.ticket)
            store.write_maintenance_item(m)
        elif item["ticket_ref"] != delivered.ticket:
            store.update("maintenance_items", {"item_id": item["item_id"]}, {"ticket_ref": delivered.ticket})
    elif delivered.op in ("intended", "link") and delivered.not_found:
        for did in payload.get("decisions", ()):
            try:
                ticket_not_found(store, did, at=at)
            except StoreRefused:
                continue                             # superseded or revoked meanwhile: nothing to expire


# ---------------------------------------------------------------------------------------------------------------
# fix verification: the poller (9.9, 9.6 Record fixed in build)

@dataclass(frozen=True)
class Fixed:
    ticket: str
    decision_id: str                 # the decision the ticket names
    fixed_id: str                    # the fixed_in_build row written
    build_id: str


@dataclass(frozen=True)
class Skipped:
    ticket: str
    decision_id: str | None
    why: str


@dataclass(frozen=True)
class PollResult:
    """What one poll (or one webhook ticket) wrote."""
    fixed: tuple[Fixed, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    closed: tuple[str, ...] = ()     # SENT tickets whose ledger row closed

    def __add__(self, other: "PollResult") -> "PollResult":
        return PollResult(self.fixed + other.fixed, self.skipped + other.skipped, self.closed + other.closed)


def fix_build(store: Store, ticket: jira.Ticket, build_of: Callable[[str], str | None] | None = None) -> str | None:
    """The build a Done ticket was fixed in (choice 4): its build id field, else the earliest registered build its
    fixVersions name; None when neither names a registered build."""
    names = [n for n in ([ticket.build_id] + [build_of(v) if build_of else v for v in ticket.fix_versions]) if n]
    if not names:
        return None
    known = {r[0]: r[1] for r in store.query(
        f"SELECT build_id, first_seen_at FROM builds WHERE build_id IN ({','.join('?' * len(names))})", names)}
    if ticket.build_id in known:
        return ticket.build_id
    fixes = [n for n in names if n in known]
    return min(fixes, key=lambda b: (known[b], b)) if fixes else None


def _head(store: Store, row):
    """The current row of a decision's supersession chain (choice 5)."""
    while True:
        nxt = store.query("SELECT * FROM decisions WHERE supersedes_decision_id = ? ORDER BY decision_id DESC "
                          "LIMIT 1", (row["decision_id"],))
        if not nxt:
            return row
        row = nxt[0]


def _fix_one(store: Store, ticket: jira.Ticket, did: str, build: str, at: str) -> Fixed | Skipped | None:
    rows = store.query("SELECT * FROM decisions WHERE decision_id = ?", (decision_int(did),))
    if not rows:
        return Skipped(ticket.key, did, "unknown decision")
    head = _head(store, rows[0])
    ref = decision_ref(head["decision_id"])
    if head["action"] == "fixed_in_build" and head["fixed_in_build_id"] == build:
        if head["status"] == "active":
            return None                                          # already recorded: a re-poll writes nothing
        return Skipped(ticket.key, did, f"{ref} fixed in {build} is {head['status']}; a new fix needs a new build")
    if not ((head["action"] == "confirm_bug" and head["status"] == "active")
            or (head["action"] == "fixed_in_build" and head["status"] == "reopened")):
        return Skipped(ticket.key, did, f"{ref} is a {head['status']} {head['action']}")
    old = store.get_decision(ref)
    nxt = decision_ref(store.query("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions")[0][0])
    try:
        new = Decision(nxt, "fixed_in_build", "confirmed_bug", old.signature, decided_by=ticket.resolver,
                       ticket=ticket.key, source="jira_sync", role="ticket_resolver",
                       reason=f"Jira {ticket.key} Done, fixed in build {build}",
                       decided_at=utc(ticket.resolved_at) or utc(at))
        stored = store.record_decision(new, run_id=head["run_id"], supersedes=ref, fixed_in_build_id=build)
    except (StoreRefused, TaxonomyError) as e:
        return Skipped(ticket.key, did, str(e))
    return Fixed(ticket.key, did, stored.decision_id, build)


def _close_items(store: Store, ticket: jira.Ticket, at: str) -> tuple[str, ...]:
    rows = store.query("SELECT item_id FROM maintenance_items WHERE kind = ? AND ticket_ref = ? AND closed_at IS "
                       "NULL", (LEDGER_KIND, ticket.key))
    if not rows:
        return ()
    with store.batch("jira_sent_closed") as b:
        for r in rows:
            b.update("maintenance_items", {"item_id": r[0]},
                     {"closed_at": utc(ticket.resolved_at) or utc(at), "closed_by": ticket.resolver or "jira_sync"})
    return (ticket.key,)


def sync_ticket(store: Store, ticket: jira.Ticket, *, now: str,
                build_of: Callable[[str], str | None] | None = None) -> PollResult:
    """One Done ticket (the poll's unit and the webhook path, 9.9): close its SENT ledger row and write a
    fixed_in_build for every decision it names (choices 4, 5). A ticket not Done writes nothing."""
    if not ticket.done:
        return PollResult()
    closed = _close_items(store, ticket, now)
    decisions = jira.ticket_decisions(ticket)
    if not decisions:
        return PollResult(closed=closed)
    build = fix_build(store, ticket, build_of)
    why = ("Done without a fixVersion or build id naming a registered build" if build is None else
           None if ticket.resolver else "Done without a resolver login")
    if why:
        return PollResult(skipped=tuple(Skipped(ticket.key, d, why) for d in decisions), closed=closed)
    done = [_fix_one(store, ticket, d, build, now) for d in decisions]
    return PollResult(tuple(x for x in done if isinstance(x, Fixed)),
                      tuple(x for x in done if isinstance(x, Skipped)), closed)


def poll(client: jira.JiraClient, store: Store, now: str, *, since: str | None = None,
         build_of: Callable[[str], str | None] | None = None) -> PollResult:
    """The hourly fix-verification poll (9.9): every Done ticket labelled `sentinel` through `sync_ticket`."""
    jql = POLL_JQL
    if since is not None:
        jql += f' AND updated >= "{datetime.fromisoformat(utc(since)).strftime("%Y-%m-%d %H:%M")}"'
    out = PollResult()
    for t in client.search(jql):
        out = out + sync_ticket(store, t, now=now, build_of=build_of)
    return out


# ---------------------------------------------------------------------------------------------------------------
# display: pending tickets, decisions without tickets, hand links, collisions, tasks

def jira_unreachable_since(store: Store) -> str | None:
    """When Jira stopped answering (choice 2), or None while it answers."""
    last = store.query(f"SELECT max(sent_at) FROM outbox WHERE kind IN ({_JIRA_IN}) AND state = 'sent'")[0][0]
    r = store.query(f"SELECT min(created_at) FROM outbox WHERE kind IN ({_JIRA_IN}) AND state IN "
                    "('failed','abandoned') AND closed_at IS NULL AND created_at > ?", (last or "",))
    return r[0][0]


def _et(ts: str, now: str) -> str:
    at, today = ny(datetime.fromisoformat(utc(ts))), ny(datetime.fromisoformat(utc(now)))
    return (f"{at:%Y-%m-%d} " if at.date() != today.date() else "") + f"{at:%H:%M} ET"


def ticket_note(decision: Decision, since: str | None, now: str) -> str | None:
    """What a confirm_bug's finding shows for its ticket (9.9): the key, "ticket pending (Jira unreachable since
    {time})" while Jira is down, else "ticket pending". None for other actions."""
    if decision.action != "confirm_bug":
        return None
    if decision.ticket:
        return f"ticket {decision.ticket}"
    return "ticket pending" if since is None else f"ticket pending (Jira unreachable since {_et(since, now)})"


def pending_notes(store: Store, decision_ids: Iterable[str], now: str) -> dict[str, str]:
    """`ticket_note` for each confirm_bug decision (for the items and the Known bugs rows)."""
    since = jira_unreachable_since(store)
    out = {}
    for did in decision_ids:
        d = store.get_decision(did)
        note = ticket_note(d, since, now) if d is not None else None
        if note is not None:
            out[did] = note
    return out


def decisions_without_tickets(store: Store, now: str, *, hours: int = WITHOUT_TICKET_HOURS) -> tuple[Decision, ...]:
    """Active confirm_bug decisions still without a ticket `hours` after they were made (9.9, 9.10 digest, 9.12)."""
    cutoff = utc((datetime.fromisoformat(utc(now)) - timedelta(hours=hours)).isoformat())
    rows = store.query("SELECT decision_id FROM decisions WHERE action = 'confirm_bug' AND status = 'active' AND "
                       "ticket_ref IS NULL AND decided_at <= ? ORDER BY decided_at, decision_id", (cutoff,))
    return tuple(store.get_decision(decision_ref(r[0])) for r in rows)


def without_ticket_line(decision: Decision, now: str) -> str:
    """One line of the "decisions without tickets" list: the decision, its finding, the decider and the age."""
    age = datetime.fromisoformat(utc(now)) - datetime.fromisoformat(utc(decision.decided_at))
    return (f"{decision.decision_id}: confirm_bug on {decision.signature.checkpoint_id} "
            f"({decision.finding_ref or 'no finding'}) by {decision.decided_by}, no ticket for "
            f"{int(age.total_seconds() // 3600)} h; link one by hand")


def link_ticket(store: Store, decision_id: str, key: str, *, by: str, report_base: str, at: str,
                charset_ok: bool = False) -> Decision:
    """A human links a ticket to a confirm_bug that has none (9.9 "until a human links a ticket by hand"; choice
    3): the link job, then the key on the decision and its findings. Returns the decision as stored."""
    if not jira.valid_key(key):
        raise StoreRefused(f"ticket {key!r} does not match ^[A-Z][A-Z0-9]+-[0-9]+$ (L-MAN-05)")
    if not by or by.strip().lower() in NON_HUMAN:
        raise StoreRefused(f"linked_by {by!r} is not a person (L-DEC-01)")
    d = store.get_decision(decision_id)
    if d is None or d.action != "confirm_bug" or d.status != "active":
        raise StoreRefused(f"{decision_id} is not an active confirm_bug decision (9.9)")
    if d.ticket:
        raise StoreRefused(f"{decision_id} already carries ticket {d.ticket}; a ticket is set once (L-DEC-02)")
    tier = store.query("SELECT tier FROM checkpoints WHERE checkpoint_id = ?", (d.signature.checkpoint_id,))
    enqueue(store, jira.link_job(key, [d], report_base=report_base, tier=tier[0][0] if tier else None, linked_by=by,
                                 charset_ok=charset_ok), run_id=d.run_id, at=at)
    return store.fill_ticket_ref(decision_id, key)


def record_collisions(store: Store, run_id: str, checkpoint_id: str, collisions: Iterable[jira.Ticket]) -> int:
    """Count label collisions in the run's Run health (9.9; choice 8). Returns the run's total."""
    summary = store.run_summary(run_id)
    seen = list(summary.get("jira_collisions") or [])
    for t in collisions:
        entry = {"ticket": t.key, "checkpoint": checkpoint_id}
        if entry not in seen:
            seen.append(entry)
    if not seen:
        return 0
    text = (f"{len(seen)}: " + ", ".join(f"{e['ticket']} (searched for {e['checkpoint']})" for e in seen)
            + "; identity blocks name other checkpoints, ignored for linking")
    rows = [r for r in summary.get("health_rows") or [] if r[0] != COLLISION_ROW] + [[COLLISION_ROW, text]]
    store.update_summary(run_id, {"jira_collisions": seen, "health_rows": rows})
    return len(seen)


def flow_tasks(store: Store, entry_ids: Iterable[str]) -> dict[str, str | None]:
    """The flow-authoring task of each manifest entry with a SENT ledger row (9.4 item 3): its ticket key, or None
    while the create has not landed. Entries without a row are absent."""
    out: dict[str, str | None] = {}
    for eid in entry_ids:
        key = jira.task_key("no_covering_checkpoint", eid)
        rows = store.query("SELECT ticket_ref FROM maintenance_items WHERE kind = ? AND ref = ? ORDER BY "
                           "closed_at IS NOT NULL, item_id DESC LIMIT 1", (LEDGER_KIND, key))
        if rows:
            out[eid] = rows[0][0]
    return out


def jira_queued(store: Store, run_id: str) -> int:
    """The run's Jira jobs not yet sent: the "Jira {n} queued" of the Integrations row (9.2)."""
    return store.query(f"SELECT count(*) FROM outbox WHERE run_id = ? AND kind IN ({_JIRA_IN}) AND state IN "
                       "('queued','failed')", (run_id,))[0][0]


__all__ = [
    "COLLISION_ROW", "Fixed", "LEDGER_KIND", "POLL_JQL", "PollResult", "Queued", "REOPEN_FORBIDDEN",
    "REOPEN_TRANSITION", "Skipped", "canary_bugs", "crop_links", "decisions_without_tickets", "enqueue", "fix_build",
    "flow_tasks", "intended_evidence", "jira_queued", "jira_unreachable_since", "link_ticket", "pending_notes",
    "poll", "record", "record_collisions", "reopen", "reopen_comment", "seen_again", "send", "sent_task",
    "sync_ticket", "ticket_note", "without_ticket_line",
]
