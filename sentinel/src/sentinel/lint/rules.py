"""The lint rules of 4.2 to 4.12 and the cross-file rules of 4.15 (`sentinel lint`).

`lint(root, phase, today)` loads a sentinel-config checkout and returns a `LintReport`: every
`Violation` with its spec rule id (`L-<FILE>-<nn>`), severity `error` or `warn`, file, location
in the file and message, plus the rules that cannot be checked from files alone and why (4.1:
"A rule marked `error (run start)` is checked only against live state at run start"). The lint
first validates every file against its generated schema (`schemas.py`), then runs the per-file
rules, then the cross-file rules (4.15). Rules are written defensively, so a file that fails its
schema still gets the specific rule ids that apply to it.

Choices where the spec is ambiguous (reported):

1. Schema failures, unreadable files and broken `ref:` targets that no numbered rule names carry
   the family's id `-00` (for example `L-SRF-00`); a schema failure of an auxiliary file (ladders,
   renames, policies, branches, profiles, allowed configs, redaction) carries the rule that reads it.
2. `--phase`: the TBD-glob rule (L-DEP-01) and unresolved Phase 0 branches (L-XREF-15) warn in
   Phase 0 and are errors from Phase 1; L-FIX-09 warns until Phase 2 (11.8, 4.9).
3. Order surfaces (L-FLW-12) are the surfaces that list `OrderTicketCore`; ticket surfaces (L-FLW-15)
   are `kind: ticket_variant`; Active Trader surfaces (L-WSP-06) are `kind: active_trader_panel`.
4. The derived hosted-surface list (L-SRF-14) is a tier A workspace's `gadgets_present` and
   `active_trader_panels` plus every registry descendant of them by `parent`, so a dialog whose
   parent is a hosted ticket is hosted.
5. L-FLW-11 resolves an assertion's region against the nearest preceding checkpoint; a step before
   the first checkpoint resolves against the first one, a teardown step against any checkpoint of
   the flow, and a `*_since_previous_step` kind also against its `since_step` checkpoint (the 4.7
   worked examples need all three). Rule operand lists: RULE_OPERANDS below (2.8.3 names only R01's
   and R02's operands, through the 4.7 examples).
6. L-FLW-10 checks `max_duration_s` against 5 minutes unless a measured p50 is passed in; measured
   p50s live in the Run Store, not in the files.
7. L-MUT-05 ("each appear at least once per tier-1 checkpoint") cannot hold for every tier-1
   checkpoint with the 33-entry catalog of 12.5; it is checked as: each of the six brief classes is
   bound to at least one tier-1 checkpoint, and every `color_swap` expects INV-15. The
   per-checkpoint count belongs with L-MUT-03 (approved baselines). L-MUT-02 also lets the
   `redaction` class (M19) expect `identical`: a working redaction stage leaves the capture identical,
   and 12.5 proves M19 by the redaction hit, not by a diff.
8. L-RGN-07 recognises the allowed frozen masks by the region id's last segment: `clock`,
   `connection-indicator`, `caret` or `parking`.
9. L-MSK-03 measures a panel class as the bounding box of the checkpoint's regions of that class on
   one landmark; redaction anchors (`redaction/anchors.yaml`) on a landmark the checkpoint declares
   count toward it unless `identity_field: true`.
10. L-XREF-06 reports a manifest that still names a surface or primitive absent from the registry
    or the map, and a disagreement between map and registry, once per file.
11. L-XREF-07's credential pattern: `password`, `passwd`, `pwd` or `secret` followed by `:` or `=`
    and a value that is not a vault reference, or a value after `:` of 20 or more characters with
    upper case, lower case and digits whose character class changes at least every 2.5 characters
    (random keys do, branch names and class names do not); `//` URIs and vault references are exempt.
12. `today` (default: the current date) decides expiries, roll dates and the 90-day verification age.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from ..taxonomy import is_checkpoint_id, is_slug, load as load_taxonomy
from . import schemas
from .repo import Doc, Repo


# -- rule table ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """One lint rule: spec id, severity, what it checks, and why it is skipped when it cannot be
    checked from the files (`skipped`)."""
    id: str
    severity: str
    check: str
    skipped: str | None = None


_RUN_START = "run start only: checked against live state by the Orchestrator before gate provisioned (4.1)"

RULES: dict[str, Rule] = {r.id: r for r in (
    Rule("L-BLD-00", "error", "build file reads and validates against schemas/build.schema.json"),
    Rule("L-BLD-01", "error", "build_id unique and equal to <channel>-<version_string>-<first 7 of commit_sha>"),
    Rule("L-BLD-02", "error", "artifact installs carry artifact.sha256; snapshot installs carry snapshot_id"),
    Rule("L-BLD-03", "error", "branch builds carry branch and merge_base_sha; other channels do not"),
    Rule("L-BLD-04", "error", "build pair follows the base-rule table of 4.2"),
    Rule("L-BLD-05", "error", "candidate != base unless base_rule = same_build"),
    Rule("L-BLD-06", "error", "released_at present if and only if channel = released"),
    Rule("L-MAN-00", "error", "manifest reads and validates against schemas/manifest.schema.json"),
    Rule("L-MAN-01", "error", "every declared surface exists in the Surface Registry"),
    Rule("L-MAN-02", "error", "every touched primitive exists in the dependency map"),
    Rule("L-MAN-03", "error", "expected_visual has at least 20 characters and differs from summary"),
    Rule("L-MAN-04", "error", "mock_image exists, is PNG, at most 1920x1080 and 5 MB"),
    Rule("L-MAN-05", "error", "ticket keys match the Jira regex; at least one ticket per entry"),
    Rule("L-MAN-06", "error", "empty entries need empty_reason; merge and rc manifests need approved_by"),
    Rule("L-MAN-07", "warn", "each declared surface consumes a touched primitive, or changed_paths is non-empty"),
    Rule("L-MAN-08", "error", "feature flag names match ^[a-z0-9_.]+$ and are unique within the entry"),
    Rule("L-MAN-09", "error", "manifest candidate equals the pair's candidate", _RUN_START),
    Rule("L-MAN-10", "error", "new_surface agrees with the base build's registry",
         "needs the base build's registry commit and the ticket type, which the config files do not hold"),
    Rule("L-DEP-00", "error", "dependency map reads and validates against schemas/dependency-map.schema.json"),
    Rule("L-DEP-01", "error", "primitive ids unique and PascalCase with source_packages; TBD globs warn in Phase 0, error after"),
    Rule("L-DEP-02", "error", "edge surface exists, is a glob matching a registry id, or is * on a global primitive"),
    Rule("L-DEP-03", "error", "edge confidence, evidence and confirmation fields are consistent"),
    Rule("L-DEP-04", "error", "no duplicate (primitive, surface) edge"),
    Rule("L-DEP-05", "error", "registry primitives and map edges agree in both directions"),
    Rule("L-DEP-06", "error", "every registered surface has at least one incoming edge"),
    Rule("L-DEP-07", "warn", "last_verified is at most 90 days old"),
    Rule("L-DEP-08", "error", "the edge FlexibleGrid -> trade.active-trader.ladder exists"),
    Rule("L-SRF-00", "error", "surface entry reads and validates against schemas/surface.schema.json"),
    Rule("L-SRF-01", "error", "surface id unique, a slug, and equal to the file name"),
    Rule("L-SRF-02", "error", "tier 1 has owner, smoke, a must_see landmark, anti_anchors, labels and a required control"),
    Rule("L-SRF-03", "error", "resizable needs hosts and P8 or splitters; size_ladder exists with a reason; ladders S and F as 2.5.2"),
    Rule("L-SRF-04", "error", "dialog, detached and popup windows need title_regex; main forbids it"),
    Rule("L-SRF-05", "error", "path ends with wait_for on a must_see landmark; every path target resolves on two rungs"),
    Rule("L-SRF-06", "error", "template files exist, are PNG, 16x16 to 400x400"),
    Rule("L-SRF-07", "error", "every primitive exists in the dependency map; roots non-empty"),
    Rule("L-SRF-08", "error", "masked_live surfaces: every census, sweep and split checkpoint has a rule or mask region"),
    Rule("L-SRF-09", "error", "control ids unique; jab only when phase0/branches.yaml says JAB is branch A"),
    Rule("L-SRF-10", "error", "census.workspaces non-empty and existing when enabled; dimension values are slugs"),
    Rule("L-SRF-11", "error", "parent exists without a cycle; invariant exceptions carry owner, reason and a future expiry"),
    Rule("L-SRF-12", "error", "surfaces reached only after a sent order carry paper_send",
         "which surfaces are reachable only after a sent order is product knowledge the files do not record"),
    Rule("L-SRF-13", "error", "splitter hosts are in hosts; fractions in the 2.5.2 set, 0.00 and 1.00 only when collapsible"),
    Rule("L-SRF-14", "error", "every surface is hosted by at least one tier A workspace"),
    Rule("L-SRF-15", "error", "at most 40 surfaces carry smoke: true"),
    Rule("L-WSP-00", "error", "workspace entry reads and validates against schemas/workspace.schema.json"),
    Rule("L-WSP-01", "error", "workspace file exists and its hash equals file_sha256"),
    Rule("L-WSP-02", "error", "tier A: exactly one entry per persona; persona required"),
    Rule("L-WSP-03", "error", "tier B: saved_under_build present and released"),
    Rule("L-WSP-04", "error", "tier C: size_sweep_cells present and every host surface resizable"),
    Rule("L-WSP-05", "error", "display_config is listed in configs/allowed.yaml"),
    Rule("L-WSP-06", "error", "gadgets and panels exist; every Active Trader gadget is in active_trader_panels"),
    Rule("L-WSP-07", "error", "link_groups.symbol_ref exists in the fixture the run uses"),
    Rule("L-FLW-00", "error", "flow file reads and validates against schemas/flow.schema.json"),
    Rule("L-FLW-01", "error", "flow_id unique; step ids unique; first step and last teardown step reset_input_state"),
    Rule("L-FLW-02", "error", "targets and surface ids exist; checkpoints carry surface_id, target_window, data_mode"),
    Rule("L-FLW-03", "error", "checkpoint_id equals flow/<surface_id>/<flow_id>/<step id>, unique; renames are recorded"),
    Rule("L-FLW-04", "error", "every checkpoint's regions file exists, or is default with data_mode frozen"),
    Rule("L-FLW-05", "error", "masked_live checkpoints reference a rule or mask region"),
    Rule("L-FLW-06", "error", "secret steps only in login flows; login flows disable agent fallback; no credential literal"),
    Rule("L-FLW-07", "error", "Confirm and Send step is order_stage; no Enter or Space after it until the ticket is cleared and proven empty"),
    Rule("L-FLW-08", "error", "class paper_send_ok needs policy/send_policy.yaml enabled and signed by the risk owner"),
    Rule("L-FLW-09", "error", "drag steps >= 10 and hold_ms <= 2000; wheel notches != 0"),
    Rule("L-FLW-10", "error", "budget.max_duration_s within 3 x measured p50, 5 minutes until measured"),
    Rule("L-FLW-11", "error", "assertion regions, since_step and rule operands resolve on the checkpoint"),
    Rule("L-FLW-12", "error", "preconditions paper_mode and no_modal; order flows check working orders before and after"),
    Rule("L-FLW-13", "error", "no step targets a deny control or the auto-send checkbox"),
    Rule("L-FLW-14", "error", "explorer flows carry origin run and finding, class explorer_candidate, under flows/quarantine/"),
    Rule("L-FLW-15", "error", "ticket flows have a pre-input checkpoint before the first ticket field input"),
    Rule("L-FLW-16", "error", "checkpoints after clear_ticket, close_dialog or cancel_all are recapturable: false"),
    Rule("L-FLW-17", "error", "every step target resolves on at least two rungs"),
    Rule("L-FLW-18", "error", "explorer_candidate budgets within 400 steps, 5400 s and policy/explorer_budget.yaml"),
    Rule("L-FLW-19", "error", "no click targets Cancel All; only the cancel_all teardown resolves it"),
    Rule("L-RGN-00", "error", "regions file reads and validates against schemas/regions.schema.json"),
    Rule("L-RGN-01", "error", "region ids unique; file path equals checkpoint_id; a flow or census defines the checkpoint"),
    Rule("L-RGN-02", "error", "regions of different kinds do not overlap, except a mask inside another region"),
    Rule("L-RGN-03", "error", "rule iff type rule; mask_ref iff type mask with an existing mask; fixture_dependency iff freeze"),
    Rule("L-RGN-04", "error", "money cells are diff or freeze, never floor-excepted, and asserted on their checkpoint"),
    Rule("L-RGN-05", "error", "floor_exception carries its human fields and a future expiry, on diff regions only"),
    Rule("L-RGN-06", "warn", "no non-zero calibration floor on a frozen checkpoint",
         "reads calibration/<fingerprint_id>.yaml, which the Calibration Service exports (3.9, 8.3); not read by this lint"),
    Rule("L-RGN-07", "error", "masks on frozen checkpoints only on the clock, connection indicator, caret or parking rectangle"),
    Rule("L-MSK-00", "error", "mask record reads and validates against schemas/mask.schema.json"),
    Rule("L-MSK-01", "error", "created_by human (the decision_ref lives in the Central Store)"),
    Rule("L-MSK-02", "error", "expires_at is in the future (CI)"),
    Rule("L-MSK-03", "error", "masked plus non-identity redacted area per panel class within the mask budget"),
    Rule("L-MSK-04", "warn", "mask growth over 30 days at most 2 percent of a tier-1 checkpoint",
         "needs the mask history of the last 30 days (git history and the regions table)"),
    Rule("L-MSK-05", "error", "a renewal quotes the latest nightly A/A result",
         "needs the previous version of the mask (git history) and the nightly A/A results"),
    Rule("L-FIX-00", "error", "fixture and allowlist read and validate against their schemas"),
    Rule("L-FIX-01", "error", "account ids match ^D-[0-9A-Z]+$ and appear exactly in the rig's allowlist; no patterns"),
    Rule("L-FIX-02", "error", "one golden account, at most one empty; no cash_floor when sends are enabled"),
    Rule("L-FIX-03", "error", "ticket and ladder futures carry a month code, year and a future roll_date"),
    Rule("L-FIX-04", "error", "login_ref is a vault reference; no credential pattern anywhere"),
    Rule("L-FIX-05", "error", "profiles exist; the settings profile pins the attestation items of 12.6"),
    Rule("L-FIX-06", "error", "epoch present, epoch_id recomputes, front_month covers futures, branch fields present"),
    Rule("L-FIX-07", "error", "churn_calendar covers roll dates, the monthly expiration and a timely reseed"),
    Rule("L-FIX-08", "error", "prices recomputed within 24 hours", "handled at run start by the fixture reset flow (4.9)"),
    Rule("L-FIX-09", "warn", "golden positions cover the golden list of 7.6 (error from Phase 2)"),
    Rule("L-FIX-10", "error", "the empty account has no positions and no working orders"),
    Rule("L-POP-00", "error", "popup catalog reads and every entry validates against schemas/popup.schema.json"),
    Rule("L-POP-01", "error", "popup_id matches ^POP-[0-9]{2,}$, unique, in 12.8; name unique; title_regex compiles"),
    Rule("L-POP-02", "error", "dismiss_key is not Enter, Space or Ctrl+Enter"),
    Rule("L-POP-03", "error", "Order Confirmation, Confirm or Send titles are safety kill-switch entries; no click on consent labels"),
    Rule("L-POP-04", "error", "class, click template and target, on_abort, INV-14 routing and taxonomy reasons are consistent"),
    Rule("L-POP-05", "warn", "no two entries match the same seeded title",
         "the seeded title list has no path in the 4.1 layout"),
    Rule("L-POP-06", "warn", "last_seen_build at most three releases old"),
    Rule("L-MUT-00", "error", "mutant catalog reads and every entry validates against schemas/mutant.schema.json"),
    Rule("L-MUT-01", "error", "mutant ids M01 to M33, unique; seed 20260101; region_ref exists on the checkpoint"),
    Rule("L-MUT-02", "error", "money-cell mutants expect a detecting outcome; only controls expect identical or noise"),
    Rule("L-MUT-03", "warn", "20 to 30 mutants per tier-1 checkpoint with an approved baseline",
         "needs the approved baseline set (Central Store)"),
    Rule("L-MUT-04", "error", "behavioral mutants name a canary flow without confirm-and-send; image mutants do not"),
    Rule("L-MUT-05", "error", "the six brief classes are bound to tier-1 checkpoints; color_swap expects INV-15"),
    Rule("L-MUT-06", "error", "invariant ids exist in the 2.7 catalog"),
    Rule("L-FPR-00", "error", "fingerprint validates against schemas/fingerprint.schema.json"),
    Rule("L-FPR-01", "error", "fingerprint field set equals 7.4 plus render_probe_hash; match non-null; id recomputes"),
    Rule("L-FPR-02", "error", "match equals the rig specification of 7.1", _RUN_START + "; see lint_fingerprint_rig_spec"),
    Rule("L-FPR-03", "error", "candidate and base match apart from build_id", _RUN_START + "; see lint_fingerprint_pair"),
    Rule("L-FPR-04", "error", "a calibration file exists for fingerprint_id", _RUN_START + " (uncalibrated, never not_comparable)"),
    Rule("L-FPR-05", "error", "render_probe_hash equal between sides", _RUN_START + "; see lint_fingerprint_pair"),
    Rule("L-XREF-01", "warn", "every surface is reached by a census workspace or a flow checkpoint"),
    Rule("L-XREF-02", "error", "every tier-1 surface has a flow with assertions and a regions file with a money cell"),
    Rule("L-XREF-03", "error", "masked_live checkpoints have a rule or mask region; frozen ones no must_change_within_s"),
    Rule("L-XREF-04", "error", "checkpoint ids referenced by regions files and mutants are defined or retired"),
    Rule("L-XREF-05", "warn", "tier-1 checkpoints have mutants and calibration",
         "needs the approved baselines and calibration/<fingerprint_id>.yaml (Calibration Service)"),
    Rule("L-XREF-06", "error", "map and registry agree; manifests reference existing surfaces and primitives"),
    Rule("L-XREF-07", "error", "no file contains a credential; vault references only"),
    Rule("L-XREF-08", "error", "taxonomy.yaml equals the harness taxonomy generated from the spec"),
    Rule("L-XREF-09", "error", "schemas/*.schema.json regenerate identically from the notation"),
    Rule("L-XREF-10", "error", "changed checkpoint ids, templates or rects carry a rename or recalibrate marker",
         "needs the pull request's diff and the approved checkpoints"),
    Rule("L-XREF-11", "error", "every checkpoint id matches the 4.1 grammar and names a registered surface"),
    Rule("L-XREF-12", "error", "popup ids equal POP-01..POP-22 of 12.8; mutant ids, classes and money_cell equal 12.5"),
    Rule("L-XREF-13", "error", "deny controls and Explorer deny-list surfaces exist and are named by the nightly test",
         "the 6.8 deny list and the denied-action test have no path in the 4.1 layout"),
    Rule("L-XREF-14", "error", "policy/explorer_budget.yaml equals 6.8 unless the Explorer owner approved"),
    Rule("L-XREF-15", "error", "phase0/branches.yaml names one branch per unknown; dependent files agree"),
    Rule("L-XREF-16", "error", "run manifest carries the signed authorization reference", _RUN_START),
)}

# -- spec constants ---------------------------------------------------------------------------------

POP_IDS_12_8 = tuple(f"POP-{i:02d}" for i in range(1, 23))
# 12.5: id -> (class, money_cell); authoritative for ids, seed and the money_cell flag
MUTANTS_12_5: dict[str, tuple[str, bool]] = {
    "M01": ("erased_control", True), "M02": ("erased_control", True), "M03": ("swapped_digit", True),
    "M04": ("swapped_digit", True), "M05": ("swapped_digit", True), "M06": ("swapped_label", False),
    "M07": ("color_swap", False), "M08": ("shifted_panel", False), "M09": ("shifted_panel", False),
    "M10": ("clipped_label", False), "M11": ("clipped_label", False), "M12": ("overlapped_panels", False),
    "M13": ("missing_row", False), "M14": ("header_change", False), "M15": ("placeholder", False),
    "M16": ("font_height", False), "M17": ("scrollbar", False), "M18": ("erased_control", False),
    "M19": ("redaction", False), "M20": ("sub_floor", False), "M21": ("global_shift", False),
    "M22": ("landmark_loss", False), "M23": ("global_shift", False), "M24": ("study_removed", False),
    "M25": ("aa_control", False), "M26": ("wrong_quantity", True), "M27": ("blank_field", False),
    "M28": ("gateway", True), "M29": ("outside_parent", False), "M30": ("window_offscreen", False),
    "M31": ("misaligned_column", False), "M32": ("focus_order", False), "M33": ("error_dialog", False),
}
MUTANT_SEED = 20260101
BRIEF_CLASSES = ("erased_control", "shifted_panel", "swapped_digit", "color_swap", "clipped_label", "overlapped_panels")
PERSONAS = ("active_trader", "options_trader", "futures_trader", "swing_trader", "beginner")
BRANCH_KEYS = ("jab", "data_fixture", "login_mfa", "pinned_install", "aa_floor", "session_rule", "computer_use")
SPLIT_FULL = (0.00, 0.15, 0.30, 0.50, 0.70, 0.85, 1.00)          # 2.5.2
SPLIT_SHORT = (0.15, 0.50, 0.85)
LADDER_S = tuple((w, h) for w in (960, 640, 480, 320) for h in (720, 480, 360))
LADDER_F_GRID = tuple((w, h) for w in (960, 800, 640, 480, 400, 320) for h in (720, 600, 480, 420, 360, 300))
LADDER_F_CORNERS = ((240, 160), (240, "MAXH"), ("MAXW", 160), ("MAXW", "MAXH"))
LADDER_F_ASCENDING = tuple((w, 480) for w in (320, 400, 480, 640, 800, 960))
EXPLORER_BUDGET_6_8 = {"max_actions": 400, "max_minutes": 90, "max_no_change_actions": 20,
                       "max_usd_per_session": 25, "max_usd_per_week": 500}
MASK_BUDGET = {"order_confirmation": 0.10, "order_ticket": 0.10, "position_statement": 0.10, "account_info": 0.05}
FROZEN_MASK_SEGMENTS = ("clock", "connection-indicator", "caret", "parking")
# operands of the rule assertion kind (2.8.3): required, optional (choice 5)
RULE_OPERANDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "R01": (frozenset({"qty", "price", "multiplier", "total"}), frozenset()),
    "R02": (frozenset({"bp_effect", "notional"}), frozenset()),
    "R03": (frozenset({"bid", "mark", "ask"}), frozenset({"last", "low", "high"})),
    "R04": (frozenset({"count", "selector"}), frozenset()),
    "R05": (frozenset({"delta"}), frozenset({"gamma", "vega", "theta"})),
    "R06": (frozenset({"call", "put", "strike", "underlying"}), frozenset({"rate", "dte"})),
    "R07": (frozenset({"dte", "expiration"}), frozenset()),
    "R08": (frozenset({"pl_open", "mark", "trade_price", "qty", "multiplier"}), frozenset()),
    "R09": (frozenset({"export_total", "screen_total"}), frozenset()),
    "R10": (frozenset({"value"}), frozenset()),
}
JIRA = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
FLAG = re.compile(r"^[a-z0-9_.]+$")
PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
POP_ID = re.compile(r"^POP-[0-9]{2,}$")
MUTANT_ID = re.compile(r"^M[0-9]{2,}$")
ACCOUNT = re.compile(r"^D-[0-9A-Z]+$")
FUTURE_MONTH = re.compile(r"^/(?P<root>[A-Z0-9]+?)(?P<month>[FGHJKMNQUVXZ])(?P<year>[0-9]{2})$")
ENTER_KEYS = frozenset({"enter", "return", "space", "spacebar", "ctrl+enter"})
TARGET_FIELDS = ("target", "from", "to", "at")

_CRED_KEY = re.compile(r"(?i)\b(?:password|passwd|pwd|secret)\b\s*[:=]\s*['\"]?(?P<v>[^\s'\",}\]#]+)")
_CRED_VALUE = re.compile(r":\s*['\"]?(?P<v>[^\s'\",{}\[\]#]{20,})")


def _klass(c: str) -> int:
    return 0 if c.isupper() else 1 if c.islower() else 2 if c.isdigit() else 3


def credential_hits(text: str) -> list[tuple[int, str]]:
    """Lines of `text` that match the credential pattern of L-XREF-07 (choice 11), as (line, why)."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in _CRED_KEY.finditer(line):
            v = m.group("v")
            if not v.startswith("vault://") and v.lower() not in ("null", "~", "none", "true", "false"):
                hits.append((n, "a password, pwd or secret key followed by a value"))
        for m in _CRED_VALUE.finditer(line):
            v = m.group("v")
            if v.startswith("//") or v.startswith("vault:"):
                continue
            changes = sum(_klass(a) != _klass(b) for a, b in zip(v, v[1:]))
            if (any(c.isupper() for c in v) and any(c.islower() for c in v) and any(c.isdigit() for c in v)
                    and changes / len(v) >= 0.4):
                hits.append((n, "a value of 20 or more mixed-class characters"))
    return hits


def _walk_strings(data: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """(location, `key: value` line) for every scalar in a parsed document."""
    if isinstance(data, dict):
        for k, v in data.items():
            loc = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                yield from _walk_strings(v, loc)
            elif v is not None:
                yield loc, f"{k}: {v}"
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from _walk_strings(v, f"{prefix}[{i}]")


# -- results ---------------------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class Violation:
    """One lint finding (4.15: rule, severity, file, path, message)."""
    file: str
    rule: str
    severity: str
    path: str
    message: str

    def line(self) -> str:
        """`L-XXX-nn severity file: [location: ]message`."""
        where = f"{self.path}: " if self.path else ""
        return f"{self.rule} {self.severity} {self.file}: {where}{self.message}"

    def as_dict(self) -> dict[str, str]:
        """The JSON-line form of 4.15."""
        return {"rule": self.rule, "severity": self.severity, "file": self.file, "path": self.path, "message": self.message}


@dataclass
class LintReport:
    """Violations sorted by file and rule, plus the rules skipped as not checkable from files."""
    violations: list[Violation]
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warn"]

    @property
    def ok(self) -> bool:
        """True when no error was found (warnings do not block, 4.1)."""
        return not self.errors

    def rules(self) -> set[str]:
        """The rule ids that fired."""
        return {v.rule for v in self.violations}


# -- small helpers ---------------------------------------------------------------------------------

def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _s(x: Any) -> str:
    return x if isinstance(x, str) else ""


def _ts(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _rect(r: Any) -> tuple[int, int, int, int] | None:
    r = _d(r)
    vals = [r.get(k) for k in ("dx", "dy", "w", "h")]
    return tuple(vals) if all(isinstance(v, int) and not isinstance(v, bool) for v in vals) else None  # type: ignore[return-value]


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def _inside(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[0] + inner[2] <= outer[0] + outer[2] and inner[1] + inner[3] <= outer[1] + outer[3])


def _channel(build_id: str) -> str:
    return build_id.split("-", 1)[0] if isinstance(build_id, str) else ""


def _short_sha(build_id: str) -> str:
    return build_id.rsplit("-", 1)[-1] if isinstance(build_id, str) else ""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def epoch_id(epoch: dict) -> str:
    """4.9: sha256 over the canonical JSON of the epoch fields other than `epoch_id`."""
    rest = {k: v for k, v in epoch.items() if k != "epoch_id"}
    return "sha256:" + hashlib.sha256(_canonical(rest).encode("utf-8")).hexdigest()


def fingerprint_id(fingerprint: dict) -> str:
    """4.12: sha256 over the canonical JSON of `match` with `build_id` removed."""
    match = {k: v for k, v in _d(fingerprint.get("match")).items() if k != "build_id"}
    return "sha256:" + hashlib.sha256(_canonical(match).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """`sha256:` plus the hex digest of a file's bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class CheckpointDef:
    """A checkpoint some definition produces: the census, a size sweep, a splitter or a flow step."""
    cid: str
    kind: str
    surface: str
    data_mode: str
    tier: Any
    file: str
    flow: str | None = None
