"""Frozen Branch B, OnDemand: entry, pause and clock check, the buffering-timeout counter, the replay scope and the
fallback to masked_live (7.9; 7.8 POP-12; 4.9 `ondemand_virtual_account`; 12.7 N4).

- `enter(controls, timestamp, run, shard_id=...)`: open OnDemand, set the checked-in timestamp, Go, wait for buffering
  under the 90 s hard timeout (POP-12), Pause, OCR the OnDemand clock and assert it equals the timestamp
  (`check_clock`: a mismatch is `env_error (gate: fixture_verified, reason: data_epoch_mismatch)`), park the mouse.
  Settling and the capture follow in the capture path. `OnDemandControls` is the port the rig implements.
- `OnDemandRun`: the timeout counter. A buffering timeout at setup is `env_error (gate: fixture_verified, reason:
  ondemand_timeout)` for the checkpoint being set up; three consecutive timeouts in one shard abandon OnDemand for the
  run: every remaining OnDemand checkpoint takes that outcome and the on-call channel gets one message.
- The allowlist entry for the OnDemand virtual account, valid only while the toolbar is visible, is
  `identity.judge_identity(..., ondemand_toolbar=...)`; the id without the toolbar is `aborted_safety`.
- `replay_scope(surfaces, replaying)`: checkpoints on gadgets that do not replay are `not_run (reason: scope)` while
  inside OnDemand (Phase 0 lists the gadgets that replay).
- `frozen_fallback_active(branch, ondemand_usable)` and `effective_data_mode(...)`: when DR-02 is Branch B and OnDemand
  is not usable on the test login (the entry needs Live Trading without the risk owner's written approval, or Phase 0
  found it unreachable), frozen checkpoints fall back to masked_live; the larger rule set of that fallback is config
  (regions files with `rule` regions and the daily chart aggregation), not code.

Choices where the spec is ambiguous or silent (reported):

1. The OnDemand clock format is a Phase 0 read (7.14 check 9); `check_clock` parses the OCR text with the pinned format
   (`DEFAULT_CLOCK_FORMAT` until then) and compares it with the timestamp rendered in America/New_York (the app's
   pinned time zone, 7.1) at the format's precision, so zero padding does not matter. An unreadable or unparseable
   clock cannot confirm the epoch and takes the mismatch outcome.
2. The first and second timeouts of a streak are run-health lines without an alert; the abandonment posts the one
   on-call message 7.9 names, once per run however many shards reach three. A successful buffering resets the shard's
   streak ("consecutive"). The abandonment is run-wide, so later entries on any shard return the remaining-checkpoint
   outcome without trying OnDemand again and without another alert.
3. "The remaining OnDemand checkpoints" are the frozen checkpoints not yet given an outcome (under Branch B every
   frozen checkpoint is an OnDemand one): the subset selector `frozen` that N4 uses.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from . import alerts
from .results import ControlResult, Outcome, SubsetOutcome

BUFFER_TIMEOUT_S = 90.0                      # POP-12
ABANDON_AFTER = 3                            # 7.9
DEFAULT_CLOCK_FORMAT = "%m/%d/%Y %H:%M:%S"   # choice 1, until Phase 0 pins it
NEW_YORK = ZoneInfo("America/New_York")
GATE = "fixture_verified"


class OnDemandControls(Protocol):
    """What the OnDemand sequence needs from the rig (7.9 Pausing); the rig drives the app through the gateway."""

    def open(self) -> None:
        """Open OnDemand."""

    def set_timestamp(self, at: datetime) -> None:
        """Enter the checked-in timestamp."""

    def go(self) -> None:
        """Press Go."""

    def wait_buffering(self, timeout_s: float) -> bool:
        """Wait for prebuffering to end; False when it did not within `timeout_s` (POP-12)."""

    def pause(self) -> None:
        """Press Pause."""

    def read_clock(self) -> str | None:
        """OCR of the OnDemand clock; None when unreadable."""

    def park_mouse(self) -> None:
        """Park the mouse before settle and capture."""


def _timeout_outcome(detail: str) -> Outcome:
    return Outcome("env_error", "ondemand_timeout", GATE, detail)


def check_clock(text: str | None, timestamp: datetime, *, clock_format: str = DEFAULT_CLOCK_FORMAT) -> ControlResult:
    """The paused OnDemand clock equals the timestamp (7.9, choice 1); else `env_error (gate: fixture_verified, reason:
    data_epoch_mismatch)` for the checkpoint."""
    if timestamp.tzinfo is None:
        raise ValueError("the OnDemand timestamp is an aware time (4.9 ts)")
    want = timestamp.astimezone(NEW_YORK).strftime(clock_format)
    got: str | None = None
    if text is not None:
        try:
            got = datetime.strptime(" ".join(text.split()), clock_format).strftime(clock_format)
        except ValueError:
            got = None
    if got == want:
        return ControlResult("ondemand_clock", True, GATE, evidence={"clock": text, "timestamp": want})
    why = f"OnDemand clock {text!r} is not the timestamp {want!r}" if text is not None else \
        "the OnDemand clock is unreadable"
    o = Outcome("env_error", "data_epoch_mismatch", GATE, why)
    return ControlResult("ondemand_clock", False, GATE, checkpoint=o, alert=alerts.env_error(), detail=why,
                         evidence={"clock": text, "timestamp": want})


@dataclass
class OnDemandRun:
    """The OnDemand buffering-timeout counters of one run (choice 2): consecutive timeouts per shard, abandoned once."""

    streaks: dict[str, int] = field(default_factory=dict)
    abandoned: bool = False

    def buffered(self, shard_id: str) -> None:
        """Buffering ended in time: the shard's streak resets."""
        self.streaks[shard_id] = 0

    def timeout(self, shard_id: str) -> ControlResult:
        """A 90 s buffering timeout at setup (POP-12): the checkpoint is `ondemand_timeout`; the third consecutive one
        in the shard abandons OnDemand for the run (7.9)."""
        n = self.streaks.get(shard_id, 0) + 1
        self.streaks[shard_id] = n
        if n < ABANDON_AFTER:
            detail = f"OnDemand buffering timed out after {BUFFER_TIMEOUT_S:.0f} s ({n} in a row in {shard_id})"
            return ControlResult("ondemand", False, GATE, checkpoint=_timeout_outcome(detail), detail=detail,
                                 evidence={"shard": shard_id, "consecutive": n})
        first = not self.abandoned
        self.abandoned = True
        detail = f"three consecutive OnDemand buffering timeouts in {shard_id}: OnDemand abandoned for the run"
        o = _timeout_outcome(detail)
        return ControlResult("ondemand", False, GATE, checkpoint=o, outcomes=(SubsetOutcome("frozen", o),),
                             alert=alerts.env_error() if first else None, detail=detail,
                             evidence={"shard": shard_id, "consecutive": n})

    def abandoned_result(self) -> ControlResult:
        """After the abandonment: the remaining OnDemand checkpoints are `ondemand_timeout`, with no further alert."""
        detail = "OnDemand was abandoned for the run after three consecutive buffering timeouts"
        o = _timeout_outcome(detail)
        return ControlResult("ondemand", False, GATE, checkpoint=o, outcomes=(SubsetOutcome("frozen", o),),
                             detail=detail)


def enter(controls: OnDemandControls, timestamp: datetime, run: OnDemandRun, *, shard_id: str,
          clock_format: str = DEFAULT_CLOCK_FORMAT) -> ControlResult:
    """The 7.9 entry sequence (module docstring); passed when OnDemand is paused at the timestamp, mouse parked."""
    if run.abandoned:
        return run.abandoned_result()
    controls.open()
    controls.set_timestamp(timestamp)
    controls.go()
    if not controls.wait_buffering(BUFFER_TIMEOUT_S):
        return run.timeout(shard_id)
    run.buffered(shard_id)
    controls.pause()
    clock = check_clock(controls.read_clock(), timestamp, clock_format=clock_format)
    if not clock.passed:
        return clock
    controls.park_mouse()
    return ControlResult("ondemand", True, GATE, evidence=clock.evidence)


def replay_scope(surfaces: Mapping[str, str], replaying: Iterable[str]) -> dict[str, Outcome]:
    """`not_run (reason: scope)` for every checkpoint (id to surface id) whose gadget does not replay inside OnDemand
    (7.9); the others are absent from the result."""
    ok = set(replaying)
    return {cid: Outcome("not_run", "scope", detail=f"{sid} does not replay inside OnDemand")
            for cid, sid in sorted(surfaces.items()) if sid not in ok}


def frozen_fallback_active(branch: str, ondemand_usable: bool) -> bool:
    """True when frozen checkpoints fall back to masked_live: DR-02 Branch B with OnDemand not usable (7.9)."""
    if branch not in ("A", "B", "undecided"):
        raise ValueError(f"DR-02 branch {branch!r}")
    return branch == "B" and not ondemand_usable


def effective_data_mode(data_mode: str, *, branch: str, ondemand_usable: bool) -> str:
    """The data mode a checkpoint runs in: `masked_live` for a frozen checkpoint under the fallback, else its own."""
    if data_mode not in ("frozen", "masked_live"):
        raise ValueError(f"data mode {data_mode!r}")
    return "masked_live" if data_mode == "frozen" and frozen_fallback_active(branch, ondemand_usable) else data_mode
