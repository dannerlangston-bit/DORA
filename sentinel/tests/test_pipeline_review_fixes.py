"""Regression tests for the wave-1 review findings on the simulated run (sentinel.pipeline.run): the fire drill's
result through the Orchestrator (6.5, 1.10), the step boundary inside a sweep ladder (6.6, 2.5.1), a gateway drop
that does not end the flow (1.10, 5.4.3 stage 7, L-OUT-07), a failed post-run check after a safety abort (8.2, 4.13),
the base evidence of an unsettled pair (9.2, 8.4) and a harness_error cell in the coverage row (2.12)."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date

import sentinel.pipeline.run as pipeline_run
from sentinel.demo import EXAMPLE

TODAY = date(2026, 11, 10)


def _nightly(tmp_path, run_id: str = "run-2026-11-10-nightly-01"):
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.sim import SimBuild
    return pipeline_run.run_nightly(EXAMPLE, pipeline_run.NightlyScenario(
        "nightly", run_id, SimBuild(BASE_ID), overlay=dict([_main_build()]), mutants=("M01",)), tmp_path, TODAY)


def test_a_fire_drill_that_placed_an_order_aborts_the_nightly_with_drill_order_placed(tmp_path, monkeypatch):
    """pipeline:0 (6.5, 1.10, 12.2): a drill that saw a new order row stops the shard through the Orchestrator: the
    kill switch runs for that result and the run, the shard and the login block all carry `drill_order_placed`."""
    real = pipeline_run.run_drill

    def leaky(rig, *a, **kw):
        rig._send()                               # a real Working row on the simulated book before the drill
        return real(rig, *a, **kw)

    monkeypatch.setattr(pipeline_run, "run_drill", leaky)
    n = _nightly(tmp_path)
    assert (n.status, n.reason) == ("aborted_safety", "drill_order_placed")
    assert [(s["verdict"], s["reason"]) for s in n.shards] == [("aborted_safety", "drill_order_placed")]
    assert n.shards[0]["kill_switch"]             # the kill switch path ran for the drill's result
    with sqlite3.connect(tmp_path / "sentinel.db") as c:
        assert c.execute("SELECT outcome, abort_reason FROM runs").fetchall() == [("aborted_safety",
                                                                                  "drill_order_placed")]
        assert c.execute("SELECT blocked_reason FROM logins WHERE blocked_at IS NOT NULL").fetchall() == [
            ("drill_order_placed",)]


def test_a_clean_fire_drill_leaves_the_nightly_complete(tmp_path):
    """pipeline:0: the safety pass's passing results go through the Orchestrator without stopping the shard."""
    n = _nightly(tmp_path)
    assert (n.status, n.reason) == ("complete", None)
    assert n.shards[0]["kill_switch"] is None


def test_a_stop_during_a_sweep_ladder_trips_at_the_next_ladder_step(tmp_path):
    """pipeline:2 (6.6 Triggers, 2.5.1, 1.10): `sentinel stop` while the first Ladder S step is captured trips the
    kill switch at the next ladder step's boundary; the eleven steps not reached are `env_error (running,
    kill_switch)`, none of them captured."""
    from sentinel.demo import AT, app_crash_scenario
    from sentinel.pipeline.orchestration import RigScript
    sc = dataclasses.replace(app_crash_scenario(TODAY), rig=RigScript(stop_at=f"sweep/{AT}/flexgrid/960x720"),
                             name="sweep-stop")
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "kill_switch")
    ladder = {o.checkpoint_id.rsplit("/", 1)[1]: o for o in r.outcomes if o.checkpoint_id.startswith(f"sweep/{AT}/")}
    assert len(ladder) == 12 and ladder.pop("960x720").outcome == "identical"
    assert {(o.outcome, o.reason, o.gate) for o in ladder.values()} == {("env_error", "kill_switch", "running")}
    assert not [o for o in ladder.values() if o.artifacts]

