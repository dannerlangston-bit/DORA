"""L-XREF-17 and L-XREF-18: the gateway files of 4.16 (lint/checks/gateway_files.py)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint import lint
from sentinel.lint.checks import gateway_files
from sentinel.lint.repo import Repo

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
HOTKEYS = "gateway/default_order_hotkeys.yaml"
ALLOW = "gateway/dismiss_allowlist.yaml"
BRANCHES = "phase0/branches.yaml"
PIN = "read_on_build: main-1998.2.0-4f1c9a2\nread_by: system-owner\nread_at: '2026-10-05T14:00:00Z'\n"
GOOD_HOTKEYS = "schema_version: 1\nbuild: main-1998.2.0-4f1c9a2\nverified: true\nchords:\n" \
               "  - {chord: Shift+B, action: buy}\n  - {chord: Shift+S, action: sell}\n"


def run(tmp_path: Path, files: dict[str, str], phase: int = 0) -> list[tuple[str, str, str, str]]:
    """Write `files` into an empty config tree and run the module: (rule, severity, path, message) per finding."""
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    out: list[tuple[str, str, str, str]] = []

    def add(rule: str, file: str, message: str, path: str = "", severity: str | None = None) -> None:
        out.append((rule, severity or "error", path, message))

    gateway_files.check(Repo.load(tmp_path), add, phase, TODAY)
    return out


def test_the_example_is_clean_and_both_rules_run():
    for phase in (0, 1):
        report = lint(EXAMPLE, phase=phase, today=TODAY)
        assert not [v for v in report.violations if v.rule in gateway_files.RULE_IDS]
        assert not set(gateway_files.RULE_IDS) & set(report.skipped)


# -- L-XREF-17 ------------------------------------------------------------------------------------------------
def test_a_missing_hotkeys_file_is_an_error(tmp_path):
    assert [(r, s) for r, s, _, _ in run(tmp_path, {})] == [("L-XREF-17", "error")]


@pytest.mark.parametrize("chords,path", [
    ("  - {chord: Hyper+B, action: buy}\n", "chords[0].chord"),
    ("  - {chord: Shift+B, action: buy}\n  - {chord: shift+b, action: buy}\n", "chords[1].chord"),
    ("  - {chord: Ctrl+Shift+B, action: buy}\n  - {chord: Shift+Ctrl+B, action: buy}\n", "chords[1].chord"),
])
def test_unparsable_and_repeated_chords_are_errors(tmp_path, chords, path):
    text = "schema_version: 1\nbuild: main-1998.2.0-4f1c9a2\nverified: true\nchords:\n" + chords
    found = run(tmp_path, {HOTKEYS: text})
    assert [(r, s, p) for r, s, p, _ in found] == [("L-XREF-17", "error", path)]


def test_more_than_sixteen_chords_is_an_error(tmp_path):
    rows = "".join(f"  - {{chord: Shift+{c}, action: buy}}\n" for c in "ABCDEFGHIJKLMNOPQ")
    found = run(tmp_path, {HOTKEYS: "schema_version: 1\nbuild: main-1998.2.0-4f1c9a2\nverified: true\nchords:\n" + rows})
    assert len(found) == 1 and "16" in found[0][3]


def test_unverified_chords_warn_from_phase_1(tmp_path):
    files = {HOTKEYS: GOOD_HOTKEYS.replace("verified: true", "verified: false")}
    assert run(tmp_path, files, phase=0) == []
    assert [(r, s, p) for r, s, p, _ in run(tmp_path, files, phase=1)] == [("L-XREF-17", "warn", "verified")]


# -- L-XREF-18 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("labels", ["[Send]", "[Auto Send]", "[Confirm]", "[Submit]", "[Replace]", "['Del.*']",
                                    "['Delete ']"])
def test_send_like_or_pattern_labels_are_errors(tmp_path, labels):
    found = run(tmp_path, {HOTKEYS: GOOD_HOTKEYS, ALLOW: f"schema_version: 1\nlabels: {labels}\n{PIN}"})
    assert [(r, s, p) for r, s, p, _ in found] == [("L-XREF-18", "error", "labels[0]")]


def test_edit_needs_the_phase_0_observation(tmp_path):
    allow = f"schema_version: 1\nlabels: [Delete, Edit]\n{PIN}"
    found = run(tmp_path, {HOTKEYS: GOOD_HOTKEYS, ALLOW: allow,
                           BRANCHES: "schema_version: 1\nsafety:\n  - {item: 10, result: undecided}\n"})
    assert [(r, p) for r, _, p, _ in found] == [("L-XREF-18", "labels[1]")]
    observed = "schema_version: 1\nsafety:\n  - {item: 10, result: confirmed, observed: [Delete, Edit]}\n"
    assert run(tmp_path, {HOTKEYS: GOOD_HOTKEYS, ALLOW: allow, BRANCHES: observed}) == []


def test_an_empty_or_absent_allowlist_is_valid_and_unpinned_labels_warn(tmp_path):
    assert run(tmp_path, {HOTKEYS: GOOD_HOTKEYS}) == []
    assert run(tmp_path, {HOTKEYS: GOOD_HOTKEYS, ALLOW: "schema_version: 1\nlabels: []\n"}) == []
    found = run(tmp_path, {HOTKEYS: GOOD_HOTKEYS, ALLOW: "schema_version: 1\nlabels: [Delete]\n"})
    assert [(r, s, p) for r, s, p, _ in found] == [("L-XREF-18", "warn", "labels")]


def test_the_full_lint_reports_both_rules_with_schema_failures(tmp_path):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    (root / HOTKEYS).write_text("schema_version: 1\nchords: []\n")                     # build missing
    (root / ALLOW).write_text("schema_version: 1\nlabels: [Send]\n")
    rules = {(v.rule, v.file) for v in lint(root, today=TODAY).violations}
    assert ("L-XREF-17", HOTKEYS) in rules and ("L-XREF-18", ALLOW) in rules
