"""The bounds source of a session under DR-01 Branch A: the Java Access Bridge enablement check at gate `launched`
(7.5) and a bridge that dies mid-run (5.9, 2.7, SD-54).

Branch A takes element bounds from the JAB tree; Branch B from OCR word boxes and templates, with no bridge, so
nothing here applies to it (preflight H9 covers both branches at run start, 12.7).

- `jab_enablement(state, properties_present=..., query_answers=...)` at the first `launched` gate of a session:
  `.accessibility.properties` present and one JAB query answering, or else `harness_error (reason:
  bounds_source_unavailable)` for the invariants that need bounds, while locators fall to the next rung (7.5).
- `bridge_lost(state, checkpoint_id=...)` when the bridge stops answering mid-run: the checkpoint in progress and
  every later checkpoint are `harness_error (reason: bounds_source_unavailable)` (`checkpoint_outcome`) until the
  Watchdog's relaunch, which `relaunch_due` asks for at the next flow boundary (5.9).
- `after_relaunch(state, properties_present=..., query_answers=...)` at the relaunch's `launched` gate: a bridge
  that answers again restores normal outcomes; one still down after the relaunch that followed a mid-run loss ends
  the run `aborted_harness (reason: bounds_source_unavailable)` with every unreached checkpoint `harness_error`
  with the same reason (5.9, SD-54). After a relaunch for any other cause it is the 7.5 check again.

The platform side reads bridge liveness through `ports.AccessPort.available()` and the file through the rig; the
results here are what the Orchestrator applies.

Choices where the spec is ambiguous or silent (reported):

1. The relaunch for a lost bridge runs at the next flow boundary (`relaunch_due`), never inside a flow, as the
   proactive relaunch of 10.2 does; it spends no app relaunch (10.2 counts crashes, hangs, OOMs and process exits),
   so `machine.RELAUNCH_BUDGETS` gains a cause that spends nothing. The flow in progress continues without bounds
   (locators fall to the next rung, 7.5), its checkpoints `harness_error (reason: bounds_source_unavailable)`.
2. Between the loss and the relaunch every checkpoint takes the outcome, whether or not its invariants need bounds
   (5.9: "every later checkpoint"), and the bridge answering again by itself does not lift it ("until the
   Watchdog's relaunch restores it"). After a failed 7.5 check only checkpoints whose invariants need bounds take
   it (7.5), selector `bounds_invariants`, until a later `launched` gate passes the check.
3. A bridge that was never up in the session (the 7.5 check failed) and is still down after a relaunch for another
   cause is the 7.5 outcome again, not the 5.9 abort, which 5.9 gives only to a bridge that died mid-run.
4. Each loss is counted (`losses`, for run health); a bridge restored by its relaunch that dies again later gets
   another relaunch, bounded by the run's wall budget.
5. The harness_error outcomes leave `detail` for the caller's Watchdog event id (4.13 requires `watchdog_event:
   <id>` in it); the result's `detail` describes the event.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import alerts
from .results import ControlResult, Outcome, SubsetOutcome, harness_abort_outcome

BRANCHES = ("A", "B")                          # DR-01 (11.8)
STATUSES = ("up", "unavailable", "lost")       # unavailable: failed the 7.5 check; lost: died mid-run (5.9)
REASON = "bounds_source_unavailable"
BOUNDS_SELECTOR = "bounds_invariants"          # checkpoints whose invariants need bounds (7.5, choice 2)


class BoundsError(ValueError):
    """A bounds-source call that does not fit the session's branch or state."""


@dataclass
class BoundsState:
    """The bounds source of one session (5.9, 7.5): the DR-01 branch, its status and the losses so far."""

    branch: str = "A"
    status: str = "up"
    lost_at: str | None = None                 # the checkpoint in progress when the bridge was lost
    losses: int = 0

    def __post_init__(self) -> None:
        if self.branch not in BRANCHES:
            raise BoundsError(f"DR-01 branch {self.branch!r}; one of {BRANCHES}")
        if self.status not in STATUSES:
            raise BoundsError(f"bounds status {self.status!r}; one of {STATUSES}")


def _outcome() -> Outcome:
    return Outcome("harness_error", REASON)


def jab_enablement(state: BoundsState, *, properties_present: bool, query_answers: bool) -> ControlResult | None:
    """The 7.5 Java Access Bridge row at gate `launched`: None under Branch B or when the file is present and one
    JAB query answers; otherwise `harness_error (reason: bounds_source_unavailable)` for the invariants that need
    bounds, the run continuing with locators on the next rung (choice 2)."""
    if state.branch == "B":
        return None
    if properties_present and query_answers:
        state.status, state.lost_at = "up", None
        return None
    state.status = "unavailable"
    why = "no .accessibility.properties in the user profile" if not properties_present else "no JAB query answered"
    return ControlResult("jab_enablement", False, "launched", outcomes=(SubsetOutcome(BOUNDS_SELECTOR, _outcome()),),
                         alert=alerts.harness_error(), evidence={"properties_present": properties_present,
                                                                 "query_answers": query_answers},
                         detail=f"Java Access Bridge not enabled ({why}): invariants that need bounds are "
                                f"{REASON}; locators fall to the next rung (7.5)")


def bridge_lost(state: BoundsState, *, checkpoint_id: str | None = None) -> ControlResult:
    """The bridge stopped answering mid-run (5.9): the checkpoint in progress and the rest of the flow take
    `harness_error (reason: bounds_source_unavailable)`, and a relaunch is due at the next flow boundary
    (choice 1). A loss already pending gives the same outcomes and counts once."""
    if state.branch == "B":
        raise BoundsError("Branch B has no bridge to lose (DR-01)")
    if state.status != "lost":
        state.status, state.lost_at = "lost", checkpoint_id
        state.losses += 1
    o = _outcome()
    return ControlResult("bounds_source", False, "running", checkpoint=o,
                         outcomes=(SubsetOutcome("in_progress", o), SubsetOutcome("rest_of_flow", o)),
                         alert=alerts.harness_error(), evidence={"lost_at": state.lost_at, "losses": state.losses},
                         detail="Java Access Bridge stopped answering mid-run: every checkpoint until the relaunch "
                                f"is {REASON}; relaunch at the next flow boundary (5.9)")


def checkpoint_outcome(state: BoundsState, *, needs_bounds: bool = True) -> Outcome | None:
    """The outcome a checkpoint takes from the bounds source, or None (choice 2): every checkpoint while the
    bridge is lost; those whose invariants need bounds while it is unavailable."""
    if state.status == "lost" or (state.status == "unavailable" and needs_bounds):
        return _outcome()
    return None


def relaunch_due(state: BoundsState) -> bool:
    """True at a flow boundary while a lost bridge waits for its relaunch (choice 1)."""
    return state.status == "lost"


def after_relaunch(state: BoundsState, *, properties_present: bool, query_answers: bool) -> ControlResult | None:
    """The bridge check at a relaunch's `launched` gate: restored is None; still down after the relaunch that
    followed a mid-run loss is `aborted_harness (reason: bounds_source_unavailable)` (5.9, SD-54); otherwise the
    7.5 check (choice 3)."""
    if state.branch == "B":
        return None
    if state.status != "lost" or (properties_present and query_answers):
        return jab_enablement(state, properties_present=properties_present, query_answers=query_answers)
    run, unreached = harness_abort_outcome(REASON, "the Java Access Bridge is still down after the relaunch")
    return ControlResult("bounds_source", False, "launched", run=run, unreached=unreached,
                         alert=alerts.aborted_harness(), actions=("teardown",),
                         evidence={"lost_at": state.lost_at, "losses": state.losses},
                         detail="Java Access Bridge still down after the relaunch (5.9)")
