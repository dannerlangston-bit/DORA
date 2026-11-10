"""Watchdog decisions and the Orchestrator's heartbeat (10.2; 5.2, 5.4.2, 10.1; F27, F36, F44, F62).

The Watchdog is a separate process that emits no input and never writes SQLite; these are the
platform-independent decisions it and the Orchestrator take:

- Heartbeat staleness: over 120 s is a hung Orchestrator, over 5 minutes while `provisioned` or
  `launched` (installs and launches are slow). The reverse heartbeat `watchdog.json` older than 30 s or
  a dead Watchdog PID ends the run `aborted_harness (reason: watchdog_dead)` after the step in progress
  and teardown.
- EDT freeze by screenshot staleness, the primary hang detector: with `expect_screen_change_since` set,
  the window hash unchanged since then, no anti-anchor on screen and `max(2 x max_wait_s, 60 s)`
  elapsed. No screenshot is taken while `capture_suspended` is true (secret steps).
- App failures (crash, hang, OOM, process exit) consume the 3 app relaunches per shard; the fourth
  ends the run `aborted_budget (reason: app_restart_budget)` with the unreached checkpoints `not_run
  (reason: budget)`. The first launch of a shard is no exception (F62): an app_alive failure before
  `launched` passes or anywhere in the first gate sequence is `assertion_failed (assertion: app_alive)`
  on the shard's first checkpoint with the `app_crash` finding and one relaunch consumed, never
  `env_error`. A `process_exit` with a launcher update or a changed build manifest is `env_error
  (gate: launched, reason: version_changed_mid_run)` for the rest of the shard, no finding, no relaunch
  counted (F44).
- An Orchestrator death consumes one of 2 restarts; spent, `aborted_harness (reason:
  orchestrator_restart_budget)`. A hook host death consumes its own budget of 1; the second ends the run
  `aborted_harness (reason: hook_lost)`, as does a shared-memory `magic` or `layout_version` mismatch.
- The dead-man finalize: a run that reached `running` and missed its deadline plus grace is
  `aborted_harness (reason: dead_man)` with `harness_error (reason: dead_man)` on every checkpoint without
  an outcome; a run still before `running` is a failure of the gate in force, `env_error (gate: <state>,
  reason: timeout)`, run `complete_with_env_errors`. A run that never reached `running` is never
  `dead_man`.
- Detector precedence when two fire in one step (F27): `env_error`, then `assertion_failed (assertion:
  app_alive)`, then `harness_error`.

Choice (reported): heartbeat staleness in the gates between `authenticated` and `canary_flow_passed`
uses the 120 s threshold of `running`, since 10.2 names the 5-minute allowance for `provisioned` and
`launched` only.
"""
from __future__ import annotations

import errno
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import alerts
from .budgets import ShardBudgets
from .clock import Clock
from .results import (ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, budget_abort_outcome,
                      harness_abort_outcome)
from .rig import CrashSignal

HEARTBEAT_STALE_S = 120.0
HEARTBEAT_STALE_SLOW_S = 300.0
SLOW_STATES = ("provisioned", "launched")
WATCHDOG_STALE_S = 30.0
HANG_FLOOR_S = 60.0
DISK_ABORT_BYTES = 2 * 1024 ** 3
PROACTIVE_FLOWS = 40
PROACTIVE_UPTIME_S = 90 * 60.0
DEAD_MAN_GRACE = {"pr": timedelta(minutes=15), "merge": timedelta(minutes=30), "rc": timedelta(minutes=60)}
APP_CAUSES = ("jvm_crash", "hang", "oom", "process_exit")


# -- heartbeats ---------------------------------------------------------------------------------------
def orchestrator_hung(state: str, heartbeat_age_s: float) -> bool:
    """10.2 sampling 1: the Orchestrator heartbeat is stale for the state it last wrote."""
    return heartbeat_age_s > (HEARTBEAT_STALE_SLOW_S if state in SLOW_STATES else HEARTBEAT_STALE_S)


def watchdog_check(age_s: float, pid_alive: bool) -> ControlResult | None:
    """The Orchestrator's read of `watchdog.json` at every gate and flow boundary; None while healthy."""
    if age_s <= WATCHDOG_STALE_S and pid_alive:
        return None
    run, un = harness_abort_outcome("watchdog_dead")
    return ControlResult("watchdog", False, "running", run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=("teardown",), detail="no dead-man coverage and no kill-switch second half; "
                                                       "finish the step in progress, tear down")


@dataclass(frozen=True)
class Heartbeat:
    """The heartbeat JSON of 10.2, written atomically at every step start and end, every gate, every 10 s."""

    run_id: str
    shard: int
    attempt: int
    state: str
    ts: str
    mono_ts: float
    flow_id: str | None = None
    step_id: str | None = None
    checkpoint_id: str | None = None
    app_pid: int | None = None
    orchestrator_pid: int | None = None
    hook_host_pid: int | None = None
    expect_screen_change_since: str | None = None
    max_wait_s: float | None = None
    capture_suspended: bool = False
    last_capture_sha256: str | None = None


class HeartbeatUnwritable(OSError):
    """The heartbeat write failed twice, 1 s apart (10.2)."""


class HeartbeatWriter:
    """Writes `heartbeat.json` atomically (temp file, then rename); one retry after 1 s, then raises."""

    def __init__(self, path: str | Path, clock: Clock):
        self.path = Path(path)
        self.clock = clock

    def write(self, hb: Heartbeat) -> None:
        body = json.dumps(asdict(hb), sort_keys=True)
        for attempt in (1, 2):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, self.path)
                return
            except OSError as e:
                if attempt == 2:
                    raise HeartbeatUnwritable(e.errno, f"heartbeat write failed twice: {e}") from e
                self.clock.sleep(1.0)


def heartbeat_failed(err: OSError) -> ControlResult:
    """A heartbeat write that failed twice (10.2): `ENOSPC` asks for the free-space check (the `disk_full` case),
    anything else signals `self_abort` and ends the run `aborted_harness (reason: heartbeat_unwritable)`."""
    if err.errno == errno.ENOSPC:
        return ControlResult("heartbeat", False, "running", actions=("free_space_check",),
                             detail="ENOSPC: the Watchdog's free-space check decides disk_full")
    run, un = harness_abort_outcome("heartbeat_unwritable")
    return ControlResult("heartbeat", False, "running", run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=("self_abort", "teardown"), detail="teardown with capture disabled")


def disk_check(free_bytes: int, floor_bytes: int) -> ControlResult | None:
    """The Watchdog's mid-run free-space check (10.2, F35): below 2 GB ends the run `disk_full`; below the rig
    floor is a run-health warning; None above the floor."""
    if free_bytes < DISK_ABORT_BYTES:
        run, un = harness_abort_outcome("disk_full")
        return ControlResult("disk", False, "running", run=run, unreached=un, alert=alerts.aborted_harness(),
                             actions=("teardown",), detail="stop capturing and tear down")
    if free_bytes < floor_bytes:
        return ControlResult("disk", True, "running", detail="free space below the rig floor (run-health warning)")
    return None


# -- app failures ---------------------------------------------------------------------------------------
def edt_frozen(*, now: float, expect_change_since: float | None, last_hash_change: float, max_wait_s: float,
               anti_anchor: bool, capture_suspended: bool) -> bool:
    """10.2 sampling 5: the screenshot-staleness hang detector."""
    if capture_suspended or expect_change_since is None or anti_anchor:
        return False
    if last_hash_change > expect_change_since:
        return False
    return now - expect_change_since >= max(2 * max_wait_s, HANG_FLOOR_S)


def crash_signal(signals: Sequence[CrashSignal]) -> CrashSignal | None:
    """The signal that classifies an app failure: an OOM line or heap dump makes the cause `oom` even when the
    process survives (10.2 sampling 6); otherwise the first signal."""
    if not signals:
        return None
    return next((s for s in signals if s.cause == "oom"), signals[0])


def app_failure(sig: CrashSignal, budgets: ShardBudgets, *, phase: str) -> ControlResult:
    """One app failure (10.1, 10.2, F36, F44, F62). `phase` is `first_launch`, `relaunch_gates` (a gate sequence
    of the shard), `running` or `teardown`."""
    if sig.cause not in APP_CAUSES:
        raise ValueError(f"app_alive cause {sig.cause!r}")
    if phase not in ("first_launch", "relaunch_gates", "running", "teardown"):
        raise ValueError(f"phase {phase!r}")
    if sig.cause == "process_exit" and (sig.launcher_update or sig.build_manifest_changed):
        o = Outcome("env_error", "version_changed_mid_run", "launched")
        return ControlResult("watchdog", False, "launched", run=RunVerdict("complete_with_env_errors"), checkpoint=o,
                             unreached=o, alert=alerts.env_error(),
                             detail="launcher update or build manifest change: no relaunch counted, no finding")
    if phase == "teardown":
        if budgets.take("app"):
            return ControlResult("watchdog", False, "teardown", actions=("relaunch", "recovery_login"),
                                 detail="app died in teardown: one relaunch re-attempts teardown (10.2)")
        return ControlResult("watchdog", False, "teardown",
                             detail="app died in teardown with no relaunch left: the proof is UNPROVEN (6.6)")
    alive = Outcome("assertion_failed", "assertion:app_alive", detail=sig.cause)
    finding = FindingSpec("app_crash", f"thinkorswim {sig.cause} ({sig.source})", tier=1, detail=sig.cause)
    if phase == "running":
        subset = (SubsetOutcome("in_progress", alive), SubsetOutcome("rest_of_flow", Outcome("not_run", "app_crash")))
    else:
        subset = (SubsetOutcome("first_unreached", alive),)
    gate = "running" if phase == "running" else "launched"
    if budgets.take("app"):
        return ControlResult("watchdog", False, gate, checkpoint=alive, outcomes=subset, findings=(finding,),
                             actions=("reset_input_state", "kill_tree", "wait_session_release", "relaunch",
                                      "resume_next_flow"), step="failed",
                             detail=f"app relaunch {budgets.used_app} of {budgets.app_relaunches}")
    run, un = budget_abort_outcome("app_restart_budget")
    return ControlResult("watchdog", False, gate, run=run, checkpoint=alive, unreached=un, outcomes=subset,
                         findings=(finding,), alert=alerts.aborted_budget("app_restart_budget"),
                         actions=("reset_input_state", "kill_tree", "teardown"), step="failed",
                         detail=f"app failure {budgets.app_relaunches + 1} in the shard")


def orchestrator_died(budgets: ShardBudgets, *, step_id: str | None, state: str = "running") -> ControlResult:
    """An Orchestrator exception or hang (5.2, 10.2): restart and resume, or `orchestrator_restart_budget`."""
    gate = state if state in ("running", "teardown") else "running"
    if budgets.take("orchestrator"):
        subset = () if state == "teardown" else (
            SubsetOutcome("in_progress", Outcome("harness_error", "orchestrator_died")),
            SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{step_id or 'unknown'}")))
        return ControlResult("watchdog", False, gate, outcomes=subset,
                             checkpoint=None if state == "teardown" else Outcome("harness_error", "orchestrator_died"),
                             actions=("kill_tree", "restart_orchestrator") + (() if state == "teardown"
                                                                               else ("resume_next_flow",)),
                             alert=alerts.harness_error(),
                             detail=f"Orchestrator restart {budgets.used_orchestrator} of {budgets.orchestrator_restarts}"
                                    + ("; resumes at teardown" if state == "teardown" else ""))
    run, un = harness_abort_outcome("orchestrator_restart_budget")
    return ControlResult("watchdog", False, gate, run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=("kill_tree",), detail="Orchestrator restart budget spent")


def hook_host_died(budgets: ShardBudgets, *, step_id: str | None, layout_mismatch: bool = False) -> ControlResult:
    """A dead hook host (5.4.2, 10.2 sampling 2): fail closed, restart it once per shard out of its own budget."""
    if not layout_mismatch and budgets.take("hook_host"):
        return ControlResult(
            "watchdog", False, "running", step="blocked_by_gateway", step_reason="hook_host_dead",
            checkpoint=Outcome("harness_error", "hook_lost"),
            outcomes=(SubsetOutcome("in_progress", Outcome("harness_error", "hook_lost")),
                      SubsetOutcome("rest_of_flow", Outcome("not_run", f"step_failed:{step_id or 'unknown'}"))),
            findings=(FindingSpec("gateway_integrity", "gateway hook host died"),), alert=alerts.harness_error(),
            actions=("deny_all", "kill_tree", "restart_hook_host", "relaunch", "resume_next_flow"),
            detail="hook host restarted out of its own budget; no app relaunch consumed")
    run, un = harness_abort_outcome("hook_lost")
    return ControlResult("watchdog", False, "running", run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=("deny_all", "kill_tree"), step="blocked_by_gateway", step_reason="hook_host_dead",
                         detail="shared memory layout mismatch" if layout_mismatch
                         else "second hook host death in the shard")


def proactive_relaunch_due(flows_since_launch: int, uptime_s: float, *, inside_flow: bool) -> bool:
    """After 40 flows or 90 minutes of app uptime, never inside a flow; consumes no budget (10.2)."""
    return not inside_flow and (flows_since_launch >= PROACTIVE_FLOWS or uptime_s >= PROACTIVE_UPTIME_S)


def resolve_detectors(candidates: Sequence[Outcome]) -> Outcome:
    """F27 precedence when two detectors fire in one step: env_error > app_alive failure > harness_error."""
    def rank(o: Outcome) -> int:
        if o.outcome == "env_error":
            return 0
        if o.outcome == "assertion_failed" and o.reason == "assertion:app_alive":
            return 1
        if o.outcome == "harness_error":
            return 2
        return 3
    if not candidates:
        raise ValueError("no detector fired")
    return min(candidates, key=rank)


# -- the dead-man -----------------------------------------------------------------------------------------
def dead_man_deadline(run_type: str, deadline: datetime, *, nightly_cutoff: datetime | None = None) -> datetime:
    """Deadline plus grace (10.2): 15 minutes for PR, 30 for merge, 60 for RC; the Nightly run's 06:00
    America/New_York cutoff is passed in as `nightly_cutoff`."""
    if run_type == "nightly":
        if nightly_cutoff is None:
            raise ValueError("a Nightly run's dead-man time is 06:00 America/New_York: pass nightly_cutoff")
        return nightly_cutoff
    if run_type not in DEAD_MAN_GRACE:
        raise ValueError(f"no dead-man grace for run type {run_type!r}")
    return deadline + DEAD_MAN_GRACE[run_type]


def dead_man_finalize(state: str, reached_running: bool, *, run_type: str) -> ControlResult:
    """`orchestrator --resume <run_id> --finalize` at deadline plus grace (10.2)."""
    if reached_running:
        run, un = harness_abort_outcome("dead_man")
        return ControlResult("dead_man", False, state, run=run, unreached=un, alert=alerts.dead_man(run_type),
                             actions=("kill_tree",), detail="the run reached running and stopped reporting")
    return ControlResult("dead_man", False, state, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "timeout", state), alert=alerts.env_error(),
                         actions=("kill_tree",), detail=f"still at {state} at the dead-man deadline: never dead_man")
