"""The Orchestrator on the simulated run: one shard per rig through the 5.2 gate machine (6.2 to 6.6, 9.10, 10.2).

`run.run_pipeline` drives every shard of a PR run with `orchestrator.Orchestrator` over a
`PipelineRig`, the Orchestrator's `SimulatedRig` set up from the run's config: the paper-mode assertion
reads the account selector against `gateway/allowlist.yaml` (6.4), settings attestation reads the rows
the pinned app-settings profile expects with the shard's workspace's Active Trader panels (6.3), the
fixture gate reads the accounts of `fixtures/<fixture>.yaml` (7.6), and the canary flow, teardown and
the post-run proof follow. The shard's checkpoints are the Executor's work in `running`
(`run._ShardWork`). This module holds what that needs besides the core:

- `RigScript`: what a scenario makes the rig do (a live-format account id in the selector, a scripted
  Enter on the open Order Confirmation dialog, an EDT freeze during a checkpoint, a failed preflight
  item);
- `PipelineRig`, `shard_setup`: the rig and the `RunContext` of one shard, from the config;
- `ShardSink`: the Orchestrator's `OutcomeSink` for one shard. The store's `fill_unreached` fills the
  whole run, so a shard's unreached outcomes are held here and written with the shard's other records;
- `alert_entry`, `persisted_actions`: the 9.10 route of every control result for `alerts.json`, and
  the actions the Orchestrator asks for but does not perform itself (quarantine records, blocked
  logins, a disabled rig, maintenance tasks, a withheld report) for `orchestrator.json`;
- `proof_text`: the proof verdict in the Reporter's run-health form.

Choices (reported):

- The example config describes one rig's fixture and allowlist (`fx.rig-01.paper`, rig-01). Every
  shard's simulated rig asserts paper mode against that rig's allowlist entries and verifies that
  fixture; the shard keeps its own rig id for the store and the report.
- Shards run one after the other in rig order. A shard that ends its run `aborted_*` stops the run:
  later shards are not started and their checkpoints take the abort's unreached outcome (1.10); a
  shard that ends `complete_with_env_errors` fills only its own checkpoints and the next shard runs.
- The fixture account a PR shard selects (A2) is the fixture's empty account, the account order-stage
  flows stage tickets in (6.3, 7.7 step 7).

WORKAROUND(orchestrator): `SimulatedRig.read_title_build_id` returns the module constant `BUILD`
whatever the launch observation said, so the Executor's build re-read before each flow (5.2,
`gates.check_build_unchanged`) could never see the build the `launched` gate accepted. `PipelineRig`
returns the title build id of its last launch. Reported to the coordinator.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..orchestrator.alerts import AlertRoute
from ..orchestrator.attestation import AttestationProfile
from ..orchestrator.clock import Clock
from ..orchestrator.gates import AccountExpectation, WorkspaceExpectation
from ..orchestrator.gateway import Chord
from ..orchestrator.identity import load_allowlist
from ..orchestrator.killswitch import ORDERS_SENT, UNPROVEN, Proof
from ..orchestrator.machine import RunContext
from ..orchestrator.results import ControlResult, Outcome
from ..orchestrator.rig import AccountState, LaunchObservation, SelectorRead, WorkspaceObservation
from ..orchestrator.simrig import TABS, SimScenario, SimulatedRig, default_settings
from .config import SentinelConfig

LAUNCHES = 8                    # the first launch, three relaunches (10.2) and room for a recovery login
DEFAULT_CHORDS = ("Shift+B", "Shift+S")          # the order hot keys the profile pins as unassigned (A6)
# Actions a control result asks for that the Orchestrator does not perform on the rig itself (results.ACTIONS)
PERSISTED = {
    "quarantine_flow": "quarantine record for the flow (5.4.8, 6.6)",
    "quarantine_order_stage_flows": "quarantine record for every flow with an order-stage step (6.5)",
    "quarantine_all_flows": "quarantine record for every flow on the rig (6.5)",
    "block_login": "login blocked until a human clears it (6.4, 6.6)",
    "disable_rig": "rig disabled in the scheduler (6.6)",
    "maintenance_task": "maintenance task for the owner (6.3, 6.4, 7.6)",
    "withhold_report": "report withheld (6.9)",
    "rotate_credential": "credential rotation (6.9)",
    "queue_calibration": "calibration queued for the fingerprint (7.13)",
}


@dataclass(frozen=True)
class RigScript:
    """What a scenario makes the simulated rig do; every field defaults to a clean run."""

    selector_text: str | None = None                 # the first shard's first account selector read (6.4)
    enter_after: tuple[str, str] | None = None       # (flow id, step id): a scripted Enter on the open dialog after it
    freeze_at: str | None = None                     # checkpoint id: the app's EDT freezes while it is captured
    preflight_failures: Mapping[str, str | None] = field(default_factory=dict)


class PipelineRig(SimulatedRig):
    """The Orchestrator's `SimulatedRig`, launching the build under test (module docstring)."""

    def __init__(self, scenario: SimScenario, *, clock: Clock, build_id: str):
        super().__init__(scenario, clock=clock)
        self.build_id = build_id
        self._title: str | None = None

    def launch(self) -> LaunchObservation:
        obs = super().launch()
        self._title = obs.title_build_id
        return obs

    def read_title_build_id(self) -> str | None:
        # WORKAROUND(orchestrator): the base class answers its constant BUILD (module docstring)
        return self._title if self.app_running else None


class ShardSink:
    """The `OutcomeSink` of one shard: unreached outcomes are held until the run writes the shard."""

    def __init__(self, scope: Sequence[str]):
        self.scope = list(scope)
        self.done: set[str] = set()
        self.filled: dict[str, Outcome] = {}

    def fill_unreached(self, run_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None,
                       detail: str | None = None) -> list[str]:
        out = [c for c in self.scope if c not in self.done and c not in self.filled]
        for c in out:
            self.filled[c] = Outcome(outcome, reason, gate, detail)
        return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _accounts(fixture: Mapping[str, Any]) -> tuple[dict[str, AccountExpectation], tuple[AccountState, ...]]:
    """The fixture's accounts as the gate expects them (4.9, 7.6) and as the simulated rig reads them."""
    symbols = {s["id"]: str(s["symbol"]) for s in fixture.get("symbols") or ()}
    expected, states = {}, []
    for a in fixture.get("accounts") or ():
        aid = str(a["account_id"])
        pos = {symbols[p["symbol_ref"]]: float(p["qty"]) for p in fixture.get("positions") or ()
               if p.get("account_ref") == aid}
        expected[aid] = AccountExpectation(str(a["role"]), pos, float(a["cash"]), float(a.get("cash_tolerance", 0)))
        states.append(AccountState(aid, str(a["role"]), pos, 0, float(a["cash"])))
    return expected, tuple(states)


def shard_setup(cfg: SentinelConfig, *, run_id: str, run_type: str, shard_id: str, shard_no: int, build_id: str,
                workspace_id: str, script: RigScript, first_shard: bool) -> tuple[RunContext, SimScenario]:
    """The `RunContext` of one shard and the `SimScenario` its simulated rig observes (module docstring)."""
    fx = cfg.fixture
    rig = str(fx.get("rig", "rig-01"))
    allowlist = load_allowlist(cfg.root / "gateway" / "allowlist.yaml", rig)
    expected, states = _accounts(fx)
    empty = next((a for a, e in expected.items() if e.role == "empty"), next(iter(expected)))
    prof_id = str(fx.get("settings_profile", ""))
    prof_path = cfg.root / "profiles" / f"{prof_id}.yaml"
    prof_doc = next((d.data for d in cfg.repo.of("profile") if (d.data or {}).get("id") == prof_id), {}) or {}
    entry = next((d.data for d in cfg.repo.of("workspace") if (d.data or {}).get("id") == workspace_id
                  and "file_sha256" in (d.data or {})), {}) or {}
    panels = tuple(str(p["panel_id"]) for p in entry.get("active_trader_panels") or ())
    profile = AttestationProfile.from_settings(prof_doc.get("settings") or {}, panels=panels,
                                               sha256=_sha(prof_path) if prof_path.is_file() else None)
    ws_sha = str(entry.get("file_sha256", "")).removeprefix("sha256:") or hashlib.sha256(workspace_id.encode()).hexdigest()
    ctx = RunContext(run_id=run_id, run_type=run_type, shard_id=shard_id, expected_build=build_id,
                     allowlist=allowlist, fixture_account=empty, profile=profile, fixture=expected,
                     workspace=WorkspaceExpectation(ws_sha, TABS, 1),
                     default_order_chords=tuple(Chord.parse(c) for c in DEFAULT_CHORDS), shard=shard_no)
    settings = dict(default_settings(), A7={p: False for p in panels})
    reads = [SelectorRead(script.selector_text, crop_sha256=hashlib.sha256(script.selector_text.encode()).hexdigest())
             ] if (script.selector_text and first_shard) else []
    scenario = SimScenario(preflight_failures=dict(script.preflight_failures) if first_shard else {},
                           launches=[LaunchObservation(True, build_id, waited_s=40.0)] * LAUNCHES,
                           selector_reads=reads, selected=empty, settings=settings,
                           workspace=WorkspaceObservation(ws_sha, True, TABS, 1), accounts=states)
    return ctx, scenario


# -- what the run writes besides the store ----------------------------------------------------------------
def alert_entry(run_id: str, shard_id: str, r: ControlResult) -> dict | None:
    """One `alerts.json` entry: the 9.10 route of a control result, or None when it pages nobody."""
    a: AlertRoute | None = r.alert
    if a is None:
        return None
    return {"run_id": run_id, "shard_id": shard_id, "rule": a.rule, "priority": a.priority, "paged": list(a.paged),
            "notified": list(a.notified), "safety_table": a.is_safety, "control": r.control, "gate": r.gate,
            "incident": r.incident, "run_outcome": r.run_status, "run_reason": r.run_reason, "detail": r.detail}


def persisted_actions(run_id: str, shard_id: str, rig: str, login: str, r: ControlResult,
                      flow_id: str | None) -> list[dict]:
    """The actions of a control result the Orchestrator does not perform itself (module docstring)."""
    out = []
    for act in r.actions:
        if act in PERSISTED:
            out.append({"run_id": run_id, "shard_id": shard_id, "action": act, "what": PERSISTED[act],
                        "control": r.control, "gate": r.gate, "rig": rig, "login": login,
                        "flow_id": flow_id if act == "quarantine_flow" else None, "detail": r.detail})
    return out


def proof_text(proof: Proof | None) -> str:
    """The proof as the Reporter's run-health row prints it (`PROVEN: ...` or `UNPROVEN: ...`, 6.6).

    `ORDERS SENT: undeclared rows` (killswitch choice 5) has no form there; it is printed as an
    UNPROVEN line that names the rows (reported as a Reporter gap)."""
    if proof is None:
        return "UNPROVEN: no proof ran"
    if proof.verdict == UNPROVEN:
        return f"UNPROVEN: {proof.reason}"
    if proof.verdict == ORDERS_SENT:
        return f"UNPROVEN: {proof.verdict} ({', '.join(r.key for r in proof.undeclared)})"
    return proof.verdict
