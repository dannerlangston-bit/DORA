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
   worked examples need all three). Rule operand lists: RULE_OPERANDS below, generated from the
   2.8.3 operand table (SD-34).
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
   count toward it unless `identity_field: true`. Every mask and every redaction box, identity or not,
   is checked against the money cells (budget 0 percent). A rect with a non-positive w or h is an
   L-RGN-00 (regions) or L-MSK-03 (redaction anchors) error and takes part in no geometry check.
10. L-XREF-06 reports a manifest that still names a surface or primitive absent from the registry
    or the map, and a disagreement between map and registry, once per file.
11. L-XREF-07's credential pattern: `password`, `passwd`, `pwd` or `secret`, alone or inside a key such
    as `db_password` or `client_secret`, quoted or not, followed by `:` or `=`
    and a value that is not a vault reference, or a value after `:` of 20 or more characters with
    upper case, lower case and digits whose character class changes at least every 2.5 characters
    (random keys do, branch names and class names do not); `//` URIs and vault references are exempt.
12. `today` (default: the current date) decides expiries, roll dates and the 90-day verification age.
13. File shapes the rules read: `phase0/branches.yaml` has the keys of BRANCH_KEYS with `A` or `B`
    (under DR-07 branch B a flow's `agent_fallback: allowed` disagrees, L-XREF-15);
    `policy/send_policy.yaml` is enabled by `enabled: true` with `risk_owner` and `signature`
    (L-FLW-08); an `approved_by` in `policy/explorer_budget.yaml` stands for the Explorer owner's PR
    approval (L-XREF-14); the settings profile pins `order_confirmation: true`, `shift_click_send:
    false` (or `absent`), `active_trader_hotkeys: false`, `order_hotkeys: []` (L-FIX-05).
14. `taxonomy.yaml` and `schemas/` are optional in a config checkout (this harness carries both);
    when present they must equal the harness's (L-XREF-08, L-XREF-09). The popup and mutant
    catalogs are bare lists (4.10, 4.11) or a map with `schema_version` and the list.
15. "The fixture the run uses" (L-WSP-07) is every fixture a flow on that workspace names, or any
    fixture when no flow uses the workspace. The masked_live rule of L-SRF-08 covers census, sweep
    and split checkpoints; flow checkpoints carry their own data mode (L-FLW-05).

Rules checked only in part from files (the rest needs live state, history or a store; reported):
L-DEP-07 (the 90 days after the first Phase 1 scan), L-FLW-03 (that a removed id was renamed needs
the previous commit; the rename map itself is checked), L-FLW-13 (the 6.8 Explorer deny list has no
file), L-RGN-02 (bounds from the last calibration), L-RGN-04 (the zero floor lives in calibration;
on census checkpoints the 2.8 Oracle checks are code, not files), L-MSK-01 (the decision record is in
the Central Store), L-POP-06 (counts the released builds present in builds/), L-XREF-04 and L-XREF-11
(calibration files), L-XREF-15 (JAB fields are reported by L-SRF-09).
"""
from __future__ import annotations

import hashlib
import importlib
import itertools
import pkgutil
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from PIL import Image

from ..canonical import epoch_id
from ..canonical import fingerprint_id_of as fingerprint_id
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
# A rule of the section 4 tables whose check body is not written yet: the lint lists it as pending until a
# `sentinel.lint.checks` module names it in its RULE_IDS (see run_check_modules).
_PENDING = "pending: no check body yet; the owning package adds it as a sentinel.lint.checks module"
# Record rules of 4.13 and 4.14: the Run Store refuses the row on write (8.3); a config checkout holds no records.
_ON_WRITE = "record rule: enforced by the Run Store on write (4.13, 4.14, 8.3), not by the config lint"

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
    Rule("L-SRF-12", "error", "surfaces with reached_after_send: true carry paper_send in entitlements", _PENDING),
    Rule("L-SRF-13", "error", "splitter hosts are in hosts; fractions in the 2.5.2 set, 0.00 and 1.00 only when collapsible"),
    Rule("L-SRF-14", "error", "every surface is hosted by at least one tier A workspace"),
    Rule("L-SRF-15", "error", "at most 40 surfaces carry smoke: true"),
    Rule("L-SRF-16", "error", "critical: true requires tier: 1", _PENDING),
    Rule("L-WSP-00", "error", "workspace entry reads and validates against schemas/workspace.schema.json"),
    Rule("L-WSP-01", "error", "workspace file exists and its hash equals file_sha256"),
    Rule("L-WSP-02", "error", "tier A: exactly one entry per persona; persona required"),
    Rule("L-WSP-03", "error", "tier B: saved_under_build present and released"),
    Rule("L-WSP-04", "error", "tier C: size_sweep_cells present and every host surface resizable"),
    Rule("L-WSP-05", "error", "display_config is listed in configs/allowed.yaml"),
    Rule("L-WSP-06", "error", "gadgets and panels exist; every Active Trader gadget is in active_trader_panels"),
    Rule("L-WSP-07", "error", "link_groups.symbol_ref exists in the fixture the run uses"),
    Rule("L-WSP-08", "error", "tier A and tier C entries list no streaming: true surface in gadgets_present", _PENDING),
    Rule("L-WSP-09", "warn", "corpus shape of 2.3: one tier C entry, four to eight tier B with two on the previous "
         "release (error from Phase 3)", _PENDING),
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
    Rule("L-RGN-04", "error", "money cells are never mask or floor-excepted; on a flow checkpoint diff or freeze and "
         "asserted, on a census checkpoint a rule region"),
    Rule("L-RGN-05", "error", "floor_exception carries its human fields and a future expiry, on diff regions only"),
    Rule("L-RGN-06", "warn", "no non-zero calibration floor on a frozen checkpoint (calibration/<hex>.yaml)", _PENDING),
    Rule("L-RGN-07", "error", "masks on frozen checkpoints only on the clock, connection indicator, caret or parking rectangle"),
    Rule("L-RGN-08", "error", "money_cell iff money_role; money_role only on order_confirmation and order_ticket panels",
         _PENDING),
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
    Rule("L-POP-01", "error", "popup_id matches ^POP-[0-9]{2,}$, unique, further ids in sequence; name unique; title_regex "
         "compiles; retired needs retired_reason; the file is {schema_version, entries}"),
    Rule("L-POP-02", "error", "dismiss_key is not Enter, Space or Ctrl+Enter"),
    Rule("L-POP-03", "error", "Order Confirmation, Confirm or Send titles are safety kill-switch entries; no click on consent labels"),
    Rule("L-POP-04", "error", "class, click template and target, on_abort, INV-14 routing and taxonomy reasons are consistent"),
    Rule("L-POP-05", "warn", "no two entries match one title of either entry's seed_titles", _PENDING),
    Rule("L-POP-06", "warn", "last_seen_build at most three releases old"),
    Rule("L-MUT-00", "error", "mutant catalog reads and every entry validates against schemas/mutant.schema.json"),
    Rule("L-MUT-01", "error", "mutant ids unique, M01 to M33 equal to 12.5 then M34 on; seed; region_ref exists on the "
         "checkpoint; the file is {schema_version, entries}"),
    Rule("L-MUT-02", "error", "money-cell mutants expect a detecting outcome; only controls expect identical or noise"),
    Rule("L-MUT-03", "error", "every critical checkpoint has at least two behavioral mutants bound to it", _PENDING),
    Rule("L-MUT-04", "error", "behavioral mutants name a canary flow without confirm-and-send; image mutants do not"),
    Rule("L-MUT-05", "error", "the six brief classes are bound to tier-1 checkpoints; color_swap expects INV-15"),
    Rule("L-MUT-06", "error", "invariant ids exist in the 2.7 catalog"),
    Rule("L-FPR-00", "error", "fingerprint validates against schemas/fingerprint.schema.json"),
    Rule("L-FPR-01", "error", "fingerprint field set equals 7.4 plus render_probe_hash; match non-null; id recomputes"),
    Rule("L-FPR-02", "error", "match equals the rig specification of 7.1", _RUN_START + "; see lint_fingerprint_rig_spec"),
    Rule("L-FPR-03", "error", "candidate and base match apart from build_id", _RUN_START + "; see lint_fingerprint_pair"),
    Rule("L-FPR-04", "error", "a calibration exists for the fingerprint's calibration key (4.12)",
         _RUN_START + " (uncalibrated, never not_comparable)"),
    Rule("L-FPR-05", "error", "render_probe_hash equal between sides", _RUN_START + "; see lint_fingerprint_pair"),
    Rule("L-OUT-01", "error", "outcome, status, reason, gate, finding kind and triage_state values are taxonomy.yaml's",
         _ON_WRITE),
    Rule("L-OUT-02", "error", "the required fields of the record's outcome are present", _ON_WRITE),
    Rule("L-OUT-03", "error", "no reviewer classification and no decision on invariant_violation or assertion_failed",
         _ON_WRITE),
    Rule("L-OUT-04", "error", "synthetic records are excluded from metrics and baseline promotion", _ON_WRITE),
    Rule("L-OUT-05", "error", "one checkpoint_outcome per (run_id, checkpoint_id); synthetic outcomes kept apart",
         _ON_WRITE),
    Rule("L-OUT-06", "error", "a record carries the current session's paper_assertion of an allowlisted account",
         _ON_WRITE),
    Rule("L-OUT-07", "error", "every scoped checkpoint of a finished run has exactly one record", _ON_WRITE),
    Rule("L-DEC-01", "error", "decided_by is a human login of the roster, the PR author or the Jira resolver", _ON_WRITE),
    Rule("L-DEC-02", "error", "ticket present at write for mark_intended, fixed_in_build and a defect reject_baseline",
         _ON_WRITE),
    Rule("L-DEC-03", "error", "category from taxonomy.yaml; phash 16 hex; hamming_tolerance within 8 (6 on tier 1); bbox_q4 "
         "with the exact fallback", _ON_WRITE),
    Rule("L-DEC-04", "error", "no noise, mask or floor exception on a money cell; no mask over the panel budget", _ON_WRITE),
    Rule("L-DEC-05", "error", "approve_baseline only from a complete, non-synthetic, non-PR run; not by a tier-1 author",
         _ON_WRITE),
    Rule("L-DEC-06", "error", "two-person rule for a PR author's tier-1 or tier-2 mark_intended", _ON_WRITE),
    Rule("L-DEC-07", "error", "a waiver of invariant_violation or assertion_failed expires within 30 days", _ON_WRITE),
    Rule("L-DEC-08", "error", "fixed_in_build names a newer build; the finding closes or reopens on later runs", _ON_WRITE),
    Rule("L-XREF-01", "warn", "every surface is reached by a census workspace or a flow checkpoint"),
    Rule("L-XREF-02", "error", "every tier-1 surface has a flow with assertions and a regions file with a money cell"),
    Rule("L-XREF-03", "error", "masked_live checkpoints have a rule or mask region; frozen ones no must_change_within_s"),
    Rule("L-XREF-04", "error", "checkpoint ids referenced by regions files, mutants and calibration files are defined or "
         "retired"),
    Rule("L-XREF-05", "warn", "critical checkpoints have behavioral mutants; tier-1 checkpoints a calibration for the "
         "canonical calibration key (error after the first Calibration run)", _PENDING),
    Rule("L-XREF-06", "error", "map and registry agree; manifests reference existing surfaces and primitives"),
    Rule("L-XREF-07", "error", "no file contains a credential; vault references only"),
    Rule("L-XREF-08", "error", "taxonomy.yaml equals the harness taxonomy generated from the spec"),
    Rule("L-XREF-09", "error", "schemas/*.schema.json regenerate identically from the notation"),
    Rule("L-XREF-10", "error", "changed checkpoint ids, templates or rects carry a rename or recalibrate marker",
         "needs the pull request's diff and the approved checkpoints"),
    Rule("L-XREF-11", "error", "every checkpoint id matches the 4.1 grammar and names a registered surface; quarantined "
         "flows exist"),
    Rule("L-XREF-12", "error", "every 12.8 starter popup present unless retired; M01 to M33 equal their 12.5 rows; further "
         "ids in sequence"),
    Rule("L-XREF-13", "error", "deny controls and policy/explorer_deny_list.yaml targets exist, detect, and are named by "
         "the nightly test", _PENDING),
    Rule("L-XREF-14", "error", "policy/explorer_budget.yaml equals 6.8 unless the Explorer owner approved"),
    Rule("L-XREF-15", "error", "phase0/branches.yaml names one branch per unknown; dependent files agree"),
    Rule("L-XREF-16", "error", "run manifest carries the signed authorization reference", _RUN_START),
    Rule("L-XREF-17", "error", "gateway/default_order_hotkeys.yaml exists; chords parse and do not repeat", _PENDING),
    Rule("L-XREF-18", "error", "gateway/dismiss_allowlist.yaml labels are exact, never send-like; Edit only when observed",
         _PENDING),
    Rule("L-XREF-19", "error", "policy/egress_allowlist.yaml: exact hosts with 6.10 purposes, signed, covering the model "
         "and reporting hosts", _PENDING),
    Rule("L-XREF-20", "error", "the authorization in force: document and record, hash, three signatures, accounts, rigs, "
         "endpoint, rate caps, send branch", _PENDING),
    Rule("L-XREF-21", "error", "policy/reporting.yaml: dated mode history, Jira keys, the four Slack channels, auto-file "
         "approval", _PENDING),
    Rule("L-XREF-22", "error", "policy/budgets.yaml within 11.4 unless approved; Explorer caps equal explorer_budget.yaml",
         _PENDING),
    Rule("L-XREF-23", "error", "policy/release_gate.yaml: unique trains, business days at least 1, RC cut before GA minus N",
         _PENDING),
    Rule("L-XREF-24", "error", "schedule/windows.yaml defines the four named windows; requires_window names one", _PENDING),
    Rule("L-XREF-25", "error", "ops/credential_rotation.yaml covers every allowlisted login; ended rotations name their "
         "validating run", _PENDING),
    Rule("L-XREF-26", "error", "CODEOWNERS routes flows, registry and every 4.16 file to its owner", _PENDING),
    Rule("L-XREF-27", "error", "soak.yaml has one budget per soak metric, none looser than 10.11 unless approved", _PENDING),
    Rule("L-XREF-28", "error", "rig/thinkorswim.vmoptions.template carries the heap, crash and Java2D flags", _PENDING),
    Rule("L-XREF-29", "error", "the reviewer version tuple in force has a passing eval report", _PENDING),
    Rule("L-XREF-30", "error", "registry invariants blocks name existing controls; a palette exists for every declared theme",
         _PENDING),
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
# operands of the rule assertion kind: (required, optional) per rule, generated from the 2.8.3 operand table through
# taxonomy.yaml `rule_operands` (SD-34)
RULE_OPERANDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    rule: load_taxonomy().rule_operands(rule) for rule in load_taxonomy().data["rule_operands"]}
JIRA = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
FLAG = re.compile(r"^[a-z0-9_.]+$")
PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
POP_ID = re.compile(r"^POP-[0-9]{2,}$")
MUTANT_ID = re.compile(r"^M[0-9]{2,}$")
ACCOUNT = re.compile(r"^D-[0-9A-Z]+$")
FUTURE_MONTH = re.compile(r"^/(?P<root>[A-Z0-9]+?)(?P<month>[FGHJKMNQUVXZ])(?P<year>[0-9]{2})$")
ENTER_KEYS = frozenset({"enter", "return", "space", "spacebar", "ctrl+enter"})
TARGET_FIELDS = ("target", "from", "to", "at")

# The keyword may sit inside a snake_case key (`db_password`, `client_secret`, S-19) and a quoted key's closing quote
# may sit before the separator (`"password": ...` in JSON, S-20); 4.15 L-XREF-07, 4.9 L-FIX-04.
_CRED_KEY = re.compile(r"(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd|secret)[A-Za-z0-9_]*['\"]?\s*[:=]\s*['\"]?"
                       r"(?P<v>[^\s'\",}\]#]+)")
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
    """(dx, dy, w, h) of a 4.8 rect; None unless all four are integers and the size is positive (S-23)."""
    r = _d(r)
    vals = [r.get(k) for k in ("dx", "dy", "w", "h")]
    ok = all(isinstance(v, int) and not isinstance(v, bool) for v in vals) and vals[2] > 0 and vals[3] > 0
    return tuple(vals) if ok else None  # type: ignore[return-value]


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


def _id_diff(got: set, want: Iterable[str]) -> str:
    want = set(want)
    return f"missing {sorted(want - got)}, extra {sorted(map(str, got - want))}"


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


# -- the linter ------------------------------------------------------------------------------------

_SCHEMA_OF: dict[str, tuple[str, str]] = {      # doc kind -> (schema name, rule id for its failures)
    "build": ("build", "L-BLD-00"), "manifest": ("manifest", "L-MAN-00"),
    "dependency_map": ("dependency-map", "L-DEP-00"), "surface": ("surface", "L-SRF-00"),
    "ladders": ("ladders", "L-SRF-03"), "workspace": ("workspace", "L-WSP-00"),
    "flow": ("flow", "L-FLW-00"), "renames": ("renames", "L-FLW-03"), "regions": ("regions", "L-RGN-00"),
    "redaction": ("redaction", "L-MSK-03"), "mask": ("mask", "L-MSK-00"), "fixture": ("fixture", "L-FIX-00"),
    "allowlist": ("allowlist", "L-FIX-00"), "margin_rules": ("margin-rules", "L-FIX-00"),
    "profile": ("profile", "L-FIX-05"), "configs_allowed": ("configs-allowed", "L-WSP-05"), "send_policy": ("send-policy", "L-FLW-08"),
    "explorer_budget": ("explorer-budget", "L-XREF-14"), "branches": ("branches", "L-XREF-15"),
    "quarantine": ("quarantine", "L-XREF-11"),
}
_CATALOGS = {"popups": ("popup#popup", "L-POP-00", "popups"), "mutants": ("mutant#mutant", "L-MUT-00", "mutants")}   # one catalog entry


def _rule_view(repo: Repo) -> Repo:
    """The repository as the rules see it: every document passed through `schemas.coerce_shapes`."""
    view = Repo(repo.root)
    for rel, doc in repo.docs.items():
        data = doc.data
        if doc.kind in _CATALOGS:
            name, _rule, key = _CATALOGS[doc.kind]
            if isinstance(data, list):
                data = [schemas.coerce_shapes(name, x) for x in data]
            elif isinstance(data, dict) and isinstance(data.get(key), list):
                data = {**data, key: [schemas.coerce_shapes(name, x) for x in data[key]]}
        elif doc.kind in _SCHEMA_OF:
            data = schemas.coerce_shapes(_SCHEMA_OF[doc.kind][0], data)
        view.docs[rel] = Doc(rel, doc.kind, data, doc.error)
    return view


class _Linter:
    """One lint pass over a loaded repository; `run()` returns the violations."""

    def __init__(self, repo: Repo, phase: int, today: date, flow_p50_s: dict[str, float] | None):
        self.raw = repo
        self.r = _rule_view(repo)
        self.phase = phase
        self.today = today
        self.now = datetime.combine(today, time(0), tzinfo=timezone.utc)
        self.p50 = flow_p50_s or {}
        self.tax = load_taxonomy()
        self.out: list[Violation] = []
        self._index()

    # -- reporting -------------------------------------------------------------------------------

    def add(self, rule: str, file: str, message: str, path: str = "", severity: str | None = None) -> None:
        self.out.append(Violation(file, rule, severity or RULES[rule].severity, path, message))

    def _phase_sev(self, until_phase: int) -> str:
        return "warn" if self.phase < until_phase else "error"

    # -- index -----------------------------------------------------------------------------------

    def _index(self) -> None:
        r = self.r
        self.surfaces: dict[str, dict] = {}
        self.surface_file: dict[str, str] = {}
        self.controls: dict[str, tuple[str, dict]] = {}
        for doc in r.of("surface"):
            sid = _d(doc.data).get("id")
            if isinstance(sid, str) and sid not in self.surfaces:
                self.surfaces[sid] = doc.data
                self.surface_file[sid] = doc.rel
                for c in _l(doc.data.get("controls")):
                    if isinstance(_d(c).get("id"), str):
                        self.controls.setdefault(f"{sid}.{c['id']}", (sid, c))
        dm = r.one("dependency_map")
        self.depmap_file = dm.rel if dm else "registry/dependency-map.yaml"
        self.depmap = _d(dm.data) if dm else {}
        self.primitives = {p["id"]: p for p in map(_d, _l(self.depmap.get("primitives"))) if isinstance(p.get("id"), str)}
        self.edges = [_d(e) for e in _l(self.depmap.get("edges"))]
        self.workspaces: dict[str, tuple[str, dict]] = {}
        for doc in r.of("workspace"):
            wid = _d(doc.data).get("id")
            if isinstance(wid, str):
                self.workspaces.setdefault(wid, (doc.rel, doc.data))
        self.flows: dict[str, Doc] = {}
        for doc in r.of("flow"):
            fid = _d(doc.data).get("flow_id")
            if isinstance(fid, str):
                self.flows.setdefault(fid, doc)
        self.fixtures = {d.data["fixture_id"]: d for d in r.of("fixture") if isinstance(_d(d.data).get("fixture_id"), str)}
        self.profiles = {d.data["id"]: d.data for d in r.of("profile") if isinstance(_d(d.data).get("id"), str)}
        self.builds: dict[str, dict] = {}
        self.pairs: list[tuple[str, dict]] = []
        for doc in r.of("build"):
            b, p = _d(doc.data).get("build"), _d(doc.data).get("build_pair")
            if isinstance(b, dict) and isinstance(b.get("build_id"), str):
                self.builds.setdefault(b["build_id"], b)
            if isinstance(p, dict):
                self.pairs.append((doc.rel, p))
        self.regions: dict[str, Doc] = {d.rel[len("regions/"):-len(".yaml")]: d for d in r.of("regions")}
        self.masks: dict[tuple[str, str], Doc] = {}
        for doc in r.of("mask"):
            m = _d(doc.data)
            self.masks.setdefault((_s(m.get("checkpoint_id")), _s(m.get("mask_id"))), doc)
        b = r.one("branches")
        # 4.16: each unknown is a branch_choice whose `branch` is A, B or undecided; the rules read that letter
        self.branches = {k: (v.get("branch") if isinstance(v, dict) else v) for k, v in _d(b.data if b else {}).items()}
        sp = r.one("send_policy")
        self.send_policy = _d(sp.data) if sp else {}
        eb = r.one("explorer_budget")
        self.explorer_budget = _d(eb.data) if eb else {}
        rn = r.one("renames")
        self.renames = [_d(x) for x in _l(_d(rn.data).get("renames"))] if rn else []
        self.renames_file = rn.rel if rn else "flows/RENAMES.yaml"
        self.retired = {x.get("old") for x in self.renames if x.get("new") == "retired"}
        red = r.one("redaction")
        self.redaction = [_d(x) for x in _l(_d(red.data).get("regions"))] if red else []
        self.ladder_sizes = self._ladder_sizes()
        self.defined: dict[str, CheckpointDef] = {}
        self._define_checkpoints()

    def _ladder_sizes(self) -> list[tuple[int, int]]:
        doc = self.r.one("ladders")
        sizes: list[tuple[int, int]] = []
        for lad in map(_d, _l(_d(doc.data).get("ladders")) if doc else []):
            for st in map(_d, _l(lad.get("steps"))):
                w, h = st.get("w"), st.get("h")
                if isinstance(w, int) and isinstance(h, int) and (w, h) not in sizes:
                    sizes.append((w, h))
        if not doc:
            sizes = list(dict.fromkeys(LADDER_S + LADDER_F_GRID + ((240, 160),)))
        return sizes

    def _define_checkpoints(self) -> None:
        for sid, s in self.surfaces.items():
            file, mode, tier = self.surface_file[sid], s.get("data_mode"), s.get("tier")
            census = _d(s.get("census"))
            if census.get("enabled", True):
                dims = [(k, _l(v)) for k, v in _d(census.get("dimensions")).items()]
                combos = list(itertools.product(*[[f"{k}={v}" for v in vals] for k, vals in dims])) if dims else [()]
                for ws in _l(census.get("workspaces")):
                    for combo in combos:
                        cid = f"census/{sid}/{ws}" + (f"/{','.join(combo)}" if combo else "")
                        self.defined.setdefault(cid, CheckpointDef(cid, "census", sid, mode, tier, file))
            if s.get("resizable") is True:
                for host in _l(s.get("hosts")):
                    for w, h in self.ladder_sizes:
                        cid = f"sweep/{sid}/{host}/{w}x{h}"
                        self.defined.setdefault(cid, CheckpointDef(cid, "sweep", sid, mode, tier, file))
            for sp in map(_d, _l(s.get("splitters"))):
                fractions = [f for f in (_l(sp.get("fractions")) or SPLIT_FULL) if _num(f) is not None]
                for f in fractions:
                    if f in (0.0, 1.0) and not sp.get("collapsible"):
                        continue
                    cid = f"split/{sid}/{sp.get('id')}/{float(f):.2f}"
                    self.defined.setdefault(cid, CheckpointDef(cid, "split", sid, mode, tier, file))
        for fid, doc in self.flows.items():
            for st in self._seq(doc.data):
                if st.get("type") == "checkpoint" and isinstance(st.get("checkpoint_id"), str):
                    sid = _s(st.get("surface_id"))
                    tier = st.get("tier", _d(self.surfaces.get(sid)).get("tier"))
                    cid = st["checkpoint_id"]
                    self.defined.setdefault(cid, CheckpointDef(cid, "flow", sid, _s(st.get("data_mode")), tier, doc.rel, fid))

    @staticmethod
    def _seq(flow: Any) -> list[dict]:
        """Steps then teardown: the order a flow runs in after its preconditions."""
        f = _d(flow)
        return [_d(x) for x in _l(f.get("steps")) + _l(f.get("teardown"))]

    @staticmethod
    def _all_steps(flow: Any) -> list[tuple[str, dict]]:
        f = _d(flow)
        return [(sec, _d(x)) for sec in ("normalize", "preconditions", "steps", "teardown") for x in _l(f.get(sec))]

    def region_map(self, cid: str) -> dict[str, dict]:
        """Region id -> region of the regions file of a checkpoint (empty when there is none)."""
        doc = self.regions.get(cid)
        return {x["id"]: x for x in map(_d, _l(_d(doc.data).get("regions") if doc else [])) if isinstance(x.get("id"), str)}

    def expand(self, spec: Any) -> set[str]:
        """Registry ids an edge's surface names: an id, a `<prefix>.*` glob or `*` (4.4)."""
        if spec == "*":
            return set(self.surfaces)
        if isinstance(spec, str) and spec.endswith(".*"):
            return {s for s in self.surfaces if s.startswith(spec[:-1])}
        return {spec} if spec in self.surfaces else set()

    def rungs(self, target: str) -> int:
        """How many locator rungs a target has: the template, plus JAB (branch A only), plus OCR text."""
        ctl = self.controls.get(target)
        if not ctl:
            return 0
        c = ctl[1]
        return (1 if c.get("template") else 0) + (1 if c.get("jab") and self.branches.get("jab") == "A" else 0) \
            + (1 if c.get("ocr_text") else 0)

    def png(self, rel: Any) -> tuple[str | None, int, int, int]:
        """(error, width, height, bytes) of a repository PNG."""
        if not isinstance(rel, str) or not self.r.exists(rel):
            return (f"{rel} does not exist", 0, 0, 0)
        p = self.r.path(rel)
        try:
            with Image.open(p) as im:
                fmt, (w, h) = im.format, im.size
        except (OSError, ValueError) as exc:
            return (f"{rel} is not an image: {exc}", 0, 0, 0)
        if fmt != "PNG":
            return (f"{rel} is {fmt}, not PNG", w, h, 0)
        return (None, w, h, p.stat().st_size)

    def hosted(self) -> set[str]:
        """The derived hosted-surface list of the tier A workspaces (choice 4)."""
        out: set[str] = set()
        for _rel, w in self.workspaces.values():
            if w.get("tier") == "A":
                out |= {x for x in _l(w.get("gadgets_present")) if isinstance(x, str)}
                out |= {_d(p).get("surface") for p in _l(w.get("active_trader_panels")) if isinstance(_d(p).get("surface"), str)}
        grew = True
        while grew:
            more = {sid for sid, s in self.surfaces.items() if s.get("parent") in out} - out
            out |= more
            grew = bool(more)
        return out

    def order_surface(self, sid: str) -> bool:
        return "OrderTicketCore" in _l(_d(self.surfaces.get(sid)).get("primitives"))

    # -- run -------------------------------------------------------------------------------------

    def run(self) -> list[Violation]:
        for check in (self.check_schemas, self.check_builds, self.check_manifests, self.check_depmap,
                      self.check_surfaces, self.check_ladders, self.check_workspaces, self.check_flows,
                      self.check_renames, self.check_regions, self.check_masks, self.check_fixtures,
                      self.check_popups, self.check_mutants, self.check_fingerprint_schema, self.check_xref):
            check()
        return sorted(set(self.out))

    # -- schemas (4.1) ---------------------------------------------------------------------------

    def check_schemas(self) -> None:
        for doc in self.raw.docs.values():
            if doc.kind in _CATALOGS:
                name, rule, key = _CATALOGS[doc.kind]
                if doc.error:
                    self.add(rule, doc.rel, doc.error)
                    continue
                items = doc.data if isinstance(doc.data, list) else _d(doc.data).get(key)
                if not isinstance(items, list):
                    self.add(rule, doc.rel, f"must be a list of {name} entries (4.10, 4.11)")
                    continue
                for i, item in enumerate(items):
                    for loc, msg in schemas.validate(name, item):
                        self.add(rule, doc.rel, msg, f"[{i}]" + (f".{loc}" if loc else ""))
                continue
            if doc.kind not in _SCHEMA_OF:
                continue
            name, rule = _SCHEMA_OF[doc.kind]
            if doc.error:
                self.add(rule, doc.rel, doc.error)
                continue
            for loc, msg in schemas.validate(name, doc.data):
                self.add(rule, doc.rel, msg, loc)
            if doc.kind in ("regions", "redaction"):        # 4.8 rect {dx, dy, w, h}: an offset and a size
                for i, x in enumerate(_l(_d(doc.data).get("regions"))):
                    rect = _d(_d(x).get("rect"))
                    if any(isinstance(rect.get(k), int) and not isinstance(rect.get(k), bool) and rect[k] <= 0 for k in ("w", "h")):
                        self.add(rule, doc.rel, "rect w and h are positive sizes", f"regions[{i}].rect")

    # -- 4.2 builds ------------------------------------------------------------------------------

    def check_builds(self) -> None:
        seen: dict[str, str] = {}
        for doc in self.r.of("build"):
            b = _d(_d(doc.data).get("build"))
            if not b:
                continue
            bid, ch, sha = b.get("build_id"), b.get("channel"), _s(b.get("commit_sha"))
            if bid in seen:
                self.add("L-BLD-01", doc.rel, f"build_id {bid} also defined in {seen[bid]}", "build.build_id")
            seen.setdefault(bid, doc.rel)
            want = f"{ch}-{b.get('version_string')}-{sha[:7]}"
            if bid != want:
                self.add("L-BLD-01", doc.rel, f"build_id {bid} must be {want}", "build.build_id")
            method = b.get("install_method")
            if method == "artifact" and not _d(b.get("artifact")).get("sha256"):
                self.add("L-BLD-02", doc.rel, "install_method artifact requires artifact.sha256", "build.artifact")
            if method == "snapshot" and not b.get("snapshot_id"):
                self.add("L-BLD-02", doc.rel, "install_method snapshot requires snapshot_id", "build.snapshot_id")
            for f in ("branch", "merge_base_sha"):
                if ch == "branch" and not b.get(f):
                    self.add("L-BLD-03", doc.rel, f"branch builds carry {f}", f"build.{f}")
                if ch != "branch" and f in b:
                    self.add("L-BLD-03", doc.rel, f"{ch} builds do not carry {f}", f"build.{f}")
            if ("released_at" in b) != (ch == "released"):
                self.add("L-BLD-06", doc.rel, "released_at is present if and only if channel is released", "build.released_at")
        table = {
            "pr": ("merge_base_main", "branch", "main"), "merge": ("previous_main", "main", "main"),
            "rc": ("last_released", "rc", "released"), "nightly": ("same_build", None, None),
            "calibration": ("same_build", None, None),
        }
        for rel, p in self.pairs:
            cand, base, rule, rt = p.get("candidate"), p.get("base"), p.get("base_rule"), p.get("run_type")
            if rt in table:
                want_rule, want_c, want_b = table[rt]
                if rule != want_rule:
                    self.add("L-BLD-04", rel, f"run_type {rt} requires base_rule {want_rule}, not {rule}", "build_pair.base_rule")
                cc = _d(self.builds.get(cand)).get("channel", _channel(cand))
                bc = _d(self.builds.get(base)).get("channel", _channel(base))
                if want_c and cc != want_c:
                    self.add("L-BLD-04", rel, f"run_type {rt} requires a {want_c} candidate, not {cc}", "build_pair.candidate")
                if want_b and bc != want_b:
                    self.add("L-BLD-04", rel, f"run_type {rt} requires a {want_b} base, not {bc}", "build_pair.base")
                if rt == "pr":
                    mb = _s(_d(self.builds.get(cand)).get("merge_base_sha"))
                    base_sha = _s(_d(self.builds.get(base)).get("commit_sha"))
                    if not mb:
                        self.add("L-BLD-04", rel, f"candidate {cand} has no merge_base_sha in builds/", "build_pair.candidate")
                    elif (base_sha and base_sha != mb) or (not base_sha and not mb.startswith(_short_sha(base))):
                        self.add("L-BLD-04", rel, f"base {base} is not the candidate's merge base {mb[:7]}", "build_pair.base")
                if rt == "rc" and not p.get("lineage_baseline_set"):
                    self.add("L-BLD-04", rel, "rc pairs carry lineage_baseline_set", "build_pair.lineage_baseline_set")
            if cand == base and rule != "same_build":
                self.add("L-BLD-05", rel, f"candidate equals base ({cand}) without base_rule same_build", "build_pair")

    # -- 4.3 manifests ---------------------------------------------------------------------------

    def check_manifests(self) -> None:
        for doc in self.r.of("manifest"):
            m = _d(doc.data)
            entries = _l(m.get("entries"))
            if not entries and not m.get("empty_reason"):
                self.add("L-MAN-06", doc.rel, "entries is empty and empty_reason is missing", "entries")
            cand = _s(m.get("candidate"))
            if (m.get("source") == "union" or _d(self.builds.get(cand)).get("channel", _channel(cand)) in ("main", "rc")) \
                    and not m.get("approved_by"):
                self.add("L-MAN-06", doc.rel, "merge and rc manifests carry approved_by", "approved_by")
            for i, e in enumerate(map(_d, entries)):
                at = f"entries[{i}]"
                for s in _l(e.get("declared_surfaces")):
                    if s not in self.surfaces:
                        self.add("L-MAN-01", doc.rel, f"declared surface {s} is not in the Surface Registry", f"{at}.declared_surfaces")
                for p in _l(e.get("touched_primitives")):
                    if p not in self.primitives:
                        self.add("L-MAN-02", doc.rel, f"touched primitive {p} is not in the dependency map", f"{at}.touched_primitives")
                ev, summary = _s(e.get("expected_visual")).strip(), _s(e.get("summary")).strip()
                if len(ev) < 20 or ev == summary:
                    self.add("L-MAN-03", doc.rel, "expected_visual needs at least 20 characters and must differ from summary", f"{at}.expected_visual")
                if "mock_image" in e:
                    err, w, h, size = self.png(e.get("mock_image"))
                    if err or w > 1920 or h > 1080 or size > 5 * 1024 * 1024:
                        self.add("L-MAN-04", doc.rel, err or f"mock is {w}x{h}, {size} bytes; at most 1920x1080 and 5 MB", f"{at}.mock_image")
                tickets = _l(e.get("tickets"))
                if not tickets:
                    self.add("L-MAN-05", doc.rel, "an entry carries at least one ticket", f"{at}.tickets")
                for t in tickets:
                    if not (isinstance(t, str) and JIRA.match(t)):
                        self.add("L-MAN-05", doc.rel, f"ticket {t!r} does not match ^[A-Z][A-Z0-9]+-[0-9]+$", f"{at}.tickets")
                names = [_d(f).get("name") for f in _l(e.get("feature_flags"))]
                for n in names:
                    if not (isinstance(n, str) and FLAG.match(n)):
                        self.add("L-MAN-08", doc.rel, f"feature flag name {n!r} does not match ^[a-z0-9_.]+$", f"{at}.feature_flags")
                for n in sorted({n for n in names if isinstance(n, str) and names.count(n) > 1}):
                    self.add("L-MAN-08", doc.rel, f"feature flag {n} appears twice", f"{at}.feature_flags")
                touched = set(_l(e.get("touched_primitives")))
                if not _l(e.get("changed_paths")):
                    for s in _l(e.get("declared_surfaces")):
                        if s in self.surfaces and not any(ed.get("primitive") in touched and s in self.expand(ed.get("surface")) for ed in self.edges):
                            self.add("L-MAN-07", doc.rel, f"declared surface {s} consumes no touched primitive and changed_paths is empty", f"{at}.declared_surfaces")

    # -- 4.4 dependency map ----------------------------------------------------------------------

    def check_depmap(self) -> None:
        f = self.depmap_file
        if not self.r.one("dependency_map"):
            self.add("L-DEP-00", f, "registry/dependency-map.yaml is missing")
            return
        seen: set[str] = set()
        for i, p in enumerate(map(_d, _l(self.depmap.get("primitives")))):
            pid, at = p.get("id"), f"primitives[{i}]"
            if pid in seen:
                self.add("L-DEP-01", f, f"primitive id {pid} is not unique", f"{at}.id")
            seen.add(pid)
            if not (isinstance(pid, str) and PASCAL.match(pid)):
                self.add("L-DEP-01", f, f"primitive id {pid!r} is not PascalCase", f"{at}.id")
            pkgs = _l(p.get("source_packages"))
            if not pkgs:
                self.add("L-DEP-01", f, "a primitive has at least one source_packages entry", f"{at}.source_packages")
            for g in pkgs:
                if isinstance(g, str) and g.startswith("TBD."):
                    self.add("L-DEP-01", f, f"{g} is the unfilled 12.9 placeholder; fill it from the Phase 0 jar listing (11.8)",
                             f"{at}.source_packages", self._phase_sev(1))
        pairs: set[tuple[Any, Any]] = set()
        for i, e in enumerate(self.edges):
            at, prim, surf = f"edges[{i}]", e.get("primitive"), e.get("surface")
            p = self.primitives.get(prim)
            if p is None:
                self.add("L-DEP-02", f, f"edge primitive {prim} is not in primitives", f"{at}.primitive")
            if surf == "*":
                if p is not None and not p.get("global"):
                    self.add("L-DEP-02", f, f'surface "*" is allowed only for global primitives, not {prim}', f"{at}.surface")
            elif isinstance(surf, str) and surf.endswith(".*"):
                if not self.expand(surf):
                    self.add("L-DEP-02", f, f"glob {surf} matches no registry id", f"{at}.surface")
            elif surf not in self.surfaces:
                self.add("L-DEP-02", f, f"surface {surf} is not in the Surface Registry", f"{at}.surface")
            src, conf = e.get("source"), _num(e.get("confidence"))
            if src == "declared" and conf != 1.0:
                self.add("L-DEP-03", f, "declared edges have confidence 1.0", f"{at}.confidence")
            if src == "static_analysis":
                if not e.get("evidence"):
                    self.add("L-DEP-03", f, "static_analysis edges carry evidence", f"{at}.evidence")
                if conf is None or not 0 < conf <= 1:
                    self.add("L-DEP-03", f, "static_analysis confidence is in (0, 1]", f"{at}.confidence")
                if conf == 1.0 and not e.get("confirmed_from_source"):
                    self.add("L-DEP-03", f, "confidence 1.0 on a static_analysis edge requires confirmed_from_source", f"{at}.confidence")
            if e.get("confirmed_from_source") and not e.get("confirmed_build"):
                self.add("L-DEP-03", f, "confirmed_from_source requires confirmed_build", f"{at}.confirmed_build")
            key = (prim, surf)
            if key in pairs:
                self.add("L-DEP-04", f, f"duplicate edge {prim} -> {surf}", at)
            pairs.add(key)
            lv = _ts(e.get("last_verified"))
            if lv and self.now - lv > timedelta(days=90):
                self.add("L-DEP-07", f, f"last_verified {e.get('last_verified')} is older than 90 days", f"{at}.last_verified")
        for sid, s in self.surfaces.items():
            for prim in _l(s.get("primitives")):
                if prim in self.primitives and not any(e.get("primitive") == prim and sid in self.expand(e.get("surface")) for e in self.edges):
                    self.add("L-DEP-05", f, f"{sid} lists {prim} but no edge {prim} -> {sid} exists")
            if not any(sid in self.expand(e.get("surface")) for e in self.edges):
                self.add("L-DEP-06", f, f"surface {sid} has no incoming edge")
        for i, e in enumerate(self.edges):
            prim, surf = e.get("primitive"), e.get("surface")
            if surf == "*" or _d(self.primitives.get(prim)).get("global"):
                continue
            for sid in sorted(self.expand(surf)):
                if prim not in _l(self.surfaces[sid].get("primitives")):
                    self.add("L-DEP-05", f, f"edge {prim} -> {surf} but {sid} does not list {prim}", f"edges[{i}]")
        ladder = "trade.active-trader.ladder"
        if not any(e.get("primitive") == "FlexibleGrid" and (e.get("surface") == ladder or
                   (isinstance(e.get("surface"), str) and e["surface"].endswith(".*") and ladder.startswith(e["surface"][:-1])))
                   for e in self.edges):
            self.add("L-DEP-08", f, "the edge FlexibleGrid -> trade.active-trader.ladder is missing (Phase 1 exit scenario)")

    # -- 4.5 surfaces ----------------------------------------------------------------------------

    def check_surfaces(self) -> None:
        seen: dict[str, str] = {}
        smoke = []
        hosted = self.hosted()
        jab_a = self.branches.get("jab") == "A"
        for doc in self.r.of("surface"):
            s, f = _d(doc.data), doc.rel
            sid = s.get("id")
            if not (isinstance(sid, str) and is_slug(sid)):
                self.add("L-SRF-01", f, f"id {sid!r} is not a dotted, hyphenated slug", "id")
                continue
            if sid in seen:
                self.add("L-SRF-01", f, f"id {sid} is also defined in {seen[sid]}", "id")
                continue
            seen[sid] = f
            if Path(f).name != f"{sid}.yaml":
                self.add("L-SRF-01", f, f"file name must be {sid}.yaml", "id")
            if s.get("smoke") is True:
                smoke.append(sid)
            landmarks = [_d(x) for x in _l(s.get("landmarks"))]
            controls = [_d(x) for x in _l(s.get("controls"))]
            if s.get("tier") == 1:
                missing = [what for what, ok in (
                    ("owner", bool(s.get("owner"))), ("smoke: true", s.get("smoke") is True),
                    ("a must_see landmark", any(x.get("must_see") is True for x in landmarks)),
                    ("anti_anchors", bool(_l(s.get("anti_anchors")))), ("labels", bool(_l(s.get("labels")))),
                    ("a required control", any(c.get("required") is True for c in controls))) if not ok]
                if missing:
                    self.add("L-SRF-02", f, "tier 1 requires " + ", ".join(missing))
            hosts = _l(s.get("hosts"))
            splitters = [_d(x) for x in _l(s.get("splitters"))]
            if s.get("resizable") is True:
                if not hosts:
                    self.add("L-SRF-03", f, "resizable surfaces list their hosts", "hosts")
                if s.get("enumerable_by") != "P8" and not splitters:
                    self.add("L-SRF-03", f, "resizable is set only from probe P8 or a declared splitter", "resizable")
            if "size_ladder" in s:
                if s.get("size_ladder") not in self.ladder_ids():
                    self.add("L-SRF-03", f, f"size_ladder {s.get('size_ladder')} is not in registry/ladders.yaml", "size_ladder")
                if not s.get("ladder_reason"):
                    self.add("L-SRF-03", f, "size_ladder requires ladder_reason", "ladder_reason")
            tw = _d(s.get("target_window"))
            needs = s.get("kind") == "dialog" or tw.get("kind") in ("dialog", "detached", "popup")
            if needs and not tw.get("title_regex"):
                self.add("L-SRF-04", f, "dialog, detached and popup windows require target_window.title_regex", "target_window")
            if tw.get("kind") == "main" and "title_regex" in tw:
                self.add("L-SRF-04", f, "a main target window has no title_regex", "target_window.title_regex")
            if isinstance(tw.get("title_regex"), str):
                try:
                    re.compile(tw["title_regex"])
                except re.error as exc:
                    self.add("L-SRF-04", f, f"title_regex does not compile: {exc}", "target_window.title_regex")
            path = [_d(x) for x in _l(s.get("path"))]
            must_see = {x.get("id") for x in landmarks if x.get("must_see") is True}
            last = path[-1] if path else {}
            if last.get("type") != "wait_for" or _d(last.get("anchor")).get("landmark") not in must_see:
                self.add("L-SRF-05", f, "path ends with a wait_for on a must_see landmark of this surface", "path")
            for i, st in enumerate(path):
                for k in TARGET_FIELDS:
                    t = st.get(k)
                    if isinstance(t, str) and self.rungs(t) < 2:
                        why = "does not resolve to a registry control" if t not in self.controls else "resolves on fewer than two rungs"
                        self.add("L-SRF-05", f, f"path target {t} {why}", f"path[{i}].{k}")
            templates = [(f"landmarks[{i}].template", x.get("template")) for i, x in enumerate(landmarks)]
            templates += [(f"controls[{i}].template", x.get("template")) for i, x in enumerate(controls)]
            templates += [(f"splitters[{i}].template", x.get("template")) for i, x in enumerate(splitters)]
            for loc, t in templates:
                err, w, h, _ = self.png(t)
                if err or not (16 <= w <= 400 and 16 <= h <= 400):
                    self.add("L-SRF-06", f, err or f"template {t} is {w}x{h}; templates are 16x16 to 400x400", loc)
            for p in _l(s.get("primitives")):
                if p not in self.primitives:
                    self.add("L-SRF-07", f, f"primitive {p} is not in the dependency map", "primitives")
            if not _l(s.get("roots")):
                self.add("L-SRF-07", f, "roots is empty", "roots")
            ids = [c.get("id") for c in controls]
            for c in sorted({x for x in ids if ids.count(x) > 1}, key=str):
                self.add("L-SRF-09", f, f"control id {c} is not unique", "controls")
            for i, c in enumerate(controls):
                if "jab" in c and not jab_a:
                    self.add("L-SRF-09", f, "jab fields need phase0/branches.yaml jab: A", f"controls[{i}].jab")
            census = _d(s.get("census"))
            if census.get("enabled", True):
                if not _l(census.get("workspaces")):
                    self.add("L-SRF-10", f, "census.workspaces is empty while the census is enabled", "census.workspaces")
                for w in _l(census.get("workspaces")):
                    if w not in self.workspaces:
                        self.add("L-SRF-10", f, f"census workspace {w} is not in the corpus", "census.workspaces")
            for k, vals in _d(census.get("dimensions")).items():
                for v in _l(vals):
                    if not is_slug(str(v)):
                        self.add("L-SRF-10", f, f"dimension value {v!r} of {k} is not a slug", f"census.dimensions.{k}")
            parent = s.get("parent")
            if parent is not None:
                if parent not in self.surfaces:
                    self.add("L-SRF-11", f, f"parent {parent} is not in the registry", "parent")
                else:
                    chain, cur = {sid}, parent
                    while cur in self.surfaces and cur not in chain:
                        chain.add(cur)
                        cur = self.surfaces[cur].get("parent")
                    if cur in chain:
                        self.add("L-SRF-11", f, f"parent chain of {sid} has a cycle at {cur}", "parent")
            for i, x in enumerate(map(_d, _l(s.get("invariant_exceptions")))):
                exp = _ts(x.get("expires_at"))
                if not (x.get("owner") and x.get("reason")) or exp is None or exp <= self.now:
                    self.add("L-SRF-11", f, "an invariant exception carries owner, reason and a future expires_at",
                             f"invariant_exceptions[{i}]")
            for i, sp in enumerate(splitters):
                if sp.get("host") not in hosts:
                    self.add("L-SRF-13", f, f"splitter host {sp.get('host')} is not in hosts", f"splitters[{i}].host")
                for fr in _l(sp.get("fractions")):
                    if fr not in SPLIT_FULL:
                        self.add("L-SRF-13", f, f"fraction {fr} is not in the 2.5.2 set", f"splitters[{i}].fractions")
                    elif fr in (0.0, 1.0) and not sp.get("collapsible"):
                        self.add("L-SRF-13", f, f"fraction {fr} needs collapsible: true", f"splitters[{i}].fractions")
            if sid not in hosted:
                self.add("L-SRF-14", f, f"{sid} is hosted by no tier A workspace (gadgets_present or a hosted parent)")
            if s.get("data_mode") == "masked_live":
                bare = [c for c in self.defined.values() if c.surface == sid and c.kind != "flow" and not self._has_rule_or_mask(c.cid)]
                for c in bare:
                    self.add("L-SRF-08", f, f"masked_live checkpoint {c.cid} has no rule or mask region", "data_mode")
        if len(smoke) > 40:
            self.add("L-SRF-15", "registry/surfaces", f"{len(smoke)} surfaces carry smoke: true; the cap is 40")

    def _has_rule_or_mask(self, cid: str) -> bool:
        return any(x.get("type") in ("rule", "mask") for x in self.region_map(cid).values())

    def ladder_ids(self) -> set[str]:
        doc = self.r.one("ladders")
        return {_d(x).get("id") for x in _l(_d(doc.data).get("ladders"))} if doc else set()

    def check_ladders(self) -> None:
        doc = self.r.one("ladders")
        if not doc:
            self.add("L-SRF-03", "registry/ladders.yaml", "registry/ladders.yaml is missing (2.5.2 ladders S and F)")
            return
        data = _d(doc.data)
        by_name = {_d(x).get("name"): _d(x) for x in _l(data.get("ladders"))}
        want = {"S": [(w, h, "grid") for w, h in LADDER_S],
                "F": [(w, h, "grid") for w, h in LADDER_F_GRID] + [(w, h, "corner") for w, h in LADDER_F_CORNERS]
                + [(w, h, "ascending") for w, h in LADDER_F_ASCENDING]}
        for name, steps in want.items():
            lad = by_name.get(name)
            got = [(_d(x).get("w"), _d(x).get("h"), _d(x).get("part", "grid")) for x in _l(_d(lad).get("steps"))]
            if lad is None or got != steps:
                self.add("L-SRF-03", doc.rel, f"ladder {name} differs from 2.5.2 (steps, part and order)", "ladders")
        if _d(by_name.get("F")).get("corpus_sizes") is not True:
            self.add("L-SRF-03", doc.rel, "ladder F includes the corpus sizes (2.5.2)", "ladders")
        sets = _d(data.get("splitter_sets"))
        if tuple(_l(sets.get("full"))) != SPLIT_FULL or tuple(_l(sets.get("short"))) != SPLIT_SHORT:
            self.add("L-SRF-03", doc.rel, "splitter_sets differ from 2.5.2 (full 0.00..1.00, short 0.15, 0.50, 0.85)", "splitter_sets")

    # -- 4.6 workspaces --------------------------------------------------------------------------

    def check_workspaces(self) -> None:
        allowed_doc = self.r.one("configs_allowed")
        allowed = [{k: _d(c).get(k) for k in ("width", "height", "scale_percent", "theme", "font_size")}
                   for c in _l(_d(allowed_doc.data).get("configs") if allowed_doc else [])]
        personas: dict[str, list[str]] = {p: [] for p in PERSONAS}
        flows_by_ws: dict[str, set[str]] = {}
        for doc in self.flows.values():
            fl = _d(doc.data)
            flows_by_ws.setdefault(_s(fl.get("workspace")), set()).add(_s(fl.get("fixture")))
        for doc in self.r.of("workspace"):
            w, f = _d(doc.data), doc.rel
            file = w.get("file")
            if not (isinstance(file, str) and self.r.exists(file)):
                self.add("L-WSP-01", f, f"workspace file {file} does not exist", "file")
            elif file_sha256(self.r.path(file)) != w.get("file_sha256"):
                self.add("L-WSP-01", f, f"sha256 of {file} does not equal file_sha256", "file_sha256")
            tier = w.get("tier")
            if tier == "A":
                if w.get("persona") in personas:
                    personas[w["persona"]].append(f)
                else:
                    self.add("L-WSP-02", f, "tier A entries name their persona", "persona")
            if tier == "B":
                sub = w.get("saved_under_build")
                if not sub:
                    self.add("L-WSP-03", f, "tier B entries carry saved_under_build", "saved_under_build")
                elif _d(self.builds.get(sub)).get("channel", _channel(sub)) != "released":
                    self.add("L-WSP-03", f, f"saved_under_build {sub} is not a released build", "saved_under_build")
            if tier == "C":
                cells = _l(w.get("size_sweep_cells"))
                if not cells:
                    self.add("L-WSP-04", f, "tier C entries carry size_sweep_cells", "size_sweep_cells")
                for i, c in enumerate(map(_d, cells)):
                    if _d(self.surfaces.get(c.get("hosts"))).get("resizable") is not True:
                        self.add("L-WSP-04", f, f"sweep cell host {c.get('hosts')} is not a resizable surface", f"size_sweep_cells[{i}].hosts")
            dc = _d(w.get("display_config"))
            if {k: dc.get(k) for k in ("width", "height", "scale_percent", "theme", "font_size")} not in allowed:
                self.add("L-WSP-05", f, "display_config is not listed in configs/allowed.yaml", "display_config")
            panels = {_d(p).get("surface") for p in _l(w.get("active_trader_panels"))}
            for g in _l(w.get("gadgets_present")):
                if g not in self.surfaces:
                    self.add("L-WSP-06", f, f"gadget {g} is not in the registry", "gadgets_present")
                elif self.surfaces[g].get("kind") == "active_trader_panel" and g not in panels:
                    self.add("L-WSP-06", f, f"Active Trader surface {g} is missing from active_trader_panels (attestation A7)", "active_trader_panels")
            for p in panels:
                if p not in self.surfaces:
                    self.add("L-WSP-06", f, f"active_trader_panels surface {p} is not in the registry", "active_trader_panels")
            used = flows_by_ws.get(_s(w.get("id"))) or set(self.fixtures)
            for i, lg in enumerate(map(_d, _l(w.get("link_groups")))):
                ref = lg.get("symbol_ref")
                for fx in sorted(x for x in used if x):
                    fdoc = self.fixtures.get(fx)
                    syms = {_d(s).get("id") for s in _l(_d(fdoc.data if fdoc else None).get("symbols"))}
                    if ref not in syms:
                        self.add("L-WSP-07", f, f"symbol_ref {ref} is not a symbol of fixture {fx}", f"link_groups[{i}].symbol_ref")
                if not used - {""}:
                    self.add("L-WSP-07", f, f"no fixture defines symbol_ref {ref}", f"link_groups[{i}].symbol_ref")
        if self.r.of("workspace"):
            for p, files in personas.items():
                if len(files) != 1:
                    self.add("L-WSP-02", "corpus/workspaces", f"persona {p} has {len(files)} tier A entries; exactly one is required")

    # -- 4.7 flows -------------------------------------------------------------------------------

    def check_flows(self) -> None:
        seen_flow: dict[str, str] = {}
        seen_cp: dict[str, str] = {}
        for doc in self.r.of("flow"):
            fl, f = _d(doc.data), doc.rel
            fid = _s(fl.get("flow_id"))
            if fid in seen_flow:
                self.add("L-FLW-01", f, f"flow_id {fid} is also defined in {seen_flow[fid]}", "flow_id")
            seen_flow.setdefault(fid, f)
            self._flow_structure(fl, f)
            self._flow_targets(fl, f)
            self._flow_checkpoints(fl, f, fid, seen_cp)
            self._flow_assertions(fl, f)
            self._flow_safety(fl, f)
            self._flow_budget(fl, f, fid)

    def _flow_structure(self, fl: dict, f: str) -> None:
        ids = [st.get("id") for _sec, st in self._all_steps(fl)]
        for x in sorted({i for i in ids if ids.count(i) > 1}, key=str):
            self.add("L-FLW-01", f, f"step id {x} is not unique in the file", "steps")
        steps, teardown = [_d(x) for x in _l(fl.get("steps"))], [_d(x) for x in _l(fl.get("teardown"))]
        if not steps or steps[0].get("type") != "reset_input_state":
            self.add("L-FLW-01", f, "the first step is reset_input_state", "steps[0]")
        if not teardown or teardown[-1].get("type") != "reset_input_state":
            self.add("L-FLW-01", f, "the last teardown step is reset_input_state", "teardown")
        if fl.get("surface") not in self.surfaces:
            self.add("L-FLW-02", f, f"flow surface {fl.get('surface')} is not in the registry", "surface")
        if fl.get("surface") not in _l(fl.get("surfaces")):
            self.add("L-FLW-02", f, "the primary surface is listed in surfaces", "surfaces")
        for s in _l(fl.get("surfaces")):
            if s not in self.surfaces:
                self.add("L-FLW-02", f, f"surface {s} is not in the registry", "surfaces")
        if fl.get("workspace") not in self.workspaces:
            self.add("L-FLW-00", f, f"workspace {fl.get('workspace')} is not in the corpus", "workspace")
        if fl.get("fixture") not in self.fixtures:
            self.add("L-FLW-00", f, f"fixture {fl.get('fixture')} is not in fixtures/", "fixture")
        pre = [_d(x) for x in _l(fl.get("preconditions"))]
        checks = {p.get("check") for p in pre}
        for need in ("paper_mode", "no_modal"):
            if need not in checks:
                self.add("L-FLW-12", f, f"preconditions include {need}", "preconditions")
        if any(self.order_surface(s) for s in _l(fl.get("surfaces"))):
            if not any(p.get("check") == "working_orders" and p.get("expect") == 0 for p in pre):
                self.add("L-FLW-12", f, "order flows check working_orders with expect: 0 before the first step", "preconditions")
            if not any(t.get("type") == "teardown" and t.get("action") == "working_orders_check" and t.get("expect") == 0 for t in teardown):
                self.add("L-FLW-12", f, "order flows end with a working_orders_check teardown with expect: 0", "teardown")
        is_quarantine = f.startswith("flows/quarantine/")
        origin = _d(fl.get("origin"))
        if origin.get("kind") == "explorer":
            if not (origin.get("run_id") and origin.get("finding_ref")):
                self.add("L-FLW-14", f, "explorer flows carry origin.run_id and origin.finding_ref", "origin")
            if fl.get("class") != "explorer_candidate":
                self.add("L-FLW-14", f, "explorer flows keep class explorer_candidate until a human promotes them", "class")
            if not is_quarantine:
                self.add("L-FLW-14", f, "explorer flows live under flows/quarantine/", "origin")
        if is_quarantine and fl.get("class") != "explorer_candidate":
            self.add("L-FLW-14", f, "flows under flows/quarantine/ are class explorer_candidate", "class")

    def _flow_targets(self, fl: dict, f: str) -> None:
        cls = fl.get("class")
        for sec, st in self._all_steps(fl):
            at = f"{sec}[{st.get('id')}]"
            if st.get("type") == "secret" and cls != "login":
                self.add("L-FLW-06", f, "secret steps are allowed only in class login flows", at)
            if st.get("type") == "type_fixture" and isinstance(st.get("value"), str):
                if credential_hits(f"value: {st['value']}"):
                    self.add("L-FLW-06", f, "a value literal matches a credential pattern", f"{at}.value")
            if st.get("type") == "drag":
                if not (isinstance(st.get("steps"), int) and st["steps"] >= 10):
                    self.add("L-FLW-09", f, "drag steps is at least 10", f"{at}.steps")
                if not (isinstance(st.get("hold_ms"), int) and st["hold_ms"] <= 2000):
                    self.add("L-FLW-09", f, "drag hold_ms is at most 2000", f"{at}.hold_ms")
            if st.get("type") == "wheel" and st.get("notches") == 0:
                self.add("L-FLW-09", f, "wheel notches is not 0", f"{at}.notches")
            for k in TARGET_FIELDS:
                t = st.get(k)
                if not isinstance(t, str):
                    continue
                ctl = self.controls.get(t)
                if ctl is None:
                    self.add("L-FLW-02", f, f"target {t} is not a <surface>.<control> of the registry", f"{at}.{k}")
                    continue
                c = ctl[1]
                if c.get("deny") is True or c.get("id") == "auto-send-checkbox":
                    self.add("L-FLW-13", f, f"step targets {t}, a denied control", f"{at}.{k}")
                if self.rungs(t) < 2:
                    self.add("L-FLW-17", f, f"target {t} resolves on fewer than two rungs (template plus jab or ocr_text)", f"{at}.{k}")
                cancel_all = c.get("id") == "cancel-all" or _s(c.get("ocr_text")).strip().casefold() == "cancel all"
                if cancel_all and st.get("type") in ("click_named", "rclick", "dblclick"):
                    self.add("L-FLW-19", f, f"{st.get('type')} targets Cancel All; only the cancel_all teardown may", f"{at}.{k}")
        if cls == "login" and fl.get("agent_fallback") != "disabled":
            self.add("L-FLW-06", f, "login flows have agent_fallback: disabled", "agent_fallback")
        if cls == "paper_send_ok":
            sp = self.send_policy
            if not (sp.get("enabled") is True and sp.get("risk_owner") and sp.get("signature")):
                self.add("L-FLW-08", f, "class paper_send_ok needs policy/send_policy.yaml enabled: true with a risk-owner signature", "class")

    def _flow_checkpoints(self, fl: dict, f: str, fid: str, seen_cp: dict[str, str]) -> None:
        surfaces = _l(fl.get("surfaces"))
        cleared = False
        for st in self._seq(fl):
            if st.get("type") == "teardown" and st.get("action") in ("clear_ticket", "close_dialog", "cancel_all"):
                cleared = True
            if st.get("type") != "checkpoint":
                continue
            at = f"steps[{st.get('id')}]"
            for need in ("surface_id", "target_window", "data_mode"):
                if need not in st:
                    self.add("L-FLW-02", f, f"checkpoint {st.get('id')} carries {need}; nothing is inherited", at)
            sid = st.get("surface_id")
            if "surface_id" in st and sid not in self.surfaces:
                self.add("L-FLW-02", f, f"surface_id {sid} is not in the registry", f"{at}.surface_id")
            elif "surface_id" in st and sid not in surfaces:
                self.add("L-FLW-02", f, f"surface_id {sid} is not in the flow's surfaces", f"{at}.surface_id")
            cid = st.get("checkpoint_id")
            want = f"flow/{sid}/{fid}/{st.get('id')}"
            if cid != want:
                self.add("L-FLW-03", f, f"checkpoint_id must be {want}", f"{at}.checkpoint_id")
            if isinstance(cid, str):
                if cid in seen_cp:
                    self.add("L-FLW-03", f, f"checkpoint_id {cid} is also used in {seen_cp[cid]}", f"{at}.checkpoint_id")
                seen_cp.setdefault(cid, f)
            regions, mode = st.get("regions"), st.get("data_mode")
            if regions == "default":
                if mode != "frozen":
                    self.add("L-FLW-04", f, "regions: default (whole-surface diff) is allowed only with data_mode frozen", f"{at}.regions")
            elif not (isinstance(regions, str) and self.r.exists(regions)):
                self.add("L-FLW-04", f, f"regions file {regions} does not exist", f"{at}.regions")
            else:
                rdoc = self.r.docs.get(regions)
                if rdoc is None or _d(rdoc.data).get("checkpoint_id") != cid:
                    self.add("L-FLW-04", f, f"regions file {regions} belongs to another checkpoint", f"{at}.regions")
            if mode == "masked_live":
                rmap = self.region_map(regions[len("regions/"):-len(".yaml")]) if isinstance(regions, str) and regions.startswith("regions/") else {}
                if not any(x.get("type") in ("rule", "mask") for x in rmap.values()):
                    self.add("L-FLW-05", f, f"masked_live checkpoint {st.get('id')} references no rule or mask region", f"{at}.regions")
            if cleared and st.get("recapturable", True) is not False:
                self.add("L-FLW-16", f, f"checkpoint {st.get('id')} follows a clear_ticket, close_dialog or cancel_all and is recapturable: false", f"{at}.recapturable")
        ticket_surfaces = [s for s in surfaces if _d(self.surfaces.get(s)).get("kind") == "ticket_variant"]
        steps = [_d(x) for x in _l(fl.get("steps"))]
        for ts_ in ticket_surfaces:
            first = next((k for k, st in enumerate(steps) if st.get("type") in ("type_fixture", "click_named")
                          and _s(st.get("target")).startswith(ts_ + ".") and _s(st.get("target")) != f"{ts_}.symbol-box"), None)
            if first is not None and not any(st.get("type") == "checkpoint" and st.get("surface_id") == ts_ for st in steps[:first]):
                self.add("L-FLW-15", f, f"no pre-input checkpoint on {ts_} before step {steps[first].get('id')} (2.6.3)", "steps")

    def _cp_regions(self, st: dict) -> set[str] | None:
        reg = st.get("regions")
        if isinstance(reg, str) and reg.startswith("regions/") and reg.endswith(".yaml"):
            return set(self.region_map(reg[len("regions/"):-len(".yaml")]))
        return set() if reg == "default" else None

    def _flow_assertions(self, fl: dict, f: str) -> None:
        steps = [("steps", _d(x)) for x in _l(fl.get("steps"))] + [("teardown", _d(x)) for x in _l(fl.get("teardown"))]
        cps = [k for k, (_sec, st) in enumerate(steps) if st.get("type") == "checkpoint"]
        cp_by_id = {steps[k][1].get("id"): k for k in cps}
        all_regions: set[str] = set()
        for k in cps:
            all_regions |= self._cp_regions(steps[k][1]) or set()
        for k, (sec, st) in enumerate(steps):
            groups: list[tuple[str, list, set[str] | None, int]] = []
            if st.get("type") == "checkpoint":
                groups.append((f"{sec}[{st.get('id')}].expect", _l(st.get("expect")), self._cp_regions(st), k))
            else:
                items = _l(st.get("assertions")) if st.get("type") == "assert" else [st["verify"]] if isinstance(st.get("verify"), dict) else []
                if not items:
                    continue
                if sec == "teardown":
                    base = all_regions
                else:
                    before = [c for c in cps if c < k]
                    after = [c for c in cps if c > k]
                    pick = before[-1] if before else after[0] if after else None
                    base = self._cp_regions(steps[pick][1]) if pick is not None else set()
                groups.append((f"{sec}[{st.get('id')}]", items, base, k))
            for at, items, base, pos in groups:
                for a in map(_d, items):
                    self._assertion(f, at, a, base, pos, steps, cp_by_id)

    def _assertion(self, f: str, at: str, a: dict, base: set[str] | None, pos: int,
                   steps: list[tuple[str, dict]], cp_by_id: dict[Any, int]) -> None:
        loc = f"{at}[{a.get('id')}]"
        allowed = set(base or set())
        since = a.get("since_step")
        if a.get("kind") in ("changed_since_previous_step", "unchanged_since_previous_step") or since is not None:
            k = cp_by_id.get(since)
            if k is None or k >= pos:
                self.add("L-FLW-11", f, f"since_step {since} is not an earlier checkpoint", f"{loc}.since_step")
            else:
                allowed |= self._cp_regions(steps[k][1]) or set()
        region = a.get("region")
        if a.get("kind") != "rule":
            if region is None:
                self.add("L-FLW-11", f, f"assertion kind {a.get('kind')} needs a region", loc)
            elif region not in allowed:
                self.add("L-FLW-11", f, f"region {region} is not declared on the checkpoint", f"{loc}.region")
        else:
            rid = a.get("rule_id")
            if rid not in RULE_OPERANDS:
                self.add("L-FLW-11", f, f"rule_id {rid} is not one of R01 to R10 (2.8.3)", f"{loc}.rule_id")
                return
            req, opt = RULE_OPERANDS[rid]
            inputs = _d(a.get("inputs"))
            if not req <= set(inputs) or not set(inputs) <= req | opt:
                self.add("L-FLW-11", f, f"inputs of {rid} must be {sorted(req)}" + (f" plus optional {sorted(opt)}" if opt else ""), f"{loc}.inputs")
            for name, v in inputs.items():
                if isinstance(v, str) and v not in allowed:
                    self.add("L-FLW-11", f, f"operand {name} region {v} is not declared on the checkpoint", f"{loc}.inputs.{name}")

    def _confirm_and_send(self, target: Any) -> bool:
        """A target that resolves to Confirm and Send: control id `confirm-and-send` or OCR text "Confirm and Send"."""
        ctl = self.controls.get(_s(target))
        return ctl is not None and (ctl[1].get("id") == "confirm-and-send" or _s(ctl[1].get("ocr_text")).strip().casefold() == "confirm and send")

    def _flow_safety(self, fl: dict, f: str) -> None:
        seq = self._seq(fl)
        start = None
        for k, st in enumerate(seq):
            if self._confirm_and_send(st.get("target")):
                if st.get("order_stage") is not True:
                    self.add("L-FLW-07", f, f"step {st.get('id')} targets Confirm and Send without order_stage: true", f"steps[{st.get('id')}]")
                start = k if start is None else start
        if start is None:
            return
        closed = None
        for k in range(start + 1, len(seq)):
            st = seq[k]
            if st.get("type") == "teardown" and st.get("action") in ("close_dialog", "clear_ticket"):
                closed = k
                break
            keys = [_s(st.get("key")).casefold()] if st.get("type") == "press" else \
                [_s(x).casefold() for x in _l(st.get("keys"))] if st.get("type") == "key_chord" else []
            # a type_fixture (or secret) step that commits with Enter presses Enter too (6.2)
            if st.get("type") in ("type_fixture", "secret") and _s(st.get("commit")).casefold() == "enter":
                keys.append("enter")
            if any(x in ENTER_KEYS for x in keys):
                self.add("L-FLW-07", f, f"step {st.get('id')} presses Enter or Space while the order is staged", f"steps[{st.get('id')}]")
        if closed is None:
            self.add("L-FLW-07", f, "no close_dialog or clear_ticket teardown follows the order-stage step", "teardown")
        elif not any(st.get("type") == "assert" and any(_d(a).get("kind") == "absent" for a in _l(st.get("assertions"))) for st in seq[closed + 1:]):
            self.add("L-FLW-07", f, "no absent assertion proves the ticket empty after close_dialog or clear_ticket", "teardown")

    def _flow_budget(self, fl: dict, f: str, fid: str) -> None:
        b = _d(fl.get("budget"))
        dur, steps = b.get("max_duration_s"), b.get("max_steps")
        if fl.get("class") == "explorer_candidate":
            eb = self.explorer_budget
            max_steps = min(400, eb.get("max_actions", 400) if isinstance(eb.get("max_actions"), int) else 400)
            max_dur = min(5400, eb["max_minutes"] * 60 if isinstance(eb.get("max_minutes"), int) else 5400)
            if not (isinstance(steps, int) and steps <= max_steps):
                self.add("L-FLW-18", f, f"explorer_candidate max_steps is at most {max_steps}", "budget.max_steps")
            if not (isinstance(dur, int) and dur <= max_dur):
                self.add("L-FLW-18", f, f"explorer_candidate max_duration_s is at most {max_dur}", "budget.max_duration_s")
            return
        limit = 3 * self.p50[fid] if fid in self.p50 else 300
        if not (isinstance(dur, (int, float)) and dur <= limit):
            self.add("L-FLW-10", f, f"max_duration_s {dur} exceeds {limit:g} s (3 x measured p50, 5 minutes until measured; 11.2)", "budget.max_duration_s")

    def check_renames(self) -> None:
        for i, x in enumerate(self.renames):
            old, new = x.get("old"), x.get("new")
            if old in self.defined:
                self.add("L-FLW-03", self.renames_file, f"renamed or retired id {old} is still defined", f"renames[{i}].old")
            if new != "retired" and new not in self.defined:
                self.add("L-FLW-03", self.renames_file, f"rename target {new} is not a defined checkpoint (or 'retired')", f"renames[{i}].new")

    # -- 4.8 regions and masks -------------------------------------------------------------------

    def _cp_mode(self, cid: str) -> str | None:
        c = self.defined.get(cid)
        return c.data_mode if c else None

    def check_regions(self) -> None:
        for cid, doc in self.regions.items():
            rf, f = _d(doc.data), doc.rel
            if rf.get("checkpoint_id") != cid:
                self.add("L-RGN-01", f, f"file path must be regions/{rf.get('checkpoint_id')}.yaml", "checkpoint_id")
            if cid not in self.defined and cid not in self.retired:
                self.add("L-RGN-01", f, f"no flow, census, sweep or splitter definition produces {cid}")
            regions = [_d(x) for x in _l(rf.get("regions"))]
            ids = [x.get("id") for x in regions]
            for x in sorted({i for i in ids if ids.count(i) > 1}, key=str):
                self.add("L-RGN-01", f, f"region id {x} is not unique", "regions")
            landmarks = {_d(x).get("id") for x in _l(rf.get("landmarks"))}
            for i, x in enumerate(regions):
                if x.get("landmark") not in landmarks:
                    self.add("L-RGN-00", f, f"region landmark {x.get('landmark')} is not a landmark of this file", f"regions[{i}].landmark")
            for i, a in enumerate(regions):
                for b in regions[i + 1:]:
                    ra, rb = _rect(a.get("rect")), _rect(b.get("rect"))
                    if not (ra and rb) or a.get("landmark") != b.get("landmark") or a.get("type") == b.get("type"):
                        continue
                    if _overlap(ra, rb):
                        ok = (a.get("type") == "mask" and _inside(ra, rb)) or (b.get("type") == "mask" and _inside(rb, ra))
                        if not ok:
                            self.add("L-RGN-02", f, f"{a.get('id')} ({a.get('type')}) overlaps {b.get('id')} ({b.get('type')})", "regions")
            mode = self._cp_mode(cid)
            for i, x in enumerate(regions):
                at, t, rid = f"regions[{i}]", x.get("type"), x.get("id")
                if ("rule" in x) != (t == "rule"):
                    self.add("L-RGN-03", f, "rule is present if and only if type is rule", at)
                if ("mask_ref" in x) != (t == "mask"):
                    self.add("L-RGN-03", f, "mask_ref is present if and only if type is mask", at)
                elif t == "mask":
                    mdoc = self.masks.get((cid, _s(x.get("mask_ref"))))
                    if mdoc is None:
                        self.add("L-RGN-03", f, f"mask {x.get('mask_ref')} has no record under masks/{cid}/", f"{at}.mask_ref")
                    elif _d(mdoc.data).get("region_id") != rid:
                        self.add("L-RGN-03", f, f"mask {x.get('mask_ref')} names region {_d(mdoc.data).get('region_id')}, not {rid}", f"{at}.mask_ref")
                if ("fixture_dependency" in x) != (t == "freeze"):
                    self.add("L-RGN-03", f, "fixture_dependency is present if and only if type is freeze", at)
                fe = x.get("floor_exception")
                if fe is not None:
                    fe = _d(fe)
                    exp = _ts(fe.get("expires_at"))
                    if not all(fe.get(k) for k in ("approved_by", "reason", "ticket")) or exp is None or exp <= self.now:
                        self.add("L-RGN-05", f, "floor_exception carries approved_by, reason, ticket and a future expires_at", f"{at}.floor_exception")
                    if t != "diff":
                        self.add("L-RGN-05", f, "floor_exception is allowed on diff regions only", f"{at}.floor_exception")
                if x.get("money_cell") is True:
                    if t not in ("diff", "freeze"):
                        self.add("L-RGN-04", f, f"money cell {rid} is {t}; money cells are diff or freeze", at)
                    if "floor_exception" in x:
                        self.add("L-RGN-04", f, f"money cell {rid} carries a floor_exception", f"{at}.floor_exception")
                    c = self.defined.get(cid)
                    if c and c.kind == "flow" and rid not in self._asserted_regions(c):
                        self.add("L-RGN-04", f, f"money cell {rid} is not named by an assertion of {cid}", at)
                if t == "mask" and mode == "frozen" and _s(rid).rsplit(".", 1)[-1] not in FROZEN_MASK_SEGMENTS:
                    self.add("L-RGN-07", f, f"mask {rid} on a frozen checkpoint; only the clock, connection indicator, caret or parking rectangle", at)
            self._mask_budget(cid, f, regions)

    def _asserted_regions(self, c: CheckpointDef) -> set[str]:
        """Regions named by the checkpoint's expect entries and by assert steps before the next checkpoint."""
        doc = self.flows.get(c.flow or "")
        out: set[str] = set()
        seq = self._seq(doc.data if doc else {})
        k = next((i for i, st in enumerate(seq) if st.get("checkpoint_id") == c.cid), None)
        if k is None:
            return out
        assertions = list(_l(seq[k].get("expect")))
        for st in seq[k + 1:]:
            if st.get("type") == "checkpoint":
                break
            if st.get("type") == "assert":
                assertions += _l(st.get("assertions"))
        for a in map(_d, assertions):
            if isinstance(a.get("region"), str):
                out.add(a["region"])
            out |= {v for v in _d(a.get("inputs")).values() if isinstance(v, str)}
        return out

    def _mask_budget(self, cid: str, f: str, regions: list[dict]) -> None:
        doc = self.regions.get(cid)
        landmarks = {_d(x).get("id") for x in _l(_d(doc.data).get("landmarks") if doc else [])}
        masks = [x for x in regions if x.get("type") == "mask"]
        boxes = [x for x in self.redaction if x.get("landmark") in landmarks]
        # identity_field exempts a redaction box from the panel share only; no mask or box ever covers a money cell
        # (4.8 mask budget, S-21)
        for m in masks + boxes:
            rm = _rect(m.get("rect"))
            for x in regions:
                rx = _rect(x.get("rect"))
                if x.get("money_cell") is True and rm and rx and x.get("landmark") == m.get("landmark") and _overlap(rm, rx):
                    self.add("L-MSK-03", f, f"mask or redaction box {m.get('id')} covers money cell {x.get('id')} (budget 0 percent)")
        masks += [x for x in boxes if x.get("identity_field") is not True]
        for panel, budget in MASK_BUDGET.items():
            for lm in landmarks:
                members = [x for x in regions + masks if x.get("panel", "other") == panel and x.get("landmark") == lm and _rect(x.get("rect"))]
                masked = sum(_rect(x["rect"])[2] * _rect(x["rect"])[3] for x in masks if x in members)  # type: ignore[index]
                if not masked:
                    continue
                rects = [_rect(x["rect"]) for x in members]
                x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)  # type: ignore[index]
                x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)  # type: ignore[index]
                share = masked / max(1, (x1 - x0) * (y1 - y0))
                if share > budget:
                    self.add("L-MSK-03", f, f"{panel} panel is {share:.1%} masked; the budget is {budget:.0%} (mask budget, 4.8)")

    def check_masks(self) -> None:
        for doc in self.r.of("mask"):
            m, f = _d(doc.data), doc.rel
            if m.get("created_by") != "human":
                self.add("L-MSK-01", f, "created_by is human; the Reviewer may propose a mask, never create one", "created_by")
            exp = _ts(m.get("expires_at"))
            if exp is not None and exp <= self.now:
                self.add("L-MSK-02", f, f"mask expired at {m.get('expires_at')}; renew it with the latest A/A result or remove it", "expires_at")
            want = f"masks/{m.get('checkpoint_id')}/{m.get('mask_id')}.yaml"
            cid = _s(m.get("checkpoint_id"))
            if f != want:
                self.add("L-RGN-03", f, f"mask record path must be {want}")
            elif _d(self.region_map(cid).get(_s(m.get("region_id")))).get("mask_ref") != m.get("mask_id"):
                self.add("L-RGN-03", f, f"no mask region {m.get('region_id')} with mask_ref {m.get('mask_id')} in regions/{cid}.yaml", "region_id")

    # -- 4.9 fixtures ----------------------------------------------------------------------------

    def check_fixtures(self) -> None:
        al = self.r.one("allowlist")
        rigs = {_d(x).get("rig"): [a for a in _l(_d(x).get("account_ids"))] for x in _l(_d(al.data).get("rigs") if al else [])}
        if al:
            for i, x in enumerate(map(_d, _l(_d(al.data).get("rigs")))):
                for a in _l(x.get("account_ids")):
                    if not (isinstance(a, str) and ACCOUNT.match(a)):
                        self.add("L-FIX-01", al.rel, f"allowlist entry {a!r} is not an exact ^D-[0-9A-Z]+$ id (no patterns, globs or prefixes)", f"rigs[{i}].account_ids")
        elif self.r.of("fixture"):
            self.add("L-FIX-01", "gateway/allowlist.yaml", "gateway/allowlist.yaml is missing")
        send_enabled = self.send_policy.get("enabled") is True
        for doc in self.r.of("fixture"):
            fx, f = _d(doc.data), doc.rel
            accounts = [_d(a) for a in _l(fx.get("accounts"))]
            symbols = {_d(s).get("id"): _d(s) for s in _l(fx.get("symbols"))}
            ids = {a.get("account_id") for a in accounts}
            for i, a in enumerate(accounts):
                aid = a.get("account_id")
                if not (isinstance(aid, str) and ACCOUNT.match(aid)):
                    self.add("L-FIX-01", f, f"account_id {aid!r} does not match ^D-[0-9A-Z]+$", f"accounts[{i}].account_id")
                if aid not in rigs.get(fx.get("rig"), []):
                    self.add("L-FIX-01", f, f"account {aid} is not in gateway/allowlist.yaml for rig {fx.get('rig')}", f"accounts[{i}].account_id")
                if send_enabled and "cash_floor" in a:
                    self.add("L-FIX-02", f, "cash_floor is omitted when policy/send_policy.yaml is enabled", f"accounts[{i}].cash_floor")
            roles = [a.get("role") for a in accounts]
            if roles.count("golden") != 1 or roles.count("empty") > 1:
                self.add("L-FIX-02", f, f"exactly one golden and at most one empty account (found {roles.count('golden')} and {roles.count('empty')})", "accounts")
            for sid, s in symbols.items():
                if s.get("asset") == "future" and set(_l(s.get("purpose"))) & {"ticket", "ladder"}:
                    rd = _date(s.get("roll_date"))
                    if not FUTURE_MONTH.match(_s(s.get("symbol"))) or rd is None or rd <= self.today:
                        self.add("L-FIX-03", f, f"futures symbol {sid} needs a month code, year and a future roll_date (expired contracts fail)", "symbols")
            if not _s(fx.get("login_ref")).startswith("vault://"):
                self.add("L-FIX-04", f, "login_ref is a vault:// reference, never the value", "login_ref")
            for loc, line in _walk_strings(fx):
                if credential_hits(line):
                    self.add("L-FIX-04", f, "a field matches a credential pattern", loc)
            sp, od = self.profiles.get(fx.get("settings_profile")), self.profiles.get(fx.get("order_defaults_profile"))
            if sp is None or sp.get("kind") != "app_settings":
                self.add("L-FIX-05", f, f"settings_profile {fx.get('settings_profile')} is not an app_settings profile in profiles/", "settings_profile")
            else:
                st = _d(sp.get("settings"))
                pins = (("order_confirmation", st.get("order_confirmation") is True),
                        ("shift_click_send", st.get("shift_click_send") in (False, "absent")),
                        ("active_trader_hotkeys", st.get("active_trader_hotkeys") is False),
                        ("order_hotkeys", st.get("order_hotkeys") == []))
                bad = [k for k, ok in pins if not ok]
                if bad:
                    self.add("L-FIX-05", f, f"settings profile {sp.get('id')} does not pin {', '.join(bad)} (12.6 A3 to A6)", "settings_profile")
            if od is None or od.get("kind") != "order_defaults":
                self.add("L-FIX-05", f, f"order_defaults_profile {fx.get('order_defaults_profile')} is not an order_defaults profile in profiles/", "order_defaults_profile")
            self._fixture_epoch(fx, f, symbols)
            self._fixture_positions(fx, f, accounts, symbols, ids)

    def _fixture_epoch(self, fx: dict, f: str, symbols: dict[Any, dict]) -> None:
        ep = fx.get("epoch")
        if not isinstance(ep, dict):
            self.add("L-FIX-06", f, "epoch is missing", "epoch")
            return
        if ep.get("epoch_id") != epoch_id(ep):
            self.add("L-FIX-06", f, f"epoch_id does not recompute; expected {epoch_id(ep)}", "epoch.epoch_id")
        fm = _d(ep.get("front_month"))
        for sid, s in symbols.items():
            m = re.match(r"^/([A-Z0-9]+?)(?:[FGHJKMNQUVXZ][0-9]{2})?$", _s(s.get("symbol")))
            if s.get("asset") == "future" and m and m.group(1).lower() not in fm:
                self.add("L-FIX-06", f, f"front_month has no key {m.group(1).lower()} for futures symbol {sid}", "epoch.front_month")
        branch = self.branches.get("data_fixture")
        if branch == "A" and not ep.get("dataset_id"):
            self.add("L-FIX-06", f, "frozen branch A (phase0/branches.yaml) needs epoch.dataset_id", "epoch.dataset_id")
        if branch == "B" and not ep.get("ondemand_timestamp"):
            self.add("L-FIX-06", f, "frozen branch B (phase0/branches.yaml) needs epoch.ondemand_timestamp", "epoch.ondemand_timestamp")
        cal = [_d(x) for x in _l(fx.get("churn_calendar"))]
        def has(event: str, day: Any) -> bool:
            return any(c.get("event") == event and c.get("date") == day for c in cal)
        for sid, s in symbols.items():
            if s.get("roll_date") and not has("roll", s.get("roll_date")):
                self.add("L-FIX-07", f, f"churn_calendar has no roll entry on {s.get('roll_date')} for {sid}", "churn_calendar")
        nme = ep.get("nearest_monthly_expiration")
        if nme and not has("expiration", nme):
            self.add("L-FIX-07", f, f"churn_calendar has no expiration entry on {nme}", "churn_calendar")
        golden = {_d(a).get("account_id") for a in _l(fx.get("accounts")) if _d(a).get("role") == "golden"}
        exps = _d(ep.get("option_expirations"))
        dates = [_date(d) for p in map(_d, _l(fx.get("positions"))) if p.get("account_ref") in golden
                 and _d(symbols.get(p.get("symbol_ref"))).get("asset") in ("option", "future_option")
                 for d in _l(exps.get(p.get("symbol_ref")))]
        dates = [d for d in dates if d]
        if dates and not any(c.get("event") == "reseed_due" and (_date(c.get("date")) or date.max) <= min(dates) for c in cal):
            self.add("L-FIX-07", f, f"churn_calendar has no reseed_due on or before {min(dates)}, the earliest golden option expiration", "churn_calendar")

    def _fixture_positions(self, fx: dict, f: str, accounts: list[dict], symbols: dict[Any, dict], ids: set) -> None:
        positions = [_d(p) for p in _l(fx.get("positions"))]
        for i, p in enumerate(positions):
            if p.get("account_ref") not in ids:
                self.add("L-FIX-00", f, f"account_ref {p.get('account_ref')} is not an account of this fixture", f"positions[{i}].account_ref")
            if p.get("symbol_ref") not in symbols:
                self.add("L-FIX-00", f, f"symbol_ref {p.get('symbol_ref')} is not a symbol of this fixture", f"positions[{i}].symbol_ref")
        for key in ("prices", "churn_calendar"):
            for i, x in enumerate(map(_d, _l(fx.get(key)))):
                if "symbol_ref" in x and x.get("symbol_ref") not in symbols:
                    self.add("L-FIX-00", f, f"symbol_ref {x.get('symbol_ref')} is not a symbol of this fixture", f"{key}[{i}].symbol_ref")
        for i, w in enumerate(map(_d, _l(fx.get("watchlists")))):
            for s in _l(w.get("symbols")):
                if s not in symbols:
                    self.add("L-FIX-00", f, f"watchlist symbol {s} is not a symbol of this fixture", f"watchlists[{i}].symbols")
        golden = {a.get("account_id") for a in accounts if a.get("role") == "golden"}
        empty = {a.get("account_id") for a in accounts if a.get("role") == "empty"}
        gp = [p for p in positions if p.get("account_ref") in golden]
        asset = lambda p: _d(symbols.get(p.get("symbol_ref"))).get("asset")  # noqa: E731
        window = _date(_d(fx.get("epoch")).get("window_date")) or self.today
        exps = _d(_d(fx.get("epoch")).get("option_expirations"))
        def leaps(p: dict) -> bool:
            ds = [_date(d) for d in _l(exps.get(p.get("symbol_ref")))]
            return any(d and (d - window).days > 365 for d in ds)
        cover = {
            "an equity below 1 dollar": any(asset(p) == "equity" and (_num(p.get("avg_price")) or 1) < 1 for p in gp),
            "an equity above 1,000 dollars": any(asset(p) == "equity" and (_num(p.get("avg_price")) or 0) > 1000 for p in gp),
            "a multi-leg option position": sum(asset(p) == "option" for p in gp) >= 2,
            "a LEAPS": any(asset(p) == "option" and leaps(p) for p in gp),
            "a futures position": any(asset(p) == "future" for p in gp),
            "a forex position": any(asset(p) == "forex" for p in gp),
        }
        missing = [k for k, ok in cover.items() if not ok]
        if golden and missing:
            self.add("L-FIX-09", f, "golden positions lack " + ", ".join(missing) + " (7.6 golden list)", "positions", self._phase_sev(2))
        if any(p.get("account_ref") in empty for p in positions) or fx.get("working_orders_expected", 0) != 0:
            self.add("L-FIX-10", f, "the empty account has no positions and working_orders_expected is 0", "positions")

    # -- 4.10 popups -----------------------------------------------------------------------------

    def _catalog(self, kind: str, key: str) -> tuple[str, list[dict]]:
        doc = self.r.one(kind)
        if not doc:
            return "", []
        items = doc.data if isinstance(doc.data, list) else _d(doc.data).get(key)
        return doc.rel, [_d(x) for x in _l(items)]

    def check_popups(self) -> None:
        f, items = self._catalog("popups", "popups")
        if not f:
            return
        ids = [p.get("popup_id") for p in items]
        names = [p.get("name") for p in items]
        released = sorted((t, bid) for bid, b in self.builds.items() if b.get("channel") == "released" and (t := _ts(b.get("released_at"))))
        for i, p in enumerate(items):
            at, pid = f"[{i}]", p.get("popup_id")
            if not (isinstance(pid, str) and POP_ID.match(pid)) or pid not in POP_IDS_12_8:
                self.add("L-POP-01", f, f"popup_id {pid!r} is not an id of the 12.8 table", f"{at}.popup_id")
            if ids.count(pid) > 1:
                self.add("L-POP-01", f, f"popup_id {pid} is not unique", f"{at}.popup_id")
            if names.count(p.get("name")) > 1:
                self.add("L-POP-01", f, f"name {p.get('name')} is not unique", f"{at}.name")
            rx = None
            try:
                rx = re.compile(_s(p.get("title_regex")))
            except re.error as exc:
                self.add("L-POP-01", f, f"title_regex does not compile: {exc}", f"{at}.title_regex")
            action, oa = p.get("action"), p.get("on_abort")
            if action == "dismiss_key" and _s(p.get("key")).replace(" ", "").casefold() in ENTER_KEYS:
                self.add("L-POP-02", f, f"dismiss key {p.get('key')} is Enter, Space or Ctrl+Enter", f"{at}.key")
            if rx and any(rx.search(t) for t in ("Order Confirmation", "Confirm", "Send")):
                oad = _d(oa)
                if not (p.get("class") == "safety" and action not in ("dismiss_key", "click") and oad.get("outcome") == "aborted_run"
                        and oad.get("run_outcome") == "aborted_safety" and oad.get("reason") == "kill_switch"):
                    self.add("L-POP-03", f, "a title matching Order Confirmation, Confirm or Send is class safety with no input and the kill-switch on_abort", at)
            ctl = self.controls.get(_s(p.get("target")))
            if action == "click":
                if not p.get("template"):
                    self.add("L-POP-04", f, "click entries carry a template", f"{at}.template")
                if ctl is None:
                    self.add("L-POP-04", f, f"click target {p.get('target')} is not a registry control", f"{at}.target")
                elif _s(ctl[1].get("ocr_text")).strip().casefold() in ("accept", "agree", "confirm", "send", "yes"):
                    self.add("L-POP-03", f, f"click target {p.get('target')} reads {ctl[1].get('ocr_text')}; consent and send labels are refused", f"{at}.target")
            if action in ("abort", "relogin", "wait") and not isinstance(oa, dict):
                self.add("L-POP-04", f, f"action {action} requires on_abort", f"{at}.on_abort")
            if isinstance(oa, dict):
                self._on_abort(f, at, p, oa)
            elif p.get("class") == "app_failure":
                self.add("L-POP-04", f, "app_failure entries route to INV-14 through on_abort", f"{at}.on_abort")
            last = p.get("last_seen_build")
            lt = _ts(_d(self.builds.get(last)).get("released_at")) if last else None
            if lt and sum(1 for t, _ in released if t > lt) > 3:
                self.add("L-POP-06", f, f"last_seen_build {last} is more than three releases old; flag for removal", f"{at}.last_seen_build")

    def _on_abort(self, f: str, at: str, p: dict, oa: dict) -> None:
        outcome = oa.get("outcome")
        if p.get("class") == "app_failure" and not (outcome == "invariant_violation" and oa.get("invariant_id") == "INV-14"):
            self.add("L-POP-04", f, "app_failure entries route to invariant_violation INV-14", f"{at}.on_abort")
        named = oa.get("run_outcome") if outcome == "aborted_run" else outcome
        reason = oa.get("reason")
        if reason is not None and not (isinstance(reason, str) and self.tax.reason_allowed(_s(named), reason)):
            self.add("L-POP-04", f, f"on_abort.reason {reason} is not in taxonomy.yaml for {named}", f"{at}.on_abort.reason")
        if outcome == "aborted_run" and not oa.get("run_outcome"):
            self.add("L-POP-04", f, "on_abort aborted_run names run_outcome", f"{at}.on_abort")
        if outcome == "env_error" and oa.get("gate") not in self.tax.gates:
            self.add("L-POP-04", f, f"env_error on_abort names a taxonomy gate, not {oa.get('gate')}", f"{at}.on_abort.gate")
        inv = oa.get("invariant_id")
        if (outcome == "invariant_violation" or inv is not None) and inv not in self.tax.data["reasons"]["invariant_violation"]:
            self.add("L-POP-04", f, f"invariant_id {inv} is not in the 2.7 catalog", f"{at}.on_abort.invariant_id")

    # -- 4.11 mutants ----------------------------------------------------------------------------

    def check_mutants(self) -> None:
        f, items = self._catalog("mutants", "mutants")
        if not f:
            return
        ids = [m.get("mutant_id") for m in items]
        invs = self.tax.data["reasons"]["invariant_violation"]
        detecting = {"collateral", "invariant_violation", "assertion_failed", "landmark_missing"}
        tier1_classes: dict[str, set[str]] = {}
        for i, m in enumerate(items):
            at, mid, cls = f"[{i}]", m.get("mutant_id"), m.get("class")
            if not (isinstance(mid, str) and MUTANT_ID.match(mid)):
                self.add("L-MUT-01", f, f"mutant_id {mid!r} does not match ^M[0-9]{{2,}}$", f"{at}.mutant_id")
            if ids.count(mid) > 1:
                self.add("L-MUT-01", f, f"mutant_id {mid} is not unique", f"{at}.mutant_id")
            gen = _d(m.get("generator"))
            if gen.get("seed", MUTANT_SEED) != MUTANT_SEED:
                self.add("L-MUT-01", f, f"generator seed {gen.get('seed')} is not the catalog seed {MUTANT_SEED}", f"{at}.generator.seed")
            cid = _s(m.get("checkpoint_id"))
            if m.get("region_ref") not in self.region_map(cid):
                self.add("L-MUT-01", f, f"region_ref {m.get('region_ref')} is not in regions/{cid}.yaml", f"{at}.region_ref")
            exp = _d(m.get("expected"))
            outs = set(_l(exp.get("outcomes")))
            if m.get("money_cell") is True:
                if cls not in ("aa_control", "sub_floor") and not outs <= detecting:
                    self.add("L-MUT-02", f, f"money-cell mutant {mid} expects {sorted(outs - detecting)}; only {sorted(detecting)} detect", f"{at}.expected")
                if outs & {"invariant_violation", "assertion_failed"} and not (exp.get("invariant_id") or exp.get("assertion_id")):
                    self.add("L-MUT-02", f, f"money-cell mutant {mid} names an invariant_id or assertion_id", f"{at}.expected")
            exempt = {"aa_control", "sub_floor", "redaction"}
            if outs & {"identical", "noise"} and cls not in exempt:
                self.add("L-MUT-02", f, f"only the controls M20 and M25 expect identical or noise, not {mid}", f"{at}.expected")
            if "needs_human" in outs and cls != "global_shift":
                self.add("L-MUT-02", f, f"needs_human is expected only for global_shift (reason global_shift), not {cls}", f"{at}.expected")
            mode, flow = m.get("mode"), m.get("canary_flow")
            if mode == "behavioral":
                fdoc = self.flows.get(_s(flow))
                if fdoc is None or _d(fdoc.data).get("class") != "canary":
                    self.add("L-MUT-04", f, f"behavioral mutant {mid} needs a canary_flow of class canary (got {flow})", f"{at}.canary_flow")
                elif any(_d(st).get("order_stage") is True or any(
                        _s(_d(st).get(k)).endswith(".confirm-and-send") or self._confirm_and_send(_d(st).get(k)) for k in TARGET_FIELDS)
                        for _sec, st in self._all_steps(fdoc.data)):
                    # same predicate as L-FLW-07 (id or OCR text), and any order-stage step (S-22)
                    self.add("L-MUT-04", f, f"canary flow {flow} targets confirm-and-send", f"{at}.canary_flow")
            elif mode == "image" and "canary_flow" in m:
                self.add("L-MUT-04", f, f"image mutant {mid} carries no canary_flow", f"{at}.canary_flow")
            if cls == "color_swap" and not ("invariant_violation" in outs and exp.get("invariant_id") == "INV-15"):
                self.add("L-MUT-05", f, f"color_swap mutant {mid} expects invariant_violation on INV-15", f"{at}.expected")
            c = self.defined.get(cid)
            if c and c.tier == 1:
                tier1_classes.setdefault(_s(cls), set()).add(cid)
            if "invariant_id" in exp and exp.get("invariant_id") not in invs:
                self.add("L-MUT-06", f, f"invariant_id {exp.get('invariant_id')} is not in INV-01 to INV-15", f"{at}.expected.invariant_id")
        if set(ids) != set(MUTANTS_12_5):
            self.add("L-MUT-01", f, "the catalog's id set is exactly M01 to M33 of 12.5")
        for cls in BRIEF_CLASSES:
            if cls not in tier1_classes:
                self.add("L-MUT-05", f, f"brief class {cls} is bound to no tier-1 checkpoint")

    # -- 4.12 fingerprint ------------------------------------------------------------------------

    def check_fingerprint_schema(self) -> None:
        names = schemas.fingerprint_field_names()
        want = set(schemas.FINGERPRINT_FIELDS_7_4)
        if names != want:
            self.add("L-FPR-01", "schemas/fingerprint.schema.json",
                     f"field set differs from 7.4 plus render_probe_hash: extra {sorted(names - want)}, missing {sorted(want - names)}")

    # -- 4.15 cross-file rules -------------------------------------------------------------------

    def check_xref(self) -> None:
        self._xref_coverage()
        self._xref_checkpoint_ids()
        self._xref_files()
        self._xref_policy()

    def _xref_coverage(self) -> None:
        flow_cps = {c.surface for c in self.defined.values() if c.kind == "flow"}
        census = {c.surface for c in self.defined.values() if c.kind == "census"}
        money = {c.surface for cid, c in self.defined.items()
                 if any(x.get("money_cell") is True and x.get("type") in ("diff", "freeze") for x in self.region_map(cid).values())}
        asserting: set[str] = set()
        for doc in self.flows.values():
            fl = _d(doc.data)
            seq = self._seq(fl)
            if any(st.get("type") == "assert" or _l(st.get("expect")) for st in seq):
                asserting |= {s for s in _l(fl.get("surfaces")) if isinstance(s, str)}
        for sid, s in self.surfaces.items():
            f = self.surface_file[sid]
            if sid not in flow_cps and sid not in census:
                self.add("L-XREF-01", f, f"{sid} is reached by no census workspace and no flow checkpoint")
            if s.get("tier") == 1 and s.get("kind") != "probe":
                if sid not in asserting:
                    self.add("L-XREF-02", f, f"tier-1 surface {sid} has no flow with an assert step or expect entries")
                if sid not in money:
                    self.add("L-XREF-02", f, f"tier-1 surface {sid} has no regions file with a diff or freeze money_cell region")
        for cid, c in self.defined.items():
            rmap = self.region_map(cid)
            where = self.regions[cid].rel if cid in self.regions else c.file
            if c.data_mode == "masked_live" and not any(x.get("type") in ("rule", "mask") for x in rmap.values()):
                self.add("L-XREF-03", where, f"masked_live checkpoint {cid} has no rule or mask region")
            if c.data_mode == "frozen":
                for x in rmap.values():
                    if "must_change_within_s" in _d(x.get("rule")):
                        self.add("L-XREF-03", where, f"frozen checkpoint {cid} has a must_change_within_s rule on {x.get('id')}")

    def _xref_checkpoint_ids(self) -> None:
        refs: list[tuple[str, str, str]] = []
        for doc in self.r.of("flow"):
            for st in self._seq(doc.data):
                if st.get("type") == "checkpoint":
                    refs.append((doc.rel, f"steps[{st.get('id')}].checkpoint_id", st.get("checkpoint_id")))
        for cid, doc in self.regions.items():
            refs.append((doc.rel, "checkpoint_id", _d(doc.data).get("checkpoint_id", cid)))
        for doc in self.r.of("mask"):
            refs.append((doc.rel, "checkpoint_id", _d(doc.data).get("checkpoint_id")))
        f_mut, mutants = self._catalog("mutants", "mutants")
        for i, m in enumerate(mutants):
            refs.append((f_mut, f"[{i}].checkpoint_id", m.get("checkpoint_id")))
            cid = m.get("checkpoint_id")
            if cid not in self.defined and cid not in self.retired:
                self.add("L-XREF-04", f_mut, f"checkpoint {cid} is defined by no flow or census and is not retired", f"[{i}].checkpoint_id")
        for i, x in enumerate(self.renames):
            refs.append((self.renames_file, f"renames[{i}].old", x.get("old")))
            if x.get("new") != "retired":
                refs.append((self.renames_file, f"renames[{i}].new", x.get("new")))
        q = self.r.one("quarantine")
        for i, x in enumerate(map(_d, _l(_d(q.data).get("records") if q else []))):
            refs.append((q.rel if q else "", f"records[{i}].checkpoint", x.get("checkpoint")))
        for cid, doc in self.regions.items():
            if cid not in self.defined and cid not in self.retired:
                self.add("L-XREF-04", doc.rel, f"checkpoint {cid} is defined by no flow or census and is not retired")
        for file, loc, cid in refs:
            if not (isinstance(cid, str) and is_checkpoint_id(cid)):
                self.add("L-XREF-11", file, f"{cid!r} does not match the checkpoint id grammar of 4.1", loc)
            elif cid.split("/")[1] not in self.surfaces:
                self.add("L-XREF-11", file, f"surface segment {cid.split('/')[1]} of {cid} is not in the registry", loc)

    def _xref_files(self) -> None:
        dep_bad = any(v.rule == "L-DEP-05" for v in self.out)
        if dep_bad:
            self.add("L-XREF-06", self.depmap_file, "the dependency map and the registry disagree (see L-DEP-05)")
        for doc in self.r.of("manifest"):
            stale = sorted({s for e in map(_d, _l(_d(doc.data).get("entries"))) for s in _l(e.get("declared_surfaces")) if s not in self.surfaces}
                           | {p for e in map(_d, _l(_d(doc.data).get("entries"))) for p in _l(e.get("touched_primitives")) if p not in self.primitives}, key=str)
            if stale:
                self.add("L-XREF-06", doc.rel, f"manifest references ids absent from the registry or the map: {', '.join(map(str, stale))}")
        for rel, text in self.r.text_files():
            for n, why in credential_hits(text):
                self.add("L-XREF-07", rel, f"{why}; secrets are vault://<path>#<field> references only", f"line {n}")
        tdoc = self.r.one("taxonomy")
        if tdoc:
            mine, theirs = _d(tdoc.data), self.tax.data
            keys = sorted(k for k in set(mine) | set(theirs) if k != "generated_from" and mine.get(k) != theirs.get(k))
            if keys:
                self.add("L-XREF-08", tdoc.rel, f"differs from the taxonomy generated from the spec in: {', '.join(keys)}")
        sdir = self.r.root / "schemas"
        if sdir.is_dir():
            for name, problem in schemas.check_schemas(sdir):
                self.add("L-XREF-09", f"schemas/{name}", problem)
        _f, pops = self._catalog("popups", "popups")
        if not _f:
            self.add("L-XREF-12", "popups/catalog.yaml", "popups/catalog.yaml is missing")
        else:
            got = {p.get("popup_id") for p in pops}
            if got != set(POP_IDS_12_8):
                self.add("L-XREF-12", _f, "popup ids differ from POP-01 to POP-22 of 12.8: " + _id_diff(got, POP_IDS_12_8))
        f_mut, mutants = self._catalog("mutants", "mutants")
        if not f_mut:
            self.add("L-XREF-12", "canaries/mutants.yaml", "canaries/mutants.yaml is missing")
        else:
            got = {m.get("mutant_id") for m in mutants}
            if got != set(MUTANTS_12_5):
                self.add("L-XREF-12", f_mut, "mutant ids differ from M01 to M33 of 12.5: " + _id_diff(got, MUTANTS_12_5))
            for i, m in enumerate(mutants):
                want = MUTANTS_12_5.get(_s(m.get("mutant_id")))
                if want and (m.get("class"), m.get("money_cell")) != want:
                    self.add("L-XREF-12", f_mut, f"{m.get('mutant_id')} is class {want[0]} with money_cell {want[1]} in 12.5", f"[{i}]")

    def _xref_policy(self) -> None:
        eb = self.r.one("explorer_budget")
        if not eb:
            self.add("L-XREF-14", "policy/explorer_budget.yaml", "policy/explorer_budget.yaml is missing")
        elif not self.explorer_budget.get("approved_by"):
            for k, v in EXPLORER_BUDGET_6_8.items():
                if self.explorer_budget.get(k) != v:
                    self.add("L-XREF-14", eb.rel, f"{k} is {self.explorer_budget.get(k)}; 6.8 says {v} unless the Explorer owner approves", k)
        bdoc = self.r.one("branches")
        f = bdoc.rel if bdoc else "phase0/branches.yaml"
        if not bdoc:
            self.add("L-XREF-15", f, "phase0/branches.yaml is missing; it names one branch for each of the seven unknowns")
            return
        for k in BRANCH_KEYS:
            v = self.branches.get(k)
            if k not in self.branches:
                self.add("L-XREF-15", f, f"no branch for unknown {k}", k)
            elif v not in ("A", "B"):
                self.add("L-XREF-15", f, f"unknown {k} is unresolved ({v!r}); choose A or B", k, self._phase_sev(1))
        data = self.branches.get("data_fixture")
        for doc in self.r.of("fixture"):
            provider = _d(_d(_d(doc.data).get("data_mode")).get("frozen")).get("provider")
            if (data == "A" and provider not in ("backend_fixture",)) or (data == "B" and provider not in ("ondemand", "none")):
                self.add("L-XREF-15", doc.rel, f"frozen provider {provider} disagrees with data_fixture branch {data}", "data_mode.frozen.provider")
        al = self.r.one("allowlist")
        if al and "ondemand_virtual_account" in _d(al.data) and data != "B":
            self.add("L-XREF-15", al.rel, "ondemand_virtual_account exists only under frozen branch B", "ondemand_virtual_account")
        if self.branches.get("computer_use") == "B":
            for doc in self.r.of("flow"):
                if _d(doc.data).get("agent_fallback") == "allowed":
                    self.add("L-XREF-15", doc.rel, "agent_fallback: allowed while computer use is branch B", "agent_fallback")


# -- public API ------------------------------------------------------------------------------------

class LintPluginError(RuntimeError):
    """A sentinel.lint.checks module breaks the plug-in contract of sentinel/lint/checks/__init__.py."""


def check_modules() -> list[ModuleType]:
    """Every rule module of `sentinel.lint.checks`, by name; each names its RULES rows in RULE_IDS and exposes
    `check(repo, add, phase, today)`."""
    from . import checks
    mods = []
    for info in sorted(pkgutil.iter_modules(checks.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{checks.__name__}.{info.name}")
        ids = getattr(mod, "RULE_IDS", None)
        if not isinstance(ids, tuple) or not ids or not callable(getattr(mod, "check", None)):
            raise LintPluginError(f"{mod.__name__} needs a non-empty RULE_IDS tuple and a check function")
        unknown = [r for r in ids if r not in RULES]
        if unknown:
            raise LintPluginError(f"{mod.__name__} names {unknown}, which are not rows of RULES (4.15)")
        mods.append(mod)
    return mods


def run_check_modules(repo: Repo, add: Callable[..., None], phase: int, today: date) -> frozenset[str]:
    """Run every check module against `repo`; returns the rule ids they implement."""
    implemented: set[str] = set()
    for mod in check_modules():
        own = frozenset(mod.RULE_IDS)

        def scoped(rule: str, file: str, message: str, path: str = "", severity: str | None = None,
                   _own: frozenset[str] = own, _name: str = mod.__name__) -> None:
            if rule not in _own:
                raise LintPluginError(f"{_name} reported {rule}, which its RULE_IDS do not name")
            add(rule, file, message, path, severity)

        mod.check(repo, scoped, phase, today)
        implemented |= own
    return frozenset(implemented)


def lint_repo(repo: Repo, phase: int = 0, today: date | None = None,
              flow_p50_s: dict[str, float] | None = None) -> LintReport:
    """Lint a loaded repository (4.1, 4.15). `flow_p50_s` gives measured flow p50s for L-FLW-10. The rule modules
    of sentinel.lint.checks run after the built-in rules; a pending row a module implements is not skipped."""
    linter = _Linter(repo, phase, today or date.today(), flow_p50_s)
    linter.run()
    implemented = run_check_modules(linter.r, linter.add, linter.phase, linter.today)
    skipped = {rid: r.skipped for rid, r in RULES.items()
               if r.skipped and not (r.skipped == _PENDING and rid in implemented)}
    return LintReport(sorted(set(linter.out)), skipped)


def lint(root: str | Path, phase: int = 0, today: date | None = None) -> LintReport:
    """Load the sentinel-config checkout at `root` and lint it."""
    return lint_repo(Repo.load(root), phase, today)


def _fpr(rule: str, file: str, message: str, path: str = "") -> Violation:
    return Violation(file, rule, RULES[rule].severity, path, message)


def lint_fingerprint(fp: Any, file: str = "fingerprint") -> list[Violation]:
    """L-FPR-00 and L-FPR-01 on one fingerprint record: schema, non-null match fields, recomputed id."""
    out = [_fpr("L-FPR-00", file, msg, loc) for loc, msg in schemas.validate("fingerprint", fp)]
    match = _d(_d(fp).get("match"))
    for k in sorted(schemas.schema("fingerprint")["properties"]["match"]["properties"]):
        if match.get(k) is None:
            out.append(_fpr("L-FPR-01", file, f"match field {k} is null or missing", f"match.{k}"))
    if isinstance(fp, dict) and fp.get("fingerprint_id") != fingerprint_id(fp):
        out.append(_fpr("L-FPR-01", file, f"fingerprint_id does not recompute; expected {fingerprint_id(fp)}", "fingerprint_id"))
    return out


def lint_fingerprint_rig_spec(fp: Any, allowlist_sha256: str | None = None, file: str = "fingerprint") -> list[Violation]:
    """L-FPR-02 at run start: `match` equals the rig specification of 7.1 for the phase 1 config."""
    m = _d(_d(fp).get("match"))
    want = {
        "display.width": 1920, "display.height": 1080, "display.scale_percent": 100, "display.dpi": 96,
        "display.monitors": 1, "display.bits": 32, "display.frequency": 60,
        "keyboard.layout": "0x0409", "keyboard.layout_count": 1,
        "keyboard.toggle_keys.caps": False, "keyboard.toggle_keys.num": True, "keyboard.toggle_keys.scroll": False,
        "time_zone.os": "Eastern Standard Time", "time_zone.app_display": "America/New_York",
        "time_zone.chart": "America/New_York", "input_desktop": "Default",
    }
    if allowlist_sha256:
        want["harness.allowlist_sha256"] = allowlist_sha256
    out = []
    for path, value in want.items():
        cur: Any = m
        for part in path.split("."):
            cur = _d(cur).get(part) if isinstance(cur, dict) else None
        if cur != value:
            out.append(_fpr("L-FPR-02", file, f"{path} is {cur!r}; the rig specification says {value!r}", f"match.{path}"))
    j2d = _d(m.get("java2d_pipeline"))
    if j2d.get("confirmed") not in (j2d.get("pinned"), "unknown"):
        out.append(_fpr("L-FPR-02", file, "java2d_pipeline.confirmed differs from the pinned pipeline", "match.java2d_pipeline"))
    return out


def lint_fingerprint_pair(candidate: Any, base: Any, requested: dict[str, str] | None = None,
                          file: str = "fingerprint") -> list[Violation]:
    """L-FPR-03 and L-FPR-05 at run start: the two sides match apart from build_id, which must be
    each side's requested build (`requested` maps `candidate`/`base` to build ids)."""
    cm, bm = _d(_d(candidate).get("match")), _d(_d(base).get("match"))
    out = []
    differing = sorted(k for k in set(cm) | set(bm) if k not in ("build_id", "render_probe_hash") and cm.get(k) != bm.get(k))
    if differing:
        out.append(_fpr("L-FPR-03", file, f"fingerprint_mismatch: {', '.join(differing)} differ between candidate and base", "match"))
    for side, m in (("candidate", cm), ("base", bm)):
        if requested and side in requested and m.get("build_id") != requested[side]:
            out.append(_fpr("L-FPR-03", file, f"{side} build_id {m.get('build_id')} is not the requested {requested[side]} (version_mismatch)", "match.build_id"))
    if cm.get("render_probe_hash") != bm.get("render_probe_hash"):
        out.append(_fpr("L-FPR-05", file, "render_probe_hash differs between sides (Hamming 0 required)", "match.render_probe_hash"))
    return out
