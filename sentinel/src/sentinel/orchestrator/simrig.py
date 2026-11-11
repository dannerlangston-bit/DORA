"""`SimulatedRig`: a scriptable, deterministic `Rig` for tests and the demo (no Windows, no thinkorswim).

A `SimScenario` holds what each capability returns (preflight results, launch outcomes, the login, the
selector reads, the settings rows, the workspace, the accounts, the Monitor book, teardown, the Watchdog's reverse
heartbeat and free space, the configuration the gateway enforces). Input runs through the real decision code of
gateway.py: `send_input` applies `slow_half` with the full `SafetyContext` (the session's safety state the
Orchestrator pushes through `set_session_safety`, the dismiss allowlist and Explorer deny list of the scenario's
configuration, the recovery-only arm, the gated branch's `SendGate`), writes tokens into a `GatewayState` (a
cleared send carries `send_ok`) and passes every event through `hook_decide`, and a small simulated app reacts to
what gets through: a visible docked ticket (`ticket_visible`) shows `Confirm and Send`, which the ticket detector
matches, `Confirm and Send` opens the Order Confirmation dialog (the Window Watcher then sets the flag and writes
the Send deny rectangle), `Delete` closes it, and an Enter, a Space or a click on `Send` that reaches the app while
the dialog is open sends an order, which appears as a new Working row; a `Cancel All` click that reaches it moves
every Working row to Canceled. So a bypass that the gateway failed to stop is visible to the post-run proof exactly
as on a real rig.

As on a real rig, the flag has three sources (5.4.6), and the simulated screen feeds the real Window Watcher
(`windows.WindowWatcher.poll_into`): the dialog by enumeration (title regexes `SimScenario.titles`, the stand-in for
the popup catalog's order-dialog set), the docked ticket by the template detector on a synthetic grab, and the
flow's sticky `order_stage` declaration. A cleared action of an order-stage step sets the sticky flag before its
first event is emitted; a `close_dialog` or `clear_ticket` teardown action that completes with the dialog and the
ticket absent releases it, and so does killing the process tree. The Watcher is polled again after every emitted
event, so the hook reads the flag per event: an Enter that follows the `Confirm and Send` click in the same action
meets the open dialog's flag. An agent or Explorer action refused because the flag is up is listed in `flag_aborts`
(the caller takes the 5.4.6 path). The Watcher's classification also gives the kill switch its `order_event`
window and the dialog's open time (`window_tripwires`).

The LOGIN flow's typing and the `cancel_all` click go through `send_input` like every other input, so a hook in
deny-all or outside the recovery-only arm stops them (6.6 step 5); `read_orders` renders the book as grid OCR rows
and Copy All text and reads it back through `orderbook.build_order_book`; preflight runs H5 over the rig's kill
channels (`killflag.h5_round_trip`) and H6 through the redaction stage (`capture.h6_self_test`).

Choices (reported):

1. Configuration. With `config_root` the rig enforces that checkout's `gateway/dismiss_allowlist.yaml` (empty in
   the example config, so `Confirm and Send` is refused) and `policy/explorer_deny_list.yaml`; `dismiss_allowlist`
   and `deny_list` pin either explicitly. A scenario with no configuration stands for a rig after Phase 0: the
   dialog's `Delete` (`gateway.DISMISS_ALLOWLIST`) and `explorer.DEFAULT_DENY_LIST`.
2. The synthetic grab covers the screen area where the simulated controls are drawn (`FRAME_ORIGIN`, `FRAME_SIZE`)
   as random textures matched by `SIM_TEMPLATES`; app windows other than the main frame and the dialog
   (`extra_windows`) carry no bounds, so the main frame stays the detector's foreground. Each poll is a new grab:
   a grab whose content changed at an unchanged clock reading is stamped a nanosecond earlier per change, since the
   Watcher keys its detector cache on the grab time and the simulation has no detector latency. A real Watcher polls
   every 100 ms, so two polls pass between one input and the next; the simulation takes the second one too.
3. Without a pushed session (`set_session_safety`, the Orchestrator's `safety_listener`), the slow half falls back to
   the booleans `attested` and a current paper-mode assertion (gateway.py choice 9), as for a rig driven directly.
4. The simulated Monitor's order grids carry an `Order ID` column, the rows' key (`SIM_LAYOUTS`; the real columns
   are pinned in Phase 0, orderbook.py choice 1); the buying power cell equals Net Liq unless the scenario sets it.
5. `reset_input_state` models the fresh gateway instance: no token of the dead tree's input survives; the simulation
   has no OS key state to release.
6. The login's typing is a click on the paperMoney switch, the login id, the secret (stand-in keys, never a value)
   and a click on `Log In`, all of class `login`; when the gateway stops any of it, the login dialog stays and the
   main window never appears (`timeout`). With a `vault` (`SimScenario.vault`, a `ports.VaultPort`) the secret is a
   4.7 `secret` step run by `executor.secret.run_secret_step`: leased at typing time, typed as `VK_PACKET` pairs
   with `secret` tokens, its lease kept in `leases` for the teardown plaintext scan; a lease that fails twice types
   nothing and the login reports `vault_lease: false` (6.9, 7.7).
7. Input planned by the Executor (`executor.primitives.PlannedAction`) runs unit by unit through the hook exactly as
   5.4.10 tabulates (double clicks, drags, wheels, chords, typed characters, the `reset_input_state` releases); the
   simulated app reacts to the pointer events and to Enter or Space on the open dialog as before. A click labelled
   `Close` on the open Order Confirmation is its title-bar close: the dialog goes away and nothing is sent.

`MemorySink` is an in-memory `OutcomeSink` over a list of scoped checkpoint ids, and `default_context`
builds the `RunContext` that matches the default scenario (the example config's rig-01 accounts).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from ..capture import h6_self_test
from ..records import DeclaredSend
from .attestation import AttestationProfile
from .clock import Clock, ManualClock
from .explorer import DEFAULT_DENY_LIST, DenyEntry, load_deny_list, match_deny
from .gates import PREFLIGHT_ITEMS, AccountExpectation, WorkspaceExpectation
from .gateway import (DISMISS_ALLOWLIST, VK_RETURN, VK_SPACE, Action, Chord, Flag, GatewayState, InputEvent, Rect,
                      SafetyContext, hook_decide, slow_half)
from .gateway_config import DISMISS_ALLOWLIST_PATH, load_dismiss_allowlist
from .identity import Allowlist, account_id_sha256
from .killflag import FileFlagChannel, MemoryKillChannel, h5_passed, h5_round_trip, rig_flag_path
from .killswitch import TripWires
from .machine import RunContext
from .orderbook import ORDER_GRIDS, POSITION_GRID, POSITION_LAYOUT, GridLayout, MonitorBook, build_order_book
from .ports import Frame, KillChannel, SessionSafety
from .ports import WindowInfo as ScreenWindow
from .rig import (AccountState, CanaryFlowObservation, CancelAllResult, ConfirmationCheck, CrashSignal, EmitResult,
                  GatewayLog, HotkeyRow, LaunchObservation, LoginObservation, NotificationsRead, OrderBook, OrderRow,
                  PreflightReport, Rig, SelectorRead, SettingRead, TeardownObservation, WindowInfo,
                  WorkspaceObservation)
from .sendgate import ACCOUNT_ROLES, SendGate, SendPolicy
from .windows import SendTemplate, TitleSets, WindowWatcher

BUILD = "2026.44.1"
GOLDEN, EMPTY = "D-20481133", "D-20481134"
WORKSPACE_SHA = hashlib.sha256(b"ws.tier-a.active-trader").hexdigest()
TABS = ("Monitor", "Trade", "Charts")
SEND_RECT = Rect(900, 600, 80, 28)          # the Send button of the simulated Order Confirmation dialog
DELETE_POINT = (1010, 614)
CONFIRM_POINT = (1500, 900)
CONFIRM_RECT = Rect(1440, 886, 120, 28)     # the docked ticket's Confirm and Send button, around CONFIRM_POINT
CANCEL_ALL_POINT = (1700, 300)              # Working Orders' Cancel All
MODE_SWITCH_POINT = (960, 420)              # the login dialog's paperMoney switch
LOG_IN_POINT = (960, 560)
MAIN_RECT = (0, 0, 1920, 1080)
DIALOG_RECT = (860, 560, 300, 120)          # holds SEND_RECT and DELETE_POINT
FRAME_ORIGIN = (840, 560)                   # the synthetic grab's area (choice 2)
FRAME_SIZE = (760, 380)
GRAB_EPS_S = 1e-9
FREE_BYTES = 500 * 1024 ** 3
H6_THEME = (18, 18, 18)                     # the simulated dark theme's background
LOGIN_ID_KEYS = tuple(ord(c) for c in "SIM")
SECRET_KEYS = (ord("X"), ord("Y"), ord("Z"))  # stand-ins: the secret's value never reaches the simulation (6.9)
SIM_TITLES = TitleSets.compile(order_dialog=(r"^Order Confirmation$",), detached_ticket=(r"^Order Entry\b",),
                               order_event=(r"(?i)^order (submitted|rejected|filled)\b",))
SIM_ORDER_LAYOUT = GridLayout(header=("Order ID", "Symbol", "Qty", "Status"), key=("Order ID",), status="Status",
                              symbol="Symbol", qty="Qty")
SIM_LAYOUTS = {"working": SIM_ORDER_LAYOUT, "filled": SIM_ORDER_LAYOUT, "canceled": SIM_ORDER_LAYOUT,
               POSITION_GRID: POSITION_LAYOUT}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _texture(seed: int, rect: Rect) -> np.ndarray:
    img = np.random.default_rng(seed).integers(0, 256, size=(rect.h, rect.w), dtype=np.uint8)
    img.setflags(write=False)
    return img


SIM_TEMPLATES = (SendTemplate("Send", _texture(1, SEND_RECT)),
                 SendTemplate("Confirm and Send", _texture(2, CONFIRM_RECT)))


@lru_cache(maxsize=4)
def _screen(dialog: bool, ticket: bool) -> np.ndarray:
    """The synthetic grab's pixels (choice 2): the dialog's Send and the ticket's Confirm and Send when shown."""
    img = np.full((FRAME_SIZE[1], FRAME_SIZE[0]), 128, dtype=np.uint8)
    for shown, rect, t in ((dialog, SEND_RECT, SIM_TEMPLATES[0]), (ticket, CONFIRM_RECT, SIM_TEMPLATES[1])):
        if shown:
            x, y = rect.x - FRAME_ORIGIN[0], rect.y - FRAME_ORIGIN[1]
            img[y:y + rect.h, x:x + rect.w] = t.image
    img.setflags(write=False)
    return img


def _copy_all(header: Sequence[str], rows: Sequence[dict[str, str]]) -> str:
    return "\n".join(["\t".join(header)] + ["\t".join(r[c] for c in header) for r in rows])


def _money(v: float | None) -> str | None:
    return None if v is None else f"{v:,.2f}"


def default_settings() -> dict[str, Any]:
    """Attestation rows A3 to A8 as the pinned profile expects them."""
    return {"A3": True, "A4": False, "A5": False,
            "A6": (HotkeyRow("Buy at market"), HotkeyRow("Sell at market"), HotkeyRow("Show chart", "Ctrl+Shift+C")),
            "A7": {"at-panel-1": False}, "A8": 0}


@dataclass
class SimScenario:
    """What the simulated rig observes; every field defaults to a clean run."""

    preflight_failures: dict[str, str | None] = field(default_factory=dict)
    launches: list[LaunchObservation] = field(default_factory=list)      # per launch attempt; then a clean launch
    login: LoginObservation = field(default_factory=LoginObservation)
    recovery_login_fails: int = 0                                        # later logins that fail
    selector_reads: list[SelectorRead] = field(default_factory=list)     # queued reads; then the selected account
    selected: str = EMPTY
    select_works: bool = True
    settings: dict[str, Any] = field(default_factory=default_settings)
    unreadable_reads: dict[str, int] = field(default_factory=dict)       # row -> reads that come back unreadable
    confirmation: ConfirmationCheck = field(default_factory=lambda: ConfirmationCheck(True, 0, _sha("a9")))
    notifications: NotificationsRead = field(default_factory=lambda: NotificationsRead(True, False, _sha("a10")))
    workspace: WorkspaceObservation = field(default_factory=lambda: WorkspaceObservation(WORKSPACE_SHA, True, TABS, 1))
    accounts: tuple[AccountState, ...] = (
        AccountState(GOLDEN, "golden", {"AAPL": 100.0, "/MESZ26": 1.0}, 0, 50000.0),
        AccountState(EMPTY, "empty", {}, 0, 2500.0))
    accounts_after_reset: tuple[AccountState, ...] | None = None
    canary: CanaryFlowObservation = field(default_factory=lambda: CanaryFlowObservation(1, True, True, True))
    book: OrderBook = field(default_factory=lambda: OrderBook(position_rows=0, net_liq=2500.0))
    monitor_unreadable: bool = False                                     # every read after the snapshot unreadable
    copy_all_disagrees: bool = False                                     # after the snapshot Copy All misses Working
    buying_power: float | None = None                                    # None: the Net Liq value (choice 4)
    cancel_all_located: bool = True
    teardown: TeardownObservation = field(default_factory=TeardownObservation)
    crash_signals: tuple[CrashSignal, ...] = ()
    hook_dead: bool = False
    rig_id: str = "rig-01"
    config_root: Path | None = None                  # a sentinel-config checkout the gateway enforces (choice 1)
    dismiss_allowlist: tuple[str, ...] | None = None  # pinned dismiss labels, over the configuration's
    deny_list: tuple[DenyEntry, ...] | None = None     # the enforced Explorer deny list, over the configuration's
    kill_root: Path | None = None                    # the file flag at <kill_root>/<rig_id>/run/kill.flag; None: memory
    watchdog: tuple[float, bool] = (1.0, True)       # watchdog.json age in seconds, Watchdog PID alive (10.2)
    free_bytes: int = FREE_BYTES                     # the Watchdog's free-space result (10.2)
    redaction_ocr: Any = None                        # the OCR engine of the H6 probe; None: the probe's known word box
    send_policy: SendPolicy | None = None            # the gated branch's policy: the rig holds a SendGate (6.7)
    dialog_numbers_read: bool = True                 # the Oracle read the Order Confirmation dialog's numbers (6.7)
    extra_windows: tuple[str, ...] = ()              # titles of other app windows shown (an order_event window)
    capture_fails: bool = False                      # every grab fails: the flag is unknown (5.4.6)
    titles: TitleSets = SIM_TITLES                   # the Watcher's title regexes (the popup catalog's sets, 7.8)
    vault: Any = None                                # a ports.VaultPort: the LOGIN secret is a leased secret step
    secret_ref: str = "login.password"               # the LOGIN flow's secret step vault_ref (4.7)


class SimulatedRig(Rig):
    """A deterministic `Rig` driven by a `SimScenario` (module docstring)."""

    def __init__(self, scenario: SimScenario | None = None, *, clock: Clock | None = None):
        self.s = scenario or SimScenario()
        self.clock = clock or ManualClock()
        self.gw = GatewayState()
        self.watcher = WindowWatcher(self.s.titles)
        self.selected = self.s.selected
        self.dialog_open = False
        self.ticket_visible = False
        self.extra_windows = list(self.s.extra_windows)
        self.order_stage = False                  # the sticky order_stage declaration (5.4.6 source 3)
        self.flag_aborts: list[str] = []          # profiles of agent or Explorer actions refused under the flag
        self.attested = False                     # read only without a pushed session (choice 3)
        self.session_safety: SessionSafety | None = None
        self.recovery_only = False
        self.app_running = False
        self.title_build_id: str | None = None    # the build in the main window's title, as last launched
        self.book = self.s.book
        self.calls: list[str] = []
        self.dismiss_allowlist, self.deny_list = self._configuration()
        self.send_gate = SendGate(self.s.send_policy, {a.role: account_id_sha256(a.account_id) for a in self.s.accounts
                                                       if a.role in ACCOUNT_ROLES}) if self.s.send_policy else None
        flag: KillChannel = FileFlagChannel(rig_flag_path(self.s.kill_root, self.s.rig_id)) \
            if self.s.kill_root is not None else MemoryKillChannel()
        self.flag_channel, self.pipe_channel = flag, MemoryKillChannel()
        self._launch_i = 0
        self._logins = 0
        self._reads: dict[str, int] = {}
        self._frame = 0
        self._order_reads = 0
        self._dialog_hwnd = 1000
        self._shown: tuple[bool, bool] | None = None
        self._grabs = 0
        self.session_tokens = 0
        self.allowed = 0
        self.drops = 0
        self.refusals = 0
        self.sent_orders = 0
        self.on_input: Callable[[Action, EmitResult], None] | None = None    # an observer of every decision
        self.leases: list[Any] = []               # the secret steps' vault leases, for the teardown scan (6.9)
        self.secret_results: list[Any] = []       # every LOGIN secret step's executor.secret.SecretResult

    def _configuration(self) -> tuple[frozenset[str], tuple[DenyEntry, ...]]:
        """The dismiss allowlist and Explorer deny list the gateway enforces (choice 1)."""
        root = self.s.config_root
        allow = frozenset(self.s.dismiss_allowlist) if self.s.dismiss_allowlist is not None else (
            load_dismiss_allowlist(root / DISMISS_ALLOWLIST_PATH).labels if root is not None else DISMISS_ALLOWLIST)
        deny = self.s.deny_list if self.s.deny_list is not None else (
            load_deny_list(root)[0] if root is not None else DEFAULT_DENY_LIST)
        return allow, tuple(deny)

    def _log(self, name: str) -> None:
        self.calls.append(name)

    # -- provisioning, launch, login --------------------------------------------------------------------
    def preflight(self) -> PreflightReport:
        """The 12.7 items as the scenario sets them, with H5 round-tripped over the rig's kill channels and H6 run
        through the redaction stage (an item the scenario fails is failed without running)."""
        self._log("preflight")
        items = {i: i not in self.s.preflight_failures for i in PREFLIGHT_ITEMS}
        detail = {k: v for k, v in self.s.preflight_failures.items() if v}
        if items["H5"]:
            h5 = h5_round_trip(self.flag_channel, self.pipe_channel, rig_id=self.s.rig_id, clock=self.clock)
            items["H5"] = h5_passed(h5)
            if not items["H5"]:
                detail["H5"] = h5.detail or "the kill channels did not round-trip"
        if items["H6"]:
            h6 = h6_self_test(H6_THEME, self.s.redaction_ocr)
            items["H6"] = h6.passed
            if not h6.passed:
                detail["H6"] = h6.detail or "the redaction self-test failed"
        return PreflightReport(items, detail)

    def launch(self) -> LaunchObservation:
        self._log("launch")
        i = self._launch_i
        self._launch_i += 1
        obs = self.s.launches[i] if i < len(self.s.launches) else LaunchObservation(True, BUILD, waited_s=40.0)
        self.app_running = obs.frame_found and obs.crash is None
        self.title_build_id = obs.title_build_id
        self._watch()
        return obs

    def read_title_build_id(self) -> str | None:
        """The build the running app's title shows: the one actually launched (None when no app runs)."""
        return self.title_build_id if self.app_running else None

    def login(self) -> LoginObservation:
        """The LOGIN flow (choice 6): its typing through the gateway, then the scenario's outcome (the first login),
        a failed recovery login while `recovery_login_fails` lasts, or a clean one."""
        self._log("login")
        self._logins += 1
        n = len(self.secret_results)
        if not self._type_login():
            vault_failed = len(self.secret_results) > n and self.secret_results[-1].consequence is not None
            return LoginObservation(vault_lease=not vault_failed, main_window_s=None, post_login_anchor=False,
                                    login_dialog_gone=False)
        if self._logins == 1:
            return self.s.login
        if self._logins - 1 <= self.s.recovery_login_fails:
            return LoginObservation(auth_error=True)
        return LoginObservation()

    def _type_login(self) -> bool:
        """The LOGIN flow's input, every action of class `login`; True when every event reached the app."""
        secret = Action("secret", keys=SECRET_KEYS, flow_class="login")
        steps = (Action("click", point=MODE_SWITCH_POINT, resolved_name="paperMoney", under_point_text="paperMoney",
                        flow_class="login"),
                 Action("type_fixture", keys=LOGIN_ID_KEYS, flow_class="login"),
                 secret,
                 Action("click", point=LOG_IN_POINT, resolved_name="Log In", under_point_text="Log In",
                        flow_class="login"))
        for a in steps:
            if a is secret and self.s.vault is not None:
                if not self._secret_step():
                    return False
                continue
            r = self.send_input(a)
            if r.refused is not None or not all(d.allowed for d in r.decisions):
                return False
        return True

    def _secret_step(self) -> bool:
        """The LOGIN flow's `secret` step through `executor.secret.run_secret_step` (choice 6): True when every
        event reached the app; False when the gateway stopped it or the vault lease failed twice (the result's
        `consequence`, which `login` reports as `vault_lease: false`)."""
        from ..executor.secret import run_secret_step           # lazy: the executor imports this package's modules
        step = {"id": "secret", "type": "secret", "target": "login.password-field", "vault_ref": self.s.secret_ref,
                "commit": "none"}
        sent: list[bool] = []

        def emit(action: Action) -> str:
            r = self.send_input(action)
            ok = r.refused is None and all(d.allowed for d in r.decisions)
            sent.append(ok)
            return "passed" if ok else "blocked_by_gateway"

        res = run_secret_step(step, self.s.vault, flow_class="login", emit=emit, capture=_NoCapture(),
                              now=self.clock.monotonic(), sleep=self.clock.sleep)
        self.secret_results.append(res)
        if res.lease is not None:
            self.leases.append(res.lease)
        return res.consequence is None and bool(sent) and all(sent)

    def read_account_selector(self) -> SelectorRead:
        self._log("read_account_selector")
        if self.s.selector_reads:
            return self.s.selector_reads.pop(0)
        return SelectorRead(self.selected, crop_sha256=_sha("selector:" + self.selected))

    def select_account(self, account_id: str) -> None:
        self._log("select_account")
        if self.s.select_works:
            self.selected = account_id

    # -- attestation ------------------------------------------------------------------------------------
    def read_setting(self, item: str) -> SettingRead:
        self._log(f"read_setting:{item}")
        n = self._reads.get(item, 0)
        self._reads[item] = n + 1
        if n < self.s.unreadable_reads.get(item, 0):
            return SettingRead(item, None, landmark_score=0.4, ocr_confidence=30.0, screenshot_sha256=_sha(f"{item}:{n}"))
        return SettingRead(item, self.s.settings[item], screenshot_sha256=_sha(f"{item}:{n}"))

    def reopen_settings(self) -> None:
        self._log("reopen_settings")

    def confirmation_check(self) -> ConfirmationCheck:
        self._log("confirmation_check")
        self.attested = True
        return self.s.confirmation

    def read_notifications(self) -> NotificationsRead:
        self._log("read_notifications")
        return self.s.notifications

    # -- workspace and fixture --------------------------------------------------------------------------
    def load_workspace(self) -> WorkspaceObservation:
        self._log("load_workspace")
        return self.s.workspace

    def verify_account_fixture(self) -> tuple[AccountState, ...]:
        self._log("verify_account_fixture")
        if "reset_empty_account" in self.calls and self.s.accounts_after_reset is not None:
            return self.s.accounts_after_reset
        return self.s.accounts

    def reset_empty_account(self) -> None:
        self._log("reset_empty_account")

    def run_canary_flow(self) -> CanaryFlowObservation:
        self._log("run_canary_flow")
        return self.s.canary

    # -- screen -----------------------------------------------------------------------------------------
    def _grab(self, now: float) -> Frame | None:
        """The synthetic grab of the simulated screen, a new one per poll (choice 2); None when capture fails."""
        if self.s.capture_fails:
            return None
        shown = (self.dialog_open, self.ticket_visible)
        if shown != self._shown:
            self._shown = shown
            self._grabs += 1
        return Frame(_screen(*shown), FRAME_ORIGIN, now - GRAB_EPS_S * (self._grabs % 1000))

    def _watch(self) -> None:
        """One Window Watcher poll (5.4.6): the windows and a grab through `WindowWatcher.poll_into`, which stamps the
        flag and writes the deny rectangles; a second poll when the flag is not clear (choice 2)."""
        now = self.clock.monotonic()
        windows, frame = self.enumerate_windows(), self._grab(now)
        if self.watcher.poll_into(self.gw, windows, frame, SIM_TEMPLATES, now=now, sticky=self.order_stage) \
                != Flag.CLEAR:
            self.watcher.poll_into(self.gw, windows, frame, SIM_TEMPLATES, now=now, sticky=self.order_stage)
        if not self.s.hook_dead:
            self.gw.hook_alive_mono_ts = now

    def poll_flag(self) -> Flag:
        """A fresh Window Watcher poll and the flag it leaves (5.4.6): what the Executor's emitter reads."""
        self._watch()
        return self.gw.effective_flag(self.clock.monotonic())

    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        wins: list[WindowInfo] = []
        if self.dialog_open:
            wins.append(ScreenWindow(self._dialog_hwnd, 4100, "SunAwtDialog", "Order Confirmation", rect=DIALOG_RECT,
                                     z_order=0))
        if self.app_running:
            wins += [ScreenWindow(2000 + i, 4100, "SunAwtDialog", t) for i, t in enumerate(self.extra_windows)]
            wins.append(ScreenWindow(1, 4100, "SunAwtFrame", f"Main@thinkorswim [build {self.title_build_id}]",
                                     rect=MAIN_RECT, z_order=1))
        return tuple(wins)

    def window_tripwires(self) -> TripWires:
        """The Watcher's `order_event` window and the dialog's open time at a fresh poll (6.6 Triggers)."""
        self._watch()
        c = self.watcher.classes
        return TripWires(order_event_window=c.order_event_window, dialog_open_s=c.dialog_open_s(self.clock.monotonic()))

    def capture_frame(self) -> tuple[str, float]:
        return _sha(f"frame:{self._frame}"), self.clock.monotonic()

    def crash_signals(self) -> tuple[CrashSignal, ...]:
        return self.s.crash_signals

    # -- input ------------------------------------------------------------------------------------------
    def _emit(self, a: Action, dialog_ok: bool, send_ok: bool = False) -> list[tuple[InputEvent, Any]]:
        """Stages 5 to 7 for one cleared action: the absolute move (before any token is pending, so the hook's
        move rule allows it), the tokens (a cleared send's click token carries `send_ok`, 6.7), then each event
        through the hook and the app, with a Watcher poll after every event so the hook acts on the flag as the app
        has just changed it (5.4.6). An `executor.primitives.PlannedAction` runs its units in order (choice 7): each
        unit's lead moves, its token write (`primitives.issue_tokens`), then its events."""
        now = self.clock.monotonic()
        steps: list[InputEvent] = []
        out: list[tuple[InputEvent, Any]] = []

        def run(e: InputEvent) -> None:
            out.append((e, self._through(a, e, now)))
            self._watch()

        before = self.gw.tokens_issued
        units = getattr(a, "units", ())
        if units:
            from ..executor.primitives import issue_tokens      # lazy: the executor imports this package's modules
            for unit in units:
                for e in unit.lead:
                    run(e)
                issue_tokens(unit, self.gw, now, dialog_ok=dialog_ok, send_ok=send_ok and
                             sum(1 for t in unit.tokens if t.kind == "click") == 1)
                for em in unit.events:
                    run(em.event)
            if a.flow_class != "login":
                self.session_tokens += self.gw.tokens_issued - before
            return out
        if a.is_pointer and a.point is not None:
            x, y = a.point
            button = "right" if a.kind == "rclick" else "left"
            run(InputEvent("move", x, y))
            self.gw.issue_click(x, y, now, button=button, dialog_ok=dialog_ok, send_ok=send_ok)
            steps = [InputEvent("button_down", x, y, button=button, mods=a.mods),
                     InputEvent("button_up", x, y, button=button, mods=a.mods)]
        for vk in a.keys:
            self.gw.issue_key(vk, now)
            steps += [InputEvent("key_down", vk=vk, mods=a.mods), InputEvent("key_up", vk=vk, mods=a.mods)]
        if a.flow_class != "login":
            self.session_tokens += self.gw.tokens_issued - before
        for e in steps:
            run(e)
        return out

    def _through(self, a: Action | None, e: InputEvent, now: float) -> Any:
        """One event through the hook; an allowed one reaches the simulated app."""
        d = hook_decide(self.gw, e, now)
        self.allowed += d.allowed
        self.drops += not d.allowed
        if d.allowed:
            self._app(a, e)
        return d

    def _app(self, a: Action | None, e: InputEvent) -> None:
        """What the simulated app does with an event that got through."""
        label = {s.lower() for s in a.labels} if a else set()
        if e.type == "button_up" and a is not None:
            if "confirm and send" in label:
                self.dialog_open = True
                self._dialog_hwnd += 1                # a new dialog window: its 30 s age starts now
            elif self.dialog_open and ("delete" in label or "close" in label):
                self.dialog_open = False             # Delete, or the title bar's Close (choice 7): nothing sent
            elif self.dialog_open and (SEND_RECT.contains(e.x, e.y) or "send" in label):
                self._send()
            elif "cancel all" in label and a.teardown_action == "cancel_all":
                self._cancel_working()
        if e.type == "key_down" and self.dialog_open and e.vk in (VK_RETURN, VK_SPACE):
            self._send()
        if e.type == "button_down" and self.dialog_open and SEND_RECT.contains(e.x, e.y):
            self._send()

    def _send(self) -> None:
        self.sent_orders += 1
        self.dialog_open = False
        row = OrderRow(f"sim-order-{self.sent_orders}", "working", "WORKING", "CEI", 1.0)
        self.book = replace(self.book, working=self.book.working + (row,))

    def _cancel_working(self) -> None:
        moved = tuple(replace(r, grid="canceled", status="CANCELED") for r in self.book.working)
        self.book = replace(self.book, working=(), canceled=self.book.canceled + moved)

    def _context(self, action: Action, now: float) -> SafetyContext:
        """Stage 4's context from a fresh poll (5.4.3): the flag, the dialog, the rectangles and chords, the
        configuration's allowlist and deny list, the session, the gated send's gate and the recovery-only arm."""
        title = "Order Confirmation" if self.dialog_open else None
        hit = match_deny(title=title, labels=action.labels, target_kind=action.target_kind, end_kind=action.end_kind,
                         deny_list=self.deny_list) if action.profile != "scripted" else None
        return SafetyContext(
            flag=self.gw.effective_flag(now), order_dialog_present=self.dialog_open, deny_rects=tuple(self.gw.deny_rects),
            chords=tuple(self.gw.chords), dismiss_allowlist=self.dismiss_allowlist,
            attestation_passed=self.attested, paper_assertion_current=True, deny_list_hit=hit,
            hook_alive=self.gw.hook_alive(now), order_stage=self.order_stage, session=self.session_safety, now=now,
            dialog_foreground=self.dialog_open, dialog_read=self.dialog_open and self.s.dialog_numbers_read,
            send_gate=self.send_gate, recovery_only=self.recovery_only)

    def send_input(self, action: Action, *, events: Sequence[InputEvent] = ()) -> EmitResult:
        """5.4.3 stages 4 to 7 for one action (or raw `events`); `on_input`, when set, sees every decision, the LOGIN
        flow's and Cancel All's included (the pipeline's drop-log drainer, 5.4.8)."""
        res = self._send_input(action, events=events)
        if self.on_input is not None:
            self.on_input(action, res)
        return res

    def _send_input(self, action: Action, *, events: Sequence[InputEvent] = ()) -> EmitResult:
        self._watch()
        now = self.clock.monotonic()
        if events:
            return EmitResult(decisions=tuple(self._through(None, e, now) for e in events), events=tuple(events))
        s = slow_half(action, self._context(action, now))
        if s.refused:
            self.refusals += 1
            if s.flag_abort:
                self.flag_aborts.append(action.profile)
            return EmitResult(refused=s)
        if action.order_stage_step and not self.order_stage:
            self.order_stage = True              # the declaration sets the flag before the click is emitted (6.2)
            self._watch()
        out = self._emit(action, s.dialog_ok, s.send_ok)
        self._frame += 1
        if action.teardown_action in ("close_dialog", "clear_ticket") and not (self.dialog_open or self.ticket_visible):
            self.order_stage = False             # the teardown completed and the dialog and ticket are absent
        self._watch()
        return EmitResult(decisions=tuple(d for _, d in out), events=tuple(e for e, _ in out))

    def set_hook_state(self, *, armed: bool, deny_all: bool, recovery_only: bool = False) -> None:
        self._log(f"set_hook_state:{armed}:{deny_all}" + (":recovery_only" if recovery_only else ""))
        self.gw.armed, self.gw.deny_all = armed, deny_all
        self.recovery_only = recovery_only

    def set_session_safety(self, safety: SessionSafety) -> None:
        self.session_safety = safety

    def reset_input_state(self) -> bool:
        """A fresh gateway instance (choice 5): every token and the drag's move window of the dead tree are gone."""
        self._log("reset_input_state")
        self.gw.tokens.clear()
        self.gw.move_allow = None
        return True

    def load_order_chords(self, chords: Sequence[Chord]) -> None:
        self._log("load_order_chords")
        self.gw.load_chords(list(chords))

    def gateway_log(self) -> GatewayLog:
        return GatewayLog(self.session_tokens, self.allowed, self.drops, self.refusals)

    def declared_sends(self) -> tuple[DeclaredSend, ...]:
        return tuple(self.send_gate.declared) if self.send_gate is not None else ()

    # -- orders and containment -------------------------------------------------------------------------
    def cancel_all(self) -> CancelAllResult:
        """The `cancel_all` teardown action: located by template, then clicked through the gateway (6.6)."""
        self._log("cancel_all")
        if not self.locate_cancel_all():
            return CancelAllResult(False)
        r = self.send_input(Action("click", point=CANCEL_ALL_POINT, resolved_name="Cancel All",
                                   under_point_text="Cancel All", teardown_action="cancel_all"))
        return CancelAllResult(True, clicked=r.refused is None and bool(r.decisions)
                               and all(d.allowed for d in r.decisions))

    def locate_cancel_all(self) -> bool:
        return self.app_running and self.s.cancel_all_located

    def read_orders(self) -> MonitorBook:
        """The Monitor book rendered as grid OCR rows and Copy All text and read back through
        `orderbook.build_order_book` (choice 4); with `monitor_unreadable` every read after the run-start snapshot
        has an unreadable Working grid, with `copy_all_disagrees` its Copy All misses the Working rows."""
        self._log("read_orders")
        self._order_reads += 1
        later = self._order_reads > 1
        b = self.book
        cells: dict[str, list[dict[str, str]]] = {
            g: [{"Order ID": r.key, "Symbol": r.symbol, "Qty": f"{r.qty:g}", "Status": r.status} for r in b.rows(g)]
            for g in ORDER_GRIDS}
        cells[POSITION_GRID] = [{"Instrument": f"POS-{i}", "Qty": "1", "Trade Price": "1.00", "Mark": "1.00"}
                                for i in range(1, b.position_rows + 1)]
        text: dict[str, str | None] = {g: _copy_all(SIM_LAYOUTS[g].header, rows) for g, rows in cells.items()}
        ocr: dict[str, list[dict[str, str]] | None] = dict(cells)
        if (self.s.copy_all_disagrees and later) or not b.copy_all_agrees:
            text["working"] = _copy_all(SIM_ORDER_LAYOUT.header, [])
        if (self.s.monitor_unreadable and later) or not b.readable:
            ocr["working"] = None
        bp = self.s.buying_power if self.s.buying_power is not None else b.net_liq
        return build_order_book(ocr, text, net_liq=_money(b.net_liq), buying_power=_money(bp), layouts=SIM_LAYOUTS,
                                screenshot_sha256=_sha(f"working-orders:{self._order_reads}"))

    def kill_process_tree(self) -> bool:
        self._log("kill_process_tree")
        self.app_running = False
        self.dialog_open = False
        self.ticket_visible = False              # the docked ticket and every app window go with the tree
        self.extra_windows.clear()
        self.order_stage = False                 # the flow that declared it is gone with the tree
        return True

    def teardown(self) -> TeardownObservation:
        self._log("teardown")
        return self.s.teardown

    def revert_snapshot(self) -> None:
        self._log("revert_snapshot")

    # -- the rig host -----------------------------------------------------------------------------------
    def kill_channels(self) -> tuple[KillChannel, KillChannel]:
        return self.flag_channel, self.pipe_channel

    def watchdog_status(self) -> tuple[float, bool]:
        return self.s.watchdog

    def free_bytes(self) -> int:
        return self.s.free_bytes


class _NoCapture:
    """The LOGIN flow runs before any capture: there is nothing to suspend (`executor.secret.CaptureControl`)."""

    def suspend_capture(self) -> None:
        return None

    def resume_capture(self) -> None:
        return None


class MemorySink:
    """An in-memory `OutcomeSink`: scoped checkpoint ids and the outcome written for each (8.2 stand-in)."""

    def __init__(self, scope: Sequence[str]):
        self.scope = list(scope)
        self.outcomes: dict[str, tuple[str, str | None, str | None, str | None]] = {}

    def record(self, checkpoint_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None) -> None:
        self.outcomes[checkpoint_id] = (outcome, reason, gate, None)

    def fill_unreached(self, run_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None,
                       detail: str | None = None) -> list[str]:
        filled = [c for c in self.scope if c not in self.outcomes]
        for c in filled:
            self.outcomes[c] = (outcome, reason, gate, detail)
        return filled


def default_context(run_id: str = "pr-48213-3", run_type: str = "pr", **overrides: Any) -> RunContext:
    """The `RunContext` that matches `SimScenario()`'s defaults (the example config's rig-01, 4.9)."""
    ctx = RunContext(
        run_id=run_id, run_type=run_type, shard_id="shard-1", expected_build=BUILD,
        allowlist=Allowlist("rig-01", (GOLDEN, EMPTY), _sha("allowlist")), fixture_account=EMPTY,
        profile=AttestationProfile(active_trader_panels=("at-panel-1",), sha256=_sha("profile")),
        fixture={GOLDEN: AccountExpectation("golden", {"AAPL": 100.0, "/MESZ26": 1.0}, 50000.0, 5000.0),
                 EMPTY: AccountExpectation("empty", {}, 2500.0, 250.0)},
        workspace=WorkspaceExpectation(WORKSPACE_SHA, TABS, 1),
        default_order_chords=(Chord.parse("Shift+B"), Chord.parse("Shift+S")))
    return replace(ctx, **overrides)
