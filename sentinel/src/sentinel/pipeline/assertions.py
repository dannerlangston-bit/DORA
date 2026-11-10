"""The Oracle on the simulated run (2.8, 4.7, 5.10): every flow checkpoint that declares assertions.

On the rig the Oracle reads a checkpoint's regions by OCR, the Java Access Bridge, an export or the
market-data API. Here those readers are `oracle.GroundTruthReader` over what `sentinel.synth` drew:
`sim.region_values` gives each region's text, rows top to bottom. `FlowOracle.check` evaluates the
step's assertions (`expect` of a checkpoint step) with `oracle.evaluate_assertions` on one side's
capture and returns the `OracleVerdict`; the run feeds `OracleVerdict.facts_fields()` into
`classify.Facts` (3.11 inputs G and H) and the failed assertion's `raw_values` (both builds) into
`Finding.evidence`.

What the Oracle is given (4.7, 2.8.1):

- the evaluation context: the checkpoint's data mode, the capture time, the flow's ticket symbol (the
  `fixture.symbols.<id>.symbol` its first `type_fixture` step types), and for a flow with an order-stage
  step an opening order (the example's BUY tickets) of the quantity its `qty` expectation names; the
  account kind is left to the Oracle's default, the fixture's golden account (4.9);
- prior extractions: every region read at an earlier checkpoint of the same flow on the same side, by
  step id, for `changed_since_previous_step` and `unchanged_since_previous_step`;
- no export source: the example config has no `export` step, and neither 4.7's export step (`target`,
  `kind`, `path`) nor any other record declares the header set 2.8.1 compares an export with, so there
  is no declared export header to supply (reported as a spec gap); an export assertion therefore reads
  as unreadable here, never as a pass;
- the rule tables: `fixtures/margin_rules.yaml` through `RuleConfig.load` (the config is invalid
  without it, 4.1).

An `OracleConfigError` at run time (a flow the lint accepted but the Oracle cannot evaluate) is a
harness fault, `harness_error (reason: exception:OracleConfigError)`, never a pass or a product failure.

WORKAROUND(oracle): `present` and `absent` with a `format` and no `pattern` parse the region's whole
text as one value, so a column region (the example's `ps_qty` over seven Qty rows) can never hold "an
int value". When such an assertion meets a region that reads more than one row, `FlowOracle` evaluates
it once per row with the Oracle itself and keeps the first row that does not pass (every row passes:
passed). Reported to the coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .. import oracle
from ..diff.regions import RegionsFile
from ..oracle.assertions import AssertionResult, OracleVerdict, step_assertions
from ..oracle.sources import Extraction, GroundTruthReader, extract
from ..oracle.tables import EvalContext, OracleConfigError, RuleConfig, resolve_ref
from .config import SentinelConfig
from .sim import Capture, region_values

OCR_CONFIDENCE = 95.0                  # what the simulated OCR reports for a drawn glyph (above every floor, 2.8.1)
MULTI_ROW_KINDS = ("present", "absent")


@dataclass
class OracleCheck:
    """One side's assertion results at one checkpoint, and what each region read."""

    verdict: OracleVerdict
    values: dict[str, str | None]

    def failed(self) -> list[AssertionResult]:
        return [r for r in self.verdict.results if r.status == "failed"]


@dataclass
class FlowOracle:
    """The Oracle for one run: rule tables, fixture, and the extractions of earlier checkpoints per flow."""

    cfg: SentinelConfig
    rules: RuleConfig
    capture_time: datetime
    previous: dict[tuple[str, str], dict[str, dict[str, Extraction]]] = field(default_factory=dict)

    @classmethod
    def load(cls, cfg: SentinelConfig, capture_time: datetime) -> "FlowOracle":
        """The run's Oracle; `fixtures/margin_rules.yaml` is required (`OracleConfigError` without it)."""
        path = cfg.root / "fixtures" / "margin_rules.yaml"
        if not path.is_file():
            raise OracleConfigError("fixtures/margin_rules.yaml is missing (4.1); R02 has no brackets")
        return cls(cfg, RuleConfig.load(path), capture_time)

    # -- the context of one checkpoint -----------------------------------------------------------
    def context(self, flow_id: str, step: Mapping[str, Any], data_mode: str) -> EvalContext:
        doc = self.cfg.flow_docs.get(flow_id, {})
        steps = list(doc.get("steps") or ())
        symbol = None
        for st in steps:
            ref = str(st.get("value_ref") or "")
            if st.get("type") == "type_fixture" and ref.startswith("fixture.symbols."):
                symbol = str(resolve_ref(self.cfg.fixture, ref))
                break
        order_stage = any(st.get("order_stage") is True for st in steps)
        qty = next((Decimal(str(a["expected"])) for a in step_assertions(step)
                    if a.get("id") == "qty" and a.get("expected") is not None), None)
        return EvalContext(data_mode=data_mode, capture_time=self.capture_time, symbol=symbol,
                           opening=True if order_stage else None, quantity=qty if order_stage else None)

    # -- one checkpoint, one side ----------------------------------------------------------------
    def check(self, flow_id: str, step_id: str, data_mode: str, capture: Capture, reg: RegionsFile,
              located: Mapping[str, tuple[int, int]], side: str) -> OracleCheck | None:
        """The step's assertions on one side's capture; None when the step declares none."""
        step = self.cfg.flow_step(flow_id, step_id)
        if not step_assertions(step):
            return None
        values = region_values(capture.screen, reg, located)
        sources: dict[str, Any] = {"ocr": GroundTruthReader(values, confidence=OCR_CONFIDENCE)}
        prev = self.previous.setdefault((side, flow_id), {})
        ctx = self.context(flow_id, step, data_mode)
        results = oracle.evaluate_assertions(step, sources, prev, self.cfg.fixture, self.rules, context=ctx)
        results = [self._per_row(a, r, values, sources, prev, ctx) for a, r in zip(step_assertions(step), results)]
        reads = {rid: extract(rid, "text", {"ocr": sources["ocr"]}, "ocr") for rid in values}
        for r in results:
            for ext in r.operands.values():
                if ext.field in reads:
                    reads[ext.field] = ext
        prev[step_id] = reads
        return OracleCheck(oracle.summarize(results), values)

    def _per_row(self, a: Mapping[str, Any], r: AssertionResult, values: Mapping[str, str | None],
                 sources: Mapping[str, Any], prev: Mapping[str, Mapping[str, Extraction]],
                 ctx: EvalContext) -> AssertionResult:
        """WORKAROUND(oracle): a format-only `present` or `absent` over a column, row by row (module docstring)."""
        text = values.get(str(a.get("region"))) or ""
        rows = text.split("\n")
        if a.get("kind") not in MULTI_ROW_KINDS or a.get("pattern") is not None or a.get("format") in (None, "text") \
                or len(rows) < 2:
            return r
        per = []
        for row in rows:
            one = dict(values, **{str(a["region"]): row})
            srcs = dict(sources, ocr=GroundTruthReader(one, confidence=OCR_CONFIDENCE))
            per.append(oracle.evaluate_assertions({"expect": [a]}, srcs, prev, self.cfg.fixture, self.rules,
                                                  context=ctx)[0])
        worst = next((x for x in per if x.status != "passed"), None)
        if worst is None:
            return replace(per[0], extracted=repr(text), operands=r.operands)
        return worst
