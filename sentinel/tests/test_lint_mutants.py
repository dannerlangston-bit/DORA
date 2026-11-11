"""L-MUT-03 and L-MUT-04: behavioral coverage of critical checkpoints and the canary flows' order stage (4.11, SD-42,
SD-45)."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import Repo, lint_repo
from sentinel.lint.checks import mutants

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
CANARY = "flows/tier-1/flow.canary.ticket-stock.yaml"
DIALOG = "registry/surfaces/dialog/dialog.order-confirmation.yaml"
TICKET = "registry/surfaces/trade/trade.order-entry.stock.yaml"
ALLOW = "gateway/dismiss_allowlist.yaml"
CATALOG = "canaries/mutants.yaml"
PINNED = {"schema_version": 1, "labels": ["Delete"], "read_on_build": "main-1998.2.0-4f1c9a2", "read_by": "jdoe",
          "read_at": "2026-10-01T12:00:00Z"}
CONFIRM_STEP = {"id": "c05", "type": "click_named", "target": "trade.order-entry.stock.confirm-and-send",
                "order_stage": True}
DELETE_STEP = {"id": "c06", "type": "click_named", "target": "dialog.order-confirmation.delete-button"}


@pytest.fixture
def repo() -> Repo:
    return copy.deepcopy(Repo.load(EXAMPLE))


def found(repo: Repo, phase: int = 1) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    mutants.check(repo, lambda rule, file, message, path="", severity=None: out.append((rule, file, path, message)),
                  phase, TODAY)
    return out


def with_steps(repo: Repo, *extra: dict, pinned: bool = True) -> None:
    repo.docs[CANARY].data["steps"] += [dict(s) for s in extra]
    if pinned:
        repo.docs[ALLOW].data = dict(PINNED)


def test_the_example_is_clean_and_both_rules_run(repo):
    assert found(repo) == []
    report = lint_repo(repo, phase=1, today=TODAY)
    assert "L-MUT-03" not in report.skipped
    assert not [v for v in report.violations if v.rule in mutants.RULE_IDS]


def test_a_critical_surface_without_two_behavioral_entries(repo):
    """SD-42: the Order Confirmation dialog, once critical, has no behavioral entry bound; the stock ticket has four
    (M26, M27, M28 and M30 on its canary flow checkpoint)."""
    repo.docs[DIALOG].data["critical"] = True
    repo.docs[TICKET].data["critical"] = True
    assert found(repo) == [("L-MUT-03", CATALOG, "", "critical surface dialog.order-confirmation: its checkpoints have "
                            "0 behavioral entries bound (none); every critical checkpoint needs at least 2 (SD-42)")]
    errors = [v for v in lint_repo(repo, phase=1, today=TODAY).violations if v.rule == "L-MUT-03"]
    assert [v.severity for v in errors] == ["error"]


def test_a_behavioral_entry_counts_through_its_canary_flow_checkpoints(repo):
    repo.docs[DIALOG].data["critical"] = True
    repo.docs[CANARY].data["steps"].append({
        "id": "cp-dialog", "type": "checkpoint",
        "checkpoint_id": "flow/dialog.order-confirmation/flow.canary.ticket-stock/cp-dialog",
        "surface_id": "dialog.order-confirmation", "target_window": {"kind": "dialog"}, "data_mode": "frozen",
        "regions": "default"})
    assert found(repo) == []                                   # M26, M27, M28, M30 reach the dialog through the flow
    repo.docs[CATALOG].data = [e for e in repo.docs[CATALOG].data if e["mutant_id"] not in ("M26", "M27", "M28")]
    (v,) = found(repo)
    assert v[0] == "L-MUT-03" and "1 behavioral entry bound (M30)" in v[3]


def test_a_canary_flow_may_click_confirm_and_send_once_and_close_with_delete(repo):
    """SD-45: one order_stage Confirm-and-Send step, the dialog closed by Delete through the pinned allowlist."""
    with_steps(repo, CONFIRM_STEP, DELETE_STEP)
    assert found(repo) == []


def test_the_canary_flow_order_stage_rules(repo):
    with_steps(repo, CONFIRM_STEP, dict(CONFIRM_STEP, id="c07"))
    msgs = [m for _r, _f, _p, m in found(repo)]
    assert "canary flow flow.canary.ticket-stock holds 2 order_stage steps; L-MUT-04 allows one, the Confirm and " \
           "Send click" in msgs
    assert any("no later step closes the Order Confirmation dialog with Delete" in m for m in msgs)


@pytest.mark.parametrize("steps,expect", [
    ((dict(CONFIRM_STEP, target="trade.order-entry.stock.qty-field"), DELETE_STEP),
     "order_stage step c05 of canary flow flow.canary.ticket-stock does not click Confirm and Send"),
    ((CONFIRM_STEP, {"id": "c06", "type": "click_named", "target": "dialog.order-confirmation.send-button"},
      DELETE_STEP), "step c06 of canary flow flow.canary.ticket-stock targets Send; a canary flow never does"),
    ((DELETE_STEP, CONFIRM_STEP), "no later step closes the Order Confirmation dialog with Delete"),
])
def test_order_stage_violations(repo, steps, expect):
    with_steps(repo, *steps)
    got = found(repo)
    assert all(r == "L-MUT-04" and f == CANARY for r, f, _p, _m in got)
    assert any(expect in m for _r, _f, _p, m in got), got


def test_delete_needs_the_pinned_dismiss_allowlist(repo):
    with_steps(repo, CONFIRM_STEP, DELETE_STEP, pinned=False)               # the example's allowlist is empty
    ((rule, file, path, message),) = found(repo)
    assert (rule, file, path) == ("L-MUT-04", CANARY, f"steps[{len(repo.docs[CANARY].data['steps']) - 1}]")
    assert "does not carry a pinned Delete label" in message


def test_behavioral_and_image_entries_and_the_wrapper_form(repo):
    entries = repo.docs[CATALOG].data
    m26 = next(i for i, e in enumerate(entries) if e["mutant_id"] == "M26")
    m01 = next(i for i, e in enumerate(entries) if e["mutant_id"] == "M01")
    entries[m26]["canary_flow"] = "flow.trade.order-ticket.stock.limit"
    entries[m01]["canary_flow"] = "flow.canary.ticket-stock"
    repo.docs[CATALOG].data = {"schema_version": 1, "entries": entries}       # SD-47: the 4.11 file form
    assert sorted((p, m) for _r, _f, p, m in found(repo)) == [
        (f"[{m01}].canary_flow", "image mutant M01 carries no canary_flow"),
        (f"[{m26}].canary_flow", "behavioral mutant M26 needs a canary_flow of class canary (got "
                                 "flow.trade.order-ticket.stock.limit)")]
