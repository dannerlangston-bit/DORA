## 9. Reporting, triage and integrations

The Reporter turns the rows written by the Orchestrator, Diff Engine, Invariant Checker, Oracle and Reviewer (`runs`, `checkpoints`, `outcomes`, `findings`, `decisions`, `baselines`, `masks`, `step_events`, `flake_stats`, `canary_results`, `egress_log`; see 8.3 Schema) into the PR comment and commit status, the merge and RC reports, the report UI, Jira tickets, Slack posts, pages and the metrics dashboard. It never computes an outcome, never changes one, and never decides anything a human is supposed to decide; every human action it offers is written as a decision record that the next run inherits.

### 9.1 Rules for every report

- Every checkpoint shows its outcome name from the closed taxonomy (1.10 Outcome taxonomy (authoritative)) and nothing else. `not_run` always carries its reason and is never rendered as passed or green. When several results apply, the outcome follows the precedence order in 1.10 (`invariant_violation` and `assertion_failed` above `collateral`); the lower results stay on the finding as evidence and are shown under it.
- Synthetic items (canary mutants, `synthetic = 1`) appear only on the canary panel, never in finding lists, counts or metrics. Crops are shown at 1:1 (image width equals crop pixel width, `image-rendering: pixelated`); the checkpoint page adds flicker at 2 Hz and a heatmap overlay.
- Screen text and model text are data. Reviewer descriptions, OCR strings, popup titles, manifest summaries and ticket titles are HTML-escaped and rendered as plain text or code spans, never as markdown, links or images. The only links in a comment or post are report UI URLs the Reporter composes itself. A manifest summary containing markdown image syntax therefore prints literally; nothing on screen or in a model response can inject a link, an image or a heading into a PR comment (5.13 Reporter and Integrations).
- One PR comment per PR, identified by a hidden marker (`<!-- sentinel:pr:{pr} -->`), created by the first run and edited in place by later runs, older runs in a collapsed history table. If the marker comment is missing (deleted, or lost by the PR host) the Reporter creates a new one and says so in Run health. Images are links to the report UI's `/a/<sha256>` artifact URLs behind SSO (8.7 Report UI data access); no image is uploaded to the PR host, Slack or Jira, so only text leaves, logged in `egress_log` with purpose `pr_comment`, `jira` or `slack`. Section labels in the comment are bold lines, not markdown headings, so they render at comment scale and do not pollute the assembled table of contents.
- Size. GitHub caps a comment at 65,536 characters. The Reporter renders, measures and shrinks in this order until it fits: inline Unexpected and Intended items 10, then 5, then 3; the Suppressed table collapses to a count and a link; the run history collapses to a link. The header, result line, check line, abort block, coverage table and Safety row are never dropped. Assumption to verify in Phase 0: the PR host is GitHub Enterprise; if Bitbucket, confirm that tables and `<details>` render, else the Reporter emits a flat-list variant with the same fields, and the commit status states map to Bitbucket build statuses (`error` and `failure` both to FAILED, the description carrying the distinction).
- A one-click action in the comment is a link into the report UI with finding and action pre-selected; after SSO one click completes it, plus a ticket key or reason where required. In the UI the same actions are buttons. Both post to the Reporter service API (8.7 Report UI data access; the Reporter service is the Central Store's only writer, 5.13, 8.2) with the SSO identity and write the same decision record (9.6, 9.7); if the Reporter service is down the UI shows a read-only banner and refuses the action rather than queueing it, so nobody believes a decision was made that was not. The Orchestrator writes only the Run Store of its shard on the rig and the Watchdog writes only its JSONL journal; neither serves this API.
- Suppressed findings are always listed, collapsed, under "suppressed by decision #id", so humans can audit what is not shown. A suppressed finding keeps its full evidence on the checkpoint page.
- Delivery never fails silently. Every outbound post (PR comment, commit status, Jira create or comment, Slack post, page) is an outbox job written through the single writer before the send is attempted (the `outbox` table of 8.3 Schema: job id, run id, kind, destination, payload hash, attempts, next attempt, last error, state `queued`, `sent`, `failed` or `abandoned`). The Reporter retries a failing job at 1, 2, 5, 15 and 60 minutes, then hourly until 24 hours; each attempt is an `egress_log` row with status `sent` or `failed`. A job still failing after 15 minutes raises the integration outage alert (9.10) on every other channel; after 24 hours it is `abandoned`, listed in the daily digest and on the run page until a human closes it. The report UI page is rendered first and locally, so a report exists even when every integration is down. A PR comment not posted by the PR run's budget deadline is posted late with a `report_late` note in Run health (5.13).

### 9.2 PR comment template

The comment is rendered from the run's summary JSON at teardown (8.7) and posted when the run reaches `reported`; the Reporter host posts it on a dead-man timeout when the run never reports. Braces are fields. Unexpected and Intended list at most 10 items inline, the rest counted and linked.

What the comment shows for each run state ("all" means the Unexpected, Intended, Suppressed, Coverage and Run health blocks):

| Run state | Result line | Blocks | Notes |
|---|---|---|---|
| `complete`, `review_state = reviewed` | `complete` | all | Normal. |
| `complete`, `review_state = unreviewed` or `partial` | `complete (unreviewed: {n} items pending review)` | all; each pending item is listed in Unexpected as `needs_human (review_state: pending)` with "Reviewer: pending since {time}, batch {id}" in place of a description, and all three actions available | Edited in place as verdicts land (10.7 Degraded modes). A pending tier-1 item counts as undecided for the check. |
| `complete_with_env_errors` | `complete_with_env_errors` | all, plus an Environment errors line in Run health (gate names, shards, screenshot links) and the not-compared count per tier in Coverage; "shard re-run queued" when the budget allows | When the cause is an UNPROVEN post-run proof (6.6 Kill switch, recovery and post-run proof), the Safety row prints the proof verdict and reason and "next run on login {alias} blocked until a human verifies the account". A run whose login never authenticated (vault down, authentication failed) and was never issued a gateway token prints `PROVEN: no session established`, read from the gateway log, and blocks nothing. After `session_kicked` the proof waits `session_release_wait` and logs in fresh; if the login is still unavailable after one retry the verdict is `UNPROVEN: session_unavailable`, the login is blocked and the human verification line is printed (6.6). |
| `aborted_budget` | `aborted_budget (reason: {reason})` | all, with what ran; line "Scope not finished: {n} checkpoints not_run (budget), {n} of them tier 1". For `aborted_budget (reason: dollar_cap)` the line adds "per-run model spend ceiling reached ({cost} of {cap}, 11.4 Cost model)". For `aborted_budget (reason: pool_saturated)` (11.2 Cadence and time budgets per run type) nothing ran: the abort block alone, with the text "Sentinel did not run: pool saturated, {n} runs ahead; push again or re-run the build job" | Findings are triaged as usual (1.10). Explorer budget exhaustion never aborts a run; it ends the Explorer session only (2.9 Explorer). Check `error` with "Sentinel did not run" for `pool_saturated`. |
| `aborted_safety` | `aborted_safety (reason: {reason})` | Intended and Unexpected replaced by the abort block: trigger (attestation, paper mode, kill switch, gateway block, undeclared Working or Filled row, `unexpected_working_order`), gate, last screenshot link, post-run proof verdict, incident id, "no run starts on rig {rig} until the incident closes" | Findings recorded before the abort stay on the run page, collapsed. When the verdict is ORDERS SENT, the most severe (6.6), the abort block and the Safety row add "an order reached the account: login {alias} blocked and rig disabled until a human verifies the account and the incident closes". Check `error`. |
| `aborted_harness` | `aborted_harness (reason: {reason})` | abort block: reason (`base_build_missing`, `config_invalid` with rule ids, `preflight.<item>` for a failed group H item of 12.7, `store_write_failed`, `orchestrator_restart_budget`, `hook_lost`, `watchdog_dead`, `dead_man`, the rest of 1.10's list), last gate reached, screenshot if any, "re-queued once: {run id, or 'already re-queued'}", or "not re-queued: {why}" when a re-run cannot help (`config_invalid`: the same config fails again) | For `dead_man` the Reporter host writes the comment: "Sentinel did not report: no run-completed event by {deadline}; last heartbeat {time}". For a secret leak (6.9 Credentials and secret steps, 8.9 Store failure modes) the comment says only "report withheld; security contact notified". Check `error`. |

Banners. When `detector_state` in `system_state` is `detector_blind`, `detector_degraded` or `generation_failed` (set from the last Nightly run's canary result states, 10.3 Canary Service and the mutant catalog; the Nightly run itself stays `complete`), or a `harness_error (reason: canary_path_divergence)` was recorded, the banner from 10.3 is the first line of the comment and stays until a Nightly run passes green. When the Reviewer is disabled by a failed eval gate (10.4 Eval set and Reviewer change control), the line "Reviewer disabled since {date}: eval gate {name} failed; {n} items await a human" follows it. When the run-level `global_rendering_shift` finding fired (3.7 Three-granularity diff: algorithms and parameters), the header line "Global rendering shift: {n} checkpoints collapsed into one `needs_human (reason: global_shift)` cluster; no reviewer calls made" follows. When no manifest is loaded, the Intended block opens with one of the two lines of 4.3 Change manifest: "No manifest entry and the PR touches UI code (`manifest_missing`): every diff is reported as collateral until you add one", or "No manifest and no UI code touched: diffs are `needs_human (reason: no_manifest)`".

Unexpected ranking: tier ascending; then kind order: `assertion_failed (assertion: app_alive)` with its `app_crash` finding, `invariant_violation`, `assertion_failed` (invariant and assertion findings of checkpoints whose comparison ended `not_run (reason: base_step_failed)` are listed here too, because the candidate capture was still checked, 3.1 Build pair rules), `collateral` together with `needs_human (reason: uncalibrated)` (an above-zero diff on an undeclared surface with no calibrated floor, ranked with collateral within the tier, 3.11 Classification decision table), `needs_human (reason: reviewer_no_match)` (a declared surface whose change the Reviewer confidently said does not match the entry, directly after collateral within the tier, 3.11), `landmark_missing` (candidate side only), `unregistered_surface`, other `needs_human` (pending review included; `rescued_capture` items never inherit a decision), `needs_baseline` on an undeclared surface, step `failed` (candidate side only), `passed_via_agent_fallback` (scripted locator broke, `locator_failure` finding), `dependency_map_gap`, `manifest_missing`, `unknown_popup`; then reviewer confidence descending; then novelty (signature unseen in 30 days first). Clusters (9.7) are one item with a member count.

```markdown
{detector_banner | omitted}
**Sentinel PR run {run_id}: {candidate_build} vs {base_build}**
Result: **{run_outcome}** | {n_unexpected} unexpected | {n_intended} intended | {n_suppressed} suppressed by decision | [Full report]({report_url})
Check `sentinel/pr-run`: {check_state} ({check_mode}). {check_reason}
{review_line: "Reviewer pending: {n} items; this comment is edited in place when verdicts land" | omitted}
{abort_block | omitted}

**Unexpected ({n_unexpected})**
{repeat per finding or cluster}
**{rank}. [Tier {tier}] {outcome} on `{surface_id}`, checkpoint `{checkpoint_id}`{, cluster of {n} checkpoints}{, {k} of {n} members suppressed by decision {decision_id}}**
- What moved: {invariant_or_assertion_text | reviewer_description | "Reviewer: pending since {time}"}
- Why unexpected: {one of: `{surface_id}` is not declared in the manifest. It consumes `{primitive}`, touched by manifest entry {manifest_entry_id} ("{manifest_entry_summary}", PR {pr}). | Declared surface; the Reviewer did not confirm the manifest entry ({reviewer_value}, confidence {c}). | Invariant {inv_id}: {statement}. | Assertion {assertion_id}: expected {e}, read {v} by {source}.}
- Where: workspace `{workspace_id}`, {layout_and_size | Ladder {S|F} step {w}x{h}}, region `{region_id}`, window {target_window}, data mode {data_mode}
- Evidence at 1:1: ![base]({base_crop_url}) ![candidate]({candidate_crop_url}) [heatmap, flicker]({checkpoint_url})
- Reviewer: {description ({model_id}, prompt {prompt_hash}) | "not reviewed: invariant and assertion outcomes are never reclassified" | "pending"}
- Re-run: {"stable: identical across {n} re-captures (product change)" | "not re-captured: {reason}"}
- Related decisions: {"none" | "decision {id} at Hamming {d}, not applied" | "conflicting decisions {id_a} ({verdict}) and {id_b} ({verdict}): nothing inherited, triage rotation notified"}
- Actions: [Confirm bug]({confirm_url}) | [Mark intended with ticket]({intended_url}) | [Request mask with reason]({mask_url})
{end repeat}
{if n_more > 0} ... and {n_more} more in the [full report]({report_url}#unexpected)

**Intended ({n_intended})**
{manifest_state_line | omitted}
{repeat per declared surface with a diff}
**`{surface_id}`, {n} checkpoints match {manifest_entry_id}**
- Declared: "{manifest_entry_summary}" ({manifest_entry_id}, ticket {ticket}, PR {pr})
- Evidence at 1:1: ![base]({base_crop_url}) ![candidate]({candidate_crop_url})
- Reviewer confirmation: {intended {confidence}: "{reviewer_description}" | pending}
{end repeat}
Manifest reconciliation: {repeat per entry} {manifest_entry_id}: {"verified visible on {n} checkpoints" | "covering checkpoints {ids} showed no change above floor (intended_change_not_observed; a question for {author} and the feature owner, not a bug)" | "no covering checkpoint; flow-authoring task {task_key} opened"} {end repeat}
{needs_baseline_line: "{n} checkpoints `needs_baseline` on declared new surfaces; the merge run produces the candidates, nothing is approved from a PR run" | omitted}

**Suppressed by decision ({n_suppressed})**
<details><summary>{n_suppressed} findings matched an earlier decision</summary>

| Checkpoint | Region | Decision | Action (verdict) | By | Ticket |
|---|---|---|---|---|---|
| `{checkpoint_id}` | `{region_id}` | {decision_id} | {action} ({verdict}) | {actor}, {date} | {ticket} |
</details>

**Coverage**
| Tier | Registered | In scope | Covered | Not compared | not_run |
|---|---|---|---|---|---|
| {tier} | {n} | {n} | {n} ({pct} of comparable scope) | {n} ({outcome}: {n}, ...) | {n} scope, {n} budget, {n} entitlement, {n} quarantine, {n} step_failed, {n} base_step_failed, {n} app_crash, {n} model_unavailable |

Scope: {n_declared} declared surfaces, expanded through {n_edges} dependency-map edges and the static impact step, plus the smoke census ({n} checkpoints). {n} `unregistered_surface`, {n} `dependency_map_gap` ({pr_links}). Send policy {policy}: {n} order lifecycle surfaces `not_run` (entitlement). [Matrix]({matrix_url})

**Run health**
| Item | Value |
|---|---|
| Wall clock / budget | {duration} / 45:00 on {n_shards} shards{; report_late} |
| Fingerprint | candidate {fp_hash} = base {fp_hash} |
| env_error / harness_error | {n} ({gate_names}) / {n} ({reasons}) |
| not_settled / not_comparable / landmark_missing | {n} / {n} / {n} |
| Steps: passed_after_retry / passed_via_agent_fallback / failed / blocked_by_gateway | {n} / {n} / {n} / {n} |
| Layout shift | {n} checkpoints with a whole-window translation ({dx}, {dy}) |
| Reviewer | {n} calls, {n} pending, {n} unavailable, {cost} |
| Targeted re-runs | {n} ({n} stable, {n} unstable); {n} `rerun_budget_exhausted` (stability left `unknown (cause: budget)`, 3.10 Targeted re-run and flake logic) |
| Canary (last Nightly) | money cells {pct}, other {pct}, detector state {ok | detector_blind | detector_degraded: {class} | generation_failed}, {date} |
| Safety | attestation {result}, paper mode asserted {n} times, gateway {n} drops, Cancel All {verified | failed | not needed}, proof {PROVEN: no orders sent | PROVEN: only declared sends | PROVEN: no session established | UNPROVEN: {reason} | ORDERS SENT: undeclared rows ({rows}); Cancel All {verified | failed | not needed}} |
| Integrations | PR comment {posted {time} | late | unposted since {time}}, Jira {n} queued, Slack {n} queued |

<details><summary>Run history</summary>{run, candidate, result, counts, reported, per earlier run}</details>
```

The commit status. The context `sentinel/pr-run` is posted on the candidate commit with the run page as target URL. It has two modes, set in configuration and recorded with the date of every change:

- Informational (Phase 1, and the default): the state is computed by the rules below, but the context is not in the branch protection's required list, so it never blocks a merge; the description ends "(informational)". This is the mode 1.6 Run types gives for Phase 1.
- Blocking: the system owner adds the context to branch protection on main once the Phase 2 false-positive target holds, which means a rolling 30-day false-positive rate under 20 percent for two consecutive release cycles (1.5 Success criteria and kill criteria, pilot exit). The switch is announced in the release channel. The system owner reverts to informational, with the same announcement, when the rolling 30-day false-positive rate is above 35 percent for 10 business days or the kill criterion fires.

States, identical in both modes:

| State | When | Description text |
|---|---|---|
| `pending` | Run queued or running; set from the run row at T+0:00 | "Sentinel run started, base = {base_build}" |
| `failure` | Any tier-1 Unexpected item without a decision: `collateral`, `invariant_violation`, `assertion_failed` (including `app_alive`), `needs_human` (review pending included), `landmark_missing` on the candidate side only, step `failed` on the candidate side only | "{n} tier-1 findings have no decision" |
| `error` | `aborted_safety`; `aborted_harness` (including `dead_man`); `aborted_budget (reason: pool_saturated)`; report withheld; no comment posted by budget plus 15 minutes | "aborted_safety: incident {id}", "Sentinel did not run" (pool saturated), or "Sentinel did not report" |
| `success` | Otherwise | "{n} unexpected ({n} tier 2 and 3 open); {n} tier-1 surfaces not compared ({outcomes}): see the merge run" |

Tier 2 and 3 items never fail the check. `unregistered_surface`, `dependency_map_gap`, `unknown_popup`, `manifest_missing` and harness-side findings never fail it, since they belong to the registry owner or the system owner, not to the PR. Tier-1 surfaces that were not compared (`env_error`, `not_settled`, `not_comparable`, `landmark_missing` on both sides, `not_run` for budget) do not fail it either: they are named in the description and the merge run compares them within hours; that merge run blocks the RC gate when tier-1 coverage is below 100 percent (1.5). The status is re-posted within one minute of any decision. Only the system owner may override it: forced `success` for one run, reason required, recorded in the run row and printed in Run health and the comment.

### 9.3 Filled sample: Flexible Grid change breaks the Active Trader ladder

This is the worked example of 1.11, as the developer sees it at T+0:41 (11:12 ET). PR 4812 shrinks the Flexible Grid cell header from 28 to 22 px and gives hosted content the full cell height; its manifest entry `tos-48120-1` (ticket TOS-48120) declares only the chart cell. It is a PR run, so the one tier-1 surface among the four Flexible Grid surfaces, the Active Trader cell, is swept with Ladder S (2.5.2 The ladders: widths {320, 480, 640, 960} by heights {360, 480, 720}, 12 steps, descending order), and all four Flexible Grid surfaces are compared by the scoped census in their registry workspaces, five census checkpoints in all: the chart cell in the tier A workspaces of the active trader and swing trader personas, and the Active Trader, Level II and Time and Sales cells in the active trader one (2.5.3 Which gadgets get the sweep). The base is the merge-base main build of 07:55, whose merge run completed at 09:39, so every base capture is reused under the same fingerprint and fixture epoch. The overlap reproduces at heights 300 to 420, so under Ladder S it fires at height 360 at every width. Checkpoint ids follow the grammar `<kind>/<surface-id>/<qualifier>`. The commit status is in informational mode (Phase 1). Artifact hashes are shortened.

```markdown
**Sentinel PR run run-2026-11-09-pr-4812: branch-1998.3.0-7be42d0 vs main-1998.3.0-2c91ae4**
Result: **complete** | 4 unexpected | 1 intended | 1 suppressed by decision | [Full report](https://sentinel.internal/runs/run-2026-11-09-pr-4812)
Check `sentinel/pr-run`: failure (informational). 3 tier-1 findings have no decision

**Unexpected (4)**
**1. [Tier 1] invariant_violation on `charts.flexible-grid.active-trader-cell`, checkpoint `sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360`, cluster of 4 checkpoints**
- What moved: INV-01 sibling bounds intersect: the ladder viewport (y 38 to 352) overlaps the order-control strip (y 22 to 52) by 14 px across the strip's width; the first price row draws over BUY MKT and SELL MKT. INV-03 text clipped: the MKT word box is truncated at its lower edge. Fails at height 360 for all four widths (4 of 12 Ladder S steps); first failing step in ladder order 960x360; shown at 480x360.
- Why unexpected: `charts.flexible-grid.active-trader-cell` is not declared in the manifest. It consumes `FlexibleGrid`, touched by manifest entry tos-48120-1 ("Flexible Grid cells lose 6 px of chrome; hosted content gets the full remaining cell height", PR 4812).
- Where: workspace `ws.tier-c.sweep-harness`, Ladder S step 480x360 in a 2x2 grid, cell (0,0), region `rgn.ladder.viewport`, window main, data mode frozen
- Evidence at 1:1: ![base](https://sentinel.internal/a/3f9c) ![candidate](https://sentinel.internal/a/a81d) [heatmap, flicker](https://sentinel.internal/cp/F-20931) (bounds overlaid, intersection hatched; on the base the strip and viewport touch at y=58, 0 px gap)
- Reviewer: not reviewed: invariant and assertion outcomes are never reclassified
- Re-run: stable: the one confirming re-capture on the candidate shows the same 14 px intersection (3.10 Targeted re-run and flake logic); base captures reused from merge run run-2026-11-09-main-2c91ae4 under the same fingerprint and fixture epoch
- Related decisions: none
- Actions: [Confirm bug](https://sentinel.internal/act/confirm_bug?f=F-20931) | [Mark intended with ticket](https://sentinel.internal/act/mark_intended?f=F-20931) | [Request mask with reason](https://sentinel.internal/act/request_mask?f=F-20931)

**2. [Tier 1] collateral on `charts.flexible-grid.active-trader-cell`, checkpoint `sweep/charts.flexible-grid.active-trader-cell/flexgrid/640x480`, cluster of 8 checkpoints**
- What moved: Ladder viewport starts 6 px higher than base; order controls unchanged; no overlap at this size. Heights 480 and 720 at all four widths (8 of 12 steps).
- Why unexpected: same undeclared surface, primitive `FlexibleGrid`, entry tos-48120-1.
- Where: `ws.tier-c.sweep-harness`, Ladder S step 640x480, cell (0,0), region `rgn.ladder.viewport`, window main, data mode frozen
- Evidence at 1:1: ![base](https://sentinel.internal/a/91b0) ![candidate](https://sentinel.internal/a/5e27) [heatmap, flicker](https://sentinel.internal/cp/F-20932)
- Reviewer: description, one call for the sweep cluster with the crops of its smallest and largest steps, 320x480 and 960x720 (claude-pinned-2026-06, prompt 7c1e2a), defect class shifted
- Re-run: stable: identical across 2 re-captures on the candidate (product change); base captures reused from merge run run-2026-11-09-main-2c91ae4 under the same fingerprint and fixture epoch
- Related decisions: none
- Actions: [Confirm bug](https://sentinel.internal/act/confirm_bug?f=F-20932) | [Mark intended with ticket](https://sentinel.internal/act/mark_intended?f=F-20932) | [Request mask with reason](https://sentinel.internal/act/request_mask?f=F-20932)

**3. [Tier 1] collateral on `charts.flexible-grid.active-trader-cell`, checkpoint `census/charts.flexible-grid.active-trader-cell/ws.tier-a.active-trader`**
- What moved: Cell header 6 px shorter; ladder viewport 6 px higher than base; order controls unchanged; no overlap at this workspace's cell size.
- Why unexpected: same undeclared surface, primitive `FlexibleGrid`, entry tos-48120-1.
- Where: workspace `ws.tier-a.active-trader`, census, region `rgn.ladder.viewport`, window main, data mode frozen
- Evidence at 1:1: ![base](https://sentinel.internal/a/4d60) ![candidate](https://sentinel.internal/a/e3f8) [heatmap, flicker](https://sentinel.internal/cp/F-20934)
- Reviewer: description, its own call (a census checkpoint is not a step of the ladder, so it is not in item 2's sweep cluster, 9.7), defect class shifted
- Re-run: stable: identical across 2 re-captures on the candidate (product change); base capture reused from merge run run-2026-11-09-main-2c91ae4 under the same fingerprint and fixture epoch
- Related decisions: none
- Actions: [Confirm bug](https://sentinel.internal/act/confirm_bug?f=F-20934) | [Mark intended with ticket](https://sentinel.internal/act/mark_intended?f=F-20934) | [Request mask with reason](https://sentinel.internal/act/request_mask?f=F-20934)

**4. [Tier 2] collateral on `charts.flexible-grid.level2-cell` and `charts.flexible-grid.time-sales-cell`, checkpoint `census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader`, cluster of 2 checkpoints**
- What moved: Cell header 28 to 22 px; content area 6 px taller; no invariant fired.
- Why unexpected: neither surface is declared. Both consume `FlexibleGrid`, touched by tos-48120-1.
- Where: `ws.tier-a.active-trader` (one census checkpoint per cell: `census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader` and `census/charts.flexible-grid.time-sales-cell/ws.tier-a.active-trader`), region `rgn.cell.header`, window main, data mode frozen
- Evidence at 1:1: ![base](https://sentinel.internal/a/77aa) ![candidate](https://sentinel.internal/a/b2c9) [heatmap, flicker](https://sentinel.internal/cp/F-20933)
- Reviewer: description, one call for the cluster, defect class resized
- Re-run: stable: identical across 2 re-captures on the candidate (product change); base captures reused from merge run run-2026-11-09-main-2c91ae4 under the same fingerprint and fixture epoch
- Related decisions: none
- Actions: [Confirm bug](https://sentinel.internal/act/confirm_bug?f=F-20933) | [Mark intended with ticket](https://sentinel.internal/act/mark_intended?f=F-20933) | [Request mask with reason](https://sentinel.internal/act/request_mask?f=F-20933)

**Intended (1)**
**`charts.flexible-grid.chart-cell`, 2 checkpoints match tos-48120-1**
- Declared: "Flexible Grid cells lose 6 px of chrome; hosted content gets the full remaining cell height" (tos-48120-1, ticket TOS-48120, PR 4812)
- Evidence at 1:1: ![base](https://sentinel.internal/a/c044) ![candidate](https://sentinel.internal/a/0dd3)
- Reviewer confirmation: intended 0.93: "Header 28 to 22 px and plot area 6 px taller match the entry; one more price label per chart."
Manifest reconciliation: tos-48120-1: verified visible on 2 checkpoints

**Suppressed by decision (1)**
<details><summary>1 finding matched an earlier decision</summary>

| Checkpoint | Region | Decision | Action (verdict) | By | Ticket |
|---|---|---|---|---|---|
| `census/trade.order-entry.stock/ws.tier-a.active-trader` | `rgn.ticket.header` | dec-000412 | noise_signature (noise) | j.alvarez, 2026-10-21 | |
</details>

**Coverage**
| Tier | Registered | In scope | Covered | Not compared | not_run |
|---|---|---|---|---|---|
| 1 | 41 | 20 | 14 (100 percent of comparable scope) | 0 | 21 scope, 0 budget, 6 entitlement, 0 quarantine, 0 step_failed, 0 base_step_failed, 0 app_crash, 0 model_unavailable |
| 2 | 68 | 3 | 3 (100 percent of comparable scope) | 0 | 65 scope, 0 budget, 0 entitlement, 0 quarantine, 0 step_failed, 0 base_step_failed, 0 app_crash, 0 model_unavailable |
| 3 | 112 | 0 | 0 | 0 | 112 scope, 0 budget, 0 entitlement, 0 quarantine, 0 step_failed, 0 base_step_failed, 0 app_crash, 0 model_unavailable |

Scope: 1 declared surface, expanded through 4 dependency-map edges (FlexibleGrid), confirmed by the static impact step, plus the smoke census (60 checkpoints); personas active trader and swing trader. 0 `unregistered_surface`, 0 `dependency_map_gap`. Send policy no_sends: 6 order lifecycle surfaces `not_run` (entitlement). [Matrix](https://sentinel.internal/runs/run-2026-11-09-pr-4812/coverage)

**Run health**
| Item | Value |
|---|---|
| Wall clock / budget | 41:12 / 45:00 on 2 shards |
| Fingerprint | candidate 9e1f03 = base 9e1f03 (build id differs, as expected); base captures reused from run-2026-11-09-main-2c91ae4 |
| env_error / harness_error | 0 / 0 |
| not_settled / not_comparable / landmark_missing | 0 / 0 / 0 |
| Steps: passed_after_retry / passed_via_agent_fallback / failed / blocked_by_gateway | 0 / 0 / 0 / 0 |
| Layout shift | 0 checkpoints |
| Reviewer | 4 calls (items 2, 3 and 4 and the intended chart cell), 0 pending, 0 unavailable, 0.039 USD (mid tier; 0.078 USD top tier, 11.4) |
| Targeted re-runs | 18 (18 stable, 0 unstable), 32 re-captures: 1 each for the 4 `invariant_violation` steps, 2 each for the 14 other above-threshold checkpoints; 0 `rerun_budget_exhausted` |
| Canary (last Nightly) | money cells 100 percent, other 96.9 percent (31 of 32), detector state ok, 2026-11-08 |
| Safety | attestation passed, paper mode asserted 4 times, gateway 0 drops, Cancel All not needed, proof PROVEN: no orders sent |
| Integrations | PR comment posted 11:12 ET, Jira 0 queued, Slack 2 posted to the PR author and triage (tier-1 `invariant_violation`, item 1; tier-1 `collateral`, items 2 and 3 in one thread) |
```

Expected triage. The developer, who is the PR author, clicks Confirm bug on item 1; any role may confirm a bug, so the decision row is written at once with verdict `confirmed_bug`, the outbox files TOS-48391 with both crops, the overlay, primitive, entry, PR and failing range, and the ticket key lands on the decision and the finding. On items 2 and 3 the developer clicks Mark intended with ticket TOS-48120, because the 6 px shift is the declared header change on a surface the entry did not declare. Both are tier 1, so each click is a proposal: it is recorded on the finding, the triage rotation sees it in the queue with a 4-business-hour clock, confirms it, and the decision rows are written with the rotation member as `decided_by` and "proposed by {developer}" in the reason; the manifest amendment PR declares `charts.flexible-grid.active-trader-cell` on tos-48120-1. Item 4 is tier 2 and goes the same way with the same ticket; the amendment also adds the Level II and Time and Sales cells, and the evidence comment lands on TOS-48120. The check stays `failure` in informational mode until items 1 to 3 have decisions, which they now do; it turns `success` within a minute. After the ladder fix is pushed, the next run shows no invariant at the 4 height-360 steps; those 4 and the other 8 steps are `intended` under the amended manifest (the 8 steps and the census checkpoint inherit the item-2 and item-3 decisions, 3.11 row 14; the 4 height-360 steps are confirmed by the Reviewer against the amended entry, row 20), and the item-1 `invariant_violation` signature is absent. When TOS-48391 reaches Done with a fixVersion the Jira poller records `fixed_in_build` on dec-000517, the next run asserts the signature is gone, and the finding closes.

### 9.4 Merge and RC report structure

The merge report is a report UI page plus a Slack digest to the triage rotation listing only new, unsuppressed Unexpected findings (9.9). Its manifest is the union of the manifests of the PRs merged between the two main builds; when merges coalesced (11.2 Cadence and time budgets per run type) the header names the range of main builds and PRs covered. Sections in order:

1. Header: build pair, fingerprint match, run outcome, duration against the 3 hour budget, shards, reviewer model id and prompt hash, cost, metrics header (9.11), detector banner if any.
2. Unexpected: PR comment item format, full list, ranked as in 9.2. A finding a PR run had in scope but did not report is labelled "missed in PR run", recorded as finding `missed_in_pr_run`, and opens a harness task; one outside PR scope on a surface consuming a primitive the PR touched emits `dependency_map_gap`. A finding on a surface that the candidate's dependency index reaches from a class changed by a merged PR, whose PR run had the surface `not_run (reason: scope)`, is labelled "missed by static impact", recorded as finding `missed_in_pr_run` with `detail: static_impact`, and opens a harness task for the system owner.
3. Intended: per merged PR, declared surfaces with confirmed diffs; then manifest reconciliation: entries verified visible, entries with a covering checkpoint but no visible change (`intended_change_not_observed`, a question to the PR author and feature owner, not a bug), entries with no covering checkpoint (flow-authoring task). Manifest coverage prints as entries verified over entries.
4. Known bugs: findings inheriting an open `confirm_bug` decision, with ticket status and age; contested and conflicting decisions (9.7) listed here with both deciders.
5. Suppressed by decision (full table), then census, invariants and flows: counts per outcome per tier, with links.
6. Coverage matrix: full surface x persona x entitlement matrix (9.5) from `coverage_cells`, and the "Not detectable by design" table of 2.13 What this model cannot detect and how it is labeled.
7. Run health: as in the PR comment plus popup catalog hits, `unknown_popup` findings, retries, fallbacks clustered by anchor template, p50 and p95 step time, egress log count, disk free, orphaned baselines, mask budget status, outbox state.
8. Safety proof: the A1 to A11 attestation results with evidence hashes (12.6 Run-start attestation checklist), paper-mode assertions with their count, end-of-run Cancel All result, Working and Filled diff against the start snapshot, positions flat, the proof verdict and reason block of 6.6 Kill switch, recovery and post-run proof, and the previous night's fire drill and safety canary results: pass, `harness_error (reason: drill_failed:<item>)` with the quarantined flows, or `aborted_safety (reason: drill_order_placed)` with the blocked login (6.5 Nightly fire drill and safety canaries).
9. Baseline candidates: every `intended` capture of a merged PR and every `needs_baseline` checkpoint, queued for approval. This is the only place baseline candidates are created from product runs (9.8).

The RC report adds: comparison against the lineage baseline (9.8); Intended grouped by Jira epic and reconciled against the release's ticket export (4.3 Change manifest, `source: ticket_export`); size sweep results per gadget and Ladder F step; soak summary per 10.11 Soak: long sessions, resource budgets and the freeze detector (both builds' curves per metric with the budget line, restarts, `new_log_signature` findings, the memory ceiling reached and the 24 cycle outcomes); Explorer findings, shown only when the single replay reproduced them, each carrying the replayed flow's own outcome (`invariant_violation`, `assertion_failed`, else `needs_baseline` with the trace attached, never `needs_human`; 2.9 Explorer), the rest under "Explorer: not reproduced", or the single line "Explorer: not run, model endpoint unavailable" when the session was `not_run (reason: model_unavailable)` (10.7); tier B workspace migration results; baseline approval status; an escape-tracking seed listing every tier-1 surface not compared in this RC; the release gate checklist (the G-N conditions of 10.9 Release-day timeline); and a sign-off block recording system owner and release manager, time, RC build, accepted open findings with tickets and the approved baseline set that becomes the next lineage baseline, N business days before GA (N is PA-14 in 11.1 Planning assumptions: 3 business days by default, per train in `policy/release_gate.yaml`). The report is frozen at sign-off; later decisions create a new version and the block names the version signed. An RC run superseded by a newer RC build (`aborted_budget (reason: superseded)`, 10.9, 11.2) still publishes its partial report, labelled "superseded by {build}" in the header, to the release manager and the release channel; it cannot be signed, and its decisions are inherited by the new RC run (9.7).

### 9.5 Report UI pages

The report UI is the FastAPI application of 5.13 Reporter and Integrations, on the controller host behind SSO. It reads SQLite through read-only WAL connections and writes only through the Reporter service API (8.7 Report UI data access), the Central Store's single writer. Every page shows the run's provenance: harness commit, config commit, VM image id, build pair, model ids, prompt hash.

| Page | Shows | Actions |
|---|---|---|
| Run list and run detail | List: runs by type with outcome, build pair, counts per outcome, duration, cost, open items by tier, outbox state. Detail: the sections of 9.4, or the PR comment content for PR runs; an Alerts panel with every alert raised for the run and its acknowledgement | Compare two runs, re-run reviewer on pending items, all finding actions, refresh the PR comment, close an abandoned outbox job |
| Checkpoint detail | Base and candidate at 1:1, flicker, heatmap, connected-component boxes, region overlay (freeze, rule, mask, diff), landmark match score, outcome with the lower results as evidence, reviewer inputs and output, Oracle values with tolerances, invariant results with bounds source (JAB or OCR), step events, log slice, settle timing, re-run results, flake score, history over 30 runs, the live decision lookup (what a new diff here would inherit) | Finding actions; request targeted re-run; quarantine or un-quarantine; open region in the Region Registry |
| Triage queue | Every finding without a decision, ranked as in 9.2, with SLA clock, assignee and pending proposals awaiting a second person | Finding actions, assign, confirm or decline a proposal, apply to a whole cluster |
| Decisions | Search by signature, checkpoint, ticket or decider; active, expired, superseded, reopened, revoked and contested decisions; conflicts; decisions without tickets; expiring in 7 days; suspect decisions (two reopenings) | Supersede, revoke, re-confirm an expiring noise signature |
| Baseline approval queue | Candidates per checkpoint or cluster from merge runs (9.8), base and candidate at 1:1, nearest existing baseline by perceptual hash for `needs_baseline` items, "epoch changed" flags | approve_baseline, reject_baseline, retire checkpoint |
| Coverage matrix | Surface x persona x entitlement, each cell covered / `not_run` (reason) / not registered, per run and per release, tier filter, red rows for "in manifest, no covering checkpoint" | Open a flow-authoring task for a cell |
| Canary panel | Nightly results per mutant and class, detector state, what absorbed each miss | Open the mask, decision or threshold named |
| Metrics | The dashboard of 9.11 | Change window, export CSV |

### 9.6 One-click actions and who may perform them

Actions are the `decision` enum of 4.14 Decision record (confirm_bug, mark_intended, request_mask, noise_signature, agent_mistake, expected_behavior, approve_baseline, reject_baseline, fixed_in_build). What the store keeps is the verdict enum of 8.3 (confirmed_bug, intended, noise); the decision row carries both the action and the verdict. Each action is idempotent per finding and action and is logged with actor, role, time, finding and run; the actor is always a human login (L-DEC-01 in 4.14). Roles are defined in 9.12. The Reviewer performs none of these; it may only pre-fill the baseline queue and rank the triage queue.

Action to verdict to effect on later runs:

| Action | Verdict stored | Finding `triage_state` (8.3) | A later matching diff (9.7) | Ends |
|---|---|---|---|---|
| confirm_bug | confirmed_bug | confirmed_bug | Keeps its computed outcome; filed under the ticket, listed under Known bugs, no new alert | fixed_in_build |
| mark_intended on `collateral`, `needs_human` or `intended` | intended (ticket required) | intended | Outcome `intended` with the decision id; listed under Suppressed | Baseline approved for the release, or the ticket leaves every manifest in scope |
| mark_intended on `invariant_violation` or `assertion_failed` (waiver) | intended (ticket required, expiry at most 30 days) | intended | Outcome unchanged; finding suppressed as waived | Expiry, no silent renewal |
| request_mask | noise, inert until `mask_ref` is set | open until the mask PR merges, then noise | Nothing until the mask merges; afterwards the region is type mask at the new config commit, so no diff exists; the decision row is the audit link L-MSK-01 requires | Mask expiry (default 30 days) |
| noise_signature | noise | noise | Outcome `noise` with the decision id; listed under Suppressed | 30 days unless re-confirmed |
| agent_mistake | noise | agent_mistake | An Explorer finding with the same signature is held, not reported; its candidate flow is discarded | Revoked, or signature absent 30 days |
| expected_behavior | noise | expected_behavior | Held as above; the candidate flow is kept and may be promoted with an assertion that encodes the expected behavior | Same |
| approve_baseline | intended (ticket) plus the `baselines` row `approved` | closed for the `needs_baseline` finding | RC runs on the lineage compare against the image; the Canary Service mutates it; `needs_baseline` clears for the checkpoint | Superseded or retired |
| reject_baseline | confirmed_bug when a ticket is given (the candidate shows a defect), else noise (not representative; a new candidate comes from the next merge run); the `baselines` row `retired` | confirmed_bug, or open | `needs_baseline` persists until a new candidate is approved; with a ticket the finding is a Known bug | New candidate approved |
| fixed_in_build | confirmed_bug (a new row that supersedes the confirm_bug row, `fixed_in_build` set) | fixed_pending_verify, then closed | On or after that build the signature must be absent. Absent for one run: finding closed. Present: decision status reopened, finding returns to Unexpected, ticket reopened (9.9) | Finding closed |

Who may perform each action, what it writes and what the next run does:

| Action | Who may perform | Writes | Next run |
|---|---|---|---|
| Confirm bug | Any role in 9.12, including the PR author | Decision row (action confirm_bug, verdict confirmed_bug, signature, run, build, reason); finding `confirmed_bug`; outbox job: Jira Bug or link to an open ticket (9.9); ticket key on decision and finding when it lands | Matching diffs keep their outcome, appear under Known bugs with ticket and age, no alert; the PR check stays `failure` only while a tier-1 item has no decision, so confirming clears it |
| Mark intended with ticket | Tier 3: any role, the PR author alone. Tier 1 and 2: the PR author proposes; the row is written when the triage rotation or the tier's flow owner confirms (two-person rule below). Other roles alone | Decision row (action mark_intended, verdict intended, ticket, reason naming the proposer); finding `intended`; outbox jobs: manifest amendment PR if the surface was undeclared, evidence comment on the ticket | Matching diffs become `intended` (decision id) and are suppressed; the merge run that lands the PR queues the capture as a baseline candidate (9.8) |
| Waive invariant or assertion | Flow owner of the tier; tier 1 also needs the system owner | Decision row (mark_intended on category `invariant_violation` or `assertion_failed`, ticket, `expires_at` at most 30 days) | Outcome unchanged; finding suppressed as waived until expiry. A wrong invariant is instead a registry exception with owner, reason and expiry (2.7 Layout invariants) |
| Request mask with reason | Anyone with report UI access; refused on `money_cell` regions (L-DEC-04). The UI also refuses to submit a request whose prefilled mask would take the panel's masked share (masks plus non-identity redaction, 3.8) over its budget, computed on the finding's capture, and says why: it shows the current share, the budget and the advice to declare a `rule` region instead. The finding then stays open in Unexpected until it is decided by `confirm_bug` or `mark_intended`; raising a budget is a system-owner pull request to `sentinel-config`, never a triage action | Decision row (action request_mask, verdict noise, `mask_ref` null); outbox job: Region Registry pull request with the mask record prefilled (anchor, owner, reason, expiry default 30 days, human author, `decision_ref`) | Nothing until the PR merges; the finding stays in Unexpected marked "mask requested, PR {n}" |
| Approve mask | Triage rotation; tier-1 panels also need the tier-1 flow owner as reviewer; never the requester | Merges the mask PR after the lint and per-panel budget check (4.8); sets `mask_ref` on the decision; `masks` row at the new config commit. A mask PR that fails the budget check at merge (another mask merged first) is closed by the Reporter, the request_mask decision is revoked with reason `mask_budget_exceeded`, the requester and the triage rotation are notified, and the finding returns to the triage queue with its SLA clock restarted | The region is masked; the finding closes as noise when the run on the new config commit shows no diff there |
| Mark noise | Triage rotation, flow owner of the tier; refused on `money_cell` regions | Decision row (action noise_signature, verdict noise, `expires_at` 30 days); finding `noise` | Matching diffs become `noise` (decision id); the signature joins the approved noise library used by classification (3.11 Classification decision table, input M) |
| Approve or reject baseline | Tier 1: tier-1 flow owner, never the author of the change. Tier 2 and 3: flow owner of the tier or triage rotation. Lineage set: system owner and release manager at RC sign-off. Refused from PR runs, from runs with synthetic outcomes and from runs not `complete` (L-DEC-05) | `baselines` row state change with approver, time, reason, ticket, cluster id; decision row (approve_baseline or reject_baseline, `baseline_ref`) | As in the mapping table |
| Explorer disposition | Triage rotation | Decision row (confirm_bug, agent_mistake or expected_behavior) on the Explorer finding's signature; for confirm_bug the Jira job as above. The finding carries the replayed flow's own outcome (`invariant_violation`, `assertion_failed` or `needs_baseline` with the trace, never `needs_human`; 2.9 Explorer), and these actions apply to it | As in the mapping table; the Explorer precision and agent-error ratio metrics update |
| Promote candidate flow | Flow owner of the surface's tier | Pull request moving `flows/quarantine/<finding-id>.yaml` into `flows/` after trimming (2.9 Explorer); not a signature decision | The flow runs in its class from the next run; its checkpoints are `needs_baseline` until the next good build captures one |
| Accept anchor patch | Flow owner of the tier, triage rotation | For step findings (`passed_via_agent_fallback`, `failed` locator, broken anchor): anchor-patch PR and SENT maintenance task; not a signature decision | The step resolves on a scripted rung again |
| Record fixed in build | The Jira poller on Done with a fixVersion or build id, writing the resolver's login; or any role in 9.12 by hand | Decision row (fixed_in_build, supersedes the confirm_bug row); finding `fixed_pending_verify` | Signature asserted absent on or after the build; reopened if present |
| Revoke decision | Triage rotation, flow owner, system owner | `revoked_at`, `revoked_by`, `revoke_reason` on the row and status `revoked` (8.3, 4.14; distinct from `superseded`, which only a replacement sets); finding reopened | The finding returns to Unexpected on the next matching diff |
| Supersede decision | Same roles; see the conflict rule in 9.7 | New decision row with `supersedes`; the old row status superseded | The new verdict applies |
| Quarantine or un-quarantine | Flow owner of the tier, system owner | Quarantine record PR (10.5 Flake tracking, targeted re-runs and quarantine); `flake_stats.quarantined` | `not_run (reason: quarantine)` on every run, or re-entry after 5 clean Calibration captures |
| Override check | System owner only | Run row override fields with reason | Commit status forced `success` for that run only; printed in Run health and the comment |

Two-person rule. (a) The PR author (any committer on the PR branch, as the PR host reports it) may not mark a tier 1 or tier 2 diff on their own PR intended alone. Their click is a proposal: it is stored on the finding (`evidence_json.proposal`: action, proposer, time, ticket, reason), shown in the triage queue with a 4-business-hour clock (9.12), and becomes a decision row only when the triage rotation or the tier's flow owner confirms it; `decided_by` is the confirmer and the reason names the proposer. Declining returns the finding to Unexpected with the reason. (b) Nobody approves a mask they requested. (c) Nobody approves a baseline for a change they authored. (d) A tier-1 waiver needs the flow owner and the system owner. The Reporter service API enforces every clause from the PR host's committer list and the mask PR's author, and a refused write names the clause. 6.12 Authorization, accountable owner and incident procedure references this rule.

### 9.7 Decision persistence and inheritance

Decisions live in `decisions` and are never deleted; a change is a new record that supersedes the old one (the old row's status becomes `superseded`), and a revocation sets the revoked fields, moves the row's status to `revoked` and leaves the row (4.14, 8.3).

Diff signature. Four parts: the checkpoint id (`<kind>/<surface-id>/<qualifier>`, stable, unique across the config repository, validated in CI; renames need an entry in `flows/RENAMES.yaml`); the region id from the Region Registry or, outside any registered region, the coarse grid cell (8 x 8 over the window, relative to the landmark, `g-<row>-<col>`) that holds the diff's largest connected component; the 64-bit DCT perceptual hash (pHash) of the candidate crop at the region's anchored bounds; and the category. A decision matches a later finding when checkpoint id, region id and category are equal and the Hamming distance between pHash values is at or below the tolerance stored on the decision row.

Hamming tolerance. 8 bits of 64 for signature matching; 6 for cross-checkpoint clustering and for every tier-1 checkpoint (3.10 Targeted re-run and flake logic, 3.15 Model and prompt change control; the SQL lookup is in 8.3). A region's tolerance may be lowered, never raised, by calibration: with noise spread the largest pHash distance among the region's A/A pairs and fault separation the smallest distance between its distinct mutants, the region uses the smaller of its default and fault separation minus 1; if that is at or below the noise spread it uses 0 and matching falls back to pHash equality plus the largest component's bounding box quantized to 4 px. Distances between the tolerance and 12 show on the finding as "decision #id at Hamming d, not applied". Changes print in the Nightly report.

Clustering. Findings of one run cluster by checkpoint kind, and the same clusters are the report items and the Reviewer calls (5.11). Sweep and split checkpoints of one surface and one region with equal category form ONE cluster per run whatever their sizes: one report item with a member count and, when it needs a verdict, one Reviewer call that sends the crops of the smallest and the largest affected step. Census and flow checkpoints with equal category, primitive and region, pHash within 6 and component boxes equal within 4 px, the boxes expressed relative to the region's anchored bounds, form one cluster. Invariant violations and assertion failures never go to the Reviewer (3.16); they cluster for the report by the same two rules. In 9.3, items 1 and 2 are the two sweep clusters of the Active Trader cell's region `rgn.ladder.viewport`, split by category; item 3 is a census checkpoint, not a step of the ladder, so it is its own cluster with its own call; item 4 is one census cluster of two surfaces with one call; the two intended chart-cell checkpoints are one census cluster with one call. The run makes 4 Reviewer calls. A cluster decision is written per member signature with the cluster id, so inheritance survives a later split. When a later run clusters differently, each member is matched on its own: a cluster is suppressed only when every member matches a decision with one verdict; otherwise it is listed in Unexpected as "k of n members suppressed by decision #id" and the actions apply to the unsuppressed members.

More than one match. The lookup returns every active decision within tolerance for the signature's checkpoint, region and category, not just the nearest. If all carry one verdict, the nearest by Hamming distance, then the newest, is inherited and the others are listed as related. If verdicts differ (one human marked the signature noise, another confirmed a bug), nothing is inherited: the finding keeps its computed outcome, is listed in Unexpected with "conflicting decisions #a (verdict) and #b (verdict)", both deciders and the triage rotation are notified (9.10), and the rotation resolves it by superseding one row. 8.3's lookup therefore has no `LIMIT 1`: it returns the full verdict set for the signature, and the inheritance rule above reads it.

Two humans, one signature. The API refuses a plain decision on a signature that already has an active decision and shows the active row. An intended decision whose ticket is in no loaded manifest of the run, and whose build pair is not the run's, is not active for this refusal (the mark_intended scope in the table below). The only ways forward are Supersede (reason required, records `supersedes`, notifies the earlier decider in the triage channel, marks the finding contested in the weekly summary) and Revoke. Two supersessions within 30 days on one signature freeze it: later matches inherit nothing, the finding lands in Unexpected every run, and the next decision on it needs the system owner. Concurrent writes serialize through the single writer, so the second writer receives the first's row and the refusal, never a silent overwrite.

| Decision | Applies to | Ends when |
|---|---|---|
| noise_signature | Every run, any build | 30 days after creation unless re-confirmed from the digest or the Decisions page with one click (another 30 days); on expiry re-evaluated against the A/A floor |
| mark_intended | The build pair it was made on and later pairs whose manifest carries the same ticket | The checkpoint's baseline is approved for the release, or the ticket leaves every manifest in scope |
| confirm_bug | Every run while the ticket is open; shown under Known bugs, no new alert | fixed_in_build recorded from the ticket or by hand (9.9): from that build on the run asserts the signature is gone; if it returns, status reopened, the ticket is reopened and the finding returns to Unexpected |
| request_mask | Every run while the mask record is active, through the mask region rather than the signature | Mask expiry (default 30 days); re-evaluated against the A/A floor and re-approved or dropped |
| waiver (mark_intended on an invariant or assertion) | Every run while active | Expiry at most 30 days, no silent renewal |
| agent_mistake, expected_behavior | The Explorer finding's signature | Revoked, or the signature is absent for 30 days |
| approve_baseline | The checkpoint or cluster in its lineage | Superseded by a later approval or retirement |

Rescued checkpoints (`needs_human (reason: rescued_capture)`, reached through `passed_via_agent_fallback`) never inherit a `noise` or `intended` decision: rows 13 and 14 of 3.11 Classification decision table require a scripted path, so the finding is listed and a human decides (3.1 Build pair rules, 5.5 Locator ladder); an agent rescue is capped at 25 actions and 90 seconds. Category is part of the key, so a noise_signature on a pixel diff never suppresses an `invariant_violation` on the same region and a mark_intended never suppresses an `assertion_failed`. Tier is not part of the key. Run type is not part of the key: a decision made on a PR run is inherited by the merge and RC runs and the other way round, subject to the mark_intended ticket rule. For `invariant_violation` and `assertion_failed` the outcome never changes; a decision only suppresses the finding in the report. Matching runs before the Reviewer: a finding that inherits a decision is written with the decision id and skips the Reviewer, which also caps reviewer cost. Two consecutive findings reopened against one decision mark that decision suspect in the weekly summary and on the Decisions page.

Record fields: schema and worked example in 4.14 Decision record, DDL in 8.3. Decision id, signature (checkpoint id, region id, pHash, `hamming_tolerance`, category), action, verdict, decided by, confirmed by (the second person of the two-person rule), source (`report_ui`, `pr_comment`, `jira_sync`, `explorer_triage`), decided at, run id, build id, finding ref, cluster id, ticket, reason, mask ref, baseline ref, fixed in build, expires at, status (active, expired, superseded, reopened, revoked; `revoked` is a revoke and `superseded` a replacement), supersedes, revoked fields.

### 9.8 Baseline lifecycle

Census, size sweeps, PR and merge runs compare against the base build's capture and need no stored baseline. Baselines serve three purposes: the lineage baseline for RC runs, the approved image the Canary Service mutates, and checkpoints whose flow declares an expected screen. States: candidate, approved, retired.

Promotion happens only at merge-run triage, and for the lineage set at RC sign-off. A PR run never produces a candidate: its `intended` captures are evidence for the PR, and its `needs_baseline` checkpoints are shown but cannot be approved (L-DEC-05 in 4.14 refuses approvals from PR runs). The merge run that lands the PR captures the same checkpoints on main and queues those captures as candidates (9.4, item 9). The Reviewer never promotes; an intended verdict only orders the queue.

- Candidate: produced by a merge run from an `intended` capture whose PR merged, from a checkpoint's first capture on main (outcome `needs_baseline`, excluded from bug counts but blocking a clean report), or from a completed RC run. Candidates sit in the approval queue beside the nearest existing baseline by pHash. A data-epoch change on a frozen region (7.9 Data modes operationally) flags the affected approved baselines "epoch changed" at the top of the queue with the next merge-run capture as their candidate; until re-approved they remain the lineage base and RC comparisons against them print "baseline predates epoch".
- Approved: a human-only action, per checkpoint or per signature cluster within one flow, never per run, recording approver, time, checkpoint id, candidate build, release channel (lineage), reason and ticket (approver rules in 9.6; one approved baseline per checkpoint and lineage is enforced by `ux_baseline_approved` in 8.3). Approval is a pointer change over content-addressed images, so revert is another pointer change and any run can be re-diffed against the previous one or two approved sets. Oracle extractions for the checkpoint must have passed before approval (5.10 Oracle).
- Retired: when a checkpoint is removed or renamed without a rename map, or when a candidate is rejected; tombstoned with `retired_in_build`, the build it was retired in. Orphaned baselines appear in Run health until a human confirms the retirement; the Nightly run reports them and never aborts over them.

The lineage baseline for an RC run is the approved set of the last released build on the same release channel; channels keep separate lineages. An RC cannot be signed off while tier-1 checkpoints have pending candidates or while a detector banner is up (10.3); the approved set recorded at sign-off becomes the next lineage baseline. Images are stored per 8.4 Artifact layout on disk with a manifest in git per approved set (8.5), so every run names the exact baseline set it used.

### 9.9 Jira and Slack rules

Jira. Two projects set in configuration: the product project (assumed key TOS) and the harness project (assumed key SENT). Access is a service account with create, comment, transition and JQL search; Jira edition, API version, and whether labels accept `/` and `.` are Phase 0 items (11.8 Phase 0 spike plan: seven decision records, items outside the decision records; default branch: every id-bearing label is hashed), which is why ids with slashes are hashed in labels.

Labels, on every ticket the Reporter creates or links: `sentinel`; `sentinel-tier-{1|2|3}`; `sentinel-surface-{surface id}` (dots and hyphens only, by the id grammar); `sentinel-cp-{first 12 hex of sha256(checkpoint id)}`; `sentinel-dec-{decision id}`, one per decision linked to the ticket, at most 8 as labels and the rest in the description block. The description carries a machine-readable block that is the ticket's identity, labels being only the search index:

```
sentinel:
  decision: dec-000517
  checkpoint: sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360
  region: rgn.ladder.viewport
  category: invariant_violation
  phash: 3f9c1a0e55b2d8c4
  run: run-2026-11-09-pr-4812
  report: https://sentinel.internal/f/F-20931
```

| Event | Posts | Dedup |
|---|---|---|
| Confirm bug | Bug in TOS, priority by tier (1 Highest, 2 Medium, 3 Low), the labels above; description with build pair, surface, primitive, manifest entry, PR, layout and size or failing range, workspace, 1:1 crop links, reproduction (census path, or flow and step), the identity block; key written to the decision and the finding | Primary dedup is inheritance: a finding that inherits an open confirm_bug decision takes its ticket and never searches. On a new confirm_bug the UI runs JQL `labels = sentinel AND labels = sentinel-cp-{hash} AND statusCategory != Done`, then the same with `sentinel-surface-{id}`, and offers the hits as "link to existing ticket"; the actor links or creates. Linking adds the new `sentinel-dec-` label and identity block to the ticket. A ticket found by label whose identity block names a different checkpoint id is a label collision: it is ignored for linking, counted in Run health, and the new ticket is created |
| Seen again | Comment "seen again in run X on build Y" on the open ticket of an inherited confirm_bug, with the finding link | At most once per ticket per 24 hours |
| Mark intended with ticket | Comment on the named ticket with the evidence links (the proof that replaces hand screenshots) and the manifest amendment PR; the `sentinel` and `sentinel-dec-` labels | One comment per run per ticket. The key is checked against the Jira regex at once and for existence when Jira answers; a key that does not exist returns the finding to the triage queue with "ticket not found", notifies the decider and sets the decision to expire in one business day unless superseded with a valid key |
| `env_error`, `harness_error`, aborted runs; `unregistered_surface`, `dependency_map_gap`, `unknown_popup`, no covering checkpoint; `passed_via_agent_fallback` twice on one step and build ("update scripted flow", for the flow owner); abandoned outbox jobs | Task in SENT, run links appended | One open task per key: gate name or error class, surface label, edge, popup title, manifest entry id, flow and step id, outbox job kind |
| Canary state `detector_blind`, `generation_failed` or `detector_degraded` (10.3) | Bug in SENT, Highest for `detector_blind` and `generation_failed`, High for `detector_degraded` | Keyed by mutant id, class for a floor miss, or the stage for `generation_failed` |

Jira unavailable. Posts are outbox jobs (9.1). A confirm_bug decision is written at once with `ticket_ref` null; the finding shows "ticket pending (Jira unreachable since {time})" and is already listed under Known bugs on later runs by its decision; the key lands when the job succeeds. After 15 minutes the integration outage alert fires; after 24 hours the digest and the Decisions page list "decisions without tickets" until a human links a ticket by hand or Jira returns. Nothing in a run waits on Jira.

Auto-filing of `collateral` without a human is off. The system owner may enable it per tier only when the Reviewer meets the gates of 3.14 Eval set, metrics and gates on the current eval set and the money-cell canary pass rate has been 100 percent for 30 days; even then tickets go to a SENT triage queue, never the product backlog, until a human confirms. Fix verification: tickets labelled `sentinel` are polled hourly (webhook where available); Done with a fixVersion or build id writes a fixed_in_build decision for every `sentinel-dec-` on the ticket, with the Jira resolver's login as `decided_by` and `source: jira_sync`; a signature that returns on or after that build reopens the ticket by transition with a comment naming run, build and crop, or, where the workflow forbids the transition, adds the comment and a `sentinel-reopened` label and raises a P2 alert. Synthetic records never post (10.3).

Slack. Four channels set in configuration: on-call, triage, release, and the safety channel `#sentinel-safety`. PR runs post only tier-1 `collateral`, `invariant_violation` and `assertion_failed` findings (to the PR author and triage) and every event of the 9.10 safety routing (`aborted_safety`, UNPROVEN post-run proof); everything else stays in the comment.

| Channel | Posts |
|---|---|
| on-call | dead-man, `env_error` and `harness_error` with gate name and last screenshot, the safety events of the 9.10 safety routing (also posted to `#sentinel-safety`), `aborted_harness`, `complete_with_env_errors`, gateway safety drops, a Nightly run skipped for an RC run, canary states `detector_blind`, `generation_failed` and `detector_degraded`, `canary_path_divergence`, `global_rendering_shift`, pool saturated, preflight failures, store health and cost alarms (9.10), reviewer backlog or disabled Reviewer, integration outages |
| triage | new tier-1 `collateral` (reviewer confidence at or above 0.80, or no reviewer needed), `invariant_violation` or `assertion_failed` with no matching decision; merge run digest of new, unsuppressed Unexpected findings; daily `needs_human` digest with SLA breaches and escalations; pending two-person proposals older than 2 business hours; decision conflicts and contested supersessions; baseline queue reminder 3 business days before an RC run; weekly metrics summary |
| release | RC report summary, sign-off status changes, release gate result, commit status mode changes; the detector banner is also posted here |
| `#sentinel-safety` | Every event of the safety routing in 9.10: `aborted_safety` with its reason, every S1 incident (6.12) including `explorer_order_stage`, UNPROVEN post-run proof, fire-drill failures (`drill_failed:<item>`, `drill_order_placed`) and safety canary misses (`safety_canary_blind:<canary>`), each with the incident id and a link to the incident row; nothing else is posted here, so the channel is the safety record |

Dedup: one message per run and alert kind, with later findings of that kind and updates threaded under it (in 9.3, items 2 and 3 are one tier-1 `collateral` message and its thread reply); across runs a `collateral` checkpoint id plus signature is posted at most once per 24 hours, and none when an open Jira ticket carries a matching `sentinel-dec-` label (a thread reply on the original post instead, if under 7 days old). Slack unavailable: outbox as above; a P1 that Slack cannot carry still goes out through the paging service and email (9.10). Only text and `/a/<sha256>` links leave; every post is written to `egress_log`.

### 9.10 Alerting model

P1 pages the harness on-call through the paging service and posts to Slack and email in parallel; P2 posts and mentions the rotation; P3 goes in the daily digest, which also lists findings past their SLA (9.12). A page that no channel accepts within 5 minutes writes `alert_undelivered` to `system_state` and is shown on the run list page, which is local and always available. The Watchdog and the Reporter host raise dead-man alerts; the Reporter raises the rest at `reported` or at abort. Runs happen in the operating day, 06:00 to 22:00 America/New_York (11.1 Planning assumptions, PA-12); between 22:00 and 06:00 only `aborted_safety`, `aborted_harness`, a gateway safety drop, `explorer_order_stage`, fire-drill failure (`harness_error (reason: drill_failed:<item>)` or `aborted_safety (reason: drill_order_placed)`), safety canary miss (`harness_error (reason: safety_canary_blind:<canary>)`), UNPROVEN post-run proof, canary state `detector_blind` or `generation_failed`, and dead-man page, and every other alert is posted at 06:00. The paging service is the firm's existing on-call tool; its name is a Phase 0 item (11.8).

Safety routing. This table is the one authoritative routing for safety events; 1.10, 6.4, 6.6, 6.12 and 11.6 refer to it instead of restating it. Every event in it pages the harness on-call at P1 at any hour, notifies the accountable owner and posts to `#sentinel-safety` (9.9); the rows add the other recipients.

| Safety event | Paged at P1 | Notified |
|---|---|---|
| `aborted_safety` with any reason (`unexpected_working_order`, `attempted_send`, `kill_switch`, `drill_order_placed`, `unknown_dialog_in_order_stage`, `undeclared_order_row` and the rest of 12.2), and every incident of severity S1 (6.12) | harness on-call, system owner, risk owner, accountable owner, security contact | `#sentinel-safety` |
| `explorer_order_stage`, an S1 (6.8) | as `aborted_safety`, plus the Explorer owner | `#sentinel-safety` |
| UNPROVEN post-run proof (6.6) | harness on-call, system owner, accountable owner | `#sentinel-safety` |
| Fire-drill failure `harness_error (reason: drill_failed:<item>)` and safety canary miss `harness_error (reason: safety_canary_blind:<canary>)` (6.5, 10.3) | harness on-call, system owner, risk owner | accountable owner, `#sentinel-safety` |

| Alert | Trigger | Severity | Ack |
|---|---|---|---|
| dead-man | A run that reached `running` (scope manifest uploaded, `run_started` received) and then stopped reporting: no `run_completed` event by budget plus 15 min (PR), plus 30 (merge), plus 60 (RC), or by 06:00 America/New_York (Nightly); the run row is written `aborted_harness (reason: dead_man)` (10.2 Watchdog). A run that never reached `running` is never `dead_man`: a `run_queued` event with its queue position every 60 s holds it until its start deadline (PR 45 min, merge 3 h from the trigger, RC the G-N sign-off time minus 8 h, 11.2), where it takes the start-deadline outcome of 11.2 (PR run `aborted_budget (reason: pool_saturated)`, merge run coalesced into the next merge run, RC run decided by the release manager, 9.12; a Nightly run at 02:00 or a weekend Calibration run at Saturday 02:00 `aborted_harness (reason: start_failed)`, paged under the `aborted_harness` row below, 11.2 Queue and saturation); a trigger with neither `run_started` nor `run_queued` within 15 min raises this alert as "run not started" and the run takes the same start-deadline outcome; a late PR run sets the commit status to `error`, "Sentinel did not report". Rows that arrive later replace the placeholder row and the alert closes as "late arrival" with the delay in run health | P1 (P2 for PR runs) | 15 min; root cause same day |
| env_error | Any gate failure; run ends `complete_with_env_errors` or aborted; the same gate in two consecutive runs escalates. `env_error (gate: settings_attested, reason: attestation_unreadable)` and `env_error (gate: paper_mode_asserted, reason: identity_unreadable)` (6.3, 6.4) follow the same escalation, and two consecutive unreadable identity reads block the login until its owner reads the selector by hand | P2, P1 on repeat | Next business morning; same day if an RC run is blocked |
| harness_error | Any; two consecutive escalate; two `aborted_harness` in a week freeze harness changes until root-caused (1.5). `harness_error (reason: canary_path_divergence)` (10.3) pages at once | P2, P1 on repeat | Same day |
| aborted_harness | `aborted_harness` with any reason (`dead_man`, `start_failed` (a Nightly or Calibration run not at `running` by its start deadline, with the "canaries pending" banner on the next PR comment, 11.2), `hook_lost`, `watchdog_dead`, `disk_full`, `preflight.<item>` and the rest of 1.10's list; 10.1, 10.2) | P1 to harness on-call | Immediate; root cause same day |
| aborted_safety, working order at run start | `aborted_safety` with its reason (`unexpected_working_order` at run start, `attempted_send`, `kill_switch`, `drill_order_placed`, `unknown_dialog_in_order_stage`, 12.2); incident procedure in 6.12; rigs stay stopped until it closes | P1 per the safety routing above | Immediate |
| Fire drill failure | A drill item the gateway did not block with no new order row: `harness_error (reason: drill_failed:<item>)`, the flows that use that path `not_run (reason: quarantine)` until the drill passes, the Nightly run stays `complete`. A new Working or Filled row: run `aborted_safety (reason: drill_order_placed)`, login blocked (6.5 Nightly fire drill and safety canaries) | P1 per the safety routing above (`drill_order_placed` takes the `aborted_safety` row) | Immediate |
| Gateway safety drop | `blocked_by_gateway` with a safety reason: a blocked key, or a click on a deny rect while `order_stage` is set (6.2); the flow is quarantined (10.5) | P1 to harness on-call and the flow owner | Immediate |
| Explorer order stage | Finding `explorer_order_stage`: the ticket or dialog flag was set in an Explorer session; all input dropped, the window closed by the gateway's recovery path, the session ended, counted toward the per-run trip of 10 drops, the run continues (6.8) | P1, incident severity S1 (6.12), per the safety routing above | Immediate |
| UNPROVEN post-run proof | Proof verdict UNPROVEN (6.6): run `complete_with_env_errors`, the next run on that login refused until a human verifies Working, Filled and Position Statement by hand and records it on the incident row | P1 per the safety routing above | Immediate; verification before the login is released |
| Report withheld | Plaintext secret found by the post-run scan (6.9, 8.9): run `aborted_harness`, artifacts quarantined | P1 to security contact and system owner | Immediate |
| New high-confidence collateral | Tier-1 `collateral` at 0.80 or above, or any tier-1 `invariant_violation` or `assertion_failed`, with no matching decision | P2 to PR author and triage | One business day |
| needs_human daily digest | 08:00 America/New_York on business days: counts by tier and age, oldest item, items past SLA, escalations, pending proposals, decisions without tickets, abandoned outbox jobs, queue link | P3 | One business day |
| SLA escalation | A tier-1 Unexpected item one business day past its SLA, or a two-person proposal unanswered for one business day: to the system owner naming the rotation member; at three business days the tier's flow owner is added; an untriaged tier-1 item at RC sign-off blocks the sign-off (9.12) | P2 | Same day |
| Decision conflict | Conflicting active decisions matched one finding, or a supersession was contested (9.7) | P3; P2 if tier 1 | One business day |
| Canary failure | Money-cell mutant missed: canary state `detector_blind`, `detector_state` in `system_state` set, no verdict trusted, the 10.3 banner on every report and PR comment until a Nightly passes, RC sign-off blocked; the Nightly run itself stays `complete`. No canary generated for a critical checkpoint or for the stage: state `generation_failed`, same banner ("canaries did not run"); the run is `aborted_harness (reason: canary_generation_failed)` only when no canary could run at all. A class below its rolling floor: state `detector_degraded`, banner names the class. A real pair re-evaluated through the canary entry point that does not reproduce: `harness_error (reason: canary_path_divergence)` with the detector-blind banner | P1 for `detector_blind`, `generation_failed` and `canary_path_divergence`; P2 for `detector_degraded` | Fix or declare before the next merge run |
| A/A drift | `harness_error (reason: aa_drift_global)` on the Nightly run (10.6): thresholds invalid until a Calibration run passes | P2 | Before the next merge run |
| Reviewer backlog | Pending review items older than 4 hours on a merge run, older than 1 hour on a PR run in blocking mode, or any at RC report time | P2 | 4 hours |
| Reviewer disabled or unavailable | Eval gate failed on the deployed tuple (10.4): "Reviewer disabled since {date}: eval gate {name} failed", every declared-surface diff `needs_human (reason: reviewer_unavailable)` with `review_state: pending`; or `reviewer_unavailable` on more than 2 percent of a run's reviewable items | P2 | Before the next merge run |
| Pool saturated | PR queue depth above 3, or a PR run waiting over 20 min outside an RC reservation; `aborted_budget (reason: pool_saturated)` at the start deadline (11.2) | P2, one per hour | Same day; two weeks above 70 percent utilization grow the pool (11.10 R15) |
| Global rendering shift | Run-level `global_rendering_shift` finding (3.7, 5.8): one `needs_human (reason: global_shift)` cluster, reviewer queue stopped for the run; on the Nightly run it is `harness_error (reason: aa_drift_global)` (10.6) | P2 to on-call and the harness lead | Before the next merge run |
| Redaction miss | `redaction_miss` finding on the candidate side only, a suspected identifier leak, tier 1 in Unexpected (3.6, 6.10) | P1 to security contact and flow owner | Immediate |
| Integration outage | An outbox job failing for 15 minutes: PR comment or commit status, Jira, Slack or paging; the alert goes to the channels still working; an unposted PR comment also sets the commit status `error` when that API answers | P2 | Same day; the job keeps retrying to 24 hours |
| Quarantine ceiling | Above 2 percent of tier-1 checkpoints quarantined (10.5): Run health red, RC gate blocked | P2 to system owner | Before the RC run |
| Nightly skipped for RC | An RC run pre-empted the Nightly run (10.9, 11.2); its canaries, fire drill and safety canaries run in the next free window, and until they pass every report and PR comment carries the banner "canaries pending: last Nightly skipped for RC run {run_id}" | P2 to harness on-call and system owner | Before the next merge run |
| RC start deadline | An RC run that cannot start by the RC start deadline (G-N sign-off time minus 8 hours, 11.2), or an RC run superseded by a newer RC build (`aborted_budget (reason: superseded)`) | P2 to release manager and system owner | Same day; the release manager decides (9.12) |
| Coverage loss | Checkpoints `not_run (reason: budget)`, including masked_live checkpoints skipped because no rig pair was free (11.3), or `not_run (reason: model_unavailable)` (10.7): counts in Run health for the harness on-call; a day with over 5 percent of masked_live checkpoints starved posts P2 (11.10 R22). `not_run (reason: entitlement)` cells raise no alert: they go to the system owner's weekly coverage review (9.12) | P3; P2 above the R22 threshold | Harness on-call by the next business day; entitlement at the weekly coverage review |
| Preflight failures and cost alarm | Model preflight N2 or N5 fails (degraded mode: agent fallback and the Explorer disabled, review deferred, report `unreviewed`; 10.7), deprecation notice for the pinned model (migration ticket to the system owner), masked area above the ceiling on a critical panel, model spend 2x week over week or a cap hit | P2 | Before next run; same day for cost |
| Store health | Backup job failed (`last_backup_error` set), backup older than 36 hours, integrity check failure, restore drill older than 100 days, disk below the preflight threshold (8.6 Retention and backup, 8.9) | P2 | Same day |

Message format, all channels: `[Sentinel] {severity} {alert} | {run_type} {run_id} | {build_pair} | {outcome or gate} | {surface and tier, if any} | {one line of detail} | {report link} | {crop link, if any}`. Digest format: one line per tier with count, oldest age and items past SLA, then the ten oldest items in the message format, then escalations, pending proposals, decisions without tickets and abandoned outbox jobs. Acknowledgements are recorded in the paging service and mirrored into the run's summary JSON through the writer, so the Alerts panel shows who acknowledged what and when.

### 9.11 Metrics dashboard and kill criteria

All metrics come from SQLite over a selectable window (default since the last release), exclude synthetic items, print in every report header, and are stamped with harness commit, config commit, model id and prompt hash range so a shift can be attributed. The table in 1.5 Success criteria and kill criteria is the authoritative statement of targets and kill criteria and is repeated here unchanged. Targets are provisional until two release cycles have produced data; kill criteria are not.

| Metric | Formula | Target (Phase 1 exit / Phase 2 exit) | Kill or fix criterion |
|---|---|---|---|
| False-positive rate | Findings dispositioned noise or expected over all findings shown to a human, per run and rolling 30 days; mutants and suppressed findings excluded. | Under 35 / under 20 percent | Above 50 percent after three releases: PR comments stop, fix, re-enter pilot. |
| Flake rate | Checkpoints whose verdict flips between runs of one build or between a run and its targeted re-run, over checkpoints run; the per-checkpoint flake score drives quarantine at 0.10 over the last 5 observations of the same build (10.5 Flake tracking, targeted re-runs and quarantine). | Under 5 / under 2 percent | Above 10 percent for two consecutive releases after a fix list: no new flows until under target. |
| Quarantine share | Tier-1 checkpoints currently `not_run (reason: quarantine)` over registered tier-1 checkpoints. | Under 2 percent at every RC cut | Above 2 percent: run health red and the RC gate blocks until flow owners clear rows. |
| Time to triage | Human minutes from report publish to every Unexpected finding dispositioned (capped at 10 minutes per finding); p90 latency to first decision shown beside it. | PR under 15 / 10 minutes; merge under 30 / 20 per morning; RC under 2 hours | Above 2x target for two releases: cut tier-3 scope. |
| Escape rate | Production bugs on a registered surface that a checkpoint existed for and did not flag, per release; each is a root-caused incident classed covered-missed, not run, not registered or not detectable. | Measured / zero on tier 1 | Two consecutive releases with a covered tier-1 escape: system owner review of whether to continue. |
| Coverage per tier | Covered cells over registered cells per tier (2.12 Coverage accounting and the matrix); `not_run` never counts as covered. | Tier 1 100 and tier 2 70 percent / tier 1 100, tier 2 90, tier 3 60 percent | Tier 1 below 100 percent in a merge run blocks that release's RC gate. |
| Canary pass rate | Detected over injected mutants, per class, nightly. | Money-cell 100 percent, others measured / money-cell 100, others at least 95 percent rolling | Any money-cell miss: canary state `detector_blind`, P1 page and the banner on every report; no verdict is trusted until the next green canary. This is whole-pipeline detection (diff, invariants, Oracle, Reviewer), distinct from the Reviewer's own eval gate in the Reviewer quality row. |
| A/A noise floor | Pixels over tolerance and diff components between two captures of one build (3.9 A/A calibration and threshold derivation). | Zero on frozen checkpoints; masked_live per-region floors recorded | A non-zero frozen floor is a flow or environment bug with a fix list, never a raised threshold; unfixed after 5 business days it quarantines the checkpoint. |
| Reviewer quality | Precision and recall per category on the labeled eval set (300 to 500 items, 3.14 Eval set, metrics and gates) per model and prompt version; calibration error; human-overturn rate. | Not required (zero model calls) / `intended` precision at least 0.95, `does_not_match_manifest` recall at least 0.98 on tier-1 eval-set mutants and at least 0.95 overall, per model and prompt version (3.14); this eval gate measures the Reviewer alone and is distinct from the Canary pass rate row, where the whole pipeline must detect 100 percent of money-cell mutants or the detector is blind | `intended` precision below 0.85: `intended` disabled, declared-surface diffs go to `needs_human (reason: reviewer_unavailable)` until a new eval passes. |
| Harness health | `aborted_harness` and `aborted_budget` share of runs; `env_error` share of checkpoints; `passed_via_agent_fallback` steps over steps; cost per run (rig minutes plus model dollars). | Under 10 percent of runs and 3 percent of checkpoints / under 5 and 1 percent | Every `aborted_harness` and every `aborted_safety` pages harness on-call immediately (P1); two `aborted_harness` in a week freeze harness changes until root-caused. |

Pilot exit (end of Phase 2): two release cycles; false-positive rate under 20 percent; at least one real pre-release catch on an undeclared registered surface; triage within budget; canaries at target every night of the last cycle. The Phase 1 exit test is the worked example in 1.11 run as 10.10 End-to-end acceptance test: the Flexible Grid change, injected as a mutant and as a real branch, caught in a PR run and reported as `collateral` with the primitive FlexibleGrid named.

How each metric is computed from 8.3, so two people get the same number:

| Metric | Source rows | Counting rules |
|---|---|---|
| False-positive rate | `findings` with kind in the Unexpected set, `synthetic = 0`, created in the window, excluding findings that inherited a decision (a human never saw them) | Numerator: `triage_state` in noise, expected_behavior, agent_mistake, or intended on a finding that was listed in Unexpected (an undeclared intended change is an "expected" disposition). Denominator: every such finding, open items included until disposed. The panel also prints the numerator split into detector noise and undeclared intent, so the team can see which one to fix |
| Flake rate | `flake_stats`, the `rerun_verdict` and `stability` columns of `outcomes`, Nightly A/A rows | Unstable targeted re-runs plus verdict flips between two runs of one build over compared checkpoints; the per-checkpoint flake score drives quarantine (10.5) |
| Time to triage | `created_at` on `findings` to the decision or proposal confirmation time, per finding, capped at 10 minutes each and summed per run; p90 latency to first decision shown beside it | Business hours only; proposals count from proposal time |
| Escape rate | Escape rows entered by the triage rotation at G+1 (10.9), each naming the checkpoint that should have shown it and a class: covered-missed, not run, not registered, not detectable (2.13) | Covered-missed on tier 1 is the kill-criterion count |
| Coverage per tier | `coverage_cells` for merge, RC and Nightly runs; the matrix in the run's `summary_json` for PR runs | 2.12 Coverage accounting and the matrix: not_run (entitlement) and scope-by-persona cells leave the denominator; denominators print |
| Canary pass rate | `canary_results` per night, class and `money_cell` | Detected over injected; the rolling floor is 10.3's |
| A/A noise floor | `calibration_floors` for the current fingerprint; Nightly A/A outcomes | `nonzero_on_frozen` rows are the fix list |
| Reviewer quality | `reviewer_evals` per model id and prompt hash; `decisions` revoked over made for the overturn rate | Held-out split only (3.14) |
| Quarantine share | `flake_stats.quarantined` mirrored from `quarantine.yaml` at the run's config commit, tier-1 rows of `checkpoints` | Registered tier-1 checkpoints in the denominator; evaluated at every RC cut (10.5) |
| Harness health | `runs.outcome`; `outcomes.outcome = 'env_error'` over outcomes; `step_events` for `passed_via_agent_fallback` over steps; reviewer usage blocks and rig minutes for cost per run | Per run type and overall |

Operational panels, printed in Run health and on the Metrics page but not success criteria: triage SLA compliance per item type; suppressed findings and distinct suppressing decisions; masked area per critical panel week over week (4.8 budget); quarantined share of tier-1 checkpoints per run between RC cuts (the Quarantine share row above, 10.5); fallback rate per flow; the `rerun_budget_exhausted` count per run, which feeds the sharding review (an exhausted re-run budget leaves `stability: unknown (cause: budget)` on the outcome row and is never a `needs_human` reason, 3.10); manifest entries verified over entries; decisions without tickets; abandoned outbox jobs; Explorer precision and agent-error ratio per RC run; gateway drops per run by `gate_reason` and `gate_class`, counted as `step_events` rows with `gate_decision IN ('dropped','refused')` (dropped by the hook, refused by the slow half; 8.3), which is also the number the Safety row of 9.2 prints; reviewer and rig cost per run (11.4 Cost model); p50 and p95 step time; human-overturn rate per verdict.

### 9.12 Triage SLA and roles

Roles and their weekly hours are set in 11.5 Maintenance model and 11.6 On-call and ownership; this section states what each does in triage.

| Role | Responsibilities here |
|---|---|
| System owner | Configuration of this section, commit status mode and overrides, auto-filing switches, RC sign-off with the release manager, kill criteria reporting, the only decider on a frozen signature (9.7), SLA escalation target, the weekly coverage review (below), mask budget changes by pull request to `sentinel-config` (9.6) |
| Accountable owner | Receives every S1 page and every UNPROVEN proof; verifies the account by hand before a blocked login is released (6.6, 6.12) |
| Triage rotation (one person per week) | Opens every report by 10:00 the next business day; disposes `needs_human` and `collateral` within one business day; confirms or declines tier 1 and 2 intended proposals within 4 business hours; decides mask requests; dispositions Explorer findings; resolves decision conflicts |
| Flow owner per tier | Approves baselines and waivers for the tier, reviews tier-1 masks, owns anchor patches, promotes Explorer candidate flows, owns quarantined checkpoints and repairs within five business days |
| Harness on-call (weekly roster) | Answers pages: dead-man, `harness_error`, `aborted_harness`, `aborted_safety`, gateway safety drops, `explorer_order_stage`, UNPROVEN proof; responds to `env_error`, store health and integration outages in the on-call channel; owns the run-health counts of `not_run (reason: budget)` (masked_live checkpoints with no free rig pair included) and `not_run (reason: model_unavailable)`, and the skipped-Nightly catch-up (10.9) |
| Release manager | RC sign-off with the system owner, release gate, accepted open findings at release; decides what happens when an RC run cannot start by the RC start deadline (G-N sign-off time minus 8 hours, 11.2): move GA, record an exception that signs on the last complete RC run, or accept a shortened scope printed in the release gate checklist |
| Risk owner | Receives `aborted_safety`; owns the send policy decision (6.7 Send policy: both branches) |
| Explorer owner | Owns the deny list and Explorer budgets (6.8); receives the Explorer precision and agent-error metrics, every `explorer_order_stage` finding (S1) and the "Explorer: not run, model endpoint unavailable" notice (10.7) |
| PR author | Triages their own PR's Unexpected section before merge: confirms bugs, requests masks, proposes intended for tier 1 and 2, marks intended alone for tier 3 |

| Item | SLA |
|---|---|
| PR run Unexpected, tier 1 | Decision before merge (enforced by the commit status in blocking mode, shown in informational mode); the triage rotation answers a pending intended proposal within 4 business hours |
| PR run Unexpected, tier 2 | Proposal answered within 4 business hours; decision within one business day; never blocks merge |
| PR run Unexpected, tier 3 | One business day; never blocks merge |
| Merge run findings and `needs_human` | Report opened by 10:00 the next business day; every finding disposed within one business day |
| RC run, all findings | Triaged and signed off N business days before GA (N is PA-14 in 11.1, default 3 business days, per train in `policy/release_gate.yaml`) |
| Alerts | Acknowledgement per 9.10; root cause ticket the same day |
| Expired masks and noise signatures | Re-evaluated within 5 business days of expiry; masks reviewed weekly against the per-panel budget |
| Decisions without tickets, abandoned outbox jobs | Closed by hand within one business day of appearing in the digest |
| Weekly coverage review | Every week, by the system owner |

Escalation, when an SLA is missed:

| Step | When | What happens |
|---|---|---|
| 1 | Item unclaimed for 4 business hours | Assigned to the week's rotation member; shown in the digest |
| 2 | One business day past SLA | Tier 1: P2 to the system owner naming the rotation member (9.10). Tier 2 and 3: digest only |
| 3 | Three business days past SLA | The tier's flow owner is added to the alert; the item heads the merge digest |
| 4 | Five business days past SLA, or any untriaged tier-1 item at RC sign-off | Listed in the weekly metrics summary as an SLA breach; RC sign-off refused until the item has a decision (10.9) |

Breaches appear in the daily digest and on the Metrics page as SLA compliance per item type.

Weekly coverage review. Once a week the system owner opens the coverage matrix (9.5) on the Metrics page for the last merge, RC and Nightly runs and reviews every cell that was not covered for a reason a triage decision cannot fix: `not_run (reason: entitlement)` cells (a login lacks the entitlement, or a post-Send surface waits on `paper_send_ok`), quarantined checkpoints past their five business days, and the week's `not_run (reason: budget)` and `not_run (reason: model_unavailable)` totals that the harness on-call reported from run health. For each entitlement gap the owner either asks the accounts owner for a login that has the entitlement or records the gap, with a ticket, in the matrix's "Not detectable by design" table (2.13); for budget and model gaps the owner opens a sharding or capacity task (11.2, 11.10 R15 and R22). The review's outcome is a line in the weekly metrics summary.
