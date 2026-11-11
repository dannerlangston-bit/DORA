"""Persisted safety actions, incidents and the human actions of 6.6, 6.12, 8.7 and 9.6 (`sentinel.store.safety`)."""
from __future__ import annotations

import json

import pytest

from sentinel.canary import CanaryResult
from sentinel.orchestrator import safety_canaries as sc
from sentinel.orchestrator.drill import DrillObservation, Item5, expected_records, score_drill
from sentinel.orchestrator.explorer import explorer_teardown
from sentinel.orchestrator.gateway import Chord, DropLedger, GatewayDrop
from sentinel.orchestrator.identity import judge_identity
from sentinel.orchestrator.killswitch import ORDERS_SENT, UNPROVEN, Proof, kill_switch, proof_result
from sentinel.orchestrator.rig import OrderRow, SelectorRead
from sentinel.orchestrator.simrig import default_context
from sentinel.reenable import main as reenable_main
from sentinel.store import safety as S
from sentinel.store.store import PostRunProof, RunPlan, ShardPlan, Store, StoreRefused

BUILD, OTHER = "main-2026.44.1-abc1234", "main-2026.45.1-def5678"
T0, T1, T2 = "2026-11-10T03:00:00+00:00", "2026-11-10T04:00:00+00:00", "2026-11-11T03:00:00+00:00"
CHORDS = (Chord.parse("Shift+B"),)


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    s.register_rig("rig-05", login_alias="login-05", owner="ops", snapshot_id="snap-5", allowlist_entry=2,
                   role="nightly")
    for b, seen in ((BUILD, "2026-11-01T00:00:00Z"), (OTHER, "2026-11-08T00:00:00Z")):
        s.register_build(b, channel="main", source_commit=b[-7:], pipeline_artifact_url="u", installer_sha256="0" * 64,
                         first_seen_at=seen)
    yield s
    s.close()


def plan(run_id: str, rig: str, at: str = T0) -> RunPlan:
    return RunPlan(run_id=run_id, run_type="nightly", config_commit="c", config_tarball_sha256="t",
                   snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60, scope=(),
                   shards=(ShardPlan(f"{run_id}/1", rig),), started_at=at)


def run(st: Store, run_id: str, rig: str = "rig-05", build: str = BUILD, at: str = T0) -> None:
    """A run on `rig` whose candidate fingerprint names `build`."""
    st.begin_run(plan(run_id, rig, at))
    st.insert("env_fingerprints", {"run_id": run_id, "shard_id": f"{run_id}/1", "side": "candidate", "rig_id": rig,
                                   "build_id": build, "fingerprint_sha256": "f", "fields_json": "{}", "read_at": at})


def drill_rows(*states: str) -> list[CanaryResult]:
    states = states or ("detected",) * 5
    return [CanaryResult(mutant_id=f"drill-{n}", catalog_version="1", checkpoint_id=sc.DRILL_CHECKPOINT,
                         defect_class="fire_drill", money_cell=False, seed=0, expected=["blocked"], state=s,
                         produced_outcome=None if s == "detected" else "harness_error", kind="fire_drill")
            for n, s in enumerate(states, 1)]


def canary_row(name: str, passed: bool = True) -> CanaryResult:
    return sc.score_canary(name, [sc.CanaryCase("case", "x", "x" if passed else "y")]).row


def evidence(st: Store, run_id: str, rows: list[CanaryResult], *, build: str = BUILD, at: str = T2) -> None:
    run(st, run_id, build=build, at=at)
    st.write_canary_results(run_id, rows, created_at=at)


def human(**kw):
    return {"by": "jdoe", "role": "", "reason": "account verified by hand", "ticket": "SENT-12", "build_id": BUILD,
            **kw}


def proof(run_id: str, verdict: str = "unproven", reason: str | None = "monitor_unreadable", at: str = T0,
          login: str = "login-01") -> PostRunProof:
    return PostRunProof(shard_id=f"{run_id}/1", run_id=run_id, login_alias=login, verdict=verdict,
                        send_policy_ref="default", counts={"undeclared": []}, cancel_all="not_needed",
                        paper_mode_assertions=1, paper_mode_mismatches=0, gateway_events=1, gateway_drops=0,
                        produced_at=at, reason=reason if verdict == "unproven" else None)


def ledger_rows(st: Store) -> list[dict]:
    return [r for line in (st.path.parent / "results.jsonl").read_text().splitlines()
            for r in json.loads(line)["rows"] if r.get("op") == "human_action"]


# -- logins (6.4, 6.5, 6.6, 8.3) ---------------------------------------------------------------------------------
def test_a_blocked_login_stops_shards_until_a_person_reenables_it_with_a_passing_drill(st):
    assert S.block_login(st, "login-01", "drill_order_placed", at=T1)
    assert not S.block_login(st, "login-01", "incident")                # the first block stands
    with pytest.raises(StoreRefused, match="blocked login"):
        st.begin_run(plan("pr-1", "rig-01"))
    assert S.refuse_blocked(st, "rig-01").why == "login_blocked"
    with pytest.raises(S.SafetyRefused, match="no fire drill result on build"):
        S.unblock_login(st, "login-01", **human())
    evidence(st, "nightly-2", drill_rows())
    r = S.unblock_login(st, "login-01", **human())
    assert (r.kind, r.target, r.evidence_run_id, r.ticket) == ("login", "login-01", "nightly-2", "SENT-12")
    assert S.refuse_blocked(st, "rig-01") is None
    st.begin_run(plan("pr-1", "rig-01"))                               # trg_shard_rig_gate lets it start
    row = st.query("SELECT unblocked_by, unblocked_at FROM logins WHERE login_alias = 'login-01'")[0]
    assert row["unblocked_by"] == "jdoe" and row["unblocked_at"]
    entry = ledger_rows(st)[-1]
    assert (entry["action"], entry["reason"], entry["ticket"], entry["evidence_run_id"]) == \
        ("reenable", "account verified by hand", "SENT-12", "nightly-2")


def test_the_reenable_needs_the_latest_drill_on_the_current_build_after_the_block(st):
    S.block_login(st, "login-01", "cancel_all_failed", at=T1)
    evidence(st, "nightly-0", drill_rows(), at=T0)                      # passed before the block
    evidence(st, "nightly-1", drill_rows(), build=OTHER, at=T2)        # passed on another build
    with pytest.raises(S.SafetyRefused, match="no fire drill result"):
        S.unblock_login(st, "login-01", **human())
    evidence(st, "nightly-2", drill_rows("detected", "detected", "detected", "detected", "missed"))
    with pytest.raises(S.SafetyRefused, match="drill-5 missed"):
        S.unblock_login(st, "login-01", **human())
    evidence(st, "nightly-3", drill_rows(), at="2026-11-12T03:00:00+00:00")
    assert S.unblock_login(st, "login-01", **human()).evidence_run_id == "nightly-3"
    with pytest.raises(S.SafetyRefused, match="not blocked"):
        S.unblock_login(st, "login-01", **human())


@pytest.mark.parametrize("bad", [{"by": "system"}, {"by": "Reviewer"}, {"by": ""}, {"reason": " "}, {"ticket": ""}])
def test_a_reenable_is_human_with_reason_and_ticket(st, bad):
    S.block_login(st, "login-01", "rotation", at=T1)
    with pytest.raises(S.SafetyRefused):
        S.unblock_login(st, "login-01", **human(**bad))
    assert S.unblock_login(st, "login-01", **human()).evidence_run_id is None       # rotation needs no canary row


def test_block_login_validates_reason_and_login(st):
    with pytest.raises(S.SafetyRefused):
        S.block_login(st, "login-01", "because")
    with pytest.raises(S.SafetyRefused):
        S.block_login(st, "nobody", "incident")


def test_identity_blocks_need_the_identity_canary(st):
    S.block_login(st, "login-01", "live_identity", at=T1)
    evidence(st, "nightly-2", drill_rows())
    with pytest.raises(S.SafetyRefused, match="identity canary"):
        S.unblock_login(st, "login-01", **human())
    st.write_canary_results("nightly-2", [canary_row("identity")], created_at=T2)
    assert S.unblock_login(st, "login-01", **human()).evidence_run_id == "nightly-2"


def test_unproven_blocks_need_every_proof_cleared_and_the_system_owner_after_two(st):
    run(st, "pr-1", rig="rig-01", at=T0)
    run(st, "pr-2", rig="rig-01", at=T0)
    st.write_proof(proof("pr-1", at=T0))
    st.write_proof(proof("pr-2", at=T1))
    S.block_login(st, "login-01", "unproven_proof", at=T1)
    evidence(st, "nightly-3", [canary_row("proof")])
    with pytest.raises(S.SafetyRefused, match="clear_proof first"):
        S.unblock_login(st, "login-01", **human())
    S.clear_proof(st, by="jdoe", shard_id="pr-1/1")
    S.clear_proof(st, by="asmith", shard_id="pr-2/1")
    with pytest.raises(S.SafetyRefused, match="system owner signs"):
        S.unblock_login(st, "login-01", **human())
    assert S.unblock_login(st, "login-01", **human(role="system_owner")).evidence_run_id == "nightly-3"


# -- the proof clearance (6.6, 8.7) --------------------------------------------------------------------------------
def test_clear_proof_is_a_person_once_on_an_unproven_proof(st):
    run(st, "pr-1", rig="rig-01")
    st.write_proof(proof("pr-1"))
    with pytest.raises(S.SafetyRefused, match="not a person"):
        S.clear_proof(st, by="system", shard_id="pr-1/1")
    pid = S.clear_proof(st, by="jdoe", shard_id="pr-1/1", at=T2)
    row = st.query("SELECT verified_by, verified_at FROM post_run_proofs WHERE proof_id = ?", (pid,))[0]
    assert (row["verified_by"], row["verified_at"]) == ("jdoe", T2)
    with pytest.raises(S.SafetyRefused, match="set once"):
        S.clear_proof(st, by="asmith", shard_id="pr-1/1")
    run(st, "pr-2", rig="rig-01")
    st.write_proof(proof("pr-2", verdict="proven_no_sends"))
    with pytest.raises(S.SafetyRefused, match="only an UNPROVEN proof"):
        S.clear_proof(st, by="jdoe", shard_id="pr-2/1")
    with pytest.raises(S.SafetyRefused):
        S.clear_proof(st, by="jdoe", shard_id="pr-9/1")
    with pytest.raises(S.SafetyRefused):
        S.clear_proof(st, by="jdoe", shard_id="pr-1/1", run_id="pr-1")


def test_clear_proof_of_the_run_level_row(st):
    st.begin_run(RunPlan(run_id="pr-3", run_type="pr", config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=()))
    st.write_proof(PostRunProof(shard_id=None, run_id="pr-3", login_alias=None, verdict="unproven",
                                send_policy_ref="default", counts={}, cancel_all="not_needed", paper_mode_assertions=0,
                                paper_mode_mismatches=0, gateway_events=0, gateway_drops=0, produced_at=T0,
                                reason="snapshot_missing"))
    assert S.clear_proof(st, by="jdoe", run_id="pr-3")
    assert ledger_rows(st)[-1]["action"] == "clear_proof"


# -- rigs (6.6 step 6) ---------------------------------------------------------------------------------------------
def test_a_disabled_rig_returns_after_its_incident_closes_and_the_drill_passes(st):
    run(st, "pr-1", rig="rig-01")
    assert S.disable_rig(st, "rig-01", "kill_switch", at=T1) and not S.disable_rig(st, "rig-01", "kill_switch")
    assert S.refuse_blocked(st, "rig-01").why == "rig_disabled"
    iid = S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias="login-01", severity="S2",
                          trigger="kill_switch: file_flag", at=T1)
    evidence(st, "nightly-2", drill_rows())
    with pytest.raises(S.SafetyRefused, match=f"until incident {iid} is closed"):
        S.enable_rig(st, "rig-01", **human())
    S.close_incident(st, iid, by="jdoe", reason="postmortem SENT-40")
    r = S.enable_rig(st, "rig-01", **human())
    assert r.evidence_run_id == "nightly-2" and S.refuse_blocked(st, "rig-01") is None
    assert st.query("SELECT active, enabled_by FROM rigs WHERE rig_id = 'rig-01'")[0]["active"] == 1
    with pytest.raises(S.SafetyRefused, match="not disabled"):
        S.enable_rig(st, "rig-01", **human())


def test_a_rig_disabled_after_two_unproven_proofs_needs_the_system_owner_and_the_proof_canary(st):
    S.disable_rig(st, "rig-01", "unproven_twice", at=T1)
    with pytest.raises(S.SafetyRefused, match="system owner"):
        S.enable_rig(st, "rig-01", **human())
    with pytest.raises(S.SafetyRefused, match="proof canary"):
        S.enable_rig(st, "rig-01", **human(role="system_owner"))
    evidence(st, "nightly-2", [canary_row("proof")])
    assert S.enable_rig(st, "rig-01", **human(role="system_owner")).kind == "rig"
    with pytest.raises(S.SafetyRefused):
        S.disable_rig(st, "rig-01", "tired")
    with pytest.raises(S.SafetyRefused):
        S.disable_rig(st, "rig-99", "kill_switch")


def test_refuse_blocked_names_unknown_and_inactive_rigs(st):
    assert S.refuse_blocked(st, "rig-99").why == "unknown_rig"
    st.register_rig("rig-09", login_alias="login-09", owner="ops", snapshot_id="snap-9", allowlist_entry=3)
    st.reassign_login("rig-01", "rig-09", operator="jdoe", snapshot_id="snap-9b")
    assert S.refuse_blocked(st, "rig-01").why == "rig_inactive"


# -- incidents (6.12) ----------------------------------------------------------------------------------------------
def test_one_incident_per_run_and_rig_rising_from_s2_to_s1(st):
    run(st, "pr-1", rig="rig-01")
    a = S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias="login-01", severity="S2",
                        trigger="kill_switch: attempted_send", at="2026-11-13T10:00:00+00:00")     # a Friday
    b = S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias="login-01", severity="S1",
                        trigger="proof: undeclared_order_row")
    assert a == b
    inc = S.incident(st, a)
    assert inc.severity == "S1" and inc.trigger == "kill_switch: attempted_send; proof: undeclared_order_row"
    assert inc.postmortem_due_at == "2026-11-20T10:00:00+00:00"          # five business days
    assert S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias="login-01", severity="S2",
                           trigger="proof: undeclared_order_row") == a
    s3 = S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias=None, severity="S3", trigger="drops: flow.x")
    assert s3 != a and S.incident(st, s3).postmortem_due_at is None
    assert S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias=None, severity="S3",
                           trigger="drops: flow.x") == s3
    assert [i.incident_id for i in S.incidents(st, run_id="pr-1")] == [a, s3]
    with pytest.raises(S.SafetyRefused):
        S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias=None, severity="S4", trigger="x")
    with pytest.raises(StoreRefused, match="open incident"):
        st.begin_run(plan("pr-2", "rig-01"))


def test_close_incident_is_a_person_with_a_reason_and_postmortems_are_flagged_when_late(st):
    run(st, "pr-1", rig="rig-01")
    iid = S.open_incident(st, run_id="pr-1", rig_id="rig-01", login_alias="login-01", severity="S2",
                          trigger="paper_mode: paper_mode_mismatch", at=T0)
    with pytest.raises(S.SafetyRefused, match="not a person"):
        S.close_incident(st, iid, by="orchestrator", reason="auto")
    with pytest.raises(S.SafetyRefused, match="close reason"):
        S.close_incident(st, iid, by="jdoe", reason="")
    assert [i.incident_id for i in S.overdue_postmortems(st, at="2026-11-30T00:00:00+00:00")] == [iid]
    assert S.overdue_postmortems(st, at=T1) == ()
    closed = S.close_incident(st, iid, by="jdoe", reason="selector misread by a popup", ticket_ref="SENT-41", at=T2)
    assert (closed.closed_by, closed.closed_at, closed.ticket_ref) == ("jdoe", T2, "SENT-41")
    assert S.incidents(st, open_only=True) == ()
    with pytest.raises(S.SafetyRefused, match="was closed"):
        S.close_incident(st, iid, by="jdoe", reason="again")
    S.record_postmortem(st, iid, "docs/postmortems/2026-11-10.md")
    S.record_postmortem(st, iid, "docs/postmortems/2026-11-10.md")
    with pytest.raises(S.SafetyRefused):
        S.record_postmortem(st, iid, "elsewhere.md")
    with pytest.raises(S.SafetyRefused):
        S.record_postmortem(st, 99, "x.md")
    with pytest.raises(S.SafetyRefused):
        S.close_incident(st, 99, by="jdoe", reason="x")
    assert S.overdue_postmortems(st, at="2026-11-30T00:00:00+00:00") == () and S.incident(st, 99) is None


# -- override check (9.6) ------------------------------------------------------------------------------------------
def test_override_check_is_the_system_owners_only_and_set_once(st):
    run(st, "pr-1", rig="rig-01")
    with pytest.raises(S.SafetyRefused, match="system owner"):
        S.override_check(st, "pr-1", by="jdoe", role="triage_rotation", reason="flaky host")
    with pytest.raises(S.SafetyRefused):
        S.override_check(st, "pr-1", by="system", role="system_owner", reason="x")
    S.override_check(st, "pr-1", by="jdoe", role="system_owner", reason="release blocker waived")
    S.override_check(st, "pr-1", by="jdoe", role="system_owner", reason="release blocker waived")
    row = st.query("SELECT check_override_by, check_override_reason FROM runs WHERE run_id = 'pr-1'")[0]
    assert tuple(row) == ("jdoe", "release blocker waived")
    with pytest.raises(S.SafetyRefused, match="already overridden"):
        S.override_check(st, "pr-1", by="asmith", role="system_owner", reason="other")
    with pytest.raises(S.SafetyRefused):
        S.override_check(st, "pr-9", by="jdoe", role="system_owner", reason="x")


# -- quarantines (6.5, 10.5) ---------------------------------------------------------------------------------------
def test_quarantine_rows_validate_dedupe_and_filter_flows(st):
    rig_q = S.write_safety_quarantine(st, "rig", "rig-01", "safety_canary", cause="safety_canary_blind:identity",
                                      detail="identity check blind")
    assert S.write_safety_quarantine(st, "rig", "rig-01", "safety_canary", cause="safety_canary_blind:identity") == rig_q
    stage = S.write_safety_quarantine(st, "activity", "order_stage_flows", "fire_drill", cause="drill_failed:item1")
    flow = S.write_safety_quarantine(st, "flow", "flow.trade.x", "kill_switch", cause="attempted_send")
    for bad in (dict(target_kind="window", target="x", reason="kill_switch", cause="c"),
                dict(target_kind="activity", target="lunch", reason="kill_switch", cause="c"),
                dict(target_kind="flow", target="f", reason="flake", cause="c"),
                dict(target_kind="rig", target="rig-01", reason="safety_canary", cause="identity"),
                dict(target_kind="rig", target="rig-01", reason="fire_drill", cause="item5"),
                dict(target_kind="flow", target="f", reason="kill_switch", cause="")):
        with pytest.raises(S.SafetyRefused):
            S.write_safety_quarantine(st, **bad)
    qs = S.open_quarantines(st)
    assert [q.quarantine_id for q in qs] == [rig_q, stage, flow]
    assert qs[0].cause == "safety_canary_blind:identity" and qs[0].detail == "identity check blind"
    f = dict(flow_class="scripted", order_stage=False)
    assert S.quarantined_flow(qs, flow_id="flow.a", rig_id="rig-01", **f).quarantine_id == rig_q
    assert S.quarantined_flow(qs, flow_id="flow.a", rig_id="rig-05", **f) is None
    assert S.quarantined_flow(qs, flow_id="flow.trade.x", rig_id="rig-05", **f).quarantine_id == flow
    assert S.quarantined_flow(qs, flow_id="flow.b", rig_id="rig-05", flow_class="scripted",
                              order_stage=True).quarantine_id == stage
    assert S.quarantined_flow(qs, flow_id="flow.fire-drill", rig_id="rig-01", flow_class="canary",
                              order_stage=True) is None                # the drill and canary flows are exempt
    assert S.activity_quarantined(qs, "explorer_stage") is None
    with pytest.raises(ValueError):
        S.activity_quarantined(qs, "lunch")


def test_a_quarantine_is_reenabled_with_the_canary_that_opened_it(st):
    qid = S.write_safety_quarantine(st, "activity", "explorer_stage", "safety_canary", cause="safety_canary_blind:deny_list",
                                    at=T1)
    drill = S.write_safety_quarantine(st, "activity", "order_stage_flows", "fire_drill", cause="drill_failed:item2",
                                      at=T1)
    evidence(st, "nightly-2", [canary_row("identity")])
    with pytest.raises(S.SafetyRefused, match="deny_list canary"):
        S.reenable(st, "quarantine", str(qid), **human())
    st.write_canary_results("nightly-2", [canary_row("deny_list"), *drill_rows()], created_at=T2)
    r = S.reenable(st, "quarantine", str(qid), **human())
    assert (r.kind, r.target, r.evidence_run_id) == ("quarantine", str(qid), "nightly-2")
    assert S.reenable(st, "quarantine", str(drill), **human()).evidence_run_id == "nightly-2"
    assert S.open_quarantines(st) == ()
    row = st.query("SELECT reenabled_by, reenable_reason, ticket_ref FROM safety_quarantines WHERE quarantine_id = ?",
                   (qid,))[0]
    assert tuple(row) == ("jdoe", "account verified by hand", "SENT-12")
    with pytest.raises(S.SafetyRefused, match="was re-enabled"):
        S.reenable(st, "quarantine", str(qid), **human())
    with pytest.raises(S.SafetyRefused):
        S.reenable(st, "quarantine", "x", **human())
    with pytest.raises(S.SafetyRefused):
        S.reenable(st, "quarantine", "99", **human())
    with pytest.raises(S.SafetyRefused):
        S.reenable(st, "window", "x", **human())
    flake = st.insert("safety_quarantines", {"target_kind": "flow", "target": "flow.a", "reason": "flake",
                                             "detail": "flake", "opened_at": T0})
    with pytest.raises(S.SafetyRefused, match="10.5"):
        S.reenable(st, "quarantine", str(flake), **human())
    with pytest.raises(S.SafetyRefused):
        S.canary_requirement("lunch")


# -- one control result, persisted ---------------------------------------------------------------------------------
GOOD5 = Item5(0.1, 1.0, True, True, "PROVEN: no orders sent", True)


def drill_obs(**over) -> DrillObservation:
    return DrillObservation(**{**dict(attestation_passed=True, probe_dropped_age_s=1.0, injector_signed=True,
                                      ticket_on_empty=True, records=(), item5=GOOD5), **over})


def test_a_drill_that_placed_an_order_blocks_the_login_and_opens_an_s1(st):
    run(st, "nightly-1")
    res = score_drill(drill_obs(new_order_rows=1), CHORDS).result
    a = S.apply_safety_actions(st, res, run_id="nightly-1", rig_id="rig-05", login_alias="login-05", at=T1)
    assert a.login_blocked and not a.rig_disabled and a.quarantine_ids == ()
    assert S.incident(st, a.incident_id).severity == "S1"
    assert st.query("SELECT blocked_reason FROM logins WHERE login_alias = 'login-05'")[0][0] == "drill_order_placed"
    with pytest.raises(StoreRefused):
        st.begin_run(plan("nightly-2", "rig-05"))


def test_a_failed_item5_quarantines_order_stage_flows_everywhere_and_every_flow_on_the_rig(st):
    run(st, "nightly-1")
    res = score_drill(drill_obs(item5=Item5(0.1, 1.0, True, False, "PROVEN: no orders sent", True),
                                records=tuple(expected_records(CHORDS).elements())), CHORDS).result
    a = S.apply_safety_actions(st, res, run_id="nightly-1", rig_id="rig-05", login_alias="login-05")
    qs = S.open_quarantines(st)
    assert [(q.target_kind, q.target, q.reason, q.cause) for q in qs] == [
        ("activity", "order_stage_flows", "fire_drill", "drill_failed:item5"), ("rig", "rig-05", "fire_drill",
                                                                               "drill_failed:item5")]
    assert a.quarantine_ids == tuple(q.quarantine_id for q in qs) and S.incident(st, a.incident_id).severity == "S2"


def test_safety_canary_misses_quarantine_what_they_protect(st):
    run(st, "nightly-1")
    for name in ("identity", "deny_list", "secret_scan"):
        res = sc.score_canary(name, [sc.CanaryCase("case", "x", "y")]).result
        S.apply_safety_actions(st, res, run_id="nightly-1", rig_id="rig-05", login_alias="login-05")
    qs = S.open_quarantines(st)
    assert [(q.target_kind, q.target, q.cause) for q in qs] == [
        ("rig", "rig-05", "safety_canary_blind:identity"),
        ("activity", "explorer_stage", "safety_canary_blind:deny_list"),
        ("activity", "report_publication", "safety_canary_blind:secret_scan")]
    assert S.activity_quarantined(qs, "explorer_stage") and S.activity_quarantined(qs, "report_publication")
    assert len(S.incidents(st, run_id="nightly-1")) == 1                 # one S2 for the run on the rig


def test_kill_switch_and_gateway_results_quarantine_the_running_flow_and_disable_the_rig(st):
    run(st, "pr-1", rig="rig-01")
    ks = kill_switch("file_flag", gate="running", running_flow="flow.trade.x")
    a = S.apply_safety_actions(st, ks, run_id="pr-1", rig_id="rig-01", login_alias="login-01", flow_id="flow.trade.x")
    assert a.rig_disabled and not a.login_blocked and a.incident_id is None
    assert st.query("SELECT disabled_reason FROM rigs WHERE rig_id = 'rig-01'")[0][0] == "kill_switch"
    gw = DropLedger().record(GatewayDrop("deny_rect", "dropped", "safety"), unit="flow.trade.y")
    b = S.apply_safety_actions(st, gw, run_id="pr-1", rig_id="rig-01", login_alias="login-01", flow_id="flow.trade.y")
    assert [(q.target, q.reason, q.cause) for q in S.open_quarantines(st)] == [
        ("flow.trade.x", "kill_switch", "kill_switch"), ("flow.trade.y", "gateway_drops", "attempted_send")]
    assert S.incident(st, b.incident_id).severity == "S2"
    with pytest.raises(ValueError):
        S.apply_safety_actions(st, gw, run_id="pr-1", rig_id="rig-01", login_alias="login-01")


def test_block_reasons_follow_the_control_that_asked(st):
    ctx = default_context()
    live = judge_identity([SelectorRead("12345678SCHW")], ctx.allowlist)
    blind = judge_identity([SelectorRead(None, landmark_found=False)] * 2, ctx.allowlist, unreadable_streak=1)
    unproven = proof_result(Proof(UNPROVEN, "recovery_login_failed"))
    monitor = proof_result(Proof(UNPROVEN, "monitor_unreadable"), unproven_streak=1)
    sent = proof_result(Proof(ORDERS_SENT, undeclared=(OrderRow("o-1", "working"),)))
    teardown = explorer_teardown(["alert-1"], {})
    assert [S.blocked_reason_for(r) for r in (live, blind, unproven, monitor, sent, teardown)] == [
        "live_identity", "identity_unreadable_twice", "recovery_login_failed", "unproven_proof", "incident",
        "explorer_teardown_incomplete"]
    assert [S.disable_reason_for(r) for r in (monitor, sent, kill_switch("ten_drops", gate="running"))] == [
        "unproven_twice", "undeclared_order_row", "kill_switch"]
    run(st, "pr-1", rig="rig-01")
    a = S.apply_safety_actions(st, sent, run_id="pr-1", rig_id="rig-01", login_alias="login-01")
    assert a.login_blocked and a.rig_disabled and S.incident(st, a.incident_id).severity == "S1"
    with pytest.raises(ValueError):
        S.apply_safety_actions(st, live, run_id="pr-1", rig_id="rig-01", login_alias=None)


# -- the CLI (`sentinel reenable`) ---------------------------------------------------------------------------------
def test_reenable_cli_writes_refuses_and_names_a_missing_database(st, tmp_path, capsys):
    S.block_login(st, "login-01", "live_identity", at=T1)
    run(st, "pr-1", rig="rig-05")
    st.write_proof(proof("pr-1", login="login-05"))
    evidence(st, "nightly-2", drill_rows())
    iid = S.open_incident(st, run_id="pr-1", rig_id="rig-05", login_alias="login-05", severity="S2", trigger="x")
    db = str(st.path)
    st.close()
    args = ["--db", db, "login", "login-01", "--by", "jdoe", "--reason", "verified", "--ticket", "SENT-1",
            "--build", BUILD]
    assert reenable_main(args) == 1 and "no identity canary result" in capsys.readouterr().err
    with Store(db) as s:
        s.write_canary_results("nightly-2", [canary_row("identity")], created_at=T2)
    assert reenable_main(args) == 0 and "re-enabled login login-01 by jdoe" in capsys.readouterr().out
    assert reenable_main(["--db", db, "proof", "--shard", "pr-1/1", "--by", "jdoe"]) == 0
    assert reenable_main(["--db", db, "incident", str(iid), "--by", "jdoe", "--reason", "done"]) == 0
    assert "closed by jdoe" in capsys.readouterr().out
    assert reenable_main(["--db", str(tmp_path / "missing.db"), "rig", "rig-01", "--by", "jdoe", "--reason", "r",
                          "--ticket", "t", "--build", BUILD]) == 2
    with pytest.raises(SystemExit):
        reenable_main(["--db", db, "proof", "--by", "jdoe"])          # names neither shard nor run
