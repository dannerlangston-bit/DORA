"""Tests for sentinel.win32.fingerprint_reads and sentinel.win32.preflight_reads: the 4.12 fingerprint reads and the
12.7 rig reads over a fake Win32 table, a fake registry and real install-folder files."""
import ctypes
import hashlib
import sys
import types

import pytest

from sentinel import fingerprint as F
from sentinel.orchestrator.ports import FingerprintReader, PreflightReads
from sentinel.records import Fingerprint
from sentinel.win32 import ffi
from sentinel.win32 import fingerprint_reads as fr
from sentinel.win32 import preflight_reads as pr

V2 = 0x1001
H = "sha256:" + "c" * 64
GPU_KEY = fr.DISPLAY_CLASS + "\\0000"


class FakeRegistry:
    def __init__(self):
        self.values = {
            (fr.NT_CURRENT_VERSION, "CurrentBuild"): "26100", (fr.NT_CURRENT_VERSION, "UBR"): 2314,
            (fr.DESKTOP_KEY, "FontSmoothingGamma"): 1800, (fr.DESKTOP_KEY, "ScreenSaveActive"): "0",
            (fr.PERSONALIZE, "AppsUseLightTheme"): 0, (fr.PERSONALIZE, "SystemUsesLightTheme"): 0,
            (fr.PERSONALIZE, "EnableTransparency"): 0,
            (GPU_KEY, "DriverDesc"): "Hyper-V Video", (GPU_KEY, "DriverVersion"): "10.0.26100.1",
            (fr.DISPLAY_CLASS + "\\Properties", "DriverDesc"): "not an adapter",
        }
        self.keys = {pr.WER_LOCALDUMPS}
        self.subs = {fr.DISPLAY_CLASS: ["Configuration", "Properties", "0001", "0000"]}

    def value(self, key, name):
        return self.values.get((key, name))

    def exists(self, key):
        return key in self.keys

    def subkeys(self, key):
        return self.subs.get(key, [])


class FakeApi:
    """The display, SPI, keyboard, locale, time zone and WTS reads of a 7.1 VM rig."""

    def __init__(self):
        self.dm = {"dmPelsWidth": 1920, "dmPelsHeight": 1080, "dmBitsPerPel": 32, "dmDisplayFrequency": 60}
        self.dpi = 96
        self.spi = {fr.SPI_GETFONTSMOOTHING: 1, fr.SPI_GETFONTSMOOTHINGTYPE: 2, fr.SPI_GETFONTSMOOTHINGCONTRAST: 1400,
                    fr.SPI_GETFONTSMOOTHINGORIENTATION: 1, fr.SPI_GETCLIENTAREAANIMATION: 0}
        self.keys = {fr.VK_NUMLOCK: 1}
        self.tz = "Eastern Standard Time"
        self.locale = {pr.LOCALE_SDECIMAL: ".", pr.LOCALE_STHOUSAND: ",", pr.LOCALE_SSHORTDATE: "M/d/yyyy"}
        self.wts = {pr.WTS_CLIENT_PROTOCOL_TYPE: ctypes.c_uint16(0), pr.WTS_CONNECT_STATE: ctypes.c_int32(0)}
        self.freed = []
        self.foreground = 0x55

    def EnumDisplaySettingsW(self, device, mode, dm):
        assert device is None and mode == fr.ENUM_CURRENT_SETTINGS and dm.dmSize == ctypes.sizeof(ffi.DEVMODEW)
        if self.dm is None:
            return 0
        for k, v in self.dm.items():
            setattr(dm, k, v)
        return 1

    def GetDpiForSystem(self):
        return self.dpi

    def GetSystemMetrics(self, i):
        return {fr.SM_CXSCREEN: 1920, fr.SM_CYSCREEN: 1080, fr.SM_CMONITORS: 1}[i]

    def GetThreadDpiAwarenessContext(self):
        return V2

    def AreDpiAwarenessContextsEqual(self, a, b):
        return a == V2 and ctypes.c_int64(b.value).value == -4

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        return -1

    def SystemParametersInfoW(self, action, ui, pv, flags):
        if action not in self.spi:
            return 0
        pv.contents.value = self.spi[action]
        return 1

    def GetKeyboardLayout(self, thread):
        return 0x04090409

    def GetKeyboardLayoutList(self, n, out):
        assert (n, out) == (0, None)
        return 1

    def GetKeyState(self, vk):
        return self.keys.get(vk, 0)

    def GetUserDefaultLocaleName(self, buf, n):
        buf.value = "en-US"
        return 6

    def GetDynamicTimeZoneInformation(self, tz):
        if self.tz is None:
            return fr.TIME_ZONE_ID_INVALID
        raw = self.tz.encode("utf-16-le")
        ctypes.memmove(tz.TimeZoneKeyName, raw, len(raw))
        return 1

    def GetLocaleInfoEx(self, name, lctype, buf, n):
        assert name is None
        buf.value = self.locale[lctype]
        return len(buf.value) + 1

    def WTSQuerySessionInformationW(self, server, session, info_class, buf, size):
        assert session == pr.WTS_CURRENT_SESSION
        value = self.wts.get(info_class)
        if value is None:
            return 0
        buf.value, size.value = ctypes.addressof(value), ctypes.sizeof(value)
        return 1

    def WTSFreeMemory(self, buf):
        self.freed.append(buf.value)

    def GetForegroundWindow(self):
        return self.foreground


class FakeDesktop:
    def __init__(self, desktop="Default", awareness="PER_MONITOR_AWARE_V2"):
        self.desktop, self.awareness = desktop, awareness

    def input_desktop(self):
        return self.desktop

    def process_dpi_awareness(self, pid=None):
        return self.awareness


RIG = {"hostname": "tos-rig-01", "hypervisor": "hyperv", "hypervisor_version": "10.0.26100",
       "snapshot_ids": ("snap-2026-10-28-base",), "vm_image_id": "tos-rig-win11-2026-10-28"}
SPEC_OS = dict(   # the 7.1 rig specification, as tests/test_fingerprint.py writes it
    windows_build={"current_build": "26100", "ubr": "2314"},
    display={"width": 1920, "height": 1080, "bits": 32, "frequency": 60, "scale_percent": 100, "dpi": 96,
             "awareness": "PER_MONITOR_AWARE_V2", "monitors": 1},
    cleartype={"smoothing": True, "type": "FE_FONTSMOOTHINGCLEARTYPE", "gamma": 1800, "contrast": 1400,
               "orientation": "RGB"},
    gpu={"name": "Hyper-V Video", "driver_version": "10.0.26100.1"},
    os_theme={"apps_light": False, "system_light": False, "transparency": False, "animations": False},
    keyboard={"layout": "0x0409", "layout_count": 1, "toggle_keys": {"caps": False, "num": True, "scroll": False},
              "locale": "en-US"},
    time_zone_os="Eastern Standard Time", input_desktop="Default", rig=RIG)


def probe(api=None, registry=None, desktop=None):
    return fr.OsProbe(desktop or FakeDesktop(), api or FakeApi(), registry or FakeRegistry())


@pytest.fixture
def install(tmp_path):
    d = tmp_path / "thinkorswim"
    (d / "jre").mkdir(parents=True)
    (d / "jre" / "release").write_text('IMPLEMENTOR="Eclipse Adoptium"\nJAVA_VERSION="17.0.12"\n')
    (d / "thinkorswim.vmoptions").write_text("-Xmx4g\n-Dsun.java2d.d3d=false\n-Dsun.java2d.noddraw=true\n")
    (d / "lib").mkdir()
    (d / "lib" / "a.jar").write_bytes(b"x" * 10)
    ws = tmp_path / "usergui" / "ws.sim.xml"
    ws.parent.mkdir()
    ws.write_text("<workspace/>")
    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "app_settings.xml").write_text("<s/>")
    (prof / "order_defaults.xml").write_text("<o/>")
    return fr.InstallPaths(str(d), str(d / "thinkorswim.vmoptions"), str(d / "jre" / "release"),
                           {"ws.sim": str(ws)}, (str(prof / "app_settings.xml"), str(prof / "order_defaults.xml")))


def sources(**kw):
    base = dict(title_build_id=lambda: "1998.3.0", java2d_confirmed=lambda: "software",
                app_theme=lambda: {"scheme": "dark", "font_size": "medium"},
                time_zone_app=lambda: {"app_display": "America/New_York", "chart": "America/New_York"},
                render_probe_hash=lambda: "e3b0c44298fc1c14")
    return fr.AppSources(**{**base, **kw})


def reader(install, api=None, registry=None, desktop=None, **kw):
    return fr.Win32FingerprintReader(probe(api, registry, desktop), install, sources(**kw), rig=lambda: RIG)


def test_it_is_a_fingerprint_reader_and_the_os_reads_are_the_rig_specification(install):
    r = reader(install)
    assert isinstance(r, FingerprintReader)
    assert vars(r.read_os()) == SPEC_OS


def test_the_reads_build_a_fingerprint_the_lint_accepts(install):
    r = reader(install)
    harness = F.harness_block(version="1.4.2", ocr_engine="tesseract-5.3.4", ocr_model="eng-best-4.1",
                              capture_lib="gdi", redaction_version="r3", region_registry_version="c41a0f2",
                              config_commit="c41a0f2", config_tarball_sha256=H, allowlist_sha256=H)
    fp = F.build_fingerprint(r.read_os(), r.read_app(), "run-1", "run-1/1", "candidate", "rig-01", harness,
                             data_epoch={"epoch_id": "sha256:" + "3" * 64, "data_mode": "backend_fixture"},
                             account={"account_id_sha256": H, "login_sha256": H, "role": "empty"},
                             read_at="2026-11-09T14:02:11-05:00")
    assert isinstance(fp, Fingerprint) and fp.match["java2d_pipeline"] == {"pinned": "software",
                                                                           "confirmed": "software"}


def test_the_app_reads_hash_the_install_folder(install):
    a = reader(install).read_app()
    vm = open(install.vmoptions, "rb").read()
    assert a.vmoptions_hash == "sha256:" + hashlib.sha256(vm).hexdigest()
    assert a.jre_release["java_version"] == "17.0.12" and a.jre_release["sha256"].startswith("sha256:")
    assert set(a.workspace_hash) == {"ws.sim"} and a.settings_profile_hash.startswith("sha256:")
    lines = "\n".join(sorted(["jre/release\t" + str(len(open(install.jre_release, "rb").read())), "lib/a.jar\t10",
                              f"thinkorswim.vmoptions\t{len(vm)}"]))
    assert a.jar_listing_hash == "sha256:" + hashlib.sha256(lines.encode()).hexdigest()
    assert (a.build_id_title, a.build_id_about, a.render_probe_hash) == ("1998.3.0", None, "e3b0c44298fc1c14")


@pytest.mark.parametrize("text, pipeline", [("-Dsun.java2d.d3d=false\n", "software"), ("-Xmx2g\n", "d3d"),
                                            ("-Dsun.java2d.opengl=true\n-Dsun.java2d.d3d=false\n", "opengl"),
                                            ("-Dsun.java2d.d3d=true\n", "d3d")])
def test_the_pinned_java2d_pipeline(text, pipeline):
    assert fr.java2d_pinned(text) == pipeline


def test_missing_files_are_unreadable_app_fields(install, tmp_path):
    broken = fr.InstallPaths(str(tmp_path / "gone"), str(tmp_path / "gone.vmoptions"), str(tmp_path / "release"),
                             {"ws.sim": str(tmp_path / "missing.xml")}, (str(tmp_path / "nope.xml"),))
    a = fr.Win32FingerprintReader(probe(), broken, sources(title_build_id=lambda: None,
                                                           render_probe_hash=lambda: 1 / 0)).read_app()
    assert (a.vmoptions_hash, a.jre_release, a.java2d_pipeline, a.workspace_hash, a.settings_profile_hash,
            a.jar_listing_hash, a.build_id_title, a.render_probe_hash) == (None,) * 8
    assert fr.Win32FingerprintReader(probe(), install, sources(java2d_confirmed=lambda: None)).read_app() \
        .java2d_pipeline["confirmed"] == "unknown"


def test_each_unreadable_os_read_is_none_and_the_others_stay(install):
    api, reg = FakeApi(), FakeRegistry()
    api.dm = None                                                     # display
    del api.spi[fr.SPI_GETFONTSMOOTHINGCONTRAST]                      # cleartype
    del reg.values[(fr.PERSONALIZE, "EnableTransparency")]             # os_theme (choice 3: absent is unreadable)
    reg.subs[fr.DISPLAY_CLASS] = ["Properties"]                       # gpu
    api.tz = None
    os_reads = reader(install, api, reg, FakeDesktop(desktop="")).read_os()
    assert (os_reads.display, os_reads.cleartype, os_reads.os_theme, os_reads.gpu, os_reads.time_zone_os,
            os_reads.input_desktop) == (None,) * 6
    assert os_reads.windows_build == SPEC_OS["windows_build"] and os_reads.keyboard == SPEC_OS["keyboard"]
    r = F.os_fields(os_reads, F.harness_block(version="1", ocr_engine="t", ocr_model="m", capture_lib="gdi",
                                              redaction_version="r", region_registry_version="c", config_commit="c",
                                              config_tarball_sha256=H, allowlist_sha256=H))
    assert r.unreached.reason == "fingerprint_unreadable:display"


def test_the_harness_awareness_name_reaches_the_display_block(install):
    api = FakeApi()
    api.GetThreadDpiAwarenessContext = lambda: 0x2002                  # neither V2 nor GDI-scaled; SYSTEM_AWARE
    api.GetAwarenessFromDpiAwarenessContext = lambda ctx: 1
    api.dpi = 120
    d = reader(install, api).read_os().display
    assert d["awareness"] == "SYSTEM_AWARE" and (d["dpi"], d["scale_percent"]) == (120, 125)


# -- preflight ------------------------------------------------------------------------------------------------------
def preflight(api=None, registry=None, desktop=None):
    api = api or FakeApi()
    return pr.Win32PreflightReads(probe(api, registry, desktop), api), api


def test_it_is_preflight_reads_and_reads_the_r_items():
    p, _ = preflight()
    assert isinstance(p, PreflightReads)
    assert p.read("R1").values == {"current_build": "26100", "ubr": "2314"}
    assert p.read("R2").values == {"width": 1920, "height": 1080, "bits": 32, "frequency": 60, "cx_screen": 1920,
                                   "cy_screen": 1080, "dpi": 96, "monitors": 1}
    assert p.read("R3").values == {"cleartype": SPEC_OS["cleartype"]}
    assert p.read("R4").values == {"time_zone": "Eastern Standard Time", "keyboard": SPEC_OS["keyboard"],
                                   "decimal": ".", "thousands": ",", "short_date": "M/d/yyyy"}
    assert p.read("R5").values == {"pending_restart": False, "screensaver_active": False}
    assert p.read("H3").values == {"dpi_awareness": "PER_MONITOR_AWARE_V2"}
    assert p.read("H10").values == {"wer_localdumps": True} and p.read("H10").readable


def test_r6_reads_the_console_session_and_frees_the_wts_buffers():
    p, api = preflight()
    r6 = p.read("R6")
    assert r6.readable and r6.values == {"input_desktop": "Default", "session_protocol": "console",
                                         "session_state": "active", "foreground_window": True}
    assert len(api.freed) == 2
    api.wts[pr.WTS_CLIENT_PROTOCOL_TYPE] = ctypes.c_uint16(2)
    api.wts[pr.WTS_CONNECT_STATE] = ctypes.c_int32(4)
    api.foreground = 0
    assert p.read("R6").values == {"input_desktop": "Default", "session_protocol": "rdp",
                                   "session_state": "disconnected", "foreground_window": False}


def test_a_pending_restart_and_the_dump_folder_are_read():
    reg = FakeRegistry()
    reg.keys.add(pr.REBOOT_KEYS[1])
    reg.values[(pr.WER_LOCALDUMPS, "DumpFolder")] = "D:\\dumps"
    p, _ = preflight(registry=reg)
    assert p.read("R5").values["pending_restart"] is True
    assert p.read("H10").values == {"wer_localdumps": True, "dump_folder": "D:\\dumps"}
    reg = FakeRegistry()
    reg.values[(pr.SESSION_MANAGER, "PendingFileRenameOperations")] = ["\\??\\C:\\x", ""]
    assert preflight(registry=reg)[0].read("R5").values["pending_restart"] is True


def test_unreadable_values_make_the_item_unreadable_and_keep_what_was_read():
    api, reg = FakeApi(), FakeRegistry()
    del api.wts[pr.WTS_CONNECT_STATE]
    del reg.values[(fr.DESKTOP_KEY, "ScreenSaveActive")]
    reg.keys.clear()
    p, _ = preflight(api, reg, FakeDesktop(awareness="unreadable"))
    r6 = p.read("R6")
    assert not r6.readable and r6.detail == "unreadable: session_state" and r6.values["session_protocol"] == "console"
    assert p.read("R5").detail == "unreadable: screensaver_active"
    assert not p.read("H3").readable
    h10 = p.read("H10")
    assert h10.readable and h10.values == {"wer_localdumps": False}                    # read: the key is absent


def test_items_read_elsewhere_never_pass_by_default():
    p, _ = preflight()
    for item in ("R7", "B3", "H1", "H9", "N3", "G5"):
        r = p.read(item)
        assert not r.readable and r.detail.startswith("not a desktop read")
    assert p.read("Z9").detail == "Z9 is not a 12.7 item"


def test_a_read_that_raises_is_unreadable_with_the_error():
    api = FakeApi()

    def boom(*a):
        raise ffi.Win32Error("GetLocaleInfoEx", 87)

    api.GetLocaleInfoEx = boom
    r = preflight(api)[0].read("R4")
    assert not r.readable and "GetLocaleInfoEx failed (error 87)" in r.detail


class FakeWinreg(types.ModuleType):
    """The `winreg` calls `WinRegistry` makes: keys under a denied root raise access denied (winerror 5), as HKLM
    does for a harness without the read right; `keys` exist, `values` are set; anything else is not found."""

    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER, KEY_READ, KEY_WOW64_64KEY = "HKLM", "HKCU", 0x20019, 0x100

    def __init__(self, denied=(), keys=(), values=None):
        super().__init__("winreg")
        self.denied, self.keys, self.values = set(denied), set(keys), dict(values or {})

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, sub, reserved, access):
        assert access == self.KEY_READ | self.KEY_WOW64_64KEY
        path = f"{root}\\{sub}"
        if root in self.denied:
            e = PermissionError(13, "Access is denied")
            e.winerror = 5
            raise e
        if path not in self.keys and not any(k == path for k, _ in self.values):
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.Key(path)

    def QueryValueEx(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.values[(key.path, name)], 1

    def QueryInfoKey(self, key):
        return (0, 0, 0)


def win_registry(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "winreg", fake)
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32")
        return fr.WinRegistry()


def test_the_registry_tells_an_absent_key_from_one_it_may_not_read(monkeypatch):
    reg = win_registry(monkeypatch, FakeWinreg(denied={"HKLM"}, values={(fr.DESKTOP_KEY, "ScreenSaveActive"): "0"}))
    assert reg.value(fr.DESKTOP_KEY, "ScreenSaveActive") == "0"
    assert reg.value(fr.DESKTOP_KEY, "Missing") is None and not reg.exists(fr.DESKTOP_KEY + "\\Missing")
    assert reg.subkeys(fr.DESKTOP_KEY + "\\Missing") == []
    for read in (lambda: reg.value(pr.SESSION_MANAGER, "PendingFileRenameOperations"),
                 lambda: reg.exists(pr.REBOOT_KEYS[0]), lambda: reg.subkeys(fr.DISPLAY_CLASS)):
        with pytest.raises(PermissionError):
            read()


def test_r5_with_the_reboot_keys_unreadable_is_unreadable_never_no_pending_restart(monkeypatch):
    fake = FakeWinreg(denied={"HKLM"}, values={(fr.DESKTOP_KEY, "ScreenSaveActive"): "0"})
    p, _ = preflight(registry=win_registry(monkeypatch, fake))
    r5 = p.read("R5")
    assert not r5.readable and "Access is denied" in r5.detail                 # 12.7: no item passes by default
    assert not p.read("H10").readable
    fake.denied.clear()                                                         # readable and absent: no restart
    assert p.read("R5").values == {"pending_restart": False, "screensaver_active": False}
    fake.keys.add(pr.REBOOT_KEYS[0])
    assert p.read("R5").values["pending_restart"] is True


def test_an_unreadable_registry_key_is_an_unreadable_fingerprint_field(monkeypatch):
    fake = FakeWinreg(denied={"HKLM"})
    p = probe(registry=win_registry(monkeypatch, fake))
    with pytest.raises(PermissionError):
        p.windows_build()
    assert fr._safe(p.windows_build) is None                                     # choice 7: None, never a default


@pytest.mark.skipif(sys.platform == "win32", reason="the real registry loads on Windows")
def test_without_a_registry_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        fr.WinRegistry()
    with pytest.raises(ffi.Win32Unavailable):
        fr.OsProbe(FakeDesktop())
