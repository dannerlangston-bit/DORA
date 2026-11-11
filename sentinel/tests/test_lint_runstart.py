"""The run-start lint (sentinel.lint.runstart): expired masks and exceptions are dropped and listed, never
config_invalid (4.1, 4.5 L-SRF-11, 4.8 L-MSK-02 and L-RGN-05); a manifest that fails the lint is the run's missing
manifest, never an abort (4.3); the seven-day mask warning (3.8)."""
from __future__ import annotations

import copy
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint_repo
from sentinel.lint.repo import load_yaml_text
from sentinel.lint.rules import ExpiredRecord
from sentinel.lint.runstart import expiring_soon, lint_at_run_start, lint_manifest

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
AFTER_MASK = date(2026, 12, 8)            # msk.ps.last-update-01 expired on 2026-12-05; the /MES roll is 2026-12-11
MASK_ID = "msk.ps.last-update-01"
MASK = f"masks/census/monitor.activity.position-statement/ws.tier-a.active-trader/{MASK_ID}.yaml"
MANIFEST = "manifests/branch-1998.3.0-5d3e812.yaml"
CANDIDATE = "branch-1998.3.0-5d3e812"
S07 = "regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml"
S06 = "regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06.yaml"
PS = "registry/surfaces/monitor/monitor.activity.position-statement.yaml"


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def _region(rf: dict, rid: str) -> dict:
    return next(r for r in rf["regions"] if r["id"] == rid)


def test_expired_mask_is_an_error_in_ci_and_dropped_at_run_start(base):
    assert "L-MSK-02" in lint_repo(copy.deepcopy(base), 1, AFTER_MASK).rules()
    report, expired = lint_at_run_start(copy.deepcopy(base), 1, AFTER_MASK)
    assert report.ok and report.violations == []
    assert [(e.kind, e.record_id, e.rule) for e in expired] == [("mask_expired", MASK_ID, "L-MSK-02")]
    assert expired[0].run_health_line() == f"mask expired: {MASK_ID}"
    assert expired[0].scope == "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    assert expired[0].owner == "tos-monitor-ui"


def test_expired_floor_exception_is_dropped_at_run_start(base):
    r = copy.deepcopy(base)
    _region(r.docs[S06].data, "rgn.order-entry.side").update(floor_exception={
        "approved_by": "a", "approved_at": "2026-10-01T00:00:00Z", "owner": "o", "reason": "r", "ticket": "TOS-1",
        "measured_floor": 3, "expires_at": "2026-10-30T00:00:00Z"})
    assert "L-RGN-05" in lint_repo(copy.deepcopy(r), 1, TODAY).rules()
    report, expired = lint_at_run_start(r, 1, TODAY)
    assert "L-RGN-05" not in report.rules()
    assert [e.run_health_line() for e in expired] == ["floor exception expired: rgn.order-entry.side"]
    assert expired[0].kind == "floor_exception_expired"


def test_expired_invariant_exception_is_dropped_at_run_start(base):
    r = copy.deepcopy(base)
    r.docs[PS].data["invariant_exceptions"] = [{"invariant_id": "INV-06", "owner": "o", "reason": "r",
                                                "expires_at": "2026-11-01T00:00:00Z"}]
    assert "L-SRF-11" in lint_repo(copy.deepcopy(r), 1, TODAY).rules()
    report, expired = lint_at_run_start(r, 1, TODAY)
    assert "L-SRF-11" not in report.rules()
    assert expired == [ExpiredRecord("invariant_exception_expired", "INV-06", "monitor.activity.position-statement", PS,
                                     "invariant_exceptions[0]", "2026-11-01T00:00:00Z", "L-SRF-11", "o")]
    assert expired[0].run_health_line() == "invariant exception expired: INV-06"


def test_an_exception_without_its_human_fields_still_fails_at_run_start(base):
    r = copy.deepcopy(base)
    r.docs[PS].data["invariant_exceptions"] = [{"invariant_id": "INV-06", "owner": "o", "reason": "",
                                                "expires_at": "2026-11-01T00:00:00Z"}]
    report, expired = lint_at_run_start(r, 1, TODAY)
    assert "L-SRF-11" in report.rules() and expired == []


def test_other_errors_still_abort_at_run_start(base):
    r = copy.deepcopy(base)
    r.docs[S07].data["regions"].append(copy.deepcopy(r.docs[S07].data["regions"][0]))
    report, _ = lint_at_run_start(r, 1, TODAY)
    assert "L-RGN-01" in report.rules() and not report.ok


def test_manifest_errors_are_left_out_of_the_run_start_report(base):
    r = copy.deepcopy(base)
    r.docs[MANIFEST].data["entries"][0]["tickets"] = ["tos-48120"]
    assert "L-MAN-05" in lint_repo(copy.deepcopy(r), 1, TODAY).rules()
    report, _ = lint_at_run_start(r, 1, TODAY)
    assert report.ok
    found = lint_manifest(r, MANIFEST, candidate=CANDIDATE)
    assert [v.rule for v in found] == ["L-MAN-05"] and found[0].file == MANIFEST


def test_manifest_naming_another_candidate_is_l_man_09(base):
    assert lint_manifest(copy.deepcopy(base), MANIFEST, candidate=CANDIDATE) == []
    found = lint_manifest(copy.deepcopy(base), MANIFEST, candidate="branch-1998.3.0-7be42d0")
    assert [(v.rule, v.severity, v.path) for v in found] == [("L-MAN-09", "error", "candidate")]
    assert "branch-1998.3.0-7be42d0" in found[0].message


def test_manifest_outside_the_checkout_is_linted_as_a_manifest(tmp_path, base):
    data = load_yaml_text((EXAMPLE / MANIFEST).read_text())     # keeps `state: on` the string the enum names
    data["entries"][0]["declared_surfaces"].append("charts.unknown")
    p = tmp_path / "manifest.yaml"
    p.write_text(yaml.safe_dump(data))
    found = lint_manifest(base, p, candidate=CANDIDATE)
    assert {v.rule for v in found} == {"L-MAN-01", "L-XREF-06"}
    assert {v.file for v in found} == {"manifests/manifest.yaml"}
    assert "manifests/manifest.yaml" not in base.docs                 # the caller's repository is not changed
    missing = lint_manifest(base, tmp_path / "absent.yaml")
    assert "L-MAN-00" in {v.rule for v in missing} and all(v.severity == "error" for v in missing)


def test_run_start_lint_reads_a_directory(tmp_path):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    report, expired = lint_at_run_start(root, 1, AFTER_MASK)
    assert report.ok and [e.record_id for e in expired] == [MASK_ID]


def test_owner_warning_seven_days_before_a_mask_expires(base):
    assert expiring_soon(base, date(2026, 11, 27)) == []                   # 8 days before 2026-12-05T15:20-05:00
    soon = expiring_soon(base, date(2026, 11, 30))
    assert [(w.mask_id, w.days_left, w.owner) for w in soon] == [(MASK_ID, 5, "tos-monitor-ui")]
    assert soon[0].line().startswith(f"mask {MASK_ID} expires in 5 days")
    assert expiring_soon(base, AFTER_MASK) == []                           # expired: run health, not a warning
