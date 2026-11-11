"""Triage SLA clocks, the business-hours calendar, assignment and escalation (9.12, 9.10 SLA escalation)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from sentinel import sla
from sentinel.orchestrator import alerts
from sentinel.records import Finding, Proposal
from sentinel.store import RunPlan, ScopeEntry, Store

NY = ZoneInfo("America/New_York")
WS = "ws.tier-a.active-trader"
T1 = f"census/trade.active-trader.ladder/{WS}"
T2 = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
ONCALL = yaml.safe_load("""
schema_version: 1
roles:
  - {role: system_owner, person: asmith, deputy: bkhan}
  - {role: release_manager, person: glee, deputy: cliu}
flow_owners:
  - {tier: 1, person: tos-trade-ui, deputy: pchen}
  - {tier: 2, person: tos-trade-ui, deputy: pchen}
rotations:
  - {rotation: triage, person: triage.lead, from: 2026-11-09, to: 2026-11-15}
  - {rotation: triage, person: triage.next, from: 2026-11-16, to: 2026-11-22}
  - {rotation: harness_oncall, person: bkhan, from: 2026-11-09, to: 2026-11-22}
""")


def ny(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


FRI_17 = ny(2026, 11, 6, 17)          # a Friday after the clocks went back (EST)


def iso(at):
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_build("m0", channel="main", source_commit="m0", pipeline_artifact_url="u", installer_sha256="m0",
                     first_seen_at="2026-10-01T00:00:00+00:00")
    s.register_build("b1", channel="branch", source_commit="b1", pipeline_artifact_url="u", installer_sha256="b1",
                     first_seen_at="2026-11-01T00:00:00+00:00")
    yield s
    s.close()


def run(s, run_id, run_type="merge", started=FRI_17):
    kind = run_type if run_type in ("pr", "merge", "rc") else "merge"
    pair = s.register_pair("b1", "m0", kind)
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, pair_id=pair, started_at=iso(started - timedelta(hours=1)),
                        scope=(ScopeEntry(T1, WS, "active_trader", 1, "frozen"),
                               ScopeEntry(T2, WS, "active_trader", 2, "frozen"))))


def finding(s, fid, run_id, *, tier=1, created=FRI_17, cid=None):
    s.write_finding(Finding(fid, run_id, "collateral", "ladder viewport 6 px higher", run_id,
                            checkpoint_id=cid or (T1 if tier == 1 else T2), tier=tier, region_id="rgn.ladder.viewport",
                            phash="0123456789abcdef", category="collateral", created_at=iso(created)))


# -- the business clock (choice 1) -----------------------------------------------------------------------------------

def test_one_business_day_after_friday_17_is_monday_17_and_four_hours_is_monday_13():
    c = sla.BusinessClock()
    assert c.add_days(FRI_17, 1) == ny(2026, 11, 9, 17)
    assert c.add(FRI_17, timedelta(hours=4)) == ny(2026, 11, 9, 13)
    assert c.add(ny(2026, 11, 7, 10), timedelta(hours=1)) == ny(2026, 11, 9, 10)     # Saturday starts Monday 09:00
    assert c.elapsed(FRI_17, ny(2026, 11, 9, 12, 30)) == timedelta(hours=3, minutes=30)
    assert c.elapsed(ny(2026, 11, 9, 12), ny(2026, 11, 9, 11)) == timedelta(0)


def test_exchange_and_firm_holidays_are_not_business_days():
    c = sla.BusinessClock()
    assert c.add_days(ny(2026, 11, 25, 17), 1) == ny(2026, 11, 27, 17)                 # Thanksgiving skipped
    firm = sla.BusinessClock(holidays=frozenset({date(2026, 11, 9)}))
    assert firm.add_days(FRI_17, 1) == ny(2026, 11, 10, 17)
    assert c.next_day_at(FRI_17, time(10)) == ny(2026, 11, 9, 10)                      # "10:00 the next business day"
    assert sla.business_age(c, FRI_17, ny(2026, 11, 10, 12)) == "1d 03h"
    assert sla.business_age(c, FRI_17, ny(2026, 11, 9, 11, 30)) == "2h 30m"


# -- the SLA table (choice 2) ---------------------------------------------------------------------------------------

def test_due_follows_the_912_table():
    due = sla.due
    assert due(sla.SlaItem("finding", "F-1", FRI_17, 1, "merge")) == ny(2026, 11, 9, 17)
    assert due(sla.SlaItem("finding", "F-1", FRI_17, 1, "pr")) is None                  # before merge
    assert due(sla.SlaItem("finding", "F-1", FRI_17, 2, "pr")) == ny(2026, 11, 9, 17)
    assert due(sla.SlaItem("proposal", "3", ny(2026, 11, 9, 15), 1, "pr")) == ny(2026, 11, 10, 11)
    assert due(sla.SlaItem("expired_mask", "msk.a", FRI_17)) == ny(2026, 11, 13, 17)
    assert due(sla.SlaItem("decision_without_ticket", "dec-000001", FRI_17)) == ny(2026, 11, 9, 17)
    assert due(sla.SlaItem("finding", "F-1", FRI_17, 1, "rc")) is None
    assert due(sla.SlaItem("finding", "F-1", FRI_17, 1, "rc", deadline=ny(2026, 11, 20, 12))) == ny(2026, 11, 20, 12)
    with pytest.raises(ValueError):
        sla.SlaItem("alert", "x", FRI_17)


def test_the_roster_names_the_person_behind_each_recipient():
    oc = sla.OnCall.from_doc(ONCALL)
    assert oc.person("system_owner", date(2026, 11, 10)) == "asmith"
    assert oc.person("harness_on_call", date(2026, 11, 10)) == "bkhan"
    assert oc.person("triage_rotation", date(2026, 11, 17)) == "triage.next"
    assert oc.person("flow_owner", date(2026, 11, 10), tier=1) == "tos-trade-ui"
    assert oc.person("on_call_channel", date(2026, 11, 10)) is None
    assert oc.rotation_member("triage", date(2026, 12, 1)) is None


# -- escalation (9.12 steps; choices 3 and 4) ----------------------------------------------------------------------

def test_a_tier1_merge_finding_escalates_step_by_step_once_each(store):
    oc = sla.OnCall.from_doc(ONCALL)
    run(store, "run-m1")
    finding(store, "F-1", "run-m1")
    assert sla.escalate(store, ny(2026, 11, 9, 12, 59), oncall=oc).assigned == ()
    r1 = sla.escalate(store, ny(2026, 11, 9, 13), oncall=oc)                   # unclaimed 4 business hours
    assert r1.assigned == (sla.Assignment("F-1", "triage.lead"),) and r1.alerts == ()
    row = store.query("SELECT assignee, assigned_by, escalation_step, sla_due_at FROM triage_assignments")[0]
    assert tuple(row) == ("triage.lead", "system", 1, iso(ny(2026, 11, 9, 17)))
    assert sla.escalate(store, ny(2026, 11, 10, 16, 59), oncall=oc).alerts == ()
    r2 = sla.escalate(store, ny(2026, 11, 10, 17), oncall=oc)                  # one business day past SLA
    (a,) = r2.alerts
    assert a.step == 2 and a.route == alerts.sla_escalation() and a.route.priority == "P2"
    assert a.people == {"system_owner": "asmith"} and "triage.lead" in a.detail and a.key == "sla:F-1:step2"
    assert sla.escalate(store, ny(2026, 11, 11, 9), oncall=oc).alerts == ()    # a step fires once
    (b,) = sla.escalate(store, ny(2026, 11, 12, 17), oncall=oc).alerts         # three business days
    assert b.step == 3 and b.people == {"system_owner": "asmith", "flow_owner": "tos-trade-ui"}
    r4 = sla.escalate(store, ny(2026, 11, 16, 17), oncall=oc)                  # five business days
    assert r4.alerts == () and r4.breaches == ("F-1",) and r4.merge_digest_head == ("F-1",)
    assert [i.finding_id for i in sla.breaches(store, ny(2026, 11, 16, 17))] == ["F-1"]


def test_steps_crossed_in_one_call_raise_the_step3_alert_and_the_breach(store):
    oc = sla.OnCall.from_doc(ONCALL)
    run(store, "run-m1")
    finding(store, "F-1", "run-m1")
    r = sla.escalate(store, ny(2026, 11, 16, 17), oncall=oc)
    assert r.assigned == (sla.Assignment("F-1", "triage.next"),)
    assert [a.step for a in r.alerts] == [3] and r.breaches == ("F-1",)


def test_tier2_is_digest_only_at_step2_and_heads_the_merge_digest_at_step3(store):
    oc = sla.OnCall.from_doc(ONCALL)
    run(store, "run-m1")
    finding(store, "F-2", "run-m1", tier=2)
    sla.escalate(store, ny(2026, 11, 9, 13), oncall=oc)
    r2 = sla.escalate(store, ny(2026, 11, 10, 17), oncall=oc)
    assert r2.alerts == () and r2.digest_only == ("F-2",)
    r3 = sla.escalate(store, ny(2026, 11, 12, 17), oncall=oc)
    assert r3.alerts == () and r3.merge_digest_head == ("F-2",)


def test_a_pr_tier1_finding_is_assigned_but_has_no_sla_clock(store):
    oc = sla.OnCall.from_doc(ONCALL)
    run(store, "run-p1", "pr")
    finding(store, "F-1", "run-p1")
    r = sla.escalate(store, ny(2026, 11, 20, 17), oncall=oc)
    assert r.assigned and r.alerts == () and r.breaches == ()


def test_a_proposal_unanswered_one_business_day_escalates(store):
    oc = sla.OnCall.from_doc(ONCALL)
    run(store, "run-p1", "pr")
    finding(store, "F-1", "run-p1", created=ny(2026, 11, 9, 10))
    store.write_proposal(Proposal("F-1", "run-p1", "mark_intended", "dev.author", "declared header change",
                                  proposed_at=iso(ny(2026, 11, 9, 11))))
    (item,) = sla.open_items(store)
    assert item.item.kind == "proposal" and item.due_at == ny(2026, 11, 9, 15)
    assert sla.escalate(store, ny(2026, 11, 10, 10, 59), oncall=oc).alerts == ()
    (a,) = sla.escalate(store, ny(2026, 11, 10, 11), oncall=oc).alerts
    assert a.step == 2 and "proposal" in a.detail and "dev.author" in a.detail


def test_without_a_roster_entry_the_system_owner_is_assigned_and_with_none_nothing_is(store):
    run(store, "run-m1")
    finding(store, "F-1", "run-m1")
    only_owner = sla.OnCall(roles={"system_owner": "asmith"})
    assert sla.escalate(store, ny(2026, 11, 9, 13), oncall=only_owner).assigned == (sla.Assignment("F-1", "asmith"),)
    finding(store, "F-3", "run-m1")
    r = sla.escalate(store, ny(2026, 11, 9, 13), oncall=sla.OnCall())
    assert r.unassigned == ("F-3",)


def test_rc_signoff_is_refused_by_an_untriaged_tier1_item(store):
    run(store, "run-rc1", "rc")
    finding(store, "F-1", "run-rc1")
    finding(store, "F-2", "run-rc1", tier=2)
    now = ny(2026, 11, 9, 12)
    assert [r.finding_id for r in sla.rc_signoff_refusals(store, "run-rc1", now)] == ["F-1"]
    store.update("findings", {"natural_key": "F-1"}, {"triage_state": "confirmed_bug"})
    assert sla.rc_signoff_refusals(store, "run-rc1", now) == ()
