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

The channels are recipients too: `ON_CALL_CHANNEL`, `SAFETY_CHANNEL`, `TRIAGE_CHANNEL` and `RELEASE_CHANNEL` (9.9's
four Slack channels); `report.slack.route` decides which channel a post goes to, so a route names a channel only
where its row names one. Every 9.10 row has a constructor below (the "Alert" column, in table order after the
safety table): `report_withheld`, `new_collateral`, `needs_human_digest`, `sla_escalation`, `decision_conflict`,
`canary_failure`, `canary_path_divergence`, `aa_drift`, `reviewer_backlog`, `reviewer_unavailable`,
`pool_saturated`, `global_rendering_shift`, `redaction_miss`, `integration_outage`, `quarantine_ceiling`,
`nightly_skipped_for_rc`, `rc_start_deadline`, `coverage_loss`, `model_degraded`, `store_health`, plus the
`gateway_safety_drop` row and `dead_man`. More choices (9.10, 11.6):

- `canary_failure` and `canary_path_divergence` also notify the release channel (11.6: "post to product and release
  channels"; 9.9: the detector banner is also posted there).
- `rc_start_deadline` (an RC run past its start deadline, or superseded) notifies the release channel too (11.6).
- `global_rendering_shift` notifies the system owner, the "harness lead" of the row (SD-87).
- `new_collateral` and `decision_conflict` notify the triage channel (9.9 triage row), `needs_human_digest` too.
- `sla_escalation` is P2 to the system owner; `flow_owner=True` adds the tier's flow owner (9.12 step 3).
"""
from __future__ import annotations

from dataclasses import dataclass

P1, P2, P3 = "P1", "P2", "P3"
PRIORITIES = (P1, P2, P3)                       # most urgent first
SAFETY_CHANNEL = "#sentinel-safety"
ON_CALL_CHANNEL = "on_call_channel"
TRIAGE_CHANNEL = "triage_channel"
RELEASE_CHANNEL = "release_channel"
CHANNELS = (ON_CALL_CHANNEL, TRIAGE_CHANNEL, RELEASE_CHANNEL, SAFETY_CHANNEL)   # the four Slack channels of 9.9
RECIPIENTS = frozenset({
    "harness_on_call", "system_owner", "risk_owner", "accountable_owner", "security_contact", "explorer_owner",
    "login_owner", "accounts_owner", "flow_owner", "release_manager", "pr_author", *CHANNELS,
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


def coverage_loss(*, notified: tuple[str, ...] = (), above_r22: bool = False) -> AlertRoute:
    """`not_run (reason: budget)` or `(reason: model_unavailable)` counts in run health: P3 (9.10, F48); a day with
    over 5 percent of masked_live checkpoints starved (11.10 R22) posts P2 to the on-call channel."""
    if above_r22:
        return AlertRoute("coverage_loss", P2, (), ("harness_on_call", ON_CALL_CHANNEL) + notified)
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


def gateway_safety_drop() -> AlertRoute:
    """"Gateway safety drop": `blocked_by_gateway` with a safety reason (6.2); P1 to the harness on-call and the
    flow owner, at any hour (9.10, 11.6)."""
    return AlertRoute("gateway_safety_drop", P1, ("harness_on_call", "flow_owner"), (ON_CALL_CHANNEL,))


def report_withheld() -> AlertRoute:
    """"Report withheld": a plaintext secret found by the post-run scan (6.9, 8.9); P1 to the security contact and
    the system owner."""
    return AlertRoute("report_withheld", P1, ("security_contact", "system_owner"), (ON_CALL_CHANNEL,))


def new_collateral() -> AlertRoute:
    """"New high-confidence collateral": tier-1 `collateral` at reviewer confidence 0.80 or above or with no reviewer
    needed, or any tier-1 `invariant_violation` or `assertion_failed`, with no matching decision; P2 to the PR author
    and triage (9.9, 9.10, SD-87)."""
    return AlertRoute("new_collateral", P2, (), ("pr_author", TRIAGE_CHANNEL))


def needs_human_digest() -> AlertRoute:
    """"needs_human daily digest": 08:00 America/New_York on business days, P3 to triage (9.10, 11.6)."""
    return AlertRoute("needs_human_digest", P3, (), (TRIAGE_CHANNEL,))


def sla_escalation(*, flow_owner: bool = False) -> AlertRoute:
    """"SLA escalation": a tier-1 item one business day past its SLA, or a two-person proposal unanswered for one
    business day; P2 to the system owner naming the rotation member; at three business days the tier's flow owner is
    added (9.10, 9.12 steps 2 and 3)."""
    return AlertRoute("sla_escalation", P2, (), ("system_owner",) + (("flow_owner",) if flow_owner else ()))


def decision_conflict(tier: int | None) -> AlertRoute:
    """"Decision conflict": conflicting active decisions on one finding, or a contested supersession (9.7); P3, P2
    for tier 1 (9.10)."""
    return AlertRoute("decision_conflict", P2 if tier == 1 else P3, (), (TRIAGE_CHANNEL,))


def canary_failure(state: str) -> AlertRoute:
    """"Canary failure" by the night's canary state (10.3): P1 for `detector_blind` and `generation_failed`, P2 for
    `detector_degraded` (9.10); the release channel is notified too (11.6)."""
    if state in ("detector_blind", "generation_failed"):
        return AlertRoute("canary_failure", P1, ("harness_on_call",), (ON_CALL_CHANNEL, RELEASE_CHANNEL))
    if state == "detector_degraded":
        return AlertRoute("canary_failure", P2, (), (ON_CALL_CHANNEL, RELEASE_CHANNEL))
    raise ValueError(f"canary state {state!r} raises no canary_failure alert (10.3)")


def canary_path_divergence() -> AlertRoute:
    """`harness_error (reason: canary_path_divergence)` (10.3): P1, pages at once (9.10)."""
    return AlertRoute("canary_path_divergence", P1, ("harness_on_call",), (ON_CALL_CHANNEL, RELEASE_CHANNEL))


def aa_drift() -> AlertRoute:
    """"A/A drift": `harness_error (reason: aa_drift_global)` on the Nightly run (10.6); P2."""
    return AlertRoute("aa_drift", P2, (), (ON_CALL_CHANNEL,))


def reviewer_backlog() -> AlertRoute:
    """"Reviewer backlog": pending review items past 4 h on a merge run, 1 h on a blocking PR run, or any at RC report
    time; P2 (9.10)."""
    return AlertRoute("reviewer_backlog", P2, (), (ON_CALL_CHANNEL,))


def reviewer_unavailable() -> AlertRoute:
    """"Reviewer disabled or unavailable": a failed eval gate on the deployed tuple (10.4), or `reviewer_unavailable`
    on more than 2 percent of a run's reviewable items; P2 (9.10)."""
    return AlertRoute("reviewer_unavailable", P2, (), (ON_CALL_CHANNEL,))


def pool_saturated() -> AlertRoute:
    """"Pool saturated": PR queue depth above 3, a PR run waiting over 20 min outside an RC reservation, or
    `aborted_budget (reason: pool_saturated)`; P2, one per hour (`report.alerting` limits the rate; 9.10, 11.6)."""
    return AlertRoute("pool_saturated", P2, (), (ON_CALL_CHANNEL,))


def global_rendering_shift() -> AlertRoute:
    """"Global rendering shift": the run-level `global_rendering_shift` finding (3.7, 5.8); P2 to on-call and the
    system owner, the harness lead of SD-87."""
    return AlertRoute("global_rendering_shift", P2, (), (ON_CALL_CHANNEL, "system_owner"))


def redaction_miss() -> AlertRoute:
    """"Redaction miss": a `redaction_miss` finding on the candidate side (3.6, 6.10); P1 to the security contact and
    the flow owner, at any hour (SD-84)."""
    return AlertRoute("redaction_miss", P1, ("security_contact", "flow_owner"), (ON_CALL_CHANNEL,))


def integration_outage() -> AlertRoute:
    """"Integration outage": an outbox job failing for 15 minutes (PR comment or status, Jira, Slack or paging); P2
    to the channels still working (9.10)."""
    return AlertRoute("integration_outage", P2, (), (ON_CALL_CHANNEL,))


def jira_reopen_forbidden() -> AlertRoute:
    """A fixed ticket whose regression the Jira workflow will not let the Reporter reopen (9.9: the comment and the
    `sentinel-reopened` label go on it instead); P2 to the on-call channel, the Integration outage row's routing."""
    return AlertRoute("jira_reopen_forbidden", P2, (), (ON_CALL_CHANNEL,))


def quarantine_ceiling() -> AlertRoute:
    """"Quarantine ceiling": above 2 percent of tier-1 checkpoints quarantined (10.5); P2 to the system owner."""
    return AlertRoute("quarantine_ceiling", P2, (), ("system_owner", ON_CALL_CHANNEL))


def nightly_skipped_for_rc() -> AlertRoute:
    """"Nightly skipped for RC" (10.9, 11.2); P2 to the harness on-call and the system owner."""
    return AlertRoute("nightly_skipped_for_rc", P2, (), ("harness_on_call", "system_owner", ON_CALL_CHANNEL))


def rc_start_deadline() -> AlertRoute:
    """"RC start deadline": an RC run that cannot start by G-N sign-off minus 8 h, or one superseded by a newer RC
    build (`aborted_budget (reason: superseded)`); P2 to the release manager and the system owner, posted to the
    release and on-call channels (9.10, 11.6)."""
    return AlertRoute("rc_start_deadline", P2, (), ("release_manager", "system_owner", ON_CALL_CHANNEL,
                                                    RELEASE_CHANNEL))


def store_health() -> AlertRoute:
    """"Store health": a failed or stale backup, an integrity failure, a restore drill older than 100 days, low disk
    (8.6, 8.9); P2 to the on-call channel and the system owner."""
    return AlertRoute("store_health", P2, (), (ON_CALL_CHANNEL, "system_owner"))
