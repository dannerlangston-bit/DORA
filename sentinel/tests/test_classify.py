"""Tests for the classification decision table (3.11, 1.10, 3.16, 4.13, F27, F28)."""
import re
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest

from sentinel import invariants as inv
from sentinel.classify import (
    G_ORDER, GLOBAL_SHIFT_ROWS, INPUT_VALUES, REVIEWER_ROWS, ROW_IDS, RULES, AppFailure, Classified, Condition,
    DecisionMatch, EnvFailure, Facts, HarnessFailure, Inputs, Review, apply_late_verdict, classify, close_pending,
    derive_inputs, match_row, required_field_errors, reviewer_input,
)
from sentinel.records import CheckpointOutcome, Classification, Decision, Signature
from sentinel.taxonomy import TaxonomyError

SPEC_03 = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "03-comparison-model.md"
CID = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
SIG = Signature(CID, "rgn.ladder.viewport", "0123456789abcdef", "collateral")
SPEC_ROW_ORDER = ("1", "1a", "2", "3", "4", "5", "6", "7", "8", "9", "9a", "10", "11", "11a", "12", "13", "14",
                  "15", "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "25a", "26")


def dec(i, action, verdict, ticket=None, entry=None, at="2026-11-01T00:00:00+00:00", status="active"):
    return Decision(i, action, verdict, SIG, decided_by="alice", ticket=ticket, manifest_entry_ref=entry,
                    decided_at=at, status=status)


NOISE = DecisionMatch(dec("dec-1", "noise_signature", "noise"), hamming=3)
INTENDED = DecisionMatch(dec("dec-2", "mark_intended", "intended", ticket="TOS-48120", entry="tos-48120-1"),
                         hamming=2, build_pair_id="pair-old")
BUG = DecisionMatch(dec("dec-3", "confirm_bug", "confirmed_bug", ticket="TOS-9"), hamming=1)


def answered(verdict, confidence=0.93, **kw):
    return Review("answered", verdict=verdict, confidence=confidence, **kw)


ABOVE = dict(diff="above", stability="stable", primitive="FlexibleGrid", manifest_entry_ref="tos-48120-1")
DECLARED = dict(ABOVE, manifest="declared")
REMOVED = dict(base="removed", manifest="declared", manifest_entry_ref="tos-48120-1")

#: One input per row that fires exactly that row.
ROW_FACTS = {
    "1": Facts(env=EnvFailure("authenticated", "session_kicked")),
    "1a": Facts(app=AppFailure("jvm_crash")),
    "2": Facts(harness=HarnessFailure("bounds_source_unavailable", "wd-0017")),
    "3": Facts(not_run=Condition("step_failed:s04")),
    "4": Facts(not_comparable=Condition("fingerprint_mismatch", detail="data_epoch_id")),
    "5": Facts(not_settled=Condition("frames_unstable", side="candidate", detail="no 3 equal frames in 15 s")),
    "6": Facts(landmark=Condition("landmark:lm.ladder-header", side="base", detail="lm.ladder-header below 0.92")),
    "7": Facts(invariant_ids=("INV-01",), invariant_stability="stable"),
    "8": Facts(assertion_ids=("qty",), assertion_detail="expected 1, extracted 10"),
    "9": Facts(base="absent", base_reason="new_surface"),
    "9a": Facts(oracle_unreadable=("price",), diff="below"),
    "10": Facts(),
    "11": Facts(diff="below"),
    "11a": Facts(rescued=True),
    "12": Facts(**dict(ABOVE, stability="unstable")),
    "13": Facts(**ABOVE, decisions=(NOISE,)),
    "14": Facts(**ABOVE, decisions=(INTENDED,), manifest_tickets=frozenset({"TOS-48120"})),
    "15": Facts(**ABOVE, threshold_source="uncalibrated", fix_list_item="calibration run queued for fingerprint 9e1f03"),
    "16": Facts(**ABOVE, manifest="undeclared"),
    "17": Facts(**dict(ABOVE, manifest_entry_ref=None), manifest="no_manifest_ui"),
    "18": Facts(**ABOVE, manifest="no_manifest_nonui"),
    "19": Facts(**DECLARED, rescued=True),
    "20": Facts(**DECLARED, review=answered("matches_manifest", 0.93)),
    "21": Facts(**DECLARED, review=answered("does_not_match_manifest", 0.91, defect_class="overlap")),
    "22": Facts(**DECLARED, review=answered("partially_matches_manifest", 0.88)),
    "23": Facts(**DECLARED, review=Review("pending")),
    "24": Facts(**REMOVED, review=answered("matches_manifest", 0.9)),
    "25": Facts(**REMOVED, review=Review("pending")),
    "25a": Facts(**REMOVED, review=answered("does_not_match_manifest", 0.95)),
    "26": Facts(base="removed", manifest="undeclared"),
}

EXPECTED = {   # row -> (outcome, reason, review_state)
    "1": ("env_error", "session_kicked", "none"),
    "1a": ("assertion_failed", "assertion:app_alive", "none"),
    "2": ("harness_error", "bounds_source_unavailable", "none"),
    "3": ("not_run", "step_failed:s04", "none"),
    "4": ("not_comparable", "fingerprint_mismatch", "none"),
    "5": ("not_settled", "frames_unstable", "none"),
    "6": ("landmark_missing", "landmark:lm.ladder-header", "none"),
    "7": ("invariant_violation", "INV-01", "none"),
    "8": ("assertion_failed", "assertion:qty", "none"),
    "9": ("needs_baseline", "new_surface", "none"),
    "9a": ("needs_human", "oracle_unreadable", "none"),
    "10": ("identical", None, "none"),
    "11": ("noise", "below_floor", "none"),
    "11a": ("needs_human", "rescued_capture", "none"),
    "12": ("not_settled", "unstable", "none"),
    "13": ("noise", "decision:dec-1", "none"),
    "14": ("intended", "decision:dec-2", "none"),
    "15": ("needs_human", "uncalibrated", "none"),
    "16": ("collateral", None, "none"),
    "17": ("collateral", "manifest_missing", "none"),
    "18": ("needs_human", "no_manifest", "none"),
    "19": ("needs_human", "rescued_capture", "none"),
    "20": ("intended", None, "done"),
    "21": ("needs_human", "reviewer_no_match", "done"),
    "22": ("needs_human", "reviewer_uncertain", "done"),
    "23": ("needs_human", "reviewer_unavailable", "pending"),
    "24": ("intended", None, "done"),
    "25": ("needs_human", "reviewer_unavailable", "pending"),
    "25a": ("needs_human", "reviewer_no_match", "done"),
    "26": ("collateral", None, "none"),
}


def record(c: Classified) -> CheckpointOutcome:
    return CheckpointOutcome(run_id="run-2026-11-09-pr-0931", checkpoint_id=CID, workspace_id="ws.tier-c.sweep-harness",
                             persona="active_trader", data_mode="frozen", **c.fields())


# --------------------------------------------------------------------------------------------
# The table is data, in spec order, exhaustive, every row reachable


def test_row_order_equals_spec():
    assert ROW_IDS == SPEC_ROW_ORDER
    assert set(ROW_FACTS) == set(ROW_IDS) == set(EXPECTED)


def _spec_rule_list():
    text = SPEC_03.read_text(encoding="utf-8")
    body = text[text.index("Rule list (first match wins"):]
    rows = []
    for line in body.splitlines()[1:]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] in ("#", "") or set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)
    return rows


@pytest.mark.skipif(not SPEC_03.exists(), reason="spec checkout not present")
def test_rules_equal_the_spec_table():
    """Every row's id, outcome and allowed input values equal 3.11's rule list."""
    spec = _spec_rule_list()
    assert [r[0] for r in spec] == list(ROW_IDS)
    letters = list(INPUT_VALUES)
    for cells, row in zip(spec, RULES):
        assert re.findall(r"`([^`]+)`", cells[-1])[0] == row.outcome, row.id
        for letter, cell in zip(letters, cells[1:11]):
            if cell == "*":
                assert letter not in row.when, (row.id, letter)
            else:
                assert row.when[letter] == frozenset(re.findall(r"`([^`]+)`", cell)), (row.id, letter)


def _inputs(**fixed):
    letters = list(INPUT_VALUES)
    pools = [fixed[k] if k in fixed else INPUT_VALUES[k] for k in letters]
    for combo in product(*pools):
        yield Inputs(**dict(zip(letters, combo)))


def test_table_is_exhaustive_every_row_reachable_and_reviewer_moves_nothing_else():
    """Every combination reaches exactly one row; every row is reached; outside rows 20 to 25a
    the verdict R never changes the row (3.16 item 3)."""
    fired = {match_row(Inputs(G=g, H="none", B="present", D="zero", S="stable", M="none", T="calibrated",
                              P="scripted", X="declared", R="pending")).id for g in G_ORDER if g != "ok"}
    for base in _inputs(G=("ok",), R=("pending",)):
        rows = {match_row(replace(base, R=r)).id for r in INPUT_VALUES["R"]}
        fired |= rows
        outside = rows - REVIEWER_ROWS
        assert not outside or len(rows) == 1, (base, rows)
    assert fired == set(ROW_IDS)


def test_reviewer_rows_only_give_intended_or_needs_human():
    """3.16 items 3 and 4: no verdict produces noise, identical, collateral or a capture outcome."""
    assert REVIEWER_ROWS == {"20", "21", "22", "23", "24", "25", "25a"}
    assert {r.outcome for r in RULES if r.id in REVIEWER_ROWS} == {"intended", "needs_human"}


def test_gate_precedence_follows_row_order():
    assert G_ORDER == ("env", "app", "harness", "not_run", "not_comparable", "not_settled", "landmark", "ok")


# --------------------------------------------------------------------------------------------
# One test per row


@pytest.mark.parametrize("row_id", SPEC_ROW_ORDER)
def test_each_row_fires(row_id):
    c = classify(ROW_FACTS[row_id])
    assert c.row == row_id
    assert (c.outcome, c.reason, c.review_state) == EXPECTED[row_id]
    assert c.exception is None
    assert required_field_errors(c) == []
    rec = record(c)                                    # the record foundation accepts the field set
    assert rec.outcome == c.outcome


def test_row_details():
    assert classify(ROW_FACTS["1"]).gate == "authenticated"
    app = classify(ROW_FACTS["1a"])
    assert app.findings == ("app_crash",) and app.detail == "cause: jvm_crash" and app.assertion_ids == ("app_alive",)
    assert classify(ROW_FACTS["2"]).detail == "watchdog_event: wd-0017"
    assert classify(ROW_FACTS["9"]).side == "base"
    assert classify(ROW_FACTS["11"]).classification == Classification("noise", "rule")
    assert classify(ROW_FACTS["13"]).decision_ref == "dec-1"
    assert classify(ROW_FACTS["14"]).manifest_entry_ref == "tos-48120-1"
    assert classify(ROW_FACTS["15"]).detail == "calibration run queued for fingerprint 9e1f03"
    c16 = classify(ROW_FACTS["16"])
    assert c16.classification.by == "rule" and c16.primitive == "FlexibleGrid"
    assert classify(ROW_FACTS["17"]).findings == ("manifest_missing",)
    c20 = classify(ROW_FACTS["20"])
    assert c20.classification.by == "reviewer" and c20.classification.confidence == 0.93
    assert "defect class: overlap" in classify(ROW_FACTS["21"]).detail
    assert "partially_matches_manifest" in classify(ROW_FACTS["22"]).detail


# --------------------------------------------------------------------------------------------
# Precedence (1.10, F27)


def test_env_error_outranks_app_failure():
    c = classify(Facts(env=EnvFailure("connectivity", "backend_degraded"), app=AppFailure("hang")))
    assert (c.row, c.outcome, c.gate) == ("1", "env_error", "connectivity")
    assert c.evidence == ("app failure (hang)",)


def test_app_failure_outranks_harness():
    c = classify(Facts(app=AppFailure("process_exit"), harness=HarnessFailure("exception:IOError", "wd-9")))
    assert (c.row, c.outcome, c.reason) == ("1a", "assertion_failed", "assertion:app_alive")
    assert "harness_error (exception:IOError)" in c.evidence


def test_full_gate_precedence():
    facts = Facts(env=EnvFailure("running", "occluded"), app=AppFailure("oom"),
                  harness=HarnessFailure("artifact_persist_failed", "wd-1"), not_run=Condition("budget"),
                  not_comparable=Condition("window_mismatch", detail="title"),
                  not_settled=Condition("timeout", side="candidate", detail="15 s"),
                  landmark=Condition("landmark:lm.x", side="candidate", detail="x"), invariant_ids=("INV-05",))
    order = []
    for field in ("env", "app", "harness", "not_run", "not_comparable", "not_settled", "landmark", "invariant_ids"):
        order.append(classify(facts).row)
        facts = replace(facts, **{field: () if field == "invariant_ids" else None})
    assert order == ["1", "1a", "2", "3", "4", "5", "6", "7"]


def test_invariant_violation_plus_reviewer_verdict_ends_in_row_7():
    """The verdict is attached as evidence and the Reviewer never moves it (3.11 notes, L-OUT-03)."""
    facts = Facts(**DECLARED, invariant_ids=("INV-03", "INV-01"), invariant_stability="stable",
                  review=answered("matches_manifest", 0.97))
    c = classify(facts)
    assert (c.row, c.outcome, c.reason, c.invariant_ids) == ("7", "invariant_violation", "INV-01", ("INV-01", "INV-03"))
    assert c.classification is None
    assert any("matches_manifest" in e for e in c.evidence)
    assert classify(replace(facts, review=answered("does_not_match_manifest", 0.97))).outcome == "invariant_violation"


def test_assertion_outranks_baseline_and_invariant_outranks_assertion():
    facts = Facts(base="absent", base_reason="new_surface", assertion_ids=("price",), assertion_detail="exp 10.00")
    assert classify(facts).row == "8"
    assert classify(replace(facts, invariant_ids=("INV-10",))).row == "7"     # still a product finding (row 9 note)


def test_invariant_not_reproduced_is_not_settled_unstable():
    c = classify(Facts(**ABOVE, invariant_ids=("INV-01",), invariant_stability="unstable"))
    assert (c.row, c.outcome, c.reason, c.stability, c.side) == ("5", "not_settled", "unstable", "unstable", "candidate")
    assert "INV-01" in c.detail


def test_lower_results_stay_as_evidence_on_row_7():
    """4.13 worked example: invariant_violation with a collateral classification by rule."""
    c = classify(Facts(**ABOVE, invariant_ids=("INV-01",), invariant_stability="stable"))
    assert c.classification == Classification("collateral", "rule")
    assert "row 16: collateral" in c.evidence
    assert record(c).classification.category == "collateral"


def test_waiver_never_changes_an_invariant_outcome():
    waiver = DecisionMatch(dec("dec-7", "mark_intended", "intended", ticket="TOS-1", entry="e-1"), 0, "pair-1")
    c = classify(Facts(invariant_ids=("INV-04",), invariant_stability="stable", decisions=(waiver,),
                       build_pair_id="pair-1"))
    assert (c.outcome, c.decision_ref) == ("invariant_violation", "dec-7")


# --------------------------------------------------------------------------------------------
# Inputs


@pytest.mark.parametrize("review,r", [
    (None, "pending"), (Review("pending"), "pending"), (Review("unavailable"), "unavailable"),
    (Review("capped", cause="call_cap"), "capped"), (answered("matches_manifest", 0.80), "match_hi"),
    (answered("matches_manifest", 0.79), "match_lo"), (answered("partially_matches_manifest", 0.99), "match_lo"),
    (answered("does_not_match_manifest", 0.85), "no_match"), (answered("does_not_match_manifest", 0.6), "unsure"),
    (answered("no_visible_difference", 0.9), "nothing"), (answered("cannot_judge", 0.9), "unsure"),
    (answered("not_applicable", 0.9), "unavailable"),
])
def test_reviewer_input(review, r):
    assert reviewer_input(review) == r


def test_decision_memory_scope_and_conflicts():
    out_of_scope = Facts(**ABOVE, decisions=(INTENDED,), build_pair_id="pair-new")
    assert derive_inputs(out_of_scope).M == "none" and classify(out_of_scope).row == "16"
    by_pair = replace(out_of_scope, build_pair_id="pair-old")
    assert classify(by_pair).row == "14"
    by_entry = replace(out_of_scope, manifest_entries=frozenset({"tos-48120-1"}))
    assert classify(by_entry).row == "14"
    conflict = classify(Facts(**ABOVE, decisions=(NOISE, BUG)))
    assert conflict.row == "16" and conflict.decision_ref is None
    assert any("conflicting decisions" in e for e in conflict.evidence)
    revoked = DecisionMatch(replace(NOISE.decision, status="revoked"), 0)
    assert classify(Facts(**ABOVE, decisions=(revoked,))).row == "16"


def test_nearest_then_newest_decision_is_inherited():
    near = DecisionMatch(dec("dec-near", "noise_signature", "noise", at="2026-10-01T00:00:00+00:00"), 1)
    new = DecisionMatch(dec("dec-new", "noise_signature", "noise", at="2026-11-05T00:00:00+00:00"), 1)
    far = DecisionMatch(dec("dec-far", "noise_signature", "noise", at="2026-11-09T00:00:00+00:00"), 5)
    assert classify(Facts(**ABOVE, decisions=(far, near, new))).decision_ref == "dec-new"


def test_confirmed_bug_keeps_the_computed_outcome():
    c = classify(Facts(**ABOVE, decisions=(BUG,)))
    assert (c.row, c.outcome, c.reason, c.decision_ref) == ("16", "collateral", "decision:dec-3", "dec-3")
    assert "known bug" in c.detail and "TOS-9" in c.detail
    h = classify(Facts(**DECLARED, decisions=(BUG,), review=answered("does_not_match_manifest", 0.95)))
    assert (h.row, h.reason, h.decision_ref) == ("21", "reviewer_no_match", "dec-3")


def test_rescued_capture_never_inherits_a_decision():
    c = classify(Facts(**DECLARED, rescued=True, decisions=(NOISE,)))
    assert (c.row, c.reason, c.decision_ref) == ("19", "rescued_capture", None)


def test_above_threshold_needs_stability():
    with pytest.raises(ValueError):
        classify(Facts(diff="above", manifest="undeclared"))
    unknown = classify(Facts(**dict(ABOVE, stability="unknown", stability_cause="budget")))
    assert (unknown.row, unknown.stability, unknown.stability_cause) == ("16", "unknown", "budget")


def test_facts_validation():
    with pytest.raises(ValueError):
        Facts(base="absent")
    with pytest.raises(ValueError):
        Facts(assertion_ids=("app_alive",))
    with pytest.raises(TaxonomyError):
        AppFailure("segfault")
    with pytest.raises(ValueError):
        Review("answered", verdict="matches_manifest")
    with pytest.raises(TaxonomyError):
        classify(Facts(env=EnvFailure("nowhere", "session_kicked")))


# --------------------------------------------------------------------------------------------
# needs_human reasons: fixed order, the rest in detail; the global-shift exception


def test_other_needs_human_reasons_go_in_detail():
    c = classify(Facts(**dict(ABOVE, manifest="declared"), threshold_source="uncalibrated", rescued=True,
                       bounds_unreadable=("rgn.ladder.bid-column",)))
    assert (c.row, c.reason) == ("15", "uncalibrated")
    assert c.detail.endswith("also: bounds_unreadable, rescued_capture")
    both = classify(Facts(oracle_unreadable=("price",), bounds_unreadable=("qty",)))
    assert (both.row, both.reason) == ("9a", "oracle_unreadable") and "also: bounds_unreadable" in both.detail
    unreadable_collateral = classify(Facts(**ABOVE, bounds_unreadable=("qty",)))
    assert unreadable_collateral.outcome == "collateral" and "also: bounds_unreadable" in unreadable_collateral.detail


def test_global_shift_replaces_rows_16_to_23():
    assert GLOBAL_SHIFT_ROWS == {str(n) for n in range(16, 24)}
    for row in sorted(GLOBAL_SHIFT_ROWS):
        c = classify(replace(ROW_FACTS[row], global_shift=True))
        assert (c.row, c.outcome, c.reason, c.exception) == (row, "needs_human", "global_shift", "global_shift"), row
        assert required_field_errors(c) == []
    pending = classify(replace(ROW_FACTS["23"], global_shift=True))
    assert pending.review_state == "pending"                     # 4.13: allowed with global_shift
    for row in ("13", "14", "15"):
        assert classify(replace(ROW_FACTS[row], global_shift=True)).exception is None
    late = apply_late_verdict(replace(ROW_FACTS["23"], global_shift=True), pending, answered("matches_manifest", 0.99))
    assert (late.result.reason, late.result.review_state) == ("global_shift", "done")


# --------------------------------------------------------------------------------------------
# Rows 23 and 25: review state and caps


def test_row_23_review_states_and_caps():
    gave_up = classify(replace(ROW_FACTS["23"], review=Review("unavailable", cause="batch expired")))
    assert (gave_up.reason, gave_up.review_state) == ("reviewer_unavailable", "done")
    excluded = classify(replace(ROW_FACTS["23"], review=Review("capped", cause="call_cap")))
    assert (excluded.reason, excluded.review_state) == ("reviewer_cap", "none")
    ceiling = classify(replace(ROW_FACTS["23"], review=Review("capped", cause="dollar_cap", queued=True)))
    assert (ceiling.reason, ceiling.review_state, ceiling.detail) == ("reviewer_cap", "done", "cause: dollar_cap")
    deferred = classify(replace(ROW_FACTS["23"], review=None))
    assert (deferred.reason, deferred.review_state) == ("reviewer_unavailable", "pending")


def test_row_25a_reasons():
    for review, reason in ((Review("unavailable"), "reviewer_unavailable"), (Review("capped"), "reviewer_cap"),
                           (answered("no_visible_difference", 0.9), "reviewer_no_match"),
                           (answered("matches_manifest", 0.5), "reviewer_no_match")):
        c = classify(replace(ROW_FACTS["25a"], review=review))
        assert (c.row, c.outcome, c.reason) == ("25a", "needs_human", reason)


# --------------------------------------------------------------------------------------------
# Late verdicts (F28) and the end of review (4.13)


def test_late_verdict_re_evaluates_a_pending_item():
    for facts, verdict, row, outcome, reason in (
        (ROW_FACTS["23"], answered("matches_manifest", 0.93), "20", "intended", None),
        (ROW_FACTS["23"], answered("does_not_match_manifest", 0.95), "21", "needs_human", "reviewer_no_match"),
        (ROW_FACTS["23"], answered("no_visible_difference", 0.95), "22", "needs_human", "reviewer_uncertain"),
        (ROW_FACTS["25"], answered("matches_manifest", 0.93), "24", "intended", None),
        (ROW_FACTS["25"], answered("partially_matches_manifest", 0.93), "25a", "needs_human", "reviewer_no_match"),
    ):
        current = classify(facts)
        assert current.review_state == "pending"
        late = apply_late_verdict(facts, current, verdict)
        assert not late.annotation_only
        assert (late.result.row, late.result.outcome, late.result.reason, late.result.review_state) == (
            row, outcome, reason, "done")


def test_late_verdict_never_gives_noise_or_collateral():
    current = classify(ROW_FACTS["23"])
    for verdict in ("matches_manifest", "partially_matches_manifest", "does_not_match_manifest",
                    "no_visible_difference", "cannot_judge", "not_applicable"):
        for conf in (0.1, 0.99):
            got = apply_late_verdict(ROW_FACTS["23"], current, answered(verdict, conf)).result
            assert got.outcome in ("intended", "needs_human")


def test_late_verdict_never_overrides_a_human_decision():
    facts, current = ROW_FACTS["23"], classify(ROW_FACTS["23"])
    human = dec("dec-9", "confirm_bug", "confirmed_bug", ticket="TOS-77")
    late = apply_late_verdict(facts, current, answered("matches_manifest", 0.97), human_decision=human)
    assert late.annotation_only and late.eval_dev_split                    # disagrees: joins the dev split
    assert (late.result.outcome, late.result.reason, late.result.detail) == (current.outcome, current.reason, current.detail)
    assert late.result.review_state == "done"
    agree = apply_late_verdict(facts, current, answered("does_not_match_manifest", 0.97), human_decision=human)
    assert agree.annotation_only and not agree.eval_dev_split


def test_late_verdict_on_a_settled_item_is_an_annotation():
    done = classify(ROW_FACTS["21"])
    late = apply_late_verdict(ROW_FACTS["21"], done, answered("matches_manifest", 0.99))
    assert late.annotation_only and late.result == done
    with pytest.raises(ValueError):
        apply_late_verdict(ROW_FACTS["23"], classify(ROW_FACTS["23"]), Review("pending"))


def test_close_pending():
    facts, current = ROW_FACTS["23"], classify(ROW_FACTS["23"])
    gave_up = close_pending(facts, current, cause="gave_up")
    assert (gave_up.outcome, gave_up.reason, gave_up.review_state) == ("needs_human", "reviewer_unavailable", "done")
    capped = close_pending(facts, current, cause="dollar_cap")
    assert (capped.reason, capped.review_state, capped.detail) == ("reviewer_cap", "done", "cause: dollar_cap")
    removed = close_pending(ROW_FACTS["25"], classify(ROW_FACTS["25"]), cause="gave_up")
    assert (removed.row, removed.reason, removed.review_state) == ("25a", "reviewer_unavailable", "done")
    with pytest.raises(ValueError):
        close_pending(facts, classify(ROW_FACTS["16"]), cause="gave_up")
    with pytest.raises(ValueError):
        close_pending(facts, current, cause="bored")


# --------------------------------------------------------------------------------------------
# What the Reviewer may never do (3.16), enforced by the result's own checks


def test_reviewer_classification_never_on_invariant_or_assertion():
    with pytest.raises(TaxonomyError):
        Classified(row="7", outcome="invariant_violation", reason="INV-01", invariant_ids=("INV-01",),
                   classification=Classification("collateral", "reviewer"))
    with pytest.raises(TaxonomyError):
        Classified(row="16", outcome="collateral", review_state="pending")


def test_collateral_is_decided_by_declaration_alone():
    """Rows 16 to 18 are final whatever the verdict; the Reviewer only adds a description."""
    for verdict in ("matches_manifest", "does_not_match_manifest", "no_visible_difference"):
        c = classify(replace(ROW_FACTS["16"], review=answered(verdict, 0.99, description="ladder overlaps strip")))
        assert (c.row, c.outcome, c.classification.by) == ("16", "collateral", "rule")
        assert c.classification.description == "ladder overlaps strip"
        removed_declared = classify(replace(ROW_FACTS["25a"], review=answered(verdict, 0.99)))
        assert removed_declared.outcome in ("intended", "needs_human")


def test_rule_classification_keeps_the_describing_reviewer_call():
    """A collateral by rule that the Reviewer described keeps the call's model id and prompt hash (9.2, 9.3)."""
    review = answered("does_not_match_manifest", 0.97, model_id="claude-x", prompt_hash="ab" * 32,
                      description="ladder overlaps strip", region_pointer="rgn.ladder.viewport")
    for row in ("16", "17", "18"):
        c = classify(replace(ROW_FACTS[row], review=review))
        assert (c.outcome, c.classification.by) == (EXPECTED[row][0], "rule")
        assert c.classification == Classification(c.classification.category, "rule", model_id="claude-x",
                                                  prompt_hash="ab" * 32, description="ladder overlaps strip",
                                                  region_pointer="rgn.ladder.viewport")
    undescribed = classify(ROW_FACTS["16"])
    assert undescribed.classification == Classification("collateral", "rule")   # no call: no provenance
    pending = classify(replace(ROW_FACTS["16"], review=Review("pending", model_id="claude-x")))
    assert pending.classification.model_id is None


# --------------------------------------------------------------------------------------------
# End to end: the worked example through the checker, the re-capture and the table


def test_worked_example_end_to_end():
    def ladder(header_h, strip_y, viewport_y):
        return inv.ScreenModel("A", (480, 360), (
            inv.UIComponent("frame", inv.Rect(0, 0, 480, 360), "panel"),
            inv.UIComponent("header", inv.Rect(0, 0, 480, header_h), "panel", parent="frame"),
            inv.UIComponent("order-strip", inv.Rect(0, strip_y, 480, 30), "panel", parent="frame"),
            inv.UIComponent("ladder-viewport", inv.Rect(0, viewport_y, 480, 352 - viewport_y), "viewport", parent="frame"),
        ))
    rules = inv.SurfaceRules("charts.flexible-grid.active-trader-cell", tier=1)
    base, cand = ladder(28, 28, 58), ladder(22, 22, 38)
    comparison = inv.compare((base,) * 3, (cand,) * 3, rules)
    persistence = inv.confirm(comparison, inv.check_capture((cand,) * 3, rules, reference=base))
    facts = Facts(**ABOVE, manifest="undeclared", invariant_ids=persistence.invariant_ids,
                  invariant_stability=persistence.stability, bounds_unreadable=comparison.unreadable)
    c = classify(facts)
    assert (c.row, c.outcome, c.reason, c.stability) == ("7", "invariant_violation", "INV-01", "stable")
    assert c.classification == Classification("collateral", "rule")
    rec = record(c)
    assert rec.invariant_ids == ["INV-01"] and rec.primitive == "FlexibleGrid"
