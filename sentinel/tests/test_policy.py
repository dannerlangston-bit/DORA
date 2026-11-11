"""The operations-file loaders of sentinel.policy (4.16): budgets (11.4), the signed authorization (6.12), soak
budgets (10.11) and CODEOWNERS routing (11.5, L-XREF-26)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sentinel import policy as P
from sentinel.executor.locator import RescueCap
from sentinel.orchestrator.budgets import RATE_CAPS, rate_cap
from sentinel.reviewer.budget import SpendLedger

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
ONCALL = yaml.safe_load((EXAMPLE / "ops" / "oncall.yaml").read_text(encoding="utf-8"))
CODEOWNERS = (EXAMPLE / "CODEOWNERS").read_text(encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    root = tmp_path / "cfg"
    for rel in ("policy", "authorization", "ops"):
        shutil.copytree(EXAMPLE / rel, root / rel)
    for rel in ("soak.yaml", "CODEOWNERS"):
        shutil.copy(EXAMPLE / rel, root / rel)
    return root


def edit_yaml(path: Path, **changes) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


# -- budgets ------------------------------------------------------------------------------------------------------

def test_the_example_budgets_are_the_11_4_caps():
    b = P.load_budgets(EXAMPLE)
    assert b.reviewer_call_caps() == {"pr": 60, "merge": 200, "rc": 500}
    assert (b.usd_per_explorer_session, b.usd_per_run, b.usd_per_week) == (25, 50, 500)
    assert b.above_11_4() == []
    cap = RescueCap.from_budgets(b.rescue_doc())
    assert (cap.max_steps, cap.max_actions, cap.max_seconds) == (25, 25, 90)


def test_a_per_run_cap_of_10_stops_the_spend_ledger_at_10(cfg):
    edit_yaml(cfg / P.BUDGETS_PATH, usd={"per_explorer_session": 25, "per_run": 10, "per_week": 500})
    b = P.load_budgets(cfg)
    ledger = SpendLedger(b.usd_per_run, b.usd_per_week)
    ledger.charge(9.99)
    assert ledger.stop_reason() is None
    ledger.charge(0.01)
    assert ledger.run_ceiling_reached


def test_values_above_11_4_are_listed_and_left_out_keys_take_the_default():
    b = P.Budgets.from_doc({"schema_version": 1, "reviewer_items": {"pr": 80}, "rescue": {"max_seconds": 120}})
    assert b.reviewer_items == {"pr": 80, "merge": 200, "rc": 500} and b.rescue_max_actions == 25
    assert b.above_11_4() == [("reviewer_items.pr", 80, 60), ("rescue.max_seconds", 120.0, 90)]


def test_missing_or_invalid_budgets_raise(cfg):
    (cfg / P.BUDGETS_PATH).unlink()
    with pytest.raises(P.PolicyError, match="missing"):
        P.load_budgets(cfg)
    (cfg / P.BUDGETS_PATH).write_text("schema_version: 1\nusd: {per_run: lots}\n", encoding="utf-8")
    with pytest.raises(P.PolicyError, match="schema"):
        P.load_budgets(cfg)


# -- authorization ------------------------------------------------------------------------------------------------

def test_the_authorization_in_force_carries_the_documents_reference_and_hash():
    a = P.load_authorization(EXAMPLE)
    assert (a.version, a.ref) == ("v1", "authorization/v1.md")
    assert a.sha256 == P.sha256_of(EXAMPLE / "authorization" / "v1.md")
    assert a.account_ids == {"D-20481133", "D-20481134"} and a.send_policy_branch == "default"
    assert a.gateway_rate_caps() == RATE_CAPS
    assert rate_cap({"logins_per_day": 40}, a.gateway_rate_caps()) is not None


def test_a_document_that_does_not_match_its_record_is_refused(cfg):
    (cfg / "authorization" / "v1.md").write_text("an edited document\n", encoding="utf-8")
    with pytest.raises(P.PolicyError, match="document_sha256"):
        P.load_authorization(cfg)
    (cfg / "authorization" / "v1.md").unlink()
    with pytest.raises(P.PolicyError, match="missing"):
        P.load_authorization(cfg)


def test_no_authorization_is_refused(cfg):
    shutil.rmtree(cfg / "authorization")
    with pytest.raises(P.PolicyError, match="no authorization in force"):
        P.load_authorization(cfg)


def test_the_version_in_force_is_the_head_of_the_supersedes_chain():
    assert P.in_force({"v1": {}, "v2": {"supersedes": "v1"}, "v3": {"supersedes": "v2"}}) == ("v3", None)
    version, why = P.in_force({"v1": {}, "v2": {}})
    assert version is None and "v1, v2" in why
    assert P.in_force({"v1": {"supersedes": "v2"}, "v2": {"supersedes": "v1"}})[1].endswith("cycle)")
    assert P.in_force({}) == (None, "no authorization record exists; nothing runs against an account until one does "
                                    "(6.12)")


def test_a_record_naming_another_version_is_refused(cfg):
    edit_yaml(cfg / "authorization" / "v1.yaml", version="v2")
    with pytest.raises(P.PolicyError, match="names version 'v2'"):
        P.load_authorization(cfg, "v1")


# -- soak ---------------------------------------------------------------------------------------------------------

def test_the_example_soak_budgets_are_the_10_11_defaults():
    s = P.load_soak(EXAMPLE)
    assert s.budgets == P.SoakBudgets.defaults().budgets and s.looser_than_default() == []
    assert s.budget("soak.gdi_objects") == {"max": 9000}


@pytest.mark.parametrize("metric, key, value, looser", [
    ("gdi_objects", "max", 9500, True), ("gdi_objects", "max", 8000, False),
    ("render_latency", "windows_pct_min", 95, True), ("render_latency", "windows_pct_min", 99.5, False),
    ("render_latency", "tick_window_s", 3, True), ("handles", "within_pct", 30, True),
    ("growth_slope", "window_h", 2, True), ("growth_slope", "window_h", 3, False),
])
def test_looser_soak_budgets(metric, key, value, looser):
    s = P.SoakBudgets.from_doc({"budgets": {metric: {key: value}}})
    assert bool(s.looser_than_default()) is looser


def test_missing_soak_raises(cfg):
    (cfg / P.SOAK_PATH).unlink()
    with pytest.raises(P.PolicyError, match="missing"):
        P.load_soak(cfg)


# -- release gate -------------------------------------------------------------------------------------------------

def test_the_release_gate_gives_n_per_train_and_is_assumed_without_a_file(cfg):
    gate = P.load_release_gate(EXAMPLE)
    assert (gate.default_business_days, gate.owner, gate.assumed, gate.header_line()) == (3, "release.manager", False,
                                                                                         None)
    edit_yaml(cfg / P.RELEASE_GATE_PATH, trains=[
        {"train": "1999.1", "ga_date": "2027-01-20", "rc_cut_date": "2027-01-08", "business_days": 5},
        {"train": "1999.2", "ga_date": "2027-03-17", "rc_cut_date": "2027-03-05"}])
    gate = P.load_release_gate(cfg)
    assert (gate.business_days("1999.1"), gate.business_days("1999.2"), gate.business_days()) == (5, 3, 3)
    assert gate.train("1999.2").ga_date == "2027-03-17"
    (cfg / P.RELEASE_GATE_PATH).unlink()
    gate = P.load_release_gate(cfg)
    assert gate.assumed and gate.business_days("1999.1") == 3
    assert gate.header_line() == ("release gate assumed: N = 3 business days and an RC cut at G-7 "
                                  "(policy/release_gate.yaml is missing)")


# -- CODEOWNERS ---------------------------------------------------------------------------------------------------

def test_codeowners_patterns_follow_the_github_file():
    rules = P.parse_codeowners("* @all\n/flows/tier-1/ @t1  # tier one\ndocs/ @docs\n*.md @md\n/policy/** @pol\n")
    assert P.owners_of(rules, "flows/tier-1/a.yaml") == ("@t1",)
    assert P.owners_of(rules, "flows/tier-2/a.yaml") == ("@all",)
    assert P.owners_of(rules, "x/docs/a.txt") == ("@docs",)          # unanchored: any depth
    assert P.owners_of(rules, "authorization/v1.md") == ("@md",)
    assert P.owners_of(rules, "policy/a/b.yaml") == ("@pol",)
    assert P.owners_of(P.parse_codeowners("/rig/ @r\n"), "registry/x.yaml") is None


def test_the_example_codeowners_routes_every_file():
    assert P.codeowners_check(CODEOWNERS, ONCALL) == []


def test_a_missing_tier_route_and_a_wrong_owner_are_reported():
    text = CODEOWNERS.replace("/flows/tier-1/                          @tos-trade-ui\n", "")
    text = text.replace("/soak.yaml                              @asmith", "/soak.yaml @hnovak")
    problems = P.codeowners_check(text, ONCALL)
    assert [p.path for p in problems] == ["flows/tier-1/", "soak.yaml"]
    assert "not to the system owner asmith" in problems[1].message


def test_a_role_the_roster_does_not_name_cannot_be_routed():
    oncall = {**ONCALL, "roles": [r for r in ONCALL["roles"] if r["role"] != "release_manager"]}
    (problem,) = P.codeowners_check(CODEOWNERS, oncall)
    assert problem.path == "policy/release_gate.yaml" and "names no release_manager" in problem.message
