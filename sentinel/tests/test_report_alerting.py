"""The Reporter's alert dispatcher (9.10, 9.9 Dedup, 10.1, 11.6; SD-62, SD-84, SD-85): run-level collapse, the
route table, repeats and the harness freeze, quiet hours, the undelivered watch, the message format and acks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from sentinel.orchestrator import alerts
from sentinel.records import RunOutcome
from sentinel.report import alerting, slack
from sentinel.report.alerting import Alert, Dispatcher, RunRef
from sentinel.sla import OnCall
from sentinel.store import RunPlan, Store
from sentinel.store import store as store_mod

NY = ZoneInfo("America/New_York")
RUN = "run-2026-11-10-pr-4823-01"
SAFE = ["harness_on_call", "system_owner", "risk_owner", "accountable_owner", "security_contact"]
ONCALL = OnCall.from_doc(yaml.safe_load("""
roles:
  - {role: system_owner, person: asmith}
  - {role: accountable_owner, person: cliu}
  - {role: risk_owner, person: dmora}
  - {role: security_contact, person: security-contact}
  - {role: accounts_owner, person: asmith}
  - {role: release_manager, person: glee}
flow_owners:
  - {tier: 1, person: tos-trade-ui}
rotations:
  - {rotation: triage, person: triage.lead, from: 2026-11-09, to: 2026-11-15}
  - {rotation: harness_oncall, person: bkhan, from: 2026-11-09, to: 2026-11-15}
"""))
CHANNELS = slack.SlackChannels.from_reporting({"slack": {"on_call": "#sentinel-oncall", "triage": "#sentinel-triage",
                                                          "release": "#tos-release", "safety": "#sentinel-safety"}})


def ny(d, hh, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


def iso(at):
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def paper_mode_entries():
    """The 8 alerts.json entries of demo paper-mode-mismatch: 4 shards x (aborted_safety + UNPROVEN)."""
    out = []
    for n in (4, 3, 2, 1):
        shard = f"{RUN}/{n}"
        out.append({"control": "paper_mode", "detail": "selector text is not an allowlist entry",
                    "gate": "paper_mode_asserted", "incident": "S1", "notified": ["#sentinel-safety"], "paged": SAFE,
                    "priority": "P1", "rule": "safety:aborted_safety", "run_id": RUN, "run_outcome": "aborted_safety",
                    "run_reason": "live_identity", "safety_table": True, "shard_id": shard})
        out.append({"control": "proof", "detail": "UNPROVEN: session_unavailable", "gate": "teardown",
                    "incident": None, "notified": ["#sentinel-safety"],
                    "paged": ["harness_on_call", "system_owner", "accountable_owner"], "priority": "P1",
                    "rule": "safety:unproven_proof", "run_id": RUN, "run_outcome": None, "run_reason": None,
                    "safety_table": True, "shard_id": shard})
    return out


def entry(rule="env_error", gate="preflight.R1", shard=None, priority="P2", paged=(), notified=("on_call_channel",),
          detail="failed: R1", run_id=RUN, **kw):
    return {"run_id": run_id, "shard_id": shard, "rule": rule, "priority": priority, "paged": list(paged),
            "notified": list(notified), "safety_table": False, "control": "preflight", "gate": gate,
            "incident": None, "run_outcome": kw.get("run_outcome"), "run_reason": kw.get("run_reason"),
            "detail": detail}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    yield s
    s.close()


def begin(s, run_id, run_type="pr", started=None):
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, started_at=iso(started or ny(10, 10)), scope=()))
    return RunRef.from_store(s, run_id)


def jobs(s):
    return [tuple(r) for r in s.query("SELECT kind, destination, next_attempt_at FROM outbox ORDER BY job_id")]


# -- run-level collapse (9.10, 10.1; choice 1) -----------------------------------------------------------------------

def test_paper_mode_mismatch_yields_one_aborted_safety_page_and_one_unproven_page_each_listing_4_shards(store):
    run = begin(store, RUN)
    safety, unproven = alerting.raise_run_alerts(run, paper_mode_entries())
    assert (safety.rule, safety.priority, safety.reason, safety.incident) == \
        ("safety:aborted_safety", "P1", "live_identity", "S1")
    assert safety.shards == tuple(f"{RUN}/{n}" for n in (1, 2, 3, 4)) and unproven.shards == safety.shards
    assert safety.detail == "selector text is not an allowlist entry; shards 1, 2, 3, 4"
    assert unproven.rule == "safety:unproven_proof" and len(unproven.shard_details) == 4
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    for a in (safety, unproven):
        d.dispatch(a, ny(10, 11))
    pages = [j for j in jobs(store) if j[:2] == ("page", "paging")]
    assert len(pages) == 2 and len([j for j in jobs(store) if j[:2] == ("page", "email")]) == 2
    safety_posts = [j for j in jobs(store) if j[:2] == ("slack", "#sentinel-safety")]
    assert len(safety_posts) == 2 * 5                     # each alert: its message and one reply per shard


def test_a_safety_alert_names_the_runs_incident_rows(store):
    from sentinel.store.safety import open_incident
    run = begin(store, RUN)
    open_incident(store, run_id=RUN, rig_id="rig-01", login_alias=None, severity="S1", trigger="live_identity")
    safety, _ = alerting.raise_run_alerts(run, paper_mode_entries())
    got = Dispatcher(store, channels=CHANNELS, oncall=ONCALL).dispatch(safety, ny(10, 11))
    post = json.loads(next(q.payload for q in got.jobs if q.destination == "#sentinel-safety"))
    assert post["text"].endswith(f"shards 1, 2, 3, 4; incident 1 (S1) | https://sentinel.internal/runs/{RUN}")


def test_two_shards_failing_one_gate_give_one_env_error_alert_listing_both(store):
    run = begin(store, RUN)
    (a,) = alerting.raise_run_alerts(run, [entry(shard=f"{RUN}/1"), entry(shard=f"{RUN}/2", detail="failed: R1, R3")])
    assert a.key == f"{RUN}:env_error:preflight.R1" and a.status == "env_error (gate: preflight.R1)"
    assert a.detail == "failed: R1 (+1 more); shards 1, 2"
    other = alerting.raise_run_alerts(run, [entry(shard=f"{RUN}/1"), entry(gate="workspace_loaded", shard=f"{RUN}/2")])
    assert [x.gate for x in other] == ["preflight.R1", "workspace_loaded"]


def test_the_collapsed_route_takes_the_highest_priority_and_every_recipient():
    run = RunRef(RUN, "pr")
    (a,) = alerting.raise_run_alerts(run, [entry(shard="s1"), entry(shard="s2", priority="P1",
                                                                    paged=("login_owner",))])
    assert a.priority == "P1" and a.route.paged == ("login_owner",) and a.route.notified == ("on_call_channel",)
    assert alerting.raise_run_alerts(run, [{"alert": "dead_man", "closed_as": "late arrival", "at": "x"}]) == ()


# -- the route table (9.10 rows) -----------------------------------------------------------------------------------

ROW_TABLE = [
    (alerts.report_withheld(), "P1", {"security_contact", "system_owner"}, set()),
    (alerts.new_collateral(), "P2", set(), {"pr_author", alerts.TRIAGE_CHANNEL}),
    (alerts.needs_human_digest(), "P3", set(), {alerts.TRIAGE_CHANNEL}),
    (alerts.sla_escalation(), "P2", set(), {"system_owner"}),
    (alerts.sla_escalation(flow_owner=True), "P2", set(), {"system_owner", "flow_owner"}),
    (alerts.decision_conflict(1), "P2", set(), {alerts.TRIAGE_CHANNEL}),
    (alerts.decision_conflict(2), "P3", set(), {alerts.TRIAGE_CHANNEL}),
    (alerts.canary_failure("detector_blind"), "P1", {"harness_on_call"}, {alerts.ON_CALL_CHANNEL}),
    (alerts.canary_failure("generation_failed"), "P1", {"harness_on_call"}, {alerts.ON_CALL_CHANNEL}),
    (alerts.canary_failure("detector_degraded"), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.canary_path_divergence(), "P1", {"harness_on_call"}, {alerts.ON_CALL_CHANNEL}),
    (alerts.aa_drift(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.reviewer_backlog(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.reviewer_unavailable(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.pool_saturated(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.global_rendering_shift(), "P2", set(), {alerts.ON_CALL_CHANNEL, "system_owner"}),
    (alerts.redaction_miss(), "P1", {"security_contact", "flow_owner"}, set()),
    (alerts.integration_outage(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.quarantine_ceiling(), "P2", set(), {"system_owner"}),
    (alerts.nightly_skipped_for_rc(), "P2", set(), {"harness_on_call", "system_owner"}),
    (alerts.rc_start_deadline(), "P2", set(), {"release_manager", "system_owner", alerts.RELEASE_CHANNEL}),
    (alerts.store_health(), "P2", set(), {alerts.ON_CALL_CHANNEL, "system_owner"}),
    (alerts.coverage_loss(), "P3", set(), {"harness_on_call"}),
    (alerts.coverage_loss(above_r22=True), "P2", set(), {"harness_on_call", alerts.ON_CALL_CHANNEL}),
    (alerts.gateway_safety_drop(), "P1", {"harness_on_call", "flow_owner"}, set()),
    (alerts.dead_man("merge"), "P1", {"harness_on_call"}, set()),
    (alerts.dead_man("pr"), "P2", {"harness_on_call"}, set()),
    (alerts.aborted_harness(), "P1", {"harness_on_call"}, set()),
    (alerts.env_error(), "P2", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.env_error(repeat=True), "P1", set(), {alerts.ON_CALL_CHANNEL}),
    (alerts.harness_error(repeat=True), "P1", set(), {alerts.ON_CALL_CHANNEL}),
]


@pytest.mark.parametrize("route,priority,paged,notified", ROW_TABLE, ids=lambda x: getattr(x, "rule", None))
def test_each_910_row_has_its_priority_and_recipients(route, priority, paged, notified):
    assert route.priority == priority and paged <= set(route.paged) and notified <= set(route.notified)


def test_the_release_channel_is_a_recipient():
    assert alerts.RELEASE_CHANNEL in alerts.RECIPIENTS and "pr_author" in alerts.RECIPIENTS
    with pytest.raises(ValueError):
        alerts.canary_failure("ok")


# -- repeats and the freeze (9.10, 11.6; choice 6) -------------------------------------------------------------------

def test_harness_error_in_two_consecutive_runs_is_p1_on_the_second(store):
    begin(store, "run-a", started=ny(9, 10))
    store.update_summary("run-a", {"alerts": [entry(rule="harness_error", gate="running", run_id="run-a")]})
    run = begin(store, "run-b", started=ny(10, 10))
    (a,) = alerting.raise_run_alerts(run, [entry(rule="harness_error", gate="running", run_id="run-b")])
    assert a.priority == "P2"
    (b,) = alerting.escalate_repeats(store, run, [a])
    assert b.priority == "P1" and "repeat of the previous pr run run-a" in b.detail
    begin(store, "run-n", "nightly", started=ny(9, 23))                         # another type does not count
    assert alerting.previous_run(store, run) == "run-a"


def test_env_error_on_the_same_gate_in_two_consecutive_runs_is_p1_for_every_gate(store):
    begin(store, "run-a", started=ny(9, 10))
    store.update_summary("run-a", {"alerts": [entry(gate="workspace_loaded", run_id="run-a")]})
    run = begin(store, "run-b", started=ny(10, 10))
    same, other = alerting.raise_run_alerts(run, [entry(gate="workspace_loaded"), entry(gate="launched")])
    out = alerting.escalate_repeats(store, run, [same, other])
    assert [a.priority for a in out] == ["P1", "P2"]
    first = begin(store, "run-0", started=ny(8, 10))
    assert alerting.escalate_repeats(store, first, [same]) == (same,)          # no previous run


def test_two_aborted_harness_runs_in_a_week_freeze_harness_changes(store):
    for run_id, day in (("run-n1", 4), ("run-n2", 9)):
        begin(store, run_id, "nightly", started=ny(day, 2))
        store.end_run(RunOutcome(run_id, "nightly", "aborted_harness", "running", reason="dead_man"))
    assert alerting.harness_freeze(store, ny(12, 9)) is None                    # Nov 4 is outside the 7 days
    begin(store, "run-n3", "nightly", started=ny(11, 2))
    store.end_run(RunOutcome("run-n3", "nightly", "aborted_harness", "running", reason="hook_lost"))
    value = alerting.harness_freeze(store, ny(12, 9))
    assert value and "run-n2, run-n3" in value and store.get_system_state("harness_freeze") == value
    assert alerting.harness_freeze(store, ny(12, 10)) is None                   # set once


# -- dispatch: quiet hours, rate limit, digest, paging, threads (choices 2 to 4) -----------------------------------

def test_a_p2_at_2310_is_held_to_0600_while_aborted_harness_pages_at_once(store):
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    held = d.dispatch(Alert.of(alerts.env_error(), detail="failed: R1", key="k1", run=run), ny(10, 23, 10))
    assert held.disposition == "held" and held.due_at == ny(11, 6).astimezone(timezone.utc)
    assert {j[2] for j in jobs(store)} == {iso(ny(11, 6))}
    now = d.dispatch(Alert.of(alerts.aborted_harness(), detail="hook_lost", key="k2", run=run), ny(10, 23, 10))
    assert now.disposition == "posted" and now.due_at == ny(10, 23, 10)
    assert [j for j in jobs(store) if j[:2] == ("page", "paging")] == [("page", "paging", None)]


def test_after_hours_pages_follow_the_list_of_910_and_sd84():
    for route in (alerts.safety_abort(), alerts.aborted_harness(), alerts.gateway_safety_drop(),
                  alerts.explorer_order_stage(), alerts.drill_failure(), alerts.unproven_proof(),
                  alerts.canary_failure("detector_blind"), alerts.canary_path_divergence(), alerts.redaction_miss(),
                  alerts.dead_man("nightly")):
        assert alerting.pages_after_hours(route), route.rule
    for route in (alerts.canary_failure("detector_degraded"), alerts.env_error(repeat=True), alerts.report_withheld(),
                  alerts.store_health()):
        assert not alerting.pages_after_hours(route), route.rule


def test_two_pool_saturated_within_an_hour_give_one_alert(store):
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    first = d.dispatch(Alert.of(alerts.pool_saturated(), detail="depth 4", key="ps1"), ny(10, 11))
    second = d.dispatch(Alert.of(alerts.pool_saturated(), detail="depth 5", key="ps2"), ny(10, 11, 40))
    third = d.dispatch(Alert.of(alerts.pool_saturated(), detail="depth 6", key="ps3"), ny(10, 12, 1))
    assert [x.disposition for x in (first, second, third)] == ["posted", "rate_limited", "posted"]
    assert d.dispatch(Alert.of(alerts.pool_saturated(), detail="again", key="ps3"), ny(10, 14)).disposition == \
        "duplicate"


def test_p3_waits_for_the_digest_and_queues_nothing(store):
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    assert d.dispatch(Alert.of(alerts.coverage_loss(), detail="3 budget", key="c"), ny(10, 11)).disposition == "digest"
    assert jobs(store) == []


def test_p2_posts_mention_the_rotation_and_an_auth_failure_pages_without_email(store):
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    route = alerts.env_error(notified=("login_owner", "accounts_owner")).plus(paged=("login_owner", "accounts_owner"))
    got = d.dispatch(Alert.of(route, detail="authentication_failed", key="auth", run=run,
                              people={"login_owner": "rig01.owner"}), ny(10, 11))
    kinds = [(q.kind, q.destination) for q in got.jobs]
    assert kinds == [("page", "paging"), ("slack", "#sentinel-oncall")]                # SD-62: paged at P2
    page = json.loads(got.jobs[0].payload)
    assert page["to"] == ["rig01.owner", "asmith"] and page["roles"] == ["login_owner", "accounts_owner"]
    post = json.loads(got.jobs[1].payload)
    assert post["mentions"] == ["rig01.owner", "asmith", "bkhan"] and not post["reply"]


def test_later_alerts_of_one_run_and_kind_thread_under_the_first(store):
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    a, b = alerting.raise_run_alerts(run, [entry(gate="launched"), entry(gate="workspace_loaded")])
    first, second = d.dispatch(a, ny(10, 11)), d.dispatch(b, ny(10, 11))
    p1, p2 = json.loads(first.jobs[0].payload), json.loads(second.jobs[0].payload)
    assert p1["thread"] == p2["thread"] == f"{RUN}/env_error" and (p1["reply"], p2["reply"]) == (False, True)


def test_the_message_string_matches_the_910_format(store):
    run = begin(store, RUN)
    (a,) = alerting.raise_run_alerts(run, [entry(shard=f"{RUN}/1")])
    assert a.fields().render() == (f"[Sentinel] P2 env_error | pr {RUN} | - | env_error (gate: preflight.R1) | "
                                   f"failed: R1; shard 1 | https://sentinel.internal/runs/{RUN}")
    safety, _ = alerting.raise_run_alerts(run, paper_mode_entries())
    assert safety.fields().render().startswith(
        f"[Sentinel] P1 aborted_safety | pr {RUN} | - | aborted_safety (reason: live_identity) | ")
    assert safety.entry()["shards"] == list(safety.shards) and safety.entry()["rule"] == "safety:aborted_safety"


# -- delivery, the undelivered watch and acks (choices 4 and 5) ----------------------------------------------------

def test_a_page_no_channel_accepts_within_5_minutes_writes_alert_undelivered(store, monkeypatch):
    monkeypatch.setattr(store_mod, "SYSTEM_STATE_KEYS", store_mod.SYSTEM_STATE_KEYS | {"alert_undelivered"})
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    lost = d.dispatch(Alert.of(alerts.aborted_harness(), detail="hook_lost", key="lost", run=run), ny(10, 11))
    ok = d.dispatch(Alert.of(alerts.dead_man("merge"), detail="no run_completed", key="ok", run=run), ny(10, 11))
    store.mark_sent(ok.jobs[-1].job_id, at=iso(ny(10, 11, 2)))                   # Slack accepted it
    assert d.check_undelivered(ny(10, 11, 4)) == () and len(d.pending) == 2
    (late,) = d.check_undelivered(ny(10, 11, 5))
    assert late is lost and d.pending == []
    (row,) = json.loads(store.get_system_state("alert_undelivered"))
    assert row["key"] == "lost" and row["rule"] == "aborted_harness" and row["run_id"] == RUN


def test_page_jobs_go_to_the_pager_and_the_p1_email_to_the_mailer(store):
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    got = d.dispatch(Alert.of(alerts.aborted_harness(), detail="hook_lost", key="p1", run=run), ny(10, 11))
    pager, mailer = alerting.MemoryPager(), alerting.MemoryMailer()
    page, mail = got.jobs[0], got.jobs[1]
    assert alerting.send_page_job(page.payload, pager=pager, mailer=mailer) == "page-1"
    assert alerting.send_page_job(mail.payload, pager=pager, mailer=mailer) == "mail-1"
    assert pager.pages[0]["people"] == ["bkhan"] and pager.pages[0]["text"].startswith("[Sentinel] P1 aborted_harness")
    assert mailer.sent[0]["subject"] == "[Sentinel] P1 aborted_harness" and mailer.sent[0]["to"] == ["bkhan"]


def test_an_acknowledgement_is_mirrored_into_summary_json(store):
    begin(store, RUN)
    alerting.record_ack(store, RUN, f"{RUN}:safety:aborted_safety:live_identity", by="bkhan", at=ny(10, 11, 3))
    summary = alerting.record_ack(store, RUN, f"{RUN}:safety:unproven_proof:-", by="asmith", at=ny(10, 11, 4))
    assert [a["by"] for a in store.run_summary(RUN)["alert_acks"]] == ["bkhan", "asmith"]
    assert summary["alert_acks"][0] == {"key": f"{RUN}:safety:aborted_safety:live_identity", "by": "bkhan",
                                        "at": iso(ny(10, 11, 3)), "via": "paging_service"}


def test_a_held_alerts_undelivered_clock_starts_at_0600(store, monkeypatch):
    monkeypatch.setattr(store_mod, "SYSTEM_STATE_KEYS", store_mod.SYSTEM_STATE_KEYS | {"alert_undelivered"})
    run = begin(store, RUN)
    d = Dispatcher(store, channels=CHANNELS, oncall=ONCALL)
    route = alerts.env_error().plus(paged=("accounts_owner",))
    d.dispatch(Alert.of(route, detail="authentication_challenge", key="night", run=run), ny(10, 23))
    assert d.check_undelivered(ny(10, 23) + timedelta(minutes=30)) == ()
    assert len(d.check_undelivered(ny(11, 6, 5))) == 1
