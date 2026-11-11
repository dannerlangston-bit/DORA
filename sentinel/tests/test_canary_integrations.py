"""The canary state's page and SENT Bug (9.9, 9.10, 10.3 Scoring and Morning message)."""
from __future__ import annotations

import hashlib

import pytest

from sentinel import canary as C
from sentinel import canary_integrations as I
from sentinel.records import CheckpointOutcome
from sentinel.orchestrator.alerts import P1, P2

RUN = "run-2026-11-10-nightly-0001"
CONFIRM = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
POSITIONS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
CTX = C.RowContext(run_id=RUN, workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="frozen")


def mutant(mid, cls, money=False, cid=CONFIRM, outcomes=("invariant_violation",), error=None):
    e = C.MutantEntry(mutant_id=mid, checkpoint_id=cid, defect_class=cls, mode="image", money_cell=money, op="erase",
                      params={}, expected=C.Expected(tuple(outcomes)), owner="sentinel-canaries", region_ref="rgn.x",
                      catalog_version="v1")
    return C.Mutant(entry=e, checkpoint_id=cid, seed=C.SEED, base_sha256="b" * 64,
                    mutant_sha256=None if error else "m" * 64, footprint=None if error else (1, 1, 2, 2), error=error)


def result(m, outcome, reason=None, **kw):
    return C.MutantResult(mutant=m, produced=CheckpointOutcome(run_id=RUN, checkpoint_id=m.checkpoint_id,
                                                               outcome=outcome, reason=reason,
                                                               workspace_id="ws.tier-a.active-trader",
                                                               persona="active_trader", data_mode="frozen",
                                                               synthetic=True), **kw)


def test_alert_routes_follow_the_canary_failure_row():
    blind, failed, path, degraded = (I.alert_route(s) for s in ("detector_blind", "generation_failed",
                                                                 "canary_path_divergence", "detector_degraded"))
    assert (blind.priority, blind.paged, blind.rule) == (P1, ("harness_on_call",), "canary_failure")
    assert failed == blind and (path.priority, path.rule) == (P1, "canary_path_divergence")
    assert (degraded.priority, degraded.paged, degraded.notified) == (P2, (), ("on_call_channel",))
    assert I.alert_route("ok") is None
    with pytest.raises(ValueError):
        I.alert_route("missed")


def test_alert_entries_page_for_night_two_and_not_for_a_green_night():
    ok = C.DetectorState("ok", RUN)
    assert I.alert_entries(ok, RUN) == []
    blind = C.DetectorState("detector_blind", RUN, banner="DETECTOR BLIND since 2026-11-10 03:12 ET.\nMissed 1",
                            canary_state="detector_blind", divergence=True)
    entries = I.alert_entries(blind, RUN, "run-2026-11-10-nightly-0001/1")
    assert [(e["rule"], e["priority"], e["control"]) for e in entries] == [
        ("canary_failure", P1, "canary"), ("canary_path_divergence", P1, "canary")]
    assert entries[0]["detail"] == "DETECTOR BLIND since 2026-11-10 03:12 ET." and not entries[0]["safety_table"]
    degraded = C.DetectorState("detector_degraded", RUN, banner="DETECTOR DEGRADED since x.", canary_state="detector_degraded",
                               classes=("shifted_panel",))
    (e,) = I.alert_entries(degraded, RUN)
    assert e["priority"] == P2 and e["detail"].endswith("Classes: shifted_panel.")


def test_a_blind_night_files_a_highest_bug_keyed_by_the_mutant():
    m04 = mutant("M04", "swapped_digit", money=True, outcomes=("assertion_failed",))
    rep = C.score([result(m04, "identical", absorbed_by="mask msk-1 (region rgn.confirm.bp-effect)")])
    (bug,) = I.sent_bug_payload(rep, run_id=RUN, report_link="https://sentinel.internal/r/1")
    assert (bug["project"], bug["issuetype"], bug["priority"], bug["dedup_key"]) == ("SENT", "Bug", "Highest", "M04")
    cp = hashlib.sha256(CONFIRM.encode()).hexdigest()[:12]
    assert bug["labels"] == ["sentinel", "sentinel-surface-dialog.order-confirmation", f"sentinel-cp-{cp}"]
    assert "Absorbed by: mask msk-1 (region rgn.confirm.bp-effect)." in bug["description"]
    assert "  canary: M04\n" in bug["description"] and "  report: https://sentinel.internal/r/1" in bug["description"]
    green = C.score([result(m04, "assertion_failed", "assertion:R02")])
    assert I.sent_bug_payload(green, run_id=RUN) == []


def test_a_degraded_class_files_a_high_bug_keyed_by_the_class():
    rs = [result(mutant(m, "shifted_panel", cid=POSITIONS, outcomes=("collateral",)), "identical") for m in ("M08", "M09")]
    rep = C.score(rs)
    (bug,) = I.sent_bug_payload(rep, run_id=RUN)
    assert (bug["priority"], bug["dedup_key"]) == ("High", "shifted_panel")
    assert "Class shifted_panel: rate 0%, floor 90%." in bug["description"]
    assert I.alert_route(rep.state).priority == P2


def test_generation_failed_files_one_bug_keyed_by_the_stage():
    failed = mutant("M02", "erased_control", money=True, error="no approved capture for " + CONFIRM)
    rep = C.score([C.MutantResult(failed)], context=CTX)
    (bug,) = I.sent_bug_payload(rep, run_id=RUN)
    assert (bug["priority"], bug["dedup_key"]) == ("Highest", "stage:canary_generation")
    assert f"M02 erased_control on {CONFIRM}: no approved capture for {CONFIRM}." in bug["description"]


def test_a_blind_redaction_canary_files_a_highest_bug():
    m19 = mutant("M19", "redaction", outcomes=("identical",))
    rep = C.score([result(m19, "identical", redaction_hit=False)])
    assert rep.state == "detector_blind"
    assert [(b["priority"], b["dedup_key"]) for b in I.sent_bug_payload(rep, run_id=RUN)] == [("Highest", "M19")]
