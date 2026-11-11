"""Fixture reset flow Stage 1, the profile: after `paper_mode_asserted`, before the attestation that closes
`settings_attested` (7.6 stage 1, 7.5, 6.3).

`run_stage1(port, fixture, profile)` sequences the steps over a `ProfileResetPort` (the write path the attestation
never has: the Windows rig implements it with scripted clicks, `SimulatedRig` grows a fake) and decides:

1. The safety rows A3 to A7 are read first; any mismatch is `aborted_safety (reason: attestation_mismatch)` at
   `settings_attested`, S2, before anything is corrected (something changed a safety setting since the last run).
2. Application Settings and each Order Defaults asset-class page are read against the checked-in profiles. A
   default quantity above the fixture size (the profile's quantity for that asset class) is a safety row (7.5):
   the same abort, still before any correction, and on the page's re-read after its correction too (choice 6).
   Every other differing control is corrected once and the page read again; still wrong is drift.
3. Non-fixture watchlists are deleted; missing fixture watchlists, and fixture lists whose symbols differ, are
   imported from the checked-in files; the lists read again must equal the fixture.
4. to 6. Alerts, saved orders and order templates, and notes on fixture symbols are deleted and counted again
   (zero); custom studies and scans are re-imported when the lists differ and read again.
7. Drawings on fixture symbols are cleared and the checked-in chart settings re-applied.
8. The profile checkpoint set at tight thresholds (Setup dialogs, watchlist picker, alert book, studies list, saved
   scans, the chart) must match.

Anything still wrong makes `Stage1Result.profile_drift` True, which `attestation.attest(profile_drift=...)` turns into
`env_error (gate: settings_attested, reason: fixture_drift)` for the shard (7.5, SD-41: watchlist drift is whole-shard
at `settings_attested`). `Stage1Result.steps` are the run health lines of the `fixture_reset` flow (7.6).

Choices (reported):

1. An unreadable safety row is not a mismatch: Stage 1 leaves it to the attestation's two-read path (6.3) and goes
   on, since its corrections never touch a safety row.
2. Application Settings is one page of the non-safety controls of the settings profile (every key but
   `order_confirmation`, `shift_click_send`, `active_trader_hotkeys` and `order_hotkeys`); Order Defaults is one page
   per asset class of the order-defaults profile (`order_defaults.<asset>`). A control the page read does not return
   counts as differing.
3. The Active Trader per-panel default quantity (7.5) is read here for every panel the corpus entry lists; it lives
   in the workspace file and is restored by the snapshot revert, never corrected, so a wrong one is drift at once.
4. The fixture names no custom studies or scans (4.9 has no field), so the expected lists come from the checked-in
   exports the caller passes (`Stage1Profile.studies`, `.scans`); compared as sets.
5. `watchlists()` returns the login's own lists (name -> symbols by Copy All), not the app's public lists.
6. The Order Defaults quantity is the one control 7.5 makes a safety row, and the attestation never reads it, so
   choice 1 cannot cover it. Its read is a number (an int or float, or OCR text that reads as one); an unreadable
   first read is read once more before anything is corrected, as the safety rows' two reads (6.3). A quantity above
   the fixture size on any read (the first two, or the re-read after the page's correction) aborts. A quantity still
   unreadable is never set by a scripted click, since that would hide a changed value: it is drift ("unreadable on
   two reads"), `env_error (gate: settings_attested, reason: fixture_drift)` for the shard.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import alerts
from .attestation import GATE, AttestationProfile, Floors, matches, readable
from .results import ControlResult, safety_abort_outcome
from .rig import SettingRead

STAGE1_SAFETY_ROWS = ("A3", "A4", "A5", "A6", "A7")
APP_SETTINGS_PAGE = "application_settings"
ORDER_DEFAULTS_PAGE = "order_defaults."
SAFETY_KEYS = frozenset({"order_confirmation", "shift_click_send", "active_trader_hotkeys", "order_hotkeys"})


class ProfileResetPort(Protocol):
    """The rig's write path for Stage 1 (7.6): reads by OCR and Copy All, changes by scripted clicks. Only Stage 1 holds
    it; the attestation reads through `Rig.read_setting` and never writes (6.3)."""

    def read_setting(self, item: str) -> SettingRead:
        """Read one safety row A3 to A7 as the attestation does (6.3)."""

    def read_page(self, page: str) -> Mapping[str, Any]:
        """The controls of one settings page by OCR: `application_settings` or `order_defaults.<asset>`."""

    def apply_setting(self, page: str, control: str, value: Any) -> None:
        """Set one non-safety control with scripted clicks."""

    def watchlists(self) -> Mapping[str, tuple[str, ...]]:
        """The login's own watchlists: name -> symbols (picker OCR, Copy All of each list)."""

    def delete_watchlist(self, name: str) -> None:
        """Delete one watchlist."""

    def import_watchlist(self, name: str, symbols: tuple[str, ...]) -> None:
        """Import one watchlist from its checked-in file, replacing a list of that name."""

    def alert_count(self) -> int:
        """Rows in the alert book."""

    def delete_alerts(self) -> None:
        """Delete every alert."""

    def saved_order_count(self) -> int:
        """Saved orders and order templates."""

    def delete_saved_orders(self) -> None:
        """Delete every saved order and order template."""

    def note_count(self, symbols: tuple[str, ...]) -> int:
        """Notes on the given symbols."""

    def delete_notes(self, symbols: tuple[str, ...]) -> None:
        """Delete the notes on the given symbols."""

    def studies(self) -> tuple[str, ...]:
        """The custom study names."""

    def import_studies(self, names: tuple[str, ...]) -> None:
        """Re-import the checked-in study exports."""

    def scans(self) -> tuple[str, ...]:
        """The saved scan names."""

    def import_scans(self, names: tuple[str, ...]) -> None:
        """Re-import the checked-in scans."""

    def clear_drawings(self, symbols: tuple[str, ...]) -> None:
        """Clear chart drawings on the given symbols."""

    def apply_chart_settings(self) -> None:
        """Re-apply the checked-in chart settings."""

    def active_trader_quantity(self, panel: str) -> int | None:
        """One Active Trader panel's default quantity by OCR of its header; None when unreadable."""

    def profile_checkpoints(self) -> Mapping[str, bool]:
        """The profile checkpoint set at tight thresholds: checkpoint name -> matched (7.6 step 8)."""


@dataclass(frozen=True)
class Stage1Profile:
    """The checked-in profiles Stage 1 restores (4.9): the attestation's expectations for the safety rows, the
    non-safety Application Settings controls, Order Defaults per asset class, the Active Trader default quantity, and
    the study and scan exports (choice 4)."""

    attestation: AttestationProfile
    app_settings: Mapping[str, Any]
    order_defaults: Mapping[str, Mapping[str, Any]]
    active_trader_quantity: int | None = None
    studies: tuple[str, ...] = ()
    scans: tuple[str, ...] = ()

    @classmethod
    def from_docs(cls, settings_profile: Mapping[str, Any], order_defaults_profile: Mapping[str, Any], *,
                  attestation: AttestationProfile, studies: tuple[str, ...] = (),
                  scans: tuple[str, ...] = ()) -> "Stage1Profile":
        """From the `app_settings` and `order_defaults` profile documents (config `profiles/`, choice 2)."""
        settings = settings_profile.get("settings") or {}
        defaults = order_defaults_profile.get("defaults") or {}
        atq = defaults.get("active_trader_quantity")
        return cls(attestation, {k: v for k, v in settings.items() if k not in SAFETY_KEYS},
                   {k: dict(v) for k, v in defaults.items() if isinstance(v, Mapping)},
                   int(atq) if atq is not None else None, tuple(studies), tuple(scans))


@dataclass(frozen=True)
class Stage1Result:
    """What Stage 1 decided: the safety abort (None when there was none), whether a profile row is still wrong after
    the one correction pass (`profile_drift`, for `attest`), what stayed wrong, the run health lines and the number of
    corrections made."""

    result: ControlResult | None
    profile_drift: bool
    drift: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    corrections: int = 0


def _abort(detail: str, evidence: dict[str, Any]) -> ControlResult:
    run, un = safety_abort_outcome(GATE, "attestation_mismatch", detail=detail)
    return ControlResult("fixture_reset_profile", False, GATE, run=run, unreached=un, alert=alerts.safety_abort(),
                         actions=("kill_switch",), incident="S2", detail=detail, evidence=evidence)


def _differs(read: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in expected.items() if k not in read or read[k] != v}


def _quantity(read: Mapping[str, Any]) -> float | None:
    """An Order Defaults page's default quantity as a number: an int or float, or OCR text that reads as a finite
    number (choice 6); None when unreadable."""
    q = read.get("quantity")
    if isinstance(q, str):
        try:
            q = float(q.replace(",", "").strip())
        except ValueError:
            return None
    if isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(q):
        return None
    return float(q)


def _guarded(page: str, expected: Mapping[str, Any]) -> bool:
    """The page's default quantity is the 7.5 safety row: an Order Defaults page whose profile sets a quantity."""
    return page.startswith(ORDER_DEFAULTS_PAGE) and expected.get("quantity") is not None


def _above(page: str, read: Mapping[str, Any], expected: Mapping[str, Any]) -> str | None:
    """A default quantity above the fixture size (7.5: a safety row), as the abort detail; None otherwise."""
    q = _quantity(read)
    if _guarded(page, expected) and q is not None and q > float(expected["quantity"]):
        return f"{page} quantity {read.get('quantity')} above the fixture size {expected['quantity']}"
    return None


def _unread(page: str, read: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return _guarded(page, expected) and _quantity(read) is None


@dataclass
class _Run:
    port: ProfileResetPort
    steps: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    corrections: int = 0

    def clear(self, what: str, count: Callable[[], int], delete: Callable[[], None]) -> None:
        """Delete when present, count again, zero or drift (steps 4 and 5)."""
        if count():
            delete()
        left = count()
        if left:
            self.drift.append(f"{left} {what} left after the delete")
        self.steps.append(f"{what}: {'deleted' if not left else f'{left} left'}")


def run_stage1(port: ProfileResetPort, fixture: Mapping[str, Any], profile: Stage1Profile, *,
               floors: Floors = Floors()) -> Stage1Result:
    """Stage 1 of the fixture reset flow in the order of 7.6 (module docstring); scripted only, no agent fallback."""
    run = _Run(port)
    reads = {item: port.read_setting(item) for item in STAGE1_SAFETY_ROWS}
    mism = [i for i, r in reads.items() if readable(r, profile.attestation, floors)
            and not matches(r, profile.attestation)]
    shots = {i: r.screenshot_sha256 for i, r in reads.items()}
    if mism:
        return Stage1Result(_abort("rows " + ", ".join(mism) + " changed since the last run: an incident, not drift",
                                   {"screenshots": shots}), False, steps=("step 1: safety rows mismatch",))
    run.steps.append("step 1: safety rows A3 to A7 read, no mismatch")

    pages = {APP_SETTINGS_PAGE: dict(profile.app_settings)} | {
        f"{ORDER_DEFAULTS_PAGE}{asset}": dict(v) for asset, v in profile.order_defaults.items()}
    first = {page: dict(port.read_page(page)) for page in pages}
    for page, exp in pages.items():
        if _unread(page, first[page], exp):                     # choice 6: read once more, before any correction
            first[page] = dict(port.read_page(page))
    above = [a for page, exp in pages.items() if (a := _above(page, first[page], exp))]
    if above:
        return Stage1Result(_abort("Order Defaults: " + "; ".join(above), {"pages": first, "screenshots": shots}),
                            False, steps=(run.steps[0], "step 2: an Order Defaults quantity above the fixture size"))
    for page, expected in pages.items():
        blind = {"quantity"} if _unread(page, first[page], expected) else set()
        if blind:
            run.drift.append(f"{page}.quantity unreadable on two reads, never corrected blind")
        wrong = {c: v for c, v in _differs(first[page], expected).items() if c not in blind}
        if not wrong:
            continue
        for control, value in wrong.items():
            port.apply_setting(page, control, value)
            run.corrections += 1
        again = dict(port.read_page(page))
        late = _above(page, again, expected)
        if late:
            return Stage1Result(_abort("Order Defaults: " + late, {"pages": {**first, page: again},
                                                                   "screenshots": shots}), False,
                                steps=(*run.steps, "step 2: an Order Defaults quantity above the fixture size after "
                                                   "the correction"), corrections=run.corrections)
        run.drift += [f"{page}.{c} still wrong after one correction" for c in _differs(again, expected)
                      if c not in blind]
    run.steps.append(f"step 2: settings pages, {run.corrections} correction(s)")

    symbols = {s["id"]: str(s["symbol"]) for s in fixture.get("symbols") or ()}
    want = {str(w["name"]): tuple(symbols[r] for r in w.get("symbols") or ()) for w in fixture.get("watchlists") or ()}
    have = dict(port.watchlists())
    for name in sorted(set(have) - set(want)):
        port.delete_watchlist(name)
    for name, syms in want.items():
        if name not in have or tuple(have[name]) != syms:
            port.import_watchlist(name, syms)
    after = {k: tuple(v) for k, v in port.watchlists().items()}
    if after != want:
        run.drift.append(f"watchlists {after} != fixture {want}")
    run.steps.append(f"step 3: watchlists, {len(set(have) - set(want))} deleted")

    fixture_symbols = tuple(symbols.values())
    run.clear("alerts", port.alert_count, port.delete_alerts)
    run.clear("saved orders and templates", port.saved_order_count, port.delete_saved_orders)
    run.clear("notes on fixture symbols", lambda: port.note_count(fixture_symbols),
              lambda: port.delete_notes(fixture_symbols))

    for what, listed, imp, expected in (("studies", port.studies, port.import_studies, profile.studies),
                                        ("scans", port.scans, port.import_scans, profile.scans)):
        if set(listed()) != set(expected):
            imp(tuple(expected))
            if set(listed()) != set(expected):
                run.drift.append(f"{what} differ from the checked-in exports after the re-import")
            run.steps.append(f"step 6: {what} re-imported")

    port.clear_drawings(fixture_symbols)
    port.apply_chart_settings()
    run.steps.append("step 7: drawings cleared, chart settings re-applied")

    for panel in profile.attestation.active_trader_panels:
        q = port.active_trader_quantity(panel)
        if profile.active_trader_quantity is not None and q != profile.active_trader_quantity:
            run.drift.append(f"Active Trader {panel} default quantity {q}, expected {profile.active_trader_quantity}")
    missed = sorted(name for name, ok in port.profile_checkpoints().items() if not ok)
    run.drift += [f"profile checkpoint {name} mismatch" for name in missed]
    run.steps.append(f"step 8: profile checkpoints, {len(missed)} mismatch")
    return Stage1Result(None, bool(run.drift), tuple(run.drift), tuple(run.steps), run.corrections)
