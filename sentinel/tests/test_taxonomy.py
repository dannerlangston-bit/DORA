import subprocess
import sys
from pathlib import Path

import pytest

from sentinel.taxonomy import TaxonomyError, checkpoint_parts, is_checkpoint_id, load

ROOT = Path(__file__).resolve().parents[1]


def test_taxonomy_matches_spec():
    """L-XREF-08: config/taxonomy.yaml is exactly what the spec's tables say."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_taxonomy.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_closed_outcome_sets():
    t = load()
    assert len(t.checkpoint_outcomes) == 15
    assert len(t.step_outcomes) == 5
    assert len(t.run_outcomes) == 5
    assert len(t.finding_kinds) == 40


@pytest.mark.parametrize("outcome,reason,gate", [
    ("identical", None, None),
    ("collateral", None, None),
    ("collateral", "manifest_missing", None),
    ("noise", "below_floor", None),
    ("noise", "decision:dec-000412", None),
    ("needs_human", "reviewer_uncertain", None),
    ("invariant_violation", "INV-01", None),
    ("assertion_failed", "assertion:app_alive", None),
    ("not_settled", "unexpected_window:What's New", None),
    ("not_run", "step_failed:s04", None),
    ("env_error", "version_changed_mid_run", "launched"),
    ("env_error", "gate", "preflight.R1"),
    ("env_error", "kill_switch", "running"),           # inherited from aborted_safety
    ("harness_error", "dead_man", None),               # inherited from aborted_harness
    ("harness_error", "preflight.H8", None),
    ("landmark_missing", "probe:P3", None),
])
def test_legal_checkpoint_outcomes(outcome, reason, gate):
    load().check_checkpoint(outcome, reason, gate)


@pytest.mark.parametrize("outcome,reason,gate", [
    ("ignored", None, None),                          # not an outcome
    ("noise", "looks_fine", None),                    # unknown reason
    ("harness_error", "step_failed:s04", None),       # F4: never a harness_error reason
    ("not_comparable", "calibration_missing", None),  # F2
    ("env_error", "version_mismatch", None),          # F65: env_error needs a gate
    ("env_error", "version_mismatch", "soak"),        # no soak gate (F13)
    ("harness_error", "preflight.Z9", None),          # unknown preflight item
    ("not_run", "step_failed", None),                 # parameter required
])
def test_illegal_checkpoint_outcomes(outcome, reason, gate):
    with pytest.raises(TaxonomyError):
        load().check_checkpoint(outcome, reason, gate)


def test_run_outcomes():
    t = load()
    t.check_run("complete")
    t.check_run("aborted_safety", "live_identity")
    t.check_run("aborted_budget", "dollar_cap")
    t.check_run("aborted_harness", "preflight.H8")
    with pytest.raises(TaxonomyError):
        t.check_run("aborted_safety")
    with pytest.raises(TaxonomyError):
        t.check_run("aborted_budget", "dead_man")


@pytest.mark.parametrize("cid", [
    "census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=delta,strikes=16",
    "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360",
    "split/trade.active-trader.ladder/side-panel/0.15",
    "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07",
])
def test_checkpoint_ids_valid(cid):
    assert is_checkpoint_id(cid)


@pytest.mark.parametrize("cid", [
    "sweep/charts.flexible_grid.active-trader-cell/grid/480x360",  # underscore, old host
    "size_sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360",
    "split/trade.active-trader.ladder/side-panel/0.5",
    "flow/dialog.order-confirmation/s07",
])
def test_checkpoint_ids_invalid(cid):
    assert not is_checkpoint_id(cid)


def test_checkpoint_parts():
    p = checkpoint_parts("sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360")
    assert p == {"kind": "sweep", "surface": "charts.flexible-grid.active-trader-cell", "qualifier": "flexgrid/480x360"}
