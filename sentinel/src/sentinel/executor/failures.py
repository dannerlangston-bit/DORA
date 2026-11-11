"""Exceptions into the closed taxonomy: store and artifact failures (8.9), every other unhandled exception (10.1),
the heartbeat write (10.2), and fault injection for the simulated pipeline.

`map_exception(exc, step_id=..., ...)` returns the `ControlResult` the Orchestrator applies:

- `ArtifactPersistError` (a write or read-back that failed twice, 8.4): the checkpoint `harness_error (reason:
  artifact_persist_failed)`; the run can no longer be `complete` and ends `aborted_harness (reason:
  artifact_persist_failed)` at teardown (`FailureLedger.at_teardown`) unless the Watchdog's free-space check ends it
  first as `disk_full`; with `enospc` the result also asks for that immediate check (`free_space_check`, 10.2).
- `RedactionRefused` (an image without a passing redaction header): `harness_error (reason:
  redaction_version_missing)` (8.9).
- `StoreWriteFailed` (the Run Store writer could not commit): the shard `aborted_harness (reason:
  store_write_failed)` and every remaining checkpoint `harness_error (reason: store_write_failed)`, the run's abort
  reason (8.9, 1.10); the ledger already holds the rows for replay.
- `SecretLeaked` (the put hook found a secret's plaintext in a text artifact): `secret_scan.leak_result`, the run
  `aborted_harness (reason: secret_leaked)` with the report withheld (6.9).
- `HeartbeatUnwritable`: `watchdog.heartbeat_failed` (`disk_full` path on ENOSPC, else `heartbeat_unwritable`, 10.2).
- anything else: `harness_error (reason: exception:<class>)` with the stack trace, the last screenshot and the
  Watchdog event id in `evidence` (10.1); an `OSError` with ENOSPC also asks for the free-space check (10.2).

Every result that does not end the run is the 10.1 harness row: the step `failed`, the checkpoint in progress (the
one being captured, or the one the step led to) gets the outcome (`in_progress`), the rest of the flow `not_run
(reason: step_failed:<step>)` (`rest_of_flow`), and the run resumes at the next flow boundary (`resume_next_flow`).
When a higher-precedence detector already gave that checkpoint an outcome (`pending`: an `env_error` or the app
failure), the exception is evidence on it, not a second outcome (10.1, F27).

`FaultInjector` raises chosen exceptions at named sites (`SITES`) for the simulated pipeline and the tests, and
`guard` runs one piece of checkpoint work through it and `map_exception`.

Choices where the spec is ambiguous (reported):

1. A store or artifact failure that does not end the run follows the 10.1 harness row (step `failed`, rest of the
   flow `not_run (reason: step_failed:<step>)`, resume at the next flow boundary), since 10.1 lists "SQLite or artifact
   store error" among harness failures and 8.9 names only the checkpoint's own outcome.
2. A put that refuses a secret's plaintext ends the run like the teardown scan's hit (`leak_result`, location
   `artifact_put`): 6.9 stops uploads at once on a mid-run hit, and nothing was written.
3. `at_teardown` leaves a run that already stopped with an `aborted_*` verdict as it is: the first stopping result
   ends the shard (`Orchestrator.apply`), and `disk_full` is the case 8.9 names.
4. The stack trace kept in `evidence` is capped at `TRACE_LIMIT` characters (the head and the tail are kept); the
   caller stores it as a text artifact, which passes the plaintext scan like every text artifact (6.9).
"""
from __future__ import annotations

import builtins
import errno
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from ..orchestrator import alerts
from ..orchestrator.results import (ControlResult, Outcome, RunVerdict, SubsetOutcome, harness_abort_outcome,
                                    rest_of_flow)
from ..orchestrator.watchdog import HeartbeatUnwritable, heartbeat_failed, resolve_detectors
from ..store.artifacts import ArtifactPersistError, RedactionRefused, SecretLeaked
from ..store.secret_scan import SecretHit, leak_result
from ..store.store import StoreWriteFailed

T = TypeVar("T")
TRACE_LIMIT = 16_000
# where the simulated pipeline can inject a fault (FaultInjector)
SITES = ("artifact_put", "store_write", "capture", "evaluate", "heartbeat", "step")


def _trace(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(text) <= TRACE_LIMIT:
        return text
    half = TRACE_LIMIT // 2
    return text[:half] + "\n...\n" + text[-half:]


def _evidence(exc: BaseException, checkpoint_id: str | None, last_screenshot: str | None,
              watchdog_event_id: str | None) -> dict[str, Any]:
    return {"exception": type(exc).__name__, "message": str(exc), "stack_trace": _trace(exc),
            "last_screenshot": last_screenshot, "watchdog_event_id": watchdog_event_id, "checkpoint_id": checkpoint_id}


def _in_flow(control: str, outcome: Outcome, step_id: str | None, evidence: dict[str, Any], *,
             pending: Outcome | None, actions: tuple[str, ...] = ()) -> ControlResult:
    """The 10.1 harness row (choice 1), with a higher-precedence pending outcome kept (F27)."""
    shown = outcome
    if pending is not None:
        shown = resolve_detectors([pending, outcome])
        if shown is not outcome:
            evidence = {**evidence, "superseded": f"{outcome.outcome} ({outcome.reason})"}
    return ControlResult(control, False, "running", checkpoint=shown,
                         outcomes=(SubsetOutcome("in_progress", shown), rest_of_flow(step_id or "unknown")),
                         actions=actions + ("resume_next_flow",), alert=alerts.harness_error(), step="failed",
                         detail=f"{evidence['exception']}: {evidence['message']}"[:500], evidence=evidence)


def map_exception(exc: BaseException, *, step_id: str | None, checkpoint_id: str | None = None,
                  last_screenshot: str | None = None, watchdog_event_id: str | None = None,
                  pending: Outcome | None = None) -> ControlResult:
    """The outcome of one exception raised while running `step_id` (module docstring). `checkpoint_id` is the
    checkpoint in progress, `last_screenshot` the latest capture's sha256 and `watchdog_event_id` the event the
    Watchdog journaled for it; `pending` is an outcome a higher-precedence detector already gave that checkpoint."""
    ev = _evidence(exc, checkpoint_id, last_screenshot, watchdog_event_id)
    if isinstance(exc, HeartbeatUnwritable):
        return replace(heartbeat_failed(exc), evidence=ev)
    if isinstance(exc, StoreWriteFailed):
        run, un = harness_abort_outcome("store_write_failed")
        return ControlResult("store_write", False, "running", run=run, unreached=un, alert=alerts.aborted_harness(),
                             actions=("teardown",), step="failed", evidence=ev,
                             detail="the Run Store writer cannot commit; the ledger holds the rows for replay")
    if isinstance(exc, SecretLeaked):
        hits = [SecretHit(ref, "artifact_put", "plain") for ref in exc.refs]
        return replace(leak_result(hits, evidence=ev), gate="running", step="failed")
    if isinstance(exc, ArtifactPersistError):
        return _in_flow("artifact_persist", Outcome("harness_error", "artifact_persist_failed"), step_id, ev,
                        pending=pending, actions=("free_space_check",) if exc.enospc else ())
    if isinstance(exc, RedactionRefused):
        return _in_flow("redaction", Outcome("harness_error", "redaction_version_missing"), step_id, ev,
                        pending=pending)
    enospc = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
    return _in_flow("exception", Outcome("harness_error", f"exception:{type(exc).__name__}"), step_id, ev,
                    pending=pending, actions=("free_space_check",) if enospc else ())


class FailureLedger:
    """What the shard's failures leave for teardown (8.9): an artifact write that failed twice means the run cannot
    be `complete` and ends `aborted_harness (reason: artifact_persist_failed)` at teardown."""

    def __init__(self) -> None:
        self.persist_failed: list[str] = []

    def note(self, r: ControlResult, checkpoint_id: str | None = None) -> ControlResult:
        """Record a mapped result; returns it unchanged."""
        if r.checkpoint is not None and r.checkpoint.reason == "artifact_persist_failed":
            self.persist_failed.append(checkpoint_id or str(r.evidence.get("checkpoint_id")))
        return r

    @property
    def run_cannot_complete(self) -> bool:
        return bool(self.persist_failed)

    def at_teardown(self, current: RunVerdict | None = None) -> ControlResult | None:
        """At teardown: `aborted_harness (reason: artifact_persist_failed)` when a persist failed and the run has not
        already stopped with an `aborted_*` verdict (choice 3); None otherwise."""
        if not self.persist_failed or (current is not None and current.aborted):
            return None
        run, un = harness_abort_outcome("artifact_persist_failed")
        return ControlResult("artifact_persist", False, "teardown", run=run, unreached=un,
                             alert=alerts.aborted_harness(),
                             detail=f"artifact persist failed on {len(self.persist_failed)} checkpoint(s): "
                                    + ", ".join(self.persist_failed[:5]),
                             evidence={"checkpoints": list(self.persist_failed)})


# -- fault injection ------------------------------------------------------------------------------------------------
@dataclass
class Fault:
    """Raise `make()` at `site` for `key` (a checkpoint or step id; None matches any), `times` times."""

    site: str
    make: Callable[[], BaseException]
    key: str | None = None
    times: int = 1

    def __post_init__(self) -> None:
        if self.site not in SITES:
            raise ValueError(f"fault site {self.site!r}; one of {SITES}")
        if self.times < 1:
            raise ValueError("a fault fires at least once")


@dataclass
class FaultInjector:
    """Faults for the simulated pipeline (8.9, 10.1 tests): `check(site, key)` raises the first live match."""

    faults: list[Fault] = field(default_factory=list)
    fired: list[tuple[str, str | None, str]] = field(default_factory=list)

    def add(self, fault: Fault) -> FaultInjector:
        self.faults.append(fault)
        return self

    def check(self, site: str, key: str | None = None) -> None:
        """Raise the fault planned for this site and key, if one is left."""
        for f in self.faults:
            if f.site == site and f.times > 0 and (f.key is None or f.key == key):
                f.times -= 1
                exc = f.make()
                self.fired.append((site, key, type(exc).__name__))
                raise exc

    # the 8.9 and 10.1 cases by name
    @staticmethod
    def artifact_persist(key: str | None = None, *, enospc: bool = False) -> Fault:
        """The capture's artifact write fails twice (`artifact_persist_failed`; ENOSPC asks for the disk check)."""
        return Fault("artifact_put", lambda: ArtifactPersistError("injected: write and read-back failed twice",
                                                                  enospc=enospc), key)

    @staticmethod
    def redaction_missing(key: str | None = None) -> Fault:
        """An image reaches the writer without a redaction header (`redaction_version_missing`)."""
        return Fault("artifact_put", lambda: RedactionRefused("injected: no redaction header"), key)

    @staticmethod
    def store_write(key: str | None = None) -> Fault:
        """The Run Store writer cannot commit (`store_write_failed`)."""
        return Fault("store_write", lambda: StoreWriteFailed("injected: database is locked after 3 retries"), key)

    @staticmethod
    def exception(site: str, exc_type: type[BaseException] = KeyError, key: str | None = None) -> Fault:
        """An unhandled exception of `exc_type` at `site` (`exception:<class>`)."""
        return Fault(site, lambda: exc_type(f"injected at {site}"), key)


def guard(work: Callable[[], T], *, step_id: str | None, checkpoint_id: str | None = None,
          site: str | None = None, faults: FaultInjector | None = None, ledger: FailureLedger | None = None,
          last_screenshot: str | None = None, watchdog_event_id: str | None = None,
          pending: Outcome | None = None) -> tuple[T | None, ControlResult | None]:
    """Run one piece of checkpoint work (capture, evaluate, write): `(value, None)`, or `(None, result)` when it raised
    (an injected fault at `site` included); the result is noted in `ledger`."""
    try:
        if faults is not None and site is not None:
            faults.check(site, checkpoint_id or step_id)
        return work(), None
    except Exception as exc:                       # 10.1: every unhandled exception has an outcome
        r = map_exception(exc, step_id=step_id, checkpoint_id=checkpoint_id, last_screenshot=last_screenshot,
                          watchdog_event_id=watchdog_event_id, pending=pending)
        if ledger is not None:
            ledger.note(r, checkpoint_id)
        return None, r


def faults_from(specs: Iterable[tuple[str, str | None, str]]) -> FaultInjector:
    """A `FaultInjector` from `(case, key, option)` triples, the form a simulated scenario declares: case
    `artifact_persist` (option `enospc` or ''), `redaction_missing`, `store_write`, or `exception:<site>` with the
    exception class name as option (a builtin exception)."""
    inj = FaultInjector()
    for case, key, option in specs:
        if case == "artifact_persist":
            inj.add(FaultInjector.artifact_persist(key, enospc=option == "enospc"))
        elif case == "redaction_missing":
            inj.add(FaultInjector.redaction_missing(key))
        elif case == "store_write":
            inj.add(FaultInjector.store_write(key))
        elif case.startswith("exception:"):
            exc_type = getattr(builtins, option, None)
            if not (isinstance(exc_type, type) and issubclass(exc_type, Exception)):
                raise ValueError(f"{option!r} is not a builtin exception")
            inj.add(FaultInjector.exception(case.split(":", 1)[1], exc_type, key))
        else:
            raise ValueError(f"unknown fault case {case!r}")
    return inj
