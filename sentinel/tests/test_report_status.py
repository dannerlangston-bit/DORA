"""The `sentinel/pr-run` commit status (9.2): states, informational and blocking modes."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sentinel.records import CheckpointOutcome, RunOutcome
from sentinel.report import ReportContext, ReportInputError, commit_status, pending_status

from test_report_sample import RUN_ID, sample

REPORT = "https://sentinel.internal/runs/run-2026-11-09-pr-4812"


def _status(mode: str = "informational", **overrides):
    s = sample()
    s.update(overrides)
    return commit_status(s["run"], s["outcomes"], mode, findings=s["findings"], decisions=s["decisions"],
                         manifest=s["manifest"], context=s["context"])


def _decide_tier1(s: dict) -> dict:
    """Every tier-1 Unexpected checkpoint of the 9.3 sample now carries a decision (9.3 expected triage)."""
    s["outcomes"] = [replace(o, decision_ref="dec-000517") if o.tier == 1 and o.outcome in
                     ("invariant_violation", "collateral") else o for o in s["outcomes"]]
    return s


def test_phase_1_informational_failure_never_blocks():
    st = _status("informational")
    assert (st.context, st.state, st.mode) == ("sentinel/pr-run", "failure", "informational")
    assert st.description == "3 tier-1 findings have no decision (informational)"
    assert st.target_url == REPORT and not st.blocks_merge


def test_blocking_mode_has_the_same_state_and_blocks():
    st = _status("blocking")
    assert st.state == "failure" and st.description == "3 tier-1 findings have no decision"
    assert st.blocks_merge


def test_decisions_on_every_tier_1_item_turn_the_check_green():
    s = _decide_tier1(sample())
    for mode in ("informational", "blocking"):
        st = commit_status(s["run"], s["outcomes"], mode, findings=s["findings"], decisions=s["decisions"],
                           manifest=s["manifest"], context=s["context"])
        assert st.state == "success" and not st.blocks_merge
        assert st.reason == "1 unexpected (1 tier 2 and 3 open); 0 tier-1 surfaces not compared"


def test_outcomes_alone_give_the_same_state():
    s = sample()
    st = commit_status(s["run"], s["outcomes"], "informational")
    assert st.state == "failure" and st.reason == "3 tier-1 findings have no decision"


def test_tier_2_and_registry_side_findings_never_fail_the_check():
    s = _decide_tier1(sample())
    s["outcomes"].append(CheckpointOutcome(run_id=RUN_ID, checkpoint_id="census/unregistered.p3.x/ws.tier-a.active-trader",
                                           outcome="unregistered_surface", reason="probe:P3", tier=1,
                                           workspace_id="ws.tier-a.active-trader", persona="active_trader",
                                           data_mode="frozen"))
    st = commit_status(s["run"], s["outcomes"], "blocking", findings=s["findings"], decisions=s["decisions"],
                       manifest=s["manifest"])
    assert st.state == "success"


def test_tier_1_surfaces_not_compared_are_named_but_do_not_fail():
    s = _decide_tier1(sample())
    s["outcomes"] += [
        CheckpointOutcome(run_id=RUN_ID, checkpoint_id=f"census/monitor.{n}/ws.tier-a.active-trader", outcome=out,
                          gate=gate, reason=reason, tier=1, workspace_id="ws.tier-a.active-trader",
                          persona="active_trader", data_mode="frozen")
        for n, out, gate, reason in (("a", "env_error", "fixture_verified", "fixture_drift"),
                                     ("b", "not_run", None, "budget"))]
    st = commit_status(s["run"], s["outcomes"], "informational", findings=s["findings"], decisions=s["decisions"],
                       manifest=s["manifest"])
    assert st.state == "success"
    assert st.reason == ("1 unexpected (1 tier 2 and 3 open); 2 tier-1 surfaces not compared "
                         "(env_error, not_run (budget)): see the merge run")


@pytest.mark.parametrize("status,reason,abort,expected", [
    ("aborted_safety", "attempted_send", {"incident": "INC-2026-031"}, "aborted_safety: incident INC-2026-031"),
    ("aborted_harness", "dead_man", {}, "Sentinel did not report"),
    ("aborted_harness", "secret_leaked", {}, "Sentinel did not report"),
    ("aborted_harness", "store_write_failed", {}, "aborted_harness: store_write_failed"),
    ("aborted_budget", "pool_saturated", {}, "Sentinel did not run"),
])
def test_error_states(status, reason, abort, expected):
    run = RunOutcome(run_id=RUN_ID, run_type="pr", status=status, reason=reason, last_gate="running")
    st = _status("blocking", run=run, context=replace(sample()["context"], abort=abort))
    assert (st.state, st.reason) == ("error", expected) and st.blocks_merge


def test_aborted_budget_other_than_pool_saturated_is_triaged_as_usual():
    run = RunOutcome(run_id=RUN_ID, run_type="pr", status="aborted_budget", reason="wall_clock", last_gate="running")
    assert _status(run=run).state == "failure"


def test_no_comment_by_budget_plus_15_minutes_is_an_error():
    st = _status(context=replace(sample()["context"], comment_overdue=True))
    assert (st.state, st.reason) == ("error", "Sentinel did not report")


def test_system_owner_override_forces_success_for_one_run():
    ctx = replace(sample()["context"], override={"by": "m.chen", "reason": "known rig font issue"})
    st = _status("blocking", context=ctx)
    assert st.state == "success" and st.reason == "forced success by m.chen: known rig font issue"


def test_pending_state_names_the_base_build():
    st = pending_status("main-1998.3.0-2c91ae4")
    assert st.state == "pending"
    assert st.description == "Sentinel run started, base = main-1998.3.0-2c91ae4 (informational)"


def test_description_respects_the_140_character_cap():
    ctx = replace(sample()["context"], override={"by": "m.chen", "reason": "x" * 300})
    st = _status(context=ctx)
    assert len(st.description) == 140 and st.description.endswith("... (informational)")


def test_unknown_mode_is_refused():
    with pytest.raises(ReportInputError):
        _status("advisory")
    with pytest.raises(ReportInputError):
        pending_status("main-1", mode="strict")


def test_context_type_is_exported():
    assert ReportContext(pr=1, candidate_build="a", base_build="b").check_mode == "informational"
