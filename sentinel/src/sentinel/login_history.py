"""What earlier runs on one login left, for the cross-run escalations (6.3, 6.4, 6.6, 6.9, 9.10).

`login_history(store, alias)` reads the finished runs on the login from the central store and returns the
`orchestrator.machine.LoginHistory` the shard's `RunContext` carries:

- `unproven_streak`: consecutive UNPROVEN post-run proofs on the login just before this run (6.6: the second
  disables the rig and files the `harness_error` finding; a cleared proof still counts, since 6.6 speaks of two
  consecutive results "each cleared by hand");
- `identity_unreadable_streak`: consecutive `identity_unreadable` results of the paper-mode assertion (6.4: the
  second blocks the login and opens a maintenance task);
- `attestation_unreadable_last_run`: the previous attestation result was `attestation_unreadable` (6.3: the same
  reason on two consecutive runs is P1 and a maintenance task);
- `failed_gates_last_run`: the gates of the previous run's `env_error` outcomes (9.10: the same gate in two
  consecutive runs is P1; `gates.check_provisioned(repeat_gates=...)`, `gates.check_authenticated(repeat="authenticated"
  in ...)`, which covers `vault_unreachable` on two consecutive runs, 6.9).

Choices (reported):

1. A run is on the login when one of its shards ran on a rig registered with that login, or one of its proofs names
   it. Its outcomes are those of the login's shards plus those written without a shard (the unreached fill); a
   multi-login run's shard-less rows therefore count for each of its logins, which can only escalate sooner.
2. A streak counts results, not runs: a run that stopped before the gate (its `last_gate`, from the run's summary,
   comes earlier in the 5.2 order) has no result there and neither extends nor breaks the streak. A `preflight.*`
   last gate is `provisioned`; `connectivity` is `fixture_verified`; any other is taken as reaching every gate.
   Runs with no proof row for the login likewise leave the UNPROVEN streak as it is.
3. Only finished runs count, newest first by `started_at` then run id; the run being set up is not finished.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .orchestrator.gates import GATES
from .orchestrator.machine import LoginHistory
from .store.store import Store


def _gate_index(gate: str | None) -> int:
    """The 5.2 position of a run's last gate (choice 2)."""
    if gate is None:
        return len(GATES)
    if gate.startswith("preflight."):
        return GATES.index("provisioned")
    if gate == "connectivity":
        return GATES.index("fixture_verified")
    return GATES.index(gate) if gate in GATES else len(GATES)


def _runs(store: Store, alias: str) -> list[Any]:
    return store.conn.execute(
        "SELECT r.run_id, r.summary_json FROM runs r WHERE r.lifecycle = 'finished' AND ("
        " EXISTS (SELECT 1 FROM shards s JOIN rigs g ON g.rig_id = s.rig_id"
        "         WHERE s.run_id = r.run_id AND g.login_alias = ?)"
        " OR EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = r.run_id AND p.login_alias = ?))"
        " ORDER BY r.started_at DESC, r.run_id DESC", (alias, alias)).fetchall()


def _outcomes(store: Store, run_id: str, alias: str) -> list[tuple[str, str | None, str | None]]:
    rows = store.conn.execute(
        "SELECT o.outcome, o.reason, o.gate FROM outcomes o WHERE o.run_id = ? AND (o.shard_id IS NULL OR"
        " o.shard_id IN (SELECT s.shard_id FROM shards s JOIN rigs g ON g.rig_id = s.rig_id WHERE g.login_alias = ?))",
        (run_id, alias)).fetchall()
    return [(r["outcome"], r["reason"], r["gate"]) for r in rows]


def _verdicts(store: Store, run_id: str, alias: str) -> list[str]:
    return [r["verdict"] for r in store.conn.execute(
        "SELECT verdict FROM post_run_proofs WHERE run_id = ? AND login_alias = ?", (run_id, alias))]


def _last_gate(summary_json: str | None) -> str | None:
    return (json.loads(summary_json or "{}") or {}).get("last_gate")


def _streak(results: Sequence[bool | None]) -> int:
    """Consecutive True from the newest result, skipping None (no result at that gate)."""
    n = 0
    for r in results:
        if r is None:
            continue
        if not r:
            break
        n += 1
    return n


def login_history(store: Store, alias: str) -> LoginHistory:
    """The escalation inputs for the next run on `alias` (module docstring)."""
    runs = _runs(store, alias)
    identity: list[bool | None] = []
    attestation: list[bool | None] = []
    unproven: list[bool | None] = []
    failed_last: frozenset[str] = frozenset()
    for k, run in enumerate(runs):
        rows = _outcomes(store, run["run_id"], alias)
        reached = _gate_index(_last_gate(run["summary_json"]))
        reasons = {(o, r) for o, r, _ in rows}
        identity.append(True if ("env_error", "identity_unreadable") in reasons
                        else False if reached >= GATES.index("paper_mode_asserted") else None)
        attestation.append(True if ("env_error", "attestation_unreadable") in reasons
                           else False if reached >= GATES.index("settings_attested") else None)
        verdicts = _verdicts(store, run["run_id"], alias)
        unproven.append(None if not verdicts else "unproven" in verdicts)
        if k == 0:
            failed_last = frozenset(g for o, _, g in rows if o == "env_error" and g)
    return LoginHistory(identity_unreadable_streak=_streak(identity),
                        attestation_unreadable_last_run=_streak(attestation) >= 1,
                        unproven_streak=_streak(unproven), failed_gates_last_run=failed_last)
