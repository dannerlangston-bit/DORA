"""The chaos suite of the RC run (7.11 Network preflight and chaos suite, 1.9, 11.2): scenarios, scheduling rules,
the assertion evaluators and the runner over the `ports.TrafficShaper` port.

Schedule. RC runs only, on rig 6 from T+0:20 to T+1:00, before rig 6 is re-snapshotted for the Explorer; tier-2 flows
owned by the connectivity flow owner, never in a session with census checkpoints, and `requires_window: all_quiet`
because the assertions read quote staleness (`refusal`; `windowed_items` feeds `calendar.plan_windowed`, whose
refusals are `not_run (reason: budget)` and are passed back as `skip`).

Scenarios (`SCENARIOS`): 500 ms added latency, 5 percent loss, a 256 kbit/s throttle, and 30 second and 5 minute
disconnects (a firewall block on the launcher: a `blackhole` rule), with a reconnect after each. The shaping itself
(clumsy over WinDivert inside the VM, the firewall rule, the elevated helper service) is the Windows track's
`TrafficShaper`; `SimulatedShaper` stands in for it here.

Assertions by Oracle and template (`evaluate`), each an assertion id on the scenario's checkpoint:
- `chaos.indicator`: the connection indicator changes within the declared time (disconnects);
- `chaos.quotes_stale`: quote timestamps stop advancing and the quotes are badged stale rather than silently frozen
  (a disconnect must stop them; under a degradation, quotes that stopped must carry the badge);
- `chaos.error_state`: a symbol change or chain load during the disconnect shows an explicit error state, not an
  empty grid (disconnects);
- `chaos.quotes_resume`, `chaos.no_duplicates`, `chaos.no_pop10`: after the reconnect quotes resume, Monitor and
  Working Orders reload without duplicate rows, and no POP-10 appeared (every scenario).
There is no assertion about cancelling a staged order during the disconnect (7.11).

Each scenario ends with the rule removed and verified gone: a rule still present is `harness_error (reason:
chaos_rule_stuck)` and the helper service removes all rules (`TrafficShaper.clear` again); then the connectivity
check and `verify_account_fixture`. A scenario the harness did not observe (no observation, the helper never saw its
rule take effect, or a declared observation missing) is `harness_error (reason: chaos_unobserved)`, never a product
failure; failed assertions are `assertion_failed (assertion: <id>)` with every failing id, tier 2, owned by the
connectivity flow owner.

Choices (reported):
1. Removal goes through the port's `clear()`: the port removes every rule, and the suite applies one rule at a time,
   so removing the scenario's rule is removing all; a rule `active()` still lists afterwards is stuck, and the
   helper's remove-all is a second `clear()`.
2. The declared indicator time is 15 s, and a degradation scenario holds its rule 120 s.
3. "A scenario without its expected outcome and alert" is read as a scenario the harness could not observe:
   `ChaosObservation.rule_effective` (the helper confirmed the rule in force and its probe measured the effect) is
   False, the observation is missing, or an observation the scenario's assertions read is None.
4. A rule that survives the remove-all leaves the rig shaped: the remaining scenarios are `harness_error (reason:
   chaos_rule_stuck)` too and `ChaosSuiteResult.rig_clean` is False (the Explorer must not start on that rig).
5. A failed connectivity check after a scenario is `env_error (gate: connectivity, reason: backend_degraded)` and a
   failed `verify_account_fixture` is `env_error (gate: fixture_verified, reason: fixture_drift)` for every scenario
   not yet run; the scenario just run keeps its own outcome.
6. A scenario whose assertions all pass has no outcome from this module (`ChaosResult.outcome` None): its checkpoint
   capture goes through the normal diff path.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..calendar import WindowedItem
from ..records import CheckpointOutcome
from . import alerts
from .alerts import AlertRoute
from .ports import ShapingRule, TrafficShaper
from .results import FindingSpec, Outcome

CHAOS_TIER = 2                                     # 7.11: tier-2 flows owned by the connectivity flow owner
CHAOS_WINDOW = "all_quiet"                         # 7.11: requires_window
INDICATOR_WITHIN_S = 15.0                          # choice 2
DEGRADE_S = 120.0                                  # choice 2
CHECKPOINT_STEP = "reconnected"
ASSERTIONS = ("chaos.indicator", "chaos.quotes_stale", "chaos.error_state", "chaos.quotes_resume",
              "chaos.no_duplicates", "chaos.no_pop10")
AFTER_RECONNECT = ("chaos.quotes_resume", "chaos.no_duplicates", "chaos.no_pop10")
GRIDS = ("monitor", "working_orders")              # 7.11: Monitor and Working Orders reload without duplicate rows


@dataclass(frozen=True)
class ChaosScenario:
    """One scenario of 7.11: its slug, the shaping kind (`latency`, `loss`, `throttle`, `blackhole`), the value (ms,
    loss fraction, kbit/s), how long the rule holds, and whether it is a disconnect."""

    name: str
    kind: str
    value: float
    duration_s: float
    disconnect: bool = False

    @property
    def flow_id(self) -> str:
        return f"flow.chaos.{self.name}"

    def rule(self, host: str) -> ShapingRule:
        return ShapingRule(self.kind, host, self.value, self.duration_s)

    def checkpoint_id(self, surface_id: str) -> str:
        return f"flow/{surface_id}/{self.flow_id}/{CHECKPOINT_STEP}"

    @property
    def assertions(self) -> tuple[str, ...]:
        """The assertion ids this scenario evaluates."""
        return ASSERTIONS if self.disconnect else ("chaos.quotes_stale",) + AFTER_RECONNECT


SCENARIOS: tuple[ChaosScenario, ...] = (
    ChaosScenario("latency-500ms", "latency", 500.0, DEGRADE_S),
    ChaosScenario("loss-5pct", "loss", 0.05, DEGRADE_S),
    ChaosScenario("throttle-256kbps", "throttle", 256.0, DEGRADE_S),
    ChaosScenario("disconnect-30s", "blackhole", 0.0, 30.0, disconnect=True),
    ChaosScenario("disconnect-5min", "blackhole", 0.0, 300.0, disconnect=True),
)


def refusal(run_type: str, session_kinds: Iterable[str]) -> str | None:
    """Why a chaos flow may not be scheduled in this session (7.11), or None: RC runs only, never beside census."""
    if run_type != "rc":
        return f"the chaos suite runs in RC runs only, not in a {run_type} run (7.11, 1.9)"
    if "census" in set(session_kinds):
        return "the chaos suite never runs in a session with census checkpoints (7.11)"
    return None


@dataclass(frozen=True)
class ChaosPlan:
    """Where the suite's checkpoints live: the connectivity surface, the rig 6 workspace and persona, the host the
    rules shape (the login and market-data host), and the declared indicator time."""

    surface_id: str
    workspace_id: str
    persona: str
    host: str
    indicator_within_s: float = INDICATOR_WITHIN_S
    data_mode: str = "masked_live"


def windowed_items(plan: ChaosPlan, scenarios: Sequence[ChaosScenario] = SCENARIOS) -> list[WindowedItem]:
    """`calendar.WindowedItem`s of the scenarios (requires_window `all_quiet`), for `calendar.plan_windowed`."""
    return [WindowedItem(s.flow_id, CHAOS_WINDOW, checkpoints=(s.checkpoint_id(plan.surface_id),),
                         max_duration_s=s.duration_s + 60.0) for s in scenarios]


@dataclass(frozen=True)
class ChaosObservation:
    """What the Oracle and the templates read during and after one scenario (7.11)."""

    rule_effective: bool
    indicator_change_s: float | None = None        # seconds from the rule to the indicator leaving Connected
    quotes_stopped: bool | None = None             # quote timestamps stopped advancing during the rule
    stale_badge: bool | None = None                # the quotes carried the stale badge while stopped
    error_state: bool | None = None                # a symbol change or chain load showed an explicit error state
    quotes_resumed: bool | None = None             # after the reconnect
    duplicate_rows: Mapping[str, int] = field(default_factory=dict)   # grid -> duplicate rows after the reload
    pop10_seen: bool | None = None


def unobserved(scenario: ChaosScenario, obs: ChaosObservation | None) -> str | None:
    """Why the harness did not observe the scenario (choice 3), or None."""
    if obs is None:
        return "no observation was recorded"
    if not obs.rule_effective:
        return "the helper never saw the rule take effect"
    needs = {"chaos.quotes_stale": ("quotes_stopped",), "chaos.quotes_resume": ("quotes_resumed",),
             "chaos.no_pop10": ("pop10_seen",)}
    if scenario.disconnect:
        needs |= {"chaos.quotes_stale": ("quotes_stopped", "stale_badge"), "chaos.error_state": ("error_state",)}
    missing = [f for a in scenario.assertions for f in needs.get(a, ()) if getattr(obs, f) is None]
    if "chaos.no_duplicates" in scenario.assertions and any(g not in obs.duplicate_rows for g in GRIDS):
        missing.append("duplicate_rows")
    if scenario.disconnect and obs.indicator_change_s is None and obs.quotes_stopped is None:
        missing.append("indicator_change_s")
    return f"missing {', '.join(dict.fromkeys(missing))}" if missing else None


def evaluate(scenario: ChaosScenario, obs: ChaosObservation, *, indicator_within_s: float = INDICATOR_WITHIN_S
             ) -> list[tuple[str, str]]:
    """The failing assertions of one observed scenario, in `ASSERTIONS` order: (assertion id, detail)."""
    out: list[tuple[str, str]] = []
    if scenario.disconnect:
        t = obs.indicator_change_s
        if t is None or t > indicator_within_s:
            out.append(("chaos.indicator", f"the connection indicator changed after {t} s; declared "
                                           f"{indicator_within_s:g} s" if t is not None else
                        "the connection indicator never changed"))
        if not obs.quotes_stopped:
            out.append(("chaos.quotes_stale", "quote timestamps kept advancing during the disconnect"))
        elif not obs.stale_badge:
            out.append(("chaos.quotes_stale", "quotes stopped without the stale badge (silently frozen)"))
        if not obs.error_state:
            out.append(("chaos.error_state", "a symbol change during the disconnect showed no explicit error state"))
    elif obs.quotes_stopped and not obs.stale_badge:
        out.append(("chaos.quotes_stale", "quotes stopped without the stale badge (silently frozen)"))
    if not obs.quotes_resumed:
        out.append(("chaos.quotes_resume", "quotes did not resume after the reconnect"))
    dups = {g: n for g, n in sorted(obs.duplicate_rows.items()) if n}
    if dups:
        out.append(("chaos.no_duplicates", "duplicate rows after the reload: "
                    + ", ".join(f"{g} {n}" for g, n in dups.items())))
    if obs.pop10_seen:
        out.append(("chaos.no_pop10", "POP-10 appeared after the reconnect"))
    return out


class ChaosDriver(Protocol):
    """The rig 6 side of a scenario: the scenario's flow steps and reads while the rule holds, and the checks after."""

    def observe(self, scenario: ChaosScenario, rule: ShapingRule) -> ChaosObservation | None:
        """Drive the scenario with the rule in force, wait for the reconnect, return the reads."""

    def connectivity_ok(self) -> bool:
        """The post-login connectivity check of 7.11."""

    def fixture_ok(self) -> bool:
        """`verify_account_fixture` (7.6)."""


@dataclass(frozen=True)
class ChaosResult:
    """One scenario's result: its outcome (None when its checkpoint goes through the normal path, choice 6), every
    failing assertion id, the lower-precedence results kept as evidence (9.1), the findings, the alert, whether the
    helper's remove-all ran, and the detail."""

    scenario: ChaosScenario
    checkpoint_id: str
    outcome: Outcome | None
    assertion_ids: tuple[str, ...] = ()
    lower_results: tuple[Mapping[str, str], ...] = ()
    findings: tuple[FindingSpec, ...] = ()
    alert: AlertRoute | None = None
    removed_all: bool = False
    detail: str | None = None

    def record(self, run_id: str, plan: ChaosPlan) -> CheckpointOutcome | None:
        """The scenario's checkpoint outcome record, or None when the normal path decides (choice 6)."""
        if self.outcome is None:
            return None
        o = self.outcome
        return CheckpointOutcome(
            run_id=run_id, checkpoint_id=self.checkpoint_id, outcome=o.outcome, reason=o.reason, gate=o.gate,
            detail=o.detail, workspace_id=plan.workspace_id, persona=plan.persona, data_mode=plan.data_mode,
            tier=CHAOS_TIER, flow_id=self.scenario.flow_id, step_id=CHECKPOINT_STEP,
            assertion_ids=list(self.assertion_ids) or None, lower_results=[dict(x) for x in self.lower_results] or None,
            side="candidate")

    def report_row(self) -> dict[str, object]:
        """The RC report's connectivity section row (9.4)."""
        o = self.outcome
        return {"scenario": self.scenario.name, "flow_id": self.scenario.flow_id, "checkpoint_id": self.checkpoint_id,
                "outcome": o.outcome if o else "passed", "reason": o.reason if o else None,
                "failed_assertions": list(self.assertion_ids), "detail": self.detail,
                "removed_all_rules": self.removed_all}


@dataclass(frozen=True)
class ChaosSuiteResult:
    """The whole suite: one result per scenario, whether the rig ended with no rule in force (choice 4), and the
    run-health lines."""

    results: tuple[ChaosResult, ...]
    rig_clean: bool = True
    health: tuple[str, ...] = ()

    def records(self, run_id: str, plan: ChaosPlan) -> list[CheckpointOutcome]:
        return [r for r in (x.record(run_id, plan) for x in self.results) if r is not None]


def _assertion_result(scenario: ChaosScenario, cid: str, fails: Sequence[tuple[str, str]]) -> ChaosResult:
    ids = tuple(a for a, _ in fails)
    o = Outcome("assertion_failed", f"assertion:{ids[0]}", detail="; ".join(d for _, d in fails))
    finding = FindingSpec("assertion_failed", f"chaos scenario {scenario.name}: " + "; ".join(d for _, d in fails),
                          tier=CHAOS_TIER, detail=",".join(ids))
    return ChaosResult(scenario, cid, o, ids, findings=(finding,), detail=o.detail)


def run_chaos(shaper: TrafficShaper, driver: ChaosDriver, plan: ChaosPlan,
              scenarios: Sequence[ChaosScenario] = SCENARIOS, *,
              skip: Mapping[str, Outcome] | None = None) -> ChaosSuiteResult:
    """Run the suite (module docstring). `skip` maps a scenario's checkpoint id to the outcome it gets instead of
    running (the window scheduler's `not_run (reason: budget)`)."""
    skip = skip or {}
    out: list[ChaosResult] = []
    health: list[str] = []
    stop: Outcome | None = None
    clean = True
    for sc in scenarios:
        cid = sc.checkpoint_id(plan.surface_id)
        if stop is not None:
            out.append(ChaosResult(sc, cid, stop, detail=stop.detail))
            continue
        if cid in skip:
            out.append(ChaosResult(sc, cid, skip[cid], detail=skip[cid].detail))
            continue
        rule = sc.rule(plan.host)
        shaper.apply(rule)
        obs = driver.observe(sc, rule)
        shaper.clear()
        why = unobserved(sc, obs)
        fails = evaluate(sc, obs, indicator_within_s=plan.indicator_within_s) if why is None and obs else []
        if shaper.active():
            shaper.clear()                                       # the helper service removes all rules (7.11)
            clean = not shaper.active()
            lower = tuple({"outcome": "assertion_failed", "reason": f"assertion:{a}", "detail": d} for a, d in fails)
            o = Outcome("harness_error", "chaos_rule_stuck",
                        detail=f"the {sc.kind} rule was still in force after removal; the helper removed all rules")
            out.append(ChaosResult(sc, cid, o, tuple(a for a, _ in fails), lower,
                                   (FindingSpec("harness_error", o.detail or ""),), alerts.harness_error(), True,
                                   o.detail))
            health.append(f"chaos {sc.name}: rule stuck, all rules removed"
                          + ("" if clean else "; rules still in force, the rig stays shaped"))
            if not clean:
                stop = Outcome("harness_error", "chaos_rule_stuck", detail="an earlier rule could not be removed")
                continue
        elif why is not None:
            o = Outcome("harness_error", "chaos_unobserved", detail=f"chaos scenario {sc.name}: {why}")
            out.append(ChaosResult(sc, cid, o, findings=(FindingSpec("harness_error", o.detail or ""),),
                                   alert=alerts.harness_error(), detail=o.detail))
        elif fails:
            out.append(_assertion_result(sc, cid, fails))
        else:
            out.append(ChaosResult(sc, cid, None))
        if not driver.connectivity_ok():
            stop = Outcome("env_error", "backend_degraded", "connectivity",
                           detail=f"the connectivity check failed after chaos scenario {sc.name}")
        elif not driver.fixture_ok():
            stop = Outcome("env_error", "fixture_drift", "fixture_verified",
                           detail=f"verify_account_fixture failed after chaos scenario {sc.name}")
        if stop is not None:
            health.append(stop.detail or "")
    return ChaosSuiteResult(tuple(out), clean, tuple(health))


# -- simulation ------------------------------------------------------------------------------------------------------
@dataclass
class SimulatedShaper:
    """A `TrafficShaper` for tests and the simulated rig: `stuck_clears` is how many `clear()` calls leave the rules
    in force; `clears` and `applied` record what was asked."""

    stuck_clears: int = 0
    rules: list[ShapingRule] = field(default_factory=list)
    applied: list[ShapingRule] = field(default_factory=list)
    clears: int = 0

    def apply(self, rule: ShapingRule) -> None:
        self.rules.append(rule)
        self.applied.append(rule)

    def clear(self) -> None:
        self.clears += 1
        if self.stuck_clears > 0:
            self.stuck_clears -= 1
            return
        self.rules.clear()

    def active(self) -> tuple[ShapingRule, ...]:
        return tuple(self.rules)


def healthy(scenario: ChaosScenario) -> ChaosObservation:
    """What a well-behaved client shows under a scenario (the simulated rig's default)."""
    return ChaosObservation(rule_effective=True, indicator_change_s=3.0 if scenario.disconnect else None,
                            quotes_stopped=scenario.disconnect, stale_badge=scenario.disconnect,
                            error_state=True if scenario.disconnect else None, quotes_resumed=True,
                            duplicate_rows={g: 0 for g in GRIDS}, pop10_seen=False)


@dataclass
class SimulatedChaosDriver:
    """A `ChaosDriver` that returns `healthy` reads unless `observations` overrides a scenario (by name; None means no
    observation); `connectivity` and `fixture` are the post-scenario checks' answers."""

    observations: Mapping[str, ChaosObservation | None] = field(default_factory=dict)
    connectivity: bool = True
    fixture: bool = True
    observed: list[str] = field(default_factory=list)

    def observe(self, scenario: ChaosScenario, rule: ShapingRule) -> ChaosObservation | None:
        self.observed.append(scenario.name)
        return self.observations[scenario.name] if scenario.name in self.observations else healthy(scenario)

    def connectivity_ok(self) -> bool:
        return self.connectivity

    def fixture_ok(self) -> bool:
        return self.fixture
