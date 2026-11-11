"""The Safety proof section (9.4 item 8, 6.6) and the run manifest's safety facts (6.3 Outcome, 6.4 Schedule)."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from sentinel.canary import CanaryResult
from sentinel.orchestrator.attestation import AttestationProfile, RowReads, attest
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.identity import judge_identity
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import ConfirmationCheck, SelectorRead, SettingRead
from sentinel.orchestrator.simrig import (EMPTY, GOLDEN, MemorySink, SimScenario, SimulatedRig, default_context,
                                         default_settings)
from sentinel.report.model import SafetyRows
from sentinel.report.safety import (ITEMS, RESIDUALS, AssertedAccount, ProofHeader, SafetyInputError, ShardSafety,
                                    proof_block, render, run_manifest_safety)
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.safety import Incident, SafetyQuarantine
from sentinel.store.store import PostRunProof

SPEC_06 = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "06-safety.md"
RUN, SHARD = "rc-2026-11-09", "rc-2026-11-09/1"
CP = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
SCOPE = ["census/trade.order-entry.stock/ws.tier-a.active-trader", CP]
FIXTURE = {"accounts": [{"account_id": GOLDEN, "kind": "margin", "role": "golden"},
                        {"account_id": EMPTY, "kind": "ira", "role": "empty"}]}
PROFILE = "3f9a" + "0" * 60


def sample_block() -> str:
    """The ORDER SAFETY PROOF sample of 6.6, as the spec prints it."""
    text = SPEC_06.read_text(encoding="utf-8").split("so the person who authorized the system knows", 1)[1]
    return text.split("```\n", 1)[1].split("\n```", 1)[0]


def counts(*, working_end: int = 0, net_liq_end: float = 25000.0) -> dict:
    rows = {"working orders": {"start": 0, "end": working_end, "new": working_end, "expected": 0,
                               "verdict": "UNDECLARED" if working_end else "ok"},
            "filled orders": {"start": 0, "end": 0, "new": 0, "expected": 0, "verdict": "ok"},
            "canceled orders": {"start": 0, "end": 0, "new": 0, "expected": 0, "verdict": "ok"},
            "position statement rows": {"start": 3, "end": 3, "new": 0, "expected": 0, "verdict": "ok"},
            "net liq": {"start": 25000.0, "end": net_liq_end, "drift": round(abs(net_liq_end - 25000.0), 2),
                        "verdict": "ok" if abs(net_liq_end - 25000.0) <= 0.5 else "drift"}}
    return {"rows": rows, "undeclared": ["w-77"] if working_end else [], "declared": [], "sends": []}


def proof(**kw) -> PostRunProof:
    base = dict(shard_id=SHARD, run_id=RUN, login_alias="paper-03", verdict="proven_no_sends", send_policy_ref="OPEN",
                counts=counts(), cancel_all="not_needed", paper_mode_assertions=41, paper_mode_mismatches=0,
                gateway_events=1912, gateway_drops=0, produced_at="2026-11-09T15:40:00Z")
    base.update(kw)
    return PostRunProof(**base)


def shard_safety(**kw) -> ShardSafety:
    base = dict(shard_id=SHARD, run_type="rc", attestation="passed", rows={i: "match" for i in ITEMS},
                evidence={f"A{n}": f"{n:02d}" * 32 for n in range(1, 11)}, profile_sha256=PROFILE,
                attested_at="2026-11-09T09:03:10-05:00",
                accounts=(AssertedAccount(2, "D-xxxxxxxx", "empty", "sha256:" + "ab" * 32),), mode="paperMoney",
                assertions=41, rig_id="tos-rig-03", cancel_all_located=True)
    base.update(kw)
    return ShardSafety(**base)


HEADER = ProofHeader(run_started="2026-11-09T14:02:31Z", build="2026.44.1")


# -- the 6.6 block ---------------------------------------------------------------------------------------------------

def test_a_stored_proof_row_prints_the_6_6_sample_block_exactly(tmp_path):
    """Golden test: the 8.3 row, written and read back through the store, with its shard's manifest facts."""
    with Store(tmp_path / "run.db") as s:
        s.register_rig("tos-rig-03", login_alias="paper-03", owner="qa", snapshot_id="snap-1", allowlist_entry=2)
        s.begin_run(RunPlan(run_id=RUN, run_type="rc", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=10800, scope=(ScopeEntry(CP, "ws.tier-a.swing-trader", "swing_trader", 1,
                                                                "frozen", flow_class="standard"),),
                            shards=(ShardPlan(SHARD, "tos-rig-03"),)))
        s.write_proof(proof())
        (stored,) = s.post_run_proofs(RUN)
    block = proof_block(stored, shard_safety(), HEADER, send_policy="no_sends", order_surfaces_not_run=6)
    assert block.text == sample_block()
    assert block.title == f"ORDER SAFETY PROOF, shard {SHARD}" and block.severity == 0
    assert "paper-03" not in block.text                           # the login alias is never printed (8.3)


def test_orders_sent_unproven_and_a_drifting_net_liq_print_their_verdicts():
    sent = proof_block(proof(verdict="orders_sent", cancel_all="performed_verified", counts=counts(working_end=1)),
                       shard_safety(), HEADER)
    assert sent.severity == 2
    assert "VERDICT        ORDERS SENT: undeclared rows (w-77); Cancel All verified" in sent.text
    assert "working orders            0           1           1    0         UNDECLARED" in sent.text
    drift = proof_block(proof(verdict="unproven", reason="monitor_unreadable", counts=counts(net_liq_end=25003.2)),
                        shard_safety(cancel_all_located=None), HEADER)
    assert drift.severity == 1 and "VERDICT        UNPROVEN: monitor_unreadable" in drift.text
    assert "net liq                   25,000.00   25,003.20   drift 3.20 over 0.50" in drift.text
    assert drift.text.splitlines()[-2] == "cancel all                not needed"     # located not recorded (choice 4)


def test_a_failed_attestation_names_its_rows_and_missing_evidence():
    rows = {i: "match" for i in ITEMS} | {"A3": "mismatch", "A10": "not_read", "A11": "not_read"}
    s = shard_safety(run_type="merge", attestation="failed", rows=rows, evidence={"A1": "aa" * 32})
    text = proof_block(proof(), s, HEADER).text
    assert ("attestation    failed: A3 mismatch at 14:03:10Z, profile sha256 3f9a..., evidence hashes missing for "
            "A2, A3, A4, A5, A6, A7, A8, A9") in text
    passed = proof_block(proof(), shard_safety(run_type="merge"), HEADER).text
    assert "attestation    A1 to A9 and A11 passed 14:03:10Z" in passed       # A10 is for RC runs (6.3)


def test_a_run_level_row_and_a_gated_branch_send_policy():
    row = proof(shard_id=None, login_alias=None, verdict="proven_no_session", paper_mode_assertions=0,
                gateway_events=0, counts={"rows": {}, "undeclared": [], "declared": [], "sends": []})
    block = proof_block(row, None, ProofHeader())
    assert block.title == "ORDER SAFETY PROOF, run level (no shard started)"
    assert block.text.splitlines()[:2] == ["ORDER SAFETY PROOF        run level (no shard started)",
                                           "identity       no session established,"]
    assert "VERDICT        PROVEN: no session established" in block.text
    gated = proof_block(proof(send_policy_ref="risk-dec-0042"), shard_safety(), HEADER, send_policy="gated").text
    assert "send policy    gated (decision risk-dec-0042)" in gated


# -- the run manifest's safety facts --------------------------------------------------------------------------------

def run_shard(run_type: str = "pr", scenario: SimScenario | None = None):
    clock = ManualClock()
    rig = SimulatedRig(scenario, clock=clock)
    orch = Orchestrator(rig, default_context(run_type=run_type), MemorySink(SCOPE), clock=clock)
    return orch.run(lambda o: [o.sink.record(c, "identical") for c in SCOPE])


def test_after_a_clean_rc_run_the_manifest_has_11_states_the_profile_hash_and_10_evidence_hashes():
    report = run_shard("rc")
    s = run_manifest_safety(report, shard_id="shard-1", rig_id="rig-01", allowlist=default_context().allowlist,
                            fixture=FIXTURE, attested_at="2026-11-09T14:03:10Z")
    d = s.to_dict()
    assert list(d["attestation"]["rows"]) == list(ITEMS) and set(d["attestation"]["rows"].values()) == {"match"}
    assert d["attestation"]["result"] == "passed" and d["attestation"]["profile_sha256"] == \
        default_context().profile.sha256
    hashes = d["attestation"]["evidence_sha256"]
    assert sorted(k for k, v in hashes.items() if v) == sorted(f"A{n}" for n in range(1, 11))
    assert s.missing_evidence == ()
    assert d["identity"]["mode"] == "paperMoney" and d["identity"]["assertions"] >= 1
    assert d["identity"]["accounts"][0]["account"] == "D-xxxxxxxx"            # masked, never the id (6.10)
    assert d["identity"]["accounts"][0]["account_id_sha256"].startswith("sha256:")
    assert EMPTY not in repr(d) and GOLDEN not in repr(d)
    assert ShardSafety.from_dict(d) == s                                      # the manifest form reads back


def test_a_pr_run_does_not_read_a10_and_a_run_that_never_attested_is_not_reached():
    s = run_manifest_safety(run_shard("pr"), shard_id="shard-1")
    assert s.rows["A10"] == "not_read" and s.rows["A11"] == "match" and s.missing_evidence == ()
    assert s.accounts[0].account == "" and s.accounts[0].label().startswith("(allowlist entry ")
    early = run_manifest_safety(run_shard("pr", SimScenario(preflight_failures={"H4": None})), shard_id="shard-1")
    assert early.attestation == "not_reached" and set(early.rows.values()) == {"not_read"}
    assert early.mode is None and early.profile_sha256 is None


def test_a_mismatched_setting_is_recorded_on_the_manifest():
    on = {**default_settings(), "A5": True}                     # Active Trader hot keys enabled
    s = run_manifest_safety(run_shard("pr", SimScenario(settings=on)), shard_id="shard-1")
    assert s.attestation == "failed" and s.rows["A5"] == "mismatch" and s.rows["A9"] == "not_read"
    assert s.rows["A1"] == "match" and s.rows["A2"] == "match"
    assert s.evidence.get("A5") and s.missing_evidence


def _attested(**changes):
    """One shard's results: a passing paper-mode assertion and `attest` over clean reads with `changes` applied."""
    reads = {i: RowReads(SettingRead(i, v, screenshot_sha256=f"{i}-shot")) for i, v in default_settings().items()}
    reads.update(changes.pop("reads", {}))
    evidence = {"A1": "a1-crop", "A2": "a2-crop", **{i: f"{i}-shot" for i in reads}, "A9": "a9-shot"}
    evidence.update(changes.pop("evidence", {}))
    res = attest(reads, AttestationProfile(active_trader_panels=("at-panel-1",), sha256=PROFILE),
                 a9=changes.pop("a9", ConfirmationCheck(True, 0, "a9-shot")), evidence=evidence)
    pm = judge_identity([SelectorRead(EMPTY, crop_sha256="a1-crop")], default_context().allowlist)
    return SimpleNamespace(run_type="pr", results=[pm, res.result])


def test_each_attestation_failure_is_derived_into_its_rows():
    blur = SettingRead("A4", None, 0.4, 30.0, "a4-blur")
    unread = run_manifest_safety(_attested(reads={"A4": RowReads(blur, blur)}), shard_id="s")
    assert unread.attestation == "unreadable" and unread.rows["A4"] == "unreadable"
    assert unread.rows["A3"] == "match" and unread.rows["A9"] == "not_read"        # no mismatch: the others matched
    no_dialog = run_manifest_safety(_attested(a9=ConfirmationCheck(False, 0, "a9-shot")), shard_id="s")
    assert no_dialog.attestation == "failed" and no_dialog.rows["A9"] == "mismatch" and no_dialog.rows["A8"] == "match"
    no_hash = run_manifest_safety(_attested(evidence={"A4": None}), shard_id="s")
    assert no_hash.rows["A11"] == "mismatch" and no_hash.rows["A9"] == "match" and "A4" in no_hash.missing_evidence
    clean = run_manifest_safety(_attested(), shard_id="s", allowlist=default_context().allowlist, fixture=FIXTURE)
    assert clean.attestation == "passed" and clean.accounts[0].label() == "D-xxxxxxxx (allowlist entry 2, role EMPTY)"
    assert clean.evidence["A1"] == "a1-crop" and clean.profile_sha256 == PROFILE


def test_rows_the_attestation_records_are_taken_as_they_are():
    shard = _attested()
    att = shard.results[1]
    shard.results[1] = dataclasses.replace(att, evidence={**att.evidence, "rows": {"A3": "match", "A4": "unreadable"}})
    s = run_manifest_safety(shard, shard_id="s")
    assert (s.rows["A3"], s.rows["A4"], s.rows["A5"]) == ("match", "unreadable", "not_read")


def test_a_malformed_manifest_record_is_refused():
    with pytest.raises(SafetyInputError, match="A1 to A11"):
        shard_safety(rows={"A1": "match"})
    with pytest.raises(SafetyInputError, match="states"):
        shard_safety(rows={i: "ok" for i in ITEMS})
    with pytest.raises(SafetyInputError, match="attestation result"):
        shard_safety(attestation="green")


# -- the section ----------------------------------------------------------------------------------------------------

def drill_row(n: int, state: str = "detected", reason: str | None = None) -> CanaryResult:
    produced = "harness_error" if reason and reason.startswith("drill_failed") else "env_error" if reason else None
    return CanaryResult(f"drill-{n}", "1", "flow/sentinel.fire-drill/canary.fire-drill/verdict", "fire_drill", False,
                        0, ["blocked"], state, produced_outcome=produced, produced_reason=reason, kind="fire_drill")


def canary_row(name: str, blind: bool = False) -> CanaryResult:
    return CanaryResult(name, "1", f"flow/sentinel.safety-canaries/canary.{name}/verdict", "safety_canary", True, 0,
                        ["mismatch"], "detector_blind" if blind else "detected",
                        produced_outcome="harness_error" if blind else None,
                        produced_reason=f"safety_canary_blind:{name}" if blind else None, kind="safety_canary")


def incident(**kw) -> Incident:
    base = dict(incident_id=12, run_id="nightly-2026-11-08", rig_id="rig-05", login_alias="paper-05", severity="S1",
                trigger="fire_drill: drill_order_placed", opened_at="2026-11-09T03:10:00Z", closed_at=None,
                closed_by=None, close_reason=None, ticket_ref=None, postmortem_ref=None,
                postmortem_due_at="2026-11-16T03:10:00Z")
    base.update(kw)
    return Incident(**base)


def quarantine(target_kind: str, target: str, reason: str, cause: str) -> SafetyQuarantine:
    return SafetyQuarantine(3, target_kind, target, reason, cause, None, "nightly-2026-11-08", "2026-11-09T03:00:00Z")


def test_the_drill_prints_pass_failed_with_quarantines_or_order_placed_with_the_blocked_login():
    night = "nightly-2026-11-08"
    passed = render(SafetyRows(canaries=tuple(drill_row(n) for n in range(1, 6)), nightly_run_id=night))
    title, lines = passed.groups[0]
    assert title == f"Fire drill (Nightly run {night})" and lines[0] == "pass: 5 of 5 items blocked"
    failed = render(SafetyRows(canaries=(drill_row(1), drill_row(2, "missed", "drill_failed:item2")),
                               nightly_run_id=night),
                    quarantines=[quarantine("activity", "order_stage_flows", "fire_drill", "drill_failed:item2"),
                                 quarantine("rig", "rig-04", "safety_canary", "safety_canary_blind:proof")])
    assert failed.groups[0][1][0] == ("harness_error (reason: drill_failed:item2); quarantined: every flow with an "
                                      "order-stage step, on every rig (fire_drill: drill_failed:item2)")
    assert "drill-2: missed, harness_error (reason: drill_failed:item2)" in failed.groups[0][1]
    sent = render(SafetyRows(canaries=tuple(drill_row(n, "missed", "drill_order_placed") for n in range(1, 6)),
                             nightly_run_id=night, incidents=(incident(),)))
    assert sent.groups[0][1][0] == ("aborted_safety (reason: drill_order_placed); the login of rig rig-05 blocked "
                                    "until a human verifies the account")
    assert "paper-05" not in repr(sent)                                   # a login alias is never printed (8.3)


def test_safety_canaries_banner_incidents_and_the_rc_residuals():
    rows = SafetyRows(proofs=(proof(),), canaries=(canary_row("identity"), canary_row("proof", blind=True)),
                      nightly_run_id="n-1", incidents=(incident(run_id=RUN, trigger="kill_switch: file_flag"),))
    sec = render(rows, shards=[shard_safety()], header=HEADER, banner="DETECTOR BLIND: a safety canary missed.",
                 quarantines=[quarantine("rig", "rig-04", "safety_canary", "safety_canary_blind:proof")],
                 run_type="rc")
    groups = dict(sec.groups)
    assert groups["Safety canaries (Nightly run n-1)"][0] == (
        "proof detector_blind: harness_error (reason: safety_canary_blind:proof); quarantined: every flow on rig "
        "rig-04 (safety_canary: safety_canary_blind:proof)")
    assert groups["Fire drill (Nightly run n-1)"] == ("no fire drill result recorded",)
    assert groups["Safety canary banner"] == ("DETECTOR BLIND: a safety canary missed.",)
    assert groups["Open incidents"][0].startswith("12 S1 (kill_switch: file_flag) on rig rig-05")
    assert sec.residuals == RESIDUALS and len(RESIDUALS) == 5
    assert [r.item for r in sec.attestation] == list(ITEMS) and sec.attestation[0].evidence_sha256 == "01" * 32
    assert sec.attestation[-1].evidence_sha256 is None and sec.worst == 0
    assert render(rows, shards=[shard_safety()]).residuals == ()            # the sign-off is the RC report's
