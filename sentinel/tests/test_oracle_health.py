"""Oracle health across runs (2.8.1, 5.10): OCR calibration tasks, fallbacks per source and grid-copy flow tasks,
`sentinel.oracle_health`."""
from __future__ import annotations

import pytest

from sentinel import oracle_health as H
from sentinel.oracle.sources import SourceFallback
from sentinel.records import Extraction, MaintenanceItem
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

WS = "ws.tier-a.active-trader"
FLOW = "flow.trade.order-ticket.stock.limit"
FCP = f"flow/trade.order-entry.stock/{FLOW}/s06"
T2CP = f"census/monitor.activity.position-statement/{WS}"
CELL = "rgn.ticket.limit-price"
X, Y, BASE = "main-2026.45.1-aaa1111", "main-2026.45.2-bbb2222", "main-2026.44.9-ccc3333"
MONEY = {(FCP, CELL), (T2CP, CELL)}
OWNERS = {FCP: "flow-owner-t1", T2CP: "flow-owner-t2"}


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    for b in (X, Y, BASE):
        s.register_build(b, channel="main", source_commit=b[-7:], pipeline_artifact_url="u", installer_sha256="0" * 64,
                         first_seen_at="2026-10-01T00:00:00Z")
    yield s
    s.close()


SCOPE = (ScopeEntry(FCP, WS, "active_trader", 1, "frozen", flow_class="standard"),
         ScopeEntry(T2CP, WS, "active_trader", 2, "frozen"))


def begin(st: Store, run_id: str, day: int, cand: str = X, base: str = BASE) -> str:
    st.begin_run(RunPlan(run_id=run_id, run_type="merge", config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=SCOPE, shards=(ShardPlan(f"{run_id}/1", "rig-01"),),
                         pair_id=st.register_pair(cand, base, "merge"), started_at=f"2026-11-{day:02d}T22:15:00+00:00"))
    return run_id


def read(st: Store, run_id: str, *, conf: float = 95.0, text: str = "12.50", second: str = "12.50",
         cid: str = FCP, side: str = "candidate") -> None:
    """One stored OCR read of the money cell (3x pass plus its 2x second pass, 2.8.1)."""
    common = {"run_id": run_id, "checkpoint_id": cid, "side": side, "assertion_id": f"rule.{CELL}.value",
              "field": CELL, "source": "ocr", "read_at": "2026-11-01T22:20:00+00:00", "region_id": CELL}
    st.write_extractions([Extraction(**common, text=text, value=text, confidence=conf),
                          Extraction(**common, text=second, second_pass=True)])


def unreadable(st: Store, run_id: str, **kw) -> None:
    read(st, run_id, conf=60.0, **kw)


def health(st: Store, run_id: str) -> H.OracleHealth:
    return H.evaluate(st, run_id, money_cells=MONEY, owner_of=OWNERS.__getitem__)


def test_three_consecutive_unreadable_runs_on_one_build_open_one_ocr_calibration_task(st):
    for n in range(1, 3):
        unreadable(st, begin(st, f"r{n}", n))
    assert health(st, "r2").items == () and health(st, "r2").unreadable_streaks == {f"{FCP}#{CELL}": 2}
    unreadable(st, begin(st, "r3", 3))
    [item] = health(st, "r3").items
    assert (item.kind, item.ref, item.owner, item.run_id) == ("ocr_calibration", f"{FCP}#{CELL}", "flow-owner-t1", "r3")
    assert f"3 consecutive runs on build {X}" in item.detail and "larger crop" in item.detail


def test_a_readable_run_in_between_resets_the_streak(st):
    unreadable(st, begin(st, "r1", 1))
    read(st, begin(st, "r2", 2))
    unreadable(st, begin(st, "r3", 3))
    unreadable(st, begin(st, "r4", 4))
    h = health(st, "r4")
    assert h.items == () and h.unreadable_streaks == {f"{FCP}#{CELL}": 2}
    assert health(st, "r2").unreadable_streaks == {}                     # readable in that run: no streak


def test_a_run_on_another_build_does_not_count(st):
    unreadable(st, begin(st, "r1", 1))
    unreadable(st, begin(st, "r2", 2, cand=Y))
    unreadable(st, begin(st, "r3", 3))
    assert health(st, "r3").items == () and health(st, "r2").unreadable_streaks == {f"{FCP}#{CELL}": 1}


def test_base_side_reads_count_for_the_base_build(st):
    for n in range(1, 4):
        unreadable(st, begin(st, f"r{n}", n, cand=(X, Y, X)[n - 1]), side="base")
    [item] = health(st, "r3").items
    assert f"build {BASE}" in item.detail


def test_digit_count_disagreement_between_passes_is_unreadable(st):
    for n in range(1, 4):
        read(st, begin(st, f"r{n}", n), conf=97.0, second="1.50")
    assert [i.kind for i in health(st, "r3").items] == ["ocr_calibration"]


def test_tier_2_and_non_money_cells_never_open_one(st):
    for n in range(1, 4):
        begin(st, f"r{n}", n)
        unreadable(st, f"r{n}", cid=T2CP)
    assert health(st, "r3").items == ()
    assert H.evaluate(st, "r3", money_cells={(T2CP, CELL)}, owner_of=OWNERS.__getitem__).items == ()


def test_an_open_task_is_not_duplicated_and_a_closed_one_restarts_the_count(st):
    for n in range(1, 4):
        unreadable(st, begin(st, f"r{n}", n))
    st.write_maintenance_item(MaintenanceItem("ocr_calibration", f"{FCP}#{CELL}", "flow-owner-t1",
                                              "2026-11-03T23:00:00Z"))
    unreadable(st, begin(st, "r4", 4))
    assert health(st, "r4").items == ()
    item_id = st.query("SELECT item_id FROM maintenance_items")[0][0]
    st.update("maintenance_items", {"item_id": item_id}, {"closed_at": "2026-11-04T23:00:00+00:00",
                                                          "closed_by": "jdoe"})
    unreadable(st, begin(st, "r5", 5))
    unreadable(st, begin(st, "r6", 6))
    assert health(st, "r6").items == () and health(st, "r6").unreadable_streaks == {f"{FCP}#{CELL}": 2}
    unreadable(st, begin(st, "r7", 7))
    assert [i.run_id for i in health(st, "r7").items] == ["r7"]


# -- source fallbacks ----------------------------------------------------------------------------------------------
GRID = SourceFallback("qty", "export", "ocr", "copy_all header mismatch")


def fallbacks(st: Store, run_id: str, *fbs: SourceFallback, cid: str = FCP) -> None:
    st.write_events([H.fallback_event(fb, run_id=run_id, shard_id=f"{run_id}/1", side="candidate", seq=n,
                                      wall_at="2026-11-01T22:30:00+00:00", mono_ms=10 * n, checkpoint_id=cid,
                                      flow_id=FLOW if cid == FCP else None, step_id="s06" if cid == FCP else None)
                     for n, fb in enumerate(fbs, 1)])


def test_fallback_events_round_trip_and_are_counted_per_source(st):
    jab = SourceFallback("price", "jab", "ocr", "cell not exposed")
    fallbacks(st, begin(st, "r1", 1), GRID, SourceFallback("last", "export", "ocr", "parse"), jab)
    [ev] = [e for e in st.step_events("r1") if e.seq == 3]
    assert (ev.action, ev.mode, ev.reason) == ("oracle_source_fallback", "system", jab.event())
    assert H.parse_fallback(jab.event()) == H.Fallback("price", "jab", "ocr")
    assert H.parse_fallback("retry: locator miss") is None
    h = health(st, "r1")
    assert h.fallbacks_by_source == {"export": 2, "jab": 1} and h.items == ()
    assert h.health()["oracle_fallbacks"] == {"export": 2, "jab": 1}


def test_three_grid_copy_fallbacks_on_one_grid_in_a_week_open_a_flow_task(st):
    fallbacks(st, begin(st, "r1", 1), GRID)                    # 1 November: 8 days before r4, outside the week
    fallbacks(st, begin(st, "r2", 4), GRID, GRID)              # one run with two fallbacks counts once
    fallbacks(st, begin(st, "r3", 6), SourceFallback("price", "jab", "ocr", "x"))
    fallbacks(st, begin(st, "r4", 9), GRID)
    assert health(st, "r4").items == ()
    fallbacks(st, begin(st, "r5", 10), GRID)
    [item] = health(st, "r5").items
    assert (item.kind, item.ref, item.owner) == ("flow_task", FLOW, "flow-owner-t1") and "3 runs" in item.detail
    assert health(st, "r5").health()["maintenance_opened"] == [{"kind": "flow_task", "ref": FLOW,
                                                               "owner": "flow-owner-t1"}]
    st.write_maintenance_item(item)
    fallbacks(st, begin(st, "r6", 11), GRID)
    assert health(st, "r6").items == ()                         # the open task is not opened again


def test_an_unknown_run_is_refused(st):
    with pytest.raises(ValueError, match="unknown run"):
        health(st, "nope")
