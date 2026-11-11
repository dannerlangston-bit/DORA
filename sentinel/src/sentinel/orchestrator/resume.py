"""`orchestrator --resume <run_id>` and `--finalize`: the re-entry after an Orchestrator death and the local dead-man's
close (10.2 Restart and resume rules, 10.2 Dead-man local layer, 5.12, 5.14, 8.2, 8.9).

The Watchdog starts `orchestrator --resume <run_id>` when the Orchestrator died or hung and a restart is left
(`watchdog_loop`, `watchdog.orchestrator_died`), and `orchestrator --resume <run_id> --finalize` at the dead-man
deadline. Both open the shard's Run Store, the Orchestrator's own database (8.2: the Watchdog never opens it).

`resume(store, shard_id, shard_dir)`:

1. ingests `gateway_drops.jsonl` and `watchdog_events.jsonl` into `step_events` (`store.journal.ingest_journal`,
   idempotent: a second resume, or the shard import after it, adds nothing; 8.2 single writer);
2. rebuilds the shard's budgets from its step events (`budgets_from_events`): every Watchdog `resume` line is one
   Orchestrator restart, and a count past the 2 of 10.2 refuses the resume (`ResumeRefused`: the Rig Manager ships the
   shard as it is and the import writes `harness_error (reason: orchestrator_restart_budget)`, 8.2);
3. reads the last heartbeat (`heartbeat.json`) for the gate in force and the flow, step and checkpoint in progress;
4. keeps every recorded outcome; in `running`, gives the checkpoint in progress `harness_error (reason:
   orchestrator_died)` and the rest of the interrupted flow `not_run (reason: step_failed:<step>)`, the subsets of
   `watchdog.orchestrator_died`; flows with every outcome are skipped and the rest resume at the next flow boundary,
   each from its preconditions (a ladder never resumes mid-way);
5. re-enters the state machine at `launched` (every gate again, `paper_mode_asserted` included), at `provisioned` when
   the Orchestrator died there, at `teardown` with every outcome intact, or at `reported` to retry the ship.

`finalize(store, shard_id, state=, reached_running=)` writes the outcome `watchdog.dead_man_finalize` decides: a run
that reached `running` is `aborted_harness (reason: dead_man)` with `harness_error (reason: dead_man)` on every scoped
checkpoint without an outcome; before `running`, `env_error (gate: <state>, reason: timeout)` and
`complete_with_env_errors`, or for a Nightly or Calibration run `aborted_harness (reason: start_failed)`; then the shard
is handed to the Rig Manager to ship (`FinalizeResult.ship`).

`main(argv)` is the command: `orchestrator --resume RUN_ID --shard-dir DIR [--shard SHARD] [--db PATH] [--finalize
[--state STATE] [--reached-running]]`; it prints the plan as JSON; exit 0 done, 1 refused, 2 usage.

Choices where the spec is ambiguous or silent (reported):

1. A flow unit is what the run resumes by: a flow (`flow:<flow_id>`, its checkpoints on any surface), a census visit of
   one surface in one workspace (`census:<surface>/<workspace>`, its dimension variants together), a sweep ladder of one
   surface on one host (`sweep:<surface>/<host>`, the ascending pass with it), a split of one splitter
   (`split:<surface>/<splitter>`). The caller passes the units in run order; without them they are derived from the
   Run Store's scope in checkpoint-id order (`flow_units`).
2. The interrupted unit is the one the heartbeat names (its checkpoint, else its flow), else the first unit with some
   outcomes and some silent checkpoints, while the heartbeat says `running` and the unit still has silent checkpoints;
   its checkpoint in progress is the heartbeat's checkpoint when silent, else the unit's first silent checkpoint.
   Outside `running` no flow is interrupted: an app failure during a relaunch's gates already wrote the rest of its
   flow (10.2). `done_units` are the units with every outcome before this resume wrote anything.
3. Restarts: only the Watchdog starts `--resume`, and its journal `resume` line (reaction step 5, before the start) is
   the restart's record; `budgets_from_events` counts those that are not the dead-man's finalize (a detail starting
   `finalize`). App relaunches and hook host restarts are the Orchestrator's `relaunch` events naming `budget
   app_relaunch` or `budget hook_host` in their reason (`machine.RELAUNCH_BUDGETS`, as `pipeline/run.py` writes them).
4. A `kill_switch` Watchdog event later than every Orchestrator event means the Watchdog ran the kill switch's second
   half while the Orchestrator was dead: the resume writes nothing (the kill switch's `env_error` outranks
   `harness_error`, 1.10), resumes no flow and enters `teardown`, where the Orchestrator runs the 6.6 recovery.
5. `finalize` of a finished run writes nothing (the Orchestrator ended it after all); the shard row takes the same
   outcome as the run, and the run summary's `abort` records the local layer and the gate it closed at.
6. The rows the resume writes carry the shard id; a census or sweep checkpoint without a step id in the heartbeat is
   `step_failed:unknown`, as `orchestrator_died` writes it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..records import CheckpointOutcome, RunOutcome, StepEvent
from ..store.journal import GATEWAY_JOURNAL, WATCHDOG_JOURNAL, ingest_journal
from ..store.store import Store, StoreError, StoreRefused, utc
from ..taxonomy import checkpoint_parts
from .budgets import ShardBudgets
from .results import ControlResult, Outcome, write_unreached
from .watchdog import Heartbeat, dead_man_finalize, orchestrator_died

RUN_DB = "run.db"                                # 8.4 RIG layout
HEARTBEAT_FILE = "heartbeat.json"
_BUDGET = re.compile(r"\bbudget (\w+)")


class ResumeRefused(StoreError):
    """The resume cannot run: the run is finished or unknown, or the Orchestrator restart budget is spent (8.2)."""


@dataclass(frozen=True)
class FlowUnit:
    """What the run resumes by (choice 1): its id and its checkpoints in run order."""

    unit_id: str
    checkpoints: tuple[str, ...]


def unit_of(checkpoint_id: str) -> str:
    """The flow unit a checkpoint belongs to (choice 1)."""
    p = checkpoint_parts(checkpoint_id)
    first = p["qualifier"].split("/")[0]
    if p["kind"] == "flow":
        return f"flow:{first}"
    if p["kind"] == "sweep":
        return f"sweep:{p['surface']}/{p['host']}"
    return f"{p['kind']}:{p['surface']}/{first}"


def flow_units(checkpoint_ids: Iterable[str]) -> tuple[FlowUnit, ...]:
    """The units of a scope, in the order their first checkpoint comes (choice 1)."""
    units: dict[str, list[str]] = {}
    for cid in checkpoint_ids:
        units.setdefault(unit_of(cid), []).append(cid)
    return tuple(FlowUnit(u, tuple(c)) for u, c in units.items())


def budgets_from_events(events: Iterable[StepEvent]) -> ShardBudgets:
    """The shard's restart budgets as its step events spent them (choice 3)."""
    b = ShardBudgets()
    for e in events:
        detail = (e.reason or "").split(": ", 1)[1] if ": " in (e.reason or "") else ""
        if e.action == "resume" and not detail.startswith("finalize"):
            b.used_orchestrator += 1
        elif e.action == "relaunch" and e.source == "orchestrator":
            m = _BUDGET.search(e.reason or "")
            if m and m.group(1) == "app_relaunch":
                b.used_app += 1
            elif m and m.group(1) == "hook_host":
                b.used_hook_host += 1
    return b


def read_heartbeat(path: str | Path) -> Heartbeat | None:
    """The last heartbeat the Orchestrator wrote; None when missing or unreadable."""
    try:
        return Heartbeat(**json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ResumePlan:
    """What a resume did and where the Orchestrator goes on (module docstring). `written` lists the checkpoints this
    call gave an outcome; `ingested` the journal rows new to the store, by source."""

    run_id: str
    shard_id: str
    enter_at: str
    ingested: dict[str, int]
    written: tuple[str, ...]
    done_units: tuple[str, ...]
    interrupted: str | None
    next_units: tuple[FlowUnit, ...]
    budgets: ShardBudgets
    kill_switch_pending: bool = False
    decision: ControlResult | None = field(default=None, repr=False)

    @property
    def noop(self) -> bool:
        """True when this resume changed nothing in the store (a second resume of the same death)."""
        return not self.written and not any(self.ingested.values())

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("decision", "budgets", "next_units")}
        d["next_units"] = [u.unit_id for u in self.next_units]
        d["budgets"] = {"orchestrator": self.budgets.used_orchestrator, "app": self.budgets.used_app,
                        "hook_host": self.budgets.used_hook_host}
        return d


def _run_row(store: Store, shard_id: str) -> tuple[str, str, bool]:
    """The shard's run id, run type and whether the run is finished."""
    row = store.query("SELECT r.run_id, r.run_type, r.lifecycle FROM shards s JOIN runs r ON r.run_id = s.run_id "
                      "WHERE s.shard_id = ?", (shard_id,))
    if not row:
        raise ResumeRefused(f"unknown shard {shard_id} in this Run Store (8.3)")
    return row[0]["run_id"], row[0]["run_type"], row[0]["lifecycle"] == "finished"


def silent_record(store: Store, run_id: str, cid: str, o: Outcome) -> CheckpointOutcome:
    """The record giving `o` to a scoped checkpoint of `run_id`, as `Store.fill_unreached` builds an unreached one
    (the checkpoint's own columns and the scope's persona)."""
    c = store.query("SELECT c.workspace_id, c.data_mode, c.tier, c.flow_id, c.step_id, s.persona FROM checkpoints c "
                    "JOIN run_scope s ON s.checkpoint_id = c.checkpoint_id WHERE s.run_id = ? AND c.checkpoint_id = ?",
                    (run_id, cid))[0]
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=o.outcome, reason=o.reason, gate=o.gate,
                             detail=o.detail, workspace_id=c["workspace_id"], persona=c["persona"],
                             data_mode=c["data_mode"], tier=c["tier"], flow_id=c["flow_id"], step_id=c["step_id"])


def _enter_at(state: str | None) -> str:
    if state is None or state == "provisioned":
        return "provisioned"
    return state if state in ("teardown", "reported") else "launched"


def _kill_switch_pending(events: Sequence[StepEvent], shard_id: str) -> bool:
    mine = [e for e in events if e.shard_id == shard_id]
    last_orch = max((e.seq for e in mine if e.source == "orchestrator"), default=0)
    return any(e.source == "watchdog" and e.action == "kill_switch" and e.seq > last_orch for e in mine)


def _interrupted(units: Sequence[FlowUnit], hb: Heartbeat | None, silent: set[str]) -> tuple[FlowUnit, str] | None:
    """The interrupted unit and its checkpoint in progress (choice 2)."""
    if hb is None or hb.state != "running":
        return None
    named = next((u for u in units if hb.checkpoint_id in u.checkpoints), None) or \
        next((u for u in units if hb.flow_id is not None and u.unit_id == f"flow:{hb.flow_id}"), None) or \
        next((u for u in units if any(c in silent for c in u.checkpoints) and
              not all(c in silent for c in u.checkpoints)), None)
    left = [c for c in named.checkpoints if c in silent] if named is not None else []
    if named is None or not left:
        return None
    return named, hb.checkpoint_id if hb.checkpoint_id in left else left[0]


def resume(store: Store, shard_id: str, shard_dir: str | Path, *, heartbeat: Heartbeat | None = None,
           units: Sequence[FlowUnit] | None = None) -> ResumePlan:
    """`orchestrator --resume`: ingest, rebuild, record the interrupted flow, plan the re-entry (module docstring).
    `heartbeat` defaults to the shard directory's `heartbeat.json`; `units` to `flow_units` of the scope."""
    shard_dir = Path(shard_dir)
    run_id, _, finished = _run_row(store, shard_id)
    if finished:
        raise ResumeRefused(f"run {run_id} is finished: nothing to resume")
    ingested = {source: ingest_journal(store, shard_id, shard_dir / name, source).written
                for source, name in (("gateway_hook", GATEWAY_JOURNAL), ("watchdog", WATCHDOG_JOURNAL))}
    events = store.step_events(run_id)
    budgets = budgets_from_events(e for e in events if e.shard_id == shard_id)
    if budgets.used_orchestrator > budgets.orchestrator_restarts:
        raise ResumeRefused(f"{shard_id}: Orchestrator restart {budgets.used_orchestrator} of "
                            f"{budgets.orchestrator_restarts}: the restart budget is spent, the Rig Manager ships the "
                            "shard as it is (8.2)")
    hb = heartbeat if heartbeat is not None else read_heartbeat(shard_dir / HEARTBEAT_FILE)
    scope = [r[0] for r in store.query("SELECT checkpoint_id FROM run_scope WHERE run_id = ? ORDER BY checkpoint_id",
                                       (run_id,))]
    units = tuple(units) if units is not None else flow_units(scope)
    have = {o.checkpoint_id for o in store.outcomes(run_id)}
    silent = set(scope) - have
    pending = _kill_switch_pending(events, shard_id)
    enter_at = "teardown" if pending else _enter_at(hb.state if hb is not None else None)
    state = hb.state if hb is not None and hb.state in ("running", "teardown") else "running"
    decision = orchestrator_died(ShardBudgets(used_orchestrator=max(0, budgets.used_orchestrator - 1)),
                                 step_id=hb.step_id if hb is not None else None, state=state)
    written: list[str] = []
    done = tuple(u.unit_id for u in units if not any(c in silent for c in u.checkpoints))
    hit = None if pending or enter_at != "launched" else _interrupted(units, hb, silent)
    if hit is not None:
        unit, current = hit
        subsets = {s.selector: s.outcome for s in decision.outcomes}
        for cid in [current] + [c for c in unit.checkpoints if c in silent and c != current]:
            o = subsets["in_progress"] if cid == current else subsets["rest_of_flow"]
            store.write_checkpoint(silent_record(store, run_id, cid, o), shard_id=shard_id)
            written.append(cid)
            silent.discard(cid)
    resumable = () if pending or enter_at in ("teardown", "reported") else \
        tuple(u for u in units if u.checkpoints and all(c in silent for c in u.checkpoints))
    return ResumePlan(run_id, shard_id, enter_at, ingested, tuple(written), done,
                      hit[0].unit_id if hit is not None else None, resumable, budgets, pending, decision)


@dataclass(frozen=True)
class FinalizeResult:
    """What `finalize` wrote: the run outcome and reason, the checkpoints filled, and whether the shard ships now."""

    run_id: str
    shard_id: str
    status: str | None
    reason: str | None
    filled: tuple[str, ...] = ()
    ship: bool = True
    decision: ControlResult | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "decision"}


def finalize(store: Store, shard_id: str, *, state: str, reached_running: bool,
             at: str | None = None) -> FinalizeResult:
    """`orchestrator --resume <run_id> --finalize` at the dead-man deadline (module docstring, choice 5)."""
    run_id, run_type, finished = _run_row(store, shard_id)
    if finished:
        return FinalizeResult(run_id, shard_id, None, None)
    result = dead_man_finalize(state, reached_running, run_type=run_type)
    verdict = result.run
    if verdict is None:
        raise ResumeRefused(f"the dead-man finalize of {run_id} decided no run outcome")
    filled = write_unreached(store, run_id, result)
    store.end_shard(shard_id, verdict.status, verdict.reason)
    store.end_run(RunOutcome(run_id, run_type, verdict.status, result.gate or state, reason=verdict.reason,
                             ended_at=utc(at)),
                  summary={"abort": {**(store.run_summary(run_id).get("abort") or {}),
                                     "dead_man_layer": "local", "closed_at_gate": state, "detail": result.detail}})
    return FinalizeResult(run_id, shard_id, verdict.status, verdict.reason, tuple(filled), True, result)


# -- the command ------------------------------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator", description="Resume or finalize a shard after an Orchestrator "
                                                                 "death (10.2).")
    p.add_argument("--resume", required=True, metavar="RUN_ID")
    p.add_argument("--shard-dir", required=True, type=Path)
    p.add_argument("--shard", help="the shard id (default: the run's only shard in the Run Store)")
    p.add_argument("--db", type=Path, help="the Run Store (default: <shard-dir>/run.db)")
    p.add_argument("--finalize", action="store_true", help="write the dead-man outcome and ship (10.2)")
    p.add_argument("--state", help="the gate in force (default: the last heartbeat's)")
    p.add_argument("--reached-running", action="store_true", help="the run reached running (default: by heartbeat)")
    return p


def main(argv: Sequence[str] | None = None, *, out=None) -> int:
    """The command (module docstring): exit 0 done, 1 refused, 2 usage."""
    out = out or sys.stdout
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return 0 if not e.code else 2
    try:
        store = Store(a.db or a.shard_dir / RUN_DB)
    except (StoreError, OSError) as e:
        print(f"orchestrator --resume: cannot open the Run Store: {e}", file=sys.stderr)
        return 1
    try:
        shards = [r[0] for r in store.query("SELECT shard_id FROM shards WHERE run_id = ?", (a.resume,))]
        shard = a.shard or (shards[0] if len(shards) == 1 else None)
        if shard is None or shard not in shards:
            print(f"orchestrator --resume: name the shard of run {a.resume} (--shard); this Run Store has "
                  f"{shards or 'none'}", file=sys.stderr)
            return 2 if shards else 1
        if a.finalize:
            hb = read_heartbeat(a.shard_dir / HEARTBEAT_FILE)
            state = a.state or (hb.state if hb is not None else "provisioned")
            reached = a.reached_running or (hb is not None and hb.state in ("running", "teardown", "reported"))
            doc = finalize(store, shard, state=state, reached_running=reached).to_dict()
        else:
            doc = resume(store, shard, a.shard_dir).to_dict()
    except (StoreRefused, ResumeRefused) as e:
        print(f"orchestrator --resume: refused: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(doc, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
