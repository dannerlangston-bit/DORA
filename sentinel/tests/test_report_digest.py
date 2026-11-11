"""Digests and triage reminders (9.9 triage channel, 9.10 needs_human daily digest and digest format, 9.12, 11.6)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sentinel.orchestrator import alerts
from sentinel.records import Decision, Finding, OutboxJob, Proposal, Signature
from sentinel.report import digest, slack
from sentinel.report.alerting import Alert
from sentinel.sla import OnCall, Rotation
from sentinel.store import RunPlan, ScopeEntry, Store

NY = ZoneInfo("America/New_York")
WS = "ws.tier-a.active-trader"
T1 = f"census/trade.active-trader.ladder/{WS}"
T2 = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
RUN = "run-2026-11-10-main-01"
CAND = "c" * 64
CHANNELS = slack.SlackChannels.from_reporting({"slack": {"on_call": "#sentinel-oncall", "triage": "#sentinel-triage",
                                                          "release": "#tos-release", "safety": "#sentinel-safety"}})
ONCALL = OnCall({"system_owner": "asmith"}, {1: "tos-trade-ui"},
                (Rotation("triage", "triage.lead", datetime(2026, 11, 9).date(), datetime(2026, 11, 15).date()),))


def ny(d, hh, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


WED_08 = ny(11, 8)


def iso(at):
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    for b, ch in (("m0", "main"), ("m1", "main")):
        s.register_build(b, channel=ch, source_commit=b, pipeline_artifact_url="u", installer_sha256=b,
                         first_seen_at="2026-11-01T00:00:00+00:00")
    yield s
    s.close()


def begin(s, run_id=RUN, run_type="merge", started=None):
    pair = s.register_pair("m1", "m0", "merge")
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=7200, pair_id=pair, started_at=iso(started or ny(10, 10)),
                        scope=(ScopeEntry(T1, WS, "active_trader", 1, "frozen"),
                               ScopeEntry(T2, WS, "active_trader", 2, "frozen"))))


def finding(s, fid, *, tier, created, run_id=RUN, **kw):
    s.write_finding(Finding(fid, run_id, "collateral", f"{fid} moved", kw.pop("first_seen", run_id),
                            checkpoint_id=T1 if tier == 1 else T2, tier=tier, region_id="rgn.ladder.viewport",
                            phash="0123456789abcdef", category="collateral", created_at=iso(created), **kw))


def busy_store(s):
    """Twelve open findings from Monday 09:00 every 30 min, alternating tier 1 and 2, plus the digest's sections."""
    begin(s)
    for n in range(12):
        finding(s, f"F-{n + 1:02d}", tier=1 if n % 2 == 0 else 2, created=ny(9, 9) + timedelta(minutes=30 * n))
    s.write_triage_assignment("F-01", assignee="triage.lead", assigned_by="system", escalation_step=2,
                              at=iso(ny(9, 13)))
    s.write_proposal(Proposal("F-12", RUN, "mark_intended", "dev.author", "declared change",
                              proposed_at=iso(ny(10, 15))))
    s.record_decision(Decision("dec-000007", "confirm_bug", "confirmed_bug",
                               Signature(T2, "rgn.cell.header", "fedcba9876543210", "collateral"), decided_by="alice",
                               ticket=None, reason="Jira down", decided_at=iso(ny(9, 12))), run_id=RUN)
    job = s.enqueue(OutboxJob("jira_create", "TOS", "sha256:" + "d" * 64, created_at=iso(ny(9, 12)), run_id=RUN))
    s.mark_failed(job, error="503 from Jira", next_attempt_at=iso(ny(9, 13)))
    s.abandon(job)
    s.update_summary(RUN, {"alerts": [{"run_id": RUN, "shard_id": None, "rule": "coverage_loss", "priority": "P3",
                                       "paged": [], "notified": ["harness_on_call"], "safety_table": False,
                                       "control": "budget", "gate": None, "incident": None, "run_outcome": None,
                                       "run_reason": None, "detail": "3 checkpoints not_run (budget)"}]})


# -- the daily digest (9.10 format) ----------------------------------------------------------------------------------

def test_the_daily_digest_has_the_910_layout(store):
    busy_store(store)
    d = digest.daily(store, WED_08, oncall=ONCALL)
    text = d.render().split("\n")
    assert text[0] == "[Sentinel] P3 needs_human daily digest | Wed 2026-11-11 08:00 EST"
    assert text[1:4] == ["Tier 1: 6 open, oldest 1d 23h, 6 past SLA",       # due by Tuesday 14:30
                         "Tier 2: 6 open, oldest 1d 22h, 5 past SLA",       # F-12's proposal is due Wed 11:00
                         "Tier 3: 0 open"]
    assert text[4] == "Oldest items:"
    oldest = text[5:15]
    assert len(oldest) == 10 and all(x.startswith("[Sentinel] P3 collateral | merge ") for x in oldest)
    assert oldest[0] == (f"[Sentinel] P3 collateral | merge {RUN} | m1 vs m0 | collateral | trade.active-trader.ladder "
                         f"tier 1 | F-01 moved | https://sentinel.internal/f/F-01")
    assert "F-11 moved" not in "\n".join(oldest)                                # only the ten oldest
    sections = [t for t in text[15:] if t.endswith(":")]
    assert sections == ["Escalations:", "Pending proposals:", "Decisions without tickets:", "Abandoned outbox jobs:",
                        "P3 alerts:"]
    body = "\n".join(text[15:])
    assert "F-01 (tier 1): step 2, assigned to triage.lead" in body
    assert "proposal 1 on F-12: mark_intended by dev.author, open 2h 00m business, awaiting triage.lead" in body
    assert "dec-000007: confirm_bug by alice" in body
    assert "jira_create to TOS, last error 503 from Jira" in body
    assert f"[Sentinel] P3 coverage_loss | merge {RUN} | m1 vs m0 | - | 3 checkpoints not_run (budget)" in body
    assert text[-1] == "Queue: https://sentinel.internal/triage"


def test_an_empty_queue_prints_none_in_each_section_and_takes_passed_alerts(store):
    begin(store)
    held = Alert.of(alerts.explorer_ticket(), detail="Explorer attempted send on rig-03", key="x")
    text = digest.daily(store, WED_08, alerts=[held]).render().split("\n")
    assert text[1:4] == ["Tier 1: 0 open", "Tier 2: 0 open", "Tier 3: 0 open"]
    assert text[4:6] == ["Oldest items:", "none"]
    assert text[-2].startswith("[Sentinel] P3 explorer_ticket | - | - | - | Explorer attempted send on rig-03")


def test_no_digest_on_a_saturday_or_a_holiday(store):
    begin(store)
    assert digest.daily(store, ny(14, 8)) is None                               # Saturday
    assert digest.daily(store, ny(26, 8)) is None                               # Thanksgiving
    assert digest.daily_at(datetime(2026, 11, 11).date()) == WED_08.astimezone(timezone.utc)


def test_a_new_ticket_decision_is_not_listed_before_24_hours(store):
    begin(store)
    store.record_decision(Decision("dec-000008", "confirm_bug", "confirmed_bug",
                                   Signature(T2, "rgn.cell.header", "fedcba9876543210", "collateral"),
                                   decided_by="alice", ticket=None, reason="Jira down",
                                   decided_at=iso(ny(10, 9))), run_id=RUN)
    text = digest.daily(store, WED_08).render()
    assert "dec-000008" not in text and "Decisions without tickets:\nnone" in text


# -- the weekly summary and the merge digest -------------------------------------------------------------------------

def test_the_weekly_summary_lists_contested_suspect_breaches_and_the_coverage_review(store):
    busy_store(store)
    finding(store, "F-13", tier=2, created=ny(12, 10))
    store.update("findings", {"natural_key": "F-13"}, {"contested": 1})
    store.update("findings", {"natural_key": "F-02"}, {"contested": 1})            # contested before the week
    w = digest.weekly(store, ny(17, 17), coverage_review="2 entitlement gaps recorded; capacity task SENT-88")
    text = w.render().split("\n")
    assert text[0] == "[Sentinel] weekly metrics summary | week ending Tue 2026-11-17 17:00 EST"
    assert text[1:3] == ["Contested decisions: 1", f"- F-13 on {T2} (tier 2)"]
    assert "Suspect decisions: 0" in text
    breach_line = text.index("SLA breaches: 12")
    assert text[breach_line + 1] == "- F-01 (tier 1): five business days past its SLA, assigned to triage.lead"
    assert text[-1] == "Coverage review: 2 entitlement gaps recorded; capacity task SENT-88"
    assert digest.weekly(store, ny(17, 17)).lines[-1] == "Coverage review: not recorded this week"
    assert "SLA breaches: 1" in digest.weekly(store, ny(16, 17)).lines           # only F-01 five days past by then


def test_the_merge_digest_is_headed_by_items_three_days_past_sla(store):
    begin(store, "run-old", started=ny(2, 10))
    finding(store, "F-OLD", tier=1, created=ny(2, 10), run_id="run-old")
    store.write_triage_assignment("F-OLD", assignee="triage.lead", assigned_by="system", escalation_step=3)
    begin(store)
    finding(store, "F-NEW", tier=2, created=ny(10, 10))
    finding(store, "F-SEEN", tier=2, created=ny(10, 11), first_seen="run-old")
    text = digest.merge_run(store, RUN, ny(10, 12)).render().split("\n")
    assert text[0] == f"[Sentinel] P3 merge run digest | merge {RUN} | m1 vs m0"
    assert text[1] == "Escalated, three business days past SLA:" and "F-OLD moved" in text[2]
    assert text[3] == "New Unexpected (1):" and "F-NEW moved" in text[4] and len(text) == 5


# -- reminders (9.9 triage channel) ----------------------------------------------------------------------------------

def test_a_proposal_25_business_hours_old_triggers_the_reminder_once(store):
    begin(store)
    finding(store, "F-1", tier=1, created=ny(10, 9))
    store.write_proposal(Proposal("F-1", RUN, "mark_intended", "dev.author", "declared change",
                                  proposed_at=iso(ny(10, 15, 30))))
    assert digest.reminders(store, ny(10, 16, 59)) == ()                       # 1.5 business hours
    (r,) = digest.reminders(store, ny(11, 10), since=ny(10, 16, 59))          # 2.5 business hours, overnight
    assert r.kind == "proposal" and "open 2h 30m business" in r.fields.detail
    assert digest.reminders(store, ny(11, 11), since=ny(11, 10)) == ()        # already reminded
    (q,) = digest.post_reminders(store, [r], CHANNELS, ny(11, 10))
    assert q.kind == "slack" and q.destination == "#sentinel-triage"


def test_the_baseline_queue_reminder_comes_3_business_days_before_an_rc_run(store):
    begin(store)
    rc = ny(19, 9)                                                              # Thursday
    assert digest.reminders(store, ny(16, 7, 59), rc_runs=[rc]) == ()
    (r,) = digest.reminders(store, ny(16, 8), since=ny(16, 7, 59), rc_runs=[rc])
    assert r.kind == "baseline_queue" and "0 baseline candidates" in r.fields.detail
    assert r.fields.report_link == "https://sentinel.internal/baselines"
    assert digest.reminders(store, ny(17, 8), since=ny(16, 8), rc_runs=[rc]) == ()


# -- posting -------------------------------------------------------------------------------------------------------

def test_the_digest_is_one_triage_post_with_purpose_digest_and_an_email_copy(store):
    busy_store(store)
    d = digest.daily(store, WED_08, oncall=ONCALL)
    post, mail = digest.post(store, d, CHANNELS, WED_08, email_to=["triage-list@example.internal"])
    assert (post.kind, post.destination) == ("digest", "#sentinel-triage")
    p = json.loads(post.payload)
    assert p["egress_purpose"] == "digest" and p["text"].split("\n")[0].startswith("[Sentinel] P3 needs_human")
    m = json.loads(mail.payload)
    assert (mail.kind, mail.destination, m["to"]) == ("digest", "email", ["triage-list@example.internal"])
    assert m["subject"] == d.title and "\n" in m["text"]
    assert digest.post(store, d, CHANNELS, WED_08, email_to=["triage-list@example.internal"]) == ()


def test_digest_text_is_escaped_for_slack(store):
    begin(store)
    finding(store, "F-1", tier=1, created=ny(10, 9))
    store.update("findings", {"natural_key": "F-1"}, {"description": "<!channel> see www.evil.example"})
    text = digest.daily(store, WED_08).render(slack_post=True)
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text and "www.evil" not in text
