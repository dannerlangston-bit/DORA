"""`ports.PreflightReads` over Win32: the raw reads behind the 12.7 items made on the rig's desktop (7.1, 7.11, 7.12,
12.7). The evaluation against the pinned values is the preflight module's; this adapter only reads.

- R1: `current_build`, `ubr` (registry).
- R2: `width`, `height`, `bits`, `frequency` (EnumDisplaySettings), `cx_screen`, `cy_screen` (GetSystemMetrics),
  `dpi` (GetDpiForSystem), `monitors`.
- R3: `cleartype` (the fingerprint's SPI and registry reads).
- R4: `time_zone`, `keyboard` (layout, layout count, toggle keys, locale), `decimal`, `thousands`, `short_date`
  (GetLocaleInfoEx).
- R5: `pending_restart` (the servicing and Windows Update reboot keys, PendingFileRenameOperations),
  `screensaver_active`.
- R6: `input_desktop`, `session_protocol` and `session_state` (WTSQuerySessionInformation), `foreground_window`.
- H3: `dpi_awareness` of the harness process.
- H10: `wer_localdumps` (the LocalDumps key exists), `dump_folder` when set.

Choices (reported):

1. Every other item is not a desktop read (`OTHER_READERS` names who reads it: the Rig Manager, the gateway and
   Watchdog self-tests, the stores, the network preflight, the lint): it comes back `readable=False` with that in
   `detail`, so an item routed here by mistake can never pass by default (12.7: "no item passes by default").
2. A required value that cannot be read makes the item unreadable, `detail` naming the values; the values that were
   read are kept for run health. A read that raises is unreadable with the error in `detail`.
3. Names: `session_protocol` is `console`, `ica` or `rdp`; `session_state` the `WTS_CONNECTSTATE_CLASS` name
   (`active`, `disconnected`, ...); `screensaver_active` the registry `ScreenSaveActive` as a bool.
4. R5 reads the registry halves only; powercfg, Focus Assist and the probe screenshot are the Rig Manager's and the
   capture's, and R6's probe screenshot is the capture's (12.7 lists them under the same items).

Verified here: each item's reads, names and unreadable paths with a fake table and registry. Only Phase 0 on a rig
verifies the values on the hypervisor console (R6), the reboot keys after a Windows Update (R5) and the LocalDumps key
of the golden image (H10).
"""
from __future__ import annotations

import ctypes
from collections.abc import Callable, Mapping
from typing import Any

from ..orchestrator.ports import PreflightRead
from . import ffi
from .fingerprint_reads import DESKTOP_KEY, SM_CMONITORS, OsProbe

WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_CONNECT_STATE, WTS_CLIENT_PROTOCOL_TYPE = 8, 16
PROTOCOLS = {0: "console", 1: "ica", 2: "rdp"}
CONNECT_STATES = ("active", "connected", "connect_query", "shadow", "disconnected", "idle", "listen", "reset", "down",
                  "init")
LOCALE_SDECIMAL, LOCALE_STHOUSAND, LOCALE_SSHORTDATE = 0x0E, 0x0F, 0x1F
REBOOT_KEYS = ("HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending",
               "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired")
SESSION_MANAGER = "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager"
WER_LOCALDUMPS = "HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps"
OTHER_READERS = {
    "R7": "the Rig Manager and the control host (free space, clock offset)",
    "B1": "the Rig Manager (installer hashes)", "B2": "the Rig Manager (launcher pin)",
    "B3": "the fingerprint's app reads (vmoptions, JRE release, Java2D pipeline)",
    "H1": "the gateway's injection self-test", "H2": "the hook host's self-test", "H4": "the gateway deny self-test",
    "H5": "the kill switch channels", "H6": "the redaction self-test", "H7": "the stores", "H8": "the Watchdog",
    "H9": "the OCR calibration panel and the JAB bridge",
    **{f"N{i}": "the network preflight (7.11)" for i in range(1, 8)},
    **{f"G{i}": "the lint, the registries and the fingerprint pre-match" for i in range(1, 6)},
}
NEEDED = ("WTSQuerySessionInformationW", "WTSFreeMemory", "GetLocaleInfoEx", "GetForegroundWindow")


class Win32PreflightReads:
    """`ports.PreflightReads` (module docstring) over an `OsProbe` (its table needs `NEEDED` as well)."""

    def __init__(self, probe: OsProbe, api: Any = None):
        self.probe = probe
        self.api = api if api is not None else ffi.load(NEEDED)
        self._readers: dict[str, tuple[Callable[[], Mapping[str, Any]], tuple[str, ...] | None]] = {
            "R1": (self._r1, None), "R2": (self._r2, None), "R3": (self._r3, None), "R4": (self._r4, None),
            "R5": (self._r5, None), "R6": (self._r6, None), "H3": (self._h3, None),
            "H10": (self._h10, ("wer_localdumps",)),
        }

    def read(self, item: str) -> PreflightRead:
        if item not in self._readers:
            who = OTHER_READERS.get(item)
            why = f"not a desktop read: {who} reads {item}" if who else f"{item} is not a 12.7 item"
            return PreflightRead(item, readable=False, detail=why)
        reader, required = self._readers[item]
        try:
            values = dict(reader())
        except (OSError, ValueError) as e:
            return PreflightRead(item, readable=False, detail=f"read failed: {e}")
        missing = [k for k in (required or values) if values.get(k) is None]
        if missing:
            return PreflightRead(item, values, readable=False, detail="unreadable: " + ", ".join(missing))
        return PreflightRead(item, values)

    # -- the items ----------------------------------------------------------------------------------------------
    def _r1(self) -> Mapping[str, Any]:
        build = self.probe.windows_build() or {}
        return {"current_build": build.get("current_build"), "ubr": build.get("ubr")}

    def _r2(self) -> Mapping[str, Any]:
        p = self.probe
        dm = p.devmode()
        cx, cy = p.screen_metrics()
        dpi = int(p.api.GetDpiForSystem())
        return {"width": int(dm.dmPelsWidth) if dm else None, "height": int(dm.dmPelsHeight) if dm else None,
                "bits": int(dm.dmBitsPerPel) if dm else None, "frequency": int(dm.dmDisplayFrequency) if dm else None,
                "cx_screen": cx, "cy_screen": cy, "dpi": dpi if dpi > 0 else None,
                "monitors": int(p.api.GetSystemMetrics(SM_CMONITORS))}

    def _r3(self) -> Mapping[str, Any]:
        return {"cleartype": self.probe.cleartype()}

    def _locale(self, lctype: int) -> str | None:
        buf = ctypes.create_unicode_buffer(80)
        return buf.value if self.api.GetLocaleInfoEx(None, lctype, buf, 80) > 0 else None

    def _r4(self) -> Mapping[str, Any]:
        return {"time_zone": self.probe.time_zone(), "keyboard": self.probe.keyboard(),
                "decimal": self._locale(LOCALE_SDECIMAL), "thousands": self._locale(LOCALE_STHOUSAND),
                "short_date": self._locale(LOCALE_SSHORTDATE)}

    def _r5(self) -> Mapping[str, Any]:
        reg = self.probe.registry
        pending = any(reg.exists(k) for k in REBOOT_KEYS) or bool(reg.value(SESSION_MANAGER,
                                                                             "PendingFileRenameOperations"))
        saver = reg.value(DESKTOP_KEY, "ScreenSaveActive")
        return {"pending_restart": pending, "screensaver_active": None if saver is None else str(saver).strip() == "1"}

    def _wts(self, info_class: int) -> int | None:
        buf, size = ffi.LPVOID(), ffi.DWORD()
        if not self.api.WTSQuerySessionInformationW(None, WTS_CURRENT_SESSION, info_class, buf, size) or not buf.value:
            return None
        try:
            kind = ctypes.c_uint16 if info_class == WTS_CLIENT_PROTOCOL_TYPE else ctypes.c_int32
            return int(kind.from_address(buf.value).value)
        finally:
            self.api.WTSFreeMemory(buf)

    def _r6(self) -> Mapping[str, Any]:
        protocol, state = self._wts(WTS_CLIENT_PROTOCOL_TYPE), self._wts(WTS_CONNECT_STATE)
        return {"input_desktop": self.probe.input_desktop(),
                "session_protocol": PROTOCOLS.get(protocol) if protocol is not None else None,
                "session_state": CONNECT_STATES[state] if state is not None and 0 <= state < len(CONNECT_STATES)
                else None,
                "foreground_window": bool(self.api.GetForegroundWindow())}

    def _h3(self) -> Mapping[str, Any]:
        awareness = self.probe.desktop.process_dpi_awareness()
        return {"dpi_awareness": None if awareness == "unreadable" else awareness}

    def _h10(self) -> Mapping[str, Any]:
        reg = self.probe.registry
        values: dict[str, Any] = {"wer_localdumps": reg.exists(WER_LOCALDUMPS)}
        folder = reg.value(WER_LOCALDUMPS, "DumpFolder")
        if folder:
            values["dump_folder"] = str(folder)
        return values
