## 4. Inputs and schemas

Everything Sentinel reads or writes is one of the record types this section defines. The input records of 4.2 to 4.12 and the safety and operations files of 4.16 are files in the `sentinel-config` git repository and are validated by `sentinel lint` in CI on every pull request and again by the Orchestrator at run start. The outcome record, the finding record and the decision record (4.13, 4.14) are rows in the SQLite store (see 8.3 Schema) with a JSON export form; they are validated on write. This section gives each record's schema, one worked example, its validation rules and the lint rules that enforce them. Component behaviour that consumes these records lives in sections 2 (What is tested and how), 3 (Comparison, classification and the reviewer) and 5 (Architecture and components); this section only states what a valid input is and what happens when one is invalid.

### 4.1 Conventions, repository layout and the lint

**Repository layout.** Two repositories. `sentinel-config` holds everything below: what a run is asked to do. `sentinel` holds the harness code, the lint binary and the generated JSON Schema files (`schemas/*.schema.json`, draft 2020-12, one per record type, generated from the notation below), and is pinned separately as the run's harness commit (8.5 What lives in git and how a run pins it). Every run records both commits in its environment fingerprint (4.12). The change manifest for a PR run is the one exception to this layout: it is authored in the product repository, at `.sentinel/manifest.yaml` on the branch, and copied into the run as described in 4.3.

| Path | Record type | Consumed by |
|---|---|---|
| `taxonomy.yaml` | every closed vocabulary: the outcome enums of brief 1.5 verbatim plus reasons, gates, finding kinds, triage states, decision actions and verdicts, canary states and kinds, reviewer verdicts, quarantine reasons, money roles and popup actions (table below) | every schema that carries one of them, Reporter |
| `builds/*.yaml` | build identity and build pair | Orchestrator, Rig Manager |
| `manifests/<build_id>.yaml`, `manifests/mocks/` | change manifest | scope computation, Reviewer, Reporter |
| `registry/dependency-map.yaml` | dependency map | scope computation, Reporter |
| `registry/surfaces/<area>/*.yaml` | surface registry entries | census, Executor, registry validation |
| `registry/ladders.yaml` | size-sweep ladders F and S and the splitter sets (semantics in 2.5.2 The ladders) | size sweeps |
| `corpus/workspaces/<id>/` | workspace corpus entry plus the workspace file | Rig Manager, census, size sweeps |
| `corpus/measured_sizes.yaml` | each gadget's size in every tier A and B workspace hosting it, measured by probe P8 on the first validated run and written by the Reporter's registry pull request; the corpus sizes of Ladder F (2.5.2). Owner: the registry owner. Until a surface has sizes here, its Ladder F corpus steps are `not_run (reason: scope)` with a run-health line (4.16) | scope computation, size sweeps |
| `corpus/odd-inputs.yaml` | the versioned odd-input corpus (symbol box, order fields, Analyze inputs, study parameters) with its sampling seed; `type_fixture` steps reference entries by id | Executor, Explorer |
| `configs/allowed.yaml` | the display configurations a workspace may declare, each with its work area, which gives MAXW and MAXH of Ladder F (2.5.2); phase 1: 1920x1080 at 100 percent only (schema in 4.6) | lint, size sweeps |
| `palettes/<theme>.yaml` | the pinned INV-15 palette for one theme, selected by the workspace's `display_config.theme` (2.7). Owner: the registry owner (4.16) | Invariant Checker |
| `flows/<tier>/*.yaml`, `flows/RENAMES.yaml` | flow files, checkpoint rename map | Executor, Oracle |
| `flows/quarantine/<finding-id>.yaml` | Explorer candidate flows (`class: explorer_candidate`, 2.9) until a human promotes them | Executor (replay), lint |
| `regions/<checkpoint_id>.yaml` | region registry; the checkpoint id is a directory path because it contains slashes (4.7) | Diff Engine, Oracle, Capture |
| `masks/<checkpoint_id>/*.yaml` | mask records | Diff Engine |
| `fixtures/*.yaml`, `profiles/*.yaml` | account and data fixture, pinned settings profiles | fixture reset flow, attestation |
| `fixtures/margin_rules.yaml` | the per-asset-class buying power brackets of rule R02 (2.8.3), pinned in Phase 0 (schema in 4.9) | Oracle |
| `gateway/allowlist.yaml` | the exact-match paperMoney account allowlist per rig, compiled into the gateway hook host (4.9) | lint, Input Gateway, paper-mode assertion |
| `gateway/default_order_hotkeys.yaml` | the pinned build's default order-action chords, read once in Phase 0; the blocked chord set and fire drill item 3 use it (5.4.7, 6.5). Owner: the system owner (4.16) | Input Gateway, fire drill |
| `gateway/dismiss_allowlist.yaml` | the pinned dismiss allowlist of 6.2: the only labels clickable while an order dialog is in front (`Delete`, plus `Edit` only when Phase 0 observes it, 6.13 item 10). Owner: the system owner (4.16) | Input Gateway |
| `redaction/anchors.yaml` | the one global redaction list of 6.10 Redaction pipeline and egress log, owned by the security contact: the regions-file shape of 4.8 with landmarks and regions of `type: redaction`, each with `identity_field`, applied wherever its landmark is found | Capture |
| `popups/catalog.yaml` | popup catalog: `schema_version` and `entries` (4.10) | Executor sentinel, Watchdog |
| `canaries/mutants.yaml` | the one mutant catalog, `schema_version` and `entries`: ids M01 to M33 exactly as 12.5 tables them, then M34 onward in sequence, classes of 4.11, seed 20260101, M25 the identical control | Canary Service |
| `soak.yaml` | soak resource budgets per metric, calibrated from the lineage soak curve (10.11; schema in 4.16) | Watchdog, Reporter |
| `calibration/<hex>.yaml` | per-region A/A floors and thresholds for one calibration key, `<hex>` being the key without its `sha256:` prefix (4.12 Calibration key); exported by the Calibration Service from `calibration_floors` (8.3), read-only for humans (4.16) | Diff Engine |
| `diff_engine.yaml` | the Diff Engine parameters of 3.7 Three-granularity diff; any change re-runs the canary catalog before the next product run | Diff Engine, Canary Service |
| `quarantine.yaml` | quarantine records, each naming exactly one checkpoint or one flow, with owner, ticket, entered date and reason (10.5 Flake tracking, targeted re-runs and quarantine), opened by the Orchestrator as a pull request and merged by the system owner (4.16) | scope computation, Reporter |
| `policy/send_policy.yaml` | the OPEN `paper_send_ok` switch, signed by the risk owner | lint, Input Gateway |
| `policy/explorer_budget.yaml` | Explorer session budgets: `max_actions: 400`, `max_minutes: 90`, `max_no_change_actions: 20`, `max_usd_per_session: 25`, `max_usd_per_week` (6.8 Explorer boundary and deny list); a change needs the Explorer owner's approval in the PR | Orchestrator |
| `policy/explorer_deny_list.yaml` | the Explorer deny list of 6.8: window titles, labels and landmark-anchored controls. Owner: the system owner; a change needs the Explorer owner's approval (6.8, 4.16) | Input Gateway, nightly denied-action test, lint |
| `policy/egress_allowlist.yaml` | the signed egress allowlist of 6.10: the only hosts the harness HTTP client may reach. Owner: the system owner; signed by the security contact (4.16) | harness HTTP client, Nightly egress reconciliation |
| `policy/computer_use_ruling.yaml` | the current computer-use ruling of 6.11 (endpoint, retention terms, approved model tiers, whether computer use is covered), read and validated at preflight G1. Owner: the system owner; signed by the security contact (4.16) | preflight, Orchestrator |
| `policy/budgets.yaml` | the hard caps of 11.4: reviewer items per run type, agent rescue limits and the three dollar caps (4.16) | Orchestrator, Reviewer, Reporter |
| `policy/release_gate.yaml` | N business days before GA per release train, default 3, with GA and RC cut dates per train (11.2, 10.9; 4.16) | Reporter, release gate |
| `policy/reporting.yaml` | the Reporter's settings: commit status mode history, Jira project keys, Slack channels, paging and email targets, auto-filing switches per tier (9.9; 4.16) | Reporter |
| `authorization/<version>.md`, `authorization/<version>.yaml` | the signed authorization of 6.12 as a document and as its machine-readable record, signed by the platform owner, the security contact and the compliance contact; every run manifest carries the reference and sha256 of the version in force (L-XREF-16, 4.16) | preflight, lint |
| `schedule/windows.yaml` | the operating day, the named quiet windows and the maintenance windows of 7.10 (4.16) | Orchestrator scheduler, preflight |
| `ops/oncall.yaml` | the role holders and deputies, the triage roster and the triage and harness on-call rotations (6.12, 9.12, 11.6); L-MSK-01, L-DEC-01 and L-DEC-06 read the roster (4.16) | Reporter, alerting, lint |
| `ops/credential_rotation.yaml` | the credential rotation calendar of 7.7: login owners and, per rotation, the window, the owner and the validating run id (4.16) | Orchestrator (run hold), Reporter |
| `rig/process_allowlist.yaml` | the running-process allowlist of 7.1: the harness processes, the elevated helper service, the thinkorswim tree and the golden image's Windows baseline (4.16) | Rig Manager |
| `rig/thinkorswim.vmoptions.template` | the checked-in vmoptions template of 7.3 step 4 (pipeline flags, heap, crash flags); the Rig Manager writes `thinkorswim.vmoptions` from it and B3 and H10 compare the hash (L-XREF-28) | Rig Manager, preflight |
| `phase0/branches.yaml` | the chosen branch for each of the seven unknowns of brief 1.11 (for example `jab: A`), plus the Phase 0 reads other files depend on (4.16) | lint, Executor |
| `eval/<eval_set_version>/labels.jsonl`, `eval/reports/<hex>.yaml` | reviewer eval labels (10.4), and one eval report per reviewer version tuple of 3.15, `<hex>` being the tuple hash without its prefix (4.16) | Reviewer change control, lint |
| `model_deprecations.yaml` | vendor deprecation notices for the pinned model ids (10.7 Degraded modes) | deprecation alarm |
| `CODEOWNERS` | routes `flows/` and `registry/` to the tier's flow owner and each file of 4.16 to the owner its row names (11.5, L-XREF-26) | the `sentinel-config` pull request check |

**Common conventions.**

- Identifiers. Top-level record ids (surface, workspace, fixture, flow, manifest, manifest entry, mask, and the `name` slugs of popup and mutant entries) are unique across the whole repository and match `^[a-z0-9][a-z0-9.-]*$` (lower case, digits, dots and hyphens, never underscores). Ids local to a flow (step ids and assertion ids) are unique within their flow and may contain underscores; a checkpoint step's id is also the last segment of its checkpoint id, so it follows the grammar below. Region ids are unique within their regions file, so two regions files may reuse one id (the `rgn.confirm.*` ids of 4.8 serve the stock and futures flows of 4.7). Ids local to one other file (fixture symbol and price ids, control and landmark ids within a surface) are unique within that file and may contain underscores. Surface ids follow `<area>.<surface>[.<variant>]`. Two exceptions: primitive ids use the PascalCase class name (`FlexibleGrid`); popup and mutant catalog entries are keyed by the appendix ids `POP-nn` and `Mnn` (4.10, 4.11).
- Checkpoint ids are the one identifier that contains slashes. The grammar is `<kind>/<surface-id>/<qualifier>` with kind one of `census`, `sweep`, `split`, `flow`:

  | kind | qualifier | regex | example |
  |---|---|---|---|
  | `census` | `<workspace-id>` followed by an optional `/<dim>=<value>[,<dim>=<value>]` segment for surfaces the census repeats per declared dimension (2.4) | `^census/<slug>/<slug>(?:/[a-z]+=[A-Za-z0-9.-]+(?:,[a-z]+=[A-Za-z0-9.-]+)*)?$` | `census/trade.all-products.option-chain/ws.tier-a.options-trader/layout=delta,strikes=16` |
  | `sweep` | `<host>/<w>x<h>`, or `<host>/<w>x<h>-asc` for a step of Ladder F's ascending pass (2.5.2), with host from `flexgrid`, `detached`, `dialog` | `^sweep/<slug>/(flexgrid\|detached\|dialog)/[0-9]+x[0-9]+(-asc)?$` | `sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360`, `sweep/trade.active-trader.ladder/flexgrid/640x480-asc` |
  | `split` | `<splitter>/<fraction>` with the fraction as two decimals | `^split/<slug>/<slug>/(0\|1)\.[0-9]{2}$` | `split/trade.active-trader.ladder/side-panel/0.15` |
  | `flow` | `<flow-id>/<step-id>` where the flow id is the file's full `flow_id` including its `flow.` prefix and the step id is the checkpoint step's own `id` | `^flow/<slug>/<slug>/[a-z][a-z0-9-]*$` | `flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2` |

  `<slug>` stands for the top-level identifier regex above. Tab and sidebar hosts have no `sweep` ids: they are swept through their splitters only, as `split` checkpoints (2.5.1). The id is a stable name for a screen and a state; the step id inside a `flow` checkpoint id is part of the name, so renaming that step is a checkpoint rename and needs a `flows/RENAMES.yaml` entry (L-FLW-03), which is how baselines survive flow edits. Where a checkpoint id is used as a file path, each slash is a directory.
- Timestamps are RFC 3339 with an explicit offset. Dates are `YYYY-MM-DD`. Hashes carry an algorithm prefix (`sha256:`).
- Every file carries `schema_version: int`. A schema change that is not backward compatible bumps the version and ships a migration under `schemas/migrations/` in the `sentinel` repository; lint refuses a file whose version the current harness does not know.
- Paths are repository relative and must resolve. Images are PNG. Templates (landmarks, controls, popups) are between 16x16 and 400x400 pixels; mocks are at most 1920x1080 and 5 MB.
- No secret value appears in any file. Secrets are vault references of the form `vault://<path>#<field>`; lint rejects any string that matches a credential pattern (see L-XREF-07).

**Schema notation.** Each schema below is a YAML block in which a key maps to a type expression: `str`, `int`, `num`, `bool`, `ts`, `date`, `sha256`, `path`, `id`, `enum[a|b]`, `list[T]`, `map[K,V]`, `ref:<record>.<field>` for a cross-file reference, a trailing `?` for optional, `= x` for a default. Nested objects are indented. The normative artifacts are the generated JSON Schema files in the `sentinel` repository; the notation is the readable form and the two are kept in sync by a CI job (`sentinel schemas --check`).

**The lint.** `sentinel lint` validates every file against its schema, then runs the cross-file rules of 4.15. Rule ids are `L-<FILE>-<nn>` with severity `error` (blocks the PR) or `warn` (review comment). A rule marked `error (run start)` is checked only against live state at run start. At run start the Orchestrator runs the whole lint before the `provisioned` gate; any `error` ends the run as `aborted_harness (reason: config_invalid)`, with the rule ids and file paths posted to the on-call channel and the run row written so the dead-man timer is satisfied. A lint failure never reaches the Diff Engine. Rules whose failure must not stop a run (an expired mask, a stale price) say so in their row and name the outcome they produce instead.

**Single source for the taxonomy.** `taxonomy.yaml` holds every closed vocabulary the system uses, and every schema that carries one of these values imports it from this file:

| Key | Contents | Source of truth |
|---|---|---|
| `checkpoint_outcomes`, `step_outcomes`, `run_outcomes` | the 15, 5 and 5 names of brief 1.5 | 1.10 Outcome taxonomy; 12.2 is the verbatim copy |
| `reasons.<outcome>` | the reason codes allowed with each outcome, as listed in 4.13, each with its owning section and what `detail` holds for it | this section; the owning section and `detail` meaning come from the `outcome_reasons` seed of 8.3, which 12.2 copies |
| `gates` | the Orchestrator states `provisioned`, `launched`, `authenticated`, `paper_mode_asserted`, `settings_attested`, `workspace_loaded`, `fixture_verified`, `canary_flow_passed`, `running`, `teardown`, `reported`; the preflight gates `preflight.<item id>` for every item of 12.7 Preflight checklist; the mid-run gates `connectivity`, `rig_lost` (there is no `soak` gate: a freeze or crash during soak is the app failure trio of 4.7, 10.11) | 5.2 Orchestrator, 10.7 Degraded modes |
| `finding_kinds` | the finding vocabulary of 4.13 | this section; 8.3 `findings.kind` is generated from it |
| `triage_states` | `open`, `confirmed_bug`, `intended`, `noise`, `agent_mistake`, `expected_behavior`, `fixed_pending_verify`, `closed` | 8.3 Schema |
| `decision_actions`, `decision_verdicts` | the UI actions and the stored verdicts of 4.14 | this section; 9.6 shows the same mapping |
| `canary_states` | `detected`, `missed`, `detector_blind`, `detector_degraded`, `generation_failed`; result states, never run outcomes (the Nightly run stays `complete`) | 10.3 Canary Service and the mutant catalog |
| `canary_kinds` | `mutant`, `safety_canary`, `fire_drill`, `path_equivalence`: the kinds of a `canary_results` row; for `mutant` the defect class is one of the 26 classes of 4.11, for the others it equals the kind | 8.3 `canary_results`, 10.3 |
| `reviewer_verdicts` | `matches_manifest`, `partially_matches_manifest`, `does_not_match_manifest`, `no_visible_difference`, `cannot_judge`, `not_applicable`; not outcomes | 3.13 Structured output schema |
| `quarantine_reasons` | the reasons a `quarantine.yaml` record may carry | 10.5 Flake tracking, targeted re-runs and quarantine |
| `money_roles` | `qty`, `price`, `bp_effect`, `max_loss`, `send`, `cancel`, `delete`: the closed money-cell list | 4.8 `money_role` |
| `popup_actions` | `dismiss_key`, `click`, `wait`, `relogin`, `abort`, `ignore` | 4.10 `action` |

A CI test compares the three outcome lists with the taxonomy table in 1.10 and the appendix copy, and every other list with the table that owns it; a difference fails CI (L-XREF-08). The SQLite `CHECK` constraints of 8.3 are generated from this file (L-OUT-01). Nobody adds an outcome, a reason or a finding kind by editing a schema or a DDL file.

### 4.2 Build identity and build pair

A build identity names one installable artifact. A build pair names the two builds a run compares and the rule that selected the base. Attribution comes from the pair, not from the Reviewer, so the pair is validated before anything else.

```yaml
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
```

Worked example, a PR run on a Flexible Grid change:

```yaml
schema_version: 1
build:
  build_id: branch-1998.3.0-7be42d0
  channel: branch
  version_string: "1998.3.0"
  commit_sha: 7be42d0c3a9f1e4b8d2c6a0f5e9b1d3c7a2f4e6b
  branch: feature/TOS-48213-flexgrid-cell-padding
  merge_base_sha: 4f1c9a2e7d0b3c5f8a1e6d9c2b4f7a0e3d5c8b1a
  pipeline_run_id: "tos-desktop/8812"
  artifact:
    uri: artifactory://tos-desktop/branch/7be42d0/thinkorswim-win64.zip
    sha256: sha256:0c9e1b...e4a7
  install_method: artifact
  built_at: 2026-11-09T13:42:10-05:00
build_pair:
  pair_id: pr-branch-1998.3.0-7be42d0-vs-main-1998.2.0-4f1c9a2
  run_type: pr
  candidate: branch-1998.3.0-7be42d0
  base: main-1998.2.0-4f1c9a2
  base_rule: merge_base_main
  created_at: 2026-11-09T13:45:00-05:00
```

The version string in the example is a placeholder pattern. The real format is whatever the current build writes into the main window title, read with `GetWindowText`; brief 1.11 item 4 verifies that read and the install method in Phase 0.

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-BLD-01 | `build_id` unique and equal to `<channel>-<version_string>-<first 7 of commit_sha>` | error |
| L-BLD-02 | `install_method = artifact` requires `artifact.sha256`; `snapshot` requires `snapshot_id` | error |
| L-BLD-03 | `branch` builds carry `branch` and `merge_base_sha`; other channels do not | error |
| L-BLD-04 | pair rule table: `pr` requires `merge_base_main` with candidate `branch` and base `main` whose `commit_sha` equals the candidate's merge base with main, or the nearest main ancestor of that merge base that has a build record, at most 20 commits back (distance recorded as `base_commit_distance`); `merge` requires `previous_main`, both `main`; `rc` requires `last_released`, candidate `rc`, base `released`, and `lineage_baseline_set`; `nightly` and `calibration` require `same_build` | error |
| L-BLD-05 | `candidate != base` unless `base_rule = same_build` | error |
| L-BLD-06 | `released_at` present if and only if `channel = released` | error |

At run start the Orchestrator reads the title bar after launch, after every relaunch and before each flow. A read that differs from `version_string` is `env_error (gate: launched, reason: version_mismatch)` on every scoped checkpoint of the shard, posted to the on-call channel; a change during the run marks every later checkpoint `env_error (gate: launched, reason: version_changed_mid_run)` and the run ends `complete_with_env_errors`. For a PR run the base is the merge base when main has a build record for it, else the nearest main ancestor of the merge base that has one, at most 20 commits back; the distance is `base_commit_distance` in run health, and the PR manifests merged between that build and the merge base count as declared for the run. No such build within 20 commits, or a missing base artifact (the base build cannot be installed or its capture set is absent), ends the run `aborted_harness (reason: base_build_missing)` before any capture, per 1.10. The pair record itself is linted at run creation: a pair that fails L-BLD-04 or L-BLD-05 is refused at creation, the run is never queued, and the requester (the CI job or the human who asked) gets the rule id; nothing is attributed to a pair that was never valid.

### 4.3 Change manifest

The change manifest is the only source of intent. Without an entry, no diff can be `intended`. For PR runs the manifest is authored by the change owner in the product repository at `.sentinel/manifest.yaml` on the branch; the product pipeline runs `sentinel lint --manifest` as a PR check named `sentinel/manifest`, and at run creation the Orchestrator copies the file into the run as the artifact `manifests/<candidate build_id>.yaml`, hashes it into `runs.manifest_sha256` (8.3), and never reads the moving branch again. For merge runs the Orchestrator assembles the manifest as the union of the PR manifests merged since the previous main build (`source: union`); for RC runs it is the union since the last release. A build with UI-visible changes and no manifest is a process failure that the PR comment reports (see Missing manifest below). A manifest that fails the lint is treated as missing for the run and the lint output is printed in the PR comment's Intended section; the `sentinel/manifest` check fails on the PR, informational in Phase 1 and blocking from Phase 2 on the same schedule as the main check (1.5 Success criteria and kill criteria).

```yaml
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
```

`new_surface: true` is what lets a base-side failure become `needs_baseline` instead of an error: a step that still fails on the base build after its retry makes its checkpoint `needs_baseline (reason: new_surface)` only when the entry that declares the surface carries `new_surface: true`, else `not_run (reason: base_step_failed)`; either way invariants and the Oracle still run on the candidate capture and their findings are listed (2.4, 3.1 Build pair rules). `must_be_observed` drives the manifest coverage table of 2.12: an entry whose declared surfaces show no diff above the floor, or whose visibility check (3.12 task C) returns `not_visible` or `unsure`, raises the run-level finding `intended_change_not_observed` for the feature owner; a finding, never a checkpoint outcome and never a clean report.

Worked example:

```yaml
schema_version: 1
manifest_id: mf-branch-1998.3.0-7be42d0
candidate: branch-1998.3.0-7be42d0
base: main-1998.2.0-4f1c9a2
source: manual
author: jdoe
entries:
  - entry_id: tos-48213-1
    tickets: [TOS-48213]
    summary: Flexible Grid cells get 4 px inner padding so gadget headers do not touch the cell border
    declared_surfaces:
      - charts.flexible-grid
      - charts.flexible-grid.cell-header
    touched_primitives: [FlexibleGrid]
    changed_paths:
      - "desktop/ui/grid/src/main/java/**/FlexibleGridCell*.java"
      - "desktop/ui/grid/src/main/java/**/GridCellInsets.java"
    expected_visual: >
      Every Flexible Grid cell shows a 4 px gap between the cell border and the gadget header on all
      four sides. Header text, fonts and colors are unchanged. Gadgets outside a Flexible Grid are unchanged.
    mock_image: manifests/mocks/tos-48213-flexgrid-padding.png
    feature_flags:
      - {name: flexgrid.cell_padding_v2, state: on, scope: build}
    must_be_observed: true
```

This is the manifest in the brief's worked scenario: the entry declares the grid, not the Active Trader ladder. When the ladder overlaps, the ladder surface is not declared, so the diff is `collateral`, posted with the primitive `FlexibleGrid` and the entry `tos-48213-1` that touched it.

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-MAN-01 | every `declared_surfaces` id exists in the Surface Registry | error |
| L-MAN-02 | every `touched_primitives` id exists in the dependency map | error |
| L-MAN-03 | `expected_visual` has at least 20 characters and is not identical to `summary` | error |
| L-MAN-04 | `mock_image` exists, is PNG, at most 1920x1080 and 5 MB | error |
| L-MAN-05 | ticket keys match the Jira regex; at least one ticket per entry | error |
| L-MAN-06 | `entries` empty requires `empty_reason`; `approved_by` present for `merge` and `rc` manifests | error |
| L-MAN-07 | each declared surface consumes at least one touched primitive according to the dependency map, or `changed_paths` is non-empty | warn |
| L-MAN-08 | feature flag names match `^[a-z0-9_.]+$` and are unique within the entry | error |
| L-MAN-09 | at run start `candidate` equals the pair's candidate; otherwise the manifest is treated as missing and the mismatch is printed in the PR comment | error (run start) |
| L-MAN-10 | `new_surface: true` requires that at least one declared surface is absent from the base build's registry commit, or that the entry's ticket is a feature ticket; `false` on a surface the base registry lacks is an error | error |

**Missing manifest.** If a PR run finds no `.sentinel/manifest.yaml` on the branch (or the file fails the lint), the run proceeds and the `intended` category is disabled for the run. The static impact step (2.10.2) then decides between two cases (3.11 rows 17 and 18; 1.7):

| Case | Condition | Finding | Diffs above threshold | Who sees it |
|---|---|---|---|---|
| (a) UI code touched | the static impact step derives at least one surface from the diff's changed classes | `manifest_missing`, tier 1, with the derived surfaces listed | `collateral` with reason `manifest_missing`, because an undeclared UI change is by definition unexpected and the developer must declare it or fix it | PR comment header "No manifest entry; all diffs are reported as collateral until you add one", Intended section links the template |
| (b) no UI code touched | the static impact step derives no surface | none | `needs_human (reason: no_manifest)`, since a side effect from non-UI code is possible but intent cannot be checked | PR comment Unexpected, daily digest to the triage rotation |

Merge and RC runs never lack a manifest: an empty union is a manifest with `entries: []` and `empty_reason: "no PR manifests in range"`, and the Reporter prints that line. A manifest file that is present but names the wrong candidate (L-MAN-09) is case (a) or (b) by the same test.

### 4.4 Dependency map

The dependency map says which surfaces consume which hosting primitive; it turns a manifest into a run scope and a collateral diff into a named cause. Declared edges are written by humans; static-analysis edges are proposed by the static impact step and merged by a human.

```yaml
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
```

`source_packages` are the primitive's roots in the sense of 2.10.1; a surface's own roots live on its registry entry (4.5). A `glob` surface (`charts.*`) is expanded against the registry at lint time and must match at least one id; the starter list in 12.9 Dependency map starter list is written this way, with every edge `confirmed_from_source: false` until Phase 1's scan confirms it.

Worked example (the `source_packages` values are the `TBD.<area>.**` placeholders of 12.9, filled from source by the Phase 0 jar listing, 11.8; the class names in the `evidence` line show what a Phase 1 scan writes once they are filled):

```yaml
schema_version: 1
primitives:
  - id: FlexibleGrid
    description: Grid container that hosts gadgets in resizable cells with splitters
    source_packages: ["TBD.flexgrid.**"]
    owner: tos-shell-ui
  - id: ColumnModel
    description: Shared column layout and sorting for data grids
    source_packages: ["TBD.table.**"]
    owner: tos-data-ui
  - id: Theme
    description: Colors, borders and icon sets for the custom look and feel
    source_packages: ["TBD.laf.**"]
    owner: tos-shell-ui
    global: true
edges:
  - {primitive: FlexibleGrid, surface: charts.flexible-grid, source: declared, confidence: 1.0,
     first_seen: 2026-10-01T09:00:00-04:00, last_verified: 2026-11-01T09:00:00-04:00}
  - {primitive: FlexibleGrid, surface: trade.active-trader.ladder, source: declared, confidence: 1.0,
     first_seen: 2026-10-01T09:00:00-04:00, last_verified: 2026-11-01T09:00:00-04:00}
  - {primitive: ColumnModel, surface: monitor.activity.position-statement, source: static_analysis, confidence: 0.82,
     evidence: com.tos.desktop.monitor.PositionStatementTable imports com.tos.desktop.ui.table.column.ColumnModel,
     first_seen: 2026-11-02T03:10:00-05:00, last_verified: 2026-11-02T03:10:00-05:00}
  - {primitive: Theme, surface: "*", source: declared, confidence: 1.0,
     first_seen: 2026-10-01T09:00:00-04:00, last_verified: 2026-11-01T09:00:00-04:00}
```

The scope of a PR run (algorithm in 2.11 Scope computation per run type) is the union of the manifest's declared surfaces, every surface reachable from a touched primitive by an edge, and the surfaces the static impact step derives from `changed_paths`. The static impact step (2.10.2) may change the map only through a pull request, and only in these ways: add an edge with `source: static_analysis`, `confirmed_from_source: true`, `confirmed_build` and `evidence`; on an existing edge of either source, set `confirmed_from_source: true`, set `confirmed_build`, raise `confidence` (never lower it) and refresh `last_verified`. It never removes an edge; a declared edge the scan cannot see keeps `confirmed_from_source: false`, keeps its human `reason`, and ages into L-DEP-07. When the scan finds an edge the map does not have, the run emits a `dependency_map_gap` finding naming the missing edge (`detail: missing_edge`); a changed class under no primitive's and no surface's roots is a gap only when it reaches a registered surface, and is then one separate `dependency_map_gap` finding (`detail: unowned_root`, 2.11). Either way the run opens the pull request, and the map owner (the primitive's `owner`) merges or rejects it; the finding stays open in the report until the PR is closed either way.

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-DEP-01 | primitive ids unique, PascalCase; at least one `source_packages` entry each; a `TBD.` glob (the unfilled placeholder of 12.9) warns in Phase 0 and is an error from Phase 1 (11.8) | error |
| L-DEP-02 | `surface` exists in the registry, or is a glob matching at least one registry id, or is `"*"` and the primitive is `global` | error |
| L-DEP-03 | `declared` edges have `confidence = 1.0`; `static_analysis` edges have `evidence` and `0 < confidence <= 1`; `confidence = 1.0` on a `static_analysis` edge requires `confirmed_from_source: true`; `confirmed_from_source: true` requires `confirmed_build` | error |
| L-DEP-04 | no duplicate `(primitive, surface)` pair | error |
| L-DEP-05 | bidirectional consistency: every primitive a registry entry lists has an edge to that surface, and every non-global edge's surface lists the primitive | error |
| L-DEP-06 | every registered surface has at least one incoming edge (global edges count) | error |
| L-DEP-07 | `last_verified` older than 90 days, or `confirmed_from_source: false` on a non-global edge 90 days after `first_scan_at` (the first Phase 1 scan); no clock runs while `first_scan_at` is absent | warn |
| L-DEP-08 | the edge `FlexibleGrid -> trade.active-trader.ladder` (or its replacement named in 12.9) exists, because the Phase 1 exit scenario depends on it | error |

### 4.5 Surface Registry entry

One file per surface. The registry is the coverage universe: every tab, sub-tab, gadget, dialog, settings page, context menu that matters, order ticket variant, Active Trader panel, chart and option chain view. It also hosts the named controls that `click_named` steps reference and the landmarks that settle `must_see` checks and locators use. Regions files declare their own landmark templates (4.8); no cross-reference between registry landmarks and regions-file landmarks is required.

```yaml
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
```

`census.dimensions` names the declared values the census repeats a tier-1 surface over (for the option chain: `layout: [delta, theo, ...]`, `strikes: [8, 16, all]`); each combination is its own `census` checkpoint with the dimension segment of the id grammar. `invariant_exceptions` is the registry exception of 1.10 for an invariant that is wrong for one surface: owner, reason and expiry, never silent. The `invariants` block carries the inputs the 2.7 rows say are declared per surface (the overlap allowlist, allowed scrollbars, signed columns, focus order and a font minimum); a surface without it gets the catalog defaults, so INV-15 checks no column and INV-13 runs on no sequence there. The INV-15 colors come from `palettes/<theme>.yaml` (4.16), selected by the workspace's `display_config.theme`.

Worked example:

```yaml
schema_version: 1
id: trade.active-trader.ladder
title: Active Trader
parent: trade.active-trader
kind: active_trader_panel
tier: 1
owner: tos-trade-ui
smoke: true
path:
  - {id: p1, type: click_named, target: shell.tab-bar.trade}
  - {id: p2, type: click_named, target: trade.subtab.active-trader}
  - {id: p3, type: wait_for, anchor: {landmark: lm.ladder-header}, timeout_ms: 8000}
primitives: [FlexibleGrid, Docking, ColumnModel, Theme, FontMetrics, OrderTicketCore, LadderModel]
roots: ["com.tos.desktop.trade.activetrader"]       # placeholder until Phase 0 reads the source tree
personas: [active_trader, futures_trader]
entitlements: [margin]
enumerable_by: P2
resizable: true
hosts: [tab, flexgrid, detached]
splitters:
  - {id: side-panel, template: registry/templates/active-trader/side-splitter.png, host: tab}
target_window: {kind: main}
data_mode: frozen                     # DR-02 Branch A; 12.4 lists the same value
account_state: true
landmarks:
  - {id: lm.ladder-header, template: registry/templates/active-trader/ladder-header.png, confidence_min: 0.92, must_see: true}
  - {id: lm.ladder-buy-column, template: registry/templates/active-trader/buy-col-header.png, must_see: true}
anti_anchors: [Loading, Reconnecting, "No data"]
labels: ["Bid Size", "Ask Size"]
controls:
  - {id: auto-send-checkbox, template: registry/templates/active-trader/auto-send.png, ocr_text: "Auto send", required: true}
  - {id: symbol-box, template: registry/templates/common/symbol-box.png}
  - {id: buy-mkt-button, template: registry/templates/active-trader/buy-mkt.png, required: true, deny: true}
  - {id: flatten-button, template: registry/templates/active-trader/flatten.png, ocr_text: Flatten, deny: true}
settle: {max_wait_ms: 10000}
census:
  enabled: true
  workspaces: [ws.tier-a.active-trader, ws.tier-a.futures-trader, ws.tier-c.sweep-harness]
```

`auto-send-checkbox` is a control on purpose: the run-start attestation (6.3 Run-start settings attestation) reads it on every Active Trader panel in the loaded workspace, because the setting is per panel. `buy-mkt-button` and `flatten-button` are registered with `deny: true` so the gateway deny list can name them by registry id (6.8); `required: true` on the buy button is what INV-10 checks, so an erased order control is an `invariant_violation` even though no flow may ever click it.

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-SRF-01 | `id` unique across the registry and matches the slug regex; the file name equals the id | error |
| L-SRF-02 | tier 1 requires `owner`, at least one `must_see` landmark, a non-empty `anti_anchors` list, a non-empty `labels` list and at least one `required` control (INV-05 and INV-10 need them) | error |
| L-SRF-03 | `resizable = true` requires a non-empty `hosts` list; `size_ladder`, when present, exists in `registry/ladders.yaml` and `ladder_reason` is given; `resizable` may be set only when `enumerable_by: P8` or `splitters` is non-empty | error |
| L-SRF-04 | `kind = dialog` or `target_window.kind` in `dialog`, `detached`, `popup` requires `target_window.title_regex`; `main` forbids it | error |
| L-SRF-05 | `path` ends with a `wait_for` on a `must_see` landmark of this surface, and every targeting step in `path` resolves on at least two rungs (a `template` plus `jab` or `ocr_text`) | error |
| L-SRF-06 | all template files exist, are PNG, 16x16 to 400x400 | error |
| L-SRF-07 | every primitive exists in the dependency map (see L-DEP-05); `roots` non-empty | error |
| L-SRF-08 | `data_mode = masked_live` requires that every checkpoint on this surface has at least one `rule` or `mask` region (cross-file, L-XREF-03) | error |
| L-SRF-09 | control ids unique within the surface; `jab` present only when `phase0/branches.yaml` says JAB is branch A | error |
| L-SRF-10 | `census.workspaces` non-empty when `census.enabled`; every `dimensions` value is a slug | error |
| L-SRF-11 | `parent` exists and the tree has no cycle; `invariant_exceptions` carry owner, reason and a future `expires_at`; an expired exception is dropped at run start and listed in run health, never `config_invalid` | error |
| L-SRF-12 | `entitlements` includes `paper_send` on every surface with `reached_after_send: true` (fills, Working to Filled, cancel and replace); until the `paper_send_ok` decision such surfaces are `not_run (reason: entitlement)` in every matrix | error |
| L-SRF-13 | every `splitters.host` is in `hosts`; `fractions` values are in the full set of 2.5.2 and include 0.00 or 1.00 only when `collapsible: true` | error |
| L-SRF-14 | every surface is in the derived hosted-surface list (`gadgets_present`, 4.6) of at least one tier A workspace, so the merge census reaches it (2.2.4) | error |
| L-SRF-15 | at most 40 entries carry `smoke: true` (2.11); the registry owner removes flags, the run never raises the cap | error |
| L-SRF-16 | `critical: true` requires `tier: 1` | error |

Registry validation at entry to `running`, right after `canary_flow_passed` (probes P1 to P8 of 2.2.3, enumerating the app's main menu, tab bar, gadget picker and settings tree and diffing against the registry) is runtime behaviour, not lint; a new entry yields `unregistered_surface (reason: probe:<Pn>)` on that run, and a registered entry that no probe lists on either build while its entitlements are present is `harness_error (reason: registry_entry_not_found)` for that surface's checkpoints, posted to the system owner.

### 4.6 Workspace Corpus entry

A workspace entry pins one saved workspace file and describes what it contains so the census, the size sweeps and the attestation know what to expect. Workspace files are local state: the Rig Manager copies the checked-in file into the install before launch and hashes it. The saved-workspace file name and location are community-reported (a `workspace.<name>` file in the install folder next to `usergui`); Phase 0 confirms the path and the format on the pinned build, and the `file` field carries whatever that confirmation says.

```yaml
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
```

Worked example:

```yaml
schema_version: 1
id: ws.tier-a.futures-trader
tier: A
persona: futures_trader
file: corpus/workspaces/ws.tier-a.futures-trader/workspace.sentinel-futures
file_sha256: sha256:5b7d...91c0
display_config: {width: 1920, height: 1080, scale_percent: 100, theme: dark, font_size: medium}
tabs: [Monitor, Trade, Analyze, Scan, MarketWatch, Charts, Tools]   # as Phase 0 reads the pinned build's tab bar
windows:
  - {title_regex: "thinkorswim", rect: {x: 0, y: 0, w: 1920, h: 1040}}
link_groups:
  - {color: red, symbol_ref: mes_front}
  - {color: green, symbol_ref: es_continuous}
gadgets_present:
  - charts.flexible-grid
  - trade.active-trader.ladder
  - trade.order-entry.futures
  - monitor.activity.position-statement
  - monitor.activity.working-orders
active_trader_panels:
  - {panel_id: at-left, surface: trade.active-trader.ladder}
owner: tos-trade-ui
notes: Two-column layout; Active Trader docked left of a 2x2 Flexible Grid of charts.
```

`account_role` defaults to `golden` because census checkpoints show the populated account; flows choose their own account with the flow header's `account_role` (4.7). `display_config` must be one entry of `configs/allowed.yaml` (L-WSP-05), whose work area gives the MAXW and MAXH of Ladder F (2.5.2):

```yaml
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
```

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-WSP-01 | `file` exists and its hash equals `file_sha256` | error |
| L-WSP-02 | tier A: exactly one entry per persona across the corpus; `persona` required on tier A and tier B entries | error |
| L-WSP-03 | tier B: `saved_under_build` present and its channel is `released` | error |
| L-WSP-04 | tier C: `size_sweep_cells` present and every `hosts` surface is `resizable` | error |
| L-WSP-05 | `display_config` is listed in `configs/allowed.yaml` (phase 1: only 1920x1080 at 100 percent) | error |
| L-WSP-06 | every `gadgets_present` and `active_trader_panels.surface` id exists; every Active Trader surface in `gadgets_present` appears in `active_trader_panels` | error |
| L-WSP-07 | `link_groups.symbol_ref` exists in the fixture the run uses | error |
| L-WSP-08 | tier A and tier C entries list no surface with `streaming: true` (news, video and chat gadgets, 2.3) in `gadgets_present` | error |
| L-WSP-09 | the corpus shape of 2.3: exactly one tier C entry; four to eight tier B entries, at least two of them saved under the previous release (`saved_under_build` is the latest `released` build, the RC run's base) | warn until Phase 3, then error |

### 4.7 Flow file

A flow is a YAML file the Executor drives through the Input Gateway. It has a header, a precondition block, an ordered step list and a teardown block. Every step has a type from a closed list; every checkpoint declares its target window, data mode, surface and regions; every assertion has a kind from a closed list and is evaluated by the Oracle. A flow never contains a pixel threshold, a mask or a secret value.

**Header.**

```yaml
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
```

**Step common fields and locators.** Every step has `id` (unique in the flow, `^[a-z][a-z0-9_-]*$`; a checkpoint step's id is the last segment of its checkpoint id and so has no underscore, 4.1), `type`, `timeout_ms? = 5000`, `on_fail: enum[fail|retry_once|teardown] = fail`, `order_stage: bool = false` (true on the step that opens the Order Confirmation dialog; it arms the gateway's order-stage deny state, 6.2 and 5.4.6, and is required by L-FLW-07) and `note?`. Steps that act on a control take a `target`, which is `<surface id>.<control id>` from the registry; the Executor resolves it down the locator ladder JAB, image anchor, OCR text anchor, agent, in that order (5.5 Locator ladder), and records the rung used on the step record. The agent rung is skipped when the flow says `agent_fallback: disabled`, when the API preflight failed, when `phase0/branches.yaml` says DR-07 is Branch B, or inside a `secret` step, a `login` flow or any `teardown` step. Retries are capped at one per step and never cross a checkpoint.

What a step failure produces is fixed, whatever the step type: the step is `failed` (or `blocked_by_gateway` when the gateway dropped its event); the checkpoint the step led to is `landmark_missing (reason: locator:<step>)` when the locator was exhausted on every rung, `assertion_failed` when an assertion failed, or `env_error` with the gate in force and the cause's reason when a catalog popup, a disconnect or a modal caused it (for example `env_error (gate: connectivity)` for a disconnect past 15 minutes, `env_error (gate: connectivity, reason: maintenance)` for the POP-09 maintenance notice; every `env_error` names its gate, 4.13); every later checkpoint of the flow is `not_run (reason: step_failed:<step>)` with the step id; teardown runs; a `locator_failure` finding records every rung's result. A step that reaches its target only on the agent rung is `passed_via_agent_fallback`, also with a `locator_failure` finding, and the next checkpoint is captured after `normalize` and flagged `rescued`, so its outcome is `needs_human (reason: rescued_capture)` unless a higher-precedence outcome applies (1.10 precedence).

**Step types.**

| type | Fields | Gateway behaviour and failure |
|---|---|---|
| `click_named` | `target`, `button = left` | Resolves target, arms a one-shot token at the expected coordinates, emits the click. Unresolved target after the ladder: step `failed`, outcomes as above. A target whose control is `deny: true` is rejected by the lint (L-FLW-13) and, if it ever reaches the gateway, dropped as `blocked_by_gateway`. |
| `type_fixture` | `target`, one of `value_ref` (a fixture path or `odd_inputs.<id>` from `corpus/odd-inputs.yaml`) or `value` (literal), `commit: enum[enter|tab|none]`, `verify?` (an assertion) | Types the fully qualified value; `enter` is dropped by the gateway if a ticket or confirm dialog is foreground, and the step ends `blocked_by_gateway`. An odd-input value carries the corpus seed on the step record so a nightly sample is reproducible. In agent mode only `value_ref` is accepted; a `value` literal is a tool error (5.6). |
| `press` | `key` | Single key. Enter, Space and Ctrl+Enter follow the gateway rule above. |
| `drag` | `from: target or point`, `to: target or point`, `steps >= 10`, `hold_ms <= 2000` | Interpolated press, move, release. A drag that opens an Order Confirmation dialog is allowed; the dialog is then owned by the flow. |
| `wheel` | `at: target or point`, `notches: int (signed)` | Wheel events at the point, mouse parked after. |
| `rclick`, `dblclick` | `target` | As `click_named` with the right button or a double click. |
| `key_chord` | `keys: list[str]` | Held modifiers plus one key; the gateway releases all modifiers afterwards. |
| `reset_input_state` | none | Key-up Shift, Ctrl, Alt, Win; mouse-up; one Escape only if no ticket or dialog is foreground. Required first step, last teardown step, and run by the Orchestrator on any crash recovery. |
| `secret` | `target`, `vault_ref`, `commit` | Value pulled at the last moment, typed with the Recorder hook paused and Capture suspended; log shows `<redacted:<vault field>>`. Allowed only in `login` flows. |
| `wait_for` | `anchor: {landmark?, window_title_regex?}`, `anti_anchors?`, `timeout_ms` | Polls until the anchor is present and anti-anchors absent; timeout is step `failed` and the next checkpoint `not_settled` with the reason of the condition still failing at expiry (`unexpected_window:<title>`, `anti_anchor_present`, `anchor_missing` or `frames_unstable`); `timeout` only when no frame was obtained at all (3.5). |
| `park_mouse`, `focus_neutral` | none, or `target` for the neutral control | The settle rule's two normalizing moves, available as explicit steps for `normalize` blocks. |
| `export` | `target` (the grid), `kind: enum[file|copy_all]`, `path?`, `headers: list[str]` (the column headers the export must carry, in order) | Drives the grid's export action through the gateway; the file or clipboard text becomes an Oracle source (`source: export`). 02's "copy grid" step is this type with `kind: copy_all`. An export the build does not offer (the Phase 0 list of 2.8.1) makes the flow's entries `not_run (reason: scope)`; an export action that fails at run time is step `failed` with step reason `export_unavailable`, or `export_timeout` when no file or clipboard text arrived within `timeout_ms` (4.13 `step_outcome.reason`); a header set that differs from `headers` is `harness_error (reason: export_header_mismatch)` on every checkpoint whose assertions read the export (2.8.1). |
| `checkpoint` | see below | Settle, capture, redact, diff, invariants. Produces exactly one checkpoint outcome. |
| `assert` | `assertions: list[assertion]` | Oracle evaluation against the last capture or extraction; any failure sets the enclosing checkpoint to `assertion_failed` and the step to `failed`. |
| `precondition` | `check: enum[paper_mode|workspace_loaded|no_modal|single_main_frame|main_window_rect|fixture_symbol_linked|working_orders|settings_attested|app_alive]`, `expect?` | Evaluated before the first step; a failure triggers one `reset_ui`, then one `reset_profile`; if it still fails, every checkpoint of the flow is `env_error (gate: workspace_loaded, reason: precondition:<check>)`, posted to run health and the flow owner, and the next flow re-checks. `paper_mode` mismatch is not a flow failure: the hook enters deny-all, the process tree is killed and the run is `aborted_safety (reason: paper_mode_mismatch)`. `working_orders` above `expect` is `aborted_safety (reason: unexpected_working_order)`. |
| `teardown` | `action: enum[close_dialog|clear_ticket|close_non_main_windows|reset_ui|reload_workspace|reset_profile|cancel_all|working_orders_check|positions_flat_check]`, `target?`, `expect?` | Runs even after a failed step. `cancel_all` is scripted and verified, and it is the only action that may resolve the Cancel All control, used by the run teardown and by the recovery flow of 6.6; no `click_named`, `rclick` or `dblclick` may target it (L-FLW-19). `working_orders_check` with `expect: 0` is mandatory for order-bearing flows. |

**Checkpoint fields.** `surface_id`, `target_window` and `data_mode` are required on every checkpoint, never inherited silently: the lint fills nothing in, so a reader of the flow sees what the capture will do.

```yaml
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
```

An implicit assertion `app_alive` is attached to every checkpoint: if the thinkorswim process tree is gone, or the Event Dispatch Thread is frozen past the Watchdog's hang threshold (10.2 Watchdog), the checkpoint being captured is `assertion_failed (assertion: app_alive)` with the cause `jvm_crash`, `hang`, `oom` or `process_exit` in `detail` (4.13), the rest of the flow is `not_run (reason: app_crash)`, and a tier-1 finding `app_crash` carries the hs_err_pid files, dumps and log slice. An app error or exception dialog on screen at capture is not `app_alive`; it is `invariant_violation` on INV-14 (2.7), because the app is still running. Two more built-in assertions exist: `size_reached` on sweep steps (`assertion_failed (assertion: size_reached)` when a requested size is not reached after one retry, 2.5.1) and `soak.<metric>` on soak cycle checkpoints (a budget of `soak.yaml` breached, 10.11).

**Assertion fields.** The kinds are those of 2.6.2 Assertion kinds under these spellings.

```yaml
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
```

`equals` compares the extracted field, normalized by `format`, with the expected value; `matches` applies a regex; `within_tol` accepts a numeric difference up to `tol`; `present` and `absent` test for text in the region; `changed_since_previous_step` and `unchanged_since_previous_step` compare against the extraction stored at `since_step`; `count` checks a row or item count against `expected` or `range`; `export_equals` compares an export column total with the on-screen region (2.6.3 R09); `api_within` compares with the API value as of the modelled time (2.8.2 Tolerances); `rule` evaluates one of the arithmetic rules R01 to R10 of 2.8.3 over the inputs; `sign_color_agrees` checks that a value's sign and its dominant text color agree with the pinned palette. Any failure is `assertion_failed (assertion: <id>)` with the expected and extracted values in `detail`. An extraction the Oracle cannot read at all is not a failure of the product: it is `needs_human (reason: oracle_unreadable)`, or `harness_error (reason: ocr_calibration)` when the OCR calibration panel itself misread (5.10 Oracle).

**Worked example.** Build a one-lot /MES limit order far from the market and open the Order Confirmation dialog. Nothing is sent.

```yaml
schema_version: 2
flow_id: flow.trade.order-ticket.futures.single-leg
title: One-lot /MES limit order to the Order Confirmation dialog
class: standard
tier: 1
owner: tos-trade-ui
persona: futures_trader
surface: trade.order-entry.futures
surfaces: [trade.order-entry.futures, dialog.order-confirmation]
requires: [futures]
workspace: ws.tier-a.futures-trader
fixture: fx.rig-01.paper
data_mode: masked_live
agent_fallback: allowed
budget: {max_duration_s: 180, max_steps: 40}
normalize:
  - {id: n1, type: press, key: escape}
  - {id: n2, type: park_mouse}
  - {id: n3, type: focus_neutral, target: shell.status-bar.neutral}
preconditions:
  - {id: p1, type: precondition, check: paper_mode}
  - {id: p2, type: precondition, check: workspace_loaded, expect: ws.tier-a.futures-trader}
  - {id: p3, type: precondition, check: no_modal}
  - {id: p4, type: precondition, check: working_orders, expect: 0}
steps:
  - {id: s0, type: reset_input_state}
  - {id: s1, type: click_named, target: shell.tab-bar.trade}
  - id: s2
    type: type_fixture
    target: trade.order-entry.futures.symbol-box
    value_ref: fixture.symbols.mes_front.symbol        # /MESZ26
    commit: enter
    verify: {id: sym_header, kind: equals, region: rgn.order-entry.header-symbol, value_ref: fixture.symbols.mes_front.symbol}
  - {id: s3, type: wait_for, anchor: {landmark: lm.order-entry-header}, anti_anchors: [Loading, Reconnecting], timeout_ms: 8000}
  - id: cp1
    type: checkpoint
    checkpoint_id: flow/trade.order-entry.futures/flow.trade.order-ticket.futures.single-leg/cp1
    surface_id: trade.order-entry.futures
    target_window: {kind: main}
    data_mode: masked_live
    regions: regions/flow/trade.order-entry.futures/flow.trade.order-ticket.futures.single-leg/cp1.yaml
    recapturable: true
  - id: a1
    type: assert
    assertions:
      - {id: qty_default, kind: equals, region: rgn.order-entry.qty, format: int, expected: 1}
      - {id: tif_default, kind: equals, region: rgn.order-entry.tif, expected: DAY}
  - {id: s4, type: click_named, target: trade.order-entry.futures.ask-cell}   # stages a BUY in the ticket
  - {id: s5, type: click_named, target: trade.order-entry.futures.price-field}
  - {id: s6, type: key_chord, keys: [ctrl, a]}
  - id: s7
    type: type_fixture
    target: trade.order-entry.futures.price-field
    value_ref: fixture.prices.mes_far_below.value
    commit: tab
  - {id: s8, type: click_named, target: trade.order-entry.futures.confirm-and-send, order_stage: true}
  - {id: s9, type: wait_for, anchor: {window_title_regex: "^Order Confirmation", landmark: lm.order-confirmation-title}, timeout_ms: 6000}
  - id: cp2
    type: checkpoint
    checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2
    surface_id: dialog.order-confirmation
    target_window: {kind: dialog, title_regex: "^Order Confirmation"}
    data_mode: masked_live
    regions: regions/flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2.yaml
    tier: 1
    recapturable: false
  - id: a2
    type: assert
    assertions:
      - {id: side, kind: equals, region: rgn.confirm.side, expected: BUY}
      - {id: qty, kind: equals, region: rgn.confirm.qty, format: int, expected: 1}
      - {id: symbol, kind: equals, region: rgn.confirm.symbol, value_ref: fixture.symbols.mes_front.symbol}
      - {id: price, kind: equals, region: rgn.confirm.price, format: price, value_ref: fixture.prices.mes_far_below.value}
      - {id: cost_math, kind: rule, rule_id: R01, tol: 0.01,
         inputs: {qty: rgn.confirm.qty, price: rgn.confirm.price, multiplier: 5, total: rgn.confirm.cost}}
      - {id: bp_effect, kind: present, region: rgn.confirm.bp-effect, format: money}
      - {id: send_label, kind: equals, region: rgn.confirm.send-button, expected: Send}
      - {id: no_placeholder, kind: absent, region: rgn.confirm.bp-effect, pattern: "(?i)loading|n/a|--"}
      - {id: price_changed, kind: changed_since_previous_step, region: rgn.order-entry.price, since_step: cp1}
teardown:
  - {id: t1, type: teardown, action: close_dialog, target: dialog.order-confirmation.title-close}
  - {id: t2, type: teardown, action: clear_ticket}
  - {id: t3, type: assert, assertions: [{id: ticket_empty, kind: absent, region: rgn.order-entry.qty, pattern: "[0-9]"}]}
  - {id: t4, type: teardown, action: working_orders_check, expect: 0}
  - {id: t5, type: reset_input_state}
```

Three notes on the example. The dialog is closed through its title-bar close control, because the names of the buttons beside Send in the Order Confirmation dialog are unverified (Phase 0 confirms them; until then no flow targets them). The multiplier 5 is the /MES contract multiplier and lives in R01's per-instrument table (2.8.3), not in the dialog; the Oracle reads qty, price and cost from the screen and checks the arithmetic. `cp1` is the pre-input checkpoint of 2.6.3 (G64): it is captured after the symbol is entered but before any quantity, price or type is typed, so the ticket's defaults are what the diff and the assertions see; L-FLW-15 enforces its position.

**Second worked example: the stock limit ticket flow.** `flow.trade.order-ticket.stock.limit` is the tier-1 stock ticket flow that the mutant catalog (12.5: M01 and M20 at `s06`; M02 to M06 and M22 at `s07`), the artifact layout example of 8.4 and the Watchdog example of 10.2 name, so its numbered steps are fixed here: `s01` to `s07`, where `s06` is the ticket-ready checkpoint (quantity and limit price typed, Confirm and Send visible, nothing staged) and `s07` is the Order Confirmation dialog checkpoint. Helper steps that produce no checkpoint and are named by no mutant, report or checkpoint id carry ids without the `s` prefix (`w-ticket`, `open-confirm`, `w-confirm`), so adding a helper never renumbers `s01` to `s07`. The ticket surface is `trade.order-entry.stock`, one member of the `trade.order-entry.<asset>` family (`trade.order-entry.futures` above). Normalize, preconditions and teardown are those of the futures example with the workspace id changed; the `s07` regions file uses the `rgn.confirm.*` ids of the 4.8 example.

```yaml
schema_version: 2
flow_id: flow.trade.order-ticket.stock.limit
title: 100-share AAPL limit order far below the market to the Order Confirmation dialog
class: standard
tier: 1
owner: tos-trade-ui
persona: swing_trader
surface: trade.order-entry.stock
surfaces: [trade.order-entry.stock, dialog.order-confirmation]
requires: []
workspace: ws.tier-a.swing-trader
fixture: fx.rig-01.paper
data_mode: frozen
agent_fallback: allowed
budget: {max_duration_s: 180, max_steps: 40}
# normalize, preconditions and teardown: as in flow.trade.order-ticket.futures.single-leg above, with
# workspace_loaded expecting ws.tier-a.swing-trader
steps:
  - {id: s01, type: reset_input_state}
  - id: s02
    type: type_fixture
    target: trade.order-entry.stock.symbol-box
    value_ref: fixture.symbols.aapl.symbol               # AAPL
    commit: enter
    verify: {id: sym_header, kind: equals, region: rgn.order-entry.header-symbol, value_ref: fixture.symbols.aapl.symbol}
  - {id: w-ticket, type: wait_for, anchor: {landmark: lm.order-entry-header}, anti_anchors: [Loading, Reconnecting], timeout_ms: 8000}
  - id: s03                                              # pre-input checkpoint (2.6.3, L-FLW-15)
    type: checkpoint
    checkpoint_id: flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s03
    surface_id: trade.order-entry.stock
    target_window: {kind: main}
    data_mode: frozen
    regions: regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s03.yaml
  - {id: s04, type: type_fixture, target: trade.order-entry.stock.qty-field, value: "100", commit: tab}
  - {id: s05, type: type_fixture, target: trade.order-entry.stock.price-field, value_ref: fixture.prices.aapl_far_below.value, commit: tab}
  - id: s06                                              # ticket-ready checkpoint; M01 and M20 bind here
    type: checkpoint
    checkpoint_id: flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06
    surface_id: trade.order-entry.stock
    target_window: {kind: main}
    data_mode: frozen
    regions: regions/flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06.yaml
    tier: 1
    expect:
      - {id: side, kind: equals, region: rgn.order-entry.side, expected: BUY}
      - {id: type_limit, kind: equals, region: rgn.order-entry.order-type, expected: LMT}
      - {id: qty, kind: equals, region: rgn.order-entry.qty, format: int, expected: 100}
      - {id: price, kind: equals, region: rgn.order-entry.price, format: price, value_ref: fixture.prices.aapl_far_below.value}
      - {id: send_label, kind: equals, region: rgn.order-entry.confirm-button, expected: Confirm and Send}
  - {id: open-confirm, type: click_named, target: trade.order-entry.stock.confirm-and-send, order_stage: true}
  - {id: w-confirm, type: wait_for, anchor: {window_title_regex: "^Order Confirmation", landmark: lm.order-confirmation-title}, timeout_ms: 6000}
  - id: s07                                              # Order Confirmation checkpoint; M02 to M06 and M22 bind here
    type: checkpoint
    checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07
    surface_id: dialog.order-confirmation
    target_window: {kind: dialog, title_regex: "^Order Confirmation"}
    data_mode: frozen
    regions: regions/flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07.yaml
    tier: 1
    recapturable: false
    expect:
      - {id: side, kind: equals, region: rgn.confirm.side, expected: BUY}
      - {id: qty, kind: equals, region: rgn.confirm.qty, format: int, expected: 100}
      - {id: symbol, kind: equals, region: rgn.confirm.symbol, value_ref: fixture.symbols.aapl.symbol}
      - {id: price, kind: equals, region: rgn.confirm.price, format: price, value_ref: fixture.prices.aapl_far_below.value}
      - {id: cost_math, kind: rule, rule_id: R01, tol: 0.01,
         inputs: {qty: rgn.confirm.qty, price: rgn.confirm.price, multiplier: 1, total: rgn.confirm.cost}}
      - {id: R02, kind: rule, rule_id: R02, inputs: {bp_effect: rgn.confirm.bp-effect, notional: rgn.confirm.cost}}
      - {id: send_label, kind: equals, region: rgn.confirm.send-button, expected: Send}
```

The side and order type come from the fixture's fresh-install order-defaults profile (4.9), which stages a BUY LMT DAY ticket; a ticket that opens otherwise fails `side` or `type_limit` at `s06` as `assertion_failed`, and the flow stops before `open-confirm`. Money cells come from the one closed list of 4.8 (`money_role`: `qty`, `price`, `bp_effect`, `max_loss`, `send`, `cancel`, `delete`). Here `s06` carries the quantity, the limit price and the ticket's Confirm and Send button (roles `qty`, `price` and `send`), and `s07` the quantity, the price, the Buying Power Effect and the Send button (`qty`, `price`, `bp_effect` and `send`). Each is a `diff` region named by an `expect` entry, directly or as a rule input, as L-RGN-04 requires; a dialog that also shows Max Loss, Cancel or Delete declares those cells with their roles and asserts them the same way.

**Validation rules.**

| Rule | Check | Severity |
|---|---|---|
| L-FLW-01 | `flow_id` unique; step ids unique within the file; first step and last teardown step are `reset_input_state` | error |
| L-FLW-02 | every `target` is `<surface>.<control>` and exists in the registry; every `surface_id` exists in the registry and is in `surfaces`; every checkpoint carries `surface_id`, `target_window` and `data_mode` | error |
| L-FLW-03 | `checkpoint_id` equals `flow/<surface_id>/<flow_id>/<step id>` and is unique across the library; a removed or renamed id has an entry in `flows/RENAMES.yaml` (old, new or `retired`, build) | error |
| L-FLW-04 | every checkpoint's `regions` file exists, or is `default` with `data_mode: frozen` | error |
| L-FLW-05 | `masked_live` checkpoints reference at least one `rule` or `mask` region | error |
| L-FLW-06 | `secret` steps only in `class: login`; `login` flows have `agent_fallback: disabled`; no `value` literal anywhere matches a credential pattern | error |
| L-FLW-07 | the step that targets Confirm and Send carries `order_stage: true`; after it, no `press` or `key_chord` of Enter, Space or Ctrl+Enter until a `close_dialog` or `clear_ticket` teardown, which is followed by an `absent` assertion proving the ticket is empty (static approximation; the gateway is the real guard) | error |
| L-FLW-08 | `class: paper_send_ok` requires `policy/send_policy.yaml` with `enabled: true` and a risk-owner signature; otherwise the flow is rejected (OPEN per brief 1.7) | error |
| L-FLW-09 | `drag.steps >= 10`, `hold_ms <= 2000`; `wheel.notches != 0` | error |
| L-FLW-10 | `budget.max_duration_s` is within the flow budget rule of 11.2 Cadence and time budgets per run type (3 x measured p50, 5 minutes until measured) | error |
| L-FLW-11 | assertion `region` ids exist in the regions file of the nearest preceding checkpoint; `since_step` names an earlier checkpoint; `rule_id` is one of R01 to R10 (2.8.3) and its `inputs` match the rule's rows in the operand table of 2.8.3 (every required operand present, each of its type); a rule whose operand regions are not all declared on the checkpoint fails here, never silently skips | error |
| L-FLW-12 | preconditions include `paper_mode` and `no_modal`; flows whose `surfaces` include an order surface include `working_orders` with `expect: 0` and a `working_orders_check` teardown | error |
| L-FLW-13 | no step targets a control with `deny: true` or clicks `auto-send-checkbox`; on steps run by the agent or the Explorer profile (agent-rescue goals, `explorer_candidate` flows) no target is on the Explorer deny list of 6.8 (`policy/explorer_deny_list.yaml`) | error |
| L-FLW-14 | `origin.kind: explorer` flows carry `origin.run_id`, `origin.finding_ref` and `class: explorer_candidate` until a human promotes them; they live under `flows/quarantine/` | error |
| L-FLW-15 | a flow whose `surfaces` include a ticket surface has a checkpoint on that surface before the first `type_fixture` or `click_named` into a ticket field other than the symbol box (the pre-input checkpoint of 2.6.3) | error |
| L-FLW-16 | `recapturable: false` on every checkpoint that follows a `clear_ticket`, `close_dialog` or `cancel_all` teardown action within the same flow | error |
| L-FLW-17 | every step `target` resolves on at least two rungs (a `template` plus `jab` or `ocr_text` on the control), so no flow depends on one anchor | error |
| L-FLW-18 | `budget.max_steps` at or below 400 and `max_duration_s` at or below 5400 for `class: explorer_candidate`, matching `policy/explorer_budget.yaml` | error |
| L-FLW-19 | no `click_named`, `rclick` or `dblclick` targets a control whose `ocr_text` or title is Cancel All; only the `cancel_all` teardown action resolves it | error |

### 4.8 Region Registry and mask record

One regions file per checkpoint. Every region is an offset from a landmark located by template match on each capture; a missing landmark yields `landmark_missing (reason: landmark:<id>)` with the side it was missing on, and no diff runs on its regions (3.7: any landmark missing on either side makes the checkpoint `landmark_missing`; there is never a partial or blind diff). Every region of a checkpoint's regions file is exactly one of `freeze`, `rule`, `mask` or `diff`; pixels no declared region covers belong to the implicit `diff` region `_rest` (3.8 Region Registry semantics). The fifth type, `redaction`, exists only in the global redaction list `redaction/anchors.yaml`, which uses the same shape (below).

```yaml
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
```

Worked example, the Order Confirmation checkpoint from 4.7 (file `regions/flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2.yaml`):

```yaml
schema_version: 1
checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2
landmarks:
  - {id: lm.order-confirmation-title, template: regions/templates/order-confirmation-title.png, confidence_min: 0.93}
regions:
  - {id: rgn.confirm.side,   type: diff, rect: {dx: 24, dy: 48, w: 60, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation}
  - {id: rgn.confirm.qty,    type: diff, rect: {dx: 90, dy: 48, w: 48, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation, money_role: qty, money_cell: true}
  - {id: rgn.confirm.symbol, type: diff, rect: {dx: 144, dy: 48, w: 110, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation}
  - {id: rgn.confirm.price,  type: diff, rect: {dx: 260, dy: 48, w: 90, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation, money_role: price, money_cell: true}
  - {id: rgn.confirm.cost,   type: diff, rect: {dx: 24, dy: 96, w: 200, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation}
  - {id: rgn.confirm.bp-effect, type: diff, rect: {dx: 24, dy: 120, w: 200, h: 20}, landmark: lm.order-confirmation-title, panel: order_confirmation, money_role: bp_effect, money_cell: true}
  - {id: rgn.confirm.send-button, type: diff, rect: {dx: 380, dy: 200, w: 90, h: 24}, landmark: lm.order-confirmation-title, panel: order_confirmation, money_role: send, money_cell: true}
  - {id: rgn.confirm.timestamp, type: mask, rect: {dx: 260, dy: 200, w: 110, h: 16}, landmark: lm.order-confirmation-title, panel: order_confirmation, mask_ref: msk.confirm.timestamp-01}
```

```yaml
schema_version: 1
mask_id: msk.confirm.timestamp-01
checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.futures.single-leg/cp2
region_id: rgn.confirm.timestamp
owner: tos-trade-ui
reason: Dialog shows the staging time to the second; moves on every capture.
created_by: human
created_run: run-2026-11-03-merge-0412
created_at: 2026-11-03T15:20:00-05:00
expires_at: 2026-12-03T15:20:00-05:00
ticket: TOS-48102
decision_ref: dec-000731
```

Notes on the example. Every money cell (`rgn.confirm.qty`, `rgn.confirm.price`, `rgn.confirm.bp-effect`, `rgn.confirm.send-button`, roles `qty`, `price`, `bp_effect` and `send`) is a `diff` region with a zero floor and is also asserted by the checkpoint's Oracle assertions (`qty`, `price`, `cost_math`, `bp_effect` and `send_label` of the 4.7 flow): on a flow checkpoint a money cell is always pixel-diffed and rule-asserted and never rule-only, so a value change inside an assertion's tolerance or inside R02's bracket still shows as a pixel diff and, on an undeclared surface, is `collateral` (3.11). The `rule` type remains for live cells that are not money cells, such as quote, time and change fields, and for money cells on census checkpoints, which carry no assertions and no flow-typed values: there the money cell is a `rule` region, checked because census and sweep checkpoints run every `rule` region of their regions file (3.8, L-RGN-04). The cost total is not a money cell; it is a calibrated `diff` region checked by R01. Pixels outside the declared regions fall to the implicit `_rest` region, so no catch-all region overlaps the others (L-RGN-02), and the timestamp mask lies clear of the Send button, since any mask or redaction box on a money cell breaches its zero budget. The timestamp mask satisfies L-RGN-07 because `cp2` is a `masked_live` checkpoint (4.7). On a `frozen` checkpoint, such as `s07` of the stock limit flow that reuses these region ids, L-RGN-07 allows a mask only on the clock, the connection indicator, the caret of a checkpoint with `keep_focus: true` and the parking rectangle, so there the staging timestamp is a `rule` region (`field: time`, `pattern` of the dialog's time format) and no mask record exists.

**Redaction list.** Redaction is not a mask and not a per-checkpoint record. One global file, `redaction/anchors.yaml`, owned by the security contact, uses the regions-file shape without a checkpoint id: landmarks, plus regions of `type: redaction` cut from them. Capture applies every redaction region wherever its landmark is found, on every capture, before anything is stored or released (6.10 Stage 1, which also says when a missing redaction landmark is `landmark_missing (reason: redaction_landmark:<id>)`). Regions marked `identity_field: true` (the account number and the account holder name) are excluded from the mask budget; every other redaction box counts toward it.

```yaml
# schemas/redaction.schema.json  (redaction/anchors.yaml)
redaction_file:
  schema_version: int = 1
  owner: str                            # the security contact (6.10)
  landmarks: list[{id: id, template: path, confidence_min: num = 0.92, search_rect: {x: int, y: int, w: int, h: int}?}]
  regions: list[region]                 # all of type redaction (L-RGN-03)
```

**Threshold source.** A `diff` region carries no threshold of its own. The Calibration Service writes each region's floors and the four thresholds 3.9 derives to `calibration_floors` (8.3), keyed by calibration key, checkpoint and region, and exports them to `calibration/<hex>.yaml`, `<hex>` being the calibration key of the run's fingerprint without its `sha256:` prefix (4.12 Calibration key; one helper computes the name); the Diff Engine reads the region's thresholds from there (3.9 A/A calibration and threshold derivation), so a recalibration never touches a regions file. The record per region is the four derived thresholds of 3.9 (`component_area_min`, `changed_px_min`, `delta_e_min`, `shift_px_min`) plus the measured floors; SSIM is stored for ranking only and has no threshold. Three cases: the calibration exists and has the region, `threshold_source: calibrated`; it exists but lacks the region (a region added since the last Calibration run), the region is diffed at threshold 0, any changed pixel is `needs_human (reason: uncalibrated)`, the record carries `threshold_source: uncalibrated`, and a Calibration run is queued; no calibration exists for the calibration key at all, every region of every checkpoint on it is that same `uncalibrated` case and a Calibration run is queued (3.9 step 6, 4.12 L-FPR-04). The 7.13 rows say which changes invalidate the floors of one checkpoint or one region rather than the whole key. A missing calibration is never `not_comparable`, whose only reasons are `fingerprint_mismatch`, `window_mismatch` and `surface_mismatch`. A non-zero floor on a `frozen` checkpoint is a bug to fix (brief 1.4), so the lint warns on it (L-RGN-06). A `floor_exception` is the only way to loosen a region (3.7 `edge_tolerance_px`), is human-approved with an owner, a ticket, the measured floor and an expiry at most 30 days after approval, and is refused on `money_cell` regions.

**Mask budget.** Masked plus redacted area is computed per checkpoint and per `panel` class on every run: `order_confirmation` 10 percent, `order_ticket` 10 percent, `position_statement` 10 percent, `account_info` 5 percent, any `money_cell` region 0 percent. Masks and redaction boxes both count toward it (3.8), except boxes over the identity fields: the account number and the account holder name are always redacted in Capture (`redaction/anchors.yaml`, each such region marked `identity_field: true`), are excluded from the mask budget, are never diffed, are not money cells, and are proven nightly by the redaction canary (6.5, 6.10). Redaction regions are not masks: they need no mask record and no decision, and only the security contact changes them. The lint enforces the budget on every pull request (L-MSK-03); at run time a landmark move or geometry change that pushes a panel's masked plus non-identity redacted share over its budget, or any mask or redaction box onto a money cell, is `harness_error (reason: mask_budget_exceeded)` on that checkpoint, with the panel and its share in `detail` and a `mask_budget_exceeded` finding (3.8). Account identifiers are never masked; they are redacted in Capture.

| Rule | Check | Severity |
|---|---|---|
| L-RGN-01 | region ids unique; file path equals `checkpoint_id`; a flow or census entry references the checkpoint | error |
| L-RGN-02 | every `rect` lies inside the surface's captured bounds as recorded in the last calibration; regions of different kinds do not overlap except that a `mask` may lie inside another region | error |
| L-RGN-03 | `rule` present if and only if `type = rule`; `mask_ref` present if and only if `type = mask` and the mask file exists; `fixture_dependency` present if and only if `type = freeze`; `type: redaction` only in `redaction/anchors.yaml`, where every region is of that type; `identity_field` only on `redaction` regions | error |
| L-RGN-04 | a `money_cell` region is never a `mask` and carries no `floor_exception`. On a flow checkpoint it is `diff` (or `freeze`, 3.8) with a zero floor, never `rule`, and at least one assertion of that checkpoint (an `expect` entry, or an `assert` step before the next checkpoint) names the region as its `region` or as a rule input, so it is always pixel-diffed and rule-asserted, never rule-only. On a census checkpoint, which has no assertions, every money cell is a `rule` region, run because census and sweep checkpoints evaluate every `rule` region of their regions file (3.8) | error |
| L-RGN-05 | `floor_exception` carries `approved_by`, `approved_at`, `owner`, `reason`, `ticket`, `measured_floor` and a future `expires_at` at most 30 days after `approved_at`; an expired exception is dropped at run start and listed in run health, never `config_invalid` | error |
| L-RGN-06 | the calibration file records a non-zero floor on a region of a `frozen` checkpoint | warn |
| L-RGN-07 | `mask` on a `frozen` checkpoint anywhere but the clock, the connection indicator, the parking rectangle and the caret, which is allowed only on a checkpoint with `keep_focus: true` (3.2, 3.5) | error |
| L-RGN-08 | `money_cell: true` if and only if `money_role` is set; `money_role` only on regions whose `panel` is `order_confirmation` or `order_ticket` | error |
| L-MSK-01 | `created_by = human`, `decision_ref` exists and is a `request_mask` decision by a human in the triage roster of `ops/oncall.yaml` | error |
| L-MSK-02 | `expires_at` in the past: in CI, `error` on the PR that adds or renews it; at run start the mask simply stops applying, run health lists "mask expired: <id>", the Reporter warned the owner seven days before (3.8), and the run is never `config_invalid` over it | error (CI), ignored at run start |
| L-MSK-03 | masked plus non-identity redacted area per checkpoint and per `panel` class under the budget table (identity-field redaction excluded, mask budget paragraph above) | error |
| L-MSK-04 | mask growth over 30 days above 2 percent of a tier-1 checkpoint | warn |
| L-MSK-05 | renewal (a later `expires_at` on an existing mask) quotes the latest nightly A/A result for the region in `reason`; a region stable in A/A is dropped, not renewed | error |

### 4.9 Fixture

The fixture declares the login, the paperMoney accounts, the symbols, the seeded positions and watchlists, the pinned settings profiles, the data mode and the data epoch. `verify_account_fixture` compares the running account against it at gate `fixture_verified` (7.6 Fixture reset flow and verify_account_fixture); drift is `env_error (gate: fixture_verified, reason: fixture_drift)`, not a diff. Positions are seeded by a named human during market hours and never by the Executor. Each paperMoney login provides two simulated accounts, a margin and an IRA account (verified); the fixture uses the margin account as the populated `golden` account and the IRA account as the `empty` account, falling back to a second login only if IRA restrictions block an empty-state flow (7.6 writes the roles as GOLDEN and EMPTY; the enum values here are the lowercase spellings).

**The paper-mode allowlist** is a separate file, because the paper-mode assertion (6.4) must not depend on parsing the fixture:

```yaml
# gateway/allowlist.yaml  (schemas/allowlist.schema.json)
allowlist:
  schema_version: int = 1
  rigs: list[{rig: id, account_ids: list[str], login_ref: str}]   # exact selector strings, one list per rig
  ondemand_virtual_account: {account_id: str, valid_while_template: path}?   # frozen branch B only (7.9)
```

Matching is exact string equality against the account selector text; there is no prefix, pattern or substring match, so `D-` is a lint sanity check and never the runtime rule. The lint hashes the file and the Rig Manager compiles it into the gateway hook host at snapshot build; the hash is the fingerprint field `harness.allowlist_sha256` (4.12), so a rig whose hook host carries a different allowlist than the config commit fails L-FPR-02 as `env_error (gate: provisioned, reason: rig_spec_mismatch)` before any login.

```yaml
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
```

Worked example:

```yaml
schema_version: 1
fixture_id: fx.rig-01.paper
rig: rig-01
login_ref: vault://sentinel/rigs/rig-01/tos-login
owner: asmith
accounts:
  - {account_id: D-20481133, kind: margin, role: golden, cash: 50000.00, cash_tolerance: 5000.00}
  - {account_id: D-20481134, kind: ira, role: empty, cash: 2500.00, cash_tolerance: 250.00, cash_floor: 2500}
settings_profile: profile.app-settings.sentinel-pinned
order_defaults_profile: profile.order-defaults.fresh-install
data_mode:
  frozen: {provider: none}              # Phase 0 item 2 decides backend_fixture or ondemand
  masked_live: {delay_minutes: 20, realtime: false}
symbols:
  - {id: mes_front, symbol: /MESZ26, asset: future, purpose: [ticket, ladder], roll_date: 2026-12-11}
  - {id: es_continuous, symbol: /ES, asset: future, purpose: [chart]}
  - {id: aapl, symbol: AAPL, asset: equity, purpose: [ticket, chart, chain, watchlist]}
  - {id: spx, symbol: SPX, asset: index, purpose: [chain, chart]}
prices:
  - {id: mes_far_below, symbol_ref: mes_front, rule: "last_delayed - 1000, rounded to 0.25", value: 5325.00, computed_at: 2026-11-09T08:30:00-05:00}
  - {id: aapl_far_below, symbol_ref: aapl, rule: "last_delayed x 0.65, rounded to 0.01", value: 150.25, computed_at: 2026-11-09T08:30:00-05:00}
positions:
  - {account_ref: D-20481133, symbol_ref: aapl, qty: 100, avg_price: 180.25, seeded_by: asmith, seeded_at: 2026-10-06T10:15:00-04:00}
  - {account_ref: D-20481133, symbol_ref: mes_front, qty: 1, avg_price: 6210.50, seeded_by: asmith, seeded_at: 2026-10-06T10:20:00-04:00}
watchlists:
  - {name: SENTINEL-CORE, symbols: [aapl, spx, es_continuous]}
working_orders_expected: 0
alerts_expected: 0
epoch:
  epoch_id: sha256:3d1a...b7c4
  window_date: 2026-11-09
  front_month: {mes: Z26, es: Z26}
  nearest_monthly_expiration: 2026-11-20
  data_delay: delayed_20
churn_calendar:
  - {date: 2026-11-20, event: expiration, symbol_ref: spx, action: reapprove}
  - {date: 2026-12-11, event: roll, symbol_ref: mes_front, action: reapprove}
  - {date: 2026-11-26, event: holiday, action: skip_checkpoints}
  - {date: 2026-12-18, event: reseed_due, action: reseed}
```

paperMoney data is delayed 20 minutes by default (verified), so `data_delay: delayed_20` is the normal state and `realtime` is set only after the platform team confirms it for the login; the badge is read at `fixture_verified` and a mismatch is `env_error (gate: fixture_verified, reason: delay_state)`. The `prices` list is recomputed by the fixture reset flow before each run so a far-from-market limit stays far from the market. The epoch is the fixture's contribution to the fingerprint (4.12 `data_epoch`): base and candidate must share `epoch_id`, else every checkpoint is `not_comparable (reason: fingerprint_mismatch)` with `data_epoch` in `detail` (3.2). The hash of the 24-hour trading list is not an epoch field and not in `epoch_id`: it is read each run and recorded in the fingerprint's `record` block (4.12, 7.4), so a change of the list never makes a pair `not_comparable`. Base-capture reuse (11.2) is allowed only when the whole reuse key of 7.9 matches: `fingerprint_id`, `epoch_id`, the sha256 of the fixture file and the golden account's latest `seeded_at`. The churn calendar is generated from the exchange-calendar library and the market-data API (7.9) and checked in; `reapprove` marks the day's frozen baselines for re-approval instead of scoring, `skip_checkpoints` makes the affected checkpoints `not_run (reason: scope)` with the event in `detail`, `reseed` is a calendar task for the login owner.

| Rule | Check | Severity |
|---|---|---|
| L-FIX-01 | every `account_id` matches `^D-[0-9A-Z]+$` and appears, as an exact string, in `gateway/allowlist.yaml` for the fixture's `rig`; the allowlist has no patterns, globs or prefixes | error |
| L-FIX-02 | exactly one `golden` account; at most one `empty`; `cash_floor` absent when `policy/send_policy.yaml` is enabled | error |
| L-FIX-03 | futures symbols with purpose `ticket` or `ladder` carry a month code and year and a future `roll_date`; expired contracts fail | error |
| L-FIX-04 | `login_ref` matches `^vault://`; no field anywhere matches a credential pattern | error |
| L-FIX-05 | `settings_profile` and `order_defaults_profile` exist and the settings profile pins confirmation on, Shift+click send off, Active Trader hotkeys off, no order hotkeys (attestation items of 12.6) | error |
| L-FIX-06 | `epoch` present with `epoch_id` recomputing; `front_month` keys cover every futures symbol; `dataset_id` present under frozen branch A and `ondemand_timestamp` under branch B per `phase0/branches.yaml` | error |
| L-FIX-07 | `churn_calendar` has an entry for every `roll_date`, for `nearest_monthly_expiration` and a `reseed_due` entry on or before the earliest option expiration among `golden` positions | error |
| L-FIX-08 | `prices.computed_at` older than 24 hours at run start: the fixture reset flow recomputes them from the API; if it cannot, order-bearing checkpoints are `env_error (gate: fixture_verified, reason: prices_stale)` and the rest of the run proceeds; never `config_invalid` | handled at run start |
| L-FIX-09 | the `golden` account's positions cover the checked-in golden list of 7.6: an equity below 1 dollar, an equity above 1,000 dollars, a multi-leg option position, a LEAPS, one futures and one forex position | warn until Phase 2, then error |
| L-FIX-10 | the `empty` account has no positions and `working_orders_expected: 0` | error |

A working order found at run start that the fixture does not declare is `aborted_safety (reason: unexpected_working_order)`, the process tree is killed, and the on-call channel is paged with the Working Orders screenshot and the Copy All text. Drift follows three rules (7.5, 7.6):

- Position and cash drift is scoped to `account_state` checkpoints and found at gate `fixture_verified`. On the `empty` account the fixture reset flow runs once (7.6 stage 2) and `verify_account_fixture` is re-run; a second mismatch makes every checkpoint whose surface or step carries `account_state: true` `env_error (gate: fixture_verified, reason: fixture_drift)`. There is no third attempt.
- The `golden` account is never reset: a position or cash mismatch there makes its `account_state` checkpoints `env_error (gate: fixture_verified, reason: fixture_drift)` at once and opens a re-seed task for the accounts owner.
- Watchlist drift is whole-shard and found at gate `settings_attested`, after the one correction pass of 7.6 stage 1: every scoped checkpoint of the shard is `env_error (gate: settings_attested, reason: fixture_drift)`.

In every case the rest of the run proceeds, the run ends `complete_with_env_errors`, and the channel gets the screenshots.

`fixtures/margin_rules.yaml` holds the brackets of rule R02 (2.8.3). Until Phase 0 pins the paperMoney formula from twenty staged tickets, `pinned` is false and R02 checks sign and bracket only, labelled "bracket check" in the report:

```yaml
# schemas/margin-rules.schema.json  (fixtures/margin_rules.yaml)
margin_rules_file:
  schema_version: int = 1
  pinned: bool = false                  # true once Phase 0 has read the formula per asset class
  brackets: list[{asset: enum[equity|option|future|future_option|forex|index], account: enum[margin|ira], of: enum[notional|initial_margin], low: num, high: num}]   # the share of `of` the Buying Power Effect may take, low and high inclusive
  initial_margin: map[str, num]?        # per futures root: the exchange initial margin per contract
```

### 4.10 Popup catalog entry

The popup catalog names everything the app throws on its own, with an action for each (policy in 7.8 Popup catalog policy and starter entries; the merged starter table with ids `POP-01` onward is 12.8). Before every step and every capture the Executor enumerates the top-level windows of the process tree by PID and matches each unexpected one against the catalog. An unmatched window while the current step's `order_stage` is set ends the run `aborted_safety (reason: unknown_dialog_in_order_stage)`, since an unknown dialog over a staged order may be a send path. Otherwise the flow is quarantined (`not_run (reason: quarantine)` from the next run until a human reviews the entry) and the window goes to the agent with a dismiss-only toolset, logged as an `unknown_popup` finding with a proposed catalog entry; with agent mode disabled the Executor sends WM_CLOSE, and a window that persists makes the step `failed` with the same finding. An Order Confirmation dialog is never in the catalog as something to dismiss; only a flow owns it.

```yaml
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
```

What each action produces is fixed. `dismiss_key` and `click` dismiss, log a hit with a screenshot, count per run and per build, and the step continues; a per-build rise in hits is a `popup_rate_increase` finding in the Unexpected section. `wait` polls up to `wait_timeout_ms` for the window to leave, then falls through to `on_abort`. `relogin` runs the re-login flow of 7.7 with its own checkpoints and the paper-mode assertion. `abort` ends the flow: the step is `failed`, the checkpoint in progress takes `on_abort.outcome` (`env_error` with the gate and reason named, or `invariant_violation` with the invariant id), every later checkpoint of the flow is `not_run (reason: step_failed:<step>)`, teardown runs, and the sentinel closes the window with WM_CLOSE; `aborted_run` additionally ends the run with `run_outcome`. `ignore` is for windows that are part of the product state (a detached window the corpus declares) and must never be closed.

Worked example, four entries of the file's `entries` list (title regexes are drafts until Phase 0 reads the real windows):

```yaml
- popup_id: POP-02
  name: popup.whats-new
  class: dismissable
  title_regex: "^What'?s New"
  template: popups/templates/whats-new-header.png
  action: click
  target: shell.dialogs.whats-new.close
  severity: info
  expected_phases: [login, workspace_load]
  owner: tos-shell-ui
  status: unverified
  first_seen_build: main-1998.2.0-4f1c9a2
- popup_id: POP-04
  name: popup.session-ended-elsewhere
  class: env
  title_regex: "(?i)logged in (from|on) another"
  action: abort
  on_abort: {outcome: env_error, gate: authenticated, reason: session_kicked}
  severity: high
  expected_phases: [running]
  owner: sentinel-ops
  status: unverified
  first_seen_build: main-1998.2.0-4f1c9a2
  notes: Re-login only if Phase 0 unknown 6 says a second login kicks the first (7.7); the on-call channel is alerted either way.
- popup_id: POP-10
  name: popup.java-exception-dialog
  class: app_failure
  title_regex: "(?i)^(java|exception|error)"
  action: abort
  on_abort: {outcome: invariant_violation, invariant_id: INV-14}
  severity: critical
  expected_phases: [launch, login, workspace_load, running, teardown]
  owner: sentinel-ops
  status: unverified
  first_seen_build: main-1998.2.0-4f1c9a2
  notes: The app is still running, so this is INV-14 on the checkpoint being captured, a tier-1 finding with the log slice attached (2.7); a dead or frozen process is the app_alive assertion of 4.7 instead, never this entry.
- popup_id: POP-01
  name: popup.launcher-update
  class: env
  title_regex: "(?i)(updat|download)ing"
  action: abort
  on_abort: {outcome: env_error, gate: launched, reason: version_mismatch}
  severity: high
  expected_phases: [launch]
  owner: sentinel-ops
  status: verified
  first_seen_build: main-1998.2.0-4f1c9a2
  notes: Must never appear under pinned-install branch A; the build under test is no longer the one requested.
```

| Rule | Check | Severity |
|---|---|---|
| L-POP-01 | `popup_id` matches `^POP-[0-9]{2,}$` and is unique; ids beyond the 12.8 starter rows continue the sequence; `name` unique; `title_regex` compiles; `retired: true` requires `retired_reason`; the file is `{schema_version, entries}` (a bare list warns in Phase 0 and is an error from Phase 1) | error |
| L-POP-02 | `dismiss_key.key` is not Enter, Space or Ctrl+Enter | error |
| L-POP-03 | a `title_regex` that matches "Order Confirmation", "Confirm" or "Send" is `class: safety` with no input action and `on_abort: {outcome: aborted_run, run_outcome: aborted_safety, reason: kill_switch}` (POP-14 of 7.8: the kill switch fires after 30 s without an owning step); a `click` target whose OCR text is Accept, Agree, Confirm, Send or Yes is refused | error |
| L-POP-04 | `class` present; `click` requires `template` and an existing `target`; `abort`, `relogin` and `wait` require `on_abort`; `app_failure` entries route to INV-14 through `on_abort`; `on_abort.reason` is in `taxonomy.yaml` for the named outcome; `invariant_id` exists in the 2.7 catalog | error |
| L-POP-05 | two entries whose `title_regex` both match one title of either entry's `seed_titles` | warn |
| L-POP-06 | `last_seen_build` more than three releases old | warn (flag for removal) |

### 4.11 Mutant catalog entry

Mutants are the proof that the detector can see. One catalog, `canaries/mutants.yaml` in `sentinel-config`, holds every mutant: ids M01 to M33 exactly as 12.5 tables them (12.5 is authoritative for those rows), then further entries from M34 in sequence, seed 20260101, M25 the identical control. Image mutants are applied to the approved baseline of a checkpoint with that seed and injected as candidate images through the same `evaluate_checkpoint` entry point and harness commit as a real pair (5.15); behavioural mutants drive the real app through a `canary` flow. Every mutant outcome is flagged `synthetic: true`, never promotes a baseline, never creates a decision record and never counts in product metrics. The Canary Service records one of the `canary_states` per mutant per night, `detected` or `missed`; a `missed` on a `money_cell` mutant makes the night's state `detector_blind` and the missed mutant's record `harness_error (reason: detector_blind)`, a class below its rolling floor makes it `detector_degraded`, one mutant that cannot be generated is that mutant's row state only, and the night is `generation_failed` when a critical checkpoint has no generated mutant or the stage generated none (10.3 Canary Service and the mutant catalog). The Nightly run's outcome stays `complete`; `detector_blind` or `generation_failed` sets `system_state.detector_state`, pages P1 and puts the banner on every report until the next green canary (1.4 G3). A nightly re-evaluation of one real merge-run pair through the canary entry point that does not reproduce its outcome is `harness_error (reason: canary_path_divergence)` (5.15).

```yaml
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
```

Worked example, three entries of the file's `entries` list (M02, M03 and M12 of 12.5; M02 and M03 bind to `s07` of the stock limit flow of 4.7, whose regions file uses the `rgn.confirm.*` ids of the 4.8 example, and M12, the acceptance mutant of 10.10, to the Flexible Grid cell sweep step as 10.3 and 10.10 bind it, which are authoritative for its entry):

```yaml
- mutant_id: M02
  name: mut.confirm.send-erased
  checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07
  class: erased_control
  mode: image
  money_cell: true
  generator: {op: erase, params: {fill: background_sample}, seed: 20260101}
  region_ref: rgn.confirm.send-button
  expected: {outcomes: [invariant_violation], invariant_id: INV-10}
  owner: sentinel-canaries
- mutant_id: M03
  name: mut.confirm.qty-digit
  checkpoint_id: flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07
  class: swapped_digit
  mode: image
  money_cell: true
  generator: {op: replace_glyph, params: {from: "0", to: "8", position: last}, seed: 20260101}   # quantity 100 -> 108
  region_ref: rgn.confirm.qty
  expected: {outcomes: [assertion_failed], assertion_id: qty}
  owner: sentinel-canaries
- mutant_id: M12
  name: mut.ladder.overlap-grid
  checkpoint_id: sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360
  class: overlapped_panels
  mode: image
  money_cell: false                     # the ladder is a tier-1 region, not a money cell (3.8): a non-money mutant
  generator: {op: translate, params: {dy: -14, layer: rgn.ladder.viewport, fill: background_sample}, seed: 20260101}
  region_ref: rgn.ladder.order-strip
  expected: {outcomes: [invariant_violation, collateral], invariant_id: INV-01}   # INV-03 and INV-11 fire as well
  owner: sentinel-canaries
```

| Rule | Check | Severity |
|---|---|---|
| L-MUT-01 | `mutant_id` matches `^M[0-9]{2,}$` and is unique; the catalog contains M01 to M33, each equal to its 12.5 row (class, bound checkpoint, transformation, expected outcome, `money_cell`), and further ids run from M34 in sequence; `seed` present; `region_ref` exists in the checkpoint's regions file; `checkpoint_id: "*"` only for class `aa_control`, which may then omit `region_ref`; the file is `{schema_version, entries}` (a bare list warns in Phase 0 and is an error from Phase 1) | error |
| L-MUT-02 | `money_cell = true` requires `expected.outcomes` within `{collateral, invariant_violation, assertion_failed, landmark_missing}` and, for `invariant_violation` or `assertion_failed`, at least one of `invariant_id` or `assertion_id`; the control classes `aa_control` (M25) and `sub_floor` (M20) are exempt and expect `identical` or `noise` by design, and class `gateway` (M28) is exempt and expects `step_status: blocked_by_gateway` with the later checkpoints `not_run`; no other mutant expects `identical` or `noise`, and `needs_human` is expected only with reason `global_shift` (M21, M23). `expected.reason`, when present, is a reason code of `taxonomy.yaml` for one of `expected.outcomes`, and the class fixes it: `global_shift` requires `global_shift`, `sub_floor` allows only `below_floor`, `gateway` requires `step_failed:<step>` with `not_run`; `step_status` is set only by class `gateway`, and `redaction_hit` only by class `redaction` (M19), which expects `redaction_hit: true` | error |
| L-MUT-03 | every critical checkpoint (a checkpoint on a surface with `critical: true`, 4.5) has at least two `behavioral` entries bound to it, counting a behavioral entry whose `canary_flow` checkpoint is on the same surface | error |
| L-MUT-04 | `behavioral` requires `canary_flow` of `class: canary`; `image` forbids it; a `canary_flow` may hold one `order_stage` step that clicks Confirm and Send, run under the order-stage gateway rules, with its Order Confirmation dialog closed by Delete through the dismiss allowlist, as attestation item A9 does (6.3); it never targets Send | error |
| L-MUT-05 | `color_swap` expects `invariant_violation` on INV-15. Not lint: the six brief classes (erased button, shifted panel, swapped digit, red/green swap, clipped label, overlapping panels, written `erased_control`, `shifted_panel`, `swapped_digit`, `color_swap`, `clipped_label`, `overlapped_panels`) at least once per tier-1 checkpoint, and 20 to 30 mutants per tier-1 checkpoint with an approved baseline (10.3), are counted over the mutants generated at Nightly generation; a checkpoint short of either is a coverage-gap line in the morning report | error (the INV-15 expectation); the counts are a report line |
| L-MUT-06 | `invariant_id` values exist in the 2.7 catalog (`INV-01` to `INV-15`) | error |

### 4.12 Environment fingerprint

The fingerprint is computed from live reads at gate `provisioned` (OS fields) and gate `launched` (app fields), never from a snapshot name; the cheap fields are re-read at every flow boundary and after every relaunch (7.4). The fields, how each is read and its class are the table of 7.4 Environment fingerprint: 22 fields, `render_probe_hash` of 3.3 among them; the schema below carries exactly those names and nothing else (L-FPR-01). Fields of class `match` must be identical between candidate and base or every checkpoint of the run is `not_comparable`; class `record` fields serve diagnosis and redaction. Thresholds are valid only for the fingerprint they were calibrated on. Both sides of a masked_live pair are captured on two identically fingerprinted rigs in lockstep, and the equality is asserted before capture (11.2).

```yaml
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
```

Model ids and prompt hashes are not fingerprint fields; they are stored on every Reviewer verdict (3.13) and in the run summary.

Worked example (abbreviated hashes):

```yaml
schema_version: 2
fingerprint_id: sha256:9a3f...c1e2
run_id: run-2026-11-09-pr-0931
side: candidate
rig_id: rig-01
read_at: 2026-11-09T14:02:11-05:00
match:
  build_id: branch-1998.3.0-7be42d0
  jre_release: {sha256: "sha256:77b1...0d4e", java_version: "17.0.12"}
  vmoptions_hash: "sha256:21cc...8f90"
  java2d_pipeline: {pinned: software, confirmed: software}
  windows_build: {current_build: "26100", ubr: "2314"}
  display: {width: 1920, height: 1080, bits: 32, frequency: 60, scale_percent: 100, dpi: 96, awareness: PER_MONITOR_AWARE_V2, monitors: 1}
  cleartype: {smoothing: true, type: FE_FONTSMOOTHINGCLEARTYPE, gamma: 1800, contrast: 1400, orientation: RGB}
  gpu: {name: "Hyper-V Video", driver_version: "10.0.26100.1"}
  os_theme: {apps_light: false, system_light: false, transparency: false, animations: false}
  app_theme: {scheme: dark, font_size: medium}
  workspace_hash: {ws.tier-a.futures-trader: "sha256:5b7d...91c0"}
  settings_profile_hash: "sha256:19ab...c0de"
  time_zone: {os: "Eastern Standard Time", app_display: America/New_York, chart: America/New_York}
  keyboard: {layout: "0x0409", layout_count: 1, toggle_keys: {caps: false, num: true, scroll: false}, locale: en-US}
  data_epoch: {epoch_id: "sha256:3d1a...b7c4", data_mode: none}
  input_desktop: Default
  harness: {version: 1.4.2, ocr_engine: tesseract-5.3.4, ocr_model: eng-best-4.1, capture_lib: mss-9.0.1, settle_params_sha256: "sha256:4e4e...1a1a", diff_engine_params_sha256: "sha256:7c7c...2b2b", redaction_version: r3, region_registry_version: c41a0f2, config_commit: c41a0f2, config_tarball_sha256: "sha256:6a6a...4d4d", allowlist_sha256: "sha256:9d9d...3c3c"}
  render_probe_hash: "e3b0c44298fc1c14"
record:
  trading_24h_list_sha256: "sha256:0f0f...5e5e"
  jar_listing_hash: "sha256:ab12...ef90"
  account: {account_id_sha256: "sha256:51f2...0a0a", login_sha256: "sha256:77aa...bb88", role: empty}
  rig: {hostname: tos-rig-01, hypervisor: hyperv, hypervisor_version: "10.0.26100", snapshot_ids: [snap-2026-10-28-base], vm_image_id: tos-rig-win11-2026-10-28}
  run_date: 2026-11-09
```

The JRE version is read from the bundled runtime's release file; the exact JRE current Windows builds bundle is unverified, so the field is whatever that file says. Whether `jcmd` can attach to the launcher's JVM is a Phase 0 check (7.4); when it cannot, `java2d_pipeline.confirmed` is `unknown` on both sides and the Help > About render comparison carries the check alone.

**Calibration key.** Floors depend on how pixels are rendered, not on the data or the corpus files, so calibration is keyed on a rendering subset of the `match` fields rather than on `fingerprint_id`. `calibration_key` is the sha256 over the canonical JSON of `windows_build`, `display`, `cleartype`, `gpu`, `os_theme`, `app_theme`, `jre_release`, `vmoptions_hash`, `java2d_pipeline`, `render_probe_hash` and the `harness` subfields `capture_lib`, `settle_params_sha256` and `diff_engine_params_sha256`. It leaves out `build_id`, `data_epoch`, `workspace_hash` and every other field, so a new data epoch or a corpus change never discards a whole calibration; the rows of 7.13 What invalidates calibration say which changes invalidate the floors of one checkpoint or one region. A key that differs from a calibrated key only in `jre_release` or `render_probe_hash` takes over that key's floors once a Nightly A/A pass on the new key passes (10.6); until then its regions are `uncalibrated`. The calibration lives in `calibration_floors` (8.3) and in `calibration/<hex>.yaml`, `<hex>` being `calibration_key` without its `sha256:` prefix (4.16).

| Rule | Check | Outcome on failure |
|---|---|---|
| L-FPR-01 | every `match` field non-null; `fingerprint_id` recomputes; the field set equals 7.4's table plus `render_probe_hash` (a CI test compares the schema with the table) | `error` in CI; a schema that disagrees with 7.4 at run start is `aborted_harness (reason: config_invalid)`; a `match` field that cannot be read on the rig is `env_error (gate: provisioned, reason: fingerprint_unreadable:<field>)` on every scoped checkpoint (7.4) |
| L-FPR-02 | `match` equals the rig specification of 7.1 for the declared config (1920x1080, 100 percent, 96 dpi, en-US, America/New_York, Caps Lock and Scroll Lock off and Num Lock on (12.7 R4), `input_desktop: Default`, pinned pipeline, allowlist hash equal to the config commit's) | `env_error (gate: provisioned, reason: rig_spec_mismatch)` on every scoped checkpoint, run `complete_with_env_errors`, on-call channel |
| L-FPR-03 | candidate and base `match` identical apart from `build_id`, which must equal each side's requested build | every checkpoint of the shard `not_comparable (reason: fingerprint_mismatch)` with the differing field names in `detail`, nothing captured, run `complete_with_env_errors`, on-call channel; a `build_id` that is not the requested build is `env_error (gate: launched, reason: version_mismatch)` instead |
| L-FPR-04 | a calibration exists for the fingerprint's `calibration_key` (above), or for a key it may take over after a passing Nightly A/A | none exists: every region is `threshold_source: uncalibrated`, every diff with a changed pixel is `needs_human (reason: uncalibrated)` at threshold 0, a Calibration run is queued and the Reporter prints the count (3.9 step 6); never `not_comparable`. A calibration that lacks one region's floor, or whose floors for a checkpoint or region a 7.13 row invalidated, is the same `uncalibrated` case for that checkpoint or region alone (4.8) |
| L-FPR-05 | `render_probe_hash` equal at Hamming 0 between sides | `not_comparable (reason: fingerprint_mismatch)` with `detail: render_probe_hash`; the morning report says a rasterization change happened inside the same Windows build |

### 4.13 Outcome record

Four record shapes: one per level of brief 1.5, plus the finding record that carries what a human sees about a checkpoint or a run without being an outcome. Enums are imported from `taxonomy.yaml`. The Orchestrator writes each outcome record once (single-writer rule, 8.2); the only later changes are three. First, `decision_ref`, set when a decision suppresses the finding. Second, the `review_state: pending` case of 8.3: when an asynchronous Reviewer verdict lands, the outcome becomes what 3.11's table gives, which for a pending item is only `intended` or `needs_human` with reason `reviewer_no_match` or `reviewer_uncertain` (a late verdict never produces `collateral` or `noise`, and the write is refused if it tries; a declared removed surface the Reviewer does not confirm is `needs_human (reason: reviewer_no_match)`, 3.11 row 25a), and `review_state` goes from `pending` to `done`; if a human decision already exists on the item, the verdict is attached as an annotation only and changes neither the outcome nor the decision (3.11, 10.7). Third, the end of review for a pending item that never got a verdict: when the per-run spend ceiling ends the run, its reason becomes `reviewer_cap` with `cause: dollar_cap` in `detail`, and when the Reviewer job gives up on it, it stays `reviewer_unavailable`; in both cases the outcome stays `needs_human` and `review_state` goes from `pending` to `done` (8.3 trigger, 10.7, 11.4).

```yaml
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
```

**Reason vocabulary.** `taxonomy.yaml` lists these codes under `reasons.<outcome>`; a record whose reason code is not in the list for its outcome is refused at write (L-OUT-01). Codes with a parameter take it after a colon.

| Outcome | Reason codes | Introduced by |
|---|---|---|
| `noise` | `below_floor`, `decision:<id>` (an approved noise signature or an active mask decision; 8.3 stores the code `decision` with the id in `reason_detail` and in `decision_id`) | 3.11, 4.14 |
| `intended` | none, or `decision:<id>` | 3.11 |
| `collateral` | none (including an undeclared removed surface, 3.11 row 26), `decision:<id>` (a computed `collateral` that matches a `confirm_bug` decision: the outcome is kept and the finding is filed under the ticket, 4.14), `manifest_missing` (no-manifest case (a), 4.3) | 3.11, 4.3, 4.14 |
| `needs_human` | `reviewer_uncertain` (confidence below 0.80, partial match, no visible difference or cannot judge), `reviewer_no_match`, `reviewer_unavailable` (API down, refusal, schema violation, batch expired; `review_state: pending` while queued), `reviewer_cap` (beyond the run's reviewer call cap of 3.15, PR 60, merge 200, RC 500, or excluded by the weekly dollar cap, or still pending when the per-run spend ceiling ended the run, those two with `cause: dollar_cap` in `detail`, 11.4; the pending case is the third later change of the intro and 8.3's trigger permits it; a reviewer cap never ends a run), `no_manifest`, `uncalibrated` (region or fingerprint without a floor, threshold 0), `rescued_capture`, `global_shift`, `oracle_unreadable`, `bounds_unreadable` | 3.7, 3.11, 3.15, 4.3, 4.8, 5.5, 5.10 |
| `invariant_violation` | `INV-01` to `INV-15` (the 2.7 ids; several fire as several findings, the outcome names the first by catalog order) | 2.7 |
| `assertion_failed` | `assertion:<id>`, including three built-ins: `assertion:app_alive`, whose cause (`jvm_crash`, `hang`, `oom`, `process_exit`) goes in `detail` and never in `reason` (written `assertion_failed (assertion: app_alive)` with the cause in `detail`; this form is authoritative everywhere, 10.1); `assertion:size_reached` (2.5.1); and `assertion:soak.<metric>` (10.11) | 2.6.2, 2.5.1, 10.1, 10.11 |
| `not_settled` | `frames_unstable`, `anchor_missing`, `anti_anchor_present`, `timeout`, `unexpected_window:<title>`, `unstable` (targeted re-run disagreed), `aa_drift` (Nightly A/A) | 3.5, 3.10, 5.7, 10.5, 10.6 |
| `not_comparable` | exactly three: `fingerprint_mismatch` (the differing fields in `detail`, which is how a data epoch, bounds source or accessible-tree change is named), `window_mismatch`, `surface_mismatch`. A missing calibration is `needs_human (reason: uncalibrated)`, never this | 3.3, 4.12, 5.7 |
| `landmark_missing` | `landmark:<id>` (Region Registry landmark, with the side in `side`), `locator:<step>` (step locator exhausted on every rung), `redaction_landmark:<id>` (a redaction anchor not found; nothing stored, nothing released), `probe:<Pn>` (a registry validation probe P1 to P8 whose anchor was not found) | 1.10, 2.2.3, 3.7, 5.5, 6.10 |
| `env_error` | with `gate` (a state name of 5.2, `preflight.<item>`, `connectivity` or `rig_lost`): `gate` (the gate's own check failed with no finer cause; the reason of every `gate: preflight.<item>` failure that has no finer code, for example `env_error (gate: preflight.R1)`), `version_mismatch`, `version_changed_mid_run`, `rig_spec_mismatch`, `fingerprint_unreadable:<field>`, `snapshot_revert_failed`, `install_failed`, `session_kicked`, `authentication_challenge`, `authentication_failed`, `timeout` (login screen absent within the login max wait, 7.7; or, with the gate in force, a run still before `running` at its dead-man deadline, 10.2), `relogin_budget` (checkpoints left when the re-login budget K is spent, 7.7), `vault_unreachable`, `agreement_pending`, `live_mode_selected`, `attestation_unreadable`, `identity_unreadable`, `fixture_drift`, `fixture_maintenance_needed`, `delay_state`, `data_epoch_mismatch`, `prices_stale`, `ondemand_timeout`, `ondemand_state`, `precondition:<check>`, `backend_degraded` (gate `connectivity`, 7.11), `occluded` (with the gate in force, `gate: running` for a capture or an input batch, 5.7, 7.8), `maintenance` (a scheduled maintenance notice, POP-09, `gate: connectivity`, 7.8), and, on unreached checkpoints of an `aborted_safety` run, the run's abort reason (`kill_switch`, `paper_mode_mismatch`, any code of the `aborted_safety` row) with the aborting gate (1.10) | 4.2, 4.9, 4.10, 5.2, 6.3, 6.4, 6.9, 7.2, 7.3, 7.6, 7.7, 7.8, 7.9, 7.11, 10.7, 12.7 |
| `harness_error` | `registry_entry_not_found`, `bounds_source_unavailable`, `ocr_calibration`, `export_header_mismatch`, `mask_budget_exceeded` (a panel's masked plus non-identity redacted share over its budget, or a mask or redaction box on a money cell; the panel and share in `detail`, 4.8), `store_write_failed` (a record the Orchestrator could not write or repair, L-OUT-02, 8.9), `artifact_persist_failed` (an artifact write or read-back that failed after one retry, 8.4), `redaction_version_missing` (8.9), `hook_lost`, `orchestrator_died` (the checkpoint in progress when the Orchestrator died, 10.2), `detector_blind`, `detector_degraded` (the missed mutant's record, 10.3), `safety_canary_blind:<canary>` (a missed safety canary's record, the blind detector named, 6.5, 10.3), `canary_generation_failed` (a mutant that could not be generated, 10.3), `canary_path_divergence`, `drill_failed:<item>` (6.5), `validation_self_test` (the registry probe self-test of 10.3 did not yield its one `unregistered_surface`), `chaos_unobserved` (a chaos stall that produced neither alert nor dead-man row, 10.2), `aa_drift_global`, `chaos_rule_stuck`, `invariant_on_aa` (an enabled invariant that fired on a Nightly A/A pair, which is a harness bug, 2.7, 10.6), `exception:<class>`, and, on unreached checkpoints of an `aborted_harness` run, the run's abort reason (`dead_man`, `start_failed`, `orchestrator_restart_budget`, `watchdog_dead`, `preflight.<item>`, any code of the `aborted_harness` row) (1.10). `step_failed` is never a `harness_error` reason | 2.2.3, 2.7, 2.8.1, 3.7, 3.8, 5.4, 5.10, 5.12, 5.15, 6.5, 7.11, 8.4, 8.9, 10.3, 10.6 |
| `not_run` | `entitlement` (including every post-Send surface until `paper_send_ok`; there is no separate send-policy reason), `budget`, `scope`, `quarantine`, `step_failed:<step>`, `base_step_failed`, `app_crash`, `model_unavailable` (an Explorer session or an agent-only step skipped because the model endpoint is down, 10.7) | brief 1.5, 2.6.3, 3.1, 5.2, 5.6, 5.14, 6.7, 7.11, 10.7 |
| `unregistered_surface` | `probe:<P1..P8>` with the enumerated label in `detail` (8.3 stores the code `probe` with the probe id in `reason_detail`) | 2.2.3 |
| `needs_baseline` | `new_surface`, `base_not_settled`, `base_env_error`, `base_harness_error`, `lineage_missing` (RC), `reuse_missing`, `explorer_replay` (input B of 3.11) | 1.10, 2.9, 3.1, 3.11 |
| run `aborted_safety` | `paper_mode_mismatch`, `attestation_mismatch`, `unexpected_working_order`, `kill_switch`, `attempted_send`, `undeclared_order_row` (an undeclared Working, Filled or Canceled row found at teardown or by the post-run proof, 5.2, 6.6), `live_identity` (a live-format account id read at the paper-mode assertion: not on the allowlist and not matching the paper prefix, 6.4), `drill_order_placed` (a fire drill that produced a new order row, 6.5), `unknown_dialog_in_order_stage` (an unmatched window while `order_stage` is set, 4.10); any other readable selector text that is not an allowlist entry is `paper_mode_mismatch` (6.4) | 1.10, 6.2 to 6.7 |
| run `aborted_budget` | `wall_clock` (the run's wall budget less the report margin reached, 5.2, 11.2), `app_restart_budget` (a fourth app failure in a shard, a crash or hang in the gate sequence of the shard's first launch or of a relaunch included, 5.2, 10.2), `dollar_cap` (the run's model spend, Reviewer, agent rescue and Explorer together, reached the 50-dollar per-run ceiling of 11.4: no further model call, the run ends at the next flow boundary, uncaptured checkpoints `not_run (reason: budget)`, items still awaiting a verdict `needs_human (reason: reviewer_cap)` with `cause: dollar_cap`; the 25-dollar Explorer session cap and the weekly cap never end a run, 10.7), `pool_saturated` (a PR run still queued at its start deadline; a queued merge run is coalesced into the next merge run and an RC run goes to the release manager instead, 11.2), `rate_cap` (a 6.4 rate cap on logins, symbol changes or staged orders reached), `superseded` (a newer RC build arrived while the RC run was in flight: the run stops at the next flow boundary, its unreached checkpoints `not_run (reason: budget)`, and the new RC run starts at once, 11.2) | 5.2, 6.4, 10.2, 10.7, 11.2, 11.4 |
| run `aborted_harness` | `config_invalid`, `preflight.<item>` (a failed H item of 12.7, written as 12.7 writes it, for example `preflight.H8` when the Watchdog is absent at run start), `dead_man`, `start_failed` (a Nightly or Calibration run that did not reach `running` by its start deadline, 11.2), `secret_leaked`, `secret_scan_unavailable`, `egress_unlogged`, `base_build_missing`, `taxonomy_check_failed` (the post-run taxonomy query, including L-OUT-07, failed), `orchestrator_restart_budget`, `hook_lost` (second hook host restart in one shard, 5.4.2), `bounds_source_unavailable` (the bounds bridge still down after one relaunch, 2.7), `watchdog_dead` (the Watchdog's reverse heartbeat stale mid-run, 5.14), `heartbeat_unwritable` (5.14), `disk_full` (only the Watchdog's mid-run free-space check ends a run this way, including the immediate check an `ENOSPC` requests, 10.2; a single artifact write that fails, `ENOSPC` included, is checkpoint `harness_error (reason: artifact_persist_failed)`, 8.4, 8.9), `rig_lost` (a second loss of the same shard, 10.7), `dpi_awareness`, `store_write_failed` (8.9), `artifact_persist_failed` (8.4, 8.9), `import_conflict` (8.9), `canary_generation_failed` (a Nightly run on which no canary could run at all). A blind identity canary is not a run abort: its row is `harness_error (reason: safety_canary_blind:identity)` and the dependent flows are `not_run (reason: quarantine)` while the run stays `complete` (6.4) | 1.4, 1.10, 2.7, 3.4, 4.1, 4.2, 5.2, 5.4, 5.12, 5.14, 6.9, 6.10, 8.9, 10.2, 10.3, 10.8, 11.2 |
| run `complete_with_env_errors` | the cause, when it is not an `env_error` checkpoint: `unproven_proof` (6.6), `teardown_fault` (5.2), `relogin_budget` (7.7) | 5.2, 6.6, 7.7 |

A step record carries a reason only for an export step: `export_timeout` or `export_unavailable` (4.7 `export`).

**Finding vocabulary.** `taxonomy.yaml` lists these under `finding_kinds`; 8.3's `findings.kind` CHECK is generated from it. Findings never change an outcome.

| Kind | Meaning | Section |
|---|---|---|
| `collateral`, `intended`, `needs_human`, `invariant_violation`, `assertion_failed`, `landmark_missing`, `needs_baseline`, `unregistered_surface` | the finding is the outcome, carried with its evidence (a `needs_baseline` finding is what `approve_baseline` acts on, 4.14) | 1.10 |
| `dependency_map_gap` | static edge or unowned root missing from the Dependency Map, with the proposed PR | 2.10.2, 4.4 |
| `manifest_missing` | UI-affecting PR with no manifest entry (case (a) of 4.3) | 4.3 |
| `unknown_popup` | unmatched top-level window, with the proposed catalog entry | 4.10, 7.8 |
| `popup_rate_increase` | catalog hits per build rose after a build change | 7.8 |
| `locator_failure` | a step's scripted rungs all failed, whether the agent rescued it (`passed_via_agent_fallback`) or not (`failed`); every rung's result and the proposed anchor patch | 5.5 |
| `app_crash` | process gone or EDT frozen, with hs_err_pid, dumps and log slice; tier 1 | 4.7, 10.1 |
| `explorer_candidate` | an Explorer observation with its signature, trace and replay result | 2.9 |
| `layout_shift`, `landmark_moved`, `color_shift`, `window_geometry_changed` | Diff Engine findings beside the outcome | 3.3, 3.7 |
| `global_rendering_shift` | run level: the granularity-0 pre-pass found a text-edge-only shift across the run; its checkpoints are one `needs_human (reason: global_shift)` cluster | 3.7 |
| `settle_regression` | time to settle above 2x the calibration p50 | 3.5 |
| `gateway_integrity` | an untokened or foreign input event reached the hook, or the hook was reinstalled | 5.4.4, 5.4.8 |
| `new_log_signature` | a new exception signature in the client log slice with the app alive; tier 2 | 10.11, 2.13 B14 |
| `jab_tree_changed` | under DR-01 Branch A, the accessible tree hash at build intake differs from the previous build; the run falls back to Branch B bounds and the registry owner is told; a finding, never a `not_comparable` reason | 11.8 DR-01 |
| `mask_budget_exceeded` | a landmark move or geometry change pushed a panel's masked plus non-identity redacted share over its budget, or a mask or redaction box onto a money cell; beside `harness_error (reason: mask_budget_exceeded)` | 3.8, 4.8, 6.10 |
| `mask_expired`, `floor_exception_expired`, `invariant_exception_expired` | a config record stopped applying at run start | 4.5, 4.8 |
| `redaction_miss` | the OCR sweep caught an identifier the anchor list missed | 6.10 |
| `secret_leaked` | post-run plaintext scan hit; the run is `aborted_harness` | 6.9 |
| `explorer_order_stage` | the ticket or dialog flag was set in an Explorer session: all input dropped, the window closed by the gateway's recovery path, the session ended, counted toward the per-run trip of ten; S1, paged and notified by the safety routing of 9.10, the Explorer owner told; the run continues | 5.4.6, 6.8, 9.10 |
| `missed_in_pr_run` | a merge-run finding on a surface its PR run had in scope but did not report ("missed in PR run"), or on a surface its PR run had `not_run (reason: scope)` that the candidate's dependency index reaches from a class a merged PR changed ("missed by static impact", `detail: static_impact`); opens a harness task | 1.8, 9.4 |
| `artifact_missing` | an object named by an outcome is missing or fails its hash in the weekly audit or on a report page request | 8.6, 8.9 |
| `orphaned_baseline` | an approved baseline whose checkpoint id no longer exists at the config commit | 8.5, 9.8 |
| `canary_miss` | a mutant ended `identical`, `noise` or a suppressed decision row | 10.3 |
| `intended_change_not_observed` | a manifest entry whose declared surfaces showed no diff | 2.12, 9.2 |
| `flow_maintenance_needed` | a Recorder anchor no longer matches the latest approved capture | 5.16, 11.5 |
| `harness_error` | a harness exception that did not take a checkpoint with it (an event that never reached the hook) | 5.4 |
| `memory_warning`, `hardening` | low-memory dialog per build; foreign-process occlusion on a hardened rig | 7.8, 7.12 |

`lockstep_slip` and `rerun_budget_exhausted` are run-health counters, not findings (3.1, 3.10). An outcome's `reason` and `detail` are what the coverage matrix prints for a `not_run` cell and what the on-call page carries for an `env_error`; a code outside this table cannot be written, so a new failure mode is a change to this section and to `taxonomy.yaml` before it is a line of code.

Required fields per outcome:

| outcome | Required beyond the common fields |
|---|---|
| `identical`, `noise` | `diff`; `noise` also needs `classification.by = rule` with either `below_floor` in `reason` or `decision:<id>` and a `decision_ref` to an approved noise signature |
| `intended` | `diff`, `manifest_entry_ref`, `classification.by = reviewer` with `confidence`, or `decision:<id>` with `decision_ref` |
| `collateral` | `diff`, `primitive`, `manifest_entry_ref` of the entry that touched the primitive (absent only with reason `manifest_missing`), `classification` with `by = rule` (surface declaration decides; the Reviewer only adds `description`); for an undeclared removed surface (B `removed`, 3.11 row 26) the two probe screenshots (label on base, absent on candidate) replace `diff`, and `primitive` and `manifest_entry_ref` are present only when the Dependency Map links the surface to a primitive a manifest entry touched |
| `needs_human` | `diff`, `reason` from the vocabulary; `review_state: pending` only with `reviewer_unavailable` or `global_shift` while the item is queued |
| `invariant_violation` | `invariant_ids`, `reason` the first id, `artifacts.candidate` |
| `assertion_failed` | `assertion_ids`, `reason: assertion:<id>`, expected and extracted values in `detail` |
| `not_settled`, `landmark_missing` | `reason`, `side`, `detail` naming the anchor or landmark and the wait used |
| `not_comparable` | `reason` from the vocabulary, differing field names in `detail` |
| `env_error` | `gate`, `reason`, `artifacts.candidate` (last screenshot) when a screen was captured before the failure |
| `harness_error` | `reason` and a Watchdog event id in `detail` |
| `not_run` | `reason` from the vocabulary; `step_failed:<step>` names an existing step of `flow_id` |
| `unregistered_surface` | `reason: probe:<Pn>`, `detail` with the enumerated label and where it was seen |
| `needs_baseline` | `reason`, `side`; the Reporter attaches the closest baseline by perceptual hash |

Worked example, the ladder overlap from the brief's scenario:

```yaml
outcome_id: oc-run-2026-11-09-pr-0931-000412
run_id: run-2026-11-09-pr-0931
pair_id: pr-branch-1998.3.0-7be42d0-vs-main-1998.2.0-4f1c9a2
checkpoint_id: sweep/trade.active-trader.ladder/flexgrid/640x720
surface_id: trade.active-trader.ladder
test_kind: sweep
workspace_id: ws.tier-c.sweep-harness
persona: active_trader
profile_id: paper-margin
size_step: {host: flexgrid, w: 640, h: 720}
data_mode: masked_live
fingerprint_id: sha256:9a3f...c1e2
data_epoch_id: sha256:3d1a...b7c4
cand_rig_id: rig-01
base_rig_id: rig-02
settle_ms: 1840
outcome: invariant_violation
reason: INV-01
detail: "ladder body (rgn.ladder.bid-column) intersects the order-type row by 18 px at 640x720"
review_state: none
paper_assertion: {account_id_sha256: "sha256:51f2...0a0a", mode: paperMoney, asserted_at: 2026-11-09T14:21:02-05:00}
app_uptime_s: 1912
app_restart_count: 0
diff:
  whole: {pixel_fraction: 0.031, ssim: 0.942, shift: {dx: 0, dy: 0}}
  regions:
    - {region_id: rgn.ladder.bid-column, pixel_fraction: 0.188, ssim: 0.71, above_threshold: true}
    - {region_id: rgn.ladder.header, pixel_fraction: 0.0, ssim: 1.0, above_threshold: false}
  components: [{area: 4120, bbox: {x: 12, y: 388, w: 206, h: 20}}]
diff_pixels: 4120
threshold_source: calibrated
classification:
  category: collateral
  by: rule                              # row 16 of 3.11: undeclared surface, no model call needed
  description: Surface trade.active-trader.ladder is not declared by any manifest entry; FlexibleGrid is touched by tos-48213-1.
  region_pointer: rgn.ladder.bid-column
lower_results: [{outcome: collateral}]
manifest_entry_ref: tos-48213-1
primitive: FlexibleGrid
invariant_ids: [INV-01]
artifacts: {base: "sha256:4c...", candidate: "sha256:9e...", heatmap: "sha256:b1...", crops: ["sha256:77..."]}
recaptures: 2
stability: stable
synthetic: false
captured_at: 2026-11-09T14:21:40-05:00
recorded_at: 2026-11-09T14:21:44-05:00
```

Under the precedence rule of 1.10 the outcome is `invariant_violation`, not `collateral`: INV-01 fired on the candidate capture, and `invariant_violation` outranks `collateral`. The `collateral` classification stays on the record (`classification`, `lower_results`) and on its finding as evidence, by rule (row 16 of 3.11, undeclared surface) and without a model call, which is how the PR comment names both the overlap and the primitive and manifest entry that caused it. The Reviewer's description of the diff, when one is requested, lands on the finding (`description`), never in a `classification.by = reviewer` block on an invariant violation (L-OUT-03).

| Rule | Check | Severity |
|---|---|---|
| L-OUT-01 | `outcome`, `status`, `reason` codes, `gate`, `finding.kind` and `triage_state` values are exactly the lists in `taxonomy.yaml`; the DB `CHECK` constraints of 8.3 are generated from the same file | error (CI and write) |
| L-OUT-02 | the required-fields table above holds for the record's outcome | write refused, Watchdog event, `harness_error (reason: store_write_failed)` on the checkpoint if the Orchestrator cannot repair the record |
| L-OUT-03 | `classification.by = reviewer` never appears with `invariant_violation` or `assertion_failed`; a `decision_ref` never changes those two outcomes | write refused |
| L-OUT-04 | `synthetic = true` records are excluded from every metric query and from baseline promotion (query-level test in CI) | error |
| L-OUT-05 | exactly one `checkpoint_outcome` per `(run_id, checkpoint_id)` (the id already embeds the size step); synthetic mutant outcomes are not checkpoint outcomes of the run and are kept apart, one per `(run_id, mutant_id, checkpoint_id)` (8.3 `synthetic_outcomes`); one `run_outcome` per run; `checkpoint_id` matches the grammar of 4.1 and its kind segment equals `test_kind` | write refused |
| L-OUT-06 | a record without `paper_assertion` from the current session, or whose `account_id_sha256` is not an allowlisted account's, is refused and the run is `aborted_safety (reason: paper_mode_mismatch)`. Exempt: a record that references no capture artifact (an `env_error` or `harness_error` written before any screen was captured, or `not_run` for a checkpoint never captured), which may also omit `fingerprint_id` and `data_epoch_id` | write refused, run aborted |
| L-OUT-07 | every scoped checkpoint of a finished run has exactly one record (the Orchestrator writes `not_run`; for what a dead Orchestrator left, the resumed Orchestrator or the control-host import of the Watchdog's JSONL journal writes `harness_error` with the run's abort reason; the Watchdog itself never writes the store, 10.2); such records reference no capture artifact and are exempt from L-OUT-06 | post-run query; a miss is `aborted_harness (reason: taxonomy_check_failed)` and the report is withheld |

### 4.14 Decision record

A decision is a human action on a finding, keyed by a diff signature so later runs inherit it. The Reviewer never writes one. Decisions live in SQLite (8.3 `decisions`); a nightly export to `decisions/` in git exists for review, not as a source of truth. Each record stores two things: the `action`, which is what the human clicked in the report UI (9.6 One-click actions and who may perform them), and the `verdict`, which is the stored effect that classification reads (8.3: `confirmed_bug`, `intended`, `noise`). The mapping is fixed:

| action (UI) | verdict (stored) | Inherited outcome on a later match | Finding `triage_state` | Ticket |
|---|---|---|---|---|
| `confirm_bug` | `confirmed_bug` | computed outcome unchanged; finding filed under the ticket, no new alert | `confirmed_bug` | required |
| `mark_intended` | `intended` | `intended (reason: decision:<id>)`; on `invariant_violation` or `assertion_failed` the outcome never changes and the finding is suppressed as waived, expiry at most 30 days | `intended` | required |
| `request_mask` | `noise` once the mask PR merges; nothing before | `noise (reason: decision:<id>)` while the mask is active; a request that would put the panel over its mask budget is refused at submit (L-DEC-04) | `noise` | optional |
| `noise_signature` | `noise` | `noise (reason: decision:<id>)` | `noise` | optional |
| `agent_mistake` | `noise` | the Explorer finding's signature is closed; no checkpoint outcome involved | `agent_mistake` | none |
| `expected_behavior` | `noise` | as `agent_mistake`, for behavior that is by design | `expected_behavior` | none |
| `approve_baseline` | `intended` | the capture becomes the approved baseline for the checkpoint or cluster (9.8); not a signature match | `closed` | optional |
| `reject_baseline` | `confirmed_bug` with a ticket when the candidate shows a defect, else `noise` (the capture is not representative) | the candidate `baselines` row becomes `retired` (8.3, 9.6), `needs_baseline` persists until a new candidate is approved, and the finding returns to Unexpected | `confirmed_bug`, or `open` | required for `confirmed_bug` |
| `fixed_in_build` | `confirmed_bug` (a new row that supersedes the active `confirm_bug` decision on the same signature) | from that build on the run asserts the signature is gone; a return sets `status: reopened` | `fixed_pending_verify`, then `closed` after one clean run | required |

```yaml
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
```

A region's tolerance may be lowered by calibration, never raised (9.7): with noise spread the largest pHash distance among the region's A/A pairs and fault separation the smallest distance between its distinct mutants, the region uses the smaller of its default (8, or 6 for tier 1) and fault separation minus 1, and falls back to pHash equality plus the largest component's bounding box quantized to 4 px (`bbox_q4`) when that is at or below the noise spread. A tolerance that calibration lowered is stored on the row so a later change in calibration never silently widens an old decision; an unset `hamming_tolerance` means the default.

Worked example:

```yaml
decision_id: dec-000812
signature:
  checkpoint_id: sweep/trade.active-trader.ladder/flexgrid/640x720
  region_id: rgn.ladder.bid-column
  phash: "c3a5e1f0b4d29876"
  hamming_tolerance: 6                  # tier-1 checkpoint, so the override applies
  category: invariant_violation
action: confirm_bug
verdict: confirmed_bug
decided_by: rkim
role: triage_rotation
decided_at: 2026-11-09T15:05:32-05:00
run_id: run-2026-11-09-pr-0931
build_id: branch-1998.3.0-7be42d0
finding_ref: fnd-run-2026-11-09-pr-0931-000071
ticket: TOS-48377
reason: Ladder body overlaps the order-type row at widths under 700 px after the Flexible Grid padding change.
status: active
```

Inheritance rules: the lookup returns every active decision whose signature matches (same `checkpoint_id`, same `region_id` or grid cell, same `category`, pHash within `hamming_tolerance`), never only the first. When all returned verdicts agree the finding inherits the decision and appears under "suppressed by decision #id"; when they conflict nothing is inherited and the finding is listed as "conflicting decisions #a, #b" for a human (9.7). Matching runs before the Reviewer, so an inherited finding costs no model call. A `rescued` checkpoint never inherits `noise` or `intended` (3.11 rows 13 and 14). `confirmed_bug` suppresses the alert until `fixed_in_build` is recorded; from that build on the system asserts the signature is gone and sets `status: reopened` if it returns, which also reopens the ticket. `mark_intended` applies to the build pair it was made on and later pairs whose manifest carries the same ticket. `noise_signature` and `request_mask` expire and are re-evaluated against the A/A floor; a region now stable in A/A is dropped, not renewed. `approve_baseline` is per checkpoint or per signature cluster, never per run. Category is part of the key, so a `noise` verdict on a pixel diff never suppresses an `invariant_violation` on the same region. Distances between the tolerance and 12 are shown as "related decision #id (not applied)" and never inherit.

| Rule | Check | Severity |
|---|---|---|
| L-DEC-01 | `decided_by` is a human login: in the triage roster of 9.12 (`ops/oncall.yaml`), or the PR author for `confirm_bug` and for tier-3 `mark_intended`, or the Jira ticket's resolver for a `fixed_in_build` row with `source: jira_sync` (9.9); writes from the Reviewer, Orchestrator or any service identity are refused | write refused |
| L-DEC-02 | `ticket` present at write time for `mark_intended`, `fixed_in_build` and a `reject_baseline` whose verdict is `confirmed_bug`; for `confirm_bug` the ticket may land later through the outbox (9.1), and a row still without one when the outbox window closes is listed "without ticket" in the daily digest; `verdict` equals the mapping table's value for `action` (for `reject_baseline`, `confirmed_bug` or `noise`) | write refused |
| L-DEC-03 | `category` is from `taxonomy.yaml`; `signature.phash` is 16 hex characters; `hamming_tolerance`, when set, is at most 8, and at most 6 when the checkpoint's tier is 1; `bbox_q4` is present when the region uses the exact fallback | write refused |
| L-DEC-04 | `noise_signature`, `request_mask` or a `floor_exception` on a `money_cell` region; or a `request_mask` whose prefilled mask would raise the panel's masked plus non-identity redacted share over its budget (4.8), computed on the finding's capture: the report UI refuses to submit it and says why (the current share, the budget and the mask's share), no row and no mask PR are written, and the finding stays open in Unexpected until it is decided by `confirm_bug` or `mark_intended`; raising a budget is a system-owner pull request to `sentinel-config` | write refused |
| L-DEC-05 | `approve_baseline` from a PR run (`run_type = pr`; promotion happens at merge-run triage, 9.8), from a run with `synthetic = true` outcomes or with `status != complete`, or by the author of the change for a tier-1 checkpoint | write refused |
| L-DEC-06 | two-person rule (9.6): `mark_intended` on a tier-1 or tier-2 finding whose run is a PR run authored by `decided_by` requires `confirmed_by`, a different human from the triage rotation or the tier's flow owner; until then the row is not written and the finding stays open with "proposed intended by <login>" | write refused |
| L-DEC-07 | `mark_intended` on an `invariant_violation` or `assertion_failed` finding carries `expires_at` at most 30 days out; the outcome row is never updated by it | write refused |
| L-DEC-08 | `fixed_in_build` names a build newer than `build_id`; after the first run on or after that build with the signature absent the finding moves to `closed`; a return sets `status: reopened` and the finding `open` | write refused; post-run query |

### 4.15 Cross-file rules and the lint pipeline

The per-file rules above catch local errors. The cross-file rules below run after them, over the whole repository, and are the reason the lint runs at run start as well as in CI: a merged flow and a renamed surface can each be valid alone and invalid together.

| Rule | Check | Severity |
|---|---|---|
| L-XREF-01 | every surface in the registry is reached by at least one census workspace or one flow checkpoint; otherwise the coverage matrix will show it as `not_run` forever | warn |
| L-XREF-02 | every tier-1 surface has at least one flow with an `assert` step or `expect` entries and a regions file with at least one `money_cell` region, of type `diff` or `freeze` (L-RGN-04) | error |
| L-XREF-03 | every `masked_live` checkpoint has a `rule` or `mask` region; every `frozen` checkpoint has no `must_change_within_s` rule | error |
| L-XREF-04 | every `checkpoint_id` referenced by a regions file, the mutant catalog or a calibration file exists in a flow or census definition, or in `flows/RENAMES.yaml` as retired; `"*"` on an `aa_control` mutant entry stands for every bound checkpoint and is accepted (L-MUT-01) | error |
| L-XREF-05 | every critical checkpoint has its behavioral mutants in `canaries/mutants.yaml` (L-MUT-03), and every tier-1 checkpoint a calibration entry for the canonical calibration key (4.12); missing calibration is `warn` until the first Calibration run, then `error` | warn, then error |
| L-XREF-06 | the dependency map and the registry agree (L-DEP-05) and every manifest in `manifests/` references existing surfaces and primitives after a rename | error |
| L-XREF-07 | no file contains a string matching the credential patterns (`password`, `pwd`, `secret` followed by a value; 20 or more mixed-class characters after `:`); vault references are the only allowed secret form | error |
| L-XREF-08 | `taxonomy.yaml` equals the taxonomy table in 1.10 and the appendix copy 12.2 for the three outcome lists, and equals the tables that own the other lists: reasons, gates, finding kinds, triage states, decision actions and verdicts, canary states and kinds, reviewer verdicts, quarantine reasons (10.5), money roles (4.8) and popup actions (4.10) | error |
| L-XREF-09 | the `sentinel` repository's `schemas/*.schema.json` regenerate identically from the notation blocks in this section (`sentinel schemas --check`) | error |
| L-XREF-10 | a pull request that changes a `checkpoint_id`, a landmark template or a region `rect` of an approved checkpoint carries a `RENAMES.yaml` entry or a `recalibrate: true` marker, so the baseline is retired or re-approved instead of orphaned | error |
| L-XREF-11 | every checkpoint id in a flow, regions file, mask, mutant, calibration file, `RENAMES.yaml` or `quarantine.yaml` matches the grammar of 4.1 (or is the `"*"` of an `aa_control` mutant), and its surface segment exists in the registry; every flow a `quarantine.yaml` record names exists, or is retired in `flows/RENAMES.yaml` | error |
| L-XREF-12 | `canaries/mutants.yaml` contains M01 to M33 equal to their 12.5 rows (12.5 is authoritative for them) and further ids from M34 in sequence; `popups/catalog.yaml` contains every starter `POP-nn` row of 12.8 unless that entry is explicitly retired (`retired: true` with `retired_reason`), and may add further `POP` ids in sequence; a starter row that differs from its appendix row is fixed in the same pull request | error |
| L-XREF-13 | every `deny: true` control, and every control or surface an entry of `policy/explorer_deny_list.yaml` names (6.8), exists in the registry; every deny-list entry has at least one detection (window title, label, control or surface) and its regexes compile; the nightly denied-action test of 6.8 names each entry | error |
| L-XREF-14 | `policy/explorer_budget.yaml` values equal 6.8's (400 actions, 90 minutes, 20 consecutive no-change actions) unless the PR carries the Explorer owner's approval | error |
| L-XREF-15 | `phase0/branches.yaml` names one branch, or `undecided` with a `deadline`, for each of the seven unknowns, and every file whose validity depends on a branch (JAB fields, frozen provider, agent fallback, OnDemand account entry, the dismiss allowlist of L-XREF-18) agrees with it; an `undecided` branch past its `deadline` warns | error |
| L-XREF-16 | the run manifest carries the reference and sha256 of the signed authorization of 6.12 (`authorization/<version>.md` and its record, L-XREF-20); preflight refuses to start a run without it (11.7) | error (run start): `aborted_harness (reason: config_invalid)` |
| L-XREF-17 | `gateway/default_order_hotkeys.yaml` exists (a missing file fails closed at `settings_attested`, 5.4.7); every `chord` parses as modifiers (Ctrl, Alt, Shift, Win) plus one key; no chord repeats; `verified: false` warns from Phase 1 | error |
| L-XREF-18 | `gateway/dismiss_allowlist.yaml` `labels` are exact strings, never patterns; none is send-like (none matches the never-tokenize regex of 5.4.3, `Send`, `Submit`, `Replace` or `Confirm`); `Edit` appears only when `phase0/branches.yaml` records safety item 10 with an observed `Edit` control; an empty list is valid and means no dialog click is tokened (6.13 item 10) | error |
| L-XREF-19 | `policy/egress_allowlist.yaml` names exact host names only, no wildcard or pattern, each with purposes from the `egress_log` purpose list of 6.10; it carries the security contact's signature; its model host equals the endpoint of `policy/computer_use_ruling.yaml`, and the Jira, Slack and PR hosts of `policy/reporting.yaml` are on it | error |
| L-XREF-20 | for the authorization version in force, `authorization/<version>.md` and `authorization/<version>.yaml` both exist; `document_sha256` equals the document's hash; the three signatures of 6.12 (platform owner, security contact, compliance contact) are present; `account_ids` equals the union of `gateway/allowlist.yaml`; `rig_hostnames` covers every rig; `model_endpoint` equals the ruling's endpoint; `rate_caps` equal 6.4's; `send_policy_branch` is `gated` exactly when `policy/send_policy.yaml` is enabled (6.7) | error |
| L-XREF-21 | `policy/reporting.yaml`: `check_mode_history` is in date order, its last entry is the mode in force, and every entry names who changed it; Jira keys match `^[A-Z][A-Z0-9]+$`; the four Slack channels are set and `safety` is `#sentinel-safety` (9.9); an `auto_file` switch turns on only in a pull request the system owner approves (9.9) | error |
| L-XREF-22 | `policy/budgets.yaml` values do not exceed 11.4's (reviewer items PR 60, merge 200, RC 500; a rescue of 25 actions and 90 s, 25 rescued steps per run; 25, 50 and 500 dollars) unless the pull request carries the system owner's approval, and its Explorer session and weekly dollar caps equal those of `policy/explorer_budget.yaml` | error |
| L-XREF-23 | `policy/release_gate.yaml`: train names are unique; `default_business_days` and every `business_days` are at least 1; every `rc_cut_date` is earlier than its `ga_date` minus N business days of the XNYS calendar (7.10), N being the train's `business_days` or the default | error |
| L-XREF-24 | `schedule/windows.yaml` defines the four named windows `all_quiet`, `equities_quiet`, `futures_break` and `none`; times are `HH:MM` and the operating day starts before it ends; every `requires_window` value of a flow header or checkpoint names one of them | error |
| L-XREF-25 | `ops/credential_rotation.yaml`: every login a rig of `gateway/allowlist.yaml` uses has an entry with an owner and a backup owner, plus a team-owned SMS device under DR-03 branch B (7.7); every rotation names a listed login and a window that ends after it starts; a rotation whose window has ended carries `validating_run_id`, the login-only canary run of 6.9 | error |
| L-XREF-26 | `CODEOWNERS` routes `flows/<tier>/` of every tier and `registry/` to that tier's flow owner in `ops/oncall.yaml`, and routes every file of 4.16 to the owner its 4.1 row names | error |
| L-XREF-27 | `soak.yaml` has exactly one budget for each `soak.<metric>` assertion of 10.11, and no budget is looser than the 10.11 default unless the pull request carries the system owner's approval | error |
| L-XREF-28 | `rig/thinkorswim.vmoptions.template` carries `-Xms` and `-Xmx`, `-XX:ErrorFile`, `-XX:+HeapDumpOnOutOfMemoryError` and `-XX:HeapDumpPath`, `-XX:+CreateCoredumpOnCrash` where 5.14 lists it, and the Java2D flags of 7.1 (`-Dsun.java2d.d3d=false` and `-Dsun.java2d.noddraw=true` for VM rigs; a physical rig's variant pins `-Dsun.java2d.d3d=true`); every flag is known to the bundled JDK major that Phase 0 recorded (7.14) | error |
| L-XREF-29 | the reviewer version tuple in force (3.15) has an eval report `eval/reports/<hex>.yaml` whose `reviewer_version_sha256` is the tuple's hash and whose gates of 3.14 all pass on the held-out split; a pull request to `sentinel` or `sentinel-config` that changes any element of the tuple without that report is refused | error |
| L-XREF-30 | every control a surface's `invariants` block names (in `overlap_allowlist` pairs, other than `"*"`, and in `focus_order`) is a control of that surface; `palettes/<theme>.yaml` exists, with `theme` equal to its file name, for every theme that `configs/allowed.yaml` or a workspace's `display_config` declares | error |

**Where the lint runs and what a failure means.**

| Place | Command | On failure |
|---|---|---|
| Pull request to `sentinel-config` | `sentinel lint --changed` | PR blocked on `error`; `warn` posted as a review comment |
| Pull request to the product repository | `sentinel lint --manifest .sentinel/manifest.yaml` as the `sentinel/manifest` check | check fails with the rule ids; informational in Phase 1, blocking from Phase 2 (4.3); the run treats an invalid manifest as missing |
| Pull request to `sentinel` (the harness) | `sentinel schemas --check` and the taxonomy test | PR blocked: the generated JSON Schema files, the DDL `CHECK` constraints and `taxonomy.yaml` must agree |
| Nightly | `sentinel lint --all` | nightly run `aborted_harness (reason: config_invalid)`; one line in the morning report; the dead-man alert is unaffected because the run still emits its completion event |
| Run start, before gate `provisioned` | `sentinel lint --all --config-commit <sha>` | run `aborted_harness (reason: config_invalid)`, rule ids and file paths posted to the on-call channel; rules marked "handled at run start" produce their named outcome instead and never abort |
| Recorder export | `sentinel lint --file <new flow>` | the Recorder refuses to emit a flow that fails; the author fixes it in the Recorder |
| Explorer candidate flow | `sentinel lint --file` with `class: explorer_candidate` | a failing candidate is kept as an `explorer_candidate` finding with `replayed: lint_failed` and the lint output, never replayed |

Lint output is one JSON line per violation (`rule`, `severity`, `file`, `path`, `message`); the same binary, pinned by the harness commit in the fingerprint, runs in CI and on the rig.

### 4.16 Safety and operations files

The files below hold the policies, rosters, calendars, measurements and Phase 0 reads that the safety model (section 6), the scheduler (7.10), the Diff Engine and the Reporter depend on. Each lives in `sentinel-config` at the path its 4.1 row gives, carries `schema_version`, is validated against its schema by `sentinel lint`, and changes only by a pull request that passes it; CODEOWNERS routes each to the owner its 4.1 row names (L-XREF-26). A file that section 6 says is signed carries the signature fields shown. The cross-file rules are in 4.15: L-XREF-13 and L-XREF-15 to L-XREF-30. The vmoptions template is plain text, checked by L-XREF-28, and has no schema.

**Gateway and safety files.** The dismiss allowlist is the exact set of labels the gateway may click while an order dialog is in front (6.2, 5.4.3). It stays empty until Phase 0 reads the Order Confirmation dialog, so no dialog click is tokened before then (6.13 item 10). The default order hotkeys are the pinned build's default order-action chords, read once in Phase 0; the gateway loads them at `settings_attested` and fire drill item 3 injects each one (5.4.7, 6.5).

```yaml
# schemas/dismiss-allowlist.schema.json  (gateway/dismiss_allowlist.yaml)
dismiss_allowlist:
  schema_version: int = 1
  labels: list[str]                     # exact labels: Delete, plus Edit only when Phase 0 observes it; empty until read
  read_on_build: ref:build.build_id?    # the build whose Order Confirmation dialog Phase 0 read
  read_by: str?
  read_at: ts?

# schemas/default-order-hotkeys.schema.json  (gateway/default_order_hotkeys.yaml)
default_order_hotkeys:
  schema_version: int = 1
  build: ref:build.build_id             # the pinned build whose default hotkey table was read
  verified: bool = false                # false while the chords are placeholders before the Phase 0 read
  read_by: str?
  read_at: ts?
  chords: list[{chord: str, action: str}]   # e.g. "ctrl+shift+b" and the order action the default table binds to it
```

The Explorer deny list implements the table of 6.8, one entry per denied thing, each detected by window title, by label, by a registered control (its geometry recomputed per build from its landmark) or by a whole surface. It applies to steps run under the agent and Explorer profiles (L-FLW-13), and the nightly denied-action test drives every entry (6.8).

```yaml
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
```

The egress allowlist is the signed list of hosts the harness HTTP client may reach (6.10); anything else is refused before connecting. The computer-use ruling is the security contact's current answer to DR-07 (6.11), read and validated at preflight G1: `status: ruled` needs `endpoint`, `retention`, `approved_model_tiers` and `signed_by`; branch A (`computer_use_covered: true`) also needs `model_ids` and `toolset_schema_version`; a ruling that covers the Reviewer without computer use is branch B with the Reviewer on; `status: open` is branch B with zero model calls. A ruling that fails these checks fails G1, `aborted_harness (reason: config_invalid)` (12.7).

```yaml
# schemas/egress-allowlist.schema.json  (policy/egress_allowlist.yaml)
egress_allowlist:
  schema_version: int = 1
  hosts: list[{host: str, port: int = 443, purposes: list[str]}]   # exact host names; purposes from the egress_log purpose list of 6.10
  signed_by: str                        # the security contact (6.10, 6.12)
  signed_at: ts
  signature: str

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
```

The authorization of 6.12 is a signed document, `authorization/<version>.md`, with a machine-readable record beside it that the lint and preflight check. Any change is a new version; the gated send branch is such a change (6.7).

```yaml
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
```

**Budgets, release gate and reporting.** `policy/budgets.yaml` holds the hard caps of 11.4; the Explorer's action and time budgets stay in `policy/explorer_budget.yaml`. `policy/release_gate.yaml` holds N and the train dates; with no file, N = 3 and an RC cut at G-7 are assumed and printed in the RC report header (11.2). `policy/reporting.yaml` holds what 9.9 says is set in configuration.

```yaml
# schemas/budgets.schema.json  (policy/budgets.yaml)
budgets:
  schema_version: int = 1
  reviewer_items: {pr: int = 60, merge: int = 200, rc: int = 500}   # 3.15; items beyond are needs_human (reason: reviewer_cap)
  rescue: {max_actions: int = 25, max_seconds: int = 90, max_rescued_steps_per_run: int = 25}   # 5.5; past a limit the rescue ends and the step fails
  usd: {per_explorer_session: num = 25, per_run: num = 50, per_week: num = 500}   # 11.4; only per_run ends a run (aborted_budget, reason: dollar_cap)
  approved_by: str?                     # the system owner, for a value above 11.4's

# schemas/release-gate.schema.json  (policy/release_gate.yaml)
release_gate:
  schema_version: int = 1
  default_business_days: int = 3        # N, PA-14 of 11.1
  trains: list[{train: str, ga_date: date, rc_cut_date: date, business_days: int?}]   # business_days overrides N for the train
  owner: str                            # the release manager (9.12)

# schemas/reporting.schema.json  (policy/reporting.yaml)
reporting:
  schema_version: int = 1
  check_mode_history: list[{mode: enum[informational|blocking], since: date, by: str}]   # the commit status mode of 9.2; every change dated and announced in the release channel
  jira: {product_project: str, harness_project: str}
  slack: {on_call: str, triage: str, release: str, safety: str}   # the four channels of 9.9
  paging: {service: str, escalation_policy: str?}
  email: list[str]?                     # digest recipients
  auto_file: {tier1: bool = false, tier2: bool = false, tier3: bool = false}   # auto-filing of collateral per tier, off by default (9.9)
```

**Schedule and people.** `schedule/windows.yaml` is the configuration block of 7.10. PR, merge and RC runs start only inside the operating day, and one triggered outside it waits until 06:00; Nightly and Calibration runs are exempt (7.10, 11.2). `ops/oncall.yaml` names every role holder and deputy, the flow owner per tier, the weekly triage and harness on-call rotations and the triage roster the decision rules read. `ops/credential_rotation.yaml` is the rotation calendar of 7.7: a run scheduled inside a rotation window is held, not started, with a run-health line.

```yaml
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

# schemas/oncall.schema.json  (ops/oncall.yaml)
oncall:
  schema_version: int = 1
  roles: list[{role: enum[system_owner|accountable_owner|risk_owner|security_contact|explorer_owner|accounts_owner|release_manager|registry_owner], person: str, deputy: str}]   # 6.12, 11.6
  flow_owners: list[{tier: enum[1|2|3], person: str, deputy: str}]
  rotations: list[{rotation: enum[triage|harness_oncall], person: str, from: date, to: date}]   # one person per week each (9.12)
  triage_roster: list[str]              # the logins that may decide findings (L-MSK-01, L-DEC-01, L-DEC-06)

# schemas/credential-rotation.schema.json  (ops/credential_rotation.yaml)
credential_rotation:
  schema_version: int = 1
  logins: list[{login_ref: str, owner: str, backup_owner: str, sms_device: str?}]   # login_ref as in gateway/allowlist.yaml; sms_device: the team-owned device under DR-03 branch B
  rotations: list[{login_ref: str, kind: enum[password|vault_token], window: {start: ts, end: ts}, owner: str, validating_run_id: id?}]   # validating_run_id: the login-only canary run of 6.9
```

**Rig files.** The process allowlist is the running-process rule of 7.1: a process outside it at gate `provisioned` is `env_error (gate: provisioned, reason: rig_spec_mismatch)` naming the process.

```yaml
# schemas/process-allowlist.schema.json  (rig/process_allowlist.yaml)
process_allowlist:
  schema_version: int = 1
  image_id: str                         # the golden image the baseline was read from (7.2)
  harness: list[str]                    # image names of the Orchestrator, the Gateway hook host, the Watchdog and the elevated helper service (5.1)
  app_tree: list[str]                   # the thinkorswim process tree
  windows_baseline: list[str]           # the golden image's Windows processes
```

**Measurements and outputs.** These files are written by the harness or by a Phase 0 read and reviewed by a human in the pull request: the corpus sizes of Ladder F, the INV-15 palette per theme, the soak budgets, the calibration export, the quarantine records and the reviewer eval reports. `corpus/measured_sizes.yaml` comes from probe P8 on the first validated run; a surface without sizes there has its Ladder F corpus steps `not_run (reason: scope)`, with a run-health line (2.5.2).

```yaml
# schemas/measured-sizes.schema.json  (corpus/measured_sizes.yaml)
measured_sizes:
  schema_version: int = 1
  measured_on_run: id                   # the first validated run whose probe P8 measured them
  sizes: list[{surface: ref:surface.id, workspace: ref:workspace.id, host: enum[flexgrid|detached|dialog], w: int, h: int}]

# schemas/palette.schema.json  (palettes/<theme>.yaml)
palette:
  schema_version: int = 1
  theme: enum[dark|light]               # equals <theme> in the file name
  colors: {positive: str, negative: str, neutral: str}   # "#rrggbb", read from the pinned build in Phase 0
  tolerance: num = 60                   # largest RGB distance at which a cell color counts as an entry; a color farther from every entry is not judged (INV-15)

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
```

The calibration export is written by the Calibration Service from `calibration_floors` (8.3) and never edited by hand; its record per region is what 4.8 Threshold source reads.

```yaml
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
```

A quarantine record names exactly one checkpoint or one flow; its reason is one of the quarantine reasons of 10.5 (`quarantine_reasons` in `taxonomy.yaml`), and `detail` carries what tripped it (the canary, the drill item, the rate).

```yaml
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
```

An eval report records one run of the eval set's held-out split for one reviewer version tuple of 3.15, with every gate of 3.14; L-XREF-29 requires a passing one for the tuple in force.

```yaml
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
```

**Phase 0 reads.** `phase0/branches.yaml` records the branch chosen for each of the seven decision records of 11.8 and the Phase 0 reads other files depend on. An undecided branch carries its deadline; the files that depend on a branch must agree with it (L-XREF-15).

```yaml
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
```
