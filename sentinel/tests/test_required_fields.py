"""Tests for sentinel.required_fields: the 4.13 required-fields table (L-OUT-02) and the SD-48 provenance rule."""
import pytest

from sentinel import required_fields
from sentinel.classify import Classified, required_field_errors
from sentinel.records import CheckpointOutcome, Classification, DiffSummary

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
D = DiffSummary(whole_pixel_fraction=0.02, whole_ssim=0.97)
CAND = "c" * 64
PROV = {"fingerprint_id": "sha256:fp", "data_epoch_id": "sha256:ep"}
STEPS = {"flow.trade.order-ticket.stock.limit": {"s03", "s07"}}

# one complete record per outcome, and the fields each one cannot lose
VALID = {
    "identical": ({"diff": D}, ["diff"]),
    "noise": ({"reason": "below_floor", "diff": D, "classification": Classification("noise", "rule")},
              ["diff", "classification", "reason"]),
    "intended": ({"diff": D, "manifest_entry_ref": "tos-1",
                  "classification": Classification("intended", "reviewer", confidence=0.95)},
                 ["diff", "manifest_entry_ref", "classification"]),
    "collateral": ({"diff": D, "primitive": "FlexibleGrid", "manifest_entry_ref": "tos-1",
                    "classification": Classification("collateral", "rule")},
                   ["primitive", "manifest_entry_ref", "classification"]),
    "needs_human": ({"reason": "no_manifest", "diff": D}, ["diff", "reason"]),
    "invariant_violation": ({"reason": "INV-01", "invariant_ids": ["INV-01", "INV-03"],
                             "artifacts": {"candidate": CAND}, **PROV},
                            ["invariant_ids", "artifacts", "fingerprint_id", "data_epoch_id"]),
    "assertion_failed": ({"reason": "assertion:qty", "assertion_ids": ["qty"], "detail": "expected 100, read 10"},
                         ["assertion_ids", "detail"]),
    "not_settled": ({"reason": "timeout", "side": "candidate", "detail": "anchor a1, waited 8000 ms"},
                    ["reason", "side", "detail"]),
    "landmark_missing": ({"reason": "landmark:lm1", "side": "base", "detail": "lm1 after 5000 ms"},
                         ["reason", "side", "detail"]),
    "not_comparable": ({"reason": "window_mismatch", "detail": "title"}, ["reason", "detail"]),
    "env_error": ({"gate": "fixture_verified", "reason": "fixture_drift"}, ["gate", "reason"]),
    "harness_error": ({"reason": "hook_lost", "detail": "watchdog_event: wd-0017"}, ["reason", "detail"]),
    "not_run": ({"reason": "step_failed:s03", "flow_id": "flow.trade.order-ticket.stock.limit"},
                ["reason", "flow_id"]),
    "unregistered_surface": ({"reason": "probe:P2", "detail": "label Foo, left rail of the main window"},
                             ["reason", "detail"]),
    "needs_baseline": ({"reason": "new_surface", "side": "base"}, ["reason", "side"]),
}


def rec(outcome, **kw):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=CENSUS, outcome=outcome, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", **kw)


def errs(r):
    return required_fields.errors(r, flow_steps=STEPS)


def test_every_outcome_has_a_row_here():
    from sentinel.taxonomy import load
    assert set(VALID) == set(load().checkpoint_outcomes)


@pytest.mark.parametrize("outcome", sorted(VALID))
def test_a_complete_record_is_accepted(outcome):
    assert errs(rec(outcome, **VALID[outcome][0])) == []


@pytest.mark.parametrize("outcome,field", [(o, f) for o, (_, fs) in sorted(VALID.items()) for f in fs])
def test_each_required_field_removed_is_refused(outcome, field):
    r = rec(outcome, **VALID[outcome][0])
    setattr(r, field, None)                  # records are mutable; the validator reads what is there
    found = errs(r)
    assert found and all(e.startswith(f"{outcome}: ") and "L-OUT-02" in e for e in found)


def test_step_failed_names_a_step_of_its_flow():
    ok = rec("not_run", **VALID["not_run"][0])
    assert errs(ok) == []
    assert errs(rec("not_run", reason="step_failed:zz99", flow_id="flow.trade.order-ticket.stock.limit"))
    assert errs(rec("not_run", reason="step_failed:s03", flow_id="flow.other"))
    assert required_fields.errors(rec("not_run", reason="step_failed:zz99")) == []    # no flow steps: not checked
    assert errs(rec("not_run", reason="scope")) == []


def test_reason_names_the_first_invariant_by_catalog_order():
    kw = {**VALID["invariant_violation"][0], "reason": "INV-03"}
    assert errs(rec("invariant_violation", **kw))
    kw = {**VALID["invariant_violation"][0], "invariant_ids": ["INV-10", "INV-02"], "reason": "INV-02"}
    assert errs(rec("invariant_violation", **kw)) == []


def test_assertion_reason_names_the_first_assertion():
    kw = {**VALID["assertion_failed"][0], "assertion_ids": ["other", "qty"]}
    assert errs(rec("assertion_failed", **kw))


def test_decision_forms_of_noise_and_intended():
    assert errs(rec("noise", diff=D, reason="decision:dec-000001", decision_ref="dec-000001",
                    classification=Classification("noise", "rule"))) == []
    assert errs(rec("noise", diff=D, reason="decision:dec-000001",
                    classification=Classification("noise", "rule")))           # no decision_ref
    assert errs(rec("intended", diff=D, manifest_entry_ref="tos-1", reason="decision:dec-000002",
                    decision_ref="dec-000002", classification=Classification("intended", "rule"))) == []
    assert errs(rec("intended", diff=D, manifest_entry_ref="tos-1",
                    classification=Classification("intended", "reviewer")))   # a reviewer call without confidence


def test_collateral_manifest_missing_and_the_removed_surface():
    rule = Classification("collateral", "rule")
    assert errs(rec("collateral", diff=D, primitive="FlexibleGrid", reason="manifest_missing",
                    classification=rule)) == []
    removed = rec("collateral", classification=rule, artifacts={"base": "b" * 64, "candidate": CAND}, **PROV)
    assert errs(removed) == []                                    # 3.11 row 26: the two probe screenshots
    assert errs(rec("collateral", classification=rule, artifacts={"candidate": CAND}, **PROV))


def test_needs_human_pending_only_while_queued():
    assert errs(rec("needs_human", diff=D, reason="reviewer_unavailable", review_state="pending")) == []
    r = rec("needs_human", diff=D, reason="no_manifest")
    r.review_state = "pending"
    assert errs(r)


def test_env_error_after_a_capture_needs_the_last_screenshot():
    kw = VALID["env_error"][0]
    assert errs(rec("env_error", captured_at="2026-11-09T14:21:40+00:00", **kw))
    assert errs(rec("env_error", captured_at="2026-11-09T14:21:40+00:00", artifacts={"candidate": CAND},
                    **PROV, **kw)) == []


def test_capture_provenance_and_the_synthetic_exemption():
    kw = {"diff": D, "artifacts": {"candidate": CAND}}
    assert errs(rec("identical", **kw))
    assert errs(rec("identical", **kw, **PROV)) == []
    assert errs(rec("identical", synthetic=True, **kw)) == []    # a mutant row is generated, not captured
    assert required_fields.references_capture(rec("identical", artifacts={"crops": ["e" * 64]}))
    assert not required_fields.references_capture(rec("identical", artifacts={}))


def test_without_evidence_the_engine_fields_are_not_checked():
    assert required_fields.errors(rec("identical"), evidence=False) == []
    assert required_fields.errors(rec("invariant_violation", reason="INV-01", invariant_ids=["INV-01"]),
                                  evidence=False) == []


def test_classification_uses_the_same_validator():
    c = Classified(row="16", outcome="collateral", classification=Classification("collateral", "rule"))
    assert any("primitive" in e for e in required_field_errors(c))
    removed = Classified(row="26", outcome="collateral", classification=Classification("collateral", "rule"))
    assert required_field_errors(removed) == []
