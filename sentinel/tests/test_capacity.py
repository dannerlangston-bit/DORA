"""Capacity: the 11.2 tables, utilization and R15, R22 starvation (11.2, 11.6, 11.10; `sentinel.capacity`)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sentinel import capacity as cap
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 10)                         # a Tuesday


def ny(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    for i in range(1, 5):
        s.register_rig(f"rig-0{i}", login_alias=f"vault://paper/rig-0{i}", owner="asmith", snapshot_id="snap-1",
                       allowlist_entry=i)
    s.register_rig("rig-05", login_alias="vault://paper/rig-05", owner="asmith", snapshot_id="snap-1",
                   allowlist_entry=5, role="nightly")
    yield s
    s.close()


def run(s: Store, run_id: str, run_type: str, start: datetime, sessions: dict[str, timedelta] = (),
        scope: tuple[ScopeEntry, ...] = (), finished: datetime | None = None) -> str:
    """A run with one shard per rig in `sessions` (rig -> session length), each finished after its length."""
    shards = tuple(ShardPlan(f"{run_id}/{n}", rig) for n, rig in enumerate(dict(sessions), 1))
    s.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=10800, scope=scope, shards=shards, started_at=iso(start)))
    for n, length in enumerate(dict(sessions).values(), 1):
        s.update("shards", {"shard_id": f"{run_id}/{n}"}, {"finished_at": iso(start + length)})
    if finished is not None:
        s.update("runs", {"run_id": run_id}, {"lifecycle": "finished", "outcome": "aborted_budget",
                                              "abort_reason": "wall_clock", "finished_at": iso(finished)})
    return run_id


def test_the_11_2_table_comes_back_from_the_per_side_times():
    rows = {r.run_type: r for r in cap.plan_table()}
    pr, merge, rc = rows["pr"], rows["merge"], rows["rc"]
    assert (pr.pair_no_reuse, pr.pair_reuse, pr.wall_worst, pr.wall_expected) == (80, 52, 38, 27)
    assert (merge.pair_no_reuse, round(merge.pair_reuse)) == (550, 358)
    assert merge.wall_worst == pytest.approx(155.5) and merge.wall_expected == pytest.approx(103.375)   # 156, 104
    assert (rc.pair_no_reuse, round(rc.pair_reuse), round(rc.wall_expected)) == (1236, 803, 215)
    assert rc.wall_worst == 327                       # choice 6: 11.2 prints 323 with a 4-minute startup
    assert pr.inside_budget and merge.inside_budget
    assert pr.line() == ("pr: per side 40 min; pair 80 rig-min without reuse, 52 with; wall on 4 rigs 38 min worst, "
                         "27 expected (budget 45)")
    slow = {r.run_type: r for r in cap.plan_table({"merge": 390.0})}["merge"]     # 11.2 sensitivity at 20 s per visit
    assert slow.pair_no_reuse == 780 and round(slow.wall_worst - 18) == 195 and not slow.inside_budget
    assert round(slow.pair_reuse) == 507


def test_daily_capacity_is_57_percent_with_reuse_and_87_without():
    need = cap.daily_need()
    assert need.pool_rig_hours == 64
    assert 36 <= need.rig_hours_reuse <= 37 and round(100 * need.share_reuse) == 57
    assert 55 <= need.rig_hours_no_reuse <= 56 and round(100 * need.share_no_reuse) == 87
    assert need.share_no_reuse > cap.GROWTH_TRIGGER > need.share_reuse
    assert cap.startup_rig_minutes(reuse=True) == 16 and cap.startup_rig_minutes(reuse=False) == 24


def test_utilization_counts_pool_sessions_inside_the_operating_day_once(store):
    run(store, "run-2026-11-10-merge-01", "merge", ny(DAY, 8), {"rig-01": timedelta(hours=4),
                                                                 "rig-02": timedelta(hours=2)})
    run(store, "run-2026-11-10-pr-01", "pr", ny(DAY, 10), {"rig-01": timedelta(hours=1)})     # overlaps rig-01
    run(store, "run-2026-11-10-pr-02", "pr", ny(DAY, 5), {"rig-03": timedelta(hours=2)})      # 1 h before 06:00
    run(store, "run-2026-11-10-nightly-01", "nightly", ny(DAY, 21), {"rig-05": timedelta(hours=3)})   # not pool
    u = cap.utilization(store, DAY)
    assert u.busy_min == (4 + 2 + 1) * 60
    assert u.capacity_min == 4 * 16 * 60
    assert u.share == pytest.approx(420 / 3840)


def test_two_weeks_above_70_percent_trigger_r15(store):
    for k, d in enumerate(d for d in (DAY - timedelta(days=i) for i in range(14)) if d.weekday() < 5):
        run(store, f"run-2026-11-{k:02d}-merge-01", "merge", ny(d, 6),
            {f"rig-0{i}": timedelta(hours=12) for i in range(1, 5)})
    grow, share = cap.growth_trigger(store, DAY)
    assert grow and share == pytest.approx(0.75)
    quiet = cap.growth_trigger(store, DAY + timedelta(days=20))
    assert quiet == (False, 0.0)


def test_measured_rig_minutes_wall_and_reuse_share(store):
    scope = tuple(ScopeEntry(f"census/monitor.panel-{i:03d}/ws.tier-a.options-trader", "ws.tier-a.options-trader",
                             "options_trader", 1, "frozen") for i in range(4))
    base = run(store, "run-2026-11-09-merge-01", "merge", ny(DAY - timedelta(days=1), 9))
    rid = run(store, "run-2026-11-10-merge-02", "merge", ny(DAY, 9), {"rig-01": timedelta(minutes=100),
                                                                       "rig-02": timedelta(minutes=80)},
              scope=scope, finished=ny(DAY, 11))
    for i, e in enumerate(scope):
        reused = {"base_source_run_id": base} if i < 3 else {"base_capture_sha256": "c" * 64}
        store.insert("outcomes", {"run_id": rid, "checkpoint_id": e.checkpoint_id, "outcome": "identical",
                                  "recorded_at": iso(ny(DAY, 10)), **reused})
    m = {x.run_type: x for x in cap.measured(store, ny(DAY, 0), ny(DAY, 23))}
    assert set(m) == {"merge"}
    assert (m["merge"].runs, m["merge"].rig_minutes, m["merge"].wall_minutes) == (1, 180, 120)
    assert m["merge"].reuse_share == 0.75
    assert m["merge"].line() == "merge: 1 runs, 180 rig-min per run, wall 120 min, base reuse 75 percent"


def _starvation(store: Store, run_id: str, run_type: str, starved: int, total: int = 100) -> None:
    scope = tuple(ScopeEntry(f"census/monitor.live-{i:03d}/ws.tier-a.options-trader", "ws.tier-a.options-trader",
                             "options_trader", 2, "masked_live") for i in range(total))
    run(store, run_id, run_type, ny(DAY, 10), scope=scope)
    for i, e in enumerate(scope):
        row = {"outcome": "not_run", "reason": "budget"} if i < starved else {"outcome": "identical"}
        store.insert("outcomes", {"run_id": run_id, "checkpoint_id": e.checkpoint_id, "recorded_at": iso(ny(DAY, 11)),
                                  **row})


def test_six_percent_masked_live_starvation_in_a_day_posts_p2(store):
    _starvation(store, "run-2026-11-10-merge-01", "merge", 6)
    _starvation(store, "run-2026-11-10-nightly-01", "nightly", 90)     # not pool-scheduled
    res = cap.r22_check(store, DAY)
    assert (res.masked_live, res.starved, res.share) == (100, 6, 0.06)
    assert res.alert is not None
    assert (res.alert.rule, res.alert.priority, res.alert.key) == ("coverage_loss", "P2", "r22:2026-11-10")
    assert "on_call_channel" in res.alert.route.notified
    assert cap.r22_check(store, DAY + timedelta(days=1)).masked_live == 0


def test_five_percent_is_not_over_the_r22_line(store):
    _starvation(store, "run-2026-11-10-merge-01", "merge", 5)
    res = cap.r22_check(store, DAY)
    assert res.share == 0.05 and res.alert is None


def test_report_puts_the_panel_together(store):
    _starvation(store, "run-2026-11-10-merge-01", "merge", 7)
    rep = cap.report(store, DAY)
    assert rep.alerts and rep.alerts[0].key == "r22:2026-11-10"
    lines = rep.lines()
    assert lines[0] == "capacity 2026-11-10: pool utilization 0 percent of the operating day"
    assert any(line.startswith("two-week utilization 0 percent, within the R15 trigger") for line in lines)
    assert any(line.startswith("daily need at PA-10: 36 rig-hours with reuse (57 percent of 64)") for line in lines)
    assert lines[-1].startswith("masked_live starvation 2026-11-10: 7 of 100 checkpoints")
