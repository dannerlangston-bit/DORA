import pytest

from sentinel.records import CheckpointOutcome, Decision, Finding, RunOutcome, Signature
from sentinel.taxonomy import TaxonomyError

CID = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"


def test_checkpoint_outcome_derives_surface_and_kind():
    o = CheckpointOutcome(run_id="run-1", checkpoint_id=CID, outcome="invariant_violation", reason="INV-01",
                          workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="frozen",
                          invariant_ids=["INV-01"])
    assert o.surface_id == "charts.flexible-grid.active-trader-cell" and o.test_kind == "sweep"
    assert o.to_dict()["outcome"] == "invariant_violation"


def test_pending_review_only_with_needs_human():
    with pytest.raises(TaxonomyError):
        CheckpointOutcome(run_id="r", checkpoint_id=CID, outcome="collateral", workspace_id="ws.a",
                          persona="active_trader", data_mode="frozen", review_state="pending")


def test_run_outcome_requires_abort_reason():
    RunOutcome(run_id="r", run_type="pr", status="complete", last_gate="reported", counts={"identical": 3})
    with pytest.raises(TaxonomyError):
        RunOutcome(run_id="r", run_type="pr", status="aborted_budget", last_gate="running")


def test_finding_phash_shape():
    Finding(finding_id="F-1", run_id="r", kind="app_crash", description="x", first_seen_run_id="r", phash="0123456789abcdef")
    with pytest.raises(TaxonomyError):
        Finding(finding_id="F-1", run_id="r", kind="looks_bad", description="x", first_seen_run_id="r")


def test_decisions_are_human_and_mapped():
    sig = Signature(CID, "rgn.ladder.order-strip", "0123456789abcdef", "collateral")
    Decision("dec-1", "confirm_bug", "confirmed_bug", sig, decided_by="alice", ticket="TOS-1")
    Decision("dec-2", "expected_behavior", "noise", sig, decided_by="alice")
    with pytest.raises(TaxonomyError):
        Decision("dec-3", "mark_intended", "intended", sig, decided_by="alice")  # no ticket
    with pytest.raises(TaxonomyError):
        Decision("dec-4", "noise_signature", "noise", sig, decided_by="reviewer")
    with pytest.raises(TaxonomyError):
        Decision("dec-5", "confirm_bug", "noise", sig, decided_by="alice")


def test_round_trip_and_new_fields():
    from sentinel.records import Classification, from_dict
    o = CheckpointOutcome(run_id="r", checkpoint_id=CID, outcome="collateral", workspace_id="ws.a",
                          persona="active_trader", data_mode="frozen", base_source_run_id="run-0",
                          classification=Classification(category="collateral", by="rule"))
    back = from_dict(CheckpointOutcome, o.to_dict())
    assert back.classification.category == "collateral" and back.base_source_run_id == "run-0"
    f = Finding(finding_id="F-2", run_id="r", kind="missed_in_pr_run", description="x", first_seen_run_id="r",
                detail="static_impact")
    assert f.to_dict()["detail"] == "static_impact"
    sig = Signature(CID, "rgn.a", "0123456789abcdef", "collateral")
    with pytest.raises(TaxonomyError):
        Decision("dec-9", "noise_signature", "noise", sig, decided_by="orchestrator")
    d = Decision("dec-8", "confirm_bug", "confirmed_bug", sig, decided_by="alice", ticket="TOS-1", build_pair_id="bp-1")
    assert from_dict(Decision, d.to_dict()).signature.region_id == "rgn.a"
