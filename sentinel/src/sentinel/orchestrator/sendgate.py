"""The gated branch's single-use send token (6.7 Gated branch; 6.2 Label rules, 6.4 Schedule, 5.4.3 stage 4, 5.4.4).

`SendGate` is the `gateway.SendClearance` the slow half consults for a click whose two labels are exactly `Send`
(gateway.py choice 11). It holds the policy and the run's counters and answers, at the moment of the click,
whether every gate of 6.7 holds, in the spec's order:

1. `policy/send_policy.yaml` is enabled (`SendPolicy.from_doc`: `enabled: true`, decision `approved`, a risk
   owner and a signature, L-FLW-08);
2. the flow's class is `paper_send_ok`;
3. A1 to A11 passed this session (the session's attestation stage is `a3_a9` and the session was not kicked);
4. the paper-mode assertion passed in a frame no older than 500 ms (`identity.send_frame_fresh`);
5. the account the session asserted is the GOLDEN or EMPTY entry the flow declares (`Action.account_role`);
6. the per-run and per-day send caps hold (default 5 and 20);
7. the caller is a scripted flow, never agent mode or the Explorer;
8. the Order Confirmation dialog is present and the foreground window, and the Oracle read its numbers.

When the slow half clears the click it calls `commit`: the token counts toward both caps and the send is
pending until the flow's Oracle read asserts the Working row and the Executor calls `declare`, which returns the
`records.DeclaredSend` the post-run proof lists under "PROVEN: only declared sends" (6.6). A token that was
dropped or expired unconsumed is released with `void`.

Choices where the spec is ambiguous or silent (reported):

1. A1 to A11 (6.7 gate 3). The session carries attestation as a stage up to `a3_a9` (SD-57). A1 and A2 are the
   paper-mode assertion and the account check (gates 4 and 5), A10 only gates the RC Explorer stage and A11
   (evidence stored) ends the run when it fails (attestation.py), so a running session at stage `a3_a9` has
   passed every row the send depends on.
2. Caps count tokens, not fills: a token counts when it is issued, and a voided token keeps its count, so the
   caps bound the send tokens and therefore the sends. The per-day count of earlier runs is `sends_today`, which
   the caller reads from the store.
3. One pending send at a time: while a send token is pending (issued, not yet declared or voided) the gate
   refuses another, so a flow cannot obtain a second send token before its first send is asserted. The refusal
   is the never-tokenize attempted send of 6.7 ("any gate above failing").
4. Accounts are compared by the sha256 of the account id, the form `SessionSafety.account_id_sha256` carries.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..records import DeclaredSend
from .gateway import CONFIRM_STAGE, Action, SafetyContext
from .identity import send_frame_fresh

PER_RUN_CAP = 5
PER_DAY_CAP = 20
SEND_CLASS = "paper_send_ok"
ACCOUNT_ROLES = ("golden", "empty")


@dataclass(frozen=True)
class SendPolicy:
    """The gated branch's switch and caps (6.7)."""

    enabled: bool = False
    per_run_cap: int = PER_RUN_CAP
    per_day_cap: int = PER_DAY_CAP

    @classmethod
    def from_doc(cls, data: Mapping[str, Any] | None, *, per_run_cap: int = PER_RUN_CAP,
                 per_day_cap: int = PER_DAY_CAP) -> SendPolicy:
        """`policy/send_policy.yaml` as parsed: enabled only when `enabled` is true with the risk owner's signature
        (L-FLW-08) and the decision is `approved` (6.7: the risk owner decides); a missing, unsigned or still
        `OPEN` file is the default branch, no sends."""
        d = data if isinstance(data, Mapping) else {}
        signed = bool(d.get("risk_owner")) and bool(d.get("signature"))
        return cls(d.get("enabled") is True and d.get("decision") == "approved" and signed, per_run_cap, per_day_cap)


class SendGate:
    """The gates of the single-use send token for one run (module docstring)."""

    def __init__(self, policy: SendPolicy, accounts: Mapping[str, str], *, sends_today: int = 0):
        """`accounts` maps the roles `golden` and `empty` to the sha256 of their account ids; `sends_today` is the
        number of send tokens earlier runs issued today on this login (choice 2)."""
        unknown = set(accounts) - set(ACCOUNT_ROLES)
        if unknown:
            raise ValueError(f"account roles {sorted(unknown)}; the send gate knows {ACCOUNT_ROLES}")
        self.policy = policy
        self.accounts = dict(accounts)
        self.sends_today = sends_today
        self.issued = 0
        self.pending: Action | None = None
        self.declared: list[DeclaredSend] = []

    def check(self, a: Action, ctx: SafetyContext) -> str | None:
        """None when every 6.7 gate holds for this click on `Send`; else the first failing gate, in 6.7's order."""
        session = ctx.session
        if not self.policy.enabled:
            return "send policy disabled"
        if a.flow_class != SEND_CLASS:
            return f"class {a.flow_class!r} is not {SEND_CLASS}"
        if session is None or not ctx.attested(CONFIRM_STAGE):
            return "A1 to A11 not passed in this session"
        if session.last_assertion_mono is None or ctx.now is None \
                or not send_frame_fresh(session.last_assertion_mono, ctx.now):
            return "paper-mode assertion not in a frame no older than 500 ms"
        want = self.accounts.get(a.account_role or "")
        if want is None or session.account_id_sha256 != want:
            return f"account is not the {a.account_role or 'undeclared'} entry the flow declares"
        if self.issued >= self.policy.per_run_cap:
            return f"per-run send cap {self.policy.per_run_cap} reached"
        if self.sends_today + self.issued >= self.policy.per_day_cap:
            return f"per-day send cap {self.policy.per_day_cap} reached"
        if a.profile != "scripted":
            return f"caller is profile {a.profile}, not a scripted flow"
        if not (ctx.order_dialog_present and ctx.dialog_foreground and ctx.dialog_read):
            return "Order Confirmation dialog not the foreground window with its numbers read"
        if self.pending is not None:
            return "a send token is already pending (choice 3)"
        return None

    def commit(self, a: Action, ctx: SafetyContext) -> None:
        """The slow half cleared the click with `send_ok`: count it and hold it pending (choices 2 and 3)."""
        failing = self.check(a, ctx)
        if failing is not None:
            raise ValueError(f"commit of a send the gate refuses: {failing}")
        self.issued += 1
        self.pending = a

    def declare(self, *, run_id: str, shard_id: str, flow_id: str, step_id: str, row_key: str, sent_at: str,
                token_id: int | None = None, symbol: str | None = None, qty: float | None = None) -> DeclaredSend:
        """The flow's Oracle read asserted the Working row of the pending send: the declared send (6.6, 6.7)."""
        if self.pending is None:
            raise ValueError("no send token is pending; only a send the gate cleared can be declared")
        sent = DeclaredSend(run_id, shard_id, flow_id, step_id, row_key, sent_at, token_id, symbol, qty)
        self.declared.append(sent)
        self.pending = None
        return sent

    def void(self) -> None:
        """The pending token was dropped or expired unconsumed: release it; its count stays (choice 2)."""
        self.pending = None

    @property
    def declared_rows(self) -> frozenset[str]:
        """The declared sends' Working row keys, for the post-run proof's `declared_sends` (6.6)."""
        return frozenset(d.row_key for d in self.declared)
