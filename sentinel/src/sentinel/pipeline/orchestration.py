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
- The shard's context carries the corpus entry's tabs and windows (`WorkspaceExpectation.from_entry`, SD-77; the
  simulated main frame takes the entry's window rect), the fixture document and the Stage 1 profiles (7.6; the rig
  starts with the settings pages and watchlists those profiles and the fixture name, so Stage 1 corrects nothing on a
  clean rig), the MFA branch of `phase0/branches.yaml`, whether the fixture's `reseed_due` has passed on the run's
  date (`churn.reseed_due_passed`) and the login's history from the store (`login_history`).
- A login is named by a readable label, `<rig> paper login` (the rig's one scripted paperMoney login, 6.4),
  wherever a person reads it (9.2 "login {alias}", the persisted `block_login` action). Its vault path is a
  `vault://` reference (4.9 `login_ref`, L-FIX-04): the store keys `logins` by it (8.3), and nothing a person
  reads prints it.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from datetime import date
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
from ..orchestrator.rig import AccountState, LaunchObservation, LoginObservation, SelectorRead, WorkspaceObservation
from ..orchestrator.simrig import MAIN_RECT, SIM_TITLES, TABS, SimScenario, default_settings
from ..orchestrator.popups import PopupCatalog, load_catalog, order_dialog_regexes, order_event_regexes
from ..orchestrator.windows import TitleSets
from ..orchestrator.machine import LoginHistory
from ..orchestrator.stage1 import APP_SETTINGS_PAGE, ORDER_DEFAULTS_PAGE, Stage1Profile
from ..churn import reseed_due_passed
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
    # the 4.12 fingerprint (7.4): base-side app reads that differ from the candidate's (a render probe of another
    # rasterizer), a read the rig cannot make, a cheap OS field that changes after its n-th read
    base_fingerprint: tuple[tuple[str, Any], ...] = ()
    fp_unreadable: str | None = None
    fp_change: tuple[str, int] | None = None
    golden_drift: tuple[tuple[str, float], ...] = ()  # golden account positions that drifted (symbol, qty; 7.6)
    popups: tuple[tuple[str, str], ...] = ()           # (step or checkpoint id, title): a window that appears there
    foreign_foreground: str | None = None             # a foreign program's window in the foreground (7.8)
    login: tuple[tuple[str, Any], ...] = ()           # LoginObservation fields of the first login (7.7)
    dpi_awareness: str | None = None                  # the harness's DPI awareness when not Per-Monitor-V2 (H3)


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
                kill_root: Path | None = None, today: date | None = None,
                history: LoginHistory | None = None) -> tuple[RunContext, SimScenario]:
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
    # the corpus entry's tabs and windows (4.6, SD-77); an entry without them keeps the simulated tab set
    workspace = WorkspaceExpectation.from_entry(entry) if entry.get("tabs") and entry.get("windows") \
        else WorkspaceExpectation(ws_sha, TABS, 1)
    od_id = str(fx.get("order_defaults_profile", ""))
    od_doc = next((d.data for d in cfg.repo.of("profile") if (d.data or {}).get("id") == od_id), {}) or {}
    stage1 = Stage1Profile.from_docs(prof_doc, od_doc, attestation=profile)      # 7.6 Stage 1's checked-in profiles
    pages = {APP_SETTINGS_PAGE: dict(stage1.app_settings)} | {
        f"{ORDER_DEFAULTS_PAGE}{asset}": dict(v) for asset, v in stage1.order_defaults.items()}
    symbols = {s["id"]: str(s["symbol"]) for s in fx.get("symbols") or ()}
    lists = {str(w["name"]): tuple(symbols[r] for r in w.get("symbols") or ()) for w in fx.get("watchlists") or ()}
    mfa = str(((_phase0(cfg).get("login_mfa") or {}).get("branch")) or "A")
    ctx = RunContext(run_id=run_id, run_type=run_type, shard_id=shard_id, expected_build=build_id,
                     allowlist=allowlist, fixture_account=empty, profile=profile, fixture=expected, workspace=workspace,
                     default_order_chords=hotkeys.chords if hotkeys else (), shard=shard_no, order_hotkeys=hotkeys,
                     fixture_doc=fx, stage1=stage1, mfa_branch=mfa if mfa in ("A", "B") else "A",
                     reseed_due_passed=reseed_due_passed(today, fx) if today is not None else False,
                     history=history or LoginHistory())
    settings = dict(default_settings(), A7={p: False for p in panels})
    reads = [SelectorRead(script.selector_text, crop_sha256=hashlib.sha256(script.selector_text.encode()).hexdigest())
             ] if script.selector_text else []
    if script.golden_drift:                         # the golden account's positions differ from the fixture (7.6)
        states = tuple(dataclasses.replace(a, positions={**a.positions, **dict(script.golden_drift)})
                       if a.role == "golden" else a for a in states)
    scenario = SimScenario(launches=[LaunchObservation(True, build_id, waited_s=40.0)] * LAUNCHES,
                           selector_reads=reads, selected=empty, settings=settings, fp_unreadable=script.fp_unreadable,
                           fp_change=script.fp_change, main_rect=workspace.windows[0].rect if workspace.windows else
                           MAIN_RECT, profile_pages=pages, watchlists=lists, at_quantity=stage1.active_trader_quantity,
                           workspace=WorkspaceObservation(ws_sha, True, workspace.tabs, max(1, workspace.window_count)),
                           accounts=states, login=LoginObservation(**dict(script.login)),
                           dpi_awareness=script.dpi_awareness or "PER_MONITOR_AWARE_V2",
                           foreign_foreground=script.foreign_foreground, titles=catalog_titles(cfg),
                           rig_id=rig_id or rig, kill_root=kill_root, dismiss_allowlist=SIM_DISMISS_LABELS)
    return ctx, scenario


# -- logins ---------------------------------------------------------------------------------------------------
def popup_catalog(cfg: SentinelConfig) -> PopupCatalog | None:
    """The run's popup catalog (`popups/catalog.yaml`, 4.10), or None when the config has none."""
    path = cfg.root / "popups" / "catalog.yaml"
    return load_catalog(path) if path.is_file() else None


def catalog_titles(cfg: SentinelConfig) -> TitleSets:
    """The Window Watcher's title sets from the popup catalog (5.4.6 source 1, 7.8): the `safety` entries are the order
    dialogs, the `order_event` entries the order-event windows; the detached ticket keeps the simulated regex (the
    catalog has no entry for it). Without a catalog, the simulated sets."""
    cat = popup_catalog(cfg)
    if cat is None:
        return SIM_TITLES
    return TitleSets.compile(order_dialog=order_dialog_regexes(cat) or (r"^Order Confirmation$",),
                             detached_ticket=(r"^Order Entry\b",), order_event=order_event_regexes(cat))


def _phase0(cfg: SentinelConfig) -> Mapping[str, Any]:
    """The parsed `phase0/branches.yaml` (4.16), or nothing."""
    path = cfg.root / "phase0" / "branches.yaml"
    if not path.is_file():
        return {}
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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
