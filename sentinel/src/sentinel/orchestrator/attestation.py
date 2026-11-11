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
- A3 "absent" (6.13 item 3, branch B: Phase 0 found no confirmation option): the profile's A3 reads
  "absent", A3 must read "absent", and A9 is the sole attestation of the dialog, run twice at
  `settings_attested`: both checks must pass, the second runs only after the first passed, and its
  screenshot hash joins A11 as `A9_second`.
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
unreadable, because it cannot be loaded into the chord list. A7 is a mismatch when any panel in the read,
named by the profile or not, shows Auto Send anything but Off. An "absent" A3 or A4 is compared like A4 already
was: a profile that expects "absent" and reads a toggle is a mismatch, and so is a profile that expects a toggle
and reads "absent", since either way the pinned settings page is not the one attested.
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
ABSENT = "absent"                                    # 6.13 items 2 and 3: the option is on no settings page


@dataclass(frozen=True)
class Floors:
    """Reader confidence floors (6.3 Unreadable path): page landmark template score and label OCR confidence."""

    landmark: float = 0.90
    ocr: float = 90.0


@dataclass(frozen=True)
class AttestationProfile:
    """The expected values: the checked-in settings profile (4.9) plus the corpus entry's Active Trader panels."""

    order_confirmation: bool | str = True           # A3 On, or "absent" if Phase 0 finds no such option (6.13 item 3)
    shift_click_send: bool | str = False            # A4 Off, or "absent" if Phase 0 finds no such option
    active_trader_hotkeys: bool = False             # A5 disabled
    non_order_actions: frozenset[str] = frozenset()  # A6 pinned list of non-order actions
    active_trader_panels: tuple[str, ...] = ()      # A7, from the corpus entry (4.6)
    sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("order_confirmation", "shift_click_send"):
            v = getattr(self, name)
            if not isinstance(v, bool) and v != ABSENT:
                raise ValueError(f"{name} is a bool or {ABSENT!r} (6.13), got {v!r}")

    @property
    def a9_runs(self) -> int:
        """How many A9 checks `settings_attested` needs: two when A3 is "absent" (6.13 item 3), else one."""
        return 2 if self.order_confirmation == ABSENT else 1

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], *, panels: tuple[str, ...] = (),
                      non_order_actions: frozenset[str] = frozenset(), sha256: str | None = None) -> "AttestationProfile":
        """From the `settings` block of an `app_settings` profile (config/example/profiles); A3 and A4 keep an
        "absent" value as it is."""
        def toggle(key: str, default: bool) -> bool | str:
            v = settings.get(key, default)
            return ABSENT if v == ABSENT else bool(v)
        return cls(order_confirmation=toggle("order_confirmation", True),
                   shift_click_send=toggle("shift_click_send", False),
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
    if read.item in ("A3", "A4"):
        return isinstance(v, bool) or v == ABSENT
    return isinstance(v, bool)


def _same_toggle(v: object, expected: bool | str) -> bool:
    """A toggle or "absent" read against its expectation: a bool by identity, "absent" by equality."""
    return v is expected if isinstance(v, bool) else v == expected == ABSENT


def matches(read: SettingRead, profile: AttestationProfile) -> bool:
    """The row's value equals the checklist's expectation (12.6 Expected)."""
    v = read.value
    if read.item == "A3":
        return _same_toggle(v, profile.order_confirmation)
    if read.item == "A4":
        return _same_toggle(v, profile.shift_click_send)
    if read.item == "A5":
        return v is profile.active_trader_hotkeys
    if read.item == "A6":
        return all(not r.binding or r.action in profile.non_order_actions for r in v if ORDER_WORDS.search(r.action))
    if read.item == "A7":
        # "Off on all" (6.3): the profile's panels, and any other Active Trader panel the read shows
        return all(v[p] is False for p in profile.active_trader_panels) and all(x is False for x in v.values())
    if read.item == "A8":
        return v == 0
    raise ValueError(f"not an attestation row read here: {read.item}")


def a9_passed(check: ConfirmationCheck) -> bool:
    """A9's own verdict: the dialog appeared and Working is unchanged (6.3)."""
    return check.dialog_appeared and check.working_new_rows == 0


def _with_rows(extra: Mapping[str, object], rows: Mapping[str, str], evidence: Mapping[str, str | None]) -> dict:
    """A result's evidence with the A rows decided so far and the evidence hashes present (`report.safety` reads the
    states and hashes from them, never from the detail text, 6.3, 6.6)."""
    return {**extra, "rows": dict(rows), "hashes": {k: v for k, v in evidence.items() if v}}


def attest(reads: Mapping[str, RowReads], profile: AttestationProfile, *, a9: ConfirmationCheck | None,
           evidence: Mapping[str, str | None], a10: NotificationsRead | None = None, rc_run: bool = False,
           profile_drift: bool = False, previous_unreadable: bool = False,
           floors: Floors = Floors(), a9_second: ConfirmationCheck | None = None) -> AttestationResult:
    """Decide the `settings_attested` gate from the reads of A3 to A8, A9's check (twice when A3 is "absent":
    `a9_second`, taken only after `a9` passed), A10 (RC runs) and A11."""
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
            detail="settings mismatch on " + ", ".join(mism),
            evidence=_with_rows({"screenshots": shots}, rows, evidence)), rows, chords)
    unread = [i for i in SAFETY_ROWS if rows[i] == "unreadable"]
    if unread:
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=RunVerdict("complete_with_env_errors"),
            unreached=Outcome("env_error", "attestation_unreadable", GATE, "rows " + ", ".join(unread)),
            alert=alerts.env_error(repeat=previous_unreadable),
            actions=("maintenance_task",) if previous_unreadable else (),
            detail="unreadable after one re-navigation: " + ", ".join(unread),
            evidence=_with_rows({"screenshots": {i: [reads[i].first.screenshot_sha256, reads[i].second.screenshot_sha256
                                                     if reads[i].second else None] for i in unread}},
                                rows, evidence)), rows, chords)
    if profile_drift:
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=RunVerdict("complete_with_env_errors"),
            unreached=Outcome("env_error", "fixture_drift", GATE), alert=alerts.env_error(),
            detail="a non-safety profile row still wrong after the one re-apply (7.6 stage 1)",
            evidence=_with_rows({}, rows, evidence)), rows, chords)
    if a9 is None:
        raise ValueError("A3 to A8 passed: A9 must run next")
    checks = [("A9", a9)]
    if profile.a9_runs == 2 and a9_passed(a9):
        if a9_second is None:
            raise ValueError("A3 reads 'absent': A9 is the sole attestation of the dialog and runs twice (6.13 item 3)")
        checks.append(("A9 (second check)", a9_second))
    for name, check in checks:
        if a9_passed(check):
            continue
        rows["A9"] = "mismatch"
        new_row = check.working_new_rows > 0
        run, un = safety_abort_outcome(GATE, "kill_switch" if new_row else "attestation_mismatch", detail=name)
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=run, unreached=un, alert=alerts.safety_abort(),
            actions=("kill_switch",), incident="S1" if new_row else "S2",
            detail=f"{name}: a new Working row after Confirm and Send" if new_row
            else f"{name}: the Order Confirmation dialog did not appear; the confirmation path is unproven",
            evidence=_with_rows({"screenshot": check.screenshot_sha256}, rows, evidence)), rows, chords)
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
    need = ["A1", "A2", *SAFETY_ROWS, "A9"] + (["A9_second"] if profile.a9_runs == 2 else []) \
        + (["A10"] if rc_run else [])
    absent = [i for i in need if not evidence.get(i)]
    if absent:
        rows["A11"] = "mismatch"
        run, un = harness_abort_outcome("artifact_persist_failed", detail="A11: no evidence hash for " + ", ".join(absent))
        return AttestationResult(ControlResult(
            "attestation", False, GATE, run=run, unreached=un, alert=alerts.aborted_harness(),
            detail="A11: evidence hashes missing for " + ", ".join(absent),
            evidence=_with_rows({}, rows, evidence)), rows, chords, explorer_stage)
    rows["A11"] = "match"
    alert = alerts.safety_abort().plus(paged=("explorer_owner",)) if explorer_stage == "aborted_safety" else None
    return AttestationResult(ControlResult(
        "attestation", True, GATE, outcomes=outcomes, alert=alert,
        detail="A10 mismatch: the Explorer stage is aborted, the RC run continues" if alert else None,
        evidence=_with_rows({"profile_sha256": profile.sha256}, rows, evidence)), rows, chords, explorer_stage)
