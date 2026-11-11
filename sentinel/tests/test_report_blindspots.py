"""The "Not detectable by design" table (2.13, 9.4 item 6) over the example config."""
from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest

from sentinel.lint.repo import Doc, Repo
from sentinel.pipeline.config import SentinelConfig
from sentinel.records import CheckpointOutcome, Finding
from sentinel.report.blindspots import (KNOWN_GAPS_LINE, PLATFORM_LINE, TITLES, compute, masked_share,
                                        unobserved_steps)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
RUN = "merge-2026-11-10"
WS = "ws.tier-a.active-trader"


@pytest.fixture(scope="module")
def cfg() -> SentinelConfig:
    return SentinelConfig.load(EXAMPLE)


def out(cid: str, outcome: str = "identical", *, mode: str = "frozen", ws: str = WS, **kw) -> CheckpointOutcome:
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=outcome, workspace_id=ws, persona="active_trader",
                             data_mode=mode, **kw)


def test_every_b_id_is_printed_in_order_with_its_mechanism(cfg):
    b = compute(cfg, [])
    assert [r.id for r in b.rows] == [f"B{n:02d}" for n in range(1, 17)] == list(TITLES)
    assert all(r.mechanism and r.lines for r in b.rows)
    assert b.row("B01").lines == ("send policy: default (no sends); 0 order-lifecycle surfaces not run (entitlement)",)
    assert b.row("B07").lines == (PLATFORM_LINE,) == ("platform: Windows 11 desktop only",)
    assert b.row("B11").lines == ("cross-client sync: not observed",)
    assert b.header_lines() == ("configurations covered: 0 of 1 planned", PLATFORM_LINE)
    assert b.row("B04").lines[:2] == ("profiles: margin (margin), ira (ira)", KNOWN_GAPS_LINE)
    assert b.row("B09").lines[0].startswith("accessible tree: diffed build to build")      # DR-01 branch A


def test_b15_counts_dialog_order_confirmation_by_its_flow_checkpoints(cfg):
    assert compute(cfg, []).row("B15").lines == ("registered dialogs with zero flow checkpoints: 0 of 1",)
    unreached = [f for f in cfg.flows if all(c.surface_id != "dialog.order-confirmation" for c in f.checkpoints)]
    assert len(unreached) < len(cfg.flows)
    b = compute(dataclasses.replace(cfg, flows=unreached), [])
    assert b.row("B15").lines == ("registered dialogs with zero flow checkpoints: 1 of 1 (dialog.order-confirmation)",)


def test_b16_lists_steps_with_no_checkpoint_or_assertion_within_three_steps(cfg):
    b16 = compute(cfg, []).row("B16")
    table = {row[0]: row[1:] for row in b16.table}
    assert table["flow.fixture-reset.profile"] == ("2", "2", "f01, f02")
    assert table["flow.trade.order-ticket.stock.limit"] == ("7", "0", "")
    assert b16.lines == ("tier-1 steps observed: 16 of 18 (88.9 percent)",)
    doc = {"steps": [{"id": "a", "type": "click_named"}, {"id": "b", "type": "press"}, {"id": "c", "type": "press"},
                     {"id": "d", "type": "press"}, {"id": "e", "type": "press", "verify": {"region": "r"}},
                     {"id": "f", "type": "press"}, {"id": "cp", "type": "checkpoint"}]}
    assert unobserved_steps(doc) == (6, ["a"])          # b to d see e's verify; e verifies itself; f sees cp


def test_b05_counts_frozen_and_masked_live_per_tier_and_b06_the_configurations(cfg):
    outs = [out(f"census/trade.active-trader.ladder/{WS}"), out(f"census/trade.order-entry.stock/{WS}"),
            out(f"census/monitor.activity.position-statement/{WS}", mode="masked_live"),
            out("sweep/charts.flexible-grid.chart-cell/flexgrid/480x360", tier=2),
            out(f"census/trade.order-entry.stock/{WS}", mode="masked_live", synthetic=True, mutant_id="M01")]
    b = compute(cfg, outs)
    assert b.row("B05").lines == ("tier 1: frozen 2, masked_live 1", "tier 2: frozen 1, masked_live 0")
    assert b.row("B06").lines == ("configurations covered: 1 of 1 planned",)


def test_b02_oracle_coverage_and_b03_masked_surfaces(cfg):
    b = compute(cfg, [])
    b02 = {row[0]: row[1:] for row in b.row("B02").table}
    assert b02["dialog.order-confirmation"] == ("13", "11", "2")   # the title and the reversed side carry no rule
    assert set(b02) == {s.id for s in cfg.registry if s.tier == 1}
    (row,) = b.row("B03").table
    assert row[:2] == ("monitor.activity.position-statement", "7") and row[3] == "no"
    doc = {"regions": [{"id": "a", "type": "diff", "landmark": "l", "panel": "p", "rect": {"dx": 0, "dy": 0, "w": 10,
                                                                                           "h": 10}},
                       {"id": "m", "type": "mask", "landmark": "l", "panel": "p", "rect": {"dx": 5, "dy": 0, "w": 5,
                                                                                           "h": 10}}]}
    assert masked_share(doc) == 0.5


def test_b13_env_error_gates_b14_log_findings_and_b10_the_browser_log(cfg):
    outs = [out(f"census/trade.order-entry.stock/{WS}", "env_error", reason="gate", gate="authenticated"),
            out(f"census/trade.active-trader.ladder/{WS}", "env_error", reason="gate", gate="authenticated"),
            out("flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07", "assertion_failed",
                reason="assertion:app_alive", assertion_ids=["app_alive"], tier=1)]
    finding = Finding(finding_id="f-1", run_id=RUN, kind="new_log_signature", description="NPE in QuoteFeed",
                      first_seen_run_id=RUN, checkpoint_id=f"census/trade.order-entry.stock/{WS}", tier=2)
    b = compute(cfg, outs, findings=[finding], browser_log=["12:01 open https://example.invalid/help"])
    assert b.row("B13").lines == ("env_error: 2 (authenticated 2); counted in run health, never in coverage",)
    assert b.row("B14").lines == ("assertion_failed (assertion: app_alive): 1; app_crash findings: 0; "
                                  "new_log_signature findings: 1",)
    assert b.row("B10").lines == ("browser stub log: 1 launch", "12:01 open https://example.invalid/help")


def test_b09_without_the_access_bridge_prints_not_available(cfg):
    docs = dict(cfg.repo.docs)
    rel = "phase0/branches.yaml"
    data = copy.deepcopy(docs[rel].data)
    data["jab"] = {"branch": "B"}
    docs[rel] = Doc(rel, "branches", data)
    b = compute(dataclasses.replace(cfg, repo=Repo(cfg.repo.root, docs)), [])
    assert b.row("B09").lines == ("accessible tree: not available",)
