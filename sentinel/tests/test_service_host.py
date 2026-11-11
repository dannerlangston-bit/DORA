"""The control host's timed work and the outbox senders (9.1, 9.2, 9.9, 9.10, 9.12, 8.6): service/host.py and
report/senders.py with a fake clock, an inline writer, a real store and the in-memory transports."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from sentinel.egress import EgressAllowlist
from sentinel.records import CheckpointOutcome, Decision, Finding, OutboxJob, RunOutcome, Signature
from sentinel.report import ReportContext, jira, prhost
from sentinel.report import reporting_config as rc
from sentinel.report.actions import ActionRequest, ActionResult
from sentinel.report.alerting import MemoryMailer, MemoryPager
from sentinel.report.senders import (ConfigPrSender, DigestSender, MemoryConfigRepo, MemorySlack, PageSender,
                                     SlackSender, Transports)
from sentinel.service.app import InlineWriter
from sentinel.service.host import ControlHost
from sentinel.store.artifacts import ArtifactStore
from sentinel.store.store import RunPlan, ScopeEntry, Store

NY = ZoneInfo("America/New_York")
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
REGION, PHASH, PR = "rgn.ladder.bid-column", "0123456789abcdef", 4812
EXAMPLE = rc.__file__.rsplit("src", 1)[0] + "config/example/"
REPORTING = rc.load(EXAMPLE + "policy/reporting.yaml")
ONCALL = yaml.safe_load(open(EXAMPLE + "ops/oncall.yaml", encoding="utf-8"))
ALLOW = EgressAllowlist.from_dict({"hosts": [
    {"host": "scm.example.internal", "purposes": ["pr_comment"]}, {"host": "slack.com", "purposes": ["slack", "digest"]},
    {"host": "jira.example.internal", "purposes": ["jira"]}, {"host": "paging.example.internal", "purposes": ["page"]}],
    "signed_by": "sec", "signed_at": "2026-11-01T00:00:00Z", "signature": "pgp:x"})


def ny(d, hh, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


def iso(at):
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "sentinel.db")
    for b, sha in (("cand-1", "a" * 40), ("base-1", "b" * 40)):
        s.register_build(b, channel="main" if b.startswith("base") else "branch", source_commit=sha,
                         pipeline_artifact_url=f"https://ci/{b}", installer_sha256=sha[:1] * 64)
    yield s
    s.close()


@pytest.fixture
def arts(tmp_path):
    return ArtifactStore.at(tmp_path / "artifacts", "outbox")


def pr_run(store, run_id="run-1", start=ny(10, 10), end_min=40):
    pair = store.register_pair("cand-1", "base-1", "pr")
    store.begin_run(RunPlan(run_id=run_id, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),),
                            pair_id=pair, pr_ref=f"PR {PR}", started_at=iso(start)))
    o = CheckpointOutcome(run_id=run_id, checkpoint_id=CENSUS, outcome="collateral", workspace_id=WS,
                          persona="active_trader", data_mode="frozen", tier=1, surface_id="trade.active-trader.ladder")
    store.write_checkpoint(o, findings=[Finding(f"fnd-{run_id}", run_id, "collateral", "ladder moved", run_id,
                                                checkpoint_id=CENSUS, region_id=REGION, phash=PHASH, tier=1,
                                                category="collateral", created_at=iso(start))])
    if end_min is not None:
        store.end_run(RunOutcome(run_id, "pr", "complete", "reported", started_at=iso(start),
                                 ended_at=iso(start + timedelta(minutes=end_min))))


def host(store, arts, transports=None, **kw):
    return ControlHost(InlineWriter(store), arts, allowlist=ALLOW, transports=transports, reporting=REPORTING,
                       oncall=ONCALL, **kw)


def full(pr_host=None):
    return Transports(pr_host=pr_host or prhost.FakePrHost(), jira=jira.MemoryJira(), slack=MemorySlack(),
                      pager=MemoryPager(), mailer=MemoryMailer(), config_repo=MemoryConfigRepo())


def report(store, arts, run_id, at):
    return prhost.post_report(store, arts, run_id, prhost.FakePrHost(), ReportContext(pr=0, candidate_build="cand-1",
                              base_build="base-1"), allowlist=ALLOW, now_=iso(at))


# -- the senders -------------------------------------------------------------------------------------------------

def job(kind, destination, run_id=None):
    return OutboxJob(kind, destination, "sha256:" + "0" * 64, created_at="2026-11-10T15:00:00+00:00", run_id=run_id)


def test_a_slack_reply_goes_under_its_parent_post_and_the_digest_email_to_the_mailer():
    client, mailer = MemorySlack(), MemoryMailer()
    s = SlackSender(client)
    parent = s.send(job("slack", "#sentinel-oncall"), {"text": "first", "thread": "run-1:env_error", "reply": False})
    s.send(job("slack", "#sentinel-oncall"), {"text": "shard 2", "thread": "run-1:env_error", "reply": True})
    assert client.posts[1]["thread_ts"] == parent.ref and client.posts[0]["thread_ts"] is None
    d = DigestSender(s, mailer)
    d.send(job("digest", "email"), {"to": ["sentinel-digest@example.internal"], "subject": "digest", "text": "body"})
    d.send(job("digest", "#sentinel-triage"), {"text": "digest post"})
    assert mailer.sent[0]["to"] == ["sentinel-digest@example.internal"] and client.posts[-1]["text"] == "digest post"


def test_a_page_and_the_outbox_outage_alert_reach_the_pager():
    pager, mailer = MemoryPager(), MemoryMailer()
    p = PageSender(pager, mailer)
    p.send(job("page", "oncall-paging"), {"alert": "integration_outage", "severity": "P2", "text": "[Sentinel] P2",
                                           "job": {"job_id": 7}, "channel": "jira"})
    assert pager.pages[0]["key"] == "integration_outage:7" and pager.pages[0]["roles"] == ["harness_on_call"]
    p.send(job("page", "email"), {"channel": "email", "key": "k", "severity": "P1", "subject": "s", "text": "t",
                                  "to": ["bkhan"], "roles": ["harness_on_call"]})
    assert mailer.sent[0]["to"] == ["bkhan"]


def test_a_delivered_mask_pr_marks_its_finding_mask_requested(store):
    pr_run(store)
    d = store.record_decision(Decision("dec-000001", "request_mask", "noise", Signature(CENSUS, REGION, PHASH,
                              "collateral"), decided_by="asmith", reason="clock", decided_at=iso(ny(10, 11))),
                              run_id="run-1", finding_id="fnd-run-1")
    repo = MemoryConfigRepo()
    sender = ConfigPrSender(repo, store, "scm.example.internal")
    got = sender.send(job("mask_pr", "sentinel-config", "run-1"), {"action": "request_mask", "mask": {
        "checkpoint_id": CENSUS, "region_id": REGION, "requested_by": "asmith", "decision_ref": d.decision_id}})
    assert got.ref == "1" and repo.pulls[1].kind == "mask_pr" and repo.pulls[1].author == "asmith"
    assert list(repo.files[1]) == [f"masks/{CENSUS}/{REGION}.yaml"]
    assert store.get_finding("fnd-run-1").evidence["triage_note"] == "mask requested, PR 1"


# -- the outbox through the control host ------------------------------------------------------------------------

def test_a_tick_posts_the_queued_comment_and_status_and_jira_writes_the_ticket_back(store, arts):
    pr_run(store)
    report(store, arts, "run-1", ny(10, 10, 41))
    d = store.record_decision(Decision("dec-000001", "confirm_bug", "confirmed_bug", Signature(CENSUS, REGION, PHASH,
                              "collateral"), decided_by="asmith", reason="bug", decided_at=iso(ny(10, 10, 50))),
                              run_id="run-1", finding_id="fnd-run-1")
    job_ = jira.bug_job([d], [store.get_finding("fnd-run-1")], jira.TicketContext("https://sentinel.internal",
                                                                                  "cand-1 vs base-1"))
    from sentinel.report import jira_sync
    q = jira_sync.enqueue(store, job_, run_id="run-1", at=iso(ny(10, 10, 50)))
    arts.put(q.payload, name=q.payload_sha256, kind="request", media_type="application/json")
    t = full()
    h = host(store, arts, t)
    rep = h.tick(ny(10, 10, 51))
    assert sorted(a.kind for a in rep.dispatch.attempts if a.ok) == ["commit_status", "jira_create", "pr_comment"]
    assert t.pr_host.comments[PR][0].body.startswith("<!-- sentinel:pr:4812 -->")
    assert store.get_decision(d.decision_id).ticket == "TOS-1"                     # 9.9: the key lands
    assert {"outbox", "reminders", "sla"} <= set(rep.ran)


def test_jobs_with_no_transport_wait_queued(store, arts):
    pr_run(store)
    report(store, arts, "run-1", ny(10, 10, 41))
    rep = host(store, arts, Transports()).tick(ny(10, 10, 42))
    assert rep.dispatch.attempts == [] and len(rep.dispatch.waiting) == 2


def test_a_pr_host_down_15_minutes_raises_the_outage_alert_on_slack_and_the_pager(store, arts):
    pr_run(store)
    report(store, arts, "run-1", ny(10, 10, 41))
    t = full()
    t.pr_host.down = True
    h = host(store, arts, t)
    for m in (41, 42, 43, 46, 56):
        rep = h.tick(ny(10, 10, m))
    assert len(rep.dispatch.alerts) == 2
    assert t.slack.posts[-1]["channel"] == "#sentinel-oncall" and "integration outage" in t.slack.posts[-1]["text"]
    assert t.pager.pages[-1]["key"].startswith("integration_outage:")


def test_a_job_abandoned_at_24_hours_opens_a_sent_task(store, arts):
    pr_run(store)
    report(store, arts, "run-1", ny(10, 10, 41))
    t = full()
    t.pr_host.down = True
    h = host(store, arts, t)
    h.tick(ny(10, 10, 41))
    rep = h.tick(ny(11, 10, 42))
    assert rep.dispatch.abandoned and rep.sent_tasks
    task = store.outbox_job(rep.sent_tasks[0])
    assert task.kind == "jira_create" and task.destination.startswith("SENT#task:abandoned_outbox_job")


def test_no_comment_by_budget_plus_15_minutes_queues_the_error_status(store, arts):
    pr_run(store, end_min=None)
    rep = host(store, arts, full()).tick(ny(10, 11, 1))                            # 45 min + 15 min after 10:00
    assert len(rep.overdue) == 1 and store.outbox_job(rep.overdue[0]).kind == "commit_status"


def test_an_unanswered_page_writes_alert_undelivered_after_5_minutes(store, arts):
    from sentinel.orchestrator import alerts
    from sentinel.report.alerting import Alert
    pr_run(store)
    h = host(store, arts, Transports())                                            # nothing accepts the page
    h.raise_alert(Alert.of(alerts.aborted_harness(), detail="hook_lost", key="lost"), ny(10, 11))
    assert h.tick(ny(10, 11, 4)).undelivered == ()
    (late,) = h.tick(ny(10, 11, 6)).undelivered
    assert late.alert.key == "lost" and json.loads(store.get_system_state("alert_undelivered"))[0]["key"] == "lost"
    from sentinel.store import connect_reader
    from sentinel.ui import pages
    page = pages.run_list(connect_reader(store.path), {})                          # the local page shows it (9.10)
    assert "alert_undelivered: 1 page(s) no channel accepted" in page.body and "aborted_harness (lost)" in page.body


# -- the schedule -------------------------------------------------------------------------------------------------

def test_the_daily_digest_runs_once_at_0800_on_business_days_and_the_weekly_on_mondays(store, arts):
    t = full()
    h = host(store, arts, t)
    assert "digest_daily" not in h.tick(ny(10, 7, 59)).ran
    first = h.tick(ny(10, 8, 0))
    assert "digest_daily" in first.ran and first.digests[0].startswith("[Sentinel] P3 needs_human daily digest")
    assert "digest_daily" not in h.tick(ny(10, 9)).ran
    assert "digest_daily" not in host(store, arts, t).tick(ny(14, 9)).ran           # a Saturday: none (9.10)
    assert t.mailer.sent and t.slack.posts                                          # the email copy and the post
    monday = host(store, arts, t).tick(ny(16, 8, 5))
    assert "digest_weekly" in monday.ran and "announce" in monday.ran


def test_sla_escalation_runs_hourly_in_business_hours_only(store, arts):
    h = host(store, arts, full())
    assert "sla" in h.tick(ny(10, 9, 30)).ran and "sla" not in h.tick(ny(10, 10)).ran
    assert "sla" in h.tick(ny(10, 10, 31)).ran
    assert "sla" not in host(store, arts, full()).tick(ny(10, 18)).ran


def test_the_backup_runs_at_0200_through_the_writer_and_a_failure_raises_store_health(store, arts, tmp_path):
    t = full()
    h = host(store, arts, t, artifact_root=tmp_path / "artifacts", backup_root=tmp_path / "backup", background=False)
    rep = h.tick(ny(11, 2, 1))
    assert "backup" in rep.ran and rep.maintenance[0].endswith(": ok")
    assert store.get_system_state("last_backup_at") is not None
    assert "backup" not in h.tick(ny(11, 2, 30)).ran
    (tmp_path / "blocked").write_text("a file where the backup location should be")
    bad = host(store, arts, t, artifact_root=tmp_path / "artifacts", backup_root=tmp_path / "blocked" / "backup",
               background=False)
    rep = bad.tick(ny(12, 2, 1))
    assert "failed" in rep.maintenance[0]
    assert store.query("SELECT count(*) FROM outbox WHERE kind = 'slack' AND destination = '#sentinel-oncall' AND "
                       "state = 'queued'")[0][0] == 1                                # the 9.10 Store health post


def test_the_audit_runs_on_sundays_and_a_restore_drill_is_recorded(store, arts, tmp_path):
    h = host(store, arts, full(), artifact_root=tmp_path / "artifacts", backup_root=tmp_path / "backup",
             background=False)
    assert "audit" in h.tick(ny(15, 3, 5)).ran and "audit" not in h.tick(ny(15, 4)).ran
    assert h.restore_drill("asmith", compared=12, matched=12, at=ny(15, 9))
    assert store.get_system_state("last_restore_drill_at") is not None


def test_after_an_action_the_decided_pr_run_is_re_posted_and_delivered(store, arts):
    pr_run(store)
    report(store, arts, "run-1", ny(10, 10, 41))
    t = full()
    h = host(store, arts, t)
    h.tick(ny(10, 10, 42))
    d = store.record_decision(Decision("dec-000001", "confirm_bug", "confirmed_bug", Signature(CENSUS, REGION, PHASH,
                              "collateral"), decided_by="asmith", ticket="TOS-1", reason="bug",
                              decided_at=iso(ny(10, 10, 50))), run_id="run-1", finding_id="fnd-run-1")
    posted = h.after_action(ActionResult("confirm_bug", decisions=[d.decision_id]), ActionRequest("asmith"),
                            ny(10, 10, 50))
    assert [p.status.state for p in posted] == ["success"]
    assert t.pr_host.statuses[("a" * 40, "sentinel/pr-run")]["state"] == "success"   # 9.2: within the minute
