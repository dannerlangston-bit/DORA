"""The decision lifecycle jobs over the Central Store (4.14, 8.3, 9.2, 9.5, 9.6, 9.7, 9.9), through its single writer.

`Store.record_decision` writes decisions; this module moves them afterwards, and reads what the report pages need:

- `verify_fixes(store, run_id)` is the post-run fix verification of L-DEC-08 and 9.9: for every fixed_in_build
  decision whose fix build the run's candidate build has reached, on a checkpoint the run compared, the signature
  is asserted absent. Absent: the decision's finding moves from fixed_pending_verify to closed. Present: the
  decision's status becomes reopened and `reopen_count` grows, its finding is open again, and a `jira_transition`
  outbox job reopens the ticket; two reopenings make the decision suspect (9.5, 9.7).
- `expire_due(store, now)` is the daily expiry job of the 9.6 and 9.7 "Ends" columns: noise_signature, waivers
  and every row with an `expires_at` at expiry; request_mask at its mask's expiry once linked; agent_mistake and
  expected_behavior when their signature has been absent 30 days; a plain mark_intended once its checkpoint's
  baseline is approved, or when its ticket left every manifest in scope. Expired noise signatures and masks are
  returned for the A/A re-evaluation (`reevaluate`). `expiring_within` and `suspect_decisions` feed the Decisions
  page (9.5).
- `reconfirm(store, decision_id, by=)` is the one-click re-confirmation of an expiring noise signature (9.7): a
  new row superseding the old one, 30 days after the old expiry.
- `ticket_not_found(store, decision_id)` is the 9.9 answer to a ticket key Jira says does not exist: the decision
  expires in one business day, its finding returns to the triage queue with "ticket not found", and the decider is
  notified through the triage channel.
- `lookup_explorer_dispositions(store, signature)` returns the active agent_mistake and expected_behavior
  decisions that hold a later Explorer finding with the signature (9.6, 9.7); `hold_explorer_finding` applies the
  hold to the finding before it is written (held, the decision's triage state, the candidate flow discarded or
  kept).
- `first_seen(store, finding)` is the 9.2 novelty input: the run that first showed the finding's signature within
  the last 30 days, or the finding's own run when it is new.
- `export_decisions(store, directory)` is the nightly export to `decisions/` (4.14 intro): one YAML file per
  decision in the 4.14 record shape, checked against schemas/decision.schema.json.

Choices where the spec is ambiguous (reported to the coordinator):
- Fix verification needs evidence: a fixed_in_build decision is verified only on a run whose outcome for the
  checkpoint (or a checkpoint renamed from it) is a comparison outcome (`COMPARED_OUTCOMES`); a `not_run`,
  `env_error` or not-comparable checkpoint proves nothing and is listed in `not_compared`. The signature is
  present when a non-synthetic finding of the run has the decision's region and category and a pHash within the
  decision's tolerance. Only an active decision closes its finding; a reopened one waits for a human, but a return
  is counted again (`reopen_count`), so "two consecutive findings reopened against one decision" reads as two
  reopenings (`SUSPECT_REOPENS`, the 4.14 `reopen_count` comment). A run is verified once: the reopen's outbox job
  (same run, kind and payload hash) is the idempotency key.
- Outbox payloads are canonical JSON; the job carries `sha256:<hex>` of it and the result returns the bytes, which
  the caller stores as the artifact the hash names (8.3 outbox: "the payload is an artifact"). The ticket reopen
  goes to the ticket key; the ticket-not-found notice to destination `triage` (the 9.9 triage channel) naming the
  decider.
- Business days are Monday to Friday in the timestamp's own offset, minus the `holidays` dates given, until the
  W4 calendar provides one; "expire in one business day" keeps the time of day. The new `expires_at` replaces the
  old one only when it is earlier.
- "The signature is absent for 30 days" (agent_mistake, expected_behavior) is measured from the latest of the
  decision's own time and the newest matching non-synthetic finding (rename chain included).
- A plain mark_intended ends when an approved baseline of its checkpoint (rename chain included) was approved at
  or after the decision; "every manifest in scope" is the caller's `manifest_tickets` (None: not checked).
- A request_mask decision linked to a mask ends at the latest `expires_at` of that mask's mirror rows, else at its
  own `expires_at`.
- Re-confirmation applies to an active noise_signature not yet expired; the new row is by the re-confirmer, keeps
  the signature, tolerance, run, build and cluster, has reason `RECONFIRM_PREFIX` plus the old reason and no
  finding of its own (the old row keeps it), and is not a contest or a freeze supersession (store docstring).
- An Explorer finding matched by both dispositions takes the nearest, then newest (the 9.7 rule for one verdict;
  both store `noise`). A disposition holds nothing on a frozen signature (9.7 "later matches inherit nothing") or
  when an applicable decision of another verdict (a confirmed bug, an intended) also matches (9.7 More than one
  match); the finding is then reported, and `conflicting` names the other decisions. An active disposition is an
  active decision for the 9.7 Two-humans refusal (`Store._check_signature_free`). The hold is recorded on the
  finding: `held`, triage state `agent_mistake` or `expected_behavior`, the decision ref and
  `evidence.explorer_disposition` (`candidate_flow`: discard or keep); moving or deleting the flows/quarantine file
  is the Orchestrator's.
- Novelty matches checkpoint (rename chain), region and category and a pHash within the tier's default tolerance
  (6 on tier 1, else 8; 9.7), over non-synthetic findings of other runs created in the 30 days up to the
  finding's own time; the earliest match's `first_seen_run_id` (else its run) is returned.
- The export writes every decision, valid or not, and returns the schema errors per decision: a row written
  without a finding, build or role (store fixtures, imports) cannot satisfy the 4.14 required fields.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from ..canonical import canonical_json, sha256_id
from ..classify import DecisionMatch
from ..records import Decision, Finding, OutboxJob, Signature, now
from .store import (DEFAULT_TOLERANCE, RECONFIRM_PREFIX, TIER1_TOLERANCE, WAIVER_CATEGORIES, Store, StoreRefused,
                    build_reached, candidate_build_of, decision_int, decision_ref, disposition_matches, phash_bytes,
                    rename_chain, utc)

COMPARED_OUTCOMES = ("identical", "noise", "intended", "collateral", "needs_human", "invariant_violation",
                     "assertion_failed")       # the outcomes of a checkpoint that was captured and compared (1.10)
SUSPECT_REOPENS = 2                            # 4.14 reopen_count, 9.5, 9.7: two reopenings make a decision suspect
ABSENCE_DAYS = 30                              # 9.6, 9.7: agent_mistake and expected_behavior end after 30 days absent
NOVELTY_DAYS = 30                              # 9.2: a signature unseen in 30 days is novel
RECONFIRM_DAYS = 30                            # 9.7: one click, another 30 days
EXPIRING_DAYS = 7                              # 9.5: the Decisions page lists what expires within 7 days
DISPOSITIONS = ("agent_mistake", "expected_behavior")
TICKET_NOT_FOUND = "ticket not found"          # 9.9: the triage queue's note


# ---------------------------------------------------------------------------------------------------------------
# results

@dataclass(frozen=True)
class Reopened:
    """One fixed_in_build decision whose signature returned (9.9)."""
    decision_id: str
    findings: tuple[str, ...]                  # this run's findings that carry the signature
    reopen_count: int
    suspect: bool                              # two reopenings (9.5, 9.7)
    job_id: int                                # the jira_transition outbox job that reopens the ticket
    payload: bytes                             # its canonical JSON; the caller stores it as the artifact it names


@dataclass(frozen=True)
class VerifyResult:
    """What `verify_fixes` did for one run (L-DEC-08)."""
    candidate_build: str | None
    closed: tuple[str, ...] = ()               # decisions whose finding moved to closed
    reopened: tuple[Reopened, ...] = ()
    not_compared: tuple[str, ...] = ()         # fix build reached, checkpoint not compared: nothing asserted


@dataclass(frozen=True)
class Expired:
    decision_id: str
    action: str
    why: str


@dataclass(frozen=True)
class ExpiryResult:
    """What `expire_due` moved to `expired` (9.6, 9.7) and what goes to the A/A re-evaluation."""
    expired: tuple[Expired, ...] = ()
    reevaluate: tuple[str, ...] = ()           # noise_signature and request_mask: re-evaluated against the A/A floor


@dataclass(frozen=True)
class TicketNotFound:
    """The 9.9 ticket-not-found transition of one decision."""
    decision_id: str
    expires_at: str
    finding_id: str | None
    job_id: int | None                         # the triage-channel notice; None when it was already queued
    payload: bytes


@dataclass(frozen=True)
class ExplorerDisposition:
    """The active agent_mistake and expected_behavior decisions on an Explorer finding's signature (9.6, 9.7),
    nearest then newest, and what stops them from holding it (9.7)."""
    matches: tuple[DecisionMatch, ...] = ()
    frozen: bool = False                              # two supersessions within 30 days: nothing inherited
    conflicting: tuple[DecisionMatch, ...] = ()       # applicable decisions of another verdict (More than one match)

    @property
    def decision(self) -> Decision | None:
        """The disposition that holds the finding: the nearest, then newest, match unless the signature is frozen
        or another verdict conflicts, in which case nothing is inherited and the finding is reported (9.7)."""
        if not self.matches or self.frozen or self.conflicting:
            return None
        return self.matches[0].decision

    @property
    def held(self) -> bool:
        """A disposition holds the finding: not reported."""
        return self.decision is not None

    @property
    def keep_flow(self) -> bool:
        """expected_behavior keeps the candidate flow for promotion; agent_mistake discards it."""
        d = self.decision
        return d is not None and d.action == "expected_behavior"


@dataclass(frozen=True)
class ExportResult:
    """The files `export_decisions` wrote and the schema errors of each decision that does not validate."""
    paths: tuple[Path, ...] = ()
    invalid: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------
# helpers

def add_business_days(ts: str, days: int = 1, holidays: Iterable[date] = ()) -> str:
    """`ts` moved forward by `days` business days (Monday to Friday, not a holiday), same time of day; UTC text."""
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    off = frozenset(holidays)
    left = days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.date() not in off:
            left -= 1
    return utc(d.isoformat())


def descendants(store: Store, checkpoint_id: str) -> tuple[str, ...]:
    """`checkpoint_id` and every id renamed from it, directly or through a chain (8.3 `renamed_from`)."""
    rows = store.query("WITH RECURSIVE s(id) AS (SELECT ? UNION SELECT c.checkpoint_id FROM checkpoints c "
                       "JOIN s ON c.renamed_from = s.id) SELECT id FROM s", (checkpoint_id,))
    return tuple(r[0] for r in rows)


def _payload(obj: Mapping) -> tuple[bytes, str]:
    return canonical_json(obj).encode("utf-8"), sha256_id(obj)


def _queued(store: Store, run_id: str | None, kind: str, sha: str) -> int | None:
    r = store.query("SELECT job_id FROM outbox WHERE run_id IS ? AND kind = ? AND payload_sha256 = ?",
                    (run_id, kind, sha))
    return r[0][0] if r else None


def _job_row(job: OutboxJob) -> dict:
    j = dataclasses.replace(job)
    values = {k: v for k, v in dataclasses.asdict(j).items() if k != "job_id"}
    values.update(created_at=utc(j.created_at), next_attempt_at=utc(j.next_attempt_at))
    return values


def _matching_findings(store: Store, checkpoints: Iterable[str], region: str, category: str, phash: bytes,
                       tolerance: int, extra: str = "", args: tuple = ()) -> list:
    sql = ("SELECT natural_key, run_id, first_seen_run_id, created_at FROM findings "
           "WHERE checkpoint_id IN (SELECT value FROM json_each(?)) AND region_id = ? AND category = ? "
           "AND synthetic = 0 AND phash IS NOT NULL AND hamming(phash, ?) <= ?" + extra)
    return store.query(sql, (json.dumps(list(checkpoints)), region, category, phash, tolerance, *args))


# ---------------------------------------------------------------------------------------------------------------
# fix verification (L-DEC-08, 9.6, 9.7, 9.9)

def verify_fixes(store: Store, run_id: str, *, candidate_build: str | None = None,
                 at: str | None = None) -> VerifyResult:
    """Assert, after `run_id`, that every fixed signature is gone (L-DEC-08). `candidate_build` defaults to the
    candidate of the run's build pair; with none the store cannot order builds and nothing is verified."""
    if not store.query("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)):
        raise StoreRefused(f"unknown run {run_id}")
    cand = candidate_build or candidate_build_of(store.conn, run_id)
    when = utc(at) or now()
    compared = {r[0] for r in store.query(
        f"SELECT checkpoint_id FROM outcomes WHERE run_id = ? AND synthetic = 0 AND outcome IN "
        f"({','.join('?' * len(COMPARED_OUTCOMES))})", (run_id, *COMPARED_OUTCOMES))}
    rows = store.query("SELECT d.*, f.natural_key AS finding_key, f.triage_state AS finding_state FROM decisions d "
                       "LEFT JOIN findings f ON f.finding_id = d.source_finding_id WHERE d.action = 'fixed_in_build' "
                       "AND d.status IN ('active','reopened') AND d.revoked_at IS NULL ORDER BY d.decision_id")
    closed, reopened, not_compared = [], [], []
    for d in rows:
        if not build_reached(store.conn, cand, d["fixed_in_build_id"]):
            continue
        ref = decision_ref(d["decision_id"])
        cps = [c for c in descendants(store, d["checkpoint_id"]) if c in compared]
        if not cps:
            not_compared.append(ref)
            continue
        present = [r[0] for r in _matching_findings(store, cps, d["region_id"], d["category"], bytes(d["phash"]),
                                                     d["hamming_tolerance"], " AND run_id = ? ORDER BY natural_key",
                                                     (run_id,))]
        if present:
            done = _reopen(store, d, ref, run_id, cand, present, when)
            if done is not None:
                reopened.append(done)
        elif d["status"] == "active" and d["finding_state"] == "fixed_pending_verify":
            with store.batch("fix_verified") as b:
                b.update("findings", {"finding_id": d["source_finding_id"]},
                         {"triage_state": "closed", "closed_at": when})
            closed.append(ref)
    return VerifyResult(cand, tuple(closed), tuple(reopened), tuple(not_compared))


def _reopen(store: Store, d, ref: str, run_id: str, cand: str | None, present: list[str],
            when: str) -> Reopened | None:
    """The signature returned: status reopened, reopen_count + 1, finding open, ticket reopened (9.9). None when
    this run already reopened the decision."""
    data, sha = _payload({"action": "reopen", "decision": ref, "ticket": d["ticket_ref"], "run": run_id,
                          "build": cand, "findings": present})
    if _queued(store, run_id, "jira_transition", sha) is not None:
        return None
    count = d["reopen_count"] + 1
    with store.batch("fix_reopened") as b:
        b.update("decisions", {"decision_id": d["decision_id"]}, {"status": "reopened", "reopen_count": count})
        if d["source_finding_id"] is not None:
            b.update("findings", {"finding_id": d["source_finding_id"]}, {"triage_state": "open", "closed_at": None})
        job = b.insert("outbox", _job_row(OutboxJob("jira_transition", d["ticket_ref"], sha, created_at=when,
                                                    run_id=run_id)))
    return Reopened(ref, tuple(present), count, count >= SUSPECT_REOPENS, job, data)


def suspect_decisions(store: Store) -> tuple[Decision, ...]:
    """Decisions reopened twice or more: suspect on the Decisions page and in the weekly summary (9.5, 9.7)."""
    rows = store.query("SELECT decision_id FROM decisions WHERE reopen_count >= ? ORDER BY decision_id",
                       (SUSPECT_REOPENS,))
    return tuple(store.get_decision(decision_ref(r[0])) for r in rows)


# ---------------------------------------------------------------------------------------------------------------
# expiry (9.6, 9.7 "Ends"), re-confirmation, ticket not found (9.9)

def _effective_expiry(store: Store, d) -> str | None:
    """A linked request_mask ends with its mask (9.6); every other decision at its own `expires_at`."""
    if d["action"] == "request_mask" and d["mask_ref"] is not None:
        r = store.query("SELECT max(expires_at) FROM masks WHERE mask_id = ?", (d["mask_ref"],))
        if r and r[0][0] is not None:
            return r[0][0]
    return d["expires_at"]


def _last_seen(store: Store, d) -> str:
    cps = descendants(store, d["checkpoint_id"])
    rows = _matching_findings(store, cps, d["region_id"], d["category"], bytes(d["phash"]), d["hamming_tolerance"],
                              " ORDER BY created_at DESC LIMIT 1")
    return max(d["decided_at"], rows[0]["created_at"]) if rows else d["decided_at"]


def _why_expired(store: Store, d, when: str, manifest_tickets: frozenset[str] | None) -> str | None:
    expiry = _effective_expiry(store, d)
    if expiry is not None and expiry <= when:
        return "mask expired" if expiry != d["expires_at"] else "expired"
    if d["action"] in DISPOSITIONS:
        limit = utc((datetime.fromisoformat(when) - timedelta(days=ABSENCE_DAYS)).isoformat())
        return f"signature absent {ABSENCE_DAYS} days" if _last_seen(store, d) <= limit else None
    if d["action"] == "mark_intended" and d["category"] not in WAIVER_CATEGORIES:
        cps = json.dumps(list(rename_chain(store.conn, d["checkpoint_id"])))
        if store.query("SELECT 1 FROM baselines WHERE checkpoint_id IN (SELECT value FROM json_each(?)) AND state = "
                       "'approved' AND approved_at >= ?", (cps, d["decided_at"])):
            return "baseline approved"
        if manifest_tickets is not None and d["ticket_ref"] not in manifest_tickets:
            return "ticket left every manifest in scope"
    return None


def expire_due(store: Store, at: str | None = None, *, manifest_tickets: Iterable[str] | None = None
               ) -> ExpiryResult:
    """Move every active decision whose end condition holds at `at` to `expired` (9.6, 9.7), in one batch.
    `manifest_tickets` are the tickets of every manifest in scope (a plain mark_intended whose ticket is in none of
    them ends); None skips that condition. Idempotent."""
    when = utc(at) or now()
    tickets = None if manifest_tickets is None else frozenset(manifest_tickets)
    rows = store.query("SELECT * FROM decisions WHERE status = 'active' AND revoked_at IS NULL ORDER BY decision_id")
    expired = [Expired(decision_ref(d["decision_id"]), d["action"], why) for d in rows
               if (why := _why_expired(store, d, when, tickets)) is not None]
    if expired:
        with store.batch("decision_expiry") as b:
            for e in expired:
                b.update("decisions", {"decision_id": decision_int(e.decision_id)}, {"status": "expired"})
    return ExpiryResult(tuple(expired), tuple(e.decision_id for e in expired
                                              if e.action in ("noise_signature", "request_mask")))


def expiring_within(store: Store, at: str | None = None, days: int = EXPIRING_DAYS) -> tuple[Decision, ...]:
    """Active decisions that end within `days` of `at`: the Decisions page's "expiring in 7 days" (9.5)."""
    when = utc(at) or now()
    until = utc((datetime.fromisoformat(when) + timedelta(days=days)).isoformat())
    rows = store.query("SELECT * FROM decisions WHERE status = 'active' AND revoked_at IS NULL ORDER BY decision_id")
    return tuple(store.get_decision(decision_ref(d["decision_id"])) for d in rows
                 if (x := _effective_expiry(store, d)) is not None and when < x <= until)


def reconfirm(store: Store, decision_id: str, *, by: str, at: str | None = None, role: str | None = None
              ) -> Decision:
    """Re-confirm an expiring noise signature with one click (9.5, 9.7): a new row by `by` superseding it, which
    expires 30 days after the old row would have. Refused for anything but an active, unexpired noise_signature;
    the store's write rules (roster, frozen signature) apply."""
    when = utc(at) or now()
    rows = store.query("SELECT * FROM decisions WHERE decision_id = ?", (decision_int(decision_id),))
    if not rows:
        raise StoreRefused(f"unknown decision {decision_id}")
    old = rows[0]
    if old["action"] != "noise_signature" or old["status"] != "active" or old["expires_at"] <= when:
        raise StoreRefused(f"{decision_id} is not an active, unexpired noise_signature; only those are re-confirmed "
                           "(9.7); an expired one is re-evaluated against the A/A floor")
    record = store.get_decision(decision_id)
    nxt = store.query("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions")[0][0]
    expires = utc((datetime.fromisoformat(old["expires_at"]) + timedelta(days=RECONFIRM_DAYS)).isoformat())
    new = dataclasses.replace(record, decision_id=decision_ref(nxt), decided_by=by, decided_at=when,
                              reason=RECONFIRM_PREFIX + old["reason"], expires_at=expires, supersedes=decision_id,
                              finding_ref=None, confirmed_by=None, role=role, status="active", reopen_count=0,
                              build_pair_id=None, manifest_entry_ref=None)
    return store.record_decision(new, run_id=old["run_id"], build_id=old["build_id"], cluster_id=old["cluster_id"])


def ticket_not_found(store: Store, decision_id: str, *, at: str | None = None,
                     holidays: Iterable[date] = ()) -> TicketNotFound:
    """Jira says the decision's ticket key does not exist (9.9): the decision expires in one business day unless
    superseded with a valid key, its finding returns to the triage queue with "ticket not found", and the decider
    is notified in the triage channel."""
    when = utc(at) or now()
    rows = store.query("SELECT d.*, f.natural_key AS finding_key, f.evidence_json AS evidence FROM decisions d "
                       "LEFT JOIN findings f ON f.finding_id = d.source_finding_id WHERE d.decision_id = ?",
                       (decision_int(decision_id),))
    if not rows:
        raise StoreRefused(f"unknown decision {decision_id}")
    d = rows[0]
    if d["status"] != "active" or d["ticket_ref"] is None:
        raise StoreRefused(f"{decision_id} is not an active decision with a ticket (9.9)")
    expires = add_business_days(when, 1, holidays)
    if d["expires_at"] is not None and d["expires_at"] <= expires:
        expires = d["expires_at"]                                     # shortened, never extended
    data, sha = _payload({"notice": "ticket_not_found", "decision": decision_id, "ticket": d["ticket_ref"],
                          "notify": d["decided_by"], "finding": d["finding_key"], "expires_at": expires})
    queued = _queued(store, d["run_id"], "slack", sha) is not None
    job = None
    with store.batch("ticket_not_found") as b:
        if expires != d["expires_at"]:
            b.update("decisions", {"decision_id": d["decision_id"]}, {"expires_at": expires})
        if d["source_finding_id"] is not None:
            evidence = {**json.loads(d["evidence"]), "triage_note": f"{TICKET_NOT_FOUND}: {d['ticket_ref']}"}
            b.update("findings", {"finding_id": d["source_finding_id"]},
                     {"triage_state": "open", "closed_at": None, "evidence_json": json.dumps(evidence, sort_keys=True)})
        if not queued:
            job = b.insert("outbox", _job_row(OutboxJob("slack", "triage", sha, created_at=when, run_id=d["run_id"])))
    return TicketNotFound(decision_id, expires, d["finding_key"], job, data)


# ---------------------------------------------------------------------------------------------------------------
# Explorer dispositions (9.6, 9.7)

def lookup_explorer_dispositions(store: Store, signature: Signature, *, at: str | None = None,
                                 run_id: str | None = None, manifest_entries: Iterable[str] = (),
                                 manifest_tickets: Iterable[str] = ()) -> ExplorerDisposition:
    """The active agent_mistake and expected_behavior decisions within tolerance of an Explorer finding's
    signature (checkpoint along its rename chain, region, category), nearest then newest (9.6, 9.7), with the 9.7
    freeze of the signature and the applicable decisions of another verdict that conflict with them (the 8.3
    lookup; `run_id` and the manifests give its mark_intended scope as in `Store.lookup_decisions`)."""
    matches = disposition_matches(store.conn, signature, at=at)
    if not matches:
        return ExplorerDisposition()
    look = store.lookup_decisions(signature, at=at, run_id=run_id, manifest_entries=manifest_entries,
                                  manifest_tickets=manifest_tickets)
    verdicts = {m.decision.verdict for m in matches}
    return ExplorerDisposition(matches, frozen=look.frozen,
                               conflicting=tuple(m for m in look.applicable if m.decision.verdict not in verdicts))


def hold_explorer_finding(finding: Finding, disposition: ExplorerDisposition) -> Finding:
    """The Explorer finding as written under a disposition (9.6): held (not reported), the decision's triage state
    and ref, and `evidence.explorer_disposition` saying whether the candidate flow is discarded (agent_mistake) or
    kept for promotion (expected_behavior). Unchanged without a disposition."""
    d = disposition.decision
    if d is None:
        return finding
    evidence = {**finding.evidence, "explorer_disposition": {
        "decision": d.decision_id, "candidate_flow": "keep" if disposition.keep_flow else "discard"}}
    return dataclasses.replace(finding, held=True, triage_state=d.action, decision_ref=d.decision_id,
                               evidence=evidence)


# ---------------------------------------------------------------------------------------------------------------
# novelty (9.2)

def first_seen(store: Store, finding: Finding, *, window_days: int = NOVELTY_DAYS) -> str:
    """The run that first showed `finding`'s signature within `window_days` before it (9.2 novelty): the earliest
    matching finding's first-seen run, or the finding's own run when the signature is new. A finding without a full
    signature is its own first sighting."""
    if not (finding.checkpoint_id and finding.region_id and finding.phash and finding.category):
        return finding.run_id
    tier = finding.tier
    if tier is None:
        r = store.query("SELECT tier FROM checkpoints WHERE checkpoint_id = ?", (finding.checkpoint_id,))
        tier = r[0][0] if r else None
    tolerance = TIER1_TOLERANCE if tier == 1 else DEFAULT_TOLERANCE
    created = utc(finding.created_at)
    since = utc((datetime.fromisoformat(created) - timedelta(days=window_days)).isoformat())
    rows = _matching_findings(store, rename_chain(store.conn, finding.checkpoint_id), finding.region_id,
                              finding.category, phash_bytes(finding.phash), tolerance,
                              " AND run_id <> ? AND created_at >= ? AND created_at <= ? "
                              "ORDER BY created_at, finding_id LIMIT 1", (finding.run_id, since, created))
    if not rows:
        return finding.run_id
    return rows[0]["first_seen_run_id"] or rows[0]["run_id"]


# ---------------------------------------------------------------------------------------------------------------
# the nightly export (4.14 intro)

def _export_doc(r) -> dict:
    """One decision in the 4.14 record shape; absent optional fields are left out."""
    signature = {"checkpoint_id": r["checkpoint_id"], "region_id": r["region_id"], "phash": bytes(r["phash"]).hex(),
                 "hamming_tolerance": r["hamming_tolerance"]}
    if r["bbox_q4"] is not None:
        x, y, w, h = (int(v) for v in r["bbox_q4"].split(","))
        signature["bbox_q4"] = {"x": x, "y": y, "w": w, "h": h}
    signature["category"] = r["category"]
    image = r["baseline_image"]
    doc = {"decision_id": decision_ref(r["decision_id"]), "signature": signature, "action": r["action"],
           "verdict": r["verdict"], "decided_by": r["decided_by"], "role": r["role"], "source": r["source"],
           "confirmed_by": r["confirmed_by"], "decided_at": r["decided_at"], "run_id": r["run_id"],
           "build_id": r["build_id"], "finding_ref": r["finding_key"], "ticket": r["ticket_ref"],
           "reason": r["reason"], "mask_ref": r["mask_ref"],
           "baseline_ref": None if image is None else (image if image.startswith("sha256:") else "sha256:" + image),
           "fixed_in_build": r["fixed_in_build_id"], "expires_at": r["expires_at"], "cluster_id": r["cluster_id"],
           "status": r["status"], "reopen_count": r["reopen_count"],
           "supersedes": decision_ref(r["supersedes_decision_id"]),
           "revoked": None if r["revoked_at"] is None else {"by": r["revoked_by"], "at": r["revoked_at"],
                                                            "reason": r["revoke_reason"]}}
    return {k: v for k, v in doc.items() if v is not None}


def export_decisions(store: Store, directory: str | Path) -> ExportResult:
    """Write every decision to `<directory>/<decision_id>.yaml` in the 4.14 shape (the nightly export to
    `decisions/` in git, for review; SQLite stays the source of truth) and validate each against
    schemas/decision.schema.json."""
    from ..lint.schemas import validate

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    rows = store.query("SELECT d.*, f.natural_key AS finding_key, b.image_sha256 AS baseline_image FROM decisions d "
                       "LEFT JOIN findings f ON f.finding_id = d.source_finding_id "
                       "LEFT JOIN baselines b ON b.baseline_id = d.baseline_id ORDER BY d.decision_id")
    paths, invalid = [], {}
    for r in rows:
        doc = _export_doc(r)
        path = out / f"{doc['decision_id']}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        paths.append(path)
        errors = validate("decision", doc)
        if errors:
            invalid[doc["decision_id"]] = tuple(f"{loc or '<root>'}: {msg}" for loc, msg in errors)
    return ExportResult(tuple(paths), invalid)
