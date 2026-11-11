"""`sentinel demo`: simulated end-to-end Sentinel runs a tester can read (see `sentinel.pipeline`).

    sentinel demo <scenario> --out DIR [--today YYYY-MM-DD] [--config DIR] [--reviewer simulated|live]
    sentinel demo all --out DIR

Scenarios (each writes `DIR/<scenario>/`: pr-comment.md, report.html, commit-status.json,
sentinel.db, artifacts/, summary.txt, alerts.json, orchestrator.json, plus the config each run used
and its calibration):

- `flexgrid-ladder`: the worked example of 1.11 (PR 4812).
- `clean-pr`: a declared change that only touches its declared surface.
- `backend-only`: no manifest, a non-UI change that moves the Buying Power Effect (F5, F41).
- `ui-no-manifest`: a UI change with no manifest (`manifest_missing`, 4.3 case a).
- `nightly-canary`: two Nightly canary passes, the second blind on a money cell, then a PR run
  under the detector-blind banner (10.3).
- `decision-inheritance`: PR 4812 run twice with human decisions recorded in between (9.7).
- `wrong-number`: the Order Confirmation cost is not quantity times price on an otherwise identical
  dialog; the Oracle's R01 fails (`assertion_failed (assertion: cost_math)`).
- `paper-mode-mismatch`: the account selector reads a live-format account id; the paper-mode gate
  aborts the run (`aborted_safety (reason: live_identity)`) before any capture.
- `attempted-send`: the Executor presses Enter on the open Order Confirmation; the gateway blocks it,
  the run is `aborted_safety (reason: attempted_send)` and the proof shows no order went out.
- `app-crash`: the app freezes during a sweep step; `assertion_failed (assertion: app_alive)`, the rest
  of the ladder `not_run (reason: app_crash)`, a relaunch, and the run goes on.
- `config-invalid`: a regions file names a mask record that does not exist (L-RGN-03); preflight G1 (the
  run-start lint) fails and the run is `aborted_harness (reason: config_invalid)`.
- `expired-mask`: the run date is past the first mask's expiry; the mask stops applying at run start, run health
  lists it, a `mask_expired` finding is stored and the run completes (4.1, 4.8).
- `operator-stop`: `sentinel stop` writes a rig's file flag mid-run; the next boundary trips the kill switch, the
  run is `aborted_safety (reason: kill_switch)` and the proof reads PROVEN: no orders sent (6.6).
- `redaction-miss`: the candidate echoes an account id outside the redaction anchors; the redaction stage masks it
  and stores a tier-1 `redaction_miss` finding; the check still passes (3.6, 6.10).

`sentinel demo all` runs every scenario, prints one line each and writes `DIR/index.html`, a page that
links each scenario's report, PR comment, summary and alerts with what to check.

The thinkorswim app is simulated (`sentinel.synth` screens) and so is the rig the Orchestrator drives
(`orchestrator.SimulatedRig`); the Reviewer is an offline simulated stand-in unless `--reviewer live`
is given with `ANTHROPIC_API_KEY` set. Everything else is the real core, the Oracle and the Orchestrator
included.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import shutil
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import cv2

import yaml

from . import synth
from .pipeline.orchestration import RigScript
from .pipeline.reviewer import SIMULATED_MODEL_ID
from .pipeline.run import (DEFAULT_TODAY, NOT_SIMULATED, NightlyScenario, PairRefused, RunResult, Scenario,
                           record_decisions, run_nightly, run_pipeline)
from .pipeline.sim import SimBuild
from .store import Store

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example"
SCENARIOS = ("flexgrid-ladder", "clean-pr", "backend-only", "ui-no-manifest", "nightly-canary", "decision-inheritance",
             "wrong-number", "paper-mode-mismatch", "attempted-send", "app-crash", "config-invalid", "expired-mask",
             "operator-stop", "redaction-miss", "popups", "fingerprint-mismatch", "clamped-gadget", "fixed-reopens",
             "occluded-capture")
AT = "charts.flexible-grid.active-trader-cell"
LADDER = "trade.active-trader.ladder"
STOCK_FLOW = "flow.trade.order-ticket.stock.limit"
LIVE_ACCOUNT = "12345678SCHW"                    # the live account id format of 6.13 item 5: eight digits, then SCHW
FREEZE_AT = f"sweep/{AT}/flexgrid/640x480"
PS_CENSUS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
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
    "This is a SIMULATED run. thinkorswim is replaced by synthetic screens drawn by sentinel.synth, the Windows "
    "rig by the Orchestrator's simulated rig, and the AI Reviewer by an offline stand-in "
    f"({SIMULATED_MODEL_ID}) that never calls a model. Everything else is the real Sentinel core: config lint, "
    "scope, the Orchestrator's gate machine and safety controls (paper-mode assertion, attestation, the input "
    "gateway, the Watchdog's decisions, the kill switch and the post-run proof), settle, the Diff Engine with A/A "
    "calibration, the Invariant Checker, the Oracle, classification, decisions, the SQLite store, coverage, "
    "canaries and the Reporter. What the rig observes (selector text, settings rows, the order book) and the wall "
    "clock are simulated, not measured; the Oracle reads the text synth drew, where the rig would run OCR.")


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


def _branch_build(sha7: str, pr: int, branch: str, base_sha7: str = "2c91ae4",
                  built_at: str = "2026-11-10T10:24:00-05:00") -> tuple[str, str]:
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
  built_at: {built_at}
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
    later = "2026-11-10T12:58:00-05:00"          # a later push builds later (builds.first_seen_at orders them)
    overlay = dict([_main_build(), _branch_build(sha7, 4812, "feature/TOS-48120-flexgrid-chrome",
                                                 **({"built_at": later} if seq > 1 else {}))])
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


def _backend_pr(sha7: str, pr: int, branch: str, path: str) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """A PR that changes one backend resource and carries no manifest (the no-manifest case (b) of 4.3)."""
    return f"branch-1998.3.0-{sha7}", dict([_main_build(), _branch_build(sha7, pr, branch)]), (path,)


def wrong_number_scenario(today: date) -> Scenario:
    """PR 4851: a pricing resource change transposes two digits of the Order Confirmation cost."""
    cand, overlay, paths = _backend_pr("e5a1c07", 4851, "fix/TOS-48351-cost-format",
                                       "desktop/trade/pricing/src/main/resources/pricing/cost-format.properties")
    return Scenario("wrong-number", f"run-{today.isoformat()}-pr-4851-01",
                    SimBuild(cand, frozenset({"confirm-cost-transposed"})), SimBuild(BASE_ID), pr=4851, author="pchen",
                    overlay=overlay, changed_paths=paths, depends_on={})


def paper_mode_scenario(today: date) -> Scenario:
    """PR 4823 (the chart header change) on a rig whose account selector shows a live-format account id."""
    sc = clean_scenario(today)
    return replace(sc, name="paper-mode-mismatch", rig=RigScript(selector_text=LIVE_ACCOUNT))


def attempted_send_scenario(today: date) -> Scenario:
    """PR 4853: a backend change with no visible effect; the stock ticket flow's Executor presses Enter on the
    open Order Confirmation dialog."""
    cand, overlay, paths = _backend_pr("f2b9d44", 4853, "chore/TOS-48353-ticket-timeouts",
                                       "desktop/trade/orders/src/main/resources/orders/ticket-timeouts.properties")
    return Scenario("attempted-send", f"run-{today.isoformat()}-pr-4853-01", SimBuild(cand), SimBuild(BASE_ID),
                    pr=4853, author="okafor", overlay=overlay, changed_paths=paths, depends_on={},
                    rig=RigScript(enter_after=(STOCK_FLOW, "open-confirm")))


def app_crash_scenario(today: date) -> Scenario:
    """PR 4854: the Active Trader cell's host class changes (declared, no visible change); the app freezes
    during the Ladder S sweep."""
    sha7 = "a7c3e19"
    cand = f"branch-1998.3.0-{sha7}"
    overlay = dict([_main_build(), _branch_build(sha7, 4854, "fix/TOS-48354-active-trader-cell-quantity")])
    overlay[f"manifests/{cand}.yaml"] = f"""schema_version: 1
manifest_id: mf-{cand}
candidate: {cand}
base: {BASE_ID}
source: manual
author: jdoe
entries:
  - entry_id: tos-48354-1
    tickets: [TOS-48354]
    summary: The Active Trader cell keeps its quantity when the cell is resized
    declared_surfaces: [{AT}]
    touched_primitives: []
    changed_paths:
      - "desktop/charts/src/main/java/**/cell/ActiveTraderCellHost.java"
    expected_visual: >
      No visible change at rest; the quantity field keeps its value after a resize.
    feature_flags: []
    must_be_observed: false
"""
    return Scenario("app-crash", f"run-{today.isoformat()}-pr-4854-01", SimBuild(cand), SimBuild(BASE_ID), pr=4854,
                    author="jdoe", overlay=overlay,
                    changed_paths=("desktop/charts/src/main/java/com/tos/desktop/charts/grid/cell/ActiveTraderCellHost.java",),
                    depends_on={}, rig=RigScript(freeze_at=FREEZE_AT))


PS_REGIONS = "regions/census/monitor.activity.position-statement/ws.tier-a.active-trader.yaml"


def broken_ref_overlay(config_dir: Path) -> dict[str, str]:
    """A config edit the lint refuses at run start: the Position Statement's regions file renames its mask reference
    to `msk.ps.last-update-02`, a mask record that does not exist (L-RGN-03, a broken reference)."""
    text = (Path(config_dir) / PS_REGIONS).read_text(encoding="utf-8")
    old = "mask_ref: msk.ps.last-update-01}"
    if old not in text:
        raise ValueError(f"{PS_REGIONS} does not carry the example's mask reference")
    return {PS_REGIONS: text.replace(old, "mask_ref: msk.ps.last-update-02}")}


def config_invalid_scenario(today: date, config_dir: Path = EXAMPLE) -> Scenario:
    """PR 4823 (the chart header change) on a config whose regions file names a mask record that does not exist."""
    sc = clean_scenario(today)
    return replace(sc, name="config-invalid", overlay=dict(sc.overlay, **broken_ref_overlay(config_dir)))


def mask_expiry(config_dir: Path) -> date:
    """The earliest `expires_at` of the config's masks: from the first day after it the mask stops applying at run
    start (4.8 L-MSK-02: an error in CI, ignored at run start), and the run goes on without it."""
    dates = []
    for p in sorted((config_dir / "masks").rglob("*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if doc.get("expires_at"):
            dates.append(datetime.fromisoformat(str(doc["expires_at"])).date())
    if not dates:
        raise ValueError(f"{config_dir} has no mask with an expiry")
    return min(dates)


# The single-PR-run scenarios by name, for `sentinel review` (the Reviewer items of one run)
PR_SCENARIOS: dict[str, Callable[[date], Scenario]] = {
    "flexgrid-ladder": lambda d: flexgrid_scenario(d), "clean-pr": lambda d: clean_scenario(d),
    "backend-only": backend_scenario, "ui-no-manifest": ui_no_manifest_scenario, "wrong-number": wrong_number_scenario,
    "paper-mode-mismatch": paper_mode_scenario, "attempted-send": attempted_send_scenario,
    "app-crash": app_crash_scenario, "operator-stop": lambda d: operator_stop_scenario(d),
    "redaction-miss": lambda d: redaction_miss_scenario(d),
    "popups": lambda d: popups_scenario(d), "fingerprint-mismatch": lambda d: fingerprint_mismatch_scenario(d),
    "clamped-gadget": lambda d: clamped_gadget_scenario(d), "occluded-capture": lambda d: occluded_scenario(d),
}

S03_CHECKPOINT = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s03"


def operator_stop_scenario(today: date) -> Scenario:
    """PR 4856: a backend change with no visible effect; while the stock ticket flow's s03 is captured, an operator
    runs `sentinel stop` on that shard's rig."""
    cand, overlay, paths = _backend_pr("d4c8b21", 4856, "chore/TOS-48356-quote-cache",
                                       "desktop/trade/quotes/src/main/resources/quotes/cache.properties")
    return Scenario("operator-stop", f"run-{today.isoformat()}-pr-4856-01", SimBuild(cand), SimBuild(BASE_ID),
                    pr=4856, author="okafor", overlay=overlay, changed_paths=paths, depends_on={},
                    rig=RigScript(stop_at=S03_CHECKPOINT))


def redaction_miss_scenario(today: date) -> Scenario:
    """PR 4857: a logging change echoes the selected account id on the Order Confirmation, outside the redaction
    anchors, on the candidate only."""
    cand, overlay, paths = _backend_pr("e7f3a90", 4857, "chore/TOS-48357-confirm-logging",
                                       "desktop/trade/orders/src/main/resources/orders/confirm-logging.properties")
    return Scenario("redaction-miss", f"run-{today.isoformat()}-pr-4857-01",
                    SimBuild(cand, frozenset({"confirm-account-echo"})), SimBuild(BASE_ID), pr=4857, author="pchen",
                    overlay=overlay, changed_paths=paths, depends_on={})

S07 = "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml"
S07_MASK = "masks/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07/msk.confirm.clock-01.yaml"


def blind_overlay(config_dir: Path) -> dict[str, str]:
    """A config edit that lint accepts and the canary catches: the Buying Power Effect region loses its
    money_cell flag and its money_role (SD-28) and the dialog's clock mask is pasted with that cell's offset (rect
    dx 24, dy 120)."""
    text = (config_dir / S07).read_text(encoding="utf-8")
    old = "rect: {dx: 24, dy: 120, w: 200, h: 20}, money_cell: true, money_role: bp_effect}"
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
        decided = sum(1 for f in r.findings if f.decision_ref)
        lines.append("Findings stored: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                     + (f"; {decided} of them carry an earlier human decision (the PR comment's 'Suppressed by "
                        "decision' table; a finding keeps its kind, the decision sets the outcome)" if decided else "")
                     + ".")
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
    lines += [n[0].upper() + n[1:] + "." for n in r.notes]
    if r.lint_errors:
        lines.append("Config lint errors:")
        lines += [f"  - {e}" for e in r.lint_errors]
    return lines


INDEX_FILES = ("report.html", "pr-comment.md", "summary.txt", "alerts.json")
FILES = """Files in this folder:
  pr-comment.md       the PR comment Sentinel would post (9.2), exactly as rendered
  report.html         the static run report (9.5); open it in a browser, crops are embedded at 1:1
  commit-status.json  the sentinel/pr-run commit status (9.2)
  sentinel.db         the SQLite store (8.3): runs, outcomes, findings, decisions, step events (with each
                      gateway block and its reason code), artifacts index
  artifacts/          content-addressed crops and heatmaps (8.4) with manifest.json
  calibration/        the A/A calibration file of the run's fingerprint (3.9)
  config/<run id>/    the exact config each run linted and used (the example plus the PR's files)
  reviewer-calls.json every simulated Reviewer request and answer (3.13 shape)
  alerts.json         every 9.10 alert route the Orchestrator's controls returned, per run and shard
  orchestrator.json   per run: each shard's gates, verdict, last gate, proof and kill switch steps, the
                      actions the Orchestrator does not perform itself (quarantine records, blocked logins,
                      a disabled rig, maintenance tasks) and the gateway's step events
  heartbeat/          the last heartbeat each shard wrote (10.2)
  results.jsonl       the store's append-only ledger of every write batch (8.2)
  nightly-canary only: canary-results-<run>.json (10.3 result rows), morning-report.txt (the banner),
                      nightly-report*.html (the Nightly runs' reports)"""


def _write_summary(out: Path, title: str, story: list[str], results: list[RunResult], extra: list[str] = (),
                   *, abort_expected: bool = False) -> None:
    lines = [title, "=" * len(title), "", SIMULATION_NOTE, "", "What this scenario shows", "-" * 24]
    if not abort_expected and any(r.reason == "config_invalid" for r in results):
        lines += ["NOTE: a run below stopped at T+0:00 because config lint failed (1.7): it ended",
                  "aborted_harness (reason: config_invalid) and its PR comment carries the abort block. With the",
                  "default --today 2026-11-10 the example config lints clean; a later date rolls its front-month",
                  "future (an expired mask only stops applying). The story below is what the scenario shows when the",
                  "config is valid.", ""]
    lines += story + ["", "What happened", "-" * 13]
    for r in results:
        lines += _run_block(r) + [""]
    lines += list(extra)
    lines += ["", FILES, ""]
    (out / "summary.txt").write_text("\n".join(lines), encoding="utf-8")


def _count(r: RunResult, surface: str | None = None, outcome: str | None = None, kind: str | None = None) -> int:
    return sum(1 for o in r.outcomes if not o.synthetic and (surface is None or o.surface_id == surface)
               and (outcome is None or o.outcome == outcome) and (kind is None or o.test_kind == kind))




# ------------------------------------------------------------------------------------------------
# Scenarios


def demo_flexgrid_ladder(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                        ) -> list[RunResult]:
    r = run_pipeline(config, flexgrid_scenario(today), out, today, **_rev(reviewer))
    inv_at = _count(r, AT, "invariant_violation", "sweep")
    exhausted = sum(1 for o in r.outcomes if o.stability_cause == "budget")
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
        f"- {_plural(exhausted, 'above-threshold checkpoint')} found the targeted re-run budget of 3.10 (max(30, 10",
        "  percent of the run's checkpoints) re-captured checkpoints, spent in tier order) used up, so their",
        "  classification stands with stability unknown (cause: budget) and run health prints rerun_budget_exhausted.",
        "- Item wording and invariant ids follow from the synthetic screens (INV-01 with INV-02 and INV-10 here;",
        "  9.3 has INV-01 and INV-03 from real OCR).",
    ]
    _write_summary(out, "Scenario flexgrid-ladder: the 1.11 worked example", story, [r], _safety_lines(r))
    return [r]


def demo_clean_pr(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                 ) -> list[RunResult]:
    r = run_pipeline(config, clean_scenario(today), out, today, **_rev(reviewer))
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
    _write_summary(out, "Scenario clean-pr: a declared change and nothing else", story, [r], _safety_lines(r))
    return [r]


def demo_backend_only(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                     ) -> list[RunResult]:
    r = run_pipeline(config, backend_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4830 changes a margin rules file in the backend (no Java UI class, no manifest). The static impact step",
        "finds no UI dependents, so this is the no-manifest case (b) of 4.3 and F5: no manifest_missing finding,",
        "'intended' is disabled, and any diff above threshold needs a human (needs_human, reason no_manifest).",
        "",
        "The change moves the Buying Power Effect on the Order Confirmation dialog from -15,025.00 to -15,032.51.",
        "Two independent checks see it:",
        "- Buying Power Effect is a money cell (F41): it is always pixel-diffed with a zero floor, so the one-glyph",
        "  change is above threshold. On its own that gives needs_human (reason: no_manifest).",
        "- The Oracle reads the dialog and evaluates the flow's assertions. R02 (2.8.3) wants the buying power",
        "  effect of a stock order on the margin account within 50 to 100 percent of the notional, and -15,032.51",
        "  is 100.05 percent of 15,025.00 (\"bracket check\": the margin formula is not pinned yet). An assertion",
        "  outranks every comparison outcome (3.11), so s07 ends assertion_failed (assertion: R02), with both",
        "  builds' reads in the finding and the pixel diff kept on the record.",
        "The dialog checkpoint s07 is recapturable: false in its flow, so its stability is 'unknown'.",
        "",
        "Case (b) still runs the smoke flows (2.11): the scope adds the tier-1 standard flows that touch the smoke",
        "census, so the stock ticket flow reaches the dialog although the change-derived scope is empty.",
    ]
    _write_summary(out, "Scenario backend-only: a money-cell change with no manifest", story, [r], _safety_lines(r))
    return [r]


def demo_ui_no_manifest(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                       ) -> list[RunResult]:
    r = run_pipeline(config, ui_no_manifest_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4835 changes how the Level II cell prints sizes ('2200' becomes '22x100') and carries no manifest.",
        "The changed class sits under the Level II cell's roots, so the static impact step finds a UI dependent:",
        "the no-manifest case (a) of 4.3. The run records a manifest_missing finding, and every diff above",
        "threshold is collateral (reason manifest_missing) until the developer adds a manifest entry.",
        "The Level II cell is tier 2, so the check stays success (tier 2 and 3 never block, 9.2), but the comment",
        "lists the collateral and the missing manifest.",
    ]
    _write_summary(out, "Scenario ui-no-manifest: a UI change without a manifest", story, [r], _safety_lines(r))
    return [r]


def demo_decision_inheritance(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                             ) -> list[RunResult]:
    r1 = run_pipeline(config, flexgrid_scenario(today), out, today, **_rev(reviewer))
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
    r2 = run_pipeline(config, sc2, out, today, **_rev(reviewer))
    story = [
        "PR 4812 runs twice. After the first run a human acts on the PR comment, as 1.11 'What happens next' says:",
        f"- confirm bug on the overlap (ticket TOS-48391): {_plural(len(bugs), 'decision')}, one per member signature;",
        "- mark intended with ticket TOS-48120 on the tier-1 collateral, proposed by the author and confirmed by",
        "  the triage rotation (the two-person rule for tier 1 and 2, 9.6):",
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


def demo_nightly_canary(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                       ) -> list[RunResult]:
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
                                      canary=n2.canary["health"], detector_banner=rep.banner), out, today,
                      **_rev(reviewer))
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


def _safety_lines(r: RunResult) -> list[str]:
    """The Orchestrator's side of a run, for summary.txt: each shard's verdict, last gate and proof, the alerts."""
    lines = [f"Orchestrator: {len(r.shards)} shard(s) through the gate machine of 5.2 (see orchestrator.json):"]
    for sh in r.shards:
        lines.append(f"  - {sh['shard_id']} on {sh['rig']}: {sh['verdict']}" + (f" ({sh['reason']})" if sh["reason"] else "")
                     + f", last gate {sh['last_gate']}, proof {sh['proof']}")
    if r.alerts:
        seen: dict[str, int] = {}
        for a in r.alerts:
            key = (f"{a['priority']} {a['rule']}" + (f" incident {a['incident']}" if a["incident"] else "")
                   + f" paging {', '.join(a['paged']) or 'nobody'}")
            seen[key] = seen.get(key, 0) + 1
        lines.append("Alerts routed by 9.10 (see alerts.json): " + "; ".join(
            k + (f" (x{n}, one per shard)" if n > 1 else "") for k, n in seen.items()) + ".")
    else:
        lines.append("Alerts routed by 9.10: none (alerts.json is empty).")
    return lines


def demo_wrong_number(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                      ) -> list[RunResult]:
    r = run_pipeline(config, wrong_number_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4851 changes a pricing format resource (no UI code, no manifest). The build transposes two digits of the",
        "Order Confirmation cost: 15,205.00 for 100 shares at 150.25. Every other pixel of the dialog is the base's.",
        "",
        "The Oracle reads the dialog's regions (OCR on the rig; here the text synth drew) and evaluates the flow's",
        "assertions on both builds. cost_math is rule R01 (quantity x price x multiplier = cost): it holds on the",
        "base and fails on the candidate, so s07 ends assertion_failed (assertion: cost_math), whatever the pixel",
        "diff says (an assertion outranks every comparison outcome, 3.11). The finding's text and its raw_values",
        "carry what each build read, so the PR comment shows 15,205.00 next to the base's 15,025.00.",
        "R02 (the buying power bracket) still passes: -15,025.00 lies within 50 to 100 percent of 15,205.00.",
    ]
    _write_summary(out, "Scenario wrong-number: the Oracle catches a cost that is not quantity times price", story, [r],
                   _safety_lines(r))
    return [r]


def demo_paper_mode_mismatch(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                             ) -> list[RunResult]:
    r = run_pipeline(config, paper_mode_scenario(today), out, today, **_rev(reviewer))
    story = [
        f"PR 4823's run starts on a rig whose account selector reads {LIVE_ACCOUNT}: the live account id format",
        "(eight digits, then SCHW), not an entry of gateway/allowlist.yaml. The paper-mode assertion of 6.4 runs at",
        "the paper_mode_asserted gate, before settings are read and long before anything is captured.",
        "",
        "- The gate fails with aborted_safety (reason: live_identity): the hook goes to deny-all, the process tree",
        "  is killed, the login is blocked and the 9.10 safety route pages the harness on-call at P1 (incident S1).",
        "- The kill switch of 6.6 runs: a recovery login, the Monitor read and the proof. The run had no order",
        "  snapshot yet (it stopped before one was taken), so the proof is UNPROVEN (snapshot_missing): with",
        "  nothing to compare against it cannot show that no order was sent. UNPROVEN pages a second time",
        "  (safety:unproven_proof) and keeps the login blocked until a human checks the account by hand.",
        "- Every shard's rig shows the same selector (the example config has one rig identity), so every shard",
        "  stops at that gate. Nothing was captured: no calibration, no crops, no diff. Every scoped checkpoint is",
        "  env_error with the aborting gate and reason (gate paper_mode_asserted, reason live_identity).",
        "- The PR comment carries the abort block (trigger, gate, proof, incident) instead of Unexpected and",
        "  Intended; the check is error.",
    ]
    _write_summary(out, "Scenario paper-mode-mismatch: a live account id stops the run before any capture", story, [r],
                   _safety_lines(r))
    return [r]


def demo_attempted_send(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                        ) -> list[RunResult]:
    r = run_pipeline(config, attempted_send_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4853 changes a backend timeout (no visible effect, no manifest), so the smoke flows run. In the stock",
        "ticket flow the scripted step open-confirm clicks Confirm and Send as an attested order-stage step and the",
        "Order Confirmation dialog opens. The scenario then makes the Executor press Enter on the dialog: the one",
        "key that would send the order (a step `config lint` refuses in a flow file, L-FLW-07; here it comes from",
        "the Executor, as a mis-resolved or rescued step would).",
        "",
        "- The gateway blocks it: its slow half refuses the key before any input token is written (reason code",
        "  guarded_key_refused: Enter while a ticket or dialog is open). Nothing reaches the app; the simulated rig",
        "  sent 0 orders.",
        "- One attempted send on a scripted flow is enough (5.4.8 rule 1): aborted_safety (reason: attempted_send),",
        "  the flow is quarantined and the kill switch runs on that shard's rig. Its proof compares the Monitor's",
        "  order grids with the snapshot taken at run start: PROVEN: no orders sent.",
        "- On that shard, s07 and every checkpoint not yet captured are env_error (gate running, reason",
        "  attempted_send); the checkpoints captured before the abort keep their outcomes. The other shard runs on",
        "  its own rig and finishes normally; the run takes the worst shard's result (8.2).",
        "- The blocked step is stored as blocked_by_gateway with the gateway's decision (refused), reason code",
        "  (guarded_key_refused) and class (safety); run health counts it as 1 gateway drop.",
    ]
    _write_summary(out, "Scenario attempted-send: the gateway blocks an Enter on the Order Confirmation", story, [r],
                   _safety_lines(r))
    return [r]


def demo_app_crash(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                   ) -> list[RunResult]:
    r = run_pipeline(config, app_crash_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4854 declares the Active Trader cell (a host class changed; no visible change), so its Ladder S sweep of",
        f"12 steps runs. At step {FREEZE_AT.rsplit('/', 1)[1]} the app's UI thread freezes: the window stops changing.",
        "",
        "- The Watchdog's screenshot-staleness detector fires after max(2 x 15 s, 60 s) (10.2 sampling 5) and",
        "  app_failure decides: assertion_failed (assertion: app_alive) on that step, with an app_crash finding,",
        "  and not_run (reason: app_crash) for the rest of the ladder (7 steps).",
        "- The Executor kills the process tree, waits for the session release, relaunches (one of 3 relaunches",
        "  per shard), logs in and asserts paper mode again, then resumes with the next unit of work: the census",
        "  checkpoints after the ladder are captured and compared as usual.",
        "- The run ends complete; the check fails on the tier-1 app_alive item.",
        "- Coverage: the table counts surface-and-persona cells (2.12) and the ladder's cell shows its worst outcome,",
        "  the app_alive failure, so its not_run column reads 0 app_crash; the line under the table counts the",
        "  checkpoints: \"Checkpoints not run: 7 app_crash (sweep steps after the crash)\".",
    ]
    _write_summary(out, "Scenario app-crash: a frozen app mid-sweep, a relaunch, and the run goes on", story, [r],
                   _safety_lines(r))
    return [r]


def demo_config_invalid(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                        ) -> list[RunResult]:
    r = run_pipeline(config, config_invalid_scenario(today, Path(config)), out, today, **_rev(reviewer))
    story = [
        "PR 4823's run on a config where someone renamed the Position Statement's mask reference to",
        "msk.ps.last-update-02 in its regions file, but no such mask record exists under masks/: a broken reference.",
        "The run-start lint is preflight item G1 (12.7): it fails (L-RGN-03), the Orchestrator stops at the",
        "provisioned gate and the run ends aborted_harness (reason: config_invalid) before any product action: no",
        "launch, no login, nothing captured, no scope.",
        "",
        "- The PR comment carries the abort block with the failing lint rules; the check is error.",
        "- aborted_harness pages the harness on-call at P1 (9.10); see alerts.json.",
        "- No shard started, so the run's one post-run proof is the run-level row (SD-61): PROVEN: no session",
        "  established: the app was never started, so nothing could have been sent, and no login is blocked.",
        "- An expired mask no longer does this: it only stops applying (see the expired-mask scenario).",
    ]
    _write_summary(out, "Scenario config-invalid: a broken reference stops the run at preflight", story, [r],
                   _safety_lines(r), abort_expected=True)
    return [r]


def demo_expired_mask(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                      ) -> list[RunResult]:
    day = mask_expiry(Path(config)) + timedelta(days=1)
    r = run_pipeline(config, replace(clean_scenario(day), name="expired-mask"), out, day, **_rev(reviewer))
    ps = [o for o in r.outcomes if o.checkpoint_id == PS_CENSUS]
    story = [
        f"PR 4823's run on {day.isoformat()}, the day after the example config's first mask (msk.ps.last-update-01,",
        "the Position Statement's last-update clock) expires. In CI that is a lint error (L-MSK-02); at run start it",
        "is not (4.1, 4.8): the run-start lint lists the mask as an expired record, the mask stops applying and the",
        "run goes on.",
        "",
        "- The run is not config_invalid: it plans, captures and reports as on any other day.",
        "- Run health lists the record (\"Expired records: mask expired: msk.ps.last-update-01\") and the run stores a",
        "  mask_expired finding for its owner.",
        "- Without the mask the clock's pixels diff as the enclosing region: the Position Statement census checkpoint "
        + (f"ends {ps[0].outcome}" + (f" ({ps[0].reason})" if ps[0].reason else "") + "." if ps else "is not planned."),
    ]
    _write_summary(out, "Scenario expired-mask: an expired mask stops applying and the run goes on", story, [r],
                   _safety_lines(r))
    return [r]


def demo_operator_stop(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                       ) -> list[RunResult]:
    r = run_pipeline(config, operator_stop_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4856 changes a backend cache setting (no visible effect, no manifest), so the smoke flows run. While the",
        "stock ticket flow's s03 is captured, an operator who sees an unexpected dialog on the rig's console runs",
        f"`sentinel stop <rig> --root {out / 'rigs'}`: the command writes the rig's file flag (6.6).",
        "",
        "- The Orchestrator polls the kill channels first at every step and flow boundary (killflag.check): at the next",
        "  step the flag trips the kill switch, aborted_safety (reason: kill_switch), with the request (who, why, the",
        "  cli channel) on the result. The checkpoints not yet reached are env_error (gate running, reason kill_switch).",
        "- The 6.6 sequence runs: deny-all, the tree killed, a recovery login, Cancel All, the proof, the revert; the",
        "  request is acknowledged (the flag removed). The proof reads PROVEN: no orders sent.",
        "- store.safety persists what the kill switch decided: an S2 incident, the flow quarantined and the rig",
        "  disabled until a person re-enables it (`sentinel reenable`). The other shards finish normally.",
    ]
    _write_summary(out, "Scenario operator-stop: `sentinel stop` trips the kill switch mid-run", story, [r],
                   _safety_lines(r))
    return [r]


def demo_redaction_miss(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                        ) -> list[RunResult]:
    r = run_pipeline(config, redaction_miss_scenario(today), out, today, **_rev(reviewer))
    misses = [f for f in r.findings if f.kind == "redaction_miss"]
    story = [
        "PR 4857 changes an order logging setting. The candidate build echoes the selected account id in the Order",
        "Confirmation's top row, right of the account fields the redaction anchors cover (redaction/anchors.yaml).",
        "",
        "- Capture's redaction stage (3.6, 6.10) runs before anything is diffed or stored: the anchored boxes, then an",
        "  OCR sweep for the fixture's account ids and the id patterns. The sweep finds the echoed id outside every",
        "  anchor and fills it on both captures in the theme's background, so nothing unredacted is stored or sent.",
        f"- The miss is recorded: {len(misses)} redaction_miss finding(s), on the candidate only, so ranked tier 1 as a",
        "  suspected identifier leak; the finding names the place and the pattern, never the id itself.",
        "- A redaction miss never fails the check by itself (3.6): the anchor list needs the new field. Run health's",
        "  Redaction row counts the boxes per side and the misses.",
    ]
    _write_summary(out, "Scenario redaction-miss: an account id outside the anchors is masked and reported", story,
                   [r], _safety_lines(r))
    return [r]


def popups_scenario(today: date) -> Scenario:
    """PR 4851's pricing change, with the Message Center and an unknown window popping up during the ticket flow."""
    return dataclasses.replace(wrong_number_scenario(today), name="popups", run_id=f"run-{today.isoformat()}-pr-4861-01",
                               rig=RigScript(popups=(("s04", "Message Center"), ("s05", "Untitled Tool Window"))))


def fingerprint_mismatch_scenario(today: date) -> Scenario:
    """PR 4823 (the chart header change) whose base half ran with another rasterizer (a different render probe)."""
    return dataclasses.replace(clean_scenario(today), name="fingerprint-mismatch",
                               run_id=f"run-{today.isoformat()}-pr-4862-01",
                               rig=RigScript(base_fingerprint=(("render_probe_hash", "0badc0ffee000000"),)))


def clamped_gadget_scenario(today: date) -> Scenario:
    """PR 4854 (the Active Trader cell host, no visible change) on builds whose cell is at least 400 px tall."""
    sc = app_crash_scenario(today)
    clamp = ((AT, 0, 400),)
    return dataclasses.replace(sc, name="clamped-gadget", run_id=f"run-{today.isoformat()}-pr-4863-01", rig=RigScript(),
                               candidate=dataclasses.replace(sc.candidate, minimums=clamp),
                               base=dataclasses.replace(sc.base, minimums=clamp))


def occluded_scenario(today: date) -> Scenario:
    """PR 4851's pricing change with a foreign window over the screen while s03 and s06 are captured."""
    return dataclasses.replace(wrong_number_scenario(today), name="occluded-capture",
                               run_id=f"run-{today.isoformat()}-pr-4864-01",
                               rig=RigScript(occluded=(S03_CHECKPOINT, f"flow/trade.order-entry.stock/{STOCK_FLOW}/s06")))


def demo_popups(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                ) -> list[RunResult]:
    r = run_pipeline(config, popups_scenario(today), out, today, **_rev(reviewer))
    story = [
        "The stock ticket flow of PR 4851 runs while two windows pop up: the Message Center before s04 and a tool",
        "window nobody catalogued before s05. The popup sentinel sweeps the app's windows before every step and",
        "every capture (4.10, 7.8):",
        "- the Message Center matches POP-06 (dismissable, allowed while running): Escape goes through the input",
        "  guard like any scripted key and the flow goes on;",
        "- the tool window matches nothing: it is closed (WM_CLOSE), an unknown_popup finding carries a proposed",
        "  catalog entry for the registry owner, and the flow is quarantined for later runs.",
        "Every hit is a popup step event with its screenshot, and POP-06's rate on this build (one per run, none on",
        "the base) is a popup_rate_increase finding.",
    ]
    _write_summary(out, "Scenario popups: a catalog popup dismissed, an unknown window closed", story, [r],
                   _safety_lines(r))
    return [r]


def demo_fingerprint_mismatch(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                              ) -> list[RunResult]:
    r = run_pipeline(config, fingerprint_mismatch_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4823's run, but the base half of every shard rendered with another rasterizer: its render probe hash",
        "differs while the Windows build is the same. Before the second half's first capture the pair check compares",
        "the two fingerprints field by field (4.12, L-FPR-03): they differ, so nothing is compared. Every checkpoint",
        "is not_comparable (fingerprint_mismatch) naming the field, the run is complete_with_env_errors, and the",
        "morning note says a rasterization change happened inside the same Windows build (L-FPR-05).",
    ]
    _write_summary(out, "Scenario fingerprint-mismatch: two halves that cannot be compared", story, [r],
                   _safety_lines(r))
    return [r]


def demo_clamped_gadget(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                        ) -> list[RunResult]:
    r = run_pipeline(config, clamped_gadget_scenario(today), out, today, **_rev(reviewer))
    story = [
        "PR 4854 changes the Active Trader cell's host class with no visible change. On both builds the cell will",
        "not go below 400 px tall. The sweep asks for its usual sizes; at 360 and 320 px the cell stays at 400.",
        "Each step is measured (2.5.1): a size above the request that matches the gadget's minimum is size_clamped,",
        "a pass, not a failure. Both builds clamp the same way, so the sweep is identical, and Run health shows the",
        "achieved minimum, clamped on height. A build that moved the minimum would show up as a finding.",
    ]
    _write_summary(out, "Scenario clamped-gadget: a sweep step the gadget will not take", story, [r], _safety_lines(r))
    return [r]


def demo_occluded_capture(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                          ) -> list[RunResult]:
    r = run_pipeline(config, occluded_scenario(today), out, today, **_rev(reviewer))
    story = [
        "While the stock ticket flow's s03 and s06 are captured, a Notepad window covers the screen. Before the settle",
        "loop Capture samples an 8 x 8 grid over the target window (3.4): a sample owned by a process outside the",
        "thinkorswim tree means the capture would show someone else's pixels. Those checkpoints are env_error",
        "(occluded) naming the program, a foreign_window event is logged, and the first sighting of notepad.exe",
        "raises one hardening finding and one alert for the run. The rest of the run is compared as usual.",
    ]
    _write_summary(out, "Scenario occluded-capture: a foreign window over the target", story, [r], _safety_lines(r))
    return [r]


def demo_fixed_reopens(out: Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE, reviewer: Any = None
                       ) -> list[RunResult]:
    from .pipeline.config import SentinelConfig, materialize
    from .pipeline.run import _register
    from .records import Decision
    r1 = run_pipeline(config, flexgrid_scenario(today), out, today, **_rev(reviewer))
    cand2 = "branch-1998.3.0-9c4f1e7"
    with Store(out / "sentinel.db") as store:
        at = f"{today.isoformat()}T11:20:00-05:00"
        bugs = record_decisions(store, r1, lambda o: o.outcome == "invariant_violation", action="confirm_bug",
                                decided_by="jdoe", ticket="TOS-48391", cluster_id="cl-4812-overlap", at=at,
                                reason="Ladder viewport overlaps the order strip at 360 px cell height")
        sc2 = flexgrid_scenario(today, seq=2, sha7="9c4f1e7")
        cfg2 = SentinelConfig.load(materialize(config, sc2.overlay, out / "config" / f"{sc2.run_id}-pre"))
        _register(store, cfg2, [cand2])         # the fix build, as its own run will register it
        fixed = []
        for k, b in enumerate(bugs, 1):          # the Jira poller: TOS-48391 resolved, fixed in the next build (9.9)
            nxt = store.conn.execute("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions").fetchone()[0]
            d = Decision(f"dec-{nxt:06d}", "fixed_in_build", "confirmed_bug", b.signature, decided_by="jira.poller",
                         ticket="TOS-48391", reason="TOS-48391 resolved in " + cand2, run_id=r1.run_id,
                         source="jira_sync", role="ticket_resolver", decided_at=at)
            fixed.append(store.record_decision(d, run_id=r1.run_id, supersedes=b.decision_id,
                                               build_id=flexgrid_scenario(today).candidate.build_id,
                                               fixed_in_build_id=cand2))
    r2 = run_pipeline(config, sc2, out, today, **_rev(reviewer))
    story = [
        "PR 4812 runs, the overlap is confirmed as a bug (TOS-48391), and Jira resolves the ticket as fixed in the",
        f"next build ({cand2}): each confirm_bug becomes a fixed_in_build decision for that build (9.9, SD-79).",
        "The next build is pushed, but the overlap is still there. After the second run Sentinel checks every fixed",
        "signature against what the run compared (L-DEC-08): the overlap came back on a build at or after the fix, so",
        "each fixed_in_build decision is reopened, its finding stays open, and a Jira transition back to open is",
        "queued. Run health lists the reopened fixes; a fix reopened twice is marked suspect.",
    ]
    _write_summary(out, "Scenario fixed-reopens: a fix that did not fix", story, [r1, r2],
                   [f"Decisions: confirm_bug {', '.join(d.decision_id for d in bugs)}; fixed_in_build "
                    f"{', '.join(d.decision_id for d in fixed)}."])
    return [r1, r2]


DEMOS: dict[str, Callable[..., list[RunResult]]] = {
    "flexgrid-ladder": demo_flexgrid_ladder, "clean-pr": demo_clean_pr, "backend-only": demo_backend_only,
    "ui-no-manifest": demo_ui_no_manifest, "nightly-canary": demo_nightly_canary,
    "decision-inheritance": demo_decision_inheritance, "wrong-number": demo_wrong_number,
    "paper-mode-mismatch": demo_paper_mode_mismatch, "attempted-send": demo_attempted_send,
    "app-crash": demo_app_crash, "config-invalid": demo_config_invalid, "expired-mask": demo_expired_mask,
    "operator-stop": demo_operator_stop, "redaction-miss": demo_redaction_miss, "popups": demo_popups,
    "fingerprint-mismatch": demo_fingerprint_mismatch, "clamped-gadget": demo_clamped_gadget,
    "fixed-reopens": demo_fixed_reopens, "occluded-capture": demo_occluded_capture,
}

# One sentence per scenario for out/index.html: what it demonstrates, and what a tester should check.
INDEX = {
    "flexgrid-ladder": ("The 1.11 worked example: a Flexible Grid chrome change makes the Active Trader ladder "
                        "overlap its order strip at height 360.",
                        "the comment lists invariant_violation (INV-01) for the four 360 px steps first, then the "
                        "collateral and the intended chart cells, and the check is failure."),
    "clean-pr": ("A declared change that touches only its declared surface.",
                 "Unexpected is 0, Intended lists the chart cell, and the check is success."),
    "backend-only": ("A backend margin change with no manifest moves the Buying Power Effect, a money cell.",
                     "s07 is assertion_failed (assertion: R02) with both builds' reads, the pixel diff is still above "
                     "threshold on the zero-floor money cell, and the comment says no manifest and no UI code touched."),
    "ui-no-manifest": ("A UI change with no manifest (4.3 case a).",
                       "the Level II cell is collateral (manifest_missing) and the manifest_missing finding is listed."),
    "nightly-canary": ("Two Nightly canary passes, the second blind on a money cell, then a PR run under the banner.",
                       "the morning banner names M04 and the mask that absorbed it, and the PR comment starts with "
                       "DETECTOR BLIND."),
    "decision-inheritance": ("Human decisions recorded after one run are inherited by the next (9.7).",
                             "the second run's comment shows 26 suppressed by decision and the check turns success."),
    "wrong-number": ("The Oracle catches an Order Confirmation cost that is not quantity times price on an otherwise "
                     "pixel-identical dialog.",
                     "s07 is assertion_failed (assertion: cost_math) and the comment shows the candidate's 15,205.00 "
                     "next to the base's 15,025.00."),
    "paper-mode-mismatch": ("A live-format account id in the selector stops the run at the paper-mode gate (6.4).",
                            "the run is aborted_safety (live_identity), nothing was captured, every checkpoint is "
                            "env_error (paper_mode_asserted, live_identity), the comment has the abort block and "
                            "alerts.json has a P1 S1 safety page."),
    "attempted-send": ("The gateway blocks a scripted Enter on the open Order Confirmation dialog (6.2).",
                       "the run is aborted_safety (attempted_send), the step is blocked_by_gateway, the proof reads "
                       "PROVEN: no orders sent and the flow has a quarantine record in orchestrator.json."),
    "app-crash": ("The app freezes during a sweep step; the Watchdog decides, the Executor relaunches and the run goes "
                  "on (10.2).",
                  "that step is assertion_failed (assertion: app_alive) with an app_crash finding, the rest of the "
                  "ladder is not_run (app_crash), counted under the Coverage table as \"Checkpoints not run: 7 "
                  "app_crash\", and the census checkpoints after it were still compared."),
    "config-invalid": ("A broken reference in the config (a mask record that does not exist) fails the run-start "
                       "lint at preflight G1 and the run ends before any product action.",
                       "the run is aborted_harness (config_invalid), the comment's abort block names L-RGN-03 and "
                       "alerts.json has the P1 aborted_harness page."),
    "operator-stop": ("An operator runs `sentinel stop` on a rig mid-run; the kill switch runs and proves no order "
                      "was sent (6.6).",
                      "the run is aborted_safety (kill_switch), the request (who, why, channel cli) is on the kill "
                      "switch result in orchestrator.json, and the proof reads PROVEN: no orders sent."),
    "redaction-miss": ("An account id appears outside the redaction anchors on the candidate only (3.6, 6.10).",
                       "the redaction stage masks it and stores a tier-1 redaction_miss finding (candidate only), the "
                       "Run health row counts the boxes, and the check still passes."),
    "popups": ("Two windows pop up during the stock ticket flow: the Message Center (a catalog popup) and a window "
               "the catalog does not know (4.10, 7.8).",
               "the Message Center is dismissed with Escape through the input guard and the run goes on; the unknown "
               "window is closed, listed as an unknown_popup finding with a proposed catalog entry, and its flow is "
               "quarantined; a popup_rate_increase finding counts POP-06."),
    "fingerprint-mismatch": ("The base half of every shard was rendered by another rasterizer: its render probe hash "
                             "differs (4.12, L-FPR-05).",
                             "every checkpoint is not_comparable (fingerprint_mismatch) with the field named, nothing "
                             "is compared, and Run health says a rasterization change happened inside one Windows "
                             "build."),
    "clamped-gadget": ("The Active Trader cell will not shrink below 400 px tall on either build; the sweep asks for "
                       "smaller sizes anyway (2.5.1).",
                       "the small steps are size_clamped (passed, not failures), the sweep is identical on both "
                       "builds, and Run health shows the achieved minimum, clamped on height."),
    "fixed-reopens": ("A bug is confirmed, Jira marks it fixed in the next build, and the next build still shows it "
                      "(9.9).",
                      "after the second run the fixed_in_build decisions are reopened, a ticket transition is queued, "
                      "and Run health lists the reopened fixes."),
    "occluded-capture": ("A program outside thinkorswim covers the screen while two checkpoints are captured (3.4).",
                         "those checkpoints are env_error (occluded) naming notepad.exe, one hardening finding and one "
                         "alert for the program, and the rest of the run is compared."),
    "expired-mask": ("An expired mask stops applying at run start and the run goes on (4.1, 4.8).",
                     "the run is complete, Run health lists \"mask expired: msk.ps.last-update-01\" and the store "
                     "holds a mask_expired finding."),
}


def _rev(reviewer: Any) -> dict:
    """`run_pipeline` keyword arguments for the Reviewer: none (the simulated one) or a live job."""
    return {} if reviewer is None else {"live_factory": reviewer}       # built per run over its egress gate (6.10)


def run_demo(name: str, out_root: str | Path, today: date = DEFAULT_TODAY, config: Path = EXAMPLE,
             reviewer: Callable[..., Any] | None = None) -> list[RunResult]:
    """Run one scenario into `out_root/<name>/`, replacing what a previous run left there. `reviewer` makes
    the live Reviewer's job for a run (`pipeline.live.live_job`); None keeps the offline simulated one."""
    if name not in DEMOS:
        raise KeyError(f"unknown scenario {name!r}; one of {', '.join(SCENARIOS)} or all")
    out = Path(out_root) / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return DEMOS[name](out, today, Path(config), reviewer)


def _line(name: str, last: RunResult, out: Path) -> str:
    return (f"{name}: {last.status}" + (f" ({last.reason})" if last.reason else "") +
            (f", check {last.commit_status.get('state')}" if last.commit_status else "") + f" -> {out}/summary.txt")


def write_index(out_root: str | Path, results: Mapping[str, list[RunResult]]) -> Path:
    """`out/index.html`: one self-contained page linking each scenario's report, comment, summary and alerts,
    with what each scenario demonstrates and what a tester should check."""
    rows = []
    for name in SCENARIOS:
        if name not in results:
            continue
        last = results[name][-1]
        what, check = INDEX[name]
        state = (last.commit_status or {}).get("state", "n/a")
        links = " ".join(f'<a href="{html.escape(name)}/{f}">{f}</a>' for f in INDEX_FILES)
        rows.append(f"<tr><td><b>{html.escape(name)}</b><br><code>{html.escape(last.status)}"
                    f"{html.escape(f' ({last.reason})' if last.reason else '')}</code>, check {html.escape(state)}</td>"
                    f"<td>{html.escape(what)} Check that {html.escape(check)}</td><td>{links}</td></tr>")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel demo scenarios</title>
<style>
:root {{ --bg: #ffffff; --fg: #1b1f24; --muted: #57606a; --line: #d0d7de; --link: #0b62c4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #0f1216; --fg: #e6edf3; --muted: #9aa5b1; --line: #30363d;
  --link: #6cb6ff; }} }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0 auto;
  max-width: 1100px; padding: 24px 16px; }}
table {{ border-collapse: collapse; width: 100%; }} td {{ border-top: 1px solid var(--line); padding: 10px 8px;
  vertical-align: top; }} td:last-child {{ white-space: nowrap; }} a {{ color: var(--link); margin-right: 8px; }}
p {{ color: var(--muted); }} code {{ font-size: 13px; }}
@media (max-width: 700px) {{ td {{ display: block; border: 0; padding: 4px 0; }} tr {{ display: block;
  border-top: 1px solid var(--line); padding: 8px 0; }} td:last-child {{ white-space: normal; }} }}
</style></head><body>
<h1>Sentinel demo scenarios</h1>
<p>{html.escape(SIMULATION_NOTE)}</p>
<table>
{chr(10).join(rows)}
</table></body></html>
"""
    path = Path(out_root) / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """`sentinel demo <scenario|all> --out DIR [--today YYYY-MM-DD] [--config DIR] [--reviewer simulated|live]`."""
    p = argparse.ArgumentParser(prog="sentinel demo", description=__doc__.split("\n\n")[0])
    p.add_argument("scenario", choices=SCENARIOS + ("all",))
    p.add_argument("--out", default="out", help="output root; each scenario writes <out>/<scenario>/")
    p.add_argument("--today", type=date.fromisoformat, default=DEFAULT_TODAY,
                   help="date for expiries and run ids (default 2026-11-10, which the example config assumes)")
    p.add_argument("--config", default=str(EXAMPLE), help="sentinel-config directory (default config/example)")
    p.add_argument("--reviewer", choices=("simulated", "live"), default="simulated",
                   help="simulated (default, offline) or live: the Claude adapter, only with ANTHROPIC_API_KEY set")
    p.add_argument("--reviewer-config", help="a YAML file whose `reviewer:` block sets the live Reviewer (3.15)")
    a = p.parse_args(argv)
    factory = None
    if a.reviewer == "live":
        from .pipeline.live import live_job, load_reviewer_config
        from .reviewer import NetworkNotAllowed
        settings = load_reviewer_config(a.reviewer_config)
        try:
            live_job(config=settings)                   # refuses before any client exists without a key
        except NetworkNotAllowed as exc:
            print(f"sentinel demo: --reviewer live: {exc}", file=sys.stderr)
            return 2
        factory = lambda gate=None: live_job(config=settings, gate=gate)     # noqa: E731 - one job per run
    names = SCENARIOS if a.scenario == "all" else (a.scenario,)
    done: dict[str, list[RunResult]] = {}
    for name in names:
        try:
            done[name] = run_demo(name, a.out, a.today, Path(a.config), factory)
        except PairRefused as exc:                      # 4.2: the run is never created; the rule ids go back
            print(f"sentinel demo: {name}: {exc} (rules {', '.join(exc.rule_ids)})", file=sys.stderr)
            return 2
        print(_line(name, done[name][-1], Path(a.out) / name))
    if a.scenario == "all":
        print(f"index: {write_index(a.out, done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
