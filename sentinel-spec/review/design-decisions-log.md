# Integration notes: decisions writers made where the brief was silent

The integrator must make every section agree with ONE value for each item below. Where two sections differ, the "Resolve to" column is the decision.

| Topic | Section 03 says | Section 04 says | Section 12 says | Resolve to |
|---|---|---|---|---|
| Mask budget, Order Confirmation dialog | 10% | 10% | (n/a) | 10% |
| Mask budget, order entry ticket | 10% | 10% | | 10% |
| Mask budget, Position Statement | 5% | 15% | | 10% |
| Mask budget, Account Info | 5% | (not stated) | | 5% |
| Money cells (Qty, Price, Buying Power Effect, Max Loss, account number, Send/Cancel/Delete) | 0% | 0% | money flag, 100% canary detection | 0% masked, 100% canary detection |
| Mask expiry default | 30 days | 30 days | | 30 days |
| Instability after targeted re-run | `needs_human (reason: unstable)` | | | `needs_human (reason: unstable)`; quarantine is `not_run (reason: quarantine)` |
| Candidate-side step failure, downstream checkpoints | `harness_error (reason: step_failed:<step>)` | | `not_run (scope)` | `not_run (reason: step_failed:<step>)` pointing at the step finding. A step failure is a product or locator finding, not a harness error, and downstream checkpoints were not reached. |
| Base-side step failure | `needs_baseline` only if manifest `new_surface: true`, else `harness_error (reason: base_step_failed)` | | | Keep as 03 states, but use `not_run (reason: base_step_failed)` instead of `harness_error` for the same reason as above |
| PR run with no manifest | | `intended` disabled; every above-threshold diff is `needs_human (reason: no_manifest)` | | Keep 04 |
| Region with no calibrated floor | | threshold 0, `needs_human (reason: uncalibrated)` | | Keep 04 |
| Config lint failure at run start | | run `aborted_harness (reason: config_invalid)` | | Keep 04 |
| Undeclared working order at run start | | run `aborted_safety (reason: unexpected_working_order)` | `aborted_safety` | Keep |
| Position or watchlist drift at run start | | `env_error (reason: fixture_drift)` | | Keep |
| Preflight environment failure | | | every checkpoint `env_error (gate: preflight.<item>)`, run `complete_with_env_errors` | Keep 12 |
| Harness self-test failure | | | `aborted_harness` | Keep |
| Foreign-process occlusion | `env_error` | | | Keep |
| Unexpected thinkorswim window at max wait | `not_settled (reason: unexpected_window)` | | | Keep |
| Reviewer vocabulary | `matches_manifest`, `partially_matches_manifest`, `does_not_match_manifest`, `no_visible_difference`, `cannot_judge`; orchestrator maps to outcomes; routing threshold 0.80 | | | Keep; it is not part of the outcome taxonomy |
| Reviewer call caps per run | PR 60, merge 200, RC 500 | | | Keep |
| "Temperature 0" | Current Opus-tier models reject sampling parameters; implemented as no sampling parameters, pinned effort, byte-stable prompt, majority vote at borderline | | | Keep; brief wording "temperature 0" means this |
| Pinned reviewer model | Opus-tier example id, because Fable-tier is excluded from zero data retention unless authorized; Phase 0 ruling decides | | | Keep |
| Vision input size | 1:1 crops padded 24 px; tile above 1568 px long edge; optional 2x copy for text under 12 px | | | Keep |
| Invariant font floor | | | 7 px cap height at 1920x1080 100% DPI | Keep |
| Invariant catalog | | | INV-01 to INV-11 (adds INV-09 window rects inside virtual screen, INV-11 no overlapping OCR word boxes) | Section 02's catalog must use the same ids and statements as 12.3; if 02 has more, 12.3 is extended |
| Mutant catalog | | | M01 to M28, seed 20260101 | Section 10 must reference the same ids |
| Attestation checklist | | | A1 to A11 | Section 06 must use the same ids |
| Preflight checklist | | | 30 items in groups R, B, H, N, G | Sections 05, 07, 10 must use the same ids |
| Popup catalog starter | | | P01 to P17 | Section 07 must use the same ids |
| Reason vocabularies introduced by 04 | | env_error: `version_mismatch`, `version_changed_mid_run`, `rig_spec_mismatch`, `session_kicked`, `app_crash`, `fixture_drift`; not_comparable: `fingerprint_mismatch`, `window_mismatch`, `surface_mismatch`; needs_human: `reviewer_uncertain`, `no_manifest`, `uncalibrated`, `reviewer_unavailable`, `rescued_capture`; findings: `dependency_map_gap`, `unknown_popup`, `app_crash` | | Adopt as the reason vocabulary for the whole spec; add `unstable` (needs_human), `quarantine`, `entitlement`, `budget`, `scope`, `step_failed:<step>`, `base_step_failed` (not_run), `unexpected_window` (not_settled), `gate: preflight.<item>` and the orchestrator gate names (env_error). Appendix 12.2 gets a reason table. |
| Config files added by 04 | | `taxonomy.yaml` (single source for enums), `phase0/branches.yaml`, `policy/send_policy.yaml` | | Keep; sections 05, 08, 11 reference them |
| Fingerprint addition by 03 | `render_probe_hash` from Help > About | | | Keep; section 07 fingerprint table includes it |

Cross-references left as "N.x" by writers must be resolved to real subsection numbers by the integrator.

## Added after section 02 landed

| Topic | Divergence | Resolve to |
|---|---|---|
| Surface id style | 01 uses underscores (`charts.flexible_grid.active_trader_cell`); 02 and 04 use hyphens (`trade.active-trader.ladder`) | Hyphens inside segments, dots between segments. 01 must be changed. |
| Invariant catalog | 02 has INV-01 to INV-15; 12.3 has INV-01 to INV-11 | 02's 2.7 table is authoritative; 12.3 is replaced by a verbatim copy of it. 10 and 12.5 mutants must reference ids that exist in 02's table. |
| Mutant M07 red/green swap | 12 expects `collateral` or `needs_human`; 02's INV-15 yields `invariant_violation` | `invariant_violation` (INV-15), and the mutant's expected outcome in 12.5 and 10 is changed to match. |
| Size-sweep ladders | 01's example uses the 6x6 grid only; 02 defines Ladder F (6x6 grid plus corners plus corpus sizes, about 46 steps) and Ladder S (12 steps) | 02 is authoritative; 01's worked example must name Ladder S or F and use a size that exists in it (480x360 exists in both). |
| Checkpoints after a `failed` or `blocked_by_gateway` step | 02: `not_run (reason: scope, step id recorded)`; 03: `harness_error (reason: step_failed:<step>)` | `not_run (reason: step_failed:<step>)`. 02 and 03 both change to this exact form. |
| Registry entry not found on either build with entitlement present | 02: `harness_error` ("registry entry not found") | Keep |
| OCR calibration-panel miss | 02: Oracle assertions become `harness_error`, not `assertion_failed` | Keep; 03 and 05 must agree |
| App error dialog at capture | 02: `invariant_violation` INV-14 | Keep; 07 popup policy must route app error dialogs to INV-14, not to the dismiss list |
| Paper send modeled as an entitlement | 02: post-Send flows are `not_run (reason: entitlement)` until the `paper_send_ok` decision | Keep; 06 send policy must say the same |
| Checkpoint id formats | 02: `sweep/<surface>/<host>/<w>x<h>` and `split/<surface>/<splitter>/<fraction>` | Keep; 04 checkpoint id lint and 08 schema must accept these |
| Registry validation probes | 02: P1 to P8 | 12.7 preflight list must not reuse the P prefix for popups: rename popup catalog entries in 12.8 and 07 to `POP-01`.. |

## Added after sections 07 and 10 landed

| Topic | Divergence | Resolve to |
|---|---|---|
| App failure (JVM crash, hang, OOM, process exit) | 10: `assertion_failed` on a built-in `app_alive` assertion; 04: env_error reason `app_crash` and a finding `app_crash`; 02: app error dialog = `invariant_violation` INV-14 | Three distinct things. (a) Process gone or EDT frozen past max wait: `assertion_failed (assertion: app_alive)` on the checkpoint being captured and `not_run (reason: app_crash)` for the rest of the flow; a tier-1 finding `app_crash` with hs_err_pid and logs attached. It is a product finding, so 04's env_error reason `app_crash` is removed. (b) An app error or exception dialog on screen at capture: `invariant_violation` INV-14, per 02. (c) Watchdog restart budgets exhausted: run `aborted_budget`. Sections 02, 04, 05, 07, 10 align to this. |
| Instability after targeted re-run | 03: `needs_human (reason: unstable)`; 10: `not_settled (reason: unstable)` | `not_settled (reason: unstable)`. It is a capture-stability fact, not a review question. 03 changes. Routed to the flow owner, never a product finding, as both sections already say. |
| Checkpoints downstream of a failed step | 10: `harness_error (reason: step_failed)`; 03 same; 02: `not_run (scope)` | `not_run (reason: step_failed:<step>)` as already resolved above. 03 and 10 change. |
| Watchdog restart budgets | 10: 3 app relaunches, 2 orchestrator restarts per shard; exhaustion = `aborted_budget` (app) or `aborted_harness` (orchestrator) | Keep |
| Dead-man | 10: `aborted_harness (reason: dead_man)` | Keep; 09 alerting and 12.7 preflight use the same reason |
| Canary outcomes | 10: `detector_blind` (money-cell mutant missed), `detector_degraded` (rolling floor missed); run `harness_error` | Keep as canary result states (not checkpoint outcomes); 12.5 and 09 use the same two words |
| Quarantine ceiling | 10: 2 percent of tier-1 checkpoints quarantined blocks the RC gate | Keep; 09 metrics and 11 exit criteria reference it |
| Popup catalog | 07 has 15 starter entries; 12.8 has P01 to P17 | 07's list is authoritative for policy and the integrator merges the two lists into one table in 12.8 with ids `POP-01`.., referenced from 07 |
| Fingerprint | 07: 21 fields; 03 adds `render_probe_hash` | 07's table includes `render_probe_hash`; 04's fingerprint schema lists the same 21 plus 1 fields |
| Trade Flash | 07: disabled until seen | Keep; 12.8 marks it unverified |
| Login | 07: no Guest Pass; Branch A test-backend credential, Branch B trusted-device with scripted SMS recovery | Keep; 06 and 11 prerequisites say the same |

## Added after section 08 landed

| Topic | Section 08 says | Resolve to |
|---|---|---|
| Finding triage states | open, confirmed_bug, intended, noise, agent_mistake, expected_behavior, fixed_pending_verify, closed | Adopt spec-wide; 09 uses the same names in the UI actions and Jira sync |
| Decision verdicts | confirmed_bug, intended (ticket required), noise. noise/intended set the inherited outcome; confirmed_bug keeps the computed outcome and files under the ticket; invariant_violation and assertion_failed are never changed by a decision | Adopt; 03 and 09 align |
| Unreviewed diffs | stored as `needs_human` with `review_state: pending`; reviewer may set intended, collateral or noise | Adopt; 03, 09, 10 use `review_state: pending` |
| Repos | `sentinel-config` (registry, map, flows, regions, masks, corpus, catalogs, fixtures, eval labels, released baseline manifests) separate from `sentinel` (harness) | Adopt; 04 and 11 name the same two repos |
| Hamming tolerance | 8 of 64 bits default | Adopt; 03 says 8/6 (8 default, 6 for tier-1): keep 03's two values and 08 stores the tolerance per decision row |
| Secret leak found by post-run scan | run `aborted_harness`, report withheld | Adopt; 06 says the same |
| coverage_cells | rows only for merge, RC and nightly runs; PR runs keep the matrix in the run summary | Adopt; 09 PR comment prints the matrix from the summary |

## Added after section 06 landed

| Topic | Section 06 says | Resolve to |
|---|---|---|
| Dialog-side gateway rule | allowlist of dismiss labels (not a deny regex) because "Confirm and Send" must stay clickable to open the dialog | Adopt; 05 gateway design states the same |
| Attestation never repairs settings | abort; human repairs via a maintenance flow | Adopt; 12.6 checklist says the same |
| Gateway drop thresholds | 3 drops per flow quarantine the flow; 10 per run trip the kill switch | Adopt; 05 and 10 reference the same numbers |
| UNPROVEN post-run proof | run `complete_with_env_errors`, next run on that login blocked | Adopt; 09 alerting includes it |
| Explorer budgets | 400 actions, 90 minutes, 20 consecutive no-change actions | Adopt; 02.9 and 11 use the same numbers |
| Paper-mode allowlist | exact-match account ids, not a `D-` prefix check | Adopt; 04 fixture schema and 07 state the same |

## Added after section 01 landed

| Topic | Divergence | Resolve to |
|---|---|---|
| PR with no manifest entry | 01: finding `manifest_missing`, all above-threshold diffs `collateral`; 04: `intended` disabled, diffs `needs_human (reason: no_manifest)` | Two cases. (a) The PR touches UI code (static impact step finds UI dependents) and has no manifest: finding `manifest_missing`, and every above-threshold diff is `collateral`, because an undeclared UI change is by definition unexpected and the dev must declare it or fix it. (b) The PR touches no UI code and has no manifest: no finding, diffs are `needs_human (reason: no_manifest)` since a side effect from non-UI code is possible but intent cannot be checked. 01 and 04 both state both cases. |
| Failed step, target checkpoint | 01: `landmark_missing` (locator failure) or `assertion_failed` (assertion failure) for the step's own checkpoint; later checkpoints `not_run` with the step id | Adopt, with the reason spelled exactly `not_run (reason: step_failed:<step>)` for later checkpoints |
| Unreached checkpoints of aborted runs | 01: `aborted_safety` -> `env_error`; `aborted_harness` -> `harness_error` | Adopt; 10 and 08 align (08's CHECK constraint must allow these combinations) |
| Missing base artifact | 01: run `aborted_harness` | Adopt |
| Fingerprint mismatch | 01: whole shard `not_comparable` without capturing | Adopt; 03 and 07 align |
| PR commit status | 01: informational in Phase 1; blocks on tier-1 Unexpected findings only after the Phase 2 FP target holds | Adopt; 09 and 11 state the same |
| Baseline promotion | 01: at merge-run triage, never in PR runs | Adopt; 09 lifecycle states the same |
| Metric targets | 01: FP under 35 percent Phase 1 and 20 percent Phase 2, kill above 50 percent after three releases; flake under 5 then 2 percent; canary 100 percent money-cell; A/A floor zero on frozen | Adopt as the single set; 09 dashboard and 11 exit criteria use the same numbers |

## Added after section 05 landed

| Topic | Section 05 says | Resolve to |
|---|---|---|
| Additional reason codes | `needs_human (reason: oracle_unreadable | bounds_unreadable)`, `harness_error (reason: bounds_source_unavailable | ocr_calibration)`, `env_error (reason: occluded)`, `not_settled (reason: frames_unstable | anchor_missing | anti_anchor_present | timeout)` | Adopt; appendix reason table lists them |
| Attempted send blocked by the gateway | run `aborted_safety` | Adopt; 06 says the same (10 drops per run trip the kill switch is the Explorer/agent threshold; a single attempted send on a scripted flow is itself `aborted_safety`) |
| Three processes on the rig | Orchestrator, Gateway hook host (stdlib-only, seqlock shared-memory arm/deny state), Watchdog | Adopt; 10 and 07 name the same three |
| Trailing "Word count" lines | some section files end with a word-count line | Integrator strips every such line |

## Added after section 09 landed

| Topic | Divergence | Resolve to |
|---|---|---|
| Hamming tolerance default | 04: 6; 03, 08, 09: 8 for matching, 6 for clustering and tier 1 | 8 for matching (default), 6 for clustering and for tier-1 checkpoints. 04 changes its default to 8 and documents the tier-1 override. |
| Checkpoint id format | 01: `size_sweep/charts.flexible_grid.active_trader_cell/480x360` (underscores, `size_sweep` prefix); 02: `sweep/<surface>/<host>/<w>x<h>`; 04: dotted-slug regex that rejects slashes | Format: `<kind>/<surface-id>/<qualifier>` where kind is `census | sweep | split | flow`, surface id is dotted hyphenated slug, qualifier is `<host>/<w>x<h>` for sweep, `<splitter>/<fraction>` for split, `<flow-id>/<step-id>` for flow, `<workspace-id>` for census. 04's regex is widened to this grammar; 01's example becomes `sweep/charts.flexible-grid.active-trader-cell/grid/480x360`. |
| Quarantine threshold | 03: flake score above 0.2; 10: above 0.10 over 5 observations | Above 0.10 over the last 5 observations of the same build (10's rule). 03 changes. |
| Section 11 cross-reference | 11 refers to "9.6 Alerting model"; it is 9.10 | Integrator renumbers all cross-references after assembly |
| PR comment section labels | 09 uses bold labels, not headings, so the assembler TOC is not polluted | Keep |
| Decision kinds | 04 enum: confirm_bug, mark_intended, request_mask, noise_signature, agent_mistake, expected_behavior, approve_baseline, reject_baseline, fixed_in_build; 08 verdicts: confirmed_bug, intended, noise | 04's decision kinds are the UI actions; 08's verdicts are the stored effect. 09 shows the mapping table (action -> verdict -> inherited outcome) and 08 stores both columns. |
| Two-person rule | 09: PR author may not mark their own tier-1 or tier-2 diffs intended alone | Adopt; 06 authorization section references it |

## Added after section 11 landed (all drafts in)

| Topic | Divergence | Resolve to |
|---|---|---|
| Rig fleet | 01 timelines: 6 rigs for merge (3 candidate + 3 base), 8 for RC; 11: 6 rigs and 6 logins total, rigs 1 to 4 pool for PR and merge, rig 5 nightly/calibration/soak, rig 6 Explorer and spare, RC on all 6 | 11's fleet is authoritative. 01's merge timeline becomes 4 rigs (about 96 min wall worst case, inside the 3 h budget) and RC uses all 6 (about 6.5 h, inside 8 h). |
| Base and candidate scheduling | 01, 03, 07: two rigs in parallel; 11: same rig back to back by default | By data mode. Frozen checkpoints: same rig, back to back, with base-capture reuse when fingerprint and fixture epoch match. masked_live checkpoints: two identically fingerprinted rigs in lockstep (same minute), fingerprint equality asserted before capture; if only one rig is free, masked_live checkpoints are `not_run (reason: budget)` rather than captured minutes apart. 01, 03, 07, 11 all state this rule. |
| "Temperature 0" in the brief | 11 R14 and 03 both note sampling parameters are rejected by current models | Resolved earlier: brief wording means no sampling parameters, pinned effort, byte-stable prompt, majority vote at borderline. 03 is the owner; 11 R14 points to 03. |
| Cost brackets | 11: mid tier 2/10 and top tier 4/20 dollars per MTok, pinned model id is a DR-07 output | Keep |
| Operating day | 11: 06:00 to 22:00 ET | Keep; 07 quiet-window config and 09 alert hours reference it |

## FINAL DECISIONS for the integration pass (these are law; they override anything above and anything in a section)

| # | Question raised by reviewers | Decision |
|---|---|---|
| F1 | Nightly run when canaries miss | Run outcome stays `complete`. Canary result states are `detected`, `missed`, `detector_blind`, `detector_degraded`, `generation_failed`. `detector_blind` or `generation_failed` sets `system_state.detector_state`, pages P1, and puts the banner on every report until the next green canary. The Nightly run is `aborted_harness (reason: canary_generation_failed)` only when no canary could run at all. No section may say "the Nightly run is harness_error". |
| F2 | Missing calibration for a fingerprint | `needs_human (reason: uncalibrated)` at threshold 0, never `not_comparable`. `not_comparable` reasons are only `fingerprint_mismatch`, `window_mismatch`, `surface_mismatch`. Remove `fingerprint:calibration`, `calibration_missing`, `no_calibration`. |
| F3 | Fire drill failure | Two cases. A drill item not blocked but no new order row: `harness_error (reason: drill_failed:<item>)`, flows that use that path quarantined, P1 page. A new order row appears: run `aborted_safety (reason: drill_order_placed)`, login blocked. 01 and 06 both state exactly this. |
| F4 | Step failure vocabulary | Downstream checkpoints: `not_run (reason: step_failed:<step>)`. The failed step's own checkpoint: `landmark_missing` (locator) or `assertion_failed (assertion: <name>)` or `env_error (gate: <gate>)`. Base-side step failure: `not_run (reason: base_step_failed)`, or `needs_baseline (reason: new_surface)` when the manifest says `new_surface: true`. `step_failed` is never a `harness_error` reason anywhere. |
| F5 | Missing manifest | Run proceeds. UI-touching PR without a manifest: finding `manifest_missing`, above-threshold diffs `collateral`. Non-UI PR without a manifest: diffs `needs_human (reason: no_manifest)`. Never `harness_error`. |
| F6 | Census size and merge timing | 900 census checkpoints per workspace tier A pass (02/08/11). Merge run worst case 156 min on the 4-rig pool, inside the 3 h budget. 01's merge timeline uses these numbers. |
| F7 | Mutant catalog | One catalog at `sentinel-config/canaries/mutants.yaml`, ids M01..M33 (12.5 is authoritative), classes in snake_case as 10.3 names them, `money_cell` flag, seed 20260101, M25 is the identical control. 04, 08, 10 reference this one path and these ids. |
| F8 | Preflight | 12.7 is authoritative: 32 items in groups R, B, H, N, G. 10.8's fourteen self-tests are relabeled with the 12.7 ids they implement. |
| F9 | Tier-1 recall numbers | Two different things. Canary detection of money-cell mutants through the whole pipeline: 100 percent, else `detector_blind`. Reviewer eval-set recall gate on tier-1 items: at least 0.98 per model and prompt version. Both stand; 01's 1.5 and 03's 3.14 and 12.1 state the distinction. |
| F10 | Reviewer call caps | PR 60, merge 200, RC 500. 11's planning assumption changes. |
| F11 | Writers and stores | Run Store per shard on the rig, written only by the Orchestrator. Central Store on the control host, written only by the Reporter service, which also owns the API the report UI and one-click actions call. Watchdog never writes SQLite; it writes a JSONL journal the Orchestrator ingests. 09's "Orchestrator's local API" becomes the Reporter service API. |
| F12 | Explorer session | 400 actions, 90 minutes, 20 consecutive no-change actions, 25 dollars. RC timeline allots 90 minutes for it. 01 and 11 use 90. |
| F13 | Soak | 10.11 Soak exists; a freeze or crash during soak is the app failure trio, never `env_error (gate: soak)`. |
| F14 | Calibration scheduling | Per checkpoint, 5 captures plus 1 on a second night; scheduled over a weekend `all_quiet` window across the pool; the brief's "5 times on one rig" means per checkpoint on the same rig, not the whole library serially. 01's Calibration row in 1.6 says "one weekend window", not "one night". |
| F15 | Assumption id collisions | Attestation keeps A1..A11. 01's assumptions table becomes AS-01..AS-12. 11.1 planning assumptions become PA-01..PA-14. 8.8 storage assumptions become SA-01..SA-07. Every reference to them changes accordingly. |
| F16 | Canary path | Mutants are injected through the same entry point and harness commit as real captures; a divergence detected by the nightly re-evaluation of a real pair is `harness_error (reason: canary_path_divergence)`. |
| F17 | Gateway integrity vs safety drops | Safety-reason drops (blocked key or click on a deny rect while `order_stage` is set): flow quarantined, P1. Integrity-reason drops (token mismatch, expired token, stale state): one retry, 3 per flow quarantine the flow, 10 per run trip the kill switch. An attempted send on a scripted flow or agent rescue is `aborted_safety`; in an Explorer session it ends the session and counts toward the per-run trip. |
| F18 | Agent rescue | Cap 25 actions and 90 seconds per rescue; rescued checkpoints are `needs_human (reason: rescued_capture)` and never inherit noise or intended decisions. |
| F19 | Unknown dialog label | If `order_stage` is set: `aborted_safety (reason: unknown_dialog_in_order_stage)`. Otherwise the flow is quarantined and the window is an `unknown_popup` finding. |
| F20 | Hook host restart | Its own budget: one restart per shard; the second is `aborted_harness (reason: hook_lost)`. It does not consume an app relaunch. |
| F21 | Explorer drags | `drag_named` allowed only on splitters and detach handles through the Gateway; order-bubble drags on charts and the ladder are denied as order paths. |
| F22 | Agent-mode tooling | Agent mode uses the harness's own tool definitions (click_named, type_fixture, press, zoom), not the computer-use beta tool, so there is no computer-use tool type to pin; the pinned items are the model id and the harness toolset schema version. 10.7 changes. |
| F23 | 01's worked example sweep | PR runs sweep tier-1 surfaces only with Ladder S (12 steps), per 02.5.3; the example shows 4 of 12 steps `invariant_violation` and 8 of 12 `collateral` for the ladder cell, and the other grid cells as census-only. 01, 09 and 10 agree on these numbers. |
| F24 | Redaction and mask budget | Redaction boxes count toward the mask budget (03's rule). 04 states the same. |
| F25 | Decision lookup | Returns the full verdict set for a signature; conflicting verdicts inherit nothing and are listed as conflicting (09's rule). 08's query drops LIMIT 1. |
| F26 | Jira label character set, paging service name, Order Confirmation cancel-button label, IRA sell-to-open rejection for the drill ticket | Phase 0 items with the named default branch; listed in 11.8 and 6.13. |

Integrators: the review-NN.md part (b) items were written while other reviewers were editing their files concurrently, so some claims are stale. Verify each claim against the CURRENT file before editing.

## ROUND 2 DECISIONS (law; settle trace-findings.md and final-12.md disagreements)

| # | Topic | Decision |
|---|---|---|
| F27 | Precedence | `env_error` > app failure (`assertion_failed (assertion: app_alive)`) > `harness_error`. 1.10 changes to match 10.1. |
| F28 | Late Reviewer verdict | When a pending verdict lands, the outcome becomes what 3.11's table gives (`intended`, `collateral`, `noise` only via A/A, or `needs_human (reason: reviewer_uncertain)`) and `review_state` goes `pending` -> `done`. If a human decision already exists on that item, the verdict is attached as an annotation only and never changes the outcome or the decision. 1.10, 3.11, 10.7 say exactly this. |
| F29 | Model preflight N5 fails | Degraded mode, not `env_error`: the run continues, review is deferred (`needs_human` with `review_state: pending`), agent fallback and Explorer are disabled. 7.11 changes. |
| F30 | Spend | The 50-dollar per-run ceiling (11.4) ends the run `aborted_budget (reason: dollar_cap)` at the next flow boundary. Reviewer call caps (PR 60, merge 200, RC 500) never end a run; they produce `needs_human (reason: reviewer_cap)`. 3.15 states both. 1.10 drops "reviewer dollar cap" as an abort trigger and names the per-run ceiling instead. |
| F31 | Missed money-cell mutant | The canary row is `harness_error (reason: detector_blind)` (1.10, 4.13, 10.3 agree). A missed non-money mutant is `missed` and counts toward `detector_degraded`. |
| F32 | Agent-rescued capture | Always `needs_human (reason: rescued_capture)`, even when the diff is clean. 3.11 changes. |
| F33 | Identity fields vs money cells | Money cells are Qty, Price, Buying Power Effect, Max Loss, Send, Cancel, Delete. Account number and account holder name are identity fields: always redacted, excluded from the mask budget, never diffed, and proven nightly by the redaction canary. 3.8, 4.8, 6.10 and the mask budget table state this. |
| F34 | Mask budget breach at run time | Any panel whose masked area (masks plus non-identity redaction) exceeds its budget yields `harness_error (reason: mask_budget_exceeded)` for that checkpoint. One spelling everywhere: `mask_budget_exceeded`. |
| F35 | Disk | Watchdog's free-space check fails: `aborted_harness (reason: disk_full)`. A single artifact write fails (ENOSPC or other): checkpoint `harness_error (reason: artifact_persist_failed)` and the run cannot be `complete`. 8.9 and 10.2 state both. |
| F36 | Freeze or crash during a sweep or flow | The app trio: the checkpoint being captured is `assertion_failed (assertion: app_alive)`, the rest of that flow or ladder is `not_run (reason: app_crash)`, finding `app_crash`. After relaunch the next flow starts from its preconditions; a ladder never resumes mid-way. A freeze or crash during relaunch gates is another app_alive failure that consumes the relaunch budget, never `env_error`. 10.2 changes. |
| F37 | Explorer and order paths | Deny list adds the chart and grid context-menu Buy/Sell items, BUY MKT/SELL MKT and every ladder or Level II bid/ask cell, and every order-entry button. Three denied attempts end the session (2.9, 6.8; 5.6 changes). If the ticket or dialog flag is ever set in an Explorer session: all input is dropped, the window is closed by the gateway's recovery path (Escape token), the session ends with finding `explorer_order_stage` (S1 to the safety channel and the Explorer owner), it counts toward the per-run trip of 10, and the run itself continues. The 30-second open-dialog kill-switch timer applies to scripted flows and agent rescue only. |
| F38 | Inherited `intended` | Inheritance respects the ticket scope in 9.7 (same manifest entry or same ticket); 3.11 applies it. |
| F39 | Paging | `aborted_harness` and `aborted_safety` page harness on-call immediately (1.5, 1.10); 11.6 changes. |
| F40 | RC respin | A new RC build supersedes the running RC run: the running run stops at the next flow boundary with run outcome `aborted_budget (reason: superseded)`, its unreached checkpoints `not_run (reason: budget)`, and the new RC run starts at once with the reservation transferred. RC runs pre-empt the Nightly run; a skipped Nightly is alerted and its canaries run at the next free window before any further PR comment is trusted (banner). RC start deadline is the release gate N in 11 minus 8 hours; past it the release manager decides (9.12). |
| F41 | Money cells and rule regions | A money cell is always pixel-diffed AND rule-asserted; it can never be rule-only. A within-bracket change to Buying Power Effect on an undeclared surface is therefore `collateral`. 4.8's example changes. |
| F42 | Mask request over budget | The UI refuses to submit a mask request that would exceed the budget and says why; the finding stays open in Unexpected until decided by `confirm_bug` or `mark_intended`. Raising a budget is a system-owner PR to `sentinel-config`. |
| F43 | Post-run proof without a usable session | Login never authenticated (vault down, auth failed) and no gateway token was ever issued: proof is `PROVEN: no session established` from the gateway log. Session kicked: wait `session_release_wait`, fresh login, normal proof; if the login is still unavailable after one retry, UNPROVEN with reason `session_unavailable`, login blocked, human verifies. |
| F44 | Launcher restart | On `process_exit` the Watchdog checks for a running launcher update and a changed build manifest before classifying. If either is present the event is `env_error (reason: version_changed_mid_run)`, no `app_crash` finding, no relaunch consumed. |
| F45 | Model unavailable | Add `not_run (reason: model_unavailable)` for Explorer sessions and agent-only steps skipped because the model endpoint is down. |
| F46 | Phase 0 branch B for JAB and for data | The invariant enablement test uses masked_live A/A pairs with masks applied; structural invariants do not depend on data values, so Branch-B invariants are enabled by that test. |
| F47 | Classification table completeness | 3.11 gets rows for `oracle_unreadable` and `bounds_unreadable`; when two `needs_human` reasons apply, the first in 3.11's fixed order wins and the rest go in `detail`; a removed surface the Reviewer does not confirm as declared is `collateral`. |
| F48 | Owners for `not_run` | `not_run (reason: entitlement)` goes to the system owner's weekly coverage review; `not_run (reason: budget)` and `(reason: model_unavailable)` go to run health and the harness on-call; 9.12 defines the weekly coverage review. |
| F49 | Targeted re-runs | Invariant violations are re-captured once to confirm persistence, like diffs (3.10 changes); the worked example's counts follow from that and must agree across 1.11, 9.3, 10.10. |
| F50 | Quarantine triggers | Flake above 0.10 over 5 observations, OR 3 A/A drift nights in a row, OR the gateway thresholds; 3.10, 10.5, 11.5 list all three. |
| F51 | Calibration placement | Across the pool in one weekend window (1.6); any "rig 5" claim for calibration changes. |
| F52 | Ticket surface ids | One family: `trade.order-entry.<asset>` for the order entry ticket (04, 05). `ticket.order-entry.stock` in 9.3, 12.4, 12.5 changes. Position Statement is `monitor.activity.position-statement` (12.4, 12.9); 4.4's example changes. |
| F53 | Unused reason codes | `input_blocked` and `live_identity` are removed from 4.13 and 8.3 unless a section produces them. `lockstep_slip` is a run-health counter, not a finding; removed from 08's findings CHECK. `jab_tree_changed` is added to 08's CHECK. Finding kinds with no producer are removed from 08. |
| F54 | A/A invariant firing | `harness_error (reason: invariant_on_aa)`; added to 4.13 and 8.3. |
| F55 | Rejected baseline | The candidate baseline row becomes `retired` (08, 9.6); 4.14 changes. |
| F56 | Flow step for ticket-ready checkpoint | Mutants M01 and M20 bind to step `s06` of `flow.trade.order-ticket.stock.limit`; 4.7's worked flow defines steps s01..s07 so that s06 is the ticket-ready checkpoint and s07 the Order Confirmation dialog. |

## ROUND 3 DECISIONS (law; settle trace2-findings.md and the appendix agent's notes)

| # | Topic | Decision |
|---|---|---|
| F57 | Reviewer clustering (N-01) | Invariant violations never go to the Reviewer. Sweep and split checkpoints of one surface and one region form ONE Reviewer cluster per run (one call, sending the crops of the smallest and the largest affected step). Census and flow checkpoints cluster by 9.7's rule (same surface and region, component boxes within 4 px). 5.11 and 9.7 state both. The worked example recomputes its call count from this rule (expected: 4 calls: sweep collateral cluster, Active Trader census checkpoint, tier-2 cluster, intended chart cell) and its cost from 11.4's per-review price; 1.11, 9.3, 10.10 agree. |
| F58 | Money cells are a closed list (N-02) | F33's list is closed. Ladder price column, P/L, balances and BUY/SELL labels are tier-1 regions, not money cells; their mutants are non-money (count toward `detector_degraded`). 10.3 changes. |
| F59 | Runs that never start (N-03) | `dead_man` applies only to a run that reached `running` (scope manifest uploaded) and then stopped reporting. A queued run past its start deadline: PR `aborted_budget (reason: pool_saturated)`, merge coalesced into the next merge run, RC decided by the release manager (9.12). 10.2 and 9.10's dead-man row change. |
| F60 | Inherited confirm_bug (N-04) | Keeps the computed outcome and files under the ticket. 1.10 changes. |
| F61 | Removed surfaces (N-05) | Undeclared removed surface: `collateral`. Declared removed surface the Reviewer does not confirm: `needs_human (reason: reviewer_no_match)`. 3.11 row 26 changes; F47's last clause is superseded. |
| F62 | Crash at first launch (N-06) | The app trio applies at the first launch too: an app_alive failure before `launched` passes consumes the relaunch budget; if the budget is exhausted the run is `aborted_budget (reason: relaunch_budget)` and unreached checkpoints `not_run (reason: budget)`; finding `app_crash`. 10.2 rule 3 and 5.2 state it. |
| F63 | Lockstep slip (N-07) | The pair is re-queued once; then `not_run (reason: budget)`; `lockstep_slip` is a run-health counter, never a finding. 1.6 changes. |
| F64 | Unproduced 4.13 codes | For each code no rule produces under that name: either name its producer in the owning section (if a real situation needs it) or remove it from 4.13, 8.3 and 12.2. |
| F65 | Every `env_error` has a gate | Every `env_error` anywhere names its gate (`gate: <state>` or `gate: preflight.<item>`), including `version_changed_mid_run` (1.2 AS-02, 7.4), `occluded`, `fixture_drift`, `agreement_pending`, `delay_state` and Explorer teardown. |
| F66 | Missed by static impact | The report label maps to finding kind `missed_in_pr_run`; 1.10 and 9.4 use that kind. |
| F67 | Safety channel and paging | Define Slack channel `#sentinel-safety` in 9.9. One authoritative routing in 9.10: `aborted_safety`, S1 incidents, `explorer_order_stage`, UNPROVEN proof and fire-drill failures page harness on-call at P1 and notify the accountable owner and `#sentinel-safety`. 1.10, 6.6, 6.12, 11.6 reference 9.10 instead of restating lists. |
| F68 | Drill flows | Drill and safety-canary flows are exempt from quarantine; their failure pages per F67 instead. 6.5 item 5 changes. |
| F69 | Quarantine triggers | Add 11.5's 5 percent fallback trigger to 3.10 and 10.5 (alongside F50's three). |
| F70 | `app_alive` cause | The cause (`jvm_crash`, `process_exit`, `edt_frozen`, ...) goes in `detail`; `reason` is `app_alive`'s assertion record. 4.13's form is authoritative everywhere. |
| F71 | `reviewer_cap` rewrite | 4.13 lists pending -> `needs_human (reason: reviewer_cap)` at the per-run ceiling as an allowed later change; 8.3's trigger permits it. |
| F72 | 10.8 N items | Each N item states its own outcome as 12.7 does (N2, N5 degraded; N3 `env_error (gate: preflight.N3)`; N7 proceeds). 7.11's N2 row: the Reviewer is deferred, not disabled. |
| F73 | M12 binding | 10.3 and 10.10 are authoritative (`sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360`, regions `rgn.ladder.order-strip`, `rgn.ladder.viewport`, expects `invariant_violation` and `collateral`); 4.11 changes. |
| F74 | Calibration weekend | Canaries and fire drill stay on rig 5 during the calibration weekend; calibration uses the pool (rigs 1 to 4). 1.6 and 11.2 agree. M19 is non-money (redaction canary), accepted. |
| F75 | Partial T-11 and T-29 | Fix the two `version_changed_mid_run` lines (F65) and 8.4/4.13 disk text to F35 (ENOSPC on a write is checkpoint `harness_error (reason: artifact_persist_failed)`; only the Watchdog free-space check ends the run `aborted_harness (reason: disk_full)`). |
| F76 | Other minor | 4.8 example timestamp mask satisfies L-RGN-07; 9.3's Slack count fits 9.9's rules. |
