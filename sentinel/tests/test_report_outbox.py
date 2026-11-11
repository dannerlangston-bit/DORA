"""Outbox delivery (8.3 outbox, 9.1, 9.10 Integration outage): report/outbox.py with a fake clock and fake senders."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.canonical import canonical_json, sha256_id
from sentinel.egress import EgressAllowlist
from sentinel.records import OutboxJob
from sentinel.report import outbox as ob
from sentinel.report import reporting_config as rc
from sentinel.store.artifacts import ArtifactMissing, ArtifactStore
from sentinel.store.store import RunPlan, Store, StoreRefused

RUN = "run-ob-01"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
SCM, SLACK, JIRA, PAGER = "scm.example.internal", "slack.com", "jira.example.internal", "pager.example.internal"
ALLOW = EgressAllowlist.from_dict({"hosts": [
    {"host": SCM, "purposes": ["pr_comment"]}, {"host": SLACK, "purposes": ["slack", "digest"]},
    {"host": JIRA, "purposes": ["jira"]}, {"host": PAGER, "purposes": ["digest"]}],
    "signed_by": "sec", "signed_at": "2026-11-01T00:00:00Z", "signature": "pgp:x"})
ALERTS = (ob.AlertDestination("slack", "slack", "#sentinel-oncall"), ob.AlertDestination("paging", "page", "pager"))


def t(minutes: float = 0) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class FakeSender:
    """Fails the first `failures` sends (HTTP 502), then delivers; `down` fails every send."""

    def __init__(self, host: str, failures: int = 0, *, down: bool = False, purpose: str | None = None):
        self.host, self.failures, self.down = host, failures, down
        if purpose is not None:
            self.purpose = purpose
        self.sent: list[tuple[int, dict]] = []
        self.tries = 0

    def send(self, job: OutboxJob, payload):
        self.tries += 1
        if self.down or self.tries <= self.failures:
            raise ob.DeliveryError("bad gateway", status_code=502)
        self.sent.append((job.job_id, dict(payload)))
        return ob.Delivery(status=200, request_id=f"req-{self.tries}", ref=f"ref-{job.job_id}")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "sentinel.db")
    s.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=(), started_at=t(-60)))
    yield s
    s.close()


@pytest.fixture
def arts(tmp_path):
    return ArtifactStore.at(tmp_path / "artifacts", "outbox")


def enqueue(store, arts, kind="slack", destination="#sentinel-triage", payload=None, at=0, run_id=RUN):
    return ob.enqueue_job(store, arts, kind=kind, destination=destination, payload=payload or {"text": "hello"},
                          run_id=run_id, at=t(at))


def egress(store):
    return [dict(r) for r in store.query("SELECT * FROM egress_log ORDER BY egress_id")]


def jobs(store, **where):
    sql = "SELECT * FROM outbox" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "")
    return [dict(r) for r in store.query(sql + " ORDER BY job_id", tuple(where.values()))]


# -- the schedule ------------------------------------------------------------------------------------------------

def test_the_retry_schedule_is_1_2_5_15_60_then_hourly_to_24_hours():
    assert ob.RETRY_MINUTES[:6] == (1, 2, 5, 15, 60, 120) and ob.RETRY_MINUTES[-1] == 1440
    assert len(ob.RETRY_MINUTES) == 4 + 24
    assert ob.next_attempt_at(t(0), t(0)) == t(1)
    assert ob.next_attempt_at(t(0), t(1)) == t(2)
    assert ob.next_attempt_at(t(0), t(5)) == t(15)
    assert ob.next_attempt_at(t(0), t(16)) == t(60)                 # a late attempt skips the points it missed
    assert ob.next_attempt_at(t(0), t(1439)) == t(1440)
    assert ob.next_attempt_at(t(0), t(1440)) is None                # 24 hours: abandon


# -- enqueue -----------------------------------------------------------------------------------------------------

def test_a_job_is_written_before_any_send_and_survives_a_crash(tmp_path, store, arts):
    jid = enqueue(store, arts, payload={"text": "tier-1 collateral"})
    job = store.outbox_job(jid)
    assert (job.state, job.attempts, job.next_attempt_at, job.run_id) == ("queued", 0, None, RUN)
    assert job.payload_sha256 == sha256_id({"text": "tier-1 collateral"})
    assert json.loads(arts.get(job.payload_sha256.split(":")[1])) == {"text": "tier-1 collateral"}
    assert egress(store) == []
    store.close()                                                   # the Reporter dies before the send
    again = Store(tmp_path / "db" / "sentinel.db")
    try:
        assert [j.job_id for j in ob.due_jobs(again, t(0))] == [jid]
        sender = FakeSender(SLACK)
        rep = ob.dispatch(again, arts, {"slack": sender}, t(0), allowlist=ALLOW)
        assert [a.state for a in rep.attempts] == ["sent"] and sender.sent == [(jid, {"text": "tier-1 collateral"})]
        assert again.outbox_job(jid).sent_at == t(0)
    finally:
        again.close()


def test_an_identical_open_job_is_not_written_twice(store, arts):
    a = enqueue(store, arts)
    assert enqueue(store, arts, at=1) == a
    assert enqueue(store, arts, destination="#sentinel-release") != a
    ob.dispatch(store, arts, {"slack": FakeSender(SLACK)}, t(2), allowlist=ALLOW)
    assert enqueue(store, arts, at=3) not in (a,)                   # once sent, the same post again is a new job


# -- dispatch ----------------------------------------------------------------------------------------------------

def test_a_job_that_fails_then_delivers_logs_one_egress_row_per_attempt(store, arts):
    jid = enqueue(store, arts)
    sender = FakeSender(SLACK, failures=2)
    first = ob.dispatch(store, arts, {"slack": sender}, t(0), allowlist=ALLOW)
    assert first.attempts == [ob.Attempt(jid, "slack", False, "failed", "DeliveryError (HTTP 502): bad gateway", t(1))]
    assert ob.dispatch(store, arts, {"slack": sender}, t(0.5), allowlist=ALLOW).attempts == []   # not due yet
    ob.dispatch(store, arts, {"slack": sender}, t(1), allowlist=ALLOW)
    done = ob.dispatch(store, arts, {"slack": sender}, t(2), allowlist=ALLOW)
    assert done.attempts == [ob.Attempt(jid, "slack", True, "sent", ref=f"ref-{jid}")]
    job = store.outbox_job(jid)
    assert (job.state, job.attempts, job.sent_at) == ("sent", 3, t(2))
    rows = egress(store)
    assert [(r["status"], r["response_status"], r["purpose"], r["destination"], r["run_id"]) for r in rows] == [
        ("failed", 502, "slack", SLACK, RUN), ("failed", 502, "slack", SLACK, RUN), ("sent", 200, "slack", SLACK, RUN)]
    assert rows[-1]["request_id"] == "req-3" and rows[0]["sent_at"] == t(0)
    assert all(r["bytes"] == len(canonical_json({"text": "hello"})) for r in rows)


def test_a_job_failing_for_24_hours_follows_the_schedule_and_is_abandoned(store, arts):
    jid = enqueue(store, arts)
    sender = FakeSender(SLACK, down=True)
    clock, seen = t(0), []
    for _ in range(40):
        rep = ob.dispatch(store, arts, {"slack": sender}, clock, allowlist=ALLOW)
        job = store.outbox_job(jid)
        if job.state == "abandoned":
            assert rep.abandoned == [jid]
            break
        seen.append(job.next_attempt_at)
        clock = job.next_attempt_at
    assert seen == [t(m) for m in ob.RETRY_MINUTES]
    job = store.outbox_job(jid)
    assert (job.state, job.attempts, job.next_attempt_at, job.last_error) == (
        "abandoned", 29, None, "DeliveryError (HTTP 502): bad gateway")
    rows = egress(store)
    assert len(rows) == 29 and {r["status"] for r in rows} == {"failed"}
    assert ob.dispatch(store, arts, {"slack": sender}, t(3000), allowlist=ALLOW).attempts == []
    assert [j.job_id for j in ob.abandoned_jobs(store)] == [jid] == [j.job_id for j in ob.abandoned_jobs(store, RUN)]
    assert ob.abandoned_jobs(store, "another-run") == []
    with pytest.raises(StoreRefused):
        ob.close_job(store, jid, by="reporter")                     # a person closes it (9.1)
    closed = ob.close_job(store, jid, by="asmith", at=t(1500))
    assert (closed.closed_by, closed.closed_at) == ("asmith", t(1500)) and ob.abandoned_jobs(store) == []


def test_the_outage_alert_fires_once_at_15_minutes_on_every_other_channel(store, arts):
    jid = enqueue(store, arts, kind="jira_comment", destination="TOS-41", payload={"body": "seen again"})
    jira = FakeSender(JIRA, down=True)
    slack, pager = FakeSender(SLACK), FakeSender(PAGER, purpose="digest")
    senders = {"jira_comment": jira, "slack": slack, "page": pager}
    raised = {}
    clock = t(0)
    for _ in range(40):
        rep = ob.dispatch(store, arts, senders, clock, allowlist=ALLOW, alert_to=ALERTS)
        if rep.alerts:
            raised[clock] = rep.alerts
        job = store.outbox_job(jid)
        if job.next_attempt_at is None:
            break
        clock = job.next_attempt_at
    assert list(raised) == [t(15)] and len(raised[t(15)]) == 2
    alerts = jobs(store, created_at=t(15))
    assert sorted((a["kind"], a["destination"], a["state"]) for a in alerts) == [
        ("page", "pager", "sent"), ("slack", "#sentinel-oncall", "sent")]       # attempted in the same pass
    (_, body), = slack.sent
    assert body == pager.sent[0][1]
    assert body["alert"] == "integration_outage" and body["channel"] == "jira" and body["job"]["job_id"] == jid
    assert body["text"].startswith(f"[Sentinel] P2 integration outage | pr {RUN} | running | jira: outbox job {jid} "
                                   "(jira_comment to TOS-41) failing since")


def test_a_slack_outage_alerts_the_paging_service_only(store, arts):
    enqueue(store, arts)
    pager = FakeSender(PAGER, purpose="digest")
    senders = {"slack": FakeSender(SLACK, down=True), "page": pager}
    for m in (0, 1, 2, 5, 15):
        rep = ob.dispatch(store, arts, senders, t(m), allowlist=ALLOW, alert_to=ALERTS)
    assert len(rep.alerts) == 1 and jobs(store, job_id=rep.alerts[0])[0]["kind"] == "page"
    assert pager.sent[0][1]["channel"] == "slack"


def test_one_alert_per_channel_outage_and_a_new_one_after_recovery(store, arts):
    a = enqueue(store, arts, payload={"text": "a"})
    b = enqueue(store, arts, payload={"text": "b"}, at=3)
    slack = FakeSender(SLACK, down=True)
    pager = FakeSender(PAGER, purpose="digest")
    senders = {"slack": slack, "page": pager}
    alerts = []
    for m in (0, 1, 2, 3, 4, 5, 8, 15, 18, 60):
        alerts += ob.dispatch(store, arts, senders, t(m), allowlist=ALLOW, alert_to=ALERTS).alerts
    assert len(alerts) == 1 and pager.sent[0][1]["job"]["job_id"] == a   # b crossed at 18 behind a: no second alert
    slack.down = False
    ob.dispatch(store, arts, senders, t(120), allowlist=ALLOW, alert_to=ALERTS)
    assert store.outbox_job(a).state == store.outbox_job(b).state == "sent"
    slack.down = True
    c = enqueue(store, arts, payload={"text": "c"}, at=200)
    for m in (200, 201, 202, 205, 215):
        alerts += ob.dispatch(store, arts, senders, t(m), allowlist=ALLOW, alert_to=ALERTS).alerts
    assert len(alerts) == 2 and pager.sent[-1][1]["job"]["job_id"] == c


def test_an_outage_alert_that_fails_raises_no_further_alert(store, arts):
    enqueue(store, arts, kind="jira_create", destination="SENT", payload={"summary": "task"})
    senders = {"jira_create": FakeSender(JIRA, down=True), "slack": FakeSender(SLACK, down=True),
               "page": FakeSender(PAGER, down=True, purpose="digest")}
    raised = []
    for m in range(0, 24 * 60 + 61, 1):
        if ob.due_jobs(store, t(m)):
            raised += ob.dispatch(store, arts, senders, t(m), allowlist=ALLOW, alert_to=ALERTS).alerts
    assert len(raised) == 2                                          # the jira outage's two alerts, nothing more
    assert {j["state"] for j in jobs(store)} == {"abandoned"}


def test_state_posts_to_one_destination_go_out_in_order(store, arts):
    pending = enqueue(store, arts, kind="commit_status", destination="commit:abc#sentinel/pr-run",
                      payload={"state": "pending"})
    scm = FakeSender(SCM, failures=1)
    ob.dispatch(store, arts, {"commit_status": scm}, t(0), allowlist=ALLOW)          # pending fails once
    final = enqueue(store, arts, kind="commit_status", destination="commit:abc#sentinel/pr-run",
                    payload={"state": "failure"}, at=0.5)
    other = enqueue(store, arts, kind="commit_status", destination="commit:def#sentinel/pr-run",
                    payload={"state": "success"}, at=0.5)
    rep = ob.dispatch(store, arts, {"commit_status": scm}, t(0.5), allowlist=ALLOW)
    assert rep.waiting == [final] and [a.job_id for a in rep.attempts] == [other]
    assert [j.job_id for j in ob.due_jobs(store, t(0.5))] == []
    rep = ob.dispatch(store, arts, {"commit_status": scm}, t(1), allowlist=ALLOW)
    assert [a.job_id for a in rep.attempts] == [pending, final]                    # in order, in one pass
    assert [p["state"] for _, p in scm.sent] == ["success", "pending", "failure"]


def test_a_job_with_no_sender_waits_unattempted(store, arts):
    jid = enqueue(store, arts, kind="digest", destination="sentinel-digest@example.internal")
    rep = ob.dispatch(store, arts, {}, t(0), allowlist=ALLOW)
    assert rep.waiting == [jid] and rep.attempts == [] and store.outbox_job(jid).state == "queued"
    assert egress(store) == []


def test_an_allowlist_refusal_is_a_failed_attempt_with_its_row_and_finding(store, arts):
    jid = enqueue(store, arts)
    sender = FakeSender("hooks.slack.example")
    rep = ob.dispatch(store, arts, {"slack": sender}, t(0), allowlist=ALLOW)
    assert sender.tries == 0 and rep.attempts[0].state == "failed"
    assert "destination not allowlisted: hooks.slack.example" in rep.attempts[0].error
    (row,) = egress(store)
    assert (row["status"], row["run_id"], row["purpose"]) == ("refused", RUN, "slack") and row["refusal_reason"]
    assert [(f.kind, f.detail, f.run_id) for f in rep.refusals] == [("harness_error", "egress_refused", RUN)]
    assert store.findings(RUN) == []                                # returned, not written (choice 5)
    assert store.outbox_job(jid).last_error.startswith("egress to hooks.slack.example for slack refused")


def test_a_missing_payload_fails_without_a_request(store, arts):
    jid = store.enqueue(OutboxJob("slack", "#sentinel-triage", "sha256:" + "0" * 64, created_at=t(0), run_id=RUN))
    sender = FakeSender(SLACK)
    rep = ob.dispatch(store, arts, {"slack": sender}, t(0), allowlist=ALLOW)
    assert sender.tries == 0 and egress(store) == []
    assert rep.attempts[0].error.startswith(f"payload sha256:{'0' * 64} unavailable")
    assert store.outbox_job(jid).state == "failed"


def test_a_page_needs_a_sender_that_names_its_purpose(store, arts):
    jid = enqueue(store, arts, kind="page", destination="pager", run_id=None)
    rep = ob.dispatch(store, arts, {"page": FakeSender(PAGER)}, t(0), allowlist=ALLOW)
    assert rep.attempts[0].error == "no egress purpose for page posts (6.10)" and egress(store) == []
    ob.dispatch(store, arts, {"page": FakeSender(PAGER, purpose="digest")}, t(1), allowlist=ALLOW)
    assert store.outbox_job(jid).state == "sent"
    assert [(r["purpose"], r["run_id"]) for r in egress(store)] == [("digest", None)]


def test_a_sender_that_returns_nothing_still_delivers(store, arts):
    class Bare:
        host = SLACK

        def send(self, job, payload):
            return None
    jid = enqueue(store, arts)
    rep = ob.dispatch(store, arts, {"slack": Bare()}, t(0), allowlist=ALLOW)
    assert rep.attempts == [ob.Attempt(jid, "slack", True, "sent")]
    assert [(r["status"], r["response_status"]) for r in egress(store)] == [("sent", None)]


# -- helpers -----------------------------------------------------------------------------------------------------

def test_channels_and_egress_purposes():
    assert [ob.channel_of(k) for k in ("pr_comment", "commit_status", "quarantine_pr", "mask_pr", "jira_transition",
                                       "slack", "page", "digest")] == [
        "pr_host", "pr_host", "pr_host", "pr_host", "jira", "slack", "paging", "email"]
    assert [ob.egress_purpose(k) for k in ("commit_status", "manifest_pr", "jira_create", "slack", "digest",
                                           "page")] == ["pr_comment", "pr_comment", "jira", "slack", "digest", None]
    with pytest.raises(ValueError):
        ob.channel_of("fax")


def test_alert_destinations_come_from_the_reporting_config():
    cfg = rc.load(rc.__file__.rsplit("src", 1)[0] + "config/example/policy/reporting.yaml")
    assert ob.alert_destinations(cfg) == (ob.AlertDestination("slack", "slack", "#sentinel-oncall"),
                                          ob.AlertDestination("paging", "page", "oncall-paging"))


def test_payloads_read_several_sets_and_write_the_first(tmp_path):
    a, b = ArtifactStore.at(tmp_path, "outbox"), ArtifactStore.at(tmp_path, "runs/r1")
    held = b.put(b'{"x":1}', name="r1/quarantine-pr.json", kind="request", media_type="application/json")
    both = ob.Payloads(a, b)
    assert both.get(held.sha256) == b'{"x":1}'
    new = both.put(b'{"y":2}', name="outbox/y.json", kind="request", media_type="application/json")
    assert a.get(new.sha256) == b'{"y":2}'
    with pytest.raises(ArtifactMissing):
        both.get("f" * 64)
    with pytest.raises(ValueError):
        ob.Payloads()
