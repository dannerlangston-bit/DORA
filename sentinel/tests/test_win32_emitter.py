"""Tests for sentinel.gateway.emitter_win32: the InputPort over SendInput, driven by a fake function table."""
import ctypes
import sys

import pytest

from sentinel.gateway import emitter_win32 as em
from sentinel.orchestrator.gateway import InputEvent
from sentinel.orchestrator.ports import InputPort
from sentinel.win32 import ffi

SCREEN = (0, 0, 1920, 1080)


class FakeApi:
    """The emitter's Win32 functions: SendInput records the INPUT array it was given."""

    def __init__(self, accept=None, cursor=(640, 360), error=0):
        self.calls = []
        self.accept = accept
        self.pos = cursor
        self.error = error

    def SendInput(self, n, arr, size):
        self.calls.append((n, [self._copy(i) for i in arr[:n]], size))
        return n if self.accept is None else self.accept

    @staticmethod
    def _copy(inp):
        c = ffi.INPUT()
        ctypes.memmove(ctypes.addressof(c), ctypes.addressof(inp), ctypes.sizeof(ffi.INPUT))
        return c

    def GetCursorPos(self, pt):
        if self.pos is None:
            return 0
        pt.x, pt.y = self.pos
        return 1

    def GetSystemMetrics(self, index):
        return {76: -1920, 77: 0, 78: 3840, 79: 1080}[index]

    def MapVirtualKeyW(self, vk, kind):
        assert kind == em.MAPVK_VK_TO_VSC
        return {0x0D: 0x1C, 0x25: 0x4B, 0x41: 0x1E}.get(vk, 0)

    def get_last_error(self):
        return self.error


def click(x, y):
    return [InputEvent("move", x, y), InputEvent("button_down", x, y, button="left"),
            InputEvent("button_up", x, y, button="left")]


def test_it_is_an_input_port():
    assert isinstance(em.Win32Emitter(FakeApi()), InputPort)


@pytest.mark.parametrize("screen", [(0, 0, 1920, 1080), (-1920, 0, 3840, 1080), (0, -1440, 2560, 2520),
                                    (0, 0, 7, 3), (0, 0, 32768, 1)])
def test_normalisation_round_trips_under_both_inverse_conventions(screen):
    left, top, w, h = screen
    for x in range(left, left + w, max(1, w // 997)):
        nx, _ = em.normalize(x, top, screen)
        assert 0 <= nx <= 65535
        assert left + (nx * w >> 16) == x                     # truncating inverse
        assert left + ((nx * w + 32768) >> 16) == x           # rounding inverse
    assert em.normalize(left + w - 1, top + h - 1, screen)[0] <= 65535


@pytest.mark.parametrize("point", [(1920, 0), (-1, 5), (5, 1080)])
def test_a_point_outside_the_virtual_screen_is_refused(point):
    with pytest.raises(em.EmitError, match="outside the virtual screen"):
        em.normalize(*point, SCREEN)


def test_a_click_is_an_absolute_move_then_bare_button_flags():
    move, down, up = em.to_inputs(click(960, 540), SCREEN)
    assert move.type == em.INPUT_MOUSE and move.flags == em.MOVE_FLAGS
    assert move.flags & em.MOUSEEVENTF_MOVE_NOCOALESCE
    assert (move.dx, move.dy) == em.normalize(960, 540, SCREEN)
    assert down.flags == 0x0002 and up.flags == 0x0004                  # no MOUSEEVENTF_MOVE (5.4.4 rule 3)
    assert not (down.flags | up.flags) & em.MOUSEEVENTF_MOVE


def test_x_buttons_and_wheel_carry_their_mouse_data():
    specs = em.to_inputs([InputEvent("button_down", 1, 1, button="x2"), InputEvent("wheel", 1, 1, delta=-120)],
                         SCREEN)
    assert specs[0].flags == 0x0080 and specs[0].data == 2
    assert specs[1].flags == em.MOUSEEVENTF_WHEEL and specs[1].data == -120
    arr = em.marshal(specs, ffi.EMITTER_TAG)
    assert arr[1].mi.mouseData == 0xFFFFFF88                            # signed delta as a DWORD


def test_typed_characters_are_unicode_packets():
    events = [InputEvent("key_down", vk=em.VK_PACKET), InputEvent("key_up", vk=em.VK_PACKET)]
    down, up = em.to_inputs(events, SCREEN, chars=["é", "é"])
    assert (down.vk, down.scan, down.flags) == (0, ord("é"), em.KEYEVENTF_UNICODE)
    assert up.flags == em.KEYEVENTF_UNICODE | em.KEYEVENTF_KEYUP


@pytest.mark.parametrize("chars, match", [(None, "exactly one character"), (["", ""], "exactly one character"),
                                          (["\U0001F600", "\U0001F600"], "Basic Multilingual Plane"),
                                          (["\ud800", "\ud800"], "Basic Multilingual Plane")])
def test_a_packet_without_one_bmp_character_is_refused(chars, match):
    with pytest.raises(em.EmitError, match=match):
        em.to_inputs([InputEvent("key_down", vk=em.VK_PACKET), InputEvent("key_up", vk=em.VK_PACKET)], SCREEN,
                     chars=chars)


def test_a_character_on_any_other_event_is_refused():
    with pytest.raises(em.EmitError, match="not a VK_PACKET"):
        em.to_inputs([InputEvent("key_down", vk=0x41)], SCREEN, chars=["a"])
    with pytest.raises(em.EmitError, match="characters for"):
        em.to_inputs([InputEvent("key_down", vk=0x41)], SCREEN, chars=[])


def test_keys_carry_scan_codes_and_the_extended_flag():
    specs = em.to_inputs([InputEvent("key_down", vk=0x25), InputEvent("key_up", vk=0x0D)], SCREEN,
                         scan=lambda vk: FakeApi().MapVirtualKeyW(vk, em.MAPVK_VK_TO_VSC))
    assert (specs[0].vk, specs[0].scan, specs[0].flags) == (0x25, 0x4B, em.KEYEVENTF_EXTENDEDKEY)
    assert (specs[1].vk, specs[1].scan, specs[1].flags) == (0x0D, 0x1C, em.KEYEVENTF_KEYUP)


@pytest.mark.parametrize("event", [InputEvent("key_down", vk=None), InputEvent("key_down", vk=0x100),
                                   InputEvent("button_down", button="side"), InputEvent("wheel", delta=0),
                                   InputEvent("move", 5, 5, origin="drill")])
def test_malformed_events_are_refused(event):
    with pytest.raises(em.EmitError):
        em.to_inputs([event], SCREEN)


def test_send_marshals_one_tagged_input_array():
    api = FakeApi()
    e = em.Win32Emitter(api)
    events = click(-100, 20) + [InputEvent("key_down", vk=0x41), InputEvent("key_up", vk=0x41)]
    assert e.send(events) == 5 and e.last_error is None
    (n, inputs, size), = api.calls
    assert n == 5 and size == ctypes.sizeof(ffi.INPUT)
    assert [i.type for i in inputs] == [0, 0, 0, 1, 1]
    assert all((i.mi.dwExtraInfo if i.type == 0 else i.ki.dwExtraInfo) == ffi.EMITTER_TAG for i in inputs)
    assert (inputs[0].mi.dx, inputs[0].mi.dy) == em.normalize(-100, 20, (-1920, 0, 3840, 1080))
    assert (inputs[3].ki.wVk, inputs[3].ki.wScan, inputs[4].ki.dwFlags) == (0x41, 0x1E, em.KEYEVENTF_KEYUP)


def test_a_batch_with_one_bad_event_sends_nothing():
    api = FakeApi()
    with pytest.raises(em.EmitError):
        em.Win32Emitter(api, screen=lambda: SCREEN).send(click(10, 10) + [InputEvent("move", 5000, 5)])
    assert api.calls == []
    assert em.Win32Emitter(api).send([]) == 0 and api.calls == []


def test_a_partial_acceptance_keeps_the_last_error():
    api = FakeApi(accept=1, error=5)
    e = em.Win32Emitter(api, screen=lambda: SCREEN)
    assert e.send(click(10, 10)) == 1 and e.last_error == 5


def test_cursor_reads_the_point_and_maps_a_failure():
    assert em.Win32Emitter(FakeApi(cursor=(-5, 7))).cursor() == (-5, 7)
    with pytest.raises(ffi.Win32Error) as err:
        em.Win32Emitter(FakeApi(cursor=None, error=1459)).cursor()
    assert err.value.code == 1459 and err.value.function == "GetCursorPos"


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        em.Win32Emitter()
