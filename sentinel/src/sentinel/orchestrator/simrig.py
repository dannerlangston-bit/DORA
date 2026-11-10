"""`SimulatedRig`: a scriptable, deterministic `Rig` for tests and the demo (no Windows, no thinkorswim).

A `SimScenario` holds what each capability returns (preflight results, launch outcomes, the login, the
selector reads, the settings rows, the workspace, the accounts, the Monitor book, teardown). Input runs
through the real decision code of gateway.py: `send_input` applies `slow_half`, writes tokens into a
`GatewayState` and passes every event through `hook_decide`, and a small simulated app reacts to what
gets through: a visible docked ticket (`ticket_visible`) sets the flag through the ticket detector,
`Confirm and Send` opens the Order Confirmation dialog (the Window Watcher then sets the
flag and writes the Send deny rectangle), `Delete` closes it, and an Enter, a Space or a click on `Send`
that reaches the app while the dialog is open sends an order, which appears as a new Working row. So a
bypass that the gateway failed to stop is visible to the post-run proof exactly as on a real rig.

As on a real rig, the flag has three sources (5.4.6): the dialog by enumeration, the docked ticket by the
detector, and the flow's sticky `order_stage` declaration. A cleared action of an order-stage step sets the
sticky flag before its first event is emitted; a `close_dialog` or `clear_ticket` teardown action that
completes with the dialog and the ticket absent releases it, and so does killing the process tree. The
Watcher is polled again after every emitted event, so the hook reads the flag per event: an Enter that
follows the `Confirm and Send` click in the same action meets the open dialog's flag. An agent or Explorer
action refused because the flag is up is listed in `flag_aborts` (the caller takes the 5.4.6 path).

`MemorySink` is an in-memory `OutcomeSink` over a list of scoped checkpoint ids, and `default_context`
builds the `RunContext` that matches the default scenario (the example config's rig-01 accounts).
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .attestation import AttestationProfile
from .clock import Clock, ManualClock
from .gateway import (VK_RETURN, VK_SPACE, Action, Chord, DenyRect, Flag, FlagWatcher, GatewayState, InputEvent,
                      Rect, SafetyContext, hook_decide, slow_half)
from .explorer import match_deny
from .gates import PREFLIGHT_ITEMS, AccountExpectation, WorkspaceExpectation
from .identity import Allowlist
from .machine import RunContext
from .rig import (AccountState, CanaryFlowObservation, CancelAllResult, ConfirmationCheck, CrashSignal, EmitResult,
                  GatewayLog, HotkeyRow, LaunchObservation, LoginObservation, NotificationsRead, OrderBook, OrderRow,
                  PreflightReport, Rig, SelectorRead, SettingRead, TeardownObservation, WindowInfo,
                  WorkspaceObservation)

BUILD = "2026.44.1"
GOLDEN, EMPTY = "D-20481133", "D-20481134"
WORKSPACE_SHA = hashlib.sha256(b"ws.tier-a.active-trader").hexdigest()
TABS = ("Monitor", "Trade", "Charts")
SEND_RECT = Rect(900, 600, 80, 28)          # the Send button of the simulated Order Confirmation dialog
DELETE_POINT = (1010, 614)
CONFIRM_POINT = (1500, 900)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
    monitor_unreadable: bool = False
    cancel_all_located: bool = True
    teardown: TeardownObservation = field(default_factory=TeardownObservation)
    crash_signals: tuple[CrashSignal, ...] = ()
    hook_dead: bool = False


class SimulatedRig(Rig):
    """A deterministic `Rig` driven by a `SimScenario` (module docstring)."""

    def __init__(self, scenario: SimScenario | None = None, *, clock: Clock | None = None):
        self.s = scenario or SimScenario()
        self.clock = clock or ManualClock()
        self.gw = GatewayState()
        self.watcher = FlagWatcher()
        self.selected = self.s.selected
        self.dialog_open = False
        self.ticket_visible = False
        self.order_stage = False                  # the sticky order_stage declaration (5.4.6 source 3)
        self.flag_aborts: list[str] = []          # profiles of agent or Explorer actions refused under the flag
        self.attested = False
        self.app_running = False
        self.book = self.s.book
        self.calls: list[str] = []
        self._launch_i = 0
        self._logins = 0
        self._reads: dict[str, int] = {}
        self._frame = 0
        self._order_reads = 0
        self.session_tokens = 0
        self.allowed = 0
        self.drops = 0
        self.refusals = 0
        self.sent_orders = 0

    def _log(self, name: str) -> None:
        self.calls.append(name)

    # -- provisioning, launch, login --------------------------------------------------------------------
    def preflight(self) -> PreflightReport:
        self._log("preflight")
        items = {i: i not in self.s.preflight_failures for i in PREFLIGHT_ITEMS}
        return PreflightReport(items, {k: v for k, v in self.s.preflight_failures.items() if v})

    def launch(self) -> LaunchObservation:
        self._log("launch")
        i = self._launch_i
        self._launch_i += 1
        obs = self.s.launches[i] if i < len(self.s.launches) else LaunchObservation(True, BUILD, waited_s=40.0)
        self.app_running = obs.frame_found and obs.crash is None
        self._watch()
        return obs

    def read_title_build_id(self) -> str | None:
        return BUILD if self.app_running else None

    def login(self) -> LoginObservation:
        self._log("login")
        self._logins += 1
        if self._logins == 1:
            return self.s.login
        if self._logins - 1 <= self.s.recovery_login_fails:
            return LoginObservation(auth_error=True)
        return LoginObservation()

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
    def _watch(self) -> None:
        """One Window Watcher poll: the dialog sets the flag and its Send button is a deny rectangle (5.4.6)."""
        now = self.clock.monotonic()
        flag = self.watcher.poll(enumeration_hit=self.dialog_open, detector_hit=self.ticket_visible,
                                 sticky=self.order_stage)
        if not (self.dialog_open or self.ticket_visible or self.order_stage) and flag != Flag.CLEAR:
            flag = self.watcher.poll(enumeration_hit=False, detector_hit=False, sticky=False)
        self.gw.stamp_watcher(flag, now)
        self.gw.write_deny_rects([DenyRect(SEND_RECT, "Send")] if self.dialog_open else [])
        if not self.s.hook_dead:
            self.gw.hook_alive_mono_ts = now

    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        wins = [WindowInfo(1, 4100, "SunAwtFrame", f"Main@thinkorswim [build {BUILD}]")] if self.app_running else []
        if self.dialog_open:
            wins.append(WindowInfo(2, 4100, "SunAwtDialog", "Order Confirmation"))
        return tuple(wins)

    def capture_frame(self) -> tuple[str, float]:
        return _sha(f"frame:{self._frame}"), self.clock.monotonic()

    def crash_signals(self) -> tuple[CrashSignal, ...]:
        return self.s.crash_signals

    # -- input ------------------------------------------------------------------------------------------
    def _emit(self, a: Action, dialog_ok: bool) -> list[tuple[InputEvent, Any]]:
        """Stages 5 to 7 for one cleared action: the absolute move (before any token is pending, so the hook's
        move rule allows it), the tokens, then each event through the hook and the app, with a Watcher poll
        after every event so the hook acts on the flag as the app has just changed it (5.4.6)."""
        now = self.clock.monotonic()
        steps: list[InputEvent] = []
        out: list[tuple[InputEvent, Any]] = []

        def run(e: InputEvent) -> None:
            out.append((e, self._through(a, e, now)))
            self._watch()

        before = self.gw.tokens_issued
        if a.is_pointer and a.point is not None:
            x, y = a.point
            button = "right" if a.kind == "rclick" else "left"
            run(InputEvent("move", x, y))
            self.gw.issue_click(x, y, now, button=button, dialog_ok=dialog_ok)
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
            elif self.dialog_open and "delete" in label:
                self.dialog_open = False
            elif self.dialog_open and (SEND_RECT.contains(e.x, e.y) or "send" in label):
                self._send()
        if e.type == "key_down" and self.dialog_open and e.vk in (VK_RETURN, VK_SPACE):
            self._send()
        if e.type == "button_down" and self.dialog_open and SEND_RECT.contains(e.x, e.y):
            self._send()

    def _send(self) -> None:
        self.sent_orders += 1
        self.dialog_open = False
        row = OrderRow(f"sim-order-{self.sent_orders}", "working", "WORKING", "CEI", 1.0)
        self.book = replace(self.book, working=self.book.working + (row,))

    def send_input(self, action: Action, *, events: Sequence[InputEvent] = ()) -> EmitResult:
        self._watch()
        now = self.clock.monotonic()
        if events:
            return EmitResult(decisions=tuple(self._through(None, e, now) for e in events), events=tuple(events))
        title = "Order Confirmation" if self.dialog_open else None
        ctx = SafetyContext(flag=self.gw.effective_flag(now), order_dialog_present=self.dialog_open,
                            deny_rects=tuple(self.gw.deny_rects), chords=tuple(self.gw.chords),
                            attestation_passed=self.attested, paper_assertion_current=True,
                            deny_list_hit=match_deny(title=title, labels=action.labels, target_kind=action.target_kind,
                                                     end_kind=action.end_kind)
                            if action.profile != "scripted" else None, hook_alive=self.gw.hook_alive(now),
                            order_stage=self.order_stage)
        s = slow_half(action, ctx)
        if s.refused:
            self.refusals += 1
            if s.flag_abort:
                self.flag_aborts.append(action.profile)
            return EmitResult(refused=s)
        if action.order_stage_step and not self.order_stage:
            self.order_stage = True              # the declaration sets the flag before the click is emitted (6.2)
            self._watch()
        out = self._emit(action, s.dialog_ok)
        self._frame += 1
        if action.teardown_action in ("close_dialog", "clear_ticket") and not (self.dialog_open or self.ticket_visible):
            self.order_stage = False             # the teardown completed and the dialog and ticket are absent
        self._watch()
        return EmitResult(decisions=tuple(d for _, d in out), events=tuple(e for e, _ in out))

    def set_hook_state(self, *, armed: bool, deny_all: bool) -> None:
        self._log(f"set_hook_state:{armed}:{deny_all}")
        self.gw.armed, self.gw.deny_all = armed, deny_all

    def load_order_chords(self, chords: Sequence[Chord]) -> None:
        self._log("load_order_chords")
        self.gw.load_chords(list(chords))

    def gateway_log(self) -> GatewayLog:
        return GatewayLog(self.session_tokens, self.allowed, self.drops, self.refusals)

    # -- orders and containment -------------------------------------------------------------------------
    def cancel_all(self) -> CancelAllResult:
        self._log("cancel_all")
        if not self.s.cancel_all_located:
            return CancelAllResult(False)
        rows = self.book.working
        moved = tuple(replace(r, grid="canceled", status="CANCELED") for r in rows)
        self.book = replace(self.book, working=(), canceled=self.book.canceled + moved)
        return CancelAllResult(True, clicked=bool(rows))

    def read_orders(self) -> OrderBook:
        """The Monitor book; with `monitor_unreadable` every read after the run-start snapshot is unreadable."""
        self._log("read_orders")
        self._order_reads += 1
        if self.s.monitor_unreadable and self._order_reads > 1:
            return replace(self.book, readable=False)
        return self.book

    def kill_process_tree(self) -> bool:
        self._log("kill_process_tree")
        self.app_running = False
        self.dialog_open = False
        self.order_stage = False                 # the flow that declared it is gone with the tree
        return True

    def teardown(self) -> TeardownObservation:
        self._log("teardown")
        return self.s.teardown

    def revert_snapshot(self) -> None:
        self._log("revert_snapshot")


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
