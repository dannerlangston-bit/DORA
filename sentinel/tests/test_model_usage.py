"""Per-component model usage, the cost alarms and the Reviewer backlog alarm (11.4, 11.6, 9.10;
`sentinel.model_usage`)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel import model_usage as mu
from sentinel.records import StepEvent
from sentinel.reviewer.config import Prices
from sentinel.reviewer.transport import Usage
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
PRICES = Prices(input=2.0, output=10.0, cache_write=4.0, cache_read=0.2)
CP = "census/monitor.position-statement/ws.tier-a.options-trader"


def at(delta: timedelta) -> str:
    return (NOW - delta).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="vault://paper/rig-01", owner="asmith", snapshot_id="snap-1",
                   allowlist_entry=1)
    yield s
    s.close()


def begin(s: Store, run_id: str, run_type: str = "merge", started: timedelta = timedelta(hours=6)) -> str:
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=10800, started_at=at(started),
                        scope=(ScopeEntry(CP, "ws.tier-a.options-trader", "options_trader", 1, "frozen"),),
                        shards=(ShardPlan(f"{run_id}/1", "rig-01"),)))
    return run_id


def charge(s: Store, usd: float, when: timedelta, component: str = "reviewer", run_id: str | None = None) -> None:
    mu.record(s, component=component, model_id="claude-x", usage=Usage(1000, 100), cost_usd=usd, at=at(when),
              run_id=run_id)


def pending(s: Store, run_id: str, age: timedelta) -> None:
    s.insert("outcomes", {"run_id": run_id, "checkpoint_id": CP, "outcome": "needs_human",
                          "reason": "reviewer_unavailable", "review_state": "pending", "recorded_at": at(age)})


def test_every_component_is_stored_with_its_usage_and_price(store):
    rid = begin(store, "run-2026-11-10-merge-01")
    mu.record(store, component="reviewer", model_id="claude-x", usage=Usage(3000, 300), prices=PRICES,
              at=at(timedelta(0)), run_id=rid, request_id="req-1")
    mu.record(store, component="agent_rescue", model_id="claude-x", usage={"input_tokens": 5000, "output_tokens": 150},
              cost_usd=0.012, at=at(timedelta(0)), run_id=rid)
    mu.record(store, component="explorer", model_id="claude-x", usage=Usage(5000, 150), prices=PRICES, batch=True,
              at=at(timedelta(0)), run_id=rid)
    rows = {r["component"]: r for r in store.query("SELECT * FROM model_usage")}
    assert set(rows) == {"reviewer", "agent_rescue", "explorer"}
    assert rows["reviewer"]["cost_usd"] == pytest.approx(0.009)              # 11.4: one review, mid tier
    assert rows["reviewer"]["request_id"] == "req-1" and rows["reviewer"]["input_tokens"] == 3000
    assert rows["explorer"]["cost_usd"] == pytest.approx(0.00575)            # batch factor 0.5 of 0.0115
    with pytest.raises(ValueError):
        mu.record(store, component="reviewer", model_id="m", usage=Usage(), at=at(timedelta(0)))


def test_step_usage_blocks_become_agent_and_explorer_rows_once(store):
    rid = begin(store, "run-2026-11-10-rc-01", "rc")
    usage = {"input_tokens": 5000, "output_tokens": 150}
    store.write_events([
        StepEvent(rid, f"{rid}/1", "candidate", 1, "orchestrator", at(timedelta(0)), 0, "agent", "click",
                  flow_id="flow.a", step_id="s01", model_id="claude-x", usage=usage),
        StepEvent(rid, f"{rid}/1", "candidate", 2, "orchestrator", at(timedelta(0)), 1, "explorer", "explorer_action",
                  model_id="claude-x", usage=usage),
        StepEvent(rid, f"{rid}/1", "candidate", 3, "orchestrator", at(timedelta(0)), 2, "scripted", "click",
                  flow_id="flow.a", step_id="s02")])
    assert mu.record_step_usage(store, rid, PRICES) == 2
    assert mu.record_step_usage(store, rid, PRICES) == 0
    rows = store.query("SELECT component, request_id, cost_usd FROM model_usage ORDER BY request_id")
    assert [(r[0], r[1]) for r in rows] == [("agent_rescue", f"step:{rid}/1:1"), ("explorer", f"step:{rid}/1:2")]
    assert rows[0][2] == pytest.approx(0.0115)


def test_a_2_1x_week_over_week_change_raises_the_p2_cost_alarm_and_1_9x_does_not(store):
    charge(store, 10.0, timedelta(days=10))
    charge(store, 21.0, timedelta(days=2))
    found = mu.weekly_alarms(store, NOW)
    assert {a.key for a in found} == {"cost_wow:reviewer:2026-11-10", "cost_wow:total:2026-11-10"}
    a = found[0]
    assert (a.rule, a.priority) == ("preflight_or_cost", "P2") and "on_call_channel" in a.route.notified
    assert "2.1x week over week" in a.detail
    spent = mu.spend(store, NOW - timedelta(days=7), NOW)
    assert spent["reviewer"] == 21.0 and spent["total"] == 21.0


def test_below_2x_or_no_baseline_raises_nothing(tmp_path):
    with Store(tmp_path / "a.db") as s:
        charge(s, 10.0, timedelta(days=10))
        charge(s, 19.0, timedelta(days=2))
        assert mu.weekly_alarms(s, NOW) == ()
    with Store(tmp_path / "b.db") as s:
        charge(s, 30.0, timedelta(days=2), component="explorer")
        assert mu.weekly_alarms(s, NOW) == ()


def test_each_cap_hit_raises_one_alarm(store):
    big = begin(store, "run-2026-11-09-rc-01", "rc", timedelta(days=1))
    charge(store, 30.0, timedelta(days=1), "reviewer", big)
    charge(store, 26.0, timedelta(days=1), "explorer", big)
    capped = begin(store, "run-2026-11-10-merge-02", "merge", timedelta(hours=8))
    store.insert("outcomes", {"run_id": capped, "checkpoint_id": CP, "outcome": "needs_human", "reason": "reviewer_cap",
                              "recorded_at": at(timedelta(hours=7))})
    store.write_events([StepEvent(capped, f"{capped}/1", "candidate", i + 1, "orchestrator", at(timedelta(hours=7)), i,
                                  "agent", "click", flow_id=f"flow.{i % 5}", step_id=f"s{i}") for i in range(25)])
    found = {a.key: a for a in mu.weekly_alarms(store, NOW, mu.Caps(per_week_usd=50.0))}
    assert set(found) == {"cap:week:2026-11-10", f"cap:run:{big}", f"cap:explorer:{big}",
                          f"cap:reviewer_items:{capped}", f"cap:rescue:{capped}"}
    assert found[f"cap:run:{big}"].run_id == big and "56.00 of 50 USD" in found[f"cap:run:{big}"].detail
    assert all(a.priority == "P2" for a in found.values())


def test_a_dollar_cap_abort_is_a_cap_hit_and_caps_read_the_budgets_file(store):
    rid = begin(store, "run-2026-11-10-pr-01", "pr", timedelta(hours=2))
    store.update("runs", {"run_id": rid}, {"lifecycle": "finished", "outcome": "aborted_budget",
                                           "abort_reason": "dollar_cap", "finished_at": at(timedelta(hours=1))})
    assert [a.key for a in mu.weekly_alarms(store, NOW)] == [f"cap:run:{rid}"]
    caps = mu.Caps.from_budgets({"usd": {"per_run": 40, "per_week": 400, "per_explorer_session": 20},
                                 "rescue": {"max_rescued_steps_per_run": 10}})
    assert caps == mu.Caps(40.0, 400.0, 20.0, 10)
    assert mu.Caps.from_budgets({}) == mu.Caps()


def test_reviewer_backlog_after_4_h_on_merge_1_h_on_blocking_pr_and_any_at_rc_report(store):
    merge_old = begin(store, "run-2026-11-10-merge-01")
    pending(store, merge_old, timedelta(hours=4, minutes=30))
    merge_new = begin(store, "run-2026-11-10-merge-02")
    pending(store, merge_new, timedelta(hours=3))
    pr = begin(store, "run-2026-11-10-pr-01", "pr")
    pending(store, pr, timedelta(hours=2))
    rc = begin(store, "run-2026-11-10-rc-01", "rc")
    pending(store, rc, timedelta(minutes=5))
    assert [a.key for a in mu.backlog_alarms(store, NOW)] == [f"reviewer_backlog:{merge_old}"]
    keys = [a.key for a in mu.backlog_alarms(store, NOW, pr_blocking=True, rc_reporting=[rc])]
    assert keys == [f"reviewer_backlog:{merge_old}", f"reviewer_backlog:{pr}", f"reviewer_backlog:{rc}"]
    a = mu.backlog_alarms(store, NOW)[0]
    assert (a.rule, a.priority, a.run_id) == ("reviewer_backlog", "P2", merge_old)
    assert "1 review items pending on merge run" in a.detail and "oldest 4.5 h" in a.detail
    assert json.loads(json.dumps(a.entry()))["key"] == a.key
