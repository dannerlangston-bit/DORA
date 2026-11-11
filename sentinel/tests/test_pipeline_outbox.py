"""The simulated run's PR posts through the outbox (9.1, 9.2): the pending status at T+0:00, the comment and final
status at `reported`, both posted to the output directory's simulated PR host (`pr-host.json`) through the egress
gate, the history of the PR's earlier runs from the store, and the re-post after a decision write."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from sentinel.demo import EXAMPLE, clean_scenario, config_invalid_scenario
from sentinel.pipeline import run as pr
from sentinel.report.pr_comment import comment_marker
from sentinel.store import Store

TODAY = date(2026, 11, 10)
PR = 4823


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    out = tmp_path_factory.mktemp("pr-outbox")
    first = pr.run_pipeline(EXAMPLE, clean_scenario(TODAY), out, today=TODAY)
    second = pr.run_pipeline(EXAMPLE, replace(clean_scenario(TODAY, clock="13:05:00"),
                                              run_id="run-2026-11-10-pr-4823-02"), out, today=TODAY)
    return out, first, second


def jobs(out, run_id, kinds=("pr_comment", "commit_status")):
    """The run's PR host jobs (9.1, 9.2); its Slack and page alert jobs (9.10) wait for the control host."""
    with Store(out / "sentinel.db") as s:
        return [dict(r) for r in s.query("SELECT kind, destination, state, created_at, sent_at FROM outbox "
                                         "WHERE run_id = ? ORDER BY job_id", (run_id,)) if r["kind"] in kinds]


def test_a_pr_run_queues_pending_then_comment_and_status_and_the_host_gets_them(two_runs):
    out, first, _ = two_runs
    got = jobs(out, first.run_id)
    assert [(j["kind"], j["state"]) for j in got] == [("commit_status", "sent"), ("pr_comment", "sent"),
                                                      ("commit_status", "sent")]
    assert got[0]["created_at"] == got[0]["sent_at"] < got[1]["created_at"]          # pending at T+0:00 (9.2)
    host = json.loads((out / pr.PR_HOST_FILE).read_text())
    assert [s["state"] for s in host["status_log"][:2]] == ["pending", first.commit_status["state"]]
    with Store(out / "sentinel.db") as s:
        rows = s.query("SELECT destination, purpose, status FROM egress_log WHERE run_id = ?", (first.run_id,))
    assert {tuple(r) for r in rows} == {("scm.example.internal", "pr_comment", "sent")}   # one row per attempt (6.10)


def test_the_second_run_edits_the_one_comment_and_lists_the_first_in_its_history(two_runs):
    out, first, second = two_runs
    host = json.loads((out / pr.PR_HOST_FILE).read_text())
    (comment,) = host["comments"][str(PR)]
    assert comment["body"] == second.comment and comment["body"].startswith(comment_marker(PR))
    assert "Run history" in second.comment and first.run_id in second.comment.split("Run history", 1)[1]
    assert (out / "pr-comment.md").read_text() == second.comment
    page = (out / "report.html").read_text()                  # the page sections (9.4, 9.5): every render call
    for section in ("not-detectable", "sensitivity", "safety"):
        assert f'<section id="{section}">' in page, section


def test_a_decision_write_re_posts_the_comment_and_status(two_runs):
    out, _, second = two_runs
    before = len(jobs(out, second.run_id))
    with Store(out / "sentinel.db") as s:
        posted = pr.repost(s, out, second.run_id, "2026-11-10T19:30:00+00:00")
    assert posted is not None and posted.comment_job is not None
    after = jobs(out, second.run_id)
    assert [(j["kind"], j["state"]) for j in after[before:]] == [("pr_comment", "sent"), ("commit_status", "sent")]


def test_a_config_abort_posts_its_comment_and_no_status_without_a_build_pair(tmp_path):
    res = pr.run_pipeline(EXAMPLE, config_invalid_scenario(TODAY), tmp_path, today=TODAY)
    assert res.status == "aborted_harness"
    assert [(j["kind"], j["state"]) for j in jobs(tmp_path, res.run_id)] == [("pr_comment", "sent")]
