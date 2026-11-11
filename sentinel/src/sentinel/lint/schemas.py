"""JSON Schemas (draft 2020-12) for every record type of section 4, built from its notation (4.2 to 4.16).

`NOTATION` holds the schema blocks of section 4 verbatim (4.1 Schema notation), one entry per
`# schemas/<name>.schema.json` part; `parse_notation` reads them and the builder turns each part into
the JSON Schema of that file, rooted at its first record with the part's other records in `$defs`.
`schema(name)` returns one generated schema (`<name>#<record>` for one record of it, such as one
catalog entry `popup#popup` or the 4.13 `outcome#finding`), `validate(name, data)` lists the
violations of a document, `write_schemas(dir)` writes `<dir>/<name>.schema.json` and
`check_schemas(dir)` is the regeneration check of L-XREF-09 (`sentinel schemas --check`). The files
are committed under sentinel/schemas/ (4.1, SD-35) and a test keeps them current.

Choices where the notation is silent or ambiguous (4.1; reported):

1. `id` is the slug of 4.1 except where the spec's own examples use other shapes: checkpoint ids
   follow the checkpoint grammar of 4.1; assertion ids (`qty_default`, `R02`), fixture symbol and
   price ids (`mes_front`), invariant ids (`INV-14`) and rule ids (`R01`) follow LOCAL_ID; step ids
   and `since_step` follow STEP_ID of 4.7.
2. Every object is closed (`additionalProperties: false`, `unevaluatedProperties: false` on steps):
   the notation names every field.
3. `schema_version` is required and must equal the version the notation gives, so lint refuses a
   file whose version the harness does not know (4.1).
4. `ts` is RFC 3339 with an explicit offset (`Z` allowed), `date` is `YYYY-MM-DD`, `sha256` is
   `sha256:` plus 64 lowercase hex digits, `path` is repository relative, `commit_sha` 40 hex.
5. 4.7 gives the step fields as a table, not notation; STEP_NOTATION writes that table in the same
   notation (common fields, then one block per type) and the step schema dispatches on `type`.
6. `enum[1|2|3]` holds integers (YAML reads `tier: 1` as an int); every other enum holds strings.
7. A build file carries `build`, `build_pair` or both, as the 4.2 worked example does.
8. Files whose path 4.1 names but whose shape section 4 does not write (ladders, renames, the send
   policy, the Explorer budget, profiles) get the shapes in AUX_NOTATION, chosen here from the prose
   that names their fields.
9. An inline object key without a type (`bbox: {x,y,w,h}` in 4.13) is an int, as every x, y, w and h
   of section 4. A grid cell (`id | grid_cell` of 4.14) is `g-<row>-<col>` on the 8 x 8 grid of 9.7,
   rows and columns 0 to 7.
10. A part with several records generates one file holding all of them: `outcome.schema.json` is
   rooted at `checkpoint_outcome` with `step_outcome`, `run_outcome` and `finding` in `$defs`; the
   popup and mutant schemas describe the catalog file (`popup_catalog`, `mutant_catalog`), and the
   lint validates each entry as `popup#popup` and `mutant#mutant`.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..taxonomy import CHECKPOINT_ID, SLUG, load as load_taxonomy
from .repo import load_yaml_text

DRAFT = "https://json-schema.org/draft/2020-12/schema"
SLUG_PATTERN = f"^{SLUG}$"
LOCAL_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
STEP_ID = r"^[a-z][a-z0-9_-]*$"          # 4.7 step common fields; a checkpoint step adds the id grammar of 4.1
PASCAL = r"^[A-Z][A-Za-z0-9]*$"
SHA256 = r"^sha256:[0-9a-f]{64}$"
SHA40 = r"^[0-9a-f]{40}$"
TS = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
REL_PATH = r"^(?![/\\])(?!.*\.\.)[^\\]+$"
GLOB = f"^{SLUG}\\.\\*$"
GRID_CELL = r"^g-[0-7]-[0-7]$"

# The notation blocks of section 4, verbatim: one entry per `# schemas/<name>.schema.json` part, and the flow's
# checkpoint step and assertion blocks under their record names. A test compares them with the spec.
NOTATION: dict[str, str] = {
    "build": """\
# schemas/build.schema.json
build:
  build_id: id                       # <channel>-<version_string>-<short_sha>
  channel: enum[branch|main|rc|released]
  version_string: str                # as the main window title shows it; format verified in Phase 0 (brief 1.11 item 4)
  commit_sha: str                    # 40 hex
  branch: str?                       # branch builds only
  merge_base_sha: str?               # branch builds only; selects the base
  pipeline_run_id: str
  artifact: {uri: str, sha256: sha256}
  install_method: enum[artifact|snapshot]   # branch A = artifact with auto-update disabled; branch B = VM snapshot
  snapshot_id: str?                  # required when install_method = snapshot
  jre_release_sha256: sha256?        # hash of the bundled jre/release file, filled by the Rig Manager at first install
  built_at: ts
  released_at: ts?                   # channel = released only

build_pair:
  pair_id: id                        # <run_type>-<candidate>-vs-<base>
  run_type: enum[pr|merge|rc|nightly|calibration]
  candidate: ref:build.build_id
  base: ref:build.build_id
  base_rule: enum[merge_base_main|previous_main|last_released|same_build]
  lineage_baseline_set: str?         # rc only: the approved baseline set of the last release
  created_at: ts
""",
    "manifest": """\
# schemas/manifest.schema.json
manifest:
  schema_version: int = 1
  manifest_id: id                       # mf-<candidate build_id>
  candidate: ref:build.build_id
  base: ref:build.build_id
  source: enum[ticket_export|release_notes|manual|union]
  author: str                           # corporate login
  approved_by: str?                     # required for merge and rc
  entries: list[entry]
  empty_reason: str?                    # required when entries is empty

entry:
  entry_id: id                          # <ticket key lowercased>-<n>
  tickets: list[str]                    # Jira keys, ^[A-Z][A-Z0-9]+-[0-9]+$; at least one
  summary: str
  declared_surfaces: list[ref:surface.id]
  new_surface: bool = false             # true when a declared surface does not exist on the base build
  touched_primitives: list[ref:dependency_map.primitives.id]
  changed_paths: list[str]?             # source globs from the diff; input to the static impact step
  expected_visual: str                  # at least 20 characters; what a reviewer should see on the declared surfaces
  mock_image: path?                     # PNG under manifests/mocks/, a 1:1 crop of the intended result
  feature_flags: list[{name: str, state: enum[on|off], scope: enum[build|login]}]
  must_be_observed: bool = true         # the Reporter lists entries whose surfaces showed no diff
""",
    "dependency-map": """\
# schemas/dependency-map.schema.json
dependency_map:
  schema_version: int = 1
  first_scan_at: date?                # date of the first Phase 1 bytecode scan; starts the 90-day clock of L-DEP-07
  primitives: list[primitive]
  edges: list[edge]

primitive:
  id: str                             # PascalCase, e.g. FlexibleGrid, Docking, ColumnModel, Theme, FontMetrics, ChartEngine, OrderTicketCore
  description: str
  source_packages: list[str]          # Java package globs that implement it
  owner: str
  global: bool = false                # true for primitives every surface consumes (Theme, FontMetrics)

edge:
  primitive: ref:primitive.id
  surface: ref:surface.id | glob | "*"   # glob is <prefix>.* over registry ids; "*" only for global primitives
  source: enum[declared|static_analysis]
  confirmed_from_source: bool = false # true once a bytecode scan of a named build has seen the dependency
  confirmed_build: ref:build.build_id?   # the build whose scan confirmed it; required when confirmed_from_source
  confidence: num                     # declared = 1.0; static_analysis in (0, 1]; raised to 1.0 only by confirmation
  evidence: str?                      # the class or file that imports the primitive; required for static_analysis
  reason: str?                        # declared edges the scan cannot see (reflection, properties-driven theming)
  first_seen: ts
  last_verified: ts
""",
    "surface": """\
# schemas/surface.schema.json
surface:
  schema_version: int = 1
  id: id                              # <area>.<surface>[.<variant>]
  title: str                          # as the UI labels it
  aliases: list[str]?                 # older spellings, used by validation and OCR anchors
  parent: ref:surface.id?             # hosting surface; builds the surface tree of 2.2.1
  kind: enum[tab|subtab|section|gadget|panel|dialog|menu|context_menu|settings_page|ticket_variant|chain_layout|active_trader_panel|chart|probe]   # probe: the eight registry validation surfaces of 2.2.3
  tier: enum[1|2|3]                   # 1 = money and orders, 2 = analysis, 3 = everything else
  owner: str
  smoke: bool = false                 # PR smoke subset membership, flagged by the registry owner, at most 40 (2.11, L-SRF-15); tier 1 does not imply smoke
  critical: bool = false              # critical surface: its checkpoints need behavioral mutants (L-MUT-03); tier 1 only (L-SRF-16)
  streaming: bool = false             # the whole surface moves by itself (news, video, chat): excluded from size sweeps (2.5.3) and from tier A and C workspaces (L-WSP-08)
  reached_after_send: bool = false    # reached only after a sent order (fills, Working to Filled, cancel and replace); requires paper_send (L-SRF-12)
  path: list[step]                    # steps from the canonical workspace to the surface; flow step vocabulary (4.7)
  primitives: list[ref:primitive.id]
  roots: list[str]                    # class or package prefixes implementing the surface; input to the static impact step
  personas: list[enum[active_trader|options_trader|futures_trader|swing_trader|beginner]]   # personas[0] is the primary persona: sweep and split checkpoints count toward it in the matrix (2.12)
  entitlements: list[enum[margin|options_L1|options_L2|options_L3|futures|forex|ira|portfolio_margin|multi_account|paper_send]]
  enumerable_by: enum[P1|P2|P3|P4|P5|P6|P7|P8|none]   # the registry validation probe that lists it (2.2.3)
  resizable: bool                     # set only from probe P8 or a declared in-host splitter
  hosts: list[enum[tab|sidebar|flexgrid|detached|dialog]]?   # required when resizable; one w x h sweep per flexgrid, detached or dialog host; tab and sidebar hosts are swept through their splitters only (2.5.1)
  splitters: list[{id: id, template: path, host: enum[tab|sidebar|dialog], fractions: list[num]?, collapsible: bool = false}]?   # in-host splitters for split checkpoints (2.5.2); fractions narrows the ladder set with a stated reason; 0.00 and 1.00 only when collapsible
  size_ladder: ref:ladder.id?         # optional override only: 2.5.3 assigns Ladder S or F by tier and run type
  ladder_reason: str?                 # required with size_ladder
  target_window: {kind: enum[main|dialog|detached|popup], title_regex: str?}
  data_mode: enum[frozen|masked_live] # default for census and sweep checkpoints; flows may override
  account_state: bool = false         # shows account values; fixture drift makes its checkpoints env_error (gate: fixture_verified, reason: fixture_drift) (4.9)
  landmarks: list[{id: id, template: path, confidence_min: num = 0.92, must_see: bool}]   # 0.92 is the 3.7 default
  anti_anchors: list[str]             # OCR strings that must be absent at capture
  labels: list[str]                   # required labels for INV-05; tier 1 lists its primary action labels
  controls: list[control]
  settle: {max_wait_ms: int = 8000}
  census: {enabled: bool = true, workspaces: list[ref:workspace.id], dimensions: map[str, list[str]]?}
  invariants: surface_invariants?     # the per-surface inputs of the 2.7 catalog (L-XREF-30)
  invariant_exceptions: list[{invariant_id: id, owner: str, reason: str, expires_at: ts}]?

surface_invariants:
  min_font_px: num?                   # INV-07: minimum legible font height for this surface; default the pinned 9 px
  overlap_allowlist: list[list[str]]? # INV-01, INV-02, INV-11: pairs [control id, control id] allowed to overlap (tooltips, crosshair, overlay badges); "*" matches any
  signed_columns: list[str]?          # INV-15: the declared signed columns (P/L, change); the palette is palettes/<theme>.yaml
  allowed_scrollbars: map[str, {w: int, h: int}]?   # INV-06: container -> the smallest size at which no scrollbar is expected
  focus_order: list[str]?             # INV-13: control ids in Tab order; keyboard-only ticket and confirm dialog surfaces only

control:
  id: id
  jab: {name: str, role: str}?        # branch A only (brief 1.11 item 1); ignored under branch B
  template: path                      # image anchor, always present
  ocr_text: str?                      # OCR text anchor, third rung
  click_offset: {dx: int, dy: int} = {dx: 0, dy: 0}
  required: bool = false              # must be present at capture (INV-10); tier 1 lists its primary actions
  deny: bool = false                  # on the gateway deny list; no flow may click it
""",
    "workspace": """\
# schemas/workspace.schema.json
workspace:
  schema_version: int = 1
  id: id                               # ws.tier-<a|b|c>.<persona or name>, e.g. ws.tier-a.active-trader, ws.tier-c.sweep-harness
  tier: enum[A|B|C]                    # A canonical per persona, B real power-user and migration, C size-sweep harness
  persona: enum[active_trader|options_trader|futures_trader|swing_trader|beginner]?   # required for tiers A and B (a tier B entry carries its persona tag, 2.12)
  file: path                           # corpus/workspaces/<id>/<workspace file>
  file_sha256: sha256
  saved_under_build: ref:build.build_id?   # tier B migration workspaces: the released build that saved it
  display_config: {width: int, height: int, scale_percent: int, theme: enum[dark|light], font_size: enum[small|medium|large]}
  account_role: enum[golden|empty] = golden   # the account the census checkpoints of this workspace select (4.9)
  tabs: list[str]                      # the top-level tab set in order, as the tab bar shows it; checked at workspace_loaded (7.5)
  windows: list[{title_regex: str, rect: {x: int, y: int, w: int, h: int}}]   # every top-level window, the main frame first; DWM rects compared within 4 px at workspace_loaded (7.5)
  link_groups: list[{color: str, symbol_ref: ref:fixture.symbols.id}]
  gadgets_present: list[ref:surface.id]   # the source of the derived hosted-surface list (hosted_by, 2.3) the census and scope computation use
  active_trader_panels: list[{panel_id: id, surface: ref:surface.id}]   # each one is attested for Auto Send off
  size_sweep_cells: list[{cell_id: id, hosts: ref:surface.id}]?        # tier C only
  owner: str
  notes: str?
""",
    "configs-allowed": """\
# schemas/configs-allowed.schema.json  (configs/allowed.yaml)
configs_allowed:
  schema_version: int = 1
  configs: list[allowed_config]

allowed_config:
  id: id
  width: int
  height: int
  scale_percent: int
  theme: enum[dark|light]
  font_size: enum[small|medium|large]
  work_area: {w: int, h: int}          # client area of a maximized main window on this configuration: MAXW and MAXH (2.5.2)
""",
    "flow": """\
# schemas/flow.schema.json (header)
flow:
  schema_version: int = 2
  flow_id: id                          # flow.<area>.<name>
  title: str
  class: enum[standard|login|fixture_reset|teardown|canary|paper_send_ok|explorer_candidate]
  tier: enum[1|2|3]
  owner: str
  persona: enum[active_trader|options_trader|futures_trader|swing_trader|beginner]
  surface: ref:surface.id              # primary surface
  surfaces: list[ref:surface.id]       # every surface a checkpoint targets
  requires: list[entitlement enum]
  workspace: ref:workspace.id
  fixture: ref:fixture.fixture_id
  account_role: enum[golden|empty] = empty   # the fixture account the flow selects (4.9)
  state_scope: id?                     # flows touching the same server-side state share it; such flows run in one shard (2.11)
  requires_window: enum[none|all_quiet|equities_quiet|futures_break] = none   # the quiet window of schedule/windows.yaml the flow runs in (7.10); a checkpoint may override
  data_mode: enum[frozen|masked_live]  # default for checkpoints that do not override
  agent_fallback: enum[allowed|disabled]
  budget: {max_duration_s: int, max_steps: int}
  origin: {kind: enum[authored|recorder|explorer], run_id: id?, finding_ref: id?}?
  normalize: list[step]                # steps run before a rescued or re-captured checkpoint (reset scroll, reselect sub-tab, park, focus)
  preconditions: list[step]            # only type precondition
  steps: list[step]
  teardown: list[step]
""",
    "checkpoint_step": """\
checkpoint_step:
  type: checkpoint
  checkpoint_id: id                    # flow/<surface_id>/<flow_id>/<this step's id>, unique across the library (grammar in 4.1)
  surface_id: ref:surface.id           # must exist in the registry and be listed in the flow's surfaces
  target_window: {kind: enum[main|dialog|detached|popup], title_regex: str?}   # required
  data_mode: enum[frozen|masked_live]  # required; may differ from the flow default
  regions: ref:regions_file | default  # default = whole-surface diff, allowed only for frozen
  tier: enum[1|2|3]?                   # overrides the surface tier
  max_settle_ms: int?                  # overrides the surface settle
  recapturable: bool = true            # false for states a re-capture would destroy (a ticket after Cancel); 3.10
  keep_focus: bool = false             # the state needs a focused field: settle skips its focus-neutral move and a caret mask is allowed (3.5, L-RGN-07)
  requires_window: enum[none|all_quiet|equities_quiet|futures_break]?   # overrides the flow's requires_window (7.10)
  account_state: bool?                 # overrides the surface flag (4.5)
  required_labels: list[str]?          # adds to the surface's labels for INV-05 at this state
  expect: list[assertion]?             # evaluated by the Oracle right after capture; same as an assert step here
""",
    "assertion": """\
assertion:
  id: id
  kind: enum[equals|matches|within_tol|present|absent|changed_since_previous_step|unchanged_since_previous_step|count|export_equals|api_within|rule|sign_color_agrees]
  source: enum[ocr|export|jab|api] = ocr
  region: ref:region.id?               # required for all kinds except rule
  expected: str|num?                   # equals, within_tol, count
  range: {min: num, max: num}?         # count
  value_ref: str?                      # fixture path, alternative to expected
  pattern: str?                        # matches, absent, present
  format: enum[money|price|int|percent|time|text]?
  tol: num?                            # within_tol, export_equals, api_within, rule
  since_step: id?                      # changed_since_previous_step, unchanged_since_previous_step
  export_column: str?                  # export_equals: the CSV or Copy All column compared with the region
  api_field: str?                      # api_within: the market-data API field, read as of capture time minus the delay (2.8.2)
  rule_id: id?                         # rule; one of R01 to R10, the arithmetic invariants of 2.8.3
  inputs: map[str, region or num]?     # rule operands
""",
    "regions": """\
# schemas/regions.schema.json
regions_file:
  schema_version: int = 1
  checkpoint_id: id
  landmarks: list[{id: id, template: path, confidence_min: num = 0.92, search_rect: {x: int, y: int, w: int, h: int}?}]   # one per panel compared (3.7)
  regions: list[region]
  recalibrate: bool?                    # set by a pull request that changes a landmark template or a region rect of an approved checkpoint; the baseline is re-approved, not orphaned (L-XREF-10)

region:
  id: id                                # rgn.<...>
  type: enum[freeze|rule|mask|diff|redaction]   # redaction only in redaction/anchors.yaml (L-RGN-03)
  landmark: ref:landmark.id             # the landmark this region is cut from
  rect: {dx: int, dy: int, w: int, h: int}    # offset from that landmark's top-left corner
  panel: enum[order_confirmation|order_ticket|position_statement|account_info|other] = other   # mask budget class
  money_role: enum[qty|price|bp_effect|max_loss|send|cancel|delete]?   # the closed money-cell list: Qty, Price, Buying Power Effect, Max Loss and the Send, Cancel and Delete buttons; the ticket's Confirm and Send button is send; only on panels order_confirmation and order_ticket (L-RGN-08)
  money_cell: bool = false              # true exactly when money_role is set (L-RGN-08); diff (or freeze) with a zero floor on a flow checkpoint, rule on a census checkpoint, never mask (L-RGN-04). Account number and account holder name are identity fields, not money cells: redacted in Capture, never a comparison region, never diffed (6.10)
  identity_field: bool = false          # redaction only: the account number or the account holder name; excluded from the mask budget (6.10)
  fixture_dependency: str?              # freeze only: the fixture field that pins this region (3.8)
  floor_exception: {approved_by: str, approved_at: ts, owner: str, reason: str, ticket: str, measured_floor: num, expires_at: ts, edge_tolerance_px: int = 1}?   # diff only; 3.7; expires_at at most 30 days after approved_at (L-RGN-05)
  rule: rule?                           # required for type rule
  mask_ref: ref:mask.mask_id?           # required for type mask

rule:
  field: enum[price|int|money|time|text|percent]
  decimals: int?
  min: num?
  max: num?
  must_advance: bool?                   # time fields
  must_change_within_s: int?            # live quote cells during venue hours
  must_not_change_when_closed: bool?
  min_rows: int?
  pattern: str?
  api_window: {field: str, delay_s: int = 1200, tol_ticks: int = 1}?   # the API value as of capture time minus delay_s, within tol_ticks ticks (2.8.2)
""",
    "mask": """\
# schemas/mask.schema.json  (masks/<checkpoint_id>/<mask_id>.yaml)
mask:
  schema_version: int = 1
  mask_id: id
  checkpoint_id: id
  region_id: ref:region.id
  owner: str
  reason: str
  created_by: enum[human]               # the only value; the Reviewer may propose, never create
  created_run: id
  created_at: ts
  expires_at: ts                        # default created_at plus 30 days
  ticket: str?
  decision_ref: ref:decision.decision_id
""",
    "redaction": """\
# schemas/redaction.schema.json  (redaction/anchors.yaml)
redaction_file:
  schema_version: int = 1
  owner: str                            # the security contact (6.10)
  landmarks: list[{id: id, template: path, confidence_min: num = 0.92, search_rect: {x: int, y: int, w: int, h: int}?}]
  regions: list[region]                 # all of type redaction (L-RGN-03)
""",
    "allowlist": """\
# gateway/allowlist.yaml  (schemas/allowlist.schema.json)
allowlist:
  schema_version: int = 1
  rigs: list[{rig: id, account_ids: list[str], login_ref: str}]   # exact selector strings, one list per rig
  ondemand_virtual_account: {account_id: str, valid_while_template: path}?   # frozen branch B only (7.9)
""",
    "fixture": """\
# schemas/fixture.schema.json
fixture:
  schema_version: int = 1
  fixture_id: id                        # fx.<rig>.<purpose>
  rig: id
  login_ref: str                        # vault://... ; never the value
  owner: str                            # the human who seeds and re-seeds
  accounts: list[account]
  settings_profile: ref:profile.id      # Application Settings: confirmation on, Shift+click send off, Active Trader hotkeys off, no order hotkeys
  order_defaults_profile: ref:profile.id   # per asset class; Active Trader default quantity is a separate field in the profile
  data_mode:
    frozen: {provider: enum[backend_fixture|ondemand|none], timestamp: ts?, symbol_set: id?}
    masked_live: {delay_minutes: int = 20, realtime: bool = false}
  symbols: list[symbol]
  prices: list[{id: id, symbol_ref: ref:symbol.id, rule: str, value: num, computed_at: ts}]
  positions: list[position]
  watchlists: list[{name: str, symbols: list[ref:symbol.id]}]
  working_orders_expected: int = 0
  alerts_expected: int = 0
  epoch:
    epoch_id: sha256                    # over the canonical JSON of the other epoch fields; the fingerprint's data_epoch
    window_date: date
    dataset_id: str?                    # frozen branch A: the backend fixture dataset
    ondemand_timestamp: ts?             # frozen branch B
    front_month: map[str,str]           # per futures root
    nearest_monthly_expiration: date
    option_expirations: map[str, list[date]]?   # per option symbol
    data_delay: enum[delayed_20|realtime]
  churn_calendar: list[{date: date, event: enum[expiration|roll|holiday|earnings|ex_dividend|reseed_due], symbol_ref: ref:symbol.id?, action: enum[reapprove|skip_checkpoints|reseed|none]}]

account:
  account_id: str                       # exact selector string; ^D-[0-9A-Z]+$ sanity check; must appear in gateway/allowlist.yaml for this rig
  kind: enum[margin|ira]
  role: enum[golden|empty]
  cash: num                             # expected cash; verify_account_fixture compares within cash_tolerance
  cash_tolerance: num
  cash_floor: num?                      # paperMoney only: cash held small via Adjust Account so any stray send fails for buying power; omitted when paper_send_ok is enabled

symbol:
  id: id
  symbol: str                           # fully qualified: /MESZ26, AAPL, SPX
  asset: enum[equity|option|future|future_option|forex|index]
  purpose: list[enum[ticket|chart|chain|watchlist|ladder]]
  roll_date: date?                      # futures

position:
  account_ref: ref:account.account_id
  symbol_ref: ref:symbol.id
  qty: num
  avg_price: num
  seeded_by: str
  seeded_at: ts
""",
    "margin-rules": """\
# schemas/margin-rules.schema.json  (fixtures/margin_rules.yaml)
margin_rules_file:
  schema_version: int = 1
  pinned: bool = false                  # true once Phase 0 has read the formula per asset class
  brackets: list[{asset: enum[equity|option|future|future_option|forex|index], account: enum[margin|ira], of: enum[notional|initial_margin], low: num, high: num}]   # the share of `of` the Buying Power Effect may take, low and high inclusive
  initial_margin: map[str, num]?        # per futures root: the exchange initial margin per contract
""",
    "popup": """\
# schemas/popup.schema.json  (popups/catalog.yaml)
popup_catalog:
  schema_version: int = 1
  entries: list[popup]                  # a bare list without schema_version warns in Phase 0 and is an error from Phase 1

popup:
  popup_id: str                         # ^POP-[0-9]{2,}$: the starter ids of the 12.8 table, then further ids in sequence
  name: id                              # readable slug
  class: enum[dismissable|env|login|app_failure|safety|order_event]   # the policy class of 7.8
  title_regex: str
  seed_titles: list[str]?               # real window titles the entry must match; L-POP-05 compares entries over them
  template: path?                       # small identifying crop; required when action = click
  window_class_hint: str?               # e.g. SunAwtDialog; confirmed in Phase 0
  action: enum[dismiss_key|click|wait|relogin|abort|ignore]
  key: str?                             # dismiss_key; never enter, space or ctrl+enter
  target: ref:control?                  # click
  wait_timeout_ms: int?                 # wait; timeout falls through to on_abort
  on_abort: {outcome: enum[env_error|invariant_violation|aborted_run], reason: str?, gate: str?, invariant_id: id?, run_outcome: enum[aborted_safety|aborted_harness]?}?   # required for abort, relogin and wait
  severity: enum[info|medium|high|critical]
  expected_phases: list[enum[launch|login|workspace_load|running|teardown]]
  owner: str
  status: enum[verified|unverified|disabled]   # disabled entries are kept but never matched (Trade Flash until seen)
  retired: bool = false                 # a starter row of 12.8 explicitly retired: kept, never matched; requires retired_reason (L-XREF-12)
  retired_reason: str?
  first_seen_build: ref:build.build_id
  last_seen_build: ref:build.build_id?
  notes: str?
""",
    "mutant": """\
# schemas/mutant.schema.json  (canaries/mutants.yaml)
mutant_catalog:
  schema_version: int = 1
  entries: list[mutant]                 # a bare list without schema_version warns in Phase 0 and is an error from Phase 1

mutant:
  mutant_id: str                        # ^M[0-9]{2,}$: M01 to M33 as 12.5 tables them, then M34 onward in sequence
  name: id                              # readable slug
  checkpoint_id: id | "*"               # "*" only for class aa_control: every bound checkpoint (M25)
  class: enum[erased_control|swapped_digit|swapped_label|color_swap|shifted_panel|clipped_label|overlapped_panels|missing_row|header_change|placeholder|font_height|scrollbar|redaction|sub_floor|global_shift|landmark_loss|study_removed|aa_control|wrong_quantity|blank_field|gateway|outside_parent|window_offscreen|misaligned_column|focus_order|error_dialog]   # the 26 snake_case class names of the 10.3 class table (F7); 12.5's Class column and 8.3 canary_results.defect_class carry the same names
  mode: enum[image|behavioral]
  money_cell: bool                      # money cell or Send control: 100 percent detection required (the money_cell column of 12.5); identity fields are proven by the redaction canary instead (6.10)
  generator: {op: enum[erase|translate|replace_glyph|channel_swap|crop_right|overlay|remove_row|type_value], params: map[str, any], seed: int = 20260101}
  region_ref: ref:region.id?            # required unless checkpoint_id is "*"
  canary_flow: ref:flow.flow_id?        # behavioral only
  expected: {outcomes: list[checkpoint outcome enum], min_confidence: num?, invariant_id: id?, assertion_id: id?, reason: str?, step_status: enum[passed|passed_after_retry|passed_via_agent_fallback|failed|blocked_by_gateway]?, redaction_hit: bool?}   # reason constrained by class (L-MUT-02); step_status for gateway mutants (M28); redaction_hit for redaction mutants (M19)
  owner: str
""",
    "fingerprint": """\
# schemas/fingerprint.schema.json
fingerprint:
  schema_version: int = 2
  fingerprint_id: sha256                # over the canonical JSON of match, with build_id removed
  run_id: id
  side: enum[candidate|base]
  rig_id: id
  read_at: ts
  match:                                # 7.4 class match; the field names are 7.4's
    build_id: ref:build.build_id        # must equal the build requested for this side, not the other side's
    jre_release: {sha256: sha256, java_version: str}       # bundled jre/release file, full text hashed, JAVA_VERSION parsed
    vmoptions_hash: sha256
    java2d_pipeline: {pinned: enum[d3d|opengl|software], confirmed: enum[d3d|opengl|software|unknown]}   # vmoptions flag; jcmd or Help > About render, best effort
    windows_build: {current_build: str, ubr: str}
    display: {width: int, height: int, bits: int, frequency: int, scale_percent: int, dpi: int, awareness: str, monitors: int}
    cleartype: {smoothing: bool, type: str, gamma: int, contrast: int, orientation: str}
    gpu: {name: str, driver_version: str}
    os_theme: {apps_light: bool, system_light: bool, transparency: bool, animations: bool}
    app_theme: {scheme: enum[dark|light], font_size: enum[small|medium|large]}   # color scheme and font size from the attestation OCR of Application Settings
    workspace_hash: map[id, sha256]     # per corpus id, of the file as copied into usergui
    settings_profile_hash: sha256       # the checked-in Application Settings and Order Defaults profiles 7.6 re-applies
    time_zone: {os: str, app_display: str, chart: str}     # all America/New_York
    keyboard: {layout: str, layout_count: int, toggle_keys: {caps: bool, num: bool, scroll: bool}, locale: str}
    data_epoch: {epoch_id: ref:fixture.epoch.epoch_id, data_mode: enum[backend_fixture|ondemand|none]}   # the fixture epoch of 4.9 and the frozen data provider in force (4.9 data_mode.frozen.provider, 7.9)
    input_desktop: str                  # OpenInputDesktop name; must be Default
    harness: {version: str, ocr_engine: str, ocr_model: str, capture_lib: str, settle_params_sha256: sha256, diff_engine_params_sha256: sha256, redaction_version: str, region_registry_version: str, config_commit: str, config_tarball_sha256: sha256, allowlist_sha256: sha256}
    render_probe_hash: str              # 64-bit pHash of the Help > About text region after authenticated (3.3); Hamming 0 required
  record:                               # 7.4 class record
    trading_24h_list_sha256: sha256?    # hash of the 24-hour trading list read this run (7.4, 7.10); not part of epoch_id
    jar_listing_hash: sha256            # sorted names and sizes under the install folder
    account: {account_id_sha256: sha256, login_sha256: sha256, role: enum[golden|empty]}   # redacted
    rig: {hostname: str, hypervisor: str, hypervisor_version: str, snapshot_ids: list[str], vm_image_id: str}
    run_date: date                      # America/New_York
""",
    "outcome": """\
# schemas/outcome.schema.json
checkpoint_outcome:
  outcome_id: id                        # oc-<run_id>-<zero-padded sequence>
  run_id: id
  pair_id: ref:build_pair.pair_id
  checkpoint_id: id                     # grammar of 4.1; the kind segment is the test kind
  surface_id: ref:surface.id
  test_kind: enum[census|sweep|split|flow]          # equals the id's kind segment
  flow_id: ref:flow.flow_id?
  flow_class: enum[standard|login|fixture_reset|teardown|canary|paper_send_ok|explorer_candidate]?
  step_id: id?
  workspace_id: ref:workspace.id
  persona: enum[active_trader|options_trader|futures_trader|swing_trader|beginner]
  profile_id: id                        # the entitlement profile of the login: the matrix column of 2.12
  size_step: {host: str, w: int, h: int}?           # sweep; split carries {splitter: id, fraction: num}
  data_mode: enum[frozen|masked_live]
  fingerprint_id: sha256?               # required unless the record references no capture artifact (below)
  data_epoch_id: sha256?                # as fingerprint_id
  cand_rig_id: id?                      # the rig that captured each side
  base_rig_id: id?
  settle_ms: int?                       # candidate time to settle (3.5)
  outcome: enum[identical|noise|intended|collateral|needs_human|invariant_violation|assertion_failed|not_settled|not_comparable|landmark_missing|env_error|harness_error|not_run|unregistered_surface|needs_baseline]
  reason: str?                          # <code>[:<param>] with code from taxonomy.yaml reasons.<outcome>; see the vocabulary table
  detail: str?                          # free text the reason points at: field names, step id, expected and extracted values
  side: enum[candidate|base|both]?      # landmark_missing, not_settled, needs_baseline: where the condition held
  gate: str?                            # env_error: the gate from taxonomy.yaml gates, including preflight.<item>
  review_state: enum[none|pending|done] = none      # pending only with needs_human awaiting the Reviewer (8.3)
  rescued: bool = false                 # captured after a passed_via_agent_fallback step (5.5)
  paper_assertion: {account_id_sha256: sha256, mode: str, asserted_at: ts}?   # the stamp of 6.4 with the mode read; required unless the record references no capture artifact (L-OUT-06)
  app_uptime_s: int?
  app_restart_count: int?
  diff: {whole: {pixel_fraction: num, ssim: num, shift: {dx: int, dy: int}}, regions: list[{region_id: id, pixel_fraction: num, ssim: num, above_threshold: bool}], components: list[{area: int, bbox: {x,y,w,h}}]}?
  diff_pixels: int?                     # changed pixels over the whole window
  masked_pct: num?                      # masked plus non-identity redacted share of the critical panel (4.8 mask budget)
  threshold_source: enum[calibrated|uncalibrated]?
  classification: {category: enum[intended|collateral|noise|needs_human], by: enum[rule|reviewer], confidence: num?, model_id: str?, prompt_hash: sha256?, description: str?, region_pointer: id?}?
  lower_results: list[{outcome: checkpoint outcome enum, reason: str?}]?   # the outcomes the 1.10 precedence ranked below this one, kept as evidence ("also applied")
  manifest_entry_ref: ref:entry.entry_id?
  primitive: ref:primitive.id?
  invariant_ids: list[id]?
  assertion_ids: list[id]?
  artifacts: {base: sha256?, candidate: sha256?, heatmap: sha256?, crops: list[sha256]?}   # content-addressed, redacted
  recaptures: int = 0                   # targeted re-captures taken (3.10)
  stability: enum[stable|unstable|unknown]?          # 3.10; unstable is the not_settled (reason: unstable) case
  stability_cause: enum[not_recapturable|budget]?    # with stability unknown
  base_source_run_id: id?               # frozen base capture reused from this run (3.1)
  decision_ref: ref:decision.decision_id?
  synthetic: bool = false
  mutant_id: str?                       # synthetic records: the mutant of 4.11 (synthetic mutant outcomes are stored apart, 8.3)
  captured_at: ts?
  recorded_at: ts

step_outcome:
  run_id: id
  flow_id: ref:flow.flow_id
  step_id: id
  attempt: int
  status: enum[passed|passed_after_retry|passed_via_agent_fallback|failed|blocked_by_gateway]
  reason: str?                          # export steps only: export_timeout or export_unavailable (4.7 export)
  mode: enum[scripted|agent|explorer]   # the gateway profile the step ran under (5.4)
  locator_rung: enum[jab|image|ocr|agent|none]?
  rung_results: list[{rung: enum[jab|image|ocr|agent], result: str, score: num?}]?   # every rung tried
  duration_ms: int
  app_uptime_s: int?
  app_restart_count: int?
  pre_screenshot: sha256?
  post_screenshot: sha256?
  click_crop: sha256?                   # the crop around the click point (6.2 audit trail)
  gateway_event: {decision: enum[emitted|dropped|refused|late_up|none], reason: str?, class: enum[allowed|safety|integrity|none]?}   # decision: dropped by the hook, refused by the slow half; reason is the hook or slow-half code of 5.4.3 and 5.4.4 (no_token, token_mismatch_coords, token_expired, deny_rect, shift_click_dialog, dialog_not_ok, blocked_key_dialog, blocked_chord_dialog, move_outside_allow, deny_all, not_armed, foreign_input, untokened_injected, never_tokenize, dialog_allowlist, deny_profile, guarded_key_refused, hook_host_dead, hook_reinstalled), stored as the code, never a coarse class; class is the safety or integrity split of 5.4.4 (F17). 8.3 stores these as step_events.gate_decision, gate_reason and gate_class
  finding_ref: id?                      # failed or passed_via_agent_fallback: the locator_failure finding
  recorded_at: ts

run_outcome:
  run_id: id
  pair_id: ref:build_pair.pair_id
  run_type: enum[pr|merge|rc|nightly|calibration]
  status: enum[complete|complete_with_env_errors|aborted_safety|aborted_budget|aborted_harness]
  reason: str?                          # required for aborted_*; from taxonomy.yaml reasons.<status>
  last_gate: str                        # the Orchestrator state reached
  counts: map[checkpoint outcome enum, int]
  summary: str                          # the report's one-line summary (9.2)
  coverage_matrix_ref: sha256
  egress_log_ref: sha256
  report_sha256: sha256?                # the published report
  comment_sha256: sha256?               # PR runs: the posted comment body
  post_run_proof_ref: sha256?           # the Working and Filled diff of 6.6; UNPROVEN makes status complete_with_env_errors
  base_commit_distance: int?            # PR runs: commits between the merge base and the base build used, 0 to 20 (4.2)
  authorization_sha256: sha256          # the authorization version in force (6.12, L-XREF-16)
  started_at: ts
  ended_at: ts

finding:
  finding_id: id
  run_id: id
  checkpoint_id: id?                    # absent for run-level findings
  kind: enum[finding kinds of taxonomy.yaml]        # the table below
  tier: enum[1|2|3]?
  region_id: id?
  primitive: ref:primitive.id?
  manifest_entry_ref: ref:entry.entry_id?
  phash: str?                           # 16 hex, of the owning region's anchored bounds (9.7)
  category: enum[intended|collateral|noise|needs_human|invariant_violation|assertion_failed]?
  pre_existing: bool = false            # an invariant violation present on both builds, one severity lower (2.7)
  cluster_id: id?                       # the signature cluster of 9.7
  description: str
  evidence: {artifacts: list[sha256], step_events: {first_seq: int, last_seq: int}?, raw_values: map[str, str]?}
  explorer: {session_id: id, signature: {surface_id: id, observation: str, crop_sha256: sha256}, severity: enum[low|medium|high], confidence: num, trace_ref: sha256, replayed: enum[not_replayed|reproduced|not_reproduced|lint_failed], candidate_flow: ref:flow.flow_id?}?   # explorer_candidate only (2.9)
  triage_state: enum[open|confirmed_bug|intended|noise|agent_mistake|expected_behavior|fixed_pending_verify|closed] = open
  contested: bool = false               # a supersession of its decision is contested (9.7)
  held: bool = false                    # an Explorer finding held, not reported, under an agent_mistake or expected_behavior decision (9.6)
  decision_ref: ref:decision.decision_id?
  ticket: str?
  first_seen_run_id: id
  created_at: ts
  closed_at: ts?                        # when triage_state left the states that pin evidence (to closed, noise, agent_mistake or expected_behavior); its evidence is released 180 days later (8.6)
""",
    "decision": """\
# schemas/decision.schema.json
decision:
  decision_id: id                       # dec-<zero-padded sequence>
  signature:
    checkpoint_id: id
    region_id: id | grid_cell           # region id, or g-<row>-<col> on the 8x8 grid of 9.7 for a diff outside any region
    phash: str                          # 64-bit perceptual hash of the owning region's anchored bounds (9.7), hex; component-crop hashes serve clustering only
    hamming_tolerance: int?             # unset: the default applies, 8, or 6 for tier-1 checkpoints; set when written out or lowered by calibration (9.7, L-DEC-03); 6 is also the cross-checkpoint clustering radius (3.7, 9.7)
    bbox_q4: {x: int, y: int, w: int, h: int}?   # the largest component's bounding box quantized to 4 px: the exact-match key when the region falls back to pHash equality (below)
    category: enum[intended|collateral|noise|needs_human|invariant_violation|assertion_failed]
  action: enum[confirm_bug|mark_intended|request_mask|noise_signature|agent_mistake|expected_behavior|approve_baseline|reject_baseline|fixed_in_build]
  verdict: enum[confirmed_bug|intended|noise]
  decided_by: str                       # corporate login; never a service account
  role: enum[triage_rotation|flow_owner|system_owner|release_manager|pr_author|ticket_resolver]   # the role under which decided_by acted (9.12, L-DEC-01)
  source: enum[report_ui|pr_comment|jira_sync|explorer_triage] = report_ui   # where the action was taken (8.3 decisions.source); jira_sync: a fixed_in_build row the Jira poller wrote with the ticket resolver's login (9.9)
  confirmed_by: str?                    # second human; required by the two-person rule of 9.6
  decided_at: ts
  run_id: id
  build_id: ref:build.build_id
  finding_ref: ref:finding.finding_id
  ticket: str?                          # required per the mapping table
  reason: str
  mask_ref: ref:mask.mask_id?           # request_mask, set when the mask file lands
  baseline_ref: sha256?                 # approve_baseline, reject_baseline
  fixed_in_build: ref:build.build_id?   # fixed_in_build
  expires_at: ts?                       # noise_signature, request_mask and waived mark_intended: default 30 days
  cluster_id: id?                       # when written per member of a cluster decision (9.7)
  status: enum[active|expired|superseded|reopened|revoked]
  reopen_count: int = 0                 # times the signature returned after fixed_in_build; two make the decision suspect (9.5)
  supersedes: ref:decision.decision_id?
  revoked: {by: str, at: ts, reason: str}?
""",
    "dismiss-allowlist": """\
# schemas/dismiss-allowlist.schema.json  (gateway/dismiss_allowlist.yaml)
dismiss_allowlist:
  schema_version: int = 1
  labels: list[str]                     # exact labels: Delete, plus Edit only when Phase 0 observes it; empty until read
  read_on_build: ref:build.build_id?    # the build whose Order Confirmation dialog Phase 0 read
  read_by: str?
  read_at: ts?
""",
    "default-order-hotkeys": """\
# schemas/default-order-hotkeys.schema.json  (gateway/default_order_hotkeys.yaml)
default_order_hotkeys:
  schema_version: int = 1
  build: ref:build.build_id             # the pinned build whose default hotkey table was read
  verified: bool = false                # false while the chords are placeholders before the Phase 0 read
  read_by: str?
  read_at: ts?
  chords: list[{chord: str, action: str}]   # e.g. "ctrl+shift+b" and the order action the default table binds to it
""",
    "explorer-deny-list": """\
# schemas/explorer-deny-list.schema.json  (policy/explorer_deny_list.yaml)
explorer_deny_list:
  schema_version: int = 1
  approved_by: str                      # the Explorer owner (6.8)
  entries: list[deny_entry]

deny_entry:
  id: id
  denied: str                           # the 6.8 table row it implements, e.g. "Share and tos.mx links"
  why: str
  window_title_regex: str?              # a matching window is closed with a tokened Escape
  label_regex: str?                     # matched against the resolved control name and the under-point OCR label
  control: ref:control?                 # <surface id>.<control id>
  surface: ref:surface.id?              # every control of the surface
  order_path: bool = false              # a drag that starts or ends here is denied as an order path (6.2)
""",
    "egress-allowlist": """\
# schemas/egress-allowlist.schema.json  (policy/egress_allowlist.yaml)
egress_allowlist:
  schema_version: int = 1
  hosts: list[{host: str, port: int = 443, purposes: list[str]}]   # exact host names; purposes from the egress_log purpose list of 6.10
  signed_by: str                        # the security contact (6.10, 6.12)
  signed_at: ts
  signature: str
""",
    "computer-use-ruling": """\
# schemas/computer-use-ruling.schema.json  (policy/computer_use_ruling.yaml)
computer_use_ruling:
  schema_version: int = 1
  status: enum[open|ruled]              # open until security answers DR-07
  endpoint: str?                        # the approved model endpoint host
  retention: enum[zero_data_retention|standard]?
  approved_model_tiers: list[str]?
  model_ids: list[str]?                 # the pinned model ids the ruling covers
  toolset_schema_version: str?          # the harness toolset schema version (5.6)
  reviewer_covered: bool = false        # the Reviewer's model is approved
  computer_use_covered: bool = false    # agent mode and the Explorer are approved: branch A
  signed_by: str?                       # the security contact
  signed_at: ts?
  ticket: str?
""",
    "authorization": """\
# schemas/authorization.schema.json  (authorization/<version>.yaml)
authorization:
  schema_version: int = 1
  version: str                          # equals <version> in both file names
  document_sha256: sha256               # of authorization/<version>.md
  signatures: list[{role: enum[platform_owner|security_contact|compliance_contact], name: str, signed_at: ts}]
  account_ids: list[str]                # the allowlisted paper account ids
  rig_hostnames: list[str]
  automation: list[str]                 # processes, injection, hooks, capture, the drill injector
  model_endpoint: str
  model_terms: str
  data_classes: list[str]               # the data classes that may leave the rig
  rate_caps: {logins_per_day: int, symbol_changes_per_hour: int, staged_orders_per_hour: int, staged_orders_per_day: int}   # 6.4: 40, 600, 60 and 300
  send_policy_branch: enum[default|gated]
  supersedes: str?                      # the previous version
""",
    "budgets": """\
# schemas/budgets.schema.json  (policy/budgets.yaml)
budgets:
  schema_version: int = 1
  reviewer_items: {pr: int = 60, merge: int = 200, rc: int = 500}   # 3.15; items beyond are needs_human (reason: reviewer_cap)
  rescue: {max_actions: int = 25, max_seconds: int = 90, max_rescued_steps_per_run: int = 25}   # 5.5; past a limit the rescue ends and the step fails
  usd: {per_explorer_session: num = 25, per_run: num = 50, per_week: num = 500}   # 11.4; only per_run ends a run (aborted_budget, reason: dollar_cap)
  approved_by: str?                     # the system owner, for a value above 11.4's
""",
    "release-gate": """\
# schemas/release-gate.schema.json  (policy/release_gate.yaml)
release_gate:
  schema_version: int = 1
  default_business_days: int = 3        # N, PA-14 of 11.1
  trains: list[{train: str, ga_date: date, rc_cut_date: date, business_days: int?}]   # business_days overrides N for the train
  owner: str                            # the release manager (9.12)
""",
    "reporting": """\
# schemas/reporting.schema.json  (policy/reporting.yaml)
reporting:
  schema_version: int = 1
  check_mode_history: list[{mode: enum[informational|blocking], since: date, by: str}]   # the commit status mode of 9.2; every change dated and announced in the release channel
  jira: {product_project: str, harness_project: str}
  slack: {on_call: str, triage: str, release: str, safety: str}   # the four channels of 9.9
  paging: {service: str, escalation_policy: str?}
  email: list[str]?                     # digest recipients
  auto_file: {tier1: bool = false, tier2: bool = false, tier3: bool = false}   # auto-filing of collateral per tier, off by default (9.9)
""",
    "windows": """\
# schemas/windows.schema.json  (schedule/windows.yaml)
windows_file:
  schema_version: int = 1
  operating_day: {start: str, end: str}   # "06:00" and "22:00", America/New_York
  windows: {all_quiet: quiet_window, equities_quiet: quiet_window, futures_break: quiet_window, none: quiet_window}
  maintenance_windows: list[{weekday: enum[Mon|Tue|Wed|Thu|Fri|Sat|Sun], start: str, end: str, source: str}]   # a preflight N1, N3 or N4 failure inside one reschedules the run once (7.11)

quiet_window:
  start: {calendar: enum[XNYS|CMES]?, weekday: enum[Mon|Tue|Wed|Thu|Fri|Sat|Sun], time: str}?
  end: {calendar: enum[XNYS|CMES]?, weekday: enum[Mon|Tue|Wed|Thu|Fri|Sat|Sun], time: str}?
  rule: str?                            # a calendar rule in words that the scheduler resolves (equities_quiet, futures_break)
""",
    "oncall": """\
# schemas/oncall.schema.json  (ops/oncall.yaml)
oncall:
  schema_version: int = 1
  roles: list[{role: enum[system_owner|accountable_owner|risk_owner|security_contact|explorer_owner|accounts_owner|release_manager|registry_owner], person: str, deputy: str}]   # 6.12, 11.6
  flow_owners: list[{tier: enum[1|2|3], person: str, deputy: str}]
  rotations: list[{rotation: enum[triage|harness_oncall], person: str, from: date, to: date}]   # one person per week each (9.12)
  triage_roster: list[str]              # the logins that may decide findings (L-MSK-01, L-DEC-01, L-DEC-06)
""",
    "credential-rotation": """\
# schemas/credential-rotation.schema.json  (ops/credential_rotation.yaml)
credential_rotation:
  schema_version: int = 1
  logins: list[{login_ref: str, owner: str, backup_owner: str, sms_device: str?}]   # login_ref as in gateway/allowlist.yaml; sms_device: the team-owned device under DR-03 branch B
  rotations: list[{login_ref: str, kind: enum[password|vault_token], window: {start: ts, end: ts}, owner: str, validating_run_id: id?}]   # validating_run_id: the login-only canary run of 6.9
""",
    "process-allowlist": """\
# schemas/process-allowlist.schema.json  (rig/process_allowlist.yaml)
process_allowlist:
  schema_version: int = 1
  image_id: str                         # the golden image the baseline was read from (7.2)
  harness: list[str]                    # image names of the Orchestrator, the Gateway hook host, the Watchdog and the elevated helper service (5.1)
  app_tree: list[str]                   # the thinkorswim process tree
  windows_baseline: list[str]           # the golden image's Windows processes
""",
    "measured-sizes": """\
# schemas/measured-sizes.schema.json  (corpus/measured_sizes.yaml)
measured_sizes:
  schema_version: int = 1
  measured_on_run: id                   # the first validated run whose probe P8 measured them
  sizes: list[{surface: ref:surface.id, workspace: ref:workspace.id, host: enum[flexgrid|detached|dialog], w: int, h: int}]
""",
    "palette": """\
# schemas/palette.schema.json  (palettes/<theme>.yaml)
palette:
  schema_version: int = 1
  theme: enum[dark|light]               # equals <theme> in the file name
  colors: {positive: str, negative: str, neutral: str}   # "#rrggbb", read from the pinned build in Phase 0
  tolerance: num = 60                   # largest RGB distance at which a cell color counts as an entry; a color farther from every entry is not judged (INV-15)
""",
    "soak": """\
# schemas/soak.schema.json  (soak.yaml)
soak:
  schema_version: int = 1
  calibrated_from: id?                  # the lineage soak run the budgets were calibrated from (10.11)
  budgets:                              # one per soak.<metric> assertion of 10.11; the values shown are the defaults
    working_set: {growth_pct_max: num = 25, growth_mb_max: num = 1536}
    growth_slope: {window_h: num = 3, mb_per_h_max: num = 60}
    gdi_objects: {max: int = 9000}
    handles: {within_pct: num = 20}
    threads: {within_pct: num = 20}
    heap: {pct_of_xmx_max: num = 80}
    freezes: {max: int = 0}
    relaunches: {max: int = 0}
    render_latency: {tick_window_s: int = 2, windows_pct_min: num = 99}
""",
    "calibration": """\
# schemas/calibration.schema.json  (calibration/<hex>.yaml)
calibration_file:
  schema_version: int = 1
  calibration_key: sha256               # the key of 4.12; <hex> in the file name is this without its sha256: prefix
  fingerprint_id: sha256                # the fingerprint the Calibration run captured on
  calibration_run_id: id
  exported_at: ts
  checkpoints: list[{checkpoint_id: id, settle_p50_ms: int, regions: list[calibration_region]}]

calibration_region:
  region_id: str                        # a region id, or _rest
  floors: {changed_px: int, component_area: int, delta_e: num, shift_px: int}   # the maximum over the 15 A/A pairs (3.9 step 4)
  thresholds: {component_area_min: int, changed_px_min: int, delta_e_min: num, shift_px_min: int}   # derived by 3.9
  ssim_min: num?                        # ranking only; no threshold
  noise_spread: int?                    # inputs of the tolerance rule of 9.7 and 4.14
  fault_separation: int?
  hamming_tolerance: int?
  exact_fallback: bool = false
  floor_exception: bool = false         # thresholds derived from a non-zero floor under a floor_exception (4.8)
""",
    "quarantine": """\
# schemas/quarantine.schema.json  (quarantine.yaml)
quarantine_file:
  schema_version: int = 1
  records: list[quarantine_record]

quarantine_record:
  checkpoint: id?                       # exactly one of checkpoint and flow
  flow: ref:flow.flow_id?
  owner: str
  ticket: str
  entered: date
  reason: enum[flake|aa_drift|aa_floor|maintenance_budget|fire_drill|safety_canary|kill_switch|gateway_drops|fallback_rate]
  detail: str?
""",
    "eval-report": """\
# schemas/eval-report.schema.json  (eval/reports/<hex>.yaml)
eval_report:
  schema_version: int = 1
  reviewer_version: {model_id: str, prompt_sha256: sha256, schema_version: int, few_shot_ids: list[str], effort: str, request_settings: map[str, any], crop_policy_version: str, redaction_version: str}   # the tuple of 3.15
  reviewer_version_sha256: sha256       # over the canonical JSON of reviewer_version; <hex> in the file name
  eval_set_version: str                 # eval/<eval_set_version>/labels.jsonl
  split: enum[held_out]
  run_at: ts
  gates: list[{gate: str, value: num, threshold: num, passed: bool}]   # every gate of 3.14
  canary_run_id: id?                    # the mutant catalog re-run of 3.15 before the first product run on the tuple
""",
    "branches": """\
# schemas/branches.schema.json  (phase0/branches.yaml)
branches:
  schema_version: int = 1
  jab: branch_choice                    # DR-01 Java Access Bridge exposure
  data_fixture: branch_choice           # DR-02 deterministic data fixture
  login_mfa: branch_choice              # DR-03 login and MFA
  pinned_install: branch_choice         # DR-04 pinned build install; carries title_regex once read
  aa_floor: branch_choice               # DR-05 A/A noise floor
  session_rule: branch_choice           # DR-06 session rule (7.7)
  computer_use: branch_choice           # DR-07; the ruling itself is policy/computer_use_ruling.yaml
  batch_api: enum[on|off|undecided] = undecided   # 3.15: off while the ruling requires zero data retention
  reviewer: {model_id: str?}            # the pinned model id (3.15)
  version_read: {title_regex: str?, about_regex: str?}   # how the build's version is read (4.2, 7.3)
  safety: list[{item: int, result: enum[confirmed|different|undecided], observed: list[str]?, note: str?, read_on_build: ref:build.build_id?}]   # the ten Phase 0 assumptions of 6.13; item 10 lists the dialog labels observed

branch_choice:
  branch: enum[A|B|undecided]
  decided_by: str?
  decided_at: date?
  deadline: date?                       # required while undecided (11.8)
  title_regex: str?                     # pinned_install only
""",
}

# The step table of 4.7 written in the notation of 4.1 (choice 5). `step_common` holds the fields
# every step has; `step_<type>` the fields of one type; a checkpoint step takes `checkpoint_step`.
STEP_NOTATION = """\
step_common:
  id: id
  type: enum[click_named|type_fixture|press|drag|wheel|rclick|dblclick|key_chord|reset_input_state|secret|wait_for|park_mouse|focus_neutral|export|checkpoint|assert|precondition|teardown]
  timeout_ms: int = 5000
  on_fail: enum[fail|retry_once|teardown] = fail
  order_stage: bool = false
  note: str?
step_click_named:
  target: ref:control
  button: enum[left|right|middle] = left
step_type_fixture:
  target: ref:control
  value_ref: str?
  value: str?
  commit: enum[enter|tab|none]
  verify: assertion?
step_press:
  key: str
step_drag:
  from: ref:control | {x: int, y: int}
  to: ref:control | {x: int, y: int}
  steps: int
  hold_ms: int
step_wheel:
  at: ref:control | {x: int, y: int}
  notches: int
step_rclick:
  target: ref:control
step_dblclick:
  target: ref:control
step_key_chord:
  keys: list[str]
step_secret:
  target: ref:control
  vault_ref: str
  commit: enum[enter|tab|none]
step_wait_for:
  anchor: {landmark: ref:landmark.id?, window_title_regex: str?}
  anti_anchors: list[str]?
  timeout_ms: int
step_focus_neutral:
  target: ref:control?
step_export:
  target: ref:control
  kind: enum[file|copy_all]
  path: path?
  headers: list[str]
step_assert:
  assertions: list[assertion]
step_precondition:
  check: enum[paper_mode|workspace_loaded|no_modal|single_main_frame|main_window_rect|fixture_symbol_linked|working_orders|settings_attested|app_alive]
  expect: any?
step_teardown:
  action: enum[close_dialog|clear_ticket|close_non_main_windows|reset_ui|reload_workspace|reset_profile|cancel_all|working_orders_check|positions_flat_check]
  target: ref:control?
  expect: any?
"""

# Shapes chosen here for files 4.1 names but section 4 does not write (choices 7 and 8).
AUX_NOTATION = """\
build_file:
  schema_version: int = 1
  build: build?
  build_pair: build_pair?
ladders_file:
  schema_version: int = 1
  ladders: list[ladder]
  splitter_sets: {full: list[num], short: list[num]}
ladder:
  id: id
  name: enum[S|F]
  run_types: list[enum[pr|merge|rc|nightly|calibration]]
  corpus_sizes: bool = false
  steps: list[{w: int | enum[MAXW], h: int | enum[MAXH], part: enum[grid|corner|ascending] = grid}]
renames_file:
  schema_version: int = 1
  renames: list[{old: str, new: str, build: ref:build.build_id}]
send_policy:
  schema_version: int = 1
  decision: enum[OPEN|approved|declined]
  enabled: bool = false
  risk_owner: str?
  signed_at: ts?
  signature: str?
explorer_budget:
  schema_version: int = 1
  max_actions: int
  max_minutes: int
  max_no_change_actions: int
  max_usd_per_session: num
  max_usd_per_week: num
  approved_by: str?
profile:
  schema_version: int = 1
  id: id
  kind: enum[app_settings|order_defaults]
  owner: str
  settings: map[str, any]?
  defaults: map[str, any]?
"""

# Notation parts that are not files of their own: the flow's checkpoint step and assertion blocks (4.7).
_PART_OF_FLOW = ("checkpoint_step", "assertion")
# The root record of a schema whose file is not its part's first record (choice 7).
_ROOT_OVERRIDES = {"build": "build_file"}


def _spec_schemas() -> dict[str, str]:
    """schema file name -> root record: every `# schemas/<name>.schema.json` part of section 4, rooted at its
    first record (4.1: one schema per record type; a part with several records carries the others in `$defs`)."""
    out = {}
    for name, block in NOTATION.items():
        if name not in _PART_OF_FLOW:
            out[name] = _ROOT_OVERRIDES.get(name) or next(iter(parse_notation(block).records))
    return out


AUX_SCHEMAS: dict[str, str] = {
    "ladders": "ladders_file",
    "renames": "renames_file",
    "send-policy": "send_policy",
    "explorer-budget": "explorer_budget",
    "profile": "profile",
}

# The 23 rows of the 7.4 fingerprint table: twenty-two fields plus render_probe_hash (3.3), with
# trading_24h_list_sha256 (SD-38); L-FPR-01 keeps the 4.12 schema equal to this list, and a test keeps
# this list equal to the 7.4 table.
FINGERPRINT_FIELDS_7_4: tuple[str, ...] = (
    "build_id", "jre_release", "vmoptions_hash", "jar_listing_hash", "java2d_pipeline", "render_probe_hash",
    "windows_build", "display", "cleartype", "gpu", "os_theme", "app_theme", "workspace_hash",
    "settings_profile_hash", "time_zone", "keyboard", "data_epoch", "trading_24h_list_sha256", "account",
    "input_desktop", "rig", "harness", "run_date",
)

_STR = {"type": "string"}
_SLUG = {"type": "string", "pattern": SLUG_PATTERN}
_LOCAL = {"type": "string", "pattern": LOCAL_ID}
_CHECKPOINT = {"type": "string", "pattern": CHECKPOINT_ID.pattern}
_SHA256 = {"type": "string", "pattern": SHA256}

_PRIMS: dict[str, dict] = {
    "str": _STR,
    "int": {"type": "integer"},
    "num": {"type": "number"},
    "bool": {"type": "boolean"},
    "ts": {"type": "string", "pattern": TS},
    "date": {"type": "string", "pattern": DATE},
    "sha256": _SHA256,
    "path": {"type": "string", "pattern": REL_PATH},
    "id": _SLUG,
    "any": {},
    "glob": {"type": "string", "pattern": GLOB},
    "grid_cell": {"type": "string", "pattern": GRID_CELL},
}

# ref:<record>.<field> -> the shape of the referenced identifier (resolution is a lint rule)
_REFS: dict[str, dict] = {
    "build.build_id": _SLUG,
    "surface.id": _SLUG,
    "workspace.id": _SLUG,
    "flow.flow_id": _SLUG,
    "ladder.id": _SLUG,
    "profile.id": _SLUG,
    "region.id": _SLUG,
    "landmark.id": _SLUG,
    "mask.mask_id": _SLUG,
    "decision.decision_id": _SLUG,
    "finding.finding_id": _SLUG,
    "entry.entry_id": _SLUG,
    "build_pair.pair_id": _SLUG,
    "fixture.fixture_id": _SLUG,
    "control": _SLUG,
    "primitive.id": {"type": "string", "pattern": PASCAL},
    "dependency_map.primitives.id": {"type": "string", "pattern": PASCAL},
    "fixture.symbols.id": _LOCAL,
    "symbol.id": _LOCAL,
    "account.account_id": _STR,
    "fixture.epoch.epoch_id": _SHA256,
    "regions_file": {"type": "string", "pattern": REL_PATH},
}

# Field-path overrides (choice 1 and 4); a path is <record>.<field>, `[]` for a list item.
_OVERRIDES: dict[str, dict] = {
    "build.commit_sha": {"type": "string", "pattern": SHA40},
    "build.merge_base_sha": {"type": "string", "pattern": SHA40},
    "regions_file.checkpoint_id": _CHECKPOINT,
    "mask.checkpoint_id": _CHECKPOINT,
    "mutant.checkpoint_id": _CHECKPOINT,
    "checkpoint_step.checkpoint_id": _CHECKPOINT,
    "step_common.id": {"type": "string", "pattern": STEP_ID},
    "assertion.id": _LOCAL,
    "assertion.since_step": {"type": "string", "pattern": STEP_ID},
    "assertion.rule_id": _LOCAL,
    "surface.invariants[]": _LOCAL,
    "surface.invariant_exceptions[].invariant_id": _LOCAL,
    "popup.on_abort.invariant_id": _LOCAL,
    "mutant.expected.invariant_id": _LOCAL,
    "mutant.expected.assertion_id": _LOCAL,
    "fixture.prices[].id": _LOCAL,
    "symbol.id": _LOCAL,
    "checkpoint_outcome.checkpoint_id": _CHECKPOINT,
    "checkpoint_outcome.step_id": {"type": "string", "pattern": STEP_ID},
    "checkpoint_outcome.invariant_ids[]": _LOCAL,
    "checkpoint_outcome.assertion_ids[]": _LOCAL,
    "step_outcome.step_id": {"type": "string", "pattern": STEP_ID},
    "finding.checkpoint_id": _CHECKPOINT,
    "decision.signature.checkpoint_id": _CHECKPOINT,
    "quarantine_record.checkpoint": _CHECKPOINT,
    "calibration_file.checkpoints[].checkpoint_id": _CHECKPOINT,
}

_PHRASES = {
    "region or num": "__region_or_num",
    "checkpoint outcome enum": "__checkpoint_outcome",
    "entitlement enum": "__entitlement",
    "enum[finding kinds of taxonomy.yaml]": "__finding_kind",
}
_WORD = re.compile(r"[A-Za-z0-9_.:]+")
_MISSING = object()


class NotationError(ValueError):
    """A notation block the parser cannot read."""


@dataclass
class Field:
    """One `key: type` line of a notation block."""
    name: str
    type: tuple
    optional: bool = False
    default: Any = _MISSING


@dataclass
class Notation:
    """Every record of every notation block, by record name."""
    records: dict[str, list[Field]] = field(default_factory=dict)


def _strip_comment(line: str) -> str:
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def _scan(text: str, stop: str | None = None) -> list[str] | int:
    """Split `text` on top-level commas, or return the index of the first top-level `stop`."""
    parts, depth, cur, quote = [], 0, [], None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch == '"':
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif depth == 0 and stop is not None and ch == stop:
            return i
        elif depth == 0 and stop is None and ch == ",":
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if stop is not None:
        return -1
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def parse_field(name: str, rest: str) -> Field:
    """Parse the right-hand side of `name: <type>[?][ = default]`."""
    rest = rest.strip()
    default: Any = _MISSING
    eq = _scan(rest, "=")
    assert isinstance(eq, int)
    if eq >= 0:
        default = load_yaml_text(rest[eq + 1:].strip())
        rest = rest[:eq].strip()
    optional = rest.endswith("?")
    if optional:
        rest = rest[:-1].strip()
    return Field(name, _TypeParser(rest).parse(), optional, default)


class _TypeParser:
    """Recursive descent over one type expression of 4.1."""

    def __init__(self, text: str):
        for phrase, token in _PHRASES.items():
            text = text.replace(phrase, token)
        self.s, self.i = text, 0

    def parse(self) -> tuple:
        node = self._union()
        self._ws()
        if self.i != len(self.s):
            raise NotationError(f"trailing text in type {self.s!r}")
        return node

    def _ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def _expect(self, ch: str) -> None:
        self._ws()
        if not self.s.startswith(ch, self.i):
            raise NotationError(f"expected {ch!r} at {self.i} in {self.s!r}")
        self.i += 1

    def _union(self) -> tuple:
        items = [self._atom()]
        while True:
            self._ws()
            if self.s.startswith("|", self.i):
                self.i += 1
                items.append(self._atom())
            else:
                return items[0] if len(items) == 1 else ("union", tuple(items))

    def _atom(self) -> tuple:
        self._ws()
        if self.s.startswith("{", self.i):
            depth, j = 0, self.i
            while True:
                depth += {"{": 1, "}": -1}.get(self.s[j], 0)
                if depth == 0:
                    break
                j += 1
            inner, self.i = self.s[self.i + 1:j], j + 1
            parts = _scan(inner)
            assert isinstance(parts, list)
            # an untyped key (`bbox: {x,y,w,h}` of 4.13) is an int, as every x, y, w and h of section 4 (choice 9)
            return ("obj", tuple(parse_field(*p.split(":", 1)) if ":" in p else Field(p.strip(), ("prim", "int"))
                                 for p in parts))
        if self.s.startswith('"', self.i):
            j = self.s.index('"', self.i + 1)
            value, self.i = self.s[self.i + 1:j], j + 1
            return ("const", value)
        m = _WORD.match(self.s, self.i)
        if not m:
            raise NotationError(f"unexpected {self.s[self.i:]!r} in {self.s!r}")
        word, self.i = m.group(0), m.end()
        if word in ("enum", "list", "map") and self.s.startswith("[", self.i):
            if word == "enum":
                j = self.s.index("]", self.i)
                values = [v.strip() for v in self.s[self.i + 1:j].split("|")]
                self.i = j + 1
                return ("enum", tuple(int(v) if v.isdigit() else v for v in values))
            self.i += 1
            first = self._union()
            if word == "map":
                self._expect(",")
                second = self._union()
                self._expect("]")
                return ("map", first, second)
            self._expect("]")
            return ("list", first)
        if word.startswith("ref:"):
            return ("ref", word[4:])
        if word in _PRIMS:
            return ("prim", word)
        if word == "default":
            return ("const", "default")
        if word.startswith("__"):
            return ("special", word[2:])
        return ("named", word)


def parse_notation(*blocks: str) -> Notation:
    """Read notation blocks into records; nested objects follow indentation (4.1)."""
    out = Notation()
    for text in blocks:
        stack: list[tuple[int, list[Field]]] = []
        for raw in text.splitlines():
            line = _strip_comment(raw)
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            key, _, rest = line.strip().partition(":")
            while stack and indent <= stack[-1][0]:
                stack.pop()
            if not stack:
                if indent:
                    raise NotationError(f"indented line without a record: {raw!r}")
                out.records[key] = []
                stack.append((indent, out.records[key]))
            elif rest.strip():
                stack[-1][1].append(parse_field(key, rest))
            else:
                child: list[Field] = []
                stack[-1][1].append(Field(key, ("obj", child)))
                stack.append((indent, child))
    return out


class _Builder:
    """Turns parsed records into one JSON Schema with the `$defs` it references."""

    def __init__(self, notation: Notation):
        self.n = notation
        self.defs: dict[str, dict] = {}
        self.pending: list[str] = []

    def node(self, t: tuple, path: str) -> dict:
        if path in _OVERRIDES:
            return copy.deepcopy(_OVERRIDES[path])
        kind = t[0]
        if kind == "prim":
            return copy.deepcopy(_PRIMS[t[1]])
        if kind == "enum":
            return {"enum": list(t[1])}
        if kind == "list":
            return {"type": "array", "items": self.node(t[1], path + "[]")}
        if kind == "map":
            out: dict[str, Any] = {"type": "object", "additionalProperties": self.node(t[2], path + "{}")}
            key = self.node(t[1], path + "<key>")
            if "pattern" in key:
                out["propertyNames"] = {"pattern": key["pattern"]}
            elif "enum" in key:
                out["propertyNames"] = {"enum": key["enum"]}
            return out
        if kind == "ref":
            if t[1] not in _REFS:
                raise NotationError(f"unknown reference ref:{t[1]} at {path}")
            return copy.deepcopy(_REFS[t[1]])
        if kind == "obj":
            return self.object(t[1], path)
        if kind == "const":
            return {"const": t[1]}
        if kind == "union":
            return {"anyOf": [self.node(x, path) for x in t[1]]}
        if kind == "special":
            return self._special(t[1])
        if kind == "named":
            if t[1] not in self.defs and t[1] not in self.pending:
                self.pending.append(t[1])
            return {"$ref": f"#/$defs/{t[1]}"}
        raise NotationError(f"unknown type node {t!r}")

    def _special(self, name: str) -> dict:
        if name == "region_or_num":
            return {"anyOf": [copy.deepcopy(_SLUG), {"type": "number"}]}
        if name == "checkpoint_outcome":
            return {"enum": list(load_taxonomy().checkpoint_outcomes)}
        if name == "finding_kind":
            return {"enum": list(load_taxonomy().finding_kinds)}
        if name == "entitlement":
            ent = next(f for f in self.n.records["surface"] if f.name == "entitlements")
            return {"enum": list(ent.type[1][1])}
        raise NotationError(f"unknown phrase {name}")

    def object(self, fields: list[Field] | tuple[Field, ...], path: str, closed: bool = True, root: bool = False) -> dict:
        props: dict[str, Any] = {}
        required: list[str] = []
        for f in fields:
            p = f"{path}.{f.name}"
            if root and f.name == "schema_version":
                props[f.name] = {"type": "integer", "const": f.default}
                required.append(f.name)
                continue
            s = self.node(f.type, p)
            if f.default is not _MISSING:
                s["default"] = f.default
            elif not f.optional:
                required.append(f.name)
            props[f.name] = s
        out: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            out["required"] = required
        if closed:
            out["additionalProperties"] = False
        return out

    def record(self, name: str) -> dict:
        if name == "step":
            return self._step()
        if name not in self.n.records:
            raise NotationError(f"unknown record {name}")
        return self.object(self.n.records[name], name)

    def _step(self) -> dict:
        common = self.object(self.n.records["step_common"], "step_common", closed=False)
        types = next(f for f in self.n.records["step_common"] if f.name == "type").type[1]
        branches = []
        for t in types:
            if t == "checkpoint":
                fields = [f for f in self.n.records["checkpoint_step"] if f.name != "type"]
                then = self.object(fields, "checkpoint_step", closed=False)
            else:
                then = self.object(self.n.records.get(f"step_{t}", []), f"step_{t}", closed=False)
            then.pop("type")
            if t == "type_fixture":
                then["oneOf"] = [{"required": ["value_ref"]}, {"required": ["value"]}]
            branches.append({"if": {"properties": {"type": {"const": t}}, "required": ["type"]}, "then": then})
        return {"type": "object", "properties": common["properties"], "required": ["id", "type"],
                "allOf": branches, "unevaluatedProperties": False}

    def schema(self, name: str, root: str, part_records: tuple[str, ...] = ()) -> dict:
        """The schema of file `name` rooted at `root`; every other record of its notation part goes into `$defs`
        even when the root does not reference it (4.13 carries four record shapes in one file)."""
        body = self.object(self.n.records[root], root, root=True)
        self.pending += [r for r in part_records if r != root and r not in self.pending]
        while self.pending:
            nxt = self.pending.pop(0)
            self.defs[nxt] = self.record(nxt)
        out = {"$schema": DRAFT, "$id": f"sentinel:schemas/{name}.schema.json", "title": f"{name} ({root})", **body}
        if root == "build_file":
            out["anyOf"] = [{"required": ["build"]}, {"required": ["build_pair"]}]
        if self.defs:
            out["$defs"] = dict(sorted(self.defs.items()))
        return out


SPEC_SCHEMAS: dict[str, str] = _spec_schemas()


@lru_cache(maxsize=1)
def notation() -> Notation:
    """Every record of the spec blocks, the step table and the auxiliary shapes."""
    return parse_notation(*NOTATION.values(), STEP_NOTATION, AUX_NOTATION)


@lru_cache(maxsize=None)
def _schema(name: str) -> dict:
    if "#" in name:
        return _record_schema(*name.split("#", 1))
    roots = {**SPEC_SCHEMAS, **AUX_SCHEMAS}
    if name not in roots:
        raise KeyError(f"no schema named {name!r}")
    part = tuple(parse_notation(NOTATION[name]).records) if name in NOTATION else ()
    return _Builder(notation()).schema(name, roots[name], part)


def _record_schema(name: str, record: str) -> dict:
    """`<schema>#<record>`: one record of a schema file, such as one catalog entry (`popup#popup`) or one of the
    four 4.13 shapes (`outcome#finding`), validated against that file's `$defs`."""
    whole = _schema(name)
    if record == SPEC_SCHEMAS.get(name, AUX_SCHEMAS.get(name)):
        return whole
    if record not in whole.get("$defs", {}):
        raise KeyError(f"schema {name!r} has no record {record!r}")
    return {"$schema": DRAFT, "$id": f"sentinel:schemas/{name}.schema.json#{record}", "$ref": f"#/$defs/{record}",
            "$defs": whole["$defs"]}


def schema(name: str) -> dict:
    """The generated JSON Schema for one record type (a copy the caller may change); `name` is a schema file
    name of `schema_names()`, or `<schema>#<record>` for one record of that file."""
    return copy.deepcopy(_schema(name))


def schema_names(spec_only: bool = False) -> tuple[str, ...]:
    """Names of the generated schemas: the 4.1 record types, then the auxiliary shapes."""
    return tuple(SPEC_SCHEMAS) if spec_only else tuple(SPEC_SCHEMAS) + tuple(AUX_SCHEMAS)


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_schema(name))


def _location(path: Any) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def validate(name: str, data: Any) -> list[tuple[str, str]]:
    """Every schema violation of `data` as (location, message), location like `steps[3].target`."""
    errors = sorted(_validator(name).iter_errors(data), key=lambda e: (list(map(str, e.absolute_path)), e.message))
    out = []
    for e in errors:
        msg = e.message if len(e.message) <= 200 else e.message[:197] + "..."
        out.append((_location(e.absolute_path), msg))
    return out


def fingerprint_field_names() -> set[str]:
    """The `match` and `record` field names of the generated 4.12 schema (L-FPR-01)."""
    props = _schema("fingerprint")["properties"]
    return set(props["match"]["properties"]) | set(props["record"]["properties"])


def write_schemas(directory: str | Path) -> list[Path]:
    """Write every generated schema to `<directory>/<name>.schema.json`."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for name in schema_names():
        p = d / f"{name}.schema.json"
        p.write_text(json.dumps(_schema(name), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(p)
    return written


def check_schemas(directory: str | Path) -> list[tuple[str, str]]:
    """L-XREF-09: (file, problem) for every schema file that does not regenerate identically."""
    d = Path(directory)
    problems = []
    known = {f"{n}.schema.json" for n in schema_names()}
    for name in schema_names():
        p = d / f"{name}.schema.json"
        if not p.is_file():
            problems.append((p.name, "missing; regenerate with write_schemas"))
            continue
        try:
            same = json.loads(p.read_text(encoding="utf-8")) == _schema(name)
        except (OSError, ValueError) as exc:
            problems.append((p.name, f"unreadable: {exc}"))
            continue
        if not same:
            problems.append((p.name, "differs from the schema generated from the 4.1 notation"))
    for p in sorted(d.glob("*.schema.json")):
        if p.name not in known:
            problems.append((p.name, "no record type generates this schema"))
    return problems


def _resolve(s: dict, root: dict) -> dict:
    while isinstance(s, dict) and "$ref" in s:
        s = root.get("$defs", {}).get(s["$ref"].rsplit("/", 1)[-1], {})
    return s if isinstance(s, dict) else {}


def _is_scalar(s: dict, root: dict) -> bool:
    if "anyOf" in s:
        return all(_is_scalar(_resolve(x, root), root) for x in s["anyOf"])
    return any(k in s for k in ("enum", "const", "pattern")) or s.get("type") in ("string", "integer", "number", "boolean")


def _coerce(data: Any, s: dict, root: dict) -> Any:
    s = _resolve(s, root)
    if isinstance(data, (list, dict)) and s and _is_scalar(s, root):
        return f"<{type(data).__name__}>"
    if isinstance(data, dict):
        props = dict(s.get("properties", {}))
        for branch in s.get("allOf", []):
            props.update(branch.get("then", {}).get("properties", {}))
        extra = s.get("additionalProperties")
        return {k: _coerce(v, props.get(k, extra if isinstance(extra, dict) else {}), root) for k, v in data.items()}
    if isinstance(data, list):
        return [_coerce(v, s.get("items", {}), root) for v in data]
    return data


def coerce_shapes(name: str, data: Any) -> Any:
    """A copy of `data` in which a list or map standing where the schema wants a scalar becomes the
    string `<list>` or `<dict>`, so rules can index a document that fails its schema; the schema
    violation itself is reported by `validate`."""
    root = _schema(name)
    return _coerce(data, root, root)
