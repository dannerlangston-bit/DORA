"""Jira composition for the Reporter (9.9, 9.4 item 2, 11.8): labels, the identity block, ticket descriptions and
priorities, the link-to-existing queries and label collisions, the seen-again and auto-filing rules, SENT task keys,
the outbox jobs all of these become, the `JiraClient` protocol the outbox's Jira sender drives, and `deliver`, which
carries one job out.

Nothing here touches the Central Store. `report.jira_sync` enqueues these jobs through the single writer, writes
back what Jira answers, polls for fixes and lists decisions without tickets. Posts are outbox jobs (9.1): a run or a
UI action builds and enqueues them and never waits on Jira; `deliver` runs in the outbox's sender, and a client that
cannot reach Jira raises `JiraUnavailable`, which leaves the job to the 9.1 retry schedule.

- Labels (`labels`, `cp_label`, `surface_label`, `dec_label`): `sentinel`, `sentinel-tier-{n}`,
  `sentinel-surface-{id}`, `sentinel-cp-{12 hex}` and one `sentinel-dec-{decision id}` per decision, at most 8;
  the decisions past the eighth are `Labels.overflow`, carried by their identity blocks in the description.
- Identity (`identity_block`, `parse_identity`, `ticket_decisions`): the machine-readable `sentinel:` block of 9.9.
- Product Bugs (`priority`, `summary`, `description`, `bug_job`, `link_job`): the confirm_bug row of 9.9.
- Dedup (`dedup_query`, `is_collision`, `link_candidates`): the JQL the UI runs on a new confirm_bug, cp label then
  surface label, and the collisions it ignores for linking.
- Comments (`seen_again_due`, `seen_again_job`, `intended_job`): seen again once per ticket per 24 hours; the mark
  intended evidence comment once per run per ticket, its key checked against the Jira regex at once (`valid_key`)
  and for existence on delivery.
- SENT (`SENT_TRIGGERS`, `SentEvent`, `sent_task_key`, `task_job`, `canary_job`): harness tasks and canary Bugs,
  one open ticket per key.
- Auto-filing (`auto_file_allowed`, `auto_file_job`): off unless the system owner switched the tier on and both
  gates hold; even then a SENT Bug for triage, never the product backlog.

Choices where the spec is ambiguous or silent (reported):

1. Labels (9.9 with the 11.8 Phase 0 default branch). The checkpoint label is always the hash: the first 12 hex of
   the SHA-256 of the UTF-8 checkpoint id. The surface label carries the surface id only when `charset_ok` (the
   Phase 0 read found `.` and `-` accepted); until then it is hashed the same way, `sentinel-surface-<12 hex>`.
   `charset_ok` is a parameter defaulting to False: the item is outside the decision records, so
   `phase0/branches.yaml` has no field for it yet. The decision label is `sentinel-dec-` followed by the decision
   id as 4.14 writes it (`sentinel-dec-dec-000517`); it is not hashed in either branch, since a decision id is
   `dec-` and digits, characters every `sentinel-` label already uses. A cluster ticket carries one `sentinel-cp-`
   label per member checkpoint and one surface label per surface.
2. Identity block. One block per linked decision, in the 9.9 key order; a decision without a finding links the run
   page as `report`. A block is read back from the description and the comments (a link adds its block as a
   comment, choice 5); keys repeat in one block (a canary Bug names every checkpoint), so `parse_identity` keeps
   every value. `{noformat}` wraps each block so Jira shows it verbatim, and free text from a finding (a Reviewer
   description) goes in a `{noformat}` block of its own, so it cannot inject markup (9.1).
3. JQL. 9.9's two queries, with every label value in double quotes (a surface id holds `.`, a JQL reserved
   character). `link_candidates` runs both in order, drops a ticket the first query already offered, ignores a
   collision, and offers only product-project tickets: a SENT canary Bug carries the same cp label but is never a
   product bug to link to.
4. Collision. A cp-label hit is a collision when no identity block on it names the checkpoint; a surface-label hit
   when no block names a checkpoint of that surface (a different checkpoint of the same surface is what that query
   exists to find). A hit with no identity block at all cannot prove its identity and counts as a collision.
5. Outbox jobs. 8.3 has three Jira kinds, so a create is `jira_create`, and a comment, a label edit and a link
   (labels plus the identity block as a comment) are `jira_comment`; the payload's `op` says which. The job's
   `destination` is `<project or ticket>#<dedup class>` (`TOS#bug`, `TOS-48391#seen_again`, `TOS-7#intended`,
   `SENT#task:<key>`): the sender takes its request from the payload, and the destination is what the 9.9 dedup
   reads in the outbox (one seen-again per ticket per 24 h, one intended comment per run per ticket, one SENT post
   per run per key). A job that was abandoned never posted, so it does not count.
6. Labels on an existing ticket. A link or an intended label edit adds a `sentinel-dec-` label only while the
   ticket has fewer than 8; past that the decision is in the description through its identity block (choice 2).
7. SENT keys (9.9 row 4, 9.4 item 2). `sent_task_key` is `<trigger>:<subject>`, the subject being what 9.9 keys the
   trigger by (`SENT_TRIGGERS`), whitespace collapsed. Triggers use the taxonomy's names where one exists
   (`env_error`, `harness_error`, the `aborted_*` run outcomes, `unregistered_surface`, `dependency_map_gap`,
   `unknown_popup`, `passed_via_agent_fallback`, `missed_in_pr_run`); the others are `no_covering_checkpoint` (a
   manifest entry with no covering checkpoint: the flow-authoring task), `abandoned_outbox_job` and `canary` (the
   SENT Bugs of 10.3, keyed by mutant id, class or stage as canary_integrations builds them). `missed_in_pr_run`
   is keyed by checkpoint id (9.9 lists no key for it). The condition "twice on one step and build" is the
   caller's; the key is the flow and step id. Every SENT ticket carries `sentinel-key-<12 hex of the key>`, the
   label its open-ticket search uses.
8. SENT delivery is an upsert: the sender searches the harness project for an open ticket with the key label and
   comments on it ("seen again in run X" with the run link), else creates the ticket. A comment queued while the
   key's create had not landed (`expect_open` with no ticket) waits (`JiraPending`, retried by the outbox) rather
   than create a second ticket.
9. Auto-filing gate. `auto_file` is the `policy/reporting.yaml` block (`tier1`..`tier3`, default off); the 3.14
   eval gates and the 30-day 100 percent money-cell canary rate are the caller's booleans. An auto-filed
   `collateral` is a SENT Bug with the product labels but no decision label (no decision exists yet).
10. Text. A summary is at most 255 characters (Jira's limit). Times in comments are the caller's ISO text.
11. Retries. The outbox delivers at least once, so a product Bug's delivery first searches for a ticket carrying
    its first decision's label and returns that one: a retry after Jira made the ticket but the write-back failed
    files nothing twice. A SENT upsert is idempotent the same way (choice 8).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..lint.rules import JIRA as JIRA_KEY
from ..orchestrator.alerts import AlertRoute
from ..records import OUTBOX_KINDS, Decision, Finding
from ..taxonomy import checkpoint_parts, load
from .inline import Urls, is_artifact_hash

PRODUCT_PROJECT = "TOS"                                  # 9.9: the product project (assumed key)
HARNESS_PROJECT = "SENT"                                 # 9.9: the harness project (assumed key)
BASE_LABEL = "sentinel"
REOPENED_LABEL = "sentinel-reopened"                     # 9.9: a reopen the workflow forbids
MAX_DEC_LABELS = 8                                       # 9.9: at most 8 decision labels
HASH_HEX = 12                                            # 9.9: first 12 hex of sha256
SEEN_AGAIN_HOURS = 24                                    # 9.9: at most once per ticket per 24 hours
SUMMARY_MAX = 255                                        # Jira's summary limit (choice 10)
PRIORITY_BY_TIER = {1: "Highest", 2: "Medium", 3: "Low"}            # 9.9 confirm_bug row
CANARY_PRIORITY = {"detector_blind": "Highest", "generation_failed": "Highest", "detector_degraded": "High"}
IDENTITY_KEYS = ("decision", "checkpoint", "region", "category", "phash", "run", "report")   # 9.9 block order
JIRA_KINDS = ("jira_create", "jira_comment", "jira_transition")     # the 8.3 outbox kinds that go to Jira
#: trigger -> (what 9.9 keys it by, the owner role of its task). Choice 7.
SENT_TRIGGERS: dict[str, tuple[str, str]] = {
    "env_error": ("gate name", "harness_on_call"),
    "harness_error": ("error class", "harness_on_call"),
    "aborted_safety": ("error class", "harness_on_call"),
    "aborted_budget": ("error class", "harness_on_call"),
    "aborted_harness": ("error class", "harness_on_call"),
    "unregistered_surface": ("surface label", "harness_on_call"),
    "dependency_map_gap": ("edge", "harness_on_call"),
    "unknown_popup": ("popup title", "harness_on_call"),
    "no_covering_checkpoint": ("manifest entry id", "flow_owner"),
    "passed_via_agent_fallback": ("flow and step id", "flow_owner"),
    "abandoned_outbox_job": ("outbox job kind", "harness_on_call"),
    "missed_in_pr_run": ("checkpoint id", "system_owner"),
    "canary": ("mutant id, class or stage", "harness_on_call"),
}
_DEC = re.compile(r"^dec-[0-9]+$")
_DEC_LABEL = "sentinel-dec-"
_NOFORMAT = "{noformat}"
_BLOCK_LINE = re.compile(r"^  ([a-z_]+): (.*)$")


class JiraUnavailable(Exception):
    """Jira did not answer: the outbox job fails and retries on the 9.1 schedule (9.9 Jira unavailable)."""


class JiraPending(Exception):
    """A SENT comment whose ticket's create has not landed yet: the outbox retries it (choice 8)."""


# ---------------------------------------------------------------------------------------------------------------
# labels (9.9, 11.8)

def label_hash(text: str) -> str:
    """The first 12 hex of SHA-256 over the UTF-8 text: an id as a label carries it (9.9, 11.8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_HEX]


def cp_label(checkpoint_id: str) -> str:
    """`sentinel-cp-{first 12 hex of sha256(checkpoint id)}` (9.9)."""
    checkpoint_parts(checkpoint_id)
    return f"sentinel-cp-{label_hash(checkpoint_id)}"


def surface_label(surface_id: str, *, charset_ok: bool = False) -> str:
    """`sentinel-surface-{surface id}` when Jira labels accept `.` and `-`, else the hashed form (11.8)."""
    if not surface_id:
        raise ValueError("a surface label needs the surface id")
    return f"sentinel-surface-{surface_id if charset_ok else label_hash(surface_id)}"


def tier_label(tier: int) -> str:
    """`sentinel-tier-{1|2|3}` (9.9)."""
    if tier not in PRIORITY_BY_TIER:
        raise ValueError(f"tier {tier!r}; one of 1, 2, 3")
    return f"sentinel-tier-{tier}"


def dec_label(decision_id: str) -> str:
    """`sentinel-dec-{decision id}` (9.9; choice 1)."""
    if not _DEC.match(decision_id or ""):
        raise ValueError(f"decision id {decision_id!r} is not dec-<sequence> (4.14)")
    return _DEC_LABEL + decision_id


def decision_of_label(label: str) -> str | None:
    """The decision id a `sentinel-dec-` label names, else None."""
    rest = label[len(_DEC_LABEL):] if label.startswith(_DEC_LABEL) else ""
    return rest if _DEC.match(rest) else None


def key_label(key: str) -> str:
    """`sentinel-key-<12 hex>`: the label a SENT ticket's open-ticket search uses (choice 7)."""
    return f"sentinel-key-{label_hash(key)}"


@dataclass(frozen=True)
class Labels:
    """The labels of a ticket, and the decisions past the eighth, which go in the description (9.9)."""
    labels: tuple[str, ...]
    overflow: tuple[str, ...] = ()


def labels(checkpoint_ids: str | Sequence[str], *, tier: int | None = None, decision_ids: Sequence[str] = (),
           charset_ok: bool = False) -> Labels:
    """The 9.9 labels, in its order: sentinel, tier, surface, cp, then at most 8 decision labels."""
    cps = [checkpoint_ids] if isinstance(checkpoint_ids, str) else list(checkpoint_ids)
    out = [BASE_LABEL] + ([tier_label(tier)] if tier is not None else [])
    out += [surface_label(checkpoint_parts(c)["surface"], charset_ok=charset_ok) for c in cps]
    out += [cp_label(c) for c in cps]
    decs = list(dict.fromkeys(decision_ids))
    out += [dec_label(d) for d in decs[:MAX_DEC_LABELS]]
    return Labels(tuple(dict.fromkeys(out)), tuple(decs[MAX_DEC_LABELS:]))


# ---------------------------------------------------------------------------------------------------------------
# the identity block (9.9)

def report_link(decision: Decision, report_base: str) -> str:
    """The `report` line of a decision's block: its finding's page, or its run's page without one."""
    urls = Urls(report_base.rstrip("/"), decision.run_id or "")
    if decision.finding_ref:
        return urls.finding(decision.finding_ref)
    if not decision.run_id:
        raise ValueError(f"{decision.decision_id} names neither a finding nor a run for its report link")
    return urls.report


def identity_block(decision: Decision, report_base: str) -> str:
    """The machine-readable block of 9.9 for one decision, the ticket's identity."""
    s = decision.signature
    values = (decision.decision_id, s.checkpoint_id, s.region_id, s.category, s.phash, decision.run_id or "none",
              report_link(decision, report_base))
    return "\n".join(["sentinel:"] + [f"  {k}: {v}" for k, v in zip(IDENTITY_KEYS, values)])


def _fenced(block: str) -> str:
    return f"{_NOFORMAT}\n{block}\n{_NOFORMAT}"


def parse_identity(text: str) -> list[dict[str, list[str]]]:
    """Every `sentinel:` block in `text` (description and comments), each key with all its values (choice 2)."""
    blocks: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    for line in text.splitlines():
        if line.strip() == "sentinel:":
            current = {}
            blocks.append(current)
            continue
        m = _BLOCK_LINE.match(line.rstrip())
        if current is not None and m:
            current.setdefault(m.group(1), []).append(m.group(2).strip())
        else:
            current = None
    return [b for b in blocks if b]


# ---------------------------------------------------------------------------------------------------------------
# what a client sees of a ticket, and the client protocol

@dataclass(frozen=True)
class Ticket:
    """A Jira issue as the Reporter reads it."""
    key: str
    labels: tuple[str, ...] = ()
    description: str = ""
    comments: tuple[str, ...] = ()
    status_category: str = "new"           # Jira's statusCategory key: new, indeterminate or done
    fix_versions: tuple[str, ...] = ()
    build_id: str | None = None            # the build id field, where the project has one (9.9)
    resolver: str | None = None            # login of whoever moved it to Done
    resolved_at: str | None = None

    @property
    def project(self) -> str:
        return self.key.rsplit("-", 1)[0]

    @property
    def done(self) -> bool:
        return self.status_category == "done"

    @property
    def text(self) -> str:
        return "\n".join((self.description, *self.comments))

    def identity(self) -> list[dict[str, list[str]]]:
        return parse_identity(self.text)


def ticket_decisions(ticket: Ticket) -> tuple[str, ...]:
    """Every decision linked to the ticket: its `sentinel-dec-` labels and the `decision` of each identity block
    (the decisions past the eighth live only there, 9.9), sorted."""
    ids = {d for d in map(decision_of_label, ticket.labels) if d}
    ids |= {d for b in ticket.identity() for d in b.get("decision", ()) if _DEC.match(d)}
    return tuple(sorted(ids))


class JiraClient(Protocol):
    """The service account's operations (9.9: create, comment, transition and JQL search). Every write it makes is
    an outbox job's delivery; a transport failure raises `JiraUnavailable`."""

    def search(self, jql: str) -> Sequence[Ticket]: ...

    def get(self, key: str) -> Ticket | None: ...

    def create(self, fields: Mapping[str, Any]) -> str: ...

    def comment(self, key: str, body: str) -> None: ...

    def add_labels(self, key: str, labels: Sequence[str]) -> None: ...

    def transition(self, key: str, name: str) -> bool:
        """Apply the named workflow transition; False when the workflow forbids it from the current status."""
        ...


def valid_key(key: str) -> bool:
    """The Jira key regex of L-MAN-05, checked at once on mark intended (9.9)."""
    return isinstance(key, str) and bool(JIRA_KEY.match(key))


# ---------------------------------------------------------------------------------------------------------------
# product Bugs: priority, summary, description (9.9 confirm_bug row)

def priority(tier: int) -> str:
    """Priority by tier: 1 Highest, 2 Medium, 3 Low (9.9)."""
    if tier not in PRIORITY_BY_TIER:
        raise ValueError(f"tier {tier!r}; one of 1, 2, 3")
    return PRIORITY_BY_TIER[tier]


@dataclass(frozen=True)
class TicketContext:
    """The run-level facts a product ticket names that no finding carries."""
    report_base: str
    build_pair: str                        # "candidate vs base" as the report header prints it
    pr: int | None = None
    workspace: str | None = None           # census checkpoints name theirs; others take this one
    product_project: str = PRODUCT_PROJECT
    charset_ok: bool = False               # 11.8 Phase 0 read: Jira labels accept `.` and `-`


def _clip(text: str, limit: int = SUMMARY_MAX) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def summary(finding: Finding) -> str:
    """`[Sentinel] tier-1 invariant_violation on <surface> (<region>)`, at most 255 characters."""
    surface = checkpoint_parts(finding.checkpoint_id)["surface"]
    tier = f"tier-{finding.tier} " if finding.tier else ""
    region = f" ({finding.region_id})" if finding.region_id else ""
    return _clip(f"[Sentinel] {tier}{finding.category or finding.kind} on {surface}{region}")


def _size(cid: str) -> str | None:
    p = checkpoint_parts(cid)
    if p["kind"] == "sweep":
        return p["qualifier"].split("/", 1)[1]
    if p["kind"] == "split":
        return p["qualifier"]
    return None


def reproduction(checkpoint_id: str) -> str:
    """The census path, or the flow and step, a human replays (9.9); a sweep or split names its step."""
    p = checkpoint_parts(checkpoint_id)
    q = p["qualifier"].split("/")
    if p["kind"] == "census":
        return f"census path {checkpoint_id}"
    if p["kind"] == "flow":
        return f"flow {q[0]}, step {q[1]}"
    if p["kind"] == "sweep":
        return f"size sweep of {p['surface']} in {q[0]} at {q[1]}"
    return f"splitter {q[0]} of {p['surface']} at fraction {q[1]}"


def _workspace(cid: str, ctx: TicketContext) -> str:
    p = checkpoint_parts(cid)
    return p["qualifier"].split("/")[0] if p["kind"] == "census" else (ctx.workspace or "not recorded")


def _crop_line(urls: Urls, findings: Sequence[Finding]) -> str:
    """The 1:1 crop links of the findings' evidence artifacts (`/a/<sha256>`, 9.1), once each."""
    crops = [urls.artifact(a) for f in findings for a in f.evidence.get("artifacts", ()) if is_artifact_hash(a)]
    return "Crops (1:1): " + (" ".join(dict.fromkeys(crops)) or "none recorded")


def _quoted(text: str) -> str:
    """Free text as data: in a noformat block it cannot open one of its own (9.1)."""
    return _fenced(text.replace(_NOFORMAT, "{ noformat }"))


def description(decisions: Sequence[Decision], findings: Sequence[Finding], ctx: TicketContext) -> str:
    """The product Bug's description (9.9): build pair, surface, primitive, manifest entry, PR, layout, size or
    failing range, workspace, 1:1 crop links, reproduction, then one identity block per decision."""
    if not findings:
        raise ValueError("a ticket describes at least one finding")
    first = findings[0]
    urls = Urls(ctx.report_base.rstrip("/"), first.run_id)
    cps = list(dict.fromkeys(f.checkpoint_id for f in findings))
    sizes = [s for s in map(_size, cps) if s]
    layout = first.evidence.get("layout")
    lines = [f"Sentinel finding {first.finding_id}: {first.category or first.kind}"
             + (f" in {first.region_id}" if first.region_id else ""),
             _quoted(first.description), "",
             f"Build pair: {ctx.build_pair}",
             f"Surface: {checkpoint_parts(cps[0])['surface']}" + (f" (tier {first.tier})" if first.tier else ""),
             f"Primitive: {first.primitive or 'none'}",
             f"Manifest entry: {first.manifest_entry_ref or 'none (undeclared)'}",
             f"PR: {ctx.pr if ctx.pr is not None else 'none'}"]
    if isinstance(layout, Mapping):
        lines.append(f"Layout: grid {layout.get('grid', '?')}, cell {layout.get('cell', '?')}")
    if len(sizes) == 1:
        lines.append(f"Size: {sizes[0]}")
    elif sizes:
        lines.append(f"Failing range: {sizes[0]} to {sizes[-1]} ({len(sizes)} steps: {', '.join(sizes)})")
    lines += [f"Workspace: {_workspace(cps[0], ctx)}",
              _crop_line(urls, findings),
              "Reproduction: " + "; ".join(reproduction(c) for c in cps),
              f"Report: {urls.finding(first.finding_id)}", ""]
    lines += [_fenced(identity_block(d, ctx.report_base)) for d in decisions]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# dedup on a new confirm_bug (9.9)

def _jql_labels(*names: str) -> str:
    return " AND ".join(f'labels = "{n}"' for n in names)


def dedup_query(checkpoint_id: str, *, charset_ok: bool = False) -> tuple[str, str]:
    """The two JQL searches of 9.9, in order: by the cp label, then by the surface label; open tickets only."""
    surface = checkpoint_parts(checkpoint_id)["surface"]
    return tuple(f"{_jql_labels(BASE_LABEL, lb)} AND statusCategory != Done"
                 for lb in (cp_label(checkpoint_id), surface_label(surface, charset_ok=charset_ok)))


def is_collision(ticket: Ticket, checkpoint_id: str, *, by: str = "cp") -> bool:
    """A ticket found by label whose identity names another checkpoint (`by="cp"`) or another surface
    (`by="surface"`): ignored for linking, counted in Run health (9.9; choice 4)."""
    named = [c for b in ticket.identity() for c in b.get("checkpoint", ())]
    if by == "cp":
        return checkpoint_id not in named
    if by == "surface":
        surface = checkpoint_parts(checkpoint_id)["surface"]
        return not any(c.split("/")[1:2] == [surface] for c in named)
    raise ValueError(f"by {by!r}; cp or surface")


@dataclass(frozen=True)
class LinkCandidates:
    """What the UI offers as "link to existing ticket", and the label collisions it ignored (9.9)."""
    offers: tuple[Ticket, ...] = ()
    collisions: tuple[Ticket, ...] = ()


def link_candidates(client: JiraClient, checkpoint_id: str, *, charset_ok: bool = False,
                    product_project: str = PRODUCT_PROJECT) -> LinkCandidates:
    """Run the 9.9 searches for a new confirm_bug on `checkpoint_id` (choice 3)."""
    offers: dict[str, Ticket] = {}
    collisions: dict[str, Ticket] = {}
    for by, jql in zip(("cp", "surface"), dedup_query(checkpoint_id, charset_ok=charset_ok)):
        for t in client.search(jql):
            if t.key in offers or t.key in collisions or t.done:
                continue
            if is_collision(t, checkpoint_id, by=by):
                collisions[t.key] = t
            elif t.project == product_project:
                offers[t.key] = t
    return LinkCandidates(tuple(offers.values()), tuple(collisions.values()))


# ---------------------------------------------------------------------------------------------------------------
# rules: seen again, auto-filing (9.9)

def _at(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        raise ValueError(f"{ts!r} has no offset (4 preamble: RFC 3339 with an explicit offset)")
    return d


def seen_again_due(last_posted_at: str | None, now: str) -> bool:
    """A seen-again comment may post: none on the ticket yet, or the last one at least 24 hours ago (9.9)."""
    return last_posted_at is None or _at(now) - _at(last_posted_at) >= timedelta(hours=SEEN_AGAIN_HOURS)


def auto_file_allowed(auto_file: Mapping[str, Any] | None, tier: int, *, eval_gates_met: bool,
                      money_canary_full_30d: bool) -> bool:
    """Auto-filing of `collateral` for `tier` (9.9): the system owner's switch for the tier in
    `policy/reporting.yaml` `auto_file` (off by default), the Reviewer meeting the 3.14 gates on the current eval
    set, and a 100 percent money-cell canary pass rate for 30 days. All three, or nothing is auto-filed."""
    tier_label(tier)
    return bool((auto_file or {}).get(f"tier{tier}", False)) and eval_gates_met and money_canary_full_30d


# ---------------------------------------------------------------------------------------------------------------
# SENT keys (9.9 row 4, 9.4 item 2)

@dataclass(frozen=True)
class SentEvent:
    """One event that opens or extends a SENT task (9.9): its trigger, the subject the trigger is keyed by, the
    run it happened in and a line of detail."""
    trigger: str
    subject: str
    run_id: str
    detail: str = ""
    owner: str | None = None               # None: the trigger's owner role (SENT_TRIGGERS)

    def __post_init__(self):
        if self.trigger not in SENT_TRIGGERS:
            raise ValueError(f"SENT trigger {self.trigger!r}; one of {sorted(SENT_TRIGGERS)}")
        if not " ".join(str(self.subject).split()):
            raise ValueError(f"a {self.trigger} task names its {SENT_TRIGGERS[self.trigger][0]}")
        if self.trigger == "env_error":
            load().check_member("gates", self.subject.split(":", 1)[0])
        if self.trigger == "abandoned_outbox_job" and self.subject not in OUTBOX_KINDS:
            raise ValueError(f"outbox job kind {self.subject!r}; one of {list(OUTBOX_KINDS)}")
        if not self.run_id:
            raise ValueError("a SENT event names its run")

    @property
    def role(self) -> str:
        return self.owner or SENT_TRIGGERS[self.trigger][1]


def task_key(trigger: str, subject: str) -> str:
    """`<trigger>:<subject>`, whitespace collapsed (choice 7)."""
    if trigger not in SENT_TRIGGERS:
        raise ValueError(f"SENT trigger {trigger!r}; one of {sorted(SENT_TRIGGERS)}")
    return f"{trigger}:{' '.join(str(subject).split())}"


def sent_task_key(event: SentEvent) -> str:
    """One open SENT ticket per key (9.9): the event's `task_key`."""
    return task_key(event.trigger, event.subject)


def flow_task_phrase(ticket: str | None) -> str:
    """The reconciliation's words for a manifest entry with no covering checkpoint (9.4 item 3): the task's key
    once it exists, else pending, never "opened" for a task that does not exist."""
    return f"flow-authoring task {ticket} opened" if ticket else "flow-authoring task pending"


# ---------------------------------------------------------------------------------------------------------------
# outbox jobs (choice 5)

@dataclass(frozen=True)
class JiraJob:
    """One Jira post before it is a send: the outbox kind, the destination it is deduplicated by, the payload."""
    kind: str
    destination: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in JIRA_KINDS:
            raise ValueError(f"Jira outbox kind {self.kind!r}; one of {list(JIRA_KINDS)}")

    @property
    def op(self) -> str:
        return self.payload["op"]


def _real(findings: Sequence[Finding]) -> bool:
    """Synthetic records never post (9.9, 10.3)."""
    return bool(findings) and not any(f.synthetic for f in findings)


def bug_job(decisions: Sequence[Decision], findings: Sequence[Finding], ctx: TicketContext) -> JiraJob | None:
    """The product Bug of a confirm_bug (9.9): one ticket for the decision and its cluster members; None when a
    finding is synthetic. The key lands on every decision (`report.jira_sync.record`)."""
    if not decisions or not _real(findings):
        return None
    if any(d.action != "confirm_bug" for d in decisions):
        raise ValueError("a product Bug is filed for confirm_bug decisions (9.9)")
    first = findings[0]
    lab = labels([f.checkpoint_id for f in findings], tier=first.tier,
                 decision_ids=[d.decision_id for d in decisions], charset_ok=ctx.charset_ok)
    issue = {"project": ctx.product_project, "issuetype": "Bug",
             "priority": priority(first.tier) if first.tier else "Medium", "summary": summary(first),
             "labels": list(lab.labels), "description": description(decisions, findings, ctx)}
    return JiraJob("jira_create", f"{ctx.product_project}#bug",
                   {"op": "bug", "decisions": [d.decision_id for d in decisions], "issue": issue})


def link_job(ticket: str, decisions: Sequence[Decision], *, report_base: str, tier: int | None = None,
             linked_by: str | None = None, charset_ok: bool = False) -> JiraJob:
    """Linking decisions to an existing ticket (9.9): the sentinel, tier, surface, cp and decision labels, and
    each decision's identity block as a comment."""
    if not valid_key(ticket):
        raise ValueError(f"ticket {ticket!r} does not match ^[A-Z][A-Z0-9]+-[0-9]+$ (L-MAN-05)")
    lab = labels([d.signature.checkpoint_id for d in decisions], tier=tier,
                 decision_ids=[d.decision_id for d in decisions], charset_ok=charset_ok)
    head = f"Sentinel linked {', '.join(d.decision_id for d in decisions)} to this ticket" + (
        f" (by {linked_by})" if linked_by else "") + "."
    body = "\n".join([head] + [_fenced(identity_block(d, report_base)) for d in decisions])
    return JiraJob("jira_comment", f"{ticket}#link",
                   {"op": "link", "ticket": ticket, "decisions": [d.decision_id for d in decisions],
                    "labels": list(lab.labels), "comment": body})


def seen_again_job(ticket: str, finding: Finding, *, run_id: str, build_id: str | None,
                   report_base: str) -> JiraJob | None:
    """"seen again in run X on build Y" with the finding link, on the open ticket of an inherited confirm_bug
    (9.9); None for a synthetic finding. `report.jira_sync.seen_again` applies the 24-hour rule."""
    if not _real([finding]):
        return None
    link = Urls(report_base.rstrip("/"), run_id).finding(finding.finding_id)
    body = f"seen again in run {run_id} on build {build_id or 'unknown'}: {link}"
    return JiraJob("jira_comment", f"{ticket}#seen_again",
                   {"op": "seen_again", "ticket": ticket, "run": run_id, "finding": finding.finding_id,
                    "comment": body})


def intended_job(ticket: str, decision: Decision, findings: Sequence[Finding], *, run_id: str, report_base: str,
                 comment: bool, manifest_pr: str | None = None) -> JiraJob | None:
    """Mark intended with ticket (9.9): the `sentinel` and decision labels, and with `comment` the evidence comment
    (the 1:1 crop links that replace hand screenshots, the run page and the manifest amendment PR). The key is
    checked against the Jira regex here (ValueError) and for existence on delivery. None for synthetic findings."""
    if not valid_key(ticket):
        raise ValueError(f"ticket {ticket!r} does not match ^[A-Z][A-Z0-9]+-[0-9]+$ (L-MAN-05)")
    if not _real(findings):
        return None
    urls = Urls(report_base.rstrip("/"), run_id)
    body = None
    if comment:
        body = "\n".join([f"Sentinel evidence for {ticket} in run {run_id}: {urls.report}",
                          *(f"{f.checkpoint_id}: {urls.finding(f.finding_id)}" for f in findings),
                          _crop_line(urls, findings),
                          f"Manifest amendment PR: {manifest_pr or 'none (surface declared)'}"])
    return JiraJob("jira_comment", f"{ticket}#intended",
                   {"op": "intended", "ticket": ticket, "run": run_id, "decisions": [decision.decision_id],
                    "findings": [f.finding_id for f in findings],
                    "labels": [BASE_LABEL, dec_label(decision.decision_id)], "comment": body})


def _sent_payload(op: str, key: str, run_id: str, issue: dict, report_base: str, project: str,
                  ticket: str | None, expect_open: bool, detail: str) -> dict:
    link = Urls(report_base.rstrip("/"), run_id).report
    return {"op": op, "key": key, "key_label": key_label(key), "project": project, "ticket": ticket,
            "expect_open": expect_open, "run": run_id, "issue": issue,
            "comment": f"seen again in run {run_id}: {link}" + (f"\n{_quoted(detail)}" if detail else "")}


def task_job(event: SentEvent, *, report_base: str, ticket: str | None = None, expect_open: bool = False,
             project: str = HARNESS_PROJECT) -> JiraJob:
    """A SENT task with the run link (9.9 row 4), upserted on delivery by its key label (choice 8). `ticket` and
    `expect_open` come from the open-task ledger (`report.jira_sync.sent_task`)."""
    key = sent_task_key(event)
    link = Urls(report_base.rstrip("/"), event.run_id).report
    what = SENT_TRIGGERS[event.trigger][0]
    title = ("update scripted flow: " if event.trigger == "passed_via_agent_fallback" else
             "flow-authoring task: " if event.trigger == "no_covering_checkpoint" else f"{event.trigger}: ")
    block = "\n".join(["sentinel:", f"  task: {key}", f"  run: {event.run_id}", f"  report: {link}"])
    issue = {"project": project, "issuetype": "Task", "summary": _clip(f"[Sentinel] {title}{event.subject}"),
             "labels": [BASE_LABEL, key_label(key)],
             "description": "\n".join([f"Sentinel opened this task for the {what} {event.subject} (owner: "
                                       f"{event.role}).", f"Run: {link}"]
                                      + ([_quoted(event.detail)] if event.detail else []) + [_fenced(block)])}
    return JiraJob("jira_create" if ticket is None and not expect_open else "jira_comment", f"{project}#task:{key}",
                   _sent_payload("task", key, event.run_id, issue, report_base, project, ticket, expect_open,
                                 event.detail))


def canary_job(bug: Mapping[str, Any], *, run_id: str, report_base: str, ticket: str | None = None,
               expect_open: bool = False) -> JiraJob:
    """A canary SENT Bug of `canary_integrations.sent_bug_payload` (9.9 row 5, 10.3) as a job keyed by its mutant
    id, class or stage, with the key label added for the open-ticket search."""
    if bug.get("priority") not in CANARY_PRIORITY.values():
        raise ValueError(f"canary Bug priority {bug.get('priority')!r}; one of {sorted(set(CANARY_PRIORITY.values()))}")
    key = sent_task_key(SentEvent("canary", str(bug["dedup_key"]), run_id))
    project = str(bug.get("project") or HARNESS_PROJECT)
    issue = {k: bug[k] for k in ("project", "issuetype", "priority", "summary", "description")}
    issue["labels"] = list(dict.fromkeys([*bug.get("labels", ()), key_label(key)]))
    return JiraJob("jira_create" if ticket is None and not expect_open else "jira_comment", f"{project}#task:{key}",
                   _sent_payload("canary", key, run_id, issue, report_base, project, ticket, expect_open, ""))


def auto_file_job(finding: Finding, ctx: TicketContext, *, project: str = HARNESS_PROJECT) -> JiraJob | None:
    """An auto-filed `collateral` (9.9): a SENT Bug for triage, never the product backlog; the caller checked
    `auto_file_allowed`. None for anything but a real tier-scored `collateral` finding."""
    if finding.category != "collateral" or finding.tier is None or not _real([finding]):
        return None
    lab = labels(finding.checkpoint_id, tier=finding.tier, charset_ok=ctx.charset_ok)
    urls = Urls(ctx.report_base.rstrip("/"), finding.run_id)
    issue = {"project": project, "issuetype": "Bug", "priority": priority(finding.tier),
             "summary": _clip("[Sentinel triage] " + summary(finding).removeprefix("[Sentinel] ")),
             "labels": list(lab.labels),
             "description": "\n".join(["Auto-filed for triage; a human confirms it before it reaches the product "
                                       f"backlog. Finding: {urls.finding(finding.finding_id)}",
                                       _quoted(finding.description)])}
    return JiraJob("jira_create", f"{project}#auto_file", {"op": "auto_file", "finding": finding.finding_id,
                                                           "issue": issue})


# ---------------------------------------------------------------------------------------------------------------
# delivery (run by the outbox's Jira sender)

@dataclass(frozen=True)
class Delivered:
    """What one delivery did: the ticket it reached or made, and whether Jira knew the named ticket."""
    op: str
    ticket: str | None
    created: bool = False
    commented: bool = False
    not_found: bool = False
    transitioned: bool | None = None       # reopen: False when the workflow forbade it
    alert: AlertRoute | None = None        # reopen: the P2 alert a forbidden transition raises


def _add_labels(client: JiraClient, t: Ticket, wanted: Sequence[str]) -> None:
    """Add the labels the ticket lacks; decision labels only while it has fewer than 8 (choice 6)."""
    room = MAX_DEC_LABELS - sum(1 for lb in t.labels if decision_of_label(lb))
    add = []
    for lb in wanted:
        if lb in t.labels or lb in add:
            continue
        if decision_of_label(lb):
            if room <= 0:
                continue
            room -= 1
        add.append(lb)
    if add:
        client.add_labels(t.key, add)


def _open_by_key(client: JiraClient, project: str, label: str) -> Ticket | None:
    jql = f"project = {project} AND {_jql_labels(BASE_LABEL, label)} AND statusCategory != Done"
    hits = [t for t in client.search(jql) if not t.done]
    return min(hits, key=lambda t: int(t.key.rsplit("-", 1)[1])) if hits else None


def deliver(client: JiraClient, payload: Mapping[str, Any]) -> Delivered:
    """Carry one job's payload to Jira. `JiraUnavailable` (and `JiraPending`) leave the job to the outbox's
    retries; a named ticket Jira does not know returns `not_found` (the 9.9 ticket-not-found path)."""
    op = payload.get("op")
    if op == "bug":                        # a retried create finds the ticket it made by its first decision label
        made = client.search(_jql_labels(BASE_LABEL, dec_label(payload["decisions"][0])))
        if made:
            return Delivered(op, made[0].key)
    if op in ("bug", "auto_file"):
        return Delivered(op, client.create(payload["issue"]), created=True)
    if op in ("link", "seen_again", "intended"):
        t = client.get(payload["ticket"])
        if t is None:
            return Delivered(op, payload["ticket"], not_found=True)
        if payload.get("labels"):
            _add_labels(client, t, payload["labels"])
        if payload.get("comment"):
            client.comment(t.key, payload["comment"])
        return Delivered(op, t.key, commented=bool(payload.get("comment")))
    if op in ("task", "canary"):
        hit = _open_by_key(client, payload["project"], payload["key_label"])
        if hit is not None:
            client.comment(hit.key, payload["comment"])
            return Delivered(op, hit.key, commented=True)
        if payload.get("expect_open") and payload.get("ticket") is None:
            raise JiraPending(f"the SENT ticket of {payload['key']} has not landed yet")
        return Delivered(op, client.create(payload["issue"]), created=True)
    raise ValueError(f"not a Jira job payload: op {op!r}")


# ---------------------------------------------------------------------------------------------------------------
# the in-memory Jira of the tests and the demo (the live REST transport is a Phase 2 item, 11.1 row 10)

class MemoryJira:
    """A JiraClient over dictionaries. Its JQL is the subset this module writes: `AND`-joined `labels = "x"`,
    `project = X`, `statusCategory = Done` and `statusCategory != Done`. `unreachable` makes every call raise
    `JiraUnavailable`; `forbid` names tickets whose workflow refuses the reopen transition."""

    def __init__(self, *, unreachable: bool = False, forbid: Sequence[str] = ()):
        self.unreachable = unreachable
        self.forbid = set(forbid)
        self.issues: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self._next: dict[str, int] = {}

    def _call(self, op: str, key: str) -> None:
        if self.unreachable:
            raise JiraUnavailable("Jira did not answer")
        self.calls.append((op, key))

    def _ticket(self, key: str) -> Ticket:
        i = self.issues[key]
        return Ticket(key, tuple(i["labels"]), i["description"], tuple(i["comments"]), i["status"],
                      tuple(i["fix_versions"]), i["build_id"], i["resolver"], i["resolved_at"])

    def add(self, key: str, *, labels: Sequence[str] = (), description: str = "", status: str = "new") -> Ticket:
        """Seed an existing issue."""
        self.issues[key] = {"labels": list(labels), "description": description, "comments": [], "status": status,
                            "fix_versions": [], "build_id": None, "resolver": None, "resolved_at": None,
                            "fields": {}}
        project, n = key.rsplit("-", 1)
        self._next[project] = max(self._next.get(project, 0), int(n))
        return self._ticket(key)

    def resolve(self, key: str, *, by: str, at: str, fix_versions: Sequence[str] = (),
                build_id: str | None = None) -> None:
        """Move an issue to Done as `by` would."""
        self.issues[key].update(status="done", resolver=by, resolved_at=at, fix_versions=list(fix_versions),
                                build_id=build_id)

    def _match(self, key: str, clause: str) -> bool:
        i = self.issues[key]
        m = re.fullmatch(r'labels = "([^"]+)"', clause)
        if m:
            return m.group(1) in i["labels"]
        m = re.fullmatch(r"project = (\S+)", clause)
        if m:
            return key.rsplit("-", 1)[0] == m.group(1)
        m = re.fullmatch(r"statusCategory (=|!=) Done", clause)
        if m:
            return (i["status"] == "done") == (m.group(1) == "=")
        raise ValueError(f"MemoryJira cannot read the JQL clause {clause!r}")

    def search(self, jql: str) -> list[Ticket]:
        self._call("search", jql)
        clauses = jql.split(" AND ")
        return [self._ticket(k) for k in sorted(self.issues) if all(self._match(k, c) for c in clauses)]

    def get(self, key: str) -> Ticket | None:
        self._call("get", key)
        return self._ticket(key) if key in self.issues else None

    def create(self, fields: Mapping[str, Any]) -> str:
        project = str(fields["project"])
        self._call("create", project)
        self._next[project] = self._next.get(project, 0) + 1
        key = f"{project}-{self._next[project]}"
        self.add(key, labels=fields.get("labels", ()), description=str(fields.get("description", "")))
        self.issues[key]["fields"] = dict(fields)
        return key

    def comment(self, key: str, body: str) -> None:
        self._call("comment", key)
        self.issues[key]["comments"].append(body)

    def add_labels(self, key: str, labels: Sequence[str]) -> None:
        self._call("add_labels", key)
        self.issues[key]["labels"] += [lb for lb in labels if lb not in self.issues[key]["labels"]]

    def transition(self, key: str, name: str) -> bool:
        self._call(f"transition:{name}", key)
        if key in self.forbid:
            return False
        self.issues[key].update(status="indeterminate", resolver=None, resolved_at=None)
        return True


__all__ = [
    "BASE_LABEL", "CANARY_PRIORITY", "Delivered", "HARNESS_PROJECT", "IDENTITY_KEYS", "JIRA_KINDS", "JiraClient",
    "JiraJob", "JiraPending", "JiraUnavailable", "Labels", "LinkCandidates", "MAX_DEC_LABELS", "MemoryJira",
    "PRIORITY_BY_TIER", "PRODUCT_PROJECT", "REOPENED_LABEL", "SENT_TRIGGERS", "SentEvent", "Ticket", "TicketContext",
    "auto_file_allowed", "auto_file_job", "bug_job", "canary_job", "cp_label", "dec_label", "decision_of_label",
    "dedup_query", "deliver", "description", "flow_task_phrase", "identity_block", "intended_job", "is_collision",
    "key_label", "label_hash", "labels", "link_candidates", "link_job", "parse_identity", "priority", "report_link",
    "reproduction", "seen_again_due", "seen_again_job", "sent_task_key", "summary", "surface_label", "task_job",
    "task_key", "ticket_decisions", "tier_label", "valid_key",
]
