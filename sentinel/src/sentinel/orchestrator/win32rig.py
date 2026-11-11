"""The Win32 rig: the Windows adapters composed behind `orchestrator.ports` for one shard (5.1, 5.18, 10.2).

`compose(config, ...)` builds `Win32Ports`, one adapter per port, from a `Win32RigConfig` (the pinned install folder,
the crash folders, the shard directory, the shared segment's name and the Orchestrator's pid):

- `desktop` (`ports.DesktopPort`): `win32.desktop.Win32Desktop`, whose window enumeration follows the process tree
  `processes` reports (`pids`);
- `processes` (`ports.ProcessPort`): `win32.process.Win32Processes` over the install folder and crash folders;
- `input` (`ports.InputPort`): `gateway.emitter_win32.Win32Emitter` through `rig.win32_input` (only the gateway,
  the executor's emission stage and the rig may import the emitter, 5.4.9); it takes the typed characters with the
  events (`InputPort.send(chars=)`);
- `segment` (`ports.SegmentPort`): `win32.shared_segment.SharedSegment.create`, made before the hook host starts;
- `access` (`ports.AccessPort`): `win32.access_bridge.Win32AccessBridge` on the main frame `desktop` finds;
- `clipboard` (`ports.ClipboardPort`): `win32.clipboard.Win32Clipboard`;
- `fingerprint` (`ports.FingerprintReader`) and `preflight` (`ports.PreflightReads`): `win32.fingerprint_reads`
  and `win32.preflight_reads` over one `OsProbe` on `desktop`;
- `watchdog_journal`: the Watchdog's `watchdog_events.jsonl` in the shard directory (`watchdog_loop.Journal`, the
  line format `store.journal` ingests at resume and at the shard import, 8.2, 10.2), which the Watchdog adapter
  appends to for every reaction, `main_frame_missing` included (CD-4).

Nothing here imports a Windows-only module at import time: `compose` imports the adapters when it is called, and
each adapter loads its Win32 function table (`win32.ffi.load`) only when no `api` is given. `factories` replaces any
adapter's constructor (a fake, in a test on Linux), so the composition itself is testable anywhere.

Choices (reported):

1. The main frame for the Access Bridge is the first enumerated window of the process tree whose title matches
   `config.title_regex` (the pinned main-window regex, 7.3), or None while none shows (the bridge then reads
   nothing).
2. The Watchdog journal lives at `<shard_dir>/watchdog_events.jsonl` (8.4 RIG layout), so the shard import finds it
   beside run.db (`store.importer`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..store.journal import WATCHDOG_JOURNAL
from .ports import (AccessPort, ClipboardPort, DesktopPort, FingerprintReader, InputPort, PreflightReads, ProcessPort,
                    SegmentPort)
from .rig import win32_input
from .watchdog_loop import Journal

PORTS = ("desktop", "processes", "input", "segment", "access", "clipboard", "fingerprint", "preflight")


@dataclass(frozen=True)
class Win32RigConfig:
    """What one shard's Win32 rig is built from (module docstring)."""

    install_dir: str                               # the pinned thinkorswim install folder (7.3)
    shard_dir: Path                                # the shard's directory on the rig (8.4 RIG layout)
    orchestrator_pid: int
    segment_name: str | None = None                # the hook host's named segment (5.4.2)
    crash_dirs: tuple[str, ...] = ()               # where hs_err, heap and minidumps land (7.1, 10.2)
    root_pid: Callable[[], int] = lambda: 0        # the launched app's pid; 0 = the install-folder rule alone (10.2)
    title_regex: str = r"thinkorswim"              # the pinned main-window title regex (7.3, choice 1)
    vmoptions: str = ""                            # the written vmoptions file
    jre_release: str = ""                          # the bundled runtime's release file
    workspaces: Mapping[str, str] = field(default_factory=dict)   # corpus id -> usergui path (G2)


@dataclass
class Win32Ports:
    """One shard's ports over Win32 (module docstring)."""

    desktop: DesktopPort
    processes: ProcessPort
    input: InputPort
    segment: SegmentPort | None
    access: AccessPort
    clipboard: ClipboardPort
    fingerprint: FingerprintReader
    preflight: PreflightReads
    watchdog_journal: Journal

    def missing(self) -> list[str]:
        """The ports whose adapter does not satisfy its protocol (empty for a whole rig)."""
        want = {"desktop": DesktopPort, "processes": ProcessPort, "input": InputPort, "access": AccessPort,
                "clipboard": ClipboardPort, "fingerprint": FingerprintReader, "preflight": PreflightReads}
        out = [name for name, proto in want.items() if not isinstance(getattr(self, name), proto)]
        if self.segment is not None and not isinstance(self.segment, SegmentPort):
            out.append("segment")
        return out


def _default_factories() -> dict[str, Callable[..., Any]]:
    """The Windows adapters' constructors, imported here (never at module import, module docstring)."""
    from ..win32.access_bridge import Win32AccessBridge
    from ..win32.clipboard import Win32Clipboard
    from ..win32.desktop import Win32Desktop
    from ..win32.fingerprint_reads import AppSources, InstallPaths, OsProbe, Win32FingerprintReader
    from ..win32.preflight_reads import Win32PreflightReads
    from ..win32.process import Win32Processes
    from ..win32.shared_segment import SharedSegment

    def fingerprint(probe: Any, cfg: Win32RigConfig, title: Callable[[], str | None]) -> Any:
        paths = InstallPaths(cfg.install_dir, cfg.vmoptions, cfg.jre_release, dict(cfg.workspaces))
        return Win32FingerprintReader(probe, paths, AppSources(title_build_id=title))

    return {
        "processes": lambda cfg, api: Win32Processes(cfg.install_dir, cfg.crash_dirs, api),
        "desktop": lambda pids, api: Win32Desktop(pids, api),
        "input": lambda api: win32_input(api),          # the emitter through the rig (5.4.9)
        "segment": lambda cfg: SharedSegment.create(cfg.segment_name, orchestrator_pid=cfg.orchestrator_pid),
        "access": lambda main_hwnd, api: Win32AccessBridge(main_hwnd, api),
        "clipboard": lambda api: Win32Clipboard(api),
        "probe": lambda desktop, api: OsProbe(desktop, api),
        "fingerprint": fingerprint,
        "preflight": lambda probe, api: Win32PreflightReads(probe, api),
    }


def compose(config: Win32RigConfig, *, api: Any = None,
            factories: Mapping[str, Callable[..., Any]] | None = None) -> Win32Ports:
    """The shard's ports (module docstring). `api` is the Win32 function table every adapter shares (None: each loads
    its own, Windows only); `factories` replaces adapter constructors by name (`PORTS`, plus `probe`)."""
    make = {**_default_factories(), **(factories or {})}
    processes = make["processes"](config, api)
    pids = lambda: {p.pid for p in processes.process_tree(config.root_pid())}   # noqa: E731 - the live tree
    desktop = make["desktop"](pids, api)
    pattern = re.compile(config.title_regex)

    def main_hwnd() -> int | None:
        """Choice 1: the first tree window whose title matches the pinned regex."""
        return next((w.hwnd for w in desktop.enumerate_windows() if pattern.search(w.title or "")), None)

    def title() -> str | None:
        return next((w.title for w in desktop.enumerate_windows() if pattern.search(w.title or "")), None)

    probe = make["probe"](desktop, api)
    return Win32Ports(desktop=desktop, processes=processes, input=make["input"](api),
                      segment=make["segment"](config) if config.segment_name else None,
                      access=make["access"](main_hwnd, api), clipboard=make["clipboard"](api),
                      fingerprint=make["fingerprint"](probe, config, title),
                      preflight=make["preflight"](probe, api),
                      watchdog_journal=Journal(Path(config.shard_dir) / WATCHDOG_JOURNAL))
