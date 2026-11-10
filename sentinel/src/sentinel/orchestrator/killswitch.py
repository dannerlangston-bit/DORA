"""Kill switch, recovery and the post-run proof (6.6).

Triggers (`TripWires`, evaluated by the Orchestrator at every step and flow boundary and by the
Watchdog on its 2 s sample): the file flag and the named pipe; a new Working or Filled row against
the run-start snapshot; an order-submitted or order-rejected window (`order_event`); an Order
Confirmation dialog open more than 30 s with no owning step in a scripted flow or agent rescue; ten
gateway drops or refusals; an attempted send on a scripted flow or agent rescue; a paper-mode or
attestation mismatch at any read; a dead Orchestrator heartbeat while the ticket flag is set.

`kill_switch` is the decision (run `aborted_safety`, every unreached checkpoint `env_error (gate:
<aborting gate>, reason: <abort reason>)`, the running flow quarantined, the 9.10 safety page) and
`execute_kill_switch` drives the Watchdog-owned sequence on a `Rig`: deny-all, kill the tree, a fresh
scripted login with A1 and A2, Working read, `Cancel All` when a row is new, re-read until clean or
60 s, the Monitor reads, the proof, and the snapshot revert.

`post_run_proof` gives one of the four verdicts: `PROVEN: no orders sent`, `PROVEN: only declared
sends` (gated branch), `PROVEN: no session established`, or `UNPROVEN` with `recovery_login_failed`,
`monitor_unreadable`, `cancel_all_failed`, `snapshot_missing` or `session_unavailable`. `proof_result`
turns it into the run effect: an undeclared new row is `aborted_safety (reason: undeclared_order_row)`
after the fact (S1); UNPROVEN is `complete_with_env_errors (reason: unproven_proof)` unless the run is
already `aborted_safety`, blocks the next run on the login, and a second UNPROVEN in a row on one login
disables the rig and files a `harness_error` finding against the proof flow.

Choices (reported):

1. The abort reason of a kill-switch run is the trigger's own code where 4.13 has one
   (`attempted_send`, `live_identity`, `paper_mode_mismatch`, `attestation_mismatch`) and `kill_switch`
   for the human channels, a new row, an `order_event` window, the 30-second dialog, ten drops and a dead
   Orchestrator with the flag set; unreached checkpoints carry that same reason (1.10, 12.2).
2. A changed Position Statement row count or a Net Liq drift beyond 0.50 (the sample's tolerance) with
   no new order row to explain it means the Monitor reads do not agree: UNPROVEN `monitor_unreadable`.
3. The no-session rule is applied literally: the login never authenticated and the gateway issued no
   token in the run. A login that typed credentials issued secret tokens, so it takes the normal path.
4. `Cancel All` re-reads Working every 5 s until it shows no new row or 60 s pass.
5. 6.6 names four verdicts and none fits a clean read that finds an undeclared row; such a proof has the
   verdict `ORDERS SENT: undeclared rows` and lists the rows, and the run effect is `aborted_safety
   (reason: undeclared_order_row)`. With `Cancel All` failed as well the verdict is UNPROVEN
   (`cancel_all_failed`) and the rows are still listed and still abort the run.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import alerts
from .clock import Clock
from .identity import Allowlist, judge_identity
from .results import ControlResult, FindingSpec, RunVerdict, safety_abort_outcome
from .rig import OrderBook, OrderRow, Rig

DIALOG_OPEN_MAX_S = 30.0
CANCEL_REREAD_S = 60.0
CANCEL_POLL_S = 5.0
NET_LIQ_TOLERANCE = 0.50
GRIDS = ("working", "filled", "canceled")

PROVEN_NONE = "PROVEN: no orders sent"
PROVEN_DECLARED = "PROVEN: only declared sends"
PROVEN_NO_SESSION = "PROVEN: no session established"
UNPROVEN = "UNPROVEN"
ORDERS_SENT = "ORDERS SENT: undeclared rows"      # outside the four verdicts (choice 5)
UNPROVEN_REASONS = ("recovery_login_failed", "monitor_unreadable", "cancel_all_failed", "snapshot_missing",
                    "session_unavailable")
# 4.13 aborted_safety codes a trigger keeps as the run reason (choice 1)
OWN_REASON = {"attempted_send": "attempted_send", "live_identity": "live_identity",
              "paper_mode_mismatch": "paper_mode_mismatch", "attestation_mismatch": "attestation_mismatch"}


@dataclass(frozen=True)
class TripWires:
    """One evaluation of the automatic and human triggers (6.6 Triggers)."""

    file_flag: bool = False
    named_pipe: bool = False
    new_working_or_filled: bool = False
    order_event_window: bool = False
    dialog_open_s: float = 0.0
    dialog_owned: bool = False                  # an owning step in a scripted flow or an agent rescue
    explorer_session: bool = False              # the 30 s rule never applies to an Explorer session (6.8)
    gateway_drops: int = 0
    attempted_send: bool = False                # on a scripted flow or an agent rescue
    identity_mismatch: str | None = None        # live_identity | paper_mode_mismatch
    attestation_mismatch: bool = False
    orchestrator_dead: bool = False
    flag_set: bool = False


def check_tripwires(w: TripWires, *, drop_limit: int = 10) -> str | None:
    """The first trigger that fires, as a trigger name, or None (6.6 Triggers, in the spec's order)."""
    if w.file_flag:
        return "file_flag"
    if w.named_pipe:
        return "named_pipe"
    if w.new_working_or_filled:
        return "new_order_row"
    if w.order_event_window:
        return "order_event_window"
    if w.dialog_open_s > DIALOG_OPEN_MAX_S and not w.dialog_owned and not w.explorer_session:
        return "dialog_open_30s"
    if w.gateway_drops >= drop_limit:
        return "ten_drops"
    if w.attempted_send:
        return "attempted_send"
    if w.identity_mismatch:
        return w.identity_mismatch
    if w.attestation_mismatch:
        return "attestation_mismatch"
    if w.orchestrator_dead and w.flag_set:
        return "orchestrator_dead_flag_set"
    return None


def kill_switch(trigger: str, *, gate: str, running_flow: str | None = None) -> ControlResult:
    """The decision of the 6.6 sequence for `trigger` at the aborting `gate` (steps 1 to 4 and 6)."""
    reason = OWN_REASON.get(trigger, "kill_switch")
    run, un = safety_abort_outcome(gate, reason, detail=f"trigger {trigger}")
    acts = ["deny_all", "kill_tree", "recovery_login", "revert_snapshot", "disable_rig"]
    if running_flow:
        acts.insert(1, "quarantine_flow")
    if trigger == "live_identity":
        acts.append("block_login")
    return ControlResult("kill_switch", False, gate, run=run, unreached=un, alert=alerts.safety_abort(),
                         actions=tuple(acts), incident="S1" if trigger in ("new_order_row", "live_identity") else None,
                         detail=f"kill switch: {trigger}" + (f"; flow {running_flow} quarantined" if running_flow else ""))


# -- the post-run proof -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProofInputs:
    """What the proof reads (6.6). `session` is `established`, `never` (the login never authenticated) or
    `kicked` (`session_kicked`, 7.7); `cancel_all` is `not_needed`, `done` or `failed` (not located, or rows
    left after 60 s)."""

    start: OrderBook | None
    end: OrderBook | None
    session: str = "established"
    tokens_issued: int = 1
    recovery_login_ok: bool | None = None       # None: no recovery login was needed
    kicked_login_ok: bool | None = None         # the one fresh login after session_release_wait (kicked only)
    cancel_all: str = "not_needed"
    declared_sends: frozenset[str] = frozenset()
    net_liq_tolerance: float = NET_LIQ_TOLERANCE


@dataclass(frozen=True)
class ProofRow:
    """One line of the proof table: start, end, new, expected and the line's verdict."""

    name: str
    start: float | None
    end: float | None
    new: int
    expected: int
    verdict: str


@dataclass(frozen=True)
class Proof:
    """The ORDER SAFETY PROOF section's verdict, reason, undeclared rows and table (6.6)."""

    verdict: str
    reason: str | None = None
    undeclared: tuple[OrderRow, ...] = ()
    declared: tuple[OrderRow, ...] = ()
    rows: tuple[ProofRow, ...] = ()

    def __post_init__(self):
        if self.verdict not in (PROVEN_NONE, PROVEN_DECLARED, PROVEN_NO_SESSION, UNPROVEN, ORDERS_SENT):
            raise ValueError(f"proof verdict {self.verdict!r}")
        if (self.verdict == UNPROVEN) != (self.reason is not None) or (self.reason and self.reason not in UNPROVEN_REASONS):
            raise ValueError(f"proof reason {self.reason!r} with {self.verdict}")

    @property
    def proven(self) -> bool:
        return self.verdict.startswith("PROVEN")

    @property
    def line(self) -> str:
        """The VERDICT line of the report section."""
        return f"{self.verdict} ({self.reason})" if self.reason else self.verdict


def _new_rows(start: OrderBook, end: OrderBook) -> dict[str, list[OrderRow]]:
    out = {}
    for g in GRIDS:
        before = {r.key for r in start.rows(g)}
        out[g] = [r for r in end.rows(g) if r.key not in before]
    return out


def post_run_proof(p: ProofInputs) -> Proof:
    """The proof verdict from the inputs (6.6 Post-run proof, Runs without a usable session)."""
    if p.session == "never" and p.tokens_issued == 0:
        return Proof(PROVEN_NO_SESSION)
    if p.session == "kicked" and not p.kicked_login_ok:
        return Proof(UNPROVEN, "session_unavailable")
    if p.recovery_login_ok is False:
        return Proof(UNPROVEN, "recovery_login_failed")
    if p.start is None:
        return Proof(UNPROVEN, "snapshot_missing")
    if p.end is None or not (p.end.readable and p.end.copy_all_agrees and p.start.readable):
        return Proof(UNPROVEN, "monitor_unreadable")
    new = _new_rows(p.start, p.end)
    declared = tuple(r for g in ("working", "canceled") for r in new[g] if r.key in p.declared_sends)
    undeclared = tuple(r for g in GRIDS for r in new[g] if r.key not in p.declared_sends or g == "filled")
    rows = [ProofRow(f"{g} orders", len(p.start.rows(g)), len(p.end.rows(g)), len(new[g]),
                     sum(1 for r in new[g] if r.key in p.declared_sends and g != "filled"),
                     "ok" if all(r.key in p.declared_sends and g != "filled" for r in new[g]) else "UNDECLARED")
            for g in GRIDS]
    pos_change = p.end.position_rows - p.start.position_rows
    rows.append(ProofRow("position statement rows", p.start.position_rows, p.end.position_rows, max(pos_change, 0), 0,
                         "ok" if pos_change == 0 else "changed"))
    drift = None if p.start.net_liq is None or p.end.net_liq is None else abs(p.end.net_liq - p.start.net_liq)
    rows.append(ProofRow("net liq", p.start.net_liq, p.end.net_liq, 0, 0,
                         "ok" if drift is not None and drift <= p.net_liq_tolerance else "drift"))
    if p.cancel_all == "failed":
        return Proof(UNPROVEN, "cancel_all_failed", undeclared, declared, tuple(rows))
    if undeclared:
        return Proof(ORDERS_SENT, None, undeclared, declared, tuple(rows))
    if pos_change != 0 or drift is None or drift > p.net_liq_tolerance:
        return Proof(UNPROVEN, "monitor_unreadable", (), declared, tuple(rows))
    return Proof(PROVEN_DECLARED if declared else PROVEN_NONE, None, (), declared, tuple(rows))


def proof_result(proof: Proof, *, run_so_far: RunVerdict | None = None, unproven_streak: int = 0) -> ControlResult:
    """The run effect of a proof (6.6): undeclared row, UNPROVEN, or a passing proof.

    `unproven_streak` counts the UNPROVEN results on this login just before this one.
    """
    ev = {"verdict": proof.line, "undeclared": [r.key for r in proof.undeclared]}
    if proof.undeclared:
        acts = ("block_login", "disable_rig")
        return ControlResult("proof", False, "teardown", run=RunVerdict("aborted_safety", "undeclared_order_row"),
                             alert=alerts.safety_abort().plus(paged=("login_owner",)) if not proof.proven
                             else alerts.safety_abort(), actions=acts, incident="S1",
                             detail="undeclared new order rows: " + ", ".join(r.key for r in proof.undeclared),
                             evidence=ev)
    if not proof.proven:
        keep = run_so_far is not None and run_so_far.status == "aborted_safety"
        twice = unproven_streak + 1 >= 2
        return ControlResult(
            "proof", False, "teardown", run=None if keep else RunVerdict("complete_with_env_errors", "unproven_proof"),
            alert=alerts.unproven_proof(), actions=("block_login",) + (("disable_rig",) if twice else ()),
            findings=(FindingSpec("harness_error", "two UNPROVEN proofs in a row on one login: the proof reader is "
                                  "suspect", detail="proof flow"),) if twice else (),
            detail=proof.line, evidence=ev)
    return ControlResult("proof", True, "teardown", detail=proof.line, evidence=ev)


# -- the sequence on a rig --------------------------------------------------------------------------------
@dataclass
class KillSwitchReport:
    """What `execute_kill_switch` did: the decision, the proof and each step with its monotonic time."""

    result: ControlResult
    proof: Proof
    proof_effect: ControlResult
    steps: list[tuple[str, float]] = field(default_factory=list)


def _login_ok(rig: Rig, allowlist: Allowlist, clock: Clock, fixture_account: str | None) -> bool:
    rig.launch()
    o = rig.login()
    if o.auth_error or o.challenge or o.session_kicked or o.live_selected or not o.vault_lease \
            or o.main_window_s is None or not o.post_login_anchor:
        return False
    reads = [rig.read_account_selector()]
    try:
        verdict = judge_identity(reads, allowlist)
    except ValueError:
        clock.sleep(1.0)
        reads.append(rig.read_account_selector())
        verdict = judge_identity(reads, allowlist)
    if not verdict.passed:
        return False
    if fixture_account is not None and reads[-1].text != fixture_account:
        rig.select_account(fixture_account)
        return rig.read_account_selector().text == fixture_account
    return True


def execute_kill_switch(rig: Rig, decision: ControlResult, *, clock: Clock, allowlist: Allowlist,
                        start: OrderBook | None, fixture_account: str | None = None,
                        declared_sends: Sequence[str] = (), run_so_far: RunVerdict | None = None,
                        unproven_streak: int = 0) -> KillSwitchReport:
    """Drive steps 1, 3, 5 and 6 of the 6.6 sequence on `rig` after `kill_switch` decided (steps 2 and 4 are
    the decision's unreached outcome and alert, written by the caller)."""
    steps: list[tuple[str, float]] = []
    rig.set_hook_state(armed=False, deny_all=True)
    steps.append(("deny_all", clock.monotonic()))
    rig.kill_process_tree()
    steps.append(("kill_tree", clock.monotonic()))
    cancel = "not_needed"
    end: OrderBook | None = None
    login_ok = _login_ok(rig, allowlist, clock, fixture_account)
    steps.append(("recovery_login", clock.monotonic()))
    if login_ok:
        end = rig.read_orders()
        if start is not None and end.readable and _new_rows(start, end)["working"]:
            res = rig.cancel_all()
            steps.append(("cancel_all", clock.monotonic()))
            deadline = clock.monotonic() + CANCEL_REREAD_S
            cancel = "done" if res.located and res.clicked else "failed"
            while cancel == "done":
                end = rig.read_orders()
                if not [r for r in _new_rows(start, end)["working"] if r.key not in declared_sends]:
                    break
                if clock.monotonic() >= deadline:
                    cancel = "failed"
                    break
                clock.sleep(CANCEL_POLL_S)
    proof = post_run_proof(ProofInputs(start=start, end=end, recovery_login_ok=login_ok, cancel_all=cancel,
                                       declared_sends=frozenset(declared_sends)))
    steps.append(("proof", clock.monotonic()))
    rig.revert_snapshot()
    steps.append(("revert_snapshot", clock.monotonic()))
    return KillSwitchReport(decision, proof, proof_result(proof, run_so_far=run_so_far or decision.run,
                                                          unproven_streak=unproven_streak), steps)
