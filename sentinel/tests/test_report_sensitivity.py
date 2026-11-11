"""The effective-sensitivity report, the seven-day mask warning and the mask budget notice (3.8, 3.9)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from sentinel.diff.regions import Rect, RedactionBox, Region, RegionsFile, mask_budget
from sentinel.lint.runstart import expiring_soon
from sentinel.orchestrator import alerts
from sentinel.pipeline.config import SentinelConfig
from sentinel.records import CheckpointOutcome
from sentinel.report.sensitivity import (compute, floor_exceptions, mask_budget_notices, mask_expiry_notices, measure,
                                         previous_rows)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
RUN = "merge-2026-11-10"
WS = "ws.tier-a.active-trader"
PS = f"census/monitor.activity.position-statement/{WS}"
LADDER = f"census/trade.active-trader.ladder/{WS}"


def regfile() -> RegionsFile:
    def region(rid: str, kind: str, rect: Rect, **kw) -> Region:
        return Region(rid, kind, "lm", rect, panel="position_statement", **kw)
    return RegionsFile(PS, [], [region("rgn.qty", "diff", Rect(0, 0, 50, 20)),
                                region("rgn.mark", "rule", Rect(50, 0, 25, 20), rule={"field": "price"}),
                                region("rgn.update", "mask", Rect(75, 0, 25, 20), mask_ref="msk.1")])


def test_measure_gives_the_three_shares_and_budget_use_per_panel():
    rf = regfile()
    located = {r.id: r.rect for r in rf.regions}
    box = [RedactionBox(Rect(0, 30, 10, 10))]
    budget = mask_budget(rf, {"candidate": located, "base": located}, box, {"position_statement": 0.10})
    row = measure(rf, located, box, 100, 40, budget)
    assert (row["diff_freeze"], row["rule"], row["mask_redaction"]) == (72.5, 12.5, 15.0)   # _rest is diffed
    assert {p["side"]: p["share"] for p in row["panels"]} == {"candidate": 25.0, "base": 25.0}
    report = compute({PS: row}, outcomes=[CheckpointOutcome(run_id=RUN, checkpoint_id=PS, outcome="identical",
                                                            workspace_id=WS, persona="active_trader",
                                                            data_mode="frozen", tier=1)])
    (cp,) = report.rows
    assert cp.tier == 1 and cp.shares_text() == ("72.5", "12.5", "15.0")
    assert [p.text() for p in cp.panels] == ["position_statement 25.0 of 10 percent (over budget)"]
    assert report.over_budget == (PS,)


def test_week_over_week_change_and_a_checkpoint_measured_only_by_its_outcome():
    now = {PS: {"diff_freeze": 80.0, "rule": 15.0, "mask_redaction": 5.0, "panels": []}}
    before = {PS: {"diff_freeze": 81.5, "rule": 15.0, "mask_redaction": 3.5, "panels": []}}
    ladder = CheckpointOutcome(run_id=RUN, checkpoint_id=LADDER, outcome="identical", workspace_id=WS,
                               persona="active_trader", data_mode="frozen", tier=1, masked_pct=2.25)
    report = compute(now, before, outcomes=[ladder], previous_run_id="merge-2026-11-03")
    rows = {r.checkpoint_id: r for r in report.rows}
    assert rows[PS].change_text(True) == "diff+freeze -1.5, rule +0.0, mask+redaction +1.5"
    assert rows[LADDER].shares_text() == ("not measured", "not measured", "2.2") and rows[LADDER].change_text(True) \
        == "new"
    assert report.previous_run_id == "merge-2026-11-03"
    assert compute(now).rows[0].change_text(False) == "no run a week earlier"


def test_floor_exception_regions_appear_in_the_report():
    doc = {"regions": [{"id": "rgn.chart.axis", "type": "diff", "landmark": "lm", "rect": {"dx": 0, "dy": 0, "w": 9,
                                                                                         "h": 9},
                        "floor_exception": {"approved_by": "asmith", "reason": "antialiasing", "ticket": "TOS-1",
                                            "expires_at": "2026-12-01T00:00:00Z"}},
                       {"id": "rgn.other", "type": "diff", "landmark": "lm", "rect": {"dx": 0, "dy": 9, "w": 9, "h": 9}}]}
    (fe,) = compute({}, regions={"census/charts.flexible-grid.chart-cell/ws": doc}).floor_exceptions
    assert (fe.region_id, fe.approved_by, fe.ticket) == ("rgn.chart.axis", "asmith", "TOS-1")
    assert floor_exceptions(SentinelConfig.load(EXAMPLE).regions) == ()      # the example declares none


def test_a_mask_expiring_in_6_days_warns_its_owner():
    warnings = expiring_soon(EXAMPLE, date(2026, 11, 29))
    soon = [w for w in warnings if w.mask_id == "msk.ps.last-update-01"]
    assert soon and soon[0].days_left == 6
    notices = mask_expiry_notices(warnings)
    (n,) = [x for x in notices if "tos-monitor-ui" in x.owners]
    assert n.kind == "mask_expiring" and n.route.priority == alerts.P3
    assert any(line.startswith("mask msk.ps.last-update-01 expires in 6 days") for line in n.lines)
    assert PS in n.checkpoint_ids
    assert mask_expiry_notices(expiring_soon(EXAMPLE, date(2026, 11, 20))) == []    # 15 days out: no warning yet


def test_a_mask_budget_breach_goes_to_on_call_and_the_mask_owner():
    cfg = SentinelConfig.load(EXAMPLE)
    breach = CheckpointOutcome(run_id=RUN, checkpoint_id=PS, outcome="harness_error", workspace_id=WS,
                               persona="active_trader", data_mode="frozen", reason="mask_budget_exceeded",
                               detail="panel position_statement share 0.120 over budget 0.10 (candidate)")
    clean = CheckpointOutcome(run_id=RUN, checkpoint_id=LADDER, outcome="harness_error", workspace_id=WS,
                              persona="active_trader", data_mode="frozen", reason="mask_budget_exceeded")
    a, b = mask_budget_notices([breach, clean], cfg.masks)
    assert a.owners == ("tos-monitor-ui",) and a.route == alerts.harness_error()
    assert alerts.ON_CALL_CHANNEL in a.route.notified and a.kind == "mask_budget_exceeded"
    assert "share 0.120 over budget 0.10" in a.lines[0]
    assert b.owners == () and "on-call alone" in b.lines[0]


class FakeStore:
    """`query` and `run_summary` over three runs: the current one, one 3 days and one 8 days earlier."""

    RUNS = [("merge-11-10", "merge", "2026-11-10T14:00:00Z", "running"),
            ("merge-11-07", "merge", "2026-11-07T14:00:00Z", "finished"),
            ("merge-11-02", "merge", "2026-11-02T13:00:00-05:00", "finished"),
            ("pr-11-01", "pr", "2026-11-01T14:00:00Z", "finished")]

    def query(self, sql, args=()):
        if sql.startswith("SELECT run_type, started_at FROM runs WHERE run_id"):
            return [{"run_type": t, "started_at": s} for r, t, s, _ in self.RUNS if r == args[0]]
        rows = [{"run_id": r, "started_at": s} for r, t, s, life in self.RUNS
                if t == args[0] and life == "finished" and r != args[1]]
        return sorted(rows, key=lambda x: x["started_at"], reverse=True)

    def run_summary(self, run_id):
        return {"sensitivity": {PS: {"diff_freeze": 1.0, "rule": 2.0, "mask_redaction": 3.0}}} \
            if run_id == "merge-11-02" else {}


def test_the_week_earlier_run_is_the_newest_same_type_run_at_least_seven_days_older():
    run_id, rows = previous_rows(FakeStore(), "merge-11-10")
    assert run_id == "merge-11-02" and rows[PS]["mask_redaction"] == 3.0
    assert previous_rows(FakeStore(), "unknown-run") == (None, None)
