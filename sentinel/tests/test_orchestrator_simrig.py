"""The simulated rig's foundation ports (rig.py, simrig.py): the Window Watcher on a synthetic screen, the session's
safety state, the configuration the gateway enforces, the gated send, the order book reader, login typing and
Cancel All through the gateway, the kill channels, preflight H5 and H6, the Watchdog's reads (5.4, 6.2 to 6.7, 10.2,
12.7)."""
from __future__ import annotations

from pathlib import Path

from sentinel.orchestrator.gateway import DENY_INFLATE_PX, DISMISS_ALLOWLIST, Action, Flag, SafetyContext, slow_half
from sentinel.orchestrator.gateway_config import DISMISS_ALLOWLIST_PATH, load_dismiss_allowlist
from sentinel.orchestrator.identity import account_id_sha256
from sentinel.orchestrator.killflag import FileFlagChannel, MemoryKillChannel, rig_flag_path
from sentinel.orchestrator.killswitch import TripWires
from sentinel.orchestrator.orderbook import MonitorBook
from sentinel.orchestrator.ports import KillRequest, SessionSafety
from sentinel.orchestrator.rig import OrderBook, OrderRow
from sentinel.orchestrator.sendgate import SendPolicy
from sentinel.orchestrator.simrig import CONFIRM_POINT, DELETE_POINT, EMPTY, SEND_RECT, SimScenario, SimulatedRig

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"


def ticket(rig: SimulatedRig, *, pushed: bool = False) -> SimulatedRig:
    """The rig launched with the hook armed and the docked ticket built; with `pushed`, a current session at stage
    `a3_a9` pushed as the Orchestrator's listener would, else the booleans stand in (simrig choice 3)."""
    rig.launch()
    rig.set_hook_phase("running")
    if pushed:
        rig.set_session_safety(current(rig))
    else:
        rig.attested = True
    rig.ticket_visible = True
    return rig


def confirm(**kw) -> Action:
    return Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send", under_point_text="Confirm and Send",
                  order_stage_step=True, **kw)


def current(rig: SimulatedRig, stage: str = "a3_a9", age: float = 0.2) -> SessionSafety:
    return SessionSafety(stage, rig.clock.monotonic() - age, account_id_sha256(EMPTY))


# -- the Window Watcher on the simulated screen (5.4.6) -------------------------------------------------------
def test_the_ticket_detector_sets_the_flag_and_the_dialog_writes_the_send_deny_rectangle():
    rig = SimulatedRig()
    rig.launch()
    assert rig.gw.flag == Flag.CLEAR and rig.gw.deny_rects == []
    rig.ticket_visible = True                         # no dialog, no sticky declaration: only source 2 sees it
    rig._watch()
    assert rig.gw.flag == Flag.SET and rig.gw.deny_rects == []           # Confirm and Send is never a rectangle
    assert not rig.watcher.classes.enumeration_hit
    rig.attested = True
    rig.set_hook_phase("running")
    assert rig.send_input(confirm()).refused is None and rig.dialog_open
    (send,) = rig.gw.deny_rects
    assert send.label == "Send" and send.rect == SEND_RECT.inflate(DENY_INFLATE_PX)
    assert rig.watcher.classes.enumeration_hit                             # source 1: the dialog's title
    rig.send_input(Action("click", point=DELETE_POINT, resolved_name="Delete", under_point_text="Delete",
                          teardown_action="close_dialog"))
    rig.ticket_visible = False
    rig.send_input(Action("press", keys=(0x1B,), teardown_action="clear_ticket"))
    assert not rig.dialog_open and rig.gw.deny_rects == [] and rig.gw.flag == Flag.CLEAR


def test_a_failed_capture_makes_the_flag_unknown_and_agent_input_aborts():
    rig = SimulatedRig(SimScenario(capture_fails=True))
    rig.launch()
    rig.set_hook_phase("running")
    assert rig.gw.flag == Flag.UNKNOWN
    r = rig.send_input(Action("press", profile="agent", keys=(0x09,)))
    assert r.refused.flag_abort and rig.flag_aborts == ["agent"]


def test_window_tripwires_give_the_order_event_window_and_the_dialog_age():
    rig = ticket(SimulatedRig(SimScenario(extra_windows=("Order Rejected",))))
    w = rig.window_tripwires()
    assert w.order_event_window and w.dialog_open_s == 0.0
    rig.send_input(confirm())
    rig.clock.advance(31.0)
    assert rig.window_tripwires().dialog_open_s == 31.0
    rig.kill_process_tree()
    assert rig.window_tripwires() == TripWires()                                   # nothing left on screen


# -- the session, the configuration and the gated send in stage 4 (5.4.3, 6.2, 6.7) ---------------------------
def test_the_pushed_session_decides_confirm_and_send_and_the_booleans_stand_in_without_one():
    rig = ticket(SimulatedRig(), pushed=True)
    rig.set_session_safety(current(rig, stage="a3_a8"))
    assert rig.send_input(confirm()).refused.reason == "deny_profile"            # attestation not through A9
    rig.set_session_safety(current(rig, age=121.0))
    assert "paper-mode assertion" in rig.send_input(confirm()).refused.detail    # older than the 6.4 timer
    rig.attested = True                                                           # the booleans are not read
    assert rig.send_input(confirm()).refused is not None
    rig.set_session_safety(current(rig))
    assert rig.send_input(confirm()).refused is None and rig.dialog_open
    plain = ticket(SimulatedRig())                                                # no session pushed: the booleans
    assert plain.session_safety is None and plain.send_input(confirm()).refused is None


def test_the_example_config_allowlist_is_empty_and_a_scenario_pins_delete():
    assert load_dismiss_allowlist(EXAMPLE / DISMISS_ALLOWLIST_PATH).labels == frozenset()
    rig = ticket(SimulatedRig(SimScenario(config_root=EXAMPLE)))
    assert rig.dismiss_allowlist == frozenset()
    r = rig.send_input(confirm())
    assert (r.refused.reason, r.refused.attempted_send) == ("deny_profile", False) and not rig.dialog_open
    pinned = ticket(SimulatedRig(SimScenario(config_root=EXAMPLE, dismiss_allowlist=("Delete",))))
    assert pinned.send_input(confirm()).refused is None and pinned.dialog_open
    assert SimulatedRig().dismiss_allowlist == DISMISS_ALLOWLIST                  # no configuration: after Phase 0


def test_the_gated_send_token_passes_the_send_rectangle_once_and_is_declared():
    rig = ticket(SimulatedRig(SimScenario(send_policy=SendPolicy(enabled=True))), pushed=True)
    rig.send_input(confirm())
    send = Action("click", point=(920, 610), resolved_name="Send", under_point_text="Send", flow_class="paper_send_ok",
                  account_role="empty", order_stage_step=True)
    r = rig.send_input(send)
    assert r.refused is None and all(d.allowed for d in r.decisions) and rig.sent_orders == 1
    assert rig.gw.send_rect_passed and rig.send_gate.pending is send
    again = rig.send_input(send)                                          # one send pending: the gate refuses it
    assert again.refused.reason == "never_tokenize" and again.refused.attempted_send
    row = rig.book.working[0]
    rig.send_gate.declare(run_id="r", shard_id="s", flow_id="flow.paper.send", step_id="s09", row_key=row.key,
                          sent_at="2026-11-10T19:00:00+00:00")
    assert [d.row_key for d in rig.declared_sends()] == ["sim-order-1"]
    unread = ticket(SimulatedRig(SimScenario(send_policy=SendPolicy(enabled=True), dialog_numbers_read=False)),
                    pushed=True)
    unread.send_input(confirm())
    assert unread.send_input(send).refused.reason == "never_tokenize" and unread.sent_orders == 0
    assert SimulatedRig().declared_sends() == ()                                  # the default branch: no gate


def test_the_explorer_deny_list_comes_from_the_configuration():
    chart = Action("click", profile="explorer", point=(5, 5), resolved_name="Chat Rooms", under_point_text="Chat Rooms")
    rig = SimulatedRig()
    rig.launch()
    rig.set_hook_phase("running")
    assert rig.send_input(chart).refused.detail == "chat"
    open_list = SimulatedRig(SimScenario(deny_list=()))
    open_list.launch()
    open_list.set_hook_phase("running")
    assert open_list.send_input(chart).refused is None


# -- login typing and Cancel All through the gateway (6.6 step 5, 7.7) ----------------------------------------
def test_the_login_types_through_the_gateway_and_deny_all_stops_it():
    rig = SimulatedRig()
    rig.launch()
    rig.set_hook_phase("kill_switch")
    obs = rig.login()
    assert obs.main_window_s is None and not obs.login_dialog_gone and rig.drops > 0
    rig.kill_process_tree()                                                      # the sequence: kill, reset, rearm
    rig.reset_input_state()
    rig.set_hook_phase("recovery")
    rig.launch()
    assert rig.login().main_window_s is not None and rig.gateway_log().tokens_issued == 0   # class login: no session
    watch = Action("click", point=(40, 40), resolved_name="Watchlist", under_point_text="Watchlist")
    assert rig.send_input(watch).refused.reason == "deny_profile"                # nothing else in the recovery arm


def test_cancel_all_is_located_then_clicked_through_the_gateway():
    rig = SimulatedRig()
    rig.launch()
    rig.book = OrderBook(working=(OrderRow("x1", "working", "WORKING", "CEI", 1.0),), net_liq=2500.0)
    rig.set_hook_phase("kill_switch")
    r = rig.cancel_all()
    assert r.located and not r.clicked and rig.book.working                       # deny-all drops the click
    rig.reset_input_state()
    rig.set_hook_phase("recovery")
    r = rig.cancel_all()
    assert r.located and r.clicked and not rig.book.working and rig.book.canceled[0].key == "x1"
    rig.kill_process_tree()
    assert not rig.locate_cancel_all() and not rig.cancel_all().located           # no app, no control


# -- the order book reader (6.6, 7.6) --------------------------------------------------------------------------
def test_read_orders_goes_through_the_proof_reader():
    rig = SimulatedRig(SimScenario(buying_power=5000.0))
    rig._send()
    b = rig.read_orders()
    assert isinstance(b, MonitorBook) and b.readable and b.copy_all_agrees and b.problems == ()
    assert [r.key for r in b.working] == ["sim-order-1"] and b.working[0].qty == 1.0
    assert (b.net_liq, b.buying_power) == (2500.0, 5000.0) and b.screenshot_sha256
    off = SimulatedRig(SimScenario(copy_all_disagrees=True))
    off._send()
    assert off.read_orders().copy_all_agrees                                       # the snapshot read agrees
    later = off.read_orders()
    assert later.readable and not later.copy_all_agrees and any("disagree" in p for p in later.problems)
    blind = SimulatedRig(SimScenario(monitor_unreadable=True))
    blind.read_orders()
    assert not blind.read_orders().readable


# -- the rig host: kill channels, preflight H5 and H6, the Watchdog's reads (6.6, 10.2, 12.7) -----------------
def test_kill_channels_preflight_h5_and_h6(tmp_path):
    rig = SimulatedRig(SimScenario(kill_root=tmp_path))
    flag, pipe = rig.kill_channels()
    assert isinstance(flag, FileFlagChannel) and flag.path == rig_flag_path(tmp_path, "rig-01")
    assert isinstance(pipe, MemoryKillChannel)
    assert rig.preflight().items["H5"] and flag.poll() is None                     # the probe left nothing behind
    pipe.post(KillRequest("rig-01", "watchdog", "watchdog", "hang", "2026-11-10T14:00:00+00:00"))
    report = rig.preflight()
    assert not report.items["H5"] and "named_pipe" in report.detail["H5"]
    assert isinstance(SimulatedRig().kill_channels()[0], MemoryKillChannel)       # no root: in memory

    class Blind:                                                                  # an OCR engine that reads nothing
        def words(self, image):
            return []

    blind = SimulatedRig(SimScenario(redaction_ocr=Blind())).preflight()
    assert not blind.items["H6"] and blind.detail["H6"]
    assert SimulatedRig().preflight().items["H6"]


def test_the_watchdog_reads_come_from_the_scenario():
    s = SimScenario(watchdog=(45.0, True), free_bytes=1024)
    rig = SimulatedRig(s)
    assert rig.watchdog_status() == (45.0, True) and rig.free_bytes() == 1024
    s.watchdog = (1.0, False)                                                     # scriptable mid-run
    assert rig.watchdog_status() == (1.0, False)


def test_reset_input_state_is_a_fresh_gateway_instance():
    rig = SimulatedRig()
    rig.gw.issue_click(10, 10, rig.clock.monotonic())
    rig.gw.open_move_allow(SEND_RECT, rig.clock.monotonic(), 100)
    assert rig.reset_input_state() and rig.gw.tokens == {} and rig.gw.move_allow is None
    assert rig.calls[-1] == "reset_input_state"


def test_the_slow_half_context_carries_every_field_the_gateway_reads():
    rig = ticket(SimulatedRig(SimScenario(send_policy=SendPolicy(enabled=True))), pushed=True)
    rig.set_hook_phase("recovery")
    ctx = rig._context(confirm(), rig.clock.monotonic())
    assert isinstance(ctx, SafetyContext) and ctx.recovery_only and ctx.send_gate is rig.send_gate
    assert ctx.session is rig.session_safety and ctx.now == rig.clock.monotonic()
    assert ctx.dismiss_allowlist == DISMISS_ALLOWLIST and not ctx.dialog_foreground and not ctx.dialog_read
    assert slow_half(confirm(), ctx).reason == "deny_profile"                     # the recovery arm refuses it
    rig.set_hook_phase("running")
    assert rig.send_input(confirm()).refused is None
    ctx = rig._context(confirm(), rig.clock.monotonic())
    assert ctx.dialog_foreground and ctx.dialog_read and ctx.order_dialog_present and not ctx.recovery_only


def test_read_probe_lists_the_base_and_the_candidates_changes():
    """`ports.RegistryProbes` on the simulated rig (2.2.3): the base lists the registry titles; the candidate adds,
    drops and renames labels; a hidden label is on neither build; an unlocated anchor lists nothing."""
    from sentinel.orchestrator.ports import RegistryProbes
    rig = SimulatedRig(SimScenario(probe_lists={"P2": ("Ladder", "Position Statement", "Watchlist")},
                                   probe_added=(("P2", "Crypto"),), probe_removed=(("P2", "Watchlist"),),
                                   probe_renamed=(("P2", "Ladder", "Active Trader"),),
                                   probe_hidden=(("P2", "Position Statement"),),
                                   probe_unlocated=(("P3", "base"),)))
    assert isinstance(rig, RegistryProbes)
    assert rig.read_probe("P2", "base").labels == ("Ladder", "Watchlist")
    assert rig.read_probe("P2", "candidate").labels == ("Active Trader", "Crypto")
    assert rig.read_probe("P3", "candidate").located and not rig.read_probe("P3", "base").located
    assert rig.read_probe("P5", "base").labels == () and "read_probe:P2:base" in rig.calls
