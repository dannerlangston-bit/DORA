"""The control host's run queue and scheduler (11.2 Queue and saturation, 1.6 Run types, 1.7, 5.1, 7.2, 7.10, 9.10,
10.2, 10.9, 11.6; SD-76).

`RunQueue` is a pure state machine on the injectable `Clock`: `handle(event)` and `tick()` take trigger and rig events
and return what must happen, in order. The driver delivers each output and calls `tick()` at least every 60 s.

Inputs: `PrBuild`, `MergeBuild` and `RcBuild` (the pipeline webhook, through `webhook_to_request`),
`CalibrationRequest` (a Calibration run is due, 7.13), `RunReachedRunning`, `PoolReleased` (an RC run's pair work and
targeted re-runs ended), `RunFinished`, `Requeue` (no rig could be provisioned, 7.2) and `RigState` (a rig enters or
leaves service). The Nightly timer is internal: 22:00 America/New_York.

Outputs: `RunStart` (dispatch a run on rigs), `RunQueued` (the 60 s `run_queued` event with the queue position, which
the dead-man accepts in place of `run_started`, 10.2), `Placeholder` (PR comment placeholder text), `DeadlineReached`
(the 11.2 start-deadline outcome, `start_deadline.Decision`), `Supersede` (stop an RC run at the next flow boundary:
`budgets.run_budget(superseded=True)`), `Replaced` (a queued run a newer trigger replaced; the PR comment's run history
lists it as superseded), `Coalesced` (a merge joined the queued merge run), `Scheduled` (a Calibration run's night),
`ReservationEnded` (the window of an RC pool reservation, for the RC report), `NightlySkipped`, `Alert` (deliver this
route) and `StateWrite` (a `system_state` key, 8.3: `canaries_pending`, `nightly_skipped_for`).

The rules (11.2 unless noted):

- One queue per pool, ordered RC, then merge, then PR, then age (trigger time); the Nightly and Calibration timer
  runs, when they wait, come after RC and before merge (choice 11).
- PR, merge and RC runs start only inside the operating day, 06:00 to 22:00 America/New_York (an injected predicate,
  `calendar.is_operating_time`); one triggered outside waits until it opens, a PR run with the placeholder "queued
  until 06:00 America/New_York", and its start deadline runs only while the operating day is open (SD-76, 7.10). The
  Nightly and Calibration runs are exempt.
- PR: one run in flight per PR; a push while one is queued replaces it (`Replaced`), a push while one is running
  queues the newest. A PR run still queued 45 minutes after its trigger is `aborted_budget (reason:
  pool_saturated)` with "N runs ahead". Queue depth above 3 PR runs or any PR run waiting over 20 minutes posts P2
  "pool saturated", at most once per hour (9.10, 11.6), as does the abort.
- Merge: a merge while a merge run is queued coalesces into it: candidate = the newest main, base = the last main with
  a completed merge run (resolved when the run starts), manifests = the union of the PRs merged in the range, and the
  run names the range (9.4). A merge run queued 3 hours is not aborted (`coalesce`): it joins the next merge run.
- RC: an RC run reserves the pool from its start until `PoolReleased`; PR runs are held meanwhile with their deadline
  suspended and the placeholder "queued behind RC run, pool expected free at HH:MM" (T+4:00 expected, T+6:00 once
  that has passed). A new RC build of the same train supersedes the RC run in flight (`Supersede`, label "superseded
  by {build}", P2 to the release manager) and the new run takes over the reservation and starts as soon as the old
  one has torn down, inside the operating day (choice 2). An RC run past its start deadline (the G-N sign-off time
  minus 8 hours) stays queued and goes to the release manager (P2).
- Nightly: due at 22:00 on rigs 4 and 5 (rig 5 alone while a weekend Calibration run holds the pool: the A/A pass
  pauses, 1.6). An RC run holding rig 4 or 5 at 22:00 pre-empts it: `NightlySkipped`, P2 to the harness on-call and
  system owner, `nightly_skipped_for` and `canaries_pending` = "last Nightly skipped for RC run {run_id}", and a
  catch-up run of the canaries, fire drill and safety canaries on rig 5 as soon as it is free (`catch_up=True`). A
  Nightly not at `running` by 02:00, and a Calibration run not at `running` by its start deadline (Saturday 02:00,
  emergency 01:00), end `aborted_harness (reason: start_failed)` with `canaries_pending` = "{run type} run {run_id}
  did not start"; a Calibration run is queued again for the next `all_quiet` window (the next night for an
  emergency). A Nightly run that finishes with its canaries, fire drill and safety canaries passed clears both keys.
- Calibration: a weekend run starts 22:15 on the first night of the next `all_quiet` window (an injected
  `next_all_quiet`, default Friday 20:00 America/New_York) on rigs 1 to 4; an emergency run starts 22:15 the same
  night on rigs 1 to 3 (11.2 Emergency calibration).

Choices where the spec is ambiguous (reported):

1. Rigs: PR and merge runs take every pool rig in service (rigs 1 to 4, 11.2 "recommended 4"), an RC run every rig
   in service (1 to 6), a Nightly rig 5 plus rig 4 (rig 5 required), a catch-up run rig 5, a weekend Calibration run
   the pool rigs in service and an emergency one rigs 1 to 3. A run starts only with all of them free, in queue
   order; a waiting run claims its rigs so no lower run overtakes it on them (no starvation), and runs on disjoint
   rigs proceed (a catch-up on rig 5 beside a waiting PR run).
2. A PR, merge or RC run triggered inside the operating day but still queued at 22:00 waits until 06:00 with its
   deadline suspended, as if triggered outside: the pool is off (11.4). This includes the successor of an RC
   respin: 11.2's "starts at once on all six rigs" means it does not queue behind other runs, not that it starts
   after hours (SD-76 exempts only the Nightly and Calibration runs). Outside the operating day it waits until 06:00
   holding the transferred reservation, so PR runs stay held, and claims no rigs, so a Nightly catch-up runs on
   rig 5 meanwhile.
3. Every queued PR run is held during an RC reservation with its deadline suspended, also one triggered just before
   the RC run started (11.2 names those triggered in the window; the others wait behind it the same way). Neither
   saturation rule fires during a reservation or outside the operating day.
4. "N runs ahead" counts the runs queued ahead plus the runs holding pool rigs (start_deadline choice 3).
5. An RC run holding rig 4 or rig 5 at 22:00 pre-empts the Nightly (11.2 "holds rigs 4 and 5"): the Nightly needs
   both, and waiting for the soak on rig 5 would end it `start_failed` at 02:00 instead of the skip 11.2 intends.
6. A merge run is "completed" when it finished `complete` or `complete_with_env_errors`; an aborted merge run leaves
   the base where it was, so its PRs join the next range.
7. A coalesced merge run's start deadline runs from its newest trigger. A queued RC run replaced by a newer build of
   its train never ran: it is `Replaced` (history only), and only a run in flight is superseded.
8. A weekend Calibration run holds its rigs from its start until it finishes (both nights).
9. The Nightly is due when a step crosses 22:00. After an outage that skipped several 22:00s only the latest is due,
   and a queue created after 22:00 starts with the next night's.
10. The operating-day predicate is integrated exactly between events by a 15-minute scan with bisection, so an
    open or closed stretch shorter than 15 minutes would be missed; the operating day has two edges a day.
11. A waiting Nightly or Calibration run ranks after RC runs and before merge runs: an RC run pre-empts the Nightly
    (11.2), so an RC run waiting in the operating day takes rig 5 before a catch-up, which runs "after the RC run";
    PR, merge and RC runs start only in the operating day, so they meet the night runs only on rigs still held.
12. One Calibration run per window (11.2 "one weekend `all_quiet` window"): a `CalibrationRequest`, or the requeue
    of one that did not start, for a kind and night that already has a run not being aborted (scheduled, queued,
    dispatched or running) joins it and emits nothing, so a duplicate never waits behind the real run to end
    `start_failed` with a P1 and the "canaries pending" banner. The callers send one request per run on a fingerprint
    without calibration (`gates.check_provisioned`, `aa_pass`), so duplicates are the normal input.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import start_deadline as sd
from .alerts import AlertRoute
from .clock import Clock

POOL = frozenset({1, 2, 3, 4})                      # 11.2 Fleet: rigs 1 to 4 are the pool
ALL_RIGS = frozenset({1, 2, 3, 4, 5, 6})
NIGHTLY_RIGS = frozenset({4, 5})                    # 11.2: rig 5, with the A/A pass sharded over rigs 4 and 5
CANARY_RIG = 5
EMERGENCY_RIGS = frozenset({1, 2, 3})               # 11.2 Emergency calibration: rigs 1 to 3
OPERATING_START, OPERATING_END = time(6, 0), time(22, 0)     # PA-12
NIGHTLY_AT = time(22, 0)                            # 1.6, 11.2
CALIBRATION_AT = time(22, 15)                       # 11.2: the passes start about 22:15 (scope.CALIBRATION_START)
ALL_QUIET_START = time(20, 0)                       # 7.10: Friday 20:00 (schedule/windows.yaml)
FRIDAY = 4
RUN_QUEUED_EVERY = timedelta(seconds=60)            # 11.2, 10.2
SATURATION_DEPTH = 3                                # 11.2: queue depth above 3 PR runs
SATURATION_WAIT = timedelta(minutes=20)             # 11.2: a PR run waiting over 20 min
SATURATION_REPEAT = timedelta(hours=1)              # 9.10, 11.6: one P2 per hour
RC_POOL_FREE_EXPECTED = timedelta(hours=4)          # 11.2: about T+4:00 expected
RC_POOL_FREE_WORST = timedelta(hours=6)             # 11.2: T+6:00 worst case
PRIORITY = {"rc": 0, "nightly": 1, "calibration": 1, "merge": 2, "pr": 3}      # 11.2; choice 11
PLACEHOLDER_HELD = "queued behind RC run, pool expected free at {hhmm}"
PLACEHOLDER_NIGHT = "queued until 06:00 America/New_York"
COMPLETED = ("complete", "complete_with_env_errors")         # choice 6
_SCAN = timedelta(minutes=15)
_TICK = timedelta(microseconds=1)


class WebhookError(ValueError):
    """A pipeline webhook payload the queue cannot read."""


# -- inputs ----------------------------------------------------------------------------------------------------
def _aware(at: datetime, name: str) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError(f"{name} must be an aware datetime, got {at!r}")


@dataclass(frozen=True)
class PrBuild:
    """A branch build published for an open PR (1.7 T+0:00)."""

    pr: int
    build_id: str
    triggered_at: datetime
    commit: str = ""
    merge_base: str = ""
    manifest_ref: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _aware(self.triggered_at, "triggered_at")


@dataclass(frozen=True)
class MergeBuild:
    """A merge to main: the new main build, the main build before it and the merged PR (1.8 T+0:00)."""

    build_id: str
    triggered_at: datetime
    previous_main: str | None = None
    merged_pr: int | None = None
    manifest_ref: str | None = None
    commit: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _aware(self.triggered_at, "triggered_at")


@dataclass(frozen=True)
class RcBuild:
    """An RC cut or respin of a release train; `signoff_at` is the G-N sign-off time (10.9), None when unknown."""

    build_id: str
    train: str
    triggered_at: datetime
    signoff_at: datetime | None = None
    commit: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _aware(self.triggered_at, "triggered_at")
        if self.signoff_at is not None:
            _aware(self.signoff_at, "signoff_at")


@dataclass(frozen=True)
class CalibrationRequest:
    """A Calibration run is due (7.13 `queue_calibration`): `weekend` or `emergency`."""

    kind: str
    requested_at: datetime
    requeue_of: str | None = None

    def __post_init__(self):
        _aware(self.requested_at, "requested_at")
        if self.kind not in sd.CALIBRATION_KINDS:
            raise ValueError(f"calibration kind {self.kind!r}: one of {sd.CALIBRATION_KINDS}")


@dataclass(frozen=True)
class RunReachedRunning:
    """The run passed `canary_flow_passed` and entered `running` (5.2)."""

    run_id: str


@dataclass(frozen=True)
class PoolReleased:
    """An RC run's pair work and targeted re-runs ended: rigs 1 to 4 go back to the pool (11.2)."""

    run_id: str


@dataclass(frozen=True)
class RunFinished:
    """The run ended with run outcome `status` (1.10); `canaries_passed` for a Nightly run whose canaries, fire drill
    and safety canaries passed (11.2)."""

    run_id: str
    status: str
    canaries_passed: bool = False


@dataclass(frozen=True)
class Requeue:
    """A dispatched run whose rigs could not be provisioned stays queued (7.2), its deadline running on. Send
    `RigState(rig, up=False)` for the rigs that failed first, or the run starts again on them at once."""

    run_id: str


@dataclass(frozen=True)
class RigState:
    """A rig enters (`up`) or leaves the pool (disabled by the Rig Manager, a safety stop, 6.6, 10.7)."""

    rig: int
    up: bool


Event = PrBuild | MergeBuild | RcBuild | CalibrationRequest | RunReachedRunning | PoolReleased | RunFinished | \
    Requeue | RigState


# -- the request a run carries ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class MainBuild:
    """One main build in merge order, with the PR merged into it and that PR's manifest (4.3)."""

    build_id: str
    previous_main: str | None
    merged_pr: int | None
    manifest_ref: str | None
    triggered_at: datetime


@dataclass(frozen=True)
class RunRequest:
    """What a queued or started run is about. `candidate`: the branch build (PR), the newest main (merge) or the RC
    build; `base`: the merge-base (PR) or, from the start of a merge run, the last main with a completed merge run;
    `mains`: a merge run's range, oldest first; `triggers`: the trigger events it covers."""

    run_id: str
    run_type: str
    triggered_at: datetime
    candidate: str | None = None
    base: str | None = None
    pr: int | None = None
    train: str | None = None
    mains: tuple[MainBuild, ...] = ()
    calibration: str | None = None
    requeue_of: str | None = None
    catch_up: bool = False
    aa_pass: bool = True
    supersedes: str | None = None
    signoff_at: datetime | None = None
    triggers: tuple[Any, ...] = ()

    @property
    def prs(self) -> tuple[int, ...]:
        """The PRs merged in a merge run's range."""
        return tuple(m.merged_pr for m in self.mains if m.merged_pr is not None)

    @property
    def manifest_refs(self) -> tuple[str, ...]:
        """The manifests whose union is the merge run's manifest (`scope.Manifest.union`, 4.3)."""
        return tuple(m.manifest_ref for m in self.mains if m.manifest_ref is not None)

    @property
    def coalesced(self) -> bool:
        """True when the merge run covers more than one main build (11.2)."""
        return len(self.mains) > 1

    @property
    def range_label(self) -> str | None:
        """The range a merge report names (9.4): main builds and PRs covered."""
        if self.run_type != "merge":
            return None
        prs = ", ".join(f"#{p}" for p in self.prs) or "none"
        return f"main {self.base}..{self.candidate}: {len(self.mains)} main builds, PRs {prs}"


# -- outputs ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RunStart:
    """Start this run on these rigs now."""

    request: RunRequest
    rigs: tuple[int, ...]
    at: datetime


@dataclass(frozen=True)
class RunQueued:
    """The `run_queued` event (11.2, 10.2): position 1 starts next; `deadline_at` is None while it is suspended."""

    run_id: str
    run_type: str
    position: int
    depth: int
    at: datetime
    deadline_at: datetime | None
    covers: tuple[str, ...]


@dataclass(frozen=True)
class Placeholder:
    """The PR comment placeholder text of a queued PR run (11.2)."""

    run_id: str
    pr: int
    text: str
    at: datetime


@dataclass(frozen=True)
class DeadlineReached:
    """The run's start-deadline outcome (11.2): write `decision.result` for an `abort`."""

    request: RunRequest
    decision: sd.Decision
    at: datetime


@dataclass(frozen=True)
class Supersede:
    """Stop RC run `run_id` at the next flow boundary, `aborted_budget (reason: superseded)` (1.6, 11.2); its partial
    report carries `label`."""

    run_id: str
    by_run_id: str
    by_build: str
    label: str
    at: datetime


@dataclass(frozen=True)
class Replaced:
    """A queued run that never started, replaced by a newer trigger (the PR run history's superseded push)."""

    run_id: str
    by_run_id: str
    run_type: str
    at: datetime


@dataclass(frozen=True)
class Coalesced:
    """Merge `build_id` joined the queued merge run `run_id` (11.2)."""

    run_id: str
    build_id: str
    at: datetime


@dataclass(frozen=True)
class Scheduled:
    """A Calibration run is scheduled for `night`: it queues at `start` and must reach `running` by `deadline_at`
    (11.2); `scope.plan_calibration(..., kind, day=night)` plans it."""

    request: RunRequest
    night: date
    start: datetime
    deadline_at: datetime
    at: datetime


@dataclass(frozen=True)
class ReservationEnded:
    """RC run `run_id` released the pool: PR runs were held from `start` to `end` (11.2), the window in which the RC
    report prints goal 1's 45-minute promise as suspended; `held` are the PR runs held in it."""

    run_id: str
    start: datetime
    end: datetime
    held: tuple[str, ...]
    at: datetime


@dataclass(frozen=True)
class NightlySkipped:
    """The Nightly run `run_id` was skipped: RC run `by_run_id` held its rigs at 22:00 (11.2)."""

    run_id: str
    by_run_id: str
    at: datetime


@dataclass(frozen=True)
class Alert:
    """Deliver `route` with this alert name and detail (9.10 message format)."""

    route: AlertRoute
    alert: str
    run_id: str | None
    detail: str
    at: datetime


@dataclass(frozen=True)
class StateWrite:
    """Set `system_state` key (8.3); an empty value means no banner is due."""

    key: str
    value: str
    at: datetime


Output = RunStart | RunQueued | Placeholder | DeadlineReached | Supersede | Replaced | Coalesced | Scheduled | \
    ReservationEnded | NightlySkipped | Alert | StateWrite


# -- calendars -------------------------------------------------------------------------------------------------
def fixed_operating_day(at: datetime) -> bool:
    """PA-12: 06:00 to 22:00 America/New_York, every day; `calendar.is_operating_time` replaces it."""
    t = sd.to_local(at).time()
    return OPERATING_START <= t < OPERATING_END


def friday_all_quiet(after: datetime) -> datetime:
    """The start of the first `all_quiet` window at or after `after`: Friday 20:00 America/New_York (7.10)."""
    return sd.first_local(after, ALL_QUIET_START, weekday=FRIDAY, inclusive=True)


def calibration_slot(kind: str, at: datetime,
                     next_all_quiet: Callable[[datetime], datetime] = friday_all_quiet) -> tuple[date, datetime,
                                                                                                 datetime]:
    """(night, start, start deadline) of the next Calibration run requested at `at` (11.2): weekend, the first night of
    the first `all_quiet` window whose Saturday 02:00 deadline is still ahead, from 22:15; emergency, tonight from
    22:15 to 01:00 (before 01:00 the night that is still on)."""
    if kind == "emergency":
        loc = sd.to_local(at)
        night = loc.date() if loc.time() >= sd.EMERGENCY_CALIBRATION_START_DEADLINE else loc.date() - timedelta(days=1)
        start = datetime.combine(night, CALIBRATION_AT, tzinfo=sd.NEW_YORK).astimezone(timezone.utc)
        return night, start, sd.deadline("calibration", start, calibration="emergency")
    if kind != "weekend":
        raise ValueError(f"calibration kind {kind!r}: one of {sd.CALIBRATION_KINDS}")
    cursor = at - timedelta(days=3)
    for _ in range(60):
        window = next_all_quiet(cursor)
        _aware(window, "next_all_quiet()")
        night = sd.to_local(window).date()
        first = datetime.combine(night, CALIBRATION_AT, tzinfo=sd.NEW_YORK).astimezone(timezone.utc)
        start = max(window, first)
        due = sd.deadline("calibration", start, calibration="weekend")
        if due > at:
            return night, start, due
        cursor = window + timedelta(minutes=1)
    raise ValueError("next_all_quiet gave no window ahead in 60 tries")


def open_time(is_open: Callable[[datetime], bool], a: datetime, b: datetime) -> timedelta:
    """How long `is_open` holds in [a, b) (choice 10)."""
    total = timedelta(0)
    s, state = a, is_open(a)
    while s < b:
        e = min(s + _SCAN, b)
        after = is_open(e)
        if after == state:
            total += (e - s) if state else timedelta(0)
            s = e
            continue
        lo, hi = s, e
        while hi - lo > _TICK:
            mid = lo + (hi - lo) / 2
            if is_open(mid) == state:
                lo = mid
            else:
                hi = mid
        total += (hi - s) if state else timedelta(0)
        s, state = hi, after
    return total


# -- the webhook -----------------------------------------------------------------------------------------------
def _field(event: Mapping[str, Any], name: str, kind: type, *, required: bool = True) -> Any:
    value = event.get(name)
    if value is None:
        if required:
            raise WebhookError(f"webhook {event.get('kind')!r}: {name} is required")
        return None
    if kind is int and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise WebhookError(f"webhook {name} is a positive integer, got {value!r}")
    if kind is str and (not isinstance(value, str) or not value.strip()):
        raise WebhookError(f"webhook {name} is a non-empty string, got {value!r}")
    return value


def _when(event: Mapping[str, Any], name: str, *, required: bool = True) -> datetime | None:
    raw = _field(event, name, str, required=required)
    if raw is None:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        raise WebhookError(f"webhook {name} is ISO 8601, got {raw!r}") from None
    if at.tzinfo is None:
        raise WebhookError(f"webhook {name} needs a UTC offset, got {raw!r}")
    return at


def webhook_to_request(event: Mapping[str, Any]) -> PrBuild | MergeBuild | RcBuild:
    """The trigger of one pipeline webhook payload (5.1 Scheduler, 1.7 T+0:00, 11.7 row 1).

    Payload: `kind` `pr` | `merge` | `rc`; `build_id`; `commit`; `triggered_at` (ISO 8601 with offset: the artifact
    publish, T+0:00). PR: `pr` (number), `merge_base`, optional `manifest_ref`. Merge: optional `merged_pr`,
    `previous_main`, `manifest_ref`. RC: `train`, optional `signoff_at` (the G-N sign-off time, from the release
    train policy). The whole payload rides along as `payload` (diff, dependency index). Raises `WebhookError`."""
    if not isinstance(event, Mapping):
        raise WebhookError(f"webhook payload is a mapping, got {type(event).__name__}")
    kind = event.get("kind")
    build = _field(event, "build_id", str)
    commit = _field(event, "commit", str)
    at = _when(event, "triggered_at")
    payload = dict(event)
    if kind == "pr":
        return PrBuild(_field(event, "pr", int), build, at, commit, _field(event, "merge_base", str),
                       _field(event, "manifest_ref", str, required=False), payload)
    if kind == "merge":
        return MergeBuild(build, at, _field(event, "previous_main", str, required=False),
                          _field(event, "merged_pr", int, required=False),
                          _field(event, "manifest_ref", str, required=False), commit, payload)
    if kind == "rc":
        return RcBuild(build, _field(event, "train", str), at, _when(event, "signoff_at", required=False), commit,
                       payload)
    raise WebhookError(f"webhook kind {kind!r}: one of pr, merge, rc")


# -- the queue -------------------------------------------------------------------------------------------------
@dataclass
class _Reservation:
    """An RC pool reservation: since when, and the PR runs held in it (11.2); it moves with an RC respin."""

    since: datetime
    held: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    """One run the queue knows, from its trigger until it finishes."""

    run_id: str
    run_type: str
    triggered_at: datetime
    seq: int
    status: str = "queued"              # scheduled | queued | dispatched | running | aborting
    eligible_at: datetime | None = None
    candidate: str | None = None
    merge_base: str | None = None
    pr: int | None = None
    train: str | None = None
    mains: list[MainBuild] = field(default_factory=list)
    calibration: str | None = None
    requeue_of: str | None = None
    catch_up: bool = False
    supersedes: str | None = None
    superseded_by: str | None = None
    signoff_at: datetime | None = None
    triggers: list[Any] = field(default_factory=list)
    night: date | None = None
    fixed_deadline: datetime | None = None
    consumed: timedelta = timedelta(0)
    advanced_to: datetime | None = None
    deadline_done: bool = False
    reservation: _Reservation | None = None
    rigs: tuple[int, ...] = ()
    dispatched_at: datetime | None = None
    last_queued: datetime | None = None
    placeholder: str | None = None
    base: str | None = None
    range_: tuple[MainBuild, ...] = ()
    aa_pass: bool = True

    def request(self) -> RunRequest:
        mains = self.range_ or tuple(self.mains)
        return RunRequest(self.run_id, self.run_type, self.triggered_at, self.candidate,
                          self.base if self.run_type == "merge" else self.merge_base, self.pr, self.train, mains,
                          self.calibration, self.requeue_of, self.catch_up, self.aa_pass, self.supersedes,
                          self.signoff_at, tuple(self.triggers))

    @property
    def budget(self) -> timedelta:
        return sd.PR_START if self.run_type == "pr" else sd.MERGE_START


class RunQueue:
    """The scheduler of one pool (module docstring). `operating_day` is `calendar.is_operating_time`;
    `next_all_quiet(after)` gives the start of the first `all_quiet` window at or after `after`; `run_ids(run_type)`
    names a new run. Seed `last_completed_main`, `canaries_pending` and `nightly_skipped_for` from the store."""

    def __init__(self, clock: Clock, *, operating_day: Callable[[datetime], bool] = fixed_operating_day,
                 next_all_quiet: Callable[[datetime], datetime] = friday_all_quiet,
                 run_ids: Callable[[str], str] | None = None, last_completed_main: str | None = None,
                 canaries_pending: str = "", nightly_skipped_for: str = "", rigs_down: frozenset[int] = frozenset()):
        self.clock = clock
        self.operating_day = operating_day
        self.next_all_quiet = next_all_quiet
        counter = itertools.count(1)
        self._run_ids = run_ids or (lambda run_type: f"{run_type}-{next(counter)}")
        self._seq = itertools.count()
        self._entries: dict[str, _Entry] = {}
        if not set(rigs_down) <= ALL_RIGS:
            raise ValueError(f"rigs_down {sorted(rigs_down)}: rigs are {sorted(ALL_RIGS)}")
        self._up = set(ALL_RIGS) - set(rigs_down)
        self._held: dict[int, str] = {}
        self._mains: list[MainBuild] = []
        self.last_completed_main = last_completed_main
        self._state = {"canaries_pending": canaries_pending, "nightly_skipped_for": nightly_skipped_for}
        self._last_sweep = clock.now()
        _aware(self._last_sweep, "clock.now()")
        self._last_saturation: datetime | None = None
        self._out: list[Output] = []

    # -- public ------------------------------------------------------------------------------------------------
    def tick(self) -> list[Output]:
        """Advance to `clock.now()`: timers, start deadlines, starts, `run_queued` events and saturation alerts."""
        return self._step(None)

    def handle(self, event: Event) -> list[Output]:
        """Apply one event at `clock.now()`, then do everything `tick` does."""
        return self._step(event)

    def queued(self) -> tuple[RunRequest, ...]:
        """The queued runs in queue order (position 1 first)."""
        return tuple(e.request() for e in self._order())

    def position(self, run_id: str) -> int | None:
        """The queue position of a queued run, None when it is not queued."""
        return next((i for i, e in enumerate(self._order(), 1) if e.run_id == run_id), None)

    @property
    def reserved(self) -> bool:
        """True while an RC run reserves the pool (11.2)."""
        return any(e.reservation is not None for e in self._entries.values())

    def state(self, key: str) -> str:
        """The queue's view of `canaries_pending` or `nightly_skipped_for`."""
        return self._state[key]

    # -- the step ----------------------------------------------------------------------------------------------
    def _step(self, event: Event | None) -> list[Output]:
        now = self.clock.now()
        self._out = []
        self._advance(now)
        if event is not None:
            self._apply(event, now)
        self._timers(now)
        self._deadlines(now)
        self._dispatch(now)
        self._announce(now)
        self._saturation(now)
        self._last_sweep = now
        out, self._out = self._out, []
        return out

    def _emit(self, item: Output) -> None:
        self._out.append(item)

    def _write(self, key: str, value: str, now: datetime) -> None:
        if self._state[key] != value:
            self._state[key] = value
            self._emit(StateWrite(key, value, now))

    def _new(self, run_type: str, triggered_at: datetime, **kw: Any) -> _Entry:
        e = _Entry(self._run_ids(run_type), run_type, triggered_at, next(self._seq), **kw)
        if e.run_id in self._entries:
            raise ValueError(f"run id {e.run_id!r} is already in the queue")
        if e.run_type in ("pr", "merge"):
            e.advanced_to = triggered_at
            self._consume(e, self.clock.now())
        self._entries[e.run_id] = e
        return e

    def _get(self, run_id: str) -> _Entry:
        try:
            return self._entries[run_id]
        except KeyError:
            raise ValueError(f"run {run_id!r} is not in the queue") from None

    # -- deadline clocks ---------------------------------------------------------------------------------------
    def _consume(self, e: _Entry, now: datetime) -> None:
        """Run a PR or merge run's start-deadline clock to `now`: only while the operating day is open and, for a PR
        run, no RC reservation holds (choices 2 and 3)."""
        assert e.advanced_to is not None
        if now > e.advanced_to and not (e.run_type == "pr" and self.reserved):
            e.consumed += open_time(self.operating_day, e.advanced_to, now)
        e.advanced_to = max(e.advanced_to, now)

    def _advance(self, now: datetime) -> None:
        for e in self._entries.values():
            if e.status == "queued" and e.run_type in ("pr", "merge"):
                self._consume(e, now)

    def _running_clock(self, e: _Entry, now: datetime) -> bool:
        return self.operating_day(now) and not (e.run_type == "pr" and self.reserved)

    def _deadline_at(self, e: _Entry, now: datetime) -> datetime | None:
        if e.run_type in ("pr", "merge"):
            return now + (e.budget - e.consumed) if self._running_clock(e, now) else None
        return e.fixed_deadline

    # -- events ------------------------------------------------------------------------------------------------
    def _apply(self, event: Event, now: datetime) -> None:
        if isinstance(event, PrBuild):
            self._pr(event, now)
        elif isinstance(event, MergeBuild):
            self._merge(event, now)
        elif isinstance(event, RcBuild):
            self._rc(event, now)
        elif isinstance(event, CalibrationRequest):
            self._calibration(event.kind, event.requested_at, event.requeue_of)
        elif isinstance(event, RunReachedRunning):
            e = self._get(event.run_id)
            if e.status == "dispatched":
                e.status = "running"
        elif isinstance(event, PoolReleased):
            self._pool_released(self._get(event.run_id))
        elif isinstance(event, RunFinished):
            self._finished(self._get(event.run_id), event, now)
        elif isinstance(event, Requeue):
            self._requeue(self._get(event.run_id))
        elif isinstance(event, RigState):
            if event.rig not in ALL_RIGS:
                raise ValueError(f"rig {event.rig}: one of {sorted(ALL_RIGS)}")
            (self._up.add if event.up else self._up.discard)(event.rig)
        else:
            raise TypeError(f"unknown event {event!r}")

    def _pr(self, ev: PrBuild, now: datetime) -> None:
        old = next((e for e in self._entries.values() if e.run_type == "pr" and e.pr == ev.pr
                    and e.status == "queued"), None)
        e = self._new("pr", ev.triggered_at, pr=ev.pr, candidate=ev.build_id, merge_base=ev.merge_base or None,
                      triggers=[ev])
        if old is not None:
            del self._entries[old.run_id]
            self._emit(Replaced(old.run_id, e.run_id, "pr", now))

    def _merge(self, ev: MergeBuild, now: datetime) -> None:
        main = MainBuild(ev.build_id, ev.previous_main, ev.merged_pr, ev.manifest_ref, ev.triggered_at)
        self._mains.append(main)
        e = next((x for x in self._entries.values() if x.run_type == "merge" and x.status == "queued"), None)
        if e is None:
            self._new("merge", ev.triggered_at, candidate=ev.build_id, mains=[main], triggers=[ev])
            return
        e.candidate, e.triggered_at = ev.build_id, ev.triggered_at
        e.mains.append(main)
        e.triggers.append(ev)
        e.consumed, e.advanced_to, e.deadline_done = timedelta(0), ev.triggered_at, False      # choice 7
        self._consume(e, now)
        self._emit(Coalesced(e.run_id, ev.build_id, now))

    def _rc(self, ev: RcBuild, now: datetime) -> None:
        same = [x for x in self._entries.values() if x.run_type == "rc" and x.train == ev.train]
        flight = next((x for x in same if x.status in ("dispatched", "running") and x.superseded_by is None), None)
        queued = next((x for x in same if x.status == "queued"), None)
        e = self._new("rc", ev.triggered_at, candidate=ev.build_id, train=ev.train, signoff_at=ev.signoff_at,
                      triggers=[ev], fixed_deadline=sd.deadline("rc", ev.triggered_at, signoff_at=ev.signoff_at))
        if queued is not None:
            del self._entries[queued.run_id]
            e.supersedes, e.reservation = queued.supersedes, queued.reservation
            if queued.supersedes in self._entries:
                self._entries[queued.supersedes].superseded_by = e.run_id
            self._emit(Replaced(queued.run_id, e.run_id, "rc", now))
        if flight is not None:
            flight.superseded_by, e.supersedes = e.run_id, flight.run_id
            e.reservation, flight.reservation = flight.reservation or _Reservation(now), None    # it transfers
            label = f"superseded by {ev.build_id}"
            self._emit(Supersede(flight.run_id, e.run_id, ev.build_id, label, now))
            self._emit(Alert(sd.rc_start_deadline_alert(), "RC run superseded", flight.run_id,
                             f"RC run {flight.run_id} {label} (run {e.run_id}); the release manager decides", now))

    def _calibration(self, kind: str, at: datetime, requeue_of: str | None) -> None:
        night, start, due = calibration_slot(kind, at, self.next_all_quiet)
        if any(x.run_type == "calibration" and x.calibration == kind and x.night == night and x.status != "aborting"
               for x in self._entries.values()):
            return                                                          # choice 12: one run per window
        e = self._new("calibration", start, status="scheduled", eligible_at=start, calibration=kind, night=night,
                      fixed_deadline=due, requeue_of=requeue_of)
        self._emit(Scheduled(e.request(), night, start, due, self.clock.now()))

    def _pool_released(self, e: _Entry) -> None:
        if e.run_type != "rc":
            raise ValueError(f"PoolReleased is an RC run's event, not {e.run_type!r} (11.2)")
        self._end_reservation(e, self.clock.now())
        self._release(e, POOL)

    def _end_reservation(self, e: _Entry, now: datetime) -> None:
        if e.reservation is not None:
            r, e.reservation = e.reservation, None
            self._emit(ReservationEnded(e.run_id, r.since, now, tuple(r.held), now))

    def _release(self, e: _Entry, rigs: frozenset[int] = ALL_RIGS) -> None:
        for r in [r for r, holder in self._held.items() if holder == e.run_id and r in rigs]:
            del self._held[r]
        e.rigs = tuple(r for r in e.rigs if r not in rigs)

    def _requeue(self, e: _Entry) -> None:
        if e.status != "dispatched":
            raise ValueError(f"run {e.run_id} is {e.status}: only a dispatched run goes back to the queue (7.2)")
        self._release(e)
        e.status = "queued"
        if e.run_type in ("pr", "merge"):
            e.advanced_to = e.dispatched_at                    # the run never started: its deadline ran on
            self._consume(e, self.clock.now())

    def _finished(self, e: _Entry, ev: RunFinished, now: datetime) -> None:
        if e.status in ("queued", "scheduled"):
            raise ValueError(f"run {e.run_id} never started: it cannot finish")
        self._release(e)
        del self._entries[e.run_id]
        self._end_reservation(e, now)
        if e.run_type == "merge" and ev.status in COMPLETED:
            self.last_completed_main = e.candidate
            keep = next((i for i, m in enumerate(self._mains) if m.build_id == e.candidate), None)
            if keep is not None:
                del self._mains[:keep + 1]
        if e.run_type == "nightly" and ev.canaries_passed:
            for x in [x for x in self._entries.values() if x.catch_up and x.status == "queued"]:
                del self._entries[x.run_id]
                self._emit(Replaced(x.run_id, e.run_id, "nightly", now))
            self._write("canaries_pending", "", now)
            self._write("nightly_skipped_for", "", now)

    # -- timers ------------------------------------------------------------------------------------------------
    def _timers(self, now: datetime) -> None:
        due = sd.first_local(self._last_sweep, NIGHTLY_AT)
        if due <= now:
            latest = due
            while (nxt := sd.first_local(latest, NIGHTLY_AT)) <= now:          # choice 9
                latest = nxt
            self._nightly(latest, now)
        for e in self._entries.values():
            if e.status == "scheduled" and e.eligible_at is not None and e.eligible_at <= now:
                e.status = "queued"

    def _nightly(self, at: datetime, now: datetime) -> None:
        night = sd.to_local(at).date()
        holder = next((self._held[r] for r in sorted(NIGHTLY_RIGS) if self._held.get(r) in self._entries
                       and self._entries[self._held[r]].run_type == "rc"), None)
        stale = [x for x in self._entries.values() if x.catch_up and x.status == "queued"]
        if holder is None:
            e = self._new("nightly", at, night=night, fixed_deadline=sd.deadline("nightly", at))
        else:
            skipped = self._run_ids("nightly")
            e = self._new("nightly", now, night=night, catch_up=True)
            self._emit(NightlySkipped(skipped, holder, now))
            self._emit(Alert(sd.nightly_skipped_alert(), "Nightly skipped for RC", skipped,
                             f"RC run {holder} holds the Nightly rigs at 22:00; catch-up run {e.run_id} on rig "
                             f"{CANARY_RIG} when it is free", now))
            self._write("nightly_skipped_for", holder, now)
            self._write("canaries_pending", sd.skipped_reason(holder), now)
        for x in stale:
            del self._entries[x.run_id]
            self._emit(Replaced(x.run_id, e.run_id, "nightly", now))

    # -- start deadlines ---------------------------------------------------------------------------------------
    def _deadlines(self, now: datetime) -> None:
        for e in list(self._entries.values()):
            if e.run_type in ("pr", "merge"):
                if e.status != "queued" or e.consumed < e.budget or e.deadline_done:
                    continue
                self._deadline(e, now, now - (e.consumed - e.budget))
            elif e.run_type == "rc":
                if e.status == "queued" and not e.deadline_done and e.fixed_deadline is not None \
                        and now >= e.fixed_deadline:
                    self._deadline(e, now, e.fixed_deadline)
            elif not e.catch_up and e.status in ("queued", "dispatched") and e.fixed_deadline is not None \
                    and now >= e.fixed_deadline:
                self._deadline(e, now, e.fixed_deadline)

    def _deadline(self, e: _Entry, now: datetime, due: datetime) -> None:
        order = self._order()
        ahead = next(i for i, x in enumerate(order) if x.run_id == e.run_id) if e.status == "queued" else 0
        pool_runs = {h for r, h in self._held.items() if r in POOL}
        d = sd.outcome(e.run_type, e.triggered_at, now, run_id=e.run_id, deadline_at=due, signoff_at=e.signoff_at,
                       calibration=e.calibration, runs_ahead=ahead + len(pool_runs), queue_depth=len(order),
                       on_rig=e.status == "dispatched")
        assert d is not None
        self._emit(DeadlineReached(e.request(), d, now))
        if d.disposition == "abort":
            if e.status == "queued":
                del self._entries[e.run_id]
            else:
                e.status = "aborting"                      # the Orchestrator writes it and tears down (11.2)
        else:
            e.deadline_done = True
        if e.run_type == "pr":
            self._saturated(now, f"PR run {e.run_id} aborted_budget (pool_saturated); queue depth {len(order)}",
                            e.run_id)
        elif e.run_type == "rc":
            self._emit(Alert(sd.rc_start_deadline_alert(), "RC start deadline", e.run_id,
                             f"RC run {e.run_id} not started by {due.isoformat()}; the release manager decides", now))
        elif d.result is not None and d.alert is not None:
            self._emit(Alert(d.alert, "aborted_harness", e.run_id, f"start_failed: {d.result.detail}", now))
        for key, value in d.state:
            self._write(key, value, now)
        if d.requeue:
            assert e.calibration is not None
            self._calibration(e.calibration, now, e.run_id)

    # -- starts ------------------------------------------------------------------------------------------------
    def _order(self) -> list[_Entry]:
        q = [e for e in self._entries.values() if e.status == "queued"]
        return sorted(q, key=lambda e: (PRIORITY[e.run_type], e.triggered_at, e.seq))

    def _calibration_weekend(self, night: date | None) -> bool:
        return any(x.run_type == "calibration" and x.calibration == "weekend" and x.night is not None
                   and night is not None and 0 <= (night - x.night).days <= 1 for x in self._entries.values())

    def _wants(self, e: _Entry) -> tuple[frozenset[int], frozenset[int]]:
        """(the rigs the run takes, the rigs it cannot start without), both among the rigs in service (choice 1)."""
        up = frozenset(self._up)
        if e.run_type in ("pr", "merge"):
            return up & POOL, frozenset()
        if e.run_type == "rc":
            return up & ALL_RIGS, frozenset()
        if e.run_type == "calibration":
            return up & (POOL if e.calibration == "weekend" else EMERGENCY_RIGS), frozenset()
        if e.catch_up or self._calibration_weekend(e.night):
            return up & {CANARY_RIG}, frozenset({CANARY_RIG})
        return up & NIGHTLY_RIGS, frozenset({CANARY_RIG})

    def _eligible(self, e: _Entry, now: datetime) -> bool:
        if e.run_type in ("nightly", "calibration"):
            return True
        if not self.operating_day(now):
            return False
        if e.run_type == "pr":
            if self.reserved:
                return False
            return not any(x.run_type == "pr" and x.pr == e.pr and x.status != "queued"
                           for x in self._entries.values())
        return True

    def _dispatch(self, now: datetime) -> None:
        claimed: set[int] = set()
        for e in self._order():
            if not self._eligible(e, now):
                continue
            want, need = self._wants(e)
            free = frozenset(self._up) - set(self._held)
            if want and need <= want and want <= free and not want & claimed:
                self._start(e, want, now)
            else:
                claimed |= want

    def _start(self, e: _Entry, rigs: frozenset[int], now: datetime) -> None:
        e.status, e.rigs, e.dispatched_at = "dispatched", tuple(sorted(rigs)), now
        for r in rigs:
            self._held[r] = e.run_id
        if e.run_type == "merge":
            e.base = self.last_completed_main or (e.mains[0].previous_main if e.mains else None)
            ids = [m.build_id for m in self._mains]
            lo = ids.index(e.base) + 1 if e.base in ids else 0
            hi = ids.index(e.candidate) + 1 if e.candidate in ids else len(ids)
            e.range_ = tuple(self._mains[lo:hi]) or tuple(e.mains)
        if e.run_type == "rc" and e.reservation is None:
            e.reservation = _Reservation(now)
        if e.run_type == "nightly":
            e.aa_pass = not e.catch_up and 4 in rigs
        self._emit(RunStart(e.request(), e.rigs, now))

    # -- announcements -----------------------------------------------------------------------------------------
    def _pool_free_at(self, now: datetime) -> datetime:
        rc = next(e for e in self._entries.values() if e.reservation is not None)
        start = rc.dispatched_at if rc.status in ("dispatched", "running") and rc.dispatched_at else rc.triggered_at
        expected = start + RC_POOL_FREE_EXPECTED
        return expected if now < expected else start + RC_POOL_FREE_WORST

    def _announce(self, now: datetime) -> None:
        order = self._order()
        for pos, e in enumerate(order, 1):
            if e.last_queued is None or now - e.last_queued >= RUN_QUEUED_EVERY:
                e.last_queued = now
                covers = tuple(getattr(t, "build_id", "") for t in e.triggers)
                self._emit(RunQueued(e.run_id, e.run_type, pos, len(order), now, self._deadline_at(e, now), covers))
            if e.run_type == "pr":
                held = next((x.reservation for x in self._entries.values() if x.reservation is not None), None)
                if held is not None and e.run_id not in held.held:
                    held.held.append(e.run_id)
                text = None
                if not self.operating_day(now):
                    text = PLACEHOLDER_NIGHT
                elif held is not None:
                    text = PLACEHOLDER_HELD.format(hhmm=sd.to_local(self._pool_free_at(now)).strftime("%H:%M"))
                if text is not None and text != e.placeholder:
                    e.placeholder = text
                    assert e.pr is not None
                    self._emit(Placeholder(e.run_id, e.pr, text, now))

    def _saturation(self, now: datetime) -> None:
        if self.reserved or not self.operating_day(now):
            return                                                          # choice 3
        prs = [e for e in self._order() if e.run_type == "pr"]
        waiting = [e for e in prs if e.consumed > SATURATION_WAIT]
        if len(prs) > SATURATION_DEPTH or waiting:
            oldest = max((e.consumed for e in prs), default=timedelta(0))
            self._saturated(now, f"queue depth {len(self._order())} ({len(prs)} PR runs); oldest PR run waiting "
                                 f"{int(oldest.total_seconds() // 60)} min", None)

    def _saturated(self, now: datetime, detail: str, run_id: str | None) -> None:
        if self._last_saturation is not None and now - self._last_saturation < SATURATION_REPEAT:
            return
        self._last_saturation = now
        self._emit(Alert(sd.pool_saturated_alert(), "pool saturated", run_id, detail, now))
