"""Assertion evaluation (2.6.2, 4.7) and what it means for the checkpoint (3.11, 4.13, 2.8.1).

`evaluate_assertions(flow_step, values_by_source, previous_values, fixture, rules, context=...)`
evaluates every assertion of a step (a checkpoint's `expect`, an `assert` step's `assertions`, a
`type_fixture` step's `verify`) and returns one `AssertionResult` per assertion, in declared order:

- `passed`;
- `failed`: `assertion_failed (assertion: <id>)` with expected and extracted values in `detail`;
- `unreadable`: an operand no declared source read above its confidence floor, alternates
  included: `needs_human (reason: oracle_unreadable)` with the field, the crop and every read;
- `harness_error`: `ocr_calibration` (an OCR-dependent assertion after a calibration-panel miss at
  `canary_flow_passed`) or `export_header_mismatch` (a file export whose header set differs from
  the declared one), never `assertion_failed`;
- `env_error`: an `api_within` whose market-data API failed, gate `preflight.N3` (preflight) or
  `connectivity` (mid-run); the checkpoint's other assertions stay in force.

`summarize(results)` folds them into an `OracleVerdict`, whose `facts_fields()` are the
`classify.Facts` fields of 3.11 input G (env, harness) and H (`assertion_ids`, `assertion_detail`,
`oracle_unreadable`): H is `assertion` when any assertion failed, else `unreadable` when any
operand was unreadable. `AssertionResult.raw_values(base)` is the `Finding.evidence.raw_values`
map (4.13): `expected`, `read` and `source` (the reporter's keys) plus what every source read on
the candidate and, given the base build's result, on the base.

The kinds are the closed enum of 4.7. Assertion field formats default as follows when the
assertion names none: `equals` by its expected literal (text for a string, date for a date,
otherwise price, a plain decimal), `within_tol` and `api_within` and `sign_color_agrees` price,
`export_equals` money, every other kind text (the 70 floor).

Choices where the spec is ambiguous (reported):

- `equals` is exact after normalization by format; money is rounded to the cent first (2.8.2).
- `matches` uses a regex search (the 4.8 example patterns anchor with `^` themselves).
- `present` and `absent`: with `pattern`, whether the text matches it; with only `format`, row by
  row: `present` needs every non-empty row of the region to be a valid value of that format (a column
  region such as the example's `ps_qty` over the Qty rows), `absent` needs no row to be one, and an
  empty region holds no value; with neither, whether the region holds any text.
- `changed_since_previous_step` / `unchanged_since_previous_step` compare typed values when both
  sides parse as the format and normalized text otherwise (an empty ticket field has no value).
  The earlier extraction is `previous_values[since_step][region]`; an unreadable earlier
  extraction makes the assertion unreadable.
- `count` is the number of non-empty rows the region reads.
- `export_equals` sums the export column's non-blank cells; a cell that is not a valid number
  fails the assertion. An export that produced nothing, or a Copy All text whose header differs
  (2.8.1 falls back to OCR, which cannot read a column total), is unreadable.
- `within_tol` without `tol` takes the 2.8.2 default of its format (`tables.default_tolerance`).
- `api_within` reads the region from the declared source (`source: api` means the default, OCR,
  since the API is the other side) and the API value per `tables.api_comparison`; an API that has
  no value for the modeled time is unreadable. `source: api` on any other kind is refused.
- `sign_color_agrees` classes the read's dominant color as the nearest pinned palette entry within
  the palette tolerance; a color near no entry, or the wrong entry, fails; a zero value expects
  `neutral` when the palette has it and is not judged otherwise.
- OCR dependence: an assertion whose declared source is OCR is `harness_error (ocr_calibration)`
  after a calibration miss; one declared on JAB or export is read without OCR and is
  `harness_error (ocr_calibration)` only if it would have needed the OCR fallback.
- An assertion id equal to a built-in (`app_alive`, `size_reached`, `soak.<metric>`) is refused:
  those come from the Watchdog, the sweep and the soak checks, not from the Oracle.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from ..classify import EnvFailure, HarnessFailure
from ..taxonomy import ASSERTION_ID
from .rules import DEFAULT_RULES, NO_CONTEXT, RuleResult, RuleSpec, evaluate_rule
from .sources import (EXACT_CONFIDENCE, SOURCES, ApiReader, ApiUnavailable, Attempt, Extraction, ExportReader, Reading,
                      SourceFallback, ValueSource, extract, problem)
from .tables import (EvalContext, OracleConfigError, RuleConfig, Tolerance, api_comparison, default_tolerance,
                     resolve_ref, tick_size)
from .values import NUMERIC_FORMATS, FormatError, coerce, normalize_text, number, parse, rows, show, to_cent

KINDS: tuple[str, ...] = ("equals", "matches", "within_tol", "present", "absent", "changed_since_previous_step",
                          "unchanged_since_previous_step", "count", "export_equals", "api_within", "rule",
                          "sign_color_agrees")
STATUSES: tuple[str, ...] = ("passed", "failed", "unreadable", "harness_error", "env_error")
BUILTIN_ASSERTIONS = ("app_alive", "size_reached")
_DEFAULT_FORMAT = {"within_tol": "price", "api_within": "price", "sign_color_agrees": "price", "export_equals": "money"}


@dataclass(frozen=True)
class Assertion:
    """One 4.7 assertion, validated."""

    id: str
    kind: str
    source: str = "ocr"
    region: str | None = None
    expected: Any = None
    range: tuple[Decimal, Decimal] | None = None
    value_ref: str | None = None
    pattern: str | None = None
    fmt: str | None = None
    tol: Tolerance | None = None
    since_step: str | None = None
    export_column: str | None = None
    api_field: str | None = None
    rule: RuleSpec | None = None

    @classmethod
    def from_dict(cls, a: Mapping[str, Any]) -> "Assertion":
        aid, kind = str(a.get("id", "")), a.get("kind")
        if not ASSERTION_ID.match(aid) or aid in BUILTIN_ASSERTIONS or aid.startswith("soak."):
            raise OracleConfigError(f"assertion id {aid!r} is not a flow assertion id")
        if kind not in KINDS:
            raise OracleConfigError(f"{aid}: kind {kind!r} is not one of the 4.7 kinds {KINDS}")
        source = a.get("source", "ocr")
        if source not in SOURCES:
            raise OracleConfigError(f"{aid}: source {source!r} is not one of {SOURCES}")
        if source == "api" and kind != "api_within":
            raise OracleConfigError(f"{aid}: source api reads no region; only api_within compares with the API")
        need = {"equals": ("expected|value_ref",), "matches": ("pattern",), "within_tol": ("expected|value_ref",),
                "changed_since_previous_step": ("since_step",), "unchanged_since_previous_step": ("since_step",),
                "count": ("expected|range",), "export_equals": ("export_column",), "api_within": ("api_field",)}
        for req in need.get(kind, ()):
            if not any(a.get(k) is not None for k in req.split("|")):
                raise OracleConfigError(f"{aid}: {kind} needs {req.replace('|', ' or ')}")
        if kind != "rule" and not a.get("region"):
            raise OracleConfigError(f"{aid}: {kind} needs a region (4.7)")
        if kind in _DEFAULT_FORMAT and a.get("format") not in (None, *NUMERIC_FORMATS):
            raise OracleConfigError(f"{aid}: {kind} compares numbers; format {a.get('format')!r} is not numeric")
        rng = a.get("range")
        try:
            bounds = (Decimal(str(rng["min"])), Decimal(str(rng["max"]))) if rng else None
            if kind == "count" and rng is None:
                Decimal(str(a.get("expected")))
        except (ArithmeticError, KeyError, TypeError) as exc:
            raise OracleConfigError(f"{aid}: count needs a number or a range {{min, max}}") from exc
        return cls(id=aid, kind=kind, source=source, region=a.get("region"), expected=a.get("expected"), range=bounds,
                   value_ref=a.get("value_ref"), pattern=a.get("pattern"), fmt=a.get("format"),
                   tol=Tolerance.parse(a["tol"]) if a.get("tol") is not None else None,
                   since_step=a.get("since_step"), export_column=a.get("export_column"), api_field=a.get("api_field"),
                   rule=RuleSpec.from_assertion(a) if kind == "rule" else None)

    @property
    def region_source(self) -> str:
        """The source the region is read from (`api` stands for the default on `api_within`)."""
        return "ocr" if self.source == "api" else self.source

    @property
    def uses_export(self) -> bool:
        return (self.region_source == "export" or self.kind == "export_equals"
                or (self.rule is not None and self.rule.rule_id == "R09"))


@dataclass(frozen=True)
class AssertionResult:
    """The Oracle's verdict on one assertion; `operands` are the extractions it compared."""

    assertion_id: str
    kind: str
    status: str
    expected: str | None = None
    extracted: str | None = None
    detail: str | None = None
    operands: Mapping[str, Extraction] = field(default_factory=dict)
    unreadable: tuple[str, ...] = ()
    reason: str | None = None                  # harness_error reason
    gate: str | None = None                    # env_error gate
    rule: RuleResult | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status {self.status!r}")

    @property
    def label(self) -> str | None:
        """2.8.3: "bracket check" on an unpinned R02 result."""
        return self.rule.label if self.rule else None

    @property
    def fallbacks(self) -> tuple[SourceFallback, ...]:
        return tuple(fb for ext in self.operands.values() if (fb := ext.fallback) is not None)

    def raw_values(self, base: "AssertionResult | None" = None) -> dict[str, str]:
        """`Finding.evidence.raw_values` (4.13): the reporter's `expected`, `read` and `source`, then
        `<build>.<field>.<source>` for every read on the candidate and, when given, the base."""
        out: dict[str, str] = {}
        if self.expected is not None:
            out["expected"] = self.expected
        if self.extracted is not None:
            out["read"] = self.extracted
        first = next(iter(self.operands.values()), None)
        out["source"] = (first.source or "none") if first else "none"
        if self.label:
            out["label"] = self.label
        for side, res in (("candidate", self), ("base", base)):
            for ext in (res.operands.values() if res else ()):
                for a in ext.attempts:
                    read = a.reading.describe() if a.reading else "nothing"
                    out[f"{side}.{ext.field}.{a.source}"] = read + (f"; rejected: {a.rejected}" if a.rejected else "")
                if ext.crop_ref:
                    out[f"{side}.{ext.field}.crop"] = ext.crop_ref
        return out


@dataclass(frozen=True)
class OracleVerdict:
    """The assertion results of one checkpoint, folded for classification (3.11 G and H)."""

    results: tuple[AssertionResult, ...]

    def _with(self, status: str) -> list[AssertionResult]:
        return [r for r in self.results if r.status == status]

    @property
    def failed(self) -> tuple[str, ...]:
        """Failed assertion ids in declared order; the first is the outcome reason `assertion:<id>`."""
        return tuple(r.assertion_id for r in self._with("failed"))

    @property
    def unreadable(self) -> tuple[str, ...]:
        """Fields unreadable on every source (`oracle_unreadable`, 2.8.1)."""
        return tuple(dict.fromkeys(f for r in self._with("unreadable") for f in r.unreadable))

    @property
    def harness_reason(self) -> str | None:
        return next((r.reason for r in self._with("harness_error")), None)

    @property
    def env_gate(self) -> str | None:
        return next((r.gate for r in self._with("env_error")), None)

    @property
    def detail(self) -> str | None:
        """Expected and extracted values of every failed assertion (4.13 `detail`)."""
        return "; ".join(f"{r.assertion_id}: {r.detail}" for r in self._with("failed")) or None

    @property
    def fallbacks(self) -> tuple[SourceFallback, ...]:
        return tuple(dict.fromkeys(fb for r in self.results for fb in r.fallbacks))

    def facts_fields(self, *, harness_event_id: str | None = None) -> dict[str, Any]:
        """Keyword arguments for `classify.Facts`: `assertion_ids`, `assertion_detail`,
        `oracle_unreadable`, and `env` or `harness` when an API or harness fault applies.
        `harness_event_id` (the Watchdog event, 4.13) is required when there is a harness fault."""
        out: dict[str, Any] = {"assertion_ids": self.failed, "assertion_detail": self.detail,
                               "oracle_unreadable": self.unreadable}
        if self.env_gate:
            api = next(r for r in self._with("env_error"))
            out["env"] = EnvFailure(gate=self.env_gate, reason="gate", detail=api.detail)
        if self.harness_reason:
            if not harness_event_id:
                raise ValueError("a harness_error outcome carries its Watchdog event id (4.13)")
            out["harness"] = HarnessFailure(self.harness_reason, harness_event_id)
        return out


def summarize(results: Sequence[AssertionResult]) -> OracleVerdict:
    return OracleVerdict(tuple(results))


def step_assertions(flow_step: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The assertions a step carries: `expect` (checkpoint), `assertions` (assert), `verify` (type_fixture)."""
    out = list(flow_step.get("expect") or []) + list(flow_step.get("assertions") or [])
    if flow_step.get("verify"):
        out.append(flow_step["verify"])
    return out


class _Stop(Exception):
    """Ends one assertion's evaluation with a non-pass, non-fail status."""

    def __init__(self, status: str, detail: str, *, unreadable: Sequence[str] = (), reason: str | None = None,
                 gate: str | None = None):
        super().__init__(detail)
        self.status, self.detail, self.unreadable = status, detail, tuple(unreadable)
        self.reason, self.gate = reason, gate


class _Fail(Exception):
    def __init__(self, expected: str, extracted: str, detail: str):
        super().__init__(detail)
        self.expected, self.extracted, self.detail = expected, extracted, detail


def _valid(fmt: str, text: str) -> bool:
    """`text` is a valid value of `fmt` (the format-only `present` and `absent`)."""
    try:
        parse(fmt, text)
    except FormatError:
        return False
    return True


def _palette_class(color: tuple[int, int, int], palette: Mapping[str, tuple[int, int, int]], tol: float) -> str | None:
    best, best_d = None, float("inf")
    for name, ref in palette.items():
        d = sum((a - b) ** 2 for a, b in zip(color, ref, strict=True)) ** 0.5
        if d < best_d:
            best, best_d = name, d
    return best if best_d <= tol else None


class _Evaluator:
    def __init__(self, sources: Mapping[str, Any], previous: Mapping[str, Mapping[str, Extraction]],
                 fixture: Mapping[str, Any], config: RuleConfig, context: EvalContext, ocr_calibrated: bool,
                 api_preflight_ok: bool):
        self.sources = {k: v for k, v in sources.items() if k != "api"}
        self.api: ApiReader | None = sources.get("api")
        self.previous, self.fixture, self.config, self.context = previous, fixture, config, context
        self.ocr_calibrated, self.api_preflight_ok = ocr_calibrated, api_preflight_ok
        self.api_lost: str | None = None
        self.operands: dict[str, Extraction] = {}

    # -- reads -------------------------------------------------------------------------------
    def export(self) -> ExportReader | None:
        src = self.sources.get("export")
        return src if isinstance(src, ExportReader) else None

    def guard(self, a: Assertion) -> None:
        if a.region_source == "ocr" and not self.ocr_calibrated:
            raise _Stop("harness_error", "the OCR calibration panel misread at canary_flow_passed (2.8.1)",
                        reason="ocr_calibration")
        exp = self.export()
        if a.uses_export and exp is not None and exp.kind == "file" and exp.produced and not exp.header_matches:
            raise _Stop("harness_error", "the export's header set differs from the declared set (2.8.1)",
                        reason="export_header_mismatch")

    def read(self, name: str, region: str, fmt: str, declared: str) -> Extraction:
        exclude = frozenset() if self.ocr_calibrated else frozenset({"ocr"})
        ext = extract(region, fmt, self.sources, declared, exclude=exclude)
        self.operands[name] = ext
        if not ext.readable and not self.ocr_calibrated and "ocr" in self.sources:
            raise _Stop("harness_error", f"{region} needed the OCR fallback after a calibration-panel miss (2.8.1)",
                        reason="ocr_calibration")
        return ext

    def readable(self) -> None:
        bad = [e.field for e in self.operands.values() if not e.readable]
        if bad:
            reads = " | ".join(f"{e.field}: {e.reads()}" for e in self.operands.values() if not e.readable)
            raise _Stop("unreadable", f"unreadable on every source: {reads}", unreadable=bad)

    def region(self, a: Assertion, fmt: str) -> Extraction:
        return self.read("region", a.region or "", fmt, a.region_source)

    def value(self, ext: Extraction) -> Any:
        try:
            return ext.value()
        except FormatError as exc:
            raise _Fail(f"a valid {ext.fmt}", repr(ext.text), f"{ext.field} reads {ext.text!r}, not a valid "
                                                               f"{ext.fmt}: {exc}") from exc

    def expected(self, a: Assertion) -> Any:
        return resolve_ref(self.fixture, a.value_ref) if a.value_ref else a.expected

    def want(self, a: Assertion, fmt: str, literal: Any) -> Any:
        try:
            return coerce(fmt, literal)
        except FormatError as exc:
            raise OracleConfigError(f"{a.id}: expected value {literal!r} is not a valid {fmt}: {exc}") from exc

    def tick(self, price: Decimal | None = None) -> Decimal | None:
        return tick_size(self.context.symbol, self.config, self.fixture, price) if self.context.symbol else None

    # -- kinds -------------------------------------------------------------------------------
    def equals(self, a: Assertion) -> tuple[str, str]:
        lit = self.expected(a)
        fmt = a.fmt or ("text" if isinstance(lit, str) else "date" if isinstance(lit, date) else "price")
        ext = self.region(a, fmt)
        self.readable()
        want, got = self.want(a, fmt, lit), self.value(ext)
        if fmt == "money":
            want, got = to_cent(want), to_cent(got)
        if want != got:
            raise _Fail(show(fmt, want), show(fmt, got), f"expected {show(fmt, want)}, extracted {show(fmt, got)} "
                                                         f"from {ext.field} by {ext.source}")
        return show(fmt, want), show(fmt, got)

    def matches(self, a: Assertion) -> tuple[str, str]:
        ext = self.region(a, a.fmt or "text")
        self.readable()
        text = normalize_text(ext.text or "")
        if not re.search(a.pattern or "", text):
            raise _Fail(f"/{a.pattern}/", repr(text), f"{ext.field} reads {text!r}, which does not match /{a.pattern}/")
        return f"/{a.pattern}/", repr(text)

    def within_tol(self, a: Assertion) -> tuple[str, str]:
        fmt = a.fmt or _DEFAULT_FORMAT["within_tol"]
        ext = self.region(a, fmt)
        self.readable()
        want, got = self.want(a, fmt, self.expected(a)), self.value(ext)
        tol = a.tol or default_tolerance(fmt, self.context.data_mode, self.tick(want) if fmt == "price" else None)
        if not tol.allows(got, want):
            raise _Fail(f"{show(fmt, want)} +/- {tol}", show(fmt, got),
                        f"{ext.field} reads {show(fmt, got)}, outside {show(fmt, want)} +/- {tol}")
        return f"{show(fmt, want)} +/- {tol}", show(fmt, got)

    def _present(self, a: Assertion) -> tuple[list[tuple[str, bool]], str, str]:
        """Each judged cell with whether `what` is present in it, `what`, and the region's text. A format-only
        check judges every non-empty row (choice); a pattern or a text check judges the whole text."""
        ext = self.region(a, a.fmt or "text")
        self.readable()
        text = normalize_text(ext.text or "")
        if a.pattern is not None:
            return [(text, bool(re.search(a.pattern, text)))], f"/{a.pattern}/", text
        if a.fmt is not None and a.fmt != "text":
            cells = [normalize_text(r) for r in rows(ext.text)] or [""]
            return [(c, _valid(a.fmt, c)) for c in cells], f"a {a.fmt} value", text
        return [(text, bool(text))], "text", text

    @staticmethod
    def _where(a: Assertion, cells: list[tuple[str, bool]], cell: str, text: str) -> str:
        return f"{a.region} reads {text!r}" if len(cells) == 1 else f"{a.region} row {cell!r} of {text!r}"

    def present(self, a: Assertion) -> tuple[str, str]:
        cells, what, text = self._present(a)
        miss = next((c for c, hit in cells if not hit), None)
        if miss is not None:
            raise _Fail(f"{what} present", repr(text if len(cells) == 1 else miss),
                        f"{self._where(a, cells, miss, text)}: {what} is not present")
        return f"{what} present", repr(text)

    def absent(self, a: Assertion) -> tuple[str, str]:
        cells, what, text = self._present(a)
        hit = next((c for c, h in cells if h), None)
        if hit is not None:
            raise _Fail(f"{what} absent", repr(text if len(cells) == 1 else hit),
                        f"{self._where(a, cells, hit, text)}: {what} is present")
        return f"{what} absent", repr(text)

    def _compare_previous(self, a: Assertion) -> tuple[bool, str, str]:
        fmt = a.fmt or "text"
        ext = self.region(a, fmt)
        stored = (self.previous.get(a.since_step or "") or {}).get(a.region or "")
        if stored is None:
            raise OracleConfigError(f"{a.id}: no extraction of {a.region} stored at step {a.since_step}")
        self.operands["previous"] = replace(stored, field=f"{a.since_step}:{a.region}")
        self.readable()
        try:
            before, now = parse(fmt, stored.text), parse(fmt, ext.text)
        except FormatError:
            before, now = normalize_text(stored.text or ""), normalize_text(ext.text or "")
        return before != now, repr(stored.text), repr(ext.text)

    def changed_since_previous_step(self, a: Assertion) -> tuple[str, str]:
        changed, before, now = self._compare_previous(a)
        if not changed:
            raise _Fail(f"a change from {before} at {a.since_step}", now,
                        f"{a.region} still reads {now}, as at {a.since_step}")
        return f"changed from {before}", now

    def unchanged_since_previous_step(self, a: Assertion) -> tuple[str, str]:
        changed, before, now = self._compare_previous(a)
        if changed:
            raise _Fail(f"{before} as at {a.since_step}", now,
                        f"{a.region} reads {now}; it read {before} at {a.since_step}")
        return f"{before} as at {a.since_step}", now

    def count(self, a: Assertion) -> tuple[str, str]:
        ext = self.region(a, "text")
        self.readable()
        n = len(ext.rows())
        if a.range is not None:
            lo, hi = a.range
            want, ok = f"{lo:f} to {hi:f} rows", lo <= n <= hi
        else:
            want_n = Decimal(str(a.expected))
            want, ok = f"{want_n:f} rows", n == want_n
        if not ok:
            raise _Fail(want, f"{n} rows", f"{ext.field} holds {n} rows; expected {want}")
        return want, f"{n} rows"

    def export_equals(self, a: Assertion) -> tuple[str, str]:
        fmt = a.fmt or _DEFAULT_FORMAT["export_equals"]
        exp = self.export()
        if exp is None:
            raise OracleConfigError(f"{a.id}: export_equals needs the checkpoint's export source")
        ext = self.region(a, fmt)
        column = exp.column(a.export_column or "")
        label = f"export:{a.export_column}"
        if column is None and exp.produced and exp.header_matches:
            raise OracleConfigError(f"{a.id}: column {a.export_column!r} is not in the declared export header")
        why = (None if column is not None else "nothing exported within the step timeout" if not exp.produced
               else "Copy All header differs from the declared set")
        reading = Reading(label, "export", "\n".join(column), EXACT_CONFIDENCE) if column is not None else None
        self.operands["export"] = Extraction(label, fmt, "export", (Attempt("export", reading, why),), reading)
        self.readable()
        screen: Decimal = self.value(ext)
        cells = [c for c in column or () if c.strip()]
        values: list[Decimal] = []
        for i, c in enumerate(cells):
            try:
                values.append(number(fmt, c))
            except FormatError as exc:
                raise _Fail(f"a valid {fmt}", repr(c),
                            f"{label} cell {i + 1} {c!r} is not a valid {fmt}: {exc}") from exc
        export_total = sum(values, Decimal(0))
        tol = a.tol or default_tolerance(fmt, self.context.data_mode, self.tick(screen) if fmt == "price" else None)
        if fmt == "money":
            export_total, screen = to_cent(export_total), to_cent(screen)
        if not tol.allows(export_total, screen):
            raise _Fail(show(fmt, screen), show(fmt, export_total), f"export column {a.export_column} totals "
                        f"{show(fmt, export_total)}; {ext.field} shows {show(fmt, screen)} (tolerance {tol})")
        return show(fmt, screen), show(fmt, export_total)

    def api_gate(self, a: Assertion) -> None:
        """2.8.1 Market-data API: a failed preflight N3, or an API already lost in this run, is an
        environment fault of the cross-check, ahead of any harness fault (1.10 precedence)."""
        if not self.api_preflight_ok:
            raise _Stop("env_error", "market-data API preflight N3 failed", gate="preflight.N3")
        if self.api_lost:
            raise _Stop("env_error", f"market-data API lost mid-run: {self.api_lost}", gate="connectivity")
        if self.api is None:
            raise OracleConfigError(f"{a.id}: api_within needs the market-data API source")

    def api_within(self, a: Assertion) -> tuple[str, str]:
        assert self.api is not None
        fmt = a.fmt or _DEFAULT_FORMAT["api_within"]
        ext = self.region(a, fmt)
        cmp = api_comparison(self.context, self.fixture)
        symbol = self.context.need_symbol("an API cross-check")
        try:
            reading = self.api.quote(symbol, a.api_field or "", cmp.as_of)
        except ApiUnavailable as exc:
            self.api_lost = str(exc)
            raise _Stop("env_error", f"market-data API failed mid-run: {exc}", gate="connectivity") from exc
        label = f"api:{a.api_field}"
        why = problem(reading, fmt)
        api_value: Decimal | None = None
        if why is None:
            try:
                api_value = number(fmt, reading.text)          # type: ignore[union-attr]
            except FormatError as exc:
                why = f"API value is not a valid {fmt}: {exc}"
        api_reading = replace(reading, field=label) if reading is not None else None
        self.operands["api"] = Extraction(label, fmt, "api", (Attempt("api", api_reading, why),),
                                          api_reading if why is None else None)
        self.readable()
        assert api_value is not None
        screen: Decimal = self.value(ext)
        needs_tick = cmp.mode == "tick" and a.tol is None
        tol = cmp.tolerance(a.tol, self.tick(api_value) if needs_tick else None)
        want = f"{show(fmt, api_value)} +/- {tol} (API {a.api_field} as of {cmp.as_of.isoformat()})"
        if not tol.allows(screen, api_value):
            raise _Fail(want, show(fmt, screen), f"{ext.field} reads {show(fmt, screen)}; API {a.api_field} as of "
                        f"{cmp.as_of.isoformat()} is {show(fmt, api_value)}; {cmp.why} (tolerance {tol})")
        return want, show(fmt, screen)

    def sign_color_agrees(self, a: Assertion) -> tuple[str, str]:
        if not self.config.palette:
            raise OracleConfigError(f"{a.id}: sign_color_agrees needs the pinned palette (RuleConfig.palette)")
        fmt = a.fmt or _DEFAULT_FORMAT["sign_color_agrees"]
        ext = self.region(a, fmt)
        self.readable()
        color = ext.accepted.color if ext.accepted else None
        if color is None:
            raise _Stop("unreadable", f"{ext.field}: no text color read by {ext.source}",
                        unreadable=[f"{ext.field}:color"])
        value = self.value(ext)
        expected = "positive" if value > 0 else "negative" if value < 0 else "neutral"
        if expected == "neutral" and "neutral" not in self.config.palette:
            return "neutral (not judged)", f"{ext.text!r}"
        drawn = _palette_class(color, self.config.palette, self.config.palette_tolerance)
        if drawn != expected:
            raise _Fail(f"{expected} color", f"{drawn or 'no palette color'} {color}",
                        f"{ext.field} reads {ext.text!r} ({expected}) drawn in {drawn or 'no palette color'} {color}")
        return f"{expected} color", f"{drawn} {color}"

    def rule(self, a: Assertion) -> RuleResult:
        spec = a.rule
        assert spec is not None
        operands: dict[str, Extraction | Decimal] = {}
        for name, ref in spec.inputs.items():
            if isinstance(ref, str):
                source = spec.operand_source(name, a.region_source)
                operands[name] = self.read(name, ref, spec.operand_format(name), source)
            else:
                operands[name] = ref
        return evaluate_rule(spec, operands, self.fixture, self.config, self.context)

    # -- one assertion -----------------------------------------------------------------------
    def evaluate(self, a: Assertion) -> AssertionResult:
        self.operands = {}
        try:
            if a.kind == "api_within":
                self.api_gate(a)
            self.guard(a)
            if a.kind == "rule":
                r = self.rule(a)
                if r.status == "unreadable":
                    self.readable()
                return AssertionResult(a.id, a.kind, r.status, r.expected, r.extracted, r.detail, dict(self.operands),
                                       rule=r)
            expected, extracted = getattr(self, a.kind)(a)
            return AssertionResult(a.id, a.kind, "passed", expected, extracted, None, dict(self.operands))
        except _Fail as f:
            return AssertionResult(a.id, a.kind, "failed", f.expected, f.extracted, f.detail, dict(self.operands))
        except _Stop as s:
            return AssertionResult(a.id, a.kind, s.status, detail=s.detail, operands=dict(self.operands),
                                   unreadable=s.unreadable, reason=s.reason, gate=s.gate)


def evaluate_assertions(flow_step: Mapping[str, Any], values_by_source: Mapping[str, ValueSource | ApiReader],
                        previous_values: Mapping[str, Mapping[str, Extraction]], fixture: Mapping[str, Any],
                        rules: RuleConfig = DEFAULT_RULES, *, context: EvalContext = NO_CONTEXT,
                        ocr_calibrated: bool = True, api_preflight_ok: bool = True) -> list[AssertionResult]:
    """Evaluate every assertion of `flow_step` on the capture's reads.

    `values_by_source` holds the sources the checkpoint declares, keyed `ocr`, `jab`, `export`
    (an `ExportReader` for export assertions) and `api` (an `ApiReader`); `previous_values` maps an
    earlier checkpoint's step id to its extractions by region; `fixture` is the parsed fixture
    file (4.9); `rules` the rule tables. `context.data_mode` defaults to the step's `data_mode`.
    `ocr_calibrated` is False after a calibration-panel miss at `canary_flow_passed`;
    `api_preflight_ok` is False when preflight N3 failed.
    """
    if context.data_mode is None and flow_step.get("data_mode"):
        context = replace(context, data_mode=flow_step["data_mode"])
    ev = _Evaluator(values_by_source, previous_values, fixture, rules, context, ocr_calibrated, api_preflight_ok)
    return [ev.evaluate(Assertion.from_dict(a)) for a in step_assertions(flow_step)]
