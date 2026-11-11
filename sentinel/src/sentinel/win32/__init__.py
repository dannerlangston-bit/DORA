"""The Windows adapters of the ports (5.1, 5.18; `orchestrator.ports`): thin ctypes layers over Win32, DWM, the Java
Access Bridge and the clipboard, each behind an injectable function table so the decisions are testable anywhere.

- `ffi`: fixed-width Win32 types, the structures, the function prototypes and the lazy loader (`load`).
- `desktop`: `DesktopPort` (EnumWindows, GetForegroundWindow, DWM extended frame bounds, WindowFromPoint, DPI
  awareness, the input desktop, WM_CLOSE, a GDI grab of the virtual screen).
- `process`: `ProcessPort` (Toolhelp snapshots, GetProcessMemoryInfo, GetGuiResources, the kill, crash files).
- `clipboard`: `ClipboardPort`.
- `access_bridge`: `AccessPort` over WindowsAccessBridge-64.dll.
- `fingerprint_reads`, `preflight_reads`: `FingerprintReader` and `PreflightReads` over the registry,
  EnumDisplaySettings and SystemParametersInfo.
- `shared_segment`: `SegmentPort` over the named shared-memory segment of 5.4.2.

The `SendInput` adapter is `sentinel.gateway.emitter_win32` and the hook host `sentinel.gateway.hookhost_win32`:
`sentinel lint --code` lets only the gateway's emitter inject (5.4.1, 5.4.9).

This package imports nothing on import, and no module of it imports a Windows-only module at import time: every
Win32 call is made through a table that `ffi.load` builds only when `sys.platform == "win32"`.
"""
