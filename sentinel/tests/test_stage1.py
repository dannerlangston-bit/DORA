"""Fixture reset flow Stage 1, the profile (7.6 stage 1, 7.5, 6.3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sentinel.orchestrator.attestation import SAFETY_ROWS, AttestationProfile, RowReads, attest
from sentinel.orchestrator.rig import ConfirmationCheck, HotkeyRow, SettingRead
from sentinel.orchestrator.stage1 import Stage1Profile, run_stage1

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
FIXTURE = yaml.safe_load((EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml").read_text())
SETTINGS = yaml.safe_load((EXAMPLE / "profiles" / "profile.app-settings.sentinel-pinned.yaml").read_text())
DEFAULTS = yaml.safe_load((EXAMPLE / "profiles" / "profile.order-defaults.fresh-install.yaml").read_text())
ATT = AttestationProfile.from_settings(SETTINGS["settings"], panels=("at-main",))
PROFILE = Stage1Profile.from_docs(SETTINGS, DEFAULTS, attestation=ATT, studies=("SentinelVWAP",))
CORE = ("AAPL", "SPX", "/ES")
SAFE = {"A3": True, "A4": False, "A5": False, "A6": (HotkeyRow("Buy Market", None),), "A7": {"at-main": False}}


@dataclass
class FakePort:
    """A rig whose profile state the Stage 1 driver reads and changes; `sticky` controls ignore one correction."""

    safety: dict[str, Any] = field(default_factory=lambda: dict(SAFE))
    pages: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "application_settings": {"display_time_zone": "America/New_York"},
        "order_defaults.equity": {"side": "BUY", "quantity": 100, "order_type": "LMT", "tif": "DAY"},
        "order_defaults.future": {"side": "BUY", "quantity": 1, "order_type": "LMT", "tif": "DAY"}})
    lists: dict[str, tuple[str, ...]] = field(default_factory=lambda: {"SENTINEL-CORE": CORE})
    alerts: int = 0
    saved: int = 0
    notes: int = 0
    study_names: tuple[str, ...] = ("SentinelVWAP",)
    scan_names: tuple[str, ...] = ()
    at_qty: int | None = 1
    checkpoints: dict[str, bool] = field(default_factory=lambda: {"setup": True, "watchlist_picker": True})
    sticky: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def read_setting(self, item: str) -> SettingRead:
        return SettingRead(item, self.safety[item], screenshot_sha256=f"{item}-shot")

    def read_page(self, page: str) -> dict[str, Any]:
        return dict(self.pages[page])

    def apply_setting(self, page: str, control: str, value: Any) -> None:
        self.calls.append(f"apply {page}.{control}")
        if f"{page}.{control}" not in self.sticky:
            self.pages[page][control] = value

    def watchlists(self) -> dict[str, tuple[str, ...]]:
        return dict(self.lists)

    def delete_watchlist(self, name: str) -> None:
        self.calls.append(f"delete watchlist {name}")
        self.lists.pop(name)

    def import_watchlist(self, name: str, symbols: tuple[str, ...]) -> None:
        self.calls.append(f"import watchlist {name}")
        self.lists[name] = symbols

    def alert_count(self) -> int:
        return self.alerts

    def delete_alerts(self) -> None:
        self.calls.append("delete alerts")
        self.alerts = 0 if "alerts" not in self.sticky else self.alerts

    def saved_order_count(self) -> int:
        return self.saved

    def delete_saved_orders(self) -> None:
        self.calls.append("delete saved orders")
        self.saved = 0

    def note_count(self, symbols: tuple[str, ...]) -> int:
        return self.notes

    def delete_notes(self, symbols: tuple[str, ...]) -> None:
        self.calls.append("delete notes")
        self.notes = 0

    def studies(self) -> tuple[str, ...]:
        return self.study_names

    def import_studies(self, names: tuple[str, ...]) -> None:
        self.calls.append("import studies")
        self.study_names = names

    def scans(self) -> tuple[str, ...]:
        return self.scan_names

    def import_scans(self, names: tuple[str, ...]) -> None:
        self.calls.append("import scans")
        self.scan_names = names

    def clear_drawings(self, symbols: tuple[str, ...]) -> None:
        self.calls.append("clear drawings")

    def apply_chart_settings(self) -> None:
        self.calls.append("apply chart settings")

    def active_trader_quantity(self, panel: str) -> int | None:
        return self.at_qty

    def profile_checkpoints(self) -> dict[str, bool]:
        return dict(self.checkpoints)

    @property
    def changes(self) -> list[str]:
        return [c for c in self.calls if not c.startswith(("clear drawings", "apply chart settings"))]


def test_clean_profile_changes_nothing_and_has_no_drift():
    port = FakePort()
    r = run_stage1(port, FIXTURE, PROFILE)
    assert r.result is None and not r.profile_drift and r.corrections == 0 and port.changes == []
    assert r.steps[0] == "step 1: safety rows A3 to A7 read, no mismatch" and len(r.steps) >= 6


def test_a_safety_row_mismatch_aborts_before_any_correction():
    # 7.6 stage 1 step 1: aborted_safety with the screenshot, before anything is corrected.
    port = FakePort(safety={**SAFE, "A4": True})
    port.pages["application_settings"]["display_time_zone"] = "UTC"
    r = run_stage1(port, FIXTURE, PROFILE)
    assert (r.result.run_status, r.result.run_reason) == ("aborted_safety", "attestation_mismatch")
    assert r.result.unreached.gate == "settings_attested" and r.result.incident == "S2"
    assert r.result.evidence["screenshots"]["A4"] == "A4-shot" and port.calls == []


def test_an_unreadable_safety_row_is_left_to_the_attestation():
    port = FakePort(safety={**SAFE, "A5": None})
    assert run_stage1(port, FIXTURE, PROFILE).result is None


def test_order_defaults_quantity_above_the_fixture_size_aborts():
    # 7.5 Order Defaults row: a default quantity above the fixture size is a safety row.
    port = FakePort()
    port.pages["order_defaults.future"]["quantity"] = 500
    port.pages["application_settings"]["display_time_zone"] = "UTC"
    r = run_stage1(port, FIXTURE, PROFILE)
    assert r.result.run_reason == "attestation_mismatch" and "quantity 500 above the fixture size 1" in r.result.detail
    assert port.calls == []


def test_one_correction_pass_then_drift():
    # 7.6 stage 1 step 2: one correction pass; still wrong is fixture_drift for the shard.
    port = FakePort()
    port.pages["application_settings"]["display_time_zone"] = "UTC"
    port.pages["order_defaults.equity"]["tif"] = "GTC"
    fixed = run_stage1(port, FIXTURE, PROFILE)
    assert not fixed.profile_drift and fixed.corrections == 2
    sticky = FakePort(sticky={"application_settings.display_time_zone"})
    sticky.pages["application_settings"]["display_time_zone"] = "UTC"
    r = run_stage1(sticky, FIXTURE, PROFILE)
    assert r.profile_drift and r.drift == ("application_settings.display_time_zone still wrong after one correction",)
    assert sticky.calls.count("apply application_settings.display_time_zone") == 1
    low = FakePort()
    low.pages["order_defaults.equity"]["quantity"] = 10                     # below the size: corrected, not aborted
    assert run_stage1(low, FIXTURE, PROFILE).corrections == 1


def test_watchlists_are_restored_to_the_fixture():
    # 7.6 stage 1 step 3: delete non-fixture lists, import missing ones.
    port = FakePort(lists={"MY-LIST": ("TSLA",), "SENTINEL-CORE": ("AAPL",)})
    r = run_stage1(port, FIXTURE, PROFILE)
    assert "delete watchlist MY-LIST" in port.calls and "import watchlist SENTINEL-CORE" in port.calls
    assert port.lists == {"SENTINEL-CORE": CORE} and not r.profile_drift
    missing = FakePort(lists={})
    run_stage1(missing, FIXTURE, PROFILE)
    assert missing.lists == {"SENTINEL-CORE": CORE}


def test_deletes_imports_and_what_stays_wrong():
    port = FakePort(alerts=2, saved=1, notes=3, study_names=(), scan_names=("Old",))
    r = run_stage1(port, FIXTURE, PROFILE)
    assert {"delete alerts", "delete saved orders", "delete notes", "import studies", "import scans"} <= set(port.calls)
    assert not r.profile_drift and port.study_names == ("SentinelVWAP",) and port.scan_names == ()
    stuck = FakePort(alerts=1, sticky={"alerts"}, at_qty=5, checkpoints={"setup": True, "alert_book": False})
    r = run_stage1(stuck, FIXTURE, PROFILE)
    assert r.profile_drift and set(r.drift) == {"1 alerts left after the delete",
                                                 "Active Trader at-main default quantity 5, expected 1",
                                                 "profile checkpoint alert_book mismatch"}


def test_profile_drift_feeds_the_attestation():
    # 5.2 settings_attested, SD-41: drift after the one re-apply is env_error (fixture_drift) for the shard.
    port = FakePort(lists={"MY-LIST": ("TSLA",)}, sticky=set())
    port.delete_watchlist = lambda name: None                           # the delete does not take
    r = run_stage1(port, FIXTURE, PROFILE)
    assert r.profile_drift
    reads = {i: RowReads(SettingRead(i, SAFE.get(i, 0))) for i in SAFETY_ROWS}
    res = attest(reads, ATT, a9=ConfirmationCheck(True), evidence={}, profile_drift=r.profile_drift).result
    assert res.unreached.reason == "fixture_drift" and res.unreached.gate == "settings_attested"
