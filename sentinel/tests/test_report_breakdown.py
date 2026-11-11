"""Run health's breakdown of 5.13 and 5.4.8 read from the store: drops by reason and mode, focus corrections, popups,
time to settle, agent spend, hook reinstalls and the 5.5 rescue cap."""
from __future__ import annotations

import pytest

from sentinel.records import CheckpointOutcome, ModelUsage, StepEvent
from sentinel.report.breakdown import Breakdown, DropCount, percentile, query
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

RUN, SHARD = "merge-2026-11-10", "merge-2026-11-10/1"
WS = "ws.tier-a.active-trader"
CPS = [f"census/trade.active-trader.ladder/{WS}", f"census/trade.order-entry.stock/{WS}",
       f"census/monitor.activity.position-statement/{WS}"]
FLOW = "flow.trade.order-ticket.stock.limit"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-01", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(RunPlan(run_id=RUN, run_type="merge", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=10800, scope=tuple(ScopeEntry(c, WS, "active_trader", 1, "frozen") for c in CPS),
                        shards=(ShardPlan(SHARD, "rig-01"),)))
    yield s
    s.close()


class Events:
    def __init__(self) -> None:
        self.seq = 0

    def __call__(self, action: str, **kw) -> StepEvent:
        self.seq += 1
        base = dict(run_id=RUN, shard_id=SHARD, side="candidate", seq=self.seq, source="orchestrator",
                    wall_at="2026-11-10T15:00:00Z", mono_ms=self.seq * 10, mode="scripted", action=action,
                    flow_id=FLOW, step_id=f"s{self.seq:02d}")
        base.update(kw)
        return StepEvent(**base)


def drop(ev: Events, reason: str, decision: str, mode: str = "scripted", **kw) -> StepEvent:
    return ev("gateway_decision", source="gateway_hook", mode=mode, gate_decision=decision, gate_reason=reason, **kw)


def test_drops_by_reason_and_mode_and_the_other_5_13_facts(store):
    ev = Events()
    store.write_events([
        drop(ev, "deny_profile", "refused"), drop(ev, "deny_profile", "refused"),
        drop(ev, "untokened_injected", "dropped", mode="system", gate_class="integrity", injected=True),
        drop(ev, "blocked_key_dialog", "dropped", drill=True), drop(ev, "blocked_chord_dialog", "dropped", drill=True),
        ev("focus_correction", target_text="Order Entry"), ev("focus_correction"),
        ev("popup", popup_title="POP-02"), ev("popup", popup_title="POP-02"), ev("popup", popup_title="Tip of the day"),
        ev("hook_reinstalled", mode="system", source="gateway_hook", gate_reason="hook_reinstalled",
           gate_class="integrity"),
        ev("click", mode="agent", step_id="s40", locator_level="agent"), ev("click", mode="agent", step_id="s40"),
        ev("click", step_id="s41", step_outcome="passed_via_agent_fallback"),
        ev("click", step_id="s42", locator_level="image")])
    for cid, ms in zip(CPS, (1200, 800, 3100)):
        store.write_checkpoint(CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome="identical", workspace_id=WS,
                                                 persona="active_trader", data_mode="frozen", settle_ms=ms))
    for comp, usd in (("agent_rescue", 0.30), ("agent_rescue", 0.12), ("reviewer", 2.0)):
        store.write_model_usage(ModelUsage(comp, "claude-pinned", "2026-11-10T15:01:00Z", run_id=RUN, cost_usd=usd))
    b = query(store, RUN)
    assert b.drops == (DropCount("scripted", "refused", "deny_profile", 2),
                       DropCount("system", "dropped", "untokened_injected", 1))
    rows = dict(b.rows())
    assert rows["Gateway drops by reason and mode"] == (
        "2 deny_profile refused (scripted); 1 untokened_injected dropped (system); 2 drops matched by the fire drill, "
        "not counted")
    assert rows["Focus corrections"] == "2" and rows["Hook reinstalls"] == "1"
    assert rows["Popups"] == "3 (POP-02 2, Tip of the day 1)"
    assert rows["Time to settle"] == "p50 1.20 s, p95 3.10 s over 3 checkpoints"
    assert rows["Agent spend"] == "0.42 USD (agent rescue 0.42, Explorer 0.00)"      # the Reviewer's is its own row
    assert rows["Agent rescue cap"] == "2 of 25 rescued steps used"
    assert store.gateway_drops(RUN) == 5             # the 9.2 total is unchanged: the drill rows are in it


def test_a_spent_rescue_cap_prints_the_5_5_line_and_an_empty_run_prints_zeros(store):
    ev = Events()
    store.write_events([ev("click", mode="agent", step_id=f"r{i}") for i in range(3)])
    rows = dict(query(store, RUN, rescue_cap=3).rows())
    assert rows["Agent rescue cap"] == "agent rescue cap reached after 3 steps; later locator failures are failed"
    empty = dict(Breakdown().rows())
    assert empty["Gateway drops by reason and mode"] == "0" and empty["Popups"] == "0"
    assert empty["Time to settle"] == "not recorded" and empty["Agent spend"] == "0.00 USD (agent rescue 0.00, " \
                                                                                 "Explorer 0.00)"


def test_nearest_rank_percentile():
    assert percentile([], 50) is None and percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile(list(range(1, 21)), 95) == 19
