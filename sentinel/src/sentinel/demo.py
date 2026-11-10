"""`sentinel demo`: simulated end-to-end Sentinel runs a tester can read (see `sentinel.pipeline`).

    sentinel demo <scenario> --out DIR [--today YYYY-MM-DD] [--config DIR]
    sentinel demo all --out DIR

Scenarios (each writes `DIR/<scenario>/`: pr-comment.md, report.html, commit-status.json,
sentinel.db, artifacts/, summary.txt, plus the config each run used and its calibration):

- `flexgrid-ladder`: the worked example of 1.11 (PR 4812).
- `clean-pr`: a declared change that only touches its declared surface.
- `backend-only`: no manifest, a non-UI change that moves the Buying Power Effect (F5, F41).
- `ui-no-manifest`: a UI change with no manifest (`manifest_missing`, 4.3 case a).
- `nightly-canary`: two Nightly canary passes, the second blind on a money cell, then a PR run
  under the detector-blind banner (10.3).
- `decision-inheritance`: PR 4812 run twice with human decisions recorded in between (9.7).

The thinkorswim app is simulated (`sentinel.synth` screens); the Reviewer is an offline simulated
stand-in. Everything else is the real core.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import cv2

from . import synth
from .pipeline.reviewer import SIMULATED_MODEL_ID
from .pipeline.run import (DEFAULT_TODAY, NOT_SIMULATED, NightlyScenario, RunResult, Scenario, record_decisions,
                           run_nightly, run_pipeline)
from .pipeline.sim import SimBuild
from .store import Store

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example"
SCENARIOS = ("flexgrid-ladder", "clean-pr", "backend-only", "ui-no-manifest", "nightly-canary", "decision-inheritance")
BASE_ID = "main-1998.3.0-2c91ae4"
GRID_SRC = "desktop/ui/grid/src/main/java/com/tos/desktop/ui/grid"
GRID_INDEX = {
    "com.tos.desktop.ui.grid.FlexibleGridCell": ("com.tos.desktop.ui.grid.GridCellLayout",
                                                 "com.tos.desktop.ui.grid.GridCellHeader"),
    "com.tos.desktop.charts.grid.cell.ActiveTraderCellHost": ("com.tos.desktop.ui.grid.FlexibleGridCell",),
    "com.tos.desktop.charts.grid.chartcell.ChartCell": ("com.tos.desktop.ui.grid.FlexibleGridCell",),
    "com.tos.desktop.charts.grid.level2cell.Level2Cell": ("com.tos.desktop.ui.grid.FlexibleGridCell",),
    "com.tos.desktop.charts.grid.timesalescell.TimeSalesCell": ("com.tos.desktop.ui.grid.FlexibleGridCell",),
    "com.tos.desktop.trade.activetrader.ActiveTraderPanel": ("com.tos.desktop.ui.grid.FlexibleGridCell",),
}
SIMULATION_NOTE = (
    "This is a SIMULATED run. thinkorswim is replaced by synthetic screens drawn by sentinel.synth, and the "
    f"AI Reviewer by an offline stand-in ({SIMULATED_MODEL_ID}) that never calls a model. Everything else is "
    "the real Sentinel core: config lint, scope, settle, the Diff Engine with A/A calibration, the Invariant "
    "Checker, classification, decisions, the SQLite store, coverage, canaries and the Reporter. The rig facts "
    "in Run health (gates, attestation, safety proof, wall clock) are simulated, not measured.")


def _commit(prefix: str) -> str:
    return prefix + hashlib.sha1(prefix.encode()).hexdigest()[: 40 - len(prefix)]


def _main_build(sha7: str = "2c91ae4") -> tuple[str, str]:
    bid = f"main-1998.3.0-{sha7}"
    text = f"""schema_version: 1
build:
  build_id: {bid}
  channel: main
  version_string: "1998.3.0"
  commit_sha: {_commit(sha7)}
  pipeline_run_id: "tos-desktop/8812"
  artifact:
    uri: artifactory://tos-desktop/main/{sha7}/thinkorswim-win64.zip
    sha256: "sha256:{hashlib.sha256(bid.encode()).hexdigest()}"
  install_method: artifact
  built_at: 2026-11-10T07:55:00-05:00
"""
    return f"builds/{bid}.yaml", text


def _branch_build(sha7: str, pr: int, branch: str, base_sha7: str = "2c91ae4") -> tuple[str, str]:
    bid = f"branch-1998.3.0-{sha7}"
    base = f"main-1998.3.0-{base_sha7}"
    text = f"""schema_version: 1
build:
  build_id: {bid}
  channel: branch
  version_string: "1998.3.0"
  commit_sha: {_commit(sha7)}
  branch: {branch}
  merge_base_sha: {_commit(base_sha7)}
  pipeline_run_id: "tos-desktop/pr-{pr}/1"
  artifact:
    uri: artifactory://tos-desktop/branch/{sha7}/thinkorswim-win64.zip
    sha256: "sha256:{hashlib.sha256(bid.encode()).hexdigest()}"
  install_method: artifact
  built_at: 2026-11-10T10:24:00-05:00
build_pair:
  pair_id: pr-{bid}-vs-{base}
  run_type: pr
  candidate: {bid}
  base: {base}
  base_rule: merge_base_main
  created_at: 2026-11-10T10:31:00-05:00
"""
    return f"builds/{bid}.yaml", text


def _mock_png() -> bytes:
    """The 1.11 manifest's mock: a 3x3 grid of chart cells with 22 px headers."""
    cells = [{"row": r, "col": c, "title": f"Chart {3 * r + c + 1}",
              "content": {"type": "rect", "name": f"plot{r}{c}", "color": "bg"}} for r in range(3) for c in range(3)]
    layout = {"type": "window", "children": [{"type": "flexgrid", "name": "fg", "col_widths": [316, 316, 316],
                                              "row_heights": [208, 208, 208], "header_h": 22, "cells": cells}]}
    img = synth.render(layout, (956, 632)).image
    return cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))[1].tobytes()


def _manifest_4812(candidate: str) -> str:
    return f"""schema_version: 1
manifest_id: mf-{candidate}
candidate: {candidate}
base: {BASE_ID}
source: manual
author: jdoe
entries:
  - entry_id: tos-48120-1
    tickets: [TOS-48120]
    summary: Flexible Grid cells lose 6 px of chrome; hosted content gets the full remaining cell height
    declared_surfaces: [charts.flexible-grid.chart-cell]
    touched_primitives: [FlexibleGrid]
    changed_paths:
      - "desktop/ui/grid/src/main/java/**/GridCellLayout.java"
      - "desktop/ui/grid/src/main/java/**/GridCellHeader.java"
    expected_visual: >
      Cell header 22 px instead of 28 px. Chart plot area 6 px taller.
      In a 3x3 grid at 1920x1080 each chart shows one more price label.
    mock_image: manifests/mocks/tos-48120-grid-3x3.png
    feature_flags: []
    must_be_observed: true
"""


def flexgrid_scenario(today: date, *, seq: int = 1, sha7: str = "7be42d0") -> Scenario:
    """PR 4812 of 1.11: Flexible Grid cell chrome; the Active Trader ladder overlaps its order strip."""
    cand = f"branch-1998.3.0-{sha7}"
    overlay = dict([_main_build(), _branch_build(sha7, 4812, "feature/TOS-48120-flexgrid-chrome")])
    overlay[f"manifests/{cand}.yaml"] = _manifest_4812(cand)
    overlay["manifests/mocks/tos-48120-grid-3x3.png"] = _mock_png()
    return Scenario("flexgrid-ladder", f"run-{today.isoformat()}-pr-4812-{seq:02d}",
                    SimBuild(cand, frozenset({"flexgrid-cell-chrome"})), SimBuild(BASE_ID), pr=4812, author="jdoe",
                    overlay=overlay,
                    changed_paths=(f"{GRID_SRC}/GridCellLayout.java", f"{GRID_SRC}/GridCellHeader.java"),
                    depends_on=GRID_INDEX, clock="10:31:00" if seq == 1 else "13:05:00")


def clean_scenario(today: date, *, seq: int = 1, clock: str = "10:31:00") -> Scenario:
    """PR 4823: the chart cell header shows the aggregation period; declared, and nothing else moves."""
    sha7, cand = "a41c7e9", "branch-1998.3.0-a41c7e9"
    overlay = dict([_main_build(), _branch_build(sha7, 4823, "feature/TOS-48211-chart-period")])
    overlay[f"manifests/{cand}.yaml"] = f"""schema_version: 1
manifest_id: mf-{cand}
candidate: {cand}
base: {BASE_ID}
source: manual
author: mlee
entries:
  - entry_id: tos-48211-1
    tickets: [TOS-48211]
    summary: Chart cell header shows the aggregation period after the symbol
    declared_surfaces: [charts.flexible-grid.chart-cell]
    touched_primitives: []
    changed_paths:
      - "desktop/charts/src/main/java/**/chartcell/ChartCellHeader.java"
    expected_visual: >
      The chart cell header reads "AAPL  1D" instead of "AAPL". Nothing else in the cell moves.
    feature_flags: []
    must_be_observed: true
"""
    return Scenario("clean-pr", f"run-{today.isoformat()}-pr-4823-{seq:02d}",
                    SimBuild(cand, frozenset({"chart-header-period"})), SimBuild(BASE_ID), pr=4823, author="mlee",
                    overlay=overlay,
                    changed_paths=("desktop/charts/src/main/java/com/tos/desktop/charts/grid/chartcell/ChartCellHeader.java",),
                    depends_on={}, clock=clock)


def backend_scenario(today: date) -> Scenario:
    """PR 4830: a margin rules resource changes; no UI code, no manifest; the BP Effect cell moves."""
    sha7, cand = "b7d2e15", "branch-1998.3.0-b7d2e15"
    overlay = dict([_main_build(), _branch_build(sha7, 4830, "fix/TOS-48330-house-margin")])
    return Scenario("backend-only", f"run-{today.isoformat()}-pr-4830-01",
                    SimBuild(cand, frozenset({"bp-effect-house-surcharge"})), SimBuild(BASE_ID), pr=4830,
                    author="rkhan", overlay=overlay,
                    changed_paths=("desktop/trade/margin/src/main/resources/margin/house-rules.properties",),
                    depends_on={})


def ui_no_manifest_scenario(today: date) -> Scenario:
    """PR 4835: the Level II cell's size column changes and the PR carries no manifest."""
    sha7, cand = "c93e0a4", "branch-1998.3.0-c93e0a4"
    overlay = dict([_main_build(), _branch_build(sha7, 4835, "feature/TOS-48305-l2-lots")])
    return Scenario("ui-no-manifest", f"run-{today.isoformat()}-pr-4835-01",
                    SimBuild(cand, frozenset({"level2-size-column"})), SimBuild(BASE_ID), pr=4835, author="tnguyen",
                    overlay=overlay,
                    changed_paths=("desktop/charts/src/main/java/com/tos/desktop/charts/grid/level2cell/"
                                   "Level2SizeRenderer.java",),
                    depends_on={})


S07 = "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml"
S07_MASK = "masks/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07/msk.confirm.clock-01.yaml"


def blind_overlay(config_dir: Path) -> dict[str, str]:
    """A config edit that lint accepts and the canary catches: the Buying Power Effect region loses its
    money_cell flag and the dialog's clock mask is pasted with that cell's offset (rect dx 24, dy 120)."""
    text = (config_dir / S07).read_text(encoding="utf-8")
    old = "rect: {dx: 24, dy: 120, w: 200, h: 20}, money_cell: true}"
    if old not in text:
        raise ValueError(f"{S07} does not carry the example's bp-effect line")
    text = text.replace(old, "rect: {dx: 24, dy: 120, w: 200, h: 20}}")
    text += ("  - {id: rgn.confirm.clock, type: mask, landmark: lm.order-confirmation-title, panel: order_confirmation,"
             " rect: {dx: 24, dy: 120, w: 200, h: 20}, mask_ref: msk.confirm.clock-01}\n")
    mask = """schema_version: 1
mask_id: msk.confirm.clock-01
checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07
region_id: rgn.confirm.clock
owner: tos-trade-ui
reason: The confirmation clock ticks between captures.
created_by: human
created_run: run-2026-11-08-merge-0430
created_at: 2026-11-08T16:02:00-05:00
expires_at: 2026-12-08T16:02:00-05:00
ticket: TOS-48344
decision_ref: dec-000830
"""
    return {S07: text, S07_MASK: mask}


# ------------------------------------------------------------------------------------------------
# summary.txt: what happened and why, for a tester who does not read code


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _outcome_lines(r: RunResult) -> list[str]:
    """One line per surface and outcome: how many checkpoints, of which kind, at which sizes."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for o in r.outcomes:
        if o.synthetic:
            continue
        label = o.outcome if not o.reason else f"{o.outcome} ({o.reason})"
        q = o.checkpoint_id.split("/", 2)[2]
        groups.setdefault((o.surface_id or "", o.test_kind or "", label), []).append(q)
    out = []
    for (surface, kind, label), qs in sorted(groups.items()):
        shown = ", ".join(sorted(qs)[:6]) + (", ..." if len(qs) > 6 else "")
        out.append(f"  - {surface}: {_plural(len(qs), kind + ' checkpoint')} {label} [{shown}]")
    return out


def _run_block(r: RunResult) -> list[str]:
    lines = [f"Run {r.run_id} ({r.run_type}): result {r.status}" + (f" (reason: {r.reason})" if r.reason else "") + "."]
    if r.commit_status:
        lines.append(f"Commit status sentinel/pr-run: {r.commit_status['state']}: {r.commit_status['description']}")
    counts = r.counts()
    if counts:
        lines.append("Checkpoint outcomes: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".")
        lines += _outcome_lines(r)
    synthetic = [o for o in r.outcomes if o.synthetic]
    if synthetic:
        lines.append("Synthetic canary rows in the store (they count in no metric but the canary rate, 10.3): " +
                     "; ".join(f"{o.outcome} (reason: {o.reason}) on {o.checkpoint_id}" for o in synthetic) + ".")
    kinds: dict[str, int] = {}
    for f in r.findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    if kinds:
        lines.append("Findings stored: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + ".")
    if r.reviewer_calls:
        tasks: dict[str, int] = {}
        for c in r.reviewer_calls:
            tasks[c["task"]] = tasks.get(c["task"], 0) + 1
        lines.append(f"Simulated Reviewer calls: {len(r.reviewer_calls)} (" +
                     ", ".join(f"{n} {t}" for t, n in sorted(tasks.items())) + "); see reviewer-calls.json.")
    if r.disabled_invariants:
        lines.append("Invariants switched off by the A/A enablement test of 2.7 (they fired on the base build "
                     "against itself, so they would only raise false alarms on these synthetic screens): " +
                     "; ".join(f"{s}: {', '.join(v)}" for s, v in sorted(r.disabled_invariants.items())) + ".")
    if r.lint_errors:
        lines.append("Config lint errors:")
        lines += [f"  - {e}" for e in r.lint_errors]
    return lines


FILES = """Files in this folder:
  pr-comment.md       the PR comment Sentinel would post (9.2), exactly as rendered
  report.html         the static run report (9.5); open it in a browser, crops are embedded at 1:1
  commit-status.json  the sentinel/pr-run commit status (9.2)
  sentinel.db         the SQLite store (8.3): runs, outcomes, findings, decisions, artifacts index
  artifacts/          content-addressed crops and heatmaps (8.4) with manifest.json
  calibration/        the A/A calibration file of the run's fingerprint (3.9)
  config/<run id>/    the exact config each run linted and used (the example plus the PR's files)
  reviewer-calls.json every simulated Reviewer request and answer (3.13 shape)"""


def _write_summary(out: Path, title: str, story: list[str], results: list[RunResult], extra: list[str] = ()) -> None:
    lines = [title, "=" * len(title), "", SIMULATION_NOTE, "", "What this scenario shows", "-" * 24]
    if any(r.reason == "config_invalid" for r in results):
        lines += ["NOTE: a run below stopped at T+0:00 because config lint failed (1.7): it ended",
                  "aborted_harness (reason: config_invalid) and its PR comment carries the abort block. With the",
                  "default --today 2026-11-10 the example config lints clean; a later date expires its mask and the",
                  "front-month future. The story below is what the scenario shows when the config is valid.", ""]
    lines += story + ["", "What happened", "-" * 13]
    for r in results:
        lines += _run_block(r) + [""]
    lines += list(extra)
    lines += ["", FILES, ""]
    (out / "summary.txt").write_text("\n".join(lines), encoding="utf-8")


def _count(r: RunResult, surface: str | None = None, outcome: str | None = None, kind: str | None = None) -> int:
    return sum(1 for o in r.outcomes if not o.synthetic and (surface is None or o.surface_id == surface)
               and (outcome is None or o.outcome == outcome) and (kind is None or o.test_kind == kind))


AT = "charts.flexible-grid.active-trader-cell"
LADDER = "trade.active-trader.ladder"


# ------------------------------------------------------------------------------------------------
# Scenarios


def demo_flexgrid_ladder(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    r = run_pipeline(config, flexgrid_scenario(today), out, today)
    inv_at = _count(r, AT, "invariant_violation", "sweep")
    sweep_at = _count(r, AT, kind="sweep")
    story = [
        "The worked example of spec 1.11. PR 4812 shrinks the Flexible Grid cell header from 28 to 22 px and gives",
        "the hosted content the full remaining height. The developer declared only the chart cell. In a 360 px tall",
        "cell the Active Trader ladder still positions its price viewport from its old minimum height, so the first",
        "price row is drawn 14 px over the BUY MKT / SELL MKT strip.",
        "",
        f"- The Invariant Checker sees the overlap with no baseline: {inv_at} of {sweep_at} Ladder S sweep steps of the",
        "  Active Trader cell end invariant_violation (every width at height 360): INV-01 (strip and viewport",
        "  intersect), plus INV-02 (a price word crosses the strip) and INV-10 (BUY MKT and Auto send are half",
        "  covered, so their templates no longer match).",
        "- The other steps differ from base (header 6 px shorter, viewport 6 px higher) on a surface nobody declared:",
        "  collateral, with the primitive (FlexibleGrid) and the manifest entry that touched it (tos-48120-1).",
        "- The declared chart cell changes as the entry says; the simulated Reviewer confirms it: intended.",
        "- Level II and Time & Sales cells change too (tier 2, undeclared): collateral that does not fail the check.",
        "",
        "Where this differs from the 9.3 sample, and why:",
        "- The example config's Dependency Map also routes FlexibleGrid to trade.active-trader.ladder, which the",
        "  worked example's 4-edge map did not. That surface is tier 1 and resizable in three hosts, so it gets 36",
        "  sweep steps and 3 splitter steps; its Flexible Grid host shows the same overlap. So there are two",
        "  invariant_violation clusters (4 of 12 steps each) where 9.3 has one.",
        "- The example registry lists the chart cell in four tier A workspaces (9.3 has two), so Intended counts four.",
        "- The smoke census of the example has 4 census checkpoints plus the Active Trader read-back flow (9.3: 60).",
        "- Re-run counts, the Reviewer call count and the wall clock follow from those numbers; the Reviewer costs 0",
        "  because it is simulated.",
        "- Item wording and invariant ids follow from the synthetic screens (INV-01 with INV-02 and INV-10 here;",
        "  9.3 has INV-01 and INV-03 from real OCR).",
    ]
    _write_summary(out, "Scenario flexgrid-ladder: the 1.11 worked example", story, [r])
    return [r]


def demo_clean_pr(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    r = run_pipeline(config, clean_scenario(today), out, today)
    story = [
        "PR 4823 adds the aggregation period to the chart cell header ('AAPL' becomes 'AAPL  1D') and declares",
        "exactly that surface in its manifest. Only the chart cell's own code changed, so the scope is the chart",
        "cell in its four workspaces plus the smoke census.",
        "",
        "- Every chart cell checkpoint differs from base in the header only; the simulated Reviewer, given the",
        "  manifest entry, answers matches_manifest at 0.93: intended (3.11 row 20).",
        "- The smoke census checkpoints are pixel-identical to base: identical.",
        "- Nothing is unexpected, so the sentinel/pr-run check is success.",
    ]
    _write_summary(out, "Scenario clean-pr: a declared change and nothing else", story, [r])
    return [r]


def demo_backend_only(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    r = run_pipeline(config, backend_scenario(today), out, today)
    story = [
        "PR 4830 changes a margin rules file in the backend (no Java UI class, no manifest). The static impact step",
        "finds no UI dependents, so this is the no-manifest case (b) of 4.3 and F5: no manifest_missing finding,",
        "'intended' is disabled, and any diff above threshold needs a human (needs_human, reason no_manifest).",
        "",
        "The change moves the Buying Power Effect on the Order Confirmation dialog from -15,025.00 to -15,032.51.",
        "Buying Power Effect is a money cell (F41): it is always pixel-diffed with a zero floor, so the one-glyph",
        "change is above threshold and the checkpoint ends needs_human (reason: no_manifest) instead of slipping",
        "through. The dialog checkpoint s07 is recapturable: false in its flow, so its stability is 'unknown'.",
        "",
        "Simulation note (a reported workaround): the scope module selects flows only from the change-derived scope;",
        "in case (b) that scope is empty, so no flow would reach the dialog. Following the 2.11 pseudocode, this run",
        f"adds the tier-1 flows that touch the smoke census: {', '.join(r.extra_flows) or 'none'}.",
    ]
    _write_summary(out, "Scenario backend-only: a money-cell diff with no manifest", story, [r])
    return [r]


def demo_ui_no_manifest(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    r = run_pipeline(config, ui_no_manifest_scenario(today), out, today)
    story = [
        "PR 4835 changes how the Level II cell prints sizes ('2200' becomes '22x100') and carries no manifest.",
        "The changed class sits under the Level II cell's roots, so the static impact step finds a UI dependent:",
        "the no-manifest case (a) of 4.3. The run records a manifest_missing finding, and every diff above",
        "threshold is collateral (reason manifest_missing) until the developer adds a manifest entry.",
        "The Level II cell is tier 2, so the check stays success (tier 2 and 3 never block, 9.2), but the comment",
        "lists the collateral and the missing manifest.",
    ]
    _write_summary(out, "Scenario ui-no-manifest: a UI change without a manifest", story, [r])
    return [r]


def demo_decision_inheritance(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    r1 = run_pipeline(config, flexgrid_scenario(today), out, today)
    with Store(out / "sentinel.db") as store:
        at = f"{today.isoformat()}T11:20:00-05:00"
        bugs = record_decisions(store, r1, lambda o: o.outcome == "invariant_violation", action="confirm_bug",
                                decided_by="jdoe", ticket="TOS-48391", cluster_id="cl-4812-overlap", at=at,
                                reason="Ladder viewport overlaps the order strip at 360 px cell height")
        intended = record_decisions(
            store, r1, lambda o: o.outcome == "collateral" and o.tier == 1, action="mark_intended",
            decided_by="triage.lead", proposed_by="jdoe", confirmed_by="triage.lead", ticket="TOS-48120",
            entry="tos-48120-1",
            cluster_id="cl-4812-chrome", at=at,
            reason="The 6 px shift is the declared header change on surfaces the entry did not declare")
    cand2 = "branch-1998.3.0-9c4f1e7"
    sc2 = flexgrid_scenario(today, seq=2, sha7="9c4f1e7")
    r2 = run_pipeline(config, sc2, out, today)
    story = [
        "PR 4812 runs twice. After the first run a human acts on the PR comment, as 1.11 'What happens next' says:",
        f"- confirm bug on the overlap (ticket TOS-48391): {_plural(len(bugs), 'decision')}, one per member signature;",
        f"- mark intended with ticket TOS-48120 on the tier-1 collateral, proposed by the author and confirmed by",
        f"  the triage rotation (the two-person rule for tier 1 and 2, 9.6):",
        f"  {_plural(len(intended), 'decision')}.",
        f"The developer then pushes a new commit ({cand2}) that does not touch the UI, so the screens are the same.",
        "",
        "In the second run every checkpoint's signature (checkpoint, region, pHash, category) is looked up in the",
        "store before classification (9.7):",
        "- the invariant_violation steps keep their outcome (a decision never changes an invariant) and carry the",
        "  confirm_bug decision, so they move to 'Suppressed by decision' instead of raising a new alarm;",
        "- the tier-1 collateral steps inherit mark_intended and become intended (reason decision:<id>, 3.11 row 14),",
        "  because the run's manifest still carries ticket TOS-48120;",
        "- tier-2 collateral had no decision, so it is still listed.",
        "With every tier-1 item decided, the check turns success.",
    ]
    _write_summary(out, "Scenario decision-inheritance: human decisions carried into the next run", story, [r1, r2],
                   [f"Decisions recorded between the runs: {', '.join(d.decision_id for d in bugs + intended)}.",
                    "pr-comment.md, report.html and commit-status.json are those of the second run."])
    return [r1, r2]


def demo_nightly_canary(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    yesterday = today - timedelta(days=1)
    main = SimBuild(BASE_ID)
    overlay = dict([_main_build()])
    history = {c: [1.0] * 14 for c in ("erased_control", "swapped_label", "shifted_panel", "overlapped_panels",
                                        "header_change", "sub_floor", "landmark_loss", "study_removed", "aa_control",
                                        "blank_field", "outside_parent", "window_offscreen")}
    n1 = run_nightly(config, NightlyScenario("nightly-canary", f"run-{yesterday.isoformat()}-nightly-01", main,
                                             overlay=overlay), out, yesterday, history)
    blind = dict(overlay, **blind_overlay(Path(config)))
    n2 = run_nightly(config, NightlyScenario("nightly-canary", f"run-{today.isoformat()}-nightly-02", main,
                                             overlay=blind), out, today, history)
    rep = n2.canary["report"]
    pr = run_pipeline(config, replace(clean_scenario(today, seq=1, clock="10:31:00"),
                                      overlay=dict(clean_scenario(today).overlay, **blind_overlay(Path(config))),
                                      canary=n2.canary["health"], detector_banner=rep.banner), out, today)
    (out / "morning-report.txt").write_text((rep.banner or "Detector state ok.") + "\n", encoding="utf-8")
    shutil.copyfile(out / f"nightly-report-{n2.run_id}.html", out / "nightly-report.html")
    s1, s2 = n1.canary["health"], n2.canary["health"]
    story = [
        "The Nightly run proves the detector still works (10.3): it mutates approved captures of the main build",
        "(canary.generate, seed 20260101), sends every mutant through the same capture, diff, invariant and",
        "classification path as a PR checkpoint (marked synthetic), and scores the result (canary.score).",
        "",
        f"Night 1 ({n1.run_id}): money-cell mutants {s1['money_detected']} of {s1['money_total']} detected, other "
        f"{s1['other_detected']} of {s1['other_total']}; detector state {s1['detector_state']}.",
        "",
        f"Night 2 ({n2.run_id}): someone 'tidied' the Order Confirmation regions file. The Buying Power Effect",
        "region lost its money_cell flag and the dialog's clock mask was pasted with that cell's offset. Config lint",
        "passes (the mask's id says clock, and lint knows money cells only by their flag), and the Diff Engine's",
        "money-cell guard cannot see a flag that is gone. The canary catalog still knows M04 is a money-cell",
        "mutant: the swapped digit lands under the mask, the checkpoint reads identical, and M04 is missed.",
        f"Result: money-cell mutants {s2['money_detected']} of {s2['money_total']}; detector state "
        f"{s2['detector_state']}; the synthetic outcome row of the miss is harness_error (reason: detector_blind);",
        "the morning banner names the mask that absorbed it. The Nightly run itself stays complete.",
        "",
        f"The next PR run ({pr.run_id}, the clean-pr change) carries the DETECTOR BLIND banner at the top of its",
        "comment and report until a Nightly run passes; pr-comment.md, report.html and commit-status.json are",
        "those of that PR run. nightly-report.html and morning-report.txt are night 2's.",
        "",
        f"Mutants simulated: {', '.join(n2.canary['simulated'])}. Not simulated (listed with the reason in",
        "canary-results-*.json): " + ", ".join(sorted(NOT_SIMULATED)) + ".",
    ]
    _write_summary(out, "Scenario nightly-canary: the detector proves it can see, then goes blind", story,
                   [n1, n2, pr], ["Morning banner of night 2:", "", rep.banner or "(none)"])
    return [n1, n2, pr]


DEMOS: dict[str, Callable[..., list[RunResult]]] = {
    "flexgrid-ladder": demo_flexgrid_ladder, "clean-pr": demo_clean_pr, "backend-only": demo_backend_only,
    "ui-no-manifest": demo_ui_no_manifest, "nightly-canary": demo_nightly_canary,
    "decision-inheritance": demo_decision_inheritance,
}


def run_demo(name: str, out_root: str | Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE) -> list[RunResult]:
    """Run one scenario into `out_root/<name>/`, replacing what a previous run left there."""
    if name not in DEMOS:
        raise KeyError(f"unknown scenario {name!r}; one of {', '.join(SCENARIOS)} or all")
    out = Path(out_root) / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return DEMOS[name](out, today, Path(config))


def main(argv: list[str] | None = None) -> int:
    """`sentinel demo <scenario|all> --out DIR [--today YYYY-MM-DD] [--config DIR]`."""
    p = argparse.ArgumentParser(prog="sentinel demo", description=__doc__.split("\n\n")[0])
    p.add_argument("scenario", choices=SCENARIOS + ("all",))
    p.add_argument("--out", default="out", help="output root; each scenario writes <out>/<scenario>/")
    p.add_argument("--today", type=date.fromisoformat, default=DEFAULT_TODAY,
                   help="date for expiries and run ids (default 2026-11-10, which the example config assumes)")
    p.add_argument("--config", default=str(EXAMPLE), help="sentinel-config directory (default config/example)")
    a = p.parse_args(argv)
    names = SCENARIOS if a.scenario == "all" else (a.scenario,)
    for name in names:
        results = run_demo(name, a.out, a.today, Path(a.config))
        last = results[-1]
        print(f"{name}: {last.status}" + (f" ({last.reason})" if last.reason else "") +
              (f", check {last.commit_status.get('state')}" if last.commit_status else "") +
              f" -> {Path(a.out) / name}/summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
