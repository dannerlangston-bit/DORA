"""The Reviewer's eval set, metrics and gates, weekly sampling and the drift switch (sentinel.reviewer.evals; 3.14,
3.15, 10.4, 4.16). Synthetic items and recorded or faked verdicts only: no network."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from test_reviewer import MODEL, FakeTransport, answer, config, resp

from sentinel import canary
from sentinel.change_control import ReviewerTuple, tuple_in_force
from sentinel.classify import Facts, classify
from sentinel.demo import EXAMPLE
from sentinel.reviewer import ReviewerJob
from sentinel.reviewer import evals as E
from sentinel.reviewer.evals import EvalItem, EvalSetError, Prediction
from sentinel.reviewer.prompt import ManifestEntryRef
from sentinel.reviewer.schema import TASK_A, TASK_B
from sentinel.store import Store

S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
LABEL = {"mutant": "does_not_match_manifest", "aa": "no_visible_difference", "real_intended": "matches_manifest",
         "wrong_manifest": "does_not_match_manifest", "real_collateral": "collateral"}
RIGHT = {"mutant": "does_not_match_manifest", "aa": "no_visible_difference", "real_intended": "matches_manifest",
         "wrong_manifest": "does_not_match_manifest", "real_collateral": "does_not_match_manifest"}


def item(item_id: str, source: str, *, tier: int = 1, money: bool = False, defect: str | None = None, **kw) -> EvalItem:
    task = TASK_B if source == "real_collateral" else TASK_A
    labelers = ("ann", "bo") if source in E.REAL_SOURCES else ("generator",)
    if defect is None and (source == "mutant" or source == "real_collateral"):
        defect = "text_change"
    return EvalItem(item_id, source, task, kw.pop("label", LABEL[source]), tier, kw.pop("labelers", labelers),
                    defect_class=defect, money_cell=money, **kw)


def synthetic_set(n: int = 400) -> list[EvalItem]:
    """n items with the 3.14 shares, half of them tier 1."""
    out = []
    for src, share in E.SOURCES.items():
        out += [item(f"{src}-{k:03d}", src, tier=1 if k % 2 == 0 else 2, money=(src == "mutant" and k % 10 == 0))
                for k in range(round(share * n))]
    return out


def perfect(items, conf: float = 0.97) -> dict[str, Prediction]:
    return {i.item_id: Prediction(RIGHT[i.source], conf, i.defect_class) for i in items}


# -- items and composition (3.14, 10.4) -----------------------------------------------------------------------------

def test_labels_parse_and_round_trip():
    items = synthetic_set(40)
    text = "\n".join(json.dumps(i.doc()) for i in items) + "\n"
    assert E.parse_labels(text) == items


@pytest.mark.parametrize("change, message", [
    ({"source": "invented"}, "source"),
    ({"label": "matches_manifest"}, "labeled one of"),
    ({"source": "real_intended", "label": "matches_manifest", "labelers": ["ann"]}, "two people"),
    ({"hard": True}, "adjudicated"),
    ({"split": "held_out"}, "only `development`"),
    ({"tier": 4}, "tier"),
    ({"defect_class": "smudge"}, "defect class"),
    ({"extra": 1}, "unknown keys"),
])
def test_an_item_that_breaks_the_rules_is_refused(change, message):
    raw = {**item("m-1", "mutant").doc(), **change}
    with pytest.raises(EvalSetError, match=message):
        E.parse_labels(json.dumps(raw))


def test_a_line_that_is_not_an_object_or_not_json_is_refused():
    with pytest.raises(EvalSetError, match="line 1: not a JSON object"):
        E.parse_labels("[1, 2]")
    with pytest.raises(EvalSetError, match="line 2: not JSON"):
        E.parse_labels(json.dumps(item("m-1", "mutant").doc()) + "\n{nope\n")


def test_recorded_verdicts_outside_the_schema_range_are_refused(tmp_path):
    p = tmp_path / "v.jsonl"
    p.write_text(json.dumps({"item_id": "m-1", "verdict": "matches_manifest", "confidence": 1.5}) + "\n")
    with pytest.raises(EvalSetError, match="line 1"):
        E.read_predictions(p)


def test_duplicate_ids_are_refused():
    line = json.dumps(item("m-1", "mutant").doc())
    with pytest.raises(EvalSetError, match="duplicate"):
        E.parse_labels(f"{line}\n{line}\n")


def test_composition_reports_size_shares_and_tier_one():
    assert E.composition(synthetic_set(400)) == []
    skewed = [i for i in synthetic_set(400) if i.source != "aa"]
    notes = E.composition(skewed)
    assert any("source aa: 0%" in n for n in notes)
    low_tier = [EvalItem(i.item_id, i.source, i.task, i.label, 3, i.labelers, i.defect_class) for i in synthetic_set(200)]
    notes = E.composition(low_tier)
    assert any("200 items" in n for n in notes) and any("tier-1 items are 0%" in n for n in notes)


# -- split (3.14, choice 1) -----------------------------------------------------------------------------------------

def test_the_split_is_seeded_stable_and_about_seventy_thirty():
    items = synthetic_set(400) + [item(f"extra-{k}", "aa") for k in range(600)]
    a, b = E.split(items), E.split(items)
    assert a == b
    share = len(a.development) / len(items)
    assert 0.66 < share < 0.74
    assert E.split(items, seed=7) != a
    grown = E.split(items + [item(f"new-{k}", "aa") for k in range(50)])
    assert set(a.held_out) <= set(grown.held_out)              # the set grows; nothing moves between splits


def test_a_weekly_addition_is_always_development():
    held = next(i for i in synthetic_set(400) if E.split_of(i) == E.HELD_OUT)
    forced = EvalItem(held.item_id, held.source, held.task, held.label, held.tier, held.labelers, held.defect_class,
                      split=E.DEVELOPMENT)
    assert E.split_of(forced) == E.DEVELOPMENT


def test_the_nightly_subset_is_thirty_fixed_held_out_items():
    held = E.split(synthetic_set(400)).held_out
    sub = E.nightly_subset(held)
    assert len(sub) == 30 and set(sub) <= set(held) and sub == E.nightly_subset(held)


# -- metrics (3.14) -------------------------------------------------------------------------------------------------

FIXTURE = [item("m1", "mutant", money=True), item("m2", "mutant", money=True), item("m3", "mutant", tier=2),
           item("a1", "aa"), item("a2", "aa"), item("r1", "real_intended"), item("r2", "real_intended"),
           item("r3", "real_intended"), item("w1", "wrong_manifest"), item("w2", "wrong_manifest"),
           item("c1", "real_collateral", defect="overlap"), item("c2", "real_collateral", defect="shifted")]
PREDS = {"m1": Prediction("does_not_match_manifest", 0.90), "m2": Prediction("matches_manifest", 0.85),
         "m3": Prediction("does_not_match_manifest", 0.60), "a1": Prediction("no_visible_difference", 0.95),
         "a2": Prediction("does_not_match_manifest", 0.55), "r1": Prediction("matches_manifest", 0.97),
         "r2": Prediction("matches_manifest", 0.70), "r3": None, "w1": Prediction("matches_manifest", 0.92),
         "w2": Prediction("does_not_match_manifest", 0.88),
         "c1": Prediction("does_not_match_manifest", 0.80, "overlap"),
         "c2": Prediction("does_not_match_manifest", 0.80, "text_change")}


def test_metrics_match_a_hand_computed_fixture():
    m = E.compute_metrics(FIXTURE, PREDS, 0.80)
    pv = m.per_verdict
    assert (pv["matches_manifest"]["precision"], pv["matches_manifest"]["recall"]) == (0.5, pytest.approx(2 / 3))
    assert (pv["does_not_match_manifest"]["precision"], pv["does_not_match_manifest"]["recall"]) == (0.75, 0.6)
    assert (pv["no_visible_difference"]["precision"], pv["no_visible_difference"]["recall"]) == (1.0, 0.5)
    assert pv["cannot_judge"] == {"support": 0, "predicted": 0, "precision": None, "recall": None}
    assert (m.matches_precision, m.matches_recall) == (pytest.approx(1 / 3), pytest.approx(1 / 3))
    assert (m.tier1_mutant_recall, m.dnm_recall) == (0.5, 0.6)
    assert (m.hallucination_rate, m.rubber_stamp_rate, m.defect_class_accuracy) == (0.5, 0.5, 0.5)
    assert m.money_cell_misses == ("m2",) and m.unanswered == 1
    # ECE, 10 bins over the nine answered manifest_match items: (.55 + .4 + .3 + 2 x .365 + 4 x .185) / 9
    assert m.ece == pytest.approx(2.72 / 9)
    bins = {tuple(b["bin"]): b for b in m.reliability}
    assert bins[(0.8, 0.9)]["count"] == 2 and bins[(0.8, 0.9)]["accuracy"] == 0.5
    assert bins[(0.9, 1.0)]["confidence"] == pytest.approx(0.935)


def test_the_routing_threshold_is_the_smallest_confidence_reaching_095():
    dev = [item(f"d{k}", "real_intended" if ok else "wrong_manifest") for k, ok in enumerate([0, 1, 0, 1, 1, 1])]
    confs = [0.60, 0.75, 0.80, 0.85, 0.90, 0.95]
    preds = {i.item_id: Prediction("matches_manifest", c) for i, c in zip(dev, confs)}
    preds["x"] = Prediction("does_not_match_manifest", 0.99)
    assert E.routing_threshold(dev, preds) == 0.85
    assert E.routing_threshold(dev[:1], preds) is None


def test_the_gates_flag_each_threshold():
    base = E.compute_metrics(synthetic_set(400), perfect(synthetic_set(400)), 0.80)
    assert all(g.passed for g in E.gates(base))
    worse = {"matches_precision": 0.949, "matches_recall": 0.79, "tier1_mutant_recall": 0.979, "dnm_recall": 0.949,
             "hallucination_rate": 0.021, "rubber_stamp_rate": 0.031, "ece": 0.081, "money_cell_misses": ("m",)}
    names = dict(zip(worse, [g.name for g in E.GATES]))
    import dataclasses
    for field, value in worse.items():
        failed = [g.gate for g in E.gates(dataclasses.replace(base, **{field: value})) if not g.passed]
        assert failed == [names[field]], field
    edge = dataclasses.replace(base, matches_precision=0.95, matches_recall=0.80, tier1_mutant_recall=0.98,
                               hallucination_rate=0.02, rubber_stamp_rate=0.03, ece=0.08)
    assert all(g.passed for g in E.gates(edge))
    undefined = {g.gate: g for g in E.gates(dataclasses.replace(base, tier1_mutant_recall=None, ece=None))}
    assert not undefined["tier1_mutant_recall"].passed and undefined["tier1_mutant_recall"].value == 0.0
    assert not undefined["expected_calibration_error"].passed and undefined["expected_calibration_error"].value == 1.0


def _gate_list(**values) -> list[E.GateResult]:
    good = {"matches_manifest_precision": 0.97, "matches_manifest_recall": 0.9, "tier1_mutant_recall": 1.0,
            "does_not_match_manifest_recall": 0.99, "hallucination_rate": 0.0, "rubber_stamp_rate": 0.0,
            "expected_calibration_error": 0.03, "money_cell_mutant_misses": 0.0}
    good.update(values)
    return [E.GateResult(g.name, good[g.name], g.threshold, g.passes(good[g.name])) for g in E.GATES]


def test_the_change_rule_blocks_drops_while_the_gates_hold():
    prev = _gate_list(tier1_mutant_recall=1.0, matches_manifest_precision=0.99)
    assert E.change_rule(_gate_list(tier1_mutant_recall=0.99), prev) == []                 # 1 point: allowed
    blocked = E.change_rule(_gate_list(tier1_mutant_recall=0.985), prev)                  # 1.5 points: blocked
    assert len(blocked) == 1 and "tier1_mutant_recall drops 1.5 points" in blocked[0]
    assert all(g.passed for g in _gate_list(tier1_mutant_recall=0.985))
    assert "matches_manifest_precision drops 2.5 points" in E.change_rule(_gate_list(matches_manifest_precision=0.965),
                                                                          prev)[0]
    assert E.change_rule(_gate_list(matches_manifest_precision=0.97), prev) == []          # 2 points: allowed
    missed = E.change_rule(_gate_list(money_cell_mutant_misses=1.0))
    assert missed == ["gate money_cell_mutant_misses fails: 1 (needs <= 0)"]


# -- eval reports (4.16, L-XREF-29) --------------------------------------------------------------------------------

def _tuple() -> ReviewerTuple:
    return ReviewerTuple(MODEL, "sha256:" + "a" * 64, 1, (), "high", {"effort": "high"}, "crop-1", "redaction-1-x")


def test_a_report_carries_every_gate_and_its_problems_are_found():
    t = _tuple()
    doc = E.report_doc(t, _gate_list(), eval_set_version="v1", run_at="2026-11-10T08:00:00+00:00")
    rel = E.report_path(t)
    assert rel == f"eval/reports/{t.hex}.yaml" and E.report_problems(doc, t, rel) == [] and E.report_passes(doc)
    tampered = json.loads(json.dumps(doc))
    tampered["gates"][2]["value"] = 0.9
    assert any("says passed: True" in p for p in E.report_problems(tampered, t, rel))
    lowered = json.loads(json.dumps(doc))
    lowered["gates"][0]["threshold"] = 0.5
    assert any("not the 3.14 value 0.95" in p for p in E.report_problems(lowered, t, rel))
    short = {**doc, "gates": doc["gates"][:-1]}
    assert "gate money_cell_mutant_misses is missing (every gate of 3.14)" in E.report_problems(short, t, rel)
    other = ReviewerTuple(MODEL, t.prompt_sha256, 1, ("dev-a-1",), "high", {"effort": "high"}, "crop-1", "redaction-1-x")
    assert any("differs in few_shot_ids" in p for p in E.report_problems(doc, other))
    assert any("file name" in p for p in E.report_problems(doc, t, "eval/reports/0000.yaml"))
    forged = {**doc, "reviewer_version_sha256": "sha256:" + "0" * 64}
    assert any("is not the hash of its reviewer_version" in p for p in E.report_problems(forged))


def test_record_writes_a_reviewer_evals_row(tmp_path):
    m = E.compute_metrics(FIXTURE, PREDS, 0.80)
    with Store(tmp_path / "s.db") as store:
        E.record(store, _tuple(), m, E.gates(m), eval_set_version="v1", kind="held_out",
                 run_at="2026-11-10T08:00:00+00:00")
        (row,) = store.query("SELECT * FROM reviewer_evals")
    assert (row["eval_set_version"], row["model_id"], row["items"], row["passed_gate"]) == ("v1", MODEL, 12, 0)
    doc = json.loads(row["metrics_json"])
    assert doc["kind"] == "held_out" and doc["money_cell_misses"] == ["m2"] and len(doc["gates"]) == len(E.GATES)
    with pytest.raises(ValueError):
        E.record(Store(tmp_path / "t.db"), _tuple(), m, E.gates(m), eval_set_version="v1", kind="daily")


def test_the_threshold_in_force_is_the_last_passing_evaluations_else_080(tmp_path):
    t = _tuple()
    good = E.compute_metrics(synthetic_set(400), perfect(synthetic_set(400)), 0.86)
    bad = E.compute_metrics(FIXTURE, PREDS, 0.70)
    with Store(tmp_path / "s.db") as store:
        assert E.routing_threshold_in_force(store, t.sha256) == 0.80
        E.record(store, t, good, E.gates(good), eval_set_version="v1", kind="held_out",
                 run_at="2026-11-09T08:00:00+00:00")
        E.record(store, t, bad, E.gates(bad), eval_set_version="v1", kind="weekly", run_at="2026-11-10T08:00:00+00:00")
        assert E.routing_threshold_in_force(store, t.sha256) == 0.86      # a failing evaluation never sets it
        assert E.routing_threshold_in_force(store, "sha256:" + "f" * 64) == 0.80
        assert E.routing_threshold_in_force(store, None) == 0.80


# -- the drift switch (3.14, 10.4) ----------------------------------------------------------------------------------

def _night(store: Store, ok: bool, day: str, kind: str = "nightly_subset") -> E.DriftResult:
    m = E.compute_metrics(FIXTURE if not ok else synthetic_set(40), PREDS if not ok else perfect(synthetic_set(40)),
                          0.8)
    at = f"{day}T08:00:00+00:00"
    E.record(store, _tuple(), m, E.gates(m), eval_set_version="v1", kind=kind, run_at=at)
    return E.drift_check(store, _tuple().sha256, date=day, at=at)


def test_two_failing_nights_disable_the_reviewer_and_row_23_goes_to_a_human(tmp_path):
    with Store(tmp_path / "s.db") as store:
        assert not _night(store, False, "2026-11-08").disabled          # one bad batch is not drift
        assert E.disabled_review(store) is None
        d = _night(store, False, "2026-11-09")
        assert (d.disabled, d.newly, d.since, d.gate) == (True, True, "2026-11-09", "matches_manifest_precision")
        assert d.alert is not None and d.alert.rule == "reviewer_unavailable"
        assert E.reviewer_disabled(store)["since"] == "2026-11-09"
        again = _night(store, True, "2026-11-10")                       # stays disabled until a passing report
        assert again.disabled and not again.newly
        review = E.disabled_review(store)
        c = classify(Facts(diff="above", stability="stable", manifest="declared", primitive="FlexibleGrid",
                           manifest_entry_ref="tos-1", review=review))
        assert (c.outcome, c.reason) == ("needs_human", "reviewer_unavailable")


def test_a_passing_night_between_failures_keeps_the_reviewer_on(tmp_path):
    with Store(tmp_path / "s.db") as store:
        _night(store, False, "2026-11-08")
        _night(store, True, "2026-11-09")
        assert not _night(store, False, "2026-11-10").disabled


def test_a_failing_weekly_evaluation_disables_at_once(tmp_path):
    with Store(tmp_path / "s.db") as store:
        assert _night(store, False, "2026-11-08", kind="weekly").newly


def test_only_a_later_passing_report_re_enables(tmp_path):
    t = _tuple()
    with Store(tmp_path / "s.db") as store:
        _night(store, False, "2026-11-08")
        _night(store, False, "2026-11-09")
        old = E.report_doc(t, _gate_list(), eval_set_version="v1", run_at="2026-11-01T08:00:00+00:00")
        failing = E.report_doc(t, _gate_list(hallucination_rate=0.5), eval_set_version="v1",
                               run_at="2026-11-12T08:00:00+00:00")
        assert not E.reenable(store, old) and not E.reenable(store, failing)
        new = {**old, "run_at": "2026-11-12T08:00:00+00:00"}
        assert E.reenable(store, new, at="2026-11-12T09:00:00+00:00")
        assert E.reviewer_disabled(store) is None and E.disabled_review(store) is None
        assert not _night(store, False, "2026-11-13").disabled          # old failures before re-enable do not count


# -- eval mutants (3.14 row 1) --------------------------------------------------------------------------------------

BUTTON, BELOW, DIGITS, CELL, HDR_A, HDR_B = ((20, 20, 60, 24), (20, 50, 60, 16), (100, 40, 90, 30), (20, 80, 40, 20),
                                            (100, 6, 40, 18), (150, 6, 40, 18))
ENTRIES = [ManifestEntryRef("tos-1", ("TOS-1",), "Ticket header", ("dialog.order-confirmation",), "x"),
           ManifestEntryRef("tos-2", ("TOS-2",), "Chart period", ("charts.flexible-grid.chart-cell",), "y")]


def frame() -> np.ndarray:
    img = np.full((120, 220, 3), 245, np.uint8)
    cv2.rectangle(img, (20, 20), (79, 43), (40, 60, 160), -1)
    cv2.rectangle(img, (20, 50), (79, 65), (120, 120, 120), -1)
    for k, ch in enumerate("1847"):
        cv2.putText(img, ch, (104 + 20 * k, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_8)
    cv2.rectangle(img, (20, 80), (59, 99), (220, 30, 30), -1)
    cv2.putText(img, "Bid", (104, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_8)
    cv2.putText(img, "Ask", (154, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_8)
    return img


def test_the_eval_stream_differs_from_the_canary_stream():
    a = E.mutant_rng(12).integers(1 << 30, size=4)
    assert not np.array_equal(a, canary.mutant_rng(canary.SEED, "M12").integers(1 << 30, size=4))
    assert np.array_equal(a, E.mutant_rng(12).integers(1 << 30, size=4))


@pytest.mark.parametrize("op, box, neighbor", [("shift", BUTTON, None), ("erase", BUTTON, None),
                                               ("digit", DIGITS, None), ("clip", HDR_A, None),
                                               ("overlap", BUTTON, BELOW), ("hue", CELL, None),
                                               ("swap_headers", HDR_A, HDR_B)])
def test_each_mutation_makes_a_reviewable_mutant(op, box, neighbor):
    base = frame()
    m = E.eval_mutant(base, op=op, box=box, neighbor=neighbor, index=7, checkpoint_id=S07,
                      surface_id="dialog.order-confirmation", region="rgn.confirm.send", tier=1, entries=ENTRIES,
                      workspace_id="ws.tier-a.active-trader", data_epoch="ep-1")
    assert (m.item.label, m.item.defect_class, m.item.source) == ("does_not_match_manifest", E.MUTATIONS[op], "mutant")
    assert m.item.inputs["entry"]["entry_id"] == "tos-2"                # never an entry declaring the surface
    assert m.item.inputs["mutation"] == {"op": op, "seed": canary.SEED, "stream": E.EVAL_STREAM, "index": 7}
    changed = np.argwhere((m.frame != base).any(axis=2))
    assert changed.size
    again = E.eval_mutant(base, op=op, box=box, neighbor=neighbor, index=7, checkpoint_id=S07,
                          surface_id="dialog.order-confirmation", region="rgn.confirm.send", tier=1, entries=ENTRIES,
                          workspace_id="ws.tier-a.active-trader", data_epoch="ep-1")
    assert canary.image_sha256(again.frame) == canary.image_sha256(m.frame)


def test_an_erase_changes_only_its_box():
    base = frame()
    out = E.mutate(base, "erase", BUTTON, E.mutant_rng(1))
    ys, xs = np.nonzero((out != base).any(axis=2))
    x, y, w, h = BUTTON
    assert xs.min() >= x and xs.max() < x + w and ys.min() >= y and ys.max() < y + h


def test_a_paired_mutation_needs_its_neighbor_and_an_unrelated_entry_must_exist():
    with pytest.raises(canary.GenerationError, match="neighbor"):
        E.mutate(frame(), "overlap", BUTTON, E.mutant_rng(1))
    with pytest.raises(canary.GenerationError, match="unrelated"):
        E.unrelated_entry(ENTRIES[:1], "dialog.order-confirmation", E.mutant_rng(1))


def test_predict_runs_the_job_over_stored_items(tmp_path):
    base = frame()
    items = []
    for k, (op, box, nb) in enumerate((("erase", BUTTON, None), ("hue", CELL, None))):
        m = E.eval_mutant(base, op=op, box=box, neighbor=nb, index=k, checkpoint_id=S07,
                          surface_id="dialog.order-confirmation", region="rgn.confirm.send", tier=1, entries=ENTRIES,
                          workspace_id="ws.tier-a.active-trader", data_epoch="ep-1")
        for img in m.crops.values():
            E.write_blob(tmp_path, img)
        items.append(m.item)
    ri = E.review_item(items[0], tmp_path)
    assert ri.queue_key == items[0].item_id and ri.entry.entry_id == "tos-2" and ri.diff.changed_px > 0
    fake = FakeTransport(respond=lambda params: resp(answer(TASK_A, "does_not_match_manifest", 0.91,
                                                            change="missing_element")))
    job = ReviewerJob(config(), fake, "pr")
    preds = E.predict(job, items, tmp_path)
    assert preds == {i.item_id: Prediction("does_not_match_manifest", 0.91, "missing_element") for i in items}
    assert len(fake.calls) == 2


def test_a_blob_that_does_not_hash_to_its_name_is_refused(tmp_path):
    sha = E.write_blob(tmp_path, frame())
    (tmp_path / f"{sha}.png").rename(tmp_path / f"{'0' * 64}.png")
    with pytest.raises(EvalSetError, match="does not hash"):
        E.read_blob(tmp_path, "0" * 64)


# -- weekly sampling and override rates (3.15) ----------------------------------------------------------------------

def v(rid: int, verdict: str, conf: float, mapped: str, at: str = "2026-11-03T10:00:00+00:00") -> E.VerdictRow:
    return E.VerdictRow(rid, "run-1", f"census/s{rid}/ws.tier-a.active-trader", verdict, conf, mapped, at)


def d(did: int, rid: int, verdict: str, at: str = "2026-11-04T10:00:00+00:00") -> E.HumanDecision:
    return E.HumanDecision(did, "run-1", f"census/s{rid}/ws.tier-a.active-trader", verdict, at)


WEEK_V = ([v(1, "matches_manifest", 0.93, "intended"), v(2, "does_not_match_manifest", 0.9, "needs_human"),
           v(3, "no_visible_difference", 0.9, "noise", at="2026-11-05T10:00:00+00:00"),
           v(44, "matches_manifest", 0.6, "needs_human")]
          + [v(100 + k, "matches_manifest", 0.95, "intended") for k in range(40)])
WEEK_D = [d(1, 1, "confirmed_bug"), d(2, 2, "confirmed_bug"), d(3, 3, "intended", at="2026-11-04T09:00:00+00:00"),
          d(4, 44, "intended")]


def test_the_weekly_sample_takes_overturned_late_and_five_percent_of_intended():
    picked = E.weekly_dev_sample(WEEK_V, WEEK_D, week="2026-W45")
    reasons = [(c.review.review_id, c.reason) for c in picked]
    assert reasons[:3] == [(1, "overturned"), (3, "late_disagrees"), (44, "overturned")]
    sampled = [c for c in picked if c.reason == "intended_sample"]
    assert len(sampled) == math.ceil(0.05 * 40) and all(c.review.review_id >= 100 for c in sampled)
    assert picked == E.weekly_dev_sample(WEEK_V, WEEK_D, week="2026-W45")
    assert picked[0].doc("2026-W45")["decision_verdict"] == "confirmed_bug"


def test_the_override_rate_is_per_verdict():
    rates = E.override_rates(WEEK_V, WEEK_D)
    assert rates["matches_manifest"] == {"decided": 2, "overturned": 2, "rate": 1.0}
    assert rates["does_not_match_manifest"] == {"decided": 1, "overturned": 0, "rate": 0.0}
    assert rates["no_visible_difference"]["rate"] == 1.0


def test_week_rows_reads_reviews_and_human_decisions_and_skips_nightly_runs(tmp_path):
    from sentinel.store import RunPlan, ScopeEntry
    cps = [f"census/s{k}/ws.tier-a.active-trader" for k in (1, 2)]
    with Store(tmp_path / "s.db") as store:
        for run_id, kind in (("run-1", "pr"), ("run-n", "nightly")):
            store.begin_run(RunPlan(run_id, kind, "c0ffee", "a" * 64, "b" * 64, "d00d", "auth/v1.md", 2700,
                                    tuple(ScopeEntry(c, "ws.tier-a.active-trader", "active_trader", 2, "frozen")
                                          for c in cps)))
        for rid, (run_id, cp, verdict, mapped) in enumerate((("run-1", cps[0], "matches_manifest", "intended"),
                                                              ("run-1", cps[1], "does_not_match_manifest", "needs_human"),
                                                              ("run-n", cps[0], "matches_manifest", "intended")), 1):
            store.insert("reviews", {"review_id": rid, "run_id": run_id, "checkpoint_id": cp, "diff_hash": f"h{rid}",
                                     "model_id": MODEL, "prompt_hash": "p", "request_sha256": "r", "response_json": "{}",
                                     "verdict": verdict, "mapped_outcome": mapped, "confidence": 0.93,
                                     "created_at": "2026-11-03T10:00:00+00:00"})
        store.insert("decisions", {"checkpoint_id": cps[0], "region_id": "rgn.x", "phash": bytes(8),
                                   "category": "intended", "action": "confirm_bug", "verdict": "confirmed_bug",
                                   "source": "report_ui", "decided_by": "jdoe", "decided_at": "2026-11-04T10:00:00+00:00",
                                   "run_id": "run-1", "reason": "the button must stay blue"})
        verdicts, decisions = E.week_rows(store, "2026-11-02T00:00:00+00:00", "2026-11-09T00:00:00+00:00")
    assert [x.review_id for x in verdicts] == [1, 2]
    assert [(x.checkpoint_id, x.verdict) for x in decisions] == [(cps[0], "confirmed_bug")]
    assert [c.reason for c in E.weekly_dev_sample(verdicts, decisions, week="w")] == ["overturned"]


# -- the CLIs: sentinel eval and sentinel eval-gate (10.4) ----------------------------------------------------------

@pytest.fixture(scope="module")
def pinned(tmp_path_factory) -> Path:
    """The example config with a real model pinned (the stand-in has no eval)."""
    root = tmp_path_factory.mktemp("cfg") / "config"
    shutil.copytree(EXAMPLE, root)
    p = root / "phase0" / "branches.yaml"
    p.write_text(p.read_text().replace("simulated-reviewer-offline", MODEL))
    return root


def _write_set(tmp: Path, items, preds) -> tuple[Path, Path]:
    labels, verdicts = tmp / "labels.jsonl", tmp / "verdicts.jsonl"
    labels.write_text("".join(json.dumps(i.doc()) + "\n" for i in items))
    verdicts.write_text("".join(json.dumps({"item_id": k, "verdict": p.verdict, "confidence": p.confidence,
                                            "defect_class": p.defect_class}) + "\n" for k, p in preds.items()))
    return labels, verdicts


def test_sentinel_eval_writes_the_report_for_the_tuple_in_force(tmp_path, pinned, capsys):
    items = synthetic_set(400)
    labels, verdicts = _write_set(tmp_path, items, perfect(items))
    rc = E.main([str(labels), "--config", str(pinned), "--verdicts", str(verdicts), "--out", str(tmp_path / "out"),
                 "--eval-set-version", "v1", "--run-at", "2026-11-10T08:00:00+00:00", "--db", str(tmp_path / "s.db")])
    assert rc == 0
    t = tuple_in_force(pinned)
    doc = yaml.safe_load((tmp_path / "out" / E.report_path(t)).read_text())
    assert doc["reviewer_version_sha256"] == t.sha256 and doc["split"] == "held_out" and E.report_problems(doc, t) == []
    metrics = json.loads(next((tmp_path / "out" / "eval" / "reports").glob("*.held_out.metrics.json")).read_text())
    assert metrics["routing_threshold"] == 0.97 and metrics["routing_threshold_derived"]
    held = E.split(items).held_out
    assert metrics["items"] == len(held)
    assert "PASS matches_manifest_precision" in capsys.readouterr().out
    with Store(tmp_path / "s.db") as store:
        assert store.query("SELECT passed_gate FROM reviewer_evals")[0][0] == 1


def test_sentinel_eval_reads_the_threshold_off_the_development_split_only(tmp_path, pinned):
    items = synthetic_set(400)
    preds = perfect(items)
    sp = E.split(items)
    for i in sp.held_out:                       # the held-out answers change; the threshold must not
        if i.source == "real_intended":
            preds[i.item_id] = Prediction("matches_manifest", 0.99)
    labels, verdicts = _write_set(tmp_path, items, preds)
    E.main([str(labels), "--config", str(pinned), "--verdicts", str(verdicts), "--out", str(tmp_path / "out")])
    metrics = json.loads(next((tmp_path / "out" / "eval" / "reports").glob("*.held_out.metrics.json")).read_text())
    assert metrics["routing_threshold"] == 0.97


def test_sentinel_eval_fails_on_a_gate_and_nightly_drift_disables_the_reviewer(tmp_path, pinned):
    items = synthetic_set(400)
    preds = perfect(items)
    for i in items:
        if i.source == "wrong_manifest":
            preds[i.item_id] = Prediction("matches_manifest", 0.97)          # rubber stamps
    labels, verdicts = _write_set(tmp_path, items, preds)
    db = str(tmp_path / "s.db")
    for day in ("2026-11-09", "2026-11-10"):
        rc = E.main([str(labels), "--config", str(pinned), "--verdicts", str(verdicts), "--nightly", "--threshold",
                     "0.9", "--db", db, "--run-at", f"{day}T08:00:00+00:00", "--out", str(tmp_path / day)])
        assert rc == 1
    with Store(db) as store:
        state = E.reviewer_disabled(store)
    assert state["since"] == "2026-11-10" and state["gate"] in {g.name for g in E.GATES}
    assert not list((tmp_path / "2026-11-10" / "eval" / "reports").glob("*.yaml"))   # no eval report for a subset


def test_sentinel_eval_refuses_a_config_without_a_tuple_in_force(tmp_path, capsys):
    labels, verdicts = _write_set(tmp_path, synthetic_set(40), {})
    assert E.main([str(labels), "--config", str(EXAMPLE), "--verdicts", str(verdicts)]) == 2
    assert "no reviewer version tuple" in capsys.readouterr().err


def test_sentinel_eval_runs_the_reviewer_over_stored_items_beyond_the_pr_cap(tmp_path, pinned):
    """The eval sends every item (the PR cap of 60 is raised to the item count) through the injected transport."""
    base = frame()
    items = []
    for k in range(70):
        m = E.eval_mutant(base, op="erase", box=BUTTON, index=k, checkpoint_id=S07,
                          surface_id="dialog.order-confirmation", region="rgn.confirm.send", tier=1, entries=ENTRIES,
                          workspace_id="ws.tier-a.active-trader", data_epoch="ep-1")
        for img in m.crops.values():
            E.write_blob(tmp_path / "blobs", img)
        items.append(m.item)
    labels, _ = _write_set(tmp_path, items, {})
    fake = FakeTransport(respond=lambda params: resp(answer(TASK_A, "does_not_match_manifest", 0.95,
                                                            change="missing_element")))     # not borderline: one call
    rc = E.main([str(labels), "--config", str(pinned), "--blobs", str(tmp_path / "blobs"), "--out",
                 str(tmp_path / "out")], transport=fake)
    assert len(fake.calls) == 70
    metrics = json.loads(next((tmp_path / "out" / "eval" / "reports").glob("*.held_out.metrics.json")).read_text())
    assert metrics["unanswered"] == 0 and metrics["tier1_mutant_recall"] == 1.0
    assert rc == 1                                   # only mutants: the other gates are undefined, so they fail
    assert "matches_manifest_precision" in metrics["undefined"]


def test_blobs_need_a_reviewer_and_a_live_eval_needs_a_store(tmp_path, pinned, monkeypatch, capsys):
    labels, _ = _write_set(tmp_path, synthetic_set(40), {})
    assert E.main([str(labels), "--config", str(pinned), "--blobs", str(tmp_path)]) == 2
    assert "--blobs needs --live" in capsys.readouterr().err
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    assert E.main([str(labels), "--config", str(pinned), "--blobs", str(tmp_path), "--live",
                   "--out", str(tmp_path / "out")]) == 2
    assert "pass --db" in capsys.readouterr().err


def test_a_live_eval_without_a_key_sends_nothing(tmp_path, pinned, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    labels, _ = _write_set(tmp_path, synthetic_set(40), {})
    assert E.main([str(labels), "--config", str(pinned), "--blobs", str(tmp_path), "--live", "--db",
                   str(tmp_path / "s.db"), "--out", str(tmp_path / "out")]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_sentinel_eval_gate_needs_passing_gates_the_change_rule_and_the_canary_result(tmp_path, pinned, capsys):
    t = tuple_in_force(pinned)
    report = tmp_path / f"{t.hex}.yaml"
    report.write_text(yaml.safe_dump(E.report_doc(t, _gate_list(tier1_mutant_recall=0.985), eval_set_version="v1",
                                                  run_at="2026-11-10T08:00:00+00:00")))
    previous = tmp_path / "previous.yaml"
    previous.write_text(yaml.safe_dump(E.report_doc(_tuple(), _gate_list(), eval_set_version="v0",
                                                    run_at="2026-10-10T08:00:00+00:00")))
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps({"passed": True}))
    bad.write_text(json.dumps({"passed": False, "message": "DETECTOR BLIND since 2026-11-10 03:12 ET."}))
    args = [str(report), "--config", str(pinned)]
    assert E.gate_main(args + ["--canary-result", str(ok)]) == 0
    assert E.gate_main(args + ["--canary-result", str(bad)]) == 1
    assert "the canary gate did not pass: DETECTOR BLIND" in capsys.readouterr().out
    assert E.gate_main(args + ["--canary-result", str(ok), "--previous", str(previous)]) == 1
    assert "tier1_mutant_recall drops 1.5 points" in capsys.readouterr().out
    assert E.gate_main([str(report), "--config", str(EXAMPLE), "--canary-result", str(ok)]) == 0   # no tuple in force
