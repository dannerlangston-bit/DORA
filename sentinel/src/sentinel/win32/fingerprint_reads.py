"""`ports.FingerprintReader` over Win32, the registry and the install folder (4.12, 7.1, 7.3, 7.4).

`OsProbe` makes the raw OS reads, each one a value or None when it cannot be made (`fingerprint_unreadable:<field>`
is the fingerprint module's decision, never inferred here):

- `windows_build`: registry `CurrentBuild` and `UBR` under `HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion`.
- `display`: `EnumDisplaySettingsW(ENUM_CURRENT_SETTINGS)` width, height, bits and frequency, `GetDpiForSystem`, the
  scale (dpi / 96), the harness's own DPI awareness and `SM_CMONITORS`.
- `cleartype`: `SPI_GETFONTSMOOTHING`, `SPI_GETFONTSMOOTHINGTYPE`, `SPI_GETFONTSMOOTHINGCONTRAST`,
  `SPI_GETFONTSMOOTHINGORIENTATION` and the registry `FontSmoothingGamma`.
- `gpu`: the display adapter's `DriverDesc` and `DriverVersion` from its device class key.
- `os_theme`: `AppsUseLightTheme`, `SystemUsesLightTheme`, `EnableTransparency` and `SPI_GETCLIENTAREAANIMATION`.
- `keyboard`: `GetKeyboardLayout(0)`, `GetKeyboardLayoutList` count, `GetKeyState` of Caps, Num and Scroll Lock,
  `GetUserDefaultLocaleName`.
- `time_zone`: `GetDynamicTimeZoneInformation` key name (what `tzutil /g` prints).
- `input_desktop`: the `DesktopPort`'s `OpenInputDesktop` name.

`Win32FingerprintReader.read_app` reads the install folder (platform-independent file reads: the JRE `release` file,
the vmoptions and its Java2D flags, the corpus files in usergui, the settings profiles, the jar listing) and takes
the fields only the running app can give from the caller (`AppSources`: the title build id, the About OCR, the
attestation OCR of the theme and time zones, the render probe hash, the jcmd pipeline confirmation).

Choices (reported):

1. `gpu` comes from the registry's display device class (`{4d36e968-e325-11ce-bfc1-08002be10318}`), the first adapter
   subkey with both values, not WMI (7.4 names `Win32_VideoController`): WMI needs COM and pywin32, and the class key
   holds the values `Win32_VideoController` reports. Phase 0 checks the two agree on the rig.
2. `os_theme.transparency` is the registry `EnableTransparency` (no `SystemParametersInfo` action reads it);
   `animations` is `SPI_GETCLIENTAREAANIMATION`.
3. A registry value that is absent is unreadable, not a default: `FontSmoothingGamma` included (7.1 expects the
   golden image to hold the tuning values).
4. Names: `cleartype.type` is `FE_FONTSMOOTHINGSTANDARD` or `FE_FONTSMOOTHINGCLEARTYPE`, `orientation` `RGB` or
   `BGR`, `keyboard.layout` the HKL's language id as `0x0409`, `display.scale_percent` the rounded dpi / 96 percent.
5. Hashes are `sha256:<hex>`. `settings_profile_hash` is over the canonical JSON of each profile's file name and
   file hash (one hash naming several files); `jar_listing_hash` over the sorted `relative/path<TAB>size` lines of
   every file under the install folder, slashes forward.
6. `java2d_pipeline.pinned` from the vmoptions: `-Dsun.java2d.opengl=true` is `opengl`, `-Dsun.java2d.d3d=false` is
   `software`, otherwise `d3d` (the Windows default); `confirmed` is the caller's jcmd read or `unknown`.
7. A read that raises (`OSError`, `Win32Error`, a malformed value, an exception of the caller's OCR source) is None,
   so one broken read never hides the others; the fingerprint module then makes the run's unreadable-field outcome,
   which is visible, not a quiet default.
8. The registry tells absent from unreadable: only `FileNotFoundError` reads as absent (None, False, []); access
   denied or any other `OSError` is raised, never read as "absent". Where absence is the passing state (preflight
   R5's reboot keys, 12.7: "no item passes by default"), an unreadable key makes the item unreadable
   (`Win32PreflightReads.read` catches the error); in the fingerprint reads it is None (choice 7).

Verified here: every read's marshalling and unreadable path with a fake table and registry, the file reads on real
files, and that the result is the `OsReads`/`AppReads` the fingerprint module accepts. Only Phase 0 on a rig verifies
the values against the 7.1 specification (R1 to R4) and that the registry keys exist in the golden image.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..canonical import sha256_id
from ..orchestrator.ports import AppReads, OsReads
from . import ffi
from .desktop import awareness_name

NT_CURRENT_VERSION = "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
DESKTOP_KEY = "HKCU\\Control Panel\\Desktop"
PERSONALIZE = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
DISPLAY_CLASS = "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}"
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
SM_CXSCREEN, SM_CYSCREEN, SM_CMONITORS = 0, 1, 80
SPI_GETFONTSMOOTHING, SPI_GETFONTSMOOTHINGTYPE = 0x004A, 0x200A
SPI_GETFONTSMOOTHINGCONTRAST, SPI_GETFONTSMOOTHINGORIENTATION = 0x200C, 0x2012
SPI_GETCLIENTAREAANIMATION = 0x1042
SMOOTHING_TYPES = {1: "FE_FONTSMOOTHINGSTANDARD", 2: "FE_FONTSMOOTHINGCLEARTYPE"}
ORIENTATIONS = {0: "BGR", 1: "RGB"}
VK_CAPITAL, VK_NUMLOCK, VK_SCROLL = 0x14, 0x90, 0x91
TIME_ZONE_ID_INVALID = 0xFFFFFFFF
LOCALE_NAME_MAX_LENGTH = 85
NEEDED = ("EnumDisplaySettingsW", "GetSystemMetrics", "GetDpiForSystem", "GetThreadDpiAwarenessContext",
          "AreDpiAwarenessContextsEqual", "GetAwarenessFromDpiAwarenessContext", "SystemParametersInfoW",
          "GetKeyboardLayout", "GetKeyboardLayoutList", "GetKeyState", "GetUserDefaultLocaleName",
          "GetDynamicTimeZoneInformation")


class Registry(Protocol):
    """Registry reads by full key path (`HKLM\\...`, `HKCU\\...`). Absent and unreadable differ (choice 8): each read
    raises `OSError` when the key or value cannot be read (access denied and the like)."""

    def value(self, key: str, name: str) -> Any | None:
        """The value, or None when the key or the value is absent."""

    def exists(self, key: str) -> bool:
        """True when the key exists, False when it is absent."""

    def subkeys(self, key: str) -> list[str]:
        """The names of the key's subkeys; [] when the key is absent."""


class WinRegistry:
    """`Registry` over the standard library's `winreg`, imported only when built (Windows only); 64-bit view. Only
    `FileNotFoundError` (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND) is absence; any other `OSError` is raised
    (choice 8)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ffi.Win32Unavailable("the registry exists only on Windows; pass a fake registry")
        import winreg
        self._w = winreg
        self._roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

    def _open(self, key: str) -> Any:
        root, _, sub = key.partition("\\")
        return self._w.OpenKey(self._roots[root], sub, 0, self._w.KEY_READ | self._w.KEY_WOW64_64KEY)

    def value(self, key: str, name: str) -> Any | None:
        try:
            with self._open(key) as k:
                return self._w.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        try:
            with self._open(key):
                return True
        except FileNotFoundError:
            return False

    def subkeys(self, key: str) -> list[str]:
        try:
            with self._open(key) as k:
                return [self._w.EnumKey(k, i) for i in range(self._w.QueryInfoKey(k)[0])]
        except FileNotFoundError:
            return []


def _safe(read: Callable[[], Any]) -> Any:
    """A read's value, or None when it raises (choice 7)."""
    try:
        return read()
    except Exception:
        return None


def _flag(v: Any) -> bool:
    if v is None:
        raise ValueError("absent registry value")
    return bool(int(v))


class OsProbe:
    """The raw OS reads (module docstring); each returns None when it cannot be made, or raises (a registry key it may
    not read, choice 8; callers wrap it, choice 7). `desktop` is the `DesktopPort` (its input desktop and awareness);
    `api` the function table, `registry` a `Registry`."""

    def __init__(self, desktop: Any, api: Any = None, registry: Registry | None = None):
        self.desktop = desktop
        self.api = api if api is not None else ffi.load(NEEDED)
        self.registry = registry if registry is not None else WinRegistry()

    def windows_build(self) -> dict[str, str] | None:
        build, ubr = (self.registry.value(NT_CURRENT_VERSION, n) for n in ("CurrentBuild", "UBR"))
        return None if build is None or ubr is None else {"current_build": str(build), "ubr": str(ubr)}

    def devmode(self) -> ffi.DEVMODEW | None:
        dm = ffi.DEVMODEW()
        dm.dmSize = ctypes.sizeof(ffi.DEVMODEW)
        return dm if self.api.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, dm) else None

    def display(self) -> dict[str, Any] | None:
        dm, dpi = self.devmode(), int(self.api.GetDpiForSystem())
        awareness = awareness_name(self.api, self.api.GetThreadDpiAwarenessContext())
        if dm is None or dpi <= 0 or awareness == "unreadable":
            return None
        return {"width": int(dm.dmPelsWidth), "height": int(dm.dmPelsHeight), "bits": int(dm.dmBitsPerPel),
                "frequency": int(dm.dmDisplayFrequency), "scale_percent": round(dpi * 100 / 96), "dpi": dpi,
                "awareness": awareness, "monitors": int(self.api.GetSystemMetrics(SM_CMONITORS))}

    def screen_metrics(self) -> tuple[int, int]:
        return int(self.api.GetSystemMetrics(SM_CXSCREEN)), int(self.api.GetSystemMetrics(SM_CYSCREEN))

    def _spi(self, action: int) -> int | None:
        out = ffi.UINT()
        return int(out.value) if self.api.SystemParametersInfoW(action, 0, ctypes.pointer(out), 0) else None

    def cleartype(self) -> dict[str, Any] | None:
        smoothing, kind = self._spi(SPI_GETFONTSMOOTHING), self._spi(SPI_GETFONTSMOOTHINGTYPE)
        contrast, orient = self._spi(SPI_GETFONTSMOOTHINGCONTRAST), self._spi(SPI_GETFONTSMOOTHINGORIENTATION)
        gamma = self.registry.value(DESKTOP_KEY, "FontSmoothingGamma")
        if None in (smoothing, kind, contrast, orient, gamma) or kind not in SMOOTHING_TYPES \
                or orient not in ORIENTATIONS:
            return None
        return {"smoothing": bool(smoothing), "type": SMOOTHING_TYPES[kind], "gamma": int(gamma),
                "contrast": int(contrast), "orientation": ORIENTATIONS[orient]}

    def gpu(self) -> dict[str, str] | None:
        for sub in sorted(s for s in self.registry.subkeys(DISPLAY_CLASS) if s.isdigit()):
            key = f"{DISPLAY_CLASS}\\{sub}"
            name, version = self.registry.value(key, "DriverDesc"), self.registry.value(key, "DriverVersion")
            if name and version:
                return {"name": str(name), "driver_version": str(version)}
        return None

    def os_theme(self) -> dict[str, bool] | None:
        animations = self._spi(SPI_GETCLIENTAREAANIMATION)
        if animations is None:
            return None
        return {"apps_light": _flag(self.registry.value(PERSONALIZE, "AppsUseLightTheme")),
                "system_light": _flag(self.registry.value(PERSONALIZE, "SystemUsesLightTheme")),
                "transparency": _flag(self.registry.value(PERSONALIZE, "EnableTransparency")),
                "animations": bool(animations)}

    def keyboard(self) -> dict[str, Any] | None:
        hkl = self.api.GetKeyboardLayout(0)
        locale = self.locale_name()
        if not hkl or locale is None:
            return None
        toggles = {name: bool(self.api.GetKeyState(vk) & 1)
                   for name, vk in (("caps", VK_CAPITAL), ("num", VK_NUMLOCK), ("scroll", VK_SCROLL))}
        return {"layout": f"0x{int(hkl) & 0xFFFF:04X}", "layout_count": int(self.api.GetKeyboardLayoutList(0, None)),
                "toggle_keys": toggles, "locale": locale}

    def locale_name(self) -> str | None:
        buf = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
        return buf.value if self.api.GetUserDefaultLocaleName(buf, LOCALE_NAME_MAX_LENGTH) > 0 else None

    def time_zone(self) -> str | None:
        tz = ffi.DYNAMIC_TIME_ZONE_INFORMATION()
        if self.api.GetDynamicTimeZoneInformation(tz) == TIME_ZONE_ID_INVALID:
            return None
        return ffi.wstr(tz.TimeZoneKeyName) or None

    def input_desktop(self) -> str | None:
        return self.desktop.input_desktop() or None


# -- the install folder (platform-independent) ---------------------------------------------------------------------
def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def jre_release(path: str) -> dict[str, str] | None:
    """The bundled runtime's `release` file: its hash and `JAVA_VERSION` (7.4)."""
    text = read_text(path)
    m = re.search(r'^JAVA_VERSION="?([^"\r\n]+)"?\s*$', text, re.M)
    return {"sha256": file_sha256(path), "java_version": m.group(1)} if m else None


def java2d_pinned(vmoptions_text: str) -> str:
    """The pipeline the vmoptions pin (choice 6)."""
    flags = dict(re.findall(r"^-Dsun\.java2d\.(\w+)=(\S+)\s*$", vmoptions_text, re.M))
    if flags.get("opengl", "").lower() == "true":
        return "opengl"
    return "software" if flags.get("d3d", "").lower() == "false" else "d3d"


def jar_listing_hash(install_dir: str) -> str:
    """The hash of the sorted names and sizes under the install folder (choice 5)."""
    lines = []
    for root, _dirs, files in os.walk(install_dir):
        for name in files:
            p = os.path.join(root, name)
            lines.append(f"{os.path.relpath(p, install_dir).replace(os.sep, '/')}\t{os.path.getsize(p)}")
    return "sha256:" + hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstallPaths:
    """Where the app's files are on the rig (7.3, 7.6): the install folder, its vmoptions and JRE release file, the
    corpus files as copied into usergui (corpus id -> path) and the settings profiles 7.6 re-applies."""

    install_dir: str
    vmoptions: str
    jre_release: str
    workspaces: Mapping[str, str] = field(default_factory=dict)
    settings_profiles: Sequence[str] = ()


def _none() -> None:
    return None


@dataclass(frozen=True)
class AppSources:
    """The app fields only the running app gives (7.3, 7.4), each a read that may give None."""

    title_build_id: Callable[[], str | None]
    about_build_id: Callable[[], str | None] = _none
    java2d_confirmed: Callable[[], str | None] = _none
    app_theme: Callable[[], Mapping[str, str] | None] = _none
    time_zone_app: Callable[[], Mapping[str, str] | None] = _none
    render_probe_hash: Callable[[], str | None] = _none


class Win32FingerprintReader:
    """`ports.FingerprintReader` (module docstring): the OS fields from `OsProbe`, the app fields from the install
    folder and `AppSources`; `rig` gives the record-class rig block (the Rig Manager's hostname, hypervisor and
    snapshot ids)."""

    def __init__(self, probe: OsProbe, paths: InstallPaths, app: AppSources,
                 rig: Callable[[], Mapping[str, Any] | None] = _none):
        self.probe, self.paths, self.app, self.rig = probe, paths, app, rig

    def read_os(self) -> OsReads:
        p = self.probe
        return OsReads(windows_build=_safe(p.windows_build), display=_safe(p.display), cleartype=_safe(p.cleartype),
                       gpu=_safe(p.gpu), os_theme=_safe(p.os_theme), keyboard=_safe(p.keyboard),
                       time_zone_os=_safe(p.time_zone), input_desktop=_safe(p.input_desktop), rig=_safe(self.rig))

    def read_app(self) -> AppReads:
        paths, app = self.paths, self.app
        vmoptions = _safe(lambda: read_text(paths.vmoptions))
        workspaces = {cid: _safe(lambda path=path: file_sha256(path)) for cid, path in paths.workspaces.items()}
        profiles = {os.path.basename(f): _safe(lambda f=f: file_sha256(f)) for f in paths.settings_profiles}
        return AppReads(
            build_id_title=_safe(app.title_build_id), build_id_about=_safe(app.about_build_id),
            jre_release=_safe(lambda: jre_release(paths.jre_release)),
            vmoptions_hash=_safe(lambda: file_sha256(paths.vmoptions)),
            java2d_pipeline=({"pinned": java2d_pinned(vmoptions), "confirmed": _safe(app.java2d_confirmed) or "unknown"}
                             if vmoptions is not None else None),
            app_theme=_safe(app.app_theme),
            workspace_hash=workspaces if workspaces and None not in workspaces.values() else None,
            settings_profile_hash=(sha256_id(profiles) if profiles and None not in profiles.values() else None),
            time_zone_app=_safe(app.time_zone_app), render_probe_hash=_safe(app.render_probe_hash),
            jar_listing_hash=_safe(lambda: jar_listing_hash(paths.install_dir)) if os.path.isdir(paths.install_dir)
            else None)
