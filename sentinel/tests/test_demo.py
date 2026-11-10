"""End to end: every `sentinel demo` scenario, its outputs, its store, and determinism (sentinel.demo)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sentinel import records
from sentinel.demo import AT, LADDER, main, run_demo
from sentinel.records import CheckpointOutcome
from sentinel.store import Store
from sentinel.taxonomy import load

FILES = ("pr-comment.md", "report.html", "commit-status.json", "sentinel.db", "summary.txt")
SECTIONS = ("**Unexpected (", "**Intended (", "**Suppressed by decision (", "**Coverage**", "**Run health**")
S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
CHART = "charts.flexible-grid.chart-cell"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Each scenario once per test session (scenarios are deterministic; see the determinism test)."""
    root = tmp_path_factory.mktemp("demo")
    cache: dict[str, tuple[Path, list]] = {}

    def get(name: str):
        if name not in cache:
            cache[name] = (root / name, run_demo(name, root))
        return cache[name]
    return get


def stored(out: Path, run_id: str) -> tuple[list[CheckpointOutcome], list, dict]:
    with Store(out / "sentinel.db") as store:
        return store.outcomes(run_id), store.findings(run_id), store.run_counts(run_id)


def assert_legal(outcomes: list[CheckpointOutcome]) -> None:
    """Every outcome written is legal under the taxonomy (1.10, 4.13): rebuilding the record re-validates it."""
    t = load()
    for o in outcomes:
        t.check_checkpoint(o.outcome, o.reason, o.gate)
        records.from_dict(CheckpointOutcome, o.to_dict())


def assert_outputs(out: Path) -> str:
    for f in FILES:
        assert (out / f).exists(), f
    assert (out / "artifacts" / "manifest.json").exists()
    status = json.loads((out / "commit-status.json").read_text())
    assert status["context"] == "sentinel/pr-run" and status["state"] in ("success", "failure", "error")
    assert "SIMULATED run" in (out / "summary.txt").read_text()
    return (out / "pr-comment.md").read_text()


def by(outcomes, surface=None, outcome=None, kind=None):
    return [o for o in outcomes if (surface is None or o.surface_id == surface) and (outcome is None or o.outcome == outcome)
            and (kind is None or o.test_kind == kind)]


def test_flexgrid_ladder_is_the_1_11_worked_example(runs):
    out, (r,) = runs("flexgrid-ladder")
    text = assert_outputs(out)
    outs, finds, counts = stored(out, r.run_id)
    assert_legal(outs)
    inv_steps = by(outs, AT, "invariant_violation", "sweep")
    assert sorted(o.checkpoint_id.rsplit("/", 1)[1] for o in inv_steps) == ["320x360", "480x360", "640x360", "960x360"]
    assert all(o.reason == "INV-01" and o.stability == "stable" and o.recaptures == 1 for o in inv_steps)
    assert all(o.classification.category == "collateral" and o.classification.by == "rule" for o in inv_steps)
    collateral = by(outs, AT, "collateral", "sweep")
    assert len(collateral) == 8 and all(o.primitive == "FlexibleGrid" and o.manifest_entry_ref == "tos-48120-1"
                                        for o in collateral)
    assert all(o.classification.model_id for o in collateral)          # the description call is recorded
    chart = by(outs, CHART)
    assert chart and all(o.outcome == "intended" and o.classification.by == "reviewer"
                         and o.classification.confidence == 0.93 for o in chart)
    assert len(by(outs, LADDER, "invariant_violation")) == 4
    assert {o.outcome for o in by(outs, "charts.flexible-grid.level2-cell")} == {"collateral"}
    assert counts["invariant_violation"] == 8 and counts == {**dict.fromkeys(counts, 0), **r.counts()}
    assert {f.kind for f in finds} >= {"invariant_violation", "collateral", "intended", "layout_shift"}
    assert all(f.phash and f.region_id for f in finds if f.finding_id.startswith("F-"))
    for s in SECTIONS:
        assert s in text
    assert "invariant_violation on `charts.flexible-grid.active-trader-cell`" in text
    assert "Fails at height 360 for all four widths (4 of 12 Ladder S steps); first failing step in ladder order 960x360" in text
    assert "(4 of 12 Ladder S steps)" in text and "Heights 480 and 720 at all four widths (8 of 12 steps)" in text
    assert "Manifest reconciliation: tos-48120-1: verified visible" in text
    assert "simulated-reviewer-offline" in text
    assert r.commit_status["state"] == "failure"
    assert 'src="data:image/png;base64,' in (out / "report.html").read_text()
    assert "Where this differs from the 9.3 sample" in (out / "summary.txt").read_text()


def test_clean_pr_is_intended_only_and_green(runs):
    out, (r,) = runs("clean-pr")
    text = assert_outputs(out)
    outs, finds, counts = stored(out, r.run_id)
    assert_legal(outs)
    assert {o.outcome for o in outs} == {"intended", "identical"}
    assert {o.surface_id for o in by(outs, outcome="intended")} == {CHART}
    assert "**Unexpected (0)**" in text and "**Intended (1)**" in text
    assert r.commit_status["state"] == "success"
    assert {f.kind for f in finds} == {"intended"}


def test_backend_only_money_cell_diff_needs_a_human(runs):
    out, (r,) = runs("backend-only")
    text = assert_outputs(out)
    outs, finds, counts = stored(out, r.run_id)
    assert_legal(outs)
    (s07,) = [o for o in outs if o.checkpoint_id == S07]
    assert (s07.outcome, s07.reason) == ("needs_human", "no_manifest")
    assert (s07.stability, s07.stability_cause) == ("unknown", "not_recapturable")
    assert any(rd.region_id == "rgn.confirm.bp-effect" and rd.above_threshold for rd in s07.diff.regions)
    assert counts["needs_human"] == 1 and counts["collateral"] == 0 and counts["intended"] == 0
    assert not [f for f in finds if f.kind == "manifest_missing"]
    assert "flow.trade.order-ticket.stock.limit" in r.extra_flows
    assert "No manifest and no UI code touched" in text and "needs_human (reason: no_manifest)" in text
    assert "-15,032.51" in [f for f in finds if f.checkpoint_id == S07][0].description


def test_ui_change_without_a_manifest_is_collateral(runs):
    out, (r,) = runs("ui-no-manifest")
    text = assert_outputs(out)
    outs, finds, _ = stored(out, r.run_id)
    assert_legal(outs)
    (l2,) = by(outs, "charts.flexible-grid.level2-cell")
    assert (l2.outcome, l2.reason) == ("collateral", "manifest_missing")
    assert [f.kind for f in finds if f.kind == "manifest_missing"] == ["manifest_missing"]
    assert "manifest_missing" in text and "every diff is reported as collateral until you add one" in text


def test_nightly_canary_goes_blind_on_a_money_cell(runs):
    out, (n1, n2, pr) = runs("nightly-canary")
    text = assert_outputs(out)
    assert n1.canary["report"].state == "ok" and n2.canary["report"].state == "detector_blind"
    assert n1.status == n2.status == "complete"
    blind = [r for r in n2.canary["report"].results if r.state == "detector_blind"]
    assert [(r.mutant_id, r.produced_outcome) for r in blind] == [("M04", "identical")]
    assert "msk.confirm.clock-01" in blind[0].absorbed_by
    outs, finds, counts = stored(out, n2.run_id)
    assert [(o.checkpoint_id, o.outcome, o.reason, o.synthetic) for o in outs] == \
        [(S07, "harness_error", "detector_blind", True)]
    assert all(v == 0 for v in counts.values())                     # synthetic rows count in no run metric
    assert [f.kind for f in finds] == ["canary_miss"]
    assert stored(out, n1.run_id)[0] == []
    assert text.splitlines()[1] == "```text" and "DETECTOR BLIND since" in text and "M04 swapped_digit" in text
    assert "money cells 85.7 percent (6 of 7)" in text
    assert (out / "morning-report.txt").read_text().startswith("DETECTOR BLIND")
    body = json.loads((out / f"canary-results-{n2.run_id}.json").read_text())
    assert body["detector_state"] == "detector_blind" and body["not_simulated"]
    detected = {r.mutant_id for r in n1.canary["report"].results if r.state == "detected"}
    assert {"M01", "M02", "M03", "M04", "M05", "M12", "M26", "M28"} <= detected


def test_decision_inheritance_suppresses_and_inherits(runs):
    out, (r1, r2) = runs("decision-inheritance")
    text = assert_outputs(out)
    outs, finds, counts = stored(out, r2.run_id)
    assert_legal(outs)
    with Store(out / "sentinel.db") as store:
        rows = store.conn.execute("SELECT action, count(*) FROM decisions GROUP BY action").fetchall()
    assert dict(rows) == {"confirm_bug": 8, "mark_intended": 18}
    for o in by(outs, outcome="invariant_violation"):
        assert o.decision_ref and o.reason == "INV-01"            # the outcome never changes; the finding is suppressed
    inherited = [o for o in outs if o.outcome == "intended" and (o.reason or "").startswith("decision:")]
    assert len(inherited) == 18 and all(o.decision_ref == o.reason.split(":", 1)[1] for o in inherited)
    assert counts["collateral"] == 2 and counts["invariant_violation"] == 8
    assert "**Suppressed by decision (26)**" in text and "confirm_bug (confirmed_bug)" in text
    assert "mark_intended (intended)" in text and r2.commit_status["state"] == "success"
    assert r1.commit_status["state"] == "failure"
    assert {f.decision_ref for f in stored(out, r1.run_id)[1] if f.decision_ref}   # the first run's findings carry them


def test_same_scenario_twice_gives_identical_outcomes_and_comment(tmp_path, runs):
    out, (r,) = runs("clean-pr")
    again = run_demo("clean-pr", tmp_path)[0]
    first = [o.to_dict() for o in stored(out, r.run_id)[0]]
    second = [o.to_dict() for o in stored(tmp_path / "clean-pr", again.run_id)[0]]
    assert first == second
    assert (out / "pr-comment.md").read_text() == (tmp_path / "clean-pr" / "pr-comment.md").read_text()
    assert (out / "commit-status.json").read_text() == (tmp_path / "clean-pr" / "commit-status.json").read_text()


def test_cli_usage(capsys):
    with pytest.raises(SystemExit):
        main(["no-such-scenario"])
    from sentinel.cli import main as cli
    assert cli(["run", "/no/such/dir", "--scenario", "clean-pr"]) == 2
