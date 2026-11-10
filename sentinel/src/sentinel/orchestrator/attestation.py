"""Run-start settings attestation, rows A3 to A11 (6.3, 12.6); A1 and A2 are identity.py (6.4).

Attestation reads and never repairs: there is no write path in this module and none on the `Rig`.
Each row A3 to A8 is read from OCR and template values; a row is unreadable when its page landmark
is below its floor, its label OCR confidence is below the floor pinned at calibration, or its state
matched neither template (value None). An unreadable row is read once more after Application
Settings is closed and reopened; a second unreadable read is `env_error (gate: settings_attested,
reason: attestation_unreadable)`, run `complete_with_env_errors`, never a pass and never a mismatch.

A3 to A8 run before A9, so A9 (the one row that touches a ticket) runs only with the confirmation
setting read On and no working orders. Outcomes:

- A3 to A7 mismatch: run `aborted_safety (reason: attestation_mismatch)`, S2; A8 (a working order):
  `aborted_safety (reason: unexpected_working_order)`, S1. Unreached checkpoints carry the reason with
  `gate: settings_attested` (1.10). Paged by the safety routing of 9.10.
- A9: the dialog appeared and Working is unchanged: pass. A new Working row: the kill switch, S1
  (`aborted_safety (reason: kill_switch)`). Dialog absent with no row: still `aborted_safety (reason:
  attestation_mismatch)`, because the confirmation path is unproven. A window whose title cannot be
  read counts as absent.
- A10 (RC runs): a mismatch aborts the Explorer stage only; unreadable Notifications make the Explorer
  stage `not_run (reason: entitlement)`; the RC run continues either way.
- A11: the content hashes of the A1 to A10 screenshots present on the run row, else `aborted_harness`.
- A non-safety profile row still wrong after the fixture reset flow's one re-apply (7.6 stage 1):
  `env_error (gate: settings_attested, reason: fixture_drift)` on every scoped checkpoint of the shard.

The Hot Keys rows read for A6 are returned as chords for the gateway's chord list (5.4.7).

Choices (reported): A11 names no reason and `aborted_harness` requires one; `artifact_persist_failed`
(the evidence was not persisted) is used. Floors default to 0.90 for templates (the ticket detector's
floor, 5.4.6) and 90 for OCR until Phase 0 pins them. When several rows fail, the order is: a safety
mismatch (A8 before A3 to A7, S1 before S2), then an unreadable row, then profile drift, since an unread
safety setting is the graver unknown. A Hot Keys binding whose text cannot be parsed as a chord makes A6
unreadable, because it cannot be loaded into the chord list.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import alerts
from .gateway import Chord
from .results import ControlResult, Outcome, RunVerdict, SubsetOutcome, harness_abort_outcome, safety_abort_outcome
from .rig import ConfirmationCheck, HotkeyRow, NotificationsRead, SettingRead

GATE = "settings_attested"
SAFETY_ROWS = ("A3", "A4", "A5", "A6", "A7", "A8")
ORDER_WORDS = re.compile(r"buy|sell|send|cancel|flatten|blast", re.I)


@dataclass(frozen=True)
class Floors:
    """Reader confidence floors (6.3 Unreadable path): page landmark template score and label OCR confidence."""

    landmark: float = 0.90
    ocr: float = 90.0


@dataclass(frozen=True)
class AttestationProfile:
    """The expected values: the checked-in settings profile (4.9) plus the corpus entry's Active Trader panels."""

    order_confirmation: bool = True                 # A3 On
    shift_click_send: bool | str = False            # A4 Off, or "absent" if Phase 0 finds no such option
    active_trader_hotkeys: bool = False             # A5 disabled
    non_order_actions: frozenset[str] = frozenset()  # A6 pinned list of non-order actions
    active_trader_panels: tuple[str, ...] = ()      # A7, from the corpus entry (4.6)
    sha256: str | None = None

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], *, panels: tuple[str, ...] = (),
                      non_order_actions: frozenset[str] = frozenset(), sha256: str | None = None) -> "AttestationProfile":
        """From the `settings` block of an `app_settings` profile (config/example/profiles)."""
        return cls(order_confirmation=bool(settings.get("order_confirmation", True)),
                   shift_click_send=settings.get("shift_click_send", False),  # type: ignore[arg-type]
                   active_trader_hotkeys=bool(settings.get("active_trader_hotkeys", False)),
                   non_order_actions=non_order_actions, active_trader_panels=panels, sha256=sha256)


@dataclass(frozen=True)
class RowReads:
    """One row's first read and, when the first was unreadable, its read after reopening Settings."""

    first: SettingRead
    second: SettingRead | None = None

    @property
    def last(self) -> SettingRead:
        return self.second or self.first


@dataclass(frozen=True)
class AttestationResult:
    """The attestation's `ControlResult` plus per-row states, the chords for the gateway and the Explorer stage."""

    result: ControlResult
    rows: dict[str, str] = field(default_factory=dict)       # item -> match | mismatch | unreadable | not_read
    chords: tuple[Chord, ...] = ()
    explorer_stage: str | None = None                        # allowed | aborted_safety | not_run (RC runs)


def _chords(rows: object) -> tuple[Chord, ...] | None:
    out = []
    for r in rows if isinstance(rows, tuple) else ():
        if r.binding:
            try:
                out.append(Chord.parse(r.binding))
            except ValueError:
                return None
    return tuple(out)


def readable(read: SettingRead, profile: AttestationProfile, floors: Floors = Floors()) -> bool:
    """6.3: landmark and OCR at their floors and a value of the row's type."""
    if read.landmark_score < floors.landmark or read.ocr_confidence < floors.ocr or read.value is None:
        return False
    v = read.value
    if read.item == "A6":
        return isinstance(v, tuple) and all(isinstance(r, HotkeyRow) for r in v) and _chords(v) is not None
    if read.item == "A7":
        return isinstance(v, Mapping) and all(isinstance(v.get(p), bool) for p in profile.active_trader_panels)
    if read.item == "A8":
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    if read.item == "A4":
        return isinstance(v, bool) or v == "absent"
    return isinstance(v, bool)


def matches(read: SettingRead, profile: AttestationProfile) -> bool:
    """The row's value equals the checklist's expectation (12.6 Expected)."""
    v = read.value
    if read.item == "A3":
        return v is profile.order_confirmation
    if read.item == "A4":
        return v == profile.shift_click_send
    if read.item == "A5":
        return v is profile.active_trader_hotkeys
    if read.item == "A6":
        return all(not r.binding or r.action in profile.non_order_actions for r in v if ORDER_WORDS.search(r.action))
    if read.item == "A7":
        return all(v[p] is False for p in profile.active_trader_panels)
    if read.item == "A8":
        return v == 0
    raise ValueError(f"not an attestation row read here: {read.item}")


def attest(reads: Mapping[str, RowReads], profile: AttestationProfile, *, a9: ConfirmationCheck | None,
           evidence: Mapping[str, str | None], a10: NotificationsRead | None = None, rc_run: bool = False,
           profile_drift: bool = False, previous_unreadable: bool = False,
           floors: Floors = Floors()) -> AttestationResult:
    """Decide the `settings_attested` gate from the reads of A3 to A8, A9's check, A10 (RC runs) and A11."""
    missing = [i for i in SAFETY_ROWS if i not in reads]
    if missing:
        raise ValueError(f"attestation needs every row A3 to A8; missing {missing}")
    rows: dict[str, str] = {}
    for item in SAFETY_ROWS:
        r = reads[item].last
        rows[item] = ("match" if matches(r, profile) else "mismatch") if readable(r, profile, floors) else "unreadable"
    shots = {i: reads[i].last.screenshot_sha256 for i in SAFETY_ROWS}
    chords = _chords(reads["A6"].last.value) or () if rows["A6"] != "unreadable" else ()
    mism = [i for i in SAFETY_ROWS if rows[i] == "mismatch"]
    if mism:
        reason = "unexpected_working_order" if "A8" in mism else "attestation_mismatch"
        run, un = safety_abort_outcome(GATE, reason, detail="rows " + ", ".join(mism))
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=run, unreached=un, alert=alerts.safety_abort(),
            actions=("kill_switch",), incident="S1" if "A8" in mism else "S2",
            detail="settings mismatch on " + ", ".join(mism), evidence={"screenshots": shots}), rows, chords)
    unread = [i for i in SAFETY_ROWS if rows[i] == "unreadable"]
    if unread:
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=RunVerdict("complete_with_env_errors"),
            unreached=Outcome("env_error", "attestation_unreadable", GATE, "rows " + ", ".join(unread)),
            alert=alerts.env_error(repeat=previous_unreadable),
            actions=("maintenance_task",) if previous_unreadable else (),
            detail="unreadable after one re-navigation: " + ", ".join(unread),
            evidence={"screenshots": {i: [reads[i].first.screenshot_sha256, reads[i].second.screenshot_sha256
                                          if reads[i].second else None] for i in unread}}), rows, chords)
    if profile_drift:
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=RunVerdict("complete_with_env_errors"),
            unreached=Outcome("env_error", "fixture_drift", GATE), alert=alerts.env_error(),
            detail="a non-safety profile row still wrong after the one re-apply (7.6 stage 1)"), rows, chords)
    if a9 is None:
        raise ValueError("A3 to A8 passed: A9 must run next")
    if a9.working_new_rows > 0 or not a9.dialog_appeared:
        rows["A9"] = "mismatch"
        new_row = a9.working_new_rows > 0
        run, un = safety_abort_outcome(GATE, "kill_switch" if new_row else "attestation_mismatch", detail="A9")
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=run, unreached=un, alert=alerts.safety_abort(),
            actions=("kill_switch",), incident="S1" if new_row else "S2",
            detail="A9: a new Working row after Confirm and Send" if new_row
            else "A9: the Order Confirmation dialog did not appear; the confirmation path is unproven",
            evidence={"screenshot": a9.screenshot_sha256}), rows, chords)
    rows["A9"] = "match"
    explorer_stage = None
    outcomes: tuple[SubsetOutcome, ...] = ()
    if rc_run:
        if a10 is None:
            raise ValueError("A10 runs on RC runs")
        if a10.verified_contact is None:
            explorer_stage, rows["A10"] = "not_run", "unreadable"
            outcomes = (SubsetOutcome("explorer_stage", Outcome("not_run", "entitlement")),)
        elif not a10.explorer_login_distinct or a10.verified_contact:
            explorer_stage, rows["A10"] = "aborted_safety", "mismatch"
        else:
            explorer_stage, rows["A10"] = "allowed", "match"
    need = ["A1", "A2", *SAFETY_ROWS, "A9"] + (["A10"] if rc_run else [])
    absent = [i for i in need if not evidence.get(i)]
    if absent:
        rows["A11"] = "mismatch"
        run, un = harness_abort_outcome("artifact_persist_failed", detail="A11: no evidence hash for " + ", ".join(absent))
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=run, unreached=un, alert=alerts.aborted_harness(),
            detail="A11: evidence hashes missing for " + ", ".join(absent)), rows, chords, explorer_stage)
    rows["A11"] = "match"
    alert = alerts.safety_abort().plus(paged=("explorer_owner",)) if explorer_stage == "aborted_safety" else None
    return AttestationResult(ControlResult(
        "attestation", True, GATE, outcomes=outcomes, alert=alert,
        detail="A10 mismatch: the Explorer stage is aborted, the RC run continues" if alert else None,
        evidence={"profile_sha256": profile.sha256, "hashes": dict(evidence)}), rows, chords, explorer_stage)
