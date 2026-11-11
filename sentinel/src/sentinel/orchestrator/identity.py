"""Paper-mode assertion and test identity (6.4; attestation rows A1 and A2 of 6.3; 7.7 step 7).

The selector text must equal one entry of `gateway/allowlist.yaml` for this rig exactly: no prefix,
pattern or case folding (L-FIX-01). The allowlist's sha256 is the `allowlist_sha256` of the
fingerprint's `harness` field (7.4).

- A readable text that is an entry: pass.
- The OnDemand virtual account (frozen Branch B, 7.9; `ondemand_virtual_account` of 4.9) is an entry only while the
  OnDemand toolbar is visible by template (`ondemand_toolbar`, matched with the file's `valid_while_template`); the
  same id shown without the toolbar is not an entry and takes the abort below (`aborted_safety`).
- A readable text that is not an entry: deny-all, the tree killed, run `aborted_safety` and every
  unreached checkpoint `env_error (gate: paper_mode_asserted, reason: <the abort reason>)`; the reason
  is `live_identity` when the text matches the live id pattern and not the paper prefix (the login is
  also blocked, S1), else `paper_mode_mismatch` (S2). Paged by the safety routing of 9.10.
- Unreadable (landmark missing or OCR below the floor) on two reads 1 s apart: deny-all from the first
  failed read, the tree killed, `env_error (gate: paper_mode_asserted, reason: identity_unreadable)` on
  every scoped checkpoint, run `complete_with_env_errors`, P2 to the on-call channel and the login owner
  (P1 on repeat); the second consecutive unreadable result on one login blocks it and opens a
  maintenance task for the LOGIN flow. A timer read deferred past five minutes takes the same path
  (`deferral_unreadable`, 6.4 Schedule). An unreadable identity is an S3 incident (6.12: "attestation or identity
  unreadable").
- The paper-asserted stamp (6.4 Schedule, 4.13 `paper_assertion`): `{account_id_sha256, mode, asserted_at}` of
  the latest passing assertion; a capture is released only under a stamp from the current session
  (`stamp_current`).

Choices (reported): the live id pattern is the 6.13 item 5 assumption, eight digits then `SCHW`,
configurable; the OCR floor is pinned at calibration (default 90); a readable second read after an
unreadable first one is judged on its own and lifts the deny-all (`rearm`) when it passes, since identity
is then known; A2's "still different" failure is `env_error (gate: fixture_verified, reason: gate)` on every
scoped checkpoint of the shard (the gate's own check, no finer code). The stamp's `account_id_sha256` is the
4.13 `sha256` notation (`sha256:` plus the hex digest of the selector text, the 4.13 worked example) and its
`mode` is `paperMoney`, the only mode an allowlisted identity has; the control results keep the bare hex digest
in their evidence as before. A stamp is current when it was asserted at or after the session's login.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import alerts
from .results import ControlResult, Outcome, RunVerdict, safety_abort_outcome
from .rig import SelectorRead

GATE = "paper_mode_asserted"
LIVE_ID_RE = re.compile(r"^[0-9]{8}SCHW$")      # 6.13 item 5 (secondary sources)
PAPER_PREFIX = "D-"                             # 6.13 item 5; a format expectation, never the test
OCR_FLOOR = 90.0
READ_GAP_S = 1.0                                # two reads 1 s apart
TIMER_S = 120.0                                 # the two-minute timer
DEFERRAL_MAX_S = 300.0                          # a deferral past five minutes takes the unreadable path
SEND_FRAME_MAX_S = 0.5                          # a frame no older than 500 ms before a paper_send_ok send
PAPER_MODE = "paperMoney"                       # the stamp's mode (4.13 worked example)
ONDEMAND_MODE = "onDemand"                       # the stamp's mode for the allowlisted OnDemand account (7.9, CD)
STAMP_MODES = (PAPER_MODE, ONDEMAND_MODE)
ONDEMAND_ENTRY = "ondemand_virtual_account"     # the proof's allowlist entry for the OnDemand account (7.9)


@dataclass(frozen=True)
class Allowlist:
    """The exact account ids of one rig from `gateway/allowlist.yaml`, and the file's sha256 (6.4, 7.4); with frozen
    Branch B, the OnDemand virtual account id and the toolbar template that makes it an entry (7.9)."""

    rig: str
    entries: tuple[str, ...]
    sha256: str
    ondemand_account: str | None = None
    ondemand_template: str | None = None

    def admits(self, text: object, *, ondemand_toolbar: bool = False) -> bool:
        """True when `text` is an entry: one of the rig's ids, or the OnDemand virtual account while the toolbar is
        visible (7.9)."""
        return text in self or (ondemand_toolbar and self.ondemand_account is not None
                                and text == self.ondemand_account)

    def __contains__(self, text: object) -> bool:
        return isinstance(text, str) and text in self.entries

    def index(self, text: str) -> int:
        """1-based allowlist entry number for the proof's identity line (6.6)."""
        return self.entries.index(text) + 1


def load_allowlist(path: str | Path, rig: str) -> Allowlist:
    """Read `gateway/allowlist.yaml` (4.9 allowlist schema) and return the entries of `rig`."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw) or {}
    od = data.get("ondemand_virtual_account") or {}
    for r in data.get("rigs", []):
        if r.get("rig") == rig:
            ids = tuple(str(a) for a in r.get("account_ids", []))
            if not ids:
                raise ValueError(f"rig {rig} has no account ids in {path}")
            account, template = od.get("account_id"), od.get("valid_while_template")
            return Allowlist(rig, ids, hashlib.sha256(raw).hexdigest(),
                             None if account is None else str(account), None if template is None else str(template))
    raise ValueError(f"rig {rig} is not in {path}")


def readable(r: SelectorRead, floor: float = OCR_FLOOR) -> bool:
    """6.4: the selector landmark found and OCR confidence at or above the floor, with some text."""
    return r.landmark_found and r.confidence >= floor and bool(r.text)


def classify(text: str, allowlist: Allowlist, live_re: re.Pattern[str] = LIVE_ID_RE, *,
             ondemand_toolbar: bool = False) -> str:
    """`allowlisted`, `live_identity` or `paper_mode_mismatch` for a readable selector text (6.4); the OnDemand virtual
    account is allowlisted only with the toolbar visible (7.9)."""
    if allowlist.admits(text, ondemand_toolbar=ondemand_toolbar):
        return "allowlisted"
    if live_re.match(text) and not text.startswith(PAPER_PREFIX):
        return "live_identity"
    return "paper_mode_mismatch"


def judge_identity(reads: Sequence[SelectorRead], allowlist: Allowlist, *, floor: float = OCR_FLOOR,
                   unreadable_streak: int = 0, live_re: re.Pattern[str] = LIVE_ID_RE,
                   ondemand_toolbar: bool = False) -> ControlResult:
    """The paper-mode assertion over one read, or two reads 1 s apart when the first was unreadable (6.4).

    `unreadable_streak` is the number of consecutive unreadable results on this login before this one;
    `ondemand_toolbar` is the OnDemand toolbar seen by template in the same frame (7.9).
    """
    if not reads:
        raise ValueError("at least one selector read")
    first = reads[0]
    if readable(first, floor):
        return _judge(first, allowlist, live_re, rearm=False, ondemand_toolbar=ondemand_toolbar)
    if len(reads) < 2:
        raise ValueError("the first read is unreadable: deny-all and read again 1 s later (6.4)")
    second = reads[1]
    if readable(second, floor):
        return _judge(second, allowlist, live_re, rearm=True, ondemand_toolbar=ondemand_toolbar)
    return _unreadable("selector unreadable on two reads 1 s apart", [r.crop_sha256 for r in reads[:2]],
                       unreadable_streak)


def deferral_unreadable(*, deferred_s: float, unreadable_streak: int = 0) -> ControlResult:
    """A timer assertion deferred (main frame not foreground, or a modal up) past five minutes takes the unreadable
    path (6.4 Schedule): deny-all, the tree killed, `env_error (gate: paper_mode_asserted, reason:
    identity_unreadable)`, run `complete_with_env_errors`."""
    return _unreadable(f"paper-mode assertion deferred {deferred_s:.0f} s, past five minutes", [], unreadable_streak)


def _unreadable(detail: str, crops: list[str | None], unreadable_streak: int) -> ControlResult:
    blocked = unreadable_streak + 1 >= 2
    acts = ("deny_all", "kill_tree") + (("block_login", "maintenance_task") if blocked else ())
    return ControlResult(
        "paper_mode", False, GATE, run=RunVerdict("complete_with_env_errors"),
        unreached=Outcome("env_error", "identity_unreadable", GATE),
        alert=alerts.env_error(repeat=unreadable_streak >= 1, notified=("login_owner",)), actions=acts, incident="S3",
        detail=detail + ("; login blocked" if blocked else ""), evidence={"crops": crops})


def _judge(r: SelectorRead, allowlist: Allowlist, live_re: re.Pattern[str], *, rearm: bool,
           ondemand_toolbar: bool = False) -> ControlResult:
    text = r.text or ""
    ondemand = text not in allowlist and allowlist.admits(text, ondemand_toolbar=ondemand_toolbar)
    verdict = "allowlisted" if ondemand else classify(text, allowlist, live_re)
    sha = hashlib.sha256(text.encode()).hexdigest()
    if verdict == "allowlisted":
        entry = ONDEMAND_ENTRY if ondemand else allowlist.index(text)
        return ControlResult("paper_mode", True, GATE, actions=("rearm",) if rearm else (),
                             evidence={"account_id_sha256": sha, "allowlist_entry": entry, "crop": r.crop_sha256})
    run, un = safety_abort_outcome(GATE, verdict)
    live = verdict == "live_identity"
    return ControlResult("paper_mode", False, GATE, run=run, unreached=un, alert=alerts.safety_abort(),
                         actions=("deny_all", "kill_tree") + (("block_login",) if live else ()),
                         incident="S1" if live else "S2", detail="selector text is not an allowlist entry",
                         evidence={"account_id_sha256": sha, "crop": r.crop_sha256})


def judge_fixture_account(selected: str, expected: str, *, after_select: str | None = None) -> ControlResult:
    """A2: the selected account is the pinned fixture account for the work item's role (6.3, 7.7 step 7).

    Different: select it explicitly once (`select_fixture_account`) and re-read; still different:
    `env_error (gate: fixture_verified, reason: gate)` (choice, module docstring).
    """
    if selected == expected:
        return ControlResult("fixture_account", True, "fixture_verified")
    if after_select is None:
        return ControlResult("fixture_account", False, "fixture_verified", actions=("select_fixture_account",),
                             detail="selected account differs from the role's fixture account; select it once")
    if after_select == expected:
        return ControlResult("fixture_account", True, "fixture_verified", detail="fixture account selected explicitly")
    return ControlResult("fixture_account", False, "fixture_verified", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "gate", "fixture_verified"), alert=alerts.env_error(),
                         detail="fixture account still not selected after one explicit selection")


def assertion_due(now: float, last_asserted: float, *, boundary: bool = False, main_foreground: bool = True,
                  modal_up: bool = False) -> str | None:
    """When the next assertion is taken (6.4 Schedule).

    `boundary` is a flow boundary, a re-login or session dialog, or an order-stage step: due `now`. On the
    two-minute timer the read needs the main frame foreground and no modal, else it is `defer`red to the
    next boundary; a deferral past five minutes is `unreadable` (the unreadable path). None: not due.
    """
    if boundary:
        return "now"
    age = now - last_asserted
    if age < TIMER_S:
        return None
    if main_foreground and not modal_up:
        return "now"
    return "unreadable" if age - TIMER_S > DEFERRAL_MAX_S else "defer"


def send_frame_fresh(frame_mono_ts: float, now: float) -> bool:
    """A `paper_send_ok` send needs a paper assertion in a frame no older than 500 ms (6.4, 6.7)."""
    return 0 <= now - frame_mono_ts <= SEND_FRAME_MAX_S


# -- the paper-asserted stamp (6.4 Schedule, 4.13 paper_assertion) --------------------------------------------
def account_id_sha256(text: str) -> str:
    """The 4.13 `sha256` notation of an account id: `sha256:` plus the hex digest of its UTF-8 text."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def paper_stamp(account_id: str, asserted_at: datetime, *, mode: str = PAPER_MODE) -> dict[str, str]:
    """The stamp `{account_id_sha256, mode, asserted_at}` of a passing assertion of `account_id` (6.4); its shape is
    the one `records.CheckpointOutcome.paper_assertion` accepts. `mode` is `onDemand` for the allowlisted OnDemand
    virtual account while its toolbar shows (7.9: paperMoney is not the true mode there; coordinator decision)."""
    if asserted_at.tzinfo is None:
        raise ValueError("asserted_at must be an aware time (4.13 ts)")
    if mode not in STAMP_MODES:
        raise ValueError(f"stamp mode {mode!r}; one of {STAMP_MODES}")
    return {"account_id_sha256": account_id_sha256(account_id), "mode": mode,
            "asserted_at": asserted_at.isoformat()}


def stamp_current(stamp: Mapping[str, str] | None, session_started_at: datetime | None) -> bool:
    """True when `stamp` is a paper-asserted stamp of the session that logged in at `session_started_at`: Capture
    releases no frame to the Reviewer, the agent or a report without one (6.4 Schedule)."""
    if not stamp or session_started_at is None or stamp.get("mode") not in STAMP_MODES:
        return False
    try:
        asserted = datetime.fromisoformat(stamp["asserted_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return asserted.tzinfo is not None and asserted >= session_started_at
