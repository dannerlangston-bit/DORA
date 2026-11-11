"""Jira composition (9.9, 9.4 item 2, 11.8): labels with the Phase 0 hashed default, the identity block, product Bug
descriptions and priorities, the link-to-existing queries and collisions, the seen-again and auto-filing rules, SENT
keys, and delivery against the in-memory Jira."""
from __future__ import annotations

import hashlib

import pytest

from sentinel.records import Decision, Finding, Signature
from sentinel.report import jira as J
from sentinel.taxonomy import TaxonomyError

SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"     # the 9.9 example checkpoint
SWEEP2 = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/400x300"
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2"
CENSUS = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
RUN = "run-2026-11-09-pr-4812"
BASE = "https://sentinel.internal"
PHASH = "3f9c1a0e55b2d8c4"
CROP = "c" * 64


def h12(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def decision(did="dec-000517", cid=SWEEP, finding="F-20931", action="confirm_bug", ticket=None):
    verdict = "intended" if action == "mark_intended" else "confirmed_bug"
    return Decision(did, action, verdict, Signature(cid, "rgn.ladder.viewport", PHASH, "invariant_violation"),
                    decided_by="alice", ticket=ticket, run_id=RUN, finding_ref=finding, reason="overlap")


def finding(fid="F-20931", cid=SWEEP, tier=1, synthetic=False, category="invariant_violation", **kw):
    return Finding(fid, RUN, category if category != "invariant_violation" else "invariant_violation",
                   "ladder overlaps the order entry bar", RUN, checkpoint_id=cid, tier=tier,
                   region_id="rgn.ladder.viewport", primitive="FlexibleGrid", phash=PHASH, category=category,
                   evidence={"artifacts": [CROP], "layout": {"grid": "2x2", "cell": "r1c2"}}, synthetic=synthetic,
                   **kw)


CTX = J.TicketContext(report_base=BASE, build_pair="b-2231 vs m-2230", pr=4812, workspace="ws.tier-c.flexgrid")


# -- labels (9.9, 11.8) ----------------------------------------------------------------------------------------------

def test_labels_of_the_9_9_example_decision_match_exactly():
    got = J.labels(SWEEP, tier=1, decision_ids=["dec-000517"])
    assert got.labels == ("sentinel", "sentinel-tier-1",
                          f"sentinel-surface-{h12('charts.flexible-grid.active-trader-cell')}",
                          f"sentinel-cp-{h12(SWEEP)}", "sentinel-dec-dec-000517")
    assert got.overflow == ()
    plain = J.labels(SWEEP, tier=1, decision_ids=["dec-000517"], charset_ok=True).labels
    assert plain[2] == "sentinel-surface-charts.flexible-grid.active-trader-cell" and plain[3] == got.labels[3]


def test_the_phase0_default_hashes_every_id_bearing_label():
    hashed = J.labels(FLOW, tier=1, decision_ids=["dec-000001"]).labels
    assert f"sentinel-cp-{h12(FLOW)}" in hashed and len(h12(FLOW)) == 12
    assert f"sentinel-surface-{h12('dialog.order-confirmation')}" in hashed
    assert not any("/" in lb or "." in lb for lb in hashed)
    assert "sentinel-surface-dialog.order-confirmation" in J.labels(FLOW, charset_ok=True).labels


def test_a_ninth_decision_label_moves_to_the_description():
    ids = [f"dec-{n:06d}" for n in range(600, 609)]
    got = J.labels(SWEEP, tier=1, decision_ids=ids)
    assert [lb for lb in got.labels if J.decision_of_label(lb)] == [f"sentinel-dec-{d}" for d in ids[:8]]
    assert got.overflow == ("dec-000608",)
    decs = [decision(d, finding=f"F-{n}") for n, d in enumerate(ids)]
    job = J.bug_job(decs, [finding(f"F-{n}") for n in range(9)], CTX)
    issue = job.payload["issue"]
    assert "sentinel-dec-dec-000608" not in issue["labels"] and len(
        [lb for lb in issue["labels"] if lb.startswith("sentinel-dec-")]) == 8
    assert "  decision: dec-000608" in issue["description"]
    assert J.ticket_decisions(J.Ticket("TOS-1", tuple(issue["labels"]), issue["description"])) == tuple(ids)


def test_decision_labels_round_trip_and_refuse_other_ids():
    assert J.decision_of_label(J.dec_label("dec-000042")) == "dec-000042"
    assert J.decision_of_label("sentinel-dec-x") is None and J.decision_of_label("sentinel-cp-abc") is None
    with pytest.raises(ValueError):
        J.dec_label("42")


# -- identity block and description ---------------------------------------------------------------------------------

def test_the_identity_block_is_the_9_9_example_and_parses_back():
    block = J.identity_block(decision(), BASE)
    assert block == ("sentinel:\n"
                     "  decision: dec-000517\n"
                     "  checkpoint: sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360\n"
                     "  region: rgn.ladder.viewport\n"
                     "  category: invariant_violation\n"
                     "  phash: 3f9c1a0e55b2d8c4\n"
                     "  run: run-2026-11-09-pr-4812\n"
                     "  report: https://sentinel.internal/f/F-20931")
    (parsed,) = J.parse_identity(f"Some text\n{{noformat}}\n{block}\n{{noformat}}\nmore")
    assert {k: v[0] for k, v in parsed.items()} == {
        "decision": "dec-000517", "checkpoint": SWEEP, "region": "rgn.ladder.viewport",
        "category": "invariant_violation", "phash": PHASH, "run": RUN, "report": f"{BASE}/f/F-20931"}


def test_priority_follows_the_tier():
    assert [J.priority(t) for t in (1, 2, 3)] == ["Highest", "Medium", "Low"]
    with pytest.raises(ValueError):
        J.priority(4)


def test_the_bug_description_names_every_9_9_field():
    job = J.bug_job([decision(), decision("dec-000518", SWEEP2, "F-20932")],
                    [finding(), finding("F-20932", SWEEP2)], CTX)
    assert (job.kind, job.destination, job.op) == ("jira_create", "TOS#bug", "bug")
    issue = job.payload["issue"]
    assert (issue["project"], issue["issuetype"], issue["priority"]) == ("TOS", "Bug", "Highest")
    assert issue["summary"] == ("[Sentinel] tier-1 invariant_violation on charts.flexible-grid.active-trader-cell "
                                "(rgn.ladder.viewport)")
    text = issue["description"]
    for line in ("Build pair: b-2231 vs m-2230", "Surface: charts.flexible-grid.active-trader-cell (tier 1)",
                 "Primitive: FlexibleGrid", "Manifest entry: none (undeclared)", "PR: 4812",
                 "Layout: grid 2x2, cell r1c2", "Failing range: 480x360 to 400x300 (2 steps: 480x360, 400x300)",
                 "Workspace: ws.tier-c.flexgrid", f"Crops (1:1): {BASE}/a/{CROP}",
                 "Reproduction: size sweep of charts.flexible-grid.active-trader-cell in flexgrid at 480x360"):
        assert line in text, line
    assert text.count("sentinel:\n") == 2 and job.payload["decisions"] == ["dec-000517", "dec-000518"]
    assert f"sentinel-cp-{h12(SWEEP2)}" in issue["labels"]


def test_reproduction_names_the_census_path_or_the_flow_and_step():
    assert J.reproduction(CENSUS) == f"census path {CENSUS}"
    assert J.reproduction(FLOW) == "flow flow.trade.order-ticket.futures.single-leg, step cp2"


def test_finding_text_cannot_break_out_of_its_noformat_block():
    f = Finding("F-1", RUN, "collateral", "evil {noformat}[link|http://x]{noformat}", RUN, checkpoint_id=CENSUS,
                tier=2, region_id="r", category="collateral")
    text = J.description([decision(cid=CENSUS, finding="F-1")], [f], CTX)
    body = text.split("\n")
    assert body[1] == "{noformat}" and body[2] == "evil { noformat }[link|http://x]{ noformat }"
    assert "Workspace: ws.tier-a.active-trader" in text            # a census checkpoint names its workspace


# -- dedup on a new confirm_bug -----------------------------------------------------------------------------------

def test_the_dedup_queries_run_cp_then_surface():
    cp, surface = J.dedup_query(SWEEP)
    assert cp == f'labels = "sentinel" AND labels = "sentinel-cp-{h12(SWEEP)}" AND statusCategory != Done'
    surface_hash = h12("charts.flexible-grid.active-trader-cell")
    assert surface == f'labels = "sentinel" AND labels = "sentinel-surface-{surface_hash}" AND statusCategory != Done'


def _block(cid: str, did: str = "dec-000001") -> str:
    return "{noformat}\n" + J.identity_block(decision(did, cid), BASE) + "\n{noformat}"


def test_a_label_hit_naming_another_checkpoint_is_a_collision():
    other = J.Ticket("TOS-9", (), _block(CENSUS))
    same = J.Ticket("TOS-8", (), _block(SWEEP))
    assert J.is_collision(other, SWEEP) and not J.is_collision(same, SWEEP)
    assert J.is_collision(J.Ticket("TOS-7"), SWEEP)                          # no identity block: unproven
    sibling = J.Ticket("TOS-6", (), _block(SWEEP2))
    assert J.is_collision(sibling, SWEEP) and not J.is_collision(sibling, SWEEP, by="surface")
    assert J.is_collision(other, SWEEP, by="surface")


def test_link_candidates_offer_open_product_tickets_and_set_collisions_aside():
    jira = J.MemoryJira()
    cp, surface = J.cp_label(SWEEP), J.surface_label("charts.flexible-grid.active-trader-cell")
    jira.add("TOS-1", labels=["sentinel", cp], description=_block(SWEEP))
    jira.add("TOS-2", labels=["sentinel", cp], description=_block(CENSUS))       # cp hash collision
    jira.add("TOS-3", labels=["sentinel", surface], description=_block(SWEEP2))
    jira.add("TOS-4", labels=["sentinel", cp], description=_block(SWEEP), status="done")
    jira.add("SENT-5", labels=["sentinel", cp, surface], description=_block(SWEEP))   # a canary Bug
    got = J.link_candidates(jira, SWEEP)
    assert [t.key for t in got.offers] == ["TOS-1", "TOS-3"]
    assert [t.key for t in got.collisions] == ["TOS-2"]
    assert [c for c in jira.calls if c[0] == "search"] == [("search", q) for q in J.dedup_query(SWEEP)]


# -- rules ----------------------------------------------------------------------------------------------------------

def test_seen_again_posts_at_most_once_per_ticket_per_24_hours():
    assert J.seen_again_due(None, "2026-11-10T10:00:00+00:00")
    assert not J.seen_again_due("2026-11-10T10:00:00+00:00", "2026-11-11T09:59:00+00:00")
    assert J.seen_again_due("2026-11-10T10:00:00+00:00", "2026-11-11T05:00:00-05:00")
    with pytest.raises(ValueError):
        J.seen_again_due("2026-11-10T10:00:00", "2026-11-11T10:00:00+00:00")


def test_synthetic_records_never_post():
    syn = finding(synthetic=True)
    assert J.bug_job([decision()], [syn], CTX) is None
    assert J.seen_again_job("TOS-1", syn, run_id=RUN, build_id="b1", report_base=BASE) is None
    assert J.intended_job("TOS-1", decision(action="mark_intended", ticket="TOS-1"), [syn], run_id=RUN,
                          report_base=BASE, comment=True) is None
    assert J.auto_file_job(finding(synthetic=True, category="collateral"), CTX) is None


def test_auto_filing_needs_the_switch_and_both_gates_and_goes_to_sent():
    on = {"tier1": True}
    assert not J.auto_file_allowed(None, 1, eval_gates_met=True, money_canary_full_30d=True)
    assert not J.auto_file_allowed(on, 1, eval_gates_met=False, money_canary_full_30d=True)
    assert not J.auto_file_allowed(on, 1, eval_gates_met=True, money_canary_full_30d=False)
    assert not J.auto_file_allowed(on, 2, eval_gates_met=True, money_canary_full_30d=True)
    assert J.auto_file_allowed(on, 1, eval_gates_met=True, money_canary_full_30d=True)
    job = J.auto_file_job(finding(category="collateral"), CTX)
    issue = job.payload["issue"]
    assert (issue["project"], job.destination) == ("SENT", "SENT#auto_file")
    assert not any(lb.startswith("sentinel-dec-") for lb in issue["labels"])
    assert issue["summary"].startswith("[Sentinel triage] tier-1 collateral")
    assert J.auto_file_job(finding(), CTX) is None                                # only collateral


def test_mark_intended_checks_the_key_at_once():
    with pytest.raises(ValueError, match="L-MAN-05"):
        J.intended_job("tos-12", decision(action="mark_intended", ticket="TOS-1"), [finding()], run_id=RUN,
                       report_base=BASE, comment=True)
    job = J.intended_job("TOS-12", decision(action="mark_intended", ticket="TOS-12"), [finding()], run_id=RUN,
                         report_base=BASE, comment=True, manifest_pr="sentinel-config#77")
    assert job.payload["labels"] == ["sentinel", "sentinel-dec-dec-000517"]
    assert f"{BASE}/a/{CROP}" in job.payload["comment"] and "sentinel-config#77" in job.payload["comment"]
    labels_only = J.intended_job("TOS-12", decision(action="mark_intended", ticket="TOS-12"), [finding()],
                                 run_id=RUN, report_base=BASE, comment=False)
    assert labels_only.payload["comment"] is None and labels_only.destination == "TOS-12#intended"


# -- SENT keys --------------------------------------------------------------------------------------------------------

def test_sent_task_keys_name_the_trigger_and_its_subject():
    assert J.sent_task_key(J.SentEvent("env_error", "fixture_verified", RUN)) == "env_error:fixture_verified"
    assert J.sent_task_key(J.SentEvent("unknown_popup", "  Market   data\tdelayed ", RUN)) == \
        "unknown_popup:Market data delayed"
    assert J.SentEvent("passed_via_agent_fallback", "flow.x/s3", RUN).role == "flow_owner"
    assert J.SentEvent("missed_in_pr_run", SWEEP, RUN).role == "system_owner"
    with pytest.raises(TaxonomyError):
        J.SentEvent("env_error", "no_such_gate", RUN)
    with pytest.raises(ValueError):
        J.SentEvent("abandoned_outbox_job", "fax", RUN)
    with pytest.raises(ValueError):
        J.SentEvent("bored", "x", RUN)


def test_a_task_job_carries_the_run_link_and_the_key_label():
    ev = J.SentEvent("no_covering_checkpoint", "tos-48001-1", RUN, detail="entry has no covering checkpoint")
    job = J.task_job(ev, report_base=BASE)
    assert (job.kind, job.destination) == ("jira_create", "SENT#task:no_covering_checkpoint:tos-48001-1")
    issue = job.payload["issue"]
    assert issue["summary"] == "[Sentinel] flow-authoring task: tos-48001-1" and issue["issuetype"] == "Task"
    assert issue["labels"] == ["sentinel", J.key_label("no_covering_checkpoint:tos-48001-1")]
    assert f"{BASE}/runs/{RUN}" in job.payload["comment"] and "  task: no_covering_checkpoint:tos-48001-1" in \
        issue["description"]
    assert J.task_job(ev, report_base=BASE, expect_open=True).kind == "jira_comment"


# -- delivery -----------------------------------------------------------------------------------------------------

def test_a_sent_task_is_created_once_then_commented():
    jira = J.MemoryJira()
    ev = J.SentEvent("env_error", "fixture_verified", RUN)
    first = J.deliver(jira, J.task_job(ev, report_base=BASE).payload)
    assert first.created and first.ticket == "SENT-1"
    again = J.SentEvent("env_error", "fixture_verified", "run-2")
    second = J.deliver(jira, J.task_job(again, report_base=BASE, ticket="SENT-1", expect_open=True).payload)
    assert (second.ticket, second.commented, second.created) == ("SENT-1", True, False)
    assert len(jira.issues) == 1 and jira.issues["SENT-1"]["comments"][0].startswith("seen again in run run-2")
    jira.resolve("SENT-1", by="bob", at="2026-11-10T12:00:00+00:00")
    third = J.deliver(jira, J.task_job(again, report_base=BASE, ticket="SENT-1", expect_open=True).payload)
    assert third.created and third.ticket == "SENT-2"                      # the old task is Done: a new one
    with pytest.raises(J.JiraPending):                                     # its create has not landed yet
        J.deliver(jira, J.task_job(J.SentEvent("env_error", "launched", RUN), report_base=BASE,
                                   expect_open=True).payload)


def test_linking_adds_labels_up_to_eight_decisions_and_the_identity_block():
    jira = J.MemoryJira()
    full = ["sentinel"] + [f"sentinel-dec-dec-{n:06d}" for n in range(1, 9)]
    jira.add("TOS-50", labels=full)
    d = decision("dec-000900")
    out = J.deliver(jira, J.link_job("TOS-50", [d], report_base=BASE, tier=1, linked_by="carol").payload)
    labels = jira.issues["TOS-50"]["labels"]
    assert out.commented and "sentinel-dec-dec-000900" not in labels and J.cp_label(SWEEP) in labels
    assert "sentinel-tier-1" in labels
    assert J.ticket_decisions(jira.get("TOS-50"))[-1] == "dec-000900"      # from the comment's block
    assert "(by carol)" in jira.issues["TOS-50"]["comments"][0]
    with pytest.raises(ValueError):
        J.link_job("not-a-key", [d], report_base=BASE)


def test_a_retried_bug_delivery_files_one_ticket():
    jira = J.MemoryJira()
    payload = J.bug_job([decision()], [finding()], CTX).payload
    first, again = J.deliver(jira, payload), J.deliver(jira, payload)
    assert (first.ticket, first.created) == ("TOS-1", True) and (again.ticket, again.created) == ("TOS-1", False)
    assert list(jira.issues) == ["TOS-1"]


def test_a_named_ticket_jira_does_not_know_comes_back_not_found():
    jira = J.MemoryJira()
    job = J.intended_job("TOS-404", decision(action="mark_intended", ticket="TOS-404"), [finding()], run_id=RUN,
                         report_base=BASE, comment=True)
    assert J.deliver(jira, job.payload).not_found
    with pytest.raises(J.JiraUnavailable):
        J.deliver(J.MemoryJira(unreachable=True), job.payload)
    with pytest.raises(ValueError):
        J.deliver(jira, {"op": "fax"})
