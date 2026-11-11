"""What the Orchestrator reads at a flow or step boundary, and the pure checks over it (5.2, 5.14, 6.4, 6.6, 10.2,
10.7, 11.2).

`Orchestrator.flow_boundary` (machine.py) runs one check sequence before every unit of work (a flow, a census
visit, a sweep ladder: "the next flow boundary" of 10.2) and returns the first result that stops the run:

1. the kill switch's human channels and automatic trip-wires (6.6): the file flag and the named pipe, a new
   Working or Filled row against the run-start snapshot, an `order_event` window, an Order Confirmation dialog
   visible more than 30 s with no owning step, ten gateway drops or refusals, an attempted send;
2. the Watchdog's reverse heartbeat `watchdog.json`: older than 30 s or a dead PID is `watchdog_dead` (10.2, 5.14);
3. the Watchdog's free-space result: below 2 GB `disk_full`, below the rig floor a run-health warning (10.2);
4. the run budgets (5.2): the wall budget less the report margin (`wall_clock`, 11.2), the 6.4 rate caps
   (`rate_cap`) and a superseding RC build (`superseded`);
5. the per-run model spend ceiling (`dollar_cap`, 10.7);
6. the model availability re-check, once per flow; a recovery is logged (10.7);
7. the paper-mode assertion, due before every flow (6.4 Schedule);
8. the proactive relaunch after 40 flows or 90 minutes of app uptime, a step event that spends no budget (10.2).

`Orchestrator.step_boundary` runs before every step inside a flow: the trip-wires, the flow's own budget (3 x its
measured p50, 5 minutes until measured, 11.2) and the paper-mode assertion when 6.4 makes it due (before an
order-stage step, after a session dialog, or on the two-minute timer while the main frame is foreground and no
modal is up, a deferral past five minutes taking the unreadable path).

Choices (reported):
- The per-flow budget is checked at step boundaries, inside the flow it bounds; a flow boundary starts the next
  flow's timer. A flow past its budget ends (`end_flow`) with its remaining checkpoints `not_run (reason:
  budget)`, and the run continues (11.2 names no run outcome for it).
- A flow boundary reads the Monitor for the new-row trip-wire unless the caller passes a read; a step boundary
  never navigates to the Monitor and uses a read only when one is passed. A read that is unreadable or disagrees
  with Copy All trips nothing (the post-run proof decides on it). A declared send of the gated branch (6.7) is not
  a new Working row; a new Filled row always trips.
- A trip-wire at a flow boundary quarantines the flow that ran last, the one the row or window came from.
- Only units that are flows count toward the 40 flows of the proactive relaunch; census visits and sweep ladders
  count toward its 90 minutes of uptime only.
- The rig floor for the disk warning defaults to 20 GB (R7's lower bound, 8.6) when the caller gives none; the
  warning is recorded once per shard.
- A `working_orders` flow precondition above its `expect` (4.7, L-FLW-12) is `aborted_safety (reason:
  unexpected_working_order)` at gate `running` with the kill switch and an S1 incident, as A8 and the
  `fixture_verified` order case are.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import alerts
from .killswitch import TripWires, check_tripwires, kill_switch
from .results import ControlResult, Outcome, SubsetOutcome, safety_abort_outcome
from .rig import OrderBook, OrderRow

DISK_FLOOR_BYTES = 20 * 1024 ** 3              # R7 (12.7, 8.6): the larger of 20 GB and twice the shard size
FLOW_BUDGET_FACTOR = 3.0                       # 11.2: a flow's budget is 3 x its measured p50
FLOW_BUDGET_DEFAULT_S = 300.0                  # 11.2: 5 minutes until measured


@dataclass(frozen=True)
class BoundaryObservations:
    """What the Executor, the Window Watcher, the Watchdog, the queue and the model client report at a boundary.

    Every field defaults to "nothing seen": a field left out is not checked, except the Watchdog's reverse heartbeat,
    which falls back to the Orchestrator's `watchdog_status`, and the Monitor read, which a flow boundary takes
    itself (module docstring).
    """

    tripwires: TripWires = TripWires()            # the human channels and the Watcher's wires (6.6 Triggers)
    orders: OrderBook | None = None               # a Monitor read for the new-row trip-wire
    declared_sends: frozenset[str] = frozenset()  # Working row keys of declared sends (gated branch, 6.7)
    order_dialog_visible: bool | None = None      # the Order Confirmation dialog is visible now (5.4.6)
    watchdog: tuple[float, bool] | None = None    # watchdog.json age in seconds and Watchdog PID alive (10.2)
    free_bytes: int | None = None                 # the Watchdog's free-space result (10.2); None reads rig.free_bytes()
    disk_floor_bytes: int = DISK_FLOOR_BYTES      # the rig floor of 8.6 (R7)
    superseded: bool = False                      # a newer RC build supersedes the run (11.2 RC respin)
    model_endpoint_ok: bool | None = None         # the once-per-flow model endpoint re-check (10.7)
    main_foreground: bool = True                  # the main frame is the foreground window (6.4 timer)
    modal_up: bool = False                        # a modal is up (6.4 timer)


def new_order_rows(start: OrderBook | None, orders: OrderBook | None,
                   declared_sends: frozenset[str] = frozenset()) -> tuple[OrderRow, ...]:
    """The new Working rows that are not declared sends, and the new Filled rows, of `orders` against the run-start
    snapshot (6.6 Triggers); none when either read is missing, unreadable or disagrees with Copy All."""
    if start is None or orders is None or not (start.readable and start.copy_all_agrees and orders.readable
                                               and orders.copy_all_agrees):
        return ()
    def new(grid: str) -> list[OrderRow]:
        before = {r.key for r in start.rows(grid)}
        return [r for r in orders.rows(grid) if r.key not in before]
    return tuple(r for r in new("working") if r.key not in declared_sends) + tuple(new("filled"))


@dataclass
class DialogTimer:
    """How long the Order Confirmation dialog has been visible across boundaries (6.6: more than 30 s with no owning
    step trips the kill switch)."""

    since: float | None = None

    def observe(self, visible: bool, now: float) -> float:
        """Record whether the dialog is visible at `now`; returns the seconds it has been open (0 when closed)."""
        if not visible:
            self.since = None
            return 0.0
        if self.since is None:
            self.since = now
        return now - self.since


def tripwire_result(wires: TripWires, *, gate: str, running_flow: str | None, drop_limit: int = 10,
                    new_rows: tuple[OrderRow, ...] = ()) -> ControlResult | None:
    """The kill switch decision for the first trip-wire that fires (6.6), or None; the new rows are named in the
    evidence."""
    trigger = check_tripwires(wires, drop_limit=drop_limit)
    if trigger is None:
        return None
    r = kill_switch(trigger, gate=gate, running_flow=running_flow)
    return replace(r, evidence={**r.evidence, "new_rows": [x.key for x in new_rows]}) if new_rows else r


# -- the flow's own budget (11.2) ---------------------------------------------------------------------------------
def flow_budget_s(p50_s: float | None) -> float:
    """A flow's budget: 3 x its measured p50, or 5 minutes until measured (11.2)."""
    if p50_s is None:
        return FLOW_BUDGET_DEFAULT_S
    if p50_s <= 0:
        raise ValueError(f"a measured p50 is positive, got {p50_s}")
    return FLOW_BUDGET_FACTOR * p50_s


@dataclass(frozen=True)
class FlowTimer:
    """The running flow, when it started on the Orchestrator's monotonic clock and its budget."""

    flow_id: str
    started_mono: float
    budget_s: float = field(default=FLOW_BUDGET_DEFAULT_S)

    def elapsed(self, now: float) -> float:
        return now - self.started_mono

    def over(self, now: float) -> bool:
        """The flow has used its whole budget."""
        return self.elapsed(now) >= self.budget_s


def flow_budget_cut(timer: FlowTimer, now: float) -> ControlResult:
    """A flow past its budget: it ends, its remaining checkpoints `not_run (reason: budget)`; the run continues."""
    return ControlResult("flow_budget", False, "running",
                         outcomes=(SubsetOutcome("rest_of_flow", Outcome("not_run", "budget")),), actions=("end_flow",),
                         detail=f"flow {timer.flow_id} ran {timer.elapsed(now):.0f} s, past its budget of "
                                f"{timer.budget_s:.0f} s (11.2)",
                         evidence={"flow_id": timer.flow_id, "elapsed_s": timer.elapsed(now),
                                   "budget_s": timer.budget_s})


# -- flow preconditions (4.7) -------------------------------------------------------------------------------------
def working_orders_precondition(count: int, *, expect: int = 0, flow_id: str | None = None) -> ControlResult | None:
    """The `working_orders` precondition of a flow (4.7, L-FLW-12): above `expect` is `aborted_safety (reason:
    unexpected_working_order)` with the kill switch; None when it holds."""
    if count < 0:
        raise ValueError(f"a Working row count is not negative, got {count}")
    if count <= expect:
        return None
    run, un = safety_abort_outcome("running", "unexpected_working_order", detail=f"precondition working_orders: "
                                                                                 f"{count} > {expect}")
    return ControlResult("precondition", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
                         actions=("kill_switch",), incident="S1",
                         detail=f"{count} Working row(s) before flow {flow_id or '?'}, {expect} expected; "
                                "a sent order is never fixture drift",
                         evidence={"working_orders": count, "expect": expect, "flow_id": flow_id})
