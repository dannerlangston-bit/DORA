"""Tests for sentinel.store.writer: one writer thread drains a queue of batches (8.2)."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.store import RunPlan, ScopeEntry, Store, StoreError, StoreRefused, WriterLockError
from sentinel.store.writer import WriterService

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CID = f"census/trade.active-trader.ladder/{WS}"
T0 = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)


def begin(s: Store) -> None:
    s.begin_run(RunPlan(run_id=RUN, run_type="rc", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=(ScopeEntry(CID, WS, "active_trader", 1, "frozen"),)))


def sample(producer: int, i: int, run_id: str = RUN) -> tuple:
    return run_id, [{"sampled_at": (T0 + timedelta(seconds=i)).isoformat(), "pid": producer, "metric": "rss",
                     "value": float(i)}]


def flake(s: Store, producer: int, i: int, run_id: str = RUN) -> int:
    """One batch that records who wrote it and when in its submission order."""
    return s.write_flake_event(run_id, CID, captures=["a" * 64, "b" * 64], metrics={"p": producer, "i": i})


def test_batches_from_four_threads_commit_in_order_and_a_refusal_raises_in_its_submitter(tmp_path):
    db = tmp_path / "run.db"
    with WriterService(lambda: Store(db, ledger=False)) as w:
        w.call(begin)
        futures: dict[int, list] = {p: [] for p in range(4)}
        bad: list = []

        def produce(p: int) -> None:
            for i in range(250):
                if p == 2 and i == 100:
                    bad.append(w.submit(flake, p, i, "run-unknown"))
                futures[p].append(w.submit(flake, p, i))

        threads = [threading.Thread(target=produce, args=(p,)) for p in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(isinstance(f.result(30), int) for fs in futures.values() for f in fs)
        with pytest.raises(StoreRefused):           # the refused batch raises in its own submitter only
            bad[0].result(30)
        rows = w.call(lambda s: [json.loads(r[0]) for r in s.query(
            "SELECT metrics_json FROM flake_events ORDER BY flake_event_id")])
    assert len(rows) == 1000
    for p in range(4):
        assert [r["i"] for r in rows if r["p"] == p] == list(range(250))   # each producer's order


def test_the_store_lives_on_the_writer_thread_and_close_drains_the_queue(tmp_path):
    db = tmp_path / "run.db"
    w = WriterService(lambda: Store(db, ledger=False)).start()
    w.call(begin)
    owner = w.call(lambda s: threading.get_ident())
    assert owner != threading.get_ident()
    pending = [w.submit(lambda s, *a: s.write_soak_samples(*a), *sample(0, i)) for i in range(20)]
    w.close()
    assert all(f.done() and f.exception() is None for f in pending)
    with pytest.raises(StoreError, match="not running"):
        w.submit(lambda s: None)
    w.close()                                       # idempotent
    with Store(db) as s:                            # the writer released its lock and connection
        assert s.query("SELECT count(*) FROM soak_samples")[0][0] == 20


def test_an_open_failure_is_raised_by_start(tmp_path):
    db = tmp_path / "run.db"
    with Store(db):
        with pytest.raises(WriterLockError):
            WriterService(lambda: Store(db)).start()
