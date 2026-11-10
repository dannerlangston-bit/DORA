"""Restart, re-login and run budgets (10.2, 7.7, 5.2, 6.4, 11.2).

- Per shard per run (10.2): 3 app relaunches, 2 Orchestrator restarts, 1 hook host restart, each its
  own budget. The first three app failures are relaunched and a fourth ends the run; a hook host
  restart never consumes an app relaunch.
- The re-login budget K (7.7, default 3 per run) counts environment re-logins only (POP-05 session
  expired, disconnected or reconnecting, and a reconnect that re-authenticates, 10.7); past it the run
  ends `complete_with_env_errors (reason: relogin_budget)` with the remaining checkpoints `env_error
  (gate: authenticated, reason: relogin_budget)`. Crash relaunches count against the 3 above and the
  proactive relaunch against neither. A "logged in elsewhere" dialog (POP-04) is never a re-login:
  `env_error (gate: authenticated, reason: session_kicked)` for the rest of the shard.
- Run budgets (5.2): the wall budget less the report margin (`wall_clock`), the 6.4 rate caps on
  logins (40 per day), symbol changes (600 per hour) and staged orders (60 per hour, 300 per day)
  (`rate_cap`), and a newer RC build (`superseded`) end the run `aborted_budget` with the remaining
  checkpoints `not_run (reason: budget)`. The per-run model spend ceiling is degraded.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import alerts
from .results import ControlResult, Outcome, RunVerdict, budget_abort_outcome

APP_RELAUNCHES = 3
ORCHESTRATOR_RESTARTS = 2
HOOK_HOST_RESTARTS = 1
RELOGIN_K = 3
SESSION_RELEASE_WAIT_S = 60.0                   # 7.7, measured in DR-06
RATE_CAPS = {"logins_per_day": 40, "symbol_changes_per_hour": 600, "orders_staged_per_hour": 60,
             "orders_staged_per_day": 300}


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


def run_budget(*, now_s: float, wall_budget_s: float, report_margin_s: float, counters: dict[str, int] | None = None,
               superseded: bool = False) -> ControlResult | None:
    """The run budgets of 5.2 at a flow boundary: None while every budget holds, else the `aborted_budget`."""
    reason = None
    if superseded:
        reason = "superseded"
    elif now_s >= wall_budget_s - report_margin_s:
        reason = "wall_clock"
    elif counters and any(counters.get(k, 0) >= cap for k, cap in RATE_CAPS.items()):
        reason = "rate_cap"
    if reason is None:
        return None
    run, un = budget_abort_outcome(reason)
    return ControlResult("run_budget", False, "running", run=run, unreached=un, alert=alerts.aborted_budget(reason),
                         actions=("teardown",), detail=f"run budget spent: {reason}")
