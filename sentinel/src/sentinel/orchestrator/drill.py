"""Nightly fire drill scoring (6.5).

The drill is a flow of class `canary` whose items 1 to 4 are attempted twice each, through the
gateway API with profile `scripted` outside an order-stage step (the slow half must refuse, no OS
event) and by raw `SendInput` from the drill injector (the hook must drop; the drops are tagged
`drill` and matched, never raised as `untokened_injected`). Item 5 is the kill switch end to end.
The drill passes only on exactly the declared drop and refusal set, no more and no fewer, with zero
new Working, Filled or Canceled rows.

Two failure cases, by the rows:

- A new order row (any item, a precondition or an item that could not run): an order was sent. The
  kill switch, run `aborted_safety (reason: drill_order_placed)`, the login blocked, S1, the safety
  routing of 9.10, the recovery `Cancel All`.
- No new row, but an item not blocked, an item that could not run, or a failed precondition: the
  drill checkpoint is `harness_error (reason: drill_failed:<item>)`, the Nightly run stays `complete`,
  every flow with an order-stage step is quarantined on every rig until the drill passes (the drill
  flow itself exempt), the fire-drill routing of 9.10 pages, S2. An item that could not run also opens
  a maintenance task on the drill flow owner. Item 5 failing in any part quarantines every flow of
  every class on the rig (the drill and safety canary flows exempt, F68).

Choices (reported): the `<item>` parameter is `item1` to `item5`, or the precondition's name
(`hook_open`, `attestation`, `injector`, `account`) for a failed precondition; with several failed items
the first names the reason and the rest go in `detail`. The gateway attempt of item 2 is refused
`order_geometry` (deny geometry outside an order-stage step; gateway.py choice 2). The slow half's
refusal of items 1, 3 and 4 uses the hook code of the same rule.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import alerts
from .gateway import Chord
from .killswitch import PROVEN_NONE
from .results import ControlResult, Outcome, safety_abort_outcome

PROBE_MAX_AGE_S = 5.0
DENY_ALL_WITHIN_S = 0.5
TREE_GONE_WITHIN_S = 5.0
PRECONDITIONS = ("hook_open", "attestation", "injector", "account")


@dataclass(frozen=True)
class DrillRecord:
    """One drop or refusal the drill caused: item, attempt (`gateway` or `raw`), decision and reason code."""

    item: int
    attempt: str
    decision: str
    reason: str


def expected_records(chords: Sequence[Chord]) -> Counter[DrillRecord]:
    """The declared drop and refusal set of items 1 to 4 (6.5), with the default order chords for item 3."""
    exp: Counter[DrillRecord] = Counter()
    for reason in ("blocked_key_dialog", "blocked_key_dialog", "blocked_chord_dialog"):    # Enter, Space, Ctrl+Enter
        exp[DrillRecord(1, "gateway", "refused", reason)] += 1
        exp[DrillRecord(1, "raw", "dropped", reason)] += 1
    exp[DrillRecord(2, "gateway", "refused", "order_geometry")] += 1
    exp[DrillRecord(2, "raw", "dropped", "no_token")] += 1
    for _ in chords:
        exp[DrillRecord(3, "gateway", "refused", "blocked_chord_dialog")] += 1
        exp[DrillRecord(3, "raw", "dropped", "blocked_chord_dialog")] += 1
    for _ in ("ticket buy button", "ladder cell"):
        exp[DrillRecord(4, "gateway", "refused", "shift_click_dialog")] += 1
        exp[DrillRecord(4, "raw", "dropped", "shift_click_dialog")] += 1
    return exp


@dataclass(frozen=True)
class Item5:
    """Item 5, the kill switch end to end: each part's measurement."""

    deny_all_s: float | None
    tree_gone_s: float | None
    recovery_login_ok: bool
    cancel_all_located: bool
    proof_verdict: str | None
    reverted: bool

    def failures(self) -> list[str]:
        out = []
        if self.deny_all_s is None or self.deny_all_s > DENY_ALL_WITHIN_S:
            out.append("deny_all not within 500 ms")
        if self.tree_gone_s is None or self.tree_gone_s > TREE_GONE_WITHIN_S:
            out.append("process tree alive after 5 s")
        if not self.recovery_login_ok:
            out.append("recovery login failed")
        if not self.cancel_all_located:
            out.append("Cancel All not located")
        if self.proof_verdict != PROVEN_NONE:
            out.append(f"proof {self.proof_verdict or 'not produced'}")
        if not self.reverted:
            out.append("rig not reverted")
        return out


@dataclass(frozen=True)
class DrillObservation:
    """Everything the drill observed."""

    attestation_passed: bool                    # A1 to A9 in this session
    probe_dropped_age_s: float | None           # seconds since the hook dropped the liveness probe; None: passed
    injector_signed: bool
    ticket_on_empty: bool
    records: tuple[DrillRecord, ...]
    item5: Item5
    new_order_rows: int = 0                     # Working, Filled and Canceled against the run-start snapshot
    could_not_run: Mapping[int, str] = field(default_factory=dict)   # item -> what was not located or did not open
    dialog_open_after_keys: bool = True         # item 1: the dialog still open by title after the keys
    absent_passed: bool = True                  # item 1: close_dialog, clear_ticket and the absent assertion


@dataclass(frozen=True)
class DrillResult:
    """The drill's `ControlResult` and the failed items in order."""

    result: ControlResult
    failed: tuple[str, ...] = ()


def score_drill(obs: DrillObservation, chords: Sequence[Chord]) -> DrillResult:
    """Score the fire drill (6.5 Failure handling, by case)."""
    if obs.new_order_rows > 0:
        run, un = safety_abort_outcome("running", "drill_order_placed")
        return DrillResult(ControlResult(
            "fire_drill", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
            actions=("kill_switch", "recovery_login", "cancel_all", "block_login"), incident="S1",
            detail=f"{obs.new_order_rows} new order row(s) during the drill: an order was sent"))
    failed: list[str] = []
    notes: list[str] = []
    if obs.probe_dropped_age_s is None or obs.probe_dropped_age_s > PROBE_MAX_AGE_S:
        failed.append("hook_open")
    if not obs.attestation_passed:
        failed.append("attestation")
    if not obs.injector_signed:
        failed.append("injector")
    if not obs.ticket_on_empty:
        failed.append("account")
    if not failed:
        seen = Counter(obs.records)
        exp = expected_records(chords)
        for item in (1, 2, 3, 4):
            mine = Counter({r: n for r, n in seen.items() if r.item == item})
            want = Counter({r: n for r, n in exp.items() if r.item == item})
            if item in obs.could_not_run:
                failed.append(f"item{item}")
                notes.append(f"item {item} could not run: {obs.could_not_run[item]}")
            elif mine != want:
                failed.append(f"item{item}")
                notes.append(f"item {item} drop set differs: missing {sorted((want - mine).elements(), key=str)}, "
                             f"extra {sorted((mine - want).elements(), key=str)}")
            elif item == 1 and not (obs.dialog_open_after_keys and obs.absent_passed):
                failed.append("item1")
                notes.append("item 1: the dialog closed after the keys" if not obs.dialog_open_after_keys
                             else "item 1: close_dialog, clear_ticket or the absent assertion failed")
        if 5 in obs.could_not_run or obs.item5.failures():
            failed.append("item5")
            notes.append("item 5: " + "; ".join(obs.item5.failures() or [obs.could_not_run.get(5, "")]))
    if not failed:
        return DrillResult(ControlResult("fire_drill", True, "running", detail="every item blocked; PROVEN"))
    acts = ["quarantine_order_stage_flows"]
    if "item5" in failed:
        acts.append("quarantine_all_flows")
    if obs.could_not_run or set(failed) & set(PRECONDITIONS):
        acts.append("maintenance_task")
    if 1 in obs.could_not_run:
        notes.append("the Order Confirmation dialog did not open: also an A9 failure")
    return DrillResult(ControlResult(
        "fire_drill", False, "running", checkpoint=Outcome("harness_error", f"drill_failed:{failed[0]}",
                                                           detail="; ".join(notes) or None),
        outcomes=(), alert=alerts.drill_failure(), actions=tuple(acts), incident="S2",
        detail="fire drill failed: " + ", ".join(failed) + "; the Nightly run stays complete"), tuple(failed))
