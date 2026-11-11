"""L-XREF-11 on quarantine.yaml flow records (lint/checks/quarantine.py; 4.16, 10.5, SD-96) and the scope reading both
kinds of record (2.11 common_filters)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint
from sentinel.lint.checks import quarantine as qcheck
from sentinel.scope import (DependencyMap, Flow, Manifest, Registry, Rig, Workspace, compute_scope, quarantine_ids,
                            quarantine_records)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
FLOW = "flow.trade.order-ticket.stock.limit"
CP = "census/trade.order-entry.stock/ws.tier-a.swing-trader"
BASE = {"owner": "tos-trade-ui", "ticket": "TOS-48201", "entered": "2026-11-09"}


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def write(root: Path, records: list[dict]) -> None:
    (root / "quarantine.yaml").write_text(yaml.safe_dump({"schema_version": 1, "records": records}, sort_keys=False))


def module(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    qcheck.check(Repo.load(root), lambda rule, file, msg, path="", severity=None: out.append((path, msg)), 0, TODAY)
    return out


def xref11(root: Path) -> list[str]:
    return [f"{v.path}: {v.message}" for v in lint(root, today=TODAY).violations if v.rule == "L-XREF-11"]


def test_a_flow_record_passes_the_module(cfg):
    write(cfg, [{"flow": FLOW, "reason": "fallback_rate", **BASE}, {"checkpoint": CP, "reason": "flake", **BASE}])
    assert module(cfg) == []
    assert "L-XREF-11" in qcheck.RULE_IDS


def test_a_flow_record_lints_clean(cfg):
    write(cfg, [{"flow": FLOW, "reason": "fallback_rate", **BASE}])
    assert xref11(cfg) == []


@pytest.mark.parametrize("record,where,text", [
    ({"checkpoint": CP, "flow": FLOW, "reason": "flake"}, "records[0]", "exactly one of checkpoint and flow"),
    ({"reason": "flake"}, "records[0]", "exactly one of checkpoint and flow"),
    ({"flow": "flow.trade.nowhere", "reason": "gateway_drops"}, "records[0].flow", "not in the flow library"),
])
def test_the_module_refuses_bad_records(cfg, record, where, text):
    write(cfg, [{**record, **BASE}])
    found = module(cfg)
    assert len(found) == 1 and found[0][0] == where and text in found[0][1], found


def test_one_record_per_target(cfg):
    write(cfg, [{"flow": FLOW, "reason": "fallback_rate", **BASE}, {"flow": FLOW, "reason": "gateway_drops", **BASE}])
    assert module(cfg) == [("records[1]", f"{FLOW} is already quarantined by records[0]; one record per checkpoint or flow")]


def test_an_explorer_candidate_cannot_be_quarantined(cfg):
    src = cfg / "flows" / "tier-1" / f"{FLOW}.yaml"
    data = yaml.safe_load(src.read_text())
    data.update(flow_id="flow.explorer.cand-1", **{"class": "explorer_candidate"})
    (cfg / "flows" / "quarantine").mkdir()
    (cfg / "flows" / "quarantine" / "flow.explorer.cand-1.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    write(cfg, [{"flow": "flow.explorer.cand-1", "reason": "flake", **BASE}])
    (found,) = module(cfg)
    assert found[0] == "records[0].flow" and "Explorer candidate" in found[1]


def test_a_reason_outside_10_5_fails_l_xref_11(cfg):
    write(cfg, [{"checkpoint": CP, "reason": "whatever", **BASE}])
    assert any("reason" in v and "whatever" in v for v in xref11(cfg)), xref11(cfg)


def test_the_scope_takes_every_checkpoint_of_a_quarantined_flow_out(cfg):
    write(cfg, [{"flow": FLOW, "reason": "fallback_rate", **BASE}, {"checkpoint": CP, "reason": "flake", **BASE}])
    repo = Repo.load(cfg)
    doc = repo.one("quarantine").data
    assert [(r.target, r.reason) for r in quarantine_records(doc)] == [(FLOW, "fallback_rate"), (CP, "flake")]
    flows = [Flow.from_dict(d.data) for d in repo.of("flow")]
    plan = compute_scope("merge", Manifest.union([], candidate="main-1998.3.0-9a0b1c2", base="main-1998.3.0-2c91ae4",
                                                 author="o"),
                         DependencyMap.from_dict(repo.one("dependency_map").data),
                         Registry.from_entries(d.data for d in repo.of("surface")), run_id="run-2026-11-10-merge-0001",
                         workspaces=[Workspace.from_dict(d.data) for d in repo.of("workspace")], flows=flows,
                         entitlements=("margin", "ira"), quarantine=quarantine_ids(doc),
                         rigs=[Rig(f"rig-{i}", "fp") for i in range(1, 5)])
    flow_cps = {c.checkpoint_id for f in flows if f.flow_id == FLOW for c in f.checkpoints}
    quarantined = {c.checkpoint.checkpoint_id for c in plan.cuts if c.reason == "quarantine"}
    assert flow_cps and flow_cps | {CP} == quarantined
    assert not quarantined & set(plan.planned_ids)
