"""Tests for sentinel.win32.desktop: the DesktopPort over a fake Win32 function table."""
import ctypes
import os
import sys

import numpy as np
import pytest

from sentinel.orchestrator.ports import DesktopPort, PointOwner
from sentinel.win32 import desktop as dk
from sentinel.win32 import ffi

APP = 4000
OTHER = 5000
V2, SYSTEM, GDISCALED = 0x1001, 0x1002, 0x1003


class FakeApi:
    """A desktop of windows in z-order (topmost first), processes, DWM, DPI contexts and a GDI screen."""

    WNDENUMPROC = staticmethod(lambda f: f)

    def __init__(self):
        # hwnd: (pid, class, title, visible, enabled, rect or None, owner, exstyle)
        self.windows = {
            10: (OTHER, "Chrome_WidgetWin_1", "Mail", True, True, (100, 100, 300, 200), 0, dk.WS_EX_TOPMOST),
            11: (APP, "SunAwtWindow", "", True, True, (50, 50, 20, 20), 12, 0),
            12: (APP, "SunAwtFrame", "Main@thinkorswim [build 1990]", True, False, (0, 0, 1920, 1040), 0, 0),
            13: (APP, "SunAwtDialog", "Order Confirmation", False, True, (500, 400, 400, 300), 12, 0),
            14: (APP, "GDI+ Hook Window Class", "GDI+", True, True, None, 0, 0),
            15: (OTHER, "Shell_TrayWnd", "", True, True, (0, 1040, 1920, 40), 0, 0),
        }
        self.images = {APP: "C:\\Program Files\\thinkorswim\\thinkorswim.exe", OTHER: "C:\\Windows\\explorer.exe"}
        self.contexts = {APP: SYSTEM, os.getpid(): V2}
        self.foreground = 12
        self.point_owner = {(5, 5): 12, (150, 150): 10}
        self.children = {77: 12}                       # a child window under the frame
        self.desktop = "Default"
        self.calls = []
        self.error = 0
        self.closing = {}
        self.gdi_fail = None
        self.screen = (-1920, 0, 4, 2)
        self.minimized = set()
        self.attached = False
        self.foreground_locked = False
        self.hung_windows, self.silent = set(), set()

    def get_last_error(self):
        return self.error

    def EnumWindows(self, proc, lparam):
        for h in self.windows:
            if not proc(h, lparam):
                break
        return 1

    def GetWindowThreadProcessId(self, h, pid):
        if pid is not None:
            pid.value = self.windows[h][0]
        return 7000 + self.windows[h][0]                # the window's thread: one per process here

    def GetCurrentThreadId(self):
        return 1111

    def IsIconic(self, h):
        return h in self.minimized

    def ShowWindow(self, h, cmd):
        self.calls.append(("show", h, cmd))
        self.minimized.discard(h)
        return 1

    def AttachThreadInput(self, mine, theirs, attach):
        self.calls.append(("attach", mine, theirs, attach))
        self.attached = attach
        return 1

    def SetForegroundWindow(self, h):
        self.calls.append(("set_foreground", h))
        if self.attached and not self.foreground_locked:
            self.foreground = h
        return int(self.foreground == h)

    def BringWindowToTop(self, h):
        self.calls.append(("top", h))
        return 1

    def IsHungAppWindow(self, h):
        return h in self.hung_windows

    def SendMessageTimeoutW(self, h, msg, w, l, flags, timeout, result):
        assert (msg, flags, timeout) == (dk.WM_NULL, dk.SMTO_ABORTIFHUNG, dk.HUNG_TIMEOUT_MS)
        return 0 if h in self.silent else 1

    def GetClassNameW(self, h, buf, n):
        buf.value = self.windows[h][1]
        return len(buf.value)

    def GetWindowTextW(self, h, buf, n):
        buf.value = self.windows[h][2]
        return len(buf.value)

    def IsWindowVisible(self, h):
        return self.windows[h][3]

    def IsWindowEnabled(self, h):
        return self.windows[h][4]

    def DwmGetWindowAttribute(self, h, attr, rect, size):
        assert attr == dk.DWMWA_EXTENDED_FRAME_BOUNDS and size == ctypes.sizeof(ffi.RECT)
        r = self.windows[h][5]
        if r is None:
            return -2147024890                        # E_HANDLE
        rect.left, rect.top, rect.right, rect.bottom = r[0], r[1], r[0] + r[2], r[1] + r[3]
        return 0

    def GetWindow(self, h, cmd):
        assert cmd == dk.GW_OWNER
        return self.windows[h][6] or None

    def GetWindowLongW(self, h, index):
        assert index == dk.GWL_EXSTYLE
        return self.windows[h][7]

    def OpenProcess(self, access, inherit, pid):
        assert access == dk.PROCESS_QUERY_LIMITED_INFORMATION
        if pid not in self.images:
            self.error = ffi.ERROR_INVALID_PARAMETER
            return None
        self.calls.append(("open", pid))
        return 0x9000 + pid

    def CloseHandle(self, h):
        self.calls.append(("close", h - 0x9000))
        return 1

    def QueryFullProcessImageNameW(self, h, flags, buf, size):
        buf.value = self.images[h - 0x9000]
        size.value = len(buf.value)
        return 1

    def GetForegroundWindow(self):
        return self.foreground

    def WindowFromPoint(self, pt):
        return {**self.point_owner, (7, 7): 77}.get((pt.x, pt.y))

    def GetAncestor(self, h, flags):
        assert flags == dk.GA_ROOT
        return self.children.get(h, h)

    def GetSystemMetrics(self, i):
        return dict(zip((76, 77, 78, 79), self.screen))[i]

    def GetThreadDpiAwarenessContext(self):
        return self.contexts[os.getpid()]

    def GetDpiAwarenessContextForProcess(self, h):
        return self.contexts.get(h - 0x9000)

    def AreDpiAwarenessContextsEqual(self, a, b):
        return {V2: -4, GDISCALED: -5}.get(a) == ctypes.c_int64(b.value).value

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        return {SYSTEM: 1}.get(ctx, -1)

    def OpenInputDesktop(self, flags, inherit, access):
        assert access == dk.DESKTOP_READOBJECTS
        return 0x77 if self.desktop is not None else None

    def GetUserObjectInformationW(self, h, index, buf, size, needed):
        assert index == dk.UOI_NAME and size == ctypes.sizeof(buf)
        buf.value = self.desktop
        needed.value = 2 * (len(self.desktop) + 1)
        return 1

    def CloseDesktop(self, h):
        self.calls.append(("close_desktop", h))
        return 1

    def IsWindow(self, h):
        if h in self.closing:
            self.closing[h] -= 1
            return self.closing[h] > 0
        return h in self.windows

    def PostMessageW(self, h, msg, w, l):
        self.calls.append(("post", h, msg))
        return 1

    # GDI
    def GetDC(self, h):
        self.calls.append("GetDC")
        return 0x1

    def ReleaseDC(self, h, dc):
        self.calls.append("ReleaseDC")
        return 1

    def CreateCompatibleDC(self, dc):
        return 0x2

    def CreateCompatibleBitmap(self, dc, w, h):
        self.calls.append(("bitmap", w, h))
        return 0x3

    def SelectObject(self, dc, obj):
        self.calls.append(("select", obj))
        return 0x4 if obj == 0x3 else 0x3

    def BitBlt(self, dst, x, y, w, h, src, sx, sy, rop):
        self.calls.append(("blt", w, h, sx, sy, rop))
        return self.gdi_fail != "BitBlt"

    def GetDIBits(self, dc, bmp, start, lines, buf, bmi, usage):
        hd = bmi.bmiHeader
        assert (hd.biHeight, hd.biBitCount, hd.biCompression, usage) == (-lines, 32, dk.BI_RGB, dk.DIB_RGB_COLORS)
        w = hd.biWidth
        px = bytes(v for i in range(w * lines) for v in (i, 100 + i, 200 + i, 255))      # B, G, R, A
        ctypes.memmove(buf, px, len(px))
        return lines

    def DeleteObject(self, obj):
        self.calls.append(("delete", obj))
        return 1

    def DeleteDC(self, dc):
        self.calls.append("DeleteDC")
        return 1


def desktop(api=None, **kw):
    api = api or FakeApi()
    return dk.Win32Desktop(lambda: {APP}, api, **kw), api


def test_it_is_a_desktop_port():
    assert isinstance(desktop()[0], DesktopPort)


def test_enumeration_keeps_the_trees_visible_windows_sunawt_first_in_z_order():
    d, api = desktop()
    wins = d.enumerate_windows()
    assert [w.hwnd for w in wins] == [11, 12, 14]              # 13 invisible; 10 and 15 another process
    popup, frame, gdi = wins
    assert (frame.cls, frame.title, frame.enabled, frame.rect) == ("SunAwtFrame", "Main@thinkorswim [build 1990]",
                                                                   False, (0, 0, 1920, 1040))
    assert (popup.owner_hwnd, popup.z_order, frame.z_order, gdi.z_order) == (12, 1, 2, 3)   # Mail is z 0
    assert frame.process_name == "thinkorswim.exe" and gdi.rect is None                     # DWM failed: None
    assert api.calls.count(("open", APP)) == 1                                              # one image read per pid


def test_the_foreground_window_carries_owner_topmost_and_image():
    d, api = desktop()
    fg = d.foreground_window()
    assert (fg.hwnd, fg.pid, fg.process_name, fg.z_order) == (12, APP, "thinkorswim.exe", None)
    api.foreground = 10
    other = d.foreground_window()
    assert other.topmost and other.process_name == "explorer.exe"
    api.foreground = 0
    assert d.foreground_window() is None


def test_set_focus_attaches_to_the_foreground_thread_and_restores_a_minimized_window():
    d, api = desktop()
    assert d.set_focus(12) and api.calls == []                         # already in front: nothing to do
    api.foreground, api.minimized = 10, {13}
    assert d.set_focus(13)
    assert [c for c in api.calls if c[0] != "close"] == [
        ("show", 13, dk.SW_RESTORE), ("attach", 1111, 7000 + OTHER, True), ("set_foreground", 13), ("top", 13),
        ("attach", 1111, 7000 + OTHER, False)]
    api.calls.clear()
    api.foreground, api.foreground_locked = 10, True                   # Windows refuses: the re-check fails
    assert not d.set_focus(12) and api.calls[-1] == ("attach", 1111, 7000 + OTHER, False)
    api.foreground, api.foreground_locked = 0, False
    api.calls.clear()
    assert not d.set_focus(12) and not any(c[0] == "attach" for c in api.calls)   # no foreground thread to join


def test_hung_is_the_secondary_hang_check():
    d, api = desktop()
    assert not d.hung(12)
    api.hung_windows.add(12)
    assert d.hung(12)
    api.hung_windows.clear()
    api.silent.add(12)
    assert d.hung(12)
    assert not d.hung(999)                                             # gone, not hung


def test_window_from_point_names_the_top_level_owner():
    d, _ = desktop()
    assert d.window_from_point([(5, 5), (150, 150), (7, 7), (900, 900)]) == (
        PointOwner(5, 5, 12, APP, "Main@thinkorswim [build 1990]"), PointOwner(150, 150, 10, OTHER, "Mail"),
        PointOwner(7, 7, 12, APP, "Main@thinkorswim [build 1990]"), PointOwner(900, 900, None, None))


def test_dpi_awareness_names():
    d, api = desktop()
    assert d.process_dpi_awareness() == "PER_MONITOR_AWARE_V2"
    assert d.process_dpi_awareness(os.getpid()) == "PER_MONITOR_AWARE_V2"
    assert d.process_dpi_awareness(APP) == "SYSTEM_AWARE"
    assert ("close", APP) in api.calls
    api.contexts[APP] = GDISCALED
    assert d.process_dpi_awareness(APP) == "UNAWARE_GDISCALED"
    assert d.process_dpi_awareness(31337) == "unreadable"
    api.contexts[os.getpid()] = 0
    assert d.process_dpi_awareness() == "unreadable"


def test_input_desktop_is_the_name_or_empty_when_unreadable():
    d, api = desktop()
    assert d.input_desktop() == "Default" and ("close_desktop", 0x77) in api.calls
    api.desktop = "Winlogon"
    assert d.input_desktop() == "Winlogon"
    api.desktop = None
    assert d.input_desktop() == ""


def test_virtual_screen_reads_the_four_metrics():
    assert desktop()[0].virtual_screen() == (-1920, 0, 4, 2)


def test_close_window_waits_for_the_window_to_go():
    clock = {"t": 0.0}
    d, api = desktop(clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    api.closing[13] = 3
    assert d.close_window(13) and ("post", 13, dk.WM_CLOSE) in api.calls
    api.closing[11] = 10 ** 6
    clock["t"] = 0.0
    assert not d.close_window(11) and clock["t"] >= dk.CLOSE_WAIT_S
    assert d.close_window(999)                                          # already gone


def test_grab_converts_the_top_down_dib_and_frees_every_gdi_object():
    d, api = desktop(clock=lambda: 12.5)
    frame = d.grab()
    assert frame.origin == (-1920, 0) and frame.mono_s == 12.5
    assert frame.image.shape == (2, 4, 3) and frame.image.dtype == np.uint8
    assert frame.image[0, 1].tolist() == [1, 101, 201] and frame.image[1, 3].tolist() == [7, 107, 207]
    assert ("blt", 4, 2, -1920, 0, dk.SRCCOPY | dk.CAPTUREBLT) in api.calls
    assert api.calls[-4:] == [("select", 0x4), ("delete", 0x3), "DeleteDC", "ReleaseDC"]


def test_a_failed_blit_raises_and_still_frees_the_gdi_objects():
    api = FakeApi()
    api.gdi_fail, api.error = "BitBlt", 6
    d, _ = desktop(api)
    with pytest.raises(ffi.Win32Error, match="BitBlt"):
        d.grab()
    assert api.calls[-4:] == [("select", 0x4), ("delete", 0x3), "DeleteDC", "ReleaseDC"]


def test_bgra_to_bgr_drops_alpha_and_keeps_row_order():
    raw = bytes([1, 2, 3, 255, 4, 5, 6, 255, 7, 8, 9, 255, 10, 11, 12, 255])
    img = dk.bgra_to_bgr(raw, 2, 2)
    assert img.tolist() == [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]] and img.flags.c_contiguous


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        dk.Win32Desktop(lambda: ())
