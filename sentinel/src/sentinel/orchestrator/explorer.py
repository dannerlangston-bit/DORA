"""Explorer boundary: the deny list, the session budgets and the order-stage path (6.8, 2.9; F37).

- The deny list (`DenyEntry`, `DEFAULT_DENY_LIST` from the table of 6.8) matches a target by window
  title, by resolved control name and under-point OCR label, and by landmark-anchored geometry (the
  resolver's `target_kind`). A hit is a `deny_profile` refusal in the slow half (gateway.py).
- Budgets, enforced by the Orchestrator and never the prompt: 400 actions, 90 minutes, 25 dollars per
  session (and the weekly 500 shared with agent rescues, 11.4), early stop after 20 consecutive actions
  with no new screenshot hash, and the RC run's deadline. Exhaustion ends the session, never the run.
- Three denied attempts in one session end it; an attempted-send reason ends it at once with an S3
  ticket and counts toward the per-run trip of ten (`DropLedger`, gateway.py).
- The ticket or dialog flag set or unknown in a session: all input dropped, the window closed by the
  gateway's tokened Escape, the session ended with the finding `explorer_order_stage` (S1, the safety
  routing of 9.10 plus the Explorer owner), counted toward the ten; the run continues. The kill switch
  does not fire for it (the 30 s dialog rule is for scripted flows and agent rescue only).
- The model endpoint down when the session is due (`agent_available: false`): the session is `not_run
  (reason: model_unavailable)` and the RC run is unaffected (10.7).
- Teardown replays the mutation ledger; an item that cannot be deleted or read back blocks the Explorer
  login with `env_error (gate: teardown, reason: precondition:explorer_ledger_clean)` on the Explorer
  stage, the item in `detail`, P2 to the Explorer owner.

Choices (reported): the label patterns of `DEFAULT_DENY_LIST` are this module's reading of the 6.8
table and fail closed (for example every label containing "alert" is denied, not only alert creation);
the versioned list in `sentinel-config` replaces them through `DenyEntry`. Budget checks run after each
action in the order actions, time, no-change, session dollars, weekly dollars.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import alerts
from .clock import Clock
from .gateway import DropLedger, GatewayDrop
from .results import ControlResult, FindingSpec, Outcome, SubsetOutcome, safety_abort_outcome


@dataclass(frozen=True)
class DenyEntry:
    """One deny-list row: a name and its title regex, label regex and geometry kinds."""

    name: str
    title: str | None = None
    label: str | None = None
    geometry: frozenset[str] = frozenset()

    def hit(self, *, title: str | None, labels: Sequence[str], target_kind: str | None) -> bool:
        if self.title and title and re.search(self.title, title, re.I):
            return True
        if self.label and any(re.search(self.label, s, re.I) for s in labels if s):
            return True
        return target_kind is not None and target_kind in self.geometry


DEFAULT_DENY_LIST: tuple[DenyEntry, ...] = (
    DenyEntry("chat", r"chat|support", r"chat|message"),
    DenyEntry("share", r"share|tos\.mx", r"share|tos\.mx"),
    DenyEntry("setup", r"setup|application settings|order defaults", r"^setup$|application settings|order defaults",
              frozenset({"setup_menu", "application_settings"})),
    DenyEntry("adjust_account", r"adjust account", r"adjust account"),
    DenyEntry("logout_account_switch", None, r"log ?out|log off", frozenset({"account_selector"})),
    DenyEntry("leaves_rig_or_persists", None, r"^help$|browser|export|print|save workspace|delete workspace"),
    DenyEntry("persistent_and_send", None, r"alert|saved orders|blast all|flatten|confirm and send|^send$",
              frozenset({"active_trader_order_column"})),
    DenyEntry("opens_ticket", None, r"^(buy|sell)\b|buy mkt|sell mkt",
              frozenset({"ladder_bid_ask_cell", "level2_bid_ask_cell", "order_entry_button", "ladder_order_cell",
                         "order_bubble"})),
)


def match_deny(*, title: str | None, labels: Sequence[str], target_kind: str | None,
               deny_list: Sequence[DenyEntry] = DEFAULT_DENY_LIST, end_kind: str | None = None) -> str | None:
    """The first deny-list entry the target hits (title, label or geometry), for `SafetyContext.deny_list_hit`.
    `end_kind` is a drag's end target: a gesture that ends on denied geometry is denied too (6.8)."""
    for e in deny_list:
        if e.hit(title=title, labels=labels, target_kind=target_kind) or (
                end_kind is not None and e.hit(title=None, labels=(), target_kind=end_kind)):
            return e.name
    return None


@dataclass(frozen=True)
class ExplorerBudget:
    """The session budgets of 6.8 (`policy/explorer_budget.yaml`)."""

    max_actions: int = 400
    max_minutes: float = 90.0
    max_no_change_actions: int = 20
    max_usd_per_session: float = 25.0
    max_usd_per_week: float = 500.0

    @classmethod
    def from_policy(cls, data: Mapping[str, object]) -> "ExplorerBudget":
        return cls(**{k: data[k] for k in ("max_actions", "max_minutes", "max_no_change_actions",
                                           "max_usd_per_session", "max_usd_per_week") if k in data})  # type: ignore[arg-type]


def not_started_model_unavailable() -> ControlResult:
    """The session due with the model endpoint down (2.9, 10.7): `not_run (reason: model_unavailable)`."""
    return ControlResult("explorer", False, "running",
                         outcomes=(SubsetOutcome("explorer_stage", Outcome("not_run", "model_unavailable")),),
                         alert=alerts.coverage_loss(notified=("explorer_owner",)),
                         detail="Explorer: not run, model endpoint unavailable")


class ExplorerSession:
    """One Explorer session's budgets and boundary decisions."""

    def __init__(self, session_id: str, clock: Clock, *, budget: ExplorerBudget = ExplorerBudget(),
                 ledger: DropLedger, deadline_mono: float | None = None, week_spent_usd: float = 0.0):
        self.session_id = session_id
        self.clock = clock
        self.budget = budget
        self.ledger = ledger
        self.deadline = deadline_mono
        self.started = clock.monotonic()
        self.actions = 0
        self.no_change = 0
        self.usd = 0.0
        self.week_usd = week_spent_usd
        self.last_hash: str | None = None
        self.ended: str | None = None

    def _end(self, why: str, **kw) -> ControlResult:
        self.ended = why
        return ControlResult("explorer", False, "running", actions=kw.pop("actions", ("end_session",)),
                             detail=kw.pop("detail", f"Explorer session ended: {why}"), **kw)

    def budget_exhausted(self) -> str | None:
        """The budget that is spent, by name, or None."""
        b = self.budget
        elapsed = self.clock.monotonic() - self.started
        if self.actions >= b.max_actions:
            return "actions"
        if elapsed >= b.max_minutes * 60:
            return "minutes"
        if self.deadline is not None and self.clock.monotonic() >= self.deadline:
            return "rc_deadline"
        if self.no_change >= b.max_no_change_actions:
            return "no_change"
        if self.usd >= b.max_usd_per_session:
            return "usd_session"
        if self.week_usd >= b.max_usd_per_week:
            return "usd_week"
        return None

    def record_action(self, screenshot_sha256: str, usd: float = 0.0) -> ControlResult | None:
        """Count one action, its post-action screenshot hash and its model spend; a result when a budget ends it."""
        if self.ended:
            raise RuntimeError(f"session already ended ({self.ended})")
        self.actions += 1
        self.no_change = self.no_change + 1 if screenshot_sha256 == self.last_hash else 0
        self.last_hash = screenshot_sha256
        self.usd += usd
        self.week_usd += usd
        spent = self.budget_exhausted()
        if spent:
            return self._end(f"budget {spent}", detail=f"Explorer budget hit: {spent}; the run continues",
                             evidence={"budget": spent})
        return None

    def record_drop(self, drop: GatewayDrop) -> ControlResult:
        """A refusal or drop in the session, through the run's ledger (three end it; a send ends it at once)."""
        r = self.ledger.record(drop, unit=self.session_id)
        if r.has("end_session") or r.has("kill_switch"):
            self.ended = drop.reason
        return r

    def order_stage(self) -> ControlResult:
        """The ticket or dialog flag set or unknown in the session (6.8 Order stage in an Explorer session)."""
        self.ended = "order_stage"
        self.ledger.run_count += 1
        finding = FindingSpec("explorer_order_stage", "the ticket or dialog flag was set in an Explorer session",
                              severity="S1")
        if self.ledger.run_count >= self.ledger.per_run:
            run, un = safety_abort_outcome("running", "kill_switch")
            return ControlResult("explorer", False, "running", run=run, unreached=un, alert=alerts.safety_abort(),
                                 actions=("end_session", "close_window_escape", "kill_switch"), findings=(finding,),
                                 incident="S1", detail="explorer_order_stage reached the per-run trip of ten")
        return ControlResult("explorer", False, "running", alert=alerts.explorer_order_stage(),
                             actions=("end_session", "close_window_escape"), findings=(finding,), incident="S1",
                             detail="all input dropped, window closed by Escape token; the run continues")


def explorer_teardown(ledger_items: Sequence[str], verified_deleted: Mapping[str, bool]) -> ControlResult:
    """Replay of the server-side mutation ledger at session end (6.8)."""
    left = [i for i in ledger_items if not verified_deleted.get(i)]
    if not left:
        return ControlResult("explorer_teardown", True, "teardown")
    o = Outcome("env_error", "precondition:explorer_ledger_clean", "teardown", "; ".join(left))
    return ControlResult("explorer_teardown", False, "teardown", outcomes=(SubsetOutcome("explorer_stage", o),),
                         alert=alerts.explorer_owner(), actions=("block_login",),
                         detail="Explorer login blocked until the owner cleans it by hand: " + ", ".join(left))
