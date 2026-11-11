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


STOCK_FLOW = "flow.trade.order-ticket.stock.limit"


def _drop_first_tab(monkeypatch, reason: str, times: int = 1) -> list:
    """The hook drops the decisions of the first `times` Tab actions (the stock flow's step s04) for `reason`."""
    from sentinel.orchestrator import simrig
    from sentinel.orchestrator.gateway import VK_TAB, HookDecision
    real = simrig.SimulatedRig._send_input
    fired: list = []

    def send_input(self, action, *, events=()):
        res = real(self, action, events=events)
        if len(fired) < times and action.keys == (VK_TAB,) and res.decisions:
            fired.append(action)
            res = dataclasses.replace(res, decisions=tuple(HookDecision(False, reason, d.token_id)
                                                           for d in res.decisions))
        return res

    monkeypatch.setattr(simrig.SimulatedRig, "_send_input", send_input)
    return fired


def _stock_run(tmp_path):
    from sentinel.demo import operator_stop_scenario
    from sentinel.pipeline.orchestration import RigScript
    from sentinel.store import Store
    sc = dataclasses.replace(operator_stop_scenario(TODAY), rig=RigScript(), name="gateway-drop")
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        summary = store.run_summary(sc.run_id)
    stock = {o.checkpoint_id.rsplit("/", 1)[1]: (o.outcome, o.reason) for o in r.outcomes
             if STOCK_FLOW in o.checkpoint_id}
    return r, summary, stock


def test_an_own_token_integrity_drop_is_retried_once_and_the_flow_goes_on(tmp_path, monkeypatch):
    """pipeline:3 (1.10 blocked_by_gateway, 5.4.3 stage 7; closed by the wave-2 Executor): one `token_expired` drop
    of the step's own token gets the single retry; the step passes after it with a `gateway_integrity` finding, the
    flow's later checkpoints are captured and the post-run check passes."""
    fired = _drop_first_tab(monkeypatch, "token_expired")
    r, summary, stock = _stock_run(tmp_path)
    assert len(fired) == 1
    assert (r.status, r.reason) == ("complete", None) and "post_run_check" not in summary
    assert stock == {"s03": ("identical", None), "s06": ("identical", None), "s07": ("identical", None)}
    assert any(f.kind == "gateway_integrity" and "retried once" in f.description for f in r.findings)


def test_an_uncounted_gateway_drop_ends_the_flow_with_an_outcome_for_every_later_checkpoint(tmp_path, monkeypatch):
    """pipeline:3 (2.6, 1.10, L-OUT-07; closed by the wave-2 Executor): a `deny_all` drop blocks the step without
    stopping the shard; the flow's later checkpoints are `not_run (step_failed:s04)`, so every scoped checkpoint has its
    record and the run is not `aborted_harness (taxonomy_check_failed)`."""
    _drop_first_tab(monkeypatch, "deny_all")
    r, summary, stock = _stock_run(tmp_path)
    assert (r.status, r.reason) == ("complete", None) and "post_run_check" not in summary
    assert stock == {"s03": ("identical", None), "s06": ("not_run", "step_failed:s04"),
                     "s07": ("not_run", "step_failed:s04")}


def test_a_failed_post_run_check_after_a_safety_abort_keeps_aborted_safety_and_reports(tmp_path, monkeypatch):
    """pipeline:4 (8.2, 4.13 L-OUT-07, 1.4 G2, 9.2): a post-run check that fails in a run whose shards ended
    `aborted_safety` does not try to end the run `aborted_harness` (the store refuses a less severe outcome): the run
    stays `aborted_safety (paper_mode_mismatch)` with the check's failures and `report_withheld` in its summary, the
    silent checkpoint takes the abort's `env_error (running, paper_mode_mismatch)` (1.10), and the withheld comment
    and the `error` commit status are still written."""
    import json

    from sentinel.demo import clean_scenario
    from sentinel.orchestrator.machine import Orchestrator
    from sentinel.store import Store
    monkeypatch.setattr(Orchestrator, "paper_stamp", lambda self: None)
    real = pipeline_run._ShardWork.write_record
    dropped: list[str] = []

    def write_record(self, o, **kw):            # a harness bug: one record never reaches the store (L-OUT-07)
        if not dropped:
            dropped.append(o.checkpoint_id)
            return o
        return real(self, o, **kw)

    monkeypatch.setattr(pipeline_run._ShardWork, "write_record", write_record)
    sc = clean_scenario(TODAY)
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "paper_mode_mismatch")
    with Store(tmp_path / "sentinel.db") as store:
        row = store.query("SELECT lifecycle, outcome, abort_reason FROM runs WHERE run_id = ?", (sc.run_id,))[0]
        summary = store.run_summary(sc.run_id)
        assert not store.missing_outcomes(sc.run_id)
        filled = [o for o in store.outcomes(sc.run_id) if o.checkpoint_id == dropped[0]]
    assert tuple(row) == ("finished", "aborted_safety", "paper_mode_mismatch")
    check = summary["post_run_check"]
    assert check["report_withheld"] is True and check["failures"][0].startswith("L-OUT-07: 1 scoped checkpoint")
    assert [(o.outcome, o.reason, o.gate) for o in filled] == [("env_error", "paper_mode_mismatch", "running")]
    assert "L-OUT-07" in (tmp_path / "pr-comment.md").read_text()
    assert json.loads((tmp_path / "commit-status.json").read_text())["state"] == "error"


def _unsettled(tmp_path, noisy_side: str):
    """The Position Statement census checkpoint with one side's frames replaced by changing noise (it never settles),
    evaluated and written through `_Writer.artifacts`: the outcome, the stored sides and the candidate image used."""
    from datetime import datetime, timezone

    import numpy as np

    from sentinel.demo import BASE_ID, PS_CENSUS
    from sentinel.pipeline.config import SentinelConfig, materialize
    from sentinel.pipeline.sim import SimBuild
    from sentinel.store.artifacts import ArtifactStore
    cfg = SentinelConfig.load(materialize(EXAMPLE, {}, tmp_path / "cfg"))
    cp = pipeline_run.planned_for(cfg, PS_CENSUS)
    run = pipeline_run._Run(cfg, f"run-unsettled-{noisy_side}", "pr", SimBuild(BASE_ID), SimBuild(BASE_ID),
                            datetime(2026, 11, 10, 15, 31, tzinfo=timezone.utc), None, None, lambda s: "undeclared")
    base, cand = run.capture(cp, "base"), run.capture(cp, "candidate", tick=1)
    rng = np.random.default_rng(0)
    noisy = base if noisy_side == "base" else cand
    noisy.frames = [dataclasses.replace(f, image=rng.integers(0, 255, f.image.shape, dtype=np.uint8))
                    for f in noisy.frames]
    ev = pipeline_run.evaluate(run, cp, base, cand, x="undeclared", primitive=None, entry_ref=None,
                               recapture=lambda: cand)
    w = pipeline_run._Writer(run, ArtifactStore(tmp_path / f"arts-{noisy_side}", "runs/x"), TODAY, "15:31")
    out, stored = w.artifacts(ev)
    return ev, out, stored, run


def test_an_unsettled_base_stores_no_candidate_pixels_as_the_base(tmp_path):
    """pipeline:5 (8.4, 9.2, 1.10 needs_baseline): a base that never settles is `needs_baseline (base_not_settled)`;
    no pair was formed, so only the candidate is stored and sent, never its pixels under side `base`."""
    ev, out, stored, run = _unsettled(tmp_path, "base")
    assert (ev.classified.outcome, ev.classified.reason) == ("needs_baseline", "base_not_settled")
    assert ev.images()[0] is None and set(out) == {"candidate"}
    assert [a.side for a in stored] == ["candidate"] and run.redaction_counts["base"] == 0


def test_an_unsettled_candidate_stores_no_candidate_pixels_as_the_base(tmp_path):
    """pipeline:5 (1.10 not_settled, Last frame kept; 8.4): the candidate's last frame is stored as the candidate and
    nothing of it is stored as the base."""
    ev, out, stored, _ = _unsettled(tmp_path, "candidate")
    assert ev.classified.outcome == "not_settled"
    assert ev.images()[0] is None and set(out) == {"candidate"} and [a.side for a in stored] == ["candidate"]


def test_a_harness_error_blocked_surface_is_not_compared_and_leaves_the_coverage_percent(tmp_path):
    """pipeline:6 (2.12, 9.2): a tier-1 surface whose checkpoints all ended `harness_error` is a `blocked` cell like
    one blocked by `env_error`: it is in the Not compared column and out of the comparable scope, so the tier row
    adds up and the percent is the same for both."""
    from sentinel.demo import operator_stop_scenario
    from sentinel.pipeline.config import SentinelConfig
    from sentinel.pipeline.orchestration import RigScript
    from sentinel.report.model import coverage_rows
    sc = dataclasses.replace(operator_stop_scenario(TODAY), rig=RigScript(), name="coverage")
    r = pipeline_run.run_pipeline(EXAMPLE, sc, tmp_path, TODAY)
    cfg = SentinelConfig.load(tmp_path / "config" / sc.run_id)
    surface = "trade.active-trader.ladder"
    assert any(o.surface_id == surface for o in r.outcomes)

    def row(outcome: str) -> tuple[dict, tuple[str, ...]]:
        kw = dict(reason="hook_lost") if outcome == "harness_error" else dict(reason="kill_switch", gate="running")
        outs = [dataclasses.replace(o, outcome=outcome, **kw) if o.surface_id == surface else o for o in r.outcomes]
        cov = pipeline_run.coverage_summary(cfg, r.plan, outs, sc.run_id)
        return cov["tiers"][1], dict(enumerate(coverage_rows(cov)))[0]

    env_tier, env_row = row("env_error")
    harness_tier, harness_row = row("harness_error")
    assert harness_tier["not_compared"] == {"harness_error": 1}
    assert harness_row[3] == env_row[3]                   # the same Covered percent of comparable scope
    assert harness_row[4] == "1 (harness_error: 1)"
    in_scope, covered = int(harness_tier["in_scope"]), int(harness_tier["covered"])
    assert in_scope == covered + 1 + sum(harness_tier["not_run"].get(k, 0) for k in harness_tier["not_run"]
                                         if k != "scope")
