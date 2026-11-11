"""`ports.DesktopPort` over Win32 (3.4, 5.4.3 stages 1 and 2, 5.4.6, 5.7, 7.8, 12.7 H3 and R6).

- `enumerate_windows`: `EnumWindows` (top-level windows in z-order, topmost first), each window's pid, class, title,
  visibility, enabled state, owner, `WS_EX_TOPMOST`, its DWM extended frame bounds and its process image name;
  `select_windows` (pure) keeps the visible windows of the launched process tree, SunAwt classes first.
- `foreground_window`: `GetForegroundWindow` with the same reads (7.8 hardening, 5.4.3 stage 1).
- `set_focus(hwnd)`: the stage 1 correction, as pywinauto's `set_focus` does it (5.4.3: "it implements
  `AttachThreadInput` plus `SetForegroundWindow`"): restore a minimized window, attach this thread's input to the
  foreground thread's, `SetForegroundWindow` and `BringWindowToTop`, detach, and re-read the foreground.
- `hung(hwnd)`: the Watchdog's secondary hang check (5.14, 10.2): `IsHungAppWindow`, then
  `SendMessageTimeout(WM_NULL)`.
- `window_from_point`: `WindowFromPoint` for each sample point of the 8 x 8 occlusion grid, as its top-level window
  (`GetAncestor(GA_ROOT)`), pid and title (3.4).
- `virtual_screen`: `GetSystemMetrics` SM_XVIRTUALSCREEN to SM_CYVIRTUALSCREEN.
- `process_dpi_awareness`: the process's DPI awareness context by name (7.1, H3); `awareness_name` is the mapping.
- `input_desktop`: the `OpenInputDesktop` name (4.12, R6).
- `close_window`: `WM_CLOSE`, then `IsWindow` polled until the window is gone or the wait ends (4.10, 7.8).
- `grab`: one GDI grab of the whole virtual screen (`BitBlt` with `CAPTUREBLT` into a top-down 32-bit DIB, which
  excludes the cursor), as an H x W x 3 BGR array (`bgra_to_bgr`) with its origin and monotonic time (5.7).

Choices (reported):

1. The z-order is the window's index among the visible top-level windows of every process in `EnumWindows` order, so
   `0` is the topmost visible window of the desktop and a foreign window over the app has a smaller index.
2. `rect` is the DWM extended frame bounds only; a window DWM cannot answer for has `rect` None (never
   `GetWindowRect`, whose invisible resize borders would shift every crop, 5.7). `monitor` is left None.
3. `process_dpi_awareness(pid)` with no pid (or this process's) reads the calling thread's context, which is what the
   machine logs at H3 (`machine.py` calls it with no argument); another process's context comes from
   `GetDpiAwarenessContextForProcess`. Names: `PER_MONITOR_AWARE_V2`, `UNAWARE_GDISCALED`, `PER_MONITOR_AWARE`,
   `SYSTEM_AWARE`, `UNAWARE`, and `unreadable` when the process cannot be opened or the context is invalid.
4. `input_desktop` is `""` when the input desktop cannot be opened or named (the secure desktop of UAC or the lock
   screen denies it), which is not `Default`.
5. `close_window` waits up to 2 s (`CLOSE_WAIT_S`), polling every 50 ms; a window that was already gone is closed.
6. The grab is GDI `BitBlt`, what mss does on Windows (5.18 names mss; it is not a dependency of this package).
7. `set_focus` never sends the Alt keystroke pywinauto adds to lift the foreground lock: that would be input from
   outside the gateway's emitter (5.4.1). If Windows refuses the change, the re-check fails and the step is
   `foreground_lost` (5.4.3 stage 1).
8. `hung` answers within `HUNG_TIMEOUT_MS` (1 s, `SMTO_ABORTIFHUNG`); a window that no longer exists is not hung (the
   empty-tree check reports an exit, 10.2).

Verified here: the selection and ordering, every read's marshalling and failure path, the DPI names, the DIB
conversion and the GDI resource cleanup, with a fake table. Only Phase 0 on a rig verifies the real reads: DWM bounds
against the probe window (H3), that the GDI grab of a PER_MONITOR_AWARE_V2 process is byte-exact (H3), and that the
input desktop and foreground reads behave on the hypervisor console (R6).
"""
from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..orchestrator.ports import Frame, PointOwner, Rect, WindowInfo
from . import ffi

AWT_CLASSES = ("SunAwtFrame", "SunAwtDialog", "SunAwtWindow")      # 5.4.6 source 1
GW_OWNER, GA_ROOT, GWL_EXSTYLE, WS_EX_TOPMOST = 4, 2, -20, 0x0008
WM_CLOSE = 0x0010
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_READOBJECTS, UOI_NAME = 0x0001, 2
SRCCOPY, CAPTUREBLT, BI_RGB, DIB_RGB_COLORS = 0x00CC0020, 0x40000000, 0, 0
DPI_CONTEXT_NAMES = ((-4, "PER_MONITOR_AWARE_V2"), (-5, "UNAWARE_GDISCALED"))
DPI_AWARENESS_NAMES = {0: "UNAWARE", 1: "SYSTEM_AWARE", 2: "PER_MONITOR_AWARE"}
CLOSE_WAIT_S, CLOSE_POLL_S = 2.0, 0.05
SW_RESTORE, WM_NULL, SMTO_ABORTIFHUNG, HUNG_TIMEOUT_MS = 9, 0x0000, 0x0002, 1000
NEEDED = ("EnumWindows", "GetWindowThreadProcessId", "GetClassNameW", "GetWindowTextW", "IsWindowVisible",
          "IsWindowEnabled", "IsWindow", "GetWindow", "GetAncestor", "GetWindowLongW", "GetForegroundWindow",
          "WindowFromPoint", "GetSystemMetrics", "PostMessageW", "OpenInputDesktop", "GetUserObjectInformationW",
          "CloseDesktop", "DwmGetWindowAttribute", "GetThreadDpiAwarenessContext", "GetDpiAwarenessContextForProcess",
          "AreDpiAwarenessContextsEqual", "GetAwarenessFromDpiAwarenessContext", "OpenProcess", "CloseHandle",
          "QueryFullProcessImageNameW", "GetDC", "ReleaseDC", "CreateCompatibleDC", "CreateCompatibleBitmap",
          "SelectObject", "BitBlt", "GetDIBits", "DeleteObject", "DeleteDC", "IsIconic", "ShowWindow",
          "AttachThreadInput", "SetForegroundWindow", "BringWindowToTop", "GetCurrentThreadId", "IsHungAppWindow",
          "SendMessageTimeoutW")


@dataclass(frozen=True)
class RawWindow:
    """What the reads gave for one top-level window, before selection."""

    hwnd: int
    pid: int
    cls: str
    title: str
    visible: bool
    enabled: bool = True
    rect: Rect | None = None
    owner: int | None = None
    topmost: bool = False
    process_name: str | None = None
    z: int | None = None

    def info(self) -> WindowInfo:
        return WindowInfo(self.hwnd, self.pid, self.cls, self.title, self.visible, rect=self.rect, z_order=self.z,
                          owner_hwnd=self.owner, process_name=self.process_name, enabled=self.enabled,
                          topmost=self.topmost)


def select_windows(raws: Iterable[RawWindow], pids: Iterable[int]) -> tuple[WindowInfo, ...]:
    """The visible windows of the process tree, SunAwt classes first, each group topmost first (5.4.6)."""
    tree = frozenset(pids)
    mine = [r for r in raws if r.pid in tree and r.visible]
    mine.sort(key=lambda r: (r.cls not in AWT_CLASSES, r.z if r.z is not None else 1 << 30))
    return tuple(r.info() for r in mine)


def bgra_to_bgr(raw: bytes, width: int, height: int) -> np.ndarray:
    """A top-down 32-bit BGRA DIB as an H x W x 3 uint8 BGR array."""
    px = np.frombuffer(raw, dtype=np.uint8, count=width * height * 4).reshape(height, width, 4)
    return np.ascontiguousarray(px[:, :, :3])


def awareness_name(api: Any, ctx: int | None) -> str:
    """The name of a DPI awareness context (choice 3)."""
    if not ctx:
        return "unreadable"
    for value, name in DPI_CONTEXT_NAMES:
        if api.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(value)):
            return name
    return DPI_AWARENESS_NAMES.get(int(api.GetAwarenessFromDpiAwarenessContext(ctx)), "unreadable")


class Win32Desktop:
    """`ports.DesktopPort` over Win32 (module docstring). `pids` gives the launched process tree's pids (the
    `ProcessPort` tree); `api` is the function table (`ffi.load` by default, Windows only)."""

    def __init__(self, pids: Callable[[], Iterable[int]], api: Any = None, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.pids = pids
        self.api = api if api is not None else ffi.load(NEEDED)
        self.clock, self.sleep = clock, sleep

    # -- windows ----------------------------------------------------------------------------------------------
    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        hwnds: list[int] = []
        proc = self.api.WNDENUMPROC(lambda h, _lparam: hwnds.append(h) or True)
        if not self.api.EnumWindows(proc, 0):
            raise ffi.fail(self.api, "EnumWindows")
        tree = frozenset(self.pids())
        names: dict[int, str | None] = {}
        raws, z = [], 0
        for h in hwnds:
            if not self.api.IsWindowVisible(h):
                continue
            pid = self._pid(h)
            if pid in tree:
                raws.append(self._raw(h, pid, z, names))
            z += 1
        return select_windows(raws, tree)

    def foreground_window(self) -> WindowInfo | None:
        h = self.api.GetForegroundWindow()
        if not h:
            return None
        return self._raw(h, self._pid(h), None, {}).info()

    def set_focus(self, hwnd: int) -> bool:
        """Bring `hwnd` to the foreground (module docstring, choice 7); True when it is the foreground window after."""
        api = self.api
        if api.GetForegroundWindow() == hwnd:
            return True
        if api.IsIconic(hwnd):
            api.ShowWindow(hwnd, SW_RESTORE)
        fg = api.GetForegroundWindow()
        mine = int(api.GetCurrentThreadId())
        theirs = int(api.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = bool(theirs and theirs != mine and api.AttachThreadInput(mine, theirs, True))
        try:
            api.SetForegroundWindow(hwnd)
            api.BringWindowToTop(hwnd)
        finally:
            if attached:
                api.AttachThreadInput(mine, theirs, False)
        return api.GetForegroundWindow() == hwnd

    def hung(self, hwnd: int) -> bool:
        """The secondary hang check (choice 8): True when Windows calls the window hung or it does not answer
        `WM_NULL` within 1 s."""
        if not self.api.IsWindow(hwnd):
            return False
        if self.api.IsHungAppWindow(hwnd):
            return True
        result = ffi.ULONG_PTR()
        return not self.api.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, HUNG_TIMEOUT_MS, result)

    def _pid(self, hwnd: int) -> int:
        pid = ffi.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, pid)
        return int(pid.value)

    def _text(self, fn: Callable[..., int], hwnd: int, size: int) -> str:
        buf = ctypes.create_unicode_buffer(size)
        n = fn(hwnd, buf, size)
        return buf.value[:n] if n > 0 else ""

    def _raw(self, hwnd: int, pid: int, z: int | None, names: dict[int, str | None]) -> RawWindow:
        if pid not in names:
            names[pid] = self._process_name(pid)
        return RawWindow(hwnd, pid, self._text(self.api.GetClassNameW, hwnd, 256),
                         self._text(self.api.GetWindowTextW, hwnd, 1024), bool(self.api.IsWindowVisible(hwnd)),
                         bool(self.api.IsWindowEnabled(hwnd)), self.frame_bounds(hwnd),
                         self.api.GetWindow(hwnd, GW_OWNER) or None,
                         bool(self.api.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST), names[pid], z)

    def frame_bounds(self, hwnd: int) -> Rect | None:
        """`DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` as (x, y, w, h); None when DWM fails (choice 2)."""
        r = ffi.RECT()
        if self.api.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, r, ctypes.sizeof(r)) != 0:
            return None
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def _process_name(self, pid: int) -> str | None:
        h = self.api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ffi.DWORD(1024)
            if not self.api.QueryFullProcessImageNameW(h, 0, buf, size):
                return None
            return os.path.basename(buf.value[:size.value].replace("\\", "/")) or None
        finally:
            self.api.CloseHandle(h)

    def window_from_point(self, points: Sequence[tuple[int, int]]) -> tuple[PointOwner, ...]:
        out = []
        for x, y in points:
            h = self.api.WindowFromPoint(ffi.POINT(x, y))
            if not h:
                out.append(PointOwner(x, y, None, None))
                continue
            root = self.api.GetAncestor(h, GA_ROOT) or h
            out.append(PointOwner(x, y, root, self._pid(root), self._text(self.api.GetWindowTextW, root, 1024)))
        return tuple(out)

    def close_window(self, hwnd: int) -> bool:
        """`WM_CLOSE`, then wait for the window to go (choice 5); False when it stayed."""
        if not self.api.IsWindow(hwnd):
            return True
        self.api.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        deadline = self.clock() + CLOSE_WAIT_S
        while self.api.IsWindow(hwnd):
            if self.clock() >= deadline:
                return False
            self.sleep(CLOSE_POLL_S)
        return True

    # -- screen and session -----------------------------------------------------------------------------------
    def virtual_screen(self) -> Rect:
        m = self.api.GetSystemMetrics
        return m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN)

    def process_dpi_awareness(self, pid: int | None = None) -> str:
        """The DPI awareness of `pid`, or of this process when None (choice 3)."""
        if pid is None or pid == os.getpid():
            return awareness_name(self.api, self.api.GetThreadDpiAwarenessContext())
        h = self.api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return "unreadable"
        try:
            return awareness_name(self.api, self.api.GetDpiAwarenessContextForProcess(h))
        finally:
            self.api.CloseHandle(h)

    def input_desktop(self) -> str:
        """The input desktop's name; `""` when it cannot be opened or named (choice 4)."""
        h = self.api.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = ffi.DWORD()
            if not self.api.GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), needed):
                return ""
            return buf.value
        finally:
            self.api.CloseDesktop(h)

    def grab(self) -> Frame:
        """One GDI grab of the virtual screen (choice 6); raises `Win32Error` when a GDI call fails."""
        api = self.api
        x, y, w, h = self.virtual_screen()
        screen_dc = api.GetDC(None)
        if not screen_dc:
            raise ffi.fail(api, "GetDC")
        mem_dc = bitmap = old = None
        try:
            mem_dc = api.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise ffi.fail(api, "CreateCompatibleDC")
            bitmap = api.CreateCompatibleBitmap(screen_dc, w, h)
            if not bitmap:
                raise ffi.fail(api, "CreateCompatibleBitmap")
            old = api.SelectObject(mem_dc, bitmap)
            if not api.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, SRCCOPY | CAPTUREBLT):
                raise ffi.fail(api, "BitBlt")
            mono = self.clock()
            bmi = ffi.BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(ffi.BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h          # negative height: top-down rows
            bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount, bmi.bmiHeader.biCompression = 1, 32, BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            if api.GetDIBits(mem_dc, bitmap, 0, h, buf, bmi, DIB_RGB_COLORS) != h:
                raise ffi.fail(api, "GetDIBits")
            return Frame(bgra_to_bgr(buf.raw, w, h), (x, y), mono)
        finally:
            if old:
                api.SelectObject(mem_dc, old)
            if bitmap:
                api.DeleteObject(bitmap)
            if mem_dc:
                api.DeleteDC(mem_dc)
            api.ReleaseDC(None, screen_dc)
