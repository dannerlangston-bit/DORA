"""The gates, the fixture reset and the login wired into the simulated PR run (w2 wiring item 4): H3's DPI awareness,
the launcher log, the workspace's corpus windows (SD-77), Stage 1 and Stage 2 of the fixture reset (7.6), the churn
calendar (4.9), the login's history (6.3, 9.10), the fixture selectors in `_select`, the unscheduled flow classes and
the 8.3 vocabulary for `version_mismatch` and `authentication_failed`."""
from __future__ import annotations

import dataclasses
import json
from datetime import date

import pytest

from sentinel.demo import EXAMPLE, wrong_number_scenario
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gates import WindowExpectation, WorkspaceExpectation, check_authenticated, check_launched
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import LaunchObservation, LoginObservation
from sentinel.orchestrator.simrig import BUILD, MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.orchestration import RigScript
from sentinel.pipeline.run import RigsRefused, run_pipeline
from sentinel.store import Store
from sentinel.scope import Flow, UNSCHEDULED_CLASSES
from sentinel.store import safety as S

TODAY = date(2026, 11, 10)


def _run(tmp_path, today=TODAY, **script):
    sc = dataclasses.replace(wrong_number_scenario(today), rig=RigScript(**script))
    return run_pipeline(EXAMPLE, sc, tmp_path, today=today)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    out = tmp_path_factory.mktemp("gates-clean")
    return out, _run(out)


def test_every_shard_runs_stage_1_and_stage_2_and_passes_on_a_clean_rig(clean):
    out, r = clean
    assert r.status == "complete"
    gates = [g for s in r.shards for g in s["gates"]]
    assert {g["control"] for g in gates} >= {"fixture", "delay_badge", "connectivity", "prices", "pinned_quote",
                                              "workspace"}
    prices = next(g for g in gates if g["control"] == "prices")
    assert prices["passed"] and "api (aapl_far_below, mes_far_below)" in prices["detail"]   # recomputed (7.6 step 6)
    text = (out / "pr-comment.md").read_text()
    assert "Fixture Stage 1" in text and "step 8: profile checkpoints, 0 mismatch" in text


def test_a_drifted_golden_account_marks_its_account_state_checkpoints(tmp_path):
    r = _run(tmp_path, golden_drift=(("AAPL", 50.0),))
    drift = [o for o in r.outcomes if o.reason == "fixture_drift"]
    assert drift and {(o.outcome, o.gate) for o in drift} == {("env_error", "fixture_verified")}
    assert {o.test_kind for o in drift} == {"census"}                # non-flow checkpoints run on the golden account
    assert all(o.surface_id != "trade.order-entry.stock" for o in drift)   # account_state: false is untouched
    assert any(o.outcome == "identical" for o in r.outcomes) and r.status == "complete_with_env_errors"
    actions = json.loads((tmp_path / "orchestrator.json").read_text())[0]["persisted_actions"]
    assert any(a["action"] == "maintenance_task" for a in actions)


def test_a_harness_without_per_monitor_v2_awareness_aborts_at_provisioned(tmp_path):
    r = _run(tmp_path, dpi_awareness="SYSTEM_AWARE")
    assert (r.status, r.reason) == ("aborted_harness", "dpi_awareness")
    assert {o.reason for o in r.outcomes} == {"dpi_awareness"}


def test_a_failed_authenticated_gate_escalates_on_the_logins_next_run(tmp_path):
    first = _run(tmp_path, login=(("challenge", True),))
    assert {(o.reason, o.gate) for o in first.outcomes} == {("authentication_challenge", "authenticated")}
    pages = [a for a in first.alerts if a["control"] == "login"]
    assert pages and {a["priority"] for a in pages} == {"P2"}
    sc = dataclasses.replace(wrong_number_scenario(TODAY), run_id=first.run_id.replace("-01", "-02"),
                             rig=RigScript(login=(("challenge", True),)))
    with pytest.raises(RigsRefused, match="blocked"):     # 11.6: the challenge took every login's rig out of the pool
        run_pipeline(EXAMPLE, sc, tmp_path)
    with Store(tmp_path / "sentinel.db") as store, store.batch("human:reenable") as b:     # a person re-enables them
        for row in store.query("SELECT login_alias FROM logins WHERE blocked_at IS NOT NULL"):
            b.update("logins", {"login_alias": row["login_alias"]},
                     {"unblocked_by": "dana.okafor", "unblocked_at": "2026-11-10T16:00:00+00:00"})
    second = run_pipeline(EXAMPLE, sc, tmp_path)
    again = [a for a in second.alerts if a["control"] == "login"]
    assert again and {a["priority"] for a in again} == {"P1"}     # the same gate failed last run (9.10)


def test_the_churn_calendars_holiday_skips_the_masked_live_checkpoints(tmp_path):
    day = date(2026, 11, 26)
    r = _run(tmp_path, today=day)
    skipped = [o for o in r.outcomes if (o.outcome, o.reason) == ("not_run", "scope")]
    assert skipped and {o.data_mode for o in skipped} == {"masked_live"}
    assert all("holiday" in (o.detail or "") for o in skipped)


def test_the_workspace_gate_compares_the_corpus_windows():
    ws = WorkspaceExpectation("sha", ("Monitor",), 1, (WindowExpectation("thinkorswim", (0, 0, 1920, 1040)),))
    for rect, passed in (((0, 0, 1920, 1040), True), ((0, 0, 1920, 1080), False)):
        clock = ManualClock()
        rig = SimulatedRig(SimScenario(main_rect=rect, workspace=dataclasses.replace(
            SimScenario().workspace, file_sha256="sha", tabs=("Monitor",))), clock=clock)
        orch = Orchestrator(rig, default_context(workspace=ws), MemorySink(["c1"]), clock=clock)
        report = orch.run()
        (w,) = [g for g in report.results if g.control == "workspace"]
        assert w.passed is passed


def test_the_fixture_reset_and_login_flows_are_never_scheduled():
    cfg = SentinelConfig.load(EXAMPLE)
    doc = dict(cfg.flow_docs["flow.trade.order-ticket.stock.limit"], flow_id="flow.x", **{"class": "fixture_reset"})
    assert Flow.from_dict(doc).flow_class in UNSCHEDULED_CLASSES and "login" in UNSCHEDULED_CLASSES


def test_the_8_3_vocabulary_names_a_version_mismatch_and_a_lockout():
    mismatch = check_launched(LaunchObservation(True, "other-build"), expected_build=BUILD, first_launch=True,
                              budgets=ShardBudgets())
    assert mismatch.has("disable_rig") and S.disable_reason_for(mismatch) == "version_mismatch"
    lockout = check_authenticated(LoginObservation(auth_error=True))
    assert lockout.has("block_login") and S.blocked_reason_for(lockout) == "authentication_failed"
    assert "authentication_failed" in S.BLOCKED_REASONS and "version_mismatch" in S.DISABLE_REASONS
