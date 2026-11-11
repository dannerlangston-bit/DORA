"""The Reporter service API behind the one-click actions (9.6, 9.5, 9.12): the 9.6 table row by row, the two-person
rule (clauses a to d), proposals, and what each action writes (decision rows, finding notes, outbox jobs, PRs)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

import pytest

from sentinel.records import CheckpointOutcome, Finding, RunOutcome
from sentinel.report.actions import (RULES, ActionContext, ActionRefused, ActionRequest, Actor, PullRequest,
                                     RoleDirectory, apply, authorize, check_table, confirm, decline, finding_target,
                                     mask_pr_opened, open_proposals, propose, rule_for, table, ticket_landed)
from sentinel.store import RunPlan, ScopeEntry, Store, StoreRefused
from sentinel.store.mirror import MaskBudgetExceeded, load_config_mirror

COMMIT = "c0ffee"
PR_RUN, MERGE_RUN, OWN_RUN = "run-pr", "run-merge", "run-own"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                                   # tier 1
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"     # tier 2
TIER3 = "census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader"              # tier 3
PH = "0123456789abcdef"
TS = "2026-11-10T15:00:00+00:00"
BUDGETS = {"order_confirmation": 0.05}

ONCALL = {
    "schema_version": 1,
    "roles": [{"role": "system_owner", "person": "sam", "deputy": "sid"},
              {"role": "release_manager", "person": "rita", "deputy": "rob"},
              {"role": "explorer_owner", "person": "eve", "deputy": "sam"}],
    "flow_owners": [{"tier": 1, "person": "fo1", "deputy": "fo1d"}, {"tier": 2, "person": "fo2", "deputy": "fo2d"},
                    {"tier": 3, "person": "fo3", "deputy": "fo3d"}],
    "rotations": [{"rotation": "triage", "person": "tess", "from": "2026-11-09", "to": "2026-11-15"},
                  {"rotation": "harness_oncall", "person": "hank", "from": "2026-11-09", "to": "2026-11-15"}],
    "triage_roster": ["tess", "tom"],
}
DIRECTORY = RoleDirectory.from_oncall(ONCALL)


def rect(dx, dy, w, h):
    return {"dx": dx, "dy": dy, "w": w, "h": h}


def regions_doc(**changes) -> dict:
    """The s07 regions file (panel 470 x 216 = 101520 px): a 1440 px clock mask, a money cell, plain diff regions."""
    doc = {"schema_version": 1, "checkpoint_id": FLOW,
           "landmarks": [{"id": "lm.title", "template": "regions/landmarks/lm.title.png"}],
           "regions": [
               {"id": "rgn.confirm.title", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(0, 0, 200, 20)},
               {"id": "rgn.confirm.qty", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(90, 48, 48, 20), "money_role": "qty", "money_cell": True},
               {"id": "rgn.confirm.symbol", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(144, 48, 110, 20)},
               {"id": "rgn.confirm.cost", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(24, 96, 200, 20)},
               {"id": "rgn.confirm.timestamp", "type": "rule", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(260, 200, 110, 16), "rule": {"field": "time"}},
               {"id": "rgn.confirm.clock", "type": "mask", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(380, 0, 90, 16), "mask_ref": "msk.clock-01"}]}
    for rid, fields in changes.items():
        next(r for r in doc["regions"] if r["id"] == rid.replace("_", ".")).update(fields)
    return doc


REDACTION = {"schema_version": 1, "owner": "security-contact", "landmarks": [{"id": "lm.title", "template": "x.png"}],
             "regions": [{"id": "rgn.redact.note", "type": "redaction", "landmark": "lm.title",
                          "panel": "order_confirmation", "rect": rect(300, 100, 50, 20)}]}


@dataclass
class FakeRepo:
    """An in-memory sentinel-config host."""
    pulls: dict[int, PullRequest] = field(default_factory=dict)
    head: dict = field(default_factory=lambda: {FLOW: regions_doc()})
    merged: list[int] = field(default_factory=list)
    closed: list[tuple[int, str]] = field(default_factory=list)
    opened: list[tuple[str, str, dict]] = field(default_factory=list)

    def pull(self, number):
        return self.pulls.get(number)

    def merge(self, number, *, by):
        self.merged.append(number)
        self.pulls[number] = replace(self.pulls[number], state="merged")
        return "c2"

    def close(self, number, *, by, reason):
        self.closed.append((number, reason))
        self.pulls[number] = replace(self.pulls[number], state="closed")

    def open(self, kind, *, title, body, author, files):
        self.opened.append((kind, author, dict(files)))
        return 40 + len(self.opened)

    def head_regions(self):
        return self.head


def plan(run_id, run_type="pr", author=None, committers=None):
    scope = (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
             ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"),
             ScopeEntry(TIER3, "ws.tier-a.swing-trader", "swing_trader", 3, "frozen"))
    return RunPlan(run_id=run_id, run_type=run_type, config_commit=COMMIT, config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope, pr_author=author, pr_committers=committers, started_at=TS)


def find(s, fid, cid=FLOW, region="rgn.confirm.symbol", category="collateral", kind=None, tier=2, run=PR_RUN,
         phash=PH, cluster=None, manifest=None):
    s.write_finding(Finding(fid, run, kind or category, "moved", run, checkpoint_id=cid, tier=tier, region_id=region,
                            phash=phash, category=category, created_at=TS, cluster_id=cluster,
                            manifest_entry_ref=manifest))


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "central.db", roster=DIRECTORY.roster, system_owners=DIRECTORY.system_owners)
    st.begin_run(plan(PR_RUN, author="pat", committers=["pat", "cody"]))
    load_config_mirror(st, COMMIT, {FLOW: regions_doc()})
    find(st, "F-t1", CENSUS, "rgn.ladder", tier=1)
    find(st, "F-t2")
    find(st, "F-t3", TIER3, "rgn.cell", tier=3)
    find(st, "F-qty", region="rgn.confirm.qty")
    find(st, "F-cost", region="rgn.confirm.cost")
    find(st, "F-inv1", CENSUS, "rgn.ladder.bid", category="invariant_violation", tier=1)
    find(st, "F-inv2", region="rgn.confirm.title", category="invariant_violation")
    find(st, "F-exp", region="rgn.confirm.title", category="needs_human", kind="explorer_candidate")
    yield st
    st.close()


def ctx(repo=None, **kw):
    return ActionContext(DIRECTORY, repo=repo, registry=lambda run_id: {FLOW: regions_doc()}, redaction=REDACTION,
                         budgets=BUDGETS, rotation="tess", clock=lambda: TS, **kw)


def grant(s, action, login, fid, **kw):
    return authorize(action, Actor(login, DIRECTORY), finding_target(s, fid), **kw)


def refused(s, action, login, fid, clause, **kw):
    with pytest.raises(ActionRefused) as e:
        grant(s, action, login, fid, **kw)
    assert e.value.clause.startswith(clause), str(e.value)
    return e.value


# -- the table itself ------------------------------------------------------------------------------------------------

def test_the_table_is_well_formed_and_lists_every_9_6_row():
    assert check_table() == []
    rows = {r["row"] for r in table()}
    for row in ("Confirm bug", "Mark intended with ticket", "Waive invariant or assertion", "Request mask with reason",
                "Approve mask", "Mark noise", "Approve or reject baseline", "Explorer disposition",
                "Promote candidate flow", "Accept anchor patch", "Record fixed in build", "Revoke decision",
                "Supersede decision", "Quarantine or un-quarantine", "Override check"):
        assert row in rows
    assert RULES["request_mask"].who == {None: ("anyone",)}                 # coordinator decision (9.6)
    assert RULES["override_check"].who == {None: ("system_owner",)}


def test_service_identities_never_act():
    with pytest.raises(ActionRefused, match="L-DEC-01"):
        Actor("reviewer", DIRECTORY)


# -- 9.6 "who may perform", one row at a time ------------------------------------------------------------------------

@pytest.mark.parametrize("action,login,fid,role,proposal", [
    ("confirm_bug", "tess", "F-t2", "triage_rotation", False),
    ("confirm_bug", "pat", "F-t2", "pr_author", False),                    # any role, the PR author included
    ("confirm_bug", "fo2", "F-t2", "flow_owner", False),
    ("mark_intended", "pat", "F-t3", "pr_author", False),                  # tier 3: the PR author alone
    ("mark_intended", "tess", "F-t2", "triage_rotation", False),           # other roles alone
    ("mark_intended", "pat", "F-t2", "pr_author", True),                   # tier 1 and 2: the author proposes
    ("mark_intended", "cody", "F-t1", "pr_author", True),                  # any committer, not only pr_author
    ("waive", "fo2", "F-inv2", "flow_owner", False),
    ("mark_intended", "fo2", "F-inv2", "flow_owner", False),               # mark_intended on an invariant: a waiver
    ("waive", "fo1", "F-inv1", "flow_owner", True),                        # tier 1: the system owner signs too
    ("request_mask", "tess", "F-t2", "triage_rotation", False),
    ("request_mask", "zed", "F-t2", None, True),                           # anyone requests; L-DEC-01 decides
    ("noise_signature", "tess", "F-t2", "triage_rotation", False),
    ("noise_signature", "fo2", "F-t2", "flow_owner", False),
    ("agent_mistake", "tess", "F-exp", "triage_rotation", False),          # Explorer disposition
    ("confirm_bug", "tess", "F-exp", "triage_rotation", False),
    ("promote_flow", "fo2", "F-t2", None, False),
    ("accept_anchor_patch", "fo2", "F-t2", None, False),
    ("accept_anchor_patch", "tom", "F-t2", None, False),
    ("quarantine", "fo2", "F-t2", None, False),
    ("unquarantine", "sam", "F-t2", None, False),
    ("assign", "hank", "F-t2", None, False),                               # any role in 9.12 (harness on-call)
])
def test_allowed_roles_are_granted(s, action, login, fid, role, proposal):
    g = grant(s, action, login, fid)
    assert (g.role, g.proposal) == (role, proposal)


@pytest.mark.parametrize("action,login,fid,clause", [
    ("confirm_bug", "zed", "F-t2", "9.6 Confirm bug"),                     # holds no role at all
    ("confirm_bug", "eve", "F-t2", "L-DEC-01"),                            # a role, but not a decider
    ("mark_intended", "eve", "F-t3", "L-DEC-01"),
    ("waive", "tess", "F-inv2", "9.6 Waive invariant or assertion"),
    ("waive", "sam", "F-inv1", "9.6 Waive invariant or assertion"),        # the system owner alone is not enough
    ("request_mask", "tess", "F-qty", "L-DEC-04"),
    ("noise_signature", "fo1", "F-t2", "9.6 Mark noise"),                  # tier 1's owner on a tier-2 finding
    ("noise_signature", "pat", "F-t2", "9.6 Mark noise"),
    ("noise_signature", "tess", "F-qty", "L-DEC-04"),
    ("expected_behavior", "fo2", "F-exp", "9.6 Explorer disposition"),
    ("agent_mistake", "tess", "F-t2", "9.6 Explorer disposition"),         # only on Explorer findings
    ("promote_flow", "tess", "F-t2", "9.6 Promote candidate flow"),
    ("accept_anchor_patch", "sam", "F-t2", "9.6 Accept anchor patch"),
    ("quarantine", "tess", "F-t2", "9.6 Quarantine or un-quarantine"),
    ("assign", "zed", "F-t2", "9.6 Triage queue assign"),
])
def test_other_roles_are_refused_with_the_clause_named(s, action, login, fid, clause):
    refused(s, action, login, fid, clause)


def test_decision_actions_take_the_roles_of_the_table(s):
    t2 = finding_target(s, "F-t2")
    for login, ok in (("tess", True), ("fo2", True), ("sam", True), ("pat", False)):
        for action in ("revoke", "supersede"):
            if ok:
                assert authorize(action, Actor(login, DIRECTORY), t2).rule.action == action
            else:
                with pytest.raises(ActionRefused, match=f"9.6 {RULES[action].row}"):
                    authorize(action, Actor(login, DIRECTORY), t2)
    with pytest.raises(ActionRefused, match="9.6 Override check"):
        authorize("override_check", Actor("tess", DIRECTORY), t2)
    assert authorize("override_check", Actor("sam", DIRECTORY), t2).rule.target == "run"
    jira = Actor("resolver.r", DIRECTORY, source="jira_sync")
    assert authorize("fixed_in_build", jira, t2).role == "ticket_resolver"   # the Jira poller (9.9)
    with pytest.raises(ActionRefused, match="L-DEC-01"):
        authorize("fixed_in_build", Actor("pat", DIRECTORY), t2)            # any role, but L-DEC-01 still applies


def test_baseline_approval_by_tier_and_never_by_the_author_at_any_tier(s):
    assert grant(s, "approve_baseline", "fo1", "F-t1").role == "flow_owner"
    refused(s, "approve_baseline", "tess", "F-t1", "9.6 Approve or reject baseline")   # tier 1: its flow owner only
    assert grant(s, "reject_baseline", "tess", "F-t2").role == "triage_rotation"
    assert grant(s, "approve_baseline", "fo3", "F-t3").role == "flow_owner"
    s.begin_run(plan(OWN_RUN, author="fo3", committers=["fo3"]))
    find(s, "F-own3", TIER3, "rgn.cell", tier=3, run=OWN_RUN)
    refused(s, "approve_baseline", "fo3", "F-own3", "9.6 two-person rule (c)")         # tier 3 too (clause c)
    refused(s, "approve_baseline", "fo2", "F-t2", "9.6 two-person rule (c)", change_authors=["fo2"])


def test_the_synthetic_items_take_no_actions(s):
    s.write_finding(Finding("F-syn", PR_RUN, "collateral", "mutant", PR_RUN, checkpoint_id=FLOW, tier=2,
                            region_id="rgn.confirm.symbol", phash=PH, category="collateral"), synthetic=True)
    refused(s, "confirm_bug", "tess", "F-syn", "9.1")


# -- writes: confirm_bug, mark_intended and the two-person proposals ---------------------------------------------------

def test_confirm_bug_writes_the_decision_and_a_jira_job_whose_key_lands_on_decision_and_finding(s):
    r = apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-t2", reason="ladder overlaps"))
    d = s.get_decision(r.decisions[0])
    assert (d.action, d.decided_by, d.role, d.ticket) == ("confirm_bug", "tess", "triage_rotation", None)
    assert s.get_finding("F-t2").triage_state == "confirmed_bug"
    job = s.outbox_job(r.jobs[0])
    assert (job.kind, job.destination, job.state) == ("jira_create", "jira", "queued")
    payload = json.loads(r.payloads[job.payload_sha256])
    assert payload["decision"] == d.decision_id
    again = apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-t2", reason="ladder overlaps"))
    assert again.decisions == [d.decision_id] and again.jobs == []           # idempotent per finding and action
    ticket_landed(s, payload, "TOS-77")
    assert s.get_decision(d.decision_id).ticket == "TOS-77" and s.get_finding("F-t2").ticket == "TOS-77"


def test_confirm_bug_with_a_ticket_links_it_instead_of_filing(s):
    r = apply("confirm_bug", s, ctx(), ActionRequest("pat", finding="F-t2", ticket="TOS-9", reason="bug"))
    job = s.outbox_job(r.jobs[0])
    assert (job.kind, job.destination) == ("jira_comment", "TOS-9")
    assert s.get_decision(r.decisions[0]).role == "pr_author"


def test_mark_intended_on_an_undeclared_surface_queues_the_manifest_amendment_and_the_ticket_comment(s):
    r = apply("mark_intended", s, ctx(), ActionRequest("tess", finding="F-t2", ticket="TOS-5", reason="new look"))
    kinds = sorted(s.outbox_job(j).kind for j in r.jobs)
    assert kinds == ["jira_comment", "manifest_pr"]
    assert s.get_finding("F-t2").triage_state == "intended"
    with pytest.raises(ActionRefused, match="L-DEC-02"):
        apply("mark_intended", s, ctx(), ActionRequest("tess", finding="F-t3", reason="no ticket"))


def test_a_committer_who_is_not_pr_author_gets_a_proposal_and_the_confirm_writes_the_decision(s):
    r = apply("mark_intended", s, ctx(), ActionRequest("cody", finding="F-t2", ticket="TOS-5", reason="redesign"))
    assert r.decisions == [] and r.proposal is not None
    assert s.query("SELECT count(*) FROM decisions")[0][0] == 0
    f = s.get_finding("F-t2")
    assert f.triage_state == "open"
    note = f.evidence["proposal"]
    assert (note["action"], note["proposer"], note["ticket"], note["state"]) == ("mark_intended", "cody", "TOS-5",
                                                                                  "proposed")
    assert [p["proposal_id"] for p in open_proposals(s)] == [r.proposal]
    again = apply("mark_intended", s, ctx(), ActionRequest("cody", finding="F-t2", ticket="TOS-5", reason="x"))
    assert again.proposal == r.proposal                                        # the same click: same proposal
    with pytest.raises(ActionRefused, match="two-person rule"):
        confirm(s, ctx(), r.proposal, "cody")                                  # never the proposer
    with pytest.raises(ActionRefused, match="9.6 Two-person rule"):
        confirm(s, ctx(), r.proposal, "fo1")                                   # tier 1's owner, tier-2 finding
    c = confirm(s, ctx(), r.proposal, "tess")
    d = s.get_decision(c.decisions[0])
    assert (d.decided_by, d.confirmed_by) == ("tess", "tess") and "proposed by cody" in d.reason
    assert s.query("SELECT proposed_by FROM decisions")[0][0] == "cody"
    row = s.query("SELECT state, decided_by FROM proposals WHERE proposal_id = ?", (r.proposal,))[0]
    assert (row["state"], row["decided_by"]) == ("confirmed", "tess")
    assert s.get_finding("F-t2").evidence["proposal"]["state"] == "confirmed"
    with pytest.raises(ActionRefused, match="already confirmed"):
        confirm(s, ctx(), r.proposal, "tom")


def test_declining_returns_the_finding_to_unexpected_with_the_reason(s):
    p = propose(s, ctx(), ActionRequest("pat", finding="F-t1", ticket="TOS-3", reason="moved on purpose")).proposal
    with pytest.raises(ActionRefused, match="reason"):
        decline(s, ctx(), p, "tess", "")
    decline(s, ctx(), p, "fo1", "the ladder must not move")
    f = s.get_finding("F-t1")
    assert f.triage_state == "open" and f.evidence["triage_note"] == "proposal declined by fo1: the ladder must not move"
    assert f.evidence["proposal"]["state"] == "declined"
    assert open_proposals(s) == []
    assert propose(s, ctx(), ActionRequest("pat", finding="F-t1", ticket="TOS-3", reason="again")).proposal != p
    with pytest.raises(ActionRefused, match="not a proposal"):
        propose(s, ctx(), ActionRequest("tess", finding="F-t2", ticket="TOS-3", reason="x"))


def test_a_tier_1_waiver_needs_the_flow_owner_and_the_system_owner(s):
    r = apply("mark_intended", s, ctx(), ActionRequest("fo1", finding="F-inv1", ticket="TOS-8", reason="known"))
    assert r.proposal is not None and r.decisions == []
    with pytest.raises(ActionRefused, match=r"\(d\)"):
        confirm(s, ctx(), r.proposal, "tess")                                  # triage is not the second sign-off
    c = confirm(s, ctx(), r.proposal, "sam")
    d = s.get_decision(c.decisions[0])
    assert (d.decided_by, d.confirmed_by, d.role) == ("fo1", "sam", "flow_owner")
    assert d.expires_at is not None and d.expires_at <= "2026-12-10T15:00:00+00:00"   # at most 30 days (L-DEC-07)
    w2 = apply("waive", s, ctx(), ActionRequest("fo2", finding="F-inv2", ticket="TOS-9", reason="known"))
    assert s.get_decision(w2.decisions[0]).confirmed_by is None               # tier 2: the flow owner alone


# -- request mask, approve mask ------------------------------------------------------------------------------------

def test_a_mask_request_over_the_panel_budget_is_refused_with_share_budget_and_advice(s):
    with pytest.raises(MaskBudgetExceeded) as e:
        apply("request_mask", s, ctx(), ActionRequest("tess", finding="F-cost", reason="moves"))
    x = e.value
    assert x.budget == 0.05 and x.current == pytest.approx(2440 / 101520) and "rule region" in str(x)
    assert s.query("SELECT count(*) FROM decisions")[0][0] == 0
    assert s.query("SELECT count(*) FROM outbox")[0][0] == 0
    with pytest.raises(MaskBudgetExceeded):                                    # the same for a proposal
        apply("request_mask", s, ctx(), ActionRequest("zed", finding="F-cost", reason="moves"))
    assert s.query("SELECT count(*) FROM proposals")[0][0] == 0


def test_a_valid_request_writes_the_inert_decision_and_the_mask_pr_job_and_the_item_reads_mask_requested(s):
    r = apply("request_mask", s, ctx(), ActionRequest("tess", finding="F-t2", reason="the symbol flickers"))
    d = s.get_decision(r.decisions[0])
    assert (d.action, d.verdict) == ("request_mask", "noise")
    assert s.query("SELECT mask_ref FROM decisions")[0][0] is None
    job = s.outbox_job(r.jobs[0])
    assert (job.kind, job.destination) == ("mask_pr", "sentinel-config")
    mask = json.loads(r.payloads[job.payload_sha256])["mask"]
    assert mask["decision_ref"] == d.decision_id and mask["created_by"] == "human" and mask["owner"] == "fo2"
    assert mask["expires_at"].startswith("2026-12-10") and mask["rect"] == rect(144, 48, 110, 20)
    assert s.get_finding("F-t2").triage_state == "open"
    mask_pr_opened(s, d.decision_id, 12)
    assert s.get_finding("F-t2").evidence["triage_note"] == "mask requested, PR 12"


def test_a_request_from_outside_the_roster_waits_for_a_decider(s):
    r = apply("request_mask", s, ctx(), ActionRequest("zed", finding="F-t2", reason="flickers"))
    assert r.proposal is not None and r.jobs == []
    c = confirm(s, ctx(), r.proposal, "tess")
    d = s.get_decision(c.decisions[0])
    assert d.decided_by == "tess" and "requested by zed" in d.reason
    assert s.query("SELECT proposed_by FROM decisions")[0][0] == "zed"
    assert s.outbox_job(c.jobs[0]).kind == "mask_pr"


def _requested(s, repo, requester="tom", pr=12):
    r = apply("request_mask", s, ctx(repo), ActionRequest(requester, finding="F-t2", reason="flickers"))
    did = r.decisions[0]
    repo.pulls[pr] = PullRequest(pr, "mask_pr", requester, checks="passed", decision_ref=did,
                                 mask_id="msk.symbol-01", checkpoint_id=FLOW,
                                 mask_rect={"landmark": "lm.title", "rect": rect(144, 48, 110, 20)})
    return did


def test_approve_mask_merges_and_links_but_never_by_its_requester(s):
    repo = FakeRepo()
    did = _requested(s, repo)
    with pytest.raises(ActionRefused, match=r"\(b\)"):
        apply("approve_mask", s, ctx(repo), ActionRequest("tom", decision=did, pr=12))
    with pytest.raises(ActionRefused, match="9.6 Approve mask"):
        apply("approve_mask", s, ctx(repo), ActionRequest("fo2", decision=did, pr=12))   # not the triage rotation
    r = apply("approve_mask", s, ctx(repo), ActionRequest("tess", decision=did, pr=12))
    assert repo.merged == [12] and r.pr == 12
    assert s.query("SELECT mask_ref FROM decisions")[0][0] == "msk.symbol-01"
    assert s.get_finding("F-t2").triage_state == "noise"


def test_approve_mask_waits_for_the_lint(s):
    repo = FakeRepo()
    did = _requested(s, repo)
    repo.pulls[12] = replace(repo.pulls[12], checks="failed")
    with pytest.raises(ActionRefused, match="lint"):
        apply("approve_mask", s, ctx(repo), ActionRequest("tess", decision=did, pr=12))
    assert repo.merged == []


def test_a_mask_pr_failing_the_budget_at_merge_is_closed_revoked_notified_and_the_sla_restarts(s):
    repo = FakeRepo()
    apply("assign", s, ctx(), ActionRequest("tom", finding="F-t2", assignee="tom", at="2026-11-10T14:00:00+00:00"))
    did = _requested(s, repo)
    repo.head = {FLOW: regions_doc(rgn_confirm_title={"type": "mask", "mask_ref": "msk.title-01"})}   # merged first
    r = apply("approve_mask", s, ctx(repo), ActionRequest("tess", decision=did, pr=12))
    assert r.note == "mask_budget_exceeded" and repo.merged == []
    assert repo.closed and repo.closed[0][0] == 12 and repo.closed[0][1].startswith("mask_budget_exceeded")
    d = s.get_decision(did)
    assert d.status == "revoked"
    assert s.query("SELECT revoke_reason FROM decisions")[0][0] == "mask_budget_exceeded"
    notified = sorted(json.loads(r.payloads[s.outbox_job(j).payload_sha256])["notify"] for j in r.jobs)
    assert notified == ["tess", "tom"]                                        # the requester and the rotation
    rows = s.query("SELECT assignee, assigned_at, released_at FROM triage_assignments ORDER BY assignment_id")
    assert rows[0]["released_at"] == TS and (rows[1]["assignee"], rows[1]["assigned_at"]) == ("tom", TS)
    f = s.get_finding("F-t2")
    assert f.triage_state == "open" and "mask_budget_exceeded" in f.evidence["triage_note"]


def test_a_tier_1_mask_needs_the_tier_1_flow_owner_as_reviewer(s):
    repo = FakeRepo()
    t1 = {CENSUS: {"schema_version": 1, "checkpoint_id": CENSUS,
                   "landmarks": [{"id": "lm.ladder", "template": "x.png"}],
                   "regions": [{"id": "rgn.ladder", "type": "rule", "landmark": "lm.ladder", "panel": "ladder",
                                "rect": rect(0, 0, 50, 20), "rule": {"field": "time"}}]}}
    c = replace(ctx(repo), registry=lambda run_id: t1)
    repo.head = t1
    did = apply("request_mask", s, c, ActionRequest("tom", finding="F-t1", reason="ticks")).decisions[0]
    repo.pulls[5] = PullRequest(5, "mask_pr", "tom", checks="passed", decision_ref=did, mask_id="msk.l-01")
    with pytest.raises(ActionRefused, match="tier-1 flow owner"):
        apply("approve_mask", s, c, ActionRequest("tess", decision=did, pr=5))
    repo.pulls[5] = replace(repo.pulls[5], reviewers=("fo1",))
    apply("approve_mask", s, c, ActionRequest("tess", decision=did, pr=5))
    assert repo.merged == [5]


# -- the other writes ------------------------------------------------------------------------------------------------

def test_override_check_is_the_system_owners_and_writes_the_run_fields_once(s):
    with pytest.raises(ActionRefused, match="9.6 Override check"):
        apply("override_check", s, ctx(), ActionRequest("tess", run=PR_RUN, reason="flaky env"))
    r = apply("override_check", s, ctx(), ActionRequest("sam", run=PR_RUN, reason="flaky env"))
    row = s.query("SELECT check_override_by, check_override_reason FROM runs WHERE run_id = ?", (PR_RUN,))[0]
    assert tuple(row) == ("sam", "flaky env")
    assert s.outbox_job(r.jobs[0]).kind == "commit_status"
    assert apply("override_check", s, ctx(), ActionRequest("sam", run=PR_RUN, reason="flaky env")).jobs == []
    with pytest.raises(ActionRefused, match="already overridden"):
        apply("override_check", s, ctx(), ActionRequest("sam", run=PR_RUN, reason="other"))


def test_noise_revoke_supersede_and_reconfirm(s):
    n = apply("noise_signature", s, ctx(), ActionRequest("fo2", finding="F-t2", reason="clock")).decisions[0]
    assert s.get_decision(n).role == "flow_owner"                              # the decision row stores the role
    with pytest.raises(StoreRefused):
        apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-t2", reason="bug"))   # 9.7 one signature
    sup = apply("supersede", s, ctx(), ActionRequest("tess", decision=n, reason="really noise", ticket=None))
    new = sup.decisions[0]
    assert s.get_decision(n).status == "superseded" and s.get_decision(new).supersedes == n
    assert json.loads(sup.payloads[s.outbox_job(sup.jobs[0]).payload_sha256])["notify"] == "fo2"
    re = apply("reconfirm_noise", s, ctx(), ActionRequest("tess", decision=new))
    assert s.get_decision(re.decisions[0]).supersedes == new
    with pytest.raises(ActionRefused, match="reason"):
        apply("revoke", s, ctx(), ActionRequest("sam", decision=re.decisions[0]))
    apply("revoke", s, ctx(), ActionRequest("sam", decision=re.decisions[0], reason="wrong"))
    assert s.get_decision(re.decisions[0]).status == "revoked"


def test_explorer_disposition_writes_with_the_explorer_source(s):
    r = apply("expected_behavior", s, ctx(), ActionRequest("tess", finding="F-exp", reason="by design"))
    d = s.get_decision(r.decisions[0])
    assert (d.action, d.source) == ("expected_behavior", "explorer_triage")
    assert s.get_finding("F-exp").triage_state == "expected_behavior"


def test_fixed_in_build_by_hand_supersedes_the_confirm_bug(s):
    for b, at in (("m0", "2026-10-01T00:00:00+00:00"), ("b1", "2026-11-01T00:00:00+00:00"),
                  ("b2", "2026-11-05T00:00:00+00:00")):
        s.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                         first_seen_at=at)
    bug = apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-t2", ticket="TOS-1", reason="bug",
                                                        build_id="b1")).decisions[0]
    r = apply("fixed_in_build", s, ctx(), ActionRequest("tom", decision=bug, fixed_in_build="b2"))
    d = s.get_decision(r.decisions[0])
    assert (d.action, d.supersedes) == ("fixed_in_build", bug)
    assert s.get_finding("F-t2").triage_state == "fixed_pending_verify"


def test_quarantine_queues_its_pr_and_promote_flow_opens_one(s):
    r = apply("quarantine", s, ctx(), ActionRequest("fo2", finding="F-t2", reason="flaky", ticket="TOS-2"))
    assert s.outbox_job(r.jobs[0]).kind == "quarantine_pr"
    repo = FakeRepo()
    p = apply("promote_flow", s, ctx(repo), ActionRequest("fo2", finding="F-t2", reason="trimmed",
                                                          files={"flows/x.yaml": "steps: []"}))
    assert p.pr == 41 and repo.opened[0][:2] == ("flow", "fo2")


def test_assign_and_close_an_abandoned_job(s):
    apply("assign", s, ctx(), ActionRequest("tess", finding="F-t2", assignee="tom"))
    apply("assign", s, ctx(), ActionRequest("tess", finding="F-t2", assignee="tom"))       # same: no new row
    assert s.query("SELECT count(*) FROM triage_assignments")[0][0] == 1
    r = apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-t2", reason="bug"))
    s.abandon(r.jobs[0])
    with pytest.raises(ActionRefused, match="Close an abandoned"):
        apply("close_outbox_job", s, ctx(), ActionRequest("zed", job=r.jobs[0]))
    apply("close_outbox_job", s, ctx(), ActionRequest("hank", job=r.jobs[0]))
    assert s.outbox_job(r.jobs[0]).closed_by == "hank"


def test_a_cluster_action_decides_every_open_member(s):
    find(s, "F-c1", region="rgn.confirm.symbol", cluster="cl-1")
    find(s, "F-c2", region="rgn.confirm.title", cluster="cl-1", phash="fedcba9876543210")
    r = apply("confirm_bug", s, ctx(), ActionRequest("tess", finding="F-c1", reason="bug", cluster=True))
    assert len(r.decisions) == 2
    assert {s.get_decision(x).signature.region_id for x in r.decisions} == {"rgn.confirm.symbol",
                                                                            "rgn.confirm.title"}
    assert s.query("SELECT DISTINCT cluster_id FROM decisions")[0][0] == "cl-1"


def test_approve_baseline_writes_the_decision_from_a_complete_merge_run(s):
    s.register_build("m0", channel="branch", source_commit="m0", pipeline_artifact_url="u", installer_sha256="m0",
                     first_seen_at="2026-10-01T00:00:00+00:00")
    s.begin_run(plan(MERGE_RUN, run_type="merge"))
    for cid, persona, tier, ws in ((CENSUS, "active_trader", 1, WS), (FLOW, "active_trader", 2, WS),
                                   (TIER3, "swing_trader", 3, "ws.tier-a.swing-trader")):
        s.write_checkpoint(CheckpointOutcome(run_id=MERGE_RUN, checkpoint_id=cid, outcome="identical",
                                             workspace_id=ws, persona=persona, data_mode="frozen", tier=tier))
    find(s, "F-base", TIER3, "rgn.cell", category="needs_human", kind="needs_baseline", tier=3, run=MERGE_RUN)
    s.end_run(RunOutcome(MERGE_RUN, "merge", "complete", "reported"))
    bid = s.insert("baselines", {"checkpoint_id": TIER3, "lineage": "released", "build_id": "m0",
                                 "image_sha256": "c" * 64, "state": "candidate", "source_run_id": MERGE_RUN,
                                 "proposed_at": TS})
    with pytest.raises(ActionRefused, match="L-DEC-02"):
        apply("approve_baseline", s, ctx(), ActionRequest("fo3", finding="F-base", baseline_id=bid, reason="ok"))
    r = apply("approve_baseline", s, ctx(), ActionRequest("fo3", finding="F-base", baseline_id=bid, reason="ok",
                                                           ticket="TOS-4"))
    d = s.get_decision(r.decisions[0])
    assert (d.action, d.role) == ("approve_baseline", "flow_owner")


def test_rule_for_routes_waivers_and_explorer_findings(s):
    assert rule_for("mark_intended", finding_target(s, "F-inv2")).action == "waive"
    assert rule_for("confirm_bug", finding_target(s, "F-exp")).action == "explorer_disposition"
    with pytest.raises(ActionRefused, match="unknown action"):
        rule_for("delete_everything")


def test_a_repeated_mask_request_is_the_same_decision_and_queues_no_second_pr(s):
    first = apply("request_mask", s, ctx(), ActionRequest("tess", finding="F-t2", reason="flickers", at=TS))
    again = apply("request_mask", s, ctx(), ActionRequest("tess", finding="F-t2", reason="flickers",
                                                          at="2026-11-10T16:00:00+00:00"))
    assert again.decisions == first.decisions and again.jobs == []
    assert s.query("SELECT count(*) FROM outbox WHERE kind = 'mask_pr'")[0][0] == 1


def test_a_committer_never_waives_a_tier_1_or_2_finding_of_their_own_pr(s):
    s.begin_run(plan(OWN_RUN, author="fo2", committers=["fo2"]))
    find(s, "F-own2", region="rgn.confirm.title", category="invariant_violation", run=OWN_RUN)
    refused(s, "waive", "fo2", "F-own2", "9.6 two-person rule (a)")
    assert grant(s, "waive", "fo2d", "F-own2").role == "flow_owner"             # another flow owner may
