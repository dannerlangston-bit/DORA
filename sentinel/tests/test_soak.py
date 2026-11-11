"""The RC soak (10.11): budgets per cycle, the freeze detector, log signatures, outcomes, the section and L-XREF-27."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sentinel import soak
from sentinel.lint.rules import lint
from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.results import Outcome
from sentinel.soak import MB, ClockSample, Sample, SoakBudgets, evaluate
from sentinel.taxonomy import is_checkpoint_id

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SIX_HOURS = 6 * 3600


def series(metric="working_set", start=2048 * MB, per_h=0.0, *, pids=(4100,), until=SIX_HOURS, step=60):
    """A sampler series every `step` s, split evenly over the processes of the tree."""
    out = []
    for t in range(step, until + 1, step):
        v = start + per_h * t / 3600
        out += [Sample(float(t), pid, metric, v / len(pids)) for pid in pids]
    return out


def breached(verdicts, metric):
    return [v.cycle for v in verdicts if metric in {b.metric for b in v.breaches}]


def test_a_series_growing_80_mb_per_hour_fails_growth_slope_at_the_cycle():
    v = evaluate(series(per_h=80 * MB, pids=(4100, 4101)))
    assert breached(v, "growth_slope") == list(range(12, 25))         # from the first cycle 3 h in
    c12 = v[11]
    assert c12.assertion_ids[0] == "soak.growth_slope" and "80.0 MB/h" in c12.breaches[0].detail
    assert breached(v, "working_set") == []                            # 12% growth at 3 h, under both bounds


def test_a_slope_under_the_budget_and_private_bytes_are_both_fitted():
    assert breached(evaluate(series(per_h=50 * MB)), "growth_slope") == []
    v = evaluate(series(per_h=10 * MB) + series("private_bytes", 1024 * MB, per_h=70 * MB))
    assert breached(v, "growth_slope")[0] == 12 and "private bytes" in v[11].breaches[0].detail


def test_working_set_growth_is_judged_against_hour_one_only_after_it():
    flat = series(until=3600)
    jump = [Sample(float(t), 4100, "working_set", 2048 * MB * 1.30) for t in range(3660, SIX_HOURS + 1, 60)]
    v = evaluate(flat + jump)
    assert breached(v, "working_set")[0] == 5 and "30% above hour 1" in v[4].breaches[0].detail
    big = [Sample(float(t), 4100, "working_set", 8192 * MB + (1600 * MB if t > 7200 else 0))
           for t in range(60, SIX_HOURS + 1, 60)]
    assert breached(evaluate(big), "working_set")[0] == 9                # 1.6 GB absolute growth, under 25%
    assert breached(evaluate(series(start=2048 * MB * 1.30, until=600) + series(until=SIX_HOURS)),
                    "working_set") == []                                 # hour 1 itself never breaches


def test_gdi_handles_threads_and_heap():
    gdi = [Sample(60.0, 1, "gdi_objects", 8999), Sample(960.0, 1, "gdi_objects", 9000)]
    assert breached(evaluate(gdi), "gdi_objects") == [2]
    handles = series("handles", 1000, until=3600) + [Sample(4000.0, 1, "handles", 1250)]
    threads = series("threads", 100, until=3600) + [Sample(4000.0, 1, "threads", 75), Sample(5000.0, 1, "threads", 85)]
    v = evaluate(handles + threads)
    assert breached(v, "handles") == [5] and breached(v, "threads") == [5]
    assert breached(evaluate(series("handles", 1000, until=3600) + [Sample(4000.0, 1, "handles", 1150)]),
                    "handles") == []
    heap = [Sample(120.0, 1, "heap_after_gc", 0.79 * 2048 * MB), Sample(1000.0, 1, "heap_after_gc", 0.8 * 2048 * MB)]
    assert breached(evaluate(heap, xmx_bytes=2048 * MB), "heap") == [2]
    assert breached(evaluate(heap), "heap") == []                         # -Xmx unreadable: not judged


def test_freezes_relaunches_and_render_latency():
    v = evaluate([], freezes=[1000.0], relaunches=[1010.0, 5000.0])
    assert breached(v, "freezes") == [2] and breached(v, "relaunches") == [2, 6]
    ticking = [ClockSample(float(t), f"h{t}") for t in range(0, 900, 2)]
    assert breached(evaluate([], clock=ticking), "render_latency") == []
    stalled = [ClockSample(float(t), "same" if 100 <= t < 120 else f"h{t}") for t in range(0, 900, 2)]
    v = evaluate([], clock=stalled)
    assert breached(v, "render_latency") == [1] and "98.0%" in v[0].breaches[0].detail


def test_budgets_from_soak_yaml():
    b = SoakBudgets.from_doc({"schema_version": 1, "calibrated_from": "rc-lineage",
                              "budgets": {"gdi_objects": {"max": 8000}, "growth_slope": {"mb_per_h_max": 40}}})
    assert (b.gdi_objects_max, b.slope_mb_per_h_max, b.heap_pct_of_xmx_max, b.calibrated_from) == (8000, 40, 80.0,
                                                                                                   "rc-lineage")
    assert breached(evaluate(series(per_h=50 * MB), b), "growth_slope")
    assert SoakBudgets.from_doc(None) == SoakBudgets()


# -- the freeze detector ---------------------------------------------------------------------------------------------
def test_constant_clock_hashes_for_ten_seconds_with_no_anti_anchor_are_a_freeze():
    frozen = [ClockSample(float(t), "a") for t in range(0, 12, 2)] + [ClockSample(12.0, "b")]
    assert soak.freeze_detector(frozen) == [soak.Freeze(0.0, 10.0)]
    short = [ClockSample(float(t), "a") for t in range(0, 10, 2)] + [ClockSample(10.0, "b")]
    assert soak.freeze_detector(short) == []
    hidden = [ClockSample(float(t), "a", anti_anchor=(t == 6)) for t in range(0, 14, 2)]
    assert soak.freeze_detector(hidden) == []
    long = [ClockSample(float(t), "a") for t in range(0, 60, 2)]
    assert len(soak.freeze_detector(long)) == 1


def test_a_freeze_is_the_app_failure_trio_with_cause_hang():
    budgets = ShardBudgets()
    r = soak.freeze_result(budgets)
    assert (r.checkpoint.outcome, r.checkpoint.reason, r.checkpoint.detail) == ("assertion_failed",
                                                                                "assertion:app_alive", "hang")
    assert r.findings[0].kind == "app_crash" and r.has("relaunch")
    soak.freeze_result(budgets), soak.freeze_result(budgets)
    fourth = soak.freeze_result(budgets)
    assert (fourth.run_status, fourth.run_reason) == ("aborted_budget", "app_restart_budget")
    assert (fourth.unreached.outcome, fourth.unreached.reason) == ("not_run", "budget")


# -- log signatures --------------------------------------------------------------------------------------------------
LOG = """\
2026-11-11 10:00:01 INFO started
java.lang.NullPointerException: Cannot invoke "Grid.row()"
\tat com.tos.grid.GridModel.cell(GridModel.java:211)
\tat com.tos.grid.GridModel$1.run(GridModel.java:88)
\tat com.tos.ui.Painter$$Lambda$412/0x0000000801234567.paint(Unknown Source)
\tat java.awt.EventQueue.dispatch(EventQueue.java:1)
2026-11-11 10:03:00 WARN retry
java.lang.NullPointerException: Cannot invoke "Grid.row()"
\tat com.tos.grid.GridModel.cell(GridModel.java:215)
\tat com.tos.grid.GridModel$2.run(GridModel.java:90)
\tat com.tos.ui.Painter$$Lambda$977/0x00000008abcdef00.paint(Unknown Source)
Exception in thread "AWT-EventQueue-0" java.lang.IllegalStateException: bad
\tat com.tos.chart.Study.compute(Study.java:9)
"""


def test_signatures_are_the_normalized_first_three_frames():
    sigs = soak.stack_signatures(LOG)
    assert len(sigs) == 3 and sigs[0][0] == sigs[1][0]                  # line numbers and lambda ids dropped
    assert sigs[0][0] == "com.tos.grid.GridModel.cell | com.tos.grid.GridModel$N.run | com.tos.ui.Painter$$Lambda.paint"
    assert sigs[2][0] == "com.tos.chart.Study.compute" and sigs[2][1] == 12


def test_a_new_stack_header_gives_a_new_log_signature_finding():
    known = soak.stack_signatures(LOG)[0][0]
    logs = soak.log_signatures(LOG, {known})
    assert logs.new == ("com.tos.chart.Study.compute",) and logs.counts[known] == 2
    f = logs.findings()
    assert [x.kind for x in f] == ["new_log_signature"] and f[0].tier == 2
    assert "count 1" in f[0].detail and "line 12" in f[0].detail
    assert logs.line == "log signatures: 1 new of 2 seen"


def test_a_missing_log_prints_not_available_and_no_lineage_set_raises_nothing():
    missing = soak.log_signatures(None, set())
    assert missing.line == "log signatures: not available" and missing.findings() == []
    orphan = soak.log_signatures(LOG, None)
    assert orphan.findings() == [] and "no lineage set" in orphan.line


# -- outcomes, the gate line, the workspace and the section ------------------------------------------------------
def test_cycle_outcomes_follow_the_1_10_order():
    v = soak.CycleVerdict(3, (soak.Breach("working_set", 1, 2, 1, "grew"), soak.Breach("heap", 1, 2, 1, "full")))
    o = soak.cycle_outcome(v)
    assert (o.outcome, o.reason) == ("assertion_failed", "assertion:soak.working_set") and "soak.heap" in o.detail
    alive = Outcome("assertion_failed", "assertion:app_alive", detail="hang")
    assert soak.cycle_outcome(v, app_failed=alive) is alive
    nb = soak.cycle_outcome(soak.CycleVerdict(3), lineage_comparable=False)
    assert (nb.outcome, nb.reason) == ("needs_baseline", "lineage_missing")
    assert soak.cycle_outcome(soak.CycleVerdict(3)) is None
    assert soak.cycle_outcome(v, lineage_comparable=False).outcome == "assertion_failed"


def test_an_unfinished_soak_is_not_run_budget_and_never_passed_in_the_gate():
    missing = soak.unreached("trade.active-trader.ladder", 20)
    assert list(missing) == [soak.cycle_checkpoint("trade.active-trader.ladder", n) for n in range(21, 25)]
    assert all(is_checkpoint_id(c) for c in missing)
    assert all((o.outcome, o.reason) == ("not_run", "budget") for o in missing.values())
    outcomes = {**{soak.cycle_checkpoint("s.x", n): None for n in range(1, 21)}, **soak.unreached("s.x", 20)}
    assert soak.gate_line(outcomes) == "soak: not finished, 4 of 24 cycles not_run (budget)"
    assert soak.gate_line({soak.cycle_checkpoint("s.x", n): None for n in range(1, 25)}) == "soak: 24 cycles run"


def test_rig_5_loads_the_tier_b_workspace_with_the_most_gadgets():
    docs = [yaml.safe_load(p.read_text()) for p in sorted((EXAMPLE / "corpus" / "workspaces").glob("*/entry.yaml"))]
    want = max((w for w in docs if w["tier"] == "B"), key=lambda w: (len(w["gadgets_present"]), w["id"]))
    got = soak.soak_workspace(docs)
    assert got in {w["id"] for w in docs if w["tier"] == "B"}
    assert len(next(w for w in docs if w["id"] == got)["gadgets_present"]) == len(want["gadgets_present"])
    assert soak.soak_workspace([{"id": "b", "tier": "B", "gadgets_present": [1, 2]},
                                {"id": "a", "tier": "B", "gadgets_present": [1, 2]},
                                {"id": "c", "tier": "A", "gadgets_present": [1, 2, 3]}]) == "a"
    assert soak.soak_workspace([{"id": "c", "tier": "A"}]) is None


def test_samples_from_store_rows_and_the_section():
    start = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
    rows = [{"sampled_at": (start + timedelta(seconds=60 * i)).isoformat().replace("+00:00", "Z"), "pid": 7,
             "metric": "working_set", "value": 1000.0 * MB} for i in range(1, 4)]
    samples = soak.samples_from_rows(rows, start)
    assert [s.t_s for s in samples] == [60.0, 120.0, 180.0]
    logs = soak.log_signatures(None, None)
    outcomes = {soak.cycle_checkpoint("s.x", 1): Outcome("assertion_failed", "assertion:soak.gdi_objects")}
    sec = soak.soak_section(samples, SoakBudgets(), outcomes, lineage=samples, lineage_comparable=False, logs=logs,
                            relaunches=1, freezes=0)
    assert sec["lineage_curves"] is None and "alone" in sec["lineage_note"]
    assert sec["memory_ceiling_mb"] == 1000.0 and sec["log_signatures_line"] == soak.NOT_AVAILABLE
    assert sec["cycles"][soak.cycle_checkpoint("s.x", 1)] == ("assertion_failed", "assertion:soak.gdi_objects")
    assert set(sec["budget_lines"]) == set(soak.METRICS) and sec["relaunches"] == 1
    both = soak.soak_section(samples, SoakBudgets(), {}, lineage=samples, lineage_comparable=True, logs=logs,
                             relaunches=0, freezes=0)
    assert both["lineage_curves"]["working_set"] == both["candidate_curves"]["working_set"]


# -- L-XREF-27 ---------------------------------------------------------------------------------------------------
DEFAULT_DOC = {"schema_version": 1, "budgets": {
    "working_set": {"growth_pct_max": 25, "growth_mb_max": 1536}, "growth_slope": {"window_h": 3, "mb_per_h_max": 60},
    "gdi_objects": {"max": 9000}, "handles": {"within_pct": 20}, "threads": {"within_pct": 20},
    "heap": {"pct_of_xmx_max": 80}, "freezes": {"max": 0}, "relaunches": {"max": 0},
    "render_latency": {"tick_window_s": 2, "windows_pct_min": 99}}}


def soak_lint(tmp_path, doc, phase=0):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    if doc is not None:
        (root / "soak.yaml").write_text(yaml.safe_dump(doc))
    else:
        (root / "soak.yaml").unlink(missing_ok=True)
    return [v for v in lint(root, phase, datetime(2026, 11, 10).date()).violations if v.rule == "L-XREF-27"]


def test_the_default_budgets_lint_clean_and_a_looser_one_warns(tmp_path):
    assert soak_lint(tmp_path, DEFAULT_DOC) == []
    loose = yaml.safe_load(yaml.safe_dump(DEFAULT_DOC))
    loose["budgets"]["gdi_objects"]["max"] = 9500
    loose["budgets"]["render_latency"]["windows_pct_min"] = 95
    found = soak_lint(tmp_path / "b", loose)
    assert [(v.severity, v.path) for v in found] == [("warn", "budgets.gdi_objects.max"),
                                                     ("warn", "budgets.render_latency.windows_pct_min")]
    assert "system owner" in found[0].message


def test_a_missing_or_extra_budget_fails_the_schema_under_l_xref_27(tmp_path):
    short = yaml.safe_load(yaml.safe_dump(DEFAULT_DOC))
    del short["budgets"]["heap"]
    assert {v.severity for v in soak_lint(tmp_path, short)} == {"error"}
    extra = yaml.safe_load(yaml.safe_dump(DEFAULT_DOC))
    extra["budgets"]["cpu"] = {"max": 50}
    assert {v.severity for v in soak_lint(tmp_path / "b", extra)} == {"error"}


@pytest.mark.parametrize("phase,expected", [(0, []), (3, ["error"])])
def test_a_missing_soak_yaml_is_an_error_from_phase_3(tmp_path, phase, expected):
    assert [v.severity for v in soak_lint(tmp_path, None, phase)] == expected


def test_looser_budgets_lists_every_loosening():
    stricter = yaml.safe_load(yaml.safe_dump(DEFAULT_DOC))
    stricter["budgets"]["gdi_objects"]["max"] = 8000
    assert soak.looser_budgets(stricter) == []
    loose = yaml.safe_load(yaml.safe_dump(DEFAULT_DOC))
    loose["budgets"]["freezes"]["max"] = 1
    loose["budgets"]["growth_slope"]["window_h"] = 6
    assert [p for p, _ in soak.looser_budgets(loose)] == ["budgets.growth_slope.window_h", "budgets.freezes.max"]
    assert soak.load_budgets(EXAMPLE) == SoakBudgets()
