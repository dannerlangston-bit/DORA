"""Cross-run escalation inputs from the central store (6.3, 6.4, 6.6, 6.9, 9.10)."""
from __future__ import annotations

import pytest

from sentinel.login_history import login_history
from sentinel.orchestrator.gates import PREFLIGHT_ITEMS, check_authenticated, check_provisioned
from sentinel.orchestrator.identity import Allowlist, judge_identity
from sentinel.orchestrator.killswitch import Proof, proof_result
from sentinel.orchestrator.machine import LoginHistory
from sentinel.orchestrator.ports import LoginObservation
from sentinel.orchestrator.rig import PreflightReport, SelectorRead
from sentinel.records import RunOutcome
from sentinel.store.store import PostRunProof, RunPlan, ScopeEntry, ShardPlan, Store

CID = "census/monitor.activity.position-statement/ws.tier-a.active-trader"


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    s.register_rig("rig-02", login_alias="login-02", owner="ops", snapshot_id="snap-2", allowlist_entry=2)
    yield s
    s.close()


def run(st: Store, run_id: str, day: int, *, reason: str | None, gate: str | None, last_gate: str,
        verdict: str = "proven_no_sends", rig: str = "rig-01", login: str = "login-01") -> None:
    """A finished one-shard run: every checkpoint env_error (`reason`, `gate`) or identical, its proof, its end."""
    at = f"2026-11-{day:02d}T03:00:00+00:00"
    st.begin_run(RunPlan(run_id=run_id, run_type="nightly", config_commit="c", config_tarball_sha256="t",
                         snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                         scope=(ScopeEntry(CID, "ws.tier-a.active-trader", "active_trader", 1, "frozen"),),
                         shards=(ShardPlan(f"{run_id}/1", rig),), started_at=at))
    if reason is None:
        st.fill_unreached(run_id, "not_run", "scope")
    else:
        st.fill_unreached(run_id, "env_error", reason, gate=gate)
    st.write_proof(PostRunProof(shard_id=f"{run_id}/1", run_id=run_id, login_alias=login, verdict=verdict,
                                send_policy_ref="default", counts={}, cancel_all="not_needed", paper_mode_assertions=1,
                                paper_mode_mismatches=0, gateway_events=0, gateway_drops=0, produced_at=at,
                                reason="monitor_unreadable" if verdict == "unproven" else None))
    status = "complete_with_env_errors" if reason or verdict == "unproven" else "complete"
    st.end_shard(f"{run_id}/1", status, "unproven_proof" if verdict == "unproven" and not reason else None)
    st.mark_shard_imported(f"{run_id}/1")
    st.end_run(RunOutcome(run_id=run_id, run_type="nightly", status=status, last_gate=last_gate,
                          reason="unproven_proof" if verdict == "unproven" and not reason else None))


def test_no_history_is_all_zero(st):
    assert login_history(st, "login-01") == LoginHistory()


def test_two_unreadable_identity_runs_block_the_login(st):
    # 6.4: two consecutive unreadable results on one login block it and open a maintenance task.
    run(st, "r1", 1, reason="identity_unreadable", gate="paper_mode_asserted", last_gate="paper_mode_asserted")
    h = login_history(st, "login-01")
    assert h.identity_unreadable_streak == 1 and h.failed_gates_last_run == frozenset({"paper_mode_asserted"})
    second = judge_identity([SelectorRead(None, landmark_found=False)] * 2, Allowlist("rig-01", ("D-1",), "a" * 64),
                            unreadable_streak=h.identity_unreadable_streak)
    assert second.has("block_login") and second.has("maintenance_task")
    assert login_history(st, "login-02").identity_unreadable_streak == 0       # another login's runs do not count


def test_a_run_that_stopped_earlier_neither_extends_nor_breaks_the_streak(st):
    run(st, "r1", 1, reason="identity_unreadable", gate="paper_mode_asserted", last_gate="paper_mode_asserted")
    run(st, "r2", 2, reason="gate", gate="preflight.R1", last_gate="preflight.R1")
    run(st, "r3", 3, reason="identity_unreadable", gate="paper_mode_asserted", last_gate="paper_mode_asserted")
    h = login_history(st, "login-01")
    assert h.identity_unreadable_streak == 2
    run(st, "r4", 4, reason=None, gate=None, last_gate="reported")              # a readable assertion breaks it
    h = login_history(st, "login-01")
    assert h.identity_unreadable_streak == 0 and h.failed_gates_last_run == frozenset()


def test_attestation_unreadable_last_run(st):
    # 6.3: the same reason on two consecutive runs is P1 and a maintenance task.
    run(st, "r1", 1, reason="attestation_unreadable", gate="settings_attested", last_gate="settings_attested")
    run(st, "r2", 2, reason="vault_unreachable", gate="authenticated", last_gate="authenticated")
    h = login_history(st, "login-01")
    assert h.attestation_unreadable_last_run                                   # r2 never reached the attestation
    assert check_authenticated(LoginObservation(vault_lease=False),
                               repeat="authenticated" in h.failed_gates_last_run).alert.priority == "P1"


def test_two_unproven_proofs_disable_the_rig(st):
    # 6.6: two consecutive UNPROVEN results on one login disable the rig and file the harness_error finding.
    run(st, "r1", 1, reason=None, gate=None, last_gate="reported", verdict="unproven")
    h = login_history(st, "login-01")
    assert h.unproven_streak == 1
    r = proof_result(Proof("UNPROVEN", reason="monitor_unreadable"), unproven_streak=h.unproven_streak)
    assert r.has("disable_rig") and any(f.kind == "harness_error" for f in r.findings)
    run(st, "r2", 2, reason=None, gate=None, last_gate="reported")
    assert login_history(st, "login-01").unproven_streak == 0


def test_a_preflight_gate_repeats_into_p1(st):
    # 9.10: the same gate in two consecutive runs escalates.
    run(st, "r1", 1, reason="gate", gate="preflight.R2", last_gate="preflight.R2")
    h = login_history(st, "login-01")
    report = PreflightReport({i: i != "R2" for i in PREFLIGHT_ITEMS})
    assert check_provisioned(report, repeat_gates=h.failed_gates_last_run).alert.priority == "P1"
