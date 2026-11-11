"""Jira over the Central Store (9.9, 9.6 Record fixed in build, 9.4 item 2, 9.11, 9.12): the 9.9 dedup at enqueue,
delivery write-back, the fix-verification poller and the reopen fallback, SENT tasks and canary Bugs, and what the
report and the digest print while Jira is down."""
from __future__ import annotations

import hashlib
import json

import pytest

from sentinel import canary as C
from sentinel import canary_integrations as I
from sentinel.canonical import sha256_id
from sentinel.orchestrator.alerts import P2
from sentinel.records import CheckpointOutcome, Decision, Finding, Signature
from sentinel.report import jira as J
from sentinel.report import jira_sync as S
from sentinel.store import RunPlan, ScopeEntry, Store, StoreRefused
from sentinel.store.lifecycle import verify_fixes

WS = "ws.tier-a.active-trader"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"     # tier 2
CENSUS = f"census/trade.active-trader.ladder/{WS}"                          # tier 1
CONFIRM = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
REGION = "rgn.ladder.bid-column"
BASE = "https://sentinel.internal"
PH = "0123456789abcdef"
T0 = "2026-11-10T15:02:00+00:00"                                            # 10:02 ET
CROP = "c" * 64


def scope():
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"), ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
            ScopeEntry(CONFIRM, WS, "active_trader", 1, "frozen", flow_class="standard"))


def start(s: Store, run_id: str, candidate: str, at: str = "2026-11-01T12:00:00+00:00") -> None:
    pair = s.register_pair(candidate, "m0", "pr")
    s.begin_run(RunPlan(run_id=run_id, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=scope(), pair_id=pair, started_at=at))


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "central.db")
    for b, at in (("m0", "2026-10-01T00:00:00+00:00"), ("b1", "2026-11-01T00:00:00+00:00"),
                  ("b2", "2026-11-05T00:00:00+00:00"), ("b3", "2026-11-08T00:00:00+00:00")):
        st.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                          first_seen_at=at)
    start(st, "run-00", "b1")
    yield st
    st.close()


def finding(s, fid, run_id="run-00", cid=SWEEP, region=REGION, phash=PH, synthetic=False, tier=2):
    f = Finding(fid, run_id, "collateral", "ladder overlaps", run_id, checkpoint_id=cid, tier=tier, region_id=region,
                phash=phash, category="collateral", evidence={"artifacts": [CROP]})
    s.write_finding(f, synthetic=synthetic)
    return s.get_finding(fid)


def confirm(s, did, fid, *, region=REGION, cid=SWEEP, ticket=None, at=T0, run_id="run-00"):
    d = Decision(did, "confirm_bug", "confirmed_bug", Signature(cid, region, PH, "collateral"), decided_by="alice",
                 ticket=ticket, finding_ref=fid, reason="overlap", decided_at=at)
    return s.record_decision(d, run_id=run_id, build_id="b1")


def ctx():
    return J.TicketContext(report_base=BASE, build_pair="b1 vs m0", pr=4812)


# -- confirm_bug while Jira is down (9.9 Jira unavailable) -------------------------------------------------------------

def test_a_bug_written_while_jira_is_down_shows_pending_then_lands(s):
    f = finding(s, "F-0")
    d = confirm(s, "dec-000001", "F-0", ticket=None)
    q = S.enqueue(s, J.bug_job([d], [f], ctx()), run_id="run-00", at=T0)
    assert S.enqueue(s, J.bug_job([d], [f], ctx()), run_id="run-00", at=T0) is None          # once
    assert s.outbox_job(q.job_id).payload_sha256 == q.payload_sha256 and q.destination == "TOS#bug"
    payload = json.loads(q.payload)
    with pytest.raises(J.JiraUnavailable):
        S.send(J.MemoryJira(unreachable=True), payload)
    s.mark_failed(q.job_id, error="connect timeout", next_attempt_at="2026-11-10T15:03:00+00:00")
    assert S.pending_notes(s, ["dec-000001"], "2026-11-10T16:00:00+00:00") == {
        "dec-000001": "ticket pending (Jira unreachable since 10:02 ET)"}
    assert S.pending_notes(s, ["dec-000001"], "2026-11-11T16:00:00+00:00")["dec-000001"] == \
        "ticket pending (Jira unreachable since 2026-11-10 10:02 ET)"
    assert S.decisions_without_tickets(s, "2026-11-11T15:01:00+00:00") == ()
    (late,) = S.decisions_without_tickets(s, "2026-11-11T15:02:00+00:00")                    # 24 h on
    assert late.decision_id == "dec-000001"
    assert S.without_ticket_line(late, "2026-11-11T17:02:00+00:00") == (
        f"dec-000001: confirm_bug on {SWEEP} (F-0) by alice, no ticket for 26 h; link one by hand")
    jira = J.MemoryJira()
    got = S.send(jira, payload)
    S.record(s, payload, got, at="2026-11-11T17:05:00+00:00")
    s.mark_sent(q.job_id, at="2026-11-11T17:05:00+00:00")
    assert got.ticket == "TOS-1" and s.get_decision("dec-000001").ticket == "TOS-1"
    assert s.get_finding("F-0").ticket == "TOS-1"
    assert jira.issues["TOS-1"]["fields"]["priority"] == "Medium"                             # tier 2
    assert S.jira_unreachable_since(s) is None
    assert S.pending_notes(s, ["dec-000001"], "2026-11-11T18:00:00+00:00") == {"dec-000001": "ticket TOS-1"}
    assert S.decisions_without_tickets(s, "2026-11-12T18:00:00+00:00") == ()


def test_a_queued_bug_without_failures_reads_plain_pending(s):
    finding(s, "F-0")
    d = confirm(s, "dec-000001", "F-0")
    assert S.ticket_note(d, None, T0) == "ticket pending"
    assert S.ticket_note(s.get_decision("dec-000001"), S.jira_unreachable_since(s), T0) == "ticket pending"


def test_link_ticket_by_hand_clears_the_list_and_queues_the_link(s):
    finding(s, "F-0")
    confirm(s, "dec-000001", "F-0", at="2026-11-08T15:00:00+00:00")
    assert [d.decision_id for d in S.decisions_without_tickets(s, T0)] == ["dec-000001"]
    with pytest.raises(StoreRefused, match="L-MAN-05"):
        S.link_ticket(s, "dec-000001", "tos-77", by="dave", report_base=BASE, at=T0)
    with pytest.raises(StoreRefused, match="L-DEC-01"):
        S.link_ticket(s, "dec-000001", "TOS-77", by="system", report_base=BASE, at=T0)
    before = s.get_decision("dec-000001")
    d = S.link_ticket(s, "dec-000001", "TOS-77", by="dave", report_base=BASE, at=T0)
    assert d.ticket == "TOS-77" and s.get_finding("F-0").ticket == "TOS-77"
    assert S.decisions_without_tickets(s, T0) == ()
    (job,) = [r for r in s.query("SELECT kind, destination, run_id, payload_sha256 FROM outbox")]
    link = J.link_job("TOS-77", [before], report_base=BASE, tier=2, linked_by="dave")
    assert tuple(job) == ("jira_comment", "TOS-77#link", "run-00", sha256_id(link.payload))
    assert "sentinel-tier-2" in link.payload["labels"]
    with pytest.raises(StoreRefused, match="L-DEC-02"):
        S.link_ticket(s, "dec-000001", "TOS-78", by="dave", report_base=BASE, at=T0)


def test_a_hand_linked_key_jira_does_not_know_takes_the_ticket_not_found_path(s):
    finding(s, "F-0")
    confirm(s, "dec-000001", "F-0")
    S.link_ticket(s, "dec-000001", "TOS-404", by="dave", report_base=BASE, at=T0)
    payload = J.link_job("TOS-404", [s.get_decision("dec-000001")], report_base=BASE, linked_by="dave").payload
    got = S.send(J.MemoryJira(), payload)
    assert got.not_found
    S.record(s, payload, got, at=T0)
    d = s.get_decision("dec-000001")
    assert d.expires_at is not None and "ticket not found" in json.dumps(s.get_finding("F-0").evidence)


# -- seen again, mark intended (9.9) --------------------------------------------------------------------------------

def test_seen_again_posts_once_per_ticket_per_24_hours(s):
    finding(s, "F-0")
    d = confirm(s, "dec-000001", "F-0", ticket="TOS-5")
    for run, cand in (("run-01", "b2"), ("run-02", "b2"), ("run-03", "b3"), ("run-04", "b3")):
        start(s, run, cand)
    f1, f2, f3, f4 = (finding(s, f"F-{n}", f"run-0{n}") for n in (1, 2, 3, 4))
    first = S.seen_again(s, d, f1, run_id="run-01", build_id="b2", report_base=BASE, at=T0)
    assert first.destination == "TOS-5#seen_again"
    assert json.loads(first.payload)["comment"] == f"seen again in run run-01 on build b2: {BASE}/f/F-1"
    assert S.seen_again(s, d, f2, run_id="run-02", build_id="b2", report_base=BASE,
                        at="2026-11-11T15:01:00+00:00") is None
    third = S.seen_again(s, d, f3, run_id="run-03", build_id="b3", report_base=BASE, at="2026-11-11T15:02:00+00:00")
    assert third is not None
    s.abandon(third.job_id)                                       # never posted: does not count
    assert S.seen_again(s, d, f4, run_id="run-04", build_id="b3", report_base=BASE,
                        at="2026-11-11T15:03:00+00:00") is not None
    syn = finding(s, "F-syn", "run-04", synthetic=True)
    assert S.seen_again(s, d, syn, run_id="run-04", build_id="b3", report_base=BASE,
                        at="2026-11-13T00:00:00+00:00") is None
    pending = confirm(s, "dec-000009", "F-4", region="rgn.other", run_id="run-04")
    assert S.seen_again(s, pending, f4, run_id="run-04", build_id="b3", report_base=BASE,
                        at="2026-11-13T00:00:00+00:00") is None     # no ticket yet: nothing to comment on


def _intended(s, did, fid, ticket, region, run_id="run-00"):
    d = Decision(did, "mark_intended", "intended", Signature(SWEEP, region, PH, "collateral"), decided_by="erin",
                 ticket=ticket, finding_ref=fid, reason="TOS-12 changes the ladder", decided_at=T0)
    return s.record_decision(d, run_id=run_id)


def test_mark_intended_posts_one_evidence_comment_per_run_per_ticket(s):
    fa, fb = finding(s, "F-a", region="rgn.a"), finding(s, "F-b", region="rgn.b")
    da, db = _intended(s, "dec-000001", "F-a", "TOS-12", "rgn.a"), _intended(s, "dec-000002", "F-b", "TOS-12", "rgn.b")
    first = S.intended_evidence(s, da, [fa], run_id="run-00", report_base=BASE, at=T0, manifest_pr="#77")
    second = S.intended_evidence(s, db, [fb], run_id="run-00", report_base=BASE, at=T0)
    p1, p2 = json.loads(first.payload), json.loads(second.payload)
    assert p1["comment"].startswith("Sentinel evidence for TOS-12 in run run-00") and p2["comment"] is None
    assert p2["labels"] == ["sentinel", "sentinel-dec-dec-000002"]
    start(s, "run-01", "b2")
    f1 = finding(s, "F-c", "run-01", region="rgn.a")
    assert json.loads(S.intended_evidence(s, da, [f1], run_id="run-01", report_base=BASE, at=T0).payload)["comment"]
    jira = J.MemoryJira()
    jira.add("TOS-12")
    for payload in (p1, p2):
        S.record(s, payload, S.send(jira, payload), at=T0)
    assert len(jira.issues["TOS-12"]["comments"]) == 1
    assert {"sentinel", "sentinel-dec-dec-000001", "sentinel-dec-dec-000002"} <= set(jira.issues["TOS-12"]["labels"])
    bug = Decision("dec-000005", "confirm_bug", "confirmed_bug", da.signature, decided_by="erin", ticket="TOS-12",
                   reason="x")
    with pytest.raises(ValueError):
        S.intended_evidence(s, bug, [fa], run_id="run-00", report_base=BASE, at=T0)


def test_an_intended_key_jira_does_not_know_expires_the_decision(s):
    fa = finding(s, "F-a", region="rgn.a")
    da = _intended(s, "dec-000001", "F-a", "TOS-404", "rgn.a")
    q = S.intended_evidence(s, da, [fa], run_id="run-00", report_base=BASE, at=T0)
    payload = json.loads(q.payload)
    got = S.send(J.MemoryJira(), payload)
    S.record(s, payload, got, at="2026-11-10T16:00:00+00:00")             # a Tuesday
    d = s.get_decision("dec-000001")
    assert got.not_found and d.expires_at == "2026-11-11T16:00:00+00:00"
    assert s.get_finding("F-a").triage_state == "open"


# -- SENT tasks and canary Bugs (9.9 rows 4 and 5, 9.4 item 2) -------------------------------------------------------

def _deliver(s, jira, q):
    payload = json.loads(q.payload)
    got = S.send(jira, payload)
    S.record(s, payload, got, at=T0)
    s.mark_sent(q.job_id, at=T0)
    return got


def test_two_env_errors_on_one_gate_make_one_task_and_one_comment(s):
    jira = J.MemoryJira()
    start(s, "run-01", "b2")
    q1 = S.sent_task(s, J.SentEvent("env_error", "fixture_verified", "run-00", detail="fixture hash differs"),
                     report_base=BASE, at=T0)
    assert S.sent_task(s, J.SentEvent("env_error", "fixture_verified", "run-00"), report_base=BASE, at=T0) is None
    q2 = S.sent_task(s, J.SentEvent("env_error", "fixture_verified", "run-01"), report_base=BASE, at=T0)
    kinds = [tuple(r) for r in s.query("SELECT kind, destination FROM outbox ORDER BY job_id")]
    assert kinds == [("jira_create", "SENT#task:env_error:fixture_verified"),
                     ("jira_comment", "SENT#task:env_error:fixture_verified")]
    assert S.jira_queued(s, "run-00") == 1
    assert _deliver(s, jira, q1).created and _deliver(s, jira, q2).commented
    assert list(jira.issues) == ["SENT-1"] and len(jira.issues["SENT-1"]["comments"]) == 1
    (item,) = s.query("SELECT kind, ref, owner, ticket_ref, closed_at FROM maintenance_items")
    assert tuple(item) == ("sent_ticket", "env_error:fixture_verified", "harness_on_call", "SENT-1", None)
    assert S.jira_queued(s, "run-00") == 0
    jira.resolve("SENT-1", by="hank", at="2026-11-11T09:00:00+00:00")       # the poller closes the ledger row
    assert S.poll(jira, s, "2026-11-11T10:00:00+00:00").closed == ("SENT-1",)
    start(s, "run-02", "b3")
    q3 = S.sent_task(s, J.SentEvent("env_error", "fixture_verified", "run-02"), report_base=BASE, at=T0)
    assert s.outbox_job(q3.job_id).kind == "jira_create" and _deliver(s, jira, q3).ticket == "SENT-2"


def test_a_detector_blind_night_queues_a_highest_sent_bug_keyed_by_the_mutant(s):
    e = C.MutantEntry(mutant_id="M04", checkpoint_id=CONFIRM, defect_class="swapped_digit", mode="image",
                      money_cell=True, op="erase", params={}, expected=C.Expected(("assertion_failed",)),
                      owner="sentinel-canaries", region_ref="rgn.x", catalog_version="v1")
    m = C.Mutant(entry=e, checkpoint_id=CONFIRM, seed=C.SEED, base_sha256="b" * 64, mutant_sha256="m" * 64,
                 footprint=(1, 1, 2, 2))
    produced = CheckpointOutcome(run_id="run-00", checkpoint_id=CONFIRM, outcome="identical", workspace_id=WS,
                                 persona="active_trader", data_mode="frozen", synthetic=True)
    rep = C.score([C.MutantResult(mutant=m, produced=produced, absorbed_by="mask msk-1")])
    bugs = I.sent_bug_payload(rep, run_id="run-00")
    (q,) = S.canary_bugs(s, bugs, run_id="run-00", report_base=BASE, at=T0)
    assert S.canary_bugs(s, bugs, run_id="run-00", report_base=BASE, at=T0) == []           # once per run
    assert q.destination == "SENT#task:canary:M04"
    jira = J.MemoryJira()
    got = _deliver(s, jira, q)
    fields = jira.issues[got.ticket]["fields"]
    assert (got.ticket, fields["priority"], fields["issuetype"]) == ("SENT-1", "Highest", "Bug")
    assert J.key_label("canary:M04") in fields["labels"]
    assert tuple(s.query("SELECT ref, ticket_ref FROM maintenance_items")[0]) == ("canary:M04", "SENT-1")


def test_the_reconciliation_names_the_real_task_or_pending(s):
    eid = "tos-48001-1"
    assert S.flow_tasks(s, [eid]) == {}
    q = S.sent_task(s, J.SentEvent("no_covering_checkpoint", eid, "run-00"), report_base=BASE, at=T0)
    assert S.flow_tasks(s, [eid]) == {eid: None}
    assert J.flow_task_phrase(None) == "flow-authoring task pending"
    _deliver(s, J.MemoryJira(), q)
    assert S.flow_tasks(s, [eid]) == {eid: "SENT-1"}
    assert J.flow_task_phrase("SENT-1") == "flow-authoring task SENT-1 opened"


# -- fix verification (9.9, 9.6 Record fixed in build) --------------------------------------------------------------

def _done_ticket(jira, key, decs, cid=SWEEP, **kw):
    blocks = "\n".join("{noformat}\n" + J.identity_block(d, BASE) + "\n{noformat}" for d in decs)
    jira.add(key, labels=J.labels(cid, tier=2, decision_ids=[d.decision_id for d in decs]).labels,
             description=blocks)
    jira.resolve(key, **kw)


def test_a_done_ticket_with_two_decisions_writes_two_fixes_by_the_resolver(s):
    finding(s, "F-a", region="rgn.a")
    finding(s, "F-b", region="rgn.b")
    da = confirm(s, "dec-000001", "F-a", region="rgn.a", ticket="TOS-9")
    db = confirm(s, "dec-000002", "F-b", region="rgn.b", ticket="TOS-9")
    jira = J.MemoryJira()
    _done_ticket(jira, "TOS-9", [da, db], by="rita.resolver", at="2026-11-06T10:00:00+00:00",
                 fix_versions=["TOS 1.42"])
    r = S.poll(jira, s, "2026-11-06T11:00:00+00:00", build_of={"TOS 1.42": "b2"}.get)
    assert [(f.decision_id, f.fixed_id, f.build_id) for f in r.fixed] == [
        ("dec-000001", "dec-000003", "b2"), ("dec-000002", "dec-000004", "b2")]
    rows = s.query("SELECT decision_id, decided_by, source, role, fixed_in_build_id, supersedes_decision_id, "
                   "ticket_ref, decided_at FROM decisions WHERE action = 'fixed_in_build' ORDER BY decision_id")
    assert [tuple(x) for x in rows] == [
        (3, "rita.resolver", "jira_sync", "ticket_resolver", "b2", 1, "TOS-9", "2026-11-06T10:00:00+00:00"),
        (4, "rita.resolver", "jira_sync", "ticket_resolver", "b2", 2, "TOS-9", "2026-11-06T10:00:00+00:00")]
    assert s.get_decision("dec-000001").status == "superseded"
    assert s.get_finding("F-a").triage_state == "fixed_pending_verify"
    assert S.poll(jira, s, "2026-11-06T12:00:00+00:00", build_of={"TOS 1.42": "b2"}.get) == S.PollResult()


def test_a_done_ticket_without_a_build_or_resolver_writes_nothing(s):
    finding(s, "F-a")
    da = confirm(s, "dec-000001", "F-a", ticket="TOS-9")
    jira = J.MemoryJira()
    _done_ticket(jira, "TOS-9", [da], by="rita", at="2026-11-06T10:00:00+00:00", fix_versions=["TOS 9.99"])
    r = S.poll(jira, s, "2026-11-06T11:00:00+00:00")
    assert r.fixed == () and r.skipped[0].why.startswith("Done without a fixVersion or build id")
    t = J.Ticket("TOS-9", tuple(jira.issues["TOS-9"]["labels"]), status_category="done", build_id="b2")
    assert S.sync_ticket(s, t, now="2026-11-06T11:00:00+00:00").skipped[0].why == "Done without a resolver login"
    stranger = J.Ticket("TOS-10", ("sentinel", "sentinel-dec-dec-000777"), status_category="done", build_id="b2",
                        resolver="rita")
    assert S.sync_ticket(s, stranger, now="2026-11-06T11:00:00+00:00").skipped[0].why == "unknown decision"
    assert S.fix_build(s, J.Ticket("TOS-1", fix_versions=("b3", "b2"))) == "b2"            # the earliest build
    assert s.query("SELECT count(*) FROM decisions WHERE action = 'fixed_in_build'")[0][0] == 0


def test_a_returning_signature_reopens_by_transition_or_by_label_and_a_p2(s):
    finding(s, "F-a")
    da = confirm(s, "dec-000001", "F-a", ticket="TOS-9")
    jira = J.MemoryJira(forbid=["TOS-9"])
    _done_ticket(jira, "TOS-9", [da], by="rita", at="2026-11-06T10:00:00+00:00", build_id="b2")
    assert S.poll(jira, s, "2026-11-06T11:00:00+00:00").fixed[0].fixed_id == "dec-000002"
    start(s, "run-02", "b2")
    s.write_checkpoint(CheckpointOutcome(run_id="run-02", checkpoint_id=SWEEP, outcome="collateral", workspace_id=WS,
                                         persona="active_trader", data_mode="frozen"))
    finding(s, "F-2", "run-02")
    (x,) = verify_fixes(s, "run-02", at="2026-11-07T00:00:00+00:00").reopened
    payload = json.loads(x.payload)
    got = S.send(jira, payload, store=s, report_base=BASE)
    assert (got.transitioned, got.alert) == (False, S.REOPEN_FORBIDDEN) and got.alert.priority == P2
    issue = jira.issues["TOS-9"]
    assert J.REOPENED_LABEL in issue["labels"] and issue["status"] == "done"
    assert issue["comments"][-1] == ("Sentinel: the signature fixed by dec-000002 returned in run run-02 on build "
                                     f"b2.\nFindings: {BASE}/f/F-2\nCrop (1:1): {BASE}/a/{CROP}")
    open_jira = J.MemoryJira()
    _done_ticket(open_jira, "TOS-9", [da], by="rita", at="2026-11-06T10:00:00+00:00", build_id="b2")
    ok = S.send(open_jira, payload)
    assert (ok.transitioned, ok.alert, open_jira.issues["TOS-9"]["status"]) == (True, None, "indeterminate")
    assert S.send(open_jira, payload).transitioned and open_jira.calls.count(("transition:Reopen", "TOS-9")) == 1
    # Jira closes it again with a later build: the reopened fix is superseded by a new one (SD-79)
    jira.resolve("TOS-9", by="rita", at="2026-11-09T10:00:00+00:00", build_id="b3")
    again = S.poll(jira, s, "2026-11-09T11:00:00+00:00")
    assert [(f.fixed_id, f.build_id) for f in again.fixed] == [("dec-000003", "b3")]


# -- Run health: label collisions ---------------------------------------------------------------------------------

def test_label_collisions_are_counted_in_run_health(s):
    t = J.Ticket("TOS-2")
    assert S.record_collisions(s, "run-00", SWEEP, [t]) == 1
    assert S.record_collisions(s, "run-00", SWEEP, [t]) == 1                     # the same collision once
    assert S.record_collisions(s, "run-00", CENSUS, [J.Ticket("TOS-3")]) == 2
    summary = s.run_summary("run-00")
    (row,) = [r for r in summary["health_rows"] if r[0] == S.COLLISION_ROW]
    assert row[1].startswith(f"2: TOS-2 (searched for {SWEEP}), TOS-3 (searched for {CENSUS})")
    assert summary["jira_collisions"][0] == {"ticket": "TOS-2", "checkpoint": SWEEP}
    assert hashlib.sha256(SWEEP.encode()).hexdigest()[:12] in J.cp_label(SWEEP)


def test_a_job_about_a_synthetic_finding_is_never_queued(s):
    f = finding(s, "F-syn", synthetic=True)
    assert not f.synthetic                                       # the store's read-back drops the flag
    job = J.auto_file_job(f, ctx())
    assert job is not None and S.enqueue(s, job, run_id="run-00", at=T0) is None
    assert s.query("SELECT count(*) FROM outbox")[0][0] == 0
    real = finding(s, "F-real")
    assert S.enqueue(s, J.auto_file_job(real, ctx()), run_id="run-00", at=T0) is not None
