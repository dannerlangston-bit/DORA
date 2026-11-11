"""L-XREF-24 (schedule/windows.yaml, requires_window, the crypto-futures profile) and L-XREF-25
(ops/credential_rotation.yaml) as lint/checks/windows.py reports them (4.15, 4.16, 7.7, 7.10)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint
from sentinel.lint.checks import windows as wcheck

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
S03 = "regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s03.yaml"
CP = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s03"
LOGIN = "vault://sentinel/rigs/rig-01/tos-login"


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def load(root: Path, rel: str):
    return yaml.safe_load((root / rel).read_text())


def save(root: Path, rel: str, data) -> None:
    (root / rel).write_text(yaml.safe_dump(data, sort_keys=False))


def module(root: Path) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    wcheck.check(Repo.load(root), lambda rule, file, msg, path="", severity=None: out.append((rule, file, path, msg)),
                 0, TODAY)
    return out


def test_the_example_is_clean_and_both_rules_run(cfg):
    assert module(cfg) == []
    report = lint(cfg, today=TODAY)
    assert {"L-XREF-24", "L-XREF-25"} <= set(wcheck.RULE_IDS)
    assert "L-XREF-24" not in report.skipped and "L-XREF-25" not in report.skipped
    assert not [v for v in report.violations if v.rule in ("L-XREF-24", "L-XREF-25")]


def test_a_missing_windows_file_is_an_error(cfg):
    (cfg / "schedule" / "windows.yaml").unlink()
    assert module(cfg) == [("L-XREF-24", "schedule/windows.yaml", "", "schedule/windows.yaml is missing; the "
                            "scheduler, the operating day and the maintenance reschedule read it (4.1, 7.10)")]


def test_windows_file_problems_report_through_the_full_lint(cfg):
    doc = load(cfg, "schedule/windows.yaml")
    doc["operating_day"] = {"start": "22:00", "end": "06:00"}
    save(cfg, "schedule/windows.yaml", doc)
    found = [(v.path, v.message) for v in lint(cfg, today=TODAY).violations if v.rule == "L-XREF-24"]
    assert found == [("operating_day", "the operating day starts at 22:00, not before it ends at 06:00")]


def test_requires_window_must_name_a_defined_window(cfg):
    doc = load(cfg, "schedule/windows.yaml")
    del doc["windows"]["futures_break"]
    save(cfg, "schedule/windows.yaml", doc)
    flow = load(cfg, FLOW)
    flow["requires_window"] = "futures_break"
    save(cfg, FLOW, flow)
    found = [(f, p, m) for r, f, p, m in module(cfg)]
    assert ("schedule/windows.yaml", "windows.futures_break", "the named window futures_break is not defined (7.10)") \
        in found
    assert (FLOW, "requires_window", "requires_window futures_break is not a window of schedule/windows.yaml") in found


def crypto(cfg: Path, *, window: str | None, rule_region: bool, quote: str) -> None:
    """Point the stock ticket's s03 at a /MBT future, masked_live, with a quote cell read by `api_within`."""
    fx = load(cfg, "fixtures/fx.rig-01.paper.yaml")
    fx["symbols"].append({"id": "mbt_front", "symbol": "/MBTZ26", "asset": "future", "purpose": ["ticket"],
                          "roll_date": "2026-12-18"})
    save(cfg, "fixtures/fx.rig-01.paper.yaml", fx)
    flow = load(cfg, FLOW)
    flow["steps"][1]["value_ref"] = flow["steps"][1]["verify"]["value_ref"] = "fixture.symbols.mbt_front.symbol"
    s03 = flow["steps"][3]
    s03["data_mode"] = "masked_live"
    if window:
        s03["requires_window"] = window
    s03["expect"].append({"id": "last_vs_api", "kind": "api_within", "region": "rgn.order-entry.last", "tol": 0.5,
                          "api_field": "last"})
    save(cfg, FLOW, flow)
    regions = load(cfg, S03)
    regions["regions"].append({"id": "rgn.order-entry.last", "type": quote, "landmark": "lm.order-entry-header",
                               "rect": {"dx": 330, "dy": 0, "w": 60, "h": 20}})
    if rule_region:
        regions["regions"][-1]["rule"] = {"field": "price", "decimals": 0}
    save(cfg, S03, regions)


def test_a_diff_quote_region_on_a_mbt_checkpoint_fails(cfg):
    crypto(cfg, window="all_quiet", rule_region=False, quote="diff")
    found = [(p, m) for r, f, p, m in module(cfg) if f == FLOW]
    assert found == [
        ("steps[3].requires_window", f"{CP} shows crypto future /MBTZ26, which has no quiet window (always live, "
                                     "7.10); requires_window all_quiet never opens for it"),
        ("steps[3].regions", f"{CP} shows crypto future /MBTZ26: the always-live profile needs rule regions (7.10)"),
        ("steps[3].regions", f"{CP} shows crypto future /MBTZ26: quote cell rgn.order-entry.last is diff; the "
                             "always-live profile never pixel-diffs a quote cell, declare it rule (7.10)")]


def test_the_always_live_profile_passes(cfg):
    crypto(cfg, window=None, rule_region=True, quote="rule")
    assert module(cfg) == []


def test_a_quote_operand_of_a_rule_assertion_is_a_quote_cell(cfg):
    crypto(cfg, window=None, rule_region=True, quote="rule")
    regions = load(cfg, S03)
    regions["regions"][-1] = {"id": "rgn.order-entry.last", "type": "freeze", "landmark": "lm.order-entry-header",
                              "rect": {"dx": 330, "dy": 0, "w": 60, "h": 20}, "fixture_dependency": "prices"}
    regions["regions"].append({"id": "rgn.order-entry.bid", "type": "rule", "landmark": "lm.order-entry-header",
                               "rect": {"dx": 400, "dy": 0, "w": 60, "h": 20}, "rule": {"field": "price"}})
    save(cfg, S03, regions)
    flow = load(cfg, FLOW)
    flow["steps"][3]["expect"] = [{"id": "R03", "kind": "rule", "rule_id": "R03", "tol": 0.01,
                                   "inputs": {"bid": "rgn.order-entry.bid", "mark": "rgn.order-entry.last",
                                              "ask": "rgn.order-entry.bid"}}]
    save(cfg, FLOW, flow)
    assert [m for r, f, p, m in module(cfg)] == [
        f"{CP} shows crypto future /MBTZ26: quote cell rgn.order-entry.last is freeze; the always-live profile never "
        "pixel-diffs a quote cell, declare it rule (7.10)"]


def test_a_frozen_crypto_checkpoint_only_refuses_a_quiet_window(cfg):
    crypto(cfg, window=None, rule_region=False, quote="diff")
    flow = load(cfg, FLOW)
    flow["steps"][3]["data_mode"] = "frozen"
    save(cfg, FLOW, flow)
    assert module(cfg) == []
    flow["requires_window"] = "futures_break"
    save(cfg, FLOW, flow)
    assert [p for r, f, p, m in module(cfg)] == ["steps[3].requires_window", "steps[6].requires_window"]   # s06 too


def test_a_checkpoint_on_a_later_non_crypto_symbol_is_not_crypto(cfg):
    crypto(cfg, window="all_quiet", rule_region=False, quote="diff")
    flow = load(cfg, FLOW)
    flow["steps"][3]["expect"].append({"id": "sym", "kind": "equals", "region": "rgn.order-entry.header-symbol",
                                       "value_ref": "fixture.symbols.aapl.symbol"})
    save(cfg, FLOW, flow)
    assert module(cfg) == []


def test_every_allowlisted_login_needs_a_rotation_entry(cfg):
    allow = load(cfg, "gateway/allowlist.yaml")
    allow["rigs"].append({"rig": "rig-02", "account_ids": ["D-20481135"], "login_ref": "vault://sentinel/rigs/rig-02/x"})
    save(cfg, "gateway/allowlist.yaml", allow)
    assert module(cfg) == [("L-XREF-25", "ops/credential_rotation.yaml", "logins",
                            "vault://sentinel/rigs/rig-02/x is used by a rig of gateway/allowlist.yaml but has no "
                            "owner entry (7.7)")]
    (cfg / "ops" / "credential_rotation.yaml").unlink()
    assert module(cfg) == [("L-XREF-25", "ops/credential_rotation.yaml", "",
                            "ops/credential_rotation.yaml is missing; every login a rig uses needs an owner entry "
                            f"(7.7): {LOGIN}, vault://sentinel/rigs/rig-02/x")]


def test_the_sms_device_follows_dr03(cfg):
    rot = load(cfg, "ops/credential_rotation.yaml")
    del rot["logins"][0]["sms_device"]
    save(cfg, "ops/credential_rotation.yaml", rot)
    assert [p for r, f, p, m in module(cfg)] == []                      # the example records DR-03 branch A
    br = load(cfg, "phase0/branches.yaml")
    br["login_mfa"] = {"branch": "undecided", "deadline": "2026-12-01"}  # the 11.8 default, B, is in force
    save(cfg, "phase0/branches.yaml", br)
    assert [p for r, f, p, m in module(cfg)] == ["logins[0].sms_device"]


def test_an_ended_rotation_needs_its_validating_run_by_the_lint_date(cfg):
    rot = load(cfg, "ops/credential_rotation.yaml")
    del rot["rotations"][0]["validating_run_id"]
    save(cfg, "ops/credential_rotation.yaml", rot)
    assert [(r, p) for r, f, p, m in module(cfg)] == [("L-XREF-25", "rotations[0].validating_run_id")]
