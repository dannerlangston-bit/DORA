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
    "sweep/trade.active-trader.ladder/flexgrid/640x480-asc",       # SD-04: Ladder F ascending pass
    "sweep/dialog.order-confirmation/dialog/640x480",
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
    "sweep/trade.active-trader.ladder/tab/640x480",                # SD-05: tab hosts are swept by splitters only
    "sweep/trade.active-trader.ladder/sidebar/640x480",
    "sweep/trade.active-trader.ladder/flexgrid/640x480-desc",
    "sweep/trade.active-trader.ladder/flexgrid/640x480asc",
])
def test_checkpoint_ids_invalid(cid):
    assert not is_checkpoint_id(cid)


def test_checkpoint_parts():
    p = checkpoint_parts("sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360")
    assert p == {"kind": "sweep", "surface": "charts.flexible-grid.active-trader-cell", "qualifier": "flexgrid/480x360",
                 "host": "flexgrid", "w": 480, "h": 360, "ascending": False}
    asc = checkpoint_parts("sweep/trade.active-trader.ladder/flexgrid/640x480-asc")
    assert (asc["qualifier"], asc["w"], asc["h"], asc["ascending"]) == ("flexgrid/640x480-asc", 640, 480, True)
    assert checkpoint_parts("split/trade.active-trader.ladder/side-panel/0.15") == {
        "kind": "split", "surface": "trade.active-trader.ladder", "qualifier": "side-panel/0.15"}


# -- the lists added with the accepted spec decisions (foundation.taxonomy) ----------------------------------------

def test_generated_lists():
    t = load()
    assert t.quarantine_reasons == ["flake", "aa_drift", "aa_floor", "maintenance_budget", "fire_drill",
                                    "safety_canary", "kill_switch", "gateway_drops", "fallback_rate"]   # 10.5, SD-96
    assert t.money_roles == ["qty", "price", "bp_effect", "max_loss", "send", "cancel", "delete"]       # 4.8, SD-28
    assert t.popup_actions == ["dismiss_key", "click", "wait", "relogin", "abort", "ignore"]          # 4.10
    assert t.canary_kinds == ["mutant", "safety_canary", "fire_drill", "path_equivalence"]            # SD-81
    assert len(t.list("mutant_classes")) == 26
    assert {"gateway_decision", "size_step", "probe", "oracle_source_fallback", "focus_correction", "hook_reinstalled",
            "foreign_window", "export", "clipboard", "recovery_login", "explorer_action"} <= set(t.step_event_actions)
    assert t.decision_statuses == ["active", "expired", "superseded", "reopened", "revoked"]
    assert "jira_sync" in t.decision_sources
    assert "bounds_source_unavailable" in t.data["reasons"]["aborted_harness"]                        # SD-54


def test_reason_metadata():
    t = load()
    assert t.reason_meta("not_comparable", "fingerprint_mismatch")["owner_section"] == "3.3"           # SD-78
    assert t.reason_meta("not_run", "step_failed:s04") == {"owner_section": "5.2",
                                                           "detail_means": "step id that ended failed or blocked_by_gateway"}
    assert t.reason_meta("aborted_harness", "preflight.H8")["owner_section"] == "12.7"
    assert t.reason_meta("harness_error", "dead_man")["owner_section"] == "10.2"   # inherited from aborted_harness
    with pytest.raises(TaxonomyError):
        t.reason_meta("noise", "looks_fine")
    every = {(o, r) for o, rows in t.data["reason_metadata"].items() for r in rows}
    listed = {(o, p.split("<")[0].rstrip(":.")) for o, pats in t.data["reasons"].items() for p in pats}
    assert every == listed


def test_rule_operands():
    t = load()
    assert t.rule_operands("R01") == (frozenset({"qty", "price", "multiplier", "total"}), frozenset())
    assert t.rule_operands("R03") == (frozenset({"bid", "mark", "ask"}), frozenset({"last", "low", "high"}))
    assert sorted(t.data["rule_operands"]) == [f"R{i:02d}" for i in range(1, 11)]
    with pytest.raises(TaxonomyError):
        t.rule_operands("R11")


def test_canary_defect_class_rule():
    t = load()
    t.check_canary("mutant", "swapped_digit")
    t.check_canary("fire_drill", "fire_drill")
    for kind, cls in (("mutant", "fire_drill"), ("safety_canary", "swapped_digit"), ("chaos", "chaos")):
        with pytest.raises(TaxonomyError):
            t.check_canary(kind, cls)


def test_generation_refuses_a_spec_that_contradicts_itself(monkeypatch):
    """L-XREF-08 / L-OUT-01: gen_taxonomy compares the copies the spec keeps and stops on a difference."""
    sys.path.insert(0, str(ROOT / "tools"))
    import gen_taxonomy
    import spec_tables
    seed = spec_tables.outcome_reason_seed()
    monkeypatch.setattr(spec_tables, "outcome_reason_seed", lambda: [r for r in seed if r[1] != "dead_man"])
    with pytest.raises(gen_taxonomy.SpecDisagreement, match="dead_man"):
        gen_taxonomy.build()
    monkeypatch.setattr(spec_tables, "outcome_reason_seed", lambda: seed)
    monkeypatch.setattr(spec_tables, "listed_in_key_table", lambda key: ["qty"])
    with pytest.raises(gen_taxonomy.SpecDisagreement, match="triage_states"):
        gen_taxonomy.build()
