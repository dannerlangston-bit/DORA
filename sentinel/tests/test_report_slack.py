"""Slack routing, the PR-run filter, threading and the cross-run dedup (9.9, 9.10 message format; SD-85, SD-87)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator import alerts
from sentinel.report import slack
from sentinel.report.slack import FindingPost, SlackChannels, SlackHistory
from sentinel.store import RunPlan, ScopeEntry, Store

RUN = "run-2026-11-09-pr-4812"
AT = "charts.flexible-grid.active-trader-cell"
SWEEP = f"sweep/{AT}/flexgrid/480x360"
SWEEP2 = f"sweep/{AT}/flexgrid/640x480"
CENSUS = f"census/{AT}/ws.tier-a.active-trader"
L2 = "census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader"
NOW = datetime(2026, 11, 9, 16, 12, tzinfo=timezone.utc)
CROP = "a81d" + "0" * 60
CHANNELS = SlackChannels.from_reporting({"reporting": {"slack": {
    "on_call": "#sentinel-oncall", "triage": "#sentinel-triage", "release": "#tos-release",
    "safety": "#sentinel-safety"}}})


def post(fid, kind, tier, cid, **kw):
    base = dict(run_id=RUN, run_type="pr", finding_id=fid, kind=kind, tier=tier, description="ladder viewport moved",
                checkpoint_id=cid, region_id="rgn.ladder.viewport", signature="3f9c1a0e55b2d8c4",
                reviewer_confidence=0.9, pr_author="dev.author", surface=AT,
                build_pair="branch-1998.3.0-7be42d0 vs main-1998.3.0-2c91ae4", category=kind)
    base.update(kw)
    return FindingPost(**base)


def sample_93():
    """The four Unexpected items of the 9.3 sample: an invariant, two tier-1 collateral, one tier-2 collateral."""
    return [post("F-20931", "invariant_violation", 1, SWEEP, reviewer_confidence=None, review_needed=False),
            post("F-20932", "collateral", 1, SWEEP2, signature="91b0aaaa00000000"),
            post("F-20934", "collateral", 1, CENSUS, signature="4d60bbbb00000000"),
            post("F-20933", "collateral", 2, L2, signature="77aacccc00000000")]


# -- the 9.3 sample and the PR-run filter -----------------------------------------------------------------------------

def test_the_93_sample_posts_one_invariant_message_and_one_collateral_thread():
    msgs = slack.plan_finding_posts(sample_93(), history=SlackHistory(), now=NOW)
    assert [(m.fields.report_link.rsplit("/", 1)[1], m.thread, m.reply) for m in msgs] == [
        ("F-20931", f"{RUN}/tier1-invariant_violation", False),
        ("F-20932", f"{RUN}/tier1-collateral", False),
        ("F-20934", f"{RUN}/tier1-collateral", True)]                  # item 3 replies under item 2
    assert all(m.channel == alerts.TRIAGE_CHANNEL and m.mentions == ("dev.author",) for m in msgs)


def test_tier2_collateral_is_not_posted_on_a_pr_run_and_other_kinds_never_post():
    tier2 = sample_93()[3]
    assert not slack.should_post_pr(tier2) and slack.route(tier2) == ()
    assert not slack.should_post_pr(post("F-9", "needs_baseline", 1, SWEEP))
    assert slack.should_post_pr(post("F-8", "assertion_failed", 1, SWEEP))


def test_collateral_posts_at_confidence_080_or_with_no_reviewer_and_never_with_a_decision():
    assert slack.triage_post(post("F-1", "collateral", 1, SWEEP, reviewer_confidence=0.80))
    assert not slack.triage_post(post("F-1", "collateral", 1, SWEEP, reviewer_confidence=0.79))
    assert not slack.triage_post(post("F-1", "collateral", 1, SWEEP, reviewer_confidence=None))
    assert slack.triage_post(post("F-1", "collateral", 1, SWEEP, reviewer_confidence=None, review_needed=False))
    assert not slack.triage_post(post("F-1", "invariant_violation", 1, SWEEP, decided=True))


def test_a_stored_finding_becomes_a_post():
    from sentinel.records import Finding
    f = Finding("F-20932", RUN, "collateral", "ladder viewport moved", RUN, checkpoint_id=SWEEP2, tier=1,
                region_id="rgn.ladder.viewport", phash="91b0aaaa00000000", category="collateral")
    p = FindingPost.from_finding(f, run_type="pr", reviewer_confidence=0.81, pr_author="dev.author")
    assert (p.surface, p.signature, p.decided, p.alert_kind) == (AT, "91b0aaaa00000000", False, "tier1-collateral")
    assert slack.triage_post(p)


def test_a_merge_run_post_mentions_nobody():
    (m,) = slack.plan_finding_posts([post("F-1", "invariant_violation", 1, SWEEP, run_type="merge")],
                                    history=SlackHistory(), now=NOW)
    assert m.mentions == ()


# -- the cross-run dedup -----------------------------------------------------------------------------------------------

def test_the_same_signature_three_hours_later_is_not_posted_and_after_24_hours_it_is():
    hist = SlackHistory()
    first = slack.plan_finding_posts([post("F-1", "collateral", 1, SWEEP2)], history=hist, now=NOW)
    assert len(first) == 1 and len(hist.records) == 1
    later = post("F-2", "collateral", 1, SWEEP2, run_id="run-2026-11-09-pr-4813", signature="3f9c1a0e55b2d8c5")
    assert slack.plan_finding_posts([later], history=hist, now=NOW + timedelta(hours=3)) == []
    assert len(slack.plan_finding_posts([later], history=hist, now=NOW + timedelta(hours=25))) == 1


def test_an_open_ticket_makes_a_thread_reply_on_the_original_under_7_days():
    hist = SlackHistory()
    slack.plan_finding_posts([post("F-1", "collateral", 1, SWEEP2)], history=hist, now=NOW)
    ticketed = post("F-2", "collateral", 1, SWEEP2, run_id="run-2026-11-10-pr-4820", open_ticket="TOS-48391")
    (m,) = slack.plan_finding_posts([ticketed], history=hist, now=NOW + timedelta(days=2))
    assert m.reply and m.thread == f"{RUN}/tier1-collateral"
    assert slack.plan_finding_posts([ticketed], history=hist, now=NOW + timedelta(days=8)) == []


def test_a_different_checkpoint_region_or_signature_is_a_new_post():
    hist = SlackHistory()
    slack.plan_finding_posts([post("F-1", "collateral", 1, SWEEP2)], history=hist, now=NOW)
    t = NOW + timedelta(hours=1)
    assert slack.cross_run_dedup(hist, SWEEP2, "3f9c1a0e55b2d8c4", t, region_id="rgn.ladder.viewport").action == "skip"
    assert slack.cross_run_dedup(hist, SWEEP, "3f9c1a0e55b2d8c4", t, region_id="rgn.ladder.viewport").action == "post"
    assert slack.cross_run_dedup(hist, SWEEP2, "3f9c1a0e55b2d8c4", t, region_id="rgn.other").action == "post"
    assert slack.cross_run_dedup(hist, SWEEP2, "c063e5f1aa4d273b", t, region_id="rgn.ladder.viewport").action == "post"


# -- alert routing (SD-85, choice 1) ----------------------------------------------------------------------------------

def test_safety_events_go_to_on_call_and_the_safety_channel_and_nothing_else_does():
    for r in (alerts.safety_abort(), alerts.unproven_proof(), alerts.explorer_order_stage(), alerts.drill_failure()):
        assert slack.route(r) == (alerts.ON_CALL_CHANNEL, alerts.SAFETY_CHANNEL)
    others = [alerts.env_error(), alerts.aborted_harness(), alerts.dead_man("pr"), alerts.pool_saturated(),
              alerts.rc_start_deadline(), alerts.canary_failure("detector_blind"), alerts.new_collateral(),
              alerts.sla_escalation(), alerts.report_withheld(), alerts.redaction_miss(), alerts.store_health()]
    assert all(alerts.SAFETY_CHANNEL not in slack.route(r) for r in others)


def test_alerts_post_to_on_call_whatever_the_run_type_and_rows_add_their_channels():
    assert slack.route(alerts.dead_man("pr")) == (alerts.ON_CALL_CHANNEL,)
    assert slack.route(alerts.rc_start_deadline()) == (alerts.ON_CALL_CHANNEL, alerts.RELEASE_CHANNEL)
    assert slack.route(alerts.canary_failure("detector_degraded")) == (alerts.ON_CALL_CHANNEL, alerts.RELEASE_CHANNEL)
    assert slack.route(alerts.decision_conflict(1)) == (alerts.TRIAGE_CHANNEL,)
    assert slack.route(alerts.sla_escalation(flow_owner=True)) == (alerts.TRIAGE_CHANNEL,)


def test_the_rc_start_deadline_and_canary_routes_of_other_modules_reach_the_release_channel():
    from sentinel.canary_integrations import alert_route
    from sentinel.orchestrator import start_deadline
    assert slack.route(start_deadline.rc_start_deadline_alert()) == (alerts.ON_CALL_CHANNEL, alerts.RELEASE_CHANNEL)
    assert alerts.RELEASE_CHANNEL in slack.route(alert_route("detector_blind"))
    assert alerts.RELEASE_CHANNEL not in slack.route(alerts.aborted_harness())


def test_channels_come_from_reporting_yaml():
    assert CHANNELS.name(alerts.SAFETY_CHANNEL) == "#sentinel-safety"
    assert CHANNELS.name(alerts.ON_CALL_CHANNEL) == "#sentinel-oncall"
    with pytest.raises(ValueError, match="release"):
        SlackChannels.from_reporting({"slack": {"on_call": "a", "triage": "b", "safety": "#sentinel-safety"}})


# -- the message format and escaping ----------------------------------------------------------------------------------

def test_the_message_matches_the_910_format():
    f = slack.MessageFields("P1", "aborted_safety", "pr", RUN, "b1 vs m0", "aborted_safety (reason: live_identity)",
                            "selector text is not an allowlist entry", f"https://sentinel.internal/runs/{RUN}",
                            crop_link=f"https://sentinel.internal/a/{CROP}")
    assert f.render() == (f"[Sentinel] P1 aborted_safety | pr {RUN} | b1 vs m0 | aborted_safety (reason: "
                          f"live_identity) | selector text is not an allowlist entry | "
                          f"https://sentinel.internal/runs/{RUN} | https://sentinel.internal/a/{CROP}")
    with_surface = slack.finding_fields(sample_93()[1], slack.Urls("https://sentinel.internal", RUN))
    assert with_surface.render().split(" | ")[4] == f"{AT} tier 1"
    assert with_surface.render().split(" | ")[6] == "https://sentinel.internal/f/F-20932"


def test_only_text_and_artifact_links_leave():
    with pytest.raises(ValueError, match="/a/<sha256>"):
        slack.MessageFields("P2", "x", None, None, None, None, "d", None, crop_link="https://evil.example/x.png")
    text = slack.slack_text("<!channel> @here see http://evil.example & www.x.io\nnext line")
    assert "<" not in text and ">" not in text and "&amp;" in text and "\n" not in text
    assert "@here" not in text and "://" not in text and "www." not in text
    assert len(slack.slack_text("x" * 1000)) == slack.DETAIL_CHARS


# -- the outbox and the history read back -------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    s.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, started_at=NOW.isoformat(),
                        scope=(ScopeEntry(SWEEP, "ws.tier-c.sweep-harness", "active_trader", 1, "frozen"),)))
    yield s
    s.close()


def test_posts_are_outbox_jobs_once_and_the_history_reads_them_back(store):
    msgs = slack.plan_finding_posts(sample_93(), history=SlackHistory(), now=NOW)
    queued = slack.enqueue(store, msgs, CHANNELS, NOW)
    assert [q.destination for q in queued] == ["#sentinel-triage"] * 3
    rows = store.query("SELECT kind, destination, run_id, state, next_attempt_at FROM outbox ORDER BY job_id")
    assert [tuple(r) for r in rows] == [("slack", "#sentinel-triage", RUN, "queued", None)] * 3
    p = json.loads(queued[1].payload)
    assert p["egress_purpose"] == "slack" and p["checkpoint_id"] == SWEEP2 and p["dedup"]["signature"]
    assert p["text"].startswith("[Sentinel] P2 new tier-1 collateral | pr ")
    assert slack.enqueue(store, msgs, CHANNELS, NOW) == ()                       # the same posts are not re-queued
    blobs = {q.payload_sha256: q.payload for q in queued}
    hist = SlackHistory.from_store(store, NOW + timedelta(hours=3), blobs.get)
    assert {(r.checkpoint_id, r.reply) for r in hist.records} == {(SWEEP2, False), (CENSUS, True)}
    again = post("F-1", "collateral", 1, SWEEP2, run_id="run-x", signature="91b0aaaa00000000")
    assert slack.plan_finding_posts([again], history=hist, now=NOW + timedelta(hours=3)) == []


def test_held_jobs_wait_and_unknown_runs_are_not_referenced(store):
    m = slack.SlackMessage(alerts.ON_CALL_CHANNEL, slack.MessageFields("P2", "pool_saturated", None, "run-queued",
                                                                       None, None, "depth 4", None),
                           thread=slack.thread_key("run-queued", "pool_saturated"), run_id="run-queued")
    (q,) = slack.enqueue(store, [m], CHANNELS, NOW, release_at=NOW + timedelta(hours=7))
    row = store.query("SELECT run_id, next_attempt_at FROM outbox WHERE job_id = ?", (q.job_id,))[0]
    assert row["run_id"] is None and row["next_attempt_at"] == (NOW + timedelta(hours=7)).isoformat()
