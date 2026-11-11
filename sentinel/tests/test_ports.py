"""Tests for sentinel.orchestrator.ports: frozen observations, compatibility with the rig.py types, the SD-57
attestation stage and the structural Protocols."""
import dataclasses
import inspect

import pytest

from sentinel.orchestrator import ports as P
from sentinel.orchestrator import rig
from sentinel.orchestrator.gateway import Flag


def test_observations_are_frozen():
    for name, cls in inspect.getmembers(P, inspect.isclass):
        if dataclasses.is_dataclass(cls) and cls.__module__ == P.__name__:
            assert cls.__dataclass_params__.frozen, name


def test_extended_observations_are_still_the_rig_types():
    w = P.WindowInfo(1, 42, "SunAwtFrame", "Main@thinkorswim", rect=(0, 0, 1920, 1040), z_order=0,
                     process_name="thinkorswim.exe")
    assert isinstance(w, rig.WindowInfo) and w.visible and w.enabled
    a = P.AccountState("D-1", "golden", {"SPY": 100.0}, 0, 5000.0, buying_power=4000.0, alert_count=0,
                       watchlists={"core": ("SPY", "QQQ")},
                       tagged_working=(rig.OrderRow("W-1", "working", tag="run-1"),),
                       delay_badge=P.DelayBadge("Delayed 20 min", True, 20))
    assert isinstance(a, rig.AccountState) and a.positions["SPY"] == 100.0
    login = P.LoginObservation(agreement_pending=True, ondemand_toolbar=False)
    assert isinstance(login, rig.LoginObservation) and login.vault_lease


def test_session_safety_is_a_stage():
    s = P.SessionSafety()
    assert not s.attested_through("a3_a8")
    s = P.SessionSafety("a3_a8", last_assertion_mono=12.5)
    assert s.attested_through("a3_a8") and not s.attested_through("a3_a9")     # A9 click yes, later sends no
    assert P.SessionSafety("a3_a9").attested_through("a3_a8")
    with pytest.raises(ValueError, match="SD-57"):
        P.SessionSafety("attested")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.attestation_stage = "a3_a9"


class FakeDesktop:
    def enumerate_windows(self):
        return (P.WindowInfo(1, 2, "SunAwtFrame", "Main"),)

    def grab(self):
        return P.Frame(image=None, origin=(0, 0), mono_s=0.0)

    def window_from_point(self, points):
        return tuple(P.PointOwner(x, y, 1, 2) for x, y in points)

    def virtual_screen(self):
        return (0, 0, 1920, 1080)

    def process_dpi_awareness(self, pid):
        return "PER_MONITOR_AWARE_V2"

    def input_desktop(self):
        return "Default"


class FakePlanner:
    def plan(self, step, points, flag, now):
        return ()


def test_protocols_are_structural():
    assert isinstance(FakeDesktop(), P.DesktopPort)
    assert not isinstance(FakeDesktop(), P.ProcessPort)
    assert isinstance(FakePlanner(), P.PrimitivePlanner)
    assert FakePlanner().plan({}, {}, Flag.CLEAR, 0.0) == ()


def test_every_port_is_a_runtime_protocol_with_documented_methods():
    names = ("DesktopPort", "InputPort", "SegmentPort", "AccessPort", "ProcessPort", "ClipboardPort", "VaultPort",
             "KillChannel", "FingerprintReader", "PreflightReads", "Hypervisor", "TrafficShaper", "OrderBookRead",
             "ConnectivityRead", "DelayBadgeRead", "Locator", "PrimitivePlanner")
    for n in names:
        proto = getattr(P, n)
        assert getattr(proto, "_is_runtime_protocol", False), n
        methods = [m for m, f in vars(proto).items() if inspect.isfunction(f) and not m.startswith("_")]
        assert methods and all(vars(proto)[m].__doc__ for m in methods), n


def test_ports_import_no_windows_module():
    src = inspect.getsource(P)
    for mod in ("ctypes", "win32", "pywinauto", "winreg", "comtypes"):
        assert f"import {mod}" not in src and f"from {mod}" not in src
