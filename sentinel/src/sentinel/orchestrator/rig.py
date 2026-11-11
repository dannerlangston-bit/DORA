"""The `Rig` interface: every capability the Orchestrator needs from Windows and thinkorswim.

This is the contract the real Windows rig implements later (pywinauto, `SendInput`, the hook host,
OCR and template matching, psutil, the hypervisor guest channel) and `SimulatedRig` implements now.
Each method names the spec section that requires it and returns a plain observation; every decision
over those observations is made by the platform-independent code of this package, never by the rig.

The observation types are frozen dataclasses so a gate check is a pure function of what the rig saw.

The input side holds the gateway's state for the Orchestrator: the hook's phase (`set_hook_state`, and
`set_hook_phase` over `gateway.HOOK_STATES`, 6.6), the session's safety state the slow half reads
(`set_session_safety`, 5.4.3 stage 4, SD-57), the gated branch's declared sends (`declared_sends`, 6.7) and the
Window Watcher's trip-wires (`window_tripwires`, 6.6). The kill switch's two human channels (`kill_channels`), the
Watchdog's reverse heartbeat (`watchdog_status`) and free space (`free_bytes`) are what the Orchestrator reads at
its boundaries (5.2, 10.2).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .gateway import HOOK_STATES, Action, Chord, InputEvent, SlowDecision

if TYPE_CHECKING:                      # ports and killswitch import this module; only the types are needed here
    from ..records import DeclaredSend
    from .killswitch import TripWires
    from .ports import KillChannel, SessionSafety


# -- observations -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PreflightReport:
    """The 12.7 checklist after the one retry per item: item id -> passed; `detail` names a blocking control."""

    items: Mapping[str, bool]
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WindowInfo:
    """One top-level window of the process tree (5.4.6 enumeration, 7.3 title read)."""

    hwnd: int
    pid: int
    cls: str
    title: str
    visible: bool = True


@dataclass(frozen=True)
class CrashSignal:
    """An app failure signal (10.2 sampling 3, 5 and 6). `cause` is one of 4.13's `app_alive` causes."""

    cause: str                          # jvm_crash | hang | oom | process_exit
    source: str                         # screenshot_staleness | crash_file | heap_dump | wer_dump | event_log | empty_tree
    launcher_update: bool = False       # a launcher update ran in the failure window (F44)
    build_manifest_changed: bool = False


@dataclass(frozen=True)
class LaunchObservation:
    """The `launched` gate's reads (5.2, 7.3)."""

    frame_found: bool                   # a visible SunAwtFrame matching the main-window regex within the wait
    title_build_id: str | None = None   # GetWindowText; None when the regex did not match
    about_build_id: str | None = None   # Help > About OCR, only when the regex did not match
    waited_s: float = 0.0
    update_progress: bool = False       # POP-01 visible (7.7 step 1)
    crash: CrashSignal | None = None
    windows: tuple[WindowInfo, ...] = ()
    processes: tuple[str, ...] = ()
    screenshot_sha256: str | None = None
    jab_properties_present: bool | None = None   # 7.5: .accessibility.properties in the profile (Branch A); None unread
    jab_query_answers: bool | None = None        # 7.5: one JAB query answered (Branch A); None unread


@dataclass(frozen=True)
class LoginObservation:
    """The LOGIN flow's exits (7.7) for the `authenticated` gate."""

    mode_switch_located: bool = True
    live_selected: bool = False         # still Live after one click on paperMoney
    remember_password_off: bool = True
    vault_lease: bool = True
    challenge: bool = False             # POP-21, or the Branch B prompt with no code inside the window
    auth_error: bool = False            # bad credential, lockout, disabled login
    session_kicked: bool = False        # POP-04
    main_window_s: float | None = 5.0   # seconds to the main window; None when absent within the wait
    post_login_anchor: bool = True
    login_dialog_gone: bool = True
    screenshot_sha256: str | None = None


@dataclass(frozen=True)
class SelectorRead:
    """One read of the account selector (6.4): text, landmark and OCR confidence (JAB reads report 100)."""

    text: str | None
    landmark_found: bool = True
    confidence: float = 100.0
    crop_sha256: str | None = None


@dataclass(frozen=True)
class HotkeyRow:
    """One row of the Hot Keys list (A6): the action label and its binding; None or '' is unassigned."""

    action: str
    binding: str | None = None


@dataclass(frozen=True)
class SettingRead:
    """One attestation row read (6.3): page landmark score, label OCR confidence and the value.

    `value` is a bool for toggles and checkboxes (None when the state template matched neither state),
    a tuple of `HotkeyRow` for A6, a mapping panel -> Auto Send state for A7 and the row count for A8.
    """

    item: str
    value: Any
    landmark_score: float = 1.0
    ocr_confidence: float = 100.0
    screenshot_sha256: str | None = None


@dataclass(frozen=True)
class ConfirmationCheck:
    """A9 (6.3): did the Order Confirmation dialog appear after `Confirm and Send`, and new Working rows."""

    dialog_appeared: bool               # by title and template; a window whose title cannot be read is False
    working_new_rows: int = 0
    screenshot_sha256: str | None = None


@dataclass(frozen=True)
class NotificationsRead:
    """A10 (6.3, RC runs): the Explorer login id and its Notifications page."""

    explorer_login_distinct: bool
    verified_contact: bool | None       # None when the Notifications page is unreadable
    screenshot_sha256: str | None = None


@dataclass(frozen=True)
class WorkspaceObservation:
    """The `workspace_loaded` gate (5.2, 2.2.3)."""

    file_sha256: str
    anchor_present: bool
    tabs: tuple[str, ...]
    window_count: int


@dataclass(frozen=True)
class AccountState:
    """`verify_account_fixture` for one account (7.6): positions by symbol, working orders and cash."""

    account_id: str
    role: str                           # golden | empty
    positions: Mapping[str, float]
    working_orders: int
    cash: float


@dataclass(frozen=True)
class CanaryFlowObservation:
    """The 60-second smoke flow of `canary_flow_passed` (5.2)."""

    locator_rung: int | None            # the rung the locator resolved at; None when it failed
    settled: bool
    within_floor: bool
    calibration_exact: bool
    zoom_scale_ok: bool | None = None   # agent mode enabled only


@dataclass(frozen=True)
class OrderRow:
    """One Monitor row (6.6): a stable key, its grid and status, and the run's own tag when it carries one."""

    key: str
    grid: str                           # working | filled | canceled
    status: str = ""
    symbol: str = ""
    qty: float = 0.0
    tag: str | None = None


@dataclass(frozen=True)
class OrderBook:
    """A Monitor read (6.6, 7.6): Working, Filled and Canceled rows, Position Statement rows and Net Liq.

    `readable` is False when a grid could not be read; `copy_all_agrees` is the grid-OCR versus Copy All
    cross-check, whose disagreement is `monitor_unreadable`.
    """

    working: tuple[OrderRow, ...] = ()
    filled: tuple[OrderRow, ...] = ()
    canceled: tuple[OrderRow, ...] = ()
    position_rows: int = 0
    net_liq: float | None = None
    readable: bool = True
    copy_all_agrees: bool = True

    def rows(self, grid: str) -> tuple[OrderRow, ...]:
        return {"working": self.working, "filled": self.filled, "canceled": self.canceled}[grid]


@dataclass(frozen=True)
class CancelAllResult:
    """The `cancel_all` teardown action (4.7, 6.6): was the control located, and was it clicked."""

    located: bool
    clicked: bool = False


@dataclass(frozen=True)
class TeardownObservation:
    """The teardown steps of 5.2 other than the Monitor diff: each one's success."""

    reset_input_state_ok: bool = True
    cancel_all_verified: bool = True
    logout_ok: bool = True
    logs_collected: bool = True
    secret_scan_ran: bool = True
    secret_found: bool = False


@dataclass(frozen=True)
class GatewayLog:
    """The gateway's audit trail summary (6.2, 6.6 proof): tokens issued, events allowed, drops, refusals."""

    tokens_issued: int = 0
    allowed: int = 0
    drops: int = 0
    refusals: int = 0


@dataclass(frozen=True)
class EmitResult:
    """What `Rig.send_input` did: refused in the slow half, or each event's hook decision (5.4.3 stages 4 to 7)."""

    refused: SlowDecision | None = None
    decisions: tuple[Any, ...] = ()     # gateway.HookDecision per emitted event
    events: tuple[InputEvent, ...] = ()

    @property
    def refused_reason(self) -> str | None:
        return self.refused.reason if self.refused else None


# -- the interface ----------------------------------------------------------------------------------------
class Rig(ABC):
    """Every capability the platform-independent Orchestrator needs from a Windows rig (module docstring)."""

    # provisioning (5.3, 12.7)
    @abstractmethod
    def preflight(self) -> PreflightReport:
        """Run the 32 preflight items on the reverted rig, each retried once after 60 s (12.7, 5.3)."""

    # launch and build id (5.2 `launched`, 7.3)
    @abstractmethod
    def launch(self) -> LaunchObservation:
        """Start the launcher, dismiss first-launch dialogs via the popup catalog and wait up to 5 minutes for
        the main `SunAwtFrame`; report the window and process lists and any crash or hang signal (5.2, 10.2)."""

    @abstractmethod
    def read_title_build_id(self) -> str | None:
        """The build id from the main window title by `GetWindowText`, no OCR; None when the regex does not
        match (7.3). Re-read after every relaunch and before each flow (5.2)."""

    # login and identity (7.7, 6.4)
    @abstractmethod
    def login(self) -> LoginObservation:
        """Run the scripted LOGIN flow once: paperMoney switch asserted before typing, secret steps, MFA branch,
        post-login popups (7.7). Never retried, never handed to agent mode."""

    @abstractmethod
    def read_account_selector(self) -> SelectorRead:
        """Read the account selector text: JAB value under DR-01 Branch A, OCR at 3x upscale otherwise (6.4)."""

    @abstractmethod
    def select_account(self, account_id: str) -> None:
        """Select the fixture account explicitly (7.7 step 7, attestation A2)."""

    # attestation (6.3)
    @abstractmethod
    def read_setting(self, item: str) -> SettingRead:
        """Navigate to and read one attestation row A3 to A8 by OCR and template from Application Settings,
        the Hot Keys tab, each Active Trader panel or Monitor (6.3, 12.6). Never changes a setting."""

    @abstractmethod
    def reopen_settings(self) -> None:
        """Close Application Settings and reopen it, for the second read of an unreadable row (6.3)."""

    @abstractmethod
    def confirmation_check(self) -> ConfirmationCheck:
        """A9: build the drill ticket, click `Confirm and Send` as an order-stage step, assert the dialog, click
        `Delete` through the dismiss allowlist, close the dialog and clear the ticket (6.3)."""

    @abstractmethod
    def read_notifications(self) -> NotificationsRead:
        """A10 on RC runs: the Explorer login id against the scripted ids, and its Notifications page (6.3)."""

    # workspace and fixture (5.2, 7.6)
    @abstractmethod
    def load_workspace(self) -> WorkspaceObservation:
        """Load the corpus workspace of the work item and run the registry probes P1 to P8 (5.2, 2.2.3)."""

    @abstractmethod
    def verify_account_fixture(self) -> tuple[AccountState, ...]:
        """`verify_account_fixture` by export and OCR, cross-checked with Copy All, for both accounts (7.6)."""

    @abstractmethod
    def reset_empty_account(self) -> None:
        """Reset the EMPTY account through Adjust Account and set its cash floor (7.6 stage 2 step 3)."""

    @abstractmethod
    def run_canary_flow(self) -> CanaryFlowObservation:
        """The 60-second smoke flow of `canary_flow_passed` (5.2)."""

    # the screen (5.4.6, 5.7, 10.2)
    @abstractmethod
    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        """`EnumWindows` filtered to the process tree, visible, class SunAwtFrame, SunAwtDialog or SunAwtWindow
        (5.4.6); the Window Watcher's 100 ms poll and the stage 4 synchronous re-check."""

    @abstractmethod
    def capture_frame(self) -> tuple[str, float]:
        """Hash of the main window region and its monotonic time: the Watchdog's staleness sample (10.2)."""

    @abstractmethod
    def crash_signals(self) -> tuple[CrashSignal, ...]:
        """New crash files, heap or WER dumps, Event Log 1000 or 1002, an empty tree, an OOM log line (10.2)."""

    # input (5.4)
    @abstractmethod
    def send_input(self, action: Action, *, events: Sequence[InputEvent] = ()) -> EmitResult:
        """Run one action through the gateway: slow half, tokens, `SendInput`, the hook (5.4.3). `events`
        injects raw events from another origin instead (the fire drill injector, 6.5)."""

    @abstractmethod
    def set_hook_state(self, *, armed: bool, deny_all: bool, recovery_only: bool = False) -> None:
        """Write `armed` and `deny_all` in the shared header (5.4.2); the kill switch's first step (6.6).
        `recovery_only` is the recovery arm of 6.6 step 5: the slow half then tokens only what
        `gateway.recovery_admits` (the scripted login and the recovery teardown actions); every other write
        clears it."""

    def set_hook_phase(self, phase: str) -> None:
        """Put the hook in one phase of `gateway.HOOK_STATES` (`running`, `kill_switch`, `recovery`, `between_runs`;
        gateway.py choice 12); `recovery` also sets the recovery-only arm."""
        armed, deny_all = HOOK_STATES[phase]
        self.set_hook_state(armed=armed, deny_all=deny_all, recovery_only=phase == "recovery")

    @abstractmethod
    def set_session_safety(self, safety: SessionSafety) -> None:
        """The session's safety state the slow half reads from now on (attestation stage, last paper-mode assertion,
        session kicked; 5.4.3 stage 4, 6.3, 6.4, SD-57): the Orchestrator's `safety_listener`."""

    @abstractmethod
    def reset_input_state(self) -> bool:
        """`reset_input_state` through a fresh gateway instance: release tokens for Shift, Ctrl, Alt, Win and every
        mouse button, nothing of the dead tree's input left pending (5.4.10, 7.7, 10.2 step 4); True when done. The
        relaunch runs it right after `kill_process_tree`."""

    @abstractmethod
    def declared_sends(self) -> tuple[DeclaredSend, ...]:
        """The gated branch's declared sends of this run, as its `SendGate` recorded them (6.7); () while the send
        policy is disabled. The post-run proof lists them and the trip-wires do not count their Working rows."""

    @abstractmethod
    def window_tripwires(self) -> TripWires:
        """The Window Watcher's trip-wires at a fresh poll (5.4.6, 6.6 Triggers): an `order_event` window, and how
        long the oldest Order Confirmation dialog has been visible (`order_event_window`, `dialog_open_s`)."""

    @abstractmethod
    def load_order_chords(self, chords: Sequence[Chord]) -> None:
        """Load the order-hotkey list into the hook's chord entries at `settings_attested` (5.4.7)."""

    @abstractmethod
    def gateway_log(self) -> GatewayLog:
        """The gateway audit trail summary for the run (6.2, the proof's gateway line and its no-token test)."""

    # orders and containment (6.6)
    @abstractmethod
    def cancel_all(self) -> CancelAllResult:
        """Locate and click `Cancel All` in Working Orders through the `cancel_all` teardown action (6.6)."""

    @abstractmethod
    def locate_cancel_all(self) -> bool:
        """`Cancel All` in Working Orders located by template, never clicked (6.5 item 5, 6.6)."""

    @abstractmethod
    def read_orders(self) -> OrderBook:
        """Read Working, Filled and Canceled Orders, Position Statement, Net Liq and buying power; grid OCR
        cross-checked with Copy All (`orderbook.build_order_book`; 6.6, 7.6)."""

    @abstractmethod
    def kill_process_tree(self) -> bool:
        """Kill the thinkorswim tree by PID, children first, the hook host last when the Orchestrator is dead;
        True when nothing from the install folder survives (6.6 step 3, 10.2)."""

    @abstractmethod
    def teardown(self) -> TeardownObservation:
        """`reset_input_state`, verified Cancel All, logout, log collection and the plaintext secret scan (5.2)."""

    @abstractmethod
    def revert_snapshot(self) -> None:
        """Revert the rig to its golden snapshot (6.6 step 6, 7.2)."""

    # the rig host (5.14, 6.6, 10.2)
    @abstractmethod
    def kill_channels(self) -> tuple[KillChannel, KillChannel]:
        """The kill switch's human channels on this rig (6.6 Triggers): the file flag (`killflag.FLAG_PATH`) and the
        named pipe (`killflag.PIPE_NAME`), polled at every step and flow boundary and proven by preflight H5."""

    @abstractmethod
    def watchdog_status(self) -> tuple[float, bool]:
        """The Watchdog's reverse heartbeat `watchdog.json`: its age in seconds and whether the Watchdog PID is alive
        (10.2, 5.14), read at every gate and flow boundary."""

    @abstractmethod
    def free_bytes(self) -> int:
        """Free bytes on the rig's artifact volume, the Watchdog's free-space result (10.2), read at every flow
        boundary."""


def win32_input(api: Any = None) -> Any:
    """The rig's `ports.InputPort` over `SendInput` (`gateway.emitter_win32.Win32Emitter`), for the Win32 rig
    composition (`orchestrator.win32rig`). The rig is one of the three places allowed to import the emitter (5.4.9,
    `lint.code.EMITTER_IMPORTERS`); the import happens here when called, never at module import (Windows only)."""
    from ..gateway.emitter_win32 import Win32Emitter
    return Win32Emitter(api)
