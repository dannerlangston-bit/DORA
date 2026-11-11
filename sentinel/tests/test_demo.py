"""End to end: every `sentinel demo` scenario, its outputs, its store, and determinism (sentinel.demo)."""
from __future__ import annotations

import json
import re
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


def product(outs: list) -> list:
    """The product checkpoints' outcomes, less registry validation's probe rows (2.2.3: each PR run adds the eight
    probes of its first tier A workspace)."""
    return [o for o in outs if not o.surface_id.startswith("registry.validation-")]


def assert_outputs(out: Path) -> str:
    for f in FILES:
        assert (out / f).exists(), f
    assert list((out / "artifacts" / "runs").glob("*/manifest.json"))      # 8.4: one set per run
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
    (s07,) = [o for o in outs if o.checkpoint_id == S07]           # case (b): the scope plans the smoke flows
    # The Oracle's R02 bracket (2.8.3: 50 to 100 percent of notional on margin) catches the 0.05 percent surcharge;
    # an assertion outranks the comparison rows, which alone would give needs_human (no_manifest) (3.11 row 8).
    assert (s07.outcome, s07.reason, s07.assertion_ids) == ("assertion_failed", "assertion:R02", ["R02"])
    assert (s07.stability, s07.stability_cause) == ("unknown", "not_recapturable")
    assert any(rd.region_id == "rgn.confirm.bp-effect" and rd.above_threshold for rd in s07.diff.regions)
    assert counts["assertion_failed"] == 1 and counts["needs_human"] == 0 and counts["collateral"] == 0
    assert counts["intended"] == 0 and not [f for f in finds if f.kind == "manifest_missing"]
    assert "No manifest and no UI code touched" in text and "needs_human (reason: no_manifest)" in text
    (f07,) = [f for f in finds if f.checkpoint_id == S07]
    assert "-15,032.51" in f07.description and f07.region_id == "rgn.confirm.bp-effect"
    raw = f07.evidence["raw_values"]
    assert raw["candidate.rgn.confirm.bp-effect.ocr"].startswith("'-15,032.51'")
    assert raw["base.rgn.confirm.bp-effect.ocr"].startswith("'-15,025.00'") and raw["label"] == "bracket check"


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
    assert not [o for o in outs if o.synthetic]          # SD-91: outcomes holds real rows only
    with Store(out / "sentinel.db") as store:            # the miss's synthetic row, linked from its canary_results row
        synth = store.query("SELECT mutant_id, checkpoint_id, outcome, reason FROM synthetic_outcomes WHERE run_id = ? "
                            "AND outcome = 'harness_error'", (n2.run_id,))
        linked = store.query("SELECT count(*) FROM canary_results WHERE run_id = ? AND kind = 'mutant' AND "
                             "synthetic_ref IS NOT NULL", (n2.run_id,))[0][0]
        detector = json.loads(store.get_system_state("detector_state"))
        manifests = store.query("SELECT run_id FROM canary_manifests ORDER BY run_id")
    assert [tuple(r) for r in synth] == [("M04", S07, "harness_error", "detector_blind")]
    assert linked == len([r for r in n2.canary["report"].results if r.kind == "mutant" and r.state != "generation_failed"])
    assert (detector["state"], detector["run_id"]) == ("detector_blind", n2.run_id)          # 8.3 system_state
    assert detector["since_text"].startswith(n2.run_id.split("-nightly")[0][4:])
    assert [r[0] for r in manifests] == [n1.run_id, n2.run_id]                               # 10.3 Determinism
    assert [f.kind for f in finds] == ["canary_miss"]
    assert [f.kind for f in stored(out, n1.run_id)[1]] == []
    for n in (n1, n2):                                   # 10.6: the A/A pass gives the nights' real rows
        rows = stored(out, n.run_id)[0]
        assert rows and {o.outcome for o in rows} == {"identical"} and len(rows) == len(n.canary["aa"].rows)
    assert "Calibration queued" in (out / "morning-report.txt").read_text()
    assert text.splitlines()[1] == "```text" and "DETECTOR BLIND since" in text and "M04 swapped_digit" in text
    assert "money cells 85.7 percent (6 of 7)" in text
    assert (out / "morning-report.txt").read_text().startswith("DETECTOR BLIND")
    body = json.loads((out / f"canary-results-{n2.run_id}.json").read_text())
    # 10.3: the whole catalog renders and scores on the simulated app; night 1 detects every mutant, the batch
    # mutants M21 and M23 on every checkpoint of their synthetic batch
    assert body["detector_state"] == "detector_blind" and body["not_simulated"] == {}
    assert body["simulated"] == [f"M{i:02d}" for i in range(1, 34)]
    assert {r.state for r in n1.canary["report"].results} == {"detected"}
    assert {r.mutant_id for r in n1.canary["report"].results} == set(body["simulated"])
    runs_ = json.loads((out / "orchestrator.json").read_text())       # every run went through the gate machine
    assert [r["run_id"] for r in runs_] == [n1.run_id, n2.run_id, pr.run_id]
    assert all(s["verdict"] == "complete" and s["proof"] == "PROVEN: no orders sent" for r in runs_ for s in r["shards"])
    assert n1.shards[0]["gates"][3]["control"] == "paper_mode"
    # 9.10 Canary failure: night 2's blind state pages the harness on-call at P1; night 1 pages nobody
    alerts = json.loads((out / "alerts.json").read_text())
    assert [(a["run_id"], a["rule"], a["priority"], a["paged"]) for a in alerts      # less the Nightly store check
            if a["control"] not in ("fingerprint", "store_health")] == \
        [(n2.run_id, "canary_failure", "P1", ["harness_on_call"])]
    # 12.7 G5 with the store's calibrated keys: no Calibration run wrote floors, so each run queues one and tells the
    # on-call channel once (10.7), without paging anyone
    # on-call channel once (10.7), without paging anyone; night 2's repeats night 1's gate, so it is P1 (9.10, 11.6)
    assert [(a["run_id"], a["rule"], a["priority"], a["paged"]) for a in alerts if a["control"] == "fingerprint"] == \
        [(n1.run_id, "env_error", "P2", []), (n2.run_id, "env_error", "P1", []), (pr.run_id, "env_error", "P2", [])]
    assert [b["priority"] for b in n2.canary["sent_bugs"]] == ["Highest"] and n1.canary["sent_bugs"] == []


def test_decision_inheritance_suppresses_and_inherits(runs):
    out, (r1, r2) = runs("decision-inheritance")
    text = assert_outputs(out)
    outs, finds, counts = stored(out, r2.run_id)
    assert_legal(outs)
    with Store(out / "sentinel.db") as store:
        rows = store.conn.execute("SELECT action, count(*) FROM decisions WHERE action <> 'request_mask' "
                                  "GROUP BY action").fetchall()       # less the masks' seeded decisions
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
    # the summary explains why the second run still stores 20 collateral findings with only 2 collateral outcomes
    second = (out / "summary.txt").read_text().split(f"Run {r2.run_id}", 1)[1]
    assert "Findings stored: 20 collateral, 4 intended, 8 invariant_violation" in second
    assert "; 26 of them carry an earlier human decision (the PR comment's 'Suppressed by decision' table" in second


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


# -- w3 integration: build pairs, base resolution, reuse and lockstep (3.1, 4.2, 7.9, SD-02) ------------------------

def _summary(out: Path, run_id: str) -> dict:
    with Store(out / "sentinel.db") as store:
        return store.run_summary(run_id)


def test_base_distance_uses_the_nearest_main_build_and_declares_the_pr_merged_in_between(runs):
    """SD-02, 3.1: the merge base has no main build; the base is main@2c91ae4, two commits back. The run row and run
    health carry the distance, and PR 4823's manifest (merged at the commit in between) is declared for the run: its
    chart cell change is intended against its own entry."""
    out, (r,) = runs("base-distance")
    outs, finds, _ = stored(out, r.run_id)
    assert_legal(outs)
    with Store(out / "sentinel.db") as store:
        (row,) = store.query("SELECT base_commit_distance FROM runs WHERE run_id = ?", (r.run_id,))
        (pair,) = store.query("SELECT base_build_id FROM build_pairs WHERE pair_id = (SELECT pair_id FROM runs "
                              "WHERE run_id = ?)", (r.run_id,))
    assert row[0] == 2 and pair[0] == "main-1998.3.0-2c91ae4"
    rows = dict(map(tuple, _summary(out, r.run_id)["health_rows"]))
    assert rows["Base"].startswith("2 commits before the merge base")
    chart = by(outs, CHART)
    assert chart and {o.outcome for o in chart} == {"intended"}
    assert {o.manifest_entry_ref for o in chart} == {"tos-48211-1"}         # the intervening PR's entry
    assert {o.outcome for o in by(outs, "charts.flexible-grid.level2-cell")} == {"intended"}
    assert "2 commits before the merge base" in assert_outputs(out) and r.commit_status["state"] == "success"


def test_base_missing_aborts_before_any_capture_and_requeues_once(runs):
    """3.1, 4.2: no main build within 20 commits of the merge base: `aborted_harness (base_build_missing)` at
    provisioned, the run-level proof of a run with no session, the P1 harness page, and the rebuild and the single
    re-queue requested."""
    out, (r,) = runs("base-missing")
    assert (r.status, r.reason) == ("aborted_harness", "base_build_missing")
    with Store(out / "sentinel.db") as store:
        run = store.run_outcome(r.run_id)
        assert store.outcomes(r.run_id) == [] and not store.query("SELECT 1 FROM shards WHERE run_id = ?", (r.run_id,))
    assert run.last_gate == "provisioned" and r.proof == "PROVEN: no session established"
    abort = _summary(out, r.run_id)["abort"]
    assert abort["requeued"] == f"{r.run_id}-requeue" and abort["rebuild"].startswith("c0de000")
    text = (out / "pr-comment.md").read_text()
    assert "Reason: `base_build_missing`" in text and "21 commits searched" in text
    assert "Re-queued once: " in text and "Rebuild requested: main at merge base c0de000" in text
    (entry,) = [e for e in json.loads((out / "orchestrator.json").read_text()) if e["run_id"] == r.run_id]
    assert {a["action"] for a in entry["persisted_actions"]} == {"rebuild_merge_base", "requeue"}
    (page,) = r.alerts
    assert page["priority"] == "P1" and page["run_reason"] == "base_build_missing"


def test_a_base_build_that_will_not_install_aborts_every_shard_at_provisioned(tmp_path):
    """4.2: the base build cannot be installed on the rig (`SimulatedRig.install_base`, the `base_install_fails`
    knob): each shard stops at provisioned before launch with `pairs.base_missing_result`; its checkpoints are
    `harness_error (base_build_missing)` and the abort block names the cause and the re-queue."""
    from dataclasses import replace

    from sentinel.demo import EXAMPLE, clean_scenario
    from sentinel.pipeline.orchestration import RigScript
    from sentinel.pipeline.run import run_pipeline
    from datetime import date
    sc = replace(clean_scenario(date(2026, 11, 10)), rig=RigScript(base_install_fails="installer checksum mismatch"))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    assert (r.status, r.reason) == ("aborted_harness", "base_build_missing")
    assert {(o.outcome, o.reason) for o in r.outcomes} == {("harness_error", "base_build_missing")}
    assert {s["last_gate"] for s in r.shards} == {"provisioned"}
    abort = _summary(tmp_path, r.run_id)["abort"]
    assert "could not be installed: installer checksum mismatch" in abort["trigger"]
    assert abort["requeued"] == f"{r.run_id}-requeue"


def test_base_reuse_reads_the_merge_runs_frozen_captures(runs):
    """3.1, 7.4, 7.9: the PR run's base is the build the merge run captured; under the same reuse key every frozen
    base comes from the merge run's stored capture (its outcome names the source run), the masked_live one is
    captured, run health counts base_reused and the report says where the captures came from."""
    out, (m, r) = runs("base-reuse")
    assert m.run_type == "merge" and r.status == "complete"
    assert m.status == "complete"                      # every sweep of the merge run reaches its sizes (2.5.1)
    outs, _, _ = stored(out, r.run_id)
    assert_legal(outs)
    outs = product(outs)
    frozen = [o for o in outs if o.data_mode == "frozen"]
    assert frozen and {o.base_source_run_id for o in frozen} == {m.run_id}
    assert {o.base_source_run_id for o in outs if o.data_mode == "masked_live"} == {None}
    assert sorted(o.outcome for o in outs) == ["identical"] * 4 + ["intended"] * 4     # clean-pr's outcomes
    s = _summary(out, r.run_id)
    rows = dict(map(tuple, s["health_rows"]))
    assert rows["Base reuse"] == f"base_reused {len(frozen)} (from {m.run_id}); re-captured 0; reuse_missing 0"
    assert set(s["reuse_fixture"]) and all(k["fixture_sha256"].startswith("sha256:") for k in s["reuse_fixture"].values())
    assert f"base captures reused from {m.run_id}" in (out / "pr-comment.md").read_text()


def test_a_reused_base_whose_stored_capture_is_gone_is_needs_baseline_reuse_missing(tmp_path):
    """3.11 input B: the plan expected to reuse the merge run's capture but its object is gone: `needs_baseline
    (reuse_missing)`, counted under reuse_missing in run health."""
    import shutil
    from datetime import date

    from sentinel.demo import EXAMPLE, base_reuse_scenarios
    from sentinel.pipeline.run import run_pipeline
    merge, pr = base_reuse_scenarios(date(2026, 11, 10))
    run_pipeline(EXAMPLE, merge, tmp_path)
    shutil.rmtree(tmp_path / "artifacts" / "runs" / merge.run_id / "objects")
    r = run_pipeline(EXAMPLE, pr, tmp_path)
    frozen = [o for o in product(r.outcomes) if o.data_mode == "frozen"]
    assert frozen and {(o.outcome, o.reason) for o in frozen} == {("needs_baseline", "reuse_missing")}
    rows = dict(map(tuple, _summary(tmp_path, r.run_id)["health_rows"]))
    assert rows["Base reuse"].endswith(f"reuse_missing {len(frozen)}")


def test_lockstep_slip_is_requeued_once_then_not_run_budget(runs):
    """3.1: the base rig lags 75 s, then 80 s: one re-queue, then `not_run (budget)`; lockstep_slip is a run-health
    counter and no finding names it."""
    out, (r,) = runs("lockstep-slip")
    outs, finds, _ = stored(out, r.run_id)
    (ps,) = [o for o in outs if o.surface_id == "monitor.activity.position-statement"]
    assert (ps.outcome, ps.reason) == ("not_run", "budget")
    assert not [f for f in finds if f.checkpoint_id == ps.checkpoint_id]
    assert "lockstep_slip 1" in dict(map(tuple, _summary(out, r.run_id)["health_rows"]))["Lockstep"]
    assert [s["side"] for s in r.shards] == ["candidate", "base", "both", "both"]


def test_run_creation_checks_a_pr_pair_with_the_resolvers_distance(tmp_path):
    """SD-02, 4.2: a checked-in PR pair whose base is not the merge base is refused (L-BLD-04) unless the run-creation
    resolver names its distance (`pair_violations(base_commit_distance=)`)."""
    import shutil

    from sentinel.demo import EXAMPLE, _branch_build_at, _commit, _main_build
    from sentinel.lint.repo import Repo
    from sentinel.pipeline.run import pair_violations
    cfg = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, cfg)
    rel, text = _branch_build_at("d5e6f70", 4871, "feature/x", _commit("9a2b3c4"))
    base = _main_build()
    (cfg / base[0]).write_text(base[1])
    (cfg / rel).write_text(text + "build_pair:\n  pair_id: pr-branch-1998.3.0-d5e6f70-vs-main-1998.3.0-2c91ae4\n"
                           "  run_type: pr\n  candidate: branch-1998.3.0-d5e6f70\n  base: main-1998.3.0-2c91ae4\n"
                           "  base_rule: merge_base_main\n  created_at: 2026-11-10T10:31:00-05:00\n")
    repo = Repo.load(cfg)
    assert {v.rule for v in pair_violations(repo, "branch-1998.3.0-d5e6f70")} == {"L-BLD-04"}
    assert pair_violations(repo, "branch-1998.3.0-d5e6f70", base_commit_distance=2) == []
    assert {v.rule for v in pair_violations(repo, "branch-1998.3.0-d5e6f70", base_commit_distance=21)} == {"L-BLD-04"}


# -- w3 integration: calibration from the store (3.9, 4.8, 7.13) ------------------------------------------------------

def test_a_pr_run_reads_the_floors_a_calibration_run_wrote_to_the_store(runs):
    """3.9, 4.8 Threshold source, 7.13: after the weekend Calibration run of the base build, every threshold of PR
    4823's run is `calibrated` from the store (`StoreCalibration`), no Calibration is requested, and the outcomes are
    clean-pr's."""
    out, (r,) = runs("calibrated-floors")
    outs, _, _ = stored(out, r.run_id)
    assert sorted(o.outcome for o in product(outs)) == ["identical"] * 4 + ["intended"] * 4
    with Store(out / "sentinel.db") as store:
        assert [tuple(x) for x in store.query("SELECT run_type, outcome FROM runs ORDER BY started_at")] == [
            ("merge", "complete"), ("calibration", "complete"), ("pr", "complete")]    # the seed's history run first
        sources = {x[0] for x in store.query("SELECT json_extract(record_json, '$.threshold_source') FROM "
                                             "outcome_records WHERE run_id = ? AND json_extract(record_json, "
                                             "'$.surface_id') NOT LIKE 'registry.validation-%'", (r.run_id,))}
        summary = store.run_summary(r.run_id)
    assert sources == {"calibrated"} and "calibration_queued" not in summary
    assert not [row for row in summary.get("health_rows", []) if row[0] == "Calibration"]
    assert not [a for a in r.alerts if a["control"] == "fingerprint"]          # G5 found the key's floors


def test_a_tab_the_registry_does_not_list_is_a_tier_1_unregistered_surface_with_a_stub_pr(runs):
    """2.2.3, SD-15: registry validation at entry to running, in the shard's first tier A workspace, on both builds.
    The candidate gains a top-level tab: P1 records `unregistered_surface (probe:P1)` on its synthetic surface, tier
    1, with the registry stub PR; the PR comment lists it and the check fails; the other probes are identical."""
    out, (r,) = runs("unregistered-tab")
    text = assert_outputs(out)
    outs, finds, _ = stored(out, r.run_id)
    by = {o.checkpoint_id: o for o in outs}
    ws = "ws.tier-a.active-trader"
    u = by[f"census/unregistered.p1.crypto/{ws}"]
    assert (u.outcome, u.reason, u.tier, u.surface_id) == ("unregistered_surface", "probe:P1", 1,
                                                           "unregistered.p1.crypto")
    assert u.artifacts["base"] != u.artifacts["candidate"]                          # the two probe screenshots
    probes = {p: by[f"census/registry.validation-p{p}/{ws}"].outcome for p in range(1, 9)}
    assert probes == {1: "collateral", **{p: "identical" for p in range(2, 9)}}
    (f,) = [f for f in finds if f.kind == "unregistered_surface"]
    stub = "registry/surfaces/unregistered/unregistered.p1.crypto.yaml"
    assert f.tier == 1 and stub in f.description and f.evidence["raw_values"]["registry_pr_path"] == stub
    assert "unregistered_surface (reason: probe:P1) on `unregistered.p1.crypto`" in text and stub in text
    assert r.commit_status["state"] == "failure"
    (prop,) = _summary(out, r.run_id)["registry_proposals"]
    assert (prop["kind"], prop["path"], prop["document"]["tier"], prop["document"]["title"]) == (
        "stub", stub, 1, "Crypto")
    assert sorted(o.outcome for o in outs if o.surface_id.startswith("charts.flexible-grid.chart-cell")) == \
        ["intended"] * 4                                                           # the rest is clean-pr's


def test_a_pr_run_without_floors_in_the_store_requests_the_calibration(runs):
    """L-FPR-04, 12.7 G5, 7.13: with no floors for the run's calibration key in the store the run measures its own
    A/A floors (the inline Calibration of 1.6) and requests the Calibration run: `calibration_queued` in the summary
    and a run-health row."""
    out, (r,) = runs("clean-pr")
    summary = _summary(out, r.run_id)
    q = summary["calibration_queued"]
    assert q["reasons"] == {"no_calibration": 1} and q["calibration_key"].startswith("sha256:")
    assert q["requested_by"] == r.run_id and len(q["checkpoints"]) == 8
    rows = dict(map(tuple, summary["health_rows"]))
    assert rows["Calibration"].startswith("Calibration run requested for key sha256:")
    (g5,) = [a for a in r.alerts if a["control"] == "fingerprint"]             # 12.7 G5 with calibrated_keys: once
    assert (g5["rule"], g5["priority"]) == ("env_error", "P2") and "no calibration" in g5["detail"]


# -- w4/w5 part B: the control host in process (9.1, 9.2, 9.5, 9.6, 9.9, 9.10) --------------------------------------

def test_report_ui_confirms_a_bug_through_the_ui_and_the_service_files_it_and_re_posts(runs):
    """The report UI and the Reporter service of `sentinel serve`, in process: the triage page, a confirm_bug through
    the UI's action endpoint, the product Bug filed with its key written back, and the PR comment edited in place."""
    out, (r,) = runs("report-ui")
    assert r.status == "complete" and r.commit_status["state"] == "success"
    text = (out / "summary.txt").read_text()
    assert "POST /act/confirm_bug f=F-483501-001: HTTP 200" in text
    assert "GET /triage after: HTTP 200, F-483501-001 listed: False" in text
    assert "Decision: confirm_bug by triage.lead, ticket TOS-1" in text and "Simulated Jira issues: TOS-1" in text
    assert "comment c1, re-rendered with the decision" in text
    with Store(out / "sentinel.db") as s:
        jobs = [tuple(j) for j in s.query("SELECT kind, state FROM outbox WHERE run_id = ? ORDER BY job_id",
                                          (r.run_id,)) if j["kind"] not in ("slack", "page", "digest")]
    assert jobs == [("commit_status", "sent"), ("pr_comment", "sent"), ("commit_status", "sent"),   # + 9.10 alerts
                    ("jira_create", "sent"), ("pr_comment", "sent"), ("commit_status", "sent")]
    assert "**Suppressed by decision (1)**" in (out / "report-ui-comment.md").read_text()


def test_outbox_outage_retries_and_alerts_slack_and_the_pager_at_15_minutes(runs):
    out, (r,) = runs("outbox-outage")
    text = (out / "summary.txt").read_text()
    assert "+5 min: 2 failed attempt(s), 0 outage alert(s) raised" in text
    assert "+15 min: 2 failed attempt(s), 2 outage alert(s) raised" in text
    assert "+60 min: the PR host is back; 2 job(s) sent" in text
    assert "Slack (#sentinel-oncall): [Sentinel] P2 integration outage | pr run-2026-11-10-pr-4835-01" in text
    assert re.search(r"Paging service: integration_outage:\d+, P2", text)        # keyed by the failing job
