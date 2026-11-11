"""The wave-1 controls wired into the simulated PR run (sentinel.pipeline.run): the Orchestrator's boundaries and
budgets (5.2, 6.4, 10.2, 11.2), the paper-asserted stamp and the L-OUT-06 session context (6.4), and the step
events of a relaunch (10.2). Each test runs one clean PR run of the demo on the example config."""
from __future__ import annotations

from datetime import date

import sentinel.pipeline.run as pipeline_run
from sentinel.demo import EXAMPLE, clean_scenario
from sentinel.orchestrator.machine import Orchestrator
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store

TODAY = date(2026, 11, 10)
CHART = "charts.flexible-grid.chart-cell"


def test_every_captured_record_carries_its_sessions_paper_stamp(tmp_path):
    """6.4: every capture is stamped `{account_id_sha256, mode, asserted_at}` at capture time, and the store checks it
    against the session's assertions (`set_session_context`, L-OUT-06) before writing it."""
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.status == "complete"
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    captured = [o for o in outs if o.artifacts]
    assert captured and all(o.paper_assertion and o.paper_assertion["mode"] == "paperMoney"
                            and o.paper_assertion["account_id_sha256"].startswith("sha256:") for o in captured)


def test_a_capture_without_a_paper_assertion_is_aborted_safety_through_the_kill_switch(tmp_path, monkeypatch):
    """L-OUT-06, 6.4: a capture with no passing assertion in its session is never taken; the shard's run is
    `aborted_safety (paper_mode_mismatch)` through the 6.6 kill switch."""
    monkeypatch.setattr(Orchestrator, "paper_stamp", lambda self: None)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "paper_mode_mismatch")
    assert all(s["kill_switch"] for s in r.shards if s["verdict"] == "aborted_safety")
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    assert outs and not [o for o in outs if o.artifacts]
    assert {(o.outcome, o.reason) for o in outs} == {("env_error", "paper_mode_mismatch")}


def test_the_reviewer_refuses_a_frame_whose_stamp_was_not_current(tmp_path, monkeypatch):
    """6.4 Schedule: Capture releases no frame to the Reviewer without a current paper-asserted stamp
    (`Orchestrator.may_release`); the held items need a human (3.11 row 23) and no call is made."""
    monkeypatch.setattr(Orchestrator, "may_release", lambda self, stamp: False)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.reviewer_calls == []
    chart = [o for o in r.outcomes if o.surface_id == CHART]
    assert chart and {(o.outcome, o.reason) for o in chart} == {("needs_human", "reviewer_unavailable")}


def test_the_wall_clock_deadline_ends_the_run_at_a_flow_boundary(tmp_path, monkeypatch):
    """11.2, 5.2: past the PR wall budget less the report margin (35 minutes) the next flow boundary ends the run
    `aborted_budget (reason: wall_clock)`; the checkpoints not reached are `not_run (reason: budget)`."""
    monkeypatch.setattr(pipeline_run, "STEP_S", 2400.0)         # each Executor step takes forty simulated minutes
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_budget", "wall_clock")
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    assert ("not_run", "budget") in {(o.outcome, o.reason) for o in outs}
    assert "wall_clock" in (tmp_path / "pr-comment.md").read_text()


def test_paper_mode_is_asserted_before_every_unit_of_work(tmp_path):
    """6.4 Schedule: the assertion runs at the gate and again before every flow, census visit or sweep ladder
    (`flow_boundary`); the proof row counts every passing assertion."""
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        proofs = store.post_run_proofs(sc.run_id)
    units = {s["shard_id"]: s for s in r.shards}
    for p in proofs:
        gates = units[p.shard_id]["gates"]
        assert p.paper_mode_assertions == sum(1 for g in gates if g["control"] == "paper_mode" and g["passed"]) > 1
