"""Every Oracle extraction stored, and rules re-run over the stored rows (2.8.1, 5.10, 8.3 `oracle_extractions`,
SD-09).

`to_rows(results, run_id=..., checkpoint_id=..., side=..., read_at=...)` turns the reads behind one side's
assertion results at one checkpoint (flow assertions and rule regions alike) into `records.Extraction` rows for
`Store.write_extractions`: one row per source tried for each field, with its raw text, confidence and crop, the
parsed value on the accepted read, and one more row for an OCR read's 2x second pass. `reads(rows)` rebuilds each
field's extraction from stored rows, and `rerun_offline(rows, rules)` re-runs rule-region checks
(`region_rules.RegionRule`) and the arithmetic invariants R01 to R10 (`rules.RuleSpec` or 4.7 `rule` assertions)
over them, re-judging every stored read against the rule's own format floor (2.8.1: "rules re-run offline over the
stored rows").

Choices where the spec is ambiguous (reported):

- One read, one row set: a read several assertions share (the checks of one rule region share its read) is stored
  once, under the first assertion that used it (a rule region's `rule.<region_id>.field`). The earlier capture a
  temporal check compares with (`previous`) is not stored again; it was stored with its own checkpoint.
- `region_id` is the region read: the field itself for a region read, and the assertion's region for the API value
  (`api:<field>`) or export column (`export:<column>`) it was compared with.
- `value` is the accepted read's parsed value in the canonical display form (`values.show`), rows joined by
  newlines, and None for a rejected read, an empty read or a read whose rows do not all parse; `text` keeps the raw
  read either way. The 2x second-pass row has no confidence of its own (None).
- `crop_sha256` is the read's crop reference when it is a sha256 (`sha256:<hex>` or 64 hex digits), else None:
  the column holds content hashes, never paths.
- Offline re-runs: when one field holds rows of several assertions, the first assertion's rows are the read. The
  temporal rule-region checks compare captures in order under a venue calendar and are not re-run; `api_window` is
  re-run over the stored API value. Rows of different checkpoints are re-run separately, with one evaluation
  context (call once per checkpoint when their symbols or data modes differ).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..records import Extraction as ExtractionRow
from .assertions import AssertionResult
from .region_rules import RegionRule, RegionRuleChecker
from .rules import DEFAULT_RULES, NO_CONTEXT, RuleSpec, check_rules, rebind
from .sources import Attempt, Extraction, Reading
from .tables import EvalContext, RuleConfig
from .values import FormatError, parse, rows, show

_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_COMPARED = ("api", "export")                     # operands that are not a read of a region
_NOT_STORED = ("previous",)                       # an earlier capture's read, stored with its own checkpoint


def _crop_sha(ref: str | None) -> str | None:
    return ref if ref is not None and _SHA256.match(ref) else None


def _value(fmt: str, text: str | None) -> str | None:
    """The parsed value of an accepted read in display form, rows joined by newlines (None: no value)."""
    cells = rows(text)
    if not cells:
        return None
    try:
        return "\n".join(show(fmt, parse(fmt, c)) for c in cells)
    except FormatError:
        return None


def _ts(t: datetime | str) -> str:
    return t.isoformat() if isinstance(t, datetime) else t


def to_rows(results: Sequence[AssertionResult], *, run_id: str, checkpoint_id: str, side: str,
            read_at: datetime | str) -> list[ExtractionRow]:
    """The 8.3 `oracle_extractions` rows of one side's assertion results at one checkpoint (module docstring)."""
    out: list[ExtractionRow] = []
    seen_reads: set[int] = set()
    seen_keys: set[tuple[str, str, str, bool]] = set()
    at = _ts(read_at)
    for r in results:
        for name, ext in r.operands.items():
            if name in _NOT_STORED or id(ext) in seen_reads:
                continue
            seen_reads.add(id(ext))
            region_id = r.region if name in _COMPARED else ext.field
            for a in ext.attempts:
                reading = a.reading
                accepted = reading is not None and reading is ext.accepted
                passes = [(False, reading.text if reading else None, reading.confidence if reading else None)]
                if reading is not None and reading.second_pass is not None:
                    passes.append((True, reading.second_pass, None))
                for second, text, confidence in passes:
                    key = (r.assertion_id, ext.field, a.source, second)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    out.append(ExtractionRow(
                        run_id=run_id, checkpoint_id=checkpoint_id, side=side, assertion_id=r.assertion_id,
                        field=ext.field, source=a.source, read_at=at, region_id=region_id, text=text,
                        value=_value(ext.fmt, text) if accepted and not second else None, confidence=confidence,
                        second_pass=second, crop_sha256=_crop_sha(reading.crop_ref if reading else None)))
    return out


@dataclass(frozen=True)
class StoredRead:
    """One field's stored read at one checkpoint and side, rebuilt from its rows. `extraction` holds every
    attempt in stored order with nothing accepted yet: `rules.rebind(extraction, fmt)` judges it under a format."""

    checkpoint_id: str
    side: str
    field: str
    region_id: str | None
    read_at: datetime
    extraction: Extraction


def reads(rows_: Sequence[ExtractionRow]) -> dict[tuple[str, str], dict[tuple[str, str | None], StoredRead]]:
    """Stored reads by (checkpoint id, side), then by (field, region id), from `Store.extractions` rows."""
    groups: dict[tuple[str, str, str, str | None], list[ExtractionRow]] = {}
    for row in rows_:
        key = (row.checkpoint_id, row.side, row.field, row.region_id)
        group = groups.setdefault(key, [])
        if not group or group[0].assertion_id == row.assertion_id:
            group.append(row)
    out: dict[tuple[str, str], dict[tuple[str, str | None], StoredRead]] = {}
    for (cid, side, fld, region_id), group in groups.items():
        second = {r.source: r.text for r in group if r.second_pass}
        attempts = []
        for r in (r for r in group if not r.second_pass):
            reading = None if r.text is None else Reading(fld, r.source, r.text, float(r.confidence or 0.0),
                                                          second.get(r.source), r.crop_sha256)
            attempts.append(Attempt(r.source, reading, None if reading is not None else "nothing read"))
        ext = Extraction(fld, "text", attempts[0].source if attempts else "ocr", tuple(attempts))
        out.setdefault((cid, side), {})[(fld, region_id)] = StoredRead(cid, side, fld, region_id,
                                                                       datetime.fromisoformat(group[0].read_at), ext)
    return out


def _rule_result(spec: RuleSpec, by_field: Mapping[tuple[str, str | None], StoredRead], fixture: Mapping[str, Any],
                 config: RuleConfig, context: EvalContext) -> AssertionResult | None:
    refs = {name: ref for name, ref in spec.inputs.items() if isinstance(ref, str)}
    if not refs or any((ref, ref) not in by_field for ref in refs.values()):
        return None
    stored = {ref: by_field[(ref, ref)].extraction for ref in refs.values()}
    [rr] = check_rules(stored, fixture, [spec], config=config, context=context)
    operands = {name: rebind(stored[ref], spec.operand_format(name)) for name, ref in refs.items()}
    return AssertionResult(spec.assertion_id, "rule", rr.status, rr.expected, rr.extracted, rr.detail, operands,
                           unreadable=rr.unreadable, rule=rr)


def rerun_offline(rows_: Sequence[ExtractionRow], rules: Sequence[RegionRule | RuleSpec | Mapping[str, Any]], *,
                  fixture: Mapping[str, Any] | None = None, config: RuleConfig = DEFAULT_RULES,
                  context: EvalContext = NO_CONTEXT) -> dict[tuple[str, str], list[AssertionResult]]:
    """Re-run `rules` over stored extraction rows (2.8.1, SD-09): for every (checkpoint id, side) whose rows hold
    a rule's reads, the rule's results in rule order. A rule region re-runs its single-read checks and, over the
    stored API value, `api_window`; an R01 to R10 rule re-runs where every region operand was stored."""
    fx: Mapping[str, Any] = fixture or {}
    checker = RegionRuleChecker(fixture=fx, config=config, context=context)
    specs = [r if isinstance(r, (RegionRule, RuleSpec)) else RuleSpec.from_assertion(r) for r in rules]
    out: dict[tuple[str, str], list[AssertionResult]] = {}
    for key, by_field in reads(rows_).items():
        results: list[AssertionResult] = []
        for spec in specs:
            if isinstance(spec, RuleSpec):
                res = _rule_result(spec, by_field, fx, config, context)
                results += [res] if res is not None else []
                continue
            read = by_field.get((spec.region_id, spec.region_id))
            if read is None:
                continue
            api = by_field.get((f"api:{spec.api_window.field}", spec.region_id)) if spec.api_window else None
            results += checker.recheck(spec, rebind(read.extraction, spec.field), read.read_at,
                                       api_read=rebind(api.extraction, "price") if api is not None else None)
        if results:
            out[key] = results
    return out
