"""Tests for sentinel.win32.clipboard: the ClipboardPort over a fake clipboard."""
import ctypes
import sys

import pytest

from sentinel.orchestrator.ports import ClipboardPort
from sentinel.win32 import clipboard as cb
from sentinel.win32 import ffi


class FakeApi:
    """A clipboard holding UTF-16 text in a global memory block, busy for the first `busy` opens."""

    def __init__(self, text=None, *, busy=0, data_ok=True, lock_ok=True):
        self.mem = None
        self.set(text)
        self.busy = busy
        self.data_ok, self.lock_ok = data_ok, lock_ok
        self.opened = self.closed = self.unlocked = 0
        self.error = 0

    def set(self, text):
        self.mem = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\0\0") if text is not None else None

    def get_last_error(self):
        return self.error

    def OpenClipboard(self, hwnd):
        if self.busy:
            self.busy -= 1
            self.error = 5
            return 0
        self.opened += 1
        return 1

    def CloseClipboard(self):
        self.closed += 1
        return 1

    def IsClipboardFormatAvailable(self, fmt):
        return fmt == cb.CF_UNICODETEXT and self.mem is not None

    def GetClipboardData(self, fmt):
        return 0x44 if self.data_ok else None

    def GlobalLock(self, h):
        assert h == 0x44
        return ctypes.addressof(self.mem) if self.lock_ok else None

    def GlobalSize(self, h):
        return ctypes.sizeof(self.mem)

    def GlobalUnlock(self, h):
        self.unlocked += 1
        return 1

    def EmptyClipboard(self):
        self.mem = None
        return 1


def clip(api):
    sleeps = []
    return cb.Win32Clipboard(api, sleep=sleeps.append), sleeps


def test_it_is_a_clipboard_port():
    assert isinstance(clip(FakeApi())[0], ClipboardPort)


def test_copy_all_text_reads_whole_and_clears():
    text = "Symbol\tQty\tMark\nSPY\t100\t512.25\n\U0001F4C8 é"
    api = FakeApi(text)
    c, _ = clip(api)
    assert c.read_text() == text
    c.clear()
    assert c.read_text() is None
    assert api.opened == api.closed == 3 and api.unlocked == 1


def test_no_text_or_empty_text_is_none():
    assert clip(FakeApi(None))[0].read_text() is None
    assert clip(FakeApi(""))[0].read_text() is None


def test_a_busy_clipboard_is_retried_then_raises():
    api = FakeApi("x", busy=2)
    c, sleeps = clip(api)
    assert c.read_text() == "x" and sleeps == [cb.OPEN_RETRY_S] * 2
    api.busy = 99
    c, sleeps = clip(api)
    with pytest.raises(ffi.Win32Error, match="OpenClipboard"):
        c.read_text()
    assert len(sleeps) == cb.OPEN_ATTEMPTS - 1


@pytest.mark.parametrize("kw, fn", [({"data_ok": False}, "GetClipboardData"), ({"lock_ok": False}, "GlobalLock")])
def test_a_promised_text_that_cannot_be_read_raises_and_closes(kw, fn):
    api = FakeApi("x", **kw)
    with pytest.raises(ffi.Win32Error, match=fn):
        clip(api)[0].read_text()
    assert api.closed == api.opened == 1


def test_decode_stops_at_the_nul_and_ignores_a_trailing_odd_byte():
    assert cb.decode_utf16("ab".encode("utf-16-le") + b"\0\0junk" + b"\x01") == "ab"
    assert cb.decode_utf16(b"") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        cb.Win32Clipboard()
