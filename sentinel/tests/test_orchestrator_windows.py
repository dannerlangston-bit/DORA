"""The Window Watcher: flag sources 1 and 2 and the deny rectangles (5.4.6, 5.4.2, 6.2, 6.6 Triggers)."""
from __future__ import annotations

import numpy as np
import pytest

from sentinel import synth
from sentinel.orchestrator.gateway import (DENY_INFLATE_PX, Flag, GatewayError, GatewayState, InputEvent, Rect,
                                           hook_decide)
from sentinel.orchestrator.killswitch import TripWires, check_tripwires
from sentinel.orchestrator.ports import Frame, WindowInfo
from sentinel.orchestrator.windows import (DETECTOR_STALE_S, LADDER_COLUMN, SendTemplate, TitleSets, WindowWatcher,
                                           classify_windows, detect_ticket, locate_send_controls)

TITLES = TitleSets.compile(order_dialog=["^Order Confirmation"], detached_ticket=["^Order Entry"],
                           order_event=["(?i)order (submitted|rejected|filled)"])
SCREEN = (1280, 800)
MAIN_AT, DIALOG_AT = (0, 0), (700, 450)
T0 = 100.0


def dialog_screen() -> synth.Screen:
    """The Order Confirmation dialog: Delete, Send and the numbers the Oracle reads."""
    return synth.render({"type": "window", "name": "dlg", "title": "Order Confirmation", "children": [
        {"type": "text", "name": "dlg.cost", "text": "Cost 15,025.00", "rect": [10, 10, 200, 18]},
        {"type": "button", "name": "dlg.delete", "label": "Delete", "rect": [60, 110, 90, 26]},
        {"type": "button", "name": "dlg.send", "label": "Send", "style": "primary", "rect": [170, 110, 90, 26]}]},
        (320, 170))


def main_screen(ticket: bool = True) -> synth.Screen:
    layout = synth.sample_layout()
    if not ticket:
        layout["children"] = layout["children"][:2]
    return synth.render(layout, (640, 400))


MAIN, DIALOG = main_screen(), dialog_screen()
TEMPLATES = (SendTemplate("Send", DIALOG.crop("dlg.send")), SendTemplate("Confirm and Send", MAIN.crop("ticket.send")))


def desktop(*, ticket: bool = True, dialog: bool = False) -> np.ndarray:
    img = np.full((SCREEN[1], SCREEN[0], 3), 8, np.uint8)
    m = main_screen(ticket).image
    img[MAIN_AT[1]:MAIN_AT[1] + m.shape[0], MAIN_AT[0]:MAIN_AT[0] + m.shape[1]] = m
    if dialog:
        d = DIALOG.image
        img[DIALOG_AT[1]:DIALOG_AT[1] + d.shape[0], DIALOG_AT[0]:DIALOG_AT[0] + d.shape[1]] = d
    return img


def frame(mono: float = T0, **kw) -> Frame:
    return Frame(desktop(**kw), (0, 0), mono)


def win(hwnd: int, title: str, cls: str = "SunAwtDialog", **kw) -> WindowInfo:
    return WindowInfo(hwnd, 4100, cls, title, **kw)


MAIN_WIN = win(1, "Main@thinkorswim [build 1998.3.0]", "SunAwtFrame", rect=(0, 0, 640, 400), z_order=1)
DIALOG_WIN = win(2, "Order Confirmation", rect=(*DIALOG_AT, 320, 170), z_order=0)


def send_bounds() -> Rect:
    x, y, w, h = DIALOG.get("dlg.send").bbox
    return Rect(DIALOG_AT[0] + x, DIALOG_AT[1] + y, w, h)


# -- classify_windows -------------------------------------------------------------------------------------
def test_an_order_confirmation_title_is_an_enumeration_hit_and_its_age_is_tracked_across_polls():
    c = classify_windows([MAIN_WIN, DIALOG_WIN], TITLES, T0)
    assert c.enumeration_hit and [w.window.hwnd for w in c.order_dialogs] == [2] and not c.order_event_window
    later = classify_windows([MAIN_WIN, DIALOG_WIN], TITLES, T0 + 31, c.first_seen)
    assert later.dialog_open_s(T0 + 31) == pytest.approx(31.0)
    assert check_tripwires(TripWires(dialog_open_s=later.dialog_open_s(T0 + 31))) == "dialog_open_30s"
    gone = classify_windows([MAIN_WIN], TITLES, T0 + 32, later.first_seen)
    back = classify_windows([MAIN_WIN, DIALOG_WIN], TITLES, T0 + 33, gone.first_seen)
    assert back.dialog_open_s(T0 + 33) == 0.0                                    # a new appearance, a new age


def test_detached_tickets_order_events_and_filtered_windows():
    wins = [win(3, "Order Entry - AAPL", "SunAwtFrame"), win(4, "Order Submitted", "SunAwtWindow"),
            win(5, "Order Confirmation", visible=False), win(6, "Order Confirmation", "Chrome_WidgetWin_1")]
    c = classify_windows(wins, TITLES, T0)
    assert [w.window.hwnd for w in c.detached_tickets] == [3] and c.enumeration_hit
    assert c.order_event_window and [w.window.hwnd for w in c.order_events] == [4]
    assert c.order_dialogs == () and c.dialog_open_s(T0) == 0.0
    assert check_tripwires(TripWires(order_event_window=c.order_event_window)) == "order_event_window"
    assert not classify_windows(wins, TitleSets(), T0).enumeration_hit           # no titles read yet


# -- sources 1 and 2 ----------------------------------------------------------------------------------------
def test_source_1_locates_send_in_the_dialog_and_never_confirm_and_send():
    found = locate_send_controls(frame(dialog=True), DIALOG_WIN.rect, TEMPLATES)
    assert [(d.rect, d.label) for d in found] == [(send_bounds(), "Send")]
    assert locate_send_controls(frame(dialog=True), MAIN_WIN.rect, TEMPLATES) == ()
    assert locate_send_controls(frame(dialog=True), (5000, 5000, 320, 170), TEMPLATES) == ()   # off the grab


def test_source_2_detects_the_docked_ticket_at_half_scale_and_floor_0_90():
    hit, rects = detect_ticket(frame(), MAIN_WIN.rect, TEMPLATES)
    assert hit and rects == ()                                     # Confirm and Send sets the flag, no rectangle
    assert detect_ticket(frame(ticket=False), MAIN_WIN.rect, TEMPLATES) == (False, ())
    other = synth.sample_layout()
    other["children"][2]["children"][-1]["label"] = "Review Order"                  # another control there
    shot = Frame(synth.render(other, (640, 400)).image, (0, 0), T0)
    assert detect_ticket(shot, MAIN_WIN.rect, TEMPLATES) == (False, ())
    hit, rects = detect_ticket(frame(dialog=True), DIALOG_WIN.rect, TEMPLATES)
    assert hit and [d.label for d in rects] == ["Send"]
    r = rects[0].rect
    assert abs(r.x - send_bounds().x) <= 1 and abs(r.y - send_bounds().y) <= 1


# -- WindowWatcher.poll -------------------------------------------------------------------------------------
def test_an_order_confirmation_window_sets_the_flag_and_its_send_button_is_a_deny_rect_inflated_12px():
    g = GatewayState(armed=True)
    w = WindowWatcher(TITLES)
    assert w.poll_into(g, [MAIN_WIN, DIALOG_WIN], frame(dialog=True), TEMPLATES, now=T0) == Flag.SET
    assert [(d.rect, d.label) for d in g.deny_rects] == [(send_bounds().inflate(DENY_INFLATE_PX), "Send")]
    assert g.effective_flag(T0) == Flag.SET and g.watcher_mono_ts == T0
    assert all(d.label != "Confirm and Send" for d in g.deny_rects)


def test_the_docked_ticket_alone_sets_the_flag_and_two_clear_polls_clear_it():
    w = WindowWatcher(TITLES)
    flag, rects = w.poll([MAIN_WIN], frame(), TEMPLATES, now=T0)
    assert flag == Flag.SET and rects == ()
    assert w.poll([MAIN_WIN], frame(T0 + 0.5, ticket=False), TEMPLATES, now=T0 + 0.5)[0] == Flag.SET
    assert w.poll([MAIN_WIN], frame(T0 + 0.5, ticket=False), TEMPLATES, now=T0 + 0.6)[0] == Flag.CLEAR


def test_enumeration_or_capture_failure_is_unknown():
    w = WindowWatcher(TITLES)
    assert w.poll(None, frame(ticket=False), TEMPLATES, now=T0)[0] == Flag.UNKNOWN
    assert w.poll([MAIN_WIN], None, TEMPLATES, now=T0)[0] == Flag.UNKNOWN
    stale = frame(T0, ticket=False)
    assert w.poll([MAIN_WIN], stale, TEMPLATES, now=T0 + DETECTOR_STALE_S + 0.01)[0] == Flag.UNKNOWN
    w.poll([MAIN_WIN], frame(T0 + 2, ticket=False), TEMPLATES, now=T0 + 2)
    assert w.poll([MAIN_WIN], frame(T0 + 2, ticket=False), TEMPLATES, now=T0 + 2.1)[0] == Flag.CLEAR


def test_a_failed_or_stale_capture_keeps_the_send_controls_the_last_good_capture_located():
    """Choice 6 (5.4.2, 5.4.4, 5.4.6): a missing or stale grab makes the flag unknown and keeps the deny rectangles
    of the last good capture, so the hook still drops a click on the dialog's Send; only a good capture that no
    longer finds the control removes its rectangle."""
    g = GatewayState(armed=True)
    w = WindowWatcher(TITLES)
    up = [MAIN_WIN, DIALOG_WIN]
    want = [(send_bounds().inflate(DENY_INFLATE_PX), "Send")]
    assert w.poll_into(g, up, frame(dialog=True), TEMPLATES, now=T0) == Flag.SET
    assert [(d.rect, d.label) for d in g.deny_rects] == want
    assert w.poll_into(g, up, None, TEMPLATES, now=T0 + 0.1) == Flag.UNKNOWN                     # no grab
    assert [(d.rect, d.label) for d in g.deny_rects] == want
    late = T0 + DETECTOR_STALE_S + 0.1
    assert w.poll_into(g, up, frame(T0, dialog=True), TEMPLATES, now=late) == Flag.UNKNOWN        # a stale grab
    assert [(d.rect, d.label) for d in g.deny_rects] == want
    x, y = send_bounds().x + 20, send_bounds().y + 10
    g.issue_click(x, y, late, dialog_ok=True)                    # a Delete-labelled token over Send after a nudge
    assert hook_decide(g, InputEvent("button_down", x, y, button="left"), late).reason == "deny_rect"
    w.poll_into(g, [MAIN_WIN], frame(T0 + 2, ticket=False), TEMPLATES, now=T0 + 2)               # dialog gone
    assert g.deny_rects == []


def test_a_failed_enumeration_keeps_the_dialog_age():
    w = WindowWatcher(TITLES)
    w.poll([MAIN_WIN, DIALOG_WIN], frame(dialog=True), TEMPLATES, now=T0)
    w.poll(None, frame(T0 + 20, dialog=True), TEMPLATES, now=T0 + 20)
    w.poll([MAIN_WIN, DIALOG_WIN], frame(T0 + 31, dialog=True), TEMPLATES, now=T0 + 31)
    assert w.classes.dialog_open_s(T0 + 31) == pytest.approx(31.0)


def test_a_popup_above_the_main_frame_never_hides_the_docked_ticket_from_the_detector():
    """Choice 3 (5.4.6 source 2): a heavyweight tooltip, menu or combo popup (a SunAwtWindow owned by the main frame,
    so above it, but never activated) is not the foreground window; the detector then searches the whole grab, so
    the docked ticket keeps the flag set and its send-like controls stay deny rectangles."""
    tip = win(9, "", "SunAwtWindow", rect=(600, 380, 120, 20), z_order=0, owner_hwnd=1)
    w = WindowWatcher(TITLES)
    polls = [[MAIN_WIN]] * 2 + [[tip, MAIN_WIN]] * 3
    assert [w.poll(wins, frame(T0 + i / 2), TEMPLATES, now=T0 + i / 2)[0] for i, wins in enumerate(polls)] == [
        Flag.SET] * 5
    docked_send = (SendTemplate("Send", MAIN.crop("ticket.send")),)
    for wins in ([MAIN_WIN], [tip, MAIN_WIN]):
        assert [d.label for d in WindowWatcher(TITLES).poll(wins, frame(), docked_send, now=T0)[1]] == ["Send"]
    native = win(8, "Save As", "#32770", rect=(600, 300, 200, 100), z_order=0)   # another class: not known either
    assert WindowWatcher(TITLES).poll([native, MAIN_WIN], frame(), TEMPLATES, now=T0)[0] == Flag.SET


def test_unread_titles_leave_the_flag_to_the_detector():
    w = WindowWatcher(TitleSets())                                   # Phase 0 has not read the titles (choice 2)
    assert w.poll([MAIN_WIN, DIALOG_WIN], frame(ticket=False), TEMPLATES, now=T0)[0] == Flag.UNKNOWN   # first poll
    assert w.poll([MAIN_WIN, DIALOG_WIN], frame(ticket=False), TEMPLATES, now=T0 + 0.1)[0] == Flag.CLEAR
    flag, rects = w.poll([MAIN_WIN, DIALOG_WIN], frame(T0 + 0.5, ticket=False, dialog=True), TEMPLATES, now=T0 + 0.5)
    assert flag == Flag.SET and [d.label for d in rects] == ["Send"]           # the foreground dialog's Send


def test_sticky_declaration_and_ladder_columns():
    w = WindowWatcher(TITLES)
    column = Rect(300, 500, 60, 280)
    flag, rects = w.poll([MAIN_WIN], frame(ticket=False), TEMPLATES, now=T0, sticky=True, ladder_columns=[column])
    assert flag == Flag.SET and [(d.rect, d.label) for d in rects] == [(column, LADDER_COLUMN)]


def test_more_than_eight_deny_rects_fail_closed_unknown():
    g = GatewayState(armed=True)
    w = WindowWatcher(TITLES)
    with pytest.raises(GatewayError):
        w.poll_into(g, [MAIN_WIN], frame(ticket=False), TEMPLATES, now=T0,
                    ladder_columns=[Rect(10 * i, 0, 5, 5) for i in range(9)])
    assert g.effective_flag(T0) == Flag.UNKNOWN


def test_template_labels_are_the_send_controls():
    with pytest.raises(ValueError):
        SendTemplate("Delete", DIALOG.crop("dlg.delete"))
