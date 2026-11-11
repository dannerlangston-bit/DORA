"""The Oracle (2.8, 5.10): typed reads of a checkpoint's regions, checked against declared
expectations, the arithmetic invariants R01 to R10 and the market-data API.

Modules:

- `values`: thinkorswim's money, price, quantity, percent, date and time display formats, parsed
  to typed values; the R10 format test.
- `sources`: the reader interfaces (OCR, JAB, export, API), the simulated `GroundTruthReader` and
  `GroundTruthApi`, exports, confidence floors, the two-pass digit check, the alternate-source rule
  and the OCR calibration panel (2.8.1).
- `tables`: tolerances (2.8.2), multipliers and ticks (2.8.3), the R02 margin brackets of
  `fixtures/margin_rules.yaml`, the paperMoney delay model and the evaluation context.
- `rules`: R01 to R10 (2.8.3) and `check_rules`, the offline re-run over stored extractions.
- `assertions`: `evaluate_assertions` over the closed kind enum of 4.7, and `summarize`, which
  folds the results into the `classify.Facts` fields of 3.11 and the reporter's `raw_values`.
- `region_rules`: `RegionRuleChecker`, the typed assertion of every `rule` region of a regions file
  (3.8, 4.8) on flow, census and sweep checkpoints, with `RegionHistory` for the temporal checks.
- `census`: `checks_for`, the Oracle checks of a census or sweep checkpoint: its rule regions (2.8.1, SD-29).
- `extraction_log`: `to_rows`, every extraction as an 8.3 `oracle_extractions` row, and `rerun_offline`,
  rules re-run over stored rows (2.8.1, SD-09).

A typical flow checkpoint:

    results = evaluate_assertions(step, {"ocr": ocr, "jab": jab}, previous, fixture, rules,
                                  context=EvalContext(data_mode="frozen", symbol="AAPL", opening=True))
    results += checker.check(checkpoint_id, "candidate", regions_file, {"ocr": ocr, "jab": jab}, history, clock)
    store.write_extractions(to_rows(results, run_id=run_id, checkpoint_id=checkpoint_id, side="candidate",
                                    read_at=clock.now()))
    facts = Facts(**summarize(results).facts_fields(harness_event_id=event_id), ...)

A census or sweep checkpoint runs only `checker.check` (its `census.checks_for(...).rules`).
"""
from .assertions import (KINDS, Assertion, AssertionResult, OracleVerdict, evaluate_assertions, step_assertions,
                         summarize)
from .census import CensusChecks, checks_for
from .extraction_log import StoredRead, rerun_offline, to_rows
from .region_rules import (REGION_RULE_KIND, ApiWindow, PriorRead, RegionHistory, RegionRule, RegionRuleChecker,
                           RegularSession, VenueCalendar, invariant_rule_regions, rule_regions)
from .rules import RULE_IDS, RuleResult, RuleSpec, check_rules, evaluate_rule
from .sources import (FLOORS, ApiReader, ApiUnavailable, CalibrationPanel, CalibrationResult, CalibrationSample,
                      ExportReader, ExportSource, ExportTable, Extraction, GroundTruthApi, GroundTruthReader,
                      JabReader, OcrReader, Reading, SourceFallback, ValueSource, check_calibration, extract)
from .tables import (DEFAULT_MARGIN, EvalContext, MarginRules, OracleConfigError, RuleConfig, Tolerance,
                     api_comparison, delay_minutes)
from .values import FORMATS, FormatError, format_error, parse

__all__ = [
    "KINDS", "Assertion", "AssertionResult", "OracleVerdict", "evaluate_assertions", "step_assertions", "summarize",
    "CensusChecks", "checks_for", "StoredRead", "rerun_offline", "to_rows",
    "REGION_RULE_KIND", "ApiWindow", "PriorRead", "RegionHistory", "RegionRule", "RegionRuleChecker", "RegularSession",
    "VenueCalendar", "invariant_rule_regions", "rule_regions",
    "RULE_IDS", "RuleResult", "RuleSpec", "check_rules", "evaluate_rule",
    "FLOORS", "ApiReader", "ApiUnavailable", "CalibrationPanel", "CalibrationResult", "CalibrationSample",
    "ExportReader", "ExportSource", "ExportTable", "Extraction", "GroundTruthApi", "GroundTruthReader", "JabReader",
    "OcrReader", "Reading", "SourceFallback", "ValueSource", "check_calibration", "extract",
    "DEFAULT_MARGIN", "EvalContext", "MarginRules", "OracleConfigError", "RuleConfig", "Tolerance",
    "api_comparison", "delay_minutes",
    "FORMATS", "FormatError", "format_error", "parse",
]
