"""The Orchestrator: one shard from `provisioned` to `reported` on a `Rig` (5.2).

`Orchestrator.run(work)` walks the gate sequence with the checks of gates.py, identity.py and
attestation.py, writes the heartbeat at every gate (10.2) and reads the Watchdog's reverse heartbeat,
hands `running` to the caller's `work` function (the Executor's flows; it reports what happens through
`apply`), then runs teardown and the post-run proof, decides the run outcome and writes every silent
scoped checkpoint through the `OutcomeSink` (1.10, L-OUT-07).

Inside `running` the Executor calls, in this order (boundary.py has the check sequences):

- `flow_boundary(obs, now=..., flow_id=..., p50_s=...)` before every flow, census visit or sweep ladder: the
  trip-wires, the Watchdog's reverse heartbeat, free space, the run budgets, the spend ceiling, the model re-check,
  the paper-mode assertion and the proactive relaunch; it returns the result that stopped the run, or None;
- `step_boundary(obs, now=..., order_stage_step=..., session_dialog=...)` before every step of a flow: the
  trip-wires, the flow's own budget (a returned result with `end_flow` ends the flow, the run continues) and the
  paper-mode assertion when due;
- `relaunch(cause)` after a POP-05 dialog (`environment_relogin`, budget K), an app failure (`app_failure`, the
  relaunch `watchdog.app_failure` already charged) or a hook host restart (`hook_host`); the proactive relaunch is
  `flow_boundary`'s own. It kills the tree, waits `session_release_wait` and re-enters at `launched`, passing every
  gate again (LOGIN, paper-mode assertion with A2, attestation with A9 and the chord reload, workspace, fixture,
  canary flow), then `running` (7.7 Re-login flow, 10.2, SD-66);
- `paper_stamp()` for every capture and outcome record, and `may_release(stamp)` before a frame goes to the
  Reviewer, the agent or a report (6.4 Schedule).

The 6.4 rate caps count through `counters` (budgets.RateCounters): every login attempt the Orchestrator makes (the
LOGIN flow, a relaunch login, a recovery login), every A9 drill ticket and every order-stage step a `step_boundary`
lets run count themselves; the caller adds symbol changes (`counters.symbol_change`) and seeds earlier runs'
events from the store. The caps are checked at every flow boundary, right before each login, A9 ticket and
order-stage step (a cap already reached ends the run `aborted_budget (reason: rate_cap)` and the event is not made),
after a proactive relaunch and once the work returns; the 6.6 recovery logins are never held back.

The session's safety state is a `ports.SessionSafety` (`safety`, pushed to `safety_listener` on every change; the
caller passes `rig.set_session_safety` there so the slow half reads it): the attestation stage `a3_a8` before the A9
click and `a3_a9` once the gate passed (SD-57), and the last passing assertion; it is cleared whenever the session
ends (the tree killed, a relaunch, a kicked session, the run's end).

What the Orchestrator reads from the rig besides the gates (rig.py): the kill switch's human channels
(`kill_channels`), polled first at every step and flow boundary (`killflag.check`, 6.6) and acknowledged once the
sequence ran; the Watchdog's reverse heartbeat and free space (`watchdog_status`, `free_bytes`; a caller's
`watchdog_status` or a boundary's observations take precedence); the Window Watcher's `order_event` window and dialog
age (`window_tripwires`); and the gated branch's declared sends (`declared_sends`), which every proof lists and the
new-row trip-wire does not count. The hook follows `gateway.HOOK_STATES`: `running` once the main frame is up, the
recovery-only arm for every recovery login and its Cancel All (the kill switch sequence, the recovery proof after a
killed tree, the kicked session's proof login), `between_runs` at the end of every run.

How a run ends, by the result that stopped it:

- an `aborted_safety` (or any result asking for the kill switch): the 6.6 sequence on the rig, its
  recovery login and the proof (killswitch.execute_kill_switch);
- a result that killed the tree with a session established (identity unreadable, app or hook budget
  spent), or a relaunch that killed the session and then stopped at one of its own gates (the session
  `ended`): the recovery login and proof of 6.6 step 5, except after an authentication error at that relaunch's
  login, which 6.9 never retries: UNPROVEN `recovery_login_failed` with no second login;
- the login never authenticated: `PROVEN: no session established` when the gateway issued no session
  token, else the recovery path; a kicked session (at the `authenticated` gate or mid-run, `session_kicked`)
  has its tree killed, waits `session_release_wait` and logs in once more for the proof, UNPROVEN
  `session_unavailable` when that fails twice;
- otherwise: teardown (`check_teardown`) and the proof from the session's own Monitor read; when that
  proof finds an undeclared row, the 6.6 sequence runs as for any `aborted_safety` (deny-all, the tree
  killed, a fresh scripted login, verified Cancel All, re-read, the proof, the revert; 1.10), and the
  incident's one safety page is recorded after the sequence with its final proof
  (`killswitch.teardown_incident`), not at the teardown read. The recovery proof after a killed tree clicks
  Cancel All on a new Working row too (6.6 step 5).

Unreached checkpoints are written once the run outcome is known, with the unreached outcome of the result
that decided it, so a recovery proof that turns an earlier abort into `aborted_safety` gives them that
abort's gate and reason (1.10).

Choices (reported): the run-start order snapshot is read as soon as A1 passes (the selector text is an
allowlist entry), the first moment identity is known, before A2 selects an account and before A9 touches a
ticket, so a later gate failure, an A2 failure included, still has a snapshot to prove against; the
account A2 selects is asserted like A1, with the two-read unreadable path (6.3 A2 "On unreadable: as A1", 6.4);
the hook is armed once the main frame is up, so the LOGIN flow's input goes
through it; the no-session rule counts the tokens issued outside `class: login` flows
(`GatewayLog.tokens_issued`), since before authentication there is no session in which anything could
be pressed (F43 names an authentication failure as a no-session case although its credentials were typed). A session
kicked at the `authenticated` gate leaves no run-start snapshot, so its proof after the fresh login is
UNPROVEN (`snapshot_missing`): the normal proof has nothing to diff against. A session that authenticated but
whose identity was never asserted (the paper-mode gate stopped the run before any session token) is not a
usable session (6.6 "Runs without a usable session"): with no session token issued its proof is `PROVEN: no
session established`, so an unreadable selector stays the P2, non-incident outcome 6.4 describes; an
`aborted_safety` at that gate still runs the full kill switch sequence.

Relaunch choices (reported): a relaunch never replaces the run-start snapshot, since the proof must cover the
whole run; its fresh Monitor read is checked against the snapshot instead, and a new Working or Filled row is the
kill switch (6.6 Triggers) at the gate in force. The hook is put in deny-all before the tree is killed and stays
there until the relaunch's `launched` gate arms it for the LOGIN flow. `session_release_wait` is waited before the
relaunch, which precedes its login (7.7 "before any relaunch login"); `reset_input_state` runs right after the kill
(7.7). The default wall budget starts when the Orchestrator is built; a Calibration run must be given its
`WallBudget` (its window end is a calendar time). The chords loaded at `settings_attested` are the A6 bindings and the
pinned defaults (`gateway_config.order_hotkey_list`, from `RunContext.order_hotkeys` or the chords given alone); more
than sixteen fail the run closed there (`aborted_harness (reason: config_invalid)`, 5.4.7).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from ..records import DeclaredSend, RunOutcome
from . import killflag
from .attestation import SAFETY_ROWS, AttestationProfile, RowReads, a9_passed, attest, matches, readable
from .boundary import (BoundaryObservations, DialogTimer, FlowTimer, flow_budget_cut, flow_budget_s, new_order_rows,
                       tripwire_result)
from .budgets import (SESSION_RELEASE_WAIT_S, RateCounters, ReloginBudget, ShardBudgets, WallBudget, rate_cap,
                      run_budget)
from .clock import Clock, SystemClock
from .degraded import ModelAvailability, SpendTracker
from .gateway import Chord, DropLedger
from .gateway_config import DefaultOrderHotkeys, GatewayConfigError, order_hotkey_list
from .gates import (AccountExpectation, WorkspaceExpectation, check_authenticated, check_canary_flow, check_fixture,
                    check_launched, check_provisioned, check_teardown, check_workspace, decide_run_outcome)
from .identity import (READ_GAP_S, TIMER_S, Allowlist, assertion_due, deferral_unreadable, judge_fixture_account,
                       judge_identity, paper_stamp, readable as selector_readable, stamp_current)
from .killswitch import (ORDERS_SENT, KillSwitchReport, Proof, ProofInputs, RecoveryLogin, account_ref,
                         cancel_new_working, execute_kill_switch, kill_switch, post_run_proof, proof_result,
                         recovery_login, teardown_incident, teardown_proof)
from .alerts import AlertRoute
from .orderbook import MonitorBook
from .ports import SessionSafety
from .results import ControlResult, OutcomeSink, RunVerdict, worse, write_unreached
from .rig import ConfirmationCheck, OrderBook, Rig, SelectorRead
from .watchdog import (Heartbeat, HeartbeatUnwritable, HeartbeatWriter, disk_check, heartbeat_failed,
                       proactive_relaunch_due, watchdog_check)

# what each relaunch cause spends (7.7, 10.2, SD-66): K for an environment re-login; an app relaunch or the hook host
# restart, already charged by the control that asked for the relaunch; nothing for the proactive relaunch
RELAUNCH_BUDGETS = {"environment_relogin": "relogin_k", "app_failure": "app_relaunch", "hook_host": "hook_host",
                    "proactive": None}
# login failures 6.9 never retries (lockouts): after one at a relaunch no recovery login is made for the proof
NEVER_RETRIED = ("authentication_failed", "authentication_challenge")


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
    default_order_chords: tuple[Chord, ...] = ()            # the default order chords alone, without their file
    reseed_due_passed: bool = False
    history: LoginHistory = LoginHistory()
    shard: int = 1
    attempt: int = 1
    order_hotkeys: DefaultOrderHotkeys | None = None         # the loaded gateway/default_order_hotkeys.yaml (5.4.7)

    def default_hotkeys(self) -> DefaultOrderHotkeys:
        """The pinned build's default order chords loaded at `settings_attested` (5.4.7): the loaded file, or the
        chords given alone."""
        if self.order_hotkeys is not None:
            return self.order_hotkeys
        return DefaultOrderHotkeys(build=self.expected_build, verified=False, chords=tuple(self.default_order_chords),
                                   actions=("",) * len(self.default_order_chords))


@dataclass
class RunReport:
    """Everything one shard decided: the verdict, the last gate, the post-run proof (every run ends with one, 6.6),
    every control result, the kill switch sequence when it ran, and the unreached checkpoints filled."""

    run_id: str
    run_type: str
    verdict: RunVerdict
    last_gate: str
    proof: Proof
    results: list[ControlResult] = field(default_factory=list)
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
                 watchdog_status: Callable[[], tuple[float, bool]] | None = None, wall: WallBudget | None = None,
                 spend: SpendTracker | None = None, model: ModelAvailability | None = None,
                 counters: RateCounters | None = None,
                 safety_listener: Callable[[SessionSafety], None] | None = None):
        self.rig = rig
        self.ctx = ctx
        self.sink = sink
        self.clock = clock or SystemClock()
        self.budgets = budgets or ShardBudgets()
        self.relogin = relogin or ReloginBudget()
        self.ledger = ledger or DropLedger()
        self.heartbeat = heartbeat
        self.watchdog_status = watchdog_status or rig.watchdog_status
        self.kill_flag, self.kill_pipe = rig.kill_channels()
        self.wall = wall if wall is not None else WallBudget.for_run(ctx.run_type, self.clock.monotonic())
        self.spend = spend or SpendTracker()
        self.model = model or ModelAvailability()
        self.counters = counters if counters is not None else RateCounters()
        self.safety = SessionSafety()
        self.safety_listener = safety_listener
        self.state = "provisioned"
        self.results: list[ControlResult] = []
        self.stop: ControlResult | None = None
        self.session = "never"
        self.start_book: OrderBook | None = None
        self.declared_sends: set[str] = set()
        self.flow: FlowTimer | None = None
        self.flows_since_launch = 0
        self.launched_mono: float | None = None
        self._last_flow: str | None = None
        self._dialog = DialogTimer()
        self._disk_warned = False
        self._stamp: dict[str, str] | None = None
        self._session_started_at: datetime | None = None
        self._a1: str | None = None
        self._a2: str | None = None

    # -- recording --------------------------------------------------------------------------------------
    def apply(self, r: ControlResult) -> ControlResult:
        """Record a control result; the first one that sets a run outcome stops the shard. A `session_kicked`
        result marks the session kicked (its proof takes the kicked path of 6.6), and a result that kills the tree
        or runs the kill switch ends the session's safety state."""
        self.results.append(r)
        if r.unreached is not None and r.unreached.reason == "session_kicked":
            self.session = "kicked"
            self._clear_safety()
        elif r.has("kill_tree") or r.has("kill_switch"):
            self._clear_safety()
        if r.run is not None and self.stop is None:
            self.stop = r
        return r

    def _contain(self, r: ControlResult) -> None:
        """A stopping result that asks for deny-all or the kill switch puts the hook in deny-all at once (6.6 step 1)."""
        if r.run is not None and (r.has("deny_all") or r.has("kill_switch")):
            self.rig.set_hook_state(armed=True, deny_all=True)

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
        r = watchdog_check(*self.watchdog_status())
        if r is not None:
            self.apply(r)
        return self.stop is None

    # -- the session's safety state (6.3, 6.4, SD-57) ---------------------------------------------------
    def _set_safety(self, s: SessionSafety) -> None:
        if s != self.safety:
            self.safety = s
            if self.safety_listener is not None:
                self.safety_listener(s)

    def _clear_safety(self) -> None:
        """The session ended (or has not been asserted yet): nothing attested, no assertion, no stamp."""
        self._stamp = None
        self._set_safety(SessionSafety(session_kicked=self.session == "kicked"))

    def _new_session(self) -> None:
        """The `authenticated` gate passed: a new session, with nothing asserted in it yet."""
        self.session = "established"
        self._session_started_at = self.clock.now()
        self._clear_safety()

    def _asserted(self, read: SelectorRead) -> None:
        """A passing paper-mode assertion of `read`: the session's stamp and its last assertion time (6.4)."""
        self._stamp = paper_stamp(read.text or "", self.clock.now())
        self._set_safety(replace(self.safety, last_assertion_mono=self.clock.monotonic(),
                                 account_id_sha256=self._stamp["account_id_sha256"]))

    def paper_stamp(self) -> dict[str, str] | None:
        """The paper-asserted stamp `{account_id_sha256, mode, asserted_at}` of the current session's latest passing
        assertion, for every checkpoint and outcome record (6.4; 8.3 `paper_asserted_at`); None when no assertion
        has passed in the current session. Take it at capture time: it is cleared when the session ends."""
        return dict(self._stamp) if self._stamp is not None else None

    def may_release(self, stamp: dict[str, str] | None) -> bool:
        """Capture releases no frame to the Reviewer, the agent or a report without a paper-asserted stamp from the
        current session (6.4 Schedule)."""
        return self._stamp is not None and stamp_current(stamp, self._session_started_at)

    # -- the gate sequence --------------------------------------------------------------------------------
    def run(self, work: Callable[["Orchestrator"], None] | None = None) -> RunReport:
        """Run the shard. `work` runs the shard's work items while the state is `running`, reports every control
        result through `apply` and returns as soon as `stop` is set (a result that set a run outcome)."""
        if self._gates() and self._gate("running"):
            if work is not None:
                work(self)
            self._end_of_work()
        return self._finish()

    def _end_of_work(self) -> None:
        """The last check of `running`, once the work returned (6.6 Triggers, killflag choice 5; 6.4): a human stop
        request still pending on the kill channels (written after the last boundary, or left in place by fire drill
        item 5) ends the run `aborted_safety (reason: kill_switch)` like one seen at a boundary, and a rate cap the
        last event reached ends it `aborted_budget (reason: rate_cap)`."""
        if self.stop is not None:
            return
        running = self.flow.flow_id if self.flow is not None else self._last_flow
        human = killflag.check(self.kill_flag, self.kill_pipe, gate=self.state, running_flow=running)
        if human is not None:
            self._contain(self.apply(human))
            return
        self._rate_cap()

    def _rate_cap(self) -> bool:
        """6.4: right before an event that counts toward a rate cap (a login, an A9 drill ticket, an order-stage step)
        and at the end of the work, a cap already reached ends the run `aborted_budget (reason: rate_cap)` and the
        event is not made; True when it did. The 6.6 recovery logins are never held back, and still count."""
        if self.stop is not None:
            return False
        r = rate_cap(self.counters.counts(self.clock.now()), self.counters.caps)
        if r is not None:
            self.apply(r)
        return r is not None

    def _gates(self, start: str = "provisioned") -> bool:
        """The gates from `start` (`provisioned` for the shard, `launched` for a relaunch) to `canary_flow_passed`."""
        if start not in ("provisioned", "launched"):
            raise ValueError(f"the gate sequence starts at provisioned or launched, not {start!r}")
        ctx, rig = self.ctx, self.rig
        if start == "provisioned":
            if not self._gate("provisioned"):
                return False
            r = self.apply(check_provisioned(rig.preflight(), repeat_gates=frozenset(
                g for g in ctx.history.failed_gates_last_run if g.startswith("preflight."))))
            if not r.passed:
                return False
        first = start == "provisioned"
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
        self.launched_mono = self.clock.monotonic()
        self.flows_since_launch = 0
        rig.set_hook_phase("running")
        if not self._gate("authenticated") or self._rate_cap():
            return False
        self.counters.login(self.clock.now())
        r = self.apply(check_authenticated(rig.login()))
        if not r.passed:
            return False
        self._new_session()
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

    def _read_identity(self) -> tuple[ControlResult, SelectorRead]:
        """One paper-mode assertion (6.4): a selector read, and under deny-all a second one 1 s later when the first
        is unreadable; a pass lifts the deny-all and stamps the session."""
        rig, ctx = self.rig, self.ctx
        reads = [rig.read_account_selector()]
        if not selector_readable(reads[0]):
            rig.set_hook_state(armed=True, deny_all=True)          # nothing is pressed while identity is unknown
            self.clock.sleep(READ_GAP_S)
            reads.append(rig.read_account_selector())
        r = self.apply(judge_identity(reads, ctx.allowlist,
                                      unreadable_streak=ctx.history.identity_unreadable_streak))
        if r.passed:
            if r.has("rearm"):
                rig.set_hook_state(armed=True, deny_all=False)
            self._asserted(reads[-1])
        return r, reads[-1]

    def _paper_mode(self) -> bool:
        rig, ctx = self.rig, self.ctx
        r, read = self._read_identity()
        if not r.passed:
            return False
        self._a1 = read.crop_sha256
        if not self._snapshot():
            return False
        selected = read.text or ""
        if selected != ctx.fixture_account:
            rig.select_account(ctx.fixture_account)
            # 6.4: the selector text must equal an allowlist entry before every flow, the selected one too
            r, after = self._read_identity()
            self._a2 = after.crop_sha256
            if not r.passed:
                return False
            r = self.apply(judge_fixture_account(selected, ctx.fixture_account, after_select=after.text))
            return r.passed
        self._a2 = read.crop_sha256
        return True

    def _snapshot(self) -> bool:
        """The run-start order snapshot, once identity is known; after a relaunch the snapshot is kept and the fresh
        read is checked against it instead (module docstring). False when a new row trips the kill switch."""
        book = self.rig.read_orders()
        if self.start_book is None:
            self.start_book = book
            return True
        self.declared_sends |= {d.row_key for d in self.rig.declared_sends()}
        rows = new_order_rows(self.start_book, book, frozenset(self.declared_sends))
        if not rows:
            return True
        r = self.apply(replace(kill_switch("new_order_row", gate=self.state, running_flow=self._last_flow),
                               evidence={"new_rows": [x.key for x in rows]}))
        self._contain(r)
        return False

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
        checks: list[ConfirmationCheck] = []
        if all_match:
            self._set_safety(replace(self.safety, attestation_stage="a3_a8"))    # SD-57: the A9 click may run
            while len(checks) < ctx.profile.a9_runs and (not checks or a9_passed(checks[-1])):
                if self._rate_cap():                            # no drill ticket past a reached cap (6.4)
                    return False
                self.counters.order_staged(self.clock.now())   # A9 stages the drill ticket (6.4 rate caps)
                checks.append(rig.confirmation_check())         # twice when A3 is "absent" (6.13 item 3)
        a9 = checks[0] if checks else None
        a9b = checks[1] if len(checks) > 1 else None
        rc = ctx.run_type == "rc"
        a10 = rig.read_notifications() if rc and all_match else None
        evidence = {"A1": self._a1, "A2": self._a2, **{i: reads[i].last.screenshot_sha256 for i in SAFETY_ROWS},
                    "A9": a9.screenshot_sha256 if a9 else None, "A10": a10.screenshot_sha256 if a10 else None}
        if ctx.profile.a9_runs == 2:
            evidence["A9_second"] = a9b.screenshot_sha256 if a9b else None
        res = attest(reads, ctx.profile, a9=a9, a9_second=a9b, a10=a10, rc_run=rc, evidence=evidence,
                     previous_unreadable=ctx.history.attestation_unreadable_last_run)
        self.apply(res.result)
        if not res.result.passed:
            return False
        try:                                    # the A6 bindings and the pinned defaults, at most sixteen (5.4.7)
            chords = order_hotkey_list(res.chords, ctx.default_hotkeys())
        except GatewayConfigError as err:
            self.apply(err.result)
            return False
        rig.load_order_chords(chords)
        self._set_safety(replace(self.safety, attestation_stage="a3_a9"))    # SD-57: a later send may run
        return True

    # -- boundaries inside `running` (boundary.py) -----------------------------------------------------
    def flow_boundary(self, obs: BoundaryObservations | None = None, *, now: float | None = None,
                      flow_id: str | None = None, p50_s: float | None = None) -> ControlResult | None:
        """The checks before every flow, census visit or sweep ladder, in boundary.py's order; returns the result
        that stopped the run (already applied), or None, in which case the unit may start. `now` is the
        Orchestrator clock's monotonic reading (the default); `flow_id` names a flow, whose budget `p50_s` sets
        (3 x p50, 5 minutes until measured) and which counts toward the proactive relaunch."""
        if self.stop is not None:
            return self.stop
        now = self.clock.monotonic() if now is None else now
        obs = obs or BoundaryObservations()
        self.flow = None                                   # the unit before this boundary has ended
        checks: tuple[Callable[[], ControlResult | None], ...] = (
            lambda: self._tripwires(now, obs, read_book=True),
            lambda: self._watchdog(obs),
            lambda: self._disk(obs),
            lambda: self._run_budget(now, obs),
            self.spend.at_flow_boundary,
            lambda: self._model(obs))
        for check in checks:
            r = check()
            if r is not None:
                self._contain(self.apply(r))
                if self.stop is not None:
                    return self.stop
        self.assert_paper_mode()                           # before every flow (6.4 Schedule)
        if self.stop is not None:
            return self.stop
        uptime = now - self.launched_mono if self.launched_mono is not None else 0.0
        if proactive_relaunch_due(self.flows_since_launch, uptime, inside_flow=False):
            if not self.relaunch("proactive") or self._rate_cap():     # its login and A9 ticket count too (6.4)
                return self.stop
            now = self.clock.monotonic()
        if flow_id is not None:
            self.flows_since_launch += 1
            self._last_flow = flow_id
            self.flow = FlowTimer(flow_id, now, flow_budget_s(p50_s))
        return None

    def step_boundary(self, obs: BoundaryObservations | None = None, *, now: float | None = None,
                      order_stage_step: bool = False, session_dialog: bool = False) -> ControlResult | None:
        """The checks before every step of a flow: the trip-wires, the flow's own budget and the paper-mode assertion
        when 6.4 makes it due (`order_stage_step`: the next step is an order-stage step; `session_dialog`: a re-login
        or session dialog was just handled). Returns the result that stopped the run, or the flow-budget result
        (`end_flow`: the flow ends, its remaining checkpoints `not_run (reason: budget)`), else None."""
        if self.stop is not None:
            return self.stop
        now = self.clock.monotonic() if now is None else now
        obs = obs or BoundaryObservations()
        r = self._tripwires(now, obs, read_book=False)
        if r is not None:
            self._contain(self.apply(r))
            return self.stop
        if self.flow is not None and self.flow.over(now):
            cut = self.apply(flow_budget_cut(self.flow, now))
            self.flow = None
            return cut
        last = self.safety.last_assertion_mono
        due = "now" if last is None else assertion_due(now, last, boundary=order_stage_step or session_dialog,
                                                         main_foreground=obs.main_foreground, modal_up=obs.modal_up)
        if due == "now":
            self.assert_paper_mode()
        elif due == "unreadable" and last is not None:
            self.rig.set_hook_state(armed=True, deny_all=True)
            self.apply(deferral_unreadable(deferred_s=now - last - TIMER_S,
                                           unreadable_streak=self.ctx.history.identity_unreadable_streak))
        if order_stage_step and self.stop is None and not self._rate_cap():
            self.counters.order_staged(self.clock.now())  # the step about to run stages an order (6.4 rate caps)
        return self.stop

    def assert_paper_mode(self) -> ControlResult:
        """The paper-mode assertion outside the gate (6.4 Schedule): before every flow, after a re-login or session
        dialog, on the two-minute timer, before every order-stage step. A mismatch is `aborted_safety` with the
        kill switch; two unreadable reads 1 s apart are the identity-unreadable path."""
        if self.stop is not None:
            return self.stop
        r, _ = self._read_identity()
        self._contain(r)
        return r

    def _tripwires(self, now: float, obs: BoundaryObservations, *, read_book: bool) -> ControlResult | None:
        """The 6.6 trip-wires at a boundary: first the human channels, the rig's file flag and named pipe
        (`killflag.check`, the request named on the decision); then the caller's wires, plus a new Working or Filled
        row against the run-start snapshot (the rig's declared sends excluded), the Window Watcher's `order_event`
        window and dialog age, the dialog timer and the run's gateway drops."""
        running = self.flow.flow_id if self.flow is not None else self._last_flow
        human = killflag.check(self.kill_flag, self.kill_pipe, gate=self.state, running_flow=running)
        if human is not None:
            return human
        self.declared_sends |= obs.declared_sends | {d.row_key for d in self.rig.declared_sends()}
        orders = obs.orders if obs.orders is not None else (self.rig.read_orders() if read_book else None)
        rows = new_order_rows(self.start_book, orders, frozenset(self.declared_sends))
        open_s = self._dialog.observe(obs.order_dialog_visible, now) if obs.order_dialog_visible is not None else 0.0
        seen = self.rig.window_tripwires()
        w = obs.tripwires
        wires = replace(w, new_working_or_filled=w.new_working_or_filled or bool(rows),
                        order_event_window=w.order_event_window or seen.order_event_window,
                        dialog_open_s=max(w.dialog_open_s, open_s, seen.dialog_open_s),
                        gateway_drops=max(w.gateway_drops, self.ledger.run_count))
        return tripwire_result(wires, gate=self.state, running_flow=running, drop_limit=self.ledger.per_run,
                               new_rows=rows)

    def _watchdog(self, obs: BoundaryObservations) -> ControlResult | None:
        return watchdog_check(*(obs.watchdog if obs.watchdog is not None else self.watchdog_status()))

    def _disk(self, obs: BoundaryObservations) -> ControlResult | None:
        free = obs.free_bytes if obs.free_bytes is not None else self.rig.free_bytes()
        r = disk_check(free, obs.disk_floor_bytes)
        if r is not None and r.run is None:                 # the run-health warning, once per shard
            if self._disk_warned:
                return None
            self._disk_warned = True
        return r

    def _run_budget(self, now: float, obs: BoundaryObservations) -> ControlResult | None:
        return run_budget(now_s=self.wall.elapsed(now), wall_budget_s=self.wall.budget_s,
                          report_margin_s=self.wall.margin_s, counters=self.counters.counts(self.clock.now()),
                          superseded=obs.superseded, caps=self.counters.caps)

    def _model(self, obs: BoundaryObservations) -> ControlResult | None:
        if obs.model_endpoint_ok is None or not self.model.recheck(obs.model_endpoint_ok):
            return None
        return ControlResult("model_recheck", True, "running",
                             detail="model endpoint back: review live, agent fallback and the Explorer enabled (10.7)")

    # -- relaunch (7.7, 10.2) ---------------------------------------------------------------------------
    def relaunch(self, cause: str) -> bool:
        """The re-login flow of 7.7 for `cause` (RELAUNCH_BUDGETS): an environment re-login spends one of K, and past
        K ends the run `complete_with_env_errors (reason: relogin_budget)`; the other causes spend nothing here. The
        tree is killed, `session_release_wait` waited, and the state machine re-enters at `launched` and passes every
        gate again, then `running`; the caller resumes at the next flow boundary. True when the shard is `running`
        again, False when the budget or a gate stopped the run."""
        if cause not in RELAUNCH_BUDGETS:
            raise ValueError(f"relaunch cause {cause!r}; one of {sorted(RELAUNCH_BUDGETS)}")
        if self.stop is not None:
            return False
        if cause == "environment_relogin" and not self.apply(self.relogin.environment_relogin()).passed:
            return False
        spent = {"environment_relogin": f"re-login {self.relogin.used} of {self.relogin.k}",
                 "app_failure": f"app relaunch {self.budgets.used_app} of {self.budgets.app_relaunches}",
                 "hook_host": f"hook host restart {self.budgets.used_hook_host} of {self.budgets.hook_host_restarts}",
                 "proactive": "none"}[cause]
        self.apply(ControlResult("relaunch", True, "running",
                                 detail=f"relaunch ({cause}), budget: {spent}; every gate passes again",
                                 evidence={"cause": cause, "budget": RELAUNCH_BUDGETS[cause],
                                           "flows_since_launch": self.flows_since_launch}))
        self.rig.set_hook_state(armed=True, deny_all=True)
        self.rig.kill_process_tree()
        self.session = "ended"                             # until the relaunch's `authenticated` gate passes again
        self.rig.reset_input_state()                       # 7.7: after the kill, before the relaunch
        self._clear_safety()
        self.flow = None
        self.clock.sleep(SESSION_RELEASE_WAIT_S)
        return self._gates(start="launched") and self._gate("running")

    # -- ending ----------------------------------------------------------------------------------------
    def _finish(self) -> RunReport:
        stop, rig, ctx = self.stop, self.rig, self.ctx
        last_gate = self.state
        self.flow = None
        self._clear_safety()
        ks: KillSwitchReport | None = None
        streak = ctx.history.unproven_streak
        so_far = stop.run if stop else None
        kill_switch_path = stop is not None and (stop.run_status == "aborted_safety" or stop.has("kill_switch"))
        sends = rig.declared_sends()                        # the gated branch's declared sends, for every proof (6.7)
        account = account_ref(ctx.allowlist, ctx.fixture_account)
        if not kill_switch_path and ((stop is not None and stop.has("kill_tree")) or self.session in ("kicked",
                                                                                                       "ended")):
            rig.kill_process_tree()          # a kicked session's tree goes too: the proof's login starts afresh
            rig.reset_input_state()          # as before every relaunch (7.7)
        if kill_switch_path:
            ks = self._kill_switch_sequence(stop, so_far, sends)
            proof = ks.proof
            self.apply(ks.proof_effect)
        elif self.session == "never" or (self.session in ("established", "ended") and self.start_book is None
                                         and rig.gateway_log().tokens_issued == 0):
            tokens = rig.gateway_log().tokens_issued
            proof = post_run_proof(ProofInputs(start=None, end=None, session="never", tokens_issued=tokens))
            self.apply(proof_result(proof, run_so_far=so_far, unproven_streak=streak))
        elif self.session in ("kicked", "ended") or (stop is not None and stop.has("kill_tree")):
            if self.session == "ended" and stop is not None and stop.unreached is not None \
                    and stop.unreached.reason in NEVER_RETRIED:          # 6.9: no second login after it
                proof, end = post_run_proof(ProofInputs(start=self.start_book, end=None, recovery_login_ok=False,
                                                        declared_sends=sends)), None
            else:
                proof, end = self._recovery_proof(kicked=self.session == "kicked", sends=sends)
            # the run so far includes a 6.4 mismatch the proof's login applied (the login blocked for it, SD-64)
            self.apply(proof_result(proof, run_so_far=self._run_so_far(), unproven_streak=streak, account=account,
                                    screenshot_sha256=_screenshot(end)))
        else:
            self.state = last_gate = "teardown"
            obs = rig.teardown()
            self.apply(check_teardown(obs))
            end = rig.read_orders()
            proof = post_run_proof(ProofInputs(start=self.start_book, end=end, declared_sends=sends,
                                               cancel_all="not_needed" if obs.cancel_all_verified else "failed"))
            effect = proof_result(proof, run_so_far=so_far, unproven_streak=streak, account=account,
                                  screenshot_sha256=_screenshot(end))
            if proof.verdict == ORDERS_SENT:
                # 1.10, 6.6: an undeclared row at teardown is an aborted_safety; the kill switch sequence cancels it,
                # and the incident pages once, after the sequence, with what Cancel All finally did (9.10)
                ks = self._kill_switch_sequence(effect, effect.run, sends)
                effect = teardown_incident(proof, effect, ks, account=account, screenshot_sha256=_screenshot(end))
                proof = teardown_proof(proof, ks)
            self.apply(effect)
        rig.set_hook_phase("between_runs")                  # every run ends with the hook disarmed (6.6, 6.2)
        report = RunReport(ctx.run_id, ctx.run_type, RunVerdict("complete"), last_gate, proof, kill_switch=ks)
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

    def _run_so_far(self) -> RunVerdict | None:
        """The most severe run outcome recorded so far (results.worse: the first wins a tie)."""
        out: RunVerdict | None = None
        for r in self.results:
            out = worse(out, r.run)
        return out

    def _kill_switch_sequence(self, decision: ControlResult, so_far: RunVerdict | None,
                              sends: tuple[DeclaredSend, ...]) -> KillSwitchReport:
        """The 6.6 sequence on the rig with the run's declared sends; its recovery login counts toward the 6.4 rate
        caps, a 6.4 mismatch that login saw is applied (killswitch choice 18), and the human requests pending on the
        kill channels are handled once it has run (killflag.py)."""
        ctx = self.ctx
        ks = execute_kill_switch(self.rig, decision, clock=self.clock, allowlist=ctx.allowlist, start=self.start_book,
                                 fixture_account=ctx.fixture_account, declared_sends=sends, run_so_far=so_far,
                                 unproven_streak=ctx.history.unproven_streak)
        if any(name == "recovery_login" for name, _ in ks.steps):
            self.counters.login(self.clock.now())
        if ks.identity is not None:
            self.apply(ks.identity)
        killflag.acknowledge(self.kill_flag, self.kill_pipe)
        return ks

    def _recovery_proof(self, *, kicked: bool, sends: tuple[DeclaredSend, ...]) -> tuple[Proof, OrderBook | None]:
        """6.6 step 5 (and the kicked-session rule): in the hook's recovery-only arm, a fresh scripted login with A1
        and A2, then the proof; returns it with the last Monitor read. A 6.4 mismatch that login reads is applied like
        any assertion and ends the attempts (killswitch choice 18); after `live_identity` the proof is UNPROVEN
        `session_unavailable` and nothing more is read (SD-64)."""
        rig, ctx = self.rig, self.ctx
        attempts = 2 if kicked else 1
        login = RecoveryLogin(False)
        for _ in range(attempts):
            if kicked:
                self.clock.sleep(SESSION_RELEASE_WAIT_S)
            rig.set_hook_phase("recovery")             # a failed attempt left deny-all and no tree (killswitch 19)
            self.counters.login(self.clock.now())
            login = recovery_login(rig, ctx.allowlist, self.clock, ctx.fixture_account)
            if login.identity is not None:
                self._contain(self.apply(login.identity))
                break
            if login.ok:
                break
        if login.live_identity:
            return post_run_proof(ProofInputs(start=self.start_book, end=None, session="blocked",
                                              declared_sends=sends)), None
        ok = login.ok
        end = rig.read_orders() if ok else None
        cancel, found = "not_needed", None
        if end is not None:
            cancel, end, found = cancel_new_working(rig, self.start_book, end, clock=self.clock)    # 6.6 step 5
        if kicked:
            return post_run_proof(ProofInputs(start=self.start_book, end=end, session="kicked", kicked_login_ok=ok,
                                              cancel_all=cancel, declared_sends=sends, found=found)), end
        return post_run_proof(ProofInputs(start=self.start_book, end=end, recovery_login_ok=ok, cancel_all=cancel,
                                          declared_sends=sends, found=found)), end


def _screenshot(book: OrderBook | None) -> str | None:
    """The Working Orders screenshot of a Monitor read, for the 6.6 Cancel All failure page."""
    return book.screenshot_sha256 if isinstance(book, MonitorBook) else None
