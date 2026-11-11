"""L-XREF-20, L-XREF-22, L-XREF-26 and L-XREF-27 (4.15) over the operations files of 4.16: one defect each in a copy of
the example sentinel-config, run through sentinel.lint.checks.ops_files."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import Repo, lint, lint_repo
from sentinel.lint.checks import ops_files
from sentinel.lint.repo import Doc

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
AUTH = "authorization/v1.yaml"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("*.png"))
    return dst


def found(repo: Repo, phase: int = 0) -> list[tuple[str, str, str, str]]:
    out: list = []
    ops_files.check(repo, lambda rule, file, message, path="", severity=None:
                    out.append((rule, file, path, severity or "error", message)), phase, TODAY)
    return sorted(out)


def rules(repo: Repo, phase: int = 0) -> set[tuple[str, str]]:
    return {(r, p) for r, _f, p, _s, _m in found(repo, phase)}


def test_the_example_is_clean():
    assert found(Repo.load(EXAMPLE)) == []
    report = lint(EXAMPLE, today=TODAY)
    assert not {"L-XREF-20", "L-XREF-22", "L-XREF-26", "L-XREF-27"} & set(report.skipped)


# -- L-XREF-20 ------------------------------------------------------------------------------------------------------

def test_no_authorization_is_an_error(root):
    shutil.rmtree(root / "authorization")
    ((rule, file, _p, _s, msg),) = found(Repo.load(root))
    assert (rule, file) == ("L-XREF-20", "authorization/") and "no authorization in force" in msg


def test_account_ids_must_equal_the_allowlist():
    repo = Repo.load(EXAMPLE)
    repo.docs[AUTH].data["account_ids"] = ["D-20481133"]
    assert rules(repo) == {("L-XREF-20", "account_ids")}


@pytest.mark.parametrize("change, path", [
    (lambda d: d["signatures"].pop(), "signatures"),
    (lambda d: d.update(rig_hostnames=["rig-02"]), "rig_hostnames"),
    (lambda d: d["rate_caps"].update(logins_per_day=80), "rate_caps.logins_per_day"),
    (lambda d: d.update(document_sha256="sha256:" + "0" * 64), "document_sha256"),
    (lambda d: d.update(version="v9"), "version"),
])
def test_record_defects(change, path):
    repo = Repo.load(EXAMPLE)
    change(repo.docs[AUTH].data)
    assert rules(repo) == {("L-XREF-20", path)}


def test_a_rig_hostname_may_be_qualified():
    repo = Repo.load(EXAMPLE)
    repo.docs[AUTH].data["rig_hostnames"] = ["rig-01.sentinel.internal"]
    assert found(repo) == []


def test_an_enabled_send_policy_under_the_default_branch_is_an_error():
    repo = Repo.load(EXAMPLE)
    repo.docs["policy/send_policy.yaml"].data.update(decision="approved", enabled=True)
    assert rules(repo) == {("L-XREF-20", "send_policy_branch")}


def test_the_model_endpoint_must_be_the_rulings():
    repo = Repo.load(EXAMPLE)
    rel = "policy/computer_use_ruling.yaml"
    repo.docs[rel] = Doc(rel, "computer_use_ruling", {"schema_version": 1, "status": "ruled",
                                                      "endpoint": "https://api.anthropic.com/v1"})
    assert found(repo) == []
    repo.docs[rel].data["endpoint"] = "models.example.internal"
    assert rules(repo) == {("L-XREF-20", "model_endpoint")}


def test_only_the_version_in_force_is_checked_and_a_document_needs_its_record(root):
    old = (root / AUTH).read_text().replace("send_policy_branch: default", "send_policy_branch: gated")
    (root / "authorization/v0.yaml").write_text(old.replace("version: v1", "version: v0"))
    (root / "authorization/v0.md").write_text("old\n")
    (root / AUTH).write_text((root / AUTH).read_text() + "supersedes: v0\n")
    (root / "authorization/draft.md").write_text("unsigned\n")
    assert [(f, m) for _r, f, _p, _s, m in found(Repo.load(root))] == [
        ("authorization/draft.md", "authorization/draft.yaml is missing: the document has no record")]


def test_two_versions_in_force_are_an_error(root):
    shutil.copy(root / AUTH, root / "authorization/v2.yaml")
    shutil.copy(root / "authorization/v1.md", root / "authorization/v2.md")
    msgs = [m for _r, f, _p, _s, m in found(Repo.load(root)) if f == "authorization/"]
    assert msgs and "versions v1, v2" in msgs[0]


# -- L-XREF-22 ------------------------------------------------------------------------------------------------------

def test_missing_budgets_is_an_error(root):
    (root / "policy/budgets.yaml").unlink()
    assert rules(Repo.load(root)) == {("L-XREF-22", "")}


def test_a_cap_above_11_4_needs_the_system_owner():
    repo = Repo.load(EXAMPLE)
    repo.docs["policy/budgets.yaml"].data["reviewer_items"]["merge"] = 300
    assert rules(repo) == {("L-XREF-22", "reviewer_items.merge")}
    repo.docs["policy/budgets.yaml"].data["approved_by"] = "hnovak"
    assert rules(repo) == {("L-XREF-22", "reviewer_items.merge")}
    repo.docs["policy/budgets.yaml"].data["approved_by"] = "asmith"
    assert found(repo) == []


def test_values_out_of_schema_are_left_to_the_schema_rule():
    repo = Repo.load(EXAMPLE)
    repo.docs["policy/budgets.yaml"].data["usd"]["per_run"] = "lots"
    repo.docs["soak.yaml"].data["budgets"]["heap"]["pct_of_xmx_max"] = "most"
    assert found(repo) == []
    assert {v.rule for v in lint_repo(repo, 0, TODAY).violations} == {"L-XREF-22", "L-XREF-27"}


def test_explorer_dollar_caps_must_equal_explorer_budget():
    repo = Repo.load(EXAMPLE)
    repo.docs["policy/explorer_budget.yaml"].data["max_usd_per_session"] = 20
    assert rules(repo) == {("L-XREF-22", "usd.per_explorer_session")}


# -- L-XREF-26 ------------------------------------------------------------------------------------------------------

def test_missing_codeowners_is_an_error(root):
    (root / "CODEOWNERS").unlink()
    assert rules(Repo.load(root)) == {("L-XREF-26", "")}


def test_codeowners_without_a_tier_1_route_is_an_error(root):
    p = root / "CODEOWNERS"
    p.write_text(p.read_text().replace("/flows/tier-1/                          @tos-trade-ui\n", ""))
    assert rules(Repo.load(root)) == {("L-XREF-26", "flows/tier-1/")}


# -- L-XREF-27 ------------------------------------------------------------------------------------------------------

def test_missing_soak_and_a_missing_metric_are_errors(root):
    repo = Repo.load(root)
    del repo.docs["soak.yaml"].data["budgets"]["heap"]
    assert rules(repo) == {("L-XREF-27", "budgets.heap")}
    (root / "soak.yaml").unlink()
    assert rules(Repo.load(root)) == set()                      # nothing reads soak.yaml before Phase 3
    assert rules(Repo.load(root), phase=3) == {("L-XREF-27", "")}


def test_a_metric_written_twice_is_an_error(root):
    p = root / "soak.yaml"
    p.write_text(p.read_text() + "  heap: {pct_of_xmx_max: 70}\n")
    assert rules(Repo.load(root)) == {("L-XREF-27", "budgets.heap")}


def test_a_looser_soak_budget_warns_while_codeowners_routes_soak_to_the_system_owner(root):
    repo = Repo.load(EXAMPLE)
    repo.docs["soak.yaml"].data["budgets"]["gdi_objects"]["max"] = 9500
    ((rule, _f, path, severity, _m),) = found(repo)
    assert (rule, path, severity) == ("L-XREF-27", "budgets.gdi_objects.max", "warn")
    p = root / "CODEOWNERS"
    p.write_text(p.read_text().replace("/soak.yaml                              @asmith", "/soak.yaml @hnovak"))
    repo = Repo.load(root)
    repo.docs["soak.yaml"].data["budgets"]["gdi_objects"]["max"] = 9500
    assert {(r, pth, s) for r, _f, pth, s, _m in found(repo)} == {
        ("L-XREF-26", "soak.yaml", "error"), ("L-XREF-27", "budgets.gdi_objects.max", "error")}
