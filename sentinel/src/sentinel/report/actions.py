"""The Reporter service API behind the one-click actions (9.6, 9.5, 8.7): who may do what, the two-person rule, and
what each action writes.

The 9.6 "who may perform" table is data (`RULES`): one `Rule` per row, plus the 9.5 page actions the table does not
name (confirm or decline a proposal, re-confirm a noise signature, assign, close an abandoned outbox job).
`authorize(action, actor, target)` reads it and returns a `Grant` or raises `ActionRefused` naming the clause.
`apply(action, store, ctx, request)` authorizes, then writes through the Central Store's single writer (8.2): the
decision row (`Store.record_decision`, whose 8.3 and L-DEC rules apply again), the finding notes and the outbox jobs
of the 9.6 "Writes" column. A refused write names its clause (9.6: "a refused write names the clause").

Roles (9.12) come from ops/oncall.yaml through `RoleDirectory.from_oncall`: its `Roster` (the L-DEC-01 and L-DEC-06
reading the store applies) plus the role holders, the flow owners' named persons and both rotations. The PR author
of a run is any committer the PR host reported (`runs.pr_author`, `runs.pr_committers_json`; 9.6 two-person rule).

Proposals (9.6 two-person rule, L-DEC-06): `propose`, `confirm` and `decline` keep the click in the 8.3 `proposals`
table and on the finding (`evidence_json.proposal`: action, proposer, time, ticket, reason, state), so the triage
queue shows it with its 4-business-hour clock (9.12). A confirm writes the decision with the confirmer as
`decided_by` and `confirmed_by` and the proposer in `proposed_by` and in the reason; a decline returns the finding to
Unexpected with the reason as its triage note.

Config pull requests go through a `ConfigRepo` (the sentinel-config host: read a pull request, merge, close, open).
Request mask, quarantine and the manifest amendment are outbox jobs (8.3 kinds `mask_pr`, `quarantine_pr`,
`manifest_pr`) that the outbox worker turns into pull requests; Approve mask merges through the repo directly, and so
do Promote candidate flow and Accept anchor patch, which have no outbox kind.

Choices where the spec is ambiguous or silent (reported to the coordinator):
- Coordinator decision (9.6, L-DEC-01, L-MSK-01): `request_mask` is a request anyone with report UI access may
  make; the L-DEC-01 roster governs who decides or approves it. A request by a decider (`Roster.deciders()`) is the
  request_mask decision row at once; a request by anyone else passes the same L-DEC-04 refusals (money cell, panel
  budget) and is stored as a proposal, which becomes the decision row (and the mask PR job) when the triage
  rotation or the tier's flow owner confirms it. The mask PR's requester for clause (b) is the proposer when there
  was one, else the decider; neither may approve the mask.
- "Any role in 9.12" is any holder of a role in ops/oncall.yaml (roles, flow owners, rotations, the triage roster)
  or a committer of the finding's run. A decision row also needs L-DEC-01 (`decided_by` a decider, or the PR author
  for confirm_bug and tier-3 mark_intended, or the Jira resolver), which `authorize` checks first so the refusal
  names it. The triage roster acts in the `triage_rotation` role, as `Roster.confirmers` reads it (L-DEC-06).
- The role stored on a decision row is the first of the actor's roles the rule accepts, preferring
  triage_rotation, flow_owner, pr_author and release_manager over system_owner, since the store lets only its
  configured system owners claim that role (9.7). A frozen signature's system-owner decision is the store's.
- A tier-1 waiver (clause d) needs the tier-1 flow owner and the system owner. One person holding both roles
  completes it alone; otherwise the first click is a proposal and the holder of the other role confirms it. The
  decision row carries the flow owner as `decided_by` and the system owner as `confirmed_by`.
- Clause (c) applies at every tier: the approver of a baseline is never a committer of the run nor one of the
  `change_authors` the caller names (the merged change's manifest author); the store keeps its own tier-1 check.
- An explorer_candidate finding takes the Explorer disposition row (triage rotation; source `explorer_triage`)
  for confirm_bug, agent_mistake and expected_behavior.
- mark_intended on an `invariant_violation` or `assertion_failed` finding is the waiver row; it gets `expires_at`
  30 days out unless the request gives an earlier one (L-DEC-07). A committer of the run never waives its tier-1 or
  tier-2 finding (clause a; the store's L-DEC-06 refuses the row): another flow owner does.
- "The surface was undeclared" (manifest amendment PR) is a finding with no `manifest_entry` or with category
  `collateral` (3.11: a diff on an undeclared surface).
- confirm_bug with a ticket key links the open ticket (a `jira_comment` job to it); without one it queues
  `jira_create`, and `ticket_landed` sets the key on the decision and finding when the job delivers (9.9).
- "Mask requested, PR {n}" is the finding's `triage_note`, written by `mask_pr_opened` when the outbox worker has
  opened the pull request; Approve mask names the pull request and the repo confirms that it carries the decision.
- Approve mask checks the pull request's lint result and recomputes the panel budget against the head registry
  (`ConfigRepo.head_regions`); a failure closes the pull request, revokes the request with reason
  `mask_budget_exceeded`, notifies the requester and the triage rotation (two `slack` jobs to destination
  `triage`) and restarts the SLA clock: the finding's current assignment is released and a new one starts now, for
  the same assignee or the week's rotation member (`ActionContext.rotation`), with no due time (the SLA job computes
  it). On a tier-1 checkpoint the tier-1 flow owner must be the approver or one of the pull request's reviewers.
- Override check writes `runs.check_override_by` and `check_override_reason` once and queues a `commit_status` job
  forcing `success` for that run; Supersede queues the triage-channel notice to the earlier decider (9.7).
- Outbox payloads are canonical JSON; the job row carries `sha256:<hex>` and `ActionResult.payloads` returns the
  bytes for the caller to store as the artifact the hash names (8.3 outbox), as `store.lifecycle` does. A job with
  the same run, kind and payload is not queued twice, which makes a retried request safe.
- Each action is logged by its writer batch in `results.jsonl` (8.2) with actor, time, finding and run; a decision
  row also stores the role.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol

from ..canonical import canonical_json, sha256_id
from ..records import ACTION_VERDICT, DECIDER_ROLES, Decision, OutboxJob, Signature, now
from ..store.lifecycle import reconfirm
from ..store.mirror import MaskBudgetExceeded, link_mask, request_mask_check
from ..store.store import (SERVICE_IDENTITIES, WAIVER_CATEGORIES, Roster, Store, StoreRefused, decision_int,
                           decision_ref, utc)

WAIVER_DAYS = 30                    # L-DEC-07, 9.6: a waiver expires at most 30 days out
MASK_EXPIRY_DAYS = 30               # 9.6: the prefilled mask record expires 30 days out by default
CONFIG_REPO = "sentinel-config"     # destination of the config pull request jobs (8.5)
TRIAGE_CHANNEL = "triage"           # destination of triage-channel notices (9.9, as store.lifecycle)
MASK_BUDGET_EXCEEDED = "mask_budget_exceeded"   # 9.6 Approve mask: the revoke reason
EXPLORER_KIND = "explorer_candidate"
EXPLORER_ACTIONS = ("confirm_bug", "agent_mistake", "expected_behavior")

# role tokens of the table
ANY_ROLE = "any_role"               # any role in 9.12, the PR author included
ANYONE = "anyone"                   # anyone with report UI access (a human login)
TRIAGE = "triage_rotation"
FLOW_OWNER = "flow_owner"           # the flow owner of the finding's tier
TIER1_FLOW_OWNER = "tier1_flow_owner"
SYSTEM_OWNER = "system_owner"
RELEASE_MANAGER = "release_manager"
PR_AUTHOR = "pr_author"             # a committer on the finding's PR (as the PR host reports it)
JIRA_POLLER = "jira_poller"         # the Jira poller writing the resolver's login (9.9)
ROLE_TOKENS = (ANY_ROLE, ANYONE, TRIAGE, FLOW_OWNER, TIER1_FLOW_OWNER, SYSTEM_OWNER, RELEASE_MANAGER, PR_AUTHOR,
               JIRA_POLLER)
# the decisions.role a token is stored as (4.14 role enum)
STORED_ROLE = {TRIAGE: "triage_rotation", FLOW_OWNER: "flow_owner", TIER1_FLOW_OWNER: "flow_owner",
               SYSTEM_OWNER: "system_owner", RELEASE_MANAGER: "release_manager", PR_AUTHOR: "pr_author",
               JIRA_POLLER: "ticket_resolver"}
ROLE_PREFERENCE = ("triage_rotation", "flow_owner", "pr_author", "release_manager", "ticket_resolver",
                   "system_owner")
NEVER_TOKENS = ("requester", "change_author")


# ---------------------------------------------------------------------------------------------------------------
# errors and results

class ActionRefused(StoreRefused):
    """A request the 9.6 table, the two-person rule or an L-DEC rule refuses; `clause` names the rule."""

    def __init__(self, clause: str, message: str):
        self.clause = clause
        super().__init__(f"{clause}: {message}")


@dataclass(frozen=True)
class Rule:
    """One row of the 9.6 "who may perform" table (or a 9.5 page action).

    `who` maps a tier (None: every tier) to the role tokens of which any one suffices; `also` maps a tier to roles
    a second sign-off must hold (clause d, and the tier-1 reviewer of a mask); `never` excludes the requester of the
    mask or the author of the change; `propose` lists the tiers on which a PR committer's click is a proposal
    (clause a); `target` is what the action acts on."""
    action: str
    row: str
    who: Mapping[int | None, tuple[str, ...]]
    target: str = "finding"                       # finding | decision | run | proposal | job | pull
    decision: str | None = None                   # the 4.14 decision action written, when one is
    also: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    never: tuple[str, ...] = ()
    propose: tuple[int, ...] = ()
    money_cell: bool = False                      # refused on a money_cell region (L-DEC-04)

    def roles_for(self, tier: int | None) -> tuple[str, ...]:
        return self.who.get(tier, self.who.get(None, ()))

    @property
    def clause(self) -> str:
        return f"9.6 {self.row}"


def _r(action: str, row: str, who: Mapping[int | None, tuple[str, ...]], **kw: Any) -> Rule:
    return Rule(action, row, who, **kw)


# The 9.6 table ("Who may perform each action"), then the 9.5 page actions it does not name.
RULES: Mapping[str, Rule] = {r.action: r for r in (
    _r("confirm_bug", "Confirm bug", {None: (ANY_ROLE,)}, decision="confirm_bug"),
    _r("mark_intended", "Mark intended with ticket", {None: (ANY_ROLE,)}, decision="mark_intended", propose=(1, 2)),
    _r("waive", "Waive invariant or assertion", {None: (FLOW_OWNER,)}, decision="mark_intended",
       also={1: (SYSTEM_OWNER,)}),
    _r("request_mask", "Request mask with reason", {None: (ANYONE,)}, decision="request_mask", money_cell=True),
    _r("approve_mask", "Approve mask", {None: (TRIAGE,)}, target="decision", also={1: (TIER1_FLOW_OWNER,)},
       never=("requester",)),
    _r("noise_signature", "Mark noise", {None: (TRIAGE, FLOW_OWNER)}, decision="noise_signature", money_cell=True),
    _r("approve_baseline", "Approve or reject baseline", {1: (TIER1_FLOW_OWNER,), None: (FLOW_OWNER, TRIAGE)},
       decision="approve_baseline", never=("change_author",)),
    _r("reject_baseline", "Approve or reject baseline", {1: (TIER1_FLOW_OWNER,), None: (FLOW_OWNER, TRIAGE)},
       decision="reject_baseline", never=("change_author",)),
    _r("explorer_disposition", "Explorer disposition", {None: (TRIAGE,)}),
    _r("promote_flow", "Promote candidate flow", {None: (FLOW_OWNER,)}),
    _r("accept_anchor_patch", "Accept anchor patch", {None: (FLOW_OWNER, TRIAGE)}),
    _r("fixed_in_build", "Record fixed in build", {None: (ANY_ROLE, JIRA_POLLER)}, decision="fixed_in_build"),
    _r("revoke", "Revoke decision", {None: (TRIAGE, FLOW_OWNER, SYSTEM_OWNER)}, target="decision"),
    _r("supersede", "Supersede decision", {None: (TRIAGE, FLOW_OWNER, SYSTEM_OWNER)}, target="decision"),
    _r("quarantine", "Quarantine or un-quarantine", {None: (FLOW_OWNER, SYSTEM_OWNER)}),
    _r("unquarantine", "Quarantine or un-quarantine", {None: (FLOW_OWNER, SYSTEM_OWNER)}),
    _r("override_check", "Override check", {None: (SYSTEM_OWNER,)}, target="run"),
    # 9.5 page actions
    _r("confirm_proposal", "Two-person rule (confirm a proposal)", {None: (TRIAGE, FLOW_OWNER)}, target="proposal"),
    _r("decline_proposal", "Two-person rule (decline a proposal)", {None: (TRIAGE, FLOW_OWNER)}, target="proposal"),
    _r("reconfirm_noise", "Mark noise (re-confirm an expiring noise signature, 9.7)", {None: (TRIAGE, FLOW_OWNER)},
       target="decision"),
    _r("assign", "Triage queue assign (9.5, 9.12)", {None: (ANY_ROLE,)}),
    _r("close_outbox_job", "Close an abandoned outbox job (9.1, 9.5)", {None: (ANY_ROLE,)}, target="job"),
)}
# explorer_candidate findings: these decision actions take the Explorer disposition row (9.6)
DISPOSITION_RULE = "explorer_disposition"


# ---------------------------------------------------------------------------------------------------------------
# roles

@dataclass(frozen=True)
class RoleDirectory:
    """ops/oncall.yaml as 9.6 reads it: the `Roster` of the decision rules, every role holder (person and deputy),
    each tier's named flow owner and the two weekly rotations (9.12, 11.6)."""
    roster: Roster = field(default_factory=Roster)
    holders: Mapping[str, frozenset[str]] = field(default_factory=dict)      # oncall role -> person and deputy
    owner_of_tier: Mapping[int, str] = field(default_factory=dict)           # tier -> named flow owner (person)
    rotations: Mapping[str, frozenset[str]] = field(default_factory=dict)    # triage / harness_oncall -> persons

    @classmethod
    def from_oncall(cls, doc: Mapping[str, Any]) -> "RoleDirectory":
        roster = Roster.from_oncall(doc)
        rotations: dict[str, frozenset[str]] = {}
        for r in doc.get("rotations") or ():
            rotations[str(r["rotation"])] = rotations.get(str(r["rotation"]), frozenset()) | {str(r["person"])}
        owners = {int(e["tier"]): str(e["person"]) for e in doc.get("flow_owners") or () if e.get("person")}
        return cls(roster, dict(roster.roles), owners, rotations)

    @property
    def system_owners(self) -> frozenset[str]:
        """The logins to pass to `Store(system_owners=)` so a system owner's decision may store that role (9.7)."""
        return self.holders.get(SYSTEM_OWNER, frozenset())

    def any_role(self, login: str) -> bool:
        """`login` holds some role of 9.12 in ops/oncall.yaml."""
        groups = (self.roster.triage, *self.roster.flow_owners.values(), *self.holders.values(),
                  *self.rotations.values())
        return any(login in g for g in groups)

    def tokens(self, login: str, tier: int | None, authors: frozenset[str]) -> frozenset[str]:
        """The role tokens `login` holds for a finding of `tier` on a run with these PR committers."""
        out = {ANYONE}
        if login in self.roster.triage:
            out.add(TRIAGE)
        if tier is not None and login in self.roster.flow_owners.get(tier, frozenset()):
            out.add(FLOW_OWNER)
        if login in self.roster.flow_owners.get(1, frozenset()):
            out.add(TIER1_FLOW_OWNER)
        for role in (SYSTEM_OWNER, RELEASE_MANAGER):
            if login in self.holders.get(role, frozenset()):
                out.add(role)
        if login in authors:
            out.add(PR_AUTHOR)
        if self.any_role(login) or login in authors:
            out.add(ANY_ROLE)
        return frozenset(out)


@dataclass(frozen=True)
class Actor:
    """The SSO identity acting (9.1, 8.7), with the directory its roles come from."""
    login: str
    directory: RoleDirectory
    source: str = "report_ui"                     # 4.14 decision source: report_ui | pr_comment | jira_sync | ...

    def __post_init__(self):
        if self.login is None or self.login.strip().lower() in SERVICE_IDENTITIES:
            raise ActionRefused("L-DEC-01", f"{self.login!r} is not a person; actions are human-only (4.14, 9.6)")


# ---------------------------------------------------------------------------------------------------------------
# what an action acts on

@dataclass(frozen=True)
class Target:
    """What `authorize` needs to know of the item: its run, tier, kind, signature and the run's PR committers."""
    run_id: str | None
    run_type: str | None = None
    tier: int | None = None
    finding_id: str | None = None                 # the natural key
    finding_rowid: int | None = None
    kind: str | None = None
    category: str | None = None
    checkpoint_id: str | None = None
    region_id: str | None = None
    phash: str | None = None
    cluster_id: str | None = None
    manifest_entry: str | None = None
    ticket: str | None = None
    authors: frozenset[str] = frozenset()
    money_cell: bool = False
    synthetic: bool = False

    @property
    def waiver(self) -> bool:
        return self.category in WAIVER_CATEGORIES

    @property
    def explorer(self) -> bool:
        return self.kind == EXPLORER_KIND

    def signature(self) -> Signature:
        if not (self.checkpoint_id and self.region_id and self.phash and self.category):
            raise ActionRefused("9.7", f"finding {self.finding_id} has no diff signature (checkpoint, region, pHash, "
                                       "category); decide it on its checkpoint page")
        return Signature(self.checkpoint_id, self.region_id, self.phash, self.category)


def run_authors(store: Store, run_id: str | None) -> frozenset[str]:
    """The PR author and every committer the PR host reported for the run (9.6 two-person rule)."""
    if run_id is None:
        return frozenset()
    rows = store.query("SELECT pr_author, pr_committers_json FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        return frozenset()
    out = set(json.loads(rows[0]["pr_committers_json"])) if rows[0]["pr_committers_json"] else set()
    if rows[0]["pr_author"]:
        out.add(rows[0]["pr_author"])
    return frozenset(out)


def finding_target(store: Store, finding_id: str) -> Target:
    """The `Target` of a finding (by natural key), with its checkpoint's tier (9.2: the tier of the checkpoint's
    surface), its run's committers and whether its region is a money cell at the run's config commit."""
    rows = store.query(
        "SELECT f.*, r.run_type, c.tier AS cp_tier, EXISTS (SELECT 1 FROM regions g WHERE g.config_commit = "
        "r.config_commit AND g.checkpoint_id = f.checkpoint_id AND g.region_id = f.region_id AND g.money_cell = 1) "
        "AS money FROM findings f JOIN runs r ON r.run_id = f.run_id LEFT JOIN checkpoints c ON c.checkpoint_id = "
        "f.checkpoint_id WHERE f.natural_key = ?", (finding_id,))
    if not rows:
        raise ActionRefused("9.6", f"unknown finding {finding_id}")
    f = rows[0]
    return Target(run_id=f["run_id"], run_type=f["run_type"], tier=f["cp_tier"] or f["tier"], finding_id=finding_id,
                  finding_rowid=f["finding_id"], kind=f["kind"], category=f["category"],
                  checkpoint_id=f["checkpoint_id"], region_id=f["region_id"],
                  phash=bytes(f["phash"]).hex() if f["phash"] is not None else None, cluster_id=f["cluster_id"],
                  manifest_entry=f["manifest_entry"], ticket=f["ticket_ref"],
                  authors=run_authors(store, f["run_id"]), money_cell=bool(f["money"]), synthetic=bool(f["synthetic"]))


def decision_target(store: Store, decision_id: str) -> tuple[Target, Any]:
    """The `Target` of a decision (its checkpoint's tier, its run's committers) and its row."""
    rows = store.query("SELECT d.*, c.tier AS cp_tier, r.run_type, f.natural_key AS finding_key FROM decisions d "
                       "JOIN checkpoints c ON c.checkpoint_id = d.checkpoint_id LEFT JOIN runs r ON r.run_id = d.run_id "
                       "LEFT JOIN findings f ON f.finding_id = d.source_finding_id WHERE d.decision_id = ?",
                       (decision_int(decision_id),))
    if not rows:
        raise ActionRefused("9.6", f"unknown decision {decision_id}")
    d = rows[0]
    t = Target(run_id=d["run_id"], run_type=d["run_type"], tier=d["cp_tier"], finding_id=d["finding_key"],
               finding_rowid=d["source_finding_id"], category=d["category"], checkpoint_id=d["checkpoint_id"],
               region_id=d["region_id"], phash=bytes(d["phash"]).hex(), cluster_id=d["cluster_id"],
               ticket=d["ticket_ref"], authors=run_authors(store, d["run_id"]))
    return t, d


def run_target(store: Store, run_id: str) -> Target:
    rows = store.query("SELECT run_type FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        raise ActionRefused("9.6", f"unknown run {run_id}")
    return Target(run_id=run_id, run_type=rows[0]["run_type"], authors=run_authors(store, run_id))


# ---------------------------------------------------------------------------------------------------------------
# authorization

@dataclass(frozen=True)
class Grant:
    """What `authorize` allows: the rule, the role the actor acts in (a 4.14 role, or None), and whether the click is
    a proposal waiting for a second person (`needs`: the role tokens the second person must hold)."""
    action: str
    rule: Rule
    login: str
    role: str | None
    proposal: bool = False
    needs: tuple[str, ...] = ()


def rule_for(action: str, target: Target | None = None) -> Rule:
    """The table row of `action` on `target`: mark_intended on an invariant or assertion is the waiver row and a
    disposition of an Explorer finding the Explorer disposition row (9.6)."""
    if action not in RULES and action not in ("agent_mistake", "expected_behavior"):
        raise ActionRefused("9.6", f"unknown action {action!r}")
    if target is not None and target.explorer and action in EXPLORER_ACTIONS:
        return RULES[DISPOSITION_RULE]
    if action in ("agent_mistake", "expected_behavior"):
        raise ActionRefused("9.6 Explorer disposition", f"{action} applies to explorer_candidate findings only")
    if action == "mark_intended" and target is not None and target.waiver:
        return RULES["waive"]
    return RULES[action]


def _stored_role(tokens: Iterable[str]) -> str | None:
    roles = {STORED_ROLE[t] for t in tokens if t in STORED_ROLE}
    return next((r for r in ROLE_PREFERENCE if r in roles), None)


def _decider(actor: Actor, decision: str, target: Target, tokens: frozenset[str]) -> None:
    """L-DEC-01: a decision row's decider is in the triage roster (as `Roster.deciders` reads it), or the PR author
    for confirm_bug and tier-3 mark_intended, or the Jira resolver for fixed_in_build from jira_sync."""
    if actor.login in actor.directory.roster.deciders():
        return
    if decision == "fixed_in_build" and actor.source == "jira_sync":
        return
    if PR_AUTHOR in tokens and (decision == "confirm_bug" or (decision == "mark_intended" and target.tier == 3)):
        return
    raise ActionRefused("L-DEC-01", f"{actor.login} is not in the triage roster of ops/oncall.yaml, and only the PR "
                                    "author (confirm_bug, tier-3 mark_intended) or a Jira resolver (fixed_in_build) "
                                    "may decide from outside it")


def authorize(action: str, actor: Actor, target: Target, *, requester: Iterable[str] = (),
              change_authors: Iterable[str] = ()) -> Grant:
    """Check `actor` against the 9.6 row of `action` on `target` and the two-person rule; return the `Grant` or raise
    `ActionRefused` naming the clause. `requester` are the logins that requested the mask (clause b) and
    `change_authors` the authors of the merged change a baseline comes from (clause c)."""
    rule = rule_for(action, target)
    if target.synthetic:
        raise ActionRefused("9.1", "synthetic items appear only on the canary panel and take no actions")
    tokens = actor.directory.tokens(actor.login, target.tier, target.authors)
    if actor.source == "jira_sync":
        tokens = tokens | {JIRA_POLLER}
    if rule.money_cell and target.money_cell:
        raise ActionRefused("L-DEC-04", f"{target.region_id} of {target.checkpoint_id} is a money cell; no "
                                        f"{rule.decision} on a money cell (decide it by confirm_bug or mark_intended)")
    if "requester" in rule.never and actor.login in frozenset(requester):
        raise ActionRefused("9.6 two-person rule (b)", f"{actor.login} requested this mask and may not approve it")
    if "change_author" in rule.never and actor.login in (target.authors | frozenset(change_authors)):
        raise ActionRefused("9.6 two-person rule (c)", f"{actor.login} authored the change and may not approve or "
                                                       "reject its baseline")
    allowed = rule.roles_for(target.tier)
    held = tokens & frozenset(allowed)
    proposal, needs = False, ()
    if rule.action == "request_mask" and actor.login not in actor.directory.roster.deciders():
        proposal, needs = True, (TRIAGE, FLOW_OWNER)            # coordinator decision: anyone requests, L-DEC-01 decides
    elif rule.action == "mark_intended" and target.tier in rule.propose and PR_AUTHOR in tokens:
        proposal, needs = True, (TRIAGE, FLOW_OWNER)            # clause (a): the PR author's click is a proposal
    if not held:
        who = " or ".join(t.replace("_", " ") for t in allowed)
        raise ActionRefused(rule.clause, f"{actor.login} may not {rule.action.replace('_', ' ')} here: it is for "
                                         f"{who}{f' on tier {target.tier}' if target.tier else ''}")
    if rule.action == "waive" and target.tier in (1, 2) and PR_AUTHOR in tokens:
        raise ActionRefused("9.6 two-person rule (a)", f"{actor.login} committed to this PR and may not waive its "
                                                       f"tier-{target.tier} finding; another flow owner waives it")
    second = rule.also.get(target.tier or 0, ())
    if second and not (tokens & frozenset(second)) and rule.action == "waive":
        proposal, needs = True, second                          # clause (d): the other sign-off confirms
    decision = rule.decision or (action if rule.action == DISPOSITION_RULE else None)
    if decision is not None and not proposal:
        _decider(actor, decision, target, tokens)
    role = _stored_role(held) if decision is not None or rule.target == "proposal" else None
    if role is None and decision is not None:
        role = _stored_role(tokens)
    return Grant(action, rule, actor.login, role, proposal, tuple(needs))


# ---------------------------------------------------------------------------------------------------------------
# the config repository and the request

@dataclass(frozen=True)
class PullRequest:
    """A sentinel-config pull request as the Reporter reads it (8.5)."""
    number: int
    kind: str                                     # mask_pr, manifest_pr, quarantine_pr, flow, anchor_patch
    author: str                                   # the human author (the mask's requester)
    state: str = "open"                           # open | merged | closed
    checks: str = "pending"                       # the lint result: passed | failed | pending
    reviewers: tuple[str, ...] = ()
    decision_ref: str | None = None
    mask_id: str | None = None
    checkpoint_id: str | None = None
    mask_rect: Mapping[str, Any] | None = None    # {landmark, rect} of the mask record (4.8)


class ConfigRepo(Protocol):
    """The sentinel-config host (8.5): the Reporter reads, merges, closes and opens pull requests through it."""

    def pull(self, number: int) -> PullRequest | None:
        """The pull request, or None."""

    def merge(self, number: int, *, by: str) -> str:
        """Merge it; returns the new config commit."""

    def close(self, number: int, *, by: str, reason: str) -> None:
        """Close it unmerged with the reason as a comment."""

    def open(self, kind: str, *, title: str, body: str, author: str, files: Mapping[str, str]) -> int:
        """Open a pull request with these files; returns its number."""

    def head_regions(self) -> Mapping[str, Mapping[str, Any]]:
        """The Region Registry at the head commit: checkpoint id -> 4.8 regions document."""


@dataclass(frozen=True)
class ActionContext:
    """Who holds what, and the config the request_mask checks read (the run's registry, redaction list, budgets)."""
    directory: RoleDirectory
    repo: ConfigRepo | None = None
    registry: Callable[[str], Mapping[str, Mapping[str, Any]]] | None = None   # run id -> regions documents
    redaction: Mapping[str, Any] | None = None
    budgets: Mapping[str, float] | None = None
    rotation: str | None = None                   # this week's triage rotation member (9.12)
    clock: Callable[[], str] = now


@dataclass(frozen=True)
class ActionRequest:
    """One click: who, on what, and the fields the action needs (ticket, reason, ...)."""
    login: str
    finding: str | None = None                    # finding natural key
    decision: str | None = None                   # dec-<n>, for decision actions
    proposal: int | None = None
    run: str | None = None                        # override check
    job: int | None = None                        # close an abandoned outbox job
    pr: int | None = None                         # approve mask: the mask pull request
    ticket: str | None = None
    reason: str | None = None
    expires_at: str | None = None
    baseline_id: int | None = None
    fixed_in_build: str | None = None
    build_id: str | None = None
    assignee: str | None = None
    rect: Mapping[str, Any] | None = None         # request_mask on a grid cell: the mask's {landmark, rect}
    files: Mapping[str, str] | None = None        # promote flow / anchor patch: the pull request's files
    change_authors: tuple[str, ...] = ()
    cluster: bool = False                         # apply to the whole cluster (9.5 triage queue)
    source: str = "report_ui"
    at: str | None = None


@dataclass
class ActionResult:
    """What one action wrote: the decision or proposal, the outbox jobs with their payload bytes, notes."""
    action: str
    decisions: list[str] = field(default_factory=list)
    proposal: int | None = None
    jobs: list[int] = field(default_factory=list)
    payloads: dict[str, bytes] = field(default_factory=dict)   # payload sha256 id -> canonical JSON bytes
    pr: int | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["payloads"] = sorted(self.payloads)
        return d


# ---------------------------------------------------------------------------------------------------------------
# small writers

def _ts(ctx: ActionContext, req: ActionRequest) -> str:
    return utc(req.at) if req.at else utc(ctx.clock())


def _next_decision(store: Store) -> str:
    return decision_ref(store.query("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions")[0][0])


def _job(b: Any, store: Store, result: ActionResult, run_id: str | None, kind: str, destination: str,
         payload: Mapping[str, Any], at: str) -> None:
    """Queue an outbox job in batch `b` unless the same (run, kind, payload) is queued already (9.1)."""
    sha = sha256_id(payload)
    result.payloads[sha] = canonical_json(payload).encode("utf-8")
    if store.query("SELECT 1 FROM outbox WHERE run_id IS ? AND kind = ? AND payload_sha256 = ?", (run_id, kind, sha)):
        return
    job = OutboxJob(kind, destination, sha, created_at=at, run_id=run_id)
    values = {k: v for k, v in dataclasses.asdict(job).items() if k != "job_id"}
    result.jobs.append(b.insert("outbox", values))


def _evidence(store: Store, finding_rowid: int) -> dict:
    return json.loads(store.query("SELECT evidence_json FROM findings WHERE finding_id = ?",
                                  (finding_rowid,))[0][0])


def _note(b: Any, store: Store, finding_rowid: int | None, **notes: Any) -> None:
    """Set triage notes (`proposal`, `triage_note`: the store's TRIAGE_EVIDENCE_KEYS) on a finding's evidence."""
    if finding_rowid is None:
        return
    ev = {**_evidence(store, finding_rowid), **notes}
    b.update("findings", {"finding_id": finding_rowid}, {"evidence_json": json.dumps(ev, sort_keys=True)})


def _decision(store: Store, grant: Grant, t: Target, req: ActionRequest, at: str, *, action: str | None = None,
              verdict: str | None = None, reason: str | None = None, decided_by: str | None = None,
              confirmed_by: str | None = None, role: str | None = None, **kw: Any) -> Decision:
    """Write one decision row for the finding through `Store.record_decision`."""
    act = action or grant.rule.decision
    sig = t.signature()
    expires = req.expires_at
    if act == "mark_intended" and t.waiver and expires is None:
        expires = (datetime.fromisoformat(at) + timedelta(days=WAIVER_DAYS)).isoformat()
    d = Decision(_next_decision(store), act, verdict or ACTION_VERDICT[act], sig, decided_by=decided_by or req.login,
                 ticket=req.ticket, reason=reason or req.reason, decided_at=at, finding_ref=t.finding_id,
                 confirmed_by=confirmed_by, expires_at=utc(expires) if expires else None,
                 source="explorer_triage" if t.explorer and grant.rule.action == DISPOSITION_RULE else req.source,
                 role=role if role is not None else grant.role)
    return store.record_decision(d, run_id=t.run_id, build_id=req.build_id, cluster_id=t.cluster_id, **kw)


def _need_reason(req: ActionRequest, clause: str) -> str:
    if not req.reason:
        raise ActionRefused(clause, "a reason is required")
    return req.reason


def _need_ticket(req: ActionRequest, clause: str) -> str:
    if not req.ticket:
        raise ActionRefused("L-DEC-02", f"{clause} needs a ticket")
    return req.ticket


# ---------------------------------------------------------------------------------------------------------------
# the finding actions

def _confirm_bug(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                 res: ActionResult) -> None:
    d = _decision(store, g, t, req, at, action="confirm_bug", reason=req.reason or "confirmed bug")
    res.decisions.append(d.decision_id)
    payload = {"action": "confirm_bug", "decision": d.decision_id, "finding": t.finding_id, "run": t.run_id,
               "checkpoint": t.checkpoint_id, "region": t.region_id, "ticket": req.ticket}
    with store.batch("action:confirm_bug") as b:
        if req.ticket:                                            # link to an open ticket (9.9)
            _job(b, store, res, t.run_id, "jira_comment", req.ticket, payload, at)
        else:
            _job(b, store, res, t.run_id, "jira_create", "jira", payload, at)


def _mark_intended(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                   res: ActionResult, *, proposer: str | None = None, confirmed_by: str | None = None) -> None:
    ticket = _need_ticket(req, g.rule.clause)
    reason = req.reason or "intended"
    if proposer:
        reason = f"proposed by {proposer}: {reason}"
    kw: dict[str, Any] = {}
    if proposer and not t.waiver:
        kw, confirmed_by = {"proposed_by": proposer}, req.login
    d = _decision(store, g, t, req, at, action="mark_intended", reason=reason, confirmed_by=confirmed_by, **kw)
    res.decisions.append(d.decision_id)
    with store.batch("action:mark_intended") as b:
        _job(b, store, res, t.run_id, "jira_comment", ticket,
             {"action": "mark_intended", "decision": d.decision_id, "finding": t.finding_id, "run": t.run_id,
              "evidence": "intended", "ticket": ticket}, at)
        if not t.waiver and (t.manifest_entry is None or t.category == "collateral"):
            surface = t.checkpoint_id.split("/")[1] if t.checkpoint_id and "/" in t.checkpoint_id else None
            _job(b, store, res, t.run_id, "manifest_pr", CONFIG_REPO,
                 {"action": "manifest_amendment", "decision": d.decision_id, "ticket": ticket, "surface": surface,
                  "checkpoint": t.checkpoint_id, "author": req.login}, at)


def _propose(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
             res: ActionResult) -> None:
    """Store the click as a proposal (9.6 two-person rule): the proposals row and `evidence_json.proposal`."""
    action = g.rule.decision or g.action
    if action == "mark_intended":
        _need_ticket(req, g.rule.clause)
    reason = _need_reason(req, g.rule.clause) if action == "request_mask" else (req.reason or action)
    if action == "request_mask":
        _mask_check(store, ctx, t, req)                          # refused at submit, before anything is stored
    t.signature()
    open_ = store.query("SELECT * FROM proposals WHERE finding_id = ? AND state = 'proposed'", (t.finding_rowid,))
    if open_:
        p = open_[0]
        if (p["action"], p["proposed_by"], p["ticket_ref"]) == (action, req.login, req.ticket):
            res.proposal = p["proposal_id"]                       # the same click again (9.6 idempotent)
            return
        raise ActionRefused("9.6 two-person rule", f"finding {t.finding_id} already has proposal {p['proposal_id']} "
                                                   f"({p['action']} by {p['proposed_by']}) awaiting a second person")
    note = {"action": action, "proposer": req.login, "time": at, "ticket": req.ticket, "reason": reason,
            "state": "proposed", "needs": list(g.needs)}
    if req.rect is not None:
        note["rect"] = dict(req.rect)
    with store.batch("action:propose") as b:
        pid = b.insert("proposals", {"finding_id": t.finding_rowid, "run_id": t.run_id, "action": action,
                                     "proposed_by": req.login, "proposed_at": at, "ticket_ref": req.ticket,
                                     "reason": reason, "state": "proposed"})
        _note(b, store, t.finding_rowid, proposal={**note, "proposal_id": pid},
              triage_note=f"proposed {action.replace('_', ' ')} by {req.login}")
    res.proposal = pid


def _mask_check(store: Store, ctx: ActionContext, t: Target, req: ActionRequest) -> Any:
    """L-DEC-04 at submit: refused on a money cell or when the prefilled mask takes its panel over budget."""
    if ctx.registry is None:
        raise ActionRefused("L-DEC-04", "the run's Region Registry is not loaded; a mask request cannot be checked")
    try:
        return request_mask_check(store, t.finding_id, registry=ctx.registry(t.run_id), redaction=ctx.redaction,
                                  rect=req.rect, budgets=ctx.budgets)
    except MaskBudgetExceeded:
        raise
    except StoreRefused as e:
        raise ActionRefused("L-DEC-04", str(e)) from e


def _request_mask(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                  res: ActionResult, *, proposer: str | None = None, rect: Mapping[str, Any] | None = None) -> None:
    reason = _need_reason(req, g.rule.clause)
    check_req = dataclasses.replace(req, rect=rect if rect is not None else req.rect)
    _mask_check(store, ctx, t, check_req)
    text = f"requested by {proposer}: {reason}" if proposer else reason
    kw = {"proposed_by": proposer} if proposer else {}
    d = _decision(store, g, t, req, at, action="request_mask", reason=text, **kw)
    res.decisions.append(d.decision_id)
    made = d.decided_at                                         # the stored row's time: a repeat makes the same job
    expires = (datetime.fromisoformat(made) + timedelta(days=MASK_EXPIRY_DAYS)).isoformat(timespec="seconds")
    regions = ctx.registry(t.run_id).get(t.checkpoint_id) or {} if ctx.registry else {}
    own = next((r for r in regions.get("regions") or () if r.get("id") == t.region_id), {})
    place = check_req.rect or {"landmark": own.get("landmark"), "rect": own.get("rect")}
    record = {"schema_version": 1, "checkpoint_id": t.checkpoint_id, "region_id": t.region_id,
              "owner": ctx.directory.owner_of_tier.get(t.tier or 0), "reason": reason, "created_by": "human",
              "requested_by": proposer or req.login, "created_run": t.run_id, "created_at": made,
              "expires_at": expires, "ticket": req.ticket, "decision_ref": d.decision_id,
              "landmark": place.get("landmark"), "rect": place.get("rect")}
    with store.batch("action:request_mask") as b:
        _job(b, store, res, t.run_id, "mask_pr", CONFIG_REPO, {"action": "request_mask", "mask": record}, at)


def _noise(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
           res: ActionResult) -> None:
    d = _decision(store, g, t, req, at, action="noise_signature", reason=req.reason or "noise")
    res.decisions.append(d.decision_id)


def _baseline(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
              res: ActionResult) -> None:
    if req.baseline_id is None:
        raise ActionRefused(g.rule.clause, "name the baselines row (baseline_id)")
    if g.action == "approve_baseline":
        _need_ticket(req, g.rule.clause)
        verdict = "intended"
    else:
        verdict = "confirmed_bug" if req.ticket else "noise"
    d = _decision(store, g, t, req, at, action=g.action, verdict=verdict,
                  reason=_need_reason(req, g.rule.clause), baseline_id=req.baseline_id,
                  change_authors=tuple(req.change_authors))
    res.decisions.append(d.decision_id)


def _disposition(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                 res: ActionResult, action: str) -> None:
    d = _decision(store, g, t, req, at, action=action, reason=req.reason or action.replace("_", " "))
    res.decisions.append(d.decision_id)
    if action == "confirm_bug":
        with store.batch("action:explorer_bug") as b:
            _job(b, store, res, t.run_id, "jira_create", "jira",
                 {"action": "confirm_bug", "decision": d.decision_id, "finding": t.finding_id, "run": t.run_id}, at)


def _fixed(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
           res: ActionResult) -> None:
    if not req.fixed_in_build or not req.decision:
        raise ActionRefused("SD-79", "fixed_in_build names the fix build and the confirm_bug decision it supersedes")
    _, old = decision_target(store, req.decision)
    ticket = req.ticket or old["ticket_ref"]
    d = Decision(_next_decision(store), "fixed_in_build", "confirmed_bug",
                 Signature(old["checkpoint_id"], old["region_id"], bytes(old["phash"]).hex(), old["category"]),
                 decided_by=req.login, ticket=ticket, reason=req.reason or f"fixed in {req.fixed_in_build}",
                 decided_at=at, source=req.source, role=g.role, supersedes=req.decision)
    stored = store.record_decision(d, run_id=old["run_id"], fixed_in_build_id=req.fixed_in_build,
                                   build_id=req.build_id or old["build_id"])
    res.decisions.append(stored.decision_id)


def _config_pr(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
               res: ActionResult) -> None:
    """Quarantine and un-quarantine: a quarantine record PR job (10.5); Promote candidate flow and Accept anchor
    patch: a pull request opened through the repo (they have no outbox kind)."""
    reason = _need_reason(req, g.rule.clause)
    if g.action in ("quarantine", "unquarantine"):
        with store.batch(f"action:{g.action}") as b:
            _job(b, store, res, t.run_id, "quarantine_pr", CONFIG_REPO,
                 {"action": g.action, "checkpoint": t.checkpoint_id, "finding": t.finding_id, "reason": reason,
                  "owner": req.login, "ticket": req.ticket, "at": at}, at)
        return
    if ctx.repo is None:
        raise ActionRefused(g.rule.clause, "the sentinel-config repository is not reachable; nothing was opened")
    if not req.files:
        raise ActionRefused(g.rule.clause, "the pull request needs its files")
    kind = "flow" if g.action == "promote_flow" else "anchor_patch"
    res.pr = ctx.repo.open(kind, title=f"{g.rule.row}: {t.checkpoint_id or t.finding_id}", author=req.login,
                           body=f"{reason}\n\nfinding {t.finding_id}, run {t.run_id}", files=dict(req.files))


# ---------------------------------------------------------------------------------------------------------------
# decision, proposal, run and job actions

def _revoke(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
            res: ActionResult) -> None:
    store.revoke_decision(req.decision, by=req.login, reason=_need_reason(req, g.rule.clause), at=at)
    res.decisions.append(req.decision)


def _supersede(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
               res: ActionResult, old: Any, action: str) -> None:
    """A new row with `supersedes` (9.7); reason required; the earlier decider is notified in the triage channel."""
    reason = _need_reason(req, g.rule.clause)
    verdict = ACTION_VERDICT.get(action) if action != "reject_baseline" else ("confirmed_bug" if req.ticket else "noise")
    if verdict is None:
        raise ActionRefused(g.rule.clause, f"{action} is not a decision action")
    d = Decision(_next_decision(store), action, verdict, t.signature(), decided_by=req.login, ticket=req.ticket,
                 reason=reason, decided_at=at, finding_ref=t.finding_id, supersedes=req.decision, source=req.source,
                 role=g.role, expires_at=utc(req.expires_at) if req.expires_at else None)
    stored = store.record_decision(d, run_id=t.run_id, build_id=req.build_id, cluster_id=t.cluster_id)
    res.decisions.append(stored.decision_id)
    with store.batch("action:supersede") as b:
        _job(b, store, res, t.run_id, "slack", TRIAGE_CHANNEL,
             {"notice": "superseded", "decision": req.decision, "by": stored.decision_id, "notify": old["decided_by"],
              "superseded_by": req.login, "reason": reason}, at)


def _approve_mask(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                  res: ActionResult, row: Any, pull: PullRequest) -> None:
    """Merge the mask PR after its lint and the per-panel budget check at the head; on a budget failure close it,
    revoke the request with `mask_budget_exceeded`, notify and restart the SLA clock (9.6 Approve mask)."""
    if pull.checks != "passed":
        raise ActionRefused("9.6 Approve mask", f"mask PR {pull.number} has not passed lint ({pull.checks}); it merges "
                                                "only after the lint and the budget check (4.8)")
    if t.tier == 1 and not ({req.login, *pull.reviewers} & ctx.directory.roster.flow_owners.get(1, frozenset())):
        raise ActionRefused("9.6 Approve mask", "a tier-1 panel's mask also needs the tier-1 flow owner as reviewer")
    try:
        request_mask_check(store, t.finding_id, registry=ctx.repo.head_regions(), redaction=ctx.redaction,
                           rect=pull.mask_rect, budgets=ctx.budgets)
    except MaskBudgetExceeded as e:
        ctx.repo.close(pull.number, by=req.login, reason=f"{MASK_BUDGET_EXCEEDED}: {e}")
        store.revoke_decision(req.decision, by=req.login, reason=MASK_BUDGET_EXCEEDED, at=at)
        _budget_failed(store, ctx, t, req, at, res, row, pull, str(e))
        res.note = MASK_BUDGET_EXCEEDED
        return
    ctx.repo.merge(pull.number, by=req.login)
    link_mask(store, req.decision, pull.mask_id)
    res.decisions.append(req.decision)
    res.pr = pull.number


def _budget_failed(store: Store, ctx: ActionContext, t: Target, req: ActionRequest, at: str, res: ActionResult,
                   row: Any, pull: PullRequest, why: str) -> None:
    requester = row["proposed_by"] or row["decided_by"]
    current = store.query("SELECT * FROM triage_assignments WHERE finding_id = ? AND released_at IS NULL",
                          (t.finding_rowid,)) if t.finding_rowid is not None else []
    assignee = current[0]["assignee"] if current else ctx.rotation
    with store.batch("action:mask_budget_exceeded") as b:
        for who in dict.fromkeys(x for x in (requester, ctx.rotation) if x):
            _job(b, store, res, t.run_id, "slack", TRIAGE_CHANNEL,
                 {"notice": MASK_BUDGET_EXCEEDED, "decision": req.decision, "pr": pull.number, "notify": who,
                  "finding": t.finding_id, "detail": why}, at)
        if current:
            b.update("triage_assignments", {"assignment_id": current[0]["assignment_id"]}, {"released_at": at})
        if assignee and t.finding_rowid is not None:          # the SLA clock restarts now (9.6)
            b.insert("triage_assignments", {"finding_id": t.finding_rowid, "assignee": assignee,
                                            "assigned_by": req.login, "assigned_at": at, "escalation_step": 0})
        _note(b, store, t.finding_rowid, triage_note=f"mask PR {pull.number} closed: {MASK_BUDGET_EXCEEDED}")


def mask_pr_opened(store: Store, decision_id: str, pr: int) -> None:
    """The outbox worker opened the mask PR of a request_mask decision: its finding stays in Unexpected marked
    "mask requested, PR {n}" (9.6)."""
    rows = store.query("SELECT action, source_finding_id FROM decisions WHERE decision_id = ?",
                       (decision_int(decision_id),))
    if not rows or rows[0]["action"] != "request_mask":
        raise ActionRefused("9.6 Request mask", f"{decision_id} is not a request_mask decision")
    with store.batch("mask_pr_opened") as b:
        _note(b, store, rows[0]["source_finding_id"], triage_note=f"mask requested, PR {pr}")


def ticket_landed(store: Store, payload: Mapping[str, Any], key: str) -> Decision:
    """A `jira_create` job of confirm_bug delivered: the ticket key goes on the decision and its finding (9.9)."""
    return store.fill_ticket_ref(str(payload["decision"]), key)


def _confirm_proposal(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                      res: ActionResult, p: Any) -> None:
    note = (_evidence(store, t.finding_rowid).get("proposal") or {})
    proposer = p["proposed_by"]
    fake = dataclasses.replace(req, ticket=p["ticket_ref"] or req.ticket, reason=p["reason"])
    if p["action"] == "request_mask":
        rule = RULES["request_mask"]
        grant = Grant("request_mask", rule, req.login, g.role)
        _request_mask(store, ctx, grant, t, fake, at, res, proposer=proposer, rect=note.get("rect"))
    elif p["action"] == "mark_intended" and t.waiver:
        # clause (d): the flow owner decides, the system owner confirms (whichever clicked first)
        owners = ctx.directory.roster.flow_owners.get(t.tier or 0, frozenset())
        owner, other = (req.login, proposer) if req.login in owners else (proposer, req.login)
        grant = Grant("waive", RULES["waive"], owner, "flow_owner")
        _mark_intended(store, ctx, grant, t, dataclasses.replace(fake, login=owner), at, res, confirmed_by=other)
    elif p["action"] == "mark_intended":
        _mark_intended(store, ctx, g, t, fake, at, res, proposer=proposer)
    else:
        raise ActionRefused("9.6 two-person rule", f"proposal {p['proposal_id']} is a {p['action']}; only "
                                                   "mark_intended and request_mask are proposed")
    with store.batch("action:confirm_proposal") as b:
        b.update("proposals", {"proposal_id": p["proposal_id"]},
                 {"state": "confirmed", "decided_by": req.login, "decided_at": at,
                  "decision_id": decision_int(res.decisions[-1])})
        _note(b, store, t.finding_rowid, proposal={**note, "state": "confirmed", "confirmed_by": req.login,
                                                   "decision": res.decisions[-1]})
    res.proposal = p["proposal_id"]


def _decline_proposal(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
                      res: ActionResult, p: Any) -> None:
    reason = _need_reason(req, g.rule.clause)
    note = _evidence(store, t.finding_rowid).get("proposal") or {}
    with store.batch("action:decline_proposal") as b:
        b.update("proposals", {"proposal_id": p["proposal_id"]},
                 {"state": "declined", "decided_by": req.login, "decided_at": at, "decline_reason": reason})
        b.update("findings", {"finding_id": t.finding_rowid}, {"triage_state": "open", "closed_at": None})
        _note(b, store, t.finding_rowid, proposal={**note, "state": "declined", "declined_by": req.login,
                                                   "decline_reason": reason},
              triage_note=f"proposal declined by {req.login}: {reason}")
    res.proposal = p["proposal_id"]


def _override(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
              res: ActionResult) -> None:
    reason = _need_reason(req, g.rule.clause)
    run = store.query("SELECT check_override_by, check_override_reason, pr_ref FROM runs WHERE run_id = ?",
                      (t.run_id,))[0]
    if run["check_override_by"] is not None:
        if (run["check_override_by"], run["check_override_reason"]) == (req.login, reason):
            return                                                 # the same click again (9.6 idempotent)
        raise ActionRefused("9.6 Override check", f"run {t.run_id} was already overridden by "
                                                  f"{run['check_override_by']}")
    with store.batch("action:override_check") as b:
        b.update("runs", {"run_id": t.run_id}, {"check_override_by": req.login, "check_override_reason": reason})
        _job(b, store, res, t.run_id, "commit_status", run["pr_ref"] or t.run_id,
             {"action": "override_check", "run": t.run_id, "state": "success", "by": req.login, "reason": reason},
             at)


def _assign(store: Store, ctx: ActionContext, g: Grant, t: Target, req: ActionRequest, at: str,
            res: ActionResult) -> None:
    who = req.assignee or req.login
    if who.strip().lower() in SERVICE_IDENTITIES:
        raise ActionRefused("9.12", f"{who!r} is not a person")
    current = store.query("SELECT * FROM triage_assignments WHERE finding_id = ? AND released_at IS NULL",
                          (t.finding_rowid,))
    if current and current[0]["assignee"] == who:
        return
    with store.batch("action:assign") as b:
        if current:
            b.update("triage_assignments", {"assignment_id": current[0]["assignment_id"]}, {"released_at": at})
        b.insert("triage_assignments", {"finding_id": t.finding_rowid, "assignee": who, "assigned_by": req.login,
                                        "assigned_at": at, "claimed_at": at if who == req.login else None,
                                        "escalation_step": 0})


# ---------------------------------------------------------------------------------------------------------------
# entry points

def _second_sign_off(store: Store, ctx: ActionContext, action: str, actor: Actor, t: Target) -> Grant:
    """Clause (d): a tier-1 waiver proposal is confirmed or declined by the holder of the sign-off it still needs
    (the system owner, since only the tier's flow owner starts a waiver)."""
    needs = (_evidence(store, t.finding_rowid).get("proposal") or {}).get("needs") or ()
    tokens = ctx.directory.tokens(actor.login, t.tier, t.authors)
    held = tokens & frozenset(needs)
    if not held:
        raise ActionRefused("9.6 two-person rule (d)", f"a tier-{t.tier} waiver needs the "
                                                       f"{' or '.join(needs) or 'system owner'} as the second sign-off")
    return Grant(action, RULES[action], actor.login, _stored_role(held))


def target_of(store: Store, action: str, req: ActionRequest) -> tuple[Target, Any]:
    """The target `action` acts on for this request, and the row it was read from (decision, proposal, job)."""
    base = RULES.get(action)
    kind = base.target if base else "finding"
    if kind == "run":
        return run_target(store, req.run or ""), None
    if kind == "job":
        rows = store.query("SELECT * FROM outbox WHERE job_id = ?", (req.job,))
        if not rows:
            raise ActionRefused("9.1", f"unknown outbox job {req.job}")
        return run_target(store, rows[0]["run_id"]) if rows[0]["run_id"] else Target(None), rows[0]
    if kind == "proposal":
        rows = store.query("SELECT p.*, f.natural_key FROM proposals p JOIN findings f ON f.finding_id = p.finding_id "
                           "WHERE p.proposal_id = ?", (req.proposal,))
        if not rows:
            raise ActionRefused("9.6 two-person rule", f"unknown proposal {req.proposal}")
        return finding_target(store, rows[0]["natural_key"]), rows[0]
    if kind == "decision" or (action == "fixed_in_build" and req.finding is None):
        if not req.decision:
            raise ActionRefused(f"9.6 {base.row if base else action}", "name the decision")
        return decision_target(store, req.decision)
    if not req.finding:
        raise ActionRefused("9.6", f"{action} acts on a finding; name it")
    return finding_target(store, req.finding), None


def apply(action: str, store: Store, ctx: ActionContext, req: ActionRequest) -> ActionResult:
    """Authorize `req` against the 9.6 table and perform `action` through the store's writer (9.6 Writes column).

    Raises `ActionRefused` (the clause), `MaskBudgetExceeded` (L-DEC-04 with the share, budget and the mask's
    share), `ActiveDecisionExists` (9.7) or another `StoreRefused` from the store's own constraints; nothing is
    written on a refusal before the first write. With `req.cluster` a finding action applies to every open member
    of the finding's cluster in its run (9.5), each with its own decision row carrying the cluster id (9.7)."""
    t, row = target_of(store, action, req)
    if req.cluster and t.cluster_id and action in ("confirm_bug", "mark_intended", "noise_signature"):
        members = store.query("SELECT natural_key FROM findings WHERE run_id = ? AND cluster_id = ? AND "
                              "triage_state = 'open' AND synthetic = 0 ORDER BY natural_key", (t.run_id, t.cluster_id))
        out = ActionResult(action)
        for m in members:
            one = apply(action, store, ctx, dataclasses.replace(req, finding=m["natural_key"], cluster=False))
            out.decisions += one.decisions
            out.jobs += one.jobs
            out.payloads.update(one.payloads)
        return out
    actor = Actor(req.login, ctx.directory, req.source)
    requester: tuple[str, ...] = ()
    pull: PullRequest | None = None
    if action == "approve_mask":
        if row["action"] != "request_mask" or row["status"] != "active":
            raise ActionRefused("9.6 Approve mask", f"{req.decision} is not an active request_mask decision")
        if ctx.repo is None or req.pr is None:
            raise ActionRefused("9.6 Approve mask", "name the mask pull request; the config repository must be up")
        pull = ctx.repo.pull(req.pr)
        if pull is None or pull.decision_ref != req.decision or pull.state != "open":
            raise ActionRefused("9.6 Approve mask", f"PR {req.pr} is not the open mask PR of {req.decision}")
        requester = tuple(x for x in (row["proposed_by"], row["decided_by"], pull.author) if x)
    if action in ("confirm_proposal", "decline_proposal"):
        if row["state"] != "proposed":
            raise ActionRefused("9.6 two-person rule", f"proposal {req.proposal} is already {row['state']}")
        if req.login == row["proposed_by"]:
            raise ActionRefused("9.6 two-person rule", "the confirmer is a different human from the proposer")
    if action in ("confirm_proposal", "decline_proposal") and t.waiver and row["action"] == "mark_intended":
        grant = _second_sign_off(store, ctx, action, actor, t)
    else:
        grant = authorize(action, actor, t, requester=requester, change_authors=req.change_authors)
    at = _ts(ctx, req)
    res = ActionResult(action)
    if grant.proposal:
        _propose(store, ctx, grant, t, req, at, res)
        return res
    name = grant.rule.action
    if name == DISPOSITION_RULE:
        _disposition(store, ctx, grant, t, req, at, res, action)
    elif name == "confirm_bug":
        _confirm_bug(store, ctx, grant, t, req, at, res)
    elif name == "mark_intended":
        _mark_intended(store, ctx, grant, t, req, at, res)
    elif name == "waive":
        _mark_intended(store, ctx, grant, t, req, at, res,
                       confirmed_by=req.login if t.tier == 1 else None)
    elif name == "request_mask":
        _request_mask(store, ctx, grant, t, req, at, res)
    elif name == "noise_signature":
        _noise(store, ctx, grant, t, req, at, res)
    elif name in ("approve_baseline", "reject_baseline"):
        _baseline(store, ctx, grant, t, req, at, res)
    elif name == "fixed_in_build":
        _fixed(store, ctx, grant, t, req, at, res)
    elif name in ("quarantine", "unquarantine", "promote_flow", "accept_anchor_patch"):
        _config_pr(store, ctx, grant, t, req, at, res)
    elif name == "revoke":
        _revoke(store, ctx, grant, t, req, at, res)
    elif name == "supersede":
        _supersede(store, ctx, grant, t, req, at, res, row, row["action"])
    elif name == "approve_mask":
        _approve_mask(store, ctx, grant, t, req, at, res, row, pull)
    elif name == "confirm_proposal":
        _confirm_proposal(store, ctx, grant, t, req, at, res, row)
    elif name == "decline_proposal":
        _decline_proposal(store, ctx, grant, t, req, at, res, row)
    elif name == "reconfirm_noise":
        res.decisions.append(reconfirm(store, req.decision, by=req.login, at=at, role=grant.role).decision_id)
    elif name == "override_check":
        _override(store, ctx, grant, t, req, at, res)
    elif name == "assign":
        _assign(store, ctx, grant, t, req, at, res)
    elif name == "close_outbox_job":
        store.close_job(req.job, by=req.login, at=at)
    return res


def propose(store: Store, ctx: ActionContext, req: ActionRequest, action: str = "mark_intended") -> ActionResult:
    """A PR committer's click on a tier-1 or tier-2 finding (or a request_mask from outside the roster): stored as a
    proposal (9.6 two-person rule). `apply` routes there by itself; this entry refuses anything that is not one."""
    t, _ = target_of(store, action, req)
    grant = authorize(action, Actor(req.login, ctx.directory, req.source), t)
    if not grant.proposal:
        raise ActionRefused("9.6 two-person rule", f"{req.login}'s {action} on {t.finding_id} is not a proposal; "
                                                   "apply it directly")
    res = ActionResult(action)
    _propose(store, ctx, grant, t, req, _ts(ctx, req), res)
    return res


def confirm(store: Store, ctx: ActionContext, proposal_id: int, login: str, *, at: str | None = None) -> ActionResult:
    """The triage rotation or the tier's flow owner confirms a proposal: the decision row is written (9.6)."""
    return apply("confirm_proposal", store, ctx, ActionRequest(login, proposal=proposal_id, at=at))


def decline(store: Store, ctx: ActionContext, proposal_id: int, login: str, reason: str, *,
            at: str | None = None) -> ActionResult:
    """A proposal declined: the finding returns to Unexpected with the reason (9.6)."""
    return apply("decline_proposal", store, ctx, ActionRequest(login, proposal=proposal_id, reason=reason, at=at))


def open_proposals(store: Store) -> list[dict]:
    """Proposals awaiting a second person, oldest first, for the triage queue (9.5, 9.12)."""
    rows = store.query("SELECT p.*, f.natural_key, f.tier FROM proposals p JOIN findings f ON f.finding_id = "
                       "p.finding_id WHERE p.state = 'proposed' ORDER BY p.proposed_at, p.proposal_id")
    return [dict(r) for r in rows]


def table() -> list[dict]:
    """The 9.6 table as rows (for the UI's help text and tests)."""
    out = []
    for r in RULES.values():
        out.append({"action": r.action, "row": r.row, "who": {str(k): list(v) for k, v in r.who.items()},
                    "also": {str(k): list(v) for k, v in r.also.items()}, "never": list(r.never),
                    "propose": list(r.propose), "money_cell": r.money_cell, "target": r.target})
    return out


def check_table(rules: Mapping[str, Rule] = RULES) -> list[str]:
    """Problems with the table itself: unknown role tokens, a decision action outside 4.14, unknown never tokens."""
    problems = []
    for r in rules.values():
        for roles in (*r.who.values(), *r.also.values()):
            problems += [f"{r.action}: unknown role {x}" for x in roles if x not in ROLE_TOKENS]
        problems += [f"{r.action}: unknown never {x}" for x in r.never if x not in NEVER_TOKENS]
        if r.decision is not None and r.decision not in ACTION_VERDICT and r.decision != "reject_baseline":
            problems.append(f"{r.action}: decision {r.decision} is not a 4.14 action")
    if not set(STORED_ROLE.values()) <= set(DECIDER_ROLES):
        problems.append("a stored role is not a 4.14 role")
    return problems


__all__ = [
    "ActionContext", "ActionRefused", "ActionRequest", "ActionResult", "Actor", "ConfigRepo", "Grant",
    "PullRequest", "RULES", "RoleDirectory", "Rule", "Target", "apply", "authorize", "check_table", "confirm",
    "decision_target", "decline", "finding_target", "mask_pr_opened", "open_proposals", "propose", "rule_for",
    "run_authors", "run_target", "table", "target_of", "ticket_landed",
]
