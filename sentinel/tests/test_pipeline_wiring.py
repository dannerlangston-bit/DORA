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


def test_a_dead_hook_host_fails_the_step_closed_and_relaunches_on_its_own_budget(tmp_path, monkeypatch):
    """5.4.2, 5.4.8 (DropLedger.record with the shard's budgets): the first `hook_host_dead` refusal of a flow step is
    `watchdog.hook_host_died`: the rest of the flow `not_run (reason: step_failed:<step>)`, a hook-host relaunch
    through every gate (`Orchestrator.relaunch("hook_host")`, a relaunch step event), and the run goes on."""
    from sentinel.demo import backend_scenario
    from sentinel.orchestrator.gateway import SlowDecision
    from sentinel.orchestrator.rig import EmitResult
    from sentinel.orchestrator.simrig import SimulatedRig

    real = SimulatedRig._send_input
    died: list[str] = []

    def send_input(self, action, *, events=()):
        if not died and action.flow_class == "standard" and action.keys:
            died.append(str(action.keys))
            return EmitResult(refused=SlowDecision(True, "hook_host_dead", detail="hook host heartbeat stale"))
        return real(self, action, events=events)

    monkeypatch.setattr(SimulatedRig, "_send_input", send_input)
    sc = backend_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert died and r.status == "complete"
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
        relaunch = store.query("SELECT reason FROM step_events WHERE run_id = ? AND action = 'relaunch'", (sc.run_id,))
        refused = store.query("SELECT gate_reason FROM step_events WHERE run_id = ? AND gate_decision = 'refused'",
                              (sc.run_id,))
    assert any(o.outcome == "not_run" and (o.reason or "").startswith("step_failed:") for o in outs)
    assert [x["reason"].split(";")[0] for x in relaunch] == ["cause hook_host"]
    assert [x["gate_reason"] for x in refused] == ["hook_host_dead"]
    assert any(a["control"] == "watchdog" and a["rule"] == "harness_error" for a in r.alerts)


def test_the_drainer_writes_every_gateway_decision_the_login_typing_included(tmp_path):
    """5.4.8, 6.2 Audit trail: the shard's drainer turns every decision into a step event: the Executor's flow input
    under its flow step, and the rig's own LOGIN typing (the secret's keys redacted, 6.9) with no flow."""
    sc = clean_scenario(TODAY)
    run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        rows = store.query("SELECT shard_id, seq, flow_id, keys, gate_decision, token_id FROM step_events "
                           "WHERE run_id = ? AND action = 'gateway_decision' ORDER BY shard_id, seq", (sc.run_id,))
        steps = store.query("SELECT shard_id, min(seq) AS first FROM step_events WHERE run_id = ? AND action = 'step' "
                            "GROUP BY shard_id", (sc.run_id,))
    login = [x for x in rows if x["flow_id"] is None]
    assert login and all(x["gate_decision"] == "emitted" and x["token_id"] is not None for x in login)
    assert any(x["keys"] == "key_down:<redacted>" for x in login)
    assert not any("0x58" in (x["keys"] or "") for x in login)          # the secret's stand-in keys never appear
    last = {}
    for x in rows:
        last[x["shard_id"]] = max(last.get(x["shard_id"], 0), x["seq"])
    assert all(s["first"] > last.get(s["shard_id"], 0) for s in steps)  # one seq per shard, no collision (8.3)


def test_a_census_rule_region_is_asserted_and_its_reads_are_stored(tmp_path, monkeypatch):
    """SD-29, 2.8.2, SD-09: a census checkpoint runs every rule region of its regions file (`checks_for`,
    `RegionRuleChecker`); a failed rule is `assertion_failed (assertion: rule.<region>.<field>)` whose signature region
    is the rule region (`AssertionResult.region`), every read goes to `oracle_extractions`, and the Invariant Checker
    is given the rule regions (`invariant_rule_regions`)."""
    from sentinel import invariants as inv
    ps = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    rel = f"regions/{ps}.yaml"
    text = (EXAMPLE / rel).read_text().replace("rule: {field: price, min: 0, min_rows: 1}",
                                               "rule: {field: price, min: 0, max: 1, min_rows: 1}")
    seen: list[tuple] = []
    real = inv.compare

    def compare(base, cand, rules, **kw):
        seen.append(tuple(r.id for r in rules.rule_regions))
        return real(base, cand, rules, **kw)

    monkeypatch.setattr(inv, "compare", compare)
    sc = clean_scenario(TODAY)
    sc = type(sc)(**{**sc.__dict__, "overlay": dict(sc.overlay, **{rel: text})})
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    (o,) = [o for o in r.outcomes if o.checkpoint_id == ps]
    assert (o.outcome, o.reason, o.assertion_ids) == ("assertion_failed", "assertion:rule.rgn.ps.mark.max",
                                                      ["rule.rgn.ps.mark.max"])
    (f,) = [f for f in r.findings if f.checkpoint_id == ps and f.kind == "assertion_failed"]
    assert f.region_id == "rgn.ps.mark"
    with Store(tmp_path / "sentinel.db") as store:
        rows = store.extractions(sc.run_id, ps)
    assert {(x.side, x.assertion_id) for x in rows} >= {("candidate", "rule.rgn.ps.mark.field"),
                                                       ("base", "rule.rgn.ps.pl-open.field")}
    assert ("rgn.ps.mark", "rgn.ps.pl-open") in seen


def test_the_default_order_chords_come_from_the_gateway_file(tmp_path):
    """5.4.7: `gateway/default_order_hotkeys.yaml` gives the shard's default order chords; a file that breaks
    L-XREF-17 fails the shard closed at `settings_attested` (`GatewayConfigError.result`)."""
    from sentinel.pipeline.config import SentinelConfig, materialize
    from sentinel.pipeline.orchestration import RigScript, order_hotkeys, shard_setup
    cfg = SentinelConfig.load(EXAMPLE)
    ctx, _ = shard_setup(cfg, run_id="run-x", run_type="pr", shard_id="run-x/1", shard_no=1, build_id="b-1",
                         workspace_id="ws.tier-a.active-trader", script=RigScript())
    from sentinel.orchestrator.gateway import Chord
    assert ctx.default_hotkeys().chords == (Chord.parse("Shift+B"), Chord.parse("Shift+S"))
    assert ctx.order_hotkeys is not None and ctx.order_hotkeys.verified
    root = materialize(EXAMPLE, {}, tmp_path / "cfg")
    (root / "gateway" / "default_order_hotkeys.yaml").unlink()
    hotkeys, err = order_hotkeys(SentinelConfig.load(root))
    assert hotkeys is None and err.result.run_status == "aborted_harness" and err.result.gate == "settings_attested"
