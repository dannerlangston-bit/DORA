"""Stage 2 of the fixture reset flow around the account comparison, and the selectors of its outcomes (7.6 stage 2,
7.7 step 8, 7.9, 7.11, 4.9 L-FIX-08, 4.5 `account_state`).

`gates.check_fixture` compares the accounts (working orders, positions, cash, buying power, the empty-account reset
and the own-tag Cancel All). This module adds what else closes `fixture_verified`, each a pure check over a rig read:

- `check_ondemand` (in gates) for the OnDemand toolbar, `check_delay_state` for the delayed-data badge against
  `epoch.data_delay` (`delay_state`), `check_connectivity` for the post-login connectivity check (`env_error (gate:
  connectivity, reason: backend_degraded)`), `check_alerts` for the alert book re-read after the reset;
- `recompute_prices` and `check_prices` for the far-from-market prices (step 6, L-FIX-08: `prices_stale` on the
  order-bearing checkpoints only);
- `check_pinned_quote` for the frozen Branch A pinned quote (`data_epoch_mismatch` on the frozen checkpoints) and
  `check_symbols` for the symbol pre-check (`fixture_maintenance_needed` on that symbol's checkpoints);
- `check_stage2` runs them in order over one `Stage2Reads` and returns every result, ending at the first that sets
  a run outcome, as the Orchestrator applies them;
- `select_subset` resolves the selectors these results name (`account_state`, `account_state:golden`,
  `order_bearing`, `symbol:<id>`) to the checkpoints of a scope; `connectivity_maintenance_event` folds one night's
  `backend_degraded` failures across rigs into the one maintenance event 7.11 asks for.

Choices where the spec is ambiguous or silent (reported):

1. Order (7.6 stage 2, 7.7 step 8, 7.11): the account comparison first (a working order is the safety abort and
   beats every environment fault), then the OnDemand toolbar (inside OnDemand nothing read is the fixture), the delay
   badge, connectivity, the alert re-read, then the subset checks: prices, pinned quote, symbols. `delay_state`,
   `ondemand_state`, `backend_degraded` and alert drift name no subset, so each takes the whole shard (5.2: a gate
   failure before `running` marks every scoped checkpoint).
2. The badge (4.9, 7.9, 7.11): a readable badge that differs from `epoch.data_delay` is `delay_state`; an unreadable
   one cannot equal the epoch, which is the connectivity check's own condition, so it is `backend_degraded`.
   A delayed badge reads as `delayed_<minutes>` (20 when the badge shows no minutes).
3. A reconnect loop (7.11) is two or more reconnect lines in the client log slice since launch.
4. Buying power by rule (7.6 step 1; the spec names no formula): never negative, and on an account that is not a
   margin account (the IRA, a cash account) at most cash plus `cash_tolerance`; with a `cash_floor`, at most the floor
   plus `cash_tolerance`, which is what keeps a stray send rejected for buying power (7.6, L-FIX-02).
5. Alerts (7.5, 7.6 step 3): Stage 1 deletes them, so `alerts_expected` is checked on every read that carries a
   count, and the re-read after an empty-account reset must carry one. A difference is fixture drift of the whole
   shard (7.5 alerts row: not an `account_state` row), at the gate where it is found, `fixture_verified`.
6. Prices (7.6 step 6, 4.9, L-FIX-08): every price is recomputed from the quote source the caller picks (the Oracle
   API, or the app quote by OCR when N3 failed); a price whose quote cannot be read keeps its value, and only one
   whose `computed_at` is more than 24 hours before run start is then stale. A rule is `<base> <op> <number>,
   rounded to <tick>` with base `last_delayed`, op `-`, `+`, `x` or `/`, rounded to the nearest tick (half up).
7. Order-bearing checkpoints (L-FIX-08) are every checkpoint of a flow with an `order_stage: true` step or a
   `value_ref` into `fixture.prices`: the stale price would be typed somewhere in that flow.
8. The checkpoints using a symbol (7.9 pre-check) are the flow checkpoints whose flow references it (a
   `fixture.symbols.<id>` or a `fixture.prices` entry on it) and every checkpoint whose workspace links it in a
   link group (4.6), since a linked gadget shows it.
9. `account_state` is every checkpoint whose step override, or else whose surface, carries `account_state: true`
   (4.9 "every checkpoint whose surface or step carries account_state"); `account_state:golden` restricts it to the
   checkpoints run on the golden account: a flow's `account_role` (default `empty`), and for a census, sweep or other
   non-flow checkpoint its workspace entry's `account_role` (default `golden`; SD-75).
10. The pinned quote compares exactly every field the dataset carries (last, bid, ask, quote time, the chart's last
    bar, the chain header, the expiration list); a field the read lacks is a mismatch.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from ..records import MaintenanceItem
from . import alerts
from .gates import AccountExpectation, check_fixture, check_ondemand
from .ports import ConnectivityObservation, DelayBadge
from .results import ControlResult, Outcome, RunVerdict, SubsetOutcome
from .rig import AccountState

GATE = "fixture_verified"
PRICES_MAX_AGE = timedelta(hours=24)                 # L-FIX-08
RECONNECT_LOOP = 2                                   # choice 3
REALTIME_FRESH_S = 5.0                               # 7.11
DELAYED_GRACE_S = 60.0                               # 7.11: the delay window plus 60 seconds
SELECTORS = ("account_state", "account_state:golden", "order_bearing")
SYMBOL_PREFIX = "symbol:"


def _shard_error(control: str, gate: str, reason: str, detail: str, *,
                 alert: alerts.AlertRoute | None = None, **kw: Any) -> ControlResult:
    """An `env_error` for every remaining checkpoint of the shard, run `complete_with_env_errors` (5.2)."""
    return ControlResult(control, False, gate, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", reason, gate, detail), alert=alert or alerts.env_error(),
                         detail=detail, **kw)


# -- the fixture -------------------------------------------------------------------------------------------
def expectations(fixture: Mapping[str, Any]) -> dict[str, AccountExpectation]:
    """The fixture's accounts as `check_fixture` compares them (4.9): positions by symbol string, cash and tolerance,
    and the buying power bounds of choice 4."""
    symbols = {s["id"]: str(s["symbol"]) for s in fixture.get("symbols") or ()}
    out = {}
    for a in fixture.get("accounts") or ():
        aid = str(a["account_id"])
        cash, tol = float(a["cash"]), float(a.get("cash_tolerance", 0))
        hi = None if a.get("kind") == "margin" else cash + tol
        if a.get("cash_floor") is not None:
            floor = float(a["cash_floor"]) + tol
            hi = floor if hi is None else min(hi, floor)
        pos = {symbols[p["symbol_ref"]]: float(p["qty"]) for p in fixture.get("positions") or ()
               if p.get("account_ref") == aid}
        out[aid] = AccountExpectation(str(a["role"]), pos, cash, tol, buying_power_min=0.0, buying_power_max=hi)
    return out


def data_delay(fixture: Mapping[str, Any]) -> tuple[str, int]:
    """`epoch.data_delay` and the delay in minutes (`data_mode.masked_live.delay_minutes`, default 20; 0 when
    real time) (4.9)."""
    state = str((fixture.get("epoch") or {}).get("data_delay", "delayed_20"))
    minutes = int(((fixture.get("data_mode") or {}).get("masked_live") or {}).get("delay_minutes", 20))
    return state, 0 if state == "realtime" else minutes


# -- the delay badge and connectivity (7.9, 7.11) ------------------------------------------------------------
def badge_state(badge: DelayBadge) -> str | None:
    """The badge as an `epoch.data_delay` value (choice 2), None when unreadable."""
    if badge.delayed is None:
        return None
    return f"delayed_{badge.delay_minutes or 20}" if badge.delayed else "realtime"


def check_delay_state(badge: DelayBadge, expected: str) -> ControlResult | None:
    """4.9, 7.9: the delayed-data badge read at `fixture_verified` against `epoch.data_delay`; a difference is
    `env_error (gate: fixture_verified, reason: delay_state)` for the shard. None when the badge is unreadable: the
    connectivity check owns that case (choice 2)."""
    state = badge_state(badge)
    if state is None:
        return None
    if state != expected:
        return _shard_error("delay_badge", GATE, "delay_state", f"badge reads {state} ({badge.text!r}); the epoch "
                            f"pins {expected}", evidence={"badge": badge.text, "confidence": badge.confidence})
    return ControlResult("delay_badge", True, GATE, evidence={"badge": badge.text})


def check_connectivity(obs: ConnectivityObservation, *, reconnects_since_launch: int, quote_age_s: float | None,
                       badge: DelayBadge, delay: str, delay_minutes: int) -> ControlResult:
    """The post-login connectivity check, part of `fixture_verified` (7.11, 10.8 check 13): the indicator is not
    Disconnected, no reconnect loop in the client log since launch, the pinned quote fresh for the entitlement (the
    delay plus 60 s for delayed data, 5 s for real time), the badge readable (choice 2). A failure is `env_error (gate:
    connectivity, reason: backend_degraded)` for the shard."""
    limit = REALTIME_FRESH_S if delay == "realtime" else delay_minutes * 60 + DELAYED_GRACE_S
    age = "unreadable" if quote_age_s is None else f"{quote_age_s:.0f} s old"
    faults = [what for bad, what in (
        (obs.connected is not True, f"connection indicator {obs.status_text!r}"),
        (obs.backend_degraded, "the status bar reports a degraded backend"),
        (reconnects_since_launch >= RECONNECT_LOOP, f"{reconnects_since_launch} reconnects in the client log since "
                                                    "launch"),
        (quote_age_s is None or quote_age_s > limit, f"pinned quote {age}, limit {limit:.0f} s"),
        (badge_state(badge) is None, "delayed-data badge unreadable"),
    ) if bad]
    ev = {"status": obs.status_text, "reconnects": reconnects_since_launch, "quote_age_s": quote_age_s}
    if faults:
        return _shard_error("connectivity", "connectivity", "backend_degraded", "; ".join(faults), evidence=ev)
    return ControlResult("connectivity", True, "connectivity", evidence=ev)


def connectivity_maintenance_event(failures: Iterable[tuple[str, ControlResult]], *, night: date, owner: str,
                                   opened_at: str) -> MaintenanceItem | None:
    """7.11: `backend_degraded` failures across rigs on one night post once, as one maintenance event that feeds the
    maintenance calendar. `failures` is (rig id, connectivity result) for every shard of the night; None when no rig
    failed."""
    rigs = sorted({rig for rig, r in failures if not r.passed and r.unreached is not None
                   and r.unreached.reason == "backend_degraded"})
    if not rigs:
        return None
    return MaintenanceItem(kind="backend_degraded", ref=f"connectivity/{night.isoformat()}", owner=owner,
                           opened_at=opened_at, detail=f"post-login connectivity failed on {', '.join(rigs)}")


def check_alerts(first: Sequence[AccountState], after_reset: Sequence[AccountState] | None,
                 alerts_expected: int) -> ControlResult:
    """The alert book (7.5 alerts row, 7.6 step 3, choice 5): every read that carries a count equals
    `alerts_expected`, and the re-read after an empty-account reset carries one."""
    faults = []
    for label, reads in (("read", first), ("re-read after the reset", after_reset or ())):
        for a in reads:
            n = getattr(a, "alert_count", None)
            if n is None and label != "read" and hasattr(a, "alert_count"):
                faults.append(f"{a.role}: alerts not {label}")
            elif n is not None and n != alerts_expected:
                faults.append(f"{a.role}: {n} alert(s) at the {label}, expected {alerts_expected}")
    if faults:
        return _shard_error("alerts", GATE, "fixture_drift", "; ".join(faults))
    return ControlResult("alerts", True, GATE)


# -- prices (7.6 step 6, L-FIX-08) -------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceRule:
    """A fixture price rule (4.9 `prices.rule`, choice 6), e.g. `last_delayed - 1000, rounded to 0.25`."""

    base: str
    op: str
    operand: Decimal
    tick: Decimal

    def apply(self, quote: float) -> float:
        """The rule over one quote, rounded to the nearest tick, half up."""
        q = Decimal(str(quote))
        raw = {"-": q - self.operand, "+": q + self.operand, "x": q * self.operand, "/": q / self.operand}[self.op]
        return float((raw / self.tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * self.tick)


_RULE = re.compile(r"^\s*(?P<base>last_delayed)\s*(?P<op>[-+x*/])\s*(?P<k>\d+(?:\.\d+)?)\s*,\s*rounded to\s*"
                   r"(?P<tick>\d+(?:\.\d+)?)\s*$")


def parse_price_rule(text: str) -> PriceRule:
    """Parse a 4.9 price rule; ValueError for any other shape."""
    m = _RULE.match(text)
    if m is None or Decimal(m["tick"]) == 0:
        raise ValueError(f"price rule {text!r}: expected '<last_delayed> <op> <number>, rounded to <tick>'")
    return PriceRule(m["base"], "x" if m["op"] == "*" else m["op"], Decimal(m["k"]), Decimal(m["tick"]))


class QuoteSource(Protocol):
    """Where the recompute reads the delayed last price: the Oracle API (`name` `api`), or the app's own quote by OCR
    when preflight N3 failed (`name` `app_quote`)."""

    name: str

    def last_delayed(self, symbol: str) -> float | None:
        """The delayed last price of a fully qualified symbol; None when it cannot be read."""


@dataclass(frozen=True)
class PriceRecompute:
    """The prices in force after step 6: value per price id, the source each came from (`api`, `app_quote`, or
    `fixture` when kept), and the ids that are stale (older than 24 h and not recomputed)."""

    values: Mapping[str, float]
    sources: Mapping[str, str]
    stale: tuple[str, ...] = ()

    @property
    def health(self) -> str:
        """The run health line: which source each price came from (7.6 step 6)."""
        by: dict[str, list[str]] = {}
        for pid in sorted(self.sources):
            by.setdefault(self.sources[pid], []).append(pid)
        return "fixture prices: " + "; ".join(f"{src} ({', '.join(ids)})" for src, ids in sorted(by.items()))


def _when(v: Any) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def recompute_prices(fixture: Mapping[str, Any], quote_source: QuoteSource | None, now: datetime) -> PriceRecompute:
    """Recompute every fixture price from `quote_source` (choice 6); `now` is the run start (tz-aware)."""
    symbols = {s["id"]: str(s["symbol"]) for s in fixture.get("symbols") or ()}
    values, sources, stale = {}, {}, []
    for p in fixture.get("prices") or ():
        pid = str(p["id"])
        rule = parse_price_rule(str(p["rule"]))
        quote = quote_source.last_delayed(symbols[p["symbol_ref"]]) if quote_source is not None else None
        if quote is not None:
            values[pid], sources[pid] = rule.apply(quote), quote_source.name      # type: ignore[union-attr]
            continue
        values[pid], sources[pid] = float(p["value"]), "fixture"
        if now - _when(p["computed_at"]) > PRICES_MAX_AGE:
            stale.append(pid)
    return PriceRecompute(values, sources, tuple(stale))


def check_prices(rc: PriceRecompute) -> ControlResult:
    """L-FIX-08: stale prices make the order-bearing checkpoints `env_error (gate: fixture_verified, reason:
    prices_stale)`; the rest of the run proceeds, never `config_invalid`. The detail is the run health line."""
    if rc.stale:
        return ControlResult("prices", False, GATE, outcomes=(SubsetOutcome(
            "order_bearing", Outcome("env_error", "prices_stale", GATE)),), alert=alerts.env_error(),
            detail=f"prices older than 24 h and not recomputed: {', '.join(rc.stale)}; {rc.health}",
            evidence={"values": dict(rc.values), "sources": dict(rc.sources)})
    return ControlResult("prices", True, GATE, detail=rc.health,
                         evidence={"values": dict(rc.values), "sources": dict(rc.sources)})


# -- the data epoch: pinned quote and symbol pre-check (7.9) -------------------------------------------------
@dataclass(frozen=True)
class EpochRead:
    """The fixture symbol's quote and chart and chain reads (frozen Branch A, 7.9), or the dataset's values for them;
    a None field of the dataset is not compared."""

    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    quote_time: str | None = None
    chart_last_bar: float | None = None
    chain_header: str | None = None
    expirations: tuple[str, ...] | None = None


def check_pinned_quote(read: EpochRead, dataset: EpochRead) -> ControlResult:
    """Frozen Branch A at `fixture_verified` (7.9, choice 10): any difference from the dataset is `env_error (gate:
    fixture_verified, reason: data_epoch_mismatch)` for the frozen checkpoints of the shard."""
    fields = ("last", "bid", "ask", "quote_time", "chart_last_bar", "chain_header", "expirations")
    diff = [f"{f} {getattr(read, f)!r} != {getattr(dataset, f)!r}" for f in fields
            if getattr(dataset, f) is not None and getattr(read, f) != getattr(dataset, f)]
    if diff:
        return ControlResult("pinned_quote", False, GATE, outcomes=(SubsetOutcome(
            "frozen", Outcome("env_error", "data_epoch_mismatch", GATE)),), alert=alerts.env_error(),
            detail="pinned quote differs from the dataset: " + "; ".join(diff))
    return ControlResult("pinned_quote", True, GATE)


@dataclass(frozen=True)
class SymbolFacts:
    """The symbol pre-check of one fixture symbol (7.9): it exists, no split or symbol change since the last run, the
    expected expiration shape (None: not checkable, the app quote only reads existence)."""

    symbol_id: str
    exists: bool | None
    changed: bool = False
    expirations_ok: bool | None = True


def check_symbols(facts: Sequence[SymbolFacts], symbol_ids: Iterable[str], *, source: str) -> ControlResult:
    """7.9: the pre-check per fixture symbol; a failure (or a symbol with no facts) is `env_error (gate:
    fixture_verified, reason: fixture_maintenance_needed)` for the checkpoints using that symbol (`symbol:<id>`),
    posted to the flow owner and the accounts owner. `source` is `api`, or `app_quote` when N3 failed, which run
    health reports as "symbol pre-check: app quote only"."""
    if source not in ("api", "app_quote"):
        raise ValueError(f"symbol pre-check source {source!r}: api or app_quote (7.9)")
    by_id = {f.symbol_id: f for f in facts}
    bad: list[tuple[str, str]] = []
    for sid in symbol_ids:
        f = by_id.get(sid)
        why = ("not checked" if f is None else "unreadable" if f.exists is None else "does not exist" if not f.exists
               else "split or symbol change" if f.changed else "unexpected expiration shape"
               if f.expirations_ok is False else "")
        if why:
            bad.append((sid, why))
    health = "symbol pre-check: app quote only" if source == "app_quote" else None
    if bad:
        detail = "; ".join(f"{sid}: {why}" for sid, why in bad) + (f"; {health}" if health else "")
        return ControlResult("symbol_precheck", False, GATE, outcomes=tuple(
            SubsetOutcome(f"{SYMBOL_PREFIX}{sid}", Outcome("env_error", "fixture_maintenance_needed", GATE))
            for sid, _ in bad), alert=alerts.env_error(notified=("flow_owner", "accounts_owner")),
            actions=("maintenance_task",), detail=detail)
    return ControlResult("symbol_precheck", True, GATE, detail=health)


# -- stage 2 in order -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Stage2Reads:
    """Everything stage 2 read on the rig (7.6 stage 2, 7.7 step 8, 7.9, 7.11). `pinned` and `dataset` are the
    frozen Branch A pinned quote and its dataset values, required when `data_mode.frozen.provider` is
    `backend_fixture`."""

    accounts: tuple[AccountState, ...]
    ondemand_toolbar: bool
    delay_badge: DelayBadge
    connectivity: ConnectivityObservation
    reconnects_since_launch: int
    quote_age_s: float | None
    prices: PriceRecompute
    symbols: tuple[SymbolFacts, ...]
    symbol_source: str = "api"
    after_reset: tuple[AccountState, ...] | None = None
    after_cancel: bool = False
    pinned: EpochRead | None = None
    dataset: EpochRead | None = None


def check_stage2(reads: Stage2Reads, fixture: Mapping[str, Any], *, reseed_due_passed: bool,
                 paper_send_ok: bool = False, run_tag: str | None = None) -> tuple[ControlResult, ...]:
    """Stage 2's results in the order of choice 1, ending at the first that sets a run outcome or asks the caller to
    act (`reset_empty_account`, `cancel_all`: act, re-read and call again with `after_reset` or `after_cancel`).
    `reseed_due_passed` is `churn.reseed_due_passed(run_date, fixture)`."""
    frozen_a = ((fixture.get("data_mode") or {}).get("frozen") or {}).get("provider") == "backend_fixture"
    if frozen_a and (reads.pinned is None or reads.dataset is None):
        raise ValueError("frozen Branch A: the pinned quote check needs the read and the dataset values (7.9)")
    acct = check_fixture(reads.accounts, expectations(fixture), after_reset=reads.after_reset,
                         reseed_due_passed=reseed_due_passed, paper_send_ok=paper_send_ok, run_tag=run_tag,
                         after_cancel=reads.after_cancel)
    out = [acct]
    if acct.run is not None or acct.has("reset_empty_account") or acct.has("cancel_all"):
        return tuple(out)
    delay, minutes = data_delay(fixture)
    whole = [check_ondemand(reads.ondemand_toolbar), check_delay_state(reads.delay_badge, delay),
             check_connectivity(reads.connectivity, reconnects_since_launch=reads.reconnects_since_launch,
                                quote_age_s=reads.quote_age_s, badge=reads.delay_badge, delay=delay,
                                delay_minutes=minutes),
             check_alerts(reads.accounts, reads.after_reset, int(fixture.get("alerts_expected", 0)))]
    for r in whole:
        if r is None:
            continue
        out.append(r)
        if r.run is not None:
            return tuple(out)
    out.append(check_prices(reads.prices))
    if frozen_a:
        out.append(check_pinned_quote(reads.pinned, reads.dataset))       # type: ignore[arg-type]
    out.append(check_symbols(reads.symbols, [str(s["id"]) for s in fixture.get("symbols") or ()],
                             source=reads.symbol_source))
    return tuple(out)


# -- selectors (4.5, 4.9, L-FIX-08, 7.9) ---------------------------------------------------------------------
class ConfigView(Protocol):
    """What the selectors read from the loaded config (`pipeline.config.SentinelConfig` has it)."""

    surfaces: Mapping[str, Mapping[str, Any]]
    flow_docs: Mapping[str, Mapping[str, Any]]
    fixture: Mapping[str, Any]
    repo: Any                                     # lint.repo.Repo: the 4.6 workspace entries


class ScopedCheckpoint(Protocol):
    """The checkpoint fields the selectors read (`scope.PlannedCheckpoint` has them)."""

    checkpoint_id: str
    surface_id: str
    workspace_id: str
    data_mode: str
    flow_id: str | None
    step_id: str | None


def handles(selector: str) -> bool:
    """True for the selectors `select_subset` resolves."""
    return selector in SELECTORS or selector.startswith(SYMBOL_PREFIX)


def _walk(node: Any, key: str) -> Iterable[Any]:
    """Every value stored under `key` anywhere in a parsed YAML document."""
    if isinstance(node, Mapping):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def _step(cfg: ConfigView, cp: ScopedCheckpoint) -> Mapping[str, Any]:
    for st in (cfg.flow_docs.get(cp.flow_id or "") or {}).get("steps") or ():
        if st.get("id") == cp.step_id:
            return st
    return {}


def _workspaces(cfg: ConfigView) -> dict[str, Mapping[str, Any]]:
    return {d.data["id"]: d.data for d in cfg.repo.of("workspace") if isinstance(d.data, Mapping) and "id" in d.data}


def account_state(cfg: ConfigView, cp: ScopedCheckpoint) -> bool:
    """4.5, 4.7: the checkpoint step's `account_state` override, or else its surface's flag (choice 9)."""
    override = _step(cfg, cp).get("account_state") if cp.flow_id else None
    if isinstance(override, bool):
        return override
    return bool((cfg.surfaces.get(cp.surface_id) or {}).get("account_state", False))


def account_role(cfg: ConfigView, cp: ScopedCheckpoint, workspaces: Mapping[str, Mapping[str, Any]]) -> str:
    """The fixture account a checkpoint runs on (7.6, SD-75): a flow's `account_role` (default `empty`); a non-flow
    checkpoint's workspace `account_role` (default `golden`)."""
    flow = cfg.flow_docs.get(cp.flow_id or "") if cp.flow_id else None
    if flow is not None:
        return str(flow.get("account_role", "empty"))
    if cp.flow_id:                                     # a flow id with no flow file (the soak): the flow default
        return "empty"
    return str((workspaces.get(cp.workspace_id) or {}).get("account_role", "golden"))


def order_bearing(cfg: ConfigView, cp: ScopedCheckpoint) -> bool:
    """L-FIX-08, choice 7: a checkpoint of a flow that stages an order or types a fixture price."""
    flow = cfg.flow_docs.get(cp.flow_id or "") if cp.flow_id else None
    if not flow:
        return False
    return any(v is True for v in _walk(flow, "order_stage")) or \
        any(str(v).startswith("fixture.prices.") for v in _walk(flow, "value_ref"))


def uses_symbol(cfg: ConfigView, cp: ScopedCheckpoint, symbol_id: str,
                workspaces: Mapping[str, Mapping[str, Any]]) -> bool:
    """7.9, choice 8: the checkpoint's flow references the symbol, or its workspace links it."""
    prices = {str(p["id"]) for p in cfg.fixture.get("prices") or () if p.get("symbol_ref") == symbol_id}
    flow = cfg.flow_docs.get(cp.flow_id or "") if cp.flow_id else None
    for ref in _walk(flow or {}, "value_ref"):
        parts = str(ref).split(".")
        if parts[:3] == ["fixture", "symbols", symbol_id] or (parts[:2] == ["fixture", "prices"] and len(parts) > 2
                                                               and parts[2] in prices):
            return True
    links = (workspaces.get(cp.workspace_id) or {}).get("link_groups") or ()
    return any(g.get("symbol_ref") == symbol_id for g in links)


def select_subset(selector: str, checkpoints: Sequence[ScopedCheckpoint], cfg: ConfigView) -> list[ScopedCheckpoint]:
    """The checkpoints of `checkpoints` a `SubsetOutcome` selector of this package names (`handles`): `account_state`,
    `account_state:golden`, `order_bearing` or `symbol:<id>` (choices 7 to 9), in their order."""
    if not handles(selector):
        raise ValueError(f"selector {selector!r} is not one of {SELECTORS} or {SYMBOL_PREFIX}<id>")
    ws = _workspaces(cfg)
    if selector == "account_state":
        return [cp for cp in checkpoints if account_state(cfg, cp)]
    if selector == "account_state:golden":
        return [cp for cp in checkpoints if account_state(cfg, cp) and account_role(cfg, cp, ws) == "golden"]
    if selector == "order_bearing":
        return [cp for cp in checkpoints if order_bearing(cfg, cp)]
    sid = selector[len(SYMBOL_PREFIX):]
    return [cp for cp in checkpoints if uses_symbol(cfg, cp, sid, ws)]
