"""2.6.3 flow classes: L-SRF-12 (paper_send on surfaces reached only after a sent order) and the minimum-set report
(sentinel.lint.checks.flow_classes)."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Callable

import pytest

from sentinel.lint import Repo, lint_repo
from sentinel.lint.checks import flow_classes as F
from sentinel.lint.repo import Doc

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
PS = "registry/surfaces/monitor/monitor.activity.position-statement.yaml"
READBACK = "flows/tier-1/flow.trade.active-trader.read-back.yaml"
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
CHART = "registry/surfaces/charts/charts.flexible-grid.chart-cell.yaml"
ODD = "corpus/odd-inputs.yaml"
CHAIN_FLOW = "flow.analyze.chain.layouts"


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def changed(base: Repo, fn: Callable[[Repo], None]) -> Repo:
    r = copy.deepcopy(base)
    fn(r)
    return r


def srf12(r: Repo) -> list:
    return [v for v in lint_repo(r, 1, TODAY).violations if v.rule == "L-SRF-12"]


def test_a_surface_reached_after_send_needs_paper_send(base):
    r = changed(base, lambda r: r.docs[PS].data.update(reached_after_send=True))
    got = srf12(r)
    assert {(v.file, v.path) for v in got} == {(PS, "entitlements"), (READBACK, "requires")}
    def fixed(r: Repo) -> None:
        r.docs[PS].data.update(reached_after_send=True)
        r.docs[PS].data["entitlements"].append("paper_send")
        r.docs[READBACK].data["requires"].append("paper_send")
    assert srf12(changed(base, fixed)) == []


def test_the_example_minimum_set_report(base):
    gaps = F.minimum_set(base)
    assert all(isinstance(g, F.ClassGap) for g in gaps)
    assert not [g for g in gaps if g.flow_class in ("Chain", "Export", "Odd input")]
    ticket = {g.missing for g in gaps if g.flow_class == "Ticket"}
    assert ticket <= {"an after-input assertion of the ticket's symbol", "an after-input assertion of the ticket's TIF"}
    assert {g.subject for g in gaps if g.flow_class == "Migration"} == {"ws.tier-b.dense-charts", "ws.tier-b.options-sidebar"}


def _chain(r: Repo, rules: tuple[str, ...]) -> None:
    r.docs[CHART].data["kind"] = "chain_layout"
    fl = copy.deepcopy(r.docs[READBACK].data)
    fl.update(flow_id="flow.analyze.chain.layouts", surfaces=[*fl["surfaces"], "charts.flexible-grid.chart-cell"])
    fl["steps"].insert(1, {"id": "a-chain", "type": "assert", "assertions": [
        {"id": f"chain_{x.lower()}", "kind": "rule", "rule_id": x, "inputs": {}} for x in rules]})
    r.docs["flows/tier-2/flow.analyze.chain.layouts.yaml"] = Doc("flows/tier-2/flow.analyze.chain.layouts.yaml", "flow", fl)


def test_a_chain_flow_without_r04_is_reported(base):
    gaps = F.minimum_set(changed(base, lambda r: _chain(r, ("R03", "R05"))))
    assert [g.missing for g in gaps if g.flow_class == "Chain" and g.subject == CHAIN_FLOW] == ["rule R04"]
    assert not [g for g in F.minimum_set(changed(base, lambda r: _chain(r, F.CHAIN_RULES))) if g.subject == CHAIN_FLOW]


def test_an_export_flow_without_r09_is_reported(base):
    def fn(r: Repo) -> None:
        r.docs[READBACK].data["steps"].insert(1, {"id": "x-export", "type": "export",
                                                  "target": "monitor.activity.position-statement.grid", "kind": "copy_all",
                                                  "headers": ["Symbol"]})
    assert [g.subject for g in F.minimum_set(changed(base, fn)) if g.flow_class == "Export"] == [
        "flow.trade.active-trader.read-back"]


def test_a_ticket_flow_missing_its_confirmation_and_cancel_assertions(base):
    def fn(r: Repo) -> None:
        s07 = next(s for s in r.docs[FLOW].data["steps"] if s["id"] == "s07")
        s07["expect"] = [a for a in s07["expect"] if a["id"] not in ("max_loss", "R02")]
        r.docs[FLOW].data["teardown"] = [t for t in r.docs[FLOW].data["teardown"] if t["id"] != "t3"]
    missing = {g.missing for g in F.minimum_set(changed(base, fn)) if g.flow_class == "Ticket"}
    assert {"an Order Confirmation assertion of the max loss", "an Order Confirmation assertion of the buying power effect",
            "an absent assertion proving the ticket empty after Cancel"} <= missing


def test_an_odd_input_corpus_missing_a_class_and_the_halt_list(base):
    def fn(r: Repo) -> None:
        r.docs[ODD] = Doc(ODD, "odd_inputs", {"schema_version": 1, "corpus_version": 1, "seed": 1, "entries": [
            {"id": "sym-empty", "field_class": "symbol_box", "value": "", "expect": "rejected"}]})
    got = {g.missing for g in F.minimum_set(changed(base, fn)) if g.flow_class == "Odd input"}
    assert got == {"order_field entries", "analyze_input entries", "study_parameter entries",
                   "a halt-list entry (the halted or delisted symbol)"}
