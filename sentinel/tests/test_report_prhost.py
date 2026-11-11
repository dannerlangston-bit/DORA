"""The PR comment and commit status through the outbox (9.1, 9.2, 9.6, 10.7): report/prhost.py with a fake PR host,
a fake clock and runs read back from a real store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.egress import EgressAllowlist
from sentinel.records import CheckpointOutcome, Decision, Finding, RunOutcome, Signature
from sentinel.report import ReportContext
from sentinel.report import outbox as ob
from sentinel.report import prhost as ph
from sentinel.report import reporting_config as rc
from sentinel.report.pr_comment import comment_marker
from sentinel.store.artifacts import ArtifactStore
from sentinel.store.store import Review, RunPlan, ScopeEntry, Store

WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
REGION = "rgn.ladder.bid-column"
PHASH = "0123456789abcdef"
PR = 4812
BASE_URL = "https://sentinel.internal"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)        # 10:00 in New York; budget 45 min: deadline 15:45
SCM = "scm.example.internal"
ALLOW = EgressAllowlist.from_dict({"hosts": [{"host": SCM, "purposes": ["pr_comment"]},
                                             {"host": "slack.com", "purposes": ["slack"]}],
                                   "signed_by": "sec", "signed_at": "2026-11-01T00:00:00Z", "signature": "pgp:x"})
COMMITS = {"cand-1": "a" * 40, "cand-2": "c" * 40, "base-1": "b" * 40}


def t(minutes: float = 0) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "sentinel.db")
    for b, sha in COMMITS.items():
        s.register_build(b, channel="main" if b.startswith("base") else "branch", source_commit=sha,
                         pipeline_artifact_url=f"https://ci/{b}", installer_sha256=sha[:1] * 64)
    yield s
    s.close()


@pytest.fixture
def arts(tmp_path):
    return ArtifactStore.at(tmp_path / "artifacts", "outbox")


@pytest.fixture
def host():
    return ph.FakePrHost(SCM)


def run(store, run_id, *, start=0, end=40, cand="cand-1", outcome="collateral", pending=False, pair=True):
    """A finished PR run with one tier-1 census checkpoint: an undecided collateral finding by default."""
    pair_id = store.register_pair(cand, "base-1", "pr") if pair else None
    store.begin_run(RunPlan(run_id=run_id, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),),
                            pair_id=pair_id, pr_ref=f"PR {PR}", started_at=t(start)))
    o = CheckpointOutcome(run_id=run_id, checkpoint_id=CENSUS, outcome=outcome, workspace_id=WS,
                          persona="active_trader", data_mode="frozen", tier=1, surface_id="trade.active-trader.ladder")
    findings = []
    if pending:
        o = CheckpointOutcome(run_id=run_id, checkpoint_id=CENSUS, outcome="needs_human", reason="reviewer_unavailable",
                              review_state="pending", workspace_id=WS, persona="active_trader", data_mode="frozen",
                              tier=1, surface_id="trade.active-trader.ladder")
    elif outcome == "collateral":
        findings = [Finding(finding_id=f"fnd-{run_id}", run_id=run_id, kind="collateral", description="ladder moved",
                            first_seen_run_id=run_id, checkpoint_id=CENSUS, region_id=REGION, phash=PHASH, tier=1)]
    store.write_checkpoint(o, findings=findings)
    if end is not None:
        store.end_run(RunOutcome(run_id, "pr", "complete", "reported", started_at=t(start), ended_at=t(end)))


def ctx(cand="cand-1") -> ReportContext:
    return ReportContext(pr=0, candidate_build=cand, base_build="base-1", base_url=BASE_URL)


def post(store, arts, host, run_id, at, **kw):
    return ph.post_report(store, arts, run_id, host, kw.pop("context", ctx()), allowlist=ALLOW, now_=t(at), **kw)


def deliver(store, arts, host, at):
    return ob.dispatch(store, arts, ph.senders(host), t(at), allowlist=ALLOW)


def status(host, cand="cand-1"):
    return host.statuses[(COMMITS[cand], "sentinel/pr-run")]


# -- the comment -------------------------------------------------------------------------------------------------

def test_the_first_run_creates_the_comment_and_posts_the_status(store, arts, host):
    run(store, "run-1")
    posted = post(store, arts, host, "run-1", 41)
    assert (posted.marker_recreated, posted.report_late, posted.comment_overdue, posted.history) == (
        False, False, False, ())
    assert posted.comment.startswith(comment_marker(PR)) and posted.status.state == "failure"
    rep = deliver(store, arts, host, 41)
    assert [(a.kind, a.state) for a in rep.attempts] == [("pr_comment", "sent"), ("commit_status", "sent")]
    (comment,) = host.comments[PR]
    assert comment.body == posted.comment
    assert status(host) == {"commit": COMMITS["cand-1"], "context": "sentinel/pr-run", "state": "failure",
                            "description": "1 tier-1 finding has no decision (informational)",
                            "target_url": f"{BASE_URL}/runs/run-1"}
    rows = store.query("SELECT destination, purpose, status FROM egress_log ORDER BY egress_id")
    assert [tuple(r) for r in rows] == [(SCM, "pr_comment", "sent")] * 2     # no lookup before the first comment


def test_the_second_run_edits_the_same_comment_and_lists_the_first_in_its_history(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    run(store, "run-2", start=60, end=100, cand="cand-2", outcome="identical")
    posted = post(store, arts, host, "run-2", 101, context=ctx("cand-2"))
    assert posted.history == ({"run_id": "run-1", "candidate": "cand-1", "result": "complete",
                               "counts": "1 unexpected, 0 intended, 0 suppressed", "reported": t(41)},)
    deliver(store, arts, host, 101)
    (comment,) = host.comments[PR]
    assert comment.comment_id == "c1" and comment.body == posted.comment
    history = comment.body.split("<summary>Run history</summary>")[1]
    assert "| run-1 | cand-1 | complete | 1 unexpected, 0 intended, 0 suppressed |" in history
    assert status(host, "cand-2")["state"] == "success" and status(host)["state"] == "failure"


def test_an_earlier_run_that_never_posted_shows_its_outcome_counts(store, arts, host):
    run(store, "run-1")
    run(store, "run-2", start=60, end=100, cand="cand-2", outcome="identical")
    (row,) = post(store, arts, host, "run-2", 101, context=ctx("cand-2")).history
    assert (row["counts"], row["reported"]) == ("1 collateral", "not posted")


def test_a_deleted_marker_comment_is_recreated_and_run_health_says_so(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    host.delete_comment(PR, "c1")
    run(store, "run-2", start=60, end=100, cand="cand-2", outcome="identical")
    posted = post(store, arts, host, "run-2", 101, context=ctx("cand-2"))
    note = "(the marker comment was missing; a new comment was created)"
    assert posted.marker_recreated and note in posted.comment
    lookup = store.query("SELECT purpose, status, bytes, run_id FROM egress_log ORDER BY egress_id DESC LIMIT 1")
    assert tuple(lookup[0]) == ("pr_comment", "sent", 0, "run-2")             # the render-time lookup is logged
    deliver(store, arts, host, 101)
    assert [c.comment_id for c in host.comments[PR]] == ["c2"]
    again = post(store, arts, host, "run-2", 105, context=ctx("cand-2"))       # a later edit keeps the note
    assert again.marker_recreated and note in again.comment
    deliver(store, arts, host, 105)
    assert [c.comment_id for c in host.comments[PR]] == ["c2"]


def test_the_first_runs_own_comment_deleted_is_recreated_on_its_next_edit(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    assert not post(store, arts, host, "run-1", 42).marker_recreated         # the marker is there: no note
    host.delete_comment(PR, "c1")
    assert post(store, arts, host, "run-1", 50).marker_recreated
    deliver(store, arts, host, 50)
    assert [c.comment_id for c in host.comments[PR]] == ["c2"]


def test_a_failed_lookup_assumes_the_marker_is_there(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    run(store, "run-2", start=60, end=100, cand="cand-2", outcome="identical")
    host.down = True
    assert not post(store, arts, host, "run-2", 101, context=ctx("cand-2")).marker_recreated
    last = store.query("SELECT status, response_status FROM egress_log ORDER BY egress_id DESC LIMIT 1")
    assert tuple(last[0]) == ("failed", 503)


def test_a_comment_posted_after_the_budget_deadline_carries_report_late(store, arts, host):
    run(store, "run-1", end=48)
    posted = post(store, arts, host, "run-1", 50)
    assert posted.report_late and not posted.comment_overdue and posted.status.state == "failure"
    assert "| Wall clock / budget | unknown / 45:00 on 1 shard; report_late |" in posted.comment
    deliver(store, arts, host, 50)
    assert post(store, arts, host, "run-1", 52).report_late                  # its first post was late


def test_a_comment_posted_in_time_is_never_late_on_later_edits(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    later = post(store, arts, host, "run-1", 90)
    assert (later.report_late, later.comment_overdue) == (False, False)
    assert ph.overdue_runs(store, t(90)) == []


def test_no_comment_by_budget_plus_15_minutes_posts_status_error(store, arts, host):
    run(store, "run-1", end=None)                                            # the run never reported
    assert ph.overdue_runs(store, t(59)) == [] and ph.post_overdue(store, arts, "run-1", base_url=BASE_URL,
                                                                   now_=t(59)) is None
    assert ph.overdue_runs(store, t(61)) == ["run-1"]
    job = ph.post_overdue(store, arts, "run-1", base_url=BASE_URL, now_=t(61))
    assert job is not None
    deliver(store, arts, host, 61)
    assert status(host) == {"commit": COMMITS["cand-1"], "context": "sentinel/pr-run", "state": "error",
                            "description": "Sentinel did not report (informational)",
                            "target_url": f"{BASE_URL}/runs/run-1"}
    assert ph.post_overdue(store, arts, "run-1", base_url=BASE_URL, now_=t(62)) is None   # once
    assert ph.overdue_runs(store, t(62)) == []
    store.end_run(RunOutcome("run-1", "pr", "complete", "reported", started_at=t(0), ended_at=t(70)))
    posted = post(store, arts, host, "run-1", 71)                            # the late report keeps the error
    assert posted.comment_overdue and posted.report_late and posted.status.state == "error"
    assert posted.status.reason == "Sentinel did not report"
    assert ph.overdue_runs(store, t(16 + 25 * 60)) == []                     # 24 hours on: not re-posted


def test_a_decision_write_re_posts_the_status_within_a_minute(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    deliver(store, arts, host, 41)
    assert status(host)["state"] == "failure"
    decided = 60
    d = Decision("dec-000001", "confirm_bug", "confirmed_bug", Signature(CENSUS, REGION, PHASH, "collateral"),
                 decided_by="alice", ticket="TOS-1", reason="real bug", decided_at=t(decided))
    store.record_decision(d, run_id="run-1", finding_id="fnd-run-1")
    posted = post(store, arts, host, "run-1", decided)
    assert posted.status.state == "success"
    rep = deliver(store, arts, host, decided + 0.5)                          # the dispatcher ticks 30 s later
    assert {a.kind for a in rep.attempts} == {"pr_comment", "commit_status"}
    sent = store.query("SELECT sent_at FROM outbox WHERE job_id = ?", (posted.status_job,))[0][0]
    assert datetime.fromisoformat(sent) - datetime.fromisoformat(t(decided)) < timedelta(minutes=1)
    assert status(host)["state"] == "success"
    (comment,) = host.comments[PR]
    assert comment.comment_id == "c1" and "**Suppressed by decision (1)**" in comment.body


def test_a_landed_verdict_re_renders_the_comment_in_place(store, arts, host):
    run(store, "run-1", pending=True)
    first = post(store, arts, host, "run-1", 41)
    assert "complete (unreviewed: 1 item pending review)" in first.comment
    assert "Reviewer pending: 1 item" in first.comment
    deliver(store, arts, host, 41)
    store.apply_review(Review(run_id="run-1", checkpoint_id=CENSUS, diff_hash="h1", model_id="model-x",
                              prompt_hash="p1", request_sha256="f" * 64, response_json="{}",
                              verdict="matches_manifest", mapped_outcome="intended", confidence=0.93))
    again = post(store, arts, host, "run-1", 50)
    assert "Reviewer pending" not in again.comment and "unreviewed" not in again.comment
    deliver(store, arts, host, 50)
    (comment,) = host.comments[PR]
    assert comment.comment_id == "c1" and comment.body == again.comment


def test_an_older_run_re_posts_its_status_but_leaves_the_comment_to_the_newer_run(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41)
    run(store, "run-2", start=60, end=100, cand="cand-2", outcome="identical")
    post(store, arts, host, "run-2", 101, context=ctx("cand-2"))
    deliver(store, arts, host, 101)
    older = post(store, arts, host, "run-1", 120)
    assert older.comment_job is None and older.status_job is not None
    deliver(store, arts, host, 120)
    assert len(host.comments[PR]) == 1 and "run-2" in host.comments[PR][0].body.splitlines()[1]


def test_a_run_without_a_build_pair_posts_no_status(store, arts, host):
    run(store, "run-1", pair=False)
    posted = post(store, arts, host, "run-1", 41)
    assert posted.status_job is None and posted.comment_job is not None
    assert ph.post_pending(store, arts, "run-1", base_url=BASE_URL, now_=t(0)) is None


# -- the status --------------------------------------------------------------------------------------------------

def test_pending_at_t0_then_the_final_status_in_order(store, arts, host):
    run(store, "run-1", end=None)
    ph.post_pending(store, arts, "run-1", base_url=BASE_URL, now_=t(0))
    host.down = True
    deliver(store, arts, host, 0)                                            # pending fails at T+0:00
    store.end_run(RunOutcome("run-1", "pr", "complete", "reported", started_at=t(0), ended_at=t(40)))
    post(store, arts, host, "run-1", 40.5)
    host.down = False
    deliver(store, arts, host, 41)
    assert [s["state"] for s in host.status_log] == ["pending", "failure"]
    assert host.status_log[0]["description"] == "Sentinel run started, base = base-1 (informational)"


def test_the_check_mode_comes_from_the_dated_history_in_new_york_time(store, arts, host):
    data = _config([("informational", "2026-01-05"), ("blocking", "2026-11-10")])
    run(store, "run-1")
    posted = post(store, arts, host, "run-1", 41, reporting=data)
    assert posted.status.mode == "blocking" and posted.status.blocks_merge
    deliver(store, arts, host, 41)
    assert status(host)["description"] == "1 tier-1 finding has no decision"
    early = ph.post_pending(store, arts, "run-1", base_url=BASE_URL, reporting=data,
                            now_="2026-11-10T03:00:00+00:00")                # still 2026-11-09 in New York
    payload = store.query("SELECT payload_sha256 FROM outbox WHERE job_id = ?", (early,))[0][0]
    assert arts.get(payload.split(":")[1]).decode().count("(informational)") == 1


def _config(history):
    d = {"schema_version": 1, "check_mode_history": [{"mode": m, "since": s, "by": "asmith"} for m, s in history],
         "jira": {"product_project": "TOS", "harness_project": "SENT"},
         "slack": {"on_call": "#sentinel-oncall", "triage": "#sentinel-triage", "release": "#sentinel-release",
                   "safety": "#sentinel-safety"}, "paging": {"service": "pager"}, "auto_file": {}}
    return rc.from_dict(d)


def test_a_mode_change_is_announced_in_the_release_channel_once(store, arts):
    cfg = _config([("informational", "2026-01-05"), ("blocking", "2026-11-09")])
    job = ph.announce_check_mode(store, arts, cfg, now_=t(0))
    (row,) = store.query("SELECT kind, destination, run_id FROM outbox WHERE job_id = ?", (job,))
    assert tuple(row) == ("slack", "#sentinel-release", None)
    assert ph.announce_check_mode(store, arts, cfg, now_=t(60)) is None
    ob.dispatch(store, arts, {"slack": _Slack()}, t(1), allowlist=ALLOW)
    assert ph.announce_check_mode(store, arts, cfg, now_=t(120)) is None     # sent once, never again
    assert ph.announce_check_mode(store, arts, cfg, now_="2026-11-08T15:00:00+00:00") is None   # not in force yet
    assert ph.announce_check_mode(store, arts, _config([("informational", "2026-01-05")]), now_=t(0)) is None
    same = _config([("informational", "2026-01-05"), ("informational", "2026-02-01")])
    assert ph.announce_check_mode(store, arts, same, now_=t(0)) is None      # no change of mode


class _Slack:
    host = "slack.com"

    def send(self, job, payload):
        assert payload["text"] == ("[Sentinel] commit status sentinel/pr-run is blocking from 2026-11-09 (was "
                                   "informational; changed by asmith)")
        return ob.Delivery(status=200)


# -- helpers -----------------------------------------------------------------------------------------------------

def test_pr_numbers_runs_and_destinations(store):
    assert [ph.pr_number(x) for x in ("PR 4812", "4812", "PR-77 ", None, "no number")] == [4812, 4812, 77, None, None]
    with pytest.raises(ValueError, match="unknown run"):
        ph.pr_run(store, "nope")
    store.begin_run(RunPlan(run_id="m-1", run_type="merge", config_commit="c", config_tarball_sha256="t",
                            snapshot_set_sha256="s", harness_sha="h", authorization_ref="a", budget_sec=60, scope=()))
    with pytest.raises(ValueError, match="not a PR run"):
        ph.pr_run(store, "m-1")
    assert ph.comment_destination(7) == "pr:7" and ph.status_destination("abc") == "commit:abc#sentinel/pr-run"
    assert ph.latest_pr_run(store, "PR 1") is None


def test_the_fake_host_refuses_editing_a_missing_comment(host):
    with pytest.raises(ph.PrHostError) as e:
        host.edit_comment(PR, "c9", "x")
    assert e.value.status_code == 404
    assert ph.find_marker([ph.Comment("c1", "hello"), ph.Comment("c2", comment_marker(PR) + "\nbody")], PR) == \
        ph.Comment("c2", comment_marker(PR) + "\nbody")
    assert ph.find_marker([], PR) is None


# -- re-post from the stored context, the withheld report, the file-backed host ----------------------------------

def test_repost_renders_with_the_stored_context_and_the_run_rows_override(store, arts, host):
    run(store, "run-1")
    post(store, arts, host, "run-1", 41, context=ReportContext(pr=0, candidate_build="cand-1", base_build="base-1",
                                                                base_url=BASE_URL, detector_banner="DETECTOR BLIND"))
    deliver(store, arts, host, 41)
    stored = store.run_summary("run-1")["pr_report"]["context"]
    assert stored["detector_banner"] == "DETECTOR BLIND" and "history" not in stored and "pr" not in stored
    store.update("runs", {"run_id": "run-1"}, {"check_override_by": "carol", "check_override_reason": "flaky"})
    again = ph.repost(store, arts, "run-1", host, allowlist=ALLOW, now_=t(60))
    assert again is not None and "DETECTOR BLIND" in again.comment and again.status.state == "success"
    deliver(store, arts, host, 60)
    (comment,) = host.comments[PR]
    assert comment.comment_id == "c1" and comment.body == again.comment          # edited in place (9.1)


def test_repost_of_a_run_never_reported_or_not_a_pr_run_does_nothing(store, arts, host):
    run(store, "run-1")
    assert ph.repost(store, arts, "run-1", host, allowlist=ALLOW, now_=t(41)) is None
    assert ph.repost(store, arts, "no-such-run", host, allowlist=ALLOW, now_=t(41)) is None
    assert store.query("SELECT count(*) FROM outbox")[0][0] == 0


def test_a_withheld_report_abandons_the_open_pr_jobs_and_queues_did_not_report(store, arts, host):
    run(store, "run-1")
    posted = post(store, arts, host, "run-1", 41)
    job = ph.post_withheld(store, arts, "run-1", description="Sentinel did not report: the report is withheld",
                           base_url=BASE_URL, now_=t(42))
    states = {r["job_id"]: r["state"] for r in store.query("SELECT job_id, state FROM outbox")}
    assert states[posted.comment_job] == states[posted.status_job] == "abandoned" and states[job] == "queued"
    deliver(store, arts, host, 42)
    assert PR not in host.comments                                                 # 6.9: no comment published
    assert status(host)["state"] == "error" and status(host)["description"].startswith("Sentinel did not report")


def test_the_file_backed_host_keeps_comments_and_statuses_across_instances(tmp_path):
    first = ph.FilePrHost(tmp_path / "pr-host.json", SCM)
    cid = first.create_comment(PR, comment_marker(PR) + "\nv1")
    first.set_status(COMMITS["cand-1"], context="sentinel/pr-run", state="pending", description="d", target_url=None)
    second = ph.FilePrHost(tmp_path / "pr-host.json", SCM)
    assert ph.find_marker(second.list_comments(PR), PR).comment_id == cid
    second.edit_comment(PR, cid, comment_marker(PR) + "\nv2")
    third = ph.FilePrHost(tmp_path / "pr-host.json", SCM)
    assert third.list_comments(PR)[0].body.endswith("v2") and third.create_comment(PR, "other") == "c2"
    assert third.statuses[(COMMITS["cand-1"], "sentinel/pr-run")]["state"] == "pending"
