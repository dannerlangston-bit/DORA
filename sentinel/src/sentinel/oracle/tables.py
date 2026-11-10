"""The Oracle's tables and its model of the checkpoint: tolerances (2.8.2), multipliers and ticks
(2.8.3), the R02 margin brackets (`fixtures/margin_rules.yaml`), the paperMoney delay model and
the evaluation context.

- `EvalContext`: what the Oracle knows about the checkpoint besides its reads (data mode, capture
  time, the fixture symbol, the order's opening or closing side, the symbol's last session close,
  the 24-hour trading list read this run).
- `RuleConfig`: the rule tables (margin brackets, tick sizes, multipliers, the pinned palette, the
  European-style roots and the parity inputs). `RuleConfig.load(margin_rules_path)` reads
  `fixtures/margin_rules.yaml`.
- `Tolerance`, `money_tolerance`, `default_tolerance`: the 2.8.2 table.
- `delay_minutes`, `api_comparison`: the paperMoney delay model for API cross-checks (2.8.2, 7.9).
- `multiplier_for`, `tick_size`, `symbol_root`, `symbol_asset`: per-instrument lookups.
- `resolve_ref`: a `value_ref` fixture path (4.7) to its value.

`fixtures/margin_rules.yaml` (4.1 names the file; its shape is not in the spec, so it is fixed
here and reported):

```yaml
schema_version: 1
pinned: false          # true once Phase 0 has read the exact paperMoney formula from twenty staged tickets
brackets:              # |buying power effect| lies in [low x base, high x base]
  - {asset: equity, account: margin, of: notional, low: 0.50, high: 1.00}
  - {asset: equity, account: ira, of: notional, low: 1.00, high: 1.00}
  - {asset: option, account: margin, of: notional, low: 1.00, high: 1.00}   # long option: the premium
  - {asset: option, account: ira, of: notional, low: 1.00, high: 1.00}
  - {asset: future, account: margin, of: initial_margin, low: 1.00, high: 1.00}
initial_margin: {MES: 2464.00, ES: 24640.00}       # exchange initial margin per contract, by futures root
```

`asset` is the fixture's symbol asset enum (4.9), `account` the fixture account kind, `of` is
`notional` (the R02 `notional` operand) or `initial_margin` (the root's initial margin times the
order quantity). A pinned file must give every bracket `low == high`: the exact formula. Until
pinned, R02 results carry the label "bracket check" (2.8.3). `DEFAULT_MARGIN` is the unpinned
table 2.8.3 states (stock 50 to 100 percent on margin and 100 percent on the IRA, long option the
premium, futures the initial margin), with no initial margins: a futures R02 needs the file.

Choices where the spec is ambiguous (reported):

- Money tolerance, `masked_live` (2.8.2 "0.01 plus one tick per leg"): one tick of a leg in money
  is tick x |quantity| x multiplier, the cost of a one-tick mid-price rounding on that leg.
- Tolerances given as `tol` in a flow are absolute; a string ending in `%` (`"0.5%"`) is relative
  to the expected value (4.7's "absolute or percent tolerance"; the schema types `tol` as num).
- API cross-checks (2.8.2): under `masked_live` the API value is taken as of capture time minus
  the fixture's delay (`data_mode.masked_live.delay_minutes`, 20; 0 when `realtime: true` or the
  epoch says `realtime`), within the assertion's `tol` or one tick, and exactly (tolerance 0, a
  `tol` notwithstanding) only when the symbol's last session closed more than the delay before
  capture and the symbol is absent from the 24-hour trading list. Under `frozen` it is taken as
  of the fixture's pinned moment, `data_mode.frozen.timestamp`, else the pinned OnDemand timestamp
  `epoch.ondemand_timestamp` (DR-02 branch B, 2.8.1, 4.9), and compared exactly unless the assertion
  gives a `tol` (the study-label row of 2.8.2 gives 0.5 percent that way); a frozen fixture with
  neither is a config error, never a comparison with today's value as of capture. An unknown session
  close is not "closed".
- Ticks: equities 0.01 (0.0001 below one dollar), options and indexes 0.01, forex 0.00001,
  futures by root (`FUTURE_TICKS`); a root with no tick fails loudly, never a guessed tick.
- Multipliers (2.8.3): equity and index options 100, futures by root (`FUTURE_MULTIPLIERS`),
  forex 1 (quantity displayed in units; a pair whose quantity is shown in lots takes its lot size
  from `RuleConfig.multipliers`), equities 1.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .values import CENT, NUMERIC_FORMATS

DATA_MODES = ("frozen", "masked_live")
ASSETS = ("equity", "option", "future", "future_option", "forex", "index")     # 4.9 symbol.asset
ACCOUNT_KINDS = ("margin", "ira")                                              # 4.9 account.kind
BRACKET_BASES = ("notional", "initial_margin")                                 # margin_rules.yaml `of`
FUTURE_MULTIPLIERS: dict[str, Decimal] = {"ES": Decimal(50), "NQ": Decimal(20), "MES": Decimal(5), "MNQ": Decimal(2)}
OPTION_MULTIPLIER = Decimal(100)
FUTURE_TICKS: dict[str, Decimal] = {"ES": Decimal("0.25"), "NQ": Decimal("0.25"), "MES": Decimal("0.25"),
                                    "MNQ": Decimal("0.25")}
ASSET_TICKS: dict[str, Decimal] = {"equity": CENT, "option": CENT, "index": CENT, "forex": Decimal("0.00001")}
SUB_DOLLAR_TICK = Decimal("0.0001")
PERCENT_TOLERANCE = CENT                       # 2.8.2 percent: 0.01 absolute
DEFAULT_DELAY_MINUTES = 20                     # 2.8.2, 4.9 masked_live.delay_minutes
_FUTURE = re.compile(r"^\.?/(?P<root>[A-Z0-9]+?)(?:[FGHJKMNQUVXZ][0-9]{2}.*)?$")
_OPTION = re.compile(r"^\.(?P<root>[A-Z]+)\d")


class OracleConfigError(ValueError):
    """The flow, fixture or rule tables do not give the Oracle what an assertion needs. The lint
    should have refused it; at run time it is a harness fault, never a pass or a product failure."""


# --------------------------------------------------------------------------------------------
# Tolerances (2.8.2)


@dataclass(frozen=True)
class Tolerance:
    """An absolute tolerance, or a relative one (`relative=True`, `value` in percent)."""

    value: Decimal
    relative: bool = False

    @classmethod
    def parse(cls, tol: object) -> "Tolerance":
        """A flow's `tol`: a number (absolute) or a string ending in `%` (relative)."""
        if isinstance(tol, str) and tol.strip().endswith("%"):
            return cls(Decimal(tol.strip()[:-1]), relative=True)
        if isinstance(tol, bool) or not isinstance(tol, (int, float, str, Decimal)):
            raise OracleConfigError(f"tol {tol!r} is neither a number nor a percent")
        return cls(Decimal(str(tol)))

    def bound(self, reference: Decimal) -> Decimal:
        """The allowed absolute difference around `reference`."""
        return abs(reference) * self.value / 100 if self.relative else self.value

    def allows(self, observed: Decimal, reference: Decimal) -> bool:
        return abs(observed - reference) <= self.bound(reference)

    def __str__(self) -> str:
        return f"{self.value.normalize():f}%" if self.relative else f"{self.value.normalize():f}"


def money_tolerance(data_mode: str | None, legs: Sequence[tuple[Decimal, Decimal, Decimal]] = ()) -> Decimal:
    """2.8.2 money: exact after rounding to the cent in `frozen` (0); 0.01 plus one tick per leg in
    `masked_live`, each leg given as (quantity, tick, multiplier)."""
    if data_mode == "frozen":
        return Decimal(0)
    if data_mode == "masked_live":
        return CENT + sum((tick * abs(qty) * mult for qty, tick, mult in legs), Decimal(0))
    raise OracleConfigError(f"money tolerance needs the checkpoint's data mode, got {data_mode!r}")


# --------------------------------------------------------------------------------------------
# Instruments (2.8.3)


def symbol_root(symbol: str) -> str:
    """`/MESZ26` -> MES, `/ES` -> ES, `.AAPL261218C200` -> AAPL, `EUR/USD` and `AAPL` unchanged."""
    if m := _FUTURE.match(symbol):
        return m["root"]
    if m := _OPTION.match(symbol):
        return m["root"]
    return symbol


def symbol_asset(symbol: str, fixture: Mapping[str, Any] | None = None) -> str:
    """The 4.9 asset class of a fully qualified symbol: the fixture's entry when it lists the
    symbol, else from its form (`/` future, `./` future option, `.` option, `X/Y` forex, equity)."""
    for s in (fixture or {}).get("symbols") or ():
        if s.get("symbol") == symbol and s.get("asset") in ASSETS:
            return s["asset"]
    if symbol.startswith("./"):
        return "future_option"
    if symbol.startswith("/"):
        return "future"
    if symbol.startswith("."):
        return "option"
    if "/" in symbol:
        return "forex"
    return "equity"


@dataclass(frozen=True)
class MarginBracket:
    """One R02 row: |buying power effect| in [low x base, high x base], base by `of`."""

    asset: str
    account: str
    of: str                                    # notional | initial_margin
    low: Decimal
    high: Decimal

    def __post_init__(self) -> None:
        if self.asset not in ASSETS or self.account not in ACCOUNT_KINDS or self.of not in BRACKET_BASES:
            raise OracleConfigError(f"margin bracket {self.asset}/{self.account}/{self.of} is outside the 4.9 enums")
        if not Decimal(0) <= self.low <= self.high:
            raise OracleConfigError(f"margin bracket {self.asset}/{self.account}: need 0 <= low <= high")


@dataclass(frozen=True)
class MarginRules:
    """`fixtures/margin_rules.yaml` (2.8.3 R02, 4.1); see the module docstring for the shape."""

    pinned: bool
    brackets: tuple[MarginBracket, ...]
    initial_margin: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [(b.asset, b.account) for b in self.brackets]
        if len(keys) != len(set(keys)):
            raise OracleConfigError("margin_rules.yaml lists an asset and account pair twice")
        if self.pinned and any(b.low != b.high for b in self.brackets):
            raise OracleConfigError("a pinned margin_rules.yaml gives the exact formula: low == high on every bracket")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarginRules":
        if data.get("schema_version") != 1:
            raise OracleConfigError("margin_rules.yaml: schema_version must be 1")
        brackets = tuple(MarginBracket(str(b["asset"]), str(b["account"]), str(b["of"]), Decimal(str(b["low"])),
                                       Decimal(str(b["high"]))) for b in data.get("brackets") or ())
        initial = {str(k): Decimal(str(v)) for k, v in (data.get("initial_margin") or {}).items()}
        return cls(bool(data.get("pinned", False)), brackets, initial)

    @classmethod
    def load(cls, path: str | Path) -> "MarginRules":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def bracket(self, asset: str, account: str) -> MarginBracket:
        for b in self.brackets:
            if b.asset == asset and b.account == account:
                return b
        raise OracleConfigError(f"margin_rules.yaml has no R02 bracket for {asset} on a {account} account")

    @property
    def label(self) -> str | None:
        """2.8.3: until pinned, R02 results are labeled "bracket check"."""
        return None if self.pinned else "bracket check"


DEFAULT_MARGIN = MarginRules(pinned=False, brackets=(
    MarginBracket("equity", "margin", "notional", Decimal("0.50"), Decimal("1.00")),
    MarginBracket("equity", "ira", "notional", Decimal("1.00"), Decimal("1.00")),
    MarginBracket("option", "margin", "notional", Decimal("1.00"), Decimal("1.00")),
    MarginBracket("option", "ira", "notional", Decimal("1.00"), Decimal("1.00")),
    MarginBracket("future", "margin", "initial_margin", Decimal("1.00"), Decimal("1.00")),
))


@dataclass(frozen=True)
class RuleConfig:
    """The rule tables of 2.8.3 and the pinned palette `sign_color_agrees` compares with.

    `tick_sizes` and `multipliers` are keyed by symbol root (futures) or pair (forex);
    `european_style` lists the option roots that are European-style and cash-settled (R06 parity
    as an equality); `risk_free_rate` is R06's annual rate when the assertion has no `rate`
    operand; `dividend_yield` (continuous, per root) enters European parity and `dividends_pv`
    (per root, present value to expiration) the American lower bound.
    """

    margin: MarginRules = DEFAULT_MARGIN
    tick_sizes: Mapping[str, Decimal] = field(default_factory=lambda: dict(FUTURE_TICKS))
    multipliers: Mapping[str, Decimal] = field(default_factory=lambda: dict(FUTURE_MULTIPLIERS))
    palette: Mapping[str, tuple[int, int, int]] = field(default_factory=dict)   # positive, negative, neutral
    palette_tolerance: float = 60.0
    european_style: frozenset[str] = frozenset({"SPX", "SPXW"})
    risk_free_rate: Decimal = Decimal(0)
    dividend_yield: Mapping[str, Decimal] = field(default_factory=dict)
    dividends_pv: Mapping[str, Decimal] = field(default_factory=dict)

    @classmethod
    def load(cls, margin_rules_path: str | Path, **overrides: Any) -> "RuleConfig":
        """The default tables with the margin brackets of `fixtures/margin_rules.yaml`."""
        return cls(margin=MarginRules.load(margin_rules_path), **overrides)


def multiplier_for(symbol: str, config: RuleConfig, fixture: Mapping[str, Any] | None = None) -> Decimal:
    """The 2.8.3 contract multiplier of a fully qualified symbol."""
    asset = symbol_asset(symbol, fixture)
    root = symbol_root(symbol)
    if asset == "option":
        return OPTION_MULTIPLIER
    if asset in ("future", "future_option"):
        if root not in config.multipliers:
            raise OracleConfigError(f"no multiplier for futures root {root} (2.8.3 fixture table)")
        return config.multipliers[root]
    if asset == "forex":
        return config.multipliers.get(root, Decimal(1))
    return Decimal(1)


def tick_size(symbol: str, config: RuleConfig, fixture: Mapping[str, Any] | None = None,
              price: Decimal | None = None) -> Decimal:
    """One price tick of `symbol` (2.8.2 "within 1 tick")."""
    root = symbol_root(symbol)
    if root in config.tick_sizes:
        return config.tick_sizes[root]
    asset = symbol_asset(symbol, fixture)
    if asset == "equity" and price is not None and abs(price) < 1:
        return SUB_DOLLAR_TICK
    if asset not in ASSET_TICKS:
        raise OracleConfigError(f"no tick size for {symbol} ({asset}); add its root to RuleConfig.tick_sizes")
    return ASSET_TICKS[asset]


def default_tolerance(fmt: str, data_mode: str | None, tick: Decimal | None) -> Tolerance:
    """The 2.8.2 tolerance of a field when the assertion gives no `tol`."""
    if fmt == "money":
        return Tolerance(money_tolerance(data_mode))
    if fmt == "percent":
        return Tolerance(PERCENT_TOLERANCE)
    if fmt == "int":
        return Tolerance(Decimal(0))
    if fmt == "price" and tick is not None:
        return Tolerance(tick)
    if fmt not in NUMERIC_FORMATS:
        raise OracleConfigError(f"a tolerance needs a numeric format, got {fmt!r}")
    raise OracleConfigError("a price tolerance needs the checkpoint's symbol for its tick (2.8.2)")


# --------------------------------------------------------------------------------------------
# Context, fixture and the delay model


@dataclass(frozen=True)
class EvalContext:
    """What the Oracle knows about the checkpoint besides its reads.

    `symbol` is fully qualified as in the fixture (`AAPL`, `/MESZ26`, `SPX`, `.SPXW261120C6000`);
    `asset` defaults to the fixture's entry for it; `account_kind` to the fixture's golden account.
    `opening` and `quantity` describe the staged order (R02); `session_close` is the symbol's last
    session close before capture and `trading_24h` the 24-hour trading list read in-platform this
    run (2.8.2); `all_strikes` is the chain's strike count for a Strikes selector of All (R04);
    `long_options` says the Greeks are a long option's (R05 theta).
    """

    data_mode: str | None = None
    capture_time: datetime | None = None
    symbol: str | None = None
    asset: str | None = None
    account_kind: str | None = None
    opening: bool | None = None
    quantity: Decimal | None = None
    session_close: datetime | None = None
    trading_24h: frozenset[str] = frozenset()
    all_strikes: int | None = None
    long_options: bool = True

    def __post_init__(self) -> None:
        if self.data_mode is not None and self.data_mode not in DATA_MODES:
            raise OracleConfigError(f"data_mode {self.data_mode!r}")
        if self.asset is not None and self.asset not in ASSETS:
            raise OracleConfigError(f"asset {self.asset!r}")
        if self.account_kind is not None and self.account_kind not in ACCOUNT_KINDS:
            raise OracleConfigError(f"account kind {self.account_kind!r}")

    def need_symbol(self, why: str) -> str:
        if not self.symbol:
            raise OracleConfigError(f"{why} needs the checkpoint's symbol in the evaluation context")
        return self.symbol

    def need_capture_time(self, why: str) -> datetime:
        if self.capture_time is None:
            raise OracleConfigError(f"{why} needs the capture time in the evaluation context")
        return self.capture_time

    def asset_of(self, fixture: Mapping[str, Any] | None) -> str:
        return self.asset or symbol_asset(self.need_symbol("the asset class"), fixture)

    def account_of(self, fixture: Mapping[str, Any] | None) -> str:
        if self.account_kind:
            return self.account_kind
        for a in (fixture or {}).get("accounts") or ():
            if a.get("role") == "golden" and a.get("kind") in ACCOUNT_KINDS:
                return a["kind"]
        raise OracleConfigError("no account kind: the context names none and the fixture has no golden account")


def resolve_ref(fixture: Mapping[str, Any], ref: str) -> Any:
    """A 4.7 `value_ref` (`fixture.symbols.aapl.symbol`, `fixture.prices.aapl_far_below.value`):
    dict keys by name, list items by their `id` (or `account_id`)."""
    parts = ref.split(".")
    if parts[0] != "fixture" or len(parts) < 2:
        raise OracleConfigError(f"value_ref {ref!r} is not a fixture path")
    node: Any = fixture
    for part in parts[1:]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list):
            hit = [x for x in node if isinstance(x, Mapping) and part in (x.get("id"), x.get("account_id"))]
            if len(hit) != 1:
                raise OracleConfigError(f"value_ref {ref!r}: no single entry with id {part!r}")
            node = hit[0]
        else:
            raise OracleConfigError(f"value_ref {ref!r}: {part!r} not found")
    if isinstance(node, (Mapping, list)):
        raise OracleConfigError(f"value_ref {ref!r} names a structure, not a value")
    return node


def delay_minutes(fixture: Mapping[str, Any]) -> int:
    """The paperMoney quote delay of the login (2.8.2, 7.9): 0 when the fixture says real time."""
    live = ((fixture.get("data_mode") or {}).get("masked_live")) or {}
    if live.get("realtime") is True or (fixture.get("epoch") or {}).get("data_delay") == "realtime":
        return 0
    return int(live.get("delay_minutes", DEFAULT_DELAY_MINUTES))


def _as_datetime(value: Any) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ApiComparison:
    """How an API cross-check compares (2.8.2): the modeled time of the API value and the mode,
    `exact` (tolerance 0 whatever `tol` says), `tick` (`tol`, else one tick) or `frozen` (`tol`,
    else exact)."""

    as_of: datetime
    mode: str
    why: str

    def tolerance(self, tol: Tolerance | None, tick: Decimal | None) -> Tolerance:
        if self.mode == "exact":
            return Tolerance(Decimal(0))
        if tol is not None:
            return tol
        if self.mode == "frozen":
            return Tolerance(Decimal(0))
        if tick is None:
            raise OracleConfigError("an API cross-check within one tick needs the symbol's tick")
        return Tolerance(tick)


def api_comparison(context: EvalContext, fixture: Mapping[str, Any]) -> ApiComparison:
    """The 2.8.2 delay model for `api_within` (see the module docstring)."""
    capture = context.need_capture_time("an API cross-check")
    if context.data_mode == "frozen":
        frozen = ((fixture.get("data_mode") or {}).get("frozen")) or {}
        if frozen.get("timestamp"):
            return ApiComparison(_as_datetime(frozen["timestamp"]), "frozen", "frozen: as of the fixture timestamp")
        ondemand = (fixture.get("epoch") or {}).get("ondemand_timestamp")
        if ondemand:
            return ApiComparison(_as_datetime(ondemand), "frozen",
                                 "frozen (OnDemand, DR-02 branch B): as of the pinned OnDemand timestamp")
        raise OracleConfigError("a frozen API cross-check needs data_mode.frozen.timestamp or "
                                "epoch.ondemand_timestamp (2.8.1, 4.9); it is never compared as of capture")
    if context.data_mode != "masked_live":
        raise OracleConfigError("an API cross-check needs the checkpoint's data mode")
    symbol = context.need_symbol("an API cross-check")
    delay = timedelta(minutes=delay_minutes(fixture))
    as_of = capture - delay
    minutes = int(delay.total_seconds() // 60)
    if symbol in context.trading_24h:
        return ApiComparison(as_of, "tick", f"{symbol} is on the 24-hour trading list: compared with the value "
                                            f"{minutes} minutes before capture within tolerance")
    if context.session_close is not None and capture - context.session_close > delay:
        return ApiComparison(as_of, "exact", f"{symbol}'s session closed more than {minutes} minutes before "
                                             "capture: exact")
    return ApiComparison(as_of, "tick", f"compared with the value {minutes} minutes before capture within tolerance")
