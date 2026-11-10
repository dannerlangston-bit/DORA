"""Paper-mode assertion and test identity (6.4; attestation rows A1 and A2 of 6.3; 7.7 step 7).

The selector text must equal one entry of `gateway/allowlist.yaml` for this rig exactly: no prefix,
pattern or case folding (L-FIX-01). The allowlist's sha256 is the `allowlist_sha256` of the
fingerprint's `harness` field (7.4).

- A readable text that is an entry: pass.
- A readable text that is not an entry: deny-all, the tree killed, run `aborted_safety` and every
  unreached checkpoint `env_error (gate: paper_mode_asserted, reason: <the abort reason>)`; the reason
  is `live_identity` when the text matches the live id pattern and not the paper prefix (the login is
  also blocked, S1), else `paper_mode_mismatch` (S2). Paged by the safety routing of 9.10.
- Unreadable (landmark missing or OCR below the floor) on two reads 1 s apart: deny-all from the first
  failed read, the tree killed, `env_error (gate: paper_mode_asserted, reason: identity_unreadable)` on
  every scoped checkpoint, run `complete_with_env_errors`, P2 to the on-call channel and the login owner
  (P1 on repeat); the second consecutive unreadable result on one login blocks it and opens a
  maintenance task for the LOGIN flow.

Choices (reported): the live id pattern is the 6.13 item 5 assumption, eight digits then `SCHW`,
configurable; the OCR floor is pinned at calibration (default 90); a readable second read after an
unreadable first one is judged on its own and lifts the deny-all (`rearm`) when it passes, since identity
is then known; A2's "still different" failure is `env_error (gate: fixture_verified, reason: gate)` on every
scoped checkpoint of the shard (the gate's own check, no finer code).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Allowlist:
    """The exact account ids of one rig from `gateway/allowlist.yaml`, and the file's sha256 (6.4, 7.4)."""

    rig: str
    entries: tuple[str, ...]
    sha256: str

    def __contains__(self, text: object) -> bool:
        return isinstance(text, str) and text in self.entries

    def index(self, text: str) -> int:
        """1-based allowlist entry number for the proof's identity line (6.6)."""
        return self.entries.index(text) + 1


def load_allowlist(path: str | Path, rig: str) -> Allowlist:
    """Read `gateway/allowlist.yaml` (4.9 allowlist schema) and return the entries of `rig`."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw) or {}
    for r in data.get("rigs", []):
        if r.get("rig") == rig:
            ids = tuple(str(a) for a in r.get("account_ids", []))
            if not ids:
                raise ValueError(f"rig {rig} has no account ids in {path}")
            return Allowlist(rig, ids, hashlib.sha256(raw).hexdigest())
    raise ValueError(f"rig {rig} is not in {path}")


def readable(r: SelectorRead, floor: float = OCR_FLOOR) -> bool:
    """6.4: the selector landmark found and OCR confidence at or above the floor, with some text."""
    return r.landmark_found and r.confidence >= floor and bool(r.text)


def classify(text: str, allowlist: Allowlist, live_re: re.Pattern[str] = LIVE_ID_RE) -> str:
    """`allowlisted`, `live_identity` or `paper_mode_mismatch` for a readable selector text (6.4)."""
    if text in allowlist:
        return "allowlisted"
    if live_re.match(text) and not text.startswith(PAPER_PREFIX):
        return "live_identity"
    return "paper_mode_mismatch"


def judge_identity(reads: Sequence[SelectorRead], allowlist: Allowlist, *, floor: float = OCR_FLOOR,
                   unreadable_streak: int = 0, live_re: re.Pattern[str] = LIVE_ID_RE) -> ControlResult:
    """The paper-mode assertion over one read, or two reads 1 s apart when the first was unreadable (6.4).

    `unreadable_streak` is the number of consecutive unreadable results on this login before this one.
    """
    if not reads:
        raise ValueError("at least one selector read")
    first = reads[0]
    if readable(first, floor):
        return _judge(first, allowlist, live_re, rearm=False)
    if len(reads) < 2:
        raise ValueError("the first read is unreadable: deny-all and read again 1 s later (6.4)")
    second = reads[1]
    if readable(second, floor):
        return _judge(second, allowlist, live_re, rearm=True)
    blocked = unreadable_streak + 1 >= 2
    acts = ("deny_all", "kill_tree") + (("block_login", "maintenance_task") if blocked else ())
    return ControlResult(
        "paper_mode", False, GATE, run=RunVerdict("complete_with_env_errors"),
        unreached=Outcome("env_error", "identity_unreadable", GATE),
        alert=alerts.env_error(repeat=unreadable_streak >= 1, notified=("login_owner",)), actions=acts,
        detail="selector unreadable on two reads 1 s apart" + ("; login blocked" if blocked else ""),
        evidence={"crops": [r.crop_sha256 for r in reads[:2]]})


def _judge(r: SelectorRead, allowlist: Allowlist, live_re: re.Pattern[str], *, rearm: bool) -> ControlResult:
    text = r.text or ""
    verdict = classify(text, allowlist, live_re)
    sha = hashlib.sha256(text.encode()).hexdigest()
    if verdict == "allowlisted":
        return ControlResult("paper_mode", True, GATE, actions=("rearm",) if rearm else (),
                             evidence={"account_id_sha256": sha, "allowlist_entry": allowlist.index(text),
                                       "crop": r.crop_sha256})
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
