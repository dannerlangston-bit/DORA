"""masked_live lockstep: the pair's fingerprint assertion and the 60 s slip rule (1.6, 3.1, 7.4, 7.9, 11.2, 11.3).

`masked_live` checkpoints run on two rigs provisioned from one snapshot, base on one and candidate on the other, in
lockstep: fingerprint equality is asserted before the first capture (and again after every relaunch, 7.4), and both
rigs execute the same checkpoint within 60 s of each other. A slip over 60 s re-queues that checkpoint pair once at
the end of the shard; a second slip is `not_run (reason: budget)`, counted as `lockstep_slip` in run health, a counter
and never a finding (3.1). Pure logic over the capture times the rigs report; no rig I/O here.

- `assert_pair(fp_a, fp_b)`: None when the two rigs' fingerprints agree on every match field but `build_id`
  (`fingerprint.pair_check`, the comparison the lint and the Diff Engine share); else a `ControlResult` whose
  `outcomes` give the shard's `masked_live` checkpoints `not_comparable (reason: fingerprint_mismatch)` with the
  differing fields (selector `masked_live`), the run `complete_with_env_errors` and the on-call alert.
- `LockstepTracker.record(checkpoint, t_a, t_b)`: `paired`, `requeue` (first slip; the pair joins the end-of-shard
  queue `drain()` hands back) or `not_run` (second slip; `not_run (reason: budget)`, `lockstep_slip` + 1).
- `lockstep_health_row(trackers)`: the run-health row over the run's shards, None without a slip.

Choices where the spec is ambiguous (reported):

1. The window is 3.1's "within 60 s of each other": a gap of exactly 60 s is paired, more is a slip. 7.4 and 11.2
   say "the same minute" for the same rule; 3.1 states the number.
2. The pair assertion covers the shard's `masked_live` checkpoints (selector `masked_live`, the dual of N4's
   `frozen`): a frozen checkpoint of a lockstep flow is compared base then candidate on one rig (3.1, 7.4), which
   makes its fingerprint match by construction, so the pair's mismatch does not reach it.
3. The unit re-queued is what the caller records: a census visit's checkpoint, or the flow item a lockstep flow
   checkpoint belongs to (a flow resumes from its preconditions, never mid-way). Capture times are aware datetimes or
   ISO strings with an offset (the rigs' wall clocks; monotonic clocks of two machines are not comparable), or both
   plain seconds on one shared time base (the simulation).
4. `lockstep_slip` counts the second slips, the pairs that ended `not_run (budget)` (3.1 "a second slip is not_run
   (reason: budget) with lockstep_slip counted"); the health row also names the pairs re-queued.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..fingerprint import pair_check
from ..records import Fingerprint
from .results import ControlResult, Outcome, SubsetOutcome

LOCKSTEP_WINDOW_S = 60.0                       # 3.1
PAIR_SELECTOR = "masked_live"                  # choice 2
PAIRED, REQUEUE, NOT_RUN = "paired", "requeue", "not_run"


def assert_pair(fp_a: Fingerprint | Mapping[str, Any], fp_b: Fingerprint | Mapping[str, Any]) -> ControlResult | None:
    """Fingerprint equality of a lockstep pair before its first capture and after every relaunch (3.1, 7.4): None
    when every match field but `build_id` agrees; else the shard's `masked_live` checkpoints are `not_comparable
    (reason: fingerprint_mismatch)` with the differing fields (choice 2), nothing of them is captured, and the run is
    `complete_with_env_errors` (3.3)."""
    mismatch = pair_check(fp_b, fp_a)
    if mismatch is None or mismatch.unreached is None:
        return None
    return ControlResult("lockstep_pair", False, None, run=mismatch.run,
                         outcomes=(SubsetOutcome(PAIR_SELECTOR, mismatch.unreached),), alert=mismatch.alert,
                         detail=mismatch.detail, evidence=dict(mismatch.evidence))


def _seconds(t: datetime | str | float) -> tuple[bool, float]:
    """(is a wall time, seconds) of one capture time (choice 3)."""
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return False, float(t)
    when = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
    if when.tzinfo is None:
        raise ValueError(f"capture time {t!r} carries no UTC offset; two rigs' times compare as aware wall times")
    return True, when.timestamp()


def slip_seconds(t_a: datetime | str | float, t_b: datetime | str | float) -> float:
    """How far apart the two rigs captured the same checkpoint, in seconds (choice 3)."""
    (wall_a, a), (wall_b, b) = _seconds(t_a), _seconds(t_b)
    if wall_a != wall_b:
        raise ValueError("capture times are both wall times or both seconds on one time base")
    return abs(a - b)


@dataclass(frozen=True)
class LockstepDecision:
    """What one lockstep execution of a checkpoint pair decided (3.1). `outcome` is `not_run (reason: budget)` on
    the second slip."""
    checkpoint: str
    action: str                                # paired | requeue | not_run
    slip_s: float
    outcome: Outcome | None = None


class LockstepTracker:
    """The slip rule of one lockstep shard (3.1, 7.4): record each pair's two capture times; drain the end-of-shard
    re-queue; read `lockstep_slip` for run health."""

    def __init__(self, pair: tuple[str, str] | None = None, *, window_s: float = LOCKSTEP_WINDOW_S):
        self.pair = pair
        self.window_s = window_s
        self._slips: dict[str, int] = {}
        self._queue: list[str] = []
        self._requeued: list[str] = []
        self._not_run: list[str] = []

    def record(self, checkpoint: str, t_a: datetime | str | float,
               t_b: datetime | str | float) -> LockstepDecision:
        """One execution of `checkpoint` on both rigs, captured at `t_a` and `t_b`: `paired` within the window; the
        first slip re-queues the pair once at the end of the shard; the second is `not_run (reason: budget)` and
        counts as `lockstep_slip`. A checkpoint already `not_run` is not executed again (ValueError)."""
        if checkpoint in self._not_run:
            raise ValueError(f"{checkpoint} already ended not_run (budget) after two lockstep slips (3.1)")
        gap = slip_seconds(t_a, t_b)
        if gap <= self.window_s:
            return LockstepDecision(checkpoint, PAIRED, gap)
        n = self._slips[checkpoint] = self._slips.get(checkpoint, 0) + 1
        if n == 1:
            self._queue.append(checkpoint)
            self._requeued.append(checkpoint)
            return LockstepDecision(checkpoint, REQUEUE, gap)
        if checkpoint in self._queue:
            self._queue.remove(checkpoint)
        self._not_run.append(checkpoint)
        return LockstepDecision(checkpoint, NOT_RUN, gap,
                                Outcome("not_run", "budget", detail=f"lockstep_slip: {gap:.0f} s apart twice"))

    def drain(self) -> list[str]:
        """The pairs to run again at the end of the shard, in slip order, each handed back once."""
        out, self._queue = self._queue, []
        return out

    @property
    def lockstep_slip(self) -> int:
        """The run-health counter (choice 4): pairs that ended `not_run (budget)` after a second slip."""
        return len(self._not_run)

    @property
    def requeued(self) -> tuple[str, ...]:
        """Every pair re-queued after its first slip, in order."""
        return tuple(self._requeued)

    @property
    def not_run(self) -> tuple[str, ...]:
        """The pairs that ended `not_run (budget)`, in order."""
        return tuple(self._not_run)


def lockstep_health_row(trackers: Iterable[LockstepTracker]) -> tuple[str, str] | None:
    """The run-health row over every lockstep shard of the run (3.1: a counter, never a finding), for
    `summary["health_rows"]`; None when no pair slipped."""
    ts = list(trackers)
    slips = sum(t.lockstep_slip for t in ts)
    requeued = sum(len(t.requeued) for t in ts)
    if not requeued:
        return None
    gone = [c for t in ts for c in t.not_run]
    text = f"lockstep_slip {slips}"
    if gone:
        text += f" ({', '.join(gone)}: not_run, budget)"
    return ("Lockstep", (f"{text}; {requeued} pairs re-queued once at the end of the shard after a slip over "
                         f"{LOCKSTEP_WINDOW_S:.0f} s"))
