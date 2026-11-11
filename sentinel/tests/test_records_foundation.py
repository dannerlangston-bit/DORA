"""Tests for the record fields and types the foundation added to sentinel.records (4.12, 4.13, 4.14, 8.3, SD-16,
SD-32, SD-91, SD-96)."""
import copy

import pytest

from sentinel import records as R
from sentinel.canonical import fingerprint_id
from sentinel.records import (
    CaptureMeta,
    CheckpointOutcome,
    Decision,
    DeclaredSend,
    EgressRow,
    Escape,
    ExplorerFinding,
    Extraction,
    Finding,
    Fingerprint,
    Incident,
    MaintenanceItem,
    ModelUsage,
    OutboxJob,
    PreflightAttempt,
    Proposal,
    RunOutcome,
    SafetyQuarantine,
    Signature,
    StepEvent,
    StepOutcome,
    SyntheticOutcome,
    from_dict,
    outcome_id,
)
from sentinel.taxonomy import Taxonomy, TaxonomyError, load

CID = "sweep/trade.active-trader.ladder/flexgrid/640x720"
RUN = "run-2026-11-09-pr-0931"
TS = "2026-11-09T14:21:44-05:00"
STAMP = {"account_id_sha256": "sha256:51f20a0a", "mode": "paper", "asserted_at": "2026-11-09T14:21:02-05:00"}


def co(**kw):
    base = {"run_id": RUN, "checkpoint_id": CID, "outcome": "identical", "workspace_id": "ws.tier-c.sweep-harness",
                "persona": "active_trader", "data_mode": "masked_live"}
    return CheckpointOutcome(**{**base, **kw})


def ev(**kw):
    base = {"run_id": RUN, "shard_id": f"{RUN}/1", "side": "candidate", "seq": 1, "source": "orchestrator", "wall_at": TS,
                "mono_ms": 1234567, "mode": "scripted", "action": "click"}
    return StepEvent(**{**base, **kw})


# -- checkpoint_outcome (4.13) ----------------------------------------------------------------------------------

def test_outcome_id_is_oc_run_seq():
    assert outcome_id(RUN, 412) == "oc-run-2026-11-09-pr-0931-000412"
    co(outcome_id=outcome_id(RUN, 412))
    for bad in ("oc-other-run-000412", f"oc-{RUN}-12", f"{RUN}-000412"):
        with pytest.raises(TaxonomyError):
            co(outcome_id=bad)
    with pytest.raises(TaxonomyError):
        outcome_id(RUN, 0)


def test_checkpoint_outcome_new_fields_round_trip():
    o = co(outcome_id=outcome_id(RUN, 1), pair_id="pr-branch-vs-main", profile_id="paper-margin", diff_pixels=0,
           masked_pct=3.5, settle_ms=812, cand_rig_id="rig-01", base_rig_id="rig-02", app_uptime_s=1912,
           app_restart_count=0, paper_assertion=dict(STAMP), fingerprint_id="sha256:9a3f", data_epoch_id="sha256:3d1a",
           lower_results=[{"outcome": "collateral"}], mutant_id="M04")
    back = from_dict(CheckpointOutcome, o.to_dict())
    assert back == o


@pytest.mark.parametrize("kw", [
    {"diff_pixels": -1}, {"settle_ms": 1.5}, {"app_uptime_s": True}, {"app_restart_count": -2},
    {"masked_pct": 120.0}, {"paper_assertion": {"account_id_sha256": "x", "asserted_at": TS}},
    {"paper_assertion": {**STAMP, "mode": ""}}, {"paper_assertion": {**STAMP, "asserted_at": "yesterday"}},
    {"lower_results": [{"outcome": "looks_bad"}]}, {"lower_results": [{"outcome": "collateral", "why": "x"}]},
    {"lower_results": [{"outcome": "not_run", "reason": "not_a_reason"}]}, {"mutant_id": ""},
])
def test_checkpoint_outcome_refuses_malformed_new_fields(kw):
    with pytest.raises(TaxonomyError):
        co(**kw)


# -- step_outcome (4.13, SD-32) ---------------------------------------------------------------------------------

def test_step_outcome_rungs_reason_and_mode():
    s = StepOutcome(RUN, "flow.trade.order-ticket.stock.limit", "s07", "passed_via_agent_fallback",
                    locator_rung="agent", mode="agent", reason="export_timeout", app_uptime_s=10, app_restart_count=1,
                    post_screenshot="sha256:aa", click_crop="sha256:bb",
                    rung_results=[{"rung": "jab", "result": "not_found"}, {"rung": "image", "result": "below",
                                                                           "score": 0.61}])
    assert from_dict(StepOutcome, s.to_dict()) == s
    StepOutcome(RUN, "f", "s", "failed", locator_rung="none")


@pytest.mark.parametrize("kw", [
    {"locator_rung": "ocr2"}, {"rung_results": [{"rung": "none", "result": "x"}]},
    {"rung_results": [{"rung": "jab"}]}, {"rung_results": [{"rung": "jab", "result": "x", "score": "high"}]},
    {"rung_results": "jab"}, {"mode": "robot"}, {"app_restart_count": -1},
])
def test_step_outcome_refuses_bad_rungs_and_modes(kw):
    with pytest.raises(TaxonomyError):
        StepOutcome(RUN, "f", "s", "passed", **kw)


# -- step events (8.3, SD-16) -----------------------------------------------------------------------------------

def test_step_event_carries_every_column():
    e = ev(screen_x=10, screen_y=20, win_x=4, win_y=5, keys="ctrl+t", pre_hash="sha256:1", post_hash="sha256:2",
           click_crop_sha256="sha256:3", locator_level="image", step_outcome="passed", gate_decision="emitted",
           gate_class="allowed", expected_outcome="ticket opens", rect_w=96, rect_h=24, usage={"input_tokens": 3},
           injected=True, token_id=7, drill=False, popup_title=None, app_uptime_s=5, app_restart_count=0)
    assert e.mono_ms == 1234567 and e.to_dict()["usage"] == {"input_tokens": 3}


def test_secret_events_are_always_redacted():
    ev(action="secret", target_text="<redacted:login.password>")
    for text in ("hunter2", None, "<redacted:>", "<redacted:a b>"):
        with pytest.raises(TaxonomyError, match="redacted"):
            ev(action="secret", target_text=text)


@pytest.mark.parametrize("kw", [
    {"action": "step"}, {"mode": "robot"}, {"source": "hook"}, {"side": "both"}, {"seq": 0}, {"mono_ms": -1},
    {"gate_decision": "dropped"}, {"gate_decision": "maybe"}, {"gate_class": "fatal"}, {"locator_level": "none"},
    {"step_outcome": "ok"}, {"wall_at": "noon"}, {"token_id": -1}, {"checkpoint_id": "not/an/id"},
])
def test_step_event_refuses_values_outside_8_3(kw):
    with pytest.raises(TaxonomyError):
        ev(**kw)


def test_step_event_actions_come_from_the_taxonomy_once_generated(monkeypatch):
    data = copy.deepcopy(load().data)
    data["step_event_actions"] = ["click"]
    monkeypatch.setattr(R, "load", lambda: Taxonomy(data))
    ev(action="click")
    with pytest.raises(TaxonomyError):
        ev(action="wheel")


# -- run_outcome, finding, decision -----------------------------------------------------------------------------

def test_run_outcome_refs_and_distance():
    r = RunOutcome(RUN, "pr", "complete", "reported", pair_id="pr-1", summary={"health": []},
                   report_sha256="sha256:r", comment_sha256="sha256:c", base_commit_distance=3,
                   authorization_sha256="sha256:a", coverage_matrix_ref="sha256:m", egress_log_ref="sha256:e",
                   post_run_proof_ref="sha256:p")
    assert from_dict(RunOutcome, r.to_dict()) == r
    with pytest.raises(TaxonomyError):
        RunOutcome(RUN, "pr", "complete", "reported", base_commit_distance=21)


def explorer(**kw):
    base = {"session_id": "xs-1", "signature": {"surface_id": "trade.all-products", "observation": "INV-03",
                                              "crop_sha256": "sha256:cc"},
                "severity": "high", "confidence": 0.8, "trace_ref": "sha256:tt", "replayed": "reproduced",
                "candidate_flow": "flow.quarantine.fnd-1"}
    return ExplorerFinding(**{**base, **kw})


def finding(**kw):
    base = {"finding_id": "fnd-1", "run_id": RUN, "kind": "explorer_candidate", "description": "x", "first_seen_run_id": RUN}
    return Finding(**{**base, **kw})


def test_finding_explorer_block_and_new_fields_round_trip():
    f = finding(explorer=explorer(), pre_existing=True, cluster_id="cl-1", contested=True, held=True,
                closed_at=TS, category="collateral",
                evidence={"artifacts": ["sha256:a"], "step_events": {"first_seq": 3, "last_seq": 9},
                          "raw_values": {"expected": "100", "read": "10"}, "layout": "grid"})
    assert from_dict(Finding, f.to_dict()) == f


@pytest.mark.parametrize("kw", [
    {"replayed": "maybe"}, {"severity": "S1"}, {"confidence": 1.5}, {"confidence": None},
    {"signature": {"surface_id": "s", "observation": "o"}}, {"trace_ref": ""},
])
def test_explorer_block_is_validated(kw):
    with pytest.raises(TaxonomyError):
        explorer(**kw)


@pytest.mark.parametrize("kw", [
    {"category": "identical"}, {"evidence": {"artifacts": "sha256:a"}},
    {"evidence": {"step_events": {"first_seq": 9, "last_seq": 3}}}, {"evidence": {"step_events": {"first_seq": 1}}},
    {"evidence": {"raw_values": {"read": 10}}}, {"kind": "collateral", "explorer": None, "closed_at": "later"},
])
def test_finding_category_evidence_and_closed_at(kw):
    with pytest.raises(TaxonomyError):
        finding(**kw)


def test_explorer_block_only_on_explorer_candidates():
    with pytest.raises(TaxonomyError, match="explorer_candidate"):
        finding(kind="collateral", explorer=explorer())


def test_decision_tolerance_unset_bbox_role_reopen():
    sig = Signature(CID, "rgn.ladder.bid-column", "c3a5e1f0b4d29876", "invariant_violation")
    d = Decision("dec-000812", "confirm_bug", "confirmed_bug", sig, decided_by="rkim", ticket="TOS-48377")
    assert d.hamming_tolerance is None and d.reopen_count == 0
    d = Decision("dec-000813", "confirm_bug", "confirmed_bug", sig, decided_by="rkim", ticket="TOS-1",
                 hamming_tolerance=6, bbox_q4=[12, 388, 208, 20], role="triage_rotation", reopen_count=1)
    assert d.bbox_q4 == (12, 388, 208, 20)
    assert from_dict(Decision, d.to_dict()) == d
    for kw in ({"hamming_tolerance": 9}, {"bbox_q4": (13, 388, 208, 20)}, {"bbox_q4": (0, 0, 0, 4)},
               {"role": "reviewer"}, {"reopen_count": -1}, {"status": "paused"}, {"source": "slack"}):
        with pytest.raises(TaxonomyError):
            Decision("dec-1", "confirm_bug", "confirmed_bug", sig, decided_by="rkim", ticket="T", **kw)


# -- the 8.3 row records ----------------------------------------------------------------------------------------

MATCH = {"build_id": "branch-1998.3.0-7be42d0", "gpu": {"name": "Radeon™", "driver_version": "1"},
         "render_probe_hash": "e3b0c44298fc1c14"}


def test_fingerprint_id_recomputes():
    fp = Fingerprint(RUN, "candidate", "rig-01", TS, dict(MATCH), shard_id=f"{RUN}/1")
    assert fp.fingerprint_id == fingerprint_id(MATCH) and fp.build_id == MATCH["build_id"]
    with pytest.raises(TaxonomyError, match="recompute"):
        Fingerprint(RUN, "candidate", "rig-01", TS, dict(MATCH), fingerprint_id="sha256:" + "0" * 64)
    for kw in ({"match": {"gpu": "x"}}, {"match": {**MATCH, "gpu": None}}, {"side": "both"}):
        with pytest.raises(TaxonomyError):
            Fingerprint(**{"run_id": RUN, "side": "base", "rig_id": "rig-02", "read_at": TS, "match": dict(MATCH),
                           **kw})


def test_capture_meta_and_extraction():
    CaptureMeta("sha256:9e", RUN, CID, "candidate", "SunAwtFrame", "Main@thinkorswim", (0, 0, 1920, 1040), TS,
                z_order=0, dpi=96, scale_percent=100, mode="scripted", redaction_boxes=2)
    with pytest.raises(TaxonomyError):
        CaptureMeta("sha256:9e", RUN, CID, "candidate", "Chrome_WidgetWin", "x", (0, 0, 10, 10), TS)
    Extraction(RUN, CID, "candidate", "rule.rgn.qty.qty", "qty", "ocr", TS, text="100", value="100", confidence=91.0,
               second_pass=True)
    for kw in ({"source": "clipboard2"}, {"confidence": 101}, {"side": "both"}):
        with pytest.raises(TaxonomyError):
            Extraction(**{"run_id": RUN, "checkpoint_id": CID, "side": "base", "assertion_id": "a", "field": "f",
                          "source": "jab", "read_at": TS, **kw})


def test_egress_outbox_and_usage():
    EgressRow("api.anthropic.com", "review", "sent", 2048, redaction_version="r3")
    with pytest.raises(TaxonomyError, match="refusal_reason"):
        EgressRow("evil.example", "review", "refused", 0)
    with pytest.raises(TaxonomyError):
        EgressRow("h", "upload", "sent", 1)
    OutboxJob("jira_create", "jira.internal", "sha256:p")
    with pytest.raises(TaxonomyError):
        OutboxJob("jira_create", "jira.internal", "sha256:p", state="sent")
    with pytest.raises(TaxonomyError):
        OutboxJob("fax", "x", "sha256:p")
    with pytest.raises(TaxonomyError):
        OutboxJob("slack", "x", "sha256:p", closed_by="alice", closed_at=TS)      # not abandoned
    ModelUsage("reviewer", "claude-x", TS, input_tokens=10, output_tokens=2, cost_usd=0.01)
    with pytest.raises(TaxonomyError):
        ModelUsage("diff_engine", "claude-x", TS)


def test_incident_proposal_escape_quarantine():
    Incident("S1", "orders_sent", TS, rig_id="rig-01")
    with pytest.raises(TaxonomyError):
        Incident("S1", "x", TS, closed_at=TS)
    with pytest.raises(TaxonomyError):
        Incident("S1", "x", TS, closed_at=TS, closed_by="system")
    Proposal("fnd-1", RUN, "mark_intended", "pr-author", "padding change", ticket="TOS-1")
    with pytest.raises(TaxonomyError, match="different human"):
        Proposal("fnd-1", RUN, "mark_intended", "bob", "r", state="confirmed", decided_by="bob", decided_at=TS,
                 decision_ref="dec-000001")
    with pytest.raises(TaxonomyError):
        Proposal("fnd-1", RUN, "mark_intended", "bob", "r", state="declined", decided_by="amy", decided_at=TS)
    Escape("1998.3.0", "TOS-9", "covered_missed", "amy", checkpoint_id=CID)
    with pytest.raises(TaxonomyError):
        Escape("1998.3.0", "TOS-9", "covered_missed", "amy")
    with pytest.raises(TaxonomyError):
        Escape("1998.3.0", "TOS-9", "not_registered", "amy", checkpoint_id=CID)
    SafetyQuarantine("flow", "flow.trade.x", "gateway_drops", TS)
    with pytest.raises(TaxonomyError):
        SafetyQuarantine("flow", "flow.trade.x", "boredom", TS)
    with pytest.raises(TaxonomyError):
        SafetyQuarantine("rig", "rig-01", "kill_switch", TS, reenabled_by="amy", reenabled_at=TS)


def test_declared_send_preflight_maintenance_synthetic():
    DeclaredSend(RUN, f"{RUN}/1", "flow.paper.send", "s03", "W-7781", TS, token_id=4, symbol="SPY", qty=1)
    with pytest.raises(TaxonomyError):
        DeclaredSend(RUN, f"{RUN}/1", "flow.paper.send", "s03", "", TS)
    PreflightAttempt(RUN, "H7", 2, False, TS)
    with pytest.raises(TaxonomyError):
        PreflightAttempt(RUN, "Z9", 1, True, TS)
    with pytest.raises(TaxonomyError):
        PreflightAttempt(RUN, "H7", 3, True, TS)
    MaintenanceItem("update_scripted_flow", "flow.trade.x/s04", "flow-owner", TS, hours=1.5)
    with pytest.raises(TaxonomyError):
        MaintenanceItem("Update Flow", "flow.trade.x", "o", TS)
    SyntheticOutcome(RUN, "M04", CID, "collateral")
    with pytest.raises(TaxonomyError):
        SyntheticOutcome(RUN, "M04", CID, "not_run")                 # not_run needs a reason


def test_step_outcome_action_is_an_8_3_action_and_round_trips(tmp_path):
    """8.3 step_events.action: a step outcome row carries a vocabulary action, and the reader can read it back."""
    import pytest as _pytest
    from sentinel.records import StepOutcome
    from sentinel.taxonomy import TaxonomyError
    with _pytest.raises(TaxonomyError):
        StepOutcome("run", "flow.x", "s01", "passed", action="step")
    assert StepOutcome("run", "flow.x", "s01", "passed", action="capture").action == "capture"
