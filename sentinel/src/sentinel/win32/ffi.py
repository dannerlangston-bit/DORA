"""The thin ctypes layer of the Windows adapters (5.1, 5.18): fixed-width Win32 types, the structures, the function
prototypes, a lazy loader and the error mapping. Nothing here decides anything.

Every adapter calls Win32 through a function table: an object whose attributes are the Win32 functions by their
exported names, plus the callback factories (`WNDENUMPROC`, `HOOKPROC`), `get_last_error` and `set_last_error`.
`load(names)` builds the real table from `PROTOTYPES` with `ctypes.WinDLL(..., use_last_error=True)` and
`ctypes.WINFUNCTYPE`, only when `sys.platform == "win32"`; elsewhere it raises `Win32Unavailable`. Tests pass a fake
table instead, so the adapters' marshalling (the structures they build and pass, the out-parameters they read back)
and their error paths run on any platform.

Choices (reported):

1. The integer types are fixed width (`LONG` is `c_int32`, `WCHAR` is `c_uint16`, `ULONG_PTR` the pointer width)
   rather than `ctypes.wintypes`, which is a Windows import and whose `c_long` and `c_wchar` are 8 and 4 bytes on
   Linux. The structures therefore have their Windows x64 layout wherever they are built, and the tests check their
   sizes and offsets against the Windows SDK values.
2. Wide strings embedded in structures are `WCHAR` arrays decoded with `wstr`; string buffers passed to a function
   are `ctypes.create_unicode_buffer` (2-byte `c_wchar` on Windows).
3. Out-parameters are passed as the structure or scalar instance itself where the prototype declares a pointer, so
   ctypes passes it by reference on Windows and a fake receives the instance and fills it in.
4. `EMITTER_TAG` and `DRILL_TAG` are the `dwExtraInfo` values the gateway's emitter and the fire drill injector put
   on every event they inject, from which the hook tells `emitter` and `drill` from `foreign_process` (5.4.2,
   `gateway.segment` choice 5). They live here, beside the INPUT and hook structures both sides use, because the
   hook host must not import the emitter. A process that forges the tag still needs a token for every event.

Verified here: the layouts and the prototype table. Only Phase 0 on a rig verifies that the DLLs export these
names with these signatures (5.19).
"""
from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# -- fixed-width Win32 types (choice 1) ---------------------------------------------------------------------------
_PTR64 = ctypes.sizeof(ctypes.c_void_p) == 8
BOOL = ctypes.c_int32
BOOLEAN = ctypes.c_uint8
BYTE = ctypes.c_uint8
WORD = ctypes.c_uint16
SHORT = ctypes.c_int16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
INT = ctypes.c_int32
UINT = ctypes.c_uint32
WCHAR = ctypes.c_uint16
ULONG_PTR = ctypes.c_uint64 if _PTR64 else ctypes.c_uint32
LONG_PTR = ctypes.c_int64 if _PTR64 else ctypes.c_int32
WPARAM = ULONG_PTR
LPARAM = LONG_PTR
LRESULT = LONG_PTR
HRESULT = LONG
SIZE_T = ctypes.c_size_t
HANDLE = ctypes.c_void_p
HWND = HDC = HBITMAP = HGDIOBJ = HHOOK = HINSTANCE = HKL = HDESK = HANDLE
LPWSTR = ctypes.c_wchar_p
LPVOID = ctypes.c_void_p
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

EMITTER_TAG = 0x534E5447454D4954           # b"SNTGEMIT" (choice 4)
DRILL_TAG = 0x534E544744524C4C             # b"SNTGDRLL"

ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF


# -- structures ---------------------------------------------------------------------------------------------------
class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", DWORD), ("flags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", DWORD), ("scanCode", DWORD), ("flags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM), ("time", DWORD),
                ("pt", POINT), ("lPrivate", DWORD)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]

    @property
    def ticks(self) -> int:
        """The 100-nanosecond count."""
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD), ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", DWORD), ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", LONG), ("dwFlags", DWORD), ("szExeFile", WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("PageFaultCount", DWORD), ("PeakWorkingSetSize", SIZE_T),
                ("WorkingSetSize", SIZE_T), ("QuotaPeakPagedPoolUsage", SIZE_T), ("QuotaPagedPoolUsage", SIZE_T),
                ("QuotaPeakNonPagedPoolUsage", SIZE_T), ("QuotaNonPagedPoolUsage", SIZE_T),
                ("PagefileUsage", SIZE_T), ("PeakPagefileUsage", SIZE_T), ("PrivateUsage", SIZE_T)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG), ("biPlanes", WORD), ("biBitCount", WORD),
                ("biCompression", DWORD), ("biSizeImage", DWORD), ("biXPelsPerMeter", LONG),
                ("biYPelsPerMeter", LONG), ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 1)]


class DEVMODEW(ctypes.Structure):
    """The display variant of DEVMODEW (the printer fields share the 16-byte union with the display position)."""

    _fields_ = [("dmDeviceName", WCHAR * 32), ("dmSpecVersion", WORD), ("dmDriverVersion", WORD), ("dmSize", WORD),
                ("dmDriverExtra", WORD), ("dmFields", DWORD), ("dmPosition", POINT),
                ("dmDisplayOrientation", DWORD), ("dmDisplayFixedOutput", DWORD), ("dmColor", SHORT),
                ("dmDuplex", SHORT), ("dmYResolution", SHORT), ("dmTTOption", SHORT), ("dmCollate", SHORT),
                ("dmFormName", WCHAR * 32), ("dmLogPixels", WORD), ("dmBitsPerPel", DWORD), ("dmPelsWidth", DWORD),
                ("dmPelsHeight", DWORD), ("dmDisplayFlags", DWORD), ("dmDisplayFrequency", DWORD),
                ("dmICMMethod", DWORD), ("dmICMIntent", DWORD), ("dmMediaType", DWORD), ("dmDitherType", DWORD),
                ("dmReserved1", DWORD), ("dmReserved2", DWORD), ("dmPanningWidth", DWORD),
                ("dmPanningHeight", DWORD)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond",
                                    "wMilliseconds")]


class DYNAMIC_TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [("Bias", LONG), ("StandardName", WCHAR * 32), ("StandardDate", SYSTEMTIME), ("StandardBias", LONG),
                ("DaylightName", WCHAR * 32), ("DaylightDate", SYSTEMTIME), ("DaylightBias", LONG),
                ("TimeZoneKeyName", WCHAR * 128), ("DynamicDaylightTimeDisabled", BOOLEAN)]


# -- prototypes ---------------------------------------------------------------------------------------------------
#: Callback types, built with WINFUNCTYPE by `load`: name -> (restype, argtypes).
CALLBACKS: dict[str, tuple[Any, tuple[Any, ...]]] = {
    "WNDENUMPROC": (BOOL, (HWND, LPARAM)),
    "HOOKPROC": (LRESULT, (INT, WPARAM, LPARAM)),
}

_P = ctypes.POINTER
#: Function name -> (DLL, restype, argtypes). A string argtype names a `CALLBACKS` type.
PROTOTYPES: dict[str, tuple[str, Any, tuple[Any, ...]]] = {
    # windows and focus
    "EnumWindows": ("user32", BOOL, ("WNDENUMPROC", LPARAM)),
    "GetWindowThreadProcessId": ("user32", DWORD, (HWND, _P(DWORD))),
    "GetClassNameW": ("user32", INT, (HWND, LPWSTR, INT)),
    "GetWindowTextW": ("user32", INT, (HWND, LPWSTR, INT)),
    "IsWindowVisible": ("user32", BOOL, (HWND,)),
    "IsWindowEnabled": ("user32", BOOL, (HWND,)),
    "IsWindow": ("user32", BOOL, (HWND,)),
    "GetWindow": ("user32", HWND, (HWND, UINT)),
    "GetAncestor": ("user32", HWND, (HWND, UINT)),
    "GetWindowLongW": ("user32", LONG, (HWND, INT)),
    "GetForegroundWindow": ("user32", HWND, ()),
    "WindowFromPoint": ("user32", HWND, (POINT,)),
    "GetSystemMetrics": ("user32", INT, (INT,)),
    "PostMessageW": ("user32", BOOL, (HWND, UINT, WPARAM, LPARAM)),
    "OpenInputDesktop": ("user32", HDESK, (DWORD, BOOL, DWORD)),
    "GetUserObjectInformationW": ("user32", BOOL, (HANDLE, INT, LPWSTR, DWORD, _P(DWORD))),
    "CloseDesktop": ("user32", BOOL, (HDESK,)),
    "GetCursorPos": ("user32", BOOL, (_P(POINT),)),
    "IsIconic": ("user32", BOOL, (HWND,)),
    "ShowWindow": ("user32", BOOL, (HWND, INT)),
    "AttachThreadInput": ("user32", BOOL, (DWORD, DWORD, BOOL)),
    "SetForegroundWindow": ("user32", BOOL, (HWND,)),
    "BringWindowToTop": ("user32", BOOL, (HWND,)),
    "GetCurrentThreadId": ("kernel32", DWORD, ()),
    "IsHungAppWindow": ("user32", BOOL, (HWND,)),
    "SendMessageTimeoutW": ("user32", LRESULT, (HWND, UINT, WPARAM, LPARAM, UINT, UINT, _P(ULONG_PTR))),
    "MapVirtualKeyW": ("user32", UINT, (UINT, UINT)),
    "DwmGetWindowAttribute": ("dwmapi", HRESULT, (HWND, DWORD, _P(RECT), DWORD)),
    # DPI (5.18: PER_MONITOR_AWARE_V2 first in every harness process)
    "SetProcessDpiAwarenessContext": ("user32", BOOL, (LPVOID,)),
    "GetThreadDpiAwarenessContext": ("user32", LPVOID, ()),
    "GetDpiAwarenessContextForProcess": ("user32", LPVOID, (HANDLE,)),
    "AreDpiAwarenessContextsEqual": ("user32", BOOL, (LPVOID, LPVOID)),
    "GetAwarenessFromDpiAwarenessContext": ("user32", INT, (LPVOID,)),
    "GetDpiForSystem": ("user32", UINT, ()),
    # GDI grab (5.7)
    "GetDC": ("user32", HDC, (HWND,)),
    "ReleaseDC": ("user32", INT, (HWND, HDC)),
    "CreateCompatibleDC": ("gdi32", HDC, (HDC,)),
    "CreateCompatibleBitmap": ("gdi32", HBITMAP, (HDC, INT, INT)),
    "SelectObject": ("gdi32", HGDIOBJ, (HDC, HGDIOBJ)),
    "BitBlt": ("gdi32", BOOL, (HDC, INT, INT, INT, INT, HDC, INT, INT, DWORD)),
    "GetDIBits": ("gdi32", INT, (HDC, HBITMAP, UINT, UINT, LPVOID, _P(BITMAPINFO), UINT)),
    "DeleteObject": ("gdi32", BOOL, (HGDIOBJ,)),
    "DeleteDC": ("gdi32", BOOL, (HDC,)),
    # hooks and the message loop (5.4.2, 5.4.4)
    "SetWindowsHookExW": ("user32", HHOOK, (INT, "HOOKPROC", HINSTANCE, DWORD)),
    "UnhookWindowsHookEx": ("user32", BOOL, (HHOOK,)),
    "CallNextHookEx": ("user32", LRESULT, (HHOOK, INT, WPARAM, LPARAM)),
    "MsgWaitForMultipleObjects": ("user32", DWORD, (DWORD, LPVOID, BOOL, DWORD, DWORD)),
    "PeekMessageW": ("user32", BOOL, (_P(MSG), HWND, UINT, UINT, UINT)),
    "TranslateMessage": ("user32", BOOL, (_P(MSG),)),
    "DispatchMessageW": ("user32", LRESULT, (_P(MSG),)),
    "GetModuleHandleW": ("kernel32", HINSTANCE, (LPWSTR,)),
    "GetCurrentThread": ("kernel32", HANDLE, ()),
    "SetThreadPriority": ("kernel32", BOOL, (HANDLE, INT)),
    "CreateEventW": ("kernel32", HANDLE, (LPVOID, BOOL, BOOL, LPWSTR)),
    # processes (5.14, 6.6, 10.2)
    "OpenProcess": ("kernel32", HANDLE, (DWORD, BOOL, DWORD)),
    "CloseHandle": ("kernel32", BOOL, (HANDLE,)),
    "WaitForSingleObject": ("kernel32", DWORD, (HANDLE, DWORD)),
    "TerminateProcess": ("kernel32", BOOL, (HANDLE, UINT)),
    "QueryFullProcessImageNameW": ("kernel32", BOOL, (HANDLE, DWORD, LPWSTR, _P(DWORD))),
    "GetProcessTimes": ("kernel32", BOOL, (HANDLE, _P(FILETIME), _P(FILETIME), _P(FILETIME), _P(FILETIME))),
    "GetProcessHandleCount": ("kernel32", BOOL, (HANDLE, _P(DWORD))),
    "CreateToolhelp32Snapshot": ("kernel32", HANDLE, (DWORD, DWORD)),
    "Process32FirstW": ("kernel32", BOOL, (HANDLE, _P(PROCESSENTRY32W))),
    "Process32NextW": ("kernel32", BOOL, (HANDLE, _P(PROCESSENTRY32W))),
    "GetProcessMemoryInfo": ("psapi", BOOL, (HANDLE, _P(PROCESS_MEMORY_COUNTERS_EX), DWORD)),
    "GetGuiResources": ("user32", DWORD, (HANDLE, DWORD)),
    # clipboard (2.8.1, 5.10)
    "OpenClipboard": ("user32", BOOL, (HWND,)),
    "CloseClipboard": ("user32", BOOL, ()),
    "EmptyClipboard": ("user32", BOOL, ()),
    "IsClipboardFormatAvailable": ("user32", BOOL, (UINT,)),
    "GetClipboardData": ("user32", HANDLE, (UINT,)),
    "GlobalLock": ("kernel32", LPVOID, (HANDLE,)),
    "GlobalUnlock": ("kernel32", BOOL, (HANDLE,)),
    "GlobalSize": ("kernel32", SIZE_T, (HANDLE,)),
    # fingerprint and preflight reads (4.12, 7.1, 7.4, 12.7)
    "EnumDisplaySettingsW": ("user32", BOOL, (LPWSTR, DWORD, _P(DEVMODEW))),
    "SystemParametersInfoW": ("user32", BOOL, (UINT, UINT, LPVOID, UINT)),
    "GetKeyboardLayout": ("user32", HKL, (DWORD,)),
    "GetKeyboardLayoutList": ("user32", INT, (INT, LPVOID)),
    "GetKeyState": ("user32", SHORT, (INT,)),
    "GetUserDefaultLocaleName": ("kernel32", INT, (LPWSTR, INT)),
    "GetLocaleInfoEx": ("kernel32", INT, (LPWSTR, DWORD, LPWSTR, INT)),
    "GetDynamicTimeZoneInformation": ("kernel32", DWORD, (_P(DYNAMIC_TIME_ZONE_INFORMATION),)),
    "WTSQuerySessionInformationW": ("wtsapi32", BOOL, (HANDLE, DWORD, INT, _P(LPVOID), _P(DWORD))),
    "WTSFreeMemory": ("wtsapi32", None, (LPVOID,)),
}


class Win32Unavailable(RuntimeError):
    """The real function table exists only on Windows; elsewhere an adapter needs a fake table."""


class Win32Error(OSError):
    """A Win32 call failed: the function's name and `GetLastError` (the `winerror` attribute)."""

    def __init__(self, function: str, code: int):
        super().__init__(f"{function} failed (error {code})")
        self.function = function
        self.code = code


def load(names: Iterable[str], extra: Mapping[str, tuple[str, Any, tuple[Any, ...]]] | None = None) -> Any:
    """The real table of the named functions (from `PROTOTYPES`, or `extra` for prototypes a module keeps to
    itself), with the `CALLBACKS` factories, `get_last_error` and `set_last_error`. Raises `Win32Unavailable` off
    Windows."""
    if not IS_WINDOWS:
        raise Win32Unavailable("the Win32 function table exists only on Windows; pass a fake table")
    protos = {**PROTOTYPES, **(extra or {})}
    factory = ctypes.WINFUNCTYPE  # type: ignore[attr-defined]
    callbacks = {name: factory(res, *args) for name, (res, args) in CALLBACKS.items()}
    table = SimpleNamespace(get_last_error=ctypes.get_last_error,  # type: ignore[attr-defined]
                            set_last_error=ctypes.set_last_error, **callbacks)  # type: ignore[attr-defined]
    dlls: dict[str, Any] = {}
    for name in names:
        dll, res, args = protos[name]
        lib = dlls.get(dll)
        if lib is None:
            lib = dlls[dll] = ctypes.WinDLL(dll, use_last_error=True)  # type: ignore[attr-defined]
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = [callbacks[a] if isinstance(a, str) else a for a in args]
        setattr(table, name, fn)
    return table


def fail(api: Any, function: str) -> Win32Error:
    """The `Win32Error` of a call that just failed, with the thread's last error."""
    return Win32Error(function, int(api.get_last_error()))


def handle_ok(h: int | None) -> bool:
    """A returned handle is usable: not NULL and not INVALID_HANDLE_VALUE."""
    return bool(h) and h != INVALID_HANDLE_VALUE


def wstr(chars: Any) -> str:
    """A `WCHAR` array (choice 2) as text, up to its first NUL."""
    raw = bytes(chars).decode("utf-16-le", errors="replace")
    return raw.split("\0", 1)[0]

