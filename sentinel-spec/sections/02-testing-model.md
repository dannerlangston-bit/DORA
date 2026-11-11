## 2. What is tested and how

This section defines what Sentinel looks at, how it decides what to look at for a given run, and what it says when it cannot look. Five test kinds and one bounded activity (the Explorer) produce checkpoints; every checkpoint ends in exactly one outcome from the closed taxonomy in 1.10 Outcome taxonomy (authoritative), chosen by the precedence order stated there. Comparison, the Region Registry and the reviewer are in 3.7 Three-granularity diff: algorithms and parameters, 3.8 Region Registry semantics and 3.12 The reviewer: inputs and prompt contract; file formats are in 4.5 Surface Registry entry, 4.6 Workspace Corpus entry and 4.7 Flow file. This section owns the registry content, the census, the size sweeps, flows and assertions, the invariant catalog, the Oracle, the Dependency Map, scope computation and coverage accounting.

Every checkpoint id in this section follows one grammar: `<kind>/<surface-id>/<qualifier>`. The kind is `census`, `sweep`, `split` or `flow`; the surface id is the registry's dotted slug with hyphens inside segments; the qualifier is `<workspace-id>` for census (plus an optional dimension segment, 2.4), `<host>/<w>x<h>` for sweep (`<host>/<w>x<h>-asc` for a step of Ladder F's ascending pass, 2.5.2), `<splitter>/<fraction>` for split and `<flow-id>/<step-id>` for flow. The lint in 4.15 Cross-file rules and the lint pipeline and the schema in 8.3 Schema accept exactly this grammar and nothing else.

### 2.1 The five test kinds and the Explorer

| Kind | Needs | Produces | Runs in | Finds |
|---|---|---|---|---|
| Surface census | Surface Registry, Workspace Corpus, base build | One checkpoint per (surface, workspace); diff against base; invariants | PR (scope plus smoke), merge (full), RC (full) | Unknown side effects on surfaces nobody declared |
| Size sweeps | Registry entries with `resizable: true`, tier C harness workspace, ladders | One checkpoint per (surface, host, size or splitter step); diff at the same step; invariants | PR (short ladder, in scope), merge (short), RC (full) | Layout failures at some size; the Active Trader ladder overlap is the reference case |
| Scripted flows | Flow YAML, fixtures, Region Registry, Oracle | Step outcomes; checkpoints with assertions | PR (tier-1 flows touching scope), merge (all tier-1), RC (all) | Interactive states: ticket defaults, confirm values, studies, scans, dialogs |
| Layout invariants | Bounds source (JAB in branch A; OCR word boxes plus templates in branch B), required-label lists | `invariant_violation` on any capture | Every capture of every run | Overlap, overflow, clipped text, missing labels, scrollbars, illegible text, placeholders, error dialogs, sign and color disagreement |
| Oracle checks | OCR on upscaled crops, CSV export, grid copy, JAB values, market-data API, fixture | `assertion_failed`; typed values stored beside every capture | Flows always; census and sweeps on rule regions | Wrong numbers, including numbers wrong in both builds |
| Explorer | Own login and VM snapshot, gateway restricted toolset, deny list, action, wall-clock and dollar budgets (2.9) | Findings and candidate flows, never outcomes directly | RC only, scheduled last | States no author thought of; each finding is replayed once as a flow before it is reported |

Census and sweeps need no description of what a screen should look like, only the base build and the invariants. Flows add intent. The Oracle adds absolute truth where a rule or independent source exists. The Explorer improvises, which is why it is bounded, isolated and last.

### 2.2 Surface Registry

#### 2.2.1 Content

The registry is YAML in git (`registry/surfaces/*.yaml`): one file per top-level tab, one each for the sidebar, dialogs, Application Settings, ticket variants, option chain layouts and the validation probes. Full schema and lint in 4.5 Surface Registry entry; the field names and vocabularies below are the ones 4.5 carries.

| Field | Meaning and rule |
|---|---|
| `id` | Stable dotted slug naming screen and state, for example `trade.active-trader.ladder`. Unique; never derived from list position; a rename needs a rename map in the same PR or CI fails |
| `title`, `aliases` | Text as displayed, plus older spellings. Used by validation and OCR anchors |
| `kind` | One of the 4.5 enum: `tab`, `subtab`, `section`, `gadget`, `panel`, `dialog`, `menu`, `context_menu`, `settings_page`, `ticket_variant`, `chain_layout`, `active_trader_panel`, `chart`, `probe`. Selects the census probe and the reach method; `probe` is reserved for the eight validation surfaces of 2.2.3 |
| `parent` | Hosting id; builds the surface tree |
| `path` | Ordered reach steps from a named workspace state in the step vocabulary of 4.7 (`click_named` targets name registry controls, which carry the JAB name, template and OCR rungs of the locator ladder, 5.5 Locator ladder); CI requires at least two rungs per targeted control |
| `primitives` | Hosting primitives consumed (FlexibleGrid, Docking, ColumnModel, Theme, FontMetrics, ChartEngine, OrderTicketCore, LadderModel, and so on); keys into the Dependency Map |
| `roots` | Class or package prefixes implementing the surface; used by the static impact step; derived from source, human-overridable |
| `personas` | Subset of `active_trader`, `options_trader`, `futures_trader`, `swing_trader`, `beginner`; matrix columns. `personas[0]` is the primary persona, which sweep and split checkpoints count toward (2.12) |
| `tier` | 1 money and orders, 2 analysis, 3 everything else |
| `critical` | Default false; allowed only on tier 1. Makes the surface's checkpoints critical checkpoints, which the mutant catalog must bind (4.11 L-MUT-03, 10.3) |
| `resizable`, `hosts` | Set only from probe P8 or a declared in-host splitter; `hosts` is a subset of {tab, sidebar, flexgrid, detached, dialog}. Only flexgrid, detached and dialog hosts take w x h sizes; tab and sidebar hosts are swept through their splitters (2.5.1) |
| `streaming` | Default false; true when the whole surface is a streaming region (news, video), which excludes it from sweeps (2.5.3) |
| `splitters` | In-host sweep splitters: list of `{id, host, template, fractions, collapsible}`. A fraction outside the list is not a checkpoint; 0.00 and 1.00 appear only when `collapsible: true` |
| `target_window` | `{kind: main or detached or dialog or popup, title_regex}`; Capture selects the window by kind plus title regex |
| `data_mode` | `frozen` or `masked_live`; default for census and sweep checkpoints, flows may override |
| `entitlements` | Required entitlements from the closed vocabulary `margin`, `options_L1`, `options_L2`, `options_L3`, `futures`, `forex`, `ira`, `portfolio_margin`, `multi_account`, `paper_send`. A missing one makes every checkpoint of the entry `not_run (reason: entitlement)`. Flows carry the same vocabulary under `requires`; `paper_send` enters a login's profile only through the `paper_send_ok` decision (6.7 Send policy: both branches) |
| `enumerable_by` | Probe P1 to P8 that lists the entry in the running app, or none |
| `census.workspaces` | The corpus workspaces the census visits the surface in, the canonical one first. The census also visits the surface in every other corpus workspace whose derived hosted-surface list (from `gadgets_present`, 4.6) contains it (2.3); lint requires at least one tier A workspace to host every surface |
| `landmarks`, `anti_anchors`, `labels`, `controls` | Must-see landmark templates (`must_see: true`) proving the surface is open and OCR strings that must be absent; required labels for INV-05; controls with `required: true` for INV-10 and `deny: true` for the gateway deny list (tier 1 must list its primary action labels and controls) |
| `invariants` | Optional per-surface invariant inputs: `min_font_px`, `overlap_allowlist` (pairs of controls), `signed_columns`, `allowed_scrollbars`, `focus_order` (2.7 Common rules) |
| `smoke`, `owner` | PR smoke subset membership, a flag the registry owner sets (rule in 2.11; tier 1 does not imply it); flow owner for routing |

```yaml
schema_version: 1
id: trade.active-trader.ladder
title: Active Trader
kind: active_trader_panel
parent: trade.active-trader
tier: 1
owner: tos-trade-ui
smoke: true
path:
  - {id: p1, type: click_named, target: shell.tab-bar.trade}          # the target's control carries the JAB name, template and OCR rungs
  - {id: p2, type: click_named, target: trade.subtab.active-trader}
  - {id: p3, type: wait_for, anchor: {landmark: lm.ladder-header}, timeout_ms: 8000}
primitives: [LadderModel, ColumnModel, Theme, FontMetrics, Docking, FlexibleGrid]
roots: ["com.devexperts.tos.ui.trade.activetrader"]     # illustrative, derived from source
personas: [active_trader, futures_trader]
entitlements: [margin]
enumerable_by: P2
resizable: true
hosts: [tab, detached, flexgrid]
splitters: [{id: side-panels, host: tab, template: registry/templates/active-trader/side-splitter.png, fractions: [0.15, 0.30, 0.50, 0.70, 0.85], collapsible: false}]
target_window: {kind: main}
data_mode: frozen
account_state: true
landmarks: [{id: lm.ladder-header, template: registry/templates/active-trader/ladder-header.png, must_see: true}]
anti_anchors: [Loading, Reconnecting]
labels: ["Bid Size", "Ask Size"]
controls:
  - {id: buy-mkt-button, template: registry/templates/active-trader/buy-mkt.png, ocr_text: "BUY MKT", required: true, deny: true}
  - {id: sell-mkt-button, template: registry/templates/active-trader/sell-mkt.png, ocr_text: "SELL MKT", required: true, deny: true}
census: {enabled: true, workspaces: [ws.tier-a.active-trader, ws.tier-a.futures-trader, ws.tier-c.sweep-harness]}
```

#### 2.2.2 Enumerating thinkorswim's surfaces

The registry starts from the list below. Names are a starter from product knowledge; the displayed names read by the probes in 2.2.3 are authoritative and correct the registry on the first validated run. Uncertain names are marked (verify). Tier-1 rows with ids are in 12.4 Surface x persona matrix template; the full list is the registry itself.

| Area | Members to register (starter) | Tier and notes |
|---|---|---|
| Monitor | Activity and Positions (sections Working Orders, Filled Orders, Canceled Orders, Rolling Strategies, Position Statement); Account Statement (sections Order History, Trade History, Cash and Sweep Vehicle, Profits and Losses, Forex sections (verify)) | 1. Each section is a surface |
| Trade | All Products (sections Underlying, Trade Grid, Option Chain, Product Depth, Today's Options Statistics, Order Entry Tools); Active Trader (ladder, Level II side panel, Time and Sales side panel, chart side panel, Active Trader ticket); Forex Trader; Futures Trader; Pairs Trader | 1. Side panels are separate surfaces from the sidebar gadgets of the same name |
| Analyze | Add Simulated Trades, Risk Profile, Probability Analysis, Economic Data, thinkBack, Fundamentals, Earnings | 2 |
| Scan | Stock Hacker, Option Hacker, Spread Hacker, Spread Book; others from P2 | 2. Scans do not run under OnDemand, so `masked_live` with rule regions |
| MarketWatch | Quotes, Alerts, Visualize, Calendar | 2. Create Alert is a dialog surface |
| Charts | Charts, Flexible Grid, Product Depth | 2. Flexible Grid is the chart grid layout and the sweep host |
| Tools, Help | myTrade, thinkLog, others from P2; Help members from P2 | 3. Chat Rooms and Support are registered and denied to the Explorer |
| Setup menu | Application Settings, Save workspace as, Load workspace, Share workspace (verify) | 3. Share and Setup are on the Explorer deny list |
| Application Settings pages | General, Look and feel, Order Defaults, Hot Keys, Notifications; others from P5 | 1 for Order Defaults and Hot Keys (attested at run start, 6.3 Run-start settings attestation), 3 otherwise |
| Left sidebar gadgets | Account Info, Watchlist, Quick Chart, Level II, Time and Sales, Live News, Trade Flash, Dashboard, Calculator, Scratch Pad, Trader TV or Schwab Network; others from P3 | Account Info 1; Watchlist, Level II, Time and Sales 2; others 3. News and video gadgets are registered but excluded from pixel diff and sweeps |
| Order ticket variants | Order Entry for stock, single option, each Spread selector value (Vertical, Calendar, Diagonal, Straddle, Strangle, Butterfly, Condor, Iron Condor, Vertical Roll, Collar, Covered Stock, Back/Ratio, Double Diagonal, Double Calendar; verify by P6), futures, forex; Advanced selector values (Single, OCO, 1st trgs Seq, 1st trgs All, Blast All; verify); Order Rules dialog; the Order Confirmation dialog (opened by Confirm and Send); Saved Orders; order templates | 1. Entry points are flows, not surfaces: Trade tab, chain click, Active Trader ticket, chart right-click Buy or Sell, watchlist right-click, Position Statement Create closing order |
| Option chain layouts | Each Layout value (Delta, Gamma, Theta, Vega, Rho, Probability OTM, Probability ITM, Volume and Open Interest, Last X and Net Change, Impl Vol, Theo Price, Custom; verify), each Strikes value (4, 6, 8, 10, 12, 14, 16, 20, 40, All), expiration filters (Weeklys, Monthlys, Quarterlys, All; verify), Single and Spread views | 1. Two dimensions of one surface; the cross product is visited in tier-1 workspaces only |
| Dialogs | Order Confirmation, Edit Studies, Chart Settings, Create Alert, Study Alert, Scan Settings, Customize columns, Symbol lookup, Adjust Account, Message Center, Save and Load Workspace, export file chooser, Java exception dialog, Reconnecting, first-launch agreements | 1 for Order Confirmation and Adjust Account, 2 otherwise. Not enumerable; reached by flows, matched by the popup catalog (7.8 Popup catalog policy and starter entries) |
| Context menus | Chart, watchlist, positions grid, order entry, option chain | 1 for positions and chain, 2 otherwise. Enumerated by P7 |

Level II and Time and Sales appear up to three times: sidebar gadget, Active Trader side panel, and Flexible Grid cell content if P8 lists them (verify). Each is a separate surface because the layout code path differs.

#### 2.2.3 Validation against the running app

At entry to `running`, right after the `canary_flow_passed` gate, on both builds of the pair and in the shard's first tier A workspace, the Orchestrator runs eight probes. Each navigates by registry path, captures, OCRs a declared region (3x upscale, inverted for the dark theme, word-level TSV with confidence), normalizes labels (case fold, collapse whitespace, strip hotkey underscores and trailing counts such as "(3)") and diffs the list against entries whose `enumerable_by` is the probe.

Each probe is itself a census checkpoint `census/registry.validation-p<n>/<workspace-id>` on the registered surface `registry.validation-p<n>` (kind `probe`, tier 1, path to the row or list it reads). Its outcome: `identical` when the candidate and base label lists are equal; `intended` or `collateral` when they differ, by whether the manifest declares the row's parent surface (the tab bar, the selector, the gadget picker); `landmark_missing (reason: probe:<Pn>)` when the probe's anchor region is not found on a build, with the side; `harness_error (reason: ocr_calibration)` when the OCR calibration panel failed at run start (2.8.1); `not_run (reason: entitlement)` when the host of the probe needs an entitlement the login lacks. A probe that ends `landmark_missing` or `harness_error` leaves the registry as truth for that area and run health prints "validation incomplete for P<n>" with the outcome. Validation never silently accepts the registry.

| Probe | Where and how | Validates |
|---|---|---|
| P1 | OCR the top tab row of the main window | Top-level tabs |
| P2 | Click each tab, OCR the sub-tab row; for Monitor and Trade also OCR section headers | Sub-tabs, sections |
| P3 | Open the add-gadget control, OCR the list, wheel until it stops changing | Sidebar gadgets |
| P4 | Open the gear menu, OCR items | Setup entries |
| P5 | Open Application Settings, OCR the page list, visit each page, OCR its title | Settings pages |
| P6 | Open each selector (Order type, Advanced, TIF, Spread, Layout, Strikes, expiration filter), OCR the list | Ticket variants, chain layouts |
| P7 | Right-click each declared host at its anchor, OCR the menu, Escape | Context menus |
| P8 | Open a Flexible Grid cell's content selector and OCR it; template-locate detach controls on every gadget | `hosts`, `resizable` |

Pairing rule for renames. Before the result table is applied, within one probe's list, an unmatched candidate label L (on the candidate, no entry) and an unmatched entry E (label or alias on the base, absent on the candidate) are paired when L's position in the candidate list equals E's position in the base list, or the normalized edit distance between L and E's label is at most 0.3. A pair is a rename candidate, never a silent match: it still produces the `unregistered_surface` checkpoint of the first row below, with `rename_candidate: <E.id>` in its reason, and E's census checkpoints are visited through E's path with L substituted for E's label in the OCR rung (the JAB and template rungs are unchanged), so the surface is still looked at and its diff is classified by the normal rules: the label change is a visible diff on E, `intended` when the manifest declares E, else `collateral`. The proposed registry PR adds L to E's `aliases` instead of a new stub. A human confirms rename or new surface at triage. An unpaired label is a new surface.

| Enumeration result | Outcome and action |
|---|---|
| Label on candidate, no entry (paired or not) | `unregistered_surface` recorded against synthetic id `unregistered.<probe>.<slug>` with both probe screenshots; listed under Unexpected, tier 1 until classified; a registry stub PR (an alias PR for a rename candidate) is proposed; repeats every run until the registry changes |
| Label on both builds, entry present | Visited by the census |
| Label on base, absent on candidate, unpaired | The census does not attempt E's path. Every census checkpoint of E enters 3.11 with input B `removed` (rows 24 to 26), the two probe screenshots serving as the crops: `intended` when the manifest declares E and the Reviewer confirms the removal with `matches_manifest` at or above the routing threshold; `needs_human (reason: reviewer_unavailable)` with `review_state: pending` while that verdict is pending, re-evaluated when it lands; when E is declared and the Reviewer does not confirm the removal, `needs_human (reason: reviewer_no_match)`, or `needs_human (reason: reviewer_cap)` or `needs_human (reason: reviewer_unavailable)` when the item is capped or the job gives up (row 25a); `collateral` when E is undeclared (row 26) |
| Label absent on base, present on candidate, entry present | E's checkpoints are `needs_baseline`; invariants still run on the candidate capture |
| Absent on both, entitlement lacking | Every checkpoint of E is `not_run (reason: entitlement)` |
| Absent on both, entitlement present | `harness_error (reason: registry_entry_not_found)` on every checkpoint of E; maintenance task to the entry's owner |
| Entry with `enumerable_by: none` (dialogs, context menus only flows reach) | Not validated here; reached by flows and matched by the popup catalog (7.8); a registered dialog no flow reaches is counted under B15 (2.13) |

#### 2.2.4 Registry lint

CI rejects: duplicate or renamed ids without a rename map; a path step with fewer than two rungs; a tier-1 entry without `labels` and `controls`; a `primitives` value absent from the Dependency Map; a `census.workspaces` entry absent from the corpus; a surface hosted by no tier A workspace (so every surface is reached at least once per merge run); an `entitlements` value outside the vocabulary; `resizable: true` with empty `hosts`; a `splitters` entry whose `host` is not in `hosts`; more than 40 entries with `smoke: true` (the registry owner removes flags; the run never raises the cap). The same lint runs at preflight item G1 (12.7 Preflight checklist); a failure there ends the run `aborted_harness (reason: config_invalid)` before any product action, so a broken registry never yields a partial census.

### 2.3 Workspace Corpus

| Tier | Content | Purpose | Count |
|---|---|---|---|
| A canonical | One workspace per persona: active trader, options trader, futures trader, swing trader, beginner default | Census, flows, the persona dimension of the matrix | 5 |
| B power user | Real internal power-user workspaces with detached windows, Flexible Grid charts, dense sidebars, custom columns; at least two saved under the previous release | Census under realistic density; migration dialogs; RC only | 4 to 8 |
| C sweep harness | One Flexible Grid workspace with a single cell, empty sidebar, parking area for detached windows, link groups fixed | Size sweeps | 1 |

Each workspace is a file in git with content hash, persona tag, the release it was saved under, and a derived list of surfaces it hosts (the probes run against it once at commit time and again whenever the registry changes). That list is the `hosted_by` relation the census (2.4) and scope computation (2.11) use; a surface in no tier A workspace fails lint (2.2.4). Rules: no news, video or chat gadgets in tiers A and C; link colors fixed; fonts and theme pinned; symbols from the symbol fixture only. The tier counts in the table and the gadget rule are linted (L-WSP-08 and L-WSP-09 in 4.6 Workspace Corpus entry). The Rig Manager copies the files into the install's usergui folder on snapshot build and the hash check after load is the `workspace_loaded` gate. Workspaces are local state; studies, scans, watchlists, drawings and most Application Settings are server-side and restored by the fixture reset flow (7.5 State model, 7.6 Fixture reset flow and verify_account_fixture). Tier B workspaces carry migration: the candidate loads a workspace saved under the previous release and every conversion or first-launch dialog is a captured checkpoint (Migration flow class, 2.6.3). A fresh install from the public installer cannot pin a build (the launcher downloads the current build), so fresh install and upgrade in place are not corpus items; they are the release-night upgrade flow under DR-04 branch B (11.8 Phase 0 spike plan: seven decision records).

### 2.4 Surface census procedure

For each workspace in scope, then each registered surface the workspace hosts (its derived hosted-surface list, 2.3) or that is global (tabs, sub-tabs, Setup entries, settings pages, and dialogs reached by a path that starts from any workspace):

```
visit(surface, workspace, build):
  reset_input_state()                                  # keyUp modifiers, mouseUp, Escape once if no ticket or dialog is foreground
  if not preconditions(workspace):                     # workspace hash, one enabled main frame, no modal, link symbol, paper account
     reset_ui(); reload the workspace once; re-check
     if still failing: checkpoint = env_error(gate: workspace_loaded, reason: precondition:<check>); return
  for step in surface.path:
     execute(step) via the locator ladder (5.5): JAB -> image anchor -> OCR text anchor -> agent
     record passed | passed_after_retry | passed_via_agent_fallback | failed | blocked_by_gateway
     if failed:              checkpoint = landmark_missing(step, side); return
     if blocked_by_gateway:  checkpoint = not_run(reason: step_failed:<step>); safety log; return
  if process tree gone or EDT frozen past max wait:    # Watchdog, 10.2
     checkpoint = assertion_failed(assertion: app_alive); finding app_crash (tier 1); return
  settle per 3.5: anchors present, anti-anchors absent, mouse parked, focus dropped, 3 identical frames
  if timeout: checkpoint = not_settled(condition); return
  capture target_window per 5.7; record title, rect, z-order
  run invariants (2.7) on this capture; an error dialog present is INV-14
  if data_mode == masked_live: apply the Region Registry (3.8): freeze, rule, mask, diff
  hand (base, candidate) to the Diff Engine; outcome by 3.11
  for tier-1 surfaces with layout or strikes dimensions: repeat per declared value
```

Checkpoint id: `census/<surface-id>/<workspace-id>`, with an optional dimension segment for the chain cross product, `census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=delta,strikes=16`. The census captures resting state only; interaction belongs to flows.

Rules the pseudocode relies on:

- Reach. A hosted or global surface is attempted in every workspace that hosts it. A path step that fails on every scripted rung and the agent rung (or with the agent disabled) is `failed`; the visit's checkpoint is `landmark_missing` with the side it failed on. Candidate-only failure is a suspected navigation regression, ranked in Unexpected with the pre-attempt screenshot; failure on both builds is a registry path bug routed to the entry's owner, with the same outcome. A step that ends `passed_via_agent_fallback` still captures; the capture is flagged rescued, the locator failure is a finding against the path, and the checkpoint is `needs_human (reason: rescued_capture)` unless a higher-precedence outcome applies.
- Not hosted. A surface that no workspace of a persona hosts is `not_run (reason: scope)` for that persona's cells; the matrix (2.12) excludes such cells from the denominator because the absence is by design. A surface the registry says a workspace hosts but which the corpus file no longer contains fails lint at commit time, never at run time.
- Preconditions. The sequence is one `reset_ui`, one workspace reload, then `env_error (gate: workspace_loaded, reason: precondition:<check>)` for this visit; the next visit re-checks. Three consecutive `env_error` visits in one workspace mark the workspace's remaining visits on that shard `env_error` with the same gate without attempting them, the shard moves to its next workspace, and on-call is told once per workspace.
- Settle. A capture is never retried across a settle timeout; `not_settled` stands with the failing condition and the next surface starts after `reset_input_state`.
- App failure. A dead process or frozen Event Dispatch Thread during a visit makes that visit `assertion_failed (assertion: app_alive)` with a tier-1 `app_crash` finding carrying hs_err_pid, dumps and the log slice; the Watchdog relaunches and the census resumes at the next surface after the gates pass again (10.2 Watchdog). A relaunch budget spent ends the run `aborted_budget (reason: app_restart_budget)`.
- Base side. The base build runs the identical visit list. A base step failure (a path step that still fails on the base after its retry) makes the checkpoint `not_run (reason: base_step_failed)` for the registry owner, unless the manifest entry declaring the surface has `new_surface: true`, which gives `needs_baseline (reason: new_surface)`. A base capture failure (`not_settled`, `env_error` or `harness_error` at the base capture) is re-captured once; if it still fails, the checkpoint is `needs_baseline` with reason `base_not_settled`, `base_env_error` or `base_harness_error` (3.11 input B). In both cases the Invariant Checker and the Oracle still run on the candidate capture and their findings are reported in Unexpected (3.1).

Budget: the tier A census pass is 900 checkpoints (five tier A workspaces times roughly 180 hosted surfaces). A visit is planned at 12 seconds p50 (planning assumption in 11.1), about 185 rig-minutes for the pass, with a sensitivity bound of 20 seconds per visit (about 5 rig-hours) that makes base-capture reuse mandatory rather than an optimization; Phase 0 measures the real figure. Merge runs shard the pass over the 4-rig pool and PR runs take a subset (2.11); rig math is in 11.2 Cadence and time budgets per run type and 11.3 Sharding and parallelism.

### 2.5 Size sweeps

#### 2.5.1 Hosting modes

A gadget is sized through one of three mechanisms, in order of preference:

1. Detached window. Detach by the template-located control, set the outer size with SetWindowPos on the detached top-level window of the process tree, read back the DWM extended frame bounds, capture that window. Exact to the pixel when the window accepts the size.
2. Flexible Grid cell. In the tier C workspace, set the gadget as cell content (if P8 lists it) and drag the grid splitters with the drag primitive (at least 10 intermediate moves). Measure the cell by template-locating its borders.
3. In-host splitter. For panels inside a tab (Active Trader ladder against its side panels, Trade tab sections, sidebar against main area, Monitor sections), drag the splitter named in the registry's `splitters` list to a fraction of the host extent and measure the panel rect by its border templates.

Tab and sidebar hosts have no w x h sizing; they are swept through their splitters only (mechanism 3, `split` checkpoints), because the main frame rect is pinned (INV-09). The w x h ladders of 2.5.2 apply to the flexgrid, detached and dialog hosts. A surface lists its applicable modes in `hosts` and is swept in every one of them, because the ladder inside the Trade tab and the same ladder detached take different Docking paths. A detach control or splitter the locator ladder cannot find ends the first step of that host's ladder `failed`; its checkpoint is `landmark_missing` with the side it failed on, and every later step of that (surface, host) ladder is `not_run (reason: step_failed:<step>)`. The next host or surface starts after `reset_input_state` and a reload of the tier C workspace. An app failure during a step (process gone or EDT frozen, 2.6.1) makes that step's checkpoint `assertion_failed (assertion: app_alive)` and the rest of that (surface, host) ladder `not_run (reason: app_crash)`; after the relaunch the next ladder starts from its preconditions, and a ladder never resumes mid-way, because the descending order of 2.5.2 needs the layout computed large first.

Every step measures the achieved size after the set or drag and compares it with the request. Three cases, closed:

| Measured result | Meaning | What happens |
|---|---|---|
| Within 4 px of the request on both axes | Size taken | Capture; the checkpoint is diffed against the base at the same requested step |
| Larger than the request on an axis, and within 4 px of the size achieved by the previous smaller request on that axis (or this is the smallest request on that axis) | The gadget's minimum size clamps the request | Step event `size_clamped` with requested and achieved sizes; capture anyway; the checkpoint keeps the requested step id and is diffed against the base at the same requested step. Achieved sizes that differ between builds by more than 4 px are padded to the common size with the sentinel color and raise the `window_geometry_changed` finding (3.3 Environment fingerprint and comparability); the diff proceeds, so a changed minimum size is a product change, `intended` or `collateral` by the manifest. The Reporter prints the achieved minimum per gadget and host |
| Any other result (the drag did not take, or the window ignored SetWindowPos while not at its minimum) | The size was not reached | One retry of the set or drag (one retry per step, never across a capture). Still not reached: the step is `failed`, its checkpoint is `assertion_failed (assertion: size_reached)` with requested and measured sizes and the crop, and the rest of that (surface, host) ladder is `not_run (reason: step_failed:<step>)`. Candidate-only failure is a suspected regression (a splitter or window that stopped responding); failure on both builds is routed to the registry owner as a harness or registry task, with the same outcome |

Corner steps such as (240, 160) are expected to clamp on most gadgets; the clamp is a measurement, not a failure, and the smallest achievable size per gadget and host is stored with the run so a build that moves it shows up as a finding.

#### 2.5.2 The ladders

Sizes are client-area pixels on the pinned 1920x1080 rig at 100 percent DPI. MAXW and MAXH are the work area of the pinned display configuration, `work_area: {w, h}` on its entry in `configs/allowed.yaml` (4.6); the ladder stores the symbols so a Windows build change cannot shift it silently. Corpus sizes come from `corpus/measured_sizes.yaml` (owner: registry owner, 4.1): probe P8 measures each gadget's size per workspace on the first validated run and the Reporter's registry PR writes the file. Until the file lists a gadget, its corpus-size steps are `not_run (reason: scope)` and run health prints a line saying so. The two ladders are named sets; a registry entry references them by name and may override with its own ladder only with a stated reason.

Ladder F (RC runs):

| Part | Values | Steps |
|---|---|---|
| Grid | Every width in {320, 400, 480, 640, 800, 960} by every height in {300, 360, 420, 480, 600, 720} | 36 |
| Corners | (240, 160), (240, MAXH), (MAXW, 160), (MAXW, MAXH) | 4 |
| Corpus sizes | The gadget's size in every tier A and B workspace hosting it, from `corpus/measured_sizes.yaml` | per corpus |
| Ascending pass | The six grid widths at height 480, smallest first; ids end in `-asc` | 6 |
| Order | Grid descending by width then by height, corners, corpus sizes, ascending pass | about 46 plus corpus |

Ladder S (PR and merge runs):

| Part | Values | Steps |
|---|---|---|
| Grid | Every width in {320, 480, 640, 960} by every height in {360, 480, 720} | 12 |
| Order | Descending by width then by height | 12 |

Splitter positions as fractions of the host extent: full set (RC) 0.00, 0.15, 0.30, 0.50, 0.70, 0.85, 1.00; short set (PR, merge) 0.15, 0.50, 0.85. The positions 0.00 (collapsed) and 1.00 exist only for splitters the registry declares `collapsible: true`; for every other splitter they are not checkpoints, and the registry's `fractions` list may narrow the set further with a stated reason.

Most overlap bugs appear when a layout computed large is shrunk, so the descending order is mandatory and the ascending pass is RC-only. Ladder S deliberately contains 480x360, the cell size at which the reference ladder overlap reproduces (1.11 Worked example: the Flexible Grid change that overlaps the Active Trader ladder), so a PR run catches it. Each step is a checkpoint, id `sweep/<surface-id>/<host>/<w>x<h>` or `split/<surface-id>/<splitter>/<fraction>`, for example `sweep/trade.active-trader.ladder/flexgrid/480x360`, diffed against the base at the same step and run through the invariants. The ascending pass revisits sizes the descending pass already has, so its steps take the id `sweep/<surface-id>/<host>/<w>x<h>-asc` (for example `sweep/trade.active-trader.ladder/flexgrid/480x480-asc`) and are planned after the descending pass, corners and corpus sizes, in ladder order. The base build runs the same ladder in the same order; a step the base failed or clamped differently follows 2.5.1 and the base-side rule of 2.4. Overlap at a size is `invariant_violation` on that step, and the Reporter names the first failing size and the failing range, which is what the PR comment needs.

#### 2.5.3 Which gadgets get the sweep

| Rule | Effect |
|---|---|
| `resizable` is set only from P8 or a declared in-host splitter | Nothing is swept by hand-set flag |
| Tier 1 | Ladder S in PR when in scope, ladder S in merge, ladder F in RC; splitters short, full in RC |
| Tier 2 | Ladder S in merge, ladder F in RC; splitters short, full in RC |
| Tier 3 | Ladder S and splitters short in RC only |
| `streaming: true` (the whole surface is a streaming region: news, video) | Excluded; every sweep checkpoint of the surface is `not_run (reason: scope)` |
| Chart surfaces | Swept in `frozen` mode only; under DR-02 branch B without OnDemand they are `not_run (reason: scope)` and the count prints in run health |

Sweep checkpoints default to `frozen`; a gadget that cannot be frozen (Level II under DR-02 branch B) sweeps under `masked_live` with its rule regions, and invariants still run because they do not depend on values.

### 2.6 Scripted flows and assertions

#### 2.6.1 Flow model

A flow is YAML in git (`flows/<tier>/<surface>/<name>.yaml`): metadata (id, surfaces, persona, tier, `requires`, owner), preconditions, steps, checkpoints, assertions, teardown. Schema in 4.7 Flow file.

- Preconditions: workspace loaded and hash-verified, exactly one enabled main frame, no modal, fixture symbol in the link group, account selector on the allowlisted paper account (exact-match account ids), zero working orders for order-bearing flows. A failure triggers one `reset_ui` (Escape through the gateway, close non-main windows, re-enter the fixture symbol, park the mouse), then one workspace reload with `reset_profile` (7.6 Fixture reset flow and verify_account_fixture); if the check still fails, every checkpoint of the flow is `env_error (gate: workspace_loaded, reason: precondition:<check>)` and the next flow re-checks. A paper-mode mismatch is never a flow failure: the process tree is killed and the run is `aborted_safety` (6.4 Paper-mode assertion and test identity).
- Steps: click, type (fixture text or vault reference; secrets typed with recorder paused and capture suspended), press, key chord, drag (from, to, at least 10 moves, hold), wheel (delta, at), right-click, double-click, wait-for anchor, export (to a file or by Copy All: `export` with `kind: file` or `copy_all` in 4.7), park mouse, focus neutral, and a mandatory reset-input-state step at flow start, in teardown and in every recovery path. The step that opens the Order Confirmation dialog carries `order_stage: true`, which arms the gateway's order-stage deny state (6.2); lint requires it on every step that targets Confirm and Send (L-FLW-07). Every targeting step carries a locator ladder (5.5 Locator ladder): JAB name or role path (branch A), image anchor with confidence floor, OCR text anchor with region, and whether agent fallback is allowed (never for login steps, never while a ticket or confirm dialog is foreground).
- Checkpoints: id `flow/<surface-id>/<flow-id>/<step-id>`, where the step id is the checkpoint step's id in the file, for example `flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2`; `target_window`, `data_mode`, anchors, anti-anchors, Region Registry reference, required labels, expect block. Renaming a checkpoint step is a rename-map entry (4.7, L-FLW-03), so decisions and calibration data follow it.
- Teardown: Escape through the gateway, cancel any staged ticket, reset symbol, close detached windows and assert the window count, reset input state. Runs even after a `failed` or `blocked_by_gateway` step. The teardown action `cancel_all` is the only action that may resolve the Cancel All control, used by the run teardown and by the recovery flow of 6.6; no click step may target it.

Step outcomes: `passed`; `passed_after_retry` (one retry per step, never across a capture; the first attempt's screenshot is kept); `passed_via_agent_fallback` (locator failure recorded as a finding; the post-recovery capture is tagged rescued and its checkpoint is `needs_human (reason: rescued_capture)` unless a higher-precedence outcome applies); `failed`; `blocked_by_gateway`. A `failed` or `blocked_by_gateway` step ends the flow and teardown runs. The checkpoint that step led to is `landmark_missing` when the locator failed on every rung (with the side it failed on), `assertion_failed` when an assertion failed, or `env_error (gate: <gate>)` when a popup with action abort, a disconnect or a session gate caused the failure. For `blocked_by_gateway` the checkpoint is `not_run (reason: step_failed:<step>)` because nothing was captured; the drop goes to the safety log and on-call, and a blocked attempted send on a scripted flow ends the run `aborted_safety` (6.2 Never-send guard at the keystroke layer). Every later checkpoint of the flow is `not_run (reason: step_failed:<step>)` pointing at the step finding, so the matrix shows them as not looked at rather than as broken. Candidate-only step failure is a suspected regression ranked in Unexpected with the pre-attempt screenshot; failure on both builds is a flow bug for the flow owner, with the same outcomes.

Base-side failure, two cases. A base step failure (the base build still fails, after its retry, a step the candidate passed) makes the checkpoint `not_run (reason: base_step_failed)` for the flow owner, unless the manifest entry declaring the surface has `new_surface: true`, which gives `needs_baseline (reason: new_surface)`; later checkpoints of the flow on that side follow the same rule. A base capture failure (`not_settled`, `env_error` or `harness_error` at a base checkpoint the steps reached) is re-captured once; if it still fails, the checkpoint is `needs_baseline` with reason `base_not_settled`, `base_env_error` or `base_harness_error` (3.11 input B). In both cases the candidate's captures still run the Invariant Checker and the Oracle, whose findings are reported in Unexpected even when the outcome is `not_run` (3.1).

App failure during a flow: if the thinkorswim process tree is gone or the Event Dispatch Thread is frozen past the checkpoint's max wait (Watchdog detection, 10.2 Watchdog), the checkpoint being captured is `assertion_failed (assertion: app_alive)`, every later checkpoint of the flow is `not_run (reason: app_crash)`, and a tier-1 finding `app_crash` carries the hs_err_pid file, heap dump and log slice. An error or exception dialog on screen at capture with the app alive is `invariant_violation` INV-14 (2.7), not an app failure. After the relaunch the next flow starts from its preconditions. The first three app failures in a shard are each relaunched; a fourth marks the shard's remaining checkpoints `not_run (reason: budget)` and the run `aborted_budget (reason: app_restart_budget)` (10.2). A crash or hang during the relaunch gates, or during the gate sequence of the shard's first launch, is another `app_alive` failure that consumes that budget, never `env_error`. The cause (`jvm_crash`, `hang`, `oom`, `process_exit`) is stored in `detail` of the `app_alive` record (4.13).

#### 2.6.2 Assertion kinds

Assertions in an expect block are evaluated by the Oracle on typed extractions from named regions. Any failure makes the checkpoint `assertion_failed` regardless of pixel score; the reviewer cannot change it. An operand that cannot be extracted above its confidence floor does not fail the assertion: the checkpoint is `needs_human (reason: oracle_unreadable)` with the crop (2.8.1), never a pass. The kind names below are the closed enum of 4.7.

| Kind | Operands | Semantics |
|---|---|---|
| `equals` | region, value | Extraction equals the literal after type coercion (money, integer, percent, text) |
| `matches` | region, regex | Extracted text matches |
| `within_tol` | region, value, tolerance | Numeric extraction within absolute or percent tolerance |
| `present`, `absent` | region or label | Control or label present or absent by template or OCR |
| `changed_since_previous_step`, `unchanged_since_previous_step` | region, step | Extraction differs or not from the named earlier checkpoint of this flow |
| `count` | region, n or range | Row or item count |
| `export_equals` | export column, region | CSV or clipboard total equals the on-screen value within tolerance |
| `api_within` | region, api field, tolerance | Within tolerance of the API value as of the modeled time (2.8.2) |
| `rule` | rule id R01 to R10 | An arithmetic invariant from 2.8.3 on the checkpoint's extractions |
| `sign_color_agrees` | region | Numeric sign and dominant text color agree with the pinned palette (`palettes/<theme>.yaml`, 2.7) |

#### 2.6.3 Flow classes and the minimum set

| Class | Contents | Required assertions |
|---|---|---|
| Ticket | One flow per entry point (Trade tab, chain click, Active Trader ticket, chart right-click, watchlist right-click, Create closing order) per asset class (stock, option, spread, futures, forex), under three Order Defaults profiles applied by scripted settings steps at run start: fresh install; power user with per asset class quantity, TIF and type; Active Trader's own default quantity | Pre-input checkpoint right after the ticket appears: default quantity, order type, price basis, TIF, routing. After input: symbol, side, quantity, type, price, TIF. On the Order Confirmation dialog: quantity, price, cost, buying power effect, max loss. After Cancel: empty ticket. A keyboard-only variant (Tab, Shift+Tab, arrows, Escape) asserts the focus owner after each key: JAB focus in branch A, before and after screenshot diff against the declared control rect in branch B |
| Chain | Layout and Strikes changes, expiration filters, spread view, column customize | R04, R03, R05 |
| Chart and study | Add, edit, remove a study; verification study set labeling close, volume, SMA 20, RSI 14, VWAP at the last bar; custom thinkScript corpus with one intentional compile error; trend line by drag; wheel zoom; pan | Label values equal fixture or API price history within tolerance in `frozen` mode; compile error dialog present for the error study |
| Scan | Run a saved scan, change a filter, save | Result count in declared range; column set equal |
| Alert | Create, edit, delete | Row present then absent; no notification channel on the login |
| Workspace | Detach and reattach a tab, resize a splitter, reorder and resize a column, drop a watchlist symbol on a chart, switch workspace | Window count after reattach; column order; linked gadgets changed, others unchanged |
| Export | Account Statement Export to File (confirmed); grid Copy and chain export (verify in Phase 0) | Column set equal to base; row count; totals equal on-screen (R09) |
| Odd input | Versioned corpus for symbol box, order fields, Analyze inputs, study parameters; sampled nightly with a logged seed; pre-Send surfaces only. A halted or delisted symbol comes from the day's halt list read in a scripted step, never from a fixed symbol; no list that night means the entry is `not_run (reason: scope)` | Pass is a visible rejection or a clamped value shown; fail is crash, freeze, silent acceptance, NaN or Infinity or null, or a third value, each as `assertion_failed` naming the field (a crash through `app_alive`). Fractional shares are a rejected expectation on desktop. Server-side rejections cannot be exercised without a send (B01) |
| Hotkey | Non-order default hotkeys read per build from Setup > Application Settings > Hot Keys by screenshot and OCR (there is no export); each fired once through the gateway as a key chord from a known stable state | The expected panel, dialog or tab by anchor within max wait; the Hot Keys page is itself a checkpoint, so a changed default binding is caught at the source. Order hotkeys and Active Trader hot keys are never fired: attestation (6.3 Run-start settings attestation) requires them unbound and disabled, and the gateway drops chords under the order-stage flag |
| Migration | Load each tier B workspace saved under the previous release on the candidate | Every first-launch and conversion dialog captured; post-load census diffed against the same workspace on base |
| Re-login | Scripted logout and login, paper-mode assertion, fixture verification | Account selector in the allowlist; fixture matches |

No flow sends an order, except class `paper_send_ok` under the gated branch of 6.7. Flows that need a sent order (fills, Working to Filled, cancel and replace, Blast All) carry `requires: [paper_send]` and every checkpoint of theirs is `not_run (reason: entitlement)` until the `paper_send_ok` decision in 6.7 Send policy: both branches. Under the gated branch, `paper_send` joins the login's entitlement profile and these flows enter scope with no other change; the coverage matrix cell reads `not_run (entitlement)` in both branches until then, never a send-policy label of its own.

### 2.7 Layout invariants

Invariants run on every capture of every kind, on both builds, with no baseline. Branch A or B is chosen in Phase 0 (DR-01 in 11.8 Phase 0 spike plan: seven decision records) and recorded in the fingerprint; a bounds-source mismatch between the two builds makes pixel diffs `not_comparable (reason: fingerprint_mismatch)` but invariants still run because they are absolute. This table is the invariant catalog for the whole spec; 12.3 Invariant catalog is a verbatim copy of it, and every mutant in 12.5 Starter mutant catalog names an id from it.

Common rules:

- Bounds. Branch A: JAB contexts (role, name, rect, states) walked from the frame; custom-painted containers exposing no children are marked opaque and their interior falls to branch B rules. Branch B: OCR word boxes (3x upscale, confidence floor 70), template-located controls from registry anchors and Region Registry landmarks, grid lines from projection profiles.
- Persistence. A violation must appear on all 3 settled frames, else it is discarded and counted in flake statistics. A violation that holds is re-captured once by the targeted re-run of 3.10 to confirm persistence; if the re-capture does not reproduce it, the checkpoint is `not_settled (reason: unstable)` with the violation kept as evidence for the flow owner.
- Pre-existing. A violation present on both builds is reported once as pre-existing, one severity level lower, keyed by diff signature so a human decision suppresses it later; never dropped, and the outcome stays `invariant_violation` on the candidate capture. Candidate-only violations get full severity.
- Bounds source failure. Under branch A, a bridge that is down at run start fails the preflight self-test and the run is `aborted_harness` before any product action; a bridge that dies mid-run makes every later checkpoint of the shard `harness_error (reason: bounds_source_unavailable)` until the Watchdog's relaunch restores it, and a bridge still down after one relaunch ends the run `aborted_harness (reason: bounds_source_unavailable)` (1.10). A single element whose bounds cannot be read is recorded per rule and the checkpoint is `needs_human (reason: bounds_unreadable)` unless a higher-precedence outcome applies. Under branch B, an OCR calibration miss is `harness_error (reason: ocr_calibration)` (2.8.1). Masks never apply to invariants: a clipped label inside a masked region is still a violation.
- Per-surface inputs. The registry entry's optional `invariants` block (4.5) carries what the table below calls "listed per surface" or "declared": `min_font_px` (INV-07), `overlap_allowlist` as pairs of controls (INV-01, INV-02, INV-11), `allowed_scrollbars` (INV-06), `signed_columns` (INV-15) and `focus_order` (INV-13); required labels and controls are the entry's `labels` and `controls` (INV-05, INV-10). The INV-15 palette is `palettes/<theme>.yaml` (owner: registry owner, 4.1), chosen by the theme of the workspace's `display_config`.
- Allowlist. Legitimately overlapping components (tooltips, chart crosshair, drag ghosts, popup menus, overlay badges) are listed per surface in `invariants.overlap_allowlist`; an allowlisted pair never fires INV-01, INV-02 or INV-11.
- Severity. S1 heads the Unexpected section; S2 is listed; S3 is under a fold. A tier-1 surface bumps S2 to S1 and S3 to S2.
- Outcome. Every fired invariant produces `invariant_violation` with id, the rects involved and a native crop. The reviewer never reclassifies it.

| Id | Statement | Branch A (JAB) | Branch B (OCR and templates) | False-positive controls | Sev | Outcome |
|---|---|---|---|---|---|---|
| INV-01 | No two sibling components' bounds intersect | Pairwise intersection of child rects per container, both showing and visible by state | Pairwise intersection of template-located control rects and of panels segmented by border lines and header templates | Overlap at least 4 px on both axes and 2 percent of the smaller rect; allowlist; 3 frames | S1 | `invariant_violation` |
| INV-02 | No component extends outside its parent | Child minus parent non-empty by more than 1 px; parent not a viewport with a showing scrollbar | Word box or control rect crossing a located panel border or the client rect by more than 1 px | Viewports excluded unless INV-06 fires; clipped-by-design regions declared per surface | S1 | `invariant_violation` |
| INV-03 | No text clipped at a boundary | Render the accessible name with the pinned per-build font metrics table and compare to inner width minus insets; OCR cross-check | Word box touching a cell or control edge within 1 px with OCR text differing from base at the same anchor, or recognized as a strict prefix of a declared label | Confidence floor 70; compare to base text, not a dictionary; a wider value with more digits is excluded when the Oracle digit count differs; inside a `rule` region or on a `masked_live` checkpoint the base-text comparison is replaced by the region's rule (format, decimals, digit count), so a word box at the edge fires only when its text fails that rule or is a strict prefix of a declared label | S1 | `invariant_violation` |
| INV-04 | No ellipsis where the base had none | Accessible name versus displayed text ends differently | OCR token ends in "..." or the ellipsis glyph where the base OCR at the same anchor did not | Same anchor on both builds; 3 frames; inside a `rule` region or on a `masked_live` checkpoint the base comparison is replaced by the region's rule, so an ellipsis fires only where that rule's format admits none | S2 | `invariant_violation` |
| INV-05 | Required labels present | Accessible names found in the tree | Template above 0.9 or OCR match within one edit in the declared region | Labels declared per surface and checkpoint, never inferred; absence on 3 frames | S1 | `invariant_violation` |
| INV-06 | No unexpected scrollbar | Scrollbar role in a container not allowed to scroll at this size | Scrollbar thumb template inside such a panel | Allowed scrollbars listed per surface with the minimum size at which none is expected | S2 | `invariant_violation` |
| INV-07 | Minimum legible font height | Font size attribute at least the pinned minimum (9 px font, about 7 px cap height, at the pinned font set) | Median word box cap height in the region at least the minimum in native pixels | Per region, not per word; icons excluded by template; thresholds from the base distribution at calibration | S2 | `invariant_violation` |
| INV-08 | No placeholder, loading or error text in a value region | Accessible text in value cells matches none of: Loading, Reconnecting, --, N/A, NaN, null, Infinity, empty-grid strings | OCR of declared value regions against the same set after settle; spinner template absent | Regions with a legitimate placeholder declare it; settle removes transient states first | S1 | `invariant_violation` |
| INV-09 | Every top-level window is inside the virtual screen; every owned dialog inside its owner's screen; main frame rect unchanged since run start | Window rects of the process tree versus virtual screen and the pinned frame rect | Same (no JAB needed) | None; off-screen or moved at pinned geometry is a defect | S2 | `invariant_violation` |
| INV-10 | Every required control present exactly once | Role and name match per declared control, count equals one | Exactly one template match above the confidence floor in the declared region | Controls declared per surface and checkpoint; a count of zero or two or more both fire; 3 frames | S1 | `invariant_violation` |
| INV-11 | No two text runs overlap | Text component bounds pairwise disjoint | Word boxes pairwise disjoint after 1 px erosion, across the whole capture | Allowlist; confidence floor 70; 3 frames | S1 | `invariant_violation` |
| INV-12 | Grid columns aligned | Cells of a column share left or right edge within 2 px per declared alignment; header spans the cells | Word boxes share right edge (numeric) or left edge (text) within 2 px; header spans | Alignment per column type; wrapped text excluded; at least 3 rows | S2 | `invariant_violation` |
| INV-13 | Focus traversal covers declared controls in order (ticket, confirm dialog) | Tab through the gateway; focus owner from JAB equals the declared sequence | Before and after diff per Tab located against declared rects; the changed rect must be the next control | Keyboard-only ticket flows only; Enter never pressed; Escape only at the end | S2 | `invariant_violation` |
| INV-14 | No error dialog or exception window present at capture | Dialog role with title matching the error regex set | Java exception dialog template plus popup catalog error entries | Title regex and template both required; dialog owned by the process tree; the popup sentinel routes error dialogs here, never to the dismiss list; a dead process or frozen EDT is not INV-14 but `assertion_failed (assertion: app_alive)` (2.6.1) | S1 | `invariant_violation` |
| INV-15 | Sign and color agree in P/L and change columns | Sign from JAB value; dominant foreground color of the cell crop against the pinned palette | Sign from OCR extraction; same color test | Declared signed columns only; palette pinned per theme; zero excluded | S1 | `invariant_violation` |

Branch B is noisier on dense dark grids; the controls above are the minimum, and the A/A calibration runs (3.9 A/A calibration and threshold derivation) must show zero false positives per invariant on frozen checkpoints before the invariant is enabled for a surface. Where a surface has no frozen checkpoints (DR-02 Branch B without OnDemand, including under DR-01 Branch B), the enablement test uses the masked_live A/A pairs of 3.9 captured in lockstep with their masks and rule regions in force, and INV-03 and INV-04 use the rule-region control above; structural invariants do not depend on data values, so zero false positives over those calibration captures enables the Branch-B invariant. Until then the invariant is listed per surface as `disabled: calibration pending` in run health, never silently off; an invariant that fires on an A/A pair after enabling is a harness bug recorded as `harness_error (reason: invariant_on_aa)` on the Nightly run. Every invariant has at least one mutant in the canary catalog (12.5 Starter mutant catalog) proving it fires: erased Send button for INV-10, erased label for INV-05, shifted or overlapped panels for INV-01 and INV-11, clipped label for INV-03, inserted ellipsis for INV-04, placeholder text for INV-08, red and green swap for INV-15 (mutant M07 ends `invariant_violation`, INV-15).

### 2.8 Oracle checks

#### 2.8.1 Sources

| Source | Reads | Applies to | Source validation and failure |
|---|---|---|---|
| OCR on upscaled crops | Typed fields (money, integer, percent, date, text) from anchored regions; 3x upscale, inverted, per-field character whitelist | Every checkpoint with rule regions or assertions | A calibration panel of checked-in crops with known values, at least one per field type, is OCRed at preflight (item H9, run `aborted_harness` on a miss) and again at the `canary_flow_passed` gate; a miss there makes every OCR-dependent assertion of the run `harness_error (reason: ocr_calibration)`, never `assertion_failed` |
| CSV export | Account Statement Export to File (confirmed); Position Statement, watchlist, scan and chain exports (verify in Phase 0; an export that does not exist on the build makes its flow class entries `not_run (reason: scope)` with the count in run health) | Export flows; totals cross-check | Header set must equal the export step's declared `headers` (4.7), else `harness_error (reason: export_header_mismatch)` on every checkpoint that depends on the export; no file within the step timeout is step `failed` with step reason `export_timeout`, and an export control the build does not offer is step `failed` with `export_unavailable` (4.13) |
| Grid copy | Copy or Copy All to clipboard (unverified; verify in Phase 0) | Grid reads without a file dialog | Must parse as tab-separated with the declared header; else fall back to OCR and record step event `oracle_source_fallback`; run health counts fallbacks per source and three on one grid in a week open a flow task |
| JAB values | Accessible value and text of cells | Branch A, surfaces where the spike showed values | Per-build tree dump at intake compared with the previous build; a cell the tree no longer exposes falls back to OCR with `oracle_source_fallback` recorded |
| Market-data API | Quote, chain, price history from the internal feed or Schwab market-data API | `api_within` assertions; symbol fixture checks | Preflight item N3 at run start; unreachable makes every checkpoint with an `api_within` assertion `env_error (gate: preflight.N3)`; a mid-run failure makes the remaining ones `env_error (gate: connectivity)` |
| Fixture | Values from the deterministic fixture (DR-02 branch A) or the pinned OnDemand timestamp (DR-02 branch B) | `frozen` checkpoints | Verified at the `fixture_verified` gate; failure is `env_error (gate: fixture_verified)` for every `frozen` checkpoint of the run |

Every extraction is stored (8.3 `oracle_extractions`) beside the capture with source, confidence and crop reference, so rules re-run offline over the stored rows and the base build's numbers reach the reviewer as text.

Census and sweep checkpoints carry no flow assertions; they run every `rule` region of their regions file (3.8). A `diff` or `freeze` region gets an Oracle check only as a flow assertion naming the region; the census half of the money-cell rule is L-RGN-04 (4.8).

Extraction confidence. Every OCR read carries a confidence. The floor is 70 for text and 85 for money, price, integer and percent fields. A read below the floor, or two passes (3x and 2x upscale) that disagree on digit count, is unreadable. The Oracle then tries the alternate source the checkpoint declares (JAB value under branch A; grid copy or export where the surface has one). If no source reads above the floor, the checkpoint is `needs_human (reason: oracle_unreadable)` with the crop, the field name and both reads, never `assertion_failed` and never a pass. On a tier-1 money cell the item is ranked with tier-1 Unexpected findings, and the same cell unreadable in three consecutive runs on one build opens an OCR calibration task for the flow owner (a larger crop, a per-field whitelist change or a `rule` region re-declaration). The calibration panel holds at least one sample of every money field type, so the nightly money-cell canaries M03 to M05 (12.5) prove the read path end to end and a blind OCR path surfaces as canary state `detector_blind` (the missed mutant's record is `harness_error (reason: detector_blind)`, the Nightly run itself stays `complete`, 10.3), not as quiet passes.

thinkorswim Web is not an Oracle source. Driving the web client is outside the authorization of 6.12 Authorization, accountable owner and incident procedure, and whether a web login ends the desktop session is unverified (DR-06). The market-data API is the independent numeric source instead; the web client appears only under B11 in 2.13.

#### 2.8.2 Tolerances

| Quantity | Tolerance | Reason |
|---|---|---|
| Money on ticket or confirm dialog, `frozen` | Exact after rounding to the cent | Deterministic fixture |
| Money, `masked_live` | 0.01 plus one tick per leg | Mid-price rounding |
| Prices against the API, `masked_live` | Compare to the API value as of capture time minus 20 minutes, within 1 tick; exact equality only for symbols whose last session closed more than 20 minutes before capture and that are absent from the 24-hour trading list read in-platform each run | paperMoney data is 20-minute delayed by default (Schwab); the 24/5 list changes |
| Percent | 0.01 absolute | Display rounding |
| Greeks | Delta 0.01; others 1 unit of the last displayed digit | Display rounding |
| Study labels against API price history, `frozen` | Exact for close and volume; 0.5 percent for SMA, RSI, VWAP | Vendor rounding of derived values |
| Row counts | Exact in `frozen`; declared range in `masked_live` | Data moves |
| Dates, DTE | Exact; DTE in calendar days from the rig's America/New_York date | Platform convention |

A tolerance is never raised to make a run pass; a change is a PR with reason and owner.

#### 2.8.3 Arithmetic invariants

Multipliers come from a fixture table: equity and SPX options 100, /ES 50, /NQ 20, /MES 5, /MNQ 2, forex per pair lot size.

| Rule | Statement |
|---|---|
| R01 | Quantity times price times multiplier equals cost on the Order Confirmation dialog, per leg and total, within money tolerance |
| R02 | Buying power effect sign is negative for an opening order and positive for a closing order, and its magnitude lies in the bracket the fixture's margin table gives per asset class and account: stock 50 to 100 percent of notional on the margin account and 100 percent on the IRA, long option the premium, futures the exchange initial margin in the fixture table. Phase 0 assumption: the exact paperMoney formula per asset class is read from twenty staged tickets and pinned in `fixtures/margin_rules.yaml`; until pinned, R02 checks sign and bracket only and the report labels its results "bracket check" |
| R03 | Bid at most mark at most ask on every quoted row; last within session low and high |
| R04 | Strike count equals the Strikes selector (all when All); expiration rows equal the header's list |
| R05 | Absolute delta at most 1 and monotone across strikes per expiration; gamma and vega non-negative; theta non-positive for long options |
| R06 | Put-call parity within tolerance for European-style cash-settled options only (SPX); American-style equity options use the inequality bounds, never the equality |
| R07 | DTE equals expiration date minus capture date in calendar days |
| R08 | Position Statement P/L Open equals (mark minus trade price) times quantity times multiplier per row; totals equal the sum of rows |
| R09 | Export total equals the on-screen total for the same grid |
| R10 | Number format matches field type: no NaN, Infinity, null, double sign or misplaced separator |

Rule operands. A `rule` assertion (4.7) binds each operand by name in its `inputs` to a region of the checkpoint or, where the type allows, a literal. This table is the operand list; the lint's and the Oracle's operand lists are generated from it. Types are the field types of 2.8.1 (`money`, `price`, `integer`, `percent`, `date`, `text`) plus `decimal` for Greeks.

| Rule | Operand | Type | Required |
|---|---|---|---|
| R01 | `qty` | integer | yes |
| R01 | `price` | price | yes |
| R01 | `multiplier` | integer (a literal, or a region of symbols whose multipliers come from the fixture table above) | yes |
| R01 | `total` | money | yes |
| R02 | `bp_effect` | money | yes |
| R02 | `notional` | money | yes |
| R03 | `bid` | price | yes |
| R03 | `mark` | price | yes |
| R03 | `ask` | price | yes |
| R03 | `last` | price | no (with `low` and `high`, all three or none) |
| R03 | `low` | price | no |
| R03 | `high` | price | no |
| R04 | `count` | integer (the row count of the region) | yes |
| R04 | `selector` | text | yes |
| R05 | `delta` | decimal | yes |
| R05 | `gamma` | decimal | no |
| R05 | `vega` | decimal | no |
| R05 | `theta` | decimal | no |
| R06 | `call` | price | yes |
| R06 | `put` | price | yes |
| R06 | `strike` | price | yes |
| R06 | `underlying` | price | yes |
| R06 | `rate` | percent | no |
| R06 | `dte` | integer | no |
| R07 | `dte` | integer | yes |
| R07 | `expiration` | date | yes |
| R08 | `pl_open` | money | yes |
| R08 | `mark` | price | yes |
| R08 | `trade_price` | price | yes |
| R08 | `qty` | integer | yes |
| R08 | `multiplier` | integer (as in R01) | yes |
| R09 | `export_total` | money | yes |
| R09 | `screen_total` | money | yes |
| R10 | `value` | text (checked against the assertion's `format`) | yes |

A rule failure is `assertion_failed`, reported with field, expected, extracted and native crop. It fires even when base and candidate are pixel-identical, which is the point: a number wrong in both builds is otherwise invisible. Rule ids R01 to R10 are the operand of the `rule` assertion kind (2.6.2) and the ids the flow lint checks; a rule whose required operands are not all bound to regions declared on the checkpoint (or to literals) fails lint (L-FLW-11), never silently skips.

### 2.9 Explorer

A sixth, bounded activity, not a test kind. Under DR-07 branch B (computer use not covered by the data agreement, 6.11 Computer use under the data agreement) it is not built and RC runs end at the sweeps, with the line "Explorer: not available under DR-07 B" in the RC report. Under branch A:

- Isolation: own paperMoney login with no verified email or phone, own VM snapshot, own saved workspace holding only allowlisted tabs, never a session shared with scripted flows, scheduled last in RC runs on rig 6 after everything else is reported (11.3 Sharding and parallelism).
- Toolset: only the Input Gateway's restricted actions (5.6 Agent-mode toolset): click a named target resolved by the locator ladder, type a fixture string by id, press one of a fixed key set (Tab, Shift+Tab, Escape, arrows, page keys, Home, End), wheel, `drag_named` between named targets (allowed only on splitters and detach handles; order-bubble drags on charts and the ladder are denied as order paths, 6.8), and zoom implemented by cropping at native resolution. No raw coordinates, no free text, no Enter or Space; a tool call outside the set is a tool error that ends the session.
- Deny list: the table in 6.8 Explorer boundary and deny list is authoritative and the gateway enforces it by window title, under-point OCR label and landmark-anchored geometry: Chat Rooms and Support chat, Share and tos.mx links, Setup menu, Application Settings and Order Defaults, Adjust Account, Logout and account switch, browser-opening controls, Help menu, Export, Print, Save or Delete workspace, MarketWatch Alerts creation, Saved Orders, Blast All, Flatten, Active Trader order columns, the Buy, Sell, Buy custom and Sell custom items of chart, Flexible Grid, watchlist, chain and Position Statement context menus, BUY MKT and SELL MKT, every ladder or Level II bid or ask cell, and every order-entry button (each opens or fills an order ticket). A click landing on a denied surface is `blocked_by_gateway` with a screenshot; three denied attempts in one session end the session; an attempted send ends the session at once and counts toward the per-run kill-switch trip (6.2, 5.4.8).
- Order stage in a session: if the ticket or dialog flag (`order_stage`, 6.2) is ever set in an Explorer session, the gateway drops all input, closes the window through its recovery path (Escape token), and the session ends with the finding `explorer_order_stage` (S1, paged and notified per 9.10, plus the Explorer owner); the event counts toward the per-run trip of 10 (5.4.8) and the RC run itself continues. The 30-second open-dialog kill-switch timer of 6.6 applies to scripted flows and agent rescue only, never to an Explorer session.
- Budgets, enforced by the Orchestrator and never by the prompt: 400 actions, 90 minutes wall clock, 25 dollars per session (plus the weekly cap of 11.4), and early stop after 20 consecutive actions with no new screenshot hash. The RC run's capture deadline caps the session as well: a session that starts late ends at the deadline (1.9). Exhaustion ends the session with a run-health line naming the budget hit; it never ends the run. If the model endpoint is unavailable at T+6:20 (`agent_available: false` after preflight N2 or N5, 12.7, with no recovery logged), the session does not start: it is `not_run (reason: model_unavailable)`, the RC report's Explorer section and run health print "Explorer: not run, model endpoint unavailable", the Explorer owner is notified, and the run is unaffected; a recovery logged before T+6:20 lets it start as planned.
- Steering: toward surfaces with the fewest flow checkpoints in the current RC run and surfaces whose `primitives` intersect the manifest's touched primitives, from a target list the Orchestrator computes before the session starts. Odd inputs come from the seeded corpus with the pass rule of 2.6.3.
- Logging per action (one `step_events` row with `mode: explorer`, 8.3 Schema): index (`seq`), time (`wall_at`, `mono_ms`), stated target (`target_text`) and expected outcome (`expected_outcome`), resolved rect (`screen_x`, `screen_y`, `rect_w`, `rect_h`), keys (`keys`), gate decision (`gate_decision`, `gate_reason`), pre and post screenshot hashes (`pre_hash`, `post_hash`), 96x96 click crop (`click_crop_sha256`), model id (`model_id`), prompt hash (`prompt_hash`), usage (`usage_json`). Before each action the agent states target and expected outcome; after it, the post-action state is verified by the cheapest means available (JAB focused element under branch A, else a template or OCR check of the expected anchor). A mismatch is logged `agent_error` and is never a product finding.
- Findings: record with signature (surface, invariant or observation, crop hash), severity, confidence, raw values, trace reference, build, first and last seen; the same signature on a later night updates last seen instead of opening a new finding. Each is emitted as a candidate flow in `flows/quarantine/<finding-id>.yaml` (class `explorer_candidate`, 4.7 Flow file) from the trace since the last named stable state, anchored by click crops, and replayed exactly once in scripted mode on the same build within the RC run. No automatic trace minimization: a replay with steps removed costs minutes per probe against a live session, so trimming is a human step (an owner may enable a bounded mode of at most 8 replays for one finding). Only a reproducing finding is reported, through the replayed flow's own checkpoint outcomes: `invariant_violation` or `assertion_failed` when the finding is one of those, else `needs_baseline (reason: explorer_replay)` with the Explorer trace and the candidate flow attached, since a new flow has no base capture; never `needs_human`. A non-reproducing one is held with triage state `agent_mistake` or `expected_behavior` (8.3), listed under "Explorer: not reproduced" in the RC report (9.4 Merge and RC report structure) and counted in Explorer precision. A human trims and promotes the flow into the library; until the next good build captures a baseline, the promoted flow's checkpoints are `needs_baseline`.
- Teardown: every session ends by replaying the server-side mutation ledger (items the Explorer created on its login), deleting each and verifying by reading, then the snapshot revert (6.8).
- Metrics per RC run: Explorer precision (reproduced over emitted findings), agent-error ratio (agent errors over actions), deny-list hits, budget hit, dollars. Precision under 30 percent on two consecutive RC runs pauses the Explorer until the steering list or toolset is fixed; the pause is a run-health line, not silence.

The Explorer never produces checkpoint outcomes itself; the replayed flow does, through the normal path.

### 2.10 Coverage model: Dependency Map and static impact

#### 2.10.1 Dependency Map

`registry/dependency-map.yaml` maps each hosting primitive to its consuming surfaces. Its schema is 4.4 Dependency map: a `primitives` list (id, `source_packages`, owner, `global`) and an `edges` list, each edge `(primitive, surface, source: declared or static_analysis, confidence, evidence or reason, confirmed_from_source)`; a surface's own `roots` live on its registry entry. Illustrative content (timestamps omitted):

```yaml
primitives:
  - {id: FlexibleGrid,    source_packages: ["TBD.flexgrid.**"], owner: ui-platform}   # TBD. placeholders, filled from source in Phase 0 (12.9)
  - {id: Docking,         source_packages: ["TBD.docking.**"],  owner: ui-platform}
  - {id: ColumnModel,     source_packages: ["TBD.table.**"],    owner: data-ui}
  - {id: Theme,           source_packages: ["TBD.laf.**"],      owner: ui-platform, global: true}
  - {id: FontMetrics,     source_packages: ["TBD.text.**"],     owner: ui-platform, global: true}
  - {id: ChartEngine,     source_packages: ["TBD.charts.**"],   owner: charts}
  - {id: OrderTicketCore, source_packages: ["TBD.trade.**"],    owner: trade-ui}
  - {id: LadderModel,     source_packages: ["TBD.ladder.**"],   owner: trade-ui}
edges:
  - {primitive: FlexibleGrid, surface: charts.flexible-grid, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: FlexibleGrid, surface: trade.active-trader.ladder, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: FlexibleGrid, surface: sidebar.level2, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: Docking, surface: "*.detached", source: static_analysis, confidence: 0.9, evidence: "..."}     # every detachable surface
  - {primitive: ColumnModel, surface: monitor.activity.position-statement, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: ColumnModel, surface: trade.all-products.option-chain, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: Theme, surface: "*", source: declared, confidence: 1.0}
  - {primitive: FontMetrics, surface: "*", source: declared, confidence: 1.0}
  - {primitive: ChartEngine, surface: "charts.*", source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: ChartEngine, surface: analyze.risk-profile, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: ChartEngine, surface: sidebar.quick-chart, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: ChartEngine, surface: trade.active-trader.chart, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: OrderTicketCore, surface: "trade.order-entry.*", source: static_analysis, confidence: 0.9, evidence: "..."}   # every ticket variant
  - {primitive: OrderTicketCore, surface: dialog.order-confirmation, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: OrderTicketCore, surface: analyze.add-simulated-trades, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: LadderModel, surface: trade.active-trader.ladder, source: static_analysis, confidence: 0.9, evidence: "..."}
  - {primitive: Theme, surface: dialog.order-confirmation, source: declared, confidence: 1.0, reason: "colors via properties, invisible to bytecode scan"}
```

The map is derived, not hand-written. A pipeline job runs a bytecode dependency scan (jdeps at class verbosity, or an ASM scanner) over the build's jars, resolves each surface's `roots` to classes, computes for each primitive's `source_packages` the surfaces whose classes transitively depend on them, and proposes the `static_analysis` edges (confidence raised to 1.0 and `confirmed_from_source: true` once a named build's scan has seen the dependency, 4.4). `declared` edges are human-owned and add what the scan cannot see (reflection, resource bundles, properties-driven theming), each with a `reason`. The committed map is regenerated on every merge to main and reviewed like code. A scan that fails (jar unreadable, scanner crash) leaves the previous committed map in force and opens a `dependency_map_gap` with reason `scan_failed` on the next merge run, so a stale map is visible. The starter list is in 12.9 Dependency map starter list.

#### 2.10.2 Static impact step

PR runs do not trust the committed map alone. The Orchestrator takes the diff's changed classes, computes their reverse transitive dependents on the candidate build's own dependency index (depth bounded at 6, configurable), intersects with every surface's `roots`, and obtains the surfaces the change can reach. Any such surface not reached through the committed map's expansion of the touched primitives and not declared in the manifest produces `dependency_map_gap` (detail `missing_edge`) naming the missing edge (primitive, surface) and opens a proposed PR against the map. A changed class in no primitive's and no surface's `roots` is a gap only when it reaches a registered surface; it is then recorded as one `dependency_map_gap` (detail `unowned_root`), separate from the `missing_edge` findings, so the primitive vocabulary grows instead of silently missing an edge. A changed class that reaches no surface (backend code) is not a gap.

### 2.11 Scope computation per run type

Inputs: registry R, committed Dependency Map D, manifest M (declared surfaces, touched primitives, tickets; may be absent), diff delta, candidate dependency index S (may be absent), entitlement profile E, quarantine list Q, flow library F, corpus W with its `hosted_by` relation, the capture budget B per rig with per-item durations, and the rig pool for the run.

Budget B is capture time per rig, by one rule for every run type: `B = capture_time(run_type)`, the run type's wall budget (11.2) minus the 10-minute report margin minus the session startup of the 11.2 capacity table for every session the rig runs (4 minutes, 8 minutes when the session includes a fixture reset). Per-item durations are the 30-day p50 from the run store, with the planning defaults of 11.1 for an item without history, and each workspace switch costs its measured load time. The minute marks of 1.7 and 1.8 are illustrative; this budget and the capacity model of 11.2 are normative.

```
function common_filters(items, E, Q):
  for item in items:
    if not item.entitlements subset_of E.entitlements: outcome(item) = not_run(entitlement); drop   # flows: requires
    if item.id in Q:                                outcome(item) = not_run(quarantine);  drop
  return items

function order_and_cut(items, B, rigs):
  sort by (tier asc, declared surface first within a tier, kind order flows, sweeps, census,
           estimated_duration desc); census items grouped by workspace
  shard: census items keyed by (surface tab, data_mode); flows by stable hash of flow_id within their tier;
         flows sharing a state_scope (4.7) or a fixture pinned to one shard; shards packed longest first
         (LPT) by measured p50, session startups included (4 min, 8 min with a fixture reset; 11.2)
  frozen items: base then candidate on the same rig, back to back; a stored base capture is reused when
                its reuse key matches (3.1); a frozen shard may run on an idle pair rig
  masked_live items: two identically fingerprinted rigs in lockstep; with one rig free they are
                not_run(budget), never captured minutes apart
  while estimated duration on any rig > B:
     item = last item of the highest tier number on that rig, never a declared or smoke surface
     outcome(item) = not_run(budget); remove
  return items per rig

function smoke_subset(R):
  return { s in R : s.smoke }   # flagged by the registry owner, at most 40 (2.2.4): tier-1 surfaces with
                                 # money cells or order entry first, one surface per top-level tab,
                                 # every attested Application Settings page

function sweeps(s, ladder, splitter_steps):          # 2.5.1: w x h steps only on hosts that take a size;
  sized = s.hosts intersect {flexgrid, detached, dialog}   # tab and sidebar hosts only through splitters
  return { (s, host, step) : host in sized, step in ladder } union { (s, step) : step in splitter_steps(s) }

function scope_pr(R, D, M, delta, S, E, Q, F, B):
  declared        = M.surfaces if M present else {}
  changed         = classes_in(delta)
  touched_prims   = (M.primitives if M present else {}) union { p in D : roots(p) intersects changed }
  map_expanded    = union over p in touched_prims of D[p].surfaces
  dependents      = reverse_closure(S, changed, max_depth = 6) if S present else {}
  static_surfaces = { s in R : roots(s) intersects dependents }
  if M absent:
     if static_surfaces union map_expanded non-empty or S absent:
          emit manifest_missing                      # case (a): UI-touching PR; diffs will be collateral
     else: mark run no_manifest                      # case (b): non-UI PR; diffs will be needs_human(no_manifest)
  for s in static_surfaces - map_expanded - declared, reached from a changed class owned by primitive p:
      emit dependency_map_gap(missing_edge = (p, s)); propose map PR
  for c in changed with no owning primitive and no owning surface, and reverse_closure(S, {c}) reaching a surface:
      emit dependency_map_gap(unowned_root = c)    # one finding per class, separate from missing_edge;
                                                   # a backend class that reaches no surface is not a gap
  change_surfaces = declared union map_expanded union static_surfaces
  census_surfaces = change_surfaces union smoke_subset(R)
  census_items    = { (s, w) : s in change_surfaces, w in s.census.workspaces }
                    union { (s, s.census.workspaces[0]) : s in smoke_subset(R) - change_surfaces }
                                                   # undeclared smoke surfaces: the canonical workspace only
  sweep_items     = union over s in change_surfaces with s.resizable and s.tier == 1
                      of sweeps(s, ladder_S, splitters_short)     # smoke adds census only (11.2 budget)
  flow_items      = { f in F : f.tier == 1 and (f.surfaces intersects change_surfaces
                                                or f.primitives intersects touched_prims) }
  if run is no_manifest (case (b)):                # no UI scope, but a backend change can still move values
     flow_items  = flow_items union { f in F : f.tier == 1, f.class == standard, f.surfaces intersects smoke_subset(R) }
  for s in R - census_surfaces: outcome(s) = not_run(scope)
  return order_and_cut(common_filters(census_items union sweep_items union flow_items, E, Q),
                       B = capture_time(pr), rigs)

function scope_merge(R, E, Q, F, W, B):
  census_items = { (s, w) : s in R, w in W.tierA, s hosted_by w or s.global }
  sweep_items  = union over s in R with s.resizable and s.tier <= 2 of sweeps(s, ladder_S, splitters_short)
  flow_items   = { f in F : f.tier == 1 }
  for (s, w) with s not hosted_by w and not s.global: outcome = not_run(scope)
  return order_and_cut(common_filters(census_items union sweep_items union flow_items, E, Q),
                       B = capture_time(merge), rigs)

function scope_rc(R, E, Q, F, W, B):
  census_items = { (s, w) : s in R, w in W.tierA union W.tierB, s hosted_by w or s.global }
  sweep_items  = union over s in R with s.resizable of
                   (sweeps(s, ladder_F, splitters_full) if s.tier <= 2       # splitters_full for tiers 1 and 2,
                    else sweeps(s, ladder_S, splitters_short))               # splitters_short for tier 3
  flow_items   = F                          # all tiers and classes, migration flows on tier B
  soak_items   = the 24 soak cycle checkpoints flow/<workspace-surface-id>/soak/c<nn> on rig 5 (10.11 Soak; 1.9)
  plan = order_and_cut(common_filters(census_items union sweep_items union flow_items union soak_items, E, Q),
                       B = capture_time(rc) - explorer_budget, rigs)   # explorer_budget = 90 min (2.9); scripted work ends by T+6:20
  append Explorer session on rig 6 with its own login, last (2.9); a later start is cut at the deadline (1.9)
  return plan
```

Rules the pseudocode relies on:

- A manifest with zero declared surfaces is allowed (pure refactor) and still yields the static impact set. A missing manifest file has two cases, decided by the static impact step. Case (a), the PR touches UI code (`static_surfaces` or `map_expanded` is non-empty): finding `manifest_missing`, the run proceeds with nothing declared, and every above-threshold diff is `collateral`, because an undeclared UI change is by definition unexpected and the developer must declare it or fix it. Case (b), the PR touches no UI code: no finding, `intended` is disabled, and every above-threshold diff is `needs_human (reason: no_manifest)`, since a side effect from non-UI code is possible but intent cannot be checked. The PR comment header states which case applied and links the manifest template.
- Tier 1 does not imply smoke. The smoke subset is the set of surfaces the registry owner flags, at most 40 (2.2.4); an unflagged tier-1 surface is still reached by the merge and RC census and by the tier-1 standard flows.
- Budget cuts hit the highest tier number first and never a declared or smoke surface. If those alone exceed the PR budget the run proceeds, run health prints the projected overrun at scope time, and at the 45-minute deadline the Orchestrator marks every unreached checkpoint `not_run (reason: budget)` and the run `aborted_budget` (1.7 How a PR run works, minute by minute); the comment still posts.
- Every registry surface outside the computed scope is `not_run (reason: scope)`, so the PR matrix shows exactly what was not looked at.
- Checkpoints a run never reached because it aborted take the abort's class: `env_error` for `aborted_safety`, `harness_error` for `aborted_harness`, `not_run (reason: budget)` for `aborted_budget` (1.10).
- Nightly and calibration runs compute no scope; they take the canary and calibration checkpoint lists (10.3 Canary Service and the mutant catalog; 3.9 A/A calibration and threshold derivation).
- If the candidate's dependency index is missing from the pipeline artifact, the static step is skipped, all other outcomes are produced, and one `dependency_map_gap` with reason `no_dependency_index` appears in run health. Without the index case (b) cannot be told from case (a), so a missing manifest with a missing index is treated as case (a). The absence is never silent.
- `splitters_short(s)` and `splitters_full(s)` are the short and full fraction sets of 2.5.2 intersected with each declared splitter's `fractions` list (2.2.1); a surface with no `splitters` contributes no split checkpoints. `ladder_F` includes Ladder F's ascending pass, whose steps carry the `-asc` id suffix (2.5.2). A surface with `streaming: true` contributes no sweep items; its sweep checkpoints are `not_run (reason: scope)` (2.5.3).

### 2.12 Coverage accounting and the matrix

The Reporter prints the coverage matrix for every run, defined exactly so runs compare cell by cell.

- Rows: every surface id at the build's registry commit, plus one synthetic row per `unregistered_surface` found this run.
- Columns: persona (active trader, options trader, futures trader, swing trader, beginner; a tier B workspace maps to its persona tag, which every tier B entry must carry, 4.6; the tier C harness carries none, so its sweeps count toward the surface's primary persona) times entitlement profile (phase 1: paperMoney margin, paperMoney IRA; more as logins are granted).
- Cell key: (surface id, persona, entitlement profile). State is exactly one of `covered`, `not_run`, `not_registered`.

```
cell(surface, persona, profile):
  if surface is synthetic: return not_registered
  C = checkpoints this run with this surface, a workspace carrying persona, a login with profile;
      sweep and split checkpoints count toward the surface's primary persona (personas[0], 4.5),
      wherever they run
  if C is empty:
     if not surface.entitlements subset_of profile.entitlements: return not_run(entitlement)
     if surface in Q:                                        return not_run(quarantine)
     if surface cut by budget:                               return not_run(budget)
     return not_run(scope)          # includes: no workspace of this persona hosts the surface
  usable = { c in C : outcome(c) in {identical, noise, intended, collateral, needs_human,
                                      invariant_violation, assertion_failed, needs_baseline} }
  if usable is empty:
     blocking = most frequent outcome in C among {not_run, not_settled, not_comparable,
                                                  landmark_missing, env_error, harness_error}
     if blocking == not_run: return not_run(the most frequent not_run reason in C)
                                    # entitlement, budget, scope, quarantine, step_failed:<step>, app_crash, base_step_failed, model_unavailable
     return not_run(blocked:<blocking outcome>)
  return covered(worst = max over usable in the order
                 invariant_violation > assertion_failed > needs_baseline > collateral > needs_human > intended > noise > identical   # the 1.10 precedence,
                 n = |usable|, kinds = test kinds that produced usable checkpoints)
```

Display rules:

- A `covered` cell shows the worst outcome and the count, never the word "passed"; all identical reads "identical (n)".
- A `not_run` cell shows its reason verbatim: `entitlement`, `budget`, `scope`, `quarantine`, `step_failed:<step>`, `app_crash`, `base_step_failed`, `model_unavailable`, or `blocked:<outcome>` naming the checkpoint outcome that stopped comparison (`not_settled`, `not_comparable`, `landmark_missing`, `env_error`, `harness_error`). A blocked cell is a fact about the run, counted in run health, not in coverage percent. The `coverage_cells.not_run_reason` column (8.3 Schema) accepts exactly this list.
- `needs_baseline` counts as covered for invariants and uncovered for comparison; the footnote gives the count.
- Coverage percent per tier is covered cells over cells that are not `not_run (entitlement)` or `not_run (scope)` by persona (a surface absent from a persona's workspace by design). A PR run's percent is relative to its computed scope, a merge run's to the whole registry; both print their denominators.
- Above 2 percent of tier-1 checkpoints `not_run (quarantine)`, run health is red and the RC gate blocks (10.5 Flake tracking, targeted re-runs and quarantine). The share is counted in checkpoints, as 10.5 and 1.5 count it; the matrix also prints the share of tier-1 cells for display.
- Aggregated by surface, the matrix feeds the PR comment; the full matrix is a report UI page (9.5 Report UI pages). Merge, RC and Nightly runs store one `coverage_cells` row per cell; PR runs keep the matrix in the run summary (8.3).
- A manifest coverage table lists every manifest entry with the checkpoints whose surfaces it names. An entry with none prints "no covering checkpoint" and opens a flow-authoring task; an entry with a covering checkpoint and no diff above the noise floor, or whose visibility check in 3.12 (task C) returns `not_visible` or `unsure`, raises the run-level finding `intended_change_not_observed` in the Intended section and opens a triage-queue question to the PR author and feature owner with the entry and the covering checkpoints attached (9.4 Merge and RC report structure); it is a finding, and no checkpoint outcome changes. An intended change that failed to ship is a visible row, never a clean report.

### 2.13 What this model cannot detect and how it is labeled

What the model does not look at is printed, not implied. Each class below has a stable id, the mechanism that labels it in every merge and RC report under "Not detectable by design", and the phase that changes its status, if any.

| Id | Not detectable | How it is labeled | Changes in |
|---|---|---|---|
| B01 | Anything after Send: fills, rejections, server-side validation (buying power, price limits, exchange quantity rules), Working to Filled, cancel and replace, Blast All, status rows | Flows carry `requires: [paper_send]`; every cell `not_run (entitlement)`; every report prints the line "send policy: default (no sends); N order-lifecycle surfaces not run (entitlement)" (9.2) | Phase 2 if `paper_send_ok` is approved (6.7 Send policy: both branches), else never; fills stay labeled untested even then until a human verifies that paperMoney simulates them on the delayed feed |
| B02 | A value wrong in both builds with no Oracle rule or assertion over it | Ends as `identical` or `noise`; an Oracle coverage table lists per tier-1 surface the value regions with and without a rule | Grows with rules; never complete |
| B03 | Temporal behavior: cell flashes, ladder re-centering, Level II and Time and Sales churn, flicker, frozen quotes | Captures are stills; streaming regions are `rule` or `mask`; the report lists surfaces with masked streaming regions and their masked share | Phase 3 soak adds frame-energy checks on a few checkpoints |
| B04 | Entitlement states paperMoney cannot produce: option approval tiers, cash-account unsettled funds, pattern day trade counter, portfolio margin, multi-account | `not_run (entitlement)` on every cell of the missing profile; the profile list prints what exists, and a paperMoney-versus-live known-gaps line (delayed data, simulated fills, no real rejections) prints under it | When fixed-entitlement test logins are granted; whether paperMoney mirrors the live login's option approval level is unverified and is read in-platform in Phase 0 |
| B05 | Live-feed data-format defects when `frozen` mode replays data (OnDemand or fixture) | Every checkpoint records its data mode; the report prints `frozen` versus `masked_live` counts per tier; `masked_live` chain and quote checkpoints stay in merge runs for this reason | DR-02 branch A reduces, never removes |
| B06 | Rendering at non-canonical configurations: 125 and 150 percent scaling, light theme, large app font, 1366x768, mixed-DPI multi-monitor | Configuration is in the fingerprint; the matrix is per configuration and the report header prints "configurations covered: N of M" (9.2) | Phase 3 tier B configurations; mixed-DPI needs physical hardware |
| B07 | macOS and Linux builds | Non-goal; no registry entries; the report header prints "platform: Windows 11 desktop only" | Not in phases 1 to 3; the registry, flow and checkpoint formats carry no Windows-only field, so nothing precludes it |
| B08 | Study mathematics beyond the verification set and thinkScript corpus | Only corpus studies have expected values; the report prints corpus size and the count of corpus-workspace studies without expectations | Grows with the corpus |
| B09 | Accessibility conformance | Branch A diffs the accessible tree build to build and reports lost names as `collateral`; nothing absolute is asserted; branch B prints "accessible tree: not available" | Not a goal |
| B10 | Side effects outside the window: email, SMS, push, sound, print, browser launches | Explorer login has no verified channels; the browser is a logging stub whose log is printed | Never |
| B11 | Cross-client state (thinkorswim Web, mobile) written by the desktop build, and the web client as a numeric oracle | Not observed; the report prints "cross-client sync: not observed". Not adopted as an Oracle source because web automation is outside the authorization of 6.12 and the session rule is unverified (DR-06) | A weekly read-only sync flow (create a watchlist, alert and drawing on desktop, read them back through a logged-in web session) only after a written automation ruling for the web client; until then never |
| B12 | Interactions outside the step vocabulary (touch, pen, IME, screen readers, drag between displays) | Flows cannot express them; the vocabulary is the closed list in 2.6.1 | Multi-monitor in Phase 3 on virtual displays |
| B13 | Surfaces unreachable because a gate failed (session rule, blocking popup) | `env_error` with gate name and screenshot; counted in run health, never in coverage | DR-06 in 11.8 sets parallelism |
| B14 | Defects whose only symptom is in the app log or a crash file | The Watchdog attaches logs and crash files per checkpoint; a crash is `assertion_failed (assertion: app_alive)` with the `app_crash` finding (2.6.1); a new exception signature in the log slice with the app alive is a tier-2 `new_log_signature` finding on the nearest checkpoint with the log lines attached (10.11), a finding that never changes the outcome; log content beyond signatures is not asserted | Phase 2 signature diff |
| B15 | A regression on a dialog the registry has but no flow reaches | Dialogs are not enumerable; the popup catalog catches those that appear on their own as `unknown_popup`; the report prints the count of registered dialogs with zero flow checkpoints | Grows with flows and Explorer candidate flows |
| B16 | Behavior with no visible symptom at any checkpoint: a key that does nothing, a setting saved but not displayed, timing, background network calls, memory growth within a run | Checkpoints and assertions are the only observation points; the report prints per flow the count of steps not followed by a checkpoint or assertion within 3 steps ("unobserved steps") and the share of tier-1 steps that are observed | Soak in Phase 3 covers memory, freeze and restart counts (10.11 Soak, 1.9 How an RC run works, 11.2); the log signature diff in Phase 2 covers B14; the rest never |

Everything else in this section ends in a closed outcome with a named mechanism. A newly discovered blind spot is added to this table with an id before work on it starts, so the report prints it the same day.
