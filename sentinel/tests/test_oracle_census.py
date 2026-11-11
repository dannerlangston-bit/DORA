"""Census and sweep checkpoints' Oracle checks (2.1, 2.8.1, 3.8, SD-29, L-RGN-04 census half): every rule region of
the regions file, evaluated on each side's capture and folded into the classifier like flow assertions."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from sentinel.classify import Facts, classify
from sentinel.oracle import (CensusChecks, EvalContext, GroundTruthReader, OracleConfigError, RegionHistory,
                             RegionRuleChecker, checks_for, summarize)
from sentinel.taxonomy import TaxonomyError

ROOT = Path(__file__).resolve().parents[1]
CENSUS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
CENSUS_FILE = yaml.safe_load((ROOT / "config/example/regions/census/monitor.activity.position-statement/"
                                     "ws.tier-a.active-trader.yaml").read_text())
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
SPLIT = "split/trade.active-trader.ladder/splitter.main/0.50"
FLOW = "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/cp-positions"
AT = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
POSITIONS = {"rgn.ps.mark": "180.25\n12.50\n5,325.00", "rgn.ps.pl-open": "+1,150.00\n-$25.00\n$0.00"}


def evaluate(values: dict, checkpoint: str = CENSUS, regions_file: dict = CENSUS_FILE) -> list:
    checks = checks_for(checkpoint, regions_file)
    assert checks.rules
    checker = RegionRuleChecker(context=EvalContext(data_mode="masked_live"))
    return checker.check(checkpoint, "candidate", regions_file, {"ocr": GroundTruthReader(values)}, RegionHistory(),
                         AT)


def test_a_census_checkpoint_runs_every_rule_region_of_its_regions_file():
    checks = checks_for(CENSUS, CENSUS_FILE)
    assert isinstance(checks, CensusChecks) and checks.kind == "census"
    assert [r.region_id for r in checks.rules] == ["rgn.ps.mark", "rgn.ps.pl-open"]
    assert checks.assertion_ids == ("rule.rgn.ps.mark.field", "rule.rgn.ps.mark.decimals", "rule.rgn.ps.mark.min",
                                    "rule.rgn.ps.mark.min_rows", "rule.rgn.ps.pl-open.field",
                                    "rule.rgn.ps.pl-open.decimals")
    results = evaluate(POSITIONS)
    assert [r.assertion_id for r in results] == list(checks.assertion_ids)
    assert all(r.status == "passed" for r in results)
    assert classify(Facts(**summarize(results).facts_fields())).outcome == "identical"


def test_a_census_rule_region_reading_nan_is_assertion_failed():
    results = evaluate(dict(POSITIONS, **{"rgn.ps.pl-open": "+1,150.00\nNaN\n$0.00"}))
    verdict = summarize(results)
    assert verdict.failed == ("rule.rgn.ps.pl-open.field",)
    c = classify(Facts(**verdict.facts_fields()))
    assert (c.row, c.outcome, c.reason) == ("8", "assertion_failed", "assertion:rule.rgn.ps.pl-open.field")
    assert "not a valid money: NaN (R10)" in c.detail


def test_a_census_money_cell_that_is_not_a_rule_region_is_listed():
    assert checks_for(CENSUS, CENSUS_FILE).unchecked_money_cells == ("rgn.ps.qty",)   # the example's diff money cell
    fixed = {**CENSUS_FILE, "regions": [
        dict(r, type="rule", rule={"field": "int", "min_rows": 1}) if r["id"] == "rgn.ps.qty" else r
        for r in CENSUS_FILE["regions"]]}
    checks = checks_for(CENSUS, fixed)
    assert checks.unchecked_money_cells == () and "rule.rgn.ps.qty.min_rows" in checks.assertion_ids


def test_sweep_and_splitter_steps_are_census_kinds_and_flows_are_refused():
    file = {"regions": [{"id": "rgn.cell.last", "type": "rule", "rule": {"field": "price", "decimals": 2}}]}
    assert checks_for(SWEEP, dict(file, checkpoint_id=SWEEP)).kind == "sweep"
    split = checks_for(SPLIT, file)
    assert split.kind == "split" and split.assertion_ids == ("rule.rgn.cell.last.field", "rule.rgn.cell.last.decimals")
    assert evaluate({"rgn.cell.last": "1.234"}, SWEEP, dict(file, checkpoint_id=SWEEP))[1].status == "failed"
    assert checks_for(SWEEP, None) == CensusChecks(SWEEP, "sweep", ())
    with pytest.raises(OracleConfigError, match="flow assertions"):
        checks_for(FLOW, file)
    with pytest.raises(OracleConfigError, match="L-RGN-01"):
        checks_for(SWEEP, CENSUS_FILE)
    with pytest.raises(TaxonomyError):
        checks_for("census/nope", file)
