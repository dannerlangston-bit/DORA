"""The appendix copies equal their sources, and the code constants equal the spec tables they mirror (12-mirror-check-
test, 04b-spec-mirror-constants; 12.2: "Where this copy and its source ever differ, the source wins and this copy is
the defect")."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT.parent / "sentinel-spec" / "sections"
pytestmark = pytest.mark.skipif(not SPEC.exists(), reason="spec checkout not present")
sys.path.insert(0, str(ROOT / "tools"))

import spec_tables as st  # noqa: E402

# (name, copy subsection, source subsection, extractor): every verbatim copy of the appendix
COPIES = [
    ("12.2 precedence and outcome tables", "12.2", "1.10", st.outcome_taxonomy_block),
    ("12.2 reason table", "12.2", "4.13", lambda t: st.table_block(t, "| Outcome | Reason codes |")),
    ("12.2 finding table", "12.2", "4.13", lambda t: st.table_block(t, "| Kind | Meaning | Section |")),
    ("12.2 action table", "12.2", "4.14", lambda t: st.table_block(t, "| action (UI) |")),
    ("12.3 invariant catalog", "12.3", "2.7", lambda t: st.table_block(t, "| Id | Statement |")),
    ("12.6 attestation checklist", "12.6", "6.3", lambda t: st.table_block(t, "| Id | Item |")),
    ("12.8 popup starter list", "12.8", "7.8", lambda t: st.table_block(t, "| Id | Window |")),
]


def mirror_differences(copy_text: str, source_text: str, extract) -> list[str]:
    """Rows (or lines) of the copy that differ from the source; empty when the copy is verbatim."""
    a, b = extract(source_text).splitlines(), extract(copy_text).splitlines()
    if len(a) != len(b):
        return [f"{len(b)} lines in the copy, {len(a)} in the source"]
    return [f"line {i}: copy {y[:80]!r} differs from source {x[:80]!r}" for i, (x, y) in enumerate(zip(a, b)) if x != y]


@pytest.mark.parametrize("name,copy,source,extract", COPIES, ids=[c[0] for c in COPIES])
def test_appendix_copy_is_verbatim(name, copy, source, extract):
    diffs = mirror_differences(st.subsection(copy), st.subsection(source), extract)
    assert not diffs, "\n".join(diffs)


def test_8_5_layout_lists_every_4_1_path():
    """8.5 lists 4.1's layout plus the files 8.5 adds; a path of the 4.1 table missing from it is a defect."""
    table = st.table_after(st.subsection("4.1"), "| Path | Record type | Consumed by |")
    paths = {p for row in table for p in st.CODE.findall(row[0])}
    sec = st.subsection("8.5")
    listing = sec[sec.index("```\n") + 4:sec.index("\n```", sec.index("```\n"))]
    listed = {name for line in listing.splitlines() for name in line[:47].strip().split(", ")}
    assert paths - listed == set()
    assert {"corpus/odd-inputs.yaml", "rig/process_allowlist.yaml", "policy/reporting.yaml"} <= listed


def test_brief_quote_of_12_2_is_verbatim():
    brief = next(ln for ln in (SPEC / "00-design-brief.md").read_text(encoding="utf-8").splitlines()
                 if ln.startswith("Per checkpoint: `identical`"))
    assert f"> {brief}" in st.subsection("12.2").splitlines()


def test_the_check_sees_one_edited_reason_cell():
    """Editing one 12.2 reason cell in a copy of the text makes the mirror check fail."""
    copy = st.subsection("12.2").replace("`wall_clock` (the run's wall budget", "`wall_clock` (the wall budget", 1)
    assert copy != st.subsection("12.2")
    assert mirror_differences(copy, st.subsection("4.13"), COPIES[1][3])
    tampered = st.subsection("12.2").replace("| `identical` |", "| `identical` | extra |", 1)
    assert mirror_differences(tampered, st.subsection("1.10"), COPIES[0][3])


def test_taxonomy_outcome_lists_equal_1_10_and_12_2():
    """L-XREF-08: the three outcome lists of taxonomy.yaml are the 1.10 tables and their 12.2 copy."""
    from sentinel.taxonomy import load
    t = load()
    for source in (st.subsection("1.10"), st.subsection("12.2")):
        lists = st.outcome_lists(source)
        assert (lists["checkpoint_outcomes"], lists["step_outcomes"], lists["run_outcomes"]) == (
            t.checkpoint_outcomes, t.step_outcomes, t.run_outcomes)
    assert st.reason_table(st.subsection("12.2")) == st.reason_table()
    assert st.finding_kinds(st.subsection("12.2")) == t.finding_kinds


def test_popup_and_mutant_constants_equal_12_8_and_12_5():
    """L-XREF-12: the lint's starter ids are the 12.8 rows and its mutant table the 12.5 rows."""
    from sentinel.lint import rules
    assert list(rules.POP_IDS_12_8) == st.popup_ids_12_8()
    assert rules.MUTANTS_12_5 == st.mutant_table_12_5()


def test_rule_operands_equal_2_8_3():
    from sentinel.lint import rules
    table = st.rule_operands()
    assert rules.RULE_OPERANDS == {r: (frozenset(k for k, v in ops.items() if v["required"]),
                                       frozenset(k for k, v in ops.items() if not v["required"]))
                                   for r, ops in table.items()}
