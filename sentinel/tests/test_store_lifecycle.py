"""The decision lifecycle (4.14, 8.3, 9.2, 9.5, 9.6, 9.7, 9.9): fixed_in_build verification and reopen, expiry,
re-confirmation, ticket not found, revoke, Explorer dispositions, novelty, the roster rules and the export."""
from __future__ import annotations

import dataclasses
import json
from datetime import date

import pytest
import yaml

from sentinel.canonical import sha256_id
from sentinel.records import CheckpointOutcome, Decision, Finding, RunOutcome, Signature
from sentinel.report import build_report
from sentinel.report.inline import plain
from sentinel.store import RunPlan, ScopeEntry, Store, StoreRefused, apply_inheritance
from sentinel.store.lifecycle import (add_business_days, expire_due, expiring_within, export_decisions, first_seen,
                                      hold_explorer_finding, lookup_explorer_dispositions, reconfirm,
                                      suspect_decisions, ticket_not_found, verify_fixes)
from sentinel.store.store import Roster

RUN0 = "run-00"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                          # tier 1
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"    # tier 2
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
TIER3 = "census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader"     # tier 3
REGION = "rgn.ladder.bid-column"
BASE = 0x0123456789ABCDEF
TS = "2026-11-01T12:00:00+00:00"


def ph(bits=()):
    v = BASE
    for b in bits:
        v ^= 1 << b
    return f"{v:016x}"


def scope():
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
            ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
            ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"),
            ScopeEntry(TIER3, "ws.tier-a.swing-trader", "swing_trader", 3, "frozen"))


def plan(run_id, pair_id=None, run_type="pr", pr_author=None, committers=None, started_at=TS):
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope(), pair_id=pair_id, pr_author=pr_author, pr_committers=committers,
                   started_at=started_at)


def builds(s):
    for b, at in (("m0", "2026-10-01T00:00:00+00:00"), ("b1", "2026-11-01T00:00:00+00:00"),
                  ("b2", "2026-11-05T00:00:00+00:00"), ("b3", "2026-11-08T00:00:00+00:00")):
        s.register_build(b, channel="branch", source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                         first_seen_at=at)


def start(s, run_id, candidate, **kw):
    s.begin_run(plan(run_id, pair_id=s.register_pair(candidate, "m0", "pr"), **kw))


def out(cid, outcome, run_id, **kw):
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", **kw)


def finding(s, fid, run_id, cid=SWEEP, phash=None, category="collateral", region=REGION, created_at=TS,
            kind="collateral", synthetic=False, tier=2):
    f = Finding(fid, run_id, kind, "ladder overlaps", run_id, checkpoint_id=cid, tier=tier, region_id=region,
                phash=phash or ph(), category=category, created_at=created_at)
    s.write_finding(f, synthetic=synthetic)
    return f


def decide(s, did, action, *, run_id=RUN0, cid=SWEEP, phash=None, category="collateral", region=REGION, by="alice",
           call=None, **kw):
    verdict = {"confirm_bug": "confirmed_bug", "fixed_in_build": "confirmed_bug", "mark_intended": "intended",
               "approve_baseline": "intended"}.get(action, "noise")
    ticket = kw.pop("ticket", "TOS-1" if action in ("confirm_bug", "mark_intended", "fixed_in_build") else None)
    d = Decision(did, action, verdict, Signature(cid, region, phash or ph(), category), decided_by=by, ticket=ticket,
                 reason="because", decided_at=kw.pop("decided_at", TS), **kw)
    return s.record_decision(d, run_id=run_id, **(call or {}))


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "central.db")
    builds(st)
    yield st
    st.close()


def fixed_bug(s):
    """Run 00 on b1 finds the bug (F-0); confirm_bug, then the Jira poller's fixed_in_build at b2 (9.9)."""
    start(s, RUN0, "b1")
    finding(s, "F-0", RUN0)
    decide(s, "dec-000001", "confirm_bug", finding_ref="F-0", call={"build_id": "b1"})
    decide(s, "dec-000002", "fixed_in_build", by="resolver.r", source="jira_sync", role="ticket_resolver",
           call={"supersedes": "dec-000001", "fixed_in_build_id": "b2"})


# -- fixed_in_build: the lookup, the write rule and the post-run verification (4.14, 8.3, 9.6, 9.9, SD-79) ---------

def test_fixed_in_build_is_a_known_bug_before_its_build_and_returns_from_it(s):
    fixed_bug(s)
    assert s.get_finding("F-0").triage_state == "fixed_pending_verify"        # the superseded row's finding
    sig = Signature(SWEEP, REGION, ph((1,)), "collateral")
    before = s.lookup_decisions(sig, candidate_build="b1")
    assert before.inherited.decision_id == "dec-000002" and before.fixed_returned == ()
    for build in ("b2", "b3"):
        lk = s.lookup_decisions(sig, candidate_build=build)
        assert [m.decision.decision_id for m in lk.fixed_returned] == ["dec-000002"]
        assert lk.applicable == () and lk.inherited is None and not lk.conflict
        computed = out(SWEEP, "collateral", RUN0)
        assert apply_inheritance(computed, lk) == computed                     # no decision: Unexpected
    assert s.lookup_decisions(sig, candidate_build="b-unknown").inherited is not None   # cannot order: a known bug
    start(s, "run-02", "b2")
    assert s.lookup_decisions(sig, run_id="run-02").fixed_returned              # from the run's build pair


def test_fixed_in_build_supersedes_the_active_confirm_bug_on_its_signature(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "confirm_bug", call={"build_id": "b1"})
    decide(s, "dec-000002", "noise_signature", region="rgn.other")
    fix = {"fixed_in_build_id": "b2"}
    with pytest.raises(StoreRefused, match="SD-79"):
        decide(s, "dec-000003", "fixed_in_build", call=fix)
    with pytest.raises(StoreRefused, match="SD-79"):
        decide(s, "dec-000003", "fixed_in_build", region="rgn.other", call={**fix, "supersedes": "dec-000002"})
    with pytest.raises(StoreRefused, match="SD-79"):                            # another signature
        decide(s, "dec-000003", "fixed_in_build", region="rgn.other", call={**fix, "supersedes": "dec-000001"})
    with pytest.raises(StoreRefused, match="SD-79"):                            # pHash beyond the tolerance
        decide(s, "dec-000003", "fixed_in_build", phash=ph(range(20)), call={**fix, "supersedes": "dec-000001"})
    assert decide(s, "dec-000003", "fixed_in_build", call={**fix, "supersedes": "dec-000001"}).status == "active"


def test_a_later_fix_supersedes_the_reopened_one_and_lifecycle_rows_never_freeze(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "confirm_bug", call={"build_id": "b1"}, decided_at="2026-11-01T00:00:00+00:00")
    decide(s, "dec-000002", "fixed_in_build", call={"supersedes": "dec-000001", "fixed_in_build_id": "b2"},
           decided_at="2026-11-02T00:00:00+00:00")
    with pytest.raises(StoreRefused, match="L-DEC-08"):                         # not newer than the first fix
        decide(s, "dec-000003", "fixed_in_build", call={"supersedes": "dec-000002", "fixed_in_build_id": "b1"})
    decide(s, "dec-000003", "fixed_in_build", call={"supersedes": "dec-000002", "fixed_in_build_id": "b3"},
           decided_at="2026-11-03T00:00:00+00:00")
    lk = s.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"), candidate_build="b2")
    assert not lk.frozen and lk.inherited.decision_id == "dec-000003"          # b2 is before the new fix


def test_a_clean_run_on_the_fix_build_closes_the_finding(s):
    fixed_bug(s)
    start(s, "run-02", "b2")
    s.write_checkpoint(out(SWEEP, "identical", "run-02"))
    r = verify_fixes(s, "run-02", at="2026-11-06T00:00:00+00:00")
    assert r.candidate_build == "b2" and r.closed == ("dec-000002",) and r.reopened == ()
    f = s.get_finding("F-0")
    assert (f.triage_state, f.closed_at) == ("closed", "2026-11-06T00:00:00+00:00")
    assert s.get_decision("dec-000002").status == "active"                    # still asserting the signature
    assert verify_fixes(s, "run-02").closed == ()                              # once


def test_a_returning_signature_reopens_the_decision_finding_and_ticket(s):
    fixed_bug(s)
    start(s, "run-02", "b2")
    s.write_checkpoint(out(SWEEP, "collateral", "run-02"))
    finding(s, "F-2", "run-02", phash=ph((2, 3)))
    r = verify_fixes(s, "run-02", at="2026-11-06T00:00:00+00:00")
    assert r.closed == () and [x.decision_id for x in r.reopened] == ["dec-000002"]
    x = r.reopened[0]
    assert (x.findings, x.reopen_count, x.suspect) == (("F-2",), 1, False)
    d = s.get_decision("dec-000002")
    assert (d.status, d.reopen_count) == ("reopened", 1)
    assert s.get_finding("F-0").triage_state == "open"
    job = s.outbox_job(x.job_id)
    assert (job.kind, job.destination, job.run_id, job.state) == ("jira_transition", "TOS-1", "run-02", "queued")
    assert job.payload_sha256 == sha256_id(json.loads(x.payload))
    assert json.loads(x.payload)["build"] == "b2"
    assert verify_fixes(s, "run-02").reopened == ()                            # the same run reopens once
    assert s.get_decision("dec-000002").reopen_count == 1
    lk = s.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"), candidate_build="b3")
    assert lk.inherited is None                                                # reopened: no suppression
    start(s, "run-03", "b3")                                                   # the next run: back again
    s.write_checkpoint(out(SWEEP, "collateral", "run-03"))
    finding(s, "F-3", "run-03")
    again = verify_fixes(s, "run-03").reopened[0]
    assert (again.reopen_count, again.suspect) == (2, True)
    assert [d.decision_id for d in suspect_decisions(s)] == ["dec-000002"]


def test_an_older_build_or_an_uncompared_checkpoint_asserts_nothing(s):
    fixed_bug(s)
    start(s, "run-01", "b1")                                                   # before the fix build
    s.write_checkpoint(out(SWEEP, "identical", "run-01"))
    early = verify_fixes(s, "run-01")
    assert (early.candidate_build, early.closed, early.reopened, early.not_compared) == ("b1", (), (), ())
    start(s, "run-02", "b3")
    s.write_checkpoint(out(SWEEP, "not_run", "run-02", reason="quarantine"))
    r = verify_fixes(s, "run-02")
    assert (r.closed, r.reopened, r.not_compared) == ((), (), ("dec-000002",))
    assert s.get_finding("F-0").triage_state == "fixed_pending_verify"
    s.begin_run(plan("run-n", run_type="nightly"))                             # a Nightly run with no build pair
    assert verify_fixes(s, "run-n").candidate_build is None
    assert verify_fixes(s, "run-n", candidate_build="b2").not_compared == ("dec-000002",)


# -- revoke (9.6) ---------------------------------------------------------------------------------------------------

def test_revoke_reopens_the_finding(s):
    start(s, RUN0, "b1")
    finding(s, "F-1", RUN0)
    decide(s, "dec-000001", "confirm_bug", finding_ref="F-1")
    assert s.get_finding("F-1").triage_state == "confirmed_bug"
    s.revoke_decision("dec-000001", by="bob", reason="wrong signature")
    assert s.get_finding("F-1").triage_state == "open"


# -- expiry (9.6, 9.7 "Ends"), the Decisions page lists (9.5) --------------------------------------------------------

AT = "2026-11-10T00:00:00+00:00"


def test_expiring_rows_expire_and_noise_goes_to_the_aa_reevaluation(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "noise_signature", decided_at="2026-10-01T00:00:00+00:00")       # 31 days ago + 9
    decide(s, "dec-000002", "mark_intended", category="invariant_violation", region="rgn.waiver",
           expires_at="2026-11-05T00:00:00+00:00")
    decide(s, "dec-000003", "noise_signature", region="rgn.fresh", decided_at="2026-11-01T00:00:00+00:00")
    r = expire_due(s, AT)
    assert [(e.decision_id, e.why) for e in r.expired] == [("dec-000001", "expired"), ("dec-000002", "expired")]
    assert r.reevaluate == ("dec-000001",)                                     # noise, not the waiver
    assert [s.get_decision(f"dec-00000{i}").status for i in (1, 2, 3)] == ["expired", "expired", "active"]
    assert expire_due(s, AT).expired == ()                                     # idempotent


def test_dispositions_end_after_30_days_without_their_signature(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "agent_mistake", region="rgn.a", source="explorer_triage",
           decided_at="2026-10-01T00:00:00+00:00")
    decide(s, "dec-000002", "expected_behavior", region="rgn.b", source="explorer_triage",
           decided_at="2026-10-01T00:00:00+00:00")
    finding(s, "F-b", RUN0, region="rgn.b", phash=ph((5,)), created_at="2026-10-25T00:00:00+00:00")
    finding(s, "F-a", RUN0, region="rgn.a", phash=ph(range(30)), created_at="2026-11-05T00:00:00+00:00")  # far
    r = expire_due(s, AT)
    assert [(e.decision_id, e.why) for e in r.expired] == [("dec-000001", "signature absent 30 days")]
    assert expire_due(s, "2026-11-25T00:00:00+00:00").expired[0].decision_id == "dec-000002"


def test_mark_intended_ends_at_baseline_approval_or_when_its_ticket_leaves_the_manifests(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "mark_intended", cid=FLOW, decided_at="2026-11-01T00:00:00+00:00")
    decide(s, "dec-000002", "mark_intended", region="rgn.c", ticket="TOS-7")
    s.insert("baselines", {"checkpoint_id": FLOW, "lineage": "released", "build_id": "b1", "image_sha256": "a" * 64,
                           "state": "approved", "source_run_id": RUN0, "proposed_at": TS, "approved_by": "fay",
                           "approved_at": "2026-11-05T00:00:00+00:00", "approval_reason": "the new header"})
    assert [e.decision_id for e in expire_due(s, AT).expired] == ["dec-000001"]
    r = expire_due(s, AT, manifest_tickets=["TOS-1"])
    assert [(e.decision_id, e.why) for e in r.expired] == [("dec-000002", "ticket left every manifest in scope")]


def test_expiring_within_seven_days_for_the_decisions_page(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "noise_signature", decided_at="2026-10-15T00:00:00+00:00")       # ends 11-14
    decide(s, "dec-000002", "noise_signature", region="rgn.x", decided_at="2026-11-01T00:00:00+00:00")  # 12-01
    assert [d.decision_id for d in expiring_within(s, AT)] == ["dec-000001"]
    assert [d.decision_id for d in expiring_within(s, AT, days=30)] == ["dec-000001", "dec-000002"]


# -- re-confirmation (9.5, 9.7) -------------------------------------------------------------------------------------

def test_reconfirm_adds_30_days_through_a_superseding_row_that_never_freezes_or_contests(s):
    start(s, RUN0, "b1")
    finding(s, "F-1", RUN0)
    decide(s, "dec-000001", "noise_signature", finding_ref="F-1", decided_at="2026-11-01T00:00:00+00:00")
    new = reconfirm(s, "dec-000001", by="bob", at="2026-11-28T00:00:00+00:00")
    assert (new.decision_id, new.decided_by, new.supersedes) == ("dec-000002", "bob", "dec-000001")
    assert new.expires_at == "2026-12-31T00:00:00+00:00"                       # 60 days after the first
    assert s.get_decision("dec-000001").status == "superseded"
    newer = reconfirm(s, "dec-000002", by="alice", at="2026-12-28T00:00:00+00:00")   # 30 days later
    assert newer.expires_at == "2027-01-30T00:00:00+00:00"
    lk = s.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"), at="2027-01-10T00:00:00+00:00")
    assert not lk.frozen and lk.inherited.decision_id == newer.decision_id
    assert not s.get_finding("F-1").contested
    with pytest.raises(StoreRefused, match="9.7"):
        reconfirm(s, "dec-000001", by="bob")                                   # superseded
    with pytest.raises(StoreRefused, match="9.7"):
        reconfirm(s, newer.decision_id, by="bob", at="2027-02-01T00:00:00+00:00")   # already expired
    decide(s, "dec-000009", "confirm_bug", region="rgn.bug")
    with pytest.raises(StoreRefused, match="noise_signature"):
        reconfirm(s, "dec-000009", by="bob")


def test_plain_supersessions_still_freeze(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "noise_signature", decided_at="2026-11-01T00:00:00+00:00")
    decide(s, "dec-000002", "noise_signature", by="bob", call={"supersedes": "dec-000001"},
           decided_at="2026-11-05T00:00:00+00:00")
    decide(s, "dec-000003", "noise_signature", by="carl", call={"supersedes": "dec-000002"},
           decided_at="2026-11-07T00:00:00+00:00")
    assert s.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"), at=AT).frozen


# -- ticket not found (9.9) -----------------------------------------------------------------------------------------

def test_ticket_not_found_expires_the_decision_next_business_day_and_requeues_the_finding(s):
    start(s, RUN0, "b1")
    f = finding(s, "F-1", RUN0)
    decide(s, "dec-000001", "mark_intended", finding_ref="F-1", ticket="TOS-404")
    friday = "2026-11-13T20:00:00+00:00"
    r = ticket_not_found(s, "dec-000001", at=friday)
    assert r.expires_at == "2026-11-16T20:00:00+00:00"                         # Monday, same time
    assert s.get_decision("dec-000001").expires_at == r.expires_at
    got = s.get_finding("F-1")
    assert got.triage_state == "open" and got.evidence["triage_note"] == "ticket not found: TOS-404"
    job = s.outbox_job(r.job_id)
    assert (job.kind, job.destination) == ("slack", "triage")
    assert json.loads(r.payload)["notify"] == "alice"
    assert ticket_not_found(s, "dec-000001", at=friday).job_id is None        # one notice
    s.write_finding(f)                                                         # a replay ignores the triage note
    assert [e.decision_id for e in expire_due(s, "2026-11-16T21:00:00+00:00").expired] == ["dec-000001"]
    with pytest.raises(StoreRefused, match="9.9"):
        ticket_not_found(s, "dec-000001", at=friday)                          # expired now


def test_business_days_skip_weekends_and_holidays():
    assert add_business_days("2026-11-13T20:00:00+00:00", 1, [date(2026, 11, 16)]) == "2026-11-17T20:00:00+00:00"
    assert add_business_days("2026-11-11T09:00:00-05:00", 1) == "2026-11-12T14:00:00+00:00"
    assert add_business_days("2026-11-14T09:00:00+00:00", 1) == "2026-11-16T09:00:00+00:00"   # from a Saturday


# -- Explorer dispositions (9.6, 9.7) -------------------------------------------------------------------------------

def test_agent_mistake_holds_a_later_explorer_finding_and_discards_its_flow_until_revoked(s):
    start(s, RUN0, "b1")
    finding(s, "F-1", RUN0, kind="explorer_candidate", category="invariant_violation")
    decide(s, "dec-000001", "agent_mistake", category="invariant_violation", source="explorer_triage",
           finding_ref="F-1")
    assert s.get_finding("F-1").triage_state == "agent_mistake"
    sig = Signature(SWEEP, REGION, ph((4,)), "invariant_violation")
    assert s.lookup_decisions(sig).matches == ()                              # no checkpoint outcome involved
    disp = lookup_explorer_dispositions(s, sig)
    assert disp.held and not disp.keep_flow and disp.decision.decision_id == "dec-000001"
    start(s, "run-02", "b2")
    later = Finding("F-2", "run-02", "explorer_candidate", "overlap", "run-02", checkpoint_id=SWEEP, tier=2,
                    region_id=REGION, phash=ph((4,)), category="invariant_violation")
    held = hold_explorer_finding(later, disp)
    assert (held.held, held.triage_state, held.decision_ref) == (True, "agent_mistake", "dec-000001")
    assert held.evidence["explorer_disposition"] == {"decision": "dec-000001", "candidate_flow": "discard"}
    s.write_finding(held)
    assert s.get_finding("F-2").held
    assert not lookup_explorer_dispositions(s, Signature(SWEEP, REGION, ph(range(20)), "invariant_violation")).held
    s.revoke_decision("dec-000001", by="tess", reason="it was a real bug")
    after = lookup_explorer_dispositions(s, sig)
    assert not after.held and hold_explorer_finding(later, after) == later


def test_expected_behavior_holds_the_finding_and_keeps_the_flow(s):
    start(s, RUN0, "b1")
    decide(s, "dec-000001", "expected_behavior", category="assertion_failed", source="explorer_triage")
    disp = lookup_explorer_dispositions(s, Signature(SWEEP, REGION, ph(), "assertion_failed"))
    later = Finding("F-2", RUN0, "explorer_candidate", "x", RUN0, checkpoint_id=SWEEP, region_id=REGION, phash=ph(),
                    category="assertion_failed")
    held = hold_explorer_finding(later, disp)
    assert disp.keep_flow and held.triage_state == "expected_behavior"
    assert held.evidence["explorer_disposition"]["candidate_flow"] == "keep"


# -- novelty (9.2) --------------------------------------------------------------------------------------------------

def test_first_seen_is_the_earliest_sighting_within_30_days(s):
    for run_id in ("run-a", "run-b", "run-now"):
        start(s, run_id, "b1")
    now = "2026-11-10T00:00:00+00:00"
    finding(s, "F-a", "run-a", phash=ph((1,)), created_at="2026-11-05T00:00:00+00:00")          # 5 days ago
    finding(s, "F-old", "run-b", region="rgn.old", created_at="2026-10-09T00:00:00+00:00")     # 32 days ago
    finding(s, "F-syn", "run-b", region="rgn.syn", created_at="2026-11-05T00:00:00+00:00", synthetic=True)
    finding(s, "F-t1", "run-b", cid=CENSUS, tier=1, created_at="2026-11-05T00:00:00+00:00")
    new = lambda fid, **kw: Finding(fid, "run-now", "collateral", "x", "run-now",  # noqa: E731
                                    **{"checkpoint_id": SWEEP, "tier": 2, "region_id": REGION, "phash": ph(),
                                       "category": "collateral", "created_at": now, **kw})
    assert first_seen(s, new("F-1")) == "run-a"
    assert first_seen(s, new("F-2", region_id="rgn.old")) == "run-now"         # unseen in 30 days: novel
    assert first_seen(s, new("F-3", region_id="rgn.syn")) == "run-now"         # synthetic rows do not count
    assert first_seen(s, new("F-4", category="invariant_violation")) == "run-now"
    assert first_seen(s, new("F-5", phash=ph(range(7)))) == "run-a"            # within 8 bits on tier 2
    assert first_seen(s, new("F-6", checkpoint_id=CENSUS, tier=1, phash=ph(range(7)))) == "run-now"   # 6 on tier 1
    assert first_seen(s, new("F-7", region_id=None)) == "run-now"


def test_a_signature_seen_five_days_ago_ranks_after_a_new_one(s):
    # 9.2: two otherwise-equal tier-1 collateral clusters; the store's first_seen makes the new one rank first,
    # although its checkpoint id sorts last.
    from test_report_sample import RUN_ID, sample

    rs = sample()
    rs["outcomes"], rs["findings"], rs["decisions"] = [], [], []
    start(s, "run-a", "b1")
    start(s, RUN_ID, "b2")
    cids = {n: f"census/charts.{n}/ws.tier-a.active-trader" for n in ("alpha", "zeta")}
    s.register_checkpoints([ScopeEntry(c, WS, "active_trader", 1, "frozen") for c in cids.values()],
                           config_commit="c0ffee")
    finding(s, "F-seen", "run-a", cid=cids["alpha"], tier=1, region="rgn.alpha",
            created_at="2026-11-04T00:00:00+00:00")
    for name, cid in cids.items():
        f = Finding(f"F-{name}", RUN_ID, "collateral", "x", RUN_ID, checkpoint_id=cid, tier=1,
                    region_id=f"rgn.{name}", phash=ph(), category="collateral", created_at="2026-11-09T00:00:00+00:00")
        rs["findings"].append(dataclasses.replace(f, first_seen_run_id=first_seen(s, f)))
        rs["outcomes"].append(CheckpointOutcome(run_id=RUN_ID, checkpoint_id=cid, outcome="collateral",
                                                workspace_id=WS, persona="active_trader", data_mode="frozen", tier=1))
    assert [f.first_seen_run_id for f in rs["findings"]] == ["run-a", RUN_ID]
    order = [plain(i.header) for i in build_report(**rs).unexpected]
    assert len(order) == 2 and "charts.zeta" in order[0] and "charts.alpha" in order[1]


# -- the roster: L-DEC-01, L-DEC-06 and L-DEC-05 (4.14, 9.6, 9.12, SD-74) ------------------------------------------

ONCALL = {"schema_version": 1,
          "roles": [{"role": "system_owner", "person": "olga", "deputy": "sam"},
                    {"role": "risk_owner", "person": "rita", "deputy": "rob"}],
          "flow_owners": [{"tier": 1, "person": "fay", "deputy": "fred"},
                          {"tier": 2, "person": "gus", "deputy": "gina"}],
          "rotations": [{"rotation": "triage", "person": "tess", "from": "2026-11-09", "to": "2026-11-15"},
                        {"rotation": "harness_oncall", "person": "hank", "from": "2026-11-09", "to": "2026-11-15"}],
          "triage_roster": ["alice", "bob"]}


def test_roster_from_oncall():
    r = Roster.from_oncall(ONCALL)
    assert r.deciders() == {"alice", "bob", "tess", "fay", "fred", "gus", "gina", "olga", "sam"}
    assert r.confirmers(1) == {"alice", "bob", "tess", "fay", "fred"}


def test_l_dec_01_refuses_a_decider_outside_the_roster(tmp_path):
    with Store(tmp_path / "c.db", roster=Roster.from_oncall(ONCALL)) as st:
        builds(st)
        start(st, RUN0, "b1", pr_author="jdoe", committers=("jdoe", "kim"))
        for who in ("mallory", "rita", "hank"):
            with pytest.raises(StoreRefused, match="L-DEC-01"):
                decide(st, "dec-000001", "noise_signature", by=who)
        assert decide(st, "dec-000001", "noise_signature", by="alice").status == "active"
        assert decide(st, "dec-000002", "confirm_bug", by="kim", region="rgn.k").status == "active"   # a committer
        assert decide(st, "dec-000003", "mark_intended", by="jdoe", cid=TIER3).status == "active"  # tier 3 alone
        with pytest.raises(StoreRefused, match="L-DEC-01"):
            decide(st, "dec-000004", "request_mask", by="jdoe", region="rgn.m")
        decide(st, "dec-000005", "confirm_bug", by="alice", region="rgn.f", call={"build_id": "b1"})
        fix = {"supersedes": "dec-000005", "fixed_in_build_id": "b2"}
        with pytest.raises(StoreRefused, match="L-DEC-01"):
            decide(st, "dec-000006", "fixed_in_build", by="resolver.r", region="rgn.f", call=fix)
        assert decide(st, "dec-000006", "fixed_in_build", by="resolver.r", region="rgn.f", source="jira_sync",
                      call=fix).source == "jira_sync"                          # the Jira ticket's resolver (9.9)
        st.set_roster(None)
        assert decide(st, "dec-000007", "noise_signature", by="mallory", region="rgn.z").status == "active"


def test_l_dec_06_the_pr_authors_intended_is_a_proposal_until_a_rotation_member_confirms(tmp_path):
    with Store(tmp_path / "c.db", roster=Roster.from_oncall(ONCALL)) as st:
        builds(st)
        start(st, RUN0, "b1", pr_author="jdoe", committers=("jdoe", "kim"))
        for who in ("jdoe", "kim"):
            with pytest.raises(StoreRefused, match="L-DEC-06"):
                decide(st, "dec-000001", "mark_intended", by=who, cid=CENSUS)
        with pytest.raises(StoreRefused, match="L-DEC-06"):                    # the confirmer is the decider
            decide(st, "dec-000001", "mark_intended", by="tess", cid=CENSUS,
                   call={"proposed_by": "jdoe", "confirmed_by": "alice"})
        with pytest.raises(StoreRefused, match="L-DEC-06"):                    # tier-2 flow owner on tier 1
            decide(st, "dec-000001", "mark_intended", by="gus", cid=CENSUS,
                   call={"proposed_by": "jdoe", "confirmed_by": "gus"})
        d = decide(st, "dec-000001", "mark_intended", by="tess", cid=CENSUS,
                   call={"proposed_by": "jdoe", "confirmed_by": "tess"})
        assert (d.decided_by, d.confirmed_by) == ("tess", "tess")
        assert st.query("SELECT proposed_by FROM decisions WHERE decision_id = 1")[0][0] == "jdoe"
        assert decide(st, "dec-000002", "mark_intended", by="gus", call={"proposed_by": "kim", "confirmed_by": "gus"}
                      ).status == "active"                                    # the tier-2 flow owner on tier 2


def test_l_dec_05_the_author_of_the_merged_change_never_approves_a_tier_1_baseline(tmp_path):
    with Store(tmp_path / "c.db") as st:
        builds(st)
        st.begin_run(plan("run-m", pair_id=st.register_pair("b1", "m0", "merge"), run_type="merge",
                          committers=("kim",)))
        for cid in (CENSUS, SWEEP, FLOW, TIER3):
            st.write_checkpoint(out(cid, "identical", "run-m"))
        st.end_run(RunOutcome("run-m", "merge", "complete", "reported"))
        for who, authors in (("jdoe", ["jdoe"]), ("kim", [])):
            with pytest.raises(StoreRefused, match="L-DEC-05"):
                decide(st, "dec-000001", "approve_baseline", run_id="run-m", cid=CENSUS, by=who,
                       call={"baseline_id": 1, "change_authors": authors})
        assert decide(st, "dec-000001", "approve_baseline", run_id="run-m", cid=CENSUS, by="fay",
                      call={"baseline_id": 1, "change_authors": ["jdoe"]}).status == "active"


# -- the nightly export (4.14 intro) --------------------------------------------------------------------------------

def test_export_writes_one_valid_yaml_per_decision(s, tmp_path):
    start(s, RUN0, "b1")
    finding(s, "fnd-1", RUN0)
    decide(s, "dec-000001", "confirm_bug", finding_ref="fnd-1", role="triage_rotation", bbox_q4=(12, 388, 208, 20),
           call={"build_id": "b1"})
    decide(s, "dec-000002", "fixed_in_build", role="ticket_resolver", source="jira_sync", by="resolver.r",
           call={"supersedes": "dec-000001", "fixed_in_build_id": "b2", "build_id": "b1"})
    decide(s, "dec-000003", "noise_signature", region="rgn.n")                 # no finding, build or role
    s.revoke_decision("dec-000003", by="bob", reason="wrong", at="2026-11-02T00:00:00+00:00")
    r = export_decisions(s, tmp_path / "decisions")
    assert [p.name for p in r.paths] == ["dec-000001.yaml", "dec-000002.yaml", "dec-000003.yaml"]
    first = yaml.safe_load(r.paths[0].read_text())
    assert first["signature"] == {"checkpoint_id": SWEEP, "region_id": REGION, "phash": ph(), "hamming_tolerance": 8,
                                  "bbox_q4": {"x": 12, "y": 388, "w": 208, "h": 20}, "category": "collateral"}
    assert (first["status"], first["finding_ref"], first["build_id"], first["ticket"]) == (
        "superseded", "fnd-1", "b1", "TOS-1")
    second = yaml.safe_load(r.paths[1].read_text())
    assert (second["supersedes"], second["fixed_in_build"], second["source"]) == ("dec-000001", "b2", "jira_sync")
    assert second["finding_ref"] == "fnd-1"                                    # taken from the confirm_bug row
    third = yaml.safe_load(r.paths[2].read_text())
    assert third["revoked"] == {"by": "bob", "at": "2026-11-02T00:00:00+00:00", "reason": "wrong"}
    assert set(r.invalid) == {"dec-000003"}
    assert any("finding_ref" in e for e in r.invalid["dec-000003"])
