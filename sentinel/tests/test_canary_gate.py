"""The CI canary gate (sentinel.canary_gate; 10.3 Gate and governance, 10.4, 12.5, 10.10 Part C): which changes need
it, and the gate itself through the Nightly canary path on the example config (a few mutants, for speed)."""
from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from sentinel import canary_gate as G
from sentinel.change_control import ReviewerTuple
from sentinel.demo import EXAMPLE, blind_night_overlay
from sentinel.pipeline.config import materialize

TODAY = "2026-11-10"
T = ReviewerTuple("claude-opus-5-5", "sha256:" + "a" * 64, 1, (), "high", {"effort": "high"}, "crop-1", "redaction-1-a")
ATCELL = "regions/sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360.yaml"


# -- which changes need the gate (10.3, 12.5) -----------------------------------------------------------------------

@pytest.mark.parametrize("path, trigger", [
    ("masks/census/monitor.activity.position-statement/ws.tier-a.active-trader/msk.ps.last-update-01.yaml", "masks"),
    ("sentinel/config/example/masks/x.yaml", "masks"),
    ("regions/landmarks/lm.ladder-header.png", "region_registry"),
    ("regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml", "region_registry"),
    ("calibration/fp-9e1f03.yaml", "thresholds"),
    ("sentinel/config/diff_engine.yaml", "thresholds"),
    ("redaction/anchors.yaml", "redaction"),
    ("sentinel/src/sentinel/capture/redaction.py", "redaction"),
    ("sentinel/src/sentinel/reviewer/prompt.py", "reviewer_prompt"),
    ("src/sentinel/reviewer/schema.py", "reviewer_prompt"),
    ("src/sentinel/reviewer/images.py", "downscale"),
    ("./phase0/branches.yaml", "model_id"),
])
def test_a_change_to_a_trigger_input_needs_the_gate(path, trigger):
    need = G.canary_gate_needed([path, "docs/GUIDE.md"])
    assert need and need.needed and list(need.triggers) == [trigger]


@pytest.mark.parametrize("path", ["docs/GUIDE.md", "registry/surfaces/trade/order-entry.stock.yaml",
                                  "flows/trade/flow.trade.order-ticket.stock.limit.yaml",
                                  "src/sentinel/diff/regions.py", "manifests/branch-1998.3.0-5d3e812.yaml"])
def test_a_change_to_anything_else_does_not(path):
    need = G.canary_gate_needed([path])
    assert not need and need.text() == "no trigger input changed"


def test_with_both_tuples_the_branches_file_counts_only_when_the_tuple_moves():
    assert not G.canary_gate_needed(["phase0/branches.yaml"], reviewer=(T, T))
    moved = G.canary_gate_needed(["phase0/branches.yaml"], reviewer=(T, dataclasses.replace(T, model_id="claude-x")))
    assert dict(moved.triggers) == {"model_id": ("model_id",)}
    shots = G.canary_gate_needed([], reviewer=(T, dataclasses.replace(T, few_shot_ids=("dev-a-1",))))
    assert list(shots.triggers) == ["few_shot_set"]
    crop = G.canary_gate_needed([], reviewer=(T, dataclasses.replace(T, crop_policy_version="crop-2")))
    assert list(crop.triggers) == ["downscale"]
    prompt = G.canary_gate_needed([], reviewer=(T, dataclasses.replace(T, prompt_sha256="sha256:" + "b" * 64)))
    assert list(prompt.triggers) == ["reviewer_prompt"]
    assert list(G.canary_gate_needed([], reviewer=(None, T)).triggers) == ["model_id"]
    assert not G.canary_gate_needed([], reviewer=(None, None))


def test_a_redaction_version_change_and_every_calibration_run_need_the_gate():
    assert list(G.canary_gate_needed([], redaction=("redaction-1-a", "redaction-1-b")).triggers) == ["redaction"]
    assert not G.canary_gate_needed([], redaction=("redaction-1-a", "redaction-1-a"))
    assert list(G.canary_gate_needed([], calibration_run=True).triggers) == ["calibration_run"]


def test_the_gate_builds_on_the_newest_main_build():
    assert G.main_build(EXAMPLE) == "main-1998.2.0-4f1c9a2"


def test_a_config_without_a_main_build_cannot_run_the_gate(tmp_path, capsys):
    cfg = materialize(EXAMPLE, {}, tmp_path / "cfg")
    for p in (cfg / "builds").glob("main-*.yaml"):
        p.unlink()
    assert G.main(["--today", TODAY, str(cfg), "--out", str(tmp_path / "out")]) == 2
    assert "no main channel build" in capsys.readouterr().err


# -- the gate (10.3, 10.10 Part C) ----------------------------------------------------------------------------------

def gate(cfg: Path, out: Path, *extra: str) -> tuple[int, dict]:
    rc = G.main([str(cfg), "--out", str(out), "--mutants", "M04,M12", "--today", TODAY, *extra])
    return rc, json.loads((out / G.RESULT_FILE).read_text())


def test_the_clean_example_passes(tmp_path):
    rc, result = gate(EXAMPLE, tmp_path, "--changed", "masks/census/x.yaml")
    assert rc == 0 and result["passed"] and result["state"] == "ok"
    assert {r["mutant_id"]: r["state"] for r in result["results"]} == {"M04": "detected", "M12": "detected"}
    assert result["triggers"] == {"masks": ["masks/census/x.yaml"]} and result["catalog_version"]


def test_a_mask_that_absorbs_a_money_cell_mutant_fails_the_gate_and_names_the_mask(tmp_path, capsys):
    """The blind overlay of the nightly-canary demo pastes a mask over the Buying Power Effect cell (and drops R02):
    M04 ends identical, the gate prints the 10.3 banner and the mask that absorbed it, and exits non-zero."""
    cfg = materialize(EXAMPLE, blind_night_overlay(EXAMPLE), tmp_path / "cfg")
    rc, result = gate(cfg, tmp_path / "out")
    out = capsys.readouterr().out
    assert rc == 1 and not result["passed"] and result["state"] == "detector_blind"
    assert result["message"].startswith("DETECTOR BLIND since 2026-11-10 03:12 ET.\nMissed 1 of 1 money-cell mutants: "
                                        "M04 swapped_digit")
    assert "Absorbed by: mask msk.confirm.clock-01 (region rgn.confirm.clock)." in result["message"]
    assert "M04 swapped_digit on flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07: " \
           "detector_blind, produced identical; absorbed by mask msk.confirm.clock-01" in out
    assert G.FAIL_TAIL in out


def test_a_branch_config_that_fails_lint_fails_the_gate_with_its_errors(tmp_path):
    """A mask over rgn.ladder.viewport on the frozen Flexible Grid cell is refused by L-RGN-07, so the catalog cannot
    run on the branch config at all: the gate fails and lists the error."""
    text = (EXAMPLE / ATCELL).read_text()
    text += ("  - {id: rgn.ladder.viewport-mask, type: mask, landmark: lm.atcell-header, "
             "rect: {dx: 0, dy: 22, w: 300, h: 250}, mask_ref: msk.atcell.viewport-01}\n")
    cfg = materialize(EXAMPLE, {ATCELL: text}, tmp_path / "cfg")
    rc, result = gate(cfg, tmp_path / "out")
    assert rc == 1 and not result["passed"] and result["state"] is None
    assert result["message"].startswith("CANARY GATE FAILED: the mutant catalog did not run on the branch config")
    assert "L-RGN-07" in result["message"] and "rgn.ladder.viewport-mask" in result["message"]


def test_a_catalog_that_generates_no_mutant_fails_the_gate(tmp_path):
    cat = (EXAMPLE / "canaries/mutants.yaml").read_text()
    cfg = materialize(EXAMPLE, {"canaries/mutants.yaml": cat.replace("layer: rgn.ladder.viewport, fill",
                                                                     "layer: rgn.ladder.nowhere, fill", 1)},
                      tmp_path / "cfg")
    rc = G.main([str(cfg), "--out", str(tmp_path / "out"), "--mutants", "M12", "--today", TODAY])
    result = json.loads((tmp_path / "out" / G.RESULT_FILE).read_text())
    assert rc == 1 and result["state"] == "generation_failed"
    assert result["message"].startswith("CANARY GATE FAILED: the mutant catalog did not run on the branch config: "
                                        "the stage generated no mutants.")


def test_a_change_that_touches_no_trigger_skips_the_gate(tmp_path, capsys):
    rc, result = gate(EXAMPLE, tmp_path, "--changed", "docs/GUIDE.md", "flows/x.yaml")
    assert rc == 0 and result == {"needed": False, "passed": True, "triggers": {},
                                  "message": "canary gate not needed: no trigger input changed"}
    assert not (tmp_path / "run").exists()


def test_the_base_checkout_lets_an_unrelated_branches_edit_skip_the_gate(tmp_path):
    rc, result = gate(EXAMPLE, tmp_path, "--changed", "phase0/branches.yaml", "--base-config", str(EXAMPLE))
    assert rc == 0 and result["needed"] is False


def test_run_gate_refuses_a_missing_directory(tmp_path):
    with pytest.raises(G.GateError):
        G.run_gate(tmp_path / "nowhere", tmp_path, today=date(2026, 11, 10))
