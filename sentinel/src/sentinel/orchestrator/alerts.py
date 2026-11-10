"""Who is paged: the alert routes of 9.10 Alerting model, as data every control result carries.

P1 pages the harness on-call through the paging service and posts in parallel; P2 posts and
mentions the rotation; P3 goes to the daily digest (9.10). The safety routing table of 9.10 is
the one authoritative routing for safety events (F67): every row pages the harness on-call at P1
at any hour, notifies the accountable owner and posts to `#sentinel-safety`, and adds the row's
other recipients. The "Who sees" column of 5.2 adds the gate-specific recipients (login owner,
accounts owner) to the `env_error` row.

Choices where 9.10 has no row (5.4.8, 6.2, 6.8 name the recipient but not the priority):

- A `gateway_integrity` finding (an `untokened_injected` or `foreign_input` drop, a hook reinstall)
  is "an on-call alert" (5.4.1, 5.4.4): it follows the `harness_error` row, P2 to the on-call channel.
- A flow quarantined by the three-per-flow threshold notifies the flow owner (6.1 "Flow owner
  (drop)") at P2.
- An Explorer attempted send opens an S3 ticket to the Explorer owner (5.4.8); S3 goes to the daily
  digest at P3 (6, preamble).
"""
from __future__ import annotations

from dataclasses import dataclass

P1, P2, P3 = "P1", "P2", "P3"
SAFETY_CHANNEL = "#sentinel-safety"
ON_CALL_CHANNEL = "on_call_channel"
RECIPIENTS = frozenset({
    "harness_on_call", "system_owner", "risk_owner", "accountable_owner", "security_contact", "explorer_owner",
    "login_owner", "accounts_owner", "flow_owner", "release_manager", ON_CALL_CHANNEL, SAFETY_CHANNEL,
})
SAFETY_PAGED = ("harness_on_call", "system_owner", "risk_owner", "accountable_owner", "security_contact")


@dataclass(frozen=True)
class AlertRoute:
    """One 9.10 route: the row it comes from, its priority, who is paged and who is notified."""

    rule: str
    priority: str
    paged: tuple[str, ...] = ()
    notified: tuple[str, ...] = ()

    def __post_init__(self):
        if self.priority not in (P1, P2, P3):
            raise ValueError(f"priority {self.priority!r}")
        unknown = set(self.paged) | set(self.notified)
        unknown -= RECIPIENTS
        if unknown:
            raise ValueError(f"unknown recipients {sorted(unknown)}")

    @property
    def is_safety(self) -> bool:
        """True for a row of the 9.10 safety routing table."""
        return self.rule.startswith("safety:")

    def plus(self, *, paged: tuple[str, ...] = (), notified: tuple[str, ...] = ()) -> "AlertRoute":
        """The same route with more recipients (the 5.2 "Who sees" additions), order kept, no repeats."""
        return AlertRoute(self.rule, self.priority, _merge(self.paged, paged), _merge(self.notified, notified))


def _merge(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    return a + tuple(x for x in b if x not in a)


# -- the safety routing table of 9.10 -----------------------------------------------------------------
def safety_abort() -> AlertRoute:
    """`aborted_safety` with any reason, and every S1 incident (6.12)."""
    return AlertRoute("safety:aborted_safety", P1, SAFETY_PAGED, (SAFETY_CHANNEL,))


def explorer_order_stage() -> AlertRoute:
    """Finding `explorer_order_stage`, an S1 (6.8): as `aborted_safety`, plus the Explorer owner."""
    return AlertRoute("safety:explorer_order_stage", P1, SAFETY_PAGED + ("explorer_owner",), (SAFETY_CHANNEL,))


def unproven_proof() -> AlertRoute:
    """UNPROVEN post-run proof (6.6)."""
    return AlertRoute("safety:unproven_proof", P1, ("harness_on_call", "system_owner", "accountable_owner"),
                      (SAFETY_CHANNEL,))


def drill_failure() -> AlertRoute:
    """Fire-drill failure `drill_failed:<item>` and safety canary miss `safety_canary_blind:<canary>` (6.5)."""
    return AlertRoute("safety:drill_failure", P1, ("harness_on_call", "system_owner", "risk_owner"),
                      ("accountable_owner", SAFETY_CHANNEL))


# -- the other rows of 9.10 ----------------------------------------------------------------------------
def env_error(*, repeat: bool = False, notified: tuple[str, ...] = ()) -> AlertRoute:
    """Any gate failure: P2 to the on-call channel, P1 when the same gate failed in the previous run too."""
    return AlertRoute("env_error", P1 if repeat else P2, (), (ON_CALL_CHANNEL,) + notified)


def harness_error(*, repeat: bool = False) -> AlertRoute:
    """A `harness_error` (and, by choice, a `gateway_integrity` finding): P2, P1 on repeat."""
    return AlertRoute("harness_error", P1 if repeat else P2, (), (ON_CALL_CHANNEL,))


def aborted_harness() -> AlertRoute:
    """`aborted_harness` with any reason: P1 to the harness on-call, at once (F39)."""
    return AlertRoute("aborted_harness", P1, ("harness_on_call",), (ON_CALL_CHANNEL,))


def report_withheld() -> AlertRoute:
    """A plaintext secret found by the post-run scan (6.9): P1 to the security contact and system owner."""
    return AlertRoute("report_withheld", P1, ("security_contact", "system_owner"), (ON_CALL_CHANNEL,))


def aborted_budget(reason: str) -> AlertRoute:
    """`aborted_budget`: posted to the on-call channel (10.2); `rate_cap` also tells the system owner (6.4)."""
    extra = ("system_owner",) if reason == "rate_cap" else ()
    return AlertRoute("aborted_budget", P2, (), (ON_CALL_CHANNEL,) + extra)


def dead_man(run_type: str) -> AlertRoute:
    """The dead-man alert: P1, P2 for PR runs (9.10)."""
    return AlertRoute("dead_man", P2 if run_type == "pr" else P1, ("harness_on_call",), (ON_CALL_CHANNEL,))


def model_degraded() -> AlertRoute:
    """Model preflight N2 or N5 failed, or a spend cap hit: the "Preflight failures and cost alarm" row, P2."""
    return AlertRoute("preflight_or_cost", P2, (), (ON_CALL_CHANNEL,))


def coverage_loss(*, notified: tuple[str, ...] = ()) -> AlertRoute:
    """`not_run (reason: budget)` or `(reason: model_unavailable)` counts in run health: P3 (9.10, F48)."""
    return AlertRoute("coverage_loss", P3, (), ("harness_on_call",) + notified)


def flow_quarantined() -> AlertRoute:
    """A flow quarantined by the gateway thresholds (5.4.8): the flow owner, P2 (choice, module docstring)."""
    return AlertRoute("flow_quarantined", P2, (), ("flow_owner",))


def explorer_ticket() -> AlertRoute:
    """An S3 ticket to the Explorer owner (an Explorer attempted send, 5.4.8; choice, module docstring)."""
    return AlertRoute("explorer_ticket", P3, (), ("explorer_owner",))


def explorer_owner(priority: str = P2) -> AlertRoute:
    """A message to the Explorer owner (teardown ledger not clean, 6.8: P2)."""
    return AlertRoute("explorer_owner", priority, (), ("explorer_owner",))
