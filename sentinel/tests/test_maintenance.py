"""Flow maintenance (11.5, 10.5, 12.2, 5.16, SD-55), `sentinel.maintenance`."""
from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone

import pytest

from sentinel import maintenance as mt
from sentinel.records import CheckpointOutcome, MaintenanceItem, StepOutcome
from sentinel.report import jira
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

TODAY = date(2026, 11, 11)
BUILD, OTHER, BASE = "main-2026.45.1-aaa1111", "main-2026.45.2-bbb2222", "main-2026.44.9-ccc3333"
WS = "ws.tier-a.active-trader"
F1, F2, F3 = "flow.trade.order-ticket.stock.limit", "flow.trade.active-trader.read-back", "flow.monitor.positions"
SURFACE = "trade.order-entry.stock"
CP1 = f"flow/{SURFACE}/{F1}/s06"
REPORT = "https://sentinel.example/reports"


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    for b in (BUILD, OTHER, BASE):
        s.register_build(b, channel="main", source_commit=b[-7:], pipeline_artifact_url="u", installer_sha256="0" * 64,
                         first_seen_at="2026-10-01T00:00:00Z")
    yield s
    s.close()


def begin(st: Store, run_id: str, day: int, *, cand: str = BUILD) -> str:
    at = f"2026-11-{day:02d}T22:15:00+00:00"
    st.begin_run(RunPlan(run_id=run_id, run_type="merge", config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=(ScopeEntry(CP1, WS, "active_trader", 1, "frozen", flow_class="standard"),),
                         shards=(ShardPlan(f"{run_id}/1", "rig-01"),), pair_id=st.register_pair(cand, BASE, "merge"),
                         started_at=at))
    return run_id


def steps(st: Store, run_id: str, *rows: tuple[str, str, str], side: str = "candidate") -> None:
    st.write_steps([StepOutcome(run_id, flow, step, status, attempt=2 if status == "passed_after_retry" else 1,
                                locator_rung="agent" if status == "passed_via_agent_fallback" else "image",
                                mode="scripted", recorded_at=f"2026-11-{int(run_id[-2:]):02d}T22:20:00+00:00")
                    for flow, step, status in rows], shard_id=f"{run_id}/1", side=side)


def run(st: Store, run_id: str, day: int, *rows: tuple[str, str, str], cand: str = BUILD) -> str:
    begin(st, run_id, day, cand=cand)
    steps(st, run_id, *rows)
    return run_id


FB, OK, RT = "passed_via_agent_fallback", "passed", "passed_after_retry"


# -- triggers --------------------------------------------------------------------------------------------------

def test_two_consecutive_fallbacks_on_one_step_and_build_open_one_update_scripted_flow_ticket(st):
    run(st, "r01", 1, (F1, "s04", FB), (F1, "s05", OK))
    run(st, "r02", 2, (F1, "s04", FB), (F1, "s05", FB))
    t = mt.ticket_triggers(st, TODAY)
    assert [(x.flow_id, x.step_id, x.build_id, x.run_ids) for x in t.tickets] == [(F1, "s04", BUILD, ("r01", "r02"))]
    ev = t.tickets[0].sent_event("tos-trade-ui")
    assert (ev.trigger, ev.subject, ev.run_id, ev.role) == (FB, f"{F1}/s04", "r02", "tos-trade-ui")
    filed = mt.file_triggers(st, t, report_base=REPORT, at="2026-11-11T05:00:00Z", owners={F1: "tos-trade-ui"})
    assert len(filed.queued) == 1 and filed.queued[0].destination == f"SENT#task:{jira.task_key(FB, f'{F1}/s04')}"
    ledger = st.query("SELECT kind, ref, owner, run_id FROM maintenance_items")
    assert [tuple(r) for r in ledger] == [("sent_ticket", f"{FB}:{F1}/s04", "tos-trade-ui", "r02")]
    again = mt.file_triggers(st, mt.ticket_triggers(st, TODAY), report_base=REPORT, at="2026-11-11T06:00:00Z")
    assert again.queued == () and st.query("SELECT COUNT(*) FROM outbox")[0][0] == 1


def test_a_pass_between_fallbacks_or_another_build_is_not_consecutive(st):
    run(st, "r01", 1, (F1, "s04", FB))
    run(st, "r02", 2, (F1, "s04", OK))
    run(st, "r03", 3, (F1, "s04", FB))
    run(st, "r04", 4, (F1, "s04", FB), cand=OTHER)
    assert mt.ticket_triggers(st, TODAY).tickets == ()
    run(st, "r05", 5, (F1, "s04", FB))
    [t] = mt.ticket_triggers(st, TODAY).tickets
    assert (t.build_id, t.run_ids) == (BUILD, ("r03", "r05"))


def test_base_side_fallbacks_count_on_the_base_build_and_old_runs_leave_the_window(st):
    begin(st, "r01", 1)
    steps(st, "r01", (F1, "s04", FB), side="base")
    begin(st, "r02", 2)
    steps(st, "r02", (F1, "s04", FB), side="base")
    [t] = mt.ticket_triggers(st, TODAY).tickets
    assert t.build_id == BASE
    assert mt.ticket_triggers(st, date(2026, 12, 25)).tickets == ()                  # 30 days later: out of window


def test_three_retries_on_one_step_in_30_days_open_one_maintenance_task(st):
    run(st, "r01", 1, (F1, "s05", RT))
    run(st, "r02", 3, (F1, "s05", RT), (F2, "s01", RT))
    assert mt.ticket_triggers(st, TODAY).tasks == ()
    run(st, "r03", 9, (F1, "s05", RT), cand=OTHER)                                 # on any build
    t = mt.ticket_triggers(st, TODAY)
    assert [(x.flow_id, x.step_id, x.count, x.run_ids) for x in t.tasks] == [(F1, "s05", 3, ("r01", "r02", "r03"))]
    filed = mt.file_triggers(st, t, report_base=REPORT, at="2026-11-11T05:00:00Z", owners={F1: "tos-trade-ui"})
    assert len(filed.tasks) == 1 and filed.queued == ()
    row = st.query("SELECT kind, ref, owner, run_id, detail FROM maintenance_items")[0]
    assert (row["kind"], row["ref"], row["owner"], row["run_id"]) == ("maintenance_task", f"{F1}/s05", "tos-trade-ui",
                                                                      "r03")
    assert row["detail"].startswith("3 passed_after_retry in 30 days")
    assert mt.file_triggers(st, t, report_base=REPORT, at="2026-11-12T05:00:00Z").tasks == ()


def test_a_closed_task_or_ticket_is_not_reopened_by_the_rows_it_answered(st):
    for n, day in enumerate((1, 2, 3), 1):
        run(st, f"r{n:02d}", day, (F1, "s05", RT), (F1, "s04", FB))
    t = mt.ticket_triggers(st, TODAY)
    assert len(t.tasks) == 1 and len(t.tickets) == 1
    mt.file_triggers(st, t, report_base=REPORT, at="2026-11-04T05:00:00Z")
    for row in st.query("SELECT item_id FROM maintenance_items"):
        mt.close_item(st, row["item_id"], by="alice", at="2026-11-05T12:00:00Z")
    assert mt.ticket_triggers(st, TODAY) == mt.Triggers()                         # the repair answered them
    run(st, "r04", 6, (F1, "s04", FB))
    run(st, "r05", 7, (F1, "s04", FB))
    [again] = mt.ticket_triggers(st, TODAY).tickets
    assert again.run_ids == ("r04", "r05")


# -- run health ------------------------------------------------------------------------------------------------

def test_run_health_clusters_flows_in_fallback_by_anchor_template(st):
    surfaces = {SURFACE: {"controls": [{"id": "qty-field", "template": "registry/templates/trade/qty-field.png"},
                                       {"id": "price-field", "template": "registry/templates/trade/price-field.png"}]}}
    flows = [{"flow_id": f, "steps": [{"id": "s04", "type": "type_fixture", "target": f"{SURFACE}.qty-field"},
                                      {"id": "s05", "type": "type_fixture", "target": f"{SURFACE}.price-field"}]}
             for f in (F1, F2, F3)]
    anchors = mt.step_anchors(flows, surfaces)
    assert anchors[(F1, "s04")] == "qty-field"
    run(st, "r01", 5, (F1, "s04", FB), (F1, "s05", FB), (F2, "s04", FB), (F3, "s05", FB), (F3, "s04", OK))
    c = mt.fallback_clusters(st, "r01", anchors=anchors)
    assert c.flows == tuple(sorted((F1, F2, F3)))
    assert c.health_rows() == [("Flows in fallback", "3 flows in fallback, 2 on anchor price-field, "
                                                     "2 on anchor qty-field")]
    begin(st, "r02", 6)
    assert mt.fallback_clusters(st, "r02").line() == "0 flows in fallback"
    steps(st, "r02", (F1, "s04", FB))
    assert mt.fallback_clusters(st, "r02").line() == "1 flow in fallback, 1 on anchor unknown"


# -- health figures (SD-55) ------------------------------------------------------------------------------------

def test_flow_rates_are_derived_from_30_days_of_step_rows_and_outcomes(st):
    run(st, "r01", 1, (F1, "s04", FB), (F1, "s05", OK), (F1, "s06", OK), (F1, "s07", OK))
    st.write_checkpoint(CheckpointOutcome(run_id="r01", checkpoint_id=CP1, outcome="identical", workspace_id=WS,
                                          persona="active_trader", data_mode="frozen", tier=1, flow_id=F1,
                                          step_id="s06"), shard_id="r01/1")
    run(st, "r02", 2, (F1, "s04", OK))
    st.write_checkpoint(CheckpointOutcome(run_id="r02", checkpoint_id=CP1, outcome="not_settled", reason="unstable",
                                          side="candidate", detail="x", workspace_id=WS, persona="active_trader",
                                          data_mode="frozen", tier=1, flow_id=F1, step_id="s06"), shard_id="r02/1")
    r = mt.rates(st, F1, today=TODAY, last_repaired="2026-10-02T10:00:00+00:00")
    assert (r.steps, r.fallbacks, r.agent_fallback_rate) == (5, 1, pytest.approx(0.2))
    assert (r.observations, r.flaky, r.flake_rate) == (2, 1, pytest.approx(0.5))
    assert r.line() == (f"{F1}: agent_fallback_rate 20.0% (1 of 5 steps), flake_rate 50.0% (1 of 2), "
                        "last_repaired 2026-10-02")
    assert mt.rates(st, F3, today=TODAY).agent_fallback_rate is None


def test_last_repaired_reads_git_history(tmp_path):
    root = tmp_path / "cfg"
    (root / "flows").mkdir(parents=True)
    (root / "flows/f.yaml").write_text("flow_id: x\n")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "repair", "--date", "2026-10-02T10:00:00+00:00"], check=True,
                   env={"GIT_COMMITTER_DATE": "2026-10-02T10:00:00+00:00", "PATH": "/usr/bin:/bin"})
    assert mt.last_repaired(root, "flows/f.yaml").startswith("2026-10-02T10:00:00")
    assert mt.last_repaired(root, "flows/none.yaml") is None
    assert mt.last_repaired(tmp_path / "not-a-repo", "x") is None


# -- the ledger and the budget-overrun cut ---------------------------------------------------------------------

FLOWS = {F1: mt.FlowFacts(1, "tos-trade-ui"), F2: mt.FlowFacts(3, "tos-trade-ui"), F3: mt.FlowFacts(3, "tos-monitor"),
         "flow.t2.x": mt.FlowFacts(2, "tos-charts")}


def item(st: Store, kind: str, ref: str, opened: str, hours: float = 0.0, owner: str = "tos-trade-ui") -> int:
    [i] = mt.open_items(st, [MaintenanceItem(kind, ref, owner, opened, hours=hours)])
    return i


def test_hours_are_logged_per_item_and_counted_per_role_and_week(st):
    a = item(st, "anchor_lint", f"{F2}/s04", "2026-11-02T09:00:00Z")
    assert mt.open_items(st, [MaintenanceItem("anchor_lint", f"{F2}/s04", "x", "2026-11-03T09:00:00Z")]) == []
    assert mt.log_hours(st, a, 1.5) == 1.5 and mt.log_hours(st, a, 1.0) == 2.5
    b = item(st, "sent_ticket", f"{FB}:{F1}/s04", "2026-10-28T09:00:00Z", 1.0)
    mt.close_item(st, b, by="alice", at="2026-11-04T12:00:00Z", hours=2.0)       # closed this week: counts this week
    c = item(st, "landmark", f"flow/{SURFACE}/{F3}/s01#lm.x", "2026-11-03T09:00:00Z", 1.0)
    item(st, "reseed", "fx.rig-01.paper/2026-11-20", "2026-11-03T09:00:00Z", 0.5, owner="accounts_owner")
    item(st, "backend_degraded", "connectivity/2026-11-03", "2026-11-03T09:00:00Z", 4.0, owner="ops")
    assert mt.week_hours(st, date(2026, 11, 5), flows=FLOWS) == {"tier3_flow_owner": 3.5, "tier1_flow_owner": 3.0,
                                                                  "accounts_owner": 0.5, None: 4.0}
    assert mt.week_hours(st, date(2026, 10, 28), flows=FLOWS) == {}
    with pytest.raises(ValueError, match="already closed"):
        mt.close_item(st, b, by="alice", at="2026-11-05T12:00:00Z")
    with pytest.raises(ValueError, match="positive"):
        mt.log_hours(st, c, 0)


def test_tier3_over_budget_two_weeks_running_quarantines_the_highest_fallback_tier3_flow(st):
    item(st, "anchor_lint", f"{F2}/s01", "2026-11-03T09:00:00Z", 2.5)           # previous week: 2.5 h > 2 h
    item(st, "maintenance_task", f"{F2}/s04", "2026-11-10T09:00:00Z", 1.5)      # this week: 3 h > 2 h
    item(st, "maintenance_task", f"{F3}/s02", "2026-11-10T09:00:00Z", 1.5)
    rates = {F2: mt.FlowRates(F2, steps=10, fallbacks=3), F3: mt.FlowRates(F3, steps=10, fallbacks=1),
             F1: mt.FlowRates(F1, steps=10, fallbacks=9)}
    o = mt.budget_overrun(st, date(2026, 11, 11), flows=FLOWS, rates=rates)
    assert [(r.role, r.previous, r.this, r.budget) for r in o.roles] == [("tier3_flow_owner", 2.5, 3.0, 2.0)]
    assert [(p.kind, p.target, p.reason, p.owner) for p in o.proposals] == [("flow", F2, "maintenance_budget",
                                                                            "tos-trade-ui")]
    assert o.backlog == 1.5 and o.allowance == 2.0 and not o.freeze_new_flows
    assert o.header_lines == ("tier 3 coverage reduced: maintenance budget, 1 flow",)
    assert o.proposals[0].entered == date(2026, 11, 15)                         # the last day of the week
    assert "maintenance_budget" in o.health_rows()[0][1] and F1 not in {p.target for p in o.proposals}
    one_week = mt.budget_overrun(st, date(2026, 11, 18), flows=FLOWS, rates=rates)
    assert one_week.roles == () and one_week.proposals == ()


def test_a_tier1_overrun_pages_and_freezes_and_never_cuts_a_flow(st):
    item(st, "anchor_lint", f"{F1}/s01", "2026-11-03T09:00:00Z", 5.0)
    item(st, "anchor_lint", f"{F1}/s02", "2026-11-10T09:00:00Z", 5.0)
    o = mt.budget_overrun(st, date(2026, 11, 11), flows=FLOWS, rates={F1: mt.FlowRates(F1, 4, 4)})
    assert [r.role for r in o.roles] == ["tier1_flow_owner"]
    assert o.proposals == () and o.header_lines == ()
    assert o.page_system_owner and o.freeze_new_flows and o.tell_release_manager
    assert "new flows frozen" in o.health_rows()[0][1]


def test_the_cut_takes_tier3_before_tier2_and_skips_quarantined_flows(st):
    for ref, opened, h in ((f"{F2}/a", "2026-11-03T09:00:00Z", 3.0), (f"{F3}/a", "2026-11-10T09:00:00Z", 3.0),
                           ("flow.t2.x/a", "2026-11-03T09:00:00Z", 4.0), ("flow.t2.x/b", "2026-11-10T09:00:00Z", 4.0)):
        item(st, "anchor_lint", ref, opened, h)
    o = mt.budget_overrun(st, date(2026, 11, 11), flows=FLOWS, quarantined=[F2])
    assert {r.role for r in o.roles} == {"tier2_flow_owner", "tier3_flow_owner"}
    assert [p.target for p in o.proposals] == [F3, "flow.t2.x"]
    assert o.header_lines == ("tier 3 coverage reduced: maintenance budget, 1 flow",
                              "tier 2 coverage reduced: maintenance budget, 1 flow")


# -- what the Reporter prints ----------------------------------------------------------------------------------

def test_items_past_sla_flows_repaired_and_the_reporter_rows(st):
    old = item(st, "anchor_lint", f"{F2}/s01", "2026-11-02T14:00:00Z")          # Monday 09:00 ET: due Friday 17:00
    item(st, "anchor_lint", f"{F3}/s01", "2026-11-10T14:00:00Z")
    now = datetime(2026, 11, 11, 15, tzinfo=timezone.utc)
    assert [p.item_id for p in mt.items_past_sla(st, now)] == [old]
    done = item(st, "maintenance_task", f"{F1}/s05", "2026-10-20T14:00:00Z")
    mt.close_item(st, done, by="alice", at="2026-11-05T12:00:00Z", hours=1.0)
    assert mt.flows_repaired(st, "2026-11-01T00:00:00Z", "2026-11-12T00:00:00Z", flows=FLOWS) == (F1,)
    run(st, "r01", 5, (F1, "s04", FB), (F1, "s05", OK))
    m = mt.reporter_metrics(st, now=now, release_since="2026-11-01T00:00:00Z", flows=FLOWS)
    rows = dict(m.health_rows())
    assert rows["Fallback rate per flow"] == f"{F1} 50.0% (1 of 2 steps)"
    assert rows["Flows repaired this release"] == f"1: {F1}"
    assert rows["Maintenance items past SLA"].startswith(f"1: anchor_lint {F2}/s01 (tos-trade-ui, due 2026-11-06)")


def test_flow_of_reads_every_ref_shape():
    assert mt.flow_of(f"{F1}/s04", FLOWS) == F1
    assert mt.flow_of(f"{FB}:{F1}/s04", FLOWS) == F1
    assert mt.flow_of(f"flow/{SURFACE}/{F3}/s01#lm.x", FLOWS) == F3
    assert mt.flow_of(F2, FLOWS) == F2
    assert mt.flow_of("connectivity/2026-11-03", FLOWS) is None
    assert mt.role_of({"ref": "x", "owner": "harness_on_call"}, FLOWS) == "harness_on_call"
