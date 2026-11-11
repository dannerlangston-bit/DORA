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
the first names the reason and the rest go in `detail`. The gateway attempts are refused with the slow-half codes of 4.13
(gateway.py choice 1): `guarded_key_refused` for the keys and chords of items 1 and 3, `deny_profile` for
the ladder cell of item 2 (deny geometry outside an order-stage step), and for item 4 `shift_click_dialog` on
the ticket's Buy button and `deny_profile` on the ladder cell, because the deny profile runs before the flag
re-check in stage 4 of 5.4.3 (the hook drops both raw attempts `shift_click_dialog`).

`run_drill` drives the drill on a `Rig` and scores it with `score_drill`. What it needs beyond the `Rig` is the
`DrillPort` (build the drill ticket, locate the drill's targets, the `close_dialog` and `clear_ticket`
teardown actions with the `absent` assertion); `SimDrillPort` implements it over `SimulatedRig`, the Windows track
over the drill flow's scripted steps. Item 5 writes the kill flag through a `ports.KillChannel`, then runs the 6.6
sequence itself with the hook in each phase of `gateway.HOOK_STATES` (deny-all, the tree killed, the recovery login
with A1 and A2 in the recovery-only arm, the Working read, `Cancel All` located by `Rig.locate_cancel_all` without a
click, the proof, the revert, then `between_runs`) and times each part with the clock from the moment the flag was
written.

Choices for `run_drill` (reported):
- The preconditions are checked first, with the drill ticket built on the way (the fourth precondition is
  that it is on the EMPTY account); a failed one stops the drill before anything is injected, and the Oracle
  reads the Monitor at once (a new row in any of the three grids is the S1 case).
- Item 1 runs on the first ticket; item 2 needs no ticket and runs after item 1's teardown; the ticket is then
  rebuilt for items 3 and 4 ("the drill ticket keeps the flag set") and item 5 writes the flag while it is up.
  An item that cannot run (a target not located, the dialog not open, the ticket not rebuilt) is recorded in
  `could_not_run` and the drill goes on with the next item, so one renamed control does not hide the others.
- A raw attempt injects the activating event only (key down or button down) with origin `drill`; the hook's
  drop of it, drained in the drill's declared raw phase, is the record. A gateway attempt is refused in the
  slow half; anything else the slow half lets through is recorded as the hook decided it, so it is never in
  the declared set. Every drop and refusal goes to the run's `DropLedger` as exempt (drill tag, 5.4.8).
- Item 5's `Cancel All` counts as located when the recovery read showed no new row and the rig located it,
  or when a new row made the recovery flow click it and the re-read came back clean. The rig counts as
  reverted when `revert_snapshot` returned without an exception. The final Oracle read is item 5's recovery
  read (taken before the revert), and the clean read that made `Cancel All` click counts too (the larger
  new-row count wins), so re-reads that go unreadable after the click never hide a sent order: with no usable
  read the new-row count is 0 and item 5 fails, so the drill is never PROVEN without it.
- `run_drill` executes the drill's own steps only; the actions of the result (block the login, quarantine,
  the kill switch after a new row) are the caller's, as for every control result.
- A selector text that is not an allowlist entry at item 5's recovery login is the 6.4 mismatch (killswitch choice
  18): item 5 fails (the recovery login failed A1), and the drill's one result also carries the mismatch's run
  outcome (`aborted_safety` with `live_identity` or `paper_mode_mismatch`), unreached outcome, incident, actions
  (the login blocked for `live_identity`), safety page and selector crop, so persisting that result applies both.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from . import alerts
from .clock import Clock
from .gateway import VK_ESCAPE, VK_RETURN, VK_SPACE, Action, Chord, DropLedger, InputEvent, drain, refusal
from .identity import Allowlist
from .killswitch import GRIDS, PROVEN_NONE, ProofInputs, cancel_new_working, post_run_proof, recovery_login
from .ports import KillChannel, KillRequest
from .results import ControlResult, Outcome, safety_abort_outcome
from .rig import EmitResult, OrderBook, Rig
from .simrig import CONFIRM_POINT, DELETE_POINT, SimulatedRig

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
        exp[DrillRecord(1, "gateway", "refused", "guarded_key_refused")] += 1
        exp[DrillRecord(1, "raw", "dropped", reason)] += 1
    exp[DrillRecord(2, "gateway", "refused", "deny_profile")] += 1
    exp[DrillRecord(2, "raw", "dropped", "no_token")] += 1
    for _ in chords:
        exp[DrillRecord(3, "gateway", "refused", "guarded_key_refused")] += 1
        exp[DrillRecord(3, "raw", "dropped", "blocked_chord_dialog")] += 1
    # item 4: the slow half runs the deny profile before the flag re-check (5.4.3 stage 4), so the ladder cell is
    # refused as order geometry outside an order-stage step before its Shift is seen; the hook drops both raw
    exp[DrillRecord(4, "gateway", "refused", "shift_click_dialog")] += 1         # ticket Buy button
    exp[DrillRecord(4, "gateway", "refused", "deny_profile")] += 1               # ladder cell
    for _ in ("ticket buy button", "ladder cell"):
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
    identity: ControlResult | None = None       # item 5: the 6.4 mismatch its recovery login saw (killswitch choice 18)


@dataclass(frozen=True)
class DrillResult:
    """The drill's `ControlResult`, the failed items in order and, from `run_drill`, what it observed."""

    result: ControlResult
    failed: tuple[str, ...] = ()
    observation: DrillObservation | None = None


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
    result = ControlResult(
        "fire_drill", False, "running", checkpoint=Outcome("harness_error", f"drill_failed:{failed[0]}",
                                                           detail="; ".join(notes) or None),
        alert=alerts.drill_failure(), actions=tuple(acts), incident="S2",
        detail="fire drill failed: " + ", ".join(failed) + "; the Nightly run stays complete")
    i = obs.identity
    if i is not None:                                    # a 6.4 mismatch at item 5's recovery login (docstring)
        ev = dict(i.evidence) | ({"blocked_reason": "live_identity"} if i.run_reason == "live_identity" else {})
        result = replace(result, run=i.run, unreached=i.unreached, alert=alerts.safety_abort(), incident=i.incident,
                         actions=tuple(dict.fromkeys(result.actions + i.actions)), evidence=ev,
                         detail=f"fire drill failed: {', '.join(failed)}; item 5's recovery login saw {i.run_reason}")
    return DrillResult(result, tuple(failed))


# -- driving the drill on a rig (6.5) -----------------------------------------------------------------------
DRILL_UNIT = "flow.fire-drill"          # the drill flow's unit in the DropLedger (its drops are exempt, 5.4.8)
DRILL_TARGETS = ("confirm_and_send", "delete", "ladder_buy_cell", "ticket_buy_button")
DIALOG_TITLE = "Order Confirmation"
CTRL = frozenset({"ctrl"})
SHIFT = frozenset({"shift"})


@runtime_checkable
class DrillPort(Protocol):
    """What the drill needs from the rig beyond `Rig` (6.5); module docstring."""

    def build_drill_ticket(self) -> str | None:
        """Build the drill ticket (1 share, far-from-market DAY limit, EMPTY account) through the drill flow's
        scripted steps; the role of the account it is on (`empty`), or None when it could not be built."""

    def locate(self, target: str) -> tuple[int, int] | None:
        """The point of one of `DRILL_TARGETS` on the current screen, or None when it is not located."""

    def clear_drill_ticket(self) -> bool:
        """The `close_dialog` and `clear_ticket` teardown actions, then the `absent` assertion on the dialog
        title and the ticket: True when both are absent."""


@dataclass(frozen=True)
class DrillPreconditions:
    """The preconditions of 6.5 that come from the session rather than the drill's own steps."""

    attestation_passed: bool                 # A1 to A9 passed in this session
    probe_dropped_age_s: float | None        # seconds since the hook dropped the liveness probe; None: passed through
    injector_signed: bool                    # the drill injector is the signed binary the lint allowlists


class SimDrillPort:
    """`DrillPort` over `SimulatedRig`: the drill ticket is the simulated docked ticket (`ticket_visible`), the
    targets are the simulated app's fixed points (`unlocated` names targets to report as not located, as after a
    rename in a new build)."""

    POINTS = {"confirm_and_send": CONFIRM_POINT, "delete": DELETE_POINT, "ladder_buy_cell": (300, 700),
              "ticket_buy_button": (50, 50)}

    def __init__(self, rig: SimulatedRig, *, unlocated: frozenset[str] = frozenset(), ticket_builds: bool = True):
        self.rig = rig
        self.unlocated = unlocated
        self.ticket_builds = ticket_builds

    def build_drill_ticket(self) -> str | None:
        if not (self.rig.app_running and self.ticket_builds):
            return None
        self.rig.ticket_visible = True
        return next((a.role for a in self.rig.s.accounts if a.account_id == self.rig.selected), None)

    def locate(self, target: str) -> tuple[int, int] | None:
        if target not in DRILL_TARGETS:
            raise ValueError(f"drill target {target!r}; one of {DRILL_TARGETS}")
        return None if target in self.unlocated or not self.rig.app_running else self.POINTS[target]

    def clear_drill_ticket(self) -> bool:
        self.rig.send_input(Action("press", keys=(VK_ESCAPE,), flow_class="canary", teardown_action="close_dialog"))
        self.rig.ticket_visible = False                  # the simulated app clears the ticket on clear_ticket
        self.rig.send_input(Action("press", keys=(VK_ESCAPE,), flow_class="canary", teardown_action="clear_ticket"))
        return not (self.rig.dialog_open or self.rig.ticket_visible)


class _Drive:
    """One drill on one rig: the records it collected and the items that could not run."""

    def __init__(self, rig: Rig, port: DrillPort, ledger: DropLedger):
        self.rig, self.port, self.ledger = rig, port, ledger
        self.records: list[DrillRecord] = []
        self.could_not_run: dict[int, str] = {}
        self.identity: ControlResult | None = None       # the 6.4 mismatch item 5's recovery login saw

    def dialog_up(self) -> bool:
        return any(w.title.startswith(DIALOG_TITLE) for w in self.rig.enumerate_windows())

    def _hook_records(self, item: int, attempt: str, r: EmitResult) -> None:
        for d, e in zip(r.decisions, r.events):
            if not d.allowed:
                drop = drain(d, e, drill_phase=True)
                self.ledger.record(drop, unit=DRILL_UNIT, exempt=True)
                self.records.append(DrillRecord(item, attempt, "dropped", drop.reason))

    def attempt(self, item: int, action: Action, raw: InputEvent) -> None:
        """One item attempted twice: through the gateway (refused in the slow half) and by the drill injector
        (dropped by the hook)."""
        r = self.rig.send_input(action)
        if r.refused is not None:
            self.ledger.record(refusal(r.refused), unit=DRILL_UNIT, exempt=True)
            self.records.append(DrillRecord(item, "gateway", "refused", r.refused.reason or ""))
        else:
            self._hook_records(item, "gateway", r)
        self._hook_records(item, "raw", self.rig.send_input(action, events=(raw,)))

    def located(self, item: int, target: str, what: str) -> tuple[int, int] | None:
        p = self.port.locate(target)
        if p is None:
            self.could_not_run.setdefault(item, f"{what} not located")
        return p


def _new_rows(start: OrderBook, end: OrderBook | None) -> int:
    """New Working, Filled and Canceled rows against the run-start snapshot (6.5); 0 without a readable read."""
    if end is None or not end.readable:
        return 0
    return sum(1 for g in GRIDS for r in end.rows(g) if r.key not in {s.key for s in start.rows(g)})


def _item1(d: _Drive) -> tuple[bool, bool]:
    """Enter, Space and Ctrl+Enter on the open Order Confirmation dialog; (dialog open after the keys, the
    Delete, close_dialog, clear_ticket and absent steps passed)."""
    p = d.located(1, "confirm_and_send", "Confirm and Send")
    if p is None:
        return True, d.port.clear_drill_ticket()
    r = d.rig.send_input(Action("click", point=p, resolved_name="Confirm and Send", under_point_text="Confirm and Send",
                                order_stage_step=True, flow_class="canary"))
    if r.refused is not None or not d.dialog_up():
        d.could_not_run[1] = "the Order Confirmation dialog did not open after Confirm and Send"
        return True, d.port.clear_drill_ticket()
    for vk, mods in ((VK_RETURN, frozenset()), (VK_SPACE, frozenset()), (VK_RETURN, CTRL)):
        d.attempt(1, Action("key_chord" if mods else "press", keys=(vk,), mods=mods, declared_guarded=True,
                            flow_class="canary"), InputEvent("key_down", vk=vk, mods=mods, origin="drill"))
    still_open = d.dialog_up()
    delete = d.located(1, "delete", "the dialog allowlist label Delete")
    if delete is not None:
        d.rig.send_input(Action("click", point=delete, resolved_name="Delete", under_point_text="Delete",
                                flow_class="canary"))
    absent = d.port.clear_drill_ticket() and not d.dialog_up()
    return still_open, absent and delete is not None


def _item2(d: _Drive) -> None:
    """The Auto Send ladder click: a ladder Buy cell on the fixture Active Trader panel."""
    p = d.located(2, "ladder_buy_cell", "the Active Trader panel or a ladder cell")
    if p is not None:
        d.attempt(2, Action("click", point=p, resolved_name="Buy", under_point_text="Buy",
                            target_kind="ladder_order_cell", flow_class="canary"),
                  InputEvent("button_down", *p, button="left", origin="drill"))


def _item3(d: _Drive, chords: Sequence[Chord]) -> None:
    """Every default order chord while the drill ticket keeps the flag set."""
    for c in chords:
        d.attempt(3, Action("key_chord", keys=(c.vk,), mods=c.mods, flow_class="canary"),
                  InputEvent("key_down", vk=c.vk, mods=c.mods, origin="drill"))


def _item4(d: _Drive) -> None:
    """Shift+click on the ticket's Buy button and on a ladder cell."""
    buy = d.located(4, "ticket_buy_button", "the ticket's Buy button")
    cell = d.located(4, "ladder_buy_cell", "a ladder cell")
    for p, kind in ((buy, None), (cell, "ladder_order_cell")):
        if p is not None:
            d.attempt(4, Action("click", point=p, resolved_name="Buy", under_point_text="Buy", mods=SHIFT,
                                target_kind=kind, flow_class="canary"),
                      InputEvent("button_down", *p, button="left", mods=SHIFT, origin="drill"))


def _item5(d: _Drive, *, clock: Clock, kill_channel: KillChannel, rig_id: str, start: OrderBook,
           allowlist: Allowlist, fixture_account: str | None) -> tuple[Item5, tuple[OrderBook | None, ...]]:
    """The kill switch end to end (6.5 item 5, 6.6): the file flag written mid-ticket, then the sequence, timed.
    Returns item 5's measurements and its Monitor reads: the last one, and the clean read that made `Cancel All`
    click (killswitch choice 4), so a row either showed counts."""
    t0 = clock.monotonic()
    kill_channel.post(KillRequest(rig_id, "file", "fire_drill", "fire drill item 5", clock.now().isoformat()))
    req = kill_channel.poll()
    if req is None:
        d.could_not_run[5] = "the kill flag did not arrive on the channel"
        d.port.clear_drill_ticket()
        return Item5(None, None, False, False, None, False), ()
    d.rig.set_hook_phase("kill_switch")
    deny_all_s = clock.monotonic() - t0
    tree_gone_s = clock.monotonic() - t0 if d.rig.kill_process_tree() else None
    d.rig.reset_input_state()
    d.rig.set_hook_phase("recovery")                 # the recovery-only arm, as in the sequence itself (6.6 step 5)
    login = recovery_login(d.rig, allowlist, clock, fixture_account)
    login_ok = login.ok
    d.identity = login.identity
    end: OrderBook | None = None
    found: OrderBook | None = None
    cancel, located = "not_needed", False
    if login_ok:
        cancel, end, found = cancel_new_working(d.rig, start, d.rig.read_orders(), clock=clock)
        located = cancel == "done" or (cancel == "not_needed" and d.rig.locate_cancel_all())
    proof = post_run_proof(ProofInputs(start=start, end=end, recovery_login_ok=login_ok, cancel_all=cancel, found=found))
    try:
        d.rig.revert_snapshot()
        reverted = True
    except Exception:            # noqa: BLE001 - any revert failure is "rig not reverted" (6.5 item 5), never a crash
        reverted = False
    d.rig.set_hook_phase("between_runs")
    kill_channel.acknowledge(req)
    return Item5(deny_all_s, tree_gone_s, login_ok, located, proof.verdict, reverted), (end, found)


def run_drill(rig: Rig, ledger: DropLedger, clock: Clock, chords: Sequence[Chord], start_book: OrderBook,
              kill_channel: KillChannel, *, port: DrillPort, preconditions: DrillPreconditions, allowlist: Allowlist,
              rig_id: str, fixture_account: str | None = None) -> DrillResult:
    """Run the Nightly fire drill on `rig` and score it (6.5): preconditions, items 1 to 5, the Oracle read
    against `start_book` (the run-start snapshot). `chords` are the default order chords of item 3, already
    loaded into the hook at `settings_attested`; the result carries the observation for the canary rows."""
    d = _Drive(rig, port, ledger)
    role = port.build_drill_ticket()
    pre = preconditions
    if (pre.probe_dropped_age_s is None or pre.probe_dropped_age_s > PROBE_MAX_AGE_S or not pre.attestation_passed
            or not pre.injector_signed or role != "empty"):
        port.clear_drill_ticket()
        obs = DrillObservation(pre.attestation_passed, pre.probe_dropped_age_s, pre.injector_signed, role == "empty",
                               (), Item5(None, None, False, False, None, False),
                               new_order_rows=_new_rows(start_book, rig.read_orders()))
        return _with(score_drill(obs, chords), obs)
    still_open, absent = _item1(d)
    _item2(d)
    if port.build_drill_ticket() is None:
        for item in (3, 4):
            d.could_not_run.setdefault(item, "the drill ticket could not be rebuilt")
    else:
        _item3(d, chords)
        _item4(d)
    item5, reads = _item5(d, clock=clock, kill_channel=kill_channel, rig_id=rig_id, start=start_book,
                          allowlist=allowlist, fixture_account=fixture_account)
    obs = DrillObservation(True, pre.probe_dropped_age_s, True, True, tuple(d.records), item5,
                           new_order_rows=max((_new_rows(start_book, b) for b in reads), default=0),
                           could_not_run=dict(d.could_not_run),
                           dialog_open_after_keys=still_open, absent_passed=absent, identity=d.identity)
    return _with(score_drill(obs, chords), obs)


def _with(r: DrillResult, obs: DrillObservation) -> DrillResult:
    return DrillResult(r.result, r.failed, obs)
