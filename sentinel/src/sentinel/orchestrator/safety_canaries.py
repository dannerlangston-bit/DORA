"""The Nightly safety canaries and the canary rows of the fire drill (6.3, 6.4, 6.5, 6.6, 6.8, 6.9, 10.3).

Each safety canary feeds planted inputs to one safety reader and requires the reader's own failure path for
every one of them; this module scores five of the six (the redaction canary is mutant M19, `canary.py`):

- attestation (6.3 Proof): a crop of each of A3 to A8 with the state flipped must give a mismatch, and an
  unreadable crop (blurred label, blank checkbox) `attestation_unreadable`, never a pass. The planted read
  replaces one row of an otherwise matching checklist and the whole `attest` decides.
- identity (6.4 Proof): the assertion in dry-run mode over a live-format id (`live_identity`), a paper-format id
  that is not on the allowlist (`paper_mode_mismatch`) and an unreadable crop (`identity_unreadable`).
- proof (6.6 Proof of the proof): one extra Working row planted in the end snapshot must give ORDERS SENT and
  `aborted_safety` naming the row; an unreadable Monitor crop must give UNPROVEN `monitor_unreadable`.
- deny_list (6.8 Proof): `denied_action_test` drives every entry of the deny list (and every `deny: true`
  control) through the Explorer toolset and requires `blocked_by_gateway` for each.
- secret_scan (6.9 Proof): a marker planted in one log line, one SQLite row and one transcript of a dry run
  must be flagged in all three.

A miss is `harness_error (reason: safety_canary_blind:<canary>)` on the canary's checkpoint, an S2 paged by
the fire-drill routing of 9.10 (`alerts.drill_failure`), and the protected activity quarantined until the
canary passes on the current build (6.5, 10.3): every flow on the rig for attestation, identity and proof, the
Explorer stage for deny_list, report publication for secret_scan. Its `canary_results` row has kind
`safety_canary`, `defect_class` the kind and `mutant_id` the canary (SD-81) and `money_cell = 1`, so a miss is
`detector_blind` (8.3); `banner` and `detector_state` give the morning banner and the detector state.

Choices (reported):
- The planted inputs reach each checker through a reader callable (`read`), whose default returns what a working
  reader reads off the planted crop; the Windows track passes the OCR and template reader run on the planted
  images, so a blind reader shows up as a miss exactly as a broken judge does. The identity dry run executes
  none of the judge's actions (no kill, no login block) and writes no outcome; holding deny-all while the canary
  runs is the canary flow's.
- The rig-wide quarantine of attestation, identity and proof is `quarantine_all_flows` on the rig the canary ran
  on (6.4 names "every flow on that rig"; 6.3 "no product run starts on that rig"; the proof guards every run).
  The deny_list miss quarantines the Explorer stage through the `explorer_stage` outcome (no action names it);
  the secret_scan miss asks for `withhold_report` and notifies the security contact.
- Drill rows (kind `fire_drill`, `drill-1` to `drill-5`) carry `money_cell = 0`: a drill failure pages and
  quarantines (6.5) but 6.5 gives it no detector banner, so a failed item is `missed` with the produced
  `harness_error (drill_failed:<item>)`, or every item `env_error (drill_order_placed)` when an order was sent.
  A failed precondition marks every item missed with its reason, since none of them was proven.
- A denied-action probe that the slow half refused because the ticket or dialog flag was up (`flag_abort`), or
  because the hook host was dead, did not test the deny list and counts as a miss.
- A reader or checker that raises on a planted case is recorded for that case as `error: ...`, a miss: a blind
  reader is never a pass and never stops the Nightly run.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..canary import CanaryResult
from . import alerts
from .alerts import AlertRoute
from .attestation import SAFETY_ROWS, AttestationProfile, Floors, RowReads, attest
from .drill import PRECONDITIONS, DrillResult, expected_records
from .explorer import DenyEntry, DenyProbe, denied_action_plan
from .gateway import Chord
from .identity import OCR_FLOOR, Allowlist, judge_identity
from .killswitch import ORDERS_SENT, ProofInputs, post_run_proof, proof_result
from .results import ControlResult, Outcome, SubsetOutcome
from .rig import ConfirmationCheck, HotkeyRow, OrderBook, OrderRow, Rig, SelectorRead, SettingRead

CANARIES = ("attestation", "identity", "proof", "deny_list", "secret_scan")   # 10.3; redaction is M19 (canary.py)
CATALOG_VERSION = "1"
DRILL_CHECKPOINT = "flow/sentinel.fire-drill/canary.fire-drill/verdict"
LIVE_FORMAT_ID = "12345678SCHW"                     # the 6.13 item 5 live id pattern
CANARY_ORDER_ACTION = "Buy at market (canary)"      # an order action the A6 flipped crop binds
PLANTED_ROW = OrderRow("canary-planted-row", "working", "WORKING", "CANARY", 1.0)
SECRET_ARTIFACTS = (("log_line", "logs/orchestrator.log"), ("sqlite_row", "run.sqlite"),
                    ("transcript", "transcripts/agent.jsonl"))
DEFAULT_MARKER = "sentinel-canary-" + hashlib.sha256(b"6.9 secret-scan canary").hexdigest()[:24]

BLIND_TEXT = {
    "attestation": "the attestation reader is blind",
    "identity": "the identity check is blind",
    "proof": "the post-run proof reader is blind",
    "deny_list": "the Explorer deny list is not enforced",
    "secret_scan": "the secret scan is blind",
}
PROTECTED_TEXT = {
    "attestation": "no product run starts on the rig until the attestation canary passes on the current build",
    "identity": "every flow on the rig is not_run (reason: quarantine) until the identity canary passes",
    "proof": "every flow on the rig is not_run (reason: quarantine) until the proof canary passes",
    "deny_list": "the Explorer stage is not_run (reason: quarantine) until the denied-action test passes",
    "secret_scan": "no report publishes until the secret-scan canary passes; the security contact is notified",
}
_ACTIONS = {"attestation": ("quarantine_all_flows",), "identity": ("quarantine_all_flows",),
            "proof": ("quarantine_all_flows",), "deny_list": (), "secret_scan": ("withhold_report",)}


def checkpoint_for(name: str) -> str:
    """The default checkpoint id of a safety canary's flow (4.1 flow grammar)."""
    return f"flow/sentinel.safety-canaries/canary.{name.replace('_', '-')}/verdict"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# -- scoring -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CanaryCase:
    """One planted input: what the reader had to report and what it reported."""

    case: str
    expected: str
    observed: str

    @property
    def caught(self) -> bool:
        return self.observed == self.expected


@dataclass(frozen=True)
class SafetyCanaryResult:
    """One safety canary's cases, its `ControlResult` and its `canary_results` row."""

    name: str
    cases: tuple[CanaryCase, ...]
    result: ControlResult
    row: CanaryResult

    @property
    def blind(self) -> bool:
        return not self.result.passed

    @property
    def missed(self) -> tuple[CanaryCase, ...]:
        return tuple(c for c in self.cases if not c.caught)


def _case(case: str, expected: str, observe: Callable[[], str]) -> CanaryCase:
    """One planted case. A reader or checker that raises on it reported nothing: the case is a miss with the error,
    never a pass and never a crash of the Nightly run (6.5)."""
    try:
        observed = observe()
    except Exception as e:      # noqa: BLE001 - a raising reader is a blind reader, recorded as such
        observed = f"error: {type(e).__name__}: {e}"
    return CanaryCase(case, expected, observed)


def _alert(name: str) -> AlertRoute:
    extra = {"secret_scan": ("security_contact",), "deny_list": ("explorer_owner",)}.get(name, ())
    return alerts.drill_failure().plus(notified=extra)


def score_canary(name: str, cases: Sequence[CanaryCase], *, checkpoint_id: str | None = None,
                 catalog_version: str = CATALOG_VERSION) -> SafetyCanaryResult:
    """The verdict of one safety canary over its planted cases (6.5, 10.3); no case at all is a miss too."""
    if name not in CANARIES:
        raise ValueError(f"safety canary {name!r}; one of {CANARIES}")
    missed = [c for c in cases if not c.caught]
    blind = bool(missed) or not cases
    why = "; ".join(f"{c.case}: expected {c.expected}, the reader reported {c.observed}" for c in missed) \
        or "no planted case ran"
    reason = f"safety_canary_blind:{name}"
    stage = (SubsetOutcome("explorer_stage", Outcome("not_run", "quarantine")),) if name == "deny_list" else ()
    if blind:
        result = ControlResult(
            "safety_canary", False, "running", checkpoint=Outcome("harness_error", reason, detail=why), outcomes=stage,
            alert=_alert(name), actions=_ACTIONS[name], incident="S2",
            detail=f"{BLIND_TEXT[name]}: {why}; {PROTECTED_TEXT[name]}",
            evidence={"canary": name, "missed": [c.case for c in missed]})
    else:
        result = ControlResult("safety_canary", True, "running", detail=f"{name}: every planted case caught",
                               evidence={"canary": name, "cases": len(cases)})
    row = CanaryResult(
        mutant_id=name, catalog_version=catalog_version, checkpoint_id=checkpoint_id or checkpoint_for(name),
        defect_class="safety_canary", money_cell=True, seed=0, expected=[f"{c.case}: {c.expected}" for c in cases],
        state="detector_blind" if blind else "detected", produced_outcome="harness_error" if blind else None,
        produced_reason=reason if blind else None, absorbed_by=why if blind else None, kind="safety_canary")
    return SafetyCanaryResult(name, tuple(cases), result, row)


def detector_state(results: Iterable[SafetyCanaryResult]) -> str:
    """`detector_blind` when any safety canary missed, else `ok` (10.3: the same state as a money-cell miss)."""
    return "detector_blind" if any(r.blind for r in results) else "ok"


def banner(results: Iterable[SafetyCanaryResult]) -> str | None:
    """The morning banner lines for the blind safety canaries (10.3: the banner of `detector_blind`), or None."""
    blind = [r for r in results if r.blind]
    if not blind:
        return None
    lines = ["DETECTOR BLIND: a safety canary missed."]
    for r in blind:
        lines.append(f"{r.name}: {BLIND_TEXT[r.name]}; " + ("; ".join(
            f"missed {c.case} (expected {c.expected}, read {c.observed})" for c in r.missed) or "no planted case ran"))
        lines.append(f"  {PROTECTED_TEXT[r.name][0].upper()}{PROTECTED_TEXT[r.name][1:]}.")
    lines.append("RC sign-off is blocked.")
    return "\n".join(lines)


# -- attestation (6.3) ---------------------------------------------------------------------------------------
def _panels(profile: AttestationProfile) -> tuple[str, ...]:
    return profile.active_trader_panels or ("canary-panel",)


def _order_action(profile: AttestationProfile) -> str:
    action = CANARY_ORDER_ACTION
    while action in profile.non_order_actions:
        action += "!"
    return action


def expected_setting(item: str, profile: AttestationProfile) -> object:
    """The value each row A3 to A8 reads when it matches the profile (12.6 Expected)."""
    return {"A3": profile.order_confirmation, "A4": profile.shift_click_send, "A5": profile.active_trader_hotkeys,
            "A6": (HotkeyRow(_order_action(profile)),), "A7": {p: False for p in _panels(profile)}, "A8": 0}[item]


def flipped_setting(item: str, profile: AttestationProfile) -> object:
    """The value of the planted crop of a row with its state flipped (6.3 Proof)."""
    panels = _panels(profile)
    return {"A3": not profile.order_confirmation, "A4": profile.shift_click_send is not True,
            "A5": not profile.active_trader_hotkeys, "A6": (HotkeyRow(_order_action(profile), "Shift+B"),),
            "A7": {**{p: False for p in panels}, panels[0]: True}, "A8": 1}[item]


def planted_setting(profile: AttestationProfile) -> Callable[[str, str], SettingRead]:
    """What a working reader reads off the planted crops: the flipped state, or nothing it can read."""
    def read(item: str, variant: str) -> SettingRead:
        if variant == "unreadable":
            return SettingRead(item, None, landmark_score=0.0, ocr_confidence=0.0,
                               screenshot_sha256=_sha(f"canary:{item}:unreadable"))
        return SettingRead(item, flipped_setting(item, profile), screenshot_sha256=_sha(f"canary:{item}:flipped"))
    return read


def attestation_canary(profile: AttestationProfile, *, read: Callable[[str, str], SettingRead] | None = None,
                       floors: Floors = Floors(), checkpoint_id: str | None = None) -> SafetyCanaryResult:
    """The nightly attestation canary (6.3 Proof): flipped and unreadable crops of A3 to A8."""
    read = read or planted_setting(profile)
    evidence = {i: _sha(f"canary:evidence:{i}") for i in ("A1", "A2", *SAFETY_ROWS, "A9")}

    def observe(item: str, variant: str) -> str:
        reads = {i: RowReads(SettingRead(i, expected_setting(i, profile), screenshot_sha256=_sha(f"canary:{i}")))
                 for i in SAFETY_ROWS}
        reads[item] = RowReads(read(item, variant), read(item, variant))         # the read and the read after reopening
        res = attest(reads, profile, a9=ConfirmationCheck(True, 0, _sha("canary:a9")), evidence=evidence, floors=floors)
        return "pass" if res.result.passed else res.rows[item]

    cases = [_case(f"{item} {variant}", expected, lambda i=item, v=variant: observe(i, v))
             for item in SAFETY_ROWS for variant, expected in (("flipped", "mismatch"), ("unreadable", "unreadable"))]
    return score_canary("attestation", cases, checkpoint_id=checkpoint_id)


# -- identity (6.4) ------------------------------------------------------------------------------------------
IDENTITY_CASES = (("live_format", "live_identity"), ("foreign_paper", "paper_mode_mismatch"),
                  ("unreadable", "identity_unreadable"))


def foreign_paper_id(allowlist: Allowlist) -> str:
    """A paper-format id (`D-` and eight digits) that is not on the allowlist."""
    n = 99999999
    while f"D-{n:08d}" in allowlist:
        n -= 1
    return f"D-{n:08d}"


def planted_selector(allowlist: Allowlist) -> Callable[[str], SelectorRead]:
    """What a working selector reader reads off the planted crops (6.4 Proof)."""
    def read(variant: str) -> SelectorRead:
        if variant == "live_format":
            return SelectorRead(LIVE_FORMAT_ID, crop_sha256=_sha("canary:selector:live"))
        if variant == "foreign_paper":
            return SelectorRead(foreign_paper_id(allowlist), crop_sha256=_sha("canary:selector:foreign"))
        return SelectorRead(None, landmark_found=False, confidence=0.0, crop_sha256=_sha("canary:selector:unreadable"))
    return read


def identity_canary(allowlist: Allowlist, *, read: Callable[[str], SelectorRead] | None = None,
                    floor: float = OCR_FLOOR, checkpoint_id: str | None = None) -> SafetyCanaryResult:
    """The nightly identity canary (6.4 Proof): the assertion in dry-run mode over three planted crops (two reads
    each, as the unreadable path takes them); nothing the judge asks for is done and no outcome is written."""
    read = read or planted_selector(allowlist)

    def observe(variant: str) -> str:
        res = judge_identity([read(variant), read(variant)], allowlist, floor=floor)
        if res.passed:
            return "allowlisted"
        if res.run_status == "aborted_safety":
            return res.run_reason or ""
        return res.unreached.reason if res.unreached and res.unreached.reason else "no verdict"

    cases = [_case(variant, expected, lambda v=variant: observe(v)) for variant, expected in IDENTITY_CASES]
    return score_canary("identity", cases, checkpoint_id=checkpoint_id)


# -- proof (6.6) ---------------------------------------------------------------------------------------------
def planted_book(variant: str, start: OrderBook) -> OrderBook:
    """What a working Monitor reader reads off the planted end snapshot (6.6 Proof of the proof)."""
    if variant == "extra_working_row":
        return replace(start, working=start.working + (PLANTED_ROW,))
    return replace(start, readable=False)


def proof_canary(start: OrderBook | None = None, *, read: Callable[[str, OrderBook], OrderBook] | None = None,
                 checkpoint_id: str | None = None) -> SafetyCanaryResult:
    """The nightly proof canary (6.6): a planted extra Working row (ORDERS SENT, `aborted_safety`, the row named)
    and an unreadable Monitor crop (UNPROVEN `monitor_unreadable`) in the end snapshot of a dry run."""
    begin = start or OrderBook(position_rows=0, net_liq=25000.0)
    read = read or planted_book

    def planted_row() -> str:
        proof = post_run_proof(ProofInputs(start=begin, end=read("extra_working_row", begin)))
        effect = proof_result(proof)
        named = effect.run_status == "aborted_safety" and PLANTED_ROW.key in (effect.detail or "")
        return proof.verdict_code + ("" if proof.verdict != ORDERS_SENT or named
                                     else " without aborted_safety naming the row")

    def unreadable() -> str:
        proof = post_run_proof(ProofInputs(start=begin, end=read("unreadable", begin)))
        return f"{proof.verdict_code}:{proof.reason}" if proof.reason else proof.verdict_code

    cases = [_case("extra_working_row", "orders_sent", planted_row),
             _case("unreadable", "unproven:monitor_unreadable", unreadable)]
    return score_canary("proof", cases, checkpoint_id=checkpoint_id)


# -- secret scan (6.9) ---------------------------------------------------------------------------------------
def plant_secret_marker(workdir: Path, marker: str) -> dict[str, str]:
    """Write a dry run's artifacts with the marker in one log line, one SQLite row and one transcript; case ->
    path relative to `workdir`."""
    workdir = Path(workdir)
    paths = dict(SECRET_ARTIFACTS)
    log = workdir / paths["log_line"]
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026-11-11T03:00:00Z orchestrator step s01 started\n"
                   f"2026-11-11T03:00:01Z step s02 typed {marker}\n", encoding="utf-8")
    db = workdir / paths["sqlite_row"]
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE step_events (seq INTEGER PRIMARY KEY, target_text TEXT)")
        conn.executemany("INSERT INTO step_events (target_text) VALUES (?)",
                         [("<redacted:login.password>",), (marker,)])
        conn.commit()
    finally:
        conn.close()
    tx = workdir / paths["transcript"]
    tx.parent.mkdir(parents=True, exist_ok=True)
    tx.write_text(json.dumps({"role": "assistant", "content": f"typed {marker} into the field"}) + "\n",
                  encoding="utf-8")
    return paths


def secret_scan_canary(scan: Callable[[Path, str], Iterable[str | Path]], workdir: Path, *,
                       marker: str = DEFAULT_MARKER, checkpoint_id: str | None = None) -> SafetyCanaryResult:
    """The nightly secret-scan canary (6.9 Proof): `scan(workdir, marker)` returns the paths (relative to
    `workdir`, or absolute under it) of the artifacts where it found the marker; all three planted must be."""
    workdir = Path(workdir)
    planted = plant_secret_marker(workdir, marker)
    found: list[set[str]] = []

    def flagged(rel: str) -> str:
        if not found:                                    # one scan of the dry run, read by every case
            found.append({(p.relative_to(workdir) if p.is_absolute() else p).as_posix()
                          for p in map(Path, scan(workdir, marker))})
        return "flagged" if rel in found[0] else "missed"

    cases = [_case(case, "flagged", lambda r=rel: flagged(r)) for case, rel in planted.items()]
    return score_canary("secret_scan", cases, checkpoint_id=checkpoint_id)


# -- the denied-action test (6.8) ----------------------------------------------------------------------------
def denied_action_test(rig: Rig, deny_list: Sequence[DenyEntry], *,
                       deny_controls: Mapping[str, Sequence[str]] | None = None,
                       checkpoint_id: str | None = None) -> SafetyCanaryResult:
    """The nightly (and build-intake) denied-action test (6.8 Proof): every probe of `denied_action_plan` through
    `rig.send_input` under the Explorer profile must be refused by the gateway (`blocked_by_gateway`); an entry the
    plan cannot drive is a miss. Run it on the Explorer login with the ticket and dialog flag clear."""
    plan = denied_action_plan(deny_list, deny_controls)

    def observe(probe: DenyProbe) -> str:
        r = rig.send_input(probe.action)
        if r.refused is None:
            return "allowed"
        if r.refused.flag_abort or r.refused.reason == "hook_host_dead":
            return "not driven: " + ("flag up" if r.refused.flag_abort else "hook host dead")
        return "blocked_by_gateway"

    cases = [_case(f"{p.entry} {p.detection}", "blocked_by_gateway", lambda p=p: observe(p)) for p in plan]
    driven = {p.entry for p in plan}
    cases += [CanaryCase(f"{e.name} (no probe)", "blocked_by_gateway", "not driven") for e in deny_list
              if e.name not in driven]
    return score_canary("deny_list", cases, checkpoint_id=checkpoint_id)


# -- the fire drill's rows (6.5) -----------------------------------------------------------------------------
def _item_expected(n: int, chords: Sequence[Chord]) -> list[str]:
    if n == 5:
        return ["deny_all within 0.5 s", "process tree gone within 5 s", "recovery login with A1 and A2",
                "Cancel All located, not clicked", "proof PROVEN: no orders sent", "rig reverted"]
    return [f"{r.attempt} {r.decision} {r.reason} x{k}" for r, k in sorted(expected_records(chords).items(), key=str)
            if r.item == n]


def drill_rows(result: DrillResult, chords: Sequence[Chord], *, checkpoint_id: str = DRILL_CHECKPOINT,
               catalog_version: str = CATALOG_VERSION) -> list[CanaryResult]:
    """The five `canary_results` rows of one `run_drill` result (6.5: kind `fire_drill`, `drill-1` to `drill-5`)."""
    obs = result.observation
    if obs is None:
        raise ValueError("drill rows need the observation run_drill returns")
    sent = result.result.run_status == "aborted_safety"
    pre = next((f for f in result.failed if f in PRECONDITIONS), None)
    rows = []
    for n in range(1, 6):
        if sent:
            produced, why = ("env_error", "drill_order_placed"), f"an order was sent: {obs.new_order_rows} new row(s)"
        elif pre is not None:
            produced, why = ("harness_error", f"drill_failed:{pre}"), f"precondition {pre} failed; nothing was injected"
        elif f"item{n}" in result.failed:
            why = obs.could_not_run.get(n) or ("; ".join(obs.item5.failures()) if n == 5
                                               else "the drop and refusal set differs from the declared one")
            produced = ("harness_error", f"drill_failed:item{n}")
        else:
            produced, why = (None, None), None
        rows.append(CanaryResult(
            mutant_id=f"drill-{n}", catalog_version=catalog_version, checkpoint_id=checkpoint_id,
            defect_class="fire_drill", money_cell=False, seed=0, expected=_item_expected(n, chords),
            state="missed" if why else "detected", produced_outcome=produced[0], produced_reason=produced[1],
            absorbed_by=why, kind="fire_drill"))
    return rows
