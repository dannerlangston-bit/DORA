"""Restart, re-login and run budgets (10.2, 7.7, 5.2, 6.4, 11.2).

- Per shard per run (10.2): 3 app relaunches, 2 Orchestrator restarts, 1 hook host restart, each its
  own budget. The first three app failures are relaunched and a fourth ends the run; a hook host
  restart never consumes an app relaunch.
- The re-login budget K (7.7, default 3 per run) counts environment re-logins only (POP-05 session
  expired, disconnected or reconnecting, and a reconnect that re-authenticates, 10.7); past it the run
  ends `complete_with_env_errors (reason: relogin_budget)` with the remaining checkpoints `env_error
  (gate: authenticated, reason: relogin_budget)`. Crash relaunches count against the 3 above and the
  proactive relaunch against neither (SD-66). A "logged in elsewhere" dialog (POP-04) is never a re-login:
  `env_error (gate: authenticated, reason: session_kicked)` for the rest of the shard.
- Run budgets (5.2): the wall budget less the report margin (`wall_clock`), the 6.4 rate caps on
  logins (40 per day), symbol changes (600 per hour) and staged orders (60 per hour, 300 per day)
  (`rate_cap`), and a newer RC build (`superseded`) end the run `aborted_budget` with the remaining
  checkpoints `not_run (reason: budget)`. The per-run model spend ceiling is degraded.py.
- The wall budget per run type is the 11.2 table: PR 45 minutes, merge 3 hours, RC 8 hours, Nightly 4 hours;
  a Calibration run's budget ends with its `all_quiet` window, or at 06:00 America/New_York for an emergency
  calibration (11.2), so its end is passed in. The run deadline is that budget minus the 10-minute report
  margin (11.2 "Inside a run").

Choices (reported):
- The wall budget is measured on the Orchestrator's monotonic clock from the run's start (`WallBudget`); a
  Calibration run converts its window end to seconds once, at the start.
- The rate caps are counted over rolling windows (the last hour, the last 24 hours) of wall-clock event times,
  since they span runs on one login and a rolling window is never laxer than a calendar day or hour; a cap is
  reached when its count equals it (6.4 "Reaching a cap ends the run"). Every login attempt counts, the
  run's own and its recovery logins included; earlier runs' events are seeded from the store.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import alerts
from .results import ControlResult, Outcome, RunVerdict, budget_abort_outcome

APP_RELAUNCHES = 3
ORCHESTRATOR_RESTARTS = 2
HOOK_HOST_RESTARTS = 1
RELOGIN_K = 3
SESSION_RELEASE_WAIT_S = 60.0                   # 7.7, measured in DR-06
RATE_CAPS = {"logins_per_day": 40, "symbol_changes_per_hour": 600, "orders_staged_per_hour": 60,
             "orders_staged_per_day": 300}
WALL_BUDGET_S = {"pr": 45 * 60.0, "merge": 3 * 3600.0, "rc": 8 * 3600.0, "nightly": 4 * 3600.0}   # 11.2
REPORT_MARGIN_S = 10 * 60.0                     # 11.2: diff, classification and report
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass
class ShardBudgets:
    """The three restart budgets of one shard (10.2); `take` spends one and says whether one was left."""

    app_relaunches: int = APP_RELAUNCHES
    orchestrator_restarts: int = ORCHESTRATOR_RESTARTS
    hook_host_restarts: int = HOOK_HOST_RESTARTS
    used_app: int = 0
    used_orchestrator: int = 0
    used_hook_host: int = 0

    def take(self, kind: str) -> bool:
        """Spend one `app`, `orchestrator` or `hook_host` restart; False when the budget was already spent."""
        limit = {"app": self.app_relaunches, "orchestrator": self.orchestrator_restarts,
                 "hook_host": self.hook_host_restarts}[kind]
        used = getattr(self, f"used_{kind}")
        if used >= limit:
            return False
        setattr(self, f"used_{kind}", used + 1)
        return True


@dataclass
class ReloginBudget:
    """The re-login budget K of 7.7 for one run."""

    k: int = RELOGIN_K
    used: int = 0

    def environment_relogin(self) -> ControlResult:
        """One environment re-login (POP-05, or a re-authenticating reconnect): allowed while K is not spent."""
        if self.used < self.k:
            self.used += 1
            return ControlResult("relogin", True, "authenticated",
                                 actions=("kill_tree", "reset_input_state", "wait_session_release", "relaunch",
                                          "resume_next_flow"),
                                 detail=f"environment re-login {self.used} of {self.k}; every gate passes again")
        return ControlResult("relogin", False, "authenticated",
                             run=RunVerdict("complete_with_env_errors", "relogin_budget"),
                             unreached=Outcome("env_error", "relogin_budget", "authenticated"),
                             alert=alerts.env_error(), detail=f"re-login budget K={self.k} spent")


def session_kicked() -> ControlResult:
    """POP-04 "logged in from another location": no re-login for the run (7.7, DR-06)."""
    return ControlResult("relogin", False, "authenticated", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "session_kicked", "authenticated"),
                         alert=alerts.env_error(notified=("accounts_owner",)),
                         detail="session kicked: the remaining checkpoints of the shard; the proof waits "
                                "session_release_wait and logs in once (6.6)")


# -- the wall budget (11.2) ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WallBudget:
    """One run's wall budget on the Orchestrator's monotonic clock: the budget, when the run started and the report
    margin; the run deadline is `started_mono + budget_s - margin_s` (11.2)."""

    budget_s: float
    started_mono: float
    margin_s: float = REPORT_MARGIN_S

    def elapsed(self, now_mono: float) -> float:
        """Seconds since the run started."""
        return now_mono - self.started_mono

    @property
    def deadline_mono(self) -> float:
        """The run deadline: from it on the run ends `aborted_budget (reason: wall_clock)`."""
        return self.started_mono + self.budget_s - self.margin_s

    @classmethod
    def for_run(cls, run_type: str, started_mono: float, *, started_at: datetime | None = None,
                ends_at: datetime | None = None) -> "WallBudget":
        """The 11.2 budget of `run_type` from `started_mono`; a Calibration run passes `started_at` and `ends_at`
        (the end of its `all_quiet` window, or 06:00 America/New_York for an emergency calibration)."""
        return cls(wall_budget_s(run_type, started_at=started_at, ends_at=ends_at), started_mono)


def wall_budget_s(run_type: str, *, started_at: datetime | None = None, ends_at: datetime | None = None) -> float:
    """The wall budget of a run type in seconds (11.2 table)."""
    if run_type == "calibration":
        if started_at is None or ends_at is None:
            raise ValueError("a Calibration run's budget ends with its all_quiet window (06:00 America/New_York for "
                             "an emergency calibration): pass started_at and ends_at (11.2)")
        if ends_at <= started_at:
            raise ValueError(f"calibration window ends {ends_at.isoformat()}, before the run starts")
        return (ends_at - started_at).total_seconds()
    if run_type not in WALL_BUDGET_S:
        raise ValueError(f"no wall budget for run type {run_type!r} (11.2)")
    return WALL_BUDGET_S[run_type]


# -- the rate caps (6.4) ------------------------------------------------------------------------------------------
@dataclass
class RateCounters:
    """The 6.4 rate-cap counters of one login: the wall times of its login attempts, symbol changes and staged
    orders. Seed the lists with the login's earlier events from the store; `caps` are the authorization
    document's values (6.12), RATE_CAPS until one is loaded."""

    logins: list[datetime] = field(default_factory=list)
    symbol_changes: list[datetime] = field(default_factory=list)
    orders_staged: list[datetime] = field(default_factory=list)
    caps: Mapping[str, int] = field(default_factory=lambda: dict(RATE_CAPS))

    def login(self, at: datetime) -> None:
        """One login attempt on the login id: the shard's LOGIN flow, a relaunch login or a recovery login."""
        self.logins.append(at)

    def symbol_change(self, at: datetime) -> None:
        """One symbol-box `type_fixture` step executed."""
        self.symbol_changes.append(at)

    def order_staged(self, at: datetime) -> None:
        """One order-stage step executed."""
        self.orders_staged.append(at)

    def counts(self, now: datetime) -> dict[str, int]:
        """The count behind each cap of RATE_CAPS over its rolling window ending at `now` (module docstring)."""
        def within(events: list[datetime], window: timedelta) -> int:
            return sum(1 for t in events if now - window < t <= now)
        return {"logins_per_day": within(self.logins, DAY),
                "symbol_changes_per_hour": within(self.symbol_changes, HOUR),
                "orders_staged_per_hour": within(self.orders_staged, HOUR),
                "orders_staged_per_day": within(self.orders_staged, DAY)}


def run_budget(*, now_s: float, wall_budget_s: float, report_margin_s: float, counters: dict[str, int] | None = None,
               superseded: bool = False, caps: Mapping[str, int] | None = None) -> ControlResult | None:
    """The run budgets of 5.2 at a flow boundary: None while every budget holds, else the `aborted_budget`.

    `now_s` is the time since the run started; `caps` default to RATE_CAPS (6.4)."""
    if superseded:
        return _spent("superseded")
    if now_s >= wall_budget_s - report_margin_s:
        return _spent("wall_clock")
    return rate_cap(counters or {}, caps)


def rate_cap(counters: Mapping[str, int], caps: Mapping[str, int] | None = None) -> ControlResult | None:
    """The 6.4 rate caps alone: `aborted_budget (reason: rate_cap)` when a count has reached its cap, else None. The
    Orchestrator checks it at every flow boundary and right before every event that counts toward a cap (a login, an
    A9 drill ticket, an order-stage step), so the event past the cap is never made."""
    caps = RATE_CAPS if caps is None else caps
    hit = [k for k, cap in caps.items() if counters.get(k, 0) >= cap]
    if not hit:
        return None
    return _spent("rate_cap", ", ".join(f"{k} {counters.get(k, 0)} of {caps[k]}" for k in hit))


def _spent(reason: str, detail: str | None = None) -> ControlResult:
    run, un = budget_abort_outcome(reason)
    return ControlResult("run_budget", False, "running", run=run, unreached=un, alert=alerts.aborted_budget(reason),
                         actions=("teardown",), detail=f"run budget spent: {reason}" + (f" ({detail})" if detail else ""))
