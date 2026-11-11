"""Triage SLA clocks, the business-hours calendar, assignment and the escalation steps (9.12; 9.10 "SLA escalation").

- `BusinessClock` is the clock every 9.12 SLA runs on: business hours on the business days of the exchange calendar
  (`calendar.is_business_day`, XNYS), less any firm holidays from configuration. `add`, `add_days`, `elapsed` and
  `next_day_at` are the arithmetic the SLA table needs.
- `OnCall.from_doc(doc)` reads `ops/oncall.yaml` (4.16, 11.6) for who holds a role on a day: the role holders, the
  flow owner per tier and the weekly triage and harness on-call rotations. `person(recipient, day, tier=...)` turns
  a 9.10 recipient (`system_owner`, `harness_on_call`, `flow_owner`, ...) into a login.
- `SlaItem` and `due(item)` are the 9.12 SLA table: when an item is due, or None when no clock runs.
- `open_items(store)` is the triage queue of 9.5 with its SLA clock and assignee: every open, non-synthetic, tiered
  finding without a decision, the clock being its open two-person proposal's when it has one.
- `escalate(store, now, oncall=...)` runs the 9.12 escalation steps once: it writes the step-1 assignment and the
  step reached into `triage_assignments` and returns the alerts (`EscalationAlert`, each a 9.10 "SLA escalation"
  route and its one-line detail) and the digest and breach lists. Calling it again does not repeat a step.
- `breaches(store, now)` lists the step-4 items for the weekly metrics summary, and `rc_signoff_refusals(store,
  rc_run_id, now)` the items that refuse RC sign-off (9.12 step 4, 10.9).

Choices where the spec is ambiguous (each is reported):

1. Business hours are 09:00 to 17:00 America/New_York on XNYS business days (9.12 names business hours and days,
   not the hours). One business day is the 8 business hours of one such day, so a finding due "within one business
   day" of Friday 17:00 is due Monday 17:00, and "4 business hours" after Friday 17:00 is Monday 13:00. An instant
   outside business hours starts its clock at the next opening.
2. The SLA table, per item (`due`): a PR-run tier-1 finding has no clock (its SLA is "before merge", which the
   commit status enforces); PR-run tier 2 and 3 and every merge, Nightly and Calibration finding are due one
   business day after creation ("every finding disposed within one business day"); an RC-run finding is due at its
   sign-off deadline (GA minus N business days, `deadline`), with no clock when the caller does not know it; an open
   two-person proposal is due 4 business hours after it was proposed; an expired mask or noise signature 5 business
   days after its expiry; a decision without a ticket or an abandoned outbox job one business day after it appeared
   in the digest. "Report opened by 10:00 the next business day" has no store record of the opening, so it is not
   clocked here.
3. Escalation (9.12 steps): step 1 assigns an item no one holds (no current `triage_assignments` row) 4 business
   hours after it was created, to the triage rotation member of that day (`assigned_by` "system"), or to the system
   owner when no rotation entry covers the day. Steps 2, 3 and 4 are 1, 3 and 5 business days past the SLA. For an
   open proposal the business days count from the proposal (9.10: "a two-person proposal unanswered for one business
   day"). An item with no clock reaches only step 1. The step reached is stored on the item's current assignment, so
   a step fires once; a step-4 item crossed in one call from step 1 raises the step-3 alert and the breach.
4. Alerts: tier-1 findings and proposals raise the P2 "SLA escalation" alert at step 2 (system owner, naming the
   rotation member) and again at step 3 with the tier's flow owner added; tier 2 and 3 findings are "digest only" at
   step 2 (`digest_only`) and head the merge digest from step 3 (`merge_digest_head`), as every tier does.
5. RC sign-off is refused by any open tier-1 finding of the RC run and by any open item five business days past its
   SLA (9.12 step 4, 10.9).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import calendar as cal
from .orchestrator import alerts
from .orchestrator.alerts import AlertRoute
from .store import Store

BUSINESS_OPEN = time(9, 0)                     # choice 1
BUSINESS_CLOSE = time(17, 0)
BUSINESS_DAY = timedelta(hours=8)
PROPOSAL_SLA = timedelta(hours=4)              # 9.12: proposals answered within 4 business hours
UNCLAIMED = timedelta(hours=4)                 # 9.12 step 1
REEVALUATE_DAYS = 5                            # 9.12: expired masks and noise signatures
STEP_DAYS = {2: 1, 3: 3, 4: 5}                 # 9.12 steps 2 to 4: business days past the SLA
ITEM_KINDS = ("finding", "proposal", "expired_mask", "noise_signature", "decision_without_ticket", "abandoned_job")
SYSTEM_ASSIGNER = "system"                     # 8.3 triage_assignments.assigned_by for the escalation's assignment
TRIAGE_ROTATION = "triage"                     # ops/oncall.yaml rotation names (4.16)
HARNESS_ROTATION = "harness_oncall"


def _aware(at: datetime | str) -> datetime:
    d = datetime.fromisoformat(at) if isinstance(at, str) else at
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def iso(at: datetime | None) -> str | None:
    """UTC text to the second, as the store writes timestamps (8.3)."""
    return None if at is None else cal.to_utc(at).isoformat(timespec="seconds")


# -- the business clock (choice 1) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessClock:
    """Business hours on XNYS business days, America/New_York, less `holidays` (firm holidays from config)."""

    holidays: frozenset[date] = frozenset()
    opens: time = BUSINESS_OPEN
    closes: time = BUSINESS_CLOSE

    def is_business_day(self, d: date) -> bool:
        return d not in self.holidays and cal.is_business_day(d)

    def _hours(self, d: date) -> tuple[datetime, datetime]:
        return cal.wall_time(d, self.opens), cal.wall_time(d, self.closes)

    def opening(self, at: datetime) -> datetime:
        """`at` when it is inside business hours, else the next opening (UTC)."""
        t = cal.to_utc(at)
        d = cal.ny(t).date()
        while True:
            if self.is_business_day(d):
                start, end = self._hours(d)
                if t < end:
                    return max(t, start)
            d += timedelta(days=1)

    def add(self, at: datetime, span: timedelta) -> datetime:
        """`at` plus `span` of business time."""
        t, left = self.opening(at), span
        while True:
            end = self._hours(cal.ny(t).date())[1]
            if left <= end - t:
                return t + left
            left -= end - t
            t = self.opening(end)

    def add_days(self, at: datetime, days: int) -> datetime:
        """`at` plus `days` business days of 8 business hours each (choice 1)."""
        return self.add(at, days * BUSINESS_DAY)

    def elapsed(self, start: datetime, end: datetime) -> timedelta:
        """The business time from `start` to `end`; zero when `end` is not later."""
        total, t, stop = timedelta(0), self.opening(start), cal.to_utc(end)
        while t < stop:
            day_end = self._hours(cal.ny(t).date())[1]
            total += min(day_end, stop) - t
            t = self.opening(day_end)
        return total

    def next_day_at(self, at: datetime, wall: time) -> datetime:
        """Wall time `wall` on the first business day after `at`'s New York date ("10:00 the next business day")."""
        d = cal.ny(at).date() + timedelta(days=1)
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return cal.wall_time(d, wall)


def business_age(clock: BusinessClock, start: datetime, end: datetime) -> str:
    """`1d 03h` or `2h 30m` of business time, for digest lines (a business day is 8 hours, choice 1)."""
    secs = int(clock.elapsed(start, end).total_seconds())
    days, rest = divmod(secs, int(BUSINESS_DAY.total_seconds()))
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours:02d}h" if days else f"{hours}h {rest // 60:02d}m"


# -- the roster (ops/oncall.yaml, 4.16) -----------------------------------------------------------------------------

def _day(v: Any) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v))


@dataclass(frozen=True)
class Rotation:
    """One `rotations` entry of ops/oncall.yaml: who holds the rotation from `start` to `end`, both included."""

    rotation: str
    person: str
    start: date
    end: date


@dataclass(frozen=True)
class OnCall:
    """Who holds each 9.12 role on a day (ops/oncall.yaml)."""

    roles: Mapping[str, str] = field(default_factory=dict)         # role -> person
    flow_owners: Mapping[int, str] = field(default_factory=dict)   # tier -> person
    rotations: tuple[Rotation, ...] = ()

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "OnCall":
        """Read a loaded ops/oncall.yaml document (its 4.16 schema: roles, flow_owners, rotations)."""
        roles = {str(e["role"]): str(e["person"]) for e in doc.get("roles") or ()}
        owners = {int(e["tier"]): str(e["person"]) for e in doc.get("flow_owners") or ()}
        rots = tuple(Rotation(str(e["rotation"]), str(e["person"]), _day(e["from"]), _day(e["to"]))
                     for e in doc.get("rotations") or ())
        return cls(roles, owners, rots)

    def role(self, name: str) -> str | None:
        return self.roles.get(name)

    def flow_owner(self, tier: int | None) -> str | None:
        return None if tier is None else self.flow_owners.get(tier)

    def rotation_member(self, rotation: str, day: date) -> str | None:
        """The person on `rotation` on `day`: the covering entry that started last."""
        cover = [r for r in self.rotations if r.rotation == rotation and r.start <= day <= r.end]
        return max(cover, key=lambda r: r.start).person if cover else None

    def person(self, recipient: str, day: date, *, tier: int | None = None) -> str | None:
        """The login behind a 9.10 recipient on `day`: the rotations for `harness_on_call` and `triage_rotation`, the
        tier's owner for `flow_owner`, else the role holder; None for a channel or a role nobody holds."""
        if recipient == "harness_on_call":
            return self.rotation_member(HARNESS_ROTATION, day)
        if recipient == "triage_rotation":
            return self.rotation_member(TRIAGE_ROTATION, day)
        if recipient == "flow_owner":
            return self.flow_owner(tier)
        return self.role(recipient)


# -- the SLA table (choice 2) ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SlaItem:
    """One clocked thing of the 9.12 SLA table."""

    kind: str                                 # one of ITEM_KINDS
    ref: str                                  # finding natural key, proposal id, decision id or outbox job id
    created_at: datetime                      # created, proposed, expired, or first listed in the digest
    tier: int | None = None
    run_type: str | None = None
    deadline: datetime | None = None          # RC run findings: the sign-off deadline (GA minus N business days)

    def __post_init__(self):
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"SLA item kind {self.kind!r}; one of {', '.join(ITEM_KINDS)}")
        cal.to_utc(self.created_at)


def due(item: SlaItem, clock: BusinessClock | None = None) -> datetime | None:
    """When the item is due by the 9.12 SLA table (choice 2), or None when no clock runs."""
    c = clock or BusinessClock()
    if item.kind == "finding":
        if item.run_type == "rc":
            return None if item.deadline is None else cal.to_utc(item.deadline)
        if item.run_type == "pr" and item.tier == 1:
            return None
        return c.add_days(item.created_at, 1)
    if item.kind == "proposal":
        return c.add(item.created_at, PROPOSAL_SLA)
    if item.kind in ("expired_mask", "noise_signature"):
        return c.add_days(item.created_at, REEVALUATE_DAYS)
    return c.add_days(item.created_at, 1)


# -- the triage queue ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenItem:
    """A finding in the triage queue (9.5): its SLA clock (the open proposal's when it has one) and assignee."""

    finding_id: str
    tier: int
    run_id: str
    run_type: str
    checkpoint_id: str | None
    kind: str
    category: str | None
    description: str
    created_at: datetime
    item: SlaItem
    due_at: datetime | None
    assignment_id: int | None = None
    assignee: str | None = None
    claimed_at: str | None = None
    escalation_step: int = 0
    proposal_id: int | None = None
    proposal_action: str | None = None
    proposed_by: str | None = None

    def past_due(self, now: datetime, clock: BusinessClock) -> timedelta | None:
        """Business time past the SLA (choice 3): from the proposal for an open proposal, else from `due_at`."""
        if self.item.kind == "proposal":
            return clock.elapsed(self.item.created_at, now)
        return None if self.due_at is None else clock.elapsed(self.due_at, now)

    def step_due(self, now: datetime, clock: BusinessClock) -> int:
        """The highest 9.12 step from 2 to 4 the item has reached at `now` by its clock (0 below step 2)."""
        past = self.past_due(now, clock)
        reached = [s for s, d in STEP_DAYS.items() if past is not None and past >= d * BUSINESS_DAY]
        return max(reached, default=0)


_QUEUE_SQL = """
SELECT f.finding_id AS fid, f.natural_key, f.tier, f.created_at, f.run_id, f.checkpoint_id, f.kind, f.category,
       f.description, r.run_type, p.proposal_id, p.proposed_at, p.proposed_by, p.action AS proposal_action,
       a.assignment_id, a.assignee, a.claimed_at, a.escalation_step
FROM findings f JOIN runs r ON r.run_id = f.run_id
LEFT JOIN proposals p ON p.finding_id = f.finding_id AND p.state = 'proposed'
LEFT JOIN triage_assignments a ON a.finding_id = f.finding_id AND a.released_at IS NULL
WHERE f.triage_state = 'open' AND f.synthetic = 0 AND f.held = 0 AND f.tier IS NOT NULL AND f.decision_id IS NULL
ORDER BY f.created_at, f.natural_key
"""


def open_items(store: Store, *, clock: BusinessClock | None = None,
               rc_deadlines: Mapping[str, datetime] | None = None) -> tuple[OpenItem, ...]:
    """The triage queue, oldest first. `rc_deadlines` gives each RC run's sign-off deadline (choice 2)."""
    c = clock or BusinessClock()
    out = []
    for r in store.query(_QUEUE_SQL):
        created = _aware(r["created_at"])
        if r["proposal_id"] is not None:
            item = SlaItem("proposal", str(r["proposal_id"]), _aware(r["proposed_at"]), r["tier"], r["run_type"])
        else:
            item = SlaItem("finding", r["natural_key"], created, r["tier"], r["run_type"],
                           (rc_deadlines or {}).get(r["run_id"]))
        out.append(OpenItem(r["natural_key"], r["tier"], r["run_id"], r["run_type"], r["checkpoint_id"], r["kind"],
                            r["category"], r["description"], created, item, due(item, c), r["assignment_id"],
                            r["assignee"], r["claimed_at"], r["escalation_step"] or 0, r["proposal_id"],
                            r["proposal_action"], r["proposed_by"]))
    return tuple(out)


# -- escalation (9.12 steps, 9.10 "SLA escalation"; choices 3 and 4) -------------------------------------------------

@dataclass(frozen=True)
class Assignment:
    """A step-1 assignment written by `escalate`."""

    finding_id: str
    assignee: str


@dataclass(frozen=True)
class EscalationAlert:
    """A 9.10 "SLA escalation" alert to dispatch: the route, the people behind it and one line of detail."""

    finding_id: str
    tier: int
    step: int
    route: AlertRoute
    people: Mapping[str, str]                 # recipient role -> login, where the roster names one
    detail: str
    run_id: str
    run_type: str

    @property
    def key(self) -> str:
        """The dedup key of the alert: one per item and step."""
        return f"sla:{self.finding_id}:step{self.step}"


@dataclass(frozen=True)
class EscalationResult:
    """What one `escalate` call did and what the caller sends."""

    assigned: tuple[Assignment, ...] = ()
    alerts: tuple[EscalationAlert, ...] = ()
    digest_only: tuple[str, ...] = ()         # tier 2 and 3 items at step 2 (9.12: digest only)
    merge_digest_head: tuple[str, ...] = ()   # items at step 3 or more: they head the merge digest
    breaches: tuple[str, ...] = ()            # items at step 4: weekly summary, RC sign-off refused
    unassigned: tuple[str, ...] = ()          # step 1 due but nobody on the roster to assign


def _assignee(oncall: OnCall, day: date) -> str | None:
    return oncall.rotation_member(TRIAGE_ROTATION, day) or oncall.role("system_owner")


def _alert(it: OpenItem, step: int, assignee: str, oncall: OnCall, now: datetime,
           clock: BusinessClock) -> EscalationAlert:
    route = alerts.sla_escalation(flow_owner=step >= 3)
    people = rotation_people(oncall, route.notified, cal.ny(now).date(), tier=it.tier)
    past = it.past_due(now, clock) or timedelta(0)
    days = int(past / BUSINESS_DAY)
    if it.item.kind == "proposal":
        detail = (f"proposal {it.proposal_id} on {it.finding_id} ({it.proposal_action} by {it.proposed_by}, tier "
                  f"{it.tier}) unanswered for {days} business days; rotation member {assignee}")
    else:
        due_text = cal.fmt_ny(it.due_at) if it.due_at is not None else "none"
        detail = (f"{it.finding_id} (tier {it.tier}) {days} business days past its SLA (due {due_text}); assigned "
                  f"to {assignee}")
    return EscalationAlert(it.finding_id, it.tier, step, route, people, detail, it.run_id, it.run_type)


def escalate(store: Store, now: datetime, *, oncall: OnCall, clock: BusinessClock | None = None,
             rc_deadlines: Mapping[str, datetime] | None = None) -> EscalationResult:
    """Run the 9.12 escalation steps at `now` (choices 3 and 4): write the step-1 assignments and the steps reached,
    and return what to send. Idempotent: a step already recorded on the item's assignment does not fire again."""
    c = clock or BusinessClock()
    today = cal.ny(now).date()
    assigned: list[Assignment] = []
    sent: list[EscalationAlert] = []
    digest_only: list[str] = []
    head: list[str] = []
    breach: list[str] = []
    unassigned: list[str] = []
    for it in open_items(store, clock=c, rc_deadlines=rc_deadlines):
        step, aid, who = it.escalation_step, it.assignment_id, it.assignee
        if aid is None and c.elapsed(it.created_at, now) >= UNCLAIMED:
            who = _assignee(oncall, today)
            if who is None:
                unassigned.append(it.finding_id)
                continue
            aid = store.write_triage_assignment(it.finding_id, assignee=who, assigned_by=SYSTEM_ASSIGNER,
                                                sla_due_at=iso(it.due_at), escalation_step=1, at=iso(now))
            step = 1
            assigned.append(Assignment(it.finding_id, who))
        target = it.step_due(now, c)
        if aid is not None and target > step:
            store.update("triage_assignments", {"assignment_id": aid}, {"escalation_step": target})
            alerting = it.tier == 1 or it.item.kind == "proposal"
            if alerting and step < 3 and target >= 2:
                sent.append(_alert(it, min(target, 3), who or "", oncall, now, c))
            elif target == 2:
                digest_only.append(it.finding_id)
            step = target
        if step >= 3:
            head.append(it.finding_id)
        if step >= 4:
            breach.append(it.finding_id)
    return EscalationResult(tuple(assigned), tuple(sent), tuple(digest_only), tuple(head), tuple(breach),
                            tuple(unassigned))


def breaches(store: Store, now: datetime, *, clock: BusinessClock | None = None,
             rc_deadlines: Mapping[str, datetime] | None = None) -> tuple[OpenItem, ...]:
    """Open items five business days past their SLA at `now`: the SLA breaches of the weekly summary (9.12 step 4)."""
    c = clock or BusinessClock()
    return tuple(it for it in open_items(store, clock=c, rc_deadlines=rc_deadlines) if it.step_due(now, c) >= 4)


@dataclass(frozen=True)
class SignoffRefusal:
    """An item that refuses RC sign-off until it has a decision (9.12 step 4, 10.9)."""

    finding_id: str
    reason: str


def rc_signoff_refusals(store: Store, rc_run_id: str, now: datetime, *, clock: BusinessClock | None = None,
                        rc_deadlines: Mapping[str, datetime] | None = None) -> tuple[SignoffRefusal, ...]:
    """What refuses RC sign-off at `now` (choice 5): an untriaged tier-1 finding of the RC run, or any open item five
    business days past its SLA. Empty: nothing in the triage queue refuses it."""
    c = clock or BusinessClock()
    out: list[SignoffRefusal] = []
    for it in open_items(store, clock=c, rc_deadlines=rc_deadlines):
        if it.run_id == rc_run_id and it.tier == 1:
            out.append(SignoffRefusal(it.finding_id, "untriaged tier-1 item at RC sign-off"))
        elif it.step_due(now, c) >= 4:
            out.append(SignoffRefusal(it.finding_id, "five business days past its SLA"))
    return tuple(out)


def rotation_people(oncall: OnCall, recipients: Iterable[str], day: date, *, tier: int | None = None
                    ) -> dict[str, str]:
    """The logins behind `recipients` on `day` (channels and unheld roles are left out)."""
    return {r: p for r in recipients if (p := oncall.person(r, day, tier=tier))}
