"""The lint modes of 4.15 beyond --all and the rules that need two trees, a store or a run (sentinel.lint.diffmode):
removed and renamed ids (L-FLW-03, 2.2.4), L-XREF-10, L-MSK-04, L-MSK-05, L-MSK-01, L-XREF-16, L-MAN-10, --changed,
--manifest, --config-commit and --file."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import Repo, lint
from sentinel.lint import diffmode as D
from sentinel.lint.cli import main
from sentinel.lint.repo import Doc
from sentinel.records import ExplorerFinding, Finding

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
READBACK = "flows/tier-1/flow.trade.active-trader.read-back.yaml"
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
POSITIONS = "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/cp-positions"
S06 = "regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06.yaml"
S06_ID = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06"
PS_CENSUS = "regions/census/monitor.activity.position-statement/ws.tier-a.active-trader.yaml"
PS_CENSUS_ID = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
MASK = "masks/census/monitor.activity.position-statement/ws.tier-a.active-trader/msk.ps.last-update-01.yaml"
STOCK = "registry/surfaces/trade/trade.order-entry.stock.yaml"
MANIFEST = "manifests/branch-1998.3.0-5d3e812.yaml"
RENAMES = "flows/RENAMES.yaml"
BUILD = "main-1998.2.0-4f1c9a2"


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def rules(vs) -> set[str]:
    return {v.rule for v in vs}


def rename(r: Repo, old: str, new: str) -> None:
    r.docs[RENAMES].data["renames"].append({"old": old, "new": new, "build": BUILD})


# -- removed and renamed ids -------------------------------------------------------------------------------------------

def _rename_step(r: Repo) -> None:
    st = next(s for s in r.docs[READBACK].data["steps"] if s.get("id") == "cp-positions")
    st["id"] = "cp-position-list"
    st["checkpoint_id"] = POSITIONS.replace("cp-positions", "cp-position-list")


def test_a_renamed_checkpoint_needs_a_renames_entry(base):
    head = copy.deepcopy(base)
    _rename_step(head)
    got = [v for v in D.history_violations(base, head) if v.rule == "L-FLW-03"]
    assert [v.file for v in got] == [READBACK] and POSITIONS in got[0].message
    rename(head, POSITIONS, POSITIONS.replace("cp-positions", "cp-position-list"))
    assert "L-FLW-03" not in rules(D.history_violations(base, head))


def test_a_renamed_surface_needs_a_rename_map_and_its_checkpoints_follow(base):
    """2.2.4 (02-registry-rename-detection): trade.order-entry.stock renamed to trade.order-entry.equity."""
    head = copy.deepcopy(base)
    doc = head.docs.pop(STOCK)
    new_rel = STOCK.replace("stock", "equity")
    head.docs[new_rel] = Doc(new_rel, "surface", {**doc.data, "id": "trade.order-entry.equity"})
    got = [v for v in D.history_violations(base, head) if v.rule == "L-SRF-01"]
    assert [(v.file, v.path) for v in got] == [(STOCK, "id")]
    rename(head, "trade.order-entry.stock", "trade.order-entry.equity")
    assert "L-SRF-01" not in rules(D.history_violations(base, head))
    entries = D.renames_of(head)
    assert D.follow_renames("census/trade.order-entry.stock/ws.tier-a.beginner", entries) == \
        "census/trade.order-entry.equity/ws.tier-a.beginner"
    assert D.follow_renames(S06_ID, entries) == S06_ID.replace("flow/trade.order-entry.stock/", "flow/trade.order-entry.equity/")
    assert D.follow_renames("census/charts.flexible-grid/ws.tier-a.beginner", entries) is None


# -- L-XREF-10 ---------------------------------------------------------------------------------------------------------

def _move_rect(r: Repo) -> None:
    r.docs[S06].data["regions"][0]["rect"]["dx"] += 4


def test_a_changed_rect_of_an_approved_checkpoint_needs_a_marker(base):
    head = copy.deepcopy(base)
    _move_rect(head)
    got = [v for v in D.history_violations(base, head) if v.rule == "L-XREF-10"]
    assert [v.file for v in got] == [S06] and "rect" in got[0].message
    assert "L-XREF-10" not in rules(D.history_violations(base, head, approved=[PS_CENSUS_ID]))   # not approved
    head.docs[S06].data["recalibrate"] = True
    assert "L-XREF-10" not in rules(D.history_violations(base, head))


def test_a_changed_template_needs_a_marker(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(EXAMPLE, a)
    shutil.copytree(EXAMPLE, b)
    tpl = Repo.load(b).docs[S06].data["landmarks"][0]["template"]
    (b / tpl).write_bytes((b / tpl).read_bytes() + b"\0")
    got = [v for v in D.history_violations(Repo.load(a), Repo.load(b)) if v.rule == "L-XREF-10"]
    assert got and "template" in got[0].message


def test_a_removed_approved_checkpoint_needs_a_renames_entry(base):
    head = copy.deepcopy(base)
    _rename_step(head)
    rel = f"regions/{POSITIONS}.yaml"
    d = head.docs.pop(rel)
    new = rel.replace("cp-positions", "cp-position-list")
    head.docs[new] = Doc(new, "regions", {**d.data, "checkpoint_id": POSITIONS.replace("cp-positions", "cp-position-list")})
    assert [v.file for v in D.history_violations(base, head) if v.rule == "L-XREF-10"] == [rel]


# -- L-MSK-05 and L-MSK-04 ---------------------------------------------------------------------------------------------

def _renew(r: Repo, reason: str | None = None) -> None:
    m = r.docs[MASK].data
    m["expires_at"] = "2027-01-04T15:20:00-05:00"
    if reason is not None:
        m["reason"] = reason


@pytest.mark.parametrize("reason,aa,needle", [
    (None, None, "quotes the latest nightly A/A result"),
    ("Still moving: A/A run-2026-11-09-nightly-0001 shows 40 px.", None, None),
    ("Still moving.", {"px": 40}, "does not quote the latest nightly A/A result run-n1"),
    ("A/A run-n1: 40 px.", {"px": 40}, None),
    ("A/A run-n1: 0 px.", {"px": 0}, "stable in A/A"),
])
def test_a_renewal_quotes_the_latest_nightly_aa_result(base, reason, aa, needle):
    head = copy.deepcopy(base)
    _renew(head, reason)
    results = None if aa is None else {(PS_CENSUS_ID, "rgn.ps.last-update"): D.AAResult("run-n1", aa["px"])}
    got = [v for v in D.history_violations(base, head, aa=results) if v.rule == "L-MSK-05"]
    assert (needle is None and got == []) or (got and needle in got[0].message), got


def test_mask_growth_above_2_percent_of_a_tier1_checkpoint_warns(base):
    head = copy.deepcopy(base)
    reg = next(x for x in head.docs[PS_CENSUS].data["regions"] if x["id"] == "rgn.ps.last-update")
    reg["rect"].update(dx=360, w=240, h=40)
    got = [v for v in D.history_violations(base, head, month_ago=base) if v.rule == "L-MSK-04"]
    assert [(v.file, v.severity) for v in got] == [(PS_CENSUS, "warn")]
    assert not [v for v in D.history_violations(base, base, month_ago=base) if v.rule == "L-MSK-04"]


# -- L-MSK-01 against the store ----------------------------------------------------------------------------------------

@dataclass
class _Dec:
    action: str
    decided_by: str


class _Store:
    def __init__(self, decisions: dict[str, _Dec]):
        self.decisions = decisions

    def get_decision(self, decision_id: str):
        return self.decisions.get(decision_id)


ONCALL = {"schema_version": 1, "roles": [], "flow_owners": [], "rotations": [], "triage_roster": ["jdoe"]}


@pytest.mark.parametrize("decisions,needle", [
    ({}, "is not a decision of the Central Store"),
    ({"dec-000812": _Dec("noise_signature", "jdoe")}, "not request_mask"),
    ({"dec-000812": _Dec("request_mask", "reviewer")}, "not a human"),
    ({"dec-000812": _Dec("request_mask", "mallory")}, "not in the triage roster"),
])
def test_l_msk_01_against_the_central_store(base, decisions, needle):
    r = copy.deepcopy(base)
    r.docs["ops/oncall.yaml"] = Doc("ops/oncall.yaml", "oncall", ONCALL)
    got = D.store_rules(r, _Store(decisions))
    assert got and all(v.rule == "L-MSK-01" for v in got) and any(needle in v.message for v in got), got


def test_a_request_mask_by_a_rostered_human_is_clean(base):
    r = copy.deepcopy(base)
    r.docs["ops/oncall.yaml"] = Doc("ops/oncall.yaml", "oncall", ONCALL)
    assert D.store_rules(r, _Store({"dec-000812": _Dec("request_mask", "jdoe")})) == []
    assert D.store_rules(r, None) == []


# -- L-XREF-16 and L-MAN-10 --------------------------------------------------------------------------------------------

def test_the_run_context_carries_the_authorization(tmp_path):
    (tmp_path / "authorization").mkdir()
    (tmp_path / "authorization/v3.md").write_text("signed text\n")
    (tmp_path / "authorization/v3.yaml").write_text("version: v3\n")
    repo = Repo(tmp_path)
    sha = D.file_sha256(tmp_path / "authorization/v3.md")
    assert D.run_context_rules(repo, {"authorization_ref": "authorization/v3.md", "authorization_sha256": sha}) == []
    for ctx, needle in (({}, "carries no authorization"),
                        ({"authorization_ref": "v4", "authorization_sha256": sha}, "has no authorization/v4.md"),
                        ({"authorization_ref": "v3", "authorization_sha256": "sha256:" + "0" * 64}, "is not the sha256")):
        got = D.run_context_rules(repo, ctx)
        assert [v.rule for v in got] == ["L-XREF-16"] and needle in got[0].message


def test_new_surface_agrees_with_the_base_registry():
    m = {"entries": [{"declared_surfaces": ["charts.new-cell"], "tickets": ["TOS-1"]}]}
    got = D.lint_manifest_against_base(m, {"charts.flexible-grid"})
    assert [(v.rule, v.path) for v in got] == [("L-MAN-10", "entries[0].new_surface")]
    m["entries"][0]["new_surface"] = True
    assert D.lint_manifest_against_base(m, {"charts.flexible-grid"}) == []
    old = {"entries": [{"declared_surfaces": ["charts.flexible-grid"], "tickets": ["TOS-1"], "new_surface": True}]}
    assert [v.rule for v in D.lint_manifest_against_base(old, {"charts.flexible-grid"})] == ["L-MAN-10"]
    assert D.lint_manifest_against_base(old, {"charts.flexible-grid"}, {"TOS-1": "Feature"}) == []


# -- --file and Explorer candidates ------------------------------------------------------------------------------------

def _finding() -> Finding:
    return Finding("fd-1", "run-1", "explorer_candidate", "candidate flow", "run-1", explorer=ExplorerFinding(
        "ses-1", {"surface_id": "trade.order-entry.stock", "observation": "x", "crop_sha256": "sha256:" + "a" * 64},
        "low", 0.5, "sha256:" + "b" * 64))


def test_a_bad_flow_file_fails_and_an_explorer_candidate_is_kept_lint_failed(tmp_path, base, capsys):
    data = Repo.load(EXAMPLE).docs[FLOW].data
    bad = copy.deepcopy(data)
    bad["preconditions"] = [p for p in bad["preconditions"] if p["check"] != "paper_mode"]
    p = tmp_path / "candidate.yaml"
    p.write_text(json.dumps(bad))
    found = D.lint_file(base, p, 1, TODAY)
    assert "L-FLW-12" in rules(found) and {v.file for v in found} == {FLOW}       # replaces the flow of its flow_id
    assert main([str(EXAMPLE), "--file", str(p), "--today", "2026-11-10"]) == 1
    good = tmp_path / "good.yaml"
    good.write_text(json.dumps(data))
    assert main([str(EXAMPLE), "--file", str(good), "--today", "2026-11-10"]) == 0
    kept = D.explorer_lint_failed(_finding(), found)
    assert kept is not None and kept.explorer.replayed == "lint_failed"
    assert any("L-FLW-12" in line for line in kept.evidence["lint"])
    assert D.explorer_lint_failed(_finding(), []) is None


def test_an_explorer_candidate_from_elsewhere_is_linted_under_quarantine(tmp_path, base):
    data = copy.deepcopy(base.docs[FLOW].data)
    data.update(flow_id="flow.explorer.cand-1", **{"class": "explorer_candidate"})
    p = tmp_path / "fd-1.yaml"
    p.write_text(json.dumps(data))
    assert {v.file for v in D.lint_file(base, p, 1, TODAY)} == {"flows/quarantine/fd-1.yaml"}


# -- --manifest --------------------------------------------------------------------------------------------------------

def _manifest(tmp_path: Path, change) -> Path:
    from sentinel.lint.repo import load_yaml_text
    data = load_yaml_text((EXAMPLE / MANIFEST).read_text())
    change(data)
    p = tmp_path / "manifest.yaml"
    p.write_text(json.dumps(data))
    return p


def test_manifest_check_is_informational_in_phase_1_and_blocking_from_phase_2(tmp_path, capsys):
    p = _manifest(tmp_path, lambda d: d["entries"][0].update(tickets=["tos-48120"]))
    assert main(["--config", str(EXAMPLE), "--manifest", str(p), "--phase", "1", "--today", "2026-11-10"]) == 0
    out = capsys.readouterr().out.splitlines()
    status = json.loads(out[-1])
    assert status["context"] == "sentinel/manifest" and status["state"] == "failure" and status["blocking"] is False
    assert "L-MAN-05" in status["description"] and "informational" in status["description"]
    assert any(line.startswith("L-MAN-05 error") for line in out[:-1])
    assert main([str(EXAMPLE), "--manifest", str(p), "--phase", "2", "--today", "2026-11-10"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["blocking"] is True


def test_manifest_check_reports_only_the_manifest(tmp_path, capsys):
    p = _manifest(tmp_path, lambda d: (d["entries"][0]["declared_surfaces"].append("charts.unknown"),
                                       d.update(candidate="branch-1998.4.0-abcdef1", manifest_id="mf-branch-1998.4.0-abcdef1")))
    assert main([str(EXAMPLE), "--manifest", str(p), "--phase", "2", "--json", "--today", "2026-11-10"]) == 1
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()[:-1]]
    assert {r["rule"] for r in rows} == {"L-MAN-01", "L-XREF-06"}             # the new branch build is not in builds/
    assert {r["file"] for r in rows} == {"manifests/branch-1998.4.0-abcdef1.yaml"}


# -- git: --changed, --base-ref, --config-commit -------------------------------------------------------------------------

def _git(root: Path, *args: str, when: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def checkout(tmp_path) -> Path:
    """The example committed on 2026-10-01 in a git repository whose config lives in a subdirectory."""
    root = tmp_path / "repo" / "config"
    shutil.copytree(EXAMPLE, root)
    p = root / "registry/dependency-map.yaml"                       # a warning in a file the PR leaves alone
    p.write_text(p.read_text().replace("last_verified: 2026-11-02T03:10:00-05:00", "last_verified: 2026-01-01T09:00:00-05:00", 1))
    _git(root.parent, "init", "-q")
    _git(root.parent, "add", "-A")
    _git(root.parent, "commit", "-q", "-m", "base", when="2026-10-01T12:00:00+00:00")
    return root


def test_changed_reports_the_edited_flow_errors_and_only_changed_warnings(checkout, capsys, tmp_path):
    assert "L-DEP-07" in rules(lint(checkout, today=TODAY).violations)               # the base carries a warning
    assert "L-DEP-07" not in rules(D.lint_changed(checkout, "HEAD", 0, TODAY)[0].violations)
    p = checkout / READBACK
    p.write_text(p.read_text().replace("  - {id: p3, type: precondition, check: no_modal}\n", ""))
    report, changed = D.lint_changed(checkout, "HEAD", 0, TODAY)
    assert changed == {READBACK}
    assert "L-FLW-12" in rules(report.violations) and "L-DEP-07" not in rules(report.violations)
    assert "L-XREF-10" not in report.skipped and "L-MSK-05" not in report.skipped
    comment = tmp_path / "comment.json"
    assert main([str(checkout), "--changed", "HEAD", "--review-comment", str(comment), "--today", "2026-11-10"]) == 1
    assert json.loads(comment.read_text())["body"].startswith("sentinel lint")


def test_changed_runs_the_history_rules_against_base_and_a_month_ago(checkout):
    p = checkout / PS_CENSUS
    p.write_text(p.read_text().replace("rect: {dx: 480, dy: 0, w: 120, h: 18}", "rect: {dx: 360, dy: 0, w: 240, h: 18}"))
    report, _ = D.lint_changed(checkout, "HEAD", 0, TODAY)
    assert {"L-XREF-10", "L-MSK-04"} <= rules(report.violations)


def test_base_ref_adds_the_history_rules_to_the_whole_lint(checkout, capsys):
    p = checkout / PS_CENSUS
    p.write_text(p.read_text().replace("rect: {dx: 480, dy: 0, w: 120, h: 18}", "rect: {dx: 470, dy: 0, w: 120, h: 18}"))
    assert main([str(checkout), "--base-ref", "HEAD", "--today", "2026-11-10"]) == 1
    assert any(x.startswith("L-XREF-10 error") for x in capsys.readouterr().out.splitlines())


def test_config_commit_lints_the_tree_at_the_commit(checkout, capsys, tmp_path):
    p = checkout / READBACK
    p.write_text(p.read_text().replace("check: no_modal", "check: no_modal_x"))       # the working tree is broken
    assert main([str(checkout), "--today", "2026-11-10"]) == 1
    capsys.readouterr()
    sha = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert main([str(checkout), "--all", "--config-commit", sha, "--phase", "1", "--today", "2026-11-10"]) == 0
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"candidate": "branch-1998.3.0-5d3e812"}))
    assert main([str(checkout), "--all", "--config-commit", sha, "--run-context", str(ctx), "--today", "2026-11-10"]) == 1
    assert any(x.startswith("L-XREF-16 error run") for x in capsys.readouterr().out.splitlines())


def test_a_missing_revision_is_a_usage_error(checkout, capsys):
    assert main([str(checkout), "--changed", "no-such-rev", "--today", "2026-11-10"]) == 2
    assert "sentinel lint: git" in capsys.readouterr().err
