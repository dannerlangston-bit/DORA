"""The Orchestrator: one shard from `provisioned` to `reported` on a `Rig` (5.2).

`Orchestrator.run(work)` walks the gate sequence with the checks of gates.py, identity.py and
attestation.py, writes the heartbeat at every gate (10.2) and reads the Watchdog's reverse heartbeat,
hands `running` to the caller's `work` function (the Executor's flows; it reports what happens through
`apply`), then runs teardown and the post-run proof, decides the run outcome and writes every silent
scoped checkpoint through the `OutcomeSink` (1.10, L-OUT-07).

How a run ends, by the result that stopped it:

- an `aborted_safety` (or any result asking for the kill switch): the 6.6 sequence on the rig, its
  recovery login and the proof (killswitch.execute_kill_switch);
- a result that killed the tree with a session established (identity unreadable, app or hook budget
  spent): the recovery login and proof of 6.6 step 5;
- the login never authenticated: `PROVEN: no session established` when the gateway issued no session
  token, else the recovery path; a kicked session waits `session_release_wait` and logs in once more
  for the proof, UNPROVEN `session_unavailable` when that fails twice;
- otherwise: teardown (`check_teardown`) and the proof from the session's own Monitor read; when that
  proof finds an undeclared row, the 6.6 sequence runs as for any `aborted_safety` (deny-all, the tree
  killed, a fresh scripted login, verified Cancel All, re-read, the proof, the revert; 1.10). The recovery
  proof after a killed tree clicks Cancel All on a new Working row too (6.6 step 5).

Unreached checkpoints are written once the run outcome is known, with the unreached outcome of the result
that decided it, so a recovery proof that turns an earlier abort into `aborted_safety` gives them that
abort's gate and reason (1.10).

Choices (reported): the run-start order snapshot is read as soon as A1 passes (the selector text is an
allowlist entry), the first moment identity is known, before A2 selects an account and before A9 touches a
ticket, so a later gate failure, an A2 failure included, still has a snapshot to prove against; the
account A2 selects is judged against the allowlist like A1 (6.4); the hook is armed once the main frame is up, so the LOGIN flow's input goes
through it; the no-session rule counts the tokens issued outside `class: login` flows
(`GatewayLog.tokens_issued`), since before authentication there is no session in which anything could
be pressed (F43 names an authentication failure as a no-session case although its credentials were typed). A session
kicked at the `authenticated` gate leaves no run-start snapshot, so its proof after the fresh login is
UNPROVEN (`snapshot_missing`): the normal proof has nothing to diff against. A session that authenticated but
whose identity was never asserted (the paper-mode gate stopped the run before any session token) is not a
usable session (6.6 "Runs without a usable session"): with no session token issued its proof is `PROVEN: no
session established`, so an unreadable selector stays the P2, non-incident outcome 6.4 describes; an
`aborted_safety` at that gate still runs the full kill switch sequence.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..records import RunOutcome
from .attestation import SAFETY_ROWS, AttestationProfile, RowReads, attest, matches, readable
from .budgets import SESSION_RELEASE_WAIT_S, ReloginBudget, ShardBudgets
from .clock import Clock, SystemClock
from .gateway import Chord, DropLedger
from .gates import (AccountExpectation, WorkspaceExpectation, check_authenticated, check_canary_flow, check_fixture,
                    check_launched, check_provisioned, check_teardown, check_workspace, decide_run_outcome)
from .identity import READ_GAP_S, Allowlist, judge_fixture_account, judge_identity, readable as selector_readable
from .killswitch import (KillSwitchReport, Proof, ProofInputs, cancel_new_working, execute_kill_switch, post_run_proof,
                         proof_result, recovery_login)
from .alerts import AlertRoute
from .results import ControlResult, OutcomeSink, RunVerdict, write_unreached
from .rig import OrderBook, Rig
from .watchdog import Heartbeat, HeartbeatUnwritable, HeartbeatWriter, heartbeat_failed, watchdog_check


@dataclass(frozen=True)
class LoginHistory:
    """What earlier runs on this login left (6.3, 6.4, 6.6, 9.10 escalation)."""

    identity_unreadable_streak: int = 0
    attestation_unreadable_last_run: bool = False
    unproven_streak: int = 0
    failed_gates_last_run: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RunContext:
    """The run request and pinned configuration one shard needs (5.2 Inputs)."""

    run_id: str
    run_type: str
    shard_id: str
    expected_build: str
    allowlist: Allowlist
    fixture_account: str
    profile: AttestationProfile
    fixture: dict[str, AccountExpectation]
    workspace: WorkspaceExpectation
    default_order_chords: tuple[Chord, ...] = ()
    reseed_due_passed: bool = False
    history: LoginHistory = LoginHistory()
    shard: int = 1
    attempt: int = 1


@dataclass
class RunReport:
    """Everything one shard decided: the verdict, the last gate, every control result, the proof and fills."""

    run_id: str
    run_type: str
    verdict: RunVerdict
    last_gate: str
    results: list[ControlResult] = field(default_factory=list)
    proof: Proof | None = None
    kill_switch: KillSwitchReport | None = None
    filled: list[str] = field(default_factory=list)

    @property
    def alerts(self) -> list[AlertRoute]:
        return [r.alert for r in self.results if r.alert is not None]

    def gate(self, name: str) -> ControlResult | None:
        """The last result recorded for a gate."""
        return next((r for r in reversed(self.results) if r.gate == name), None)

    def to_run_outcome(self, counts: dict[str, int] | None = None) -> RunOutcome:
        """The `records.RunOutcome` for the store's `end_run`."""
        return RunOutcome(run_id=self.run_id, run_type=self.run_type, status=self.verdict.status,
                          reason=self.verdict.reason, last_gate=self.last_gate, counts=counts or {})


class Orchestrator:
    """Drives one shard through the 5.2 state machine on a `Rig`."""

    def __init__(self, rig: Rig, ctx: RunContext, sink: OutcomeSink, *, clock: Clock | None = None,
                 budgets: ShardBudgets | None = None, relogin: ReloginBudget | None = None,
                 ledger: DropLedger | None = None, heartbeat: HeartbeatWriter | None = None,
                 watchdog_status: Callable[[], tuple[float, bool]] | None = None):
        self.rig = rig
        self.ctx = ctx
        self.sink = sink
        self.clock = clock or SystemClock()
        self.budgets = budgets or ShardBudgets()
        self.relogin = relogin or ReloginBudget()
        self.ledger = ledger or DropLedger()
        self.heartbeat = heartbeat
        self.watchdog_status = watchdog_status
        self.state = "provisioned"
        self.results: list[ControlResult] = []
        self.stop: ControlResult | None = None
        self.session = "never"
        self.start_book: OrderBook | None = None
        self._a1: str | None = None
        self._a2: str | None = None

    # -- recording --------------------------------------------------------------------------------------
    def apply(self, r: ControlResult) -> ControlResult:
        """Record a control result; the first one that sets a run outcome stops the shard."""
        self.results.append(r)
        if r.run is not None and self.stop is None:
            self.stop = r
        return r

    def _gate(self, state: str) -> bool:
        """Enter a gate: heartbeat and the Watchdog's reverse heartbeat (5.2, 10.2). False when either stops the run."""
        self.state = state
        if self.heartbeat is not None:
            try:
                self.heartbeat.write(Heartbeat(self.ctx.run_id, self.ctx.shard, self.ctx.attempt, state,
                                               self.clock.now().isoformat(), self.clock.monotonic()))
            except HeartbeatUnwritable as e:
                self.apply(heartbeat_failed(e))
                return self.stop is None
        if self.watchdog_status is not None:
            age, alive = self.watchdog_status()
            r = watchdog_check(age, alive)
            if r is not None:
                self.apply(r)
        return self.stop is None

    # -- the gate sequence --------------------------------------------------------------------------------
    def run(self, work: Callable[["Orchestrator"], None] | None = None) -> RunReport:
        """Run the shard. `work` runs the shard's work items while the state is `running`, reports every control
        result through `apply` and returns as soon as `stop` is set (a result that set a run outcome)."""
        if self._gates() and self._gate("running"):
            if work is not None:
                work(self)
        return self._finish()

    def _gates(self) -> bool:
        ctx, rig = self.ctx, self.rig
        if not self._gate("provisioned"):
            return False
        r = self.apply(check_provisioned(rig.preflight(), repeat_gates=frozenset(
            g for g in ctx.history.failed_gates_last_run if g.startswith("preflight."))))
        if not r.passed:
            return False
        first = True
        while True:
            if not self._gate("launched"):
                return False
            r = self.apply(check_launched(rig.launch(), expected_build=ctx.expected_build, first_launch=first,
                                          budgets=self.budgets))
            if r.has("relaunch") and self.stop is None:
                rig.kill_process_tree()
                first = False
                continue
            if not r.passed:
                return False
            break
        rig.set_hook_state(armed=True, deny_all=False)
        if not self._gate("authenticated"):
            return False
        r = self.apply(check_authenticated(rig.login()))
        if not r.passed:
            if r.unreached is not None and r.unreached.reason == "session_kicked":
                self.session = "kicked"
            return False
        self.session = "established"
        if not self._gate("paper_mode_asserted") or not self._paper_mode():
            return False
        if not self._gate("settings_attested") or not self._attest():
            return False
        if not self._gate("workspace_loaded"):
            return False
        self.apply(check_workspace(rig.load_workspace(), ctx.workspace))
        if self.stop:
            return False
        if not self._gate("fixture_verified"):
            return False
        first_read = rig.verify_account_fixture()
        r = check_fixture(first_read, ctx.fixture, reseed_due_passed=ctx.reseed_due_passed)
        if r.has("reset_empty_account"):
            self.apply(r)
            rig.reset_empty_account()
            r = check_fixture(first_read, ctx.fixture, after_reset=rig.verify_account_fixture(),
                              reseed_due_passed=ctx.reseed_due_passed)
        self.apply(r)
        if self.stop:
            return False
        if not self._gate("canary_flow_passed"):
            return False
        self.apply(check_canary_flow(rig.run_canary_flow()))
        return self.stop is None

    def _paper_mode(self) -> bool:
        rig, ctx = self.rig, self.ctx
        reads = [rig.read_account_selector()]
        if not selector_readable(reads[0]):
            rig.set_hook_state(armed=True, deny_all=True)          # nothing is pressed while identity is unknown
            self.clock.sleep(READ_GAP_S)
            reads.append(rig.read_account_selector())
        r = self.apply(judge_identity(reads, ctx.allowlist,
                                      unreadable_streak=ctx.history.identity_unreadable_streak))
        if not r.passed:
            return False
        if r.has("rearm"):
            rig.set_hook_state(armed=True, deny_all=False)
        self._a1 = reads[-1].crop_sha256
        self.start_book = rig.read_orders()                  # identity known: the run-start snapshot (choice)
        selected = reads[-1].text or ""
        if selected != ctx.fixture_account:
            rig.select_account(ctx.fixture_account)
            after = rig.read_account_selector()
            self._a2 = after.crop_sha256
            if selector_readable(after):
                # 6.4: the selector text must equal an allowlist entry before every flow, the selected one too
                r = self.apply(judge_identity([after], ctx.allowlist,
                                              unreadable_streak=ctx.history.identity_unreadable_streak))
                if not r.passed:
                    return False
            r = self.apply(judge_fixture_account(selected, ctx.fixture_account, after_select=after.text))
            return r.passed
        self._a2 = reads[-1].crop_sha256
        return True

    def _attest(self) -> bool:
        rig, ctx = self.rig, self.ctx
        firsts = {i: rig.read_setting(i) for i in SAFETY_ROWS}
        bad = [i for i, r in firsts.items() if not readable(r, ctx.profile)]
        seconds = {}
        if bad:
            rig.reopen_settings()
            seconds = {i: rig.read_setting(i) for i in bad}
        reads = {i: RowReads(firsts[i], seconds.get(i)) for i in SAFETY_ROWS}
        all_match = all(readable(r.last, ctx.profile) and matches(r.last, ctx.profile) for r in reads.values())
        a9 = rig.confirmation_check() if all_match else None
        rc = ctx.run_type == "rc"
        a10 = rig.read_notifications() if rc and all_match else None
        evidence = {"A1": self._a1, "A2": self._a2, **{i: reads[i].last.screenshot_sha256 for i in SAFETY_ROWS},
                    "A9": a9.screenshot_sha256 if a9 else None, "A10": a10.screenshot_sha256 if a10 else None}
        res = attest(reads, ctx.profile, a9=a9, a10=a10, rc_run=rc, evidence=evidence,
                     previous_unreadable=ctx.history.attestation_unreadable_last_run)
        self.apply(res.result)
        if res.result.passed:
            rig.load_order_chords(list(res.chords) + list(ctx.default_order_chords))
        return res.result.passed

    # -- ending ----------------------------------------------------------------------------------------
    def _finish(self) -> RunReport:
        stop, rig, ctx = self.stop, self.rig, self.ctx
        report = RunReport(ctx.run_id, ctx.run_type, RunVerdict("complete"), self.state)
        last_gate = self.state
        streak = ctx.history.unproven_streak
        so_far = stop.run if stop else None
        kill_switch_path = stop is not None and (stop.run_status == "aborted_safety" or stop.has("kill_switch"))
        if stop is not None and stop.has("kill_tree") and not kill_switch_path:
            rig.kill_process_tree()
        if kill_switch_path:
            ks = execute_kill_switch(rig, stop, clock=self.clock, allowlist=ctx.allowlist, start=self.start_book,
                                     fixture_account=ctx.fixture_account, run_so_far=so_far, unproven_streak=streak)
            report.kill_switch, report.proof = ks, ks.proof
            self.apply(ks.proof_effect)
        elif self.session == "never" or (self.session == "established" and self.start_book is None
                                         and rig.gateway_log().tokens_issued == 0):
            tokens = rig.gateway_log().tokens_issued
            proof = post_run_proof(ProofInputs(start=None, end=None, session="never", tokens_issued=tokens))
            report.proof = proof
            self.apply(proof_result(proof, run_so_far=so_far, unproven_streak=streak))
        elif self.session == "kicked" or (stop is not None and stop.has("kill_tree")):
            report.proof = self._recovery_proof(kicked=self.session == "kicked")
            self.apply(proof_result(report.proof, run_so_far=so_far, unproven_streak=streak))
        else:
            self.state = last_gate = "teardown"
            obs = rig.teardown()
            self.apply(check_teardown(obs))
            end = rig.read_orders()
            report.proof = post_run_proof(ProofInputs(start=self.start_book, end=end,
                                                      cancel_all="not_needed" if obs.cancel_all_verified else "failed"))
            effect = self.apply(proof_result(report.proof, run_so_far=so_far, unproven_streak=streak))
            if report.proof.undeclared:
                # 1.10, 6.6: an undeclared row at teardown is an aborted_safety; the kill switch sequence cancels it
                ks = execute_kill_switch(rig, effect, clock=self.clock, allowlist=ctx.allowlist, start=self.start_book,
                                         fixture_account=ctx.fixture_account, run_so_far=effect.run,
                                         unproven_streak=streak)
                report.kill_switch, report.proof = ks, ks.proof
                self.apply(ks.proof_effect)
        any_env = any(o is not None and o.outcome == "env_error"
                      for r in self.results for o in (r.unreached, r.checkpoint, *[s.outcome for s in r.outcomes]))
        report.verdict = decide_run_outcome([r.run for r in self.results], any_env_error=any_env,
                                            scope_finished=stop is None)
        # unreached checkpoints carry the abort that decided the run (1.10), which a proof can make worse than
        # the stop that ended the work
        decider = next((r for r in self.results if r.run == report.verdict and r.unreached is not None), stop)
        if decider is not None:
            report.filled = write_unreached(self.sink, ctx.run_id, decider)
        report.results = list(self.results)
        report.last_gate = stop.gate if stop is not None and stop.gate else last_gate
        return report

    def _recovery_proof(self, *, kicked: bool) -> Proof:
        """6.6 step 5 (and the kicked-session rule): a fresh scripted login with A1 and A2, then the proof."""
        rig, ctx = self.rig, self.ctx
        attempts = 2 if kicked else 1
        ok = False
        for _ in range(attempts):
            if kicked:
                self.clock.sleep(SESSION_RELEASE_WAIT_S)
            ok = recovery_login(rig, ctx.allowlist, self.clock, ctx.fixture_account)
            if ok:
                break
        end = rig.read_orders() if ok else None
        cancel = "not_needed"
        if end is not None:
            cancel, end = cancel_new_working(rig, self.start_book, end, clock=self.clock)    # 6.6 step 5
        if kicked:
            return post_run_proof(ProofInputs(start=self.start_book, end=end, session="kicked", kicked_login_ok=ok,
                                              cancel_all=cancel))
        return post_run_proof(ProofInputs(start=self.start_book, end=end, recovery_login_ok=ok, cancel_all=cancel))
