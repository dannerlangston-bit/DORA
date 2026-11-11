"""`ports.ClipboardPort` over Win32 (2.8.1 Grid copy, 5.10: "clipboard read after copy_grid, then cleared").

- `read_text`: `OpenClipboard`, `CF_UNICODETEXT` if present, `GetClipboardData`, `GlobalLock`, the UTF-16 text up to
  its NUL (`decode_utf16`, pure), `GlobalUnlock`, `CloseClipboard`.
- `clear`: `OpenClipboard`, `EmptyClipboard`, `CloseClipboard`.

Choices (reported):

1. Another process can hold the clipboard open for a moment (the app writing its Copy All text), so opening is tried
   10 times 20 ms apart (`OPEN_ATTEMPTS`, `OPEN_RETRY_S`); a clipboard that stays busy raises `Win32Error`, never a
   quiet None, so a copy-grid step fails instead of reading "nothing copied" (`executor.export` polls `read_text`).
2. None means no text: no `CF_UNICODETEXT` on the clipboard, or an empty text. A handle that the format promised and
   the clipboard does not give, or memory that cannot be locked, raises `Win32Error`.
3. The text is read by its `GlobalSize` and decoded as UTF-16, so no platform `wchar_t` is involved.

Verified here: the open-retry policy, the decoding and the close on every path, with a fake table. Only Phase 0 on a
rig verifies that thinkorswim's Copy All puts `CF_UNICODETEXT` on the clipboard (2.8.1: "Copy or Copy All to
clipboard (unverified)").
"""
from __future__ import annotations

import ctypes
import time
from collections.abc import Callable
from typing import Any

from . import ffi

CF_UNICODETEXT = 13
OPEN_ATTEMPTS, OPEN_RETRY_S = 10, 0.02
NEEDED = ("OpenClipboard", "CloseClipboard", "EmptyClipboard", "IsClipboardFormatAvailable", "GetClipboardData",
          "GlobalLock", "GlobalUnlock", "GlobalSize")


def decode_utf16(raw: bytes) -> str:
    """UTF-16LE bytes up to the first NUL unit (choice 3)."""
    units = raw[:len(raw) - len(raw) % 2]
    return units.decode("utf-16-le", errors="replace").split("\0", 1)[0]


class Win32Clipboard:
    """`ports.ClipboardPort` over Win32 (module docstring); `api` is the function table (`ffi.load` by default,
    Windows only)."""

    def __init__(self, api: Any = None, *, sleep: Callable[[float], None] = time.sleep):
        self.api = api if api is not None else ffi.load(NEEDED)
        self.sleep = sleep

    def _open(self) -> None:
        for attempt in range(OPEN_ATTEMPTS):
            if self.api.OpenClipboard(None):
                return
            if attempt + 1 < OPEN_ATTEMPTS:
                self.sleep(OPEN_RETRY_S)
        raise ffi.fail(self.api, "OpenClipboard")

    def read_text(self) -> str | None:
        api = self.api
        self._open()
        try:
            if not api.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            h = api.GetClipboardData(CF_UNICODETEXT)
            if not h:
                raise ffi.fail(api, "GetClipboardData")
            p = api.GlobalLock(h)
            if not p:
                raise ffi.fail(api, "GlobalLock")
            try:
                text = decode_utf16(ctypes.string_at(p, int(api.GlobalSize(h))))
            finally:
                api.GlobalUnlock(h)
            return text or None
        finally:
            api.CloseClipboard()

    def clear(self) -> None:
        self._open()
        try:
            if not self.api.EmptyClipboard():
                raise ffi.fail(self.api, "EmptyClipboard")
        finally:
            self.api.CloseClipboard()
