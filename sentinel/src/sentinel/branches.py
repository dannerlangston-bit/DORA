"""Phase 0 branches for the runtime: `phase0/branches.yaml` read once per run (4.16 Phase 0 reads, 7.14, 11.8, 6.13).

`load(path)` reads the file into `Branches`: the seven decision records DR-01 to DR-07 of 11.8, the Batch API and
reviewer model id of DR-07 (3.15), the version-read regexes of DR-04 (7.3) and the ten safety items of 6.13. An
undecided record resolves to its 11.8 default; the accessors give each consumer what it switches on:

- `gates()` (`GateBranches`): the bounds source of DR-01 (2.7, the `ScreenModel` branch), the frozen data provider
  and its masked_live fallback of DR-02 (7.9), the login challenge path of DR-03 (7.7 step 4), the launcher's update
  expectation and the release-night upgrade flow of DR-04 (7.3, 7.7 step 1), the threshold policy of DR-05 (3.9) and
  the session rule of DR-06 (7.7), plus the main-window title and About regexes (7.3).
- `identity()` (`IdentityPatterns`): the paperMoney and live account id patterns of 6.13 item 5 (6.4, L-FIX-01, the
  stage-2 redaction sweep of 6.10).
- `attestation()` (`AttestationBranches`): the A3 and A4 expectations of 6.13 items 3 and 2 (6.3).
- `drill()` (`DrillBranches`): the drill ticket of 6.13 item 6 (6.5) and the recovery `Cancel All` of item 9.
- `gateway()` (`GatewayBranches`): the dialog labels Phase 0 observed (item 10, 6.2) and chart order-bubble drags
  (item 4).
- `models()` (`ModelBranches`): agent rung, Explorer, Reviewer and Batch API under DR-07 (5.5, 6.11, 3.15, 10.7).

`problems(data)` lists what the notation of 4.16 cannot express and the runtime still refuses; `load` raises
`BranchesError` on a schema violation or any problem (the run's `config_invalid`), and `lint/checks/branches.py`
reports the same problems under L-XREF-15, so CI refuses what the run would.

Choices where the spec is ambiguous or silent (reported):

1. Undecided before its deadline (11.8). "Every record names a default branch ...: at its deadline an undecided
   record takes its default", and the Phase 0 items keep "the default that holds until it is answered". The runtime
   has no other branch to run, so an undecided record resolves to its default whatever the date; the deadline
   decides only the L-XREF-15 warning and the run-health line.
2. DR-06 letters. The notation records A or B; A is "the second session ends the first" (POP-04, abort) and B
   "sessions coexist", as the example file reads them. The default is A ("Ends, the conservative branch").
3. Safety item defaults (11.8: "the conservative branch 6.13 names for each"). Items 1, 2, 3 and 5 default to A:
   rule 2 stays either way (item 1); an attested toggle that turns out absent reads as a mismatch and fails closed
   (items 2, 3); the assumed id formats stand until another is read (item 5). Items 4, 6, 8, 9 and 10 default to B:
   chart drags are order-stage steps (4), the buy-limit drill ticket (6, as 11.8 states), no title-bar badge is
   relied on (8), the recovery class is not trusted to cancel in one action (9), and the dismiss allowlist stays
   empty until the dialog is read (10). Item 7 has no branch.
4. Item 5 branch B records the formats read in `observed` as `paper: <regex>` and `live: <regex>`, full-match
   regexes that replace `^D-[0-9A-Z]+$` and `^[0-9]{8}SCHW$`; the redaction sweep uses the same regexes unanchored,
   bounded by non-alphanumerics.
5. `version_read.title_regex` and `pinned_install.title_regex` record the same Phase 0 read (7.3 step 1, DR-04's
   output); either may carry it, and when both do they must be equal.
6. `batch_api: undecided` reads as off (3.15: off while the ruling requires zero data retention).
7. DR-07. Undecided is "B with zero model calls" (11.8): no Reviewer, no agent rung, no Explorer. Decided B keeps
   the Reviewer on the approved tier when `reviewer.model_id` is pinned. The agent rung and the Explorer need
   branch A and a pinned model id.
8. Item 10. A dismiss label counts only once item 10 is read (`confirmed` or `different`) and the label is among
   the labels `observed`; until then the allowlist is empty (6.13 item 10, branch B column). This narrows
   `gateway_config.load_dismiss_allowlist`, whose own pin rule still applies.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from .lint import schemas
from .lint.repo import load_yaml_text
from .lint.rules import ACCOUNT as PAPER_ID_RE, BRANCH_KEYS
from .orchestrator.attestation import ABSENT, AttestationProfile
from .orchestrator.identity import LIVE_ID_RE

BRANCHES_PATH = "phase0/branches.yaml"
UNDECIDED = "undecided"
LETTERS = ("A", "B")

#: The decision record of each unknown (11.8), in BRANCH_KEYS order.
RECORD_IDS: dict[str, str] = dict(zip(BRANCH_KEYS, (f"DR-0{i}" for i in range(1, 8))))
#: "Default if undecided" of each decision record (11.8; choice 2 for DR-06).
DEFAULTS: dict[str, str] = {"jab": "B", "data_fixture": "B", "login_mfa": "B", "pinned_install": "B",
                            "aa_floor": "B", "session_rule": "A", "computer_use": "B"}
SAFETY_ITEMS = tuple(range(1, 11))                         # 6.13 items 1 to 10
NO_BRANCH_ITEM = 7                                         # 6.13 item 7: n/a in both columns
#: The conservative branch of each safety item while it is undecided (11.8; choice 3).
SAFETY_DEFAULTS: dict[int, str] = {1: "A", 2: "A", 3: "A", 4: "B", 5: "A", 6: "B", 8: "B", 9: "B", 10: "B"}
RESULT_BRANCH = {"confirmed": "A", "different": "B"}
ID_FORMAT_ITEM = 5                                         # 6.13 item 5: paperMoney and live id formats
ID_KINDS = ("paper", "live")                               # choice 4: `observed` entries of item 5 branch B
SHIFT_CLICK_ITEM, CONFIRMATION_ITEM = 2, 3                 # 6.13 items 2 (A4) and 3 (A3)
CHART_DRAG_ITEM, DRILL_TICKET_ITEM, CANCEL_ALL_ITEM, DIALOG_ITEM = 4, 6, 9, 10


class BranchesError(ValueError):
    """`phase0/branches.yaml` is missing or cannot drive the run; `problems` lists why (L-XREF-15)."""

    def __init__(self, message: str, problems: Sequence[tuple[str, str]] = ()):
        super().__init__(message)
        self.problems = tuple(problems)


# -- the records ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BranchChoice:
    """One decision record as recorded (4.16 `branch_choice`), with its 11.8 default."""

    key: str                              # one of BRANCH_KEYS
    branch: str                           # A, B or undecided, as recorded
    deadline: date | None = None          # required while undecided (11.8)
    decided_by: str | None = None
    decided_at: date | None = None
    title_regex: str | None = None        # pinned_install only (DR-04 output, 7.3)

    @property
    def record(self) -> str:
        """The decision record id, `DR-01` to `DR-07`."""
        return RECORD_IDS[self.key]

    @property
    def undecided(self) -> bool:
        return self.branch == UNDECIDED

    @property
    def resolved(self) -> str:
        """The branch the run uses: the recorded letter, or the 11.8 default while undecided (choice 1)."""
        return DEFAULTS[self.key] if self.undecided else self.branch

    def past_deadline(self, today: date) -> bool:
        """Undecided after its deadline day: the default stays in force and L-XREF-15 warns."""
        return self.undecided and self.deadline is not None and today > self.deadline


@dataclass(frozen=True)
class SafetyItem:
    """One 6.13 Phase 0 assumption as read (4.16 `safety` entry)."""

    item: int
    result: str = UNDECIDED               # confirmed (branch A), different (branch B) or undecided
    observed: tuple[str, ...] = ()        # item 10: the dialog labels; item 5 branch B: the id formats (choice 4)
    note: str | None = None
    read_on_build: str | None = None

    @property
    def branch(self) -> str | None:
        """A or B as read, the conservative default while undecided (choice 3); None for item 7."""
        if self.item == NO_BRANCH_ITEM:
            return None
        return RESULT_BRANCH.get(self.result, SAFETY_DEFAULTS[self.item])

    @property
    def read(self) -> bool:
        return self.result != UNDECIDED


# -- what each consumer switches on ------------------------------------------------------------------------------

@dataclass(frozen=True)
class GateBranches:
    """The gate state machine's branches (DR-01 to DR-06; 2.7, 3.9, 7.3, 7.7, 7.9)."""

    bounds_source: str                    # DR-01: A = JAB, B = OCR and templates (the ScreenModel branch)
    data_fixture: str                     # DR-02
    login_mfa: str                        # DR-03
    pinned_install: str                   # DR-04
    aa_floor: str                         # DR-05
    session_rule: str                     # DR-06 (choice 2)
    title_regex: str | None = None        # 7.3 version read step 1
    about_regex: str | None = None        # 7.3 version read step 2 (Help > About)

    @property
    def frozen_provider(self) -> str:
        """The frozen data source: the test backend's fixture (DR-02 A) or OnDemand (B); the L-XREF-15 values."""
        return "backend_fixture" if self.data_fixture == "A" else "ondemand"

    def data_mode(self, declared: str, *, ondemand_available: bool) -> str:
        """The data mode a checkpoint runs in (DR-02, 7.9): under branch B a frozen checkpoint falls back to
        masked_live when OnDemand did not qualify on this login."""
        if declared == "frozen" and self.data_fixture == "B" and not ondemand_available:
            return "masked_live"
        return declared

    @property
    def challenge_enters_recovery(self) -> bool:
        """DR-03 B: a challenge screen is the `trusted_device_prompt` recovery (7.7 step 4); under A it is
        `env_error (gate: authenticated, reason: authentication_challenge)` at once."""
        return self.login_mfa == "B"

    @property
    def update_expected(self) -> bool:
        """DR-04 B: the launcher's updater runs before the snapshot (7.3); under A update progress (POP-01) is
        `env_error (gate: launched, reason: version_mismatch)` (7.7 step 1)."""
        return self.pinned_install == "B"

    @property
    def upgrade_flow_every_rc(self) -> bool:
        """DR-04 B: the release-night upgrade run is a flow run at every RC cut, not only at G-1 (11.8)."""
        return self.pinned_install == "B"

    @property
    def thresholds_from_aa(self) -> bool:
        """DR-05 A: per-region thresholds from the A/A floors (3.9); under B none until the fix list is closed and
        a repeat run is clean, so above-zero regions are `needs_human (reason: uncalibrated)`."""
        return self.aa_floor == "A"

    @property
    def second_login_ends_first(self) -> bool:
        """DR-06 ends: the POP-04 dialog is `env_error (gate: authenticated, reason: session_kicked)`; under
        coexists the re-login checks for a surviving orphan session (7.7)."""
        return self.session_rule == "A"


def _unanchored(rx: str) -> str:
    """A full-match id regex as a search pattern bounded by non-alphanumerics (choice 4)."""
    body = rx[1:] if rx.startswith("^") else rx
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    return rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"


@dataclass(frozen=True)
class IdentityPatterns:
    """The account id formats of 6.13 item 5: full-match regexes for identity (6.4) and L-FIX-01."""

    paper: re.Pattern[str] = PAPER_ID_RE
    live: re.Pattern[str] = LIVE_ID_RE
    replaced: bool = False                # item 5 branch B: the formats Phase 0 read replace the assumed ones

    def sweep_patterns(self, defaults: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        """The stage-2 redaction patterns (6.10): `defaults` (kind, regex) with `paper_id` and `live_id` replaced
        by the read formats under item 5 branch B (6.13: "the redaction patterns are replaced")."""
        if not self.replaced:
            return tuple(defaults)
        new = {"paper_id": _unanchored(self.paper.pattern), "live_id": _unanchored(self.live.pattern)}
        return tuple((kind, new.get(kind, rx)) for kind, rx in defaults)


@dataclass(frozen=True)
class AttestationBranches:
    """A3 and A4 of 6.3 under 6.13 items 3 and 2: On and Off, or "absent" when Phase 0 found no such option."""

    order_confirmation: bool | str = True     # A3
    shift_click_send: bool | str = False      # A4

    @property
    def a9_runs(self) -> int:
        """A9 runs twice when A3 is "absent", since it is then the sole attestation of the dialog (item 3 B)."""
        return 2 if self.order_confirmation == ABSENT else 1

    def apply(self, profile: AttestationProfile) -> AttestationProfile:
        """The settings profile's attestation expectations with A3 and A4 as the branches say."""
        return replace(profile, order_confirmation=self.order_confirmation, shift_click_send=self.shift_click_send)


@dataclass(frozen=True)
class DrillBranches:
    """The fire drill's branches (6.5): the drill ticket (item 6) and the recovery `Cancel All` (item 9). Both
    tickets are 1 share, DAY limit, far from market by the fixture's distance, on the EMPTY account."""

    sell_to_open: bool                    # item 6 A: sell-to-open on the EMPTY IRA; B: buy limit far below market
    cancel_all_confirmed: bool            # item 9 A: Cancel All cancels every row in one action

    @property
    def side(self) -> str:
        return "sell_to_open" if self.sell_to_open else "buy"

    @property
    def limit_from_market(self) -> int:
        """+1 when the limit sits above the market (a sell), -1 below (a buy)."""
        return 1 if self.sell_to_open else -1

    @property
    def stray_send_row(self) -> str:
        """What a stray send would leave: a rejected order's Canceled row (A) or a Working row (B) that the
        recovery Cancel All deletes; the post-run proof catches either (6.5)."""
        return "Canceled" if self.sell_to_open else "Working"


@dataclass(frozen=True)
class GatewayBranches:
    """The gateway's Phase 0 reads: the Order Confirmation dialog's labels (item 10) and chart drags (item 4)."""

    dialog_read: bool = False
    observed_labels: frozenset[str] = frozenset()
    chart_drag_order_stage: bool = True   # item 4 B: chart drags in paper_send_ok flows are order-stage steps

    def dismiss_labels(self, labels: Iterable[str]) -> frozenset[str]:
        """The allowlisted labels the Phase 0 read backs (choice 8): none until item 10 is read."""
        return frozenset(lb for lb in labels if lb in self.observed_labels) if self.dialog_read else frozenset()


@dataclass(frozen=True)
class ModelBranches:
    """DR-07 and the reviewer pins (choice 7; 3.15, 5.5, 6.11, 10.7)."""

    computer_use: str                     # the resolved letter
    decided: bool
    model_id: str | None = None
    batch_api: str = UNDECIDED

    @property
    def agent_rung_allowed(self) -> bool:
        """The agent rung of the locator ladder (5.5); under B the ladder ends at the OCR text anchor."""
        return self.computer_use == "A" and self.model_id is not None

    @property
    def explorer_allowed(self) -> bool:
        """The Explorer (6.8) runs in Phase 3 only under branch A."""
        return self.agent_rung_allowed

    @property
    def reviewer_enabled(self) -> bool:
        """The Reviewer runs on the pinned approved model once DR-07 is decided; otherwise reports are unreviewed."""
        return self.decided and self.model_id is not None

    @property
    def batch_api_enabled(self) -> bool:
        return self.batch_api == "on"


# -- the file ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Branches:
    """`phase0/branches.yaml` as the run reads it (4.16)."""

    choices: Mapping[str, BranchChoice]
    batch_api: str = UNDECIDED
    model_id: str | None = None
    title_regex: str | None = None        # version_read.title_regex, else pinned_install.title_regex (choice 5)
    about_regex: str | None = None
    safety: Mapping[int, SafetyItem] = field(default_factory=dict)

    def branch(self, key: str) -> str:
        """The resolved letter of one unknown (`jab`, `data_fixture`, ...)."""
        return self.choices[key].resolved

    def safety_item(self, item: int) -> SafetyItem:
        """A safety item as read; an item the file omits is undecided."""
        return self.safety.get(item, SafetyItem(item))

    def run_health_lines(self, today: date) -> tuple[str, ...]:
        """One line per undecided record, so a default in force is never silent (11.8)."""
        out = []
        for key in BRANCH_KEYS:
            c = self.choices[key]
            if c.undecided:
                when = f"deadline {c.deadline.isoformat()} passed" if c.past_deadline(today) else \
                    f"deadline {c.deadline.isoformat()}" if c.deadline else "no deadline"
                out.append(f"phase 0 {c.record} {key} undecided: default branch {c.resolved} in force ({when})")
        return tuple(out)

    def gates(self) -> GateBranches:
        return GateBranches(self.branch("jab"), self.branch("data_fixture"), self.branch("login_mfa"),
                            self.branch("pinned_install"), self.branch("aa_floor"), self.branch("session_rule"),
                            self.title_regex, self.about_regex)

    def identity(self) -> IdentityPatterns:
        item = self.safety_item(ID_FORMAT_ITEM)
        if item.branch != "B":
            return IdentityPatterns()
        formats = _id_formats(item.observed)
        return IdentityPatterns(re.compile(formats["paper"]), re.compile(formats["live"]), replaced=True)

    def attestation(self) -> AttestationBranches:
        a3 = ABSENT if self.safety_item(CONFIRMATION_ITEM).branch == "B" else True
        a4 = ABSENT if self.safety_item(SHIFT_CLICK_ITEM).branch == "B" else False
        return AttestationBranches(a3, a4)

    def drill(self) -> DrillBranches:
        return DrillBranches(sell_to_open=self.safety_item(DRILL_TICKET_ITEM).branch == "A",
                             cancel_all_confirmed=self.safety_item(CANCEL_ALL_ITEM).branch == "A")

    def gateway(self) -> GatewayBranches:
        dialog = self.safety_item(DIALOG_ITEM)
        return GatewayBranches(dialog.read, frozenset(dialog.observed),
                               chart_drag_order_stage=self.safety_item(CHART_DRAG_ITEM).branch == "B")

    def models(self) -> ModelBranches:
        c = self.choices["computer_use"]
        return ModelBranches(c.resolved, not c.undecided, self.model_id, self.batch_api)


def _id_formats(observed: Iterable[str]) -> dict[str, str]:
    """The `paper:` and `live:` entries of item 5's `observed` (choice 4), by kind; malformed entries are skipped."""
    out: dict[str, str] = {}
    for entry in observed:
        kind, sep, rx = entry.partition(":")
        if sep and kind.strip() in ID_KINDS:
            out.setdefault(kind.strip(), rx.strip())
    return out


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _regex_problem(path: str, rx: Any) -> list[tuple[str, str]]:
    if rx is None:
        return []
    try:
        re.compile(rx)
    except (re.error, TypeError) as exc:
        return [(path, f"{path} does not compile: {exc}")]
    return []


def problems(data: Any) -> list[tuple[str, str]]:
    """What the 4.16 notation cannot express and the run still refuses, as (path, message): an undecided record
    without a deadline (11.8), a regex that does not compile, two different title regexes (choice 5), safety items
    outside 1 to 10 or listed twice, and item 5 branch B without its two formats (choice 4). Run on a document
    that passed the schema."""
    if not isinstance(data, dict):
        return [("", "phase0/branches.yaml is a mapping (4.16)")]
    out: list[tuple[str, str]] = []
    for key in BRANCH_KEYS:
        c = data.get(key) if isinstance(data.get(key), dict) else {}
        if c.get("branch") == UNDECIDED and _date(c.get("deadline")) is None:
            out.append((f"{key}.deadline", f"{RECORD_IDS[key]} {key} is undecided without a deadline; an undecided "
                                           "record carries the date its default takes over (11.8)"))
        out += _regex_problem(f"{key}.title_regex", c.get("title_regex"))
    vr = data.get("version_read") if isinstance(data.get("version_read"), dict) else {}
    out += _regex_problem("version_read.title_regex", vr.get("title_regex"))
    out += _regex_problem("version_read.about_regex", vr.get("about_regex"))
    pinned = data["pinned_install"].get("title_regex") if isinstance(data.get("pinned_install"), dict) else None
    if pinned is not None and vr.get("title_regex") is not None and pinned != vr.get("title_regex"):
        out.append(("pinned_install.title_regex", "pinned_install.title_regex and version_read.title_regex differ; "
                                                  "they record the same main-window title read (7.3)"))
    seen: set[int] = set()
    for i, s in enumerate(data.get("safety") or ()):
        n = s.get("item") if isinstance(s, dict) else None
        if n not in SAFETY_ITEMS:
            out.append((f"safety[{i}].item", f"safety item {n} is not one of the ten items of 6.13"))
            continue
        if n in seen:
            out.append((f"safety[{i}].item", f"safety item {n} is listed twice"))
        seen.add(n)
        if n == ID_FORMAT_ITEM and s.get("result") == "different":
            formats = _id_formats(s.get("observed") or ())
            for kind in ID_KINDS:
                if kind not in formats:
                    out.append((f"safety[{i}].observed", f"safety item 5 is different but records no '{kind}: <regex>' "
                                                         "id format; the lint and redaction patterns need it (6.13)"))
                else:
                    out += _regex_problem(f"safety[{i}].observed.{kind}", formats[kind])
    return out


def parse(data: Any) -> Branches:
    """`Branches` from the loaded YAML of `phase0/branches.yaml`; raises `BranchesError` on a schema violation or a
    problem of `problems`."""
    found = schemas.validate("branches", data) + problems(data)
    if found:
        raise BranchesError("phase0/branches.yaml cannot drive the run: " +
                            "; ".join(f"{p or '(root)'}: {m}" for p, m in found), found)
    choices = {}
    for key in BRANCH_KEYS:
        c = data[key]
        choices[key] = BranchChoice(key, c["branch"], _date(c.get("deadline")), c.get("decided_by"),
                                    _date(c.get("decided_at")), c.get("title_regex"))
    vr = data.get("version_read") or {}
    safety = {s["item"]: SafetyItem(s["item"], s["result"], tuple(s.get("observed") or ()), s.get("note"),
                                    s.get("read_on_build")) for s in data.get("safety") or ()}
    return Branches(choices, data.get("batch_api", UNDECIDED), (data.get("reviewer") or {}).get("model_id"),
                    vr.get("title_regex") or choices["pinned_install"].title_regex, vr.get("about_regex"), safety)


def load(path: str | Path) -> Branches:
    """Read `phase0/branches.yaml` (4.16); a missing file raises `BranchesError` (L-XREF-15)."""
    p = Path(path)
    if not p.is_file():
        raise BranchesError(f"{BRANCHES_PATH} is missing at {p}; it names one branch for each of the seven unknowns")
    return parse(load_yaml_text(p.read_text(encoding="utf-8")))
