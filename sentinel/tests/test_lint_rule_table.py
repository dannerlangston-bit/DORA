"""The RULES table against the rule tables of section 4, and the sentinel.lint.checks plug-in contract (4.1, 4.15)."""
from __future__ import annotations

import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import checks, lint
from sentinel.lint import rules as R

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "example"
SPEC = ROOT.parent / "sentinel-spec" / "sections"
TODAY = date(2026, 11, 10)
NEW_ROWS = ("L-SRF-16", "L-WSP-08", "L-WSP-09", "L-RGN-08", *(f"L-XREF-{i}" for i in range(17, 31)))


def spec_rule_rows() -> dict[str, list[str]]:
    sys.path.insert(0, str(ROOT / "tools"))
    import spec_tables
    rows = {}
    for t in spec_tables.tables(spec_tables.section("04")):
        if t and t[0][0] == "Rule":
            rows.update({r[0]: r for r in t[1:]})
    return rows


@pytest.mark.skipif(not SPEC.exists(), reason="spec checkout not present")
def test_every_section_4_rule_has_a_row():
    """Every rule id of the section 4 tables is a RULES row (the -00 rows are the lint's schema-failure ids)."""
    rows = spec_rule_rows()
    assert set(rows) == {rid for rid in R.RULES if not rid.endswith("-00")}
    for rid, (_, _, severity) in rows.items():
        want = "warn" if severity.startswith("warn") else "error"
        assert R.RULES[rid].severity == want, rid


def test_new_rows_are_pending_until_a_module_checks_them():
    report = lint(EXAMPLE, today=TODAY)
    implemented = {i for m in R.check_modules() for i in m.RULE_IDS}
    for rid in NEW_ROWS:
        if rid in implemented:
            assert rid not in report.skipped, rid
        else:
            assert report.skipped[rid].startswith("pending:"), rid
    assert all(report.skipped[f"L-OUT-0{i}"].startswith("record rule") for i in range(1, 8))
    assert all(report.skipped[f"L-DEC-0{i}"].startswith("record rule") for i in range(1, 9))


def test_rule_operands_come_from_2_8_3():
    """SD-34: the lint's operand lists are the taxonomy's, which gen_taxonomy reads from the 2.8.3 table."""
    from sentinel.taxonomy import load
    assert R.RULE_OPERANDS == {r: load().rule_operands(r) for r in load().data["rule_operands"]}
    assert R.RULE_OPERANDS["R03"] == (frozenset({"bid", "mark", "ask"}), frozenset({"last", "low", "high"}))


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A directory added to sentinel.lint.checks' search path; modules written there are discovered."""
    monkeypatch.setattr(checks, "__path__", [*checks.__path__, str(tmp_path)])
    written: list[str] = []

    def write(name: str, body: str) -> None:
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
        written.append(f"{checks.__name__}.{name}")

    yield write
    for mod in written:
        sys.modules.pop(mod, None)


def test_a_check_module_reports_and_unpends_its_rules(plugin_dir):
    plugin_dir("tmp_critical", '''
        RULE_IDS = ("L-SRF-16",)

        def check(repo, add, phase, today):
            for doc in repo.of("surface")[:1]:
                add("L-SRF-16", doc.rel, "critical: true requires tier: 1", "critical")
    ''')
    report = lint(EXAMPLE, today=TODAY)
    assert [v.rule for v in report.violations] == ["L-SRF-16"]
    unnamed = {rid for rid in NEW_ROWS} - {i for m in R.check_modules() for i in m.RULE_IDS}   # rows still pending
    assert "L-SRF-16" not in report.skipped and all(rid in report.skipped for rid in unnamed)
    assert "L-FPR-02" in report.skipped                    # a run-start rule stays skipped whoever names it


def test_a_module_may_not_report_a_rule_it_does_not_name(plugin_dir):
    plugin_dir("tmp_trespass", '''
        RULE_IDS = ("L-WSP-08",)

        def check(repo, add, phase, today):
            add("L-SRF-16", "x.yaml", "not mine")
    ''')
    with pytest.raises(R.LintPluginError, match="L-SRF-16"):
        lint(EXAMPLE, today=TODAY)


@pytest.mark.parametrize("body", [
    'RULE_IDS = ("L-XREF-99",)\ndef check(repo, add, phase, today):\n    pass\n',   # invents a rule id
    'RULE_IDS = ()\ndef check(repo, add, phase, today):\n    pass\n',               # names nothing
    'RULE_IDS = ("L-WSP-08",)\n',                                                    # no check function
])
def test_a_module_breaking_the_contract_is_refused(plugin_dir, body):
    plugin_dir("tmp_broken", body)
    with pytest.raises(R.LintPluginError):
        R.check_modules()


def test_private_modules_are_not_run(plugin_dir):
    plugin_dir("_tmp_helper", "raise RuntimeError('never imported')\n")
    assert all(not m.__name__.rsplit(".", 1)[1].startswith("_") for m in R.check_modules())
