"""`orchestrator --resume` and `--finalize` (10.2 Restart and resume rules, 10.2 Dead-man, 8.2, 8.9)."""
from __future__ import annotations

import io
import json
import sqlite3

import pytest

from sentinel.orchestrator.resume import (FlowUnit, ResumeRefused, budgets_from_events, finalize, flow_units, main,
                                          resume, unit_of)
from sentinel.orchestrator.watchdog import Heartbeat
from sentinel.records import CheckpointOutcome, StepEvent
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

RUN = "pr-1"
SHARD = f"{RUN}/1"
WS = "ws.tier-a.active-trader"
FLOWS = ("flow.a", "flow.b", "flow.c", "flow.d")
STEPS = ("s02", "s05", "s08")


def cid(flow: str, step: str) -> str:
    return f"flow/trade.ticket/{flow}/{step}"


SCOPE = tuple(cid(f, s) for f in FLOWS for s in STEPS)


def make_store(path, run_type="pr") -> Store:
    s = Store(path)
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(RunPlan(run_id=RUN, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, started_at="2026-11-09T19:00:00Z",
                        scope=tuple(ScopeEntry(c, WS, "active_trader", 1, "frozen", flow_class="standard")
                                    for c in SCOPE),
                        shards=(ShardPlan(SHARD, "rig-a1"),),
                        flow_steps={f: STEPS for f in FLOWS}))
    return s


def record(s: Store, checkpoint: str, *, seq: int) -> None:
    flow, step = checkpoint.split("/")[2:]
    s.write_checkpoint(CheckpointOutcome(run_id=RUN, checkpoint_id=checkpoint, outcome="identical", workspace_id=WS,
                                         persona="active_trader", data_mode="frozen", tier=1, flow_id=flow,
                                         step_id=step),
                       steps=(), shard_id=SHARD)
    s.write_events([StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=seq, source="orchestrator",
                              wall_at="2026-11-09T19:01:00+00:00", mono_ms=1000 + seq, mode="scripted",
                              action="capture", flow_id=flow, step_id=step, checkpoint_id=checkpoint)])


def heartbeat(state="running", flow=None, step=None, checkpoint=None) -> Heartbeat:
    return Heartbeat(run_id=RUN, shard=1, attempt=1, state=state, ts="2026-11-09T14:10:00-05:00", mono_ts=1600.0,
                     flow_id=flow, step_id=step, checkpoint_id=checkpoint, orchestrator_pid=4412)


def journal(shard_dir, *lines) -> None:
    rows = [{"event_id": f"wd-{i:04d}", "at": "2026-11-09T19:10:05Z", "mono_ms": 1_605_000 + i, **x}
            for i, x in enumerate(lines, 1)]
    (shard_dir / "watchdog_events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def died(n=1):
    return {"action": "resume", "detail": f"cause: orchestrator_died; Orchestrator restart {n} of 2"}


@pytest.fixture
def shard(tmp_path):
    """A shard whose Orchestrator died after flow 2 of 4 and one checkpoint of flow 3."""
    s = make_store(tmp_path / "run.db")
    seq = 1
    for c in SCOPE[:7]:                                  # flows a and b, then flow.c s02
        record(s, c, seq=seq)
        seq += 1
    drop = StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=seq, source="gateway_hook",
                     wall_at="2026-11-09T19:09:00+00:00", mono_ms=1_590_000, mode="scripted", action="gateway_decision",
                     gate_decision="dropped", gate_reason="no_token", gate_class="integrity", keys="button_down:left")
    (tmp_path / "gateway_drops.jsonl").write_text(json.dumps(drop.to_dict()) + "\n")
    journal(tmp_path, died(1))
    (tmp_path / "heartbeat.json").write_text(json.dumps(
        heartbeat(flow="flow.c", step="s04", checkpoint=cid("flow.c", "s05")).__dict__))
    yield s, tmp_path
    s.close()


def test_units_group_flows_ladders_and_census_visits():
    assert unit_of(cid("flow.a", "s02")) == "flow:flow.a"
    assert unit_of("sweep/trade.ladder/flexgrid/800x600") == unit_of("sweep/trade.ladder/flexgrid/400x300-asc") == \
        "sweep:trade.ladder/flexgrid"
    assert unit_of(f"census/trade.ladder/{WS}/theme=dark") == unit_of(f"census/trade.ladder/{WS}") == \
        f"census:trade.ladder/{WS}"
    assert unit_of("split/trade.ladder/sp.main/0.25") == "split:trade.ladder/sp.main"
    assert [u.unit_id for u in flow_units(SCOPE)] == [f"flow:{f}" for f in FLOWS]


def test_resume_after_a_death_mid_flow_records_it_once_and_resumes_at_the_next_flow_boundary(shard):
    s, d = shard
    plan = resume(s, SHARD, d)
    assert plan.ingested == {"gateway_hook": 1, "watchdog": 1}
    assert plan.enter_at == "launched" and plan.budgets.used_orchestrator == 1
    assert plan.done_units == ("flow:flow.a", "flow:flow.b") and plan.interrupted == "flow:flow.c"
    assert [u.unit_id for u in plan.next_units] == ["flow:flow.d"]
    assert plan.written == (cid("flow.c", "s05"), cid("flow.c", "s08"))
    got = {o.checkpoint_id: (o.outcome, o.reason) for o in s.outcomes(RUN)}
    assert got[cid("flow.c", "s05")] == ("harness_error", "orchestrator_died")
    assert got[cid("flow.c", "s08")] == ("not_run", "step_failed:s04")
    assert got[cid("flow.c", "s02")] == ("identical", None)                       # recorded outcomes are kept
    rows = s.query("SELECT source, action, reason FROM step_events WHERE shard_id = ? AND source != 'orchestrator' "
                   "ORDER BY seq", (SHARD,))
    assert [tuple(r) for r in rows] == [("gateway_hook", "gateway_decision", None),
                                        ("watchdog", "resume", "wd-0001: cause: orchestrator_died; Orchestrator "
                                                               "restart 1 of 2")]
    again = resume(s, SHARD, d)                                                   # the second resume: a no-op
    assert again.noop and again.budgets.used_orchestrator == 1 and again.done_units[-1] == "flow:flow.c"
    assert [u.unit_id for u in again.next_units] == ["flow:flow.d"]


def test_resume_after_a_death_at_a_flow_boundary_skips_the_finished_flows(tmp_path):
    s = make_store(tmp_path / "run.db")
    for i, c in enumerate(SCOPE[:6], 1):                                          # flows 1 and 2 of 4
        record(s, c, seq=i)
    journal(tmp_path, died(1))
    plan = resume(s, SHARD, tmp_path, heartbeat=heartbeat(flow="flow.b", step="s08", checkpoint=cid("flow.b", "s08")))
    assert plan.written == () and plan.interrupted is None and plan.enter_at == "launched"
    assert [u.unit_id for u in plan.next_units] == ["flow:flow.c", "flow:flow.d"]
    assert plan.budgets.used_orchestrator == 1 and plan.decision.has("resume_next_flow")
    # the run's order comes from the caller
    order = (FlowUnit("flow:flow.d", SCOPE[9:]), FlowUnit("flow:flow.c", SCOPE[6:9]))
    assert [u.unit_id for u in resume(s, SHARD, tmp_path, units=order).next_units] == ["flow:flow.d", "flow:flow.c"]
    s.close()


def test_resume_in_teardown_keeps_every_outcome_and_a_third_restart_is_refused(tmp_path):
    s = make_store(tmp_path / "run.db")
    for i, c in enumerate(SCOPE, 1):
        record(s, c, seq=i)
    journal(tmp_path, died(1))
    plan = resume(s, SHARD, tmp_path, heartbeat=heartbeat("teardown"))
    assert plan.enter_at == "teardown" and plan.written == () and plan.next_units == ()
    assert resume(s, SHARD, tmp_path, heartbeat=heartbeat("reported")).enter_at == "reported"
    assert resume(s, SHARD, tmp_path, heartbeat=heartbeat("provisioned")).enter_at == "provisioned"
    journal(tmp_path, died(1), died(2), died(3))
    with pytest.raises(ResumeRefused, match="restart budget is spent"):
        resume(s, SHARD, tmp_path, heartbeat=heartbeat("teardown"))
    s.close()


def test_a_kill_switch_run_by_the_watchdog_is_left_to_the_recovery(shard):
    s, d = shard
    journal(d, {"action": "kill_switch", "detail": "kill switch second half: orchestrator_dead_flag_set"})
    plan = resume(s, SHARD, d)
    assert plan.kill_switch_pending and plan.written == () and plan.enter_at == "teardown" and plan.next_units == ()
    assert s.get_outcome(RUN, cid("flow.c", "s05")) is None                      # env_error will outrank (1.10)


def test_budgets_are_rebuilt_from_step_events():
    def ev(seq, action, reason, source="orchestrator"):
        return StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=seq, source=source,
                         wall_at="2026-11-09T19:00:00+00:00",
                         mono_ms=seq, mode="system", action=action, reason=reason)
    b = budgets_from_events([
        ev(1, "relaunch", "cause app_failure; budget app_relaunch; flows since launch 3"),
        ev(2, "relaunch", "cause hook_host; budget hook_host; flows since launch 4"),
        ev(3, "relaunch", "cause environment_relogin; budget relogin_k; flows since launch 5"),
        ev(4, "relaunch", "cause proactive; budget none; flows since launch 40"),
        ev(5, "resume", "wd-0001: cause: orchestrator_died", "watchdog"),
        ev(6, "resume", "wd-0002: finalize: dead-man deadline", "watchdog"),
        ev(7, "relaunch", "wd-0003", "watchdog")])
    assert (b.used_app, b.used_hook_host, b.used_orchestrator) == (1, 1, 1)


def test_finalize_after_running_is_dead_man_and_replays_from_the_ledger(shard, tmp_path):
    s, d = shard
    resume(s, SHARD, d)
    r = finalize(s, SHARD, state="running", reached_running=True, at="2026-11-09T19:50:00Z")
    assert (r.status, r.reason, r.ship) == ("aborted_harness", "dead_man", True)
    assert set(r.filled) == set(SCOPE[9:])                                        # flow.d, never reached
    got = {o.checkpoint_id: (o.outcome, o.reason) for o in s.outcomes(RUN)}
    assert all(got[c] == ("harness_error", "dead_man") for c in SCOPE[9:])
    assert got[cid("flow.c", "s05")] == ("harness_error", "orchestrator_died")
    out = s.run_outcome(RUN)
    assert (out.status, out.reason) == ("aborted_harness", "dead_man")
    assert s.run_summary(RUN)["abort"]["dead_man_layer"] == "local"
    assert s.query("SELECT shard_outcome, abort_reason FROM shards")[0][:] == ("aborted_harness", "dead_man")
    assert finalize(s, SHARD, state="running", reached_running=True).status is None   # finished: nothing written
    # a Run Store lost after the finalize: the ledger rebuilds the same rows (8.2, 8.9)
    with Store(tmp_path / "rebuilt" / "run.db") as fresh:
        fresh.replay(d / "results.jsonl")
    def rows(path):
        conn = sqlite3.connect(path)
        try:
            return sorted(conn.execute("SELECT checkpoint_id, outcome, reason FROM outcomes").fetchall())
        finally:
            conn.close()
    assert rows(tmp_path / "rebuilt" / "run.db") == rows(d / "run.db") and len(rows(d / "run.db")) == len(SCOPE)


def test_finalize_before_running_is_never_dead_man(tmp_path):
    pr = make_store(tmp_path / "pr" / "run.db")
    r = finalize(pr, SHARD, state="workspace_loaded", reached_running=False)
    assert (r.status, r.reason) == ("complete_with_env_errors", None)
    o = pr.get_outcome(RUN, SCOPE[0])
    assert (o.outcome, o.reason, o.gate) == ("env_error", "timeout", "workspace_loaded")
    pr.close()
    nightly = make_store(tmp_path / "n" / "run.db", run_type="nightly")
    r = finalize(nightly, SHARD, state="provisioned", reached_running=False)
    assert (r.status, r.reason) == ("aborted_harness", "start_failed")
    assert nightly.get_outcome(RUN, SCOPE[0]).reason == "start_failed"
    nightly.close()


def test_the_command_resumes_finalizes_and_refuses(shard):
    s, d = shard
    s.close()
    out = io.StringIO()
    assert main(["--resume", RUN, "--shard-dir", str(d)], out=out) == 0
    doc = json.loads(out.getvalue())
    assert doc["enter_at"] == "launched" and doc["next_units"] == ["flow:flow.d"]
    assert doc["budgets"]["orchestrator"] == 1
    out = io.StringIO()
    assert main(["--resume", RUN, "--shard-dir", str(d)], out=out) == 0
    assert json.loads(out.getvalue())["written"] == []
    out = io.StringIO()
    assert main(["--resume", RUN, "--shard-dir", str(d), "--finalize"], out=out) == 0       # running, by heartbeat
    assert json.loads(out.getvalue())["reason"] == "dead_man"
    assert main(["--resume", RUN, "--shard-dir", str(d)], out=io.StringIO()) == 1          # finished: refused
    assert main(["--resume", "pr-9", "--shard-dir", str(d)], out=io.StringIO()) == 1       # not this Run Store's run
    assert main(["--resume", RUN], out=io.StringIO()) == 2                                 # usage
    with Store(d / "run.db") as again:                                                     # the lock was released
        assert again.run_outcome(RUN).reason == "dead_man"
