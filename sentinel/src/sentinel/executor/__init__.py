"""The Executor: flow steps and census visits turned into gateway input, step outcomes and checkpoint outcomes
(5.4, 5.4.3, 5.5, 2.4, 2.6.1, 4.7), the heartbeat (10.2) and the failure mapping (8.9, 10.1).

Platform-independent logic over the ports of `orchestrator.ports` and the rig's `send_input`; nothing here imports a
Windows module.

- `flow.run_unit(unit, ports, locator, planner, ctx)`: one flow or census visit from its preconditions to its teardown
  (`Unit`, `census_unit`, `UnitResult`, `WorkspaceStreak`, `UnitHooks`; `ExportPorts` and `SecretPorts` for export
  and secret steps).
- `emitter.emit(step, ports, locator, planner, ctx)`: stages 1 to 8 of 5.4.3 for one step (`StepResult`,
  `ExecContext`, `agent_rung_block`).
- `heartbeat.HeartbeatPolicy`: the heartbeat at every step start and end, every gate and every 10 s, with
  `expect_screen_change_since`, `capture_suspended`, the PIDs and the last capture; `AppLife` tags every capture and
  step with app uptime and restart count.
- `failures.map_exception(exc, ...)`: store, artifact and unhandled exceptions into their outcomes or the shard abort
  (`FailureLedger`, `FaultInjector`, `guard`).

The input-side modules of the same package (`primitives`, `locator`, `secret`, `export`) implement the
`PrimitivePlanner` and `Locator` protocols that `run_unit` and `emit` take.
"""
from .emitter import (AgentRescue, Effect, EmitterPorts, ExecContext, Foreground, ModalState, Rescue, StepResult,
                      agent_rung_block, emit)
from .failures import FailureLedger, Fault, FaultInjector, guard, map_exception
from .flow import (AssertVerdict, Captured, CheckRead, ExportPorts, SecretPorts, Unit, UnitHooks, UnitPorts,
                   UnitResult, WaitPoll, WorkspaceStreak, census_unit, run_unit)
from .heartbeat import AppLife, HeartbeatPolicy, hang_detected, read_heartbeat

__all__ = [
    "AgentRescue", "Effect", "EmitterPorts", "ExecContext", "Foreground", "ModalState", "Rescue", "StepResult",
    "agent_rung_block", "emit",
    "FailureLedger", "Fault", "FaultInjector", "guard", "map_exception",
    "AssertVerdict", "Captured", "CheckRead", "ExportPorts", "SecretPorts", "Unit", "UnitHooks", "UnitPorts",
    "UnitResult", "WaitPoll", "WorkspaceStreak", "census_unit", "run_unit",
    "AppLife", "HeartbeatPolicy", "hang_detected", "read_heartbeat",
]
