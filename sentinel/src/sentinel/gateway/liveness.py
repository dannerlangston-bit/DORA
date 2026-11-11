"""Hook liveness: the probe and the reinstall rule (5.4.4), ending in the fail-closed sequence of 5.4.2.

Windows silently removes a low-level hook whose callback runs over the hook timeout, and input then flows
unfiltered; an endpoint control or a hook host fault can do the same. The slow half finds out with a probe:

- `ProbeScheduler`: a tokened probe move to the parking coordinate is due every 5 s while idle, before every
  step batch, and at once on an integrity drop (5.4.3 stage 7); never while a probe is in flight. The emitter
  writes it with `segment.SegmentWriter.issue_probe` and sends the move.
- `judge_probe`: the hook stamps `last_probe_mono_ts` when it sees the probe. A stamp within 100 ms of sending
  is `seen`; 100 ms with no such stamp is `missed`, a late stamp included (the hook was gone for that long).
- `HookRemovals` (one per shard): the first missed probe means the hook was removed. The caller asks the hook
  host to reinstall both hooks (`SegmentWriter.request_reinstall`; the hook host logs `hook_reinstalled` in the
  ring, which the drainer turns into the step event), the run gets a `gateway_integrity` finding and an on-call
  alert, and the next probe must succeed. A failed reinstall (`judge_reinstall`) or a second removal in the
  shard, the next probe missing included, is a dead hook host: `watchdog.hook_host_died`, which kills the tree,
  restarts the hook host out of its own budget of one per shard and relaunches; the second restart ends the run
  `aborted_harness (reason: hook_lost)`.

Times are monotonic seconds (`Clock.monotonic`); segment stamps convert with `segment.to_seconds`.

Choices (reported):

1. A reinstall request the hook host has not answered within 1 s is a failed reinstall: 1 s is the age at which
   5.4.2 already calls the hook host dead (`hook_alive_mono_ts` under 1 s old before every emission).
2. The reinstall's result passes (the run continues) and carries the finding and the alert, as the gateway's
   other "nothing reached the app" integrity results do (`DropLedger`, `untokened_injected`); the request to
   the hook host is the verdict's `reinstall` flag, since the control actions of results.py have no reinstall.
3. Removals are counted per shard across hook host restarts: 5.4.4 says "a second removal in one shard".
"""
from __future__ import annotations

from dataclasses import dataclass

from ..orchestrator import alerts
from ..orchestrator.budgets import ShardBudgets
from ..orchestrator.results import ControlResult, FindingSpec
from ..orchestrator.watchdog import hook_host_died

PROBE_INTERVAL_S = 5.0                 # 5.4.4: every 5 s while idle
PROBE_WINDOW_S = 0.1                   # 5.4.4: a probe not seen within 100 ms means the hook was removed
REINSTALL_ANSWER_S = 1.0               # choice 1


def judge_probe(sent_at: float, last_probe_ts: float, now: float) -> str:
    """`seen` when the hook stamped the probe within 100 ms of `sent_at`, `missed` once 100 ms passed without
    that (a later stamp is still a miss), `pending` before then."""
    if sent_at <= last_probe_ts <= sent_at + PROBE_WINDOW_S:
        return "seen"
    return "missed" if now - sent_at > PROBE_WINDOW_S else "pending"


def judge_reinstall(request: int, done: int, ok: bool, *, asked_at: float, now: float) -> str:
    """The hook host's answer to reinstall request `request` from the segment (`reinstall_done`,
    `reinstall_ok`): `ok`, `failed` (it failed, or no answer within 1 s; choice 1) or `pending`."""
    if done >= request:
        return "ok" if ok else "failed"
    return "failed" if now - asked_at > REINSTALL_ANSWER_S else "pending"


class ProbeScheduler:
    """When the slow half probes the hook (5.4.4), and the verdict on the probe in flight."""

    def __init__(self, interval_s: float = PROBE_INTERVAL_S):
        self.interval_s = interval_s
        self.last_sent: float | None = None
        self.in_flight: float | None = None

    def due(self, now: float, *, idle: bool = False, before_batch: bool = False,
            integrity_drop: bool = False) -> bool:
        """True when a probe should go out now: before a step batch, on an integrity drop, or idle for 5 s since
        the last probe; never while one is in flight."""
        if self.in_flight is not None:
            return False
        if before_batch or integrity_drop:
            return True
        return idle and (self.last_sent is None or now - self.last_sent >= self.interval_s)

    def sent(self, now: float) -> None:
        """The probe token is written and the move emitted at `now`."""
        self.last_sent = self.in_flight = now

    def judge(self, last_probe_ts: float, now: float) -> str:
        """`judge_probe` for the probe in flight; `seen` and `missed` close it."""
        if self.in_flight is None:
            raise ValueError("no probe in flight")
        verdict = judge_probe(self.in_flight, last_probe_ts, now)
        if verdict != "pending":
            self.in_flight = None
        return verdict


@dataclass(frozen=True)
class LivenessVerdict:
    """What a hook removal decides: `reinstall` asks the hook host to reinstall both hooks (choice 2)."""

    reinstall: bool
    result: ControlResult


class HookRemovals:
    """Hook removals in one shard (5.4.4): the first is reinstalled, a second or a failed reinstall is a dead
    hook host (`watchdog.hook_host_died`, which spends the shard's hook host budget)."""

    def __init__(self, budgets: ShardBudgets):
        self.budgets = budgets
        self.removals = 0

    def probe_missed(self, *, step_id: str | None) -> LivenessVerdict:
        """A missed probe: the hook was removed."""
        self.removals += 1
        if self.removals > 1:
            return self._dead(step_id)
        return LivenessVerdict(True, ControlResult(
            "hook_liveness", True, "running", alert=alerts.harness_error(),
            findings=(FindingSpec("gateway_integrity", "low-level hook removed (liveness probe missed); "
                                  "both hooks reinstalled once"),),
            detail="the next probe must succeed; a second removal in the shard is a dead hook host"))

    def reinstall_failed(self, *, step_id: str | None) -> LivenessVerdict:
        """The hook host could not reinstall the hooks, or did not answer (`judge_reinstall`)."""
        return self._dead(step_id)

    def _dead(self, step_id: str | None) -> LivenessVerdict:
        return LivenessVerdict(False, hook_host_died(self.budgets, step_id=step_id))
