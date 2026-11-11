"""The effective config and the Phase 0 branches wired into the simulated PR run (w2 wiring item 6): the invariant
inputs per checkpoint from `effective.effective_registry`, the checker config with the workspace theme's palette and
the catalog's error titles, the DR-07 Reviewer switch, and the lint fixes (L-FIX-01, L-FIX-05, L-SRF-11, L-XREF-15's
phase rule, L-XREF-30)."""
from __future__ import annotations

import shutil
from datetime import date

import yaml

from sentinel.demo import EXAMPLE, flexgrid_scenario
from sentinel.lint.rules import lint as lint_repo
from sentinel.pipeline.run import run_pipeline

TODAY = date(2026, 11, 10)


def _copy(tmp_path):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def _edit(path, fn):
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _rules(root, rule, phase=0):
    return [v for v in lint_repo(root, phase=phase, today=TODAY).violations if v.rule == rule]


def test_an_unpinned_reviewer_leaves_the_run_unreviewed(tmp_path):
    root = _copy(tmp_path)
    _edit(root / "phase0" / "branches.yaml", lambda d: d.update(reviewer={}))
    r = run_pipeline(root, flexgrid_scenario(TODAY), tmp_path / "out")
    assert r.reviewer_calls and all(c["model_id"] == "none" and "reviewer_disabled" in c["unavailable"]
                                    for c in r.reviewer_calls)              # 10.7: no model call is made
    assert r.status == "complete"


def test_a_missing_palette_and_a_stray_allowlist_control_are_l_xref_30(tmp_path):
    root = _copy(tmp_path)
    (root / "palettes" / "dark.yaml").unlink()
    surface = root / "registry" / "surfaces" / "trade" / "trade.active-trader.ladder.yaml"
    _edit(surface, lambda d: d.setdefault("invariants", {}).update(overlap_allowlist=[["ghost", "order-strip"]]))
    lines = [v.message for v in _rules(root, "L-XREF-30")]
    assert any("no palette" in m for m in lines) and any("'ghost'" in m for m in lines)


def test_an_invariant_exception_must_name_an_invariant_of_2_7(tmp_path):
    root = _copy(tmp_path)
    surface = root / "registry" / "surfaces" / "trade" / "trade.active-trader.ladder.yaml"
    _edit(surface, lambda d: d.update(invariant_exceptions=[{"invariant_id": "INV-99", "owner": "x", "reason": "y",
                                                              "expires_at": "2027-01-01T00:00:00Z"}]))
    assert any("INV-99" in v.message for v in _rules(root, "L-SRF-11"))


def test_order_confirmation_absent_is_accepted_only_when_item_3_reads_different(tmp_path):
    root = _copy(tmp_path)
    prof = root / "profiles" / "profile.app-settings.sentinel-pinned.yaml"
    _edit(prof, lambda d: d["settings"].update(order_confirmation="absent"))
    assert _rules(root, "L-FIX-05")

    def item3(d):
        d["safety"] = [s for s in d.get("safety") or [] if s.get("item") != 3] + [{"item": 3, "result": "different"}]
    _edit(root / "phase0" / "branches.yaml", item3)
    assert not [v for v in _rules(root, "L-FIX-05") if "order_confirmation" in v.message]
