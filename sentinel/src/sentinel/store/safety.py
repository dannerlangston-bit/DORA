"""Persisted safety actions and the human actions that undo them (6.4, 6.5, 6.6, 6.8, 6.12, 8.3, 8.7, 9.6).

A safety control decides (`orchestrator.results.ControlResult`); this module makes the decision last beyond the
run, through the single writer (8.2), so later runs obey it:

- `block_login` sets `logins.blocked_at` and `blocked_reason` (6.4, 6.5, 6.6, 6.8); `disable_rig` takes the rig
  out of the scheduler (`rigs.active = 0` with `disabled_reason`, 6.6 step 6). Both make `trg_shard_rig_gate`
  (the login) or `refuse_blocked` (the rig) refuse the next run.
- `write_safety_quarantine` writes a `safety_quarantines` row (10.5 reasons) for a flow, every flow on a rig,
  or an activity (`order_stage_flows`, `explorer_stage`, `report_publication`); `quarantined_flow` and
  `activity_quarantined` are the scope filter over the open rows, with the drill and safety canary flows
  (class `canary`) exempt (6.5, 10.5).
- `open_incident` writes the 6.12 `incidents` row and returns its id (the id the abort block and the commit
  status print, 9.2); `close_incident`, `record_postmortem` and `overdue_postmortems` follow it to the end.
- `apply_safety_actions` does all of the above for one `ControlResult`.
- The human actions of 8.7 and 9.6, each refused for a service identity (8.3, 8.7) and journaled in the
  store's ledger with the person, role, reason and ticket: `clear_proof` (6.6, `verified_by` set once on an
  UNPROVEN proof), `close_incident`, `override_check` (system owner only, 9.6) and the re-enables of 6.12,
  `unblock_login`, `enable_rig` and `reenable` (any of the three kinds, and a quarantine row), each refused
  unless the canary or drill that would have caught the cause passed on the current build after the cause.
- `refuse_blocked` is the pre-run check: a disabled or inactive rig, a blocked login, an open S1 or S2
  incident on the rig.

Choices where the spec is silent or ambiguous (reported):
- The canary that "would have caught" a cause (6.12): the fire drill (all of drill-1 to drill-5 detected in one
  run) for a fire-drill, kill-switch or gateway-drop quarantine, a login blocked for `drill_order_placed`,
  `cancel_all_failed`, `recovery_login_failed` or `incident`, and a rig disabled by the kill switch or an
  undeclared row; the proof canary for `unproven_proof`, `session_unavailable` and a rig disabled after two
  UNPROVEN proofs; the identity canary for `identity_unreadable_twice` and `live_identity`; the denied-action test
  for `explorer_teardown_incomplete`; the named canary for a `safety_canary` quarantine. `rotation` needs none:
  the login-only canary of 6.9 has no `canary_results` kind (reported as a gap). "Passed on the current build"
  is: the latest run on that build (its `env_fingerprints` build, or the candidate of its build pair) holding
  rows of the canary, written at or after the cause, has every required row `detected`.
- A blocked login is re-enabled only when every UNPROVEN proof on it has been cleared by a person (6.6), whatever
  reason blocked it first (a `live_identity` block, for one, always comes with an UNPROVEN `session_unavailable`
  proof, SD-64). When the login's last two proofs are UNPROVEN, the re-enable of the login and of its rig needs the
  system owner and a proof canary that passed on the current build after the block or disable, on top of what the
  stored reason needs (6.6 "the system owner must sign the re-enable, and no run starts on that login until the
  proof canary passes"): the rule is read from the login's proof history, since the first block or disable reason
  stands (a kill switch's `kill_switch` disable or a `recovery_login_failed` block would hide it). A rig is
  re-enabled only once its S1 and S2 incidents are closed (6.6 step 6, 6.12).
- Re-enable reason and ticket: a quarantine row keeps them in its columns; `logins` and `rigs` have no column
  for them, so the journal row of the batch carries them (`op: human_action`), as for every human action here.
- Incidents: one open S1 or S2 row per run and rig; a later S1 or S2 trigger of the same run on the same rig
  joins it (an S2 becomes S1, the triggers are listed). S3 rows are one per run, rig and trigger. S1 and S2 rows
  get `postmortem_due_at` five business days (Monday to Friday) after they open, or after the S3 to S2 rise.
- A quarantine's cause (`safety_canary_blind:<canary>`, `drill_failed:<item>`, or the trigger's code) leads its
  `detail`, as `<cause>: <text>`, since 8.3 has no column for it and the re-enable reads it back.
- The block, disable and quarantine writes are idempotent: an open block, disable or matching open quarantine
  is left as it is (the first reason and time stand).
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..records import now
from .store import SERVICE_IDENTITIES, Store, StoreRefused, utc

if TYPE_CHECKING:
    from ..orchestrator.results import ControlResult

BLOCKED_REASONS = ("unproven_proof", "identity_unreadable_twice", "cancel_all_failed", "recovery_login_failed",
                   "explorer_teardown_incomplete", "drill_order_placed", "incident", "rotation", "session_unavailable",
                   "live_identity", "authentication_failed")          # 8.3 logins.blocked_reason
DISABLE_REASONS = ("kill_switch", "undeclared_order_row", "unproven_twice", "version_mismatch")
TARGET_KINDS = ("flow", "rig", "login", "activity")                  # 8.3 safety_quarantines.target_kind
ACTIVITIES = ("order_stage_flows", "explorer_stage", "report_publication")
SAFETY_QUARANTINE_REASONS = ("fire_drill", "safety_canary", "kill_switch", "gateway_drops")   # 10.5, the safety rows
CANARY_NAMES = ("attestation", "identity", "proof", "deny_list", "secret_scan", "redaction")    # 10.3
DRILL_ITEMS = tuple(f"drill-{n}" for n in range(1, 6))
SEVERITIES = ("S1", "S2", "S3")
POSTMORTEM_BUSINESS_DAYS = 5
SYSTEM_OWNER = "system_owner"
UNPROVEN_BLOCKS = ("unproven_proof", "recovery_login_failed", "session_unavailable", "cancel_all_failed")
REENABLE_KINDS = ("login", "rig", "quarantine")

Requirement = tuple[str, tuple[str, ...]]          # (canary_results.kind, the mutant ids that must be detected)
DRILL: Requirement = ("fire_drill", DRILL_ITEMS)


def canary_requirement(name: str) -> Requirement:
    """The `canary_results` rows of one safety canary (SD-81: kind safety_canary, mutant_id the canary)."""
    if name not in CANARY_NAMES:
        raise SafetyRefused(f"{name!r} is not a safety canary of 10.3")
    return ("safety_canary", (name,))


LOGIN_EVIDENCE: dict[str, Requirement | None] = {
    "drill_order_placed": DRILL, "cancel_all_failed": DRILL, "recovery_login_failed": DRILL, "incident": DRILL,
    "unproven_proof": ("safety_canary", ("proof",)), "session_unavailable": ("safety_canary", ("proof",)),
    "identity_unreadable_twice": ("safety_canary", ("identity",)), "live_identity": ("safety_canary", ("identity",)),
    "explorer_teardown_incomplete": ("safety_canary", ("deny_list",)), "rotation": None,
    "authentication_failed": None}           # 7.7: the accounts owner clears a lockout; no canary proves a credential
RIG_EVIDENCE: dict[str, Requirement | None] = {"kill_switch": DRILL, "undeclared_order_row": DRILL,
                                               "unproven_twice": ("safety_canary", ("proof",)),
                                               "version_mismatch": None}   # 7.3: a human re-pins the install
PROOF_CANARY: Requirement = ("safety_canary", ("proof",))     # after two UNPROVEN proofs in a row, on top (6.6)
_CAUSE = {"safety_canary": re.compile(rf"^safety_canary_blind:({'|'.join(CANARY_NAMES)})$"),
          "fire_drill": re.compile(r"^drill_failed:[a-z0-9_]+$")}


class SafetyRefused(StoreRefused):
    """A safety write the harness refuses; the message names the rule (6.12, 8.7, 9.6)."""


# -- records ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SafetyQuarantine:
    """One open or closed `safety_quarantines` row (8.3), with its cause read back from `detail`."""

    quarantine_id: int
    target_kind: str
    target: str
    reason: str
    cause: str
    detail: str | None
    run_id: str | None
    opened_at: str
    reenabled_at: str | None = None

    @classmethod
    def _from(cls, r: Any) -> "SafetyQuarantine":
        cause, _, text = (r["detail"] or "").partition(": ")
        return cls(r["quarantine_id"], r["target_kind"], r["target"], r["reason"], cause, text or None, r["run_id"],
                   r["opened_at"], r["reenabled_at"])


@dataclass(frozen=True)
class Incident:
    """One `incidents` row (8.3, 6.12)."""

    incident_id: int
    run_id: str | None
    rig_id: str | None
    login_alias: str | None
    severity: str
    trigger: str
    opened_at: str
    closed_at: str | None
    closed_by: str | None
    close_reason: str | None
    ticket_ref: str | None
    postmortem_ref: str | None
    postmortem_due_at: str | None

    @classmethod
    def _from(cls, r: Any) -> "Incident":
        return cls(*(r[f.name] for f in dataclasses.fields(cls)))


@dataclass(frozen=True)
class Blocked:
    """Why `refuse_blocked` stops a run on a rig: `unknown_rig`, `rig_disabled`, `rig_inactive`, `login_blocked`
    or `open_incident`, and the detail to print."""

    rig_id: str
    why: str
    detail: str


@dataclass(frozen=True)
class Reenabled:
    """A recorded re-enable (6.12): what, by whom, why, the ticket and the run whose canary or drill passed."""

    kind: str
    target: str
    by: str
    reason: str
    ticket: str
    evidence_run_id: str | None


@dataclass(frozen=True)
class AppliedSafety:
    """What `apply_safety_actions` wrote for one control result."""

    incident_id: int | None = None
    quarantine_ids: tuple[int, ...] = ()
    login_blocked: bool = False
    rig_disabled: bool = False


# -- helpers ---------------------------------------------------------------------------------------------------
def _person(identity: str | None, role: str) -> str:
    if not isinstance(identity, str) or identity.strip().lower() in SERVICE_IDENTITIES:
        raise SafetyRefused(f"{role} {identity!r} is not a person: a human writes this, never a service account "
                            "(8.3, 8.7)")
    return identity.strip()


def _need(text: str | None, what: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise SafetyRefused(f"{what} is required (6.12, 8.7)")
    return text.strip()


def _when(at: str | None) -> str:
    return utc(at) or now()


def _one(store: Store, sql: str, args: tuple = ()) -> Any:
    rows = store.query(sql, args)
    return rows[0] if rows else None


def _journal(b: Any, action: str, **fields: Any) -> None:
    """The human action's ledger row in the batch (8.7: appended to the day's ledger with who, why and the ticket)."""
    b.rows.append({"op": "human_action", "action": action, **fields})


def _business_days_after(ts: str, days: int) -> str:
    d = datetime.fromisoformat(ts)
    left = days
    while left:
        d += timedelta(days=1)
        left -= d.weekday() < 5
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


# -- logins and rigs (6.4, 6.6, 8.3) ---------------------------------------------------------------------------
def block_login(store: Store, alias: str, reason: str, *, at: str | None = None) -> bool:
    """Block a login for every run type until a person re-enables it (`logins.blocked_at`, 8.3). False when it is
    already blocked (the first block stands)."""
    if reason not in BLOCKED_REASONS:
        raise SafetyRefused(f"blocked_reason {reason!r}; one of {BLOCKED_REASONS} (8.3)")
    row = _one(store, "SELECT blocked_at, unblocked_at FROM logins WHERE login_alias = ?", (alias,))
    if row is None:
        raise SafetyRefused(f"unknown login {alias}")
    if row["blocked_at"] is not None and row["unblocked_at"] is None:
        return False
    with store.batch("safety:block_login") as b:
        b.update("logins", {"login_alias": alias}, {"blocked_at": _when(at), "blocked_reason": reason,
                                                    "unblocked_by": None, "unblocked_at": None})
    return True


def _rig_disabled(row: Any) -> bool:
    return not row["active"] and row["disabled_at"] is not None and row["enabled_at"] is None


def disable_rig(store: Store, rig_id: str, reason: str, *, at: str | None = None) -> bool:
    """Take a rig out of the scheduler until a person re-enables it (6.6 step 6, 6.12). False when it already is."""
    if reason not in DISABLE_REASONS:
        raise SafetyRefused(f"disable reason {reason!r}; one of {DISABLE_REASONS}")
    row = _one(store, "SELECT active, disabled_at, enabled_at FROM rigs WHERE rig_id = ?", (rig_id,))
    if row is None:
        raise SafetyRefused(f"unknown rig {rig_id}")
    if _rig_disabled(row):
        return False
    with store.batch("safety:disable_rig") as b:
        b.update("rigs", {"rig_id": rig_id}, {"active": 0, "disabled_reason": reason, "disabled_at": _when(at),
                                              "enabled_by": None, "enabled_at": None})
    return True


def refuse_blocked(store: Store, rig_id: str) -> Blocked | None:
    """The pre-run check (6.4, 6.6, 6.12): why no run may start on `rig_id`, or None. The store's
    `trg_shard_rig_gate` refuses the same login and incident cases at the shard insert."""
    row = _one(store, "SELECT r.active, r.disabled_reason, r.disabled_at, r.enabled_at, r.login_alias, l.blocked_at, "
                      "l.blocked_reason, l.unblocked_at FROM rigs r JOIN logins l ON l.login_alias = r.login_alias "
                      "WHERE r.rig_id = ?", (rig_id,))
    if row is None:
        return Blocked(rig_id, "unknown_rig", f"rig {rig_id} is not registered")
    if _rig_disabled(row):
        return Blocked(rig_id, "rig_disabled", f"rig {rig_id} disabled since {row['disabled_at']} "
                                               f"({row['disabled_reason']}); a person re-enables it (6.12)")
    if not row["active"]:
        return Blocked(rig_id, "rig_inactive", f"rig {rig_id} is not active (its login moved to a spare, 11.2)")
    if row["blocked_at"] is not None and row["unblocked_at"] is None:
        return Blocked(rig_id, "login_blocked", f"login {row['login_alias']} blocked since {row['blocked_at']} "
                                                f"({row['blocked_reason']}); a person re-enables it (6.12)")
    open_ = store.query("SELECT incident_id, severity FROM incidents WHERE rig_id = ? AND closed_at IS NULL "
                        "AND severity IN ('S1','S2') ORDER BY incident_id", (rig_id,))
    if open_:
        ids = ", ".join(f"{r['incident_id']} ({r['severity']})" for r in open_)
        return Blocked(rig_id, "open_incident", f"rig {rig_id} has open incident {ids}; no run starts until it is "
                                                "closed (6.12)")
    return None


# -- quarantines (6.5, 6.6, 10.5) -------------------------------------------------------------------------------
def write_safety_quarantine(store: Store, target_kind: str, target: str, reason: str, *, cause: str,
                            run_id: str | None = None, detail: str | None = None, at: str | None = None) -> int:
    """Write a safety quarantine (8.3 `safety_quarantines`): a flow, every flow on a rig (`rig`), a login or an
    activity, for a 10.5 safety reason, with the cause that opened it. Returns its id; a matching open row is
    returned as it is."""
    if target_kind not in TARGET_KINDS:
        raise SafetyRefused(f"target_kind {target_kind!r}; one of {TARGET_KINDS} (8.3)")
    if target_kind == "activity" and target not in ACTIVITIES:
        raise SafetyRefused(f"activity {target!r}; one of {ACTIVITIES}")
    if reason not in SAFETY_QUARANTINE_REASONS:
        raise SafetyRefused(f"{reason!r} is not a safety quarantine reason {SAFETY_QUARANTINE_REASONS}; the other "
                            "10.5 reasons are quarantine.yaml pull requests")
    _need(target, "a quarantine target")
    rx = _CAUSE.get(reason)
    if not cause or (rx is not None and not rx.match(cause)) or ": " in cause:
        raise SafetyRefused(f"cause {cause!r} does not name what opened a {reason} quarantine (6.5, 10.3)")
    for q in open_quarantines(store):
        if (q.target_kind, q.target, q.reason, q.cause) == (target_kind, target, reason, cause):
            return q.quarantine_id
    with store.batch("safety:quarantine") as b:
        return b.insert("safety_quarantines", {
            "target_kind": target_kind, "target": target, "reason": reason, "run_id": run_id,
            "detail": cause + (f": {detail}" if detail else ""), "opened_at": _when(at)})


def open_quarantines(store: Store) -> tuple[SafetyQuarantine, ...]:
    """Every safety quarantine not re-enabled, oldest first (the run-start mirror the scope filter reads)."""
    return tuple(SafetyQuarantine._from(r) for r in store.query(
        "SELECT * FROM safety_quarantines WHERE reenabled_at IS NULL ORDER BY quarantine_id"))


def quarantined_flow(quarantines: tuple[SafetyQuarantine, ...], *, flow_id: str, flow_class: str | None, rig_id: str,
                     order_stage: bool) -> SafetyQuarantine | None:
    """The quarantine that makes a flow `not_run (reason: quarantine)` on a rig, or None. Class `canary` flows (the
    drill and the safety canaries) are never quarantined (6.5, 10.5)."""
    if flow_class == "canary":
        return None
    for q in quarantines:
        if ((q.target_kind == "flow" and q.target == flow_id) or (q.target_kind == "rig" and q.target == rig_id)
                or (q.target_kind == "activity" and q.target == "order_stage_flows" and order_stage)):
            return q
    return None


def activity_quarantined(quarantines: tuple[SafetyQuarantine, ...], activity: str) -> SafetyQuarantine | None:
    """The quarantine of `explorer_stage` (the Explorer stage `not_run (reason: quarantine)`, 6.8) or
    `report_publication` (no report publishes, 6.9), or None."""
    if activity not in ACTIVITIES:
        raise ValueError(f"activity {activity!r}; one of {ACTIVITIES}")
    return next((q for q in quarantines if q.target_kind == "activity" and q.target == activity), None)


# -- incidents (6.12) ------------------------------------------------------------------------------------------
def open_incident(store: Store, *, run_id: str | None, rig_id: str | None, login_alias: str | None, severity: str,
                  trigger: str, at: str | None = None) -> int:
    """Open (or join) the 6.12 incident of a run on a rig; returns `incident_id`. While it is open with S1 or S2,
    `trg_shard_rig_gate` refuses every new shard on the rig."""
    if severity not in SEVERITIES:
        raise SafetyRefused(f"incident severity {severity!r}; one of {SEVERITIES} (6.12)")
    trigger = _need(trigger, "an incident trigger")
    when = _when(at)
    for r in store.query("SELECT * FROM incidents WHERE run_id IS ? AND rig_id IS ? AND closed_at IS NULL "
                         "ORDER BY incident_id", (run_id, rig_id)):
        if severity != "S3" and r["severity"] in ("S1", "S2"):
            sev = min(severity, r["severity"])
            triggers = r["trigger"] if trigger in r["trigger"].split("; ") else f"{r['trigger']}; {trigger}"
            if (sev, triggers) != (r["severity"], r["trigger"]):
                with store.batch("safety:incident") as b:
                    b.update("incidents", {"incident_id": r["incident_id"]}, {"severity": sev, "trigger": triggers})
            return r["incident_id"]
        if severity == "S3" and r["severity"] == "S3" and r["trigger"] == trigger:
            return r["incident_id"]
    with store.batch("safety:incident") as b:
        return b.insert("incidents", {
            "run_id": run_id, "rig_id": rig_id, "login_alias": login_alias, "severity": severity, "trigger": trigger,
            "opened_at": when, "postmortem_due_at": _business_days_after(when, POSTMORTEM_BUSINESS_DAYS)
            if severity != "S3" else None})


def incident(store: Store, incident_id: int) -> Incident | None:
    row = _one(store, "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
    return None if row is None else Incident._from(row)


def incidents(store: Store, *, run_id: str | None = None, open_only: bool = False) -> tuple[Incident, ...]:
    """Incident rows for the report's links (6.12, 8.7 safety page): of one run, or all; open ones only on ask."""
    sql = "SELECT * FROM incidents WHERE 1 = 1" + (" AND run_id = ?" if run_id else "") \
        + (" AND closed_at IS NULL" if open_only else "") + " ORDER BY incident_id"
    return tuple(Incident._from(r) for r in store.query(sql, (run_id,) if run_id else ()))


def close_incident(store: Store, incident_id: int, *, by: str, reason: str, ticket_ref: str | None = None,
                   postmortem_ref: str | None = None, at: str | None = None) -> Incident:
    """Close an incident (8.7): a person, with the reason; `closed_by` and `closed_at` are set together."""
    by, reason = _person(by, "closed_by"), _need(reason, "a close reason")
    old = incident(store, incident_id)
    if old is None:
        raise SafetyRefused(f"unknown incident {incident_id}")
    if old.closed_at is not None:
        raise SafetyRefused(f"incident {incident_id} was closed by {old.closed_by} at {old.closed_at}")
    when = _when(at)
    with store.batch("human:close_incident") as b:
        b.update("incidents", {"incident_id": incident_id}, {
            "closed_at": when, "closed_by": by, "close_reason": reason,
            "ticket_ref": ticket_ref or old.ticket_ref, "postmortem_ref": postmortem_ref or old.postmortem_ref})
        _journal(b, "close_incident", incident_id=incident_id, by=by, reason=reason, ticket=ticket_ref, at=when)
    return incident(store, incident_id)                       # type: ignore[return-value]


def record_postmortem(store: Store, incident_id: int, ref: str) -> None:
    """Link the written postmortem of an incident (6.12: every S1 and S2 within five business days); set once."""
    ref = _need(ref, "a postmortem reference")
    old = incident(store, incident_id)
    if old is None:
        raise SafetyRefused(f"unknown incident {incident_id}")
    if old.postmortem_ref not in (None, ref):
        raise SafetyRefused(f"incident {incident_id} already links postmortem {old.postmortem_ref}")
    with store.batch("safety:postmortem") as b:
        b.update("incidents", {"incident_id": incident_id}, {"postmortem_ref": ref})


def overdue_postmortems(store: Store, *, at: str | None = None) -> tuple[Incident, ...]:
    """S1 and S2 incidents whose postmortem is not linked by `postmortem_due_at` (6.12), for the report to flag."""
    return tuple(Incident._from(r) for r in store.query(
        "SELECT * FROM incidents WHERE severity IN ('S1','S2') AND postmortem_ref IS NULL "
        "AND postmortem_due_at < ? ORDER BY incident_id", (_when(at),)))


# -- the human actions of 8.7 and 9.6 ----------------------------------------------------------------------------
def clear_proof(store: Store, *, by: str, shard_id: str | None = None, run_id: str | None = None,
                at: str | None = None) -> int:
    """Record that a person verified the account by hand after an UNPROVEN proof (6.6): `verified_by` and
    `verified_at`, set once (also guarded by `trg_proof_clearance`). Names the shard's proof, or with `run_id` the
    run-level row (SD-61). Returns the proof id."""
    if (shard_id is None) == (run_id is None):
        raise SafetyRefused("name the proof by its shard, or by its run for the run-level row")
    by = _person(by, "verified_by")
    row = _one(store, "SELECT * FROM post_run_proofs WHERE shard_id = ?", (shard_id,)) if shard_id else \
        _one(store, "SELECT * FROM post_run_proofs WHERE run_id = ? AND shard_id IS NULL", (run_id,))
    if row is None:
        raise SafetyRefused(f"no post-run proof for {shard_id or run_id}")
    if row["verdict"] != "unproven":
        raise SafetyRefused(f"only an UNPROVEN proof is cleared by hand; this one is {row['verdict']} (6.6)")
    if row["verified_by"] is not None:
        raise SafetyRefused(f"the proof was cleared by {row['verified_by']} at {row['verified_at']}; it is set once")
    when = _when(at)
    with store.batch("human:clear_proof") as b:
        b.update("post_run_proofs", {"proof_id": row["proof_id"]}, {"verified_by": by, "verified_at": when})
        _journal(b, "clear_proof", proof_id=row["proof_id"], by=by, at=when)
    return row["proof_id"]


def override_check(store: Store, run_id: str, *, by: str, role: str, reason: str, at: str | None = None) -> None:
    """9.6 Override check: the system owner forces the commit status `success` for one run, with the reason; the
    two `runs` fields are set together and once."""
    by, reason = _person(by, "check_override_by"), _need(reason, "an override reason")
    if role != SYSTEM_OWNER:
        raise SafetyRefused(f"Override check is the system owner's only (9.6); {by} acts as {role!r}")
    row = _one(store, "SELECT check_override_by, check_override_reason FROM runs WHERE run_id = ?", (run_id,))
    if row is None:
        raise SafetyRefused(f"unknown run {run_id}")
    if row["check_override_by"] is not None:
        if (row["check_override_by"], row["check_override_reason"]) == (by, reason):
            return
        raise SafetyRefused(f"run {run_id} is already overridden by {row['check_override_by']}")
    when = _when(at)
    with store.batch("human:override_check") as b:
        b.update("runs", {"run_id": run_id}, {"check_override_by": by, "check_override_reason": reason})
        _journal(b, "override_check", run_id=run_id, by=by, role=role, reason=reason, at=when)


def _evidence(store: Store, need: Requirement | None, build_id: str, since: str) -> str | None:
    """The run whose canary or drill passed on `build_id` at or after `since` (6.12), or a refusal."""
    if need is None:
        return None
    kind, ids = need
    what = "fire drill" if kind == "fire_drill" else f"{ids[0]} canary"
    marks = ",".join("?" * len(ids))
    rows = store.query(
        f"SELECT c.run_id, c.mutant_id, c.state FROM canary_results c JOIN runs r ON r.run_id = c.run_id "
        f"WHERE c.kind = ? AND c.mutant_id IN ({marks}) AND c.created_at >= ? "
        f"AND (EXISTS (SELECT 1 FROM env_fingerprints f WHERE f.run_id = c.run_id AND f.build_id = ?) "
        f"OR EXISTS (SELECT 1 FROM build_pairs p WHERE p.pair_id = r.pair_id AND p.candidate_build_id = ?)) "
        f"ORDER BY c.created_at, c.canary_id", (kind, *ids, since, build_id, build_id))
    if not rows:
        raise SafetyRefused(f"refused: no {what} result on build {build_id} since {since}; the {what} that would "
                            "have caught the cause must pass on the current build first (6.12)")
    last = rows[-1]["run_id"]
    states = {r["mutant_id"]: r["state"] for r in rows if r["run_id"] == last}
    bad = [i for i in ids if states.get(i) != "detected"]
    if bad:
        raise SafetyRefused(f"refused: the latest {what} on build {build_id} (run {last}) did not pass: "
                            + ", ".join(f"{i} {states.get(i, 'absent')}" for i in bad) + " (6.12)")
    return last


def unblock_login(store: Store, alias: str, *, by: str, role: str, reason: str, ticket: str, build_id: str,
                  at: str | None = None) -> Reenabled:
    """Re-enable a blocked login (6.12): a person, with reason and ticket, once the canary or drill that would have
    caught the block passed on the current build; after UNPROVEN proofs, every one cleared by hand (6.6) and the
    system owner's signature after two in a row. Lets `trg_shard_rig_gate` start shards on its rig again."""
    by, reason, ticket = _person(by, "unblocked_by"), _need(reason, "a reason"), _need(ticket, "a ticket")
    row = _one(store, "SELECT blocked_at, blocked_reason, unblocked_at FROM logins WHERE login_alias = ?", (alias,))
    if row is None:
        raise SafetyRefused(f"unknown login {alias}")
    if row["blocked_at"] is None or row["unblocked_at"] is not None:
        raise SafetyRefused(f"login {alias} is not blocked")
    proofs = _login_proofs(store, alias)
    if any(p["verdict"] == "unproven" and p["verified_by"] is None for p in proofs):    # whatever blocked it first
        raise SafetyRefused(f"login {alias} has an UNPROVEN proof no person has cleared; clear_proof first (6.6)")
    twice = _unproven_twice(proofs)
    if twice and role != SYSTEM_OWNER:
        raise SafetyRefused("two UNPROVEN proofs in a row on this login: the system owner signs the re-enable (6.6)")
    run = _evidence(store, LOGIN_EVIDENCE[row["blocked_reason"]], build_id, row["blocked_at"])
    proof_run = _evidence(store, PROOF_CANARY, build_id, row["blocked_at"]) if twice else None
    when = _when(at)
    with store.batch("human:reenable") as b:
        b.update("logins", {"login_alias": alias}, {"unblocked_by": by, "unblocked_at": when})
        _journal(b, "reenable", kind="login", target=alias, by=by, role=role, reason=reason, ticket=ticket,
                 evidence_run_id=run, proof_canary_run_id=proof_run, at=when)
    return Reenabled("login", alias, by, reason, ticket, run or proof_run)


def _login_proofs(store: Store, alias: str) -> list[Any]:
    """The login's post-run proofs (8.3), oldest first."""
    return store.query("SELECT verdict, verified_by FROM post_run_proofs WHERE login_alias = ? "
                       "ORDER BY produced_at, proof_id", (alias,))


def _unproven_twice(proofs: list[Any]) -> bool:
    """The login's last two proofs are UNPROVEN: the proof reader is suspect (6.6), whatever blocked or disabled
    first."""
    return len(proofs) >= 2 and all(p["verdict"] == "unproven" for p in proofs[-2:])


def enable_rig(store: Store, rig_id: str, *, by: str, role: str, reason: str, ticket: str, build_id: str,
               at: str | None = None) -> Reenabled:
    """Re-enable a disabled rig (6.12): its S1 and S2 incidents closed, the drill or proof canary passed on the
    current build, and the system owner's signature with the proof canary when two UNPROVEN proofs disabled it or
    are its login's last two proofs, whatever reason disabled it first (6.6)."""
    by, reason, ticket = _person(by, "enabled_by"), _need(reason, "a reason"), _need(ticket, "a ticket")
    row = _one(store, "SELECT active, disabled_reason, disabled_at, enabled_at, login_alias FROM rigs WHERE rig_id = ?",
               (rig_id,))
    if row is None:
        raise SafetyRefused(f"unknown rig {rig_id}")
    if not _rig_disabled(row):
        raise SafetyRefused(f"rig {rig_id} is not disabled")
    open_ = store.query("SELECT incident_id FROM incidents WHERE rig_id = ? AND closed_at IS NULL "
                        "AND severity IN ('S1','S2')", (rig_id,))
    if open_:
        raise SafetyRefused(f"rig {rig_id} stays stopped until incident {open_[0]['incident_id']} is closed "
                            "(6.6, 6.12)")
    twice = row["disabled_reason"] == "unproven_twice" or (
        row["login_alias"] is not None and _unproven_twice(_login_proofs(store, row["login_alias"])))
    if twice and role != SYSTEM_OWNER:
        raise SafetyRefused("two UNPROVEN proofs in a row on this rig's login: the system owner signs the re-enable "
                            "(6.6)")
    run = _evidence(store, RIG_EVIDENCE[row["disabled_reason"]], build_id, row["disabled_at"])
    proof_run = _evidence(store, PROOF_CANARY, build_id, row["disabled_at"]) if twice else None
    when = _when(at)
    with store.batch("human:reenable") as b:
        b.update("rigs", {"rig_id": rig_id}, {"active": 1, "enabled_by": by, "enabled_at": when})
        _journal(b, "reenable", kind="rig", target=rig_id, by=by, role=role, reason=reason, ticket=ticket,
                 evidence_run_id=run, proof_canary_run_id=proof_run, at=when)
    return Reenabled("rig", rig_id, by, reason, ticket, run)


def _reenable_quarantine(store: Store, quarantine_id: int, *, by: str, role: str, reason: str, ticket: str,
                         build_id: str, at: str | None) -> Reenabled:
    by, reason, ticket = _person(by, "reenabled_by"), _need(reason, "a reason"), _need(ticket, "a ticket")
    row = _one(store, "SELECT * FROM safety_quarantines WHERE quarantine_id = ?", (quarantine_id,))
    if row is None:
        raise SafetyRefused(f"unknown quarantine {quarantine_id}")
    q = SafetyQuarantine._from(row)
    if q.reenabled_at is not None:
        raise SafetyRefused(f"quarantine {quarantine_id} was re-enabled at {q.reenabled_at}")
    if q.reason not in SAFETY_QUARANTINE_REASONS:
        raise SafetyRefused(f"a {q.reason} quarantine exits through the quarantine.yaml pull request of 10.5")
    need = canary_requirement(q.cause.partition(":")[2]) if q.reason == "safety_canary" else DRILL
    run = _evidence(store, need, build_id, q.opened_at)
    when = _when(at)
    with store.batch("human:reenable") as b:
        b.update("safety_quarantines", {"quarantine_id": quarantine_id},
                 {"reenabled_by": by, "reenabled_at": when, "reenable_reason": reason, "ticket_ref": ticket})
        _journal(b, "reenable", kind="quarantine", target=quarantine_id, by=by, role=role, reason=reason,
                 ticket=ticket, evidence_run_id=run, at=when)
    return Reenabled("quarantine", str(quarantine_id), by, reason, ticket, run)


def reenable(store: Store, kind: str, target: str, *, by: str, role: str, reason: str, ticket: str, build_id: str,
             at: str | None = None) -> Reenabled:
    """The human re-enable of 6.12 for a blocked login, a disabled rig or a safety quarantine row (its id)."""
    if kind == "login":
        return unblock_login(store, target, by=by, role=role, reason=reason, ticket=ticket, build_id=build_id, at=at)
    if kind == "rig":
        return enable_rig(store, target, by=by, role=role, reason=reason, ticket=ticket, build_id=build_id, at=at)
    if kind == "quarantine":
        if not str(target).isdigit():
            raise SafetyRefused(f"a quarantine is named by its id, not {target!r}")
        return _reenable_quarantine(store, int(target), by=by, role=role, reason=reason, ticket=ticket,
                                    build_id=build_id, at=at)
    raise SafetyRefused(f"re-enable kind {kind!r}; one of {REENABLE_KINDS}")


# -- one control result, persisted ------------------------------------------------------------------------------
def blocked_reason_for(result: ControlResult) -> str:
    """The 8.3 `blocked_reason` of a result that asks for `block_login` (6.4, 6.5, 6.6, 6.8).

    The control that asked names it in `evidence["blocked_reason"]` when it can (the kill switch and the proof do);
    that wins, and the mapping below covers the controls that do not."""
    named = result.evidence.get("blocked_reason") if isinstance(result.evidence, dict) else None
    if isinstance(named, str) and named:
        return named
    if result.control == "fire_drill":
        return "drill_order_placed"
    if result.run_reason == "live_identity":
        return "live_identity"
    if result.control == "paper_mode":
        return "identity_unreadable_twice"
    if result.control == "explorer_teardown":
        return "explorer_teardown_incomplete"
    if result.control == "login" and result.unreached is not None and result.unreached.reason == "authentication_failed":
        return "authentication_failed"                 # 7.7: a lockout blocks every later run on the rig
    if result.control == "proof" and (result.detail or "").startswith("UNPROVEN: "):
        cause = (result.detail or "")[len("UNPROVEN: "):]
        return cause if cause in UNPROVEN_BLOCKS else "unproven_proof"
    return "incident"


def disable_reason_for(result: ControlResult) -> str:
    """Why a result asks for `disable_rig`: an undeclared row, two UNPROVEN proofs, a build the rig did not expect
    (`version_mismatch` at `launched` or POP-01's update progress, 7.3), or a kill-switch trip (6.6)."""
    if result.control == "proof":
        return "undeclared_order_row" if result.run_reason == "undeclared_order_row" else "unproven_twice"
    if result.unreached is not None and result.unreached.reason == "version_mismatch":
        return "version_mismatch"
    return "kill_switch"


def apply_safety_actions(store: Store, result: ControlResult, *, run_id: str, rig_id: str, login_alias: str | None,
                         flow_id: str | None = None, at: str | None = None) -> AppliedSafety:
    """Persist what one control result decided: the login block, the rig disable, the quarantines and the
    incident (6.4 to 6.12). `flow_id` is the running flow, for `quarantine_flow`. Other actions (the kill switch
    sequence, deny-all, a maintenance task) are the caller's."""
    cause = (result.checkpoint.reason if result.checkpoint and result.checkpoint.reason else None) \
        or result.run_reason or result.step_reason or result.control
    specs: list[tuple[str, str, str]] = []
    if result.has("quarantine_flow"):
        if flow_id is None:
            raise ValueError("quarantine_flow needs the running flow's id")
        specs.append(("flow", flow_id, "gateway_drops" if result.control == "gateway" else "kill_switch"))
    if result.has("quarantine_order_stage_flows"):
        specs.append(("activity", "order_stage_flows", "fire_drill"))
    if result.has("quarantine_all_flows"):
        specs.append(("rig", rig_id, "fire_drill" if cause.startswith("drill_failed") else "safety_canary"))
    if cause.startswith("safety_canary_blind:"):
        if any(o.selector == "explorer_stage" and o.outcome.reason == "quarantine" for o in result.outcomes):
            specs.append(("activity", "explorer_stage", "safety_canary"))
        if result.has("withhold_report"):
            specs.append(("activity", "report_publication", "safety_canary"))
    qids = tuple(write_safety_quarantine(store, kind, target, reason, cause=cause, run_id=run_id,
                                         detail=result.detail, at=at) for kind, target, reason in specs)
    blocked = disabled = False
    if result.has("block_login"):
        if login_alias is None:
            raise ValueError("block_login needs the login alias")
        blocked = block_login(store, login_alias, blocked_reason_for(result), at=at)
    if result.has("disable_rig"):
        disabled = disable_rig(store, rig_id, disable_reason_for(result), at=at)
    iid = None
    if result.incident:
        iid = open_incident(store, run_id=run_id, rig_id=rig_id, login_alias=login_alias, severity=result.incident,
                            trigger=f"{result.control}: {cause}", at=at)
    return AppliedSafety(iid, qids, blocked, disabled)
