"""The Orchestrator on the simulated run: one shard per rig through the 5.2 gate machine (6.2 to 6.6, 9.10, 10.2).

`run.run_pipeline` drives every shard of a PR run with `orchestrator.Orchestrator` over the
Orchestrator's `SimulatedRig`, set up from the run's config: the paper-mode assertion
reads the account selector against `gateway/allowlist.yaml` (6.4), settings attestation reads the rows
the pinned app-settings profile expects with the shard's workspace's Active Trader panels (6.3), the
fixture gate reads the accounts of `fixtures/<fixture>.yaml` (7.6), and the canary flow, teardown and
the post-run proof follow. The shard's checkpoints are the Executor's work in `running`
(`run._ShardWork`). This module holds what that needs besides the core:

- `RigScript`: what a scenario makes the rig do (a live-format account id in the selector, a scripted
  Enter on the open Order Confirmation dialog, an EDT freeze during a checkpoint);
- `shard_setup`: the `RunContext` of one shard and the `SimScenario` its rig observes, from the config
  (the rig launches the build under test, so the Executor's build re-read before each flow sees it; with
  `kill_root` its file flag is `<kill_root>/<rig>/run/kill.flag`, so `sentinel stop --root` reaches it); the default
  order chords come from `gateway/default_order_hotkeys.yaml` (`gateway_config.load_default_order_hotkeys`, 5.4.7):
  a file that is missing or breaks L-XREF-17 is returned as the `GatewayConfigError` whose result fails the shard
  closed at `settings_attested`;
- `ShardSink`: the Orchestrator's `OutcomeSink` for one shard. The store's `fill_unreached` fills the
  whole run, so a shard's unreached outcomes are held here and written with the shard's other records;
- `alert_entry`, `persisted_actions`: the 9.10 route of every control result for `alerts.json`, and
  the actions the Orchestrator asks for but does not perform itself (quarantine records, blocked
  logins, a disabled rig, maintenance tasks, a withheld report) for `orchestrator.json`;
- `login_alias`, `login_label`: a rig's login as the store keys it (the vault alias of 8.3 `logins`) and as
  every report, summary, alert and persisted action names it.

Each shard's post-run proof is written to the store (8.3 `post_run_proofs`, `run._ShardWork.write_proofs`) and
reaches the report read back from it, in the 6.6 text form the Reporter prints (`killswitch.proof_line`).

Choices (reported):

- The example config describes one rig's fixture and allowlist (`fx.rig-01.paper`, rig-01). Every
  shard's simulated rig asserts paper mode against that rig's allowlist entries and verifies that
  fixture; the shard keeps its own rig id for the store and the report.
- Shards run as parallel shards on their own rigs would, in one deterministic order: every shard's
  gates in rig order, then the captures of every shard that reached `running` in plan order (so the
  run's shared re-run budget of 3.10 is spent in the plan's tier order, as the single-rig run did),
  then each shard's teardown and proof. A shard whose control result stops it fills its own unreached
  checkpoints (1.10) and the other shards go on; the run outcome is the worst of the shards' (8.2).
- Every shard's simulated rig reads the same identity (choice above), so a scripted account selector
  text shows on every shard.
- The fixture account a PR shard selects (A2) is the fixture's empty account, the account order-stage
  flows stage tickets in (6.3, 7.7 step 7).
- The simulated rig stands for a rig after Phase 0 whose dismiss allowlist record (6.2) lists the Order
  Confirmation's `Delete` and its title-bar `Close` (`SIM_DISMISS_LABELS`): the stock ticket flow's teardown closes the
  dialog on `dialog.order-confirmation.title-close`, as 4.7's worked example does, through the Executor's emitter.
- A login is named by a readable label, `<rig> paper login` (the rig's one scripted paperMoney login, 6.4),
  wherever a person reads it (9.2 "login {alias}", the persisted `block_login` action). Its vault path is a
  `vault://` reference (4.9 `login_ref`, L-FIX-04): the store keys `logins` by it (8.3), and nothing a person
  reads prints it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..orchestrator.alerts import AlertRoute
from ..orchestrator.attestation import AttestationProfile
from ..orchestrator.gates import AccountExpectation, WorkspaceExpectation
from ..orchestrator.gateway_config import (DEFAULT_ORDER_HOTKEYS_PATH, DefaultOrderHotkeys, GatewayConfigError,
                                           load_default_order_hotkeys)
from ..orchestrator.identity import load_allowlist
from ..orchestrator.machine import RunContext
from ..orchestrator.results import ControlResult, Outcome
from ..orchestrator.rig import AccountState, LaunchObservation, SelectorRead, WorkspaceObservation
from ..orchestrator.simrig import TABS, SimScenario, default_settings
from .config import SentinelConfig

SIM_DISMISS_LABELS = ("Delete", "Close")   # the Order Confirmation's dismiss labels the simulated rig pins (choice)
LAUNCHES = 8                    # the first launch, three relaunches (10.2) and room for a recovery login
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

    selector_text: str | None = None                 # each shard's first account selector read (6.4)
    enter_after: tuple[str, str] | None = None       # (flow id, step id): a scripted Enter on the open dialog after it
    freeze_at: str | None = None                     # checkpoint id: the app's EDT freezes while it is captured
    stop_at: str | None = None                       # checkpoint id: an operator runs `sentinel stop` on its rig then
    # the Executor's simulated failures (pipeline.simports): a registry control hidden from the locator ladder for
    # that many attempts, a precondition failing for that many reads, wait_for steps that time out, steps whose
    # declared effect never appears, and the 8.9 and 10.1 faults (`executor.failures.faults_from` triples)
    locator_miss: tuple[tuple[str, int], ...] = ()
    precondition_fail: tuple[tuple[str, int], ...] = ()
    wait_timeout: tuple[str, ...] = ()
    postcondition_fail: tuple[str, ...] = ()
    faults: tuple[tuple[str, str | None, str], ...] = ()
    vault_unreachable: bool = False                  # every vault lease of the LOGIN secret step fails (6.9, 7.7)
    occluded: tuple[str, ...] = ()                   # checkpoint ids: a foreign window covers the target (3.4)
    window_missing: tuple[str, ...] = ()             # checkpoint ids: the target window is not on screen (3.4 rule 3)


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


def order_hotkeys(cfg: SentinelConfig) -> tuple[DefaultOrderHotkeys | None, GatewayConfigError | None]:
    """The pinned build's default order chords (5.4.7): the loaded file, or the error whose `result` fails the shard
    closed at `settings_attested` (L-XREF-17)."""
    try:
        return load_default_order_hotkeys(cfg.root / DEFAULT_ORDER_HOTKEYS_PATH), None
    except GatewayConfigError as err:
        return None, err


def shard_setup(cfg: SentinelConfig, *, run_id: str, run_type: str, shard_id: str, shard_no: int, build_id: str,
                workspace_id: str, script: RigScript, rig_id: str | None = None,
                kill_root: Path | None = None) -> tuple[RunContext, SimScenario]:
    """The `RunContext` of one shard and the `SimScenario` its simulated rig observes (module docstring). The
    context carries the default order chords file when it loads (`order_hotkeys`); the caller applies the load
    error's result otherwise."""
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
    hotkeys, _ = order_hotkeys(cfg)
    ctx = RunContext(run_id=run_id, run_type=run_type, shard_id=shard_id, expected_build=build_id,
                     allowlist=allowlist, fixture_account=empty, profile=profile, fixture=expected,
                     workspace=WorkspaceExpectation(ws_sha, TABS, 1),
                     default_order_chords=hotkeys.chords if hotkeys else (), shard=shard_no, order_hotkeys=hotkeys)
    settings = dict(default_settings(), A7={p: False for p in panels})
    reads = [SelectorRead(script.selector_text, crop_sha256=hashlib.sha256(script.selector_text.encode()).hexdigest())
             ] if script.selector_text else []
    scenario = SimScenario(launches=[LaunchObservation(True, build_id, waited_s=40.0)] * LAUNCHES,
                           selector_reads=reads, selected=empty, settings=settings,
                           workspace=WorkspaceObservation(ws_sha, True, TABS, 1), accounts=states,
                           rig_id=rig_id or rig, kill_root=kill_root, dismiss_allowlist=SIM_DISMISS_LABELS)
    return ctx, scenario


# -- logins ---------------------------------------------------------------------------------------------------
def login_alias(rig: str) -> str:
    """The rig's scripted login as the store keys it (8.3 `logins.login_alias`, 4.9 `login_ref`): a vault alias,
    never a credential and never printed where a person reads it (`login_label`)."""
    return f"vault://sentinel/rigs/{rig}/tos-login"


def login_label(rig: str) -> str:
    """The readable name of the rig's scripted paperMoney login (module docstring), e.g. `rig-01 paper login`."""
    return f"{rig} paper login"


# -- what the run writes besides the store ----------------------------------------------------------------
def alert_entry(run_id: str, shard_id: str | None, r: ControlResult) -> dict | None:
    """One `alerts.json` entry: the 9.10 route of a control result, or None when it pages nobody."""
    a: AlertRoute | None = r.alert
    if a is None:
        return None
    entry = {"run_id": run_id, "shard_id": shard_id, "rule": a.rule, "priority": a.priority, "paged": list(a.paged),
             "notified": list(a.notified), "safety_table": a.is_safety, "control": r.control, "gate": r.gate,
             "incident": r.incident, "run_outcome": r.run_status, "run_reason": r.run_reason, "detail": r.detail}
    for k in ("account", "working_orders_screenshot", "instruction"):     # the 6.6 Cancel All failure hand-over
        if (r.evidence or {}).get(k):
            entry[k] = r.evidence[k]
    return entry


def persisted_actions(run_id: str, shard_id: str, rig: str, r: ControlResult, flow_id: str | None) -> list[dict]:
    """The actions of a control result the Orchestrator does not perform itself (module docstring); the login is
    the rig's `login_label`."""
    out = []
    for act in r.actions:
        if act in PERSISTED:
            out.append({"run_id": run_id, "shard_id": shard_id, "action": act, "what": PERSISTED[act],
                        "control": r.control, "gate": r.gate, "rig": rig, "login": login_label(rig),
                        "flow_id": flow_id if act == "quarantine_flow" else None, "detail": r.detail})
    return out
