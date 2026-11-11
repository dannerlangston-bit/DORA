"""calibration/<hex>.yaml against the repository: L-XREF-04, L-XREF-11, L-RGN-06, L-XREF-05 and the file name
(4.1, 4.8 Threshold source, 4.15; sentinel.lint.checks.calibration_files)."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Callable

import pytest

from sentinel.canonical import calibration_filename
from sentinel.lint import Repo, lint_repo
from sentinel.lint.checks import calibration_files as C
from sentinel.lint.repo import Doc

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
KEY = "sha256:" + "ab" * 32
REL = f"calibration/{calibration_filename(KEY)}"
FROZEN = "flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06"      # data_mode frozen, tier 1
STOCK = "registry/surfaces/trade/trade.order-entry.stock.yaml"
ZERO = {"changed_px": 0, "component_area": 0, "delta_e": 0.0, "shift_px": 0}
THRESH = {"component_area_min": 4, "changed_px_min": 4, "delta_e_min": 2.0, "shift_px_min": 1}


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def tier1(base: Repo) -> list[str]:
    defined, _ = C.checkpoint_definitions(base)
    return sorted(cid for cid, c in defined.items() if c.tier == 1)


def calibration(cids: list[str], key: str = KEY, exported_at: str = "2026-11-08T03:00:00-05:00") -> dict:
    return {"schema_version": 1, "calibration_key": key, "fingerprint_id": "sha256:" + "cd" * 32,
            "calibration_run_id": "run-2026-11-08-calibration-0001", "exported_at": exported_at,
            "checkpoints": [{"checkpoint_id": c, "settle_p50_ms": 400,
                             "regions": [{"region_id": "_rest", "floors": dict(ZERO), "thresholds": dict(THRESH)}]}
                            for c in cids]}


def found(base: Repo, fn: Callable[[Repo], None]) -> list:
    r = copy.deepcopy(base)
    fn(r)
    return lint_repo(r, 1, TODAY).violations


def with_file(data: dict, rel: str = REL) -> Callable[[Repo], None]:
    def fn(r: Repo) -> None:
        r.docs[rel] = Doc(rel, "calibration", data)
    return fn


def test_a_complete_calibration_lints_clean(base):
    got = found(base, with_file(calibration(tier1(base))))
    assert not [v for v in got if v.file.startswith("calibration/")], [v.line() for v in got]


def test_an_unknown_checkpoint_is_l_xref_04(base):
    cids = tier1(base) + ["flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s99"]
    got = [v for v in found(base, with_file(calibration(cids))) if v.rule == "L-XREF-04"]
    assert [v.path for v in got] == [f"checkpoints[{len(cids) - 1}].checkpoint_id"] and got[0].file == REL


@pytest.mark.parametrize("cid,needle", [
    ("flow/Trade/x/y", "does not match the checkpoint id grammar"),
    ("census/trade.nothing/ws.tier-a.beginner", "surface segment trade.nothing"),
])
def test_a_malformed_or_unregistered_id_is_l_xref_11(base, cid, needle):
    got = [v for v in found(base, with_file(calibration(tier1(base) + [cid]))) if v.rule == "L-XREF-11"]
    assert got and needle in got[0].message


def test_a_nonzero_floor_on_a_frozen_checkpoint_warns_l_rgn_06(base):
    data = calibration(tier1(base))
    cp = next(c for c in data["checkpoints"] if c["checkpoint_id"] == FROZEN)
    cp["regions"][0]["floors"]["changed_px"] = 12
    got = [v for v in found(base, with_file(data)) if v.rule == "L-RGN-06"]
    assert [(v.severity, v.file) for v in got] == [("warn", REL)] and FROZEN in got[0].message
    cp["regions"][0]["floors"].update(changed_px=0, delta_e=1.5)                       # a color floor alone is fine
    assert not [v for v in found(base, with_file(data)) if v.rule == "L-RGN-06"]


def test_the_file_name_is_its_calibration_key(base):
    got = [v for v in found(base, with_file(calibration(tier1(base)), "calibration/other.yaml")) if v.rule == "L-RGN-00"]
    assert any(REL in v.message for v in got)


def test_a_tier1_checkpoint_missing_after_the_first_calibration_is_an_error(base):
    cids = tier1(base)
    got = [v for v in found(base, with_file(calibration([c for c in cids if c != FROZEN]))) if v.rule == "L-XREF-05"]
    assert [(v.severity, v.file) for v in got] == [("error", REL)] and FROZEN in got[0].message


def test_the_canonical_calibration_is_the_latest_export(base):
    cids = tier1(base)
    old_key = "sha256:" + "01" * 32
    def fn(r: Repo) -> None:
        with_file(calibration(cids, old_key, "2026-10-01T00:00:00-05:00"), f"calibration/{calibration_filename(old_key)}")(r)
        with_file(calibration(cids[1:], exported_at="2026-11-08T03:00:00-05:00"))(r)
    got = [v for v in found(base, fn) if v.rule == "L-XREF-05"]
    assert [v.file for v in got] == [REL] and cids[0] in got[0].message


def test_before_the_first_calibration_a_given_key_is_one_warning(base):
    """The run-start wiring passes the run's key: no file for it before any Calibration run is one warning."""
    got = C.calibration_coverage(base, KEY, first_run_done=False)
    assert [(rule, sev) for rule, _f, _m, _p, sev in got] == [("L-XREF-05", "warn")]
    assert f"{len(tier1(base))} tier-1 checkpoints are uncalibrated" in got[0][2]
    assert C.calibration_coverage(base, KEY, first_run_done=True)[0][4] == "error"
    assert C.calibration_coverage(base) == []                     # no file, no key: no canonical calibration yet


def test_critical_checkpoints_are_left_to_l_mut_03(base):
    """L-XREF-05's mutant half is L-MUT-03 (sentinel.lint.checks.mutants); this module does not repeat it."""
    def fn(r: Repo) -> None:
        r.docs[STOCK].data["critical"] = True
    assert not [v for v in found(base, fn) if v.rule == "L-XREF-05"]
