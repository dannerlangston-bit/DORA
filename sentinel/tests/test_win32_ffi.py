"""Tests for sentinel.win32.ffi: the structures have their Windows x64 layout on any platform, the prototype table is
complete for what the adapters call, and the real table is refused off Windows."""
import ctypes
import sys

import pytest

from sentinel.gateway import emitter_win32
from sentinel.win32 import ffi

X64 = ctypes.sizeof(ctypes.c_void_p) == 8


@pytest.mark.skipif(not X64, reason="the Windows SDK values below are the x64 ones")
@pytest.mark.parametrize("struct, size, offsets", [
    (ffi.POINT, 8, {"y": 4}),
    (ffi.RECT, 16, {"bottom": 12}),
    (ffi.MOUSEINPUT, 32, {"mouseData": 8, "dwFlags": 12, "dwExtraInfo": 24}),
    (ffi.KEYBDINPUT, 24, {"wScan": 2, "dwFlags": 4, "dwExtraInfo": 16}),
    (ffi.INPUT, 40, {"u": 8}),
    (ffi.MSLLHOOKSTRUCT, 32, {"mouseData": 8, "flags": 12, "dwExtraInfo": 24}),
    (ffi.KBDLLHOOKSTRUCT, 24, {"flags": 8, "dwExtraInfo": 16}),
    (ffi.MSG, 48, {"wParam": 16, "pt": 36}),
    (ffi.PROCESSENTRY32W, 568, {"th32DefaultHeapID": 16, "cntThreads": 28, "th32ParentProcessID": 32,
                                "szExeFile": 44}),
    (ffi.PROCESS_MEMORY_COUNTERS_EX, 80, {"WorkingSetSize": 16, "PrivateUsage": 72}),
    (ffi.BITMAPINFOHEADER, 40, {"biBitCount": 14, "biCompression": 16}),
    (ffi.DEVMODEW, 220, {"dmSize": 68, "dmFields": 72, "dmLogPixels": 166, "dmBitsPerPel": 168,
                         "dmPelsWidth": 172, "dmPelsHeight": 176, "dmDisplayFrequency": 184}),
    (ffi.DYNAMIC_TIME_ZONE_INFORMATION, 432, {"StandardDate": 68, "DaylightName": 88, "TimeZoneKeyName": 172,
                                              "DynamicDaylightTimeDisabled": 428}),
])
def test_structures_have_the_windows_x64_layout(struct, size, offsets):
    assert ctypes.sizeof(struct) == size
    for name, off in offsets.items():
        assert getattr(struct, name).offset == off, name


def test_wide_strings_are_two_byte_units_whatever_the_platform():
    e = ffi.PROCESSENTRY32W()
    raw = "thinkorswim.exe".encode("utf-16-le")
    ctypes.memmove(e.szExeFile, raw, len(raw))
    assert ffi.wstr(e.szExeFile) == "thinkorswim.exe"


def test_handles_and_errors():
    assert not ffi.handle_ok(None) and not ffi.handle_ok(0) and not ffi.handle_ok(ffi.INVALID_HANDLE_VALUE)
    assert ffi.handle_ok(0x1234)

    class Api:
        def get_last_error(self):
            return 5

    err = ffi.fail(Api(), "OpenProcess")
    assert isinstance(err, OSError) and err.code == 5 and "OpenProcess" in str(err)


def test_every_prototype_names_a_known_dll_and_callback():
    dlls = {"user32", "kernel32", "gdi32", "dwmapi", "psapi", "wtsapi32"}
    for name, (dll, _res, args) in ffi.PROTOTYPES.items():
        assert dll in dlls, name
        for a in args:
            assert not isinstance(a, str) or a in ffi.CALLBACKS, (name, a)
    for name in emitter_win32.NEEDED:
        assert name in ffi.PROTOTYPES or name in emitter_win32.PROTOTYPES


def test_the_injection_prototype_lives_only_in_the_emitter():
    assert "SendInput" not in ffi.PROTOTYPES
    assert set(emitter_win32.PROTOTYPES) == {"SendInput"}


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_the_real_table_is_refused_off_windows():
    with pytest.raises(ffi.Win32Unavailable):
        ffi.load(["GetForegroundWindow"])


def test_the_tags_differ_and_fit_dwextrainfo():
    assert ffi.EMITTER_TAG != ffi.DRILL_TAG
    for tag in (ffi.EMITTER_TAG, ffi.DRILL_TAG):
        assert 0 < tag < 2 ** (8 * ctypes.sizeof(ffi.ULONG_PTR))
