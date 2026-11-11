"""`sentinel lint` over the example sentinel-config: it lints clean, and the CLI contract (4.1, 4.15)."""
from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import RULES, Repo, lint
from sentinel.lint.cli import main
from sentinel.lint import rules as R
from sentinel.lint.repo import LAYOUT

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
LINE = re.compile(r"^L-[A-Z]+-\d\d (error|warn) \S+: .+$")


@pytest.mark.parametrize("phase", [0, 1])
def test_example_lints_clean(phase):
    report = lint(EXAMPLE, phase=phase, today=TODAY)
    assert report.violations == [], "\n".join(v.line() for v in report.violations)
    assert report.ok


def test_every_example_file_is_read():
    """No YAML file of the example is silently outside the 4.1 layout the lint reads."""
    repo = Repo.load(EXAMPLE)
    for p in EXAMPLE.rglob("*.yaml"):
        rel = p.relative_to(EXAMPLE).as_posix()
        assert rel in repo.docs or rel in READ_BY_A_CHECK_MODULE, rel
    missing = {kind for kind, _ in LAYOUT} - {d.kind for d in repo.docs.values()} - {"taxonomy"}
    assert missing <= EXAMPLE_PENDING, sorted(missing - EXAMPLE_PENDING)


# 4.1 files a check module reads from disk until lint/repo.py LAYOUT lists them (none left: corpus/odd-inputs.yaml is
# the layout's kind odd_inputs)
READ_BY_A_CHECK_MODULE: frozenset[str] = frozenset()


# 4.16 kinds the example does not hold yet; the build packages that own each file add it (shrink this set as they land)
EXAMPLE_PENDING = frozenset({
    "default_order_hotkeys", "dismiss_allowlist", "explorer_deny_list", "egress_allowlist", "computer_use_ruling",
    "budgets", "reporting", "authorization", "windows", "oncall", "credential_rotation", "process_allowlist",
    "measured_sizes", "palette", "soak", "calibration", "eval_report",
})


def test_skipped_rules_carry_reasons():
    report = lint(EXAMPLE, today=TODAY)
    for rid in ("L-MAN-09", "L-MAN-10", "L-FIX-08", "L-XREF-10", "L-XREF-16", "L-FPR-02"):
        assert report.skipped[rid]
    for rid in ("L-MSK-04", "L-MSK-05", "L-XREF-10"):            # checked by `sentinel lint --changed BASE`
        assert report.skipped[rid].startswith("history:"), rid
    implemented = {i for m in R.check_modules() for i in m.RULE_IDS}   # pending rows a checks module now implements
    assert set(report.skipped) == {rid for rid, r in RULES.items() if r.skipped and rid not in implemented}
    assert all(r.severity in ("error", "warn") for r in RULES.values())


def test_cli_clean_example_returns_0(capsys):
    assert main([str(EXAMPLE), "--today", "2026-11-10"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "0 errors, 0 warnings" in out.err


def test_cli_accepts_the_lint_word_and_phase(capsys):
    assert main(["lint", str(EXAMPLE), "--phase", "1", "--today", "2026-11-10"]) == 0


def test_cli_prints_findings_and_returns_1(tmp_path, capsys):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    p = root / "registry/surfaces/monitor/monitor.activity.position-statement.yaml"
    p.write_text(p.read_text().replace("roots: [\"com.tos.desktop.monitor.positions\"]", "roots: []"))
    assert main([str(root), "--today", "2026-11-10"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(LINE.match(x) for x in lines), lines
    assert any(x.startswith("L-SRF-07 error registry/surfaces/monitor/monitor.activity.position-statement.yaml: roots") for x in lines)


def test_cli_warnings_alone_return_0(tmp_path, capsys):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    p = root / "registry/dependency-map.yaml"
    p.write_text(p.read_text().replace('"com.tos.desktop.ui.grid.**"', '"TBD.flexgrid.**"'))
    assert main([str(root), "--today", "2026-11-10"]) == 0
    assert capsys.readouterr().out.startswith("L-DEP-01 warn")
    assert main([str(root), "--today", "2026-11-10", "--phase", "1"]) == 1
    assert capsys.readouterr().out.startswith("L-DEP-01 error")


def test_cli_json_lines(tmp_path, capsys):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    p = root / "policy/explorer_budget.yaml"
    p.write_text(p.read_text().replace("max_actions: 400", "max_actions: 401"))
    assert main([str(root), "--json", "--show-skipped", "--today", "2026-11-10"]) == 1
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    found = [r for r in rows if r["severity"] != "skipped"]
    assert found == [{"rule": "L-XREF-14", "severity": "error", "file": "policy/explorer_budget.yaml",
                      "path": "max_actions", "message": found[0]["message"]}]
    assert any(r["rule"] == "L-MAN-09" and r["severity"] == "skipped" for r in rows)


def test_cli_usage_error_returns_2(capsys):
    assert main([]) == 2
    assert main([str(EXAMPLE), "--phase", "7"]) == 2
    assert main([str(EXAMPLE / "missing-dir")]) == 2
