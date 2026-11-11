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

`post_run_proof` gives one of the five verdicts: `PROVEN: no orders sent`, `PROVEN: only declared
sends` (gated branch), `PROVEN: no session established`, `UNPROVEN` with `recovery_login_failed`,
`monitor_unreadable`, `cancel_all_failed`, `snapshot_missing` or `session_unavailable`, or `ORDERS SENT`
when a clean read finds an undeclared new Working, Filled or Canceled row, the most severe. `proof_result`
turns it into the run effect: ORDERS SENT is `aborted_safety (reason: undeclared_order_row)` after the fact
(S1), with the login blocked and the rig disabled; UNPROVEN is `complete_with_env_errors (reason:
unproven_proof)` unless the run is already `aborted_safety`, blocks the next run on the login, and a second
UNPROVEN in a row on one login disables the rig and files a `harness_error` finding against the proof flow.

Choices (reported):

1. The abort reason of a kill-switch run is the trigger's own code where 4.13 has one
   (`attempted_send`, `live_identity`, `paper_mode_mismatch`, `attestation_mismatch`) and `kill_switch`
   for the human channels, a new row, an `order_event` window, the 30-second dialog, ten drops and a dead
   Orchestrator with the flag set; unreached checkpoints carry that same reason (1.10, 12.2).
2. A changed Position Statement row count or a Net Liq drift beyond 0.50 (the sample's tolerance) with
   no new order row to explain it means the Monitor reads do not agree: UNPROVEN `monitor_unreadable`.
3. The no-session rule is applied literally: the login never authenticated and the gateway issued no
   token in the run. A login that typed credentials issued secret tokens, so it takes the normal path.
4. `Cancel All` re-reads Working every 5 s until a clean read (readable and agreeing with Copy All) shows no new row
   of any kind, declared sends included, or 60 s pass (6.6 step 5: "until Working shows no new rows"). The clean read
   that made it click is kept (`ProofInputs.found`): its undeclared rows are ORDERS SENT whatever the re-reads show,
   so re-reads that stay unreadable give `ORDERS SENT: ...; Cancel All failed`, never UNPROVEN (6.6 "which UNPROVEN
   would understate"); the proof then names that read as its end read (choice 8).
5. Undeclared rows found by a clean read are ORDERS SENT whatever `Cancel All` did, so `Cancel All` failing
   on undeclared rows reads `ORDERS SENT: ...; Cancel All failed`; UNPROVEN `cancel_all_failed` is left for
   a Cancel All that failed with no undeclared row (declared sends of the gated branch left Working, or a
   teardown Cancel All not verified). When no undeclared row is Working, `Cancel All` was not
   needed and the line says so (`Cancel All not needed`; 6.6 writes the form with `verified` and `failed`).
   The abort is decided at `teardown`, so unreached checkpoints carry `env_error (gate: teardown, reason:
   undeclared_order_row)`.
6. Every proof records what `Cancel All` did (`Proof.cancel_all`: `not_needed`, `done` or `failed`).
   `cancel_new_working` is the one Cancel All and re-read loop of 6.6 step 5, used by the kill switch
   sequence and by the recovery proof of machine.py.
7. An undeclared row found by the teardown proof is one incident and gets one page (6.6 step 4, 9.10 safety
   routing): the page is sent after the kill switch sequence, never at the teardown read, so it carries the
   sequence's proof and what `Cancel All` finally did (`teardown_incident`). The teardown read's line ("Cancel All
   not needed": it clicked nothing) is not paged. When the sequence's proof is not ORDERS SENT (its recovery login
   failed, or no fresh read was clean), the run's proof stays the teardown read's ORDERS SENT with `Cancel All failed`
   for a Working row (`teardown_proof`), and the sequence's line is kept on the page's evidence.
8. Every proof carries the sha256 of the start and end Monitor reads it compared (`book_sha256`, 8.3
   `post_run_proofs.start_snapshot_sha256` and `end_snapshot_sha256`), None for a read it did not have; and its
   8.3 row form (`verdict_code`, `cancel_all_code`, `counts`), from which `proof_line` gives the 6.6 text back.
9. Declared sends (6.7) come in as the `records.DeclaredSend` rows SendGate wrote, matched to the Monitor rows by
   `row_key` (orderbook.row_key keeps an order's key when it moves from Working to Canceled). A declared send is
   proven only by a new Canceled row with its key and no Working row left: one still Working at the end (Cancel All
   not run, not verified, or rows left after 60 s) is UNPROVEN `cancel_all_failed`, the reason 6.6 gives for declared
   rows left; one with no Canceled row and no Working row is UNPROVEN `monitor_unreadable`, since the reads do not
   account for an order the run placed. Every proof lists the run's sends (`Proof.sends`, `counts["sends"]`,
   `send_lines`), so an UNPROVEN proof names the orders a human must check by hand.
10. Buying power (6.6 step 5) is a proof line when both Monitor reads carry it (`orderbook.MonitorBook`); like Net
    Liq (choice 2) a drift beyond the same 0.50 tolerance with no new order row to explain it is UNPROVEN
    `monitor_unreadable`.
11. A UNPROVEN proof with reason `recovery_login_failed` or `cancel_all_failed`, or any other UNPROVEN reason with
    `Cancel All` failed (clicked for a new Working row and not located, or not verified within 60 s, such as an
    unclean fresh read giving `monitor_unreadable`), re-sends the 9.10 safety page (the `aborted_safety` row when the
    run already is, else the UNPROVEN row) with the login owner added, and its detail and evidence carry the account,
    the Working Orders screenshot and the instruction to cancel by hand (6.6 Cancel All failure, which names the
    failure itself, not a reason, as the trigger). ORDERS SENT with Cancel All failed already pages the login owner;
    its evidence carries the same three, and its detail stays the VERDICT line, which the one incident page and the
    report print (choice 7). The
    account is named as the 6.6 proof block prints it (`D-xxxxxxxx`, its allowlist entry and the rig), so a page never
    echoes an account id (6.10).
12. Every result that blocks the login names the 8.3 `logins.blocked_reason` in `evidence["blocked_reason"]`:
    `incident` for ORDERS SENT (blocked until the incident closes), the UNPROVEN reason for `recovery_login_failed`,
    `cancel_all_failed` and `session_unavailable`, `live_identity` for a login already blocked for it (SD-64), and
    `unproven_proof` otherwise.
13. SD-64: after `live_identity` no recovery login is made; the proof is UNPROVEN `session_unavailable` and the login
    stays blocked (`ProofInputs.session == "blocked"`).
14. SD-61: a run that ended before any shard started (preflight G1 `config_invalid`) has one run-level 8.3 row with no
    shard and no login, `PROVEN: no session established` read from the gateway log (`config_invalid_proof`).
15. A human request (`ports.KillRequest`, killflag.py) is named in the decision's detail and evidence (6.6 step 2: the
    run is marked with its trigger).
16. Incident severity (6.12): a kill switch is S1 for a new order row or a Live identity, else S2 ("kill switch tripped
    with a clean proof"); the proof raises it to S1 when it finds an undeclared row (`proof_result`).
17. The hook per phase of the sequence (gateway.HOOK_STATES, 6.6 steps 1 and 5): deny-all before the tree is killed; the
    recovery-only arm for the recovery login and Cancel All, so the slow half tokens nothing but the scripted login and
    the recovery teardown actions; `between_runs` once the proof is produced and the snapshot reverted. After
    `live_identity` the recovery arm is never entered, since no recovery login is made (SD-64). `reset_input_state`
    runs right after the kill, as before every relaunch (7.7, 10.2 step 4), so no token of the dead tree's input is
    pending when the recovery login types.
18. The paper-mode assertion runs after every re-login (6.4 Schedule), the recovery login's included: a selector text
    that is not an allowlist entry is the 6.4 mismatch (`RecoveryLogin.identity`), which the caller applies as at any
    other read (S1 and the login blocked for `live_identity`; S2 for `paper_mode_mismatch`). The run keeps the abort
    that came first (results.worse: the first wins a tie), so a kill switch whose recovery login sees a Live account
    stays `aborted_safety (kill_switch)` with the S1 added. After `live_identity` nothing more is read through that
    session: the proof is UNPROVEN `session_unavailable` and the login is blocked for `live_identity` (SD-64), with no
    cancel-by-hand hand-over through it; a `paper_mode_mismatch` or unreadable selector is `recovery_login_failed`.
19. A recovery login that fails, at authentication or after it (A1 or A2), ends its session before it returns: the hook
    enters deny-all and the tree is killed (6.4 "an unidentified session must not stay logged in"; 6.6 step 5 "on
    failure, stop"), so no caller leaves it running and a second attempt (the kicked rule) starts from a fresh tree.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace

from ..records import DeclaredSend
from ..store.store import PostRunProof
from . import alerts
from .clock import Clock
from .identity import READ_GAP_S, Allowlist, judge_identity
from .orderbook import MonitorBook
from .ports import KillRequest
from .results import ControlResult, FindingSpec, RunVerdict, safety_abort_outcome
from .rig import GatewayLog, OrderBook, OrderRow, Rig, SelectorRead

DIALOG_OPEN_MAX_S = 30.0
CANCEL_REREAD_S = 60.0
CANCEL_POLL_S = 5.0
NET_LIQ_TOLERANCE = 0.50
GRIDS = ("working", "filled", "canceled")

PROVEN_NONE = "PROVEN: no orders sent"
PROVEN_DECLARED = "PROVEN: only declared sends"
PROVEN_NO_SESSION = "PROVEN: no session established"
UNPROVEN = "UNPROVEN"
ORDERS_SENT = "ORDERS SENT: undeclared rows"      # the fifth verdict, the most severe (6.6, choice 5)
# what Cancel All did, as the ORDERS SENT line prints it (6.6)
CANCEL_ALL_TEXT = {"done": "verified", "failed": "failed", "not_needed": "not needed"}
# the 8.3 `post_run_proofs` codes of a proof's verdict and of what Cancel All did (choice 8)
VERDICT_CODES = {PROVEN_NONE: "proven_no_sends", PROVEN_DECLARED: "proven_declared_sends",
                 PROVEN_NO_SESSION: "proven_no_session", UNPROVEN: "unproven", ORDERS_SENT: "orders_sent"}
CANCEL_ALL_CODES = {"not_needed": "not_needed", "done": "performed_verified", "failed": "performed_unverified"}
UNPROVEN_REASONS = ("recovery_login_failed", "monitor_unreadable", "cancel_all_failed", "snapshot_missing",
                    "session_unavailable")
# 4.13 aborted_safety codes a trigger keeps as the run reason (choice 1)
OWN_REASON = {"attempted_send": "attempted_send", "live_identity": "live_identity",
              "paper_mode_mismatch": "paper_mode_mismatch", "attestation_mismatch": "attestation_mismatch"}
SESSIONS = ("established", "never", "kicked", "blocked")
# the 8.3 logins.blocked_reason a UNPROVEN proof blocks the login with (choice 12)
UNPROVEN_BLOCK = {"recovery_login_failed": "recovery_login_failed", "cancel_all_failed": "cancel_all_failed",
                  "session_unavailable": "session_unavailable", "monitor_unreadable": "unproven_proof",
                  "snapshot_missing": "unproven_proof"}
HAND_OVER_REASONS = ("recovery_login_failed", "cancel_all_failed")      # 6.6 Cancel All failure (choice 11)
CANCEL_BY_HAND = ("cancel by hand through the hypervisor console now; the login stays blocked for every run type "
                  "until a human records the account as clean")


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


def kill_switch(trigger: str, *, gate: str, running_flow: str | None = None,
                request: KillRequest | None = None) -> ControlResult:
    """The decision of the 6.6 sequence for `trigger` at the aborting `gate` (steps 1 to 4 and 6). The unreached
    checkpoints carry the run's abort reason, `kill_switch` for the human channels and the automatic trip-wires
    (SD-59); `request` is the human request behind a `file_flag` or `named_pipe` trigger (choice 15)."""
    reason = OWN_REASON.get(trigger, "kill_switch")
    run, un = safety_abort_outcome(gate, reason, detail=f"trigger {trigger}")
    acts = ["deny_all", "kill_tree", "recovery_login", "revert_snapshot", "disable_rig"]
    ev: dict = {"trigger": trigger}
    if running_flow:
        acts.insert(1, "quarantine_flow")
    if trigger == "live_identity":
        acts.remove("recovery_login")                   # SD-64: no recovery login on a login blocked for live_identity
        acts.append("block_login")
        ev["blocked_reason"] = "live_identity"
    asked = ""
    if request is not None:
        ev["request"] = asdict(request)
        asked = f" (requested by {request.requested_by} via {request.channel}: {request.reason})"
    return ControlResult("kill_switch", False, gate, run=run, unreached=un, alert=alerts.safety_abort(),
                         actions=tuple(acts), incident="S1" if trigger in ("new_order_row", "live_identity") else "S2",
                         detail=f"kill switch: {trigger}{asked}" + (f"; flow {running_flow} quarantined" if running_flow
                                                                     else ""), evidence=ev)


# -- the post-run proof -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProofInputs:
    """What the proof reads (6.6). `session` is `established`, `never` (the login never authenticated), `kicked`
    (`session_kicked`, 7.7) or `blocked` (a login just blocked for `live_identity`: no recovery login, SD-64);
    `cancel_all` is `not_needed`, `done` or `failed` (not located, or rows left after 60 s); `declared_sends` are the
    gated branch's sends as SendGate recorded them (6.7, choice 9); `found` is the clean read that made `Cancel All`
    click (choice 4), whose undeclared rows count whatever `end` shows."""

    start: OrderBook | None
    end: OrderBook | None
    session: str = "established"
    tokens_issued: int = 1
    recovery_login_ok: bool | None = None       # None: no recovery login was needed
    kicked_login_ok: bool | None = None         # the one fresh login after session_release_wait (kicked only)
    cancel_all: str = "not_needed"
    declared_sends: tuple[DeclaredSend, ...] = ()
    net_liq_tolerance: float = NET_LIQ_TOLERANCE
    found: OrderBook | None = None

    def __post_init__(self):
        if self.session not in SESSIONS:
            raise ValueError(f"proof session {self.session!r}; one of {SESSIONS}")
        if self.cancel_all not in CANCEL_ALL_CODES:
            raise ValueError(f"cancel_all {self.cancel_all!r}")
        if not all(isinstance(d, DeclaredSend) for d in self.declared_sends):
            raise TypeError("declared sends are records.DeclaredSend rows (6.7)")


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
    """The ORDER SAFETY PROOF section's verdict, reason, undeclared rows, declared rows and sends, and table (6.6)."""

    verdict: str
    reason: str | None = None
    undeclared: tuple[OrderRow, ...] = ()
    declared: tuple[OrderRow, ...] = ()         # new Working or Canceled rows of declared sends (choice 9)
    rows: tuple[ProofRow, ...] = ()
    cancel_all: str = "not_needed"              # what Cancel All did before the final read (choice 6)
    start_sha256: str | None = None             # the Monitor reads the proof compared (choice 8)
    end_sha256: str | None = None
    sends: tuple[DeclaredSend, ...] = ()        # the run's declared sends, listed on every proof (choice 9)

    def __post_init__(self):
        if self.verdict not in (PROVEN_NONE, PROVEN_DECLARED, PROVEN_NO_SESSION, UNPROVEN, ORDERS_SENT):
            raise ValueError(f"proof verdict {self.verdict!r}")
        if self.cancel_all not in ("not_needed", "done", "failed"):
            raise ValueError(f"cancel_all {self.cancel_all!r}")
        if (self.verdict == UNPROVEN) != (self.reason is not None) or (self.reason and self.reason not in UNPROVEN_REASONS):
            raise ValueError(f"proof reason {self.reason!r} with {self.verdict}")
        if (self.verdict == ORDERS_SENT) != bool(self.undeclared):
            raise ValueError(f"{self.verdict} with {len(self.undeclared)} undeclared row(s): ORDERS SENT exactly when "
                             "a row is undeclared (6.6)")
        if self.proven and (self.verdict == PROVEN_DECLARED) != bool(self.sends):
            raise ValueError(f"{self.verdict} with {len(self.sends)} declared send(s): PROVEN: only declared sends "
                             "exactly when a proven run declared a send (6.6, 6.7)")

    @property
    def proven(self) -> bool:
        return self.verdict.startswith("PROVEN")

    @property
    def line(self) -> str:
        """The VERDICT line in the text form of 6.6, as the Reporter's Safety row and abort block print it (9.2):
        `PROVEN: ...`, `UNPROVEN: <reason>`, or `ORDERS SENT: undeclared rows (<rows>); Cancel All <verified|failed>`
        (`Cancel All not needed` when no undeclared row was Working, choice 5)."""
        return proof_line(self.verdict_code, self.reason, [r.key for r in self.undeclared], self.cancel_all_code)

    @property
    def verdict_code(self) -> str:
        """The 8.3 `post_run_proofs.verdict` of this proof."""
        return VERDICT_CODES[self.verdict]

    @property
    def cancel_all_code(self) -> str:
        """The 8.3 `post_run_proofs.cancel_all` of this proof."""
        return CANCEL_ALL_CODES[self.cancel_all]

    @property
    def counts(self) -> dict:
        """The 8.3 `post_run_proofs.counts_json` of this proof: per table line (working, filled and canceled
        orders, position statement rows: start, end, new, expected; net liq and buying power: start, end, drift) and
        its verdict, the undeclared and declared row keys the ORDERS SENT and declared-sends lines name (choice 8), and
        each declared send with its Working row and its Canceled row, or None (choice 9)."""
        rows: dict[str, dict] = {}
        for r in self.rows:
            if r.name in ("net liq", "buying power"):
                drift = None if r.start is None or r.end is None else round(abs(r.end - r.start), 2)
                rows[r.name] = {"start": r.start, "end": r.end, "drift": drift, "verdict": r.verdict}
            else:
                rows[r.name] = {"start": r.start, "end": r.end, "new": r.new, "expected": r.expected,
                                "verdict": r.verdict}
        canceled = {r.key for r in self.declared if r.grid == "canceled"}
        working = {r.key for r in self.declared if r.grid == "working"}
        sends = [{"row_key": d.row_key, "flow_id": d.flow_id, "step_id": d.step_id, "token_id": d.token_id,
                  "sent_at": d.sent_at, "symbol": d.symbol, "qty": d.qty, "working": d.row_key,
                  "canceled": d.row_key if d.row_key in canceled else None, "left_working": d.row_key in working}
                 for d in self.sends]
        return {"rows": rows, "undeclared": [r.key for r in self.undeclared],
                "declared": [r.key for r in self.declared], "sends": sends}


def proof_line(verdict_code: str, reason: str | None, undeclared: Sequence[str], cancel_all_code: str) -> str:
    """The 6.6 VERDICT line of a proof from its 8.3 row (verdict and cancel_all codes, the reason and the undeclared
    row keys of counts_json), so a proof read back from the store prints as `Proof.line` does (choice 8)."""
    verdict = next(v for v, c in VERDICT_CODES.items() if c == verdict_code)
    if verdict == ORDERS_SENT:
        cancel = next(k for k, c in CANCEL_ALL_CODES.items() if c == cancel_all_code)
        return f"{ORDERS_SENT} ({', '.join(undeclared)}); Cancel All {CANCEL_ALL_TEXT[cancel]}"
    if verdict == UNPROVEN:
        return f"{UNPROVEN}: {reason}"
    return verdict


def send_lines(counts: dict) -> list[str]:
    """One line per declared send under the VERDICT line of 6.6 ("each send listed with its Working and Canceled
    rows"), from a proof's counts_json, so a proof read back from the store lists its sends (choice 9)."""
    out = []
    for d in counts.get("sends", ()):
        end = (f"Canceled row {d['canceled']}" if d["canceled"] else
               "still Working, no Canceled row" if d["left_working"] else "no Canceled row")
        out.append(f"send {d['flow_id']} {d['step_id']} at {d['sent_at']}: Working row {d['working']}, {end}")
    return out


def book_sha256(book: OrderBook | None) -> str | None:
    """The sha256 of a Monitor read in canonical JSON form (8.3 `start_snapshot_sha256`, choice 8); None for none."""
    if book is None:
        return None
    return hashlib.sha256(json.dumps(asdict(book), sort_keys=True).encode()).hexdigest()


def _new_rows(start: OrderBook, end: OrderBook) -> dict[str, list[OrderRow]]:
    out = {}
    for g in GRIDS:
        before = {r.key for r in start.rows(g)}
        out[g] = [r for r in end.rows(g) if r.key not in before]
    return out


def clean(book: OrderBook | None) -> bool:
    """A clean Monitor read (6.6): readable and agreeing with Copy All."""
    return book is not None and book.readable and book.copy_all_agrees


def _undeclared(start: OrderBook, end: OrderBook, keys: set[str]) -> tuple[OrderRow, ...]:
    """The new rows of `end` no declared send explains: any Working or Canceled row not keyed to one, every fill."""
    new = _new_rows(start, end)
    return tuple(r for g in GRIDS for r in new[g] if r.key not in keys or g == "filled")


def _buying_power(book: OrderBook) -> float | None:
    return book.buying_power if isinstance(book, MonitorBook) else None


def post_run_proof(p: ProofInputs) -> Proof:
    """The proof verdict from the inputs (6.6 Post-run proof, Runs without a usable session), with what
    Cancel All did and the run's declared sends recorded on it."""
    verdict, reason, undeclared, declared, rows = _verdict(p)
    end = p.found if verdict == ORDERS_SENT and not clean(p.end) else p.end      # the read the rows came from (choice 4)
    return Proof(verdict, reason, undeclared, declared, rows, cancel_all=p.cancel_all, start_sha256=book_sha256(p.start),
                 end_sha256=book_sha256(end), sends=p.declared_sends)


_Verdict = tuple[str, str | None, tuple[OrderRow, ...], tuple[OrderRow, ...], tuple[ProofRow, ...]]


def _verdict(p: ProofInputs) -> _Verdict:
    if p.session == "never" and p.tokens_issued == 0:
        return PROVEN_NO_SESSION, None, (), (), ()
    if p.session == "blocked" or (p.session == "kicked" and not p.kicked_login_ok):
        return UNPROVEN, "session_unavailable", (), (), ()          # SD-64; the kicked rule
    if p.recovery_login_ok is False:
        return UNPROVEN, "recovery_login_failed", (), (), ()
    if p.start is None:
        return UNPROVEN, "snapshot_missing", (), (), ()
    keys = {d.row_key for d in p.declared_sends}
    found_rows = _undeclared(p.start, p.found, keys) if clean(p.start) and p.found is not None and clean(p.found) else ()
    end = p.end
    if end is None or not (clean(end) and clean(p.start)):
        if found_rows:                                   # the clean read that made Cancel All click (choice 4)
            return _verdict(replace(p, end=p.found, found=None))
        return UNPROVEN, "monitor_unreadable", (), (), ()           # either Monitor read failed its cross-check (6.6)
    new = _new_rows(p.start, end)
    declared = tuple(r for g in ("working", "canceled") for r in new[g] if r.key in keys)
    undeclared = _undeclared(p.start, end, keys)
    undeclared += tuple(r for r in found_rows if r.key not in {u.key for u in undeclared})
    work, fill, canc = new["working"], new["filled"], new["canceled"]
    left_working = [r for r in work if r.key in keys]
    canceled = {r.key for r in canc if r.key in keys}
    missing = [d for d in p.declared_sends if d.row_key not in canceled]
    rows = [
        ProofRow("working orders", len(p.start.working), len(end.working), len(work), 0,
                 "UNDECLARED" if any(r.key not in keys for r in work) else "not canceled" if work else "ok"),
        ProofRow("filled orders", len(p.start.filled), len(end.filled), len(fill), 0, "UNDECLARED" if fill else "ok"),
        ProofRow("canceled orders", len(p.start.canceled), len(end.canceled), len(canc), len(p.declared_sends),
                 "UNDECLARED" if any(r.key not in keys for r in canc) else "missing" if missing else "ok"),
    ]
    pos_change = end.position_rows - p.start.position_rows
    rows.append(ProofRow("position statement rows", p.start.position_rows, end.position_rows, max(pos_change, 0), 0,
                         "ok" if pos_change == 0 else "changed"))
    drift = None if p.start.net_liq is None or end.net_liq is None else abs(end.net_liq - p.start.net_liq)
    rows.append(ProofRow("net liq", p.start.net_liq, end.net_liq, 0, 0,
                         "ok" if drift is not None and drift <= p.net_liq_tolerance else "drift"))
    bp0, bp1 = _buying_power(p.start), _buying_power(end)
    bp_drift = None if bp0 is None or bp1 is None else abs(bp1 - bp0)
    if bp_drift is not None:                             # both reads carry it (choice 10)
        rows.append(ProofRow("buying power", bp0, bp1, 0, 0, "ok" if bp_drift <= p.net_liq_tolerance else "drift"))
    table = tuple(rows)
    if undeclared:                                       # a clean read: an order was sent (choice 5)
        return ORDERS_SENT, None, undeclared, declared, table
    if p.cancel_all == "failed" or left_working:         # declared sends left Working (choices 5, 9)
        return UNPROVEN, "cancel_all_failed", (), declared, table
    if missing or pos_change != 0 or drift is None or drift > p.net_liq_tolerance \
            or (bp_drift is not None and bp_drift > p.net_liq_tolerance):
        return UNPROVEN, "monitor_unreadable", (), declared, table
    return (PROVEN_DECLARED if p.declared_sends else PROVEN_NONE), None, (), declared, table


def _blocked_reason(proof: Proof, run_so_far: RunVerdict | None) -> str:
    """The 8.3 `logins.blocked_reason` of a proof that blocks the login (choice 12)."""
    if proof.verdict == ORDERS_SENT:
        return "incident"
    if run_so_far is not None and run_so_far.reason == "live_identity":
        return "live_identity"                           # SD-64: the login stays blocked for live_identity
    return UNPROVEN_BLOCK[proof.reason or ""]


def proof_result(proof: Proof, *, run_so_far: RunVerdict | None = None, unproven_streak: int = 0,
                 account: str | None = None, screenshot_sha256: str | None = None) -> ControlResult:
    """The run effect of a proof (6.6): ORDERS SENT (undeclared rows), UNPROVEN, or a passing proof.

    `unproven_streak` counts the UNPROVEN results on this login just before this one. `account` (as `account_ref`
    gives it) and `screenshot_sha256` (the last Working Orders read) go on the hand-over page of a failed recovery
    login or Cancel All (6.6 Cancel All failure, choice 11).
    """
    ev: dict = {"verdict": proof.line, "undeclared": [r.key for r in proof.undeclared], "cancel_all": proof.cancel_all}
    if proof.sends:
        ev["sends"] = send_lines(proof.counts)
    keep = run_so_far is not None and run_so_far.status == "aborted_safety"
    hand_over = proof.reason in HAND_OVER_REASONS or proof.cancel_all == "failed"     # whatever the reason (choice 11)
    detail = proof.line
    if not proof.proven:
        ev["blocked_reason"] = _blocked_reason(proof, run_so_far)
    if hand_over:
        ev.update(account=account, working_orders_screenshot=screenshot_sha256, instruction=CANCEL_BY_HAND)
        if proof.verdict == UNPROVEN:                    # ORDERS SENT keeps its VERDICT line as the page text (choice 11)
            shot = f"Working Orders screenshot {screenshot_sha256}" if screenshot_sha256 else "no Working Orders read"
            detail = f"{proof.line}; {account or 'the fixture account'}, {shot}: {CANCEL_BY_HAND}"
    if proof.verdict == ORDERS_SENT:
        acts = ("block_login", "disable_rig")
        run, un = safety_abort_outcome("teardown", "undeclared_order_row")
        return ControlResult("proof", False, "teardown", run=run, unreached=un,
                             alert=alerts.safety_abort().plus(paged=("login_owner",)), actions=acts, incident="S1",
                             detail=detail, evidence=ev)
    if not proof.proven:
        twice = unproven_streak + 1 >= 2
        alert = alerts.unproven_proof()
        if hand_over:                                    # the safety page re-sent with the login owner (choice 11)
            alert = (alerts.safety_abort() if keep else alert).plus(paged=("login_owner",))
        return ControlResult(
            "proof", False, "teardown", run=None if keep else RunVerdict("complete_with_env_errors", "unproven_proof"),
            alert=alert, actions=("block_login",) + (("disable_rig",) if twice else ()),
            findings=(FindingSpec("harness_error", "two UNPROVEN proofs in a row on one login: the proof reader is "
                                  "suspect", detail="proof flow"),) if twice else (),
            detail=detail, evidence=ev)
    return ControlResult("proof", True, "teardown", detail=detail, evidence=ev)


def config_invalid_proof(run_id: str, *, produced_at: str, gateway: GatewayLog = GatewayLog(),
                         send_policy_ref: str = "OPEN") -> PostRunProof:
    """The run-level 8.3 `post_run_proofs` row (no shard, no login; SD-61) of a run that ended before any shard
    started, such as `aborted_harness (reason: config_invalid)` at preflight G1: `PROVEN: no session established`,
    read from the gateway log (6.6 Runs without a usable session), for the caller to write with `Store.write_proof`.
    A gateway log that shows a token issued gives the UNPROVEN row instead. `send_policy_ref` defaults to `OPEN`, the
    default branch, since an invalid config may not name the risk owner's decision (choice 14)."""
    p = post_run_proof(ProofInputs(None, None, session="never", tokens_issued=gateway.tokens_issued))
    return PostRunProof(shard_id=None, run_id=run_id, login_alias=None, verdict=p.verdict_code, reason=p.reason,
                        send_policy_ref=send_policy_ref, counts=p.counts, cancel_all=p.cancel_all_code,
                        paper_mode_assertions=0, paper_mode_mismatches=0,
                        gateway_events=gateway.allowed + gateway.drops + gateway.refusals, gateway_drops=gateway.drops,
                        refusals_count=gateway.refusals, produced_at=produced_at)


# -- the sequence on a rig --------------------------------------------------------------------------------
@dataclass
class KillSwitchReport:
    """What `execute_kill_switch` did: the decision, the proof and each step with its monotonic time, and the 6.4
    mismatch its recovery login saw (`RecoveryLogin.identity`), which the caller applies before the proof effect."""

    result: ControlResult
    proof: Proof
    proof_effect: ControlResult
    steps: list[tuple[str, float]] = field(default_factory=list)
    identity: ControlResult | None = None


def masked_account(account_id: str) -> str:
    """An account id as the 6.6 proof block prints it: the prefix kept, every other character `x` (`D-xxxxxxxx`)."""
    head, sep, tail = account_id.partition("-")
    return f"{head}{sep}{'x' * len(tail)}" if sep else "x" * len(account_id)


def account_ref(allowlist: Allowlist, account_id: str | None) -> str:
    """The account a hand-over page names, as the 6.6 identity line does: masked, its allowlist entry, the rig."""
    if account_id is None or account_id not in allowlist:
        return f"the fixture account on rig {allowlist.rig}"
    return f"account {masked_account(account_id)} (allowlist entry {allowlist.index(account_id)}) on rig {allowlist.rig}"


def live_identity_blocked(decision: ControlResult, run_so_far: RunVerdict | None = None) -> bool:
    """True when the login was just blocked for `live_identity`, by the decision or the run so far: no recovery
    login may be made on it (6.4, 6.6 step 5, SD-64)."""
    return "live_identity" in (decision.run_reason, run_so_far.reason if run_so_far is not None else None)


@dataclass(frozen=True)
class RecoveryLogin:
    """What `recovery_login` did (6.6 step 5): `ok` when the session is usable for the proof; `identity` the 6.4
    mismatch result of its A1 read (`judge_identity`: `aborted_safety` with `live_identity`, S1 and the login blocked,
    or `paper_mode_mismatch`, S2) when the selector showed a text that is not an allowlist entry, for the caller to
    apply like any other assertion (choice 18)."""

    ok: bool
    identity: ControlResult | None = None

    def __bool__(self) -> bool:
        """True when the session is usable for the proof, as the bool `recovery_login` used to return."""
        return self.ok

    @property
    def live_identity(self) -> bool:
        return self.identity is not None and self.identity.run_reason == "live_identity"


def _assert_identity(rig: Rig, allowlist: Allowlist, clock: Clock) -> tuple[ControlResult, SelectorRead]:
    """One A1 read with the two-read unreadable path (6.4): the verdict and the read it judged."""
    reads = [rig.read_account_selector()]
    try:
        return judge_identity(reads, allowlist), reads[-1]
    except ValueError:
        clock.sleep(READ_GAP_S)
        reads.append(rig.read_account_selector())
        return judge_identity(reads, allowlist), reads[-1]


def recovery_login(rig: Rig, allowlist: Allowlist, clock: Clock, fixture_account: str | None) -> RecoveryLogin:
    """A fresh scripted login with A1 and A2 (6.6 step 5): relaunch, LOGIN, an allowlisted selector, the fixture
    account selected (A1 holds for the selected account too, 6.4). The caller puts the hook in the recovery-only arm
    first (choice 17): the login's typing goes through the gateway like any other input. A login that fails leaves no
    session behind: the hook enters deny-all and the tree is killed before it returns (choice 19)."""
    rig.launch()
    o = rig.login()
    if o.auth_error or o.challenge or o.session_kicked or o.live_selected or not o.vault_lease \
            or o.main_window_s is None or not o.post_login_anchor:
        return _close(rig, RecoveryLogin(False))
    verdict, read = _assert_identity(rig, allowlist, clock)
    if verdict.passed and fixture_account is not None and read.text != fixture_account:
        rig.select_account(fixture_account)
        verdict, read = _assert_identity(rig, allowlist, clock)
    if verdict.passed and (fixture_account is None or read.text == fixture_account):
        return RecoveryLogin(True)
    return _close(rig, RecoveryLogin(False, verdict if verdict.run_status == "aborted_safety" else None))


def _close(rig: Rig, login: RecoveryLogin) -> RecoveryLogin:
    """A failed recovery login's session ends at once (choice 19): deny-all, the tree killed, a fresh gateway."""
    rig.set_hook_phase("kill_switch")
    rig.kill_process_tree()
    rig.reset_input_state()
    return login


def cancel_new_working(rig: Rig, start: OrderBook | None, end: OrderBook, *, clock: Clock,
                       steps: list[tuple[str, float]] | None = None) -> tuple[str, OrderBook, OrderBook | None]:
    """6.6 step 5: when a fresh read of Working shows a row not in the start snapshot, click `Cancel All` and
    re-read every 5 s until a clean read shows no new Working row of any kind (declared sends too) or 60 s pass
    (choice 4). Returns what Cancel All did (`not_needed`, `done`, `failed`), the last read, and the read that made it
    click when that read was clean (`ProofInputs.found`), else None."""
    if start is None or not end.readable or not _new_rows(start, end)["working"]:
        return "not_needed", end, None
    found = end if clean(end) else None
    res = rig.cancel_all()
    if steps is not None:
        steps.append(("cancel_all", clock.monotonic()))
    deadline = clock.monotonic() + CANCEL_REREAD_S
    cancel = "done" if res.located and res.clicked else "failed"
    while cancel == "done":
        end = rig.read_orders()
        if clean(end) and not _new_rows(start, end)["working"]:
            break
        if clock.monotonic() >= deadline:
            cancel = "failed"
            break
        clock.sleep(CANCEL_POLL_S)
    return cancel, end, found


def execute_kill_switch(rig: Rig, decision: ControlResult, *, clock: Clock, allowlist: Allowlist,
                        start: OrderBook | None, fixture_account: str | None = None,
                        declared_sends: Sequence[DeclaredSend] = (), run_so_far: RunVerdict | None = None,
                        unproven_streak: int = 0) -> KillSwitchReport:
    """Drive steps 1, 3, 5 and 6 of the 6.6 sequence on `rig` after `kill_switch` decided (steps 2 and 4 are
    the decision's unreached outcome and alert, written by the caller), with the hook in the phase each step needs
    (choice 17). After `live_identity` (the decision's or the run's so far) no recovery login is made and the proof
    is UNPROVEN `session_unavailable` (SD-64)."""
    steps: list[tuple[str, float]] = []
    rig.set_hook_phase("kill_switch")
    steps.append(("deny_all", clock.monotonic()))
    rig.kill_process_tree()
    rig.reset_input_state()                     # a fresh gateway instance before the relaunch (7.7, 10.2 step 4)
    steps.append(("kill_tree", clock.monotonic()))
    sends = tuple(declared_sends)
    end: OrderBook | None = None
    so_far = run_so_far or decision.run
    login = RecoveryLogin(False)
    if live_identity_blocked(decision, run_so_far):
        inputs = ProofInputs(start=start, end=None, session="blocked", declared_sends=sends)
    else:
        cancel = "not_needed"
        rig.set_hook_phase("recovery")
        login = recovery_login(rig, allowlist, clock, fixture_account)
        steps.append(("recovery_login", clock.monotonic()))
        found = None
        if login.ok:
            cancel, end, found = cancel_new_working(rig, start, rig.read_orders(), clock=clock, steps=steps)
        inputs = ProofInputs(start=start, end=end, recovery_login_ok=login.ok, cancel_all=cancel, declared_sends=sends,
                             found=found)
        if login.identity is not None and login.live_identity:      # 6.4, SD-64: the login is now blocked for it
            inputs = ProofInputs(start=start, end=None, session="blocked", declared_sends=sends)
            so_far = login.identity.run
    proof = post_run_proof(inputs)
    steps.append(("proof", clock.monotonic()))
    rig.revert_snapshot()
    steps.append(("revert_snapshot", clock.monotonic()))
    rig.set_hook_phase("between_runs")
    shot = end.screenshot_sha256 if isinstance(end, MonitorBook) else None
    return KillSwitchReport(decision, proof, proof_result(proof, run_so_far=so_far, unproven_streak=unproven_streak,
                                                          account=account_ref(allowlist, fixture_account),
                                                          screenshot_sha256=shot), steps, identity=login.identity)


def teardown_proof(found: Proof, ks: KillSwitchReport) -> Proof:
    """The run's proof after the teardown read found an undeclared row and the kill switch sequence ran (6.6, choice
    7): the sequence's own proof when it is ORDERS SENT; otherwise (its recovery login failed, or no fresh read was
    clean) the teardown read's ORDERS SENT, since that clean read found an order and UNPROVEN would understate it, with
    `Cancel All` failed when an undeclared row was Working, because no cancel of it was ever verified (6.6 Cancel All
    failure: `ORDERS SENT: undeclared rows (<rows>); Cancel All failed`)."""
    if ks.proof.verdict == ORDERS_SENT:
        return ks.proof
    working = any(r.grid == "working" for r in found.undeclared)
    return replace(found, cancel_all="failed" if working else found.cancel_all)


def teardown_incident(found: Proof, decision: ControlResult, ks: KillSwitchReport, *, account: str | None = None,
                      screenshot_sha256: str | None = None) -> ControlResult:
    """The one control result, so the one 9.10 page, of an undeclared row found by the teardown proof (6.6, 9.10).

    `found` is the teardown read's ORDERS SENT proof, `decision` its `proof_result` (the `aborted_safety` that ran
    the kill switch sequence) and `ks` what the sequence did. The incident pages once, after the sequence, with what
    `Cancel All` finally did (choice 7): the sequence's own result when its proof is ORDERS SENT; otherwise (the
    recovery login failed, or the fresh Monitor read was unreadable) the decision's safety page, run outcome and
    unreached outcome, with the evidence of the run's proof (`teardown_proof`: its verdict, rows and Cancel All, the
    login blocked for `incident`, and the hand-over with `account` and the teardown read's `screenshot_sha256` when
    Cancel All failed), the sequence's own line under `kill_switch_proof`, and both reads named in the detail.
    """
    if ks.proof.verdict == ORDERS_SENT:
        return ks.proof_effect
    effect = proof_result(teardown_proof(found, ks), run_so_far=decision.run, account=account,
                          screenshot_sha256=screenshot_sha256)
    rows = ", ".join(r.key for r in found.undeclared)
    return replace(decision, actions=tuple(dict.fromkeys(decision.actions + effect.actions)),
                   findings=decision.findings + effect.findings,
                   detail=f"{ORDERS_SENT} ({rows}) at teardown; after the kill switch sequence: {ks.proof.line}",
                   evidence={**effect.evidence, "teardown_undeclared": [r.key for r in found.undeclared],
                             "kill_switch_proof": ks.proof.line})
