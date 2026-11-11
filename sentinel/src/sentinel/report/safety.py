"""The Safety proof section of the merge and RC report (9.4 item 8, 6.6) and the run manifest's safety facts (6.3, 6.4).

Two functions:

- `run_manifest_safety(shard, ...)` is what one shard's attestation and paper-mode assertions put on the run manifest:
  "the result, the profile hash and the A11 hashes go on the run manifest and into the Safety proof section" (6.3
  Outcome) and "the asserted account_id and mode are written into the run manifest" (6.4 Schedule). It reads the
  shard's control results (`orchestrator.machine.RunReport.results`) and returns a `ShardSafety`: the eleven A1 to A11
  states, the attestation result and time, the settings profile sha256, the A1 to A10 evidence hashes, the asserted
  accounts (masked, allowlist entry, role, `sha256:` id) and mode, and the assertion counts. `ShardSafety.to_dict()` is
  the manifest and run-summary form; `ShardSafety.from_dict` reads it back.
- `render(rows, ...)` prints the section from the store rows (`model.SafetyRows`: the run's `post_run_proofs`, the last
  Nightly's fire drill and safety canary rows, open incidents, overdue postmortems), each shard's `ShardSafety` and the
  open safety quarantines: one 6.6 ORDER SAFETY PROOF block per proof, laid out as the 6.6 sample is (header,
  identity, attestation, send policy, gateway, the start/end/new/expected table, net liq, cancel all, VERDICT); the A1
  to A11 results with their evidence hashes (12.6); the previous night's fire drill and safety canaries in the three
  forms of 9.4 item 8 (pass; `harness_error (reason: drill_failed:<item>)` with the quarantined flows; `aborted_safety
  (reason: drill_order_placed)` with the blocked login); the safety canary banner, open incidents and overdue
  postmortems; and on an RC run the 6.13 known residuals for the sign-off (`RESIDUALS`).

Choices where the spec is ambiguous (reported):

1. The A1 to A11 states use the vocabulary of `attestation.AttestationResult.rows` (`match`, `mismatch`, `unreadable`,
   `not_read`). A1 comes from the paper-mode results (a mismatch wins, then an unreadable result, then a pass); A2 is a
   match once the attestation was reached (it runs only after A1 and A2 passed) unless the explicit fixture-account
   selection failed. A3 to A11 come from the last attestation result: its `evidence["rows"]` when the control records
   them, else derived from the result. When a mismatch stopped the attestation and no rows were recorded, the A3 to
   A8 rows it does not name read `unknown` (read, state not recorded). A10 is `not_read` outside RC runs (6.3: A10 is
   for RC runs), and the attestation line then says "A1 to A9 and A11".
2. The identity line names every allowlist entry the shard asserted (a shard serving GOLDEN and EMPTY work items
   asserts two), each masked as the proof prints it (`killswitch.masked_account`) with its fixture role in capitals as
   in the sample ("role EMPTY"); the counts come from the proof row, which is the stored truth.
3. The gateway line's "allowed" count is the proof row's events less its drops and refusals (`gateway_events` counts
   every event the gateway decided, `pipeline.run.write_proofs`); "(drill flows excluded)" is printed as 6.6 writes
   it, since drill-tagged drops are matched by the drill and never counted against the run (6.5, 5.4.8).
4. "(control located)" after the Cancel All result is printed only when the shard recorded whether the teardown
   located the control (`ShardSafety.cancel_all_located`, `Rig.locate_cancel_all`); otherwise the line ends at the
   result.
5. The run-time header prints the run's start to the minute and the attestation time to the second, both in UTC
   with a `Z`, as the sample does; the profile sha256 is shortened to four hex digits there and printed in full in
   the attestation table.
6. Declared sends (gated branch) and a hand clearance are printed under the VERDICT line, indented to its value
   column ("each send listed with its Working and Canceled rows", 6.6).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ..canary import CanaryResult
from ..orchestrator.attestation import SAFETY_ROWS
from ..orchestrator.identity import PAPER_MODE, Allowlist
from ..orchestrator.killswitch import (CANCEL_ALL_CODES, CANCEL_ALL_TEXT, NET_LIQ_TOLERANCE, masked_account, proof_line,
                                       send_lines)
from ..orchestrator.results import ControlResult
from ..store.safety import SafetyQuarantine
from ..store.store import PostRunProof
from .inline import plural, words_and
from .model import SafetyRows, _incident_text, _overdue_text

ITEMS = tuple(f"A{n}" for n in range(1, 12))
# 6.3 / 12.6 checklist names, as the attestation table prints them
ITEM_NAMES = {
    "A1": "Paper mode", "A2": "Fixture account", "A3": "Confirmation dialog enabled", "A4": "Shift+click send off",
    "A5": "Active Trader hot keys disabled", "A6": "No order hot keys bound",
    "A7": "Auto Send off on every Active Trader panel", "A8": "Working Orders empty", "A9": "Confirmation still required",
    "A10": "Explorer identity (RC runs)", "A11": "Evidence stored"}
EVIDENCE_ITEMS = tuple(f"A{n}" for n in range(1, 11))          # A11 is the check that these hashes exist
STATES = ("match", "mismatch", "unreadable", "not_read", "unknown")
RESULTS = ("passed", "failed", "unreadable", "not_reached")
# 8.3 post_run_proofs.verdict codes, least to most severe (6.6)
VERDICT_SEVERITY = {"proven_no_sends": 0, "proven_declared_sends": 0, "proven_no_session": 0, "unproven": 1,
                    "orders_sent": 2}
_CANCEL_ALL_SAID = {code: CANCEL_ALL_TEXT[k] for k, code in CANCEL_ALL_CODES.items()}
# the 6.6 table's row order; the stored counts_json keeps no order (8.3 canonical JSON)
TABLE_ROWS = ("working orders", "filled orders", "canceled orders", "position statement rows", "net liq",
              "buying power")
_LABEL_W, _NAME_W, _NUM_W, _NEW_W, _EXP_W = 15, 26, 12, 5, 10
# 6.13 Known residuals, accepted and listed in every RC sign-off
RESIDUALS = (
    "The 5.4.6 race between the flag re-check and delivery of a declared guarded key; bounded by A3, exercised by "
    "drill item 1.",
    "A9 and drill item 1 click `Confirm and Send` on a real ticket; a misread A3 could let that one click send a "
    "1-share paper order, bounded by A1, the drill ticket's construction (item 6) and the recovery Cancel All.",
    "The `Cancel All` click is exercised only when a row exists; in the default branch it is located but never "
    "clicked.",
    "Every attestation and identity read is OCR or template based; a blind reader is caught by the canaries the next "
    "night, not at the moment of the run, which is why unreadable fails closed.",
    "Stage-2 redaction depends on OCR recall; the redaction canary measures it nightly with planted ids, and the "
    "anchored list is the primary control.",
)
_QUARANTINE_TARGET = {"order_stage_flows": "every flow with an order-stage step, on every rig",
                      "explorer_stage": "the Explorer stage", "report_publication": "report publication"}


class SafetyInputError(ValueError):
    """A manifest safety record that cannot be what a shard produced (6.3, 6.4)."""


# -- the run manifest's safety facts (6.3 Outcome, 6.4 Schedule) ----------------------------------------------------
@dataclass(frozen=True)
class AssertedAccount:
    """One allowlist entry a shard's paper-mode assertion passed on: masked as the proof prints it, its fixture role
    and the 4.13 `sha256:` id of the selector text (6.4); the id itself is never stored."""

    entry: int | str
    account: str                       # masked, `D-xxxxxxxx`; empty when the allowlist was not given
    role: str | None                   # golden | empty (4.9)
    account_id_sha256: str             # `sha256:<hex>` (4.13)

    def label(self) -> str:
        """`D-xxxxxxxx (allowlist entry 2, role EMPTY)` (6.6 identity line)."""
        inside = f"allowlist entry {self.entry}" + (f", role {self.role.upper()}" if self.role else "")
        return f"{self.account} ({inside})" if self.account else f"({inside})"


@dataclass(frozen=True)
class ShardSafety:
    """The run manifest's safety facts of one shard (module docstring)."""

    shard_id: str | None
    run_type: str
    attestation: str                                   # passed | failed | unreadable | not_reached
    rows: Mapping[str, str]                            # A1 to A11 -> a `STATES` value
    evidence: Mapping[str, str | None]                 # A1 to A10 (and A9_second) -> screenshot sha256
    profile_sha256: str | None = None
    attested_at: str | None = None
    accounts: tuple[AssertedAccount, ...] = ()
    mode: str | None = None                            # paperMoney once an assertion passed (6.4)
    assertions: int = 0
    mismatches: int = 0
    unreadable: int = 0
    rig_id: str | None = None
    cancel_all_located: bool | None = None             # Rig.locate_cancel_all at teardown, when it was read

    def __post_init__(self) -> None:
        if self.attestation not in RESULTS:
            raise SafetyInputError(f"attestation result {self.attestation!r}; one of {RESULTS}")
        if tuple(self.rows) != ITEMS:
            raise SafetyInputError(f"attestation rows {sorted(self.rows)}: the manifest carries A1 to A11 in order (6.3)")
        bad = {k: v for k, v in self.rows.items() if v not in STATES}
        if bad:
            raise SafetyInputError(f"attestation row states {bad}; each one of {STATES}")
        if any(k not in (*EVIDENCE_ITEMS, "A9_second") for k in self.evidence):
            raise SafetyInputError(f"evidence keys {sorted(self.evidence)}: A1 to A10 and A9_second (6.3 A11)")

    @property
    def required_evidence(self) -> tuple[str, ...]:
        """The items whose screenshot hash A11 requires: A1 to A9, A9's second check when recorded, A10 on RC runs."""
        need = [i for i in EVIDENCE_ITEMS if i != "A10" or self.run_type == "rc"]
        return tuple(need + (["A9_second"] if "A9_second" in self.evidence else []))

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return tuple(i for i in self.required_evidence if not self.evidence.get(i))

    def to_dict(self) -> dict[str, Any]:
        """The manifest and run-summary form."""
        return {"shard_id": self.shard_id, "run_type": self.run_type, "rig_id": self.rig_id,
                "attestation": {"result": self.attestation, "at": self.attested_at, "rows": dict(self.rows),
                                "profile_sha256": self.profile_sha256, "evidence_sha256": dict(self.evidence)},
                "identity": {"mode": self.mode, "assertions": self.assertions, "mismatches": self.mismatches,
                             "unreadable": self.unreadable,
                             "accounts": [{"entry": a.entry, "account": a.account, "role": a.role,
                                           "account_id_sha256": a.account_id_sha256} for a in self.accounts]},
                "cancel_all_located": self.cancel_all_located}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ShardSafety":
        att, ident = d.get("attestation") or {}, d.get("identity") or {}
        return cls(d.get("shard_id"), str(d.get("run_type") or ""), str(att.get("result") or "not_reached"),
                   dict(att.get("rows") or {}), dict(att.get("evidence_sha256") or {}), att.get("profile_sha256"),
                   att.get("at"), tuple(AssertedAccount(a["entry"], a.get("account") or "", a.get("role"),
                                                        a["account_id_sha256"]) for a in ident.get("accounts") or ()),
                   ident.get("mode"), int(ident.get("assertions") or 0), int(ident.get("mismatches") or 0),
                   int(ident.get("unreadable") or 0), d.get("rig_id"), d.get("cancel_all_located"))


class ShardResults(Protocol):
    """What `run_manifest_safety` reads of a shard: `orchestrator.machine.RunReport` satisfies it."""

    run_type: str
    results: list[ControlResult]


def _sha(hex_or_tagged: str) -> str:
    return hex_or_tagged if hex_or_tagged.startswith("sha256:") else f"sha256:{hex_or_tagged}"


def _role(fixture: Mapping[str, Any] | None, account_id: str | None) -> str | None:
    for a in (fixture or {}).get("accounts") or ():
        if account_id is not None and str(a.get("account_id")) == account_id:
            return str(a.get("role")) if a.get("role") else None
    return None


def _accounts(passed: Sequence[ControlResult], allowlist: Allowlist | None,
              fixture: Mapping[str, Any] | None) -> tuple[AssertedAccount, ...]:
    out: dict[object, AssertedAccount] = {}
    for r in passed:
        entry, sha = r.evidence.get("allowlist_entry"), r.evidence.get("account_id_sha256")
        if entry is None or not sha or entry in out:
            continue
        account_id = allowlist.entries[entry - 1] if isinstance(entry, int) and allowlist is not None \
            and 0 < entry <= len(allowlist.entries) else None
        out[entry] = AssertedAccount(entry, masked_account(account_id) if account_id else "",
                                     _role(fixture, account_id), _sha(str(sha)))
    return tuple(out.values())


def _a1(pm: Sequence[ControlResult]) -> str:
    if any(r.run_status == "aborted_safety" for r in pm):
        return "mismatch"
    if any(not r.passed and r.unreached is not None and r.unreached.reason == "identity_unreadable" for r in pm):
        return "unreadable"
    return "match" if any(r.passed for r in pm) else "not_read"


def _named(detail: str | None) -> set[str]:
    """The A3 to A8 rows a failed attestation's detail names ("settings mismatch on A3, A5")."""
    return set(re.findall(r"\bA[3-8]\b", detail or ""))


def _attestation_rows(att: ControlResult | None, run_type: str) -> dict[str, str]:
    """A3 to A11 from the last attestation result (choice 1)."""
    rows = {i: "not_read" for i in ITEMS[2:]}
    if att is None:
        return rows
    recorded = att.evidence.get("rows")
    if isinstance(recorded, Mapping):
        rows.update({k: str(v) for k, v in recorded.items() if k in rows})
        return rows
    reason = att.run_reason or (att.unreached.reason if att.unreached is not None else None)
    safety = {i: "match" for i in SAFETY_ROWS}
    if att.passed:
        rows.update(safety, A9="match", A11="match")
        if run_type == "rc":
            stage = next((o for o in att.outcomes if o.selector == "explorer_stage"), None)
            rows["A10"] = "unreadable" if stage is not None else "mismatch" if att.alert is not None else "match"
    elif (att.detail or "").startswith("settings mismatch on"):
        named = _named(att.detail)
        rows.update({i: "mismatch" if i in named else "unknown" for i in SAFETY_ROWS})
    elif reason == "attestation_unreadable":
        named = _named(att.detail)
        rows.update({i: "unreadable" if i in named else "match" for i in SAFETY_ROWS})
    elif reason == "fixture_drift":
        rows.update(safety)
    elif (att.detail or "").startswith("A9"):
        rows.update(safety, A9="mismatch")
    elif att.run_status == "aborted_harness":                 # A11: evidence hashes missing
        rows.update(safety, A9="match", A11="mismatch")
    return rows


def _evidence(att: ControlResult | None, pm: Sequence[ControlResult]) -> dict[str, str | None]:
    ev: dict[str, str | None] = {}
    crops = [r.evidence.get("crop") for r in pm if r.passed and r.evidence.get("crop")]
    if crops:
        ev["A1"] = str(crops[-1])
    if att is not None:
        hashes = att.evidence.get("hashes")
        shots = att.evidence.get("screenshots")
        if isinstance(hashes, Mapping):
            ev.update({k: v for k, v in hashes.items() if k in (*EVIDENCE_ITEMS, "A9_second")})
        elif isinstance(shots, Mapping):
            for k, v in shots.items():
                first = next((x for x in v if x), None) if isinstance(v, (list, tuple)) else v
                if k in EVIDENCE_ITEMS:
                    ev[k] = first
        elif att.evidence.get("screenshot"):
            ev["A9"] = str(att.evidence["screenshot"])
    return ev


def run_manifest_safety(shard: ShardResults, *, shard_id: str | None, rig_id: str | None = None,
                        allowlist: Allowlist | None = None, fixture: Mapping[str, Any] | None = None,
                        attested_at: str | None = None, cancel_all_located: bool | None = None) -> ShardSafety:
    """The run manifest's safety facts of one shard (6.3 Outcome, 6.4 Schedule; module docstring). `allowlist` and
    `fixture` turn the asserted allowlist entries into the masked account and its role; `attested_at` is the time the
    `settings_attested` gate passed; `cancel_all_located` what `Rig.locate_cancel_all` read at teardown."""
    results = list(shard.results)
    pm = [r for r in results if r.control == "paper_mode"]
    atts = [r for r in results if r.control == "attestation" and r.gate == "settings_attested"]
    att = atts[-1] if atts else None
    a2_failed = any(r.control == "fixture_account" and not r.passed and r.run is not None for r in results)
    rows = {"A1": _a1(pm), "A2": "mismatch" if a2_failed else "match" if att is not None else "not_read",
            **_attestation_rows(att, shard.run_type)}
    if shard.run_type != "rc":
        rows["A10"] = "not_read"
    result = ("not_reached" if att is None else "passed" if att.passed else
              "unreadable" if (att.unreached is not None and att.unreached.reason == "attestation_unreadable")
              else "failed")
    evidence = _evidence(att, pm)
    passed = [r for r in pm if r.passed]
    return ShardSafety(
        shard_id, shard.run_type, result, {i: rows[i] for i in ITEMS}, evidence,
        profile_sha256=att.evidence.get("profile_sha256") if att is not None else None, attested_at=attested_at,
        accounts=_accounts(passed, allowlist, fixture), mode=PAPER_MODE if passed else None,
        assertions=len(passed), mismatches=sum(1 for r in pm if r.run_status == "aborted_safety"),
        unreadable=sum(1 for r in pm if not r.passed and r.unreached is not None
                       and r.unreached.reason == "identity_unreadable"),
        rig_id=rig_id, cancel_all_located=cancel_all_located)


# -- the Safety proof section (9.4 item 8, 6.6) ---------------------------------------------------------------------
@dataclass(frozen=True)
class ProofHeader:
    """The run facts the 6.6 block's first line prints: the run's start, the candidate build."""

    run_started: str | None = None
    build: str | None = None
    side: str = "candidate"


@dataclass(frozen=True)
class ProofBlock:
    """One ORDER SAFETY PROOF block: its title for the page, the monospace text of 6.6 and its verdict's severity."""

    title: str
    text: str
    severity: int


@dataclass(frozen=True)
class AttestationRow:
    """One row of the A1 to A11 table (9.4 item 8, 12.6) of one shard."""

    shard_id: str | None
    item: str
    check: str
    state: str
    evidence_sha256: str | None


@dataclass(frozen=True)
class SafetySection:
    """Section 8 of the merge and RC report (9.4) as `render` builds it; html.py prints it."""

    blocks: tuple[ProofBlock, ...] = ()
    attestation: tuple[AttestationRow, ...] = ()
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()     # titled lines: drill, canaries, banner, incidents
    residuals: tuple[str, ...] = ()                          # 6.13, RC sign-off only
    profile_sha256: Mapping[str | None, str | None] = field(default_factory=dict)

    @property
    def worst(self) -> int | None:
        return max((b.severity for b in self.blocks), default=None)


def _utc(ts: str | None, fmt: str) -> str | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)
    t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    return t.strftime(fmt)


def _line(label: str, value: str) -> str:
    return f"{label:<{_LABEL_W}}{value}".rstrip()


def _cell(x: Any, width: int) -> str:
    return f"{x:<{width}}"


def _n(x: Any) -> str:
    return "not read" if x is None else f"{float(x):g}"


def _money(x: Any) -> str:
    return "not read" if x is None else f"{float(x):,.2f}"


def _header(p: PostRunProof, s: ShardSafety | None, h: ProofHeader) -> str:
    parts = []
    run = _utc(h.run_started, "%Y-%m-%dT%H:%MZ")
    if run:
        parts.append(f"run {run}")
    parts.append(f"rig {s.rig_id}" if s is not None and s.rig_id else
                 "run level (no shard started)" if p.shard_id is None else f"shard {p.shard_id}")
    if h.build:
        parts.append(f"build {h.build} ({h.side})")
    return f"{'ORDER SAFETY PROOF':<{_NAME_W}}" + "  ".join(parts)


def _identity(p: PostRunProof, s: ShardSafety | None) -> list[str]:
    accounts = s.accounts if s is not None else ()
    if accounts:
        who = ("account " if len(accounts) == 1 else "accounts ") + words_and([a.label() for a in accounts])
    elif p.verdict == "proven_no_session":
        who = "no session established"
    else:
        who = "account not recorded on the run manifest"
    mode = f", {s.mode}," if s is not None and s.mode else ","
    return [_line("identity", who + mode),
            _line("", f"asserted {plural(p.paper_mode_assertions, 'time')}, {p.paper_mode_mismatches} mismatches, "
                      f"{p.unreadable_count} unreadable")]


def _attestation_line(p: PostRunProof, s: ShardSafety | None) -> str:
    if s is None:
        return _line("attestation", f"recorded as {p.attestation_ref}" if p.attestation_ref else "not on the run manifest")
    if s.attestation == "not_reached":
        return _line("attestation", "not reached")
    at = _utc(s.attested_at, "%H:%M:%SZ")
    span = "A1 to A11" if s.run_type == "rc" else "A1 to A9 and A11"
    if s.attestation == "passed":
        text = f"{span} passed" + (f" {at}" if at else "")
    else:
        bad = [f"{i} {st}" for i, st in s.rows.items() if st in ("mismatch", "unreadable")]
        text = f"{s.attestation}: " + (", ".join(bad) or "no row named") + (f" at {at}" if at else "")
    if s.profile_sha256:
        text += f", profile sha256 {s.profile_sha256[:4]}..."
    missing = s.missing_evidence
    text += ", evidence hashes stored" if not missing else f", evidence hashes missing for {', '.join(missing)}"
    return _line("attestation", text)


def _send_policy_line(p: PostRunProof, send_policy: str | None, not_run: int | None) -> str:
    default = send_policy in ("no_sends", "default") or (send_policy is None and p.send_policy_ref == "OPEN")
    text = "default (no sends)" if default else f"gated (decision {p.send_policy_ref})"
    if default and not_run is not None:
        text += f"; {plural(not_run, 'order-lifecycle surface')} not run (entitlement)"
    return _line("send policy", text)


def _gateway_line(p: PostRunProof) -> str:
    allowed = max(0, p.gateway_events - p.gateway_drops - p.refusals_count)
    return _line("gateway", f"{allowed:,} events allowed, {plural(p.gateway_drops, 'drop')}, "
                            f"{plural(p.refusals_count, 'refusal')} (drill flows excluded)")


def _table(p: PostRunProof) -> list[str]:
    stored = p.counts.get("rows") or {}
    rows = sorted(stored.items(), key=lambda kv: (TABLE_ROWS.index(kv[0]) if kv[0] in TABLE_ROWS else len(TABLE_ROWS),
                                                  kv[0]))
    if not rows:
        return []
    out = [" " * _NAME_W + _cell("start", _NUM_W) + _cell("end", _NUM_W) + _cell("new", _NEW_W)
           + _cell("expected", _EXP_W) + "verdict"]
    for name, r in rows:
        if "drift" in r:
            if r.get("drift") is None:
                tail = "drift not read"
            else:
                word = "within" if r.get("verdict") == "ok" else "over"
                tail = f"drift {_money(r['drift'])} {word} {NET_LIQ_TOLERANCE:.2f}"
            out.append(_cell(name, _NAME_W) + _cell(_money(r.get("start")), _NUM_W)
                       + _cell(_money(r.get("end")), _NUM_W) + tail)
        else:
            out.append(_cell(name, _NAME_W) + _cell(_n(r.get("start")), _NUM_W) + _cell(_n(r.get("end")), _NUM_W)
                       + _cell(r.get("new", 0), _NEW_W) + _cell(r.get("expected", 0), _EXP_W)
                       + str(r.get("verdict", "unknown")))
    return out


def _cancel_line(p: PostRunProof, s: ShardSafety | None) -> str:
    text = _CANCEL_ALL_SAID[p.cancel_all]
    located = s.cancel_all_located if s is not None else None
    if located is not None:
        text += " (control located)" if located else " (control not located)"
    return _cell("cancel all", _NAME_W) + text


def proof_block(p: PostRunProof, shard: ShardSafety | None = None, header: ProofHeader = ProofHeader(), *,
                send_policy: str | None = None, order_surfaces_not_run: int | None = None) -> ProofBlock:
    """One `post_run_proofs` row as the 6.6 block prints it, with its shard's manifest facts; the login alias is never
    printed (8.3). `send_policy` is the run's (`no_sends` or `default`, `gated`) and `order_surfaces_not_run` the B01
    count of the send-policy line (9.2, 6.7)."""
    lines = [_header(p, shard, header), *_identity(p, shard), _attestation_line(p, shard),
             _send_policy_line(p, send_policy, order_surfaces_not_run), _gateway_line(p), *_table(p),
             _cancel_line(p, shard),
             _line("VERDICT", proof_line(p.verdict, p.reason, p.counts.get("undeclared") or [], p.cancel_all))]
    lines += [_line("", x) for x in send_lines(p.counts)]
    if p.verified_by:
        lines.append(_line("", f"verified by hand by {p.verified_by} at {p.verified_at}"))
    title = f"ORDER SAFETY PROOF, shard {p.shard_id}" if p.shard_id else "ORDER SAFETY PROOF, run level (no shard started)"
    return ProofBlock(title, "\n".join(lines), VERDICT_SEVERITY[p.verdict])


def _quarantine_text(q: SafetyQuarantine) -> str:
    what = (_QUARANTINE_TARGET.get(q.target, q.target) if q.target_kind == "activity" else
            f"every flow on rig {q.target}" if q.target_kind == "rig" else f"{q.target_kind} {q.target}")
    return f"{what} ({q.reason}: {q.cause})"


def _row_text(r: CanaryResult) -> str:
    text = f"{r.mutant_id}: {r.state}"
    if r.produced_outcome:
        text += f", {r.produced_outcome}" + (f" (reason: {r.produced_reason})" if r.produced_reason else "")
    if r.absorbed_by:
        text += f": {r.absorbed_by}"
    return text


def drill_summary(rows: Sequence[CanaryResult], *, quarantines: Sequence[SafetyQuarantine] = (),
                  incidents: Sequence[Any] = (), nightly_run_id: str | None = None) -> str:
    """The previous night's fire drill in the three forms of 9.4 item 8: pass; `harness_error (reason:
    drill_failed:<item>)` with the quarantined flows; `aborted_safety (reason: drill_order_placed)` with the blocked
    login (6.5)."""
    if not rows:
        return "no fire drill result recorded"
    if any(r.produced_reason == "drill_order_placed" for r in rows):
        rigs = sorted({i.rig_id for i in incidents if i.rig_id and "drill_order_placed" in i.trigger
                       and (nightly_run_id is None or i.run_id == nightly_run_id)})
        who = (("the login of rig " if len(rigs) == 1 else "the logins of rigs ") + words_and(rigs)) if rigs \
            else "the drill rig's login"
        return f"aborted_safety (reason: drill_order_placed); {who} blocked until a human verifies the account"
    failed = [r for r in rows if r.state != "detected"]
    if not failed:
        return f"pass: {len(rows)} of {len(rows)} items blocked"
    reasons = list(dict.fromkeys(r.produced_reason for r in failed if r.produced_reason))
    held = [q for q in quarantines if q.reason == "fire_drill"]
    text = "harness_error (reason: " + ", ".join(reasons or ["drill_failed"]) + ")"
    text += "; quarantined: " + ("; ".join(_quarantine_text(q) for q in held) if held else "none recorded")
    return text


def canary_summary(rows: Sequence[CanaryResult], *, quarantines: Sequence[SafetyQuarantine] = ()) -> str:
    """The previous night's safety canaries (6.5, 10.3): pass, or each blind detector with what is quarantined."""
    if not rows:
        return "no safety canary result recorded"
    failed = [r for r in rows if r.state != "detected"]
    if not failed:
        return f"pass: {len(rows)} of {len(rows)} detected"
    held = [q for q in quarantines if q.reason == "safety_canary"]
    text = "; ".join(f"{r.mutant_id} {r.state}: {r.produced_outcome} (reason: {r.produced_reason})" for r in failed)
    return text + "; quarantined: " + ("; ".join(_quarantine_text(q) for q in held) if held else "none recorded")


def _shard_map(shards: Mapping[str | None, ShardSafety] | Sequence[ShardSafety]) -> dict[str | None, ShardSafety]:
    return dict(shards) if isinstance(shards, Mapping) else {s.shard_id: s for s in shards}


def render(rows: SafetyRows, *, shards: Mapping[str | None, ShardSafety] | Sequence[ShardSafety] = (),
           header: ProofHeader = ProofHeader(), send_policy: str | None = None,
           order_surfaces_not_run: int | None = None, quarantines: Sequence[SafetyQuarantine] = (),
           banner: str | None = None, run_type: str = "pr") -> SafetySection:
    """Section 8 of the merge and RC report (9.4) for one run (module docstring). `shards` are the run manifest's
    `ShardSafety` records; `quarantines` the open safety quarantines (`store.safety.open_quarantines`); `banner` the
    safety canary banner of the run summary; an `rc` run adds the 6.13 residuals."""
    by_shard = _shard_map(shards)
    blocks = tuple(proof_block(p, by_shard.get(p.shard_id), header, send_policy=send_policy,
                               order_surfaces_not_run=order_surfaces_not_run) for p in rows.proofs)
    table = tuple(AttestationRow(s.shard_id, i, ITEM_NAMES[i], s.rows[i], s.evidence.get(i) if i != "A11" else None)
                  for s in by_shard.values() for i in ITEMS)
    night = f" (Nightly run {rows.nightly_run_id})" if rows.nightly_run_id else ""
    groups: list[tuple[str, tuple[str, ...]]] = []
    drill = [r for r in rows.canaries if r.kind == "fire_drill"]
    canaries = [r for r in rows.canaries if r.kind == "safety_canary"]
    if rows.nightly_run_id or drill:
        groups.append(("Fire drill" + night, (drill_summary(drill, quarantines=quarantines, incidents=rows.incidents,
                                                            nightly_run_id=rows.nightly_run_id),
                                              *map(_row_text, drill))))
    if rows.nightly_run_id or canaries:
        groups.append(("Safety canaries" + night, (canary_summary(canaries, quarantines=quarantines),
                                                   *map(_row_text, canaries))))
    if banner:
        groups.append(("Safety canary banner", tuple(str(banner).splitlines())))
    if rows.incidents:
        groups.append(("Open incidents", tuple(f"{_incident_text(i)}, opened {i.opened_at}"
                                               + (f", postmortem due {i.postmortem_due_at}" if i.postmortem_due_at
                                                  else "") for i in rows.incidents)))
    if rows.overdue:
        groups.append(("Postmortems overdue", tuple(_overdue_text(i) + ", no postmortem linked" for i in rows.overdue)))
    return SafetySection(blocks, table, tuple(groups), RESIDUALS if run_type == "rc" else (),
                         {s.shard_id: s.profile_sha256 for s in by_shard.values()})
