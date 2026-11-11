"""A3 "absent" (6.13 item 3, branch B): A3 reads "absent" and A9 is the sole attestation of the dialog, run twice at
`settings_attested` (6.3)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sentinel.orchestrator.attestation import AttestationProfile, RowReads, attest, matches, readable
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import ConfirmationCheck, SettingRead
from sentinel.orchestrator.simrig import MemorySink, SimScenario, SimulatedRig, default_context, default_settings

ABSENT = AttestationProfile(order_confirmation="absent", active_trader_panels=("at-panel-1",), sha256="p" * 64)
ON = replace(ABSENT, order_confirmation=True)
OK = ConfirmationCheck(True, 0, "sha-A9")
OK2 = ConfirmationCheck(True, 0, "sha-A9-2")
EVIDENCE = {**{i: f"sha-{i}" for i in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9")}, "A9_second": "sha-A9-2"}
SCOPE = ["census/trade.order-entry.stock/ws.tier-a.active-trader",
         "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"]


def rows(**over) -> dict[str, RowReads]:
    vals = {**default_settings(), "A3": "absent", **over}
    return {i: RowReads(SettingRead(i, vals[i], screenshot_sha256=f"sha-{i}")) for i in vals}


def test_the_profile_keeps_absent_and_refuses_anything_else():
    p = AttestationProfile.from_settings({"order_confirmation": "absent", "shift_click_send": "absent"})
    assert (p.order_confirmation, p.shift_click_send, p.a9_runs) == ("absent", "absent", 2)
    assert AttestationProfile.from_settings({"order_confirmation": True}).a9_runs == 1
    with pytest.raises(ValueError):
        AttestationProfile(order_confirmation="maybe")


def test_a3_reads_absent_against_an_absent_profile_and_mismatches_a_toggle_profile():
    absent = SettingRead("A3", "absent")
    assert readable(absent, ABSENT) and matches(absent, ABSENT) and not matches(absent, ON)
    assert readable(SettingRead("A3", True), ABSENT) and not matches(SettingRead("A3", True), ABSENT)
    assert not readable(SettingRead("A3", "unknown"), ABSENT)
    r = attest(rows(), ON, a9=None, evidence=EVIDENCE).result               # the option disappeared from the page
    assert (r.run_status, r.run_reason) == ("aborted_safety", "attestation_mismatch") and "A3" in r.detail


def test_two_passing_a9_checks_pass_the_gate_and_both_hashes_are_a11_evidence():
    res = attest(rows(), ABSENT, a9=OK, a9_second=OK2, evidence=EVIDENCE)
    assert res.result.passed and res.rows["A3"] == "match" and res.rows["A9"] == "match"
    assert res.result.evidence["hashes"]["A9_second"] == "sha-A9-2"
    missing = attest(rows(), ABSENT, a9=OK, a9_second=OK2, evidence={**EVIDENCE, "A9_second": None}).result
    assert (missing.run_status, missing.run_reason) == ("aborted_harness", "artifact_persist_failed")
    with pytest.raises(ValueError):
        attest(rows(), ABSENT, a9=OK, evidence=EVIDENCE)                    # the second check is required


def test_a_second_dialog_absence_aborts_and_a_new_row_on_it_is_the_kill_switch():
    r = attest(rows(), ABSENT, a9=OK, a9_second=ConfirmationCheck(False, 0, "x"), evidence=EVIDENCE).result
    assert (r.run_status, r.run_reason, r.incident) == ("aborted_safety", "attestation_mismatch", "S2")
    assert "second check" in r.detail
    sent = attest(rows(), ABSENT, a9=OK, a9_second=ConfirmationCheck(True, 1, "x"), evidence=EVIDENCE).result
    assert (sent.run_reason, sent.incident) == ("kill_switch", "S1")
    first = attest(rows(), ABSENT, a9=ConfirmationCheck(False, 0, "x"), evidence=EVIDENCE).result
    assert first.run_reason == "attestation_mismatch" and "second" not in first.detail


class SecondA9Absent(SimulatedRig):
    """The dialog appears for the first A9 check and not for the second."""

    def confirmation_check(self) -> ConfirmationCheck:
        first = "confirmation_check" not in self.calls
        super().confirmation_check()
        return ConfirmationCheck(first, 0, "sha-a9-1" if first else "sha-a9-2")


def _run(rig_cls=SimulatedRig):
    clock = ManualClock()
    rig = rig_cls(SimScenario(settings={**default_settings(), "A3": "absent"}), clock=clock)
    sink = MemorySink(SCOPE)
    orch = Orchestrator(rig, default_context(profile=ABSENT), sink, clock=clock)
    report = orch.run(lambda o: [o.sink.record(c, "identical") for c in SCOPE])
    return report, rig, sink


def test_the_orchestrator_runs_a9_twice_when_a3_is_absent():
    report, rig, _ = _run()
    assert report.verdict.status == "complete" and rig.calls.count("confirmation_check") == 2
    assert "load_order_chords" in rig.calls


def test_a_dialog_absent_on_the_second_a9_check_aborts_the_run():
    report, rig, sink = _run(SecondA9Absent)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "attestation_mismatch")
    assert {v[:3] for v in sink.outcomes.values()} == {("env_error", "attestation_mismatch", "settings_attested")}
    assert rig.calls.count("confirmation_check") == 2 and report.kill_switch is not None
