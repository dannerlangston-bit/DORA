"""Quarantine triggers, proposals, the quarantine.yaml patch, the run-start mirror and the tier-1 share
(10.5, 3.10 step 6, 3.9 step 6, 11.5, 9.11), `sentinel.quarantine`."""
from __future__ import annotations

from datetime import date

import pytest

from sentinel import flake, quarantine as Q
from sentinel.flake import BuildCounts, FlakeStats
from sentinel.lint import schemas
from sentinel.lint.repo import load_yaml_text
from sentinel.records import CheckpointOutcome, StepEvent
from sentinel.store import safety as S
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

TODAY = date(2026, 11, 10)                       # a Tuesday
WS = "ws.tier-a.active-trader"
CP = f"census/trade.order-entry.stock/{WS}"
FLOW = "flow.trade.order-ticket.stock.limit"
FCP = f"flow/trade.order-entry.stock/{FLOW}/s06"
CANARY = "flow.canary.ticket-stock"
CCP = f"flow/trade.order-entry.stock/{CANARY}/cp-ready"
BUILD = "main-2026.45.1-aaa1111"


def stats(cid: str = CP, **kw) -> FlakeStats:
    base = {"tier": 1, "data_mode": "frozen"}
    if cid == FCP:
        base.update(flow_id=FLOW, flow_class="standard")
    if cid == CCP:
        base.update(flow_id=CANARY, flow_class="canary")
    return FlakeStats(cid, **{**base, **kw})


def night(n: int, drifted: bool) -> Q.AANight:
    return Q.AANight(f"nightly-{n}", f"2026-11-0{n}T03:00:00+00:00", drifted)


def steps(flow: str, fallbacks: int, total: int, *, day: str = "2026-11-09", drill: bool = False) -> list[StepEvent]:
    return [StepEvent("run-1", "run-1/1", "candidate", n + 1, "orchestrator", f"{day}T12:00:00+00:00", n,
                      "agent" if n < fallbacks else "scripted", "click", flow_id=flow, step_id=f"s{n:02d}",
                      step_outcome="passed_via_agent_fallback" if n < fallbacks else "passed", drill=drill)
            for n in range(total)]


def evaluate(stats_=(), events=(), fixes=(), history=None, **kw) -> Q.Evaluation:
    return Q.evaluate(list(stats_), list(events), list(fixes), history or {}, TODAY, **kw)


# -- triggers, each exactly at its threshold and not below ---------------------------------------------------------
@pytest.mark.parametrize("counts, fires", [
    (BuildCounts(9, 1, 0), True),                # 0.111 over 9 observations of one build
    (BuildCounts(10, 1, 0), False),              # exactly 0.10 is not above it
    (BuildCounts(5, 0, 1), True),                # a flip counts like an unstable outcome
    (BuildCounts(4, 1, 0), False),               # 0.25 but fewer than 5 observations
])
def test_flake_score_above_0_10_over_5_or_more_observations_of_one_build(counts, fires):
    ev = evaluate([stats(by_build={BUILD: counts}, observations=counts.observations)])
    assert [p.reason for p in ev.proposals] == (["flake"] if fires else [])
    if fires:
        assert f"observations of build {BUILD}" in ev.proposals[0].detail


@pytest.mark.parametrize("field, value, fires", [
    ("unstable_7d", 3, True), ("unstable_7d", 2, False), ("flips_same_build", 2, True), ("flips_same_build", 1, False)])
def test_three_unstable_in_7_days_and_two_flips_on_one_build(field, value, fires):
    ev = evaluate([stats(**{field: value})])
    assert [(p.kind, p.target, p.reason) for p in ev.proposals] == ([("checkpoint", CP, "flake")] if fires else [])


def test_aa_drift_on_three_consecutive_nightly_rows():
    hit = evaluate(history={CP: (night(1, False), night(2, True), night(3, True), night(4, True))})
    assert [(p.target, p.reason) for p in hit.proposals] == [(CP, "aa_drift")]
    assert "nightly-2, nightly-3, nightly-4" in hit.proposals[0].detail
    assert evaluate(history={CP: (night(1, True), night(2, False), night(3, True), night(4, True))}).proposals == ()
    assert evaluate(history={CP: (night(3, True), night(4, True))}).proposals == ()


def test_a_frozen_fix_item_quarantines_on_the_sixth_business_day_with_its_owner():
    monday = Q.OpenFix(7, CP, "frozen", "2026-11-02T09:00:00+00:00", "flow-owner-t1", "SENT-3")
    [p] = evaluate(fixes=[monday]).proposals             # Tuesday 10 November: the 6th business day
    assert (p.reason, p.owner, p.target) == ("aa_floor", "flow-owner-t1", CP)
    assert not Q.fix_overdue(monday.opened_at, date(2026, 11, 9))        # Monday: 5 business days, within
    assert not Q.fix_overdue(monday.opened_at, TODAY, holidays=[date(2026, 11, 5)])
    live = Q.OpenFix(8, CP, "masked_live", "2026-10-01T09:00:00+00:00", "flow-owner-t1")
    assert evaluate(fixes=[live]).proposals == ()


def test_a_flow_with_6_of_100_steps_in_agent_fallback_over_30_days_is_quarantined_whole():
    [p] = evaluate(events=steps(FLOW, 6, 100)).proposals
    assert (p.kind, p.target, p.reason) == ("flow", FLOW, "fallback_rate") and "6 of 100" in p.detail
    assert evaluate(events=steps(FLOW, 5, 100)).proposals == ()                       # exactly 5 percent
    assert evaluate(events=steps(FLOW, 6, 100, drill=True)).proposals == ()           # drill rows
    assert evaluate(events=steps(FLOW, 6, 100, day="2026-10-11")).proposals == ()     # outside 30 days


def test_open_safety_quarantines_of_flows_become_records(tmp_path):
    with Store(tmp_path / "c.db") as st:
        S.write_safety_quarantine(st, "flow", FLOW, "gateway_drops", cause="token_expired", detail="3 drops in flow")
        S.write_safety_quarantine(st, "activity", "order_stage_flows", "fire_drill", cause="drill_failed:drill_2")
        safety = S.open_quarantines(st)
    [p] = evaluate(safety=safety).proposals
    assert (p.kind, p.target, p.reason) == ("flow", FLOW, "gateway_drops") and "token_expired" in p.detail


def test_drill_and_safety_canary_flows_are_paged_never_proposed():
    ev = evaluate([stats(CCP, unstable_7d=4)], events=steps(CANARY, 9, 10))
    assert ev.proposals == ()
    assert sorted((p.kind, p.reason) for p in ev.paged) == [("checkpoint", "flake"), ("flow", "fallback_rate")]
    other = evaluate(events=steps("flow.drill.raw", 9, 10), flow_classes={"flow.drill.raw": "canary"})
    assert other.proposals == () and len(other.paged) == 1


def test_one_proposal_per_target_with_the_first_reason_and_every_trigger():
    ev = evaluate([stats(FCP, unstable_7d=3)], history={FCP: (night(1, True), night(2, True), night(3, True))},
                  owners={FLOW: "flow-owner-t1"})
    [p] = ev.proposals
    assert (p.reason, p.triggers, p.owner, p.flow_id) == ("flake", ("flake", "aa_drift"), "flow-owner-t1", FLOW)
    assert "unstable outcomes in 7 days" in p.detail and "consecutive Nightly runs" in p.detail


def test_a_mirrored_checkpoint_gets_no_new_proposal():
    hist = {CP: (night(1, True), night(2, True), night(3, True))}
    assert evaluate([stats(unstable_7d=5, quarantined=True)], history=hist).proposals == ()


# -- the quarantine.yaml patch -------------------------------------------------------------------------------------
CURRENT = f"""# Quarantined checkpoints (10.5): checkpoint, owner, ticket, entered, reason.
schema_version: 1
records:
- checkpoint: {CP}
  owner: flow-owner-t1
  ticket: SENT-1
  entered: 2026-11-01
  reason: flake
"""


def test_render_patch_adds_complete_records_keeps_the_header_and_holds_incomplete_ones():
    done = Q.Proposal("flow", FLOW, "fallback_rate", TODAY, "6 of 100", owner="flow-owner-t1", ticket="SENT-9")
    again = Q.Proposal("checkpoint", CP, "aa_drift", TODAY, "3 nights", owner="o", ticket="SENT-2")
    waiting = Q.Proposal("checkpoint", FCP, "flake", TODAY, "3 unstable", owner="flow-owner-t1")
    patch = Q.render_patch([done, again, waiting], CURRENT, base_commit="abc1234")
    assert patch.content.startswith("# Quarantined checkpoints (10.5)")
    doc = load_yaml_text(patch.content)
    assert schemas.validate("quarantine", doc) == []
    assert [r.get("checkpoint") or r.get("flow") for r in doc["records"]] == [CP, FLOW]
    assert doc["records"][1] == {"flow": FLOW, "owner": "flow-owner-t1", "ticket": "SENT-9", "entered": "2026-11-10",
                                 "reason": "fallback_rate", "detail": "6 of 100"}
    assert patch.skipped == (again,) and patch.pending == ((waiting, ("ticket",)),)
    assert patch.payload["path"] == "quarantine.yaml" and patch.payload["content"] == patch.content
    assert patch.payload["base_commit"] == "abc1234" and patch.payload_sha256.startswith("sha256:")
    assert Q.render_patch([done, again, waiting], CURRENT, base_commit="abc1234").payload_sha256 == patch.payload_sha256
    none = Q.render_patch([again], CURRENT)
    assert none.payload is None and none.payload_sha256 is None and none.added == ()


def test_render_patch_on_an_empty_file_and_a_record_needs_owner_and_ticket():
    p = Q.Proposal("checkpoint", CP, "aa_floor", TODAY, "fix 7", owner="a", ticket="SENT-4")
    doc = load_yaml_text(Q.render_patch([p]).content)
    assert doc == {"schema_version": 1, "records": [p.record()]}
    with pytest.raises(ValueError, match="owner and ticket"):
        Q.Proposal("checkpoint", CP, "flake", TODAY, "x").record()
    with pytest.raises(ValueError, match="checkpoint or a flow"):
        Q.Proposal("rig", "rig-01", "flake", TODAY, "x")


# -- the store side: inputs, mirror, exit evidence, tier-1 share ------------------------------------------------------
SCOPE = (ScopeEntry(CP, WS, "active_trader", 1, "frozen"),
         ScopeEntry(FCP, WS, "active_trader", 1, "frozen", flow_class="standard"),
         ScopeEntry(f"flow/trade.order-entry.stock/{FLOW}/s07", WS, "active_trader", 1, "frozen",
                    flow_class="standard"),
         ScopeEntry(CCP, WS, "active_trader", 1, "frozen", flow_class="canary"))


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-05", login_alias="login-05", owner="ops", snapshot_id="snap-5", allowlist_entry=1,
                   role="nightly")
    s.register_build(BUILD, channel="main", source_commit="aaa1111", pipeline_artifact_url="u",
                     installer_sha256="0" * 64, first_seen_at="2026-10-01T00:00:00Z")
    s.register_checkpoints(SCOPE, config_commit="c")
    yield s
    s.close()


def begin(st: Store, run_id: str, at: str, run_type: str = "nightly", scope=SCOPE) -> None:
    st.begin_run(RunPlan(run_id=run_id, run_type=run_type, config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=scope, shards=(ShardPlan(f"{run_id}/1", "rig-05"),), started_at=at))


def aa_row(run_id: str, outcome: str, reason: str | None) -> CheckpointOutcome:
    kw = {"side": "both", "detail": "regions: _rest"} if outcome == "not_settled" else {}
    return CheckpointOutcome(run_id=run_id, checkpoint_id=CP, outcome=outcome, reason=reason, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", tier=1, **kw)


def test_inputs_reads_open_fixes_nightly_history_and_safety_rows(st):
    for n, (outcome, reason) in enumerate([("identical", None), ("not_settled", "aa_drift"),
                                           ("not_settled", "aa_drift"), ("not_settled", "aa_drift")], 1):
        begin(st, f"nightly-{n}", f"2026-11-0{n + 4}T03:00:00+00:00")
        st.write_checkpoint(aa_row(f"nightly-{n}", outcome, reason), shard_id=f"nightly-{n}/1")
    begin(st, "cal-1", "2026-10-30T22:15:00+00:00", run_type="calibration")
    st.write_calibration([], fixes=[
        {"checkpoint_id": CP, "cause_class": "caret", "outcome": "not_settled", "owner": "flow-owner-t1",
         "opened_at": "2026-11-02T09:00:00Z", "calibration_run_id": "cal-1"},
        {"checkpoint_id": FCP, "cause_class": "hover", "outcome": "not_settled", "owner": "x",
         "opened_at": "2026-11-02T09:00:00Z", "closed_at": "2026-11-03T09:00:00Z", "closed_by": "jdoe"}])
    S.write_safety_quarantine(st, "flow", FLOW, "kill_switch", cause="kill_switch")
    ins = Q.inputs(st, TODAY)
    assert [f.checkpoint_id for f in ins.fixes] == [CP] and ins.fixes[0].data_mode == "frozen"
    assert [n.drifted for n in ins.aa_history[CP]] == [False, True, True, True]
    assert [q.target for q in ins.safety] == [FLOW]
    ev = Q.evaluate(flake.refresh(st, TODAY), ins.step_events, ins.fixes, ins.aa_history, TODAY,
                    safety=ins.safety, owners={CP: "flow-owner-t1", FLOW: "flow-owner-t1"})
    assert sorted((p.target, p.reason, p.triggers) for p in ev.proposals) == [
        (CP, "aa_drift", ("aa_drift", "aa_floor")), (FLOW, "kill_switch", ("kill_switch",))]


def test_mirror_sets_and_releases_flake_stats_and_lists_exits_without_evidence(st):
    doc = load_yaml_text(CURRENT + f"""- flow: {FLOW}
  owner: flow-owner-t2
  ticket: SENT-5
  entered: 2026-11-03
  reason: fallback_rate
- flow: flow.not.registered
  owner: x
  ticket: SENT-6
  entered: 2026-11-03
  reason: fallback_rate
""")
    m = Q.mirror(st, doc, commit="abc1234", at="2026-11-10T03:00:00Z")
    assert m.quarantined == (CP, FCP, f"flow/trade.order-entry.stock/{FLOW}/s07")
    assert m.unknown == ("flow.not.registered",) and m.released == ()
    row = st.query("SELECT * FROM flake_stats WHERE checkpoint_id = ?", (FCP,))[0]
    assert (row["quarantined"], row["quarantine_reason"], row["quarantine_owner"], row["quarantine_ticket_ref"],
            row["quarantine_commit"], row["quarantined_at"]) == (1, "fallback_rate", "flow-owner-t2", "SENT-5",
                                                                 "abc1234", "2026-11-03T00:00:00+00:00")
    assert [s.checkpoint_id for s in flake.collect(st, TODAY) if s.quarantined] == sorted(m.quarantined)
    begin(st, "cal-1", "2026-11-07T22:15:00+00:00", run_type="calibration")
    zero = {"region_id": "_rest", "rendering_key_sha256": "sha256:" + "1" * 64, "calibration_run_id": "cal-1",
            "samples": 15, "diff_pixels_max": 0, "components_max": 0, "threshold_component_area": 4,
            "threshold_changed_px": 4, "threshold_delta_e": 2.0, "threshold_shift_px": 1}
    st.write_calibration([{**zero, "checkpoint_id": CP}, {**zero, "checkpoint_id": FCP, "diff_pixels_max": 3}])
    released = Q.mirror(st, {"schema_version": 1, "records": []}, commit="def5678")
    assert set(released.released) == set(m.quarantined) and released.quarantined == ()
    assert CP not in released.unproven_exits and FCP in released.unproven_exits
    assert Q.exit_evidence(st, CP, "2026-11-01T00:00:00Z") == "cal-1"
    assert Q.exit_evidence(st, CP, "2026-11-08T00:00:00Z") is None             # the run predates the entry
    assert st.query("SELECT count(*) FROM flake_stats WHERE quarantined = 1")[0][0] == 0


def test_tier1_quarantine_share_is_red_above_2_percent(st):
    many = [ScopeEntry(f"census/surface.n{n:03d}/{WS}", WS, "active_trader", 1, "frozen") for n in range(96)]
    st.register_checkpoints(many, config_commit="c")             # 100 tier-1 checkpoints with SCOPE
    st.replace_flake_stats([{"checkpoint_id": e.checkpoint_id, "quarantined": 1, "quarantine_commit": "c",
                             "quarantine_owner": "o", "quarantine_ticket_ref": "SENT-1", "quarantine_reason": "flake",
                             "updated_at": "2026-11-10T00:00:00Z"} for e in many[:2]])
    share = Q.tier1_quarantine_share(st)
    assert (share.quarantined, share.checkpoints, share.red) == (2, 100, False)
    S.write_safety_quarantine(st, "flow", CANARY, "gateway_drops", cause="token_expired")     # exempt class
    assert Q.tier1_quarantine_share(st).quarantined == 2
    S.write_safety_quarantine(st, "flow", FLOW, "gateway_drops", cause="token_expired")       # its 2 checkpoints
    share = Q.tier1_quarantine_share(st)
    assert (share.quarantined, share.red) == (4, True)
