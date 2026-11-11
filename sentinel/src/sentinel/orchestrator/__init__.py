"""The Orchestrator and its safety controls, platform independent (5.2, 6.2 to 6.8, 7.7, 10.2, 10.7, 9.10).

Everything that needs Windows sits behind the `Rig` interface (rig.py) and the narrower ports (ports.py), which
double as the contract for the Windows rig; `SimulatedRig` (simrig.py) implements them for tests and the demo. Every
gate and control returns a `ControlResult` (results.py) with the exact outcome, reason and gate of the closed
taxonomy and the 9.10 alert route.

- `machine`: `Orchestrator`, one shard from `provisioned` to `reported` over a `Rig`; `boundary`: what it reads at
  every flow and step boundary and the checks over it.
- `gates`: the gate checks of the 5.2 state machine and the run-outcome rules.
- `identity`: the paper-mode assertion against `gateway/allowlist.yaml` (6.4, A1, A2).
- `attestation`: settings attestation A3 to A11 (6.3).
- `gateway`: the never-send guard's slow half, fast half, flag watcher, hook phases and drop thresholds (6.2, 5.4);
  `windows`: the Window Watcher (5.4.6); `sendgate`: the gated branch's send token (6.7); `gateway_config`: the
  dismiss allowlist and the default order hotkeys (4.16).
- `drill`: the fire drill driver and scoring (6.5); `safety_canaries`: the Nightly safety canaries (6.3 to 6.9).
- `killswitch`: the kill switch sequence and the post-run proof (6.6); `killflag`: its human channels and preflight
  H5; `orderbook`: the proof reader (grid OCR cross-checked with Copy All).
- `explorer`: the Explorer deny list, budgets and order-stage path (6.8).
- `watchdog`, `budgets`: heartbeats, hang detection, restart, re-login and run budgets, the dead-man
  (10.2, 7.7, 5.2).
- `degraded`: model API down, spend ceilings, network and rig loss (10.7).
- `alerts`: the 9.10 routes; `clock`: the injectable clock.
"""
from .alerts import AlertRoute
from .attestation import AttestationProfile, AttestationResult, RowReads, attest
from .boundary import BoundaryObservations, working_orders_precondition
from .budgets import RateCounters, ReloginBudget, ShardBudgets, WallBudget, run_budget, session_kicked
from .clock import Clock, ManualClock, SystemClock
from .drill import DrillObservation, DrillRecord, Item5, expected_records, score_drill
from .explorer import DEFAULT_DENY_LIST, DenyEntry, ExplorerBudget, ExplorerSession, explorer_teardown, match_deny
from .gates import (GATES, AccountExpectation, WorkspaceExpectation, check_authenticated, check_canary_flow,
                    check_fixture, check_launched, check_provisioned, check_reported, check_teardown, check_workspace,
                    decide_run_outcome)
from .gateway import (HOOK_STATES, Action, Chord, DropLedger, Flag, FlagWatcher, GatewayDrop, GatewayState, InputEvent,
                      SafetyContext, drain, hook_decide, recovery_admits, refusal, slow_half)
from .identity import Allowlist, judge_fixture_account, judge_identity, load_allowlist
from .killflag import FileFlagChannel, MemoryKillChannel, h5_passed, h5_round_trip
from .killswitch import (Proof, ProofInputs, TripWires, check_tripwires, execute_kill_switch, kill_switch,
                         post_run_proof, proof_result)
from .machine import LoginHistory, Orchestrator, RunContext, RunReport
from .orderbook import MonitorBook, build_order_book
from .results import ControlResult, FindingSpec, Outcome, OutcomeSink, RunVerdict, SubsetOutcome, write_unreached
from .rig import Rig
from .sendgate import SendGate, SendPolicy
from .simrig import MemorySink, SimScenario, SimulatedRig, default_context
from .windows import TitleSets, WindowWatcher

__all__ = [
    "AccountExpectation", "Action", "AlertRoute", "Allowlist", "AttestationProfile", "AttestationResult",
    "BoundaryObservations", "Chord", "Clock", "ControlResult", "DEFAULT_DENY_LIST", "DenyEntry", "DrillObservation",
    "DrillRecord", "DropLedger", "ExplorerBudget", "ExplorerSession", "FileFlagChannel", "FindingSpec", "Flag",
    "FlagWatcher", "GATES", "GatewayDrop", "GatewayState", "HOOK_STATES", "InputEvent", "Item5", "LoginHistory",
    "ManualClock", "MemoryKillChannel", "MemorySink", "MonitorBook", "Orchestrator", "Outcome", "OutcomeSink", "Proof",
    "ProofInputs", "RateCounters", "ReloginBudget", "Rig", "RowReads", "RunContext", "RunReport", "RunVerdict",
    "SafetyContext", "SendGate", "SendPolicy", "ShardBudgets", "SimScenario", "SimulatedRig", "SubsetOutcome",
    "SystemClock", "TitleSets", "TripWires", "WallBudget", "WindowWatcher", "WorkspaceExpectation", "attest",
    "build_order_book", "check_authenticated", "check_canary_flow", "check_fixture", "check_launched",
    "check_provisioned", "check_reported", "check_teardown", "check_tripwires", "check_workspace",
    "decide_run_outcome", "default_context", "drain", "execute_kill_switch", "expected_records", "explorer_teardown",
    "h5_passed", "h5_round_trip", "hook_decide", "judge_fixture_account", "judge_identity", "kill_switch",
    "load_allowlist", "match_deny", "post_run_proof", "proof_result", "recovery_admits", "refusal", "run_budget",
    "score_drill", "session_kicked", "slow_half", "working_orders_precondition", "write_unreached",
]
