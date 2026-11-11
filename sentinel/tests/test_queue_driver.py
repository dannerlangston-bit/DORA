"""Demo runs through the run queue (pipeline.queue_driver, 11.2): starts, the start deadline written for a run that
never started, the queue's alerts converted for dispatch, and the journal that survives a control-host restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sentinel.calendar import WindowsConfig
from sentinel.lint.repo import Repo
from sentinel.orchestrator import queue as q
from sentinel.orchestrator.clock import ManualClock
from sentinel.pipeline.queue_driver import QueueDriver, event_line, read_event
from sentinel.store import Store

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
T0 = datetime(2026, 11, 10, 15, 31, tzinfo=timezone.utc)          # 10:31 New York


def driver(tmp_path, started, **kw):
    windows = WindowsConfig.from_doc(Repo.load(EXAMPLE).one("windows").data)
    store = Store(tmp_path / "sentinel.db")
    clock = ManualClock(start=T0)
    ids = iter(f"run-2026-11-10-pr-{n:02d}" for n in range(1, 99))
    d = QueueDriver(store, clock, cfg_root=EXAMPLE, windows=windows, state_path=tmp_path / "state" / "queue.jsonl",
                    start=lambda req, rigs: started.append((req.run_id, req.pr, tuple(rigs))) or "complete",
                    run_ids=lambda _t: next(ids), **kw)
    return d, store


def test_a_pr_webhook_starts_its_run_on_the_pool(tmp_path):
    started: list = []
    d, store = driver(tmp_path, started)
    d.submit(q.PrBuild(4823, "branch-1998.3.0-a41c7e9", T0))
    assert started == [("run-2026-11-10-pr-01", 4823, (1, 2, 3, 4))]
    assert d.finished == {"run-2026-11-10-pr-01": "complete"}
    store.close()


def test_a_pr_with_the_pool_down_is_aborted_at_its_start_deadline_by_the_reporter(tmp_path):
    started: list = []
    d, store = driver(tmp_path, started)
    for rig in (1, 2, 3, 4):
        d.submit(q.RigState(rig, up=False))
    d.submit(q.PrBuild(4823, "branch-1998.3.0-a41c7e9", T0))
    d.advance(46 * 60)
    assert started == []
    rid = "run-2026-11-10-pr-01"
    out = store.run_outcome(rid)
    assert (out.status, out.reason) == ("aborted_budget", "pool_saturated")
    assert "runs_ahead" in store.run_summary(rid)["abort"]
    assert any(a.rule == "pool_saturated" and a.key.endswith(":pool_saturated") for a in d.alerts)   # 9.10
    store.close()


def test_the_queue_survives_a_control_host_restart_through_its_journal(tmp_path):
    started: list = []
    d, store = driver(tmp_path, started)
    for rig in (1, 2, 3, 4):
        d.submit(q.RigState(rig, up=False))
    d.submit(q.PrBuild(4823, "branch-1998.3.0-a41c7e9", T0))
    d.advance(10 * 60)
    position = d.queue.position("run-2026-11-10-pr-01")
    assert position == 1
    again = QueueDriver(store, d.clock, cfg_root=EXAMPLE, windows=d.windows, state_path=d.state_path,
                        start=lambda req, rigs: started.append(req.run_id) or "complete")
    assert again.restore() == 5 and again.queue.position("run-2026-11-10-pr-01") == position
    again.submit(q.RigState(1, up=True))                  # a rig comes back: the queued run starts there
    assert started == ["run-2026-11-10-pr-01"]
    store.close()


def test_journal_lines_round_trip_every_event_type():
    ev = q.RcBuild("rc-1998.4.0", "1998.4", T0, signoff_at=T0 + timedelta(days=2), payload={"k": [1, 2]})
    at, back, ids = read_event(event_line(ev, T0, ["rc-1"]))
    assert at == T0 and back == ev and ids == ["rc-1"]
