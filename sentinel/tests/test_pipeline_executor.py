"""The Executor wired into the simulated PR run (w2 wiring item 1): every flow, census visit and sweep step runs
through `executor.flow.run_unit`, its input through the emitter, the primitives' planner and the locator ladder on
the simulated rig (`pipeline.simports`); heartbeats through `HeartbeatPolicy`, the app's age through `AppLife`,
faults through `executor.failures`, the LOGIN secret through `executor.secret.run_secret_step`."""
from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from sentinel.demo import EXAMPLE, STOCK_FLOW, app_crash_scenario, clean_scenario, wrong_number_scenario
from sentinel.executor.flow import CENSUS_CHECKS
from sentinel.orchestrator.simrig import SimScenario, SimulatedRig
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.orchestration import RigScript
from sentinel.pipeline.run import SIM_SECRET, run_pipeline
from sentinel.pipeline.simports import SimDesk, SimVault, fixture_values
from sentinel.store import Store

TODAY = date(2026, 11, 10)
S03 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s03"
S06 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s06"
S07 = f"flow/dialog.order-confirmation/{STOCK_FLOW}/s07"
QTY = "trade.order-entry.stock.qty-field"


def _run(tmp_path: Path, **script):
    sc = wrong_number_scenario(TODAY)
    sc = dataclasses.replace(sc, rig=RigScript(**script))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        steps = [s for s in store.step_outcomes(r.run_id) if s.flow_id == STOCK_FLOW]
        events = store.step_events(r.run_id)
        findings = store.findings(r.run_id)
    return r, {o.checkpoint_id: o for o in r.outcomes}, steps, events, findings


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("exec-clean"))


def test_the_stock_flow_runs_through_the_emitter_planner_and_ladder(clean):
    r, by, steps, events, _ = clean
    status = {s.step_id: (s.status, s.locator_rung, s.action) for s in steps}
    # input steps resolved by the ladder's image rung on the simulated desk; every step one outcome row (4.13)
    assert status["s02"] == status["s04"] == status["s05"] == ("passed", "image", "type")
    assert status["open-confirm"] == ("passed", "image", "click")
    assert status["s03"][0] == status["s06"][0] == status["s07"][0] == "passed" and status["s07"][2] == "capture"
    assert status["w-confirm"] == ("passed", None, "wait")
    # the teardown closes the dialog through its title bar (4.7's example, 6.2 dismiss allowlist of the rig)
    assert status["t1"] == ("passed", "image", "click") and len([s for s in steps if s.step_id == "t1"]) == 1
    # the rung results of the ladder are on the step (rung 1 skipped while DR-01 is undecided)
    s04 = next(s for s in steps if s.step_id == "s04")
    assert s04.rung_results is None or {"rung": "jab", "result": "rung1_skipped"} in s04.rung_results
    # every gateway decision is the drainer's row with its token; the emitter's action rows carry the rung
    typed = [e for e in events if e.action == "gateway_decision" and e.step_id == "s04" and e.gate_decision == "emitted"]
    assert typed and all(e.token_id is not None for e in typed)
    clicks = [e for e in events if e.action == "click" and e.step_id == "open-confirm"]
    assert clicks and clicks[0].locator_level == "image" and clicks[0].target_text == "Confirm and Send"
    assert clicks[0].screen_x == 1500 and clicks[0].screen_y == 900            # the ticket's Confirm and Send
    assert not [e for e in events if e.gate_decision in ("dropped", "refused")]
    assert r.status == "complete" and by[S07].outcome == "assertion_failed"


def test_captures_carry_the_apps_uptime_and_restart_count(clean):
    _, by, _, _, _ = clean
    assert by[S03].app_uptime_s is not None and by[S03].app_restart_count == 0


def test_a_control_hidden_for_two_attempts_is_found_on_the_steps_retry(tmp_path):
    _, by, steps, events, _ = _run(tmp_path, locator_miss=((QTY, 2),))
    s04 = next(s for s in steps if s.step_id == "s04")
    assert (s04.status, s04.locator_rung) == ("passed_after_retry", "image")
    assert any(e.action == "reset_input_state" and e.step_id == "s04" for e in events)
    assert by[S06].outcome == "identical"


def test_an_exhausted_ladder_is_landmark_missing_and_ends_the_flow(tmp_path):
    r, by, steps, _, findings = _run(tmp_path, locator_miss=((QTY, 4),))
    s04 = next(s for s in steps if s.step_id == "s04")
    assert (s04.status, s04.locator_rung) == ("failed", None)          # rung `none` is no 8.3 locator level
    assert (by[S06].outcome, by[S06].reason) == ("landmark_missing", "locator:s04")
    assert (by[S07].outcome, by[S07].reason) == ("not_run", "step_failed:s04")
    assert by[S03].outcome == "identical"
    (lf,) = [f for f in findings if f.kind == "locator_failure" and f.step_id == "s04"]
    assert "s04" in lf.description and lf.checkpoint_id == S06
    # teardown still runs after a failed step (2.6.1)
    assert any(s.step_id == "t4" for s in steps)


def test_a_precondition_that_resets_cannot_fix_is_env_error_for_the_unit(tmp_path):
    r, by, _, _, _ = _run(tmp_path, precondition_fail=(("working_orders", 3),))
    failed = {c: o for c, o in by.items() if o.reason == "precondition:working_orders"}
    assert failed and {(o.outcome, o.gate) for o in failed.values()} == {("env_error", "workspace_loaded")}
    # the unit only: each rig's first flow reads the stuck precondition (SD-14 puts the two flows on two rigs); every
    # checkpoint of a failed flow takes it, and the rest of each shard (its census visits) still runs
    flows = {o.flow_id for o in failed.values()}
    assert flows and all(o.outcome == "env_error" for o in by.values() if o.flow_id in flows)
    assert {o.outcome for o in by.values() if o.test_kind == "census"} == {"identical"}
    assert r.status == "complete_with_env_errors"


def test_a_wait_for_timeout_is_not_settled_on_the_next_checkpoint(tmp_path):
    _, by, steps, _, _ = _run(tmp_path, wait_timeout=("w-confirm",))
    assert (by[S07].outcome, by[S07].reason, by[S07].side) == ("not_settled", "anchor_missing", "candidate")
    assert by[S07].detail == "wait_for w-confirm: anchor_missing after 6000 ms"
    assert next(s for s in steps if s.step_id == "w-confirm").status == "failed"


def test_an_exception_in_a_capture_is_the_10_1_harness_row(tmp_path):
    _, by, _, _, _ = _run(tmp_path, faults=(("exception:capture", S06, "KeyError"),))
    assert (by[S06].outcome, by[S06].reason) == ("harness_error", "exception:KeyError")
    assert "watchdog_event: " in (by[S06].detail or "")
    assert (by[S07].outcome, by[S07].reason) == ("not_run", "step_failed:s06")


def test_an_artifact_write_that_fails_twice_ends_the_run_aborted_harness(tmp_path):
    r, by, _, _, _ = _run(tmp_path, faults=(("artifact_persist", S03, ""),))
    assert (by[S03].outcome, by[S03].reason) == ("harness_error", "artifact_persist_failed")
    assert (r.status, r.reason) == ("aborted_harness", "artifact_persist_failed")


def test_an_unreachable_vault_types_nothing_and_pages_the_system_owner(tmp_path):
    r, by, _, _, _ = _run(tmp_path, vault_unreachable=True)
    assert {(o.outcome, o.reason, o.gate) for o in by.values()} == {("env_error", "vault_unreachable",
                                                                       "authenticated")}
    assert r.status == "complete_with_env_errors"
    pages = [a for a in r.alerts if a["run_reason"] is None and "vault" in (a["detail"] or "")]
    assert pages and all("system_owner" in a["paged"] for a in pages)


def test_census_visits_whose_precondition_fails_are_env_error(tmp_path):
    sc = dataclasses.replace(clean_scenario(TODAY), rig=RigScript(precondition_fail=(("single_main_frame", 999),)))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    census = [o for o in r.outcomes if o.test_kind == "census"]
    assert census and {(o.outcome, o.reason, o.gate) for o in census} == {
        ("env_error", "precondition:single_main_frame", "workspace_loaded")}
    assert {o.outcome for o in r.outcomes if o.test_kind == "flow"} <= {"identical", "intended"}   # flows don't read it


def test_a_marked_workspace_selects_the_shards_remaining_census_visits():
    """The `workspace:<id>` subset of a WorkspaceStreak mark (2.4): the shard's census checkpoints in that workspace
    that have no outcome yet."""
    from types import SimpleNamespace
    from sentinel.executor.flow import WorkspaceStreak
    from sentinel.orchestrator.results import Outcome
    from sentinel.pipeline.orchestration import ShardSink
    from sentinel.pipeline.run import _ShardWork
    cps = [SimpleNamespace(checkpoint_id=f"census/s{i}/{ws}", test_kind=kind, workspace_id=ws)
           for i, (kind, ws) in enumerate([("census", "ws.a"), ("census", "ws.a"), ("flow", "ws.a"), ("census", "ws.b"),
                                           ("census", "ws.a")])]
    st = SimpleNamespace(cps=cps, sink=ShardSink([c.checkpoint_id for c in cps]))
    st.sink.done.add(cps[0].checkpoint_id)
    work = SimpleNamespace(_todo=lambda s: _ShardWork._todo(None, s))
    streak = WorkspaceStreak()
    bad = Outcome("env_error", "precondition:no_modal", "workspace_loaded")
    assert streak.record("ws.a", bad) is None and streak.record("ws.a", bad) is None
    mark = streak.record("ws.a", bad)
    (sub,) = mark.outcomes
    picked = _ShardWork._select(work, st, sub.selector, None, [])
    assert [c.checkpoint_id for c in picked] == [cps[1].checkpoint_id, cps[4].checkpoint_id]


def test_the_app_crash_relaunch_counts_in_the_restart_tags(tmp_path):
    # the freeze hits the first unit of a shard with more work after it (SD-14 places the app-crash scenario's sweep
    # ladder alone on its rig, so its relaunch has no later capture there)
    sc = dataclasses.replace(app_crash_scenario(TODAY),
                             rig=RigScript(freeze_at="census/trade.active-trader.ladder/ws.tier-a.active-trader"))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    rig = next(s["rig"] for s in r.shards if s["checkpoints"] and any(g["control"] == "relaunch" for g in s["gates"]))
    after = [o for o in r.outcomes if o.cand_rig_id == rig and o.app_restart_count == 1]
    assert after                                                   # captures after the relaunch carry it (10.2)


def test_the_login_secret_is_leased_typed_redacted_and_released(tmp_path):
    vault = SimVault()
    rig = SimulatedRig(SimScenario(vault=vault))
    rig.set_hook_state(armed=True, deny_all=False)
    obs = rig.login()
    assert obs.main_window_s is not None and vault.leased and rig.leases == vault.leased
    (res,) = rig.secret_results
    assert res.status == "passed" and res.chars == len(SIM_SECRET) and res.target_text == "<redacted:login.password>"
    bad = SimulatedRig(SimScenario(vault=SimVault(unreachable=True)))
    bad.set_hook_state(armed=True, deny_all=False)
    assert bad.login().vault_lease is False and bad.secret_results[0].consequence is not None


def test_the_pipeline_scans_and_releases_every_lease_at_teardown(clean, tmp_path):
    r, _, _, events, _ = clean
    secret_rows = [e for e in events if e.gate_decision == "emitted" and e.keys and "redacted" in e.keys]
    assert all(SIM_SECRET not in json.dumps(e.to_dict()) for e in events)
    assert r.status == "complete"                                   # the teardown scan found no plaintext


def test_the_desk_draws_every_registry_control_with_its_template():
    cfg = SentinelConfig.load(EXAMPLE)
    desk = SimDesk(cfg.surfaces)
    assert set(desk.templates) == {c.template for c in desk.controls.values()}
    rects = [c.rect for c in desk.controls.values()]
    for i, a in enumerate(rects):           # no two controls overlap on the desk
        for b in rects[i + 1:]:
            assert a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y
    assert desk.target("dialog.order-confirmation.title-close")["window"] == "^Order Confirmation"
    values = fixture_values(cfg.fixture, list(cfg.flow_docs.values()))
    assert values["fixture.symbols.aapl.symbol"] == "AAPL" and values["fixture.prices.aapl_far_below.value"] == "150.25"
    assert "paper_mode" in CENSUS_CHECKS
