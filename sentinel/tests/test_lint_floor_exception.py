"""L-RGN-05, SD-24: a floor exception expires at most 30 days after its approval (lint/checks/floor_exception.py)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from sentinel.lint import lint
from sentinel.lint.checks import floor_exception
from sentinel.lint.repo import Repo
from sentinel.lint.rules import check_modules

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
REL = "regions/flow/charts.flexible-grid.active-trader-cell/flow.trade.active-trader.read-back/cp-atcell.yaml"


def regions_file(approved_at: str, expires_at: str) -> str:
    return ("schema_version: 1\n"
            "checkpoint_id: flow/charts.flexible-grid.active-trader-cell/flow.trade.active-trader.read-back/cp-atcell\n"
            "landmarks:\n  - {id: lm.atcell-header, template: regions/landmarks/lm.atcell-header.png}\n"
            "regions:\n"
            "  - {id: rgn.atcell.viewport, type: diff, landmark: lm.atcell-header, rect: {dx: 0, dy: 22, w: 300, h: 250}}\n"
            "  - id: rgn.atcell.depth\n    type: diff\n    landmark: lm.atcell-header\n"
            "    rect: {dx: 0, dy: 276, w: 60, h: 20}\n"
            "    floor_exception: {approved_by: jdoe, approved_at: '" + approved_at + "', owner: jdoe, "
            "reason: quote flash, ticket: TOS-48200, measured_floor: 6, expires_at: '" + expires_at + "'}\n")


def run(tmp_path: Path, text: str) -> list[tuple[str, str, str]]:
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    out: list[tuple[str, str, str]] = []

    def add(rule: str, file: str, message: str, path: str = "", severity: str | None = None) -> None:
        out.append((rule, path, message))

    floor_exception.check(Repo.load(tmp_path), add, 0, TODAY)
    return out


def test_thirty_days_is_the_most_a_floor_exception_lasts(tmp_path):
    assert run(tmp_path, regions_file("2026-11-09T12:00:00Z", "2026-12-09T12:00:00Z")) == []          # 30 days
    [(rule, path, message)] = run(tmp_path, regions_file("2026-11-09T12:00:00Z", "2026-12-24T12:00:00Z"))
    assert rule == "L-RGN-05" and path == "regions[1].floor_exception.expires_at" and "45 days" in message
    [(rule, _, message)] = run(tmp_path, regions_file("2026-11-09T12:00:00Z", "2026-11-09T12:00:00Z"))
    assert rule == "L-RGN-05" and "not after its approval" in message
    assert run(tmp_path, regions_file("not a time", "2026-12-24T12:00:00Z")) == []    # left to L-RGN-00


def test_the_module_is_discovered_and_the_example_stays_clean():
    assert floor_exception in check_modules()
    report = lint(EXAMPLE, today=TODAY)
    assert not [v for v in report.violations if v.rule == "L-RGN-05"]
