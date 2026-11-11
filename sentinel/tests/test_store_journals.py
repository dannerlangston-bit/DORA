"""Tests for sentinel.store.journal: the drop and Watchdog journals become step_events rows, idempotently (8.2,
8.3, 8.4, 10.2)."""
import json

import pytest

from sentinel.records import StepEvent
from sentinel.store import ImportConflict, RunPlan, ScopeEntry, ShardPlan, Store, StoreRefused
from sentinel.store.journal import GATEWAY_JOURNAL, WATCHDOG_JOURNAL, ingest_journal

RUN = "run-01"
SHARD = f"{RUN}/1"
WS = "ws.tier-a.active-trader"
CID = f"census/trade.active-trader.ladder/{WS}"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.register_rig("rig-a2", login_alias="paper-02", owner="qa", snapshot_id="snap-2", allowlist_entry=2)
    s.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=2700, scope=(ScopeEntry(CID, WS, "active_trader", 1, "frozen"),),
                        shards=(ShardPlan(SHARD, "rig-a1"), ShardPlan(f"{RUN}/2", "rig-a2"))))
    yield s
    s.close()


def drop(seq, shard=SHARD, reason="no_token", gate_class="integrity"):
    return StepEvent(run_id=RUN, shard_id=shard, side="candidate", seq=seq, source="gateway_hook",
                     wall_at="2026-11-09T19:03:21+00:00", mono_ms=1000 + seq, mode="scripted",
                     action="gateway_decision", gate_decision="dropped", gate_reason=reason, gate_class=gate_class,
                     keys="button_down:left")


def write_lines(path, rows, torn=""):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows) + torn)


def test_a_drop_journal_ingests_once_and_counts_as_gateway_drops(store, tmp_path):
    store.write_events([StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=1, source="orchestrator",
                                  wall_at="2026-11-09T19:03:20+00:00", mono_ms=990, mode="scripted", action="click")])
    journal = tmp_path / GATEWAY_JOURNAL
    write_lines(journal, [drop(2).to_dict(), drop(3, reason="deny_rect", gate_class="safety").to_dict()],
                torn='{"run_id": "run-01", "se')          # the Orchestrator died mid-write
    r = ingest_journal(store, SHARD, journal, "gateway_hook")
    assert (r.read, r.written) == (2, 2)
    assert ingest_journal(store, SHARD, journal, "gateway_hook").written == 0      # resume, then import: once
    rows = store.query("SELECT seq, source, gate_decision FROM step_events WHERE shard_id = ? ORDER BY seq", (SHARD,))
    assert [tuple(r) for r in rows] == [(1, "orchestrator", "none"), (2, "gateway_hook", "dropped"),
                                        (3, "gateway_hook", "dropped")]
    assert store.gateway_drops(RUN) == 2
    write_lines(journal, [{**drop(2).to_dict(), "gate_reason": "token_expired"}])
    with pytest.raises(ImportConflict):                     # the same seq with other content
        ingest_journal(store, SHARD, journal, "gateway_hook")
    write_lines(journal, [drop(9, shard=f"{RUN}/2").to_dict()])
    with pytest.raises(StoreRefused, match="journal of"):
        ingest_journal(store, SHARD, journal, "gateway_hook")


def test_a_watchdog_journal_yields_system_rows_keyed_by_event_id(store, tmp_path):
    store.write_events([StepEvent(run_id=RUN, shard_id=SHARD, side="candidate", seq=1, source="orchestrator",
                                  wall_at="2026-11-09T19:03:20+00:00", mono_ms=990, mode="scripted", action="click")])
    hang = {"event_id": "wd-0001", "action": "watchdog_hang", "at": "2026-11-09T14:03:41-05:00", "mono_ms": 2000,
            "checkpoint_id": CID, "detail": "cause: hang", "app_uptime_s": 1820, "app_restart_count": 0}
    relaunch = {"event_id": "wd-0002", "action": "relaunch", "at": "2026-11-09T19:04:10Z", "mono_ms": 2500}
    journal = tmp_path / WATCHDOG_JOURNAL
    write_lines(journal, [hang, relaunch])
    assert ingest_journal(store, SHARD, journal, "watchdog").written == 2
    assert ingest_journal(store, SHARD, journal, "watchdog").written == 0
    rows = store.query("SELECT seq, source, mode, action, reason, wall_at FROM step_events WHERE source = 'watchdog' "
                       "ORDER BY seq")
    assert [tuple(r) for r in rows] == [
        (2, "watchdog", "system", "watchdog_hang", "wd-0001: cause: hang", "2026-11-09T19:03:41+00:00"),
        (3, "watchdog", "system", "relaunch", "wd-0002", "2026-11-09T19:04:10+00:00")]
    write_lines(journal, [{**hang, "detail": "cause: oom"}])
    with pytest.raises(ImportConflict, match="wd-0001"):
        ingest_journal(store, SHARD, journal, "watchdog")
    for bad in ({**relaunch, "event_id": "wd-9", "action": "click"}, {"event_id": "wd-8", "action": "relaunch"}):
        write_lines(journal, [bad])
        with pytest.raises(StoreRefused):
            ingest_journal(store, SHARD, journal, "watchdog")
    assert ingest_journal(store, SHARD, tmp_path / "absent.jsonl", "watchdog").read == 0
    with pytest.raises(StoreRefused, match="source"):
        ingest_journal(store, SHARD, journal, "orchestrator")
    with pytest.raises(StoreRefused, match="unknown shard"):
        ingest_journal(store, f"{RUN}/9", journal, "watchdog")
