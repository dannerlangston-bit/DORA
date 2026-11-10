"""The arithmetic invariants R01 to R10 (2.8.3), the operand of the `rule` assertion kind (2.6.2).

`RuleSpec.from_assertion(a)` reads a 4.7 `rule` assertion (`rule_id`, `inputs`, `tol`, and for R10
`format`); its operand names are the lint's (`sentinel.lint.rules.RULE_OPERANDS`), so a flow the
lint accepts is a flow the Oracle can evaluate. `evaluate_rule(spec, operands, ...)` decides one
rule over extracted operands; `check_rules(values, fixture, rules)` re-runs rules offline over
stored extractions (2.8.1: "so rules re-run offline").

A rule result is `passed`, `failed` (2.8.3: `assertion_failed`, with field, expected and extracted)
or `unreadable` (an operand no source read above its floor: `needs_human (reason:
oracle_unreadable)`, 2.8.1). A displayed value that reads cleanly but is not a valid number of its
type (`NaN`, `--`, a misplaced separator) fails the rule: that is a product defect, not a read
problem. A rule fires even when base and candidate are pixel-identical (2.8.3).

Operand types: quantities are integers, prices and Greeks decimals, costs, P/L, buying power and
totals money, expirations dates, DTE an integer that may sit in the chain's parentheses (`(10)`),
R04's count and selector text, R10's value the assertion's `format`.

Choices where the spec is ambiguous (reported):

- Multi-row operands. A region may hold several rows (legs, chain rows, positions); row i of each
  operand belongs together. A scalar operand must read exactly one row.
- The multiplier operand of R01 and R08 is a literal (the 4.7 examples) or a region whose rows are
  the instruments' symbols; each row's multiplier then comes from the 2.8.3 table
  (`tables.multiplier_for`), so a Position Statement mixing stock, /MES and options checks every
  row with its own multiplier.
- R01 "per leg and total": the cost region holds the total (one row), one row per leg, or one row
  per leg plus the total last. Magnitudes are compared: the dialog's debit or credit sign is a
  display convention, which the side assertion and R02 cover.
- R02's opening or closing side, quantity (futures), asset class and account kind come from the
  evaluation context and the fixture, since the operands are only `bp_effect` and `notional`. A
  zero buying power effect has neither sign and fails.
- R03: `last`, `low` and `high` are given together or not at all.
- R04: the count is the number of rows of the `count` region. A selector of `All` expects the
  context's `all_strikes`. A selector whose rows all read as dates is the chain header's
  expiration list, and the rule compares the `count` region's rows with it as a list.
- R05: monotone means non-increasing or non-decreasing across the rows, within the 2.8.2 delta
  tolerance of 0.01; |delta| <= 1, gamma and vega >= 0 exactly; theta <= 0 when the context says
  the Greeks are a long option's.
- R06: European parity C - P = S e^(-qT) - K e^(-rT) within tolerance, for roots in
  `RuleConfig.european_style` (SPX); American bounds S - D - K <= C - P <= S - K e^(-rT). T is
  DTE / 365 (calendar days, 2.8.2); a literal `rate` is a fraction (0.045), a region `rate` a
  percent; the default tolerance is 0.01 plus one tick per option leg.
- R07: the capture date is the capture time's America/New_York date (2.8.2); a naive capture time
  is taken as New York time.
- R08: the P/L Open region holds one row per position, optionally followed by the total row,
  which must equal the sum of the displayed rows within the money tolerance plus half a cent per
  row (displayed rows are each rounded to the cent).
- R09: `export_total` is read from the export source (the region's cell in the export) and
  `screen_total` from the assertion's source.
- Money comparisons round both sides to the cent first (2.8.2); the default money tolerance is
  `tables.money_tolerance` for the data mode, with one leg per row.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..lint.rules import RULE_OPERANDS
from .sources import Attempt, Extraction, literal, problem
from .tables import (CENT, EvalContext, OracleConfigError, RuleConfig, Tolerance, money_tolerance, multiplier_for,
                     symbol_root, tick_size)
from .values import FORMATS, FormatError, Value, format_error, number, parse, show, to_cent

RULE_IDS: tuple[str, ...] = tuple(f"R{n:02d}" for n in range(1, 11))
DEFAULT_RULES = RuleConfig()
NO_CONTEXT = EvalContext()
NEW_YORK = ZoneInfo("America/New_York")
DELTA_TOLERANCE = CENT                                  # 2.8.2 Greeks: delta 0.01
HALF_CENT = Decimal("0.005")
_FORMATS: dict[str, dict[str, str]] = {
    "R01": {"qty": "int", "price": "price", "multiplier": "text", "total": "money"},
    "R02": {"bp_effect": "money", "notional": "money"},
    "R03": {"bid": "price", "mark": "price", "ask": "price", "last": "price", "low": "price", "high": "price"},
    "R04": {"count": "text", "selector": "text"},
    "R05": {"delta": "price", "gamma": "price", "vega": "price", "theta": "price"},
    "R06": {"call": "price", "put": "price", "strike": "price", "underlying": "price", "rate": "percent", "dte": "int"},
    "R07": {"dte": "int", "expiration": "date"},
    "R08": {"pl_open": "money", "mark": "price", "trade_price": "price", "qty": "int", "multiplier": "text"},
    "R09": {"export_total": "money", "screen_total": "money"},
    "R10": {},
}

Operand = Extraction | Decimal


@dataclass(frozen=True)
class RuleSpec:
    """A `rule` assertion: its id, the rule, the operands (region id or literal) and the tolerance."""

    assertion_id: str
    rule_id: str
    inputs: Mapping[str, str | Decimal]
    tol: Tolerance | None = None
    fmt: str | None = None

    def __post_init__(self) -> None:
        if self.rule_id not in RULE_IDS:
            raise OracleConfigError(f"rule_id {self.rule_id!r} is not one of R01 to R10 (2.8.3)")
        req, opt = RULE_OPERANDS[self.rule_id]
        if not req <= set(self.inputs) <= req | opt:
            raise OracleConfigError(f"{self.assertion_id}: inputs of {self.rule_id} must be {sorted(req)}"
                                    + (f" plus optional {sorted(opt)}" if opt else "") + " (L-FLW-11)")
        if self.rule_id == "R10" and self.fmt not in FORMATS:
            raise OracleConfigError(f"{self.assertion_id}: R10 needs the field's format")

    @classmethod
    def from_assertion(cls, a: Mapping[str, Any]) -> "RuleSpec":
        inputs: dict[str, str | Decimal] = {}
        for name, v in (a.get("inputs") or {}).items():
            if isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal)):
                raise OracleConfigError(f"{a.get('id')}: operand {name} is neither a region nor a number")
            inputs[str(name)] = v if isinstance(v, str) else Decimal(str(v))
        tol = Tolerance.parse(a["tol"]) if a.get("tol") is not None else None
        return cls(str(a.get("id")), str(a.get("rule_id")), inputs, tol, a.get("format"))

    def operand_format(self, name: str) -> str:
        return self.fmt if self.rule_id == "R10" else _FORMATS[self.rule_id][name]   # type: ignore[return-value]

    def operand_source(self, name: str, declared: str) -> str:
        """R09's export total is read from the export; every other operand from the declared source."""
        return "export" if (self.rule_id, name) == ("R09", "export_total") else declared


@dataclass(frozen=True)
class RuleResult:
    """One rule's verdict: `passed`, `failed` or `unreadable`; `label` is "bracket check" for an
    unpinned R02 (2.8.3)."""

    assertion_id: str
    rule_id: str
    status: str
    expected: str | None = None
    extracted: str | None = None
    detail: str | None = None
    fields: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    label: str | None = None


class _Fail(Exception):
    def __init__(self, expected: str, extracted: str, detail: str):
        super().__init__(detail)
        self.expected, self.extracted, self.detail = expected, extracted, detail


@dataclass
class _Ops:
    """The operands of one rule evaluation, parsed on demand."""

    spec: RuleSpec
    operands: Mapping[str, Operand]
    context: EvalContext
    config: RuleConfig
    fixture: Mapping[str, Any]

    def has(self, name: str) -> bool:
        return name in self.operands

    def label(self, name: str) -> str:
        op = self.operands[name]
        return op.field if isinstance(op, Extraction) else f"{name}={op.normalize():f}"

    def texts(self, name: str) -> list[str]:
        op = self.operands[name]
        return op.rows() if isinstance(op, Extraction) else [f"{op:f}"]

    def rows(self, name: str, fmt: str | None = None) -> list[Value]:
        op = self.operands[name]
        if not isinstance(op, Extraction):
            return [op]
        fmt = fmt or self.spec.operand_format(name)
        out = []
        for text in op.rows() or [""]:
            try:
                out.append(parse(fmt, text))
            except FormatError as exc:
                raise _Fail(f"a valid {fmt}", repr(text), f"{op.field} {text!r} is not a valid {fmt}: {exc}") from exc
        return out

    def numbers(self, name: str) -> list[Decimal]:
        return [v for v in self.rows(name) if isinstance(v, Decimal)]

    def scalar(self, name: str) -> Decimal:
        values = self.numbers(name)
        if len(values) != 1:
            raise _Fail("one value", f"{len(values)} rows", f"{self.label(name)} holds {len(values)} rows, not one")
        return values[0]

    def multipliers(self, name: str, n: int) -> list[Decimal]:
        """A literal multiplier for every row, or each row's instrument multiplier (2.8.3 table)."""
        op = self.operands[name]
        if not isinstance(op, Extraction):
            return [op] * n
        symbols = op.rows()
        if len(symbols) != n:
            raise _Fail(f"{n} instruments", f"{len(symbols)}",
                        f"{op.field} holds {len(symbols)} instruments for {n} rows")
        return [multiplier_for(s, self.config, self.fixture) for s in symbols]

    def ticks(self, name: str, prices: Sequence[Decimal]) -> list[Decimal]:
        """One price tick per row: each row's instrument when the multiplier names them, else the context symbol."""
        op = self.operands.get(name)
        if isinstance(op, Extraction):
            return [tick_size(s, self.config, self.fixture, p) for s, p in zip(op.rows(), prices, strict=True)]
        symbol = self.context.need_symbol("a masked_live money tolerance")
        return [tick_size(symbol, self.config, self.fixture, p) for p in prices]

    @property
    def live(self) -> bool:
        """The money tolerance depends on the legs' ticks: `masked_live` and no `tol` given."""
        return self.context.data_mode == "masked_live" and self.spec.tol is None

    def money_tol(self, legs: Sequence[tuple[Decimal, Decimal, Decimal]] = ()) -> Tolerance:
        """The assertion's `tol`, else 2.8.2 money: 0 in frozen, 0.01 plus one tick per leg (quantity,
        tick, multiplier) in masked_live."""
        if self.spec.tol is not None:
            return self.spec.tol
        return Tolerance(money_tolerance(self.context.data_mode, legs if self.live else ()))


def _money_ok(observed: Decimal, expected: Decimal, tol: Tolerance) -> bool:
    return tol.allows(to_cent(observed), to_cent(expected))


def _m(v: Decimal) -> str:
    return show("money", v)


def _n(v: Decimal) -> str:
    return f"{v.normalize():f}"


def _r01(o: _Ops) -> tuple[str, str]:
    q, p = o.numbers("qty"), o.numbers("price")
    if len(q) != len(p):
        raise _Fail(f"{len(q)} price rows", f"{len(p)}", f"{len(q)} quantity rows and {len(p)} price rows")
    mult = o.multipliers("multiplier", len(q))
    legs = [qi * pi * mi for qi, pi, mi in zip(q, p, mult, strict=True)]
    net = sum(legs, Decimal(0))
    costs = o.numbers("total")
    tol = o.money_tol(list(zip(q, o.ticks("multiplier", p), mult, strict=True)) if o.live else ())
    if len(costs) == 1:
        pairs = [("total", net, costs[0])]
    elif len(costs) in (len(legs), len(legs) + 1):
        pairs = [(f"leg {i + 1}", leg, c) for i, (leg, c) in enumerate(zip(legs, costs[:len(legs)], strict=True))]
        if len(costs) == len(legs) + 1:
            pairs.append(("total", net, costs[-1]))
    else:
        raise _Fail(f"1, {len(legs)} or {len(legs) + 1} cost rows", f"{len(costs)}",
                    f"{o.label('total')} holds {len(costs)} rows for {len(legs)} legs")
    formula = " + ".join(f"{_n(qi)} x {_n(pi)} x {_n(mi)}" for qi, pi, mi in zip(q, p, mult, strict=True))
    for what, want, got in pairs:
        if not _money_ok(abs(got), abs(want), tol):
            raise _Fail(_m(abs(want)), _m(abs(got)), f"{what}: {formula} = {_m(abs(want))}, but {o.label('total')} "
                                                     f"shows {_m(abs(got))} (tolerance {tol})")
    return _m(abs(net)), _m(abs(costs[-1]))


def _r02(o: _Ops) -> tuple[str, str]:
    bp, notional = o.scalar("bp_effect"), o.scalar("notional")
    if o.context.opening is None:
        raise OracleConfigError("R02 needs the order's opening or closing side in the evaluation context")
    asset, account = o.context.asset_of(o.fixture), o.context.account_of(o.fixture)
    bracket = o.config.margin.bracket(asset, account)
    if bracket.of == "notional":
        base, basis = abs(notional), f"notional {_m(abs(notional))}"
    else:
        root = symbol_root(o.context.need_symbol("R02 on futures"))
        if root not in o.config.margin.initial_margin:
            raise OracleConfigError(f"margin_rules.yaml has no initial margin for {root} (R02)")
        if o.context.quantity is None:
            raise OracleConfigError("R02 on futures needs the order quantity in the evaluation context")
        base = o.config.margin.initial_margin[root] * abs(o.context.quantity)
        basis = f"initial margin {_m(o.config.margin.initial_margin[root])} x {_n(abs(o.context.quantity))}"
    lo, hi = to_cent(bracket.low * base), to_cent(bracket.high * base)
    side, sign = ("opening", "negative") if o.context.opening else ("closing", "positive")
    expected = f"{sign}, magnitude {_m(lo)} to {_m(hi)}"
    if (bp < 0) != o.context.opening or bp == 0:
        raise _Fail(expected, _m(bp), f"buying power effect {_m(bp)} is not {sign} for an {side} order")
    tol = o.money_tol()
    magnitude = to_cent(abs(bp))
    if not lo - tol.bound(lo) <= magnitude <= hi + tol.bound(hi):
        pct = f"{_n(bracket.low * 100)}% to {_n(bracket.high * 100)}%"
        raise _Fail(expected, _m(bp), f"buying power effect {_m(bp)} is outside the {asset} {account} bracket "
                                      f"{_m(lo)} to {_m(hi)} ({pct} of {basis}, tolerance {tol})")
    return expected, _m(bp)


def _r03(o: _Ops) -> tuple[str, str]:
    bid, mark, ask = o.numbers("bid"), o.numbers("mark"), o.numbers("ask")
    if not len(bid) == len(mark) == len(ask):
        raise _Fail("equal row counts", f"{len(bid)}/{len(mark)}/{len(ask)}", "bid, mark and ask row counts differ")
    for i, (b, m, a) in enumerate(zip(bid, mark, ask, strict=True)):
        if not b <= m <= a:
            raise _Fail("bid <= mark <= ask", f"{_n(b)} / {_n(m)} / {_n(a)}",
                        f"row {i + 1}: bid {_n(b)}, mark {_n(m)}, ask {_n(a)} are out of order")
    session = [o.has(n) for n in ("last", "low", "high")]
    if any(session) and not all(session):
        raise OracleConfigError(f"{o.spec.assertion_id}: R03's last, low and high go together")
    if all(session):
        last, low, high = o.numbers("last"), o.numbers("low"), o.numbers("high")
        if not len(last) == len(low) == len(high) == len(bid):
            raise _Fail("equal row counts", f"{len(last)}/{len(low)}/{len(high)}",
                        "last, low and high row counts differ")
        for i, (la, lo, hi) in enumerate(zip(last, low, high, strict=True)):
            if not lo <= la <= hi:
                raise _Fail(f"{_n(lo)} <= last <= {_n(hi)}", _n(la),
                            f"row {i + 1}: last {_n(la)} is outside the session range {_n(lo)} to {_n(hi)}")
    return "bid <= mark <= ask", f"{len(bid)} rows in order"


def _r04(o: _Ops) -> tuple[str, str]:
    selector, count_rows = o.texts("selector"), o.texts("count")
    try:
        header = [parse("date", t) for t in selector] if selector else None
    except FormatError:
        header = None
    if header:
        listed = o.rows("count", "date")
        if listed != header:
            raise _Fail(", ".join(show("date", d) for d in header), ", ".join(show("date", d) for d in listed),
                        f"{o.label('count')} expirations differ from the header list {o.label('selector')}")
        return f"{len(header)} expirations", f"{len(listed)} expirations"
    if len(selector) != 1:
        raise _Fail("one selector value", f"{len(selector)} rows", f"{o.label('selector')} holds {len(selector)} rows")
    if selector[0].strip().casefold() == "all":
        if o.context.all_strikes is None:
            raise OracleConfigError("R04 with Strikes = All needs the chain's strike count (EvalContext.all_strikes)")
        want, said = o.context.all_strikes, "All"
    else:
        want, said = _int(selector[0], o.label("selector")), selector[0]
    if len(count_rows) != want:
        raise _Fail(f"{want} strikes", f"{len(count_rows)} strikes",
                    f"{o.label('count')} shows {len(count_rows)} strike rows; the Strikes selector says {said}")
    return f"{want} strikes", f"{len(count_rows)} strikes"


def _int(text: str, where: str) -> int:
    try:
        return int(number("int", text))
    except FormatError as exc:
        raise _Fail("an integer", repr(text), f"{where} {text!r} is not a valid int: {exc}") from exc


def _monotone(values: Sequence[Decimal], tol: Decimal) -> bool:
    pairs = list(zip(values, values[1:], strict=False))
    return all(b <= a + tol for a, b in pairs) or all(b >= a - tol for a, b in pairs)


def _r05(o: _Ops) -> tuple[str, str]:
    delta = o.numbers("delta")
    for i, d in enumerate(delta):
        if abs(d) > 1:
            raise _Fail("|delta| <= 1", _n(d), f"{o.label('delta')} row {i + 1}: |delta| {_n(d)} exceeds 1")
    if not _monotone(delta, DELTA_TOLERANCE):
        raise _Fail("monotone across strikes", ", ".join(_n(d) for d in delta),
                    f"{o.label('delta')} is not monotone across strikes (tolerance {_n(DELTA_TOLERANCE)})")
    for name in ("gamma", "vega"):
        if o.has(name):
            for i, v in enumerate(o.numbers(name)):
                if v < 0:
                    raise _Fail(f"{name} >= 0", _n(v), f"{o.label(name)} row {i + 1}: {name} {_n(v)} is negative")
    if o.has("theta") and o.context.long_options:
        for i, v in enumerate(o.numbers("theta")):
            if v > 0:
                raise _Fail("theta <= 0", _n(v),
                            f"{o.label('theta')} row {i + 1}: theta {_n(v)} is positive for a long option")
    return "Greeks within bounds", f"{len(delta)} rows"


def _exp(x: Decimal) -> Decimal:
    return Decimal(repr(math.exp(float(x))))


def _r06(o: _Ops) -> tuple[str, str]:
    c, p, k, s = (o.scalar(n) for n in ("call", "put", "strike", "underlying"))
    if not o.has("rate"):
        rate = o.config.risk_free_rate
    else:
        rate = o.scalar("rate") / (100 if isinstance(o.operands["rate"], Extraction) else 1)
    dte = o.scalar("dte") if o.has("dte") else Decimal(0)
    t = dte / 365
    symbol = o.context.need_symbol("R06")
    root = symbol_root(symbol)
    tol = o.spec.tol or Tolerance(CENT + 2 * tick_size(symbol, o.config, o.fixture))
    lhs = c - p
    if root in o.config.european_style:
        rhs = s * _exp(-o.config.dividend_yield.get(root, Decimal(0)) * t) - k * _exp(-rate * t)
        if not tol.allows(lhs, rhs):
            raise _Fail(f"C - P = {_n(rhs.quantize(CENT))}", _n(lhs),
                        f"put-call parity ({root}, European): C - P = {_n(lhs)}, S e^(-qT) - K e^(-rT) = "
                        f"{_n(rhs.quantize(CENT))} (tolerance {tol})")
        return f"C - P = {_n(rhs.quantize(CENT))}", _n(lhs)
    lower = s - o.config.dividends_pv.get(root, Decimal(0)) - k
    upper = s - k * _exp(-rate * t)
    if not lower - tol.bound(lower) <= lhs <= upper + tol.bound(upper):
        raise _Fail(f"{_n(lower.quantize(CENT))} <= C - P <= {_n(upper.quantize(CENT))}", _n(lhs),
                    f"American bounds ({root}): C - P = {_n(lhs)} outside {_n(lower.quantize(CENT))} to "
                    f"{_n(upper.quantize(CENT))} (tolerance {tol})")
    return f"{_n(lower.quantize(CENT))} <= C - P <= {_n(upper.quantize(CENT))}", _n(lhs)


def capture_date(context: EvalContext) -> date:
    """The rig's America/New_York date at capture (2.8.2 DTE convention)."""
    t = context.need_capture_time("R07")
    return t.astimezone(NEW_YORK).date() if t.tzinfo else t.date()


def _r07(o: _Ops) -> tuple[str, str]:
    today = capture_date(o.context)
    dtes = [_int(t.strip("()"), o.label("dte")) for t in o.texts("dte")]
    exps = [d for d in o.rows("expiration") if isinstance(d, date)]
    if len(dtes) != len(exps):
        raise _Fail(f"{len(exps)} DTE rows", f"{len(dtes)}", f"{len(dtes)} DTE rows and {len(exps)} expiration rows")
    for dte, exp in zip(dtes, exps, strict=True):
        want = (exp - today).days
        if dte != want:
            raise _Fail(str(want), str(dte), f"DTE {dte} for expiration {exp.isoformat()}; {exp.isoformat()} minus "
                                             f"capture date {today.isoformat()} is {want} calendar days")
    return f"{len(exps)} DTE values", "match"


def _r08(o: _Ops) -> tuple[str, str]:
    mark, trade, qty = o.numbers("mark"), o.numbers("trade_price"), o.numbers("qty")
    if not len(mark) == len(trade) == len(qty):
        raise _Fail("equal row counts", f"{len(mark)}/{len(trade)}/{len(qty)}",
                    "mark, trade price and qty row counts differ")
    n = len(mark)
    mult = o.multipliers("multiplier", n)
    pl = o.numbers("pl_open")
    if len(pl) not in (n, n + 1):
        raise _Fail(f"{n} or {n + 1} P/L rows", f"{len(pl)}",
                    f"{o.label('pl_open')} holds {len(pl)} rows for {n} positions")
    ticks = o.ticks("multiplier", mark) if o.live else [Decimal(0)] * n
    for i in range(n):
        want = (mark[i] - trade[i]) * qty[i] * mult[i]
        tol = o.money_tol([(qty[i], ticks[i], mult[i])])
        if not _money_ok(pl[i], want, tol):
            raise _Fail(_m(want), _m(pl[i]), f"row {i + 1}: ({_n(mark[i])} - {_n(trade[i])}) x {_n(qty[i])} x "
                                             f"{_n(mult[i])} = {_m(want)}, but P/L Open shows {_m(pl[i])} "
                                             f"(tolerance {tol})")
    if len(pl) == n + 1:
        rows_sum = sum(pl[:n], Decimal(0))
        tol = Tolerance(o.money_tol().bound(rows_sum) + HALF_CENT * n)
        if not tol.allows(to_cent(pl[n]), to_cent(rows_sum)):
            raise _Fail(_m(rows_sum), _m(pl[n]), f"total P/L Open {_m(pl[n])} is not the sum of the rows "
                                                 f"{_m(rows_sum)} (tolerance {tol})")
    return f"{n} rows", "match"


def _r09(o: _Ops) -> tuple[str, str]:
    export, screen = o.scalar("export_total"), o.scalar("screen_total")
    tol = o.money_tol()
    if not _money_ok(export, screen, tol):
        raise _Fail(_m(screen), _m(export), f"export total {_m(export)} differs from the on-screen total {_m(screen)} "
                                            f"(tolerance {tol})")
    return _m(screen), _m(export)


def _r10(o: _Ops) -> tuple[str, str]:
    fmt = o.spec.operand_format("value")
    texts = o.texts("value")
    for text in texts or [""]:
        why = format_error(fmt, text)
        if why is not None:
            raise _Fail(f"a valid {fmt}", repr(text), f"{o.label('value')} {text!r} is not a valid {fmt}: {why}")
    return f"valid {fmt}", f"{len(texts)} values"


_RULES: dict[str, Callable[[_Ops], tuple[str, str]]] = {
    "R01": _r01, "R02": _r02, "R03": _r03, "R04": _r04, "R05": _r05,
    "R06": _r06, "R07": _r07, "R08": _r08, "R09": _r09, "R10": _r10,
}


def evaluate_rule(spec: RuleSpec, operands: Mapping[str, Operand], fixture: Mapping[str, Any],
                  config: RuleConfig, context: EvalContext) -> RuleResult:
    """Decide one rule over its operands (extractions for regions, Decimals for literals)."""
    missing = set(spec.inputs) - set(operands)
    if missing:
        raise OracleConfigError(f"{spec.assertion_id}: operands {sorted(missing)} were not supplied")
    fields = tuple(op.field for op in operands.values() if isinstance(op, Extraction))
    unreadable = tuple(op.field for op in operands.values() if isinstance(op, Extraction) and not op.readable)
    label = config.margin.label if spec.rule_id == "R02" else None
    if unreadable:
        return RuleResult(spec.assertion_id, spec.rule_id, "unreadable", fields=fields, unreadable=unreadable,
                          label=label, detail=f"unreadable on every source: {', '.join(unreadable)}")
    try:
        expected, extracted = _RULES[spec.rule_id](_Ops(spec, operands, context, config, fixture))
    except _Fail as f:
        return RuleResult(spec.assertion_id, spec.rule_id, "failed", f.expected, f.extracted,
                          f"{spec.rule_id}: {f.detail}", fields, label=label)
    return RuleResult(spec.assertion_id, spec.rule_id, "passed", expected, extracted, None, fields, label=label)


def rebind(extraction: Extraction, fmt: str) -> Extraction:
    """A stored extraction re-accepted under `fmt`'s floor (its attempts are re-judged in order)."""
    attempts = tuple(Attempt(a.source, a.reading, problem(a.reading, fmt)) for a in extraction.attempts)
    accepted = next((a.reading for a in attempts if a.rejected is None), None)
    return replace(extraction, fmt=fmt, attempts=attempts, accepted=accepted)


def check_rules(values: Mapping[str, Extraction | str | int | float | Decimal], fixture: Mapping[str, Any],
                rules: Sequence[RuleSpec | Mapping[str, Any]], *, config: RuleConfig = DEFAULT_RULES,
                context: EvalContext = NO_CONTEXT) -> list[RuleResult]:
    """Re-run rules over stored values (2.8.1): `values` maps a region id to its stored extraction,
    its text (taken as a clean read) or a number; `rules` are `RuleSpec`s or 4.7 `rule` assertions."""
    out = []
    for r in rules:
        spec = r if isinstance(r, RuleSpec) else RuleSpec.from_assertion(r)
        operands: dict[str, Operand] = {}
        for name, ref in spec.inputs.items():
            if not isinstance(ref, str):
                operands[name] = ref
                continue
            if ref not in values:
                raise OracleConfigError(f"{spec.assertion_id}: no stored value for region {ref}")
            v = values[ref]
            fmt = spec.operand_format(name)
            if isinstance(v, Extraction):
                operands[name] = rebind(v, fmt)
            elif isinstance(v, str):
                operands[name] = literal(ref, fmt, v, spec.operand_source(name, "ocr"))
            else:
                operands[name] = literal(ref, fmt, str(v), spec.operand_source(name, "ocr"))
        out.append(evaluate_rule(spec, operands, fixture, config, context))
    return out
