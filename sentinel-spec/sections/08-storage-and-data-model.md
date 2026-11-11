## 8. Storage and data model

Sentinel keeps three kinds of state in three kinds of store: facts and decisions in SQLite, bytes in a content-addressed artifact store, configuration in git. Every store has exactly one writer. The rules here exist so that any run can say which configuration it ran, which images it compared, who decided what, what left the machine, and so that nothing the system failed to store can be mistaken for something it checked.

### 8.1 Stores and systems of record

The process layout of 5.1 Component diagram and process layout puts the Orchestrator on the rig and the Reporter service on the control host, so there are two SQLite databases with the same schema and different writers.

| Store | Where | Holds | System of record for | Writer | Readers |
|---|---|---|---|---|---|
| Run Store | `C:\sentinel\run\<shard_id>\run.db` on the rig, WAL mode | the rows of one shard: run, shard, outcomes, step events, Oracle extractions, findings, reviews requested, egress rows, fingerprint; read-only snapshots of decisions, baselines, regions, masks, calibration floors and quarantine shipped in the config tarball | nothing after import; the shard's truth until then | the Orchestrator of that shard | the Watchdog (read-only), the Rig Manager when shipping |
| Central Store | `D:\sentinel\central.db` on the control host, WAL mode | every table in 8.3 for every run | every outcome, human decision, baseline approval, canary result, flake statistic, incident and egress row | the Reporter service (5.13 Reporter and Integrations) | report UI (read-only), Reviewer queue drainer, Canary Service, Calibration Service, Scheduler, metrics jobs |
| Artifact store | `C:\sentinel\run\<shard_id>\objects\` on the rig, then `D:\sentinel\artifacts\` on the control host | captures, diff products, sidecars, exports, agent frames, video, crash files, log slices, attestation and gate screenshots, reviewer requests, manifests, ledgers | the bytes behind every claim; rebuild source for the databases | Capture and the Orchestrator on the rig; the Reporter service's import on the control host (rigs upload, never write the central tree) | report UI, Canary and Calibration services, backup job |
| git, `sentinel-config` | the config host | Surface Registry, Dependency Map, flows and rename map, Region Registry, mask records, Workspace Corpus, popup catalog, mutant catalog, fixtures, profiles, ladders, calibration exports, quarantine records, eval labels, released baseline manifests, `taxonomy.yaml`, `policy/send_policy.yaml`, `phase0/branches.yaml` | what a run was asked to do | humans by pull request; the Orchestrator and the Calibration Service by pull request only; one sign-off bot commit per RC | Orchestrator at run creation; `sentinel lint` |
| git, `sentinel` | the config host | harness code, schemas, lint | how the run executed | engineers | Rig Manager at snapshot build |
| Backup location | another host on approved internal storage | database snapshots, JSONL exports, baselines mirror, pinned artifacts | nothing; a copy | backup job | restore drill |

Three rules follow. The databases are indexes over facts that also exist as files: every shard directory carries a `results.jsonl` ledger that the Orchestrator appends before each commit, and the Central Store exports decisions, baselines, masks, flake statistics, incidents and quarantine records as JSONL every night, so a lost database is rebuilt by replay. The artifact store holds only redacted bytes (6.10 Redaction pipeline and egress log, 3.6 Redaction before storage); unredacted frames exist in Capture process memory on the rig and nowhere else. And a run is `complete` only when every one of its shards has been imported and every artifact its outcome rows name exists in the central tree; the trigger in 8.3 refuses anything else.

### 8.2 Single-writer rule

Exactly one process writes each database.

- Run Store: the Orchestrator of the shard, through one writer thread. The Gateway hook host writes only its shared-memory drop ring (5.4.2); the Orchestrator's drop-log drainer turns it into `gateway_drops.jsonl` and `step_events` rows. The Watchdog appends to `watchdog_events.jsonl` and never opens the database (10.2 Watchdog). On resume the Orchestrator ingests both journals into `step_events`. If the Orchestrator's restart budget is spent, the Rig Manager ships the Run Store, the ledger, the journals and the artifact folder as they are, and the import writes `harness_error (reason: orchestrator_restart_budget)`, the run's abort reason (1.10), for every scoped checkpoint of the shard that has no outcome row, so no checkpoint is left silent. A lock file holding the writer's PID sits beside the database; a second writer refuses to start while the PID is alive.
- Central Store: the Reporter service, through one writer thread and a local HTTP API on the control host. Everything that changes the Central Store is a request to that API: the shard import, the Reviewer queue drainer's verdicts, the Canary and Calibration services' rows, the report UI's one-click actions, the Jira poller's `fixed_in_build` decisions, the metrics jobs' summaries. The run outcome is written by the Reporter service when the last shard imports or the dead-man deadline passes, as the worst of the shards' outcomes in the order `aborted_safety`, `aborted_harness`, `aborted_budget`, `complete_with_env_errors`, `complete`.
- Nobody opens either database file over a share. SQLite's file locking on SMB and NFS is unreliable, so two hosts can both believe they hold the write lock and corrupt the file, and WAL mode coordinates readers and writers through a shared-memory file that only processes on one host can see, so WAL does not work across hosts at all. Startup preflight (H7 in 12.7 Preflight checklist) requires both the database path and the artifact root on a fixed local volume (Win32 `GetDriveType` returns `DRIVE_FIXED`; not UNC, not a mapped drive); otherwise the process refuses to start, writes the reason to its heartbeat file, and the dead-man alert fires at the next deadline.

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;   -- survives process crash; power loss is covered by the ledger and the backup
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA user_version;           -- schema version, migrated forward-only at service start
```

- The writer thread drains a queue of batches, one checkpoint per batch (outcome, step events, findings, review request, artifact rows) in one `BEGIN IMMEDIATE` transaction, so a reader never sees an outcome without its evidence. Artifacts are verified on disk before the batch that names them is queued; an outcome row therefore never points at bytes that were not stored. Each batch is appended to `results.jsonl` before it commits; replay is idempotent because every row carries its natural key, and a replayed row that differs from a stored row with the same key is an import conflict (8.9), never a silent overwrite.
- Shard import: at `reported` the Rig Manager ships the shard directory to `incoming\<shard_id>\` on the control host. The Reporter service verifies every object's hash, verifies that the Run Store's rows equal the ledger, then imports the rows in one transaction per shard and renames the objects into `runs\<run_id>\objects\`. A shard is `imported` only when both halves succeeded; a partially imported shard is never visible to a reader.
- Rig to control host: `POST /v1/shards/{shard_id}/ship` with the manifest, then `PUT /v1/artifacts/{sha256}` per object with headers for kind, media type, redaction version, run id and checkpoint id. Both are idempotent; the service recomputes every hash before acknowledging, so a rig retrying after a dropped connection is safe.
- Report UI writes go to the Reporter service's local API; the UI never holds a write connection. If the service is down the UI shows a read-only banner and refuses the action rather than queueing it in a browser.
- Readers open with the `mode=ro` URI flag and `PRAGMA query_only = 1`. WAL readers never block the writer, but a long read transaction blocks checkpointing, so the UI keeps transactions short and the writer runs `PRAGMA wal_checkpoint(PASSIVE)` after each shard import.
- The writer and every read connection register a scalar function `hamming(a, b)` (population count of the XOR of two 8-byte blobs) so the decision lookup in 8.3 runs in SQL.

### 8.3 Schema

The DDL below is the complete schema for phases 1 to 3 and is the same in both stores. Every CHECK that names outcomes uses the closed taxonomy of 1.10 Outcome taxonomy (authoritative) verbatim; the lists are generated from `taxonomy.yaml` (4.1 Conventions, repository layout and the lint, rule L-OUT-01), so a row with any other value cannot be written. Timestamps are ISO 8601 UTC text. Identifiers are text slugs where humans read them and integers where only the database does.

**Reasons.** The reason vocabularies are long and grow by pull request, so they live in a lookup table seeded from the `reasons` list of `taxonomy.yaml` at service start rather than in CHECK constraints. The pair (outcome, reason) is a foreign key; a reason the file does not list cannot be written. A reason marked `parametric` requires `reason_detail` and renders as `reason:detail`, which is the form the rest of this specification writes as `not_run (reason: step_failed:<step>)`, `landmark_missing (reason: locator:<step>)` or `assertion_failed (assertion: app_alive)`; the one exception is `preflight`, whose detail is a 12.7 item id and renders with a dot, `preflight.H7`, as 12.7 writes it. An `env_error` carries its gate in the `gate` column of `outcomes`, so `env_error (gate: fixture_verified, reason: fixture_drift)` is gate `fixture_verified` plus reason `fixture_drift`; the reason `gate` alone means the gate's own anchor check failed with no finer cause. Reasons are never deleted; a retired reason keeps its history and refuses new rows. The seed below is the reason table of 12.2 Outcome taxonomy in SQL form; both are generated from the same `taxonomy.yaml`, and a difference between them is a lint error (L-OUT-01).

```sql
CREATE TABLE outcome_reasons (
  outcome     TEXT NOT NULL,              -- a checkpoint outcome or a run outcome
  reason      TEXT NOT NULL,
  parametric  INTEGER NOT NULL DEFAULT 0 CHECK (parametric IN (0,1)),
  detail_means TEXT,                      -- what reason_detail holds when parametric
  owner_section TEXT NOT NULL,            -- the subsection whose rule produces it
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  PRIMARY KEY (outcome, reason)
) WITHOUT ROWID;

-- Seed, generated from taxonomy.yaml; the file is the source, this is its image. It equals the reason table of 4.13.
INSERT INTO outcome_reasons (outcome, reason, parametric, detail_means, owner_section) VALUES
 ('noise','below_floor',0,NULL,'3.11'),
 ('noise','decision',1,'decision id of the approved noise signature or active mask decision (4.13 writes decision:<id>)','4.14'),
 ('intended','decision',1,'decision id of the inherited mark_intended decision','4.14'),
 ('collateral','decision',1,'decision id of the matched confirm_bug decision; the computed collateral outcome is kept (4.14)','4.14'),
 ('collateral','manifest_missing',0,NULL,'4.3'),
 ('needs_human','reviewer_uncertain',0,NULL,'3.11'), ('needs_human','reviewer_no_match',0,NULL,'3.11'),
 ('needs_human','no_manifest',0,NULL,'4.3'),
 ('needs_human','uncalibrated',0,NULL,'3.9'), ('needs_human','reviewer_unavailable',0,NULL,'10.7'),
 ('needs_human','reviewer_cap',0,NULL,'3.15'),
 ('needs_human','rescued_capture',0,NULL,'5.5'), ('needs_human','oracle_unreadable',0,NULL,'5.10'),
 ('needs_human','bounds_unreadable',0,NULL,'5.9'), ('needs_human','global_shift',0,NULL,'3.7'),
 ('invariant_violation','INV-01',0,NULL,'2.7'), ('invariant_violation','INV-02',0,NULL,'2.7'),
 ('invariant_violation','INV-03',0,NULL,'2.7'), ('invariant_violation','INV-04',0,NULL,'2.7'),
 ('invariant_violation','INV-05',0,NULL,'2.7'), ('invariant_violation','INV-06',0,NULL,'2.7'),
 ('invariant_violation','INV-07',0,NULL,'2.7'), ('invariant_violation','INV-08',0,NULL,'2.7'),
 ('invariant_violation','INV-09',0,NULL,'2.7'), ('invariant_violation','INV-10',0,NULL,'2.7'),
 ('invariant_violation','INV-11',0,NULL,'2.7'), ('invariant_violation','INV-12',0,NULL,'2.7'),
 ('invariant_violation','INV-13',0,NULL,'2.7'), ('invariant_violation','INV-14',0,NULL,'2.7'),
 ('invariant_violation','INV-15',0,NULL,'2.7'),
 ('assertion_failed','assertion',1,'assertion id or Oracle rule R01 to R10; the built-in assertions are app_alive (detail also carries jvm_crash, hang, oom or process_exit), size_reached and soak.<metric> (10.1, 10.11)','2.6.2'),
 ('not_settled','frames_unstable',0,NULL,'3.5'), ('not_settled','anchor_missing',0,NULL,'3.5'),
 ('not_settled','anti_anchor_present',0,NULL,'3.5'), ('not_settled','timeout',0,NULL,'3.5'),
 ('not_settled','unexpected_window',1,'title of the thinkorswim window still present at max wait','3.4'),
 ('not_settled','unstable',0,NULL,'10.5'), ('not_settled','aa_drift',0,NULL,'10.6'),
 ('not_comparable','fingerprint_mismatch',0,'the differing match-class field names, never empty; data_epoch, bounds source and render_probe_hash are fields (7.4)','3.3'),
 ('not_comparable','window_mismatch',0,NULL,'3.4'), ('not_comparable','surface_mismatch',0,NULL,'3.4'),
 ('landmark_missing','landmark',1,'landmark id and the side it was missing on','3.8'),
 ('landmark_missing','locator',1,'step id whose locator failed on every rung','5.5'),
 ('landmark_missing','redaction_landmark',1,'redaction landmark id; nothing stored, nothing released','6.10'),
 ('landmark_missing','probe',1,'registry validation probe P1 to P8 whose anchor was not found','2.2.3'),
 -- env_error: the gate is in outcomes.gate; reason gate alone means the gate's own check failed with no finer cause
 ('env_error','gate',0,NULL,'5.2'),
 ('env_error','version_mismatch',0,NULL,'7.3'), ('env_error','version_changed_mid_run',0,NULL,'7.3'),
 ('env_error','rig_spec_mismatch',0,NULL,'7.1'), ('env_error','session_kicked',0,NULL,'7.7'),
 ('env_error','fixture_drift',0,NULL,'7.6'), ('env_error','occluded',0,NULL,'3.4'),
 ('env_error','identity_unreadable',0,NULL,'6.4'), ('env_error','attestation_unreadable',0,NULL,'6.3'),
 ('env_error','vault_unreachable',0,NULL,'6.9'),
 ('env_error','delay_state',0,NULL,'7.9'), ('env_error','data_epoch_mismatch',0,NULL,'7.9'),
 ('env_error','prices_stale',0,NULL,'4.9'),
 ('env_error','fixture_maintenance_needed',0,NULL,'7.6'), ('env_error','backend_degraded',0,NULL,'7.11'),
 ('env_error','agreement_pending',0,NULL,'7.3'), ('env_error','authentication_challenge',0,NULL,'7.7'),
 ('env_error','authentication_failed',0,NULL,'7.7'), ('env_error','live_mode_selected',0,NULL,'7.7'),
 ('env_error','timeout',0,NULL,'7.7'), ('env_error','relogin_budget',0,NULL,'7.7'),
 ('env_error','snapshot_revert_failed',0,NULL,'7.2'), ('env_error','install_failed',0,NULL,'7.3'),
 ('env_error','fingerprint_unreadable',1,'fingerprint field that could not be read','7.4'),
 ('env_error','ondemand_timeout',0,NULL,'7.9'), ('env_error','ondemand_state',0,NULL,'7.9'),
 ('env_error','precondition',1,'flow precondition check that still failed after one reset_ui and one reset_profile','2.6.1'),
 ('env_error','maintenance',0,NULL,'7.8'),
 ('harness_error','bounds_source_unavailable',0,NULL,'5.9'), ('harness_error','ocr_calibration',0,NULL,'5.10'),
 ('harness_error','mask_budget_exceeded',0,NULL,'3.8'), ('harness_error','invariant_on_aa',0,NULL,'2.7'),
 ('harness_error','hook_lost',0,NULL,'5.4'), ('harness_error','orchestrator_died',0,NULL,'10.2'),
 ('harness_error','canary_generation_failed',0,NULL,'10.3'), ('harness_error','canary_path_divergence',0,NULL,'10.3'),
 ('harness_error','detector_blind',0,NULL,'10.3'), ('harness_error','detector_degraded',0,NULL,'10.3'),
 ('harness_error','safety_canary_blind',1,'blind detector: attestation, identity, proof, deny_list, secret_scan or redaction','6.5'),
 ('harness_error','drill_failed',1,'fire drill item 1 to 5 that was not blocked or could not run','6.5'),
 ('harness_error','validation_self_test',0,NULL,'10.3'), ('harness_error','chaos_unobserved',0,NULL,'10.2'),
 ('harness_error','aa_drift_global',0,NULL,'10.6'),
 ('harness_error','registry_entry_not_found',0,NULL,'2.2.3'), ('harness_error','export_header_mismatch',0,NULL,'2.8.1'),
 ('harness_error','chaos_rule_stuck',0,NULL,'7.11'),
 ('harness_error','exception',1,'exception class and Watchdog event id','10.1'),
 ('harness_error','store_write_failed',0,NULL,'8.9'), ('harness_error','artifact_persist_failed',0,NULL,'8.9'),
 ('harness_error','redaction_version_missing',0,NULL,'8.9'),
 ('not_run','entitlement',0,NULL,'2.12'), ('not_run','budget',0,NULL,'11.2'), ('not_run','scope',0,NULL,'2.11'),
 ('not_run','quarantine',0,NULL,'10.5'), ('not_run','step_failed',1,'step id that ended failed or blocked_by_gateway','5.2'),
 ('not_run','base_step_failed',0,NULL,'3.1'), ('not_run','app_crash',0,NULL,'10.1'),
 ('not_run','model_unavailable',0,NULL,'10.7'),
 ('unregistered_surface','probe',1,'registry validation probe P1 to P8 that enumerated the entry; the label goes in detail','2.2.3'),
 ('needs_baseline','new_surface',0,NULL,'1.10'), ('needs_baseline','base_not_settled',0,NULL,'1.10'),
 ('needs_baseline','base_env_error',0,NULL,'1.10'), ('needs_baseline','base_harness_error',0,NULL,'1.10'),
 ('needs_baseline','lineage_missing',0,NULL,'9.8'), ('needs_baseline','reuse_missing',0,NULL,'7.4'),
 ('needs_baseline','explorer_replay',0,NULL,'2.9'),
 -- run abort reasons share the table (1.10); the cause of a complete_with_env_errors run does too
 ('aborted_safety','attestation_mismatch',0,NULL,'6.3'), ('aborted_safety','paper_mode_mismatch',0,NULL,'6.4'),
 ('aborted_safety','kill_switch',0,NULL,'6.6'), ('aborted_safety','attempted_send',0,NULL,'6.2'),
 ('aborted_safety','unexpected_working_order',0,NULL,'7.6'), ('aborted_safety','undeclared_order_row',0,NULL,'6.6'),
 ('aborted_safety','drill_order_placed',0,NULL,'6.5'), ('aborted_safety','unknown_dialog_in_order_stage',0,NULL,'5.4.3'),
 ('aborted_safety','live_identity',0,NULL,'6.4'),
 ('aborted_budget','wall_clock',0,NULL,'11.2'), ('aborted_budget','app_restart_budget',0,NULL,'10.2'),
 ('aborted_budget','pool_saturated',0,NULL,'11.2'), ('aborted_budget','rate_cap',0,NULL,'6.4'),
 ('aborted_budget','dollar_cap',0,NULL,'11.4'), ('aborted_budget','superseded',0,NULL,'11.2'),
 ('aborted_harness','config_invalid',0,NULL,'4.15'), ('aborted_harness','bounds_source_unavailable',0,NULL,'5.9'),
 ('aborted_harness','preflight',1,'H item of 12.7; rendered preflight.<item> as 12.7 writes it','12.7'),
 ('aborted_harness','dead_man',0,NULL,'10.2'), ('aborted_harness','orchestrator_restart_budget',0,NULL,'10.2'),
 ('aborted_harness','start_failed',0,NULL,'11.2'),
 ('aborted_harness','hook_lost',0,NULL,'5.4.2'), ('aborted_harness','watchdog_dead',0,NULL,'10.2'),
 ('aborted_harness','heartbeat_unwritable',0,NULL,'10.2'), ('aborted_harness','disk_full',0,NULL,'10.2'),
 ('aborted_harness','rig_lost',0,NULL,'10.7'), ('aborted_harness','canary_generation_failed',0,NULL,'10.3'),
 ('aborted_harness','taxonomy_check_failed',0,NULL,'1.4'), ('aborted_harness','dpi_awareness',0,NULL,'7.1'),
 ('aborted_harness','store_write_failed',0,NULL,'8.9'), ('aborted_harness','artifact_persist_failed',0,NULL,'8.9'),
 ('aborted_harness','secret_leaked',0,NULL,'6.9'), ('aborted_harness','secret_scan_unavailable',0,NULL,'6.9'),
 ('aborted_harness','egress_unlogged',0,NULL,'6.10'), ('aborted_harness','base_build_missing',0,NULL,'3.1'),
 ('aborted_harness','import_conflict',0,NULL,'8.9'),
 ('complete_with_env_errors','unproven_proof',0,NULL,'6.6'), ('complete_with_env_errors','teardown_fault',0,NULL,'5.2'),
 ('complete_with_env_errors','relogin_budget',0,NULL,'7.7');

-- Unreached checkpoints carry the run's abort reason (1.10): every aborted_safety reason is also an env_error
-- reason and every aborted_harness reason a harness_error reason, with the same parameter rule.
INSERT OR IGNORE INTO outcome_reasons (outcome, reason, parametric, detail_means, owner_section)
  SELECT 'env_error', reason, parametric, detail_means, '1.10' FROM outcome_reasons WHERE outcome = 'aborted_safety';
INSERT OR IGNORE INTO outcome_reasons (outcome, reason, parametric, detail_means, owner_section)
  SELECT 'harness_error', reason, parametric, detail_means, '1.10' FROM outcome_reasons WHERE outcome = 'aborted_harness';
```

`identical` carries no reason. `intended` and `collateral` carry none unless a decision or the no-manifest rule produced them: a row that inherits a decision has reason `decision` with the decision id in `reason_detail` and in `decision_id` (the form 4.13 and 4.14 write as `decision:<id>`; for `collateral` this is a computed `collateral` kept under a matched `confirm_bug` decision, 4.14), and a UI-touching PR without a manifest gives `collateral (reason: manifest_missing)`. A `noise` row is `below_floor` or `decision` (an approved noise signature or an active mask decision). The review that moved a pending row is linked through `review_id`. The cause of a `complete_with_env_errors` run (`unproven_proof`, `teardown_fault`, `relogin_budget`) sits in `runs.abort_reason` beside the abort reasons, so the report prints why a run was not `complete`. Unreached checkpoints of an `aborted_safety` run are `env_error` with the aborting gate and the run's abort reason as the reason (`env_error (gate: paper_mode_asserted, reason: paper_mode_mismatch)`), and of an `aborted_harness` run `harness_error` with the run's abort reason (`harness_error (reason: dead_man)`), per 1.10; the two `INSERT ... SELECT` statements above make every abort reason a valid reason for those two outcomes.

**Identity: logins, rigs, builds, build pairs, runs, shards, fingerprints.**

```sql
CREATE TABLE logins (
  login_alias      TEXT PRIMARY KEY,       -- vault alias, never a credential
  kind             TEXT NOT NULL CHECK (kind IN ('scripted','explorer')),
  allowlist_entry  INTEGER NOT NULL,       -- position in the hard-coded paper-mode allowlist; the id itself is never stored
  owner            TEXT NOT NULL,
  rotation_due     TEXT,
  blocked_at TEXT,
  blocked_reason   TEXT CHECK (blocked_reason IN ('unproven_proof','identity_unreadable_twice','cancel_all_failed',
                     'recovery_login_failed','explorer_teardown_incomplete','drill_order_placed','incident','rotation',
                     'session_unavailable','live_identity','authentication_failed')),   -- 6.4 to 6.8, 7.7; a human clears it
  unblocked_by TEXT, unblocked_at TEXT,
  CHECK ((blocked_at IS NULL) OR (blocked_reason IS NOT NULL))
);

CREATE TABLE rigs (
  rig_id       TEXT PRIMARY KEY,
  tier         TEXT NOT NULL CHECK (tier IN ('A','B')),   -- B = the physical D3D rig
  role         TEXT NOT NULL CHECK (role IN ('pool','nightly','explorer')),   -- fleet of 11.2
  login_alias  TEXT NOT NULL REFERENCES logins(login_alias),
  snapshot_id  TEXT NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  disabled_reason TEXT, disabled_at TEXT,   -- a safety action took the rig out of service (6.12)
  enabled_by TEXT, enabled_at TEXT,         -- re-enabled by a human (6.12)
  reassigned_by TEXT, reassigned_at TEXT,   -- the operator who moved this rig's login to a spare after a re-snapshot (11.2)
  CHECK ((disabled_at IS NULL) = (disabled_reason IS NULL)),
  CHECK ((enabled_by IS NULL) = (enabled_at IS NULL)),
  CHECK ((reassigned_by IS NULL) = (reassigned_at IS NULL)),
  CHECK (enabled_by IS NULL OR enabled_by NOT IN ('reviewer','system','orchestrator','reporter'))
);

CREATE UNIQUE INDEX ux_rig_login_active ON rigs(login_alias) WHERE active = 1;   -- a login is never on two rigs at once (11.2, 11.3)

-- 11.3: the Explorer login never does scripted work; pool and nightly rigs run scripted logins only
CREATE TRIGGER trg_rig_login_kind_ins BEFORE INSERT ON rigs
WHEN (NEW.role = 'explorer') <> ((SELECT kind FROM logins l WHERE l.login_alias = NEW.login_alias) = 'explorer')
BEGIN SELECT RAISE(ABORT, 'rig role and login kind disagree: explorer rigs run the explorer login, pool and nightly rigs scripted ones'); END;

CREATE TRIGGER trg_rig_login_kind_upd BEFORE UPDATE OF role, login_alias ON rigs
WHEN (NEW.role = 'explorer') <> ((SELECT kind FROM logins l WHERE l.login_alias = NEW.login_alias) = 'explorer')
BEGIN SELECT RAISE(ABORT, 'rig role and login kind disagree: explorer rigs run the explorer login, pool and nightly rigs scripted ones'); END;

CREATE TRIGGER trg_login_kind_rigs BEFORE UPDATE OF kind ON logins
WHEN EXISTS (SELECT 1 FROM rigs r WHERE r.login_alias = NEW.login_alias AND (r.role = 'explorer') <> (NEW.kind = 'explorer'))
BEGIN SELECT RAISE(ABORT, 'rig role and login kind disagree: explorer rigs run the explorer login, pool and nightly rigs scripted ones'); END;

CREATE TABLE builds (
  build_id              TEXT PRIMARY KEY,   -- as read from the main window title
  channel               TEXT NOT NULL CHECK (channel IN ('branch','main','rc','released')),
  source_commit         TEXT NOT NULL,
  branch                TEXT,
  pipeline_artifact_url TEXT NOT NULL,
  installer_sha256      TEXT NOT NULL,
  first_seen_at         TEXT NOT NULL,
  released_at           TEXT                -- set at GA; starts a lineage
);

CREATE TABLE build_pairs (
  pair_id            INTEGER PRIMARY KEY,
  candidate_build_id TEXT NOT NULL REFERENCES builds(build_id),
  base_build_id      TEXT NOT NULL REFERENCES builds(build_id),
  pair_kind          TEXT NOT NULL CHECK (pair_kind IN ('pr','merge','rc','aa')),
  merge_base_commit  TEXT,
  created_at         TEXT NOT NULL,
  CHECK (pair_kind = 'aa' OR candidate_build_id <> base_build_id)
);

CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,       -- ULID
  run_type          TEXT NOT NULL CHECK (run_type IN ('pr','merge','rc','nightly','calibration')),
  pair_id           INTEGER REFERENCES build_pairs(pair_id),   -- NULL for nightly
  lifecycle         TEXT NOT NULL CHECK (lifecycle IN ('queued','running','finished')),
  outcome           TEXT CHECK (outcome IN ('complete','complete_with_env_errors','aborted_safety','aborted_budget',
                                            'aborted_harness')),
  abort_reason      TEXT,
  abort_detail      TEXT,
  config_commit     TEXT NOT NULL,          -- sentinel-config commit, both sides
  config_tarball_sha256 TEXT NOT NULL,
  snapshot_set_sha256 TEXT NOT NULL,        -- decisions, baselines, regions, masks, floors, quarantine shipped to the rigs
  harness_sha       TEXT NOT NULL,
  authorization_ref TEXT NOT NULL,          -- the signed authorization of 6.12
  manifest_sha256   TEXT,                   -- change manifest artifact
  pr_ref            TEXT, pr_author TEXT,   -- for the two-person rule of 9.6
  budget_sec        INTEGER NOT NULL,
  started_at        TEXT NOT NULL, finished_at TEXT,
  data_window_start TEXT, data_window_end TEXT,
  scope_count       INTEGER NOT NULL DEFAULT 0,   -- non-synthetic checkpoints in scope, all shards
  catalog_versions_json TEXT,               -- popup and mutant catalog versions, preflight G3
  review_state      TEXT NOT NULL DEFAULT 'unreviewed'
                      CHECK (review_state IN ('unreviewed','partial','reviewed')),
  check_override_by TEXT, check_override_reason TEXT,   -- 9.6 Override check, system owner only
  summary_json      TEXT,                   -- counts, coverage matrix, run health, detector state; written at teardown
  base_commit_distance INTEGER CHECK (base_commit_distance BETWEEN 0 AND 20),   -- PR runs: merge base to the base build's commit (L-BLD-04)
  authorization_sha256 TEXT,                -- hash of the signed authorization document of authorization_ref (6.12)
  diff_params_sha256 TEXT,                  -- diff engine parameters the run used (3.7)
  reviewer_version_sha256 TEXT,             -- model id, prompt hash and few-shot version tuple (3.13, 10.4)
  requeue_of        TEXT REFERENCES runs(run_id),   -- the run this one re-queues (10.7, 11.2)
  pr_committers_json TEXT,                  -- the PR host's committer list for the two-person rule (9.6)
  FOREIGN KEY (outcome, abort_reason) REFERENCES outcome_reasons(outcome, reason),
  CHECK ((lifecycle = 'finished') = (outcome IS NOT NULL)),
  CHECK (outcome IS NULL OR outcome NOT LIKE 'aborted_%' OR abort_reason IS NOT NULL),
  CHECK (abort_reason IS NULL OR outcome LIKE 'aborted_%' OR outcome = 'complete_with_env_errors'),   -- the cause of a complete_with_env_errors run lives here too
  CHECK ((check_override_by IS NULL) = (check_override_reason IS NULL))
);

CREATE TABLE shards (                       -- one session on one rig; a run has one or more
  shard_id         TEXT PRIMARY KEY,        -- <run_id>/<n>
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  rig_id           TEXT NOT NULL REFERENCES rigs(rig_id),
  attempt          INTEGER NOT NULL DEFAULT 1,   -- a shard re-queued after a lost rig is a new attempt; a returning rig's rows land in outcomes_superseded (10.7)
  sides            TEXT NOT NULL CHECK (sides IN ('both','candidate','base')),   -- 11.3: frozen back to back, masked_live in lockstep
  scope_count      INTEGER NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('queued','running','finished','shipping',
                     'imported','import_failed','lost','superseded')),
  shard_outcome    TEXT CHECK (shard_outcome IN ('complete','complete_with_env_errors',
                     'aborted_safety','aborted_budget','aborted_harness')),
  abort_reason     TEXT, abort_detail TEXT,
  run_store_sha256 TEXT, ledger_sha256 TEXT, ledger_rows INTEGER,
  started_at TEXT, finished_at TEXT, imported_at TEXT,
  FOREIGN KEY (shard_outcome, abort_reason) REFERENCES outcome_reasons(outcome, reason),
  CHECK ((state = 'imported') = (imported_at IS NOT NULL))
);

CREATE TRIGGER trg_shard_rig_gate BEFORE INSERT ON shards
WHEN EXISTS (SELECT 1 FROM incidents i WHERE i.rig_id = NEW.rig_id AND i.closed_at IS NULL AND i.severity IN ('S1','S2'))
  OR EXISTS (SELECT 1 FROM rigs r JOIN logins l ON l.login_alias = r.login_alias
             WHERE r.rig_id = NEW.rig_id AND l.blocked_at IS NOT NULL AND l.unblocked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'rig has an open incident or a blocked login'); END;

CREATE TABLE env_fingerprints (
  fingerprint_id      INTEGER PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES runs(run_id),
  shard_id            TEXT NOT NULL REFERENCES shards(shard_id),
  side                TEXT NOT NULL CHECK (side IN ('candidate','base')),
  rig_id              TEXT NOT NULL REFERENCES rigs(rig_id),
  build_id            TEXT NOT NULL REFERENCES builds(build_id),
  fingerprint_sha256  TEXT NOT NULL,        -- hash of the canonical match-class fields
  fields_json         TEXT NOT NULL,        -- the 21 fields of 7.4 plus render_probe_hash, each with its class
  render_probe_hash   BLOB,                 -- Help > About text region pHash, compared at Hamming distance zero (3.3)
  read_at             TEXT NOT NULL,
  UNIQUE (shard_id, side)
);
```

**Checkpoints and outcomes.** A checkpoint id follows the grammar `<kind>/<surface-id>/<qualifier>` with kind one of `census`, `sweep`, `split`, `flow`; the surface id is a dotted hyphenated slug; the qualifier is `<workspace-id>` for a census visit, `<host>/<w>x<h>` for a size-sweep step (`<host>/<w>x<h>-asc` for a step of Ladder F's ascending pass, 2.5.2), `<splitter>/<fraction>` for a splitter step and `<flow-id>/<step-id>` for a flow checkpoint (2.5.2 The ladders; lint rule L-FLW-03 in 4.7 Flow file). Examples: `census/trade.active-trader.ladder/ws.tier-a.active-trader`, `sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360` (host one of `tab`, `sidebar`, `flexgrid`, `detached`, `dialog`, 4.1; only `flexgrid`, `detached` and `dialog` hosts take w x h steps, while `tab` and `sidebar` hosts are swept through their splitters only, 2.5.1), `split/trade.active-trader.ladder/main-vertical/0.35`, `flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07` (the flow-id segment is the file's full `flow_id` including its `flow.` prefix, 4.1). A census id may carry one optional dimension segment, `census/<surface-id>/<workspace-id>/<dim>=<value>[,<dim>=<value>]`, for the per-dimension census visits of 2.4 (option chain layouts, strike counts); soak cycle checkpoints are flow ids of the form `flow/<workspace-surface-id>/soak/c<nn>` (10.11). Because the ladder step is part of the id, one row per checkpoint per run is exact and the Reporter can name the first failing size. The step segment of a flow checkpoint is the flow's declared checkpoint slug, not a position, so inserting or reordering steps does not move it; a rename goes through `flows/RENAMES.yaml` and `renamed_from`, which carries baselines, decisions and flake statistics to the new id (gap G33). Ids are never reused; a retired id is tombstoned. The outcome row is written once, after targeted re-runs finish.

```sql
CREATE TABLE checkpoints (
  checkpoint_id     TEXT PRIMARY KEY,
  kind              TEXT NOT NULL CHECK (kind IN ('census','sweep','split','flow')),
  surface_id        TEXT NOT NULL,
  workspace_id      TEXT NOT NULL,
  flow_id TEXT, step_id TEXT,
  flow_class        TEXT CHECK (flow_class IN ('standard','login','fixture_reset','teardown','canary',
                      'paper_send_ok','explorer_candidate')),
  host              TEXT CHECK (host IS NULL OR host IN ('flexgrid','detached','dialog')),   -- sweep: the w x h hosts of 4.1; tab and sidebar hosts are swept through their splitters only (SD-05)
  size_w INTEGER, size_h INTEGER,           -- sweep
  splitter_id TEXT, fraction REAL,          -- split
  tier              INTEGER NOT NULL CHECK (tier IN (1,2,3)),
  data_mode         TEXT NOT NULL CHECK (data_mode IN ('frozen','masked_live')),
  recapturable      INTEGER NOT NULL DEFAULT 1,
  first_seen_commit TEXT NOT NULL,
  renamed_from      TEXT REFERENCES checkpoints(checkpoint_id),
  retired_in_build TEXT, retired_commit TEXT,
  CHECK (substr(checkpoint_id, 1, instr(checkpoint_id, '/') - 1) = kind),
  CHECK (checkpoint_id NOT GLOB '*[^a-zA-Z0-9./x=,-]*'),   -- hyphens inside segments; '=' and ',' only in a census dimension segment
  CHECK (length(checkpoint_id) - length(replace(checkpoint_id, '/', '')) = CASE kind WHEN 'census' THEN 2 ELSE 3 END
      OR (kind = 'census' AND length(checkpoint_id) - length(replace(checkpoint_id, '/', '')) = 3 AND instr(checkpoint_id, '=') > 0)),   -- census/<surface>/<workspace>[/<dim>=<value>,...] (2.4)
  CHECK ((kind = 'sweep') = (host IS NOT NULL AND size_w IS NOT NULL AND size_h IS NOT NULL)),
  CHECK (kind <> 'sweep' OR checkpoint_id GLOB ('sweep/*/' || host || '/' || size_w || 'x' || size_h)
      OR checkpoint_id GLOB ('sweep/*/' || host || '/' || size_w || 'x' || size_h || '-asc')),   -- <host>/<w>x<h>, or -asc for Ladder F's ascending pass (SD-04)
  CHECK ((kind = 'split') = (splitter_id IS NOT NULL AND fraction IS NOT NULL)),
  CHECK ((kind = 'flow') = (flow_id IS NOT NULL AND step_id IS NOT NULL AND flow_class IS NOT NULL))
);

CREATE TABLE outcomes (
  run_id              TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id       TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  shard_id            TEXT REFERENCES shards(shard_id),
  side                TEXT CHECK (side IN ('candidate','base','both')),   -- the side a landmark_missing, not_settled or base-step failure was observed on
  gate                TEXT,                 -- env_error only: an Orchestrator gate of 5.2, preflight.<item> of 12.7, connectivity, rig_lost; on an unreached checkpoint of an aborted_safety run, the aborting gate
  outcome             TEXT NOT NULL CHECK (outcome IN ('identical','noise','intended','collateral','needs_human',
                                                       'invariant_violation','assertion_failed','not_settled',
                                                       'not_comparable','landmark_missing','env_error',
                                                       'harness_error','not_run','unregistered_surface',
                                                       'needs_baseline')),
  reason              TEXT,                 -- from outcome_reasons
  reason_detail       TEXT,                 -- step id, landmark id, decision id, probe id, all fired invariant ids, assertion id and values, differing fingerprint fields
  rescued             INTEGER NOT NULL DEFAULT 0 CHECK (rescued IN (0,1)),   -- reached through passed_via_agent_fallback (5.5)
  base_source_run_id  TEXT REFERENCES runs(run_id),   -- the run whose base capture was reused (7.4, base_reused)
  data_epoch_id       TEXT,                 -- hash of the 7.9 epoch tuple, equal on both sides
  paper_asserted_at   TEXT,                 -- the paper-mode assertion the capture was released under (6.4)
  review_state        TEXT NOT NULL DEFAULT 'none' CHECK (review_state IN ('none','pending','done')),   -- 4.13; none when no review applies
  cand_capture_sha256 TEXT, base_capture_sha256 TEXT,
  cand_rig_id TEXT, base_rig_id TEXT,
  settle_ms           INTEGER,
  diff_pixels INTEGER, diff_pct REAL, ssim REAL, shift_dx INTEGER, shift_dy INTEGER,
  components INTEGER, largest_bbox TEXT,    -- x,y,w,h
  masked_pct          REAL,                 -- masked plus redaction share of the critical panel, for the mask budget of 3.8 (redaction boxes count)
  heatmap_sha256      TEXT,
  threshold_source    TEXT CHECK (threshold_source IN ('calibrated','uncalibrated')),
  recaptures          INTEGER NOT NULL DEFAULT 0,   -- targeted re-captures taken (3.10, 4.13)
  stability           TEXT CHECK (stability IN ('stable','unstable','unknown')),   -- 3.10; unstable is the not_settled (reason: unstable) case
  stability_cause     TEXT CHECK (stability_cause IN ('not_recapturable','budget')),   -- 3.10: with stability unknown; an exhausted run budget is budget, never a reason
  review_id           INTEGER,              -- reviews(review_id)
  decision_id         INTEGER,              -- decisions(decision_id) inherited by this row
  manifest_entry      TEXT, primitive TEXT,
  synthetic           INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),   -- 1 for canary mutants
  captured_at         TEXT,
  recorded_at         TEXT NOT NULL,
  outcome_id          TEXT UNIQUE,          -- oc-<run_id>-<seq> (4.13)
  profile_id          TEXT,                 -- the login's entitlement profile (2.12 coverage column)
  PRIMARY KEY (run_id, checkpoint_id),
  FOREIGN KEY (outcome, reason) REFERENCES outcome_reasons(outcome, reason),
  CHECK (outcome <> 'identical' OR reason IS NULL),
  CHECK (reason IS NOT NULL OR outcome IN ('identical','intended','collateral')),
  CHECK (reason IS NOT 'decision' OR decision_id IS NOT NULL),   -- decision:<id> of 4.13
  CHECK (stability_cause IS NULL OR stability = 'unknown'),
  CHECK (review_state <> 'pending' OR outcome = 'needs_human'),
  CHECK (review_state = 'none' OR outcome IN ('needs_human','intended','collateral','noise')),
  CHECK (synthetic = 0 OR decision_id IS NULL),    -- synthetic rows never match a decision (10.3)
  CHECK (gate IS NULL OR outcome = 'env_error'),
  CHECK (outcome <> 'env_error' OR reason <> 'gate' OR gate IS NOT NULL),
  CHECK (rescued = 0 OR decision_id IS NULL)       -- a rescued checkpoint never inherits a decision (5.5)
);

CREATE TRIGGER trg_outcome_reason_ins BEFORE INSERT ON outcomes
WHEN NEW.reason IS NOT NULL AND (
     (SELECT active FROM outcome_reasons WHERE outcome = NEW.outcome AND reason = NEW.reason) = 0
  OR ((SELECT parametric FROM outcome_reasons WHERE outcome = NEW.outcome AND reason = NEW.reason) = 1
      AND NEW.reason_detail IS NULL))
BEGIN SELECT RAISE(ABORT, 'retired reason or missing reason_detail'); END;

CREATE TRIGGER trg_outcome_reason_upd BEFORE UPDATE OF outcome, reason, reason_detail ON outcomes
WHEN NEW.reason IS NOT NULL AND (
     (SELECT active FROM outcome_reasons WHERE outcome = NEW.outcome AND reason = NEW.reason) = 0
  OR ((SELECT parametric FROM outcome_reasons WHERE outcome = NEW.outcome AND reason = NEW.reason) = 1
      AND NEW.reason_detail IS NULL))
BEGIN SELECT RAISE(ABORT, 'retired reason or missing reason_detail'); END;

CREATE TRIGGER trg_outcome_immutable BEFORE UPDATE OF outcome, reason, reason_detail ON outcomes
WHEN NOT (OLD.outcome = 'needs_human' AND OLD.review_state = 'pending' AND NEW.review_state = 'done'
          AND (   (NEW.outcome IN ('intended','collateral','noise','needs_human') AND NEW.review_id IS NOT NULL)
               -- end of review without a verdict (4.13): per-run spend ceiling, or the Reviewer job gave up
               OR (NEW.outcome = 'needs_human' AND NEW.reason IN ('reviewer_cap','reviewer_unavailable')
                   AND NEW.review_id IS NULL)))
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

CREATE TRIGGER trg_outcome_evidence_immutable BEFORE UPDATE OF cand_capture_sha256, base_capture_sha256,
  heatmap_sha256, diff_pixels, diff_pct, ssim, components, largest_bbox, masked_pct, captured_at, synthetic ON outcomes
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;

CREATE TRIGGER trg_review_state BEFORE UPDATE OF review_state ON outcomes
WHEN NOT (OLD.review_state = 'pending' AND NEW.review_state = 'done')
BEGIN SELECT RAISE(ABORT, 'review_state moves only from pending to done'); END;

CREATE TRIGGER trg_run_complete BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = 'complete' AND (
     EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0 AND o.outcome = 'env_error')
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.outcome = 'harness_error'
             AND o.reason IN ('store_write_failed','artifact_persist_failed','redaction_version_missing'))
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0
             AND o.outcome = 'not_comparable' AND o.reason = 'fingerprint_mismatch')   -- 1.10 per run
  OR EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = NEW.run_id AND p.verdict = 'unproven' AND p.verified_by IS NULL)   -- 6.6
  OR (SELECT count(*) FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0) < NEW.scope_count
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.state <> 'imported')
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND (
        (o.cand_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.cand_capture_sha256))
     OR (o.base_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.base_capture_sha256))
     OR (o.heatmap_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.heatmap_sha256)))))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: env_error, store failure, fingerprint mismatch, unproven proof, missing outcome, unimported shard or missing artifact'); END;

-- The checkpoints a run is asked to visit, with the persona of the visit (4.13 checkpoint_outcome.persona).
-- runs.scope_count is the count of these rows. With trg_run_complete_scope a run is complete only when every one
-- of them, by id, has an outcome (8.1), and the import of 8.2 can fill the silent ones.
CREATE TABLE run_scope (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  persona       TEXT NOT NULL,
  PRIMARY KEY (run_id, checkpoint_id)
) WITHOUT ROWID;

-- The 4.13 outcome record as written, so the store hands back the whole record (persona, classification,
-- diff regions: fields no column above holds). The `outcomes` columns stay the truth for the later changes.
CREATE TABLE outcome_records (
  run_id        TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  record_json   TEXT NOT NULL,
  PRIMARY KEY (run_id, checkpoint_id),
  FOREIGN KEY (run_id, checkpoint_id) REFERENCES outcomes(run_id, checkpoint_id)
) WITHOUT ROWID;

CREATE TRIGGER trg_outcome_record_immutable BEFORE UPDATE ON outcome_records
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

CREATE TRIGGER trg_outcome_record_keep BEFORE DELETE ON outcome_records
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

-- 4.13: "Nothing else is ever rewritten." The triggers above guard outcome, reason, reason_detail, review_state
-- and the evidence columns; these guard every other column, and deletion.
CREATE TRIGGER trg_outcome_keep BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

CREATE TRIGGER trg_outcome_columns_immutable BEFORE UPDATE OF run_id, checkpoint_id, shard_id, side, gate, rescued,
  base_source_run_id, data_epoch_id, paper_asserted_at, cand_rig_id, base_rig_id, settle_ms, shift_dx, shift_dy,
  threshold_source, recaptures, stability, stability_cause, manifest_entry, primitive, recorded_at, outcome_id,
  profile_id ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

-- The first later change of 4.13: decision_ref is set when a decision suppresses the finding, once.
CREATE TRIGGER trg_outcome_decision_ref BEFORE UPDATE OF decision_id ON outcomes
WHEN OLD.decision_id IS NOT NULL AND NEW.decision_id IS NOT OLD.decision_id
BEGIN SELECT RAISE(ABORT, 'decision_ref is set once (4.13)'); END;

-- The review id moves only with the pending -> done transition (4.13 second and third later changes).
CREATE TRIGGER trg_outcome_review_ref BEFORE UPDATE OF review_id ON outcomes
WHEN NEW.review_id IS NOT OLD.review_id
 AND NOT (OLD.review_state = 'pending' AND NEW.review_state = 'done')
BEGIN SELECT RAISE(ABORT, 'review_id is set only when the review ends (4.13)'); END;

-- F65: every env_error names its gate, from the gates list of taxonomy.yaml (4.13).
CREATE TRIGGER trg_outcome_env_gate BEFORE INSERT ON outcomes
WHEN NEW.outcome = 'env_error' AND (NEW.gate IS NULL OR NEW.gate NOT IN ('provisioned','launched','authenticated',
                                                                         'paper_mode_asserted','settings_attested',
                                                                         'workspace_loaded','fixture_verified',
                                                                         'canary_flow_passed','running','teardown',
                                                                         'reported','connectivity','rig_lost',
                                                                         'preflight.R1','preflight.R2','preflight.R3',
                                                                         'preflight.R4','preflight.R5','preflight.R6',
                                                                         'preflight.R7','preflight.B1','preflight.B2',
                                                                         'preflight.B3','preflight.H1','preflight.H2',
                                                                         'preflight.H3','preflight.H4','preflight.H5',
                                                                         'preflight.H6','preflight.H7','preflight.H8',
                                                                         'preflight.H9','preflight.H10',
                                                                         'preflight.N1','preflight.N2','preflight.N3',
                                                                         'preflight.N4','preflight.N5','preflight.N6',
                                                                         'preflight.N7','preflight.G1','preflight.G2',
                                                                         'preflight.G3','preflight.G4',
                                                                         'preflight.G5'))
BEGIN SELECT RAISE(ABORT, 'env_error needs a gate from taxonomy.yaml (F65)'); END;

-- SD-78: fingerprint_mismatch is a plain reason whose reason_detail, the differing field names, is never empty.
CREATE TRIGGER trg_outcome_fingerprint_detail BEFORE INSERT ON outcomes
WHEN NEW.outcome = 'not_comparable' AND NEW.reason = 'fingerprint_mismatch'
 AND (NEW.reason_detail IS NULL OR trim(NEW.reason_detail) = '')
BEGIN SELECT RAISE(ABORT, 'fingerprint_mismatch names the differing fields in reason_detail (SD-78)'); END;

-- 8.1 and 8.3: a run is complete only when every scoped checkpoint, by id, has a non-synthetic outcome.
CREATE TRIGGER trg_run_complete_scope BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = 'complete' AND EXISTS (
  SELECT 1 FROM run_scope s WHERE s.run_id = NEW.run_id AND NOT EXISTS (
    SELECT 1 FROM outcomes o WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND o.synthetic = 0))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: a scoped checkpoint has no outcome'); END;

-- 6.6 and 8.2: `complete` also needs no ORDERS SENT post-run proof and no shard that ended worse than complete (the
-- run outcome is the worst of the shards'); trg_run_complete holds the 1.10 conditions.
CREATE TRIGGER trg_run_complete_env BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = 'complete' AND (
     EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = NEW.run_id AND p.verdict = 'orders_sent')
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.shard_outcome <> 'complete'))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: an orders-sent post-run proof or a shard worse than complete'); END;
```

The immutability triggers make every outcome other than a pending `needs_human` final at write time, and they are how the `unreviewed` report of 10.7 Degraded modes works: a diff above threshold that awaits the Reviewer is written as `needs_human` with `review_state: pending`; the asynchronous Reviewer job may later set `intended`, `collateral` or `noise` (`noise` only through A/A), or leave `needs_human` with reason `reviewer_no_match` (which is also the outcome of a declared removed surface the Reviewer does not confirm, 3.11 row 25a) or `reviewer_uncertain`, as 3.11's table gives, and in every case sets `review_state: done` with the review id (4.13, 3.11). A pending row that never gets a verdict also ends its review, the third later change 4.13 allows: when the per-run spend ceiling ends the run its reason becomes `reviewer_cap` with `cause: dollar_cap` in `reason_detail`, and when the Reviewer job gives up on it the reason stays `reviewer_unavailable`; both keep `needs_human`, set `review_state: done` with no review id, and are the second branch of `trg_outcome_immutable` (10.7, 11.4). If a human decision already exists on the item when the verdict lands (a `decisions` row whose finding is the item's), the Reporter stores the `reviews` row and sets `review_state: done` without touching `outcome` or `reason`: the verdict is an annotation only and never changes the outcome or the decision (10.7). Nothing else is ever rewritten. The run trigger is the rule that a run never reports `complete` while any shard is unimported, any scoped checkpoint has no outcome, any outcome names an artifact the central tree lacks, any checkpoint is `env_error`, `not_comparable (reason: fingerprint_mismatch)` or a store-failure `harness_error`, or any `post_run_proofs` row of the run reads UNPROVEN (1.10 `complete_with_env_errors`). Rows of a shard attempt that was superseded (a lost rig that returned after its shard was re-queued, 10.7) are kept for diagnosis in a table of the same shape and never read by the Reporter's counts:

```sql
CREATE TABLE outcomes_superseded AS SELECT o.*, 1 AS attempt FROM outcomes o WHERE 0;   -- no triggers; filled by the import of a returning rig
```

**Step events.** One row per input event, popup action, capture, gateway decision, Watchdog event and relaunch, and per size step, registry probe, Oracle source fallback, focus correction, hook reinstall, foreign window, export, clipboard read, recovery login and Explorer action, in scripted, agent and Explorer modes alike (gap G53). Retries, fallbacks and gateway drops are rows here, which is what makes them visible.

```sql
CREATE TABLE step_events (
  event_id          INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  shard_id          TEXT NOT NULL REFERENCES shards(shard_id),
  side              TEXT NOT NULL CHECK (side IN ('candidate','base')),
  seq               INTEGER NOT NULL,       -- order within the shard
  source            TEXT NOT NULL CHECK (source IN ('orchestrator','gateway_hook','watchdog')),
  flow_id TEXT, step_id TEXT, checkpoint_id TEXT,
  wall_at           TEXT NOT NULL, mono_ms INTEGER NOT NULL,
  mode              TEXT NOT NULL CHECK (mode IN ('scripted','agent','explorer','system')),
  action            TEXT NOT NULL,          -- click, dblclick, rclick, drag, wheel, key_chord, type, secret, wait,
                                            -- capture, popup, relogin, reset_input_state, relaunch, resume,
                                            -- watchdog_hang, watchdog_crash, kill_switch, gateway_decision, size_step,
                                            -- probe, oracle_source_fallback, focus_correction, hook_reinstalled,
                                            -- foreign_window, export, clipboard, recovery_login, explorer_action
  locator_level     TEXT CHECK (locator_level IN ('jab','image','ocr','agent')),
  attempt           INTEGER NOT NULL DEFAULT 1,   -- 2 = the single permitted retry
  step_outcome      TEXT CHECK (step_outcome IN ('passed','passed_after_retry','passed_via_agent_fallback','failed',
                                                 'blocked_by_gateway')),
  target_text       TEXT,                   -- as script or model stated it; '<redacted:login.password>'
  screen_x INTEGER, screen_y INTEGER, win_x INTEGER, win_y INTEGER,
  keys              TEXT,
  gate_decision     TEXT NOT NULL CHECK (gate_decision IN ('emitted','dropped','refused','late_up','none')),   -- 4.13 gateway_event.decision: dropped by the hook, refused by the slow half
  gate_reason       TEXT CHECK (gate_reason IN (
                      'no_token','token_mismatch_coords','token_expired','deny_rect','shift_click_dialog','dialog_not_ok',
                      'blocked_key_dialog','blocked_chord_dialog','move_outside_allow','deny_all','not_armed',
                      'foreign_input','untokened_injected',
                      'never_tokenize','dialog_allowlist','deny_profile','guarded_key_refused','hook_host_dead',
                      'hook_reinstalled')),   -- 4.13 gateway_event.reason: the hook and slow-half codes of 5.4.3 and 5.4.4 themselves, not a coarse class
  gate_class        TEXT CHECK (gate_class IN ('allowed','safety','integrity','none')),   -- the 5.4.4 split; safety reasons are attempted sends
  drill             INTEGER NOT NULL DEFAULT 0 CHECK (drill IN (0,1)),   -- tagged by the drainer inside the fire drill's declared raw-injection phase (6.5)
  pre_hash TEXT, post_hash TEXT,            -- sha256 of pre and post screenshots
  click_crop_sha256 TEXT,                   -- 96x96 crop at the click point
  popup_title       TEXT,                   -- catalog entry matched, or raw title for unknown_popup
  model_id TEXT, prompt_hash TEXT,          -- agent and explorer modes
  reason            TEXT,                   -- retry or fallback reason, Watchdog event id
  app_uptime_s INTEGER, app_restart_count INTEGER,
  duration_ms       INTEGER,
  expected_outcome  TEXT,                   -- Explorer: the outcome the model stated before acting (2.9, 6.2)
  rect_w INTEGER, rect_h INTEGER,           -- target rect size at the click point (6.2)
  usage_json        TEXT,                   -- the model response's usage block, agent and explorer modes (11.4)
  injected          INTEGER NOT NULL DEFAULT 0 CHECK (injected IN (0,1)),   -- the hook saw the injected flag (5.4.8)
  token_id          INTEGER,                -- the gateway token the event consumed (5.4.3)
  UNIQUE (shard_id, seq),
  CHECK (gate_decision NOT IN ('dropped','refused') OR gate_reason IS NOT NULL),
  CHECK (action <> 'secret' OR (target_text IS NOT NULL AND target_text LIKE '<redacted:%>'))   -- a secret is never stored (6.2, 6.9)
);

CREATE TABLE oracle_extractions (           -- 2.8.1: every Oracle extraction is stored; rules can re-run over these rows
  extraction_id     INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id     TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  side              TEXT NOT NULL CHECK (side IN ('candidate','base')),
  assertion_id      TEXT NOT NULL,          -- flow assertion id, or rule.<region_id>.<field> for a rule region
  region_id         TEXT,
  field             TEXT NOT NULL,
  source            TEXT NOT NULL CHECK (source IN ('ocr','export','jab','api')),   -- the 4.7 assertion source enum
  text              TEXT,                   -- raw text read; rows separated by newlines
  value             TEXT,                   -- the parsed value
  confidence        REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
  second_pass       INTEGER NOT NULL DEFAULT 0 CHECK (second_pass IN (0,1)),   -- the 2x OCR re-read (2.8.1)
  crop_sha256       TEXT,
  read_at           TEXT NOT NULL,
  UNIQUE (run_id, checkpoint_id, side, assertion_id, field, source, second_pass)
);
```

**Reviews, findings and decisions.** A finding is what a human sees; an outcome is what happened. Finding kinds reuse outcome names where the finding is the outcome and add the harness-side kinds the brief calls for; the list is generated from the `finding_kinds` list of `taxonomy.yaml`. Triage states are the spec-wide set adopted in the integration notes.

```sql
CREATE TABLE reviews (
  review_id        INTEGER PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id    TEXT NOT NULL,
  region_id        TEXT,
  diff_hash        TEXT NOT NULL,           -- queue key of 10.7 with run and checkpoint
  model_id         TEXT NOT NULL,           -- from the response, not the request
  prompt_hash      TEXT NOT NULL,
  fewshot_version TEXT, redaction_version TEXT, mask_version TEXT,
  request_sha256   TEXT NOT NULL,           -- canonical JSON of all inputs, stored as an artifact
  response_json    TEXT NOT NULL,           -- structured output as returned (3.13)
  verdict          TEXT NOT NULL CHECK (verdict IN ('matches_manifest','partially_matches_manifest',
                                                    'does_not_match_manifest','no_visible_difference',
                                                    'cannot_judge')),
  mapped_outcome   TEXT NOT NULL CHECK (mapped_outcome IN ('intended','collateral','noise','needs_human')),
  confidence       REAL NOT NULL,
  votes            INTEGER NOT NULL DEFAULT 1,     -- majority vote at borderline (3.12)
  latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
  batch_id         TEXT,
  created_at       TEXT NOT NULL,
  response_model   TEXT,                    -- the model the response named, kept beside model_id when the endpoint aliases (3.13)
  registry_version TEXT,                    -- Surface Registry version of the inputs (3.13)
  request_ids_json TEXT,                    -- the vendor request id of every call, votes included (3.13)
  cache_read_tokens INTEGER,
  votes_json       TEXT,                    -- each vote's verdict and confidence at borderline (3.12)
  task             TEXT CHECK (task IS NULL OR task IN ('manifest_match','describe_collateral',
                                                        'manifest_visibility')),   -- 3.13 tasks A to C
  intent           TEXT,                    -- the declared intent the item was judged against (3.13)
  UNIQUE (run_id, checkpoint_id, diff_hash, model_id, prompt_hash)
);

CREATE TABLE findings (
  finding_id       INTEGER PRIMARY KEY,
  natural_key      TEXT NOT NULL UNIQUE,    -- run, checkpoint, kind, region or step, invariant or assertion id
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id    TEXT REFERENCES checkpoints(checkpoint_id),
  kind             TEXT NOT NULL CHECK (kind IN ('collateral','intended','needs_human','invariant_violation',
                                                 'assertion_failed','landmark_missing','needs_baseline',
                                                 'unregistered_surface','dependency_map_gap','manifest_missing',
                                                 'unknown_popup','popup_rate_increase','locator_failure','app_crash',
                                                 'explorer_candidate','layout_shift','landmark_moved','color_shift',
                                                 'window_geometry_changed','global_rendering_shift',
                                                 'settle_regression','gateway_integrity','new_log_signature',
                                                 'jab_tree_changed','mask_budget_exceeded','mask_expired',
                                                 'floor_exception_expired','invariant_exception_expired',
                                                 'redaction_miss','secret_leaked','explorer_order_stage',
                                                 'missed_in_pr_run','artifact_missing','orphaned_baseline',
                                                 'canary_miss','intended_change_not_observed',
                                                 'flow_maintenance_needed','harness_error','memory_warning',
                                                 'hardening')),
                     -- equals the finding vocabulary of 4.13; generated from taxonomy.yaml finding_kinds
  tier             INTEGER CHECK (tier IN (1,2,3)),
  severity         TEXT CHECK (severity IN ('S1','S2','S3')),   -- invariants, 2.7
  pre_existing     INTEGER NOT NULL DEFAULT 0,   -- violation present on both builds (2.7)
  side             TEXT CHECK (side IN ('candidate','base','both')),
  region_id        TEXT,                    -- region, or grid cell g-<row>-<col> outside any region (9.7)
  step_id          TEXT,
  primitive        TEXT,                    -- hosting primitive consumed
  manifest_entry   TEXT,                    -- manifest entry that touched it
  phash            BLOB,                    -- 8 bytes, anchored crop
  category         TEXT CHECK (category IN ('intended','collateral','noise','needs_human','invariant_violation',
                                            'assertion_failed')),
  cluster_id       TEXT,
  description      TEXT NOT NULL,
  evidence_json    TEXT NOT NULL,           -- artifact hashes, step_events range, raw and expected values, candidate flow path
  decision_id      INTEGER,                 -- the decision this finding inherited or produced
  ticket_ref       TEXT,
  triage_state     TEXT NOT NULL DEFAULT 'open' CHECK (triage_state IN ('open','confirmed_bug','intended','noise',
                                                                        'agent_mistake','expected_behavior',
                                                                        'fixed_pending_verify','closed')),
  synthetic        INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),
  first_seen_run_id TEXT,
  created_at       TEXT NOT NULL,
  detail           TEXT,                    -- what the finding's reason points at, e.g. static_impact (1.8, 2.10)
  closed_at        TEXT,                    -- starts the 180-day release of its evidence (8.6)
  contested        INTEGER NOT NULL DEFAULT 0 CHECK (contested IN (0,1)),   -- conflicting decisions on the signature (9.7)
  held             INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0,1)),        -- held from the report: Explorer, not reproduced (2.9)
  explorer_json    TEXT,                    -- the 4.13 explorer block of an explorer_candidate finding
  CHECK (explorer_json IS NULL OR kind = 'explorer_candidate')
);

CREATE TABLE decisions (
  decision_id       INTEGER PRIMARY KEY,
  checkpoint_id     TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id         TEXT NOT NULL,          -- region id, or grid cell g-<row>-<col>
  phash             BLOB NOT NULL,          -- 64-bit perceptual hash of the anchored crop (3.7)
  hamming_tolerance INTEGER NOT NULL DEFAULT 8 CHECK (hamming_tolerance BETWEEN 0 AND 64),   -- 6 for tier 1; calibration may lower (9.7)
  category          TEXT NOT NULL CHECK (category IN ('intended','collateral','noise','needs_human',
                                                      'invariant_violation','assertion_failed')),
  action            TEXT NOT NULL CHECK (action IN ('confirm_bug','mark_intended','request_mask','noise_signature',
                                                    'agent_mistake','expected_behavior','approve_baseline',
                                                    'reject_baseline','fixed_in_build')),                          -- the UI action of 4.14 and 9.6
  verdict           TEXT NOT NULL CHECK (verdict IN ('confirmed_bug','intended','noise')),   -- the stored effect
  source            TEXT NOT NULL CHECK (source IN ('report_ui','pr_comment','jira_sync','explorer_triage')),
  decided_by        TEXT NOT NULL,          -- SSO identity of a human; for jira_sync the ticket's resolver
  proposed_by       TEXT,                   -- PR author when a tier 1 or 2 intended proposal was confirmed by another
  confirmed_by      TEXT,                   -- 4.14's name for the second human of the two-person rule; equals decided_by when a proposal was confirmed
  decided_at        TEXT NOT NULL,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  build_id          TEXT REFERENCES builds(build_id),
  reason            TEXT NOT NULL,
  ticket_ref        TEXT,
  mask_ref          TEXT,                   -- request_mask: mask id once the record merges
  baseline_id       INTEGER,                -- approve_baseline, reject_baseline
  fixed_in_build_id TEXT REFERENCES builds(build_id),   -- fixed_in_build: next run asserts the signature is gone
  cluster_id        TEXT,                   -- written per member signature when decided as a cluster
  expires_at        TEXT,                   -- noise_signature, request_mask and waivers: default 30 days
  status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','superseded','reopened',
                                                                     'revoked')),   -- 4.14
  supersedes_decision_id INTEGER REFERENCES decisions(decision_id),
  revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT,
  source_finding_id INTEGER REFERENCES findings(finding_id),
  bbox_q4           TEXT,                   -- x,y,w,h of the largest component quantized to 4 px: the fallback key at or below noise spread (4.14)
  role              TEXT CHECK (role IS NULL OR role IN ('triage_rotation','flow_owner','system_owner',
                                                         'release_manager','pr_author','ticket_resolver')),   -- 4.14, 9.12, L-DEC-01
  reopen_count      INTEGER NOT NULL DEFAULT 0 CHECK (reopen_count >= 0),   -- fixed_in_build signatures that returned (4.14)
  CHECK ((action IN ('confirm_bug','fixed_in_build') AND verdict = 'confirmed_bug')
      OR (action IN ('mark_intended','approve_baseline') AND verdict = 'intended')
      OR (action IN ('request_mask','noise_signature','agent_mistake','expected_behavior') AND verdict = 'noise')
      OR (action = 'reject_baseline' AND verdict IN ('confirmed_bug','noise'))),   -- the 4.14 and 9.6 mapping; request_mask is inert until mask_ref is set
  CHECK (action NOT IN ('mark_intended','fixed_in_build') OR ticket_ref IS NOT NULL),   -- L-DEC-02 at write time
  CHECK (action <> 'reject_baseline' OR verdict <> 'confirmed_bug' OR ticket_ref IS NOT NULL),
  -- confirm_bug may be written with ticket_ref NULL while Jira is unreachable; the outbox job fills it and the digest lists the row as without ticket until then (9.9)
  CHECK (action <> 'fixed_in_build' OR fixed_in_build_id IS NOT NULL),
  CHECK (action NOT IN ('approve_baseline','reject_baseline') OR baseline_id IS NOT NULL),
  CHECK (action NOT IN ('noise_signature','request_mask') OR expires_at IS NOT NULL),
  CHECK (decided_by NOT IN ('reviewer','system','orchestrator','reporter')),                        -- L-DEC-01
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK ((revoked_at IS NOT NULL) = (status = 'revoked'))
);

CREATE TRIGGER trg_decision_two_person BEFORE INSERT ON decisions
WHEN NEW.action = 'mark_intended'
 AND (SELECT tier FROM checkpoints c WHERE c.checkpoint_id = NEW.checkpoint_id) IN (1, 2)
 AND NEW.decided_by = (SELECT pr_author FROM runs r WHERE r.run_id = NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'two-person rule: the PR author cannot mark a tier 1 or 2 diff intended alone'); END;

CREATE TRIGGER trg_decision_money_cell BEFORE INSERT ON decisions
WHEN NEW.action IN ('noise_signature','request_mask')
 AND EXISTS (SELECT 1 FROM regions g WHERE g.checkpoint_id = NEW.checkpoint_id AND g.region_id = NEW.region_id
             AND g.money_cell = 1 AND g.config_commit = (SELECT config_commit FROM runs r WHERE r.run_id = NEW.run_id))
BEGIN SELECT RAISE(ABORT, 'L-DEC-04: no noise signature or mask on a money cell'); END;

CREATE TRIGGER trg_decision_not_synthetic BEFORE INSERT ON decisions
WHEN NEW.source_finding_id IS NOT NULL
 AND (SELECT synthetic FROM findings f WHERE f.finding_id = NEW.source_finding_id) = 1
BEGIN SELECT RAISE(ABORT, 'synthetic findings never produce decisions'); END;

-- request_mask acts once its mask file lands: mask_ref goes from NULL to a value once (4.14)
CREATE TRIGGER trg_decision_mask_ref BEFORE UPDATE OF mask_ref ON decisions
WHEN NEW.mask_ref IS NOT OLD.mask_ref
 AND NOT (OLD.mask_ref IS NULL AND NEW.mask_ref IS NOT NULL AND OLD.action = 'request_mask')
BEGIN SELECT RAISE(ABORT, 'mask_ref is set once, on a request_mask decision (4.14)'); END;

-- a confirm_bug written while Jira was unreachable gets its ticket from the outbox once (L-DEC-02, 9.9)
CREATE TRIGGER trg_decision_ticket_ref BEFORE UPDATE OF ticket_ref ON decisions
WHEN NEW.ticket_ref IS NOT OLD.ticket_ref
 AND NOT (OLD.ticket_ref IS NULL AND NEW.ticket_ref IS NOT NULL AND OLD.action = 'confirm_bug')
BEGIN SELECT RAISE(ABORT, 'ticket_ref is filled once, on a confirm_bug decision written without one (9.9)'); END;
```

A decision is never deleted and never edited in place: a revoke sets `revoked_at`, `revoked_by` and `revoke_reason` together and moves `status` to `revoked` (4.14); a replacement is a new row whose `supersedes_decision_id` points at the old one, which the writer marks `superseded`. The CHECK constraints allow revocation fields only on a revoked row, always all three together. Every action maps to one stored verdict and one effect; 9.6 One-click actions and who may perform them shows the same table from the user's side.

| Action (4.14) | Verdict stored | Inherited outcome on a later matching diff | Finding triage state | Ticket |
|---|---|---|---|---|
| confirm_bug | confirmed_bug | computed outcome unchanged; finding filed under the ticket, no new alert | confirmed_bug | required |
| mark_intended | intended | `intended`; for `invariant_violation` and `assertion_failed` the outcome never changes and the finding is suppressed as waived until `expires_at` | intended | required |
| noise_signature | noise | `noise` | noise | optional |
| request_mask | noise, inert until `mask_ref` is set when the mask record merges; then the region is masked and no diff arises; a request whose mask would put the panel over its mask budget is refused by the Reporter service API before any row is written (L-DEC-04), and the finding stays open until `confirm_bug` or `mark_intended` | none through the signature; the mask region | open until merge, then noise | optional |
| agent_mistake | noise | none; Explorer finding signature suppressed, candidate flow discarded | agent_mistake | none |
| expected_behavior | noise | none; Explorer finding signature suppressed, candidate flow kept for promotion with an assertion (9.6) | expected_behavior | none |
| approve_baseline | intended | none through the signature; the `baselines` row becomes `approved` and the image is the lineage baseline and the canary base | closed | optional |
| reject_baseline | confirmed_bug with a ticket (the candidate shows a defect), else noise (not representative) | none; the `baselines` row is `retired` and `needs_baseline` persists until a new candidate is approved | confirmed_bug, or open | required for confirmed_bug |
| fixed_in_build | confirmed_bug, with `fixed_in_build_id`; supersedes the confirm_bug row | from that build on the run asserts the signature is gone; a return sets `status: reopened` and reopens the finding | fixed_pending_verify, then closed | required |

Signature lookup at diff time runs on the rig against the shipped decision snapshot; the tolerance comes from the row (default 8 of 64 bits, 6 for tier-1 checkpoints, lowered further by calibration per 9.7 Decision persistence and inheritance):

```sql
SELECT decision_id, verdict FROM decisions
WHERE checkpoint_id = :cp AND region_id = :region AND category = :cat
  AND verdict IS NOT NULL AND status IN ('active','reopened')
  AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > :now)
  AND hamming(phash, :phash) <= hamming_tolerance
ORDER BY hamming(phash, :phash), decided_at DESC;   -- every active decision within tolerance; no LIMIT, the verdict set is the answer (9.7)
```

The query returns the full verdict set for the signature. When every returned row carries one verdict, the first row (nearest by Hamming distance, then newest) is inherited and the others are listed as related; when the verdicts differ, nothing is inherited, the finding keeps its computed outcome and is listed in Unexpected as conflicting with both decision ids, and the triage rotation resolves it by superseding one row (9.7). A rescued checkpoint (`rescued = 1`) never runs the query (5.5). Inheritance: `noise` and `intended` verdicts set the new outcome row to that outcome with the decision id; a confirmed bug verdict leaves the outcome as computed and files the finding under the existing ticket without a new alert. For `invariant_violation` and `assertion_failed` the outcome never changes; the decision only suppresses the finding in the report. Synthetic outcomes never consult this query (10.3 Canary Service and the mutant catalog). A decision with a fixed-in build whose signature reappears on or after that build reopens the finding.

**Baselines, regions and masks.** Baseline images are never deleted; the lifecycle moves a pointer. Approval is human-only: the CHECK constraints make a row approved by anything other than a named person unwritable, and the trigger enforces L-DEC-05.

```sql
CREATE TABLE baselines (
  baseline_id     INTEGER PRIMARY KEY,
  checkpoint_id   TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  lineage         TEXT NOT NULL,            -- release channel; 'released' in phases 1 to 3
  build_id        TEXT NOT NULL REFERENCES builds(build_id),
  image_sha256    TEXT NOT NULL,
  sidecar_sha256  TEXT,                     -- OCR word boxes and bounds of the baseline image
  state           TEXT NOT NULL CHECK (state IN ('candidate','approved','retired')),
  source_run_id   TEXT REFERENCES runs(run_id),
  proposed_at     TEXT NOT NULL,
  approved_by TEXT, approved_at TEXT, approval_reason TEXT, ticket_ref TEXT,
  cluster_id      TEXT,                     -- signature cluster when approved as a group
  retired_at TEXT, retired_by TEXT, retired_in_build TEXT, superseded_by INTEGER REFERENCES baselines(baseline_id),
  CHECK (state <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
                                 AND approval_reason IS NOT NULL)),
  CHECK (approved_by IS NULL OR approved_by NOT IN ('reviewer','system','orchestrator','reporter'))
);
CREATE UNIQUE INDEX ux_baseline_approved ON baselines(checkpoint_id, lineage) WHERE state = 'approved';

CREATE TRIGGER trg_baseline_approve BEFORE UPDATE OF state ON baselines
WHEN NEW.state = 'approved' AND (
     (SELECT outcome FROM runs r WHERE r.run_id = NEW.source_run_id) IS NOT 'complete'
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.source_run_id
             AND o.checkpoint_id = NEW.checkpoint_id AND o.synthetic = 1))
BEGIN SELECT RAISE(ABORT, 'L-DEC-05: baseline source run must be complete and not synthetic'); END;

CREATE TABLE regions (                      -- mirror of the Region Registry at a config commit
  checkpoint_id   TEXT NOT NULL,
  region_id       TEXT NOT NULL,
  config_commit   TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('freeze','rule','mask','diff','redaction')),
  money_cell      INTEGER NOT NULL DEFAULT 0 CHECK (money_cell IN (0,1)),
  identity_field  INTEGER NOT NULL DEFAULT 0 CHECK (identity_field IN (0,1)),   -- redaction over account number or holder name: excluded from the mask budget (4.8, 6.10)
  landmark_id     TEXT NOT NULL,
  dx INTEGER NOT NULL, dy INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
  hamming_tolerance INTEGER CHECK (hamming_tolerance BETWEEN 0 AND 64),   -- lowered by calibration (9.7); NULL = default
  money_role      TEXT CHECK (money_role IS NULL OR money_role IN ('qty','price','bp_effect','max_loss','send',
                                                                   'cancel','delete')),   -- 4.8 (SD-28)
  exact_fallback  INTEGER NOT NULL DEFAULT 0 CHECK (exact_fallback IN (0,1)),   -- tolerance at or below noise spread: pHash equality plus the 4 px box (4.14, 9.7)
  PRIMARY KEY (checkpoint_id, region_id, config_commit),
  CHECK (money_cell = 0 OR kind IN ('diff','freeze','rule')),   -- L-RGN-04: never masked; diff or freeze on a flow checkpoint, rule on census and sweep (3.8, SD-29)
  CHECK (identity_field = 0 OR kind = 'redaction'),
  CHECK (money_cell = (money_role IS NOT NULL))           -- a money cell is exactly a region with a money role (4.8, SD-28)
);

CREATE TABLE masks (                        -- mirror of mask records at a config commit
  mask_id         TEXT NOT NULL,
  config_commit   TEXT NOT NULL,            -- commit this record was loaded from
  checkpoint_id   TEXT NOT NULL,
  landmark_id     TEXT NOT NULL,            -- template located each run
  dx INTEGER NOT NULL, dy INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
  owner           TEXT NOT NULL,
  reason          TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_by_kind TEXT NOT NULL CHECK (created_by_kind = 'human'),
  created_at      TEXT NOT NULL,
  expires_at      TEXT NOT NULL,            -- default creation plus 30 days
  critical_panel  TEXT,                     -- named panel for the budget check
  decision_id     INTEGER REFERENCES decisions(decision_id),   -- the request_mask decision
  PRIMARY KEY (mask_id, config_commit)
);
```

**Flake, canary, calibration and eval.**

```sql
CREATE TABLE flake_events (                 -- one row per unstable targeted re-run (3.10)
  flake_event_id INTEGER PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id  TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id      TEXT,
  captures_json  TEXT NOT NULL,             -- sha256 of every capture compared
  metrics_json   TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE flake_stats (                  -- rolling 30 days, refreshed by the Nightly run (10.5)
  checkpoint_id    TEXT PRIMARY KEY REFERENCES checkpoints(checkpoint_id),
  observations     INTEGER NOT NULL DEFAULT 0,   -- captures compared
  recaptures       INTEGER NOT NULL DEFAULT 0,
  unstable_total   INTEGER NOT NULL DEFAULT 0,
  unstable_7d      INTEGER NOT NULL DEFAULT 0,
  flip_total       INTEGER NOT NULL DEFAULT 0,   -- verdict flips between runs of one build
  flips_same_build INTEGER NOT NULL DEFAULT 0,
  step_retries     INTEGER NOT NULL DEFAULT 0,
  agent_fallbacks  INTEGER NOT NULL DEFAULT 0,
  flake_score      REAL NOT NULL DEFAULT 0,      -- unstable plus flips over observations
  last_flip_run_id TEXT,
  quarantined      INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0,1)),
  quarantine_commit TEXT,                   -- sentinel-config commit whose quarantine.yaml holds the record
  quarantined_at TEXT, quarantined_by TEXT, quarantine_owner TEXT, quarantine_ticket_ref TEXT,
  quarantine_reason TEXT CHECK (quarantine_reason IS NULL OR quarantine_reason IN ('flake','aa_drift','aa_floor',
                      'maintenance_budget','fire_drill','safety_canary','kill_switch','gateway_drops','fallback_rate')),   -- 10.5 (SD-96)
  updated_at       TEXT NOT NULL,
  CHECK (quarantined = 0 OR (quarantine_commit IS NOT NULL AND quarantine_owner IS NOT NULL
                             AND quarantine_ticket_ref IS NOT NULL))
);

CREATE TABLE synthetic_outcomes (           -- 10.3: what each canary mutant produced; outcomes holds real rows only (SD-91)
  synthetic_id      INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  mutant_id         TEXT NOT NULL,
  checkpoint_id     TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  outcome           TEXT NOT NULL CHECK (outcome IN ('identical','noise','intended','collateral','needs_human',
                                                     'invariant_violation','assertion_failed','not_settled',
                                                     'not_comparable','landmark_missing','env_error','harness_error',
                                                     'not_run','unregistered_surface','needs_baseline')),
  reason            TEXT,
  reason_detail     TEXT,
  gate              TEXT,
  mutant_sha256     TEXT,
  recorded_at       TEXT NOT NULL,
  UNIQUE (run_id, mutant_id, checkpoint_id),
  FOREIGN KEY (outcome, reason) REFERENCES outcome_reasons(outcome, reason)
);

CREATE TABLE canary_manifests (             -- 10.3 determinism: the generated catalog of a run, re-generated and compared
  run_id            TEXT PRIMARY KEY REFERENCES runs(run_id),
  catalog_sha256    TEXT NOT NULL,
  manifest_json     TEXT NOT NULL,          -- mutant id, checkpoint, seed and mutant hash per generated mutant
  created_at        TEXT NOT NULL
);

CREATE TABLE canary_results (
  canary_id         INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  kind              TEXT NOT NULL DEFAULT 'mutant' CHECK (kind IN ('mutant','safety_canary','fire_drill',
                                                                   'path_equivalence')),   -- 10.3 mutants; 6.3 to 6.10 safety canaries; 6.5 drill items; the nightly re-evaluated real pair of 10.3
  mutant_id         TEXT NOT NULL,          -- M01 to M33 and on of sentinel-config/canaries/mutants.yaml (12.5); drill items are drill-1 to drill-5; safety canaries name their control (6.3 to 6.10)
  catalog_version   TEXT NOT NULL,
  checkpoint_id     TEXT NOT NULL,
  defect_class      TEXT NOT NULL,          -- kind mutant: one of the 26 snake_case class names of 10.3 and 12.5, equal to the class
                                            -- enum of 4.11; every other kind: the kind itself, with mutant_id naming the item (SD-81)
  money_cell        INTEGER NOT NULL DEFAULT 0 CHECK (money_cell IN (0,1)),
  seed              INTEGER NOT NULL,
  base_sha256 TEXT, mutant_sha256 TEXT,
  expected_json     TEXT NOT NULL,          -- the mutant's expected outcome list
  produced_outcome  TEXT,
  produced_reason   TEXT,
  state             TEXT NOT NULL CHECK (state IN ('detected','missed','detector_blind','detector_degraded',
                                                   'generation_failed')),
  absorbed_by       TEXT,                   -- mask id, decision id, threshold or landmark that swallowed a miss
  class_rate REAL, class_floor REAL,        -- that night's class detection rate and rolling floor
  reviewer_verdict TEXT, reviewer_confidence REAL,
  mask_version TEXT, threshold_version TEXT, model_id TEXT, prompt_hash TEXT,
  created_at        TEXT NOT NULL,
  synthetic_ref     INTEGER REFERENCES synthetic_outcomes(synthetic_id),   -- the mutant's produced outcome row (SD-91)
  CHECK ((kind = 'mutant' AND defect_class IN ('erased_control','swapped_digit','swapped_label','color_swap',
           'shifted_panel','clipped_label','overlapped_panels','missing_row','header_change','placeholder','font_height',
           'scrollbar','redaction','sub_floor','global_shift','landmark_loss','study_removed','aa_control',
           'wrong_quantity','blank_field','gateway','outside_parent','window_offscreen','misaligned_column',
           'focus_order','error_dialog'))
      OR (kind <> 'mutant' AND defect_class = kind)),   -- SD-81: a safety canary, drill item or path-equivalence pair names its kind
  CHECK (NOT (money_cell = 1 AND state = 'missed')),            -- a money-cell miss is always detector_blind
  CHECK (NOT (money_cell = 0 AND state = 'detector_blind')),
  CHECK (state <> 'detector_degraded' OR (class_rate IS NOT NULL AND class_floor IS NOT NULL)),
  CHECK (state NOT IN ('missed','detector_blind','detector_degraded') OR produced_outcome IS NOT NULL)
);

CREATE TABLE calibration_floors (           -- 3.9 step 3 and 4: per calibration key, checkpoint and region (SD-19, SD-69)
  floor_id           INTEGER PRIMARY KEY,
  checkpoint_id      TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id          TEXT NOT NULL,         -- '*' for whole window
  rendering_key_sha256 TEXT NOT NULL,       -- the calibration key: the match fields that affect pixels (7.13)
  fingerprint_sha256 TEXT,                  -- the fingerprint the floor was measured on (record)
  calibration_run_id TEXT NOT NULL REFERENCES runs(run_id),
  samples            INTEGER NOT NULL,      -- A/A pairs measured
  diff_pixels_p50 INTEGER, diff_pixels_p95 INTEGER, diff_pixels_max INTEGER,
  ssim_min           REAL,
  components_max INTEGER, largest_bbox_max TEXT,
  noise_spread       INTEGER,               -- largest pHash distance among the region's A/A pairs (9.7)
  fault_separation   INTEGER,               -- smallest distance between the region's distinct mutants (9.7)
  hamming_tolerance  INTEGER CHECK (hamming_tolerance BETWEEN 0 AND 8),   -- the effective tolerance the floors give (4.14, 9.7)
  exact_fallback     INTEGER NOT NULL DEFAULT 0 CHECK (exact_fallback IN (0,1)),   -- tolerance at or below the noise spread (9.7)
  pairs_json         TEXT,                  -- the per-pair metrics of 3.9 step 3 (15 pairs per checkpoint and region)
  settle_p50_ms      INTEGER,               -- time-to-settle p50, the settle_regression base (3.9)
  threshold_component_area INTEGER NOT NULL,   -- the four thresholds of 3.9
  threshold_changed_px INTEGER NOT NULL,
  threshold_delta_e  REAL NOT NULL,
  threshold_shift_px INTEGER NOT NULL,
  threshold_ssim     REAL,                  -- SSIM ranks and has no threshold (3.9); NULL unless a ranking floor is kept
  regions_file_sha256 TEXT, mask_versions_json TEXT, redaction_version TEXT,   -- what the floor was measured under (7.13)
  workspace_sha256   TEXT, epoch_definition_sha256 TEXT,
  nonzero_on_frozen  INTEGER NOT NULL DEFAULT 0,   -- 1 marks a flow or environment bug to fix
  exported_commit    TEXT,                  -- sentinel-config commit of calibration/<hex>.yaml, <hex> the calibration key without sha256: (4.12, SD-36, SD-69)
  valid_from         TEXT NOT NULL, superseded_at TEXT
);
CREATE UNIQUE INDEX ux_floor_current ON calibration_floors(checkpoint_id, region_id, rendering_key_sha256)
  WHERE superseded_at IS NULL;

CREATE TABLE calibration_fixes (            -- the fix list of 3.9: one row per fix item, frozen and masked_live checkpoints
  fix_id         INTEGER PRIMARY KEY,
  floor_id       INTEGER REFERENCES calibration_floors(floor_id),   -- NULL for a failed-capture item (3.9 step 2)
  calibration_run_id TEXT REFERENCES runs(run_id),
  checkpoint_id  TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id      TEXT,                      -- NULL for a failed-capture item
  cause_class    TEXT NOT NULL CHECK (cause_class IN ('caret','hover','tooltip','flash','scroll','sort','live_data',
                   'settle','landmark_jitter','unknown')),   -- the ten classes of 3.9 step 5 (SD-20)
  outcome        TEXT CHECK (outcome IS NULL OR outcome IN ('not_settled','landmark_missing','env_error',
                                                            'harness_error')),   -- a failed capture's outcome (3.9 step 2)
  reason         TEXT,
  owner          TEXT NOT NULL,
  ticket_ref     TEXT,
  opened_at      TEXT NOT NULL,             -- age = now minus opened_at; open 5 business days quarantines the checkpoint (10.5)
  closed_at TEXT, closed_by TEXT, floor_exception_ref TEXT,   -- closed by a fix, or by a human-approved floor_exception (3.9)
  CHECK ((closed_at IS NULL) = (closed_by IS NULL)),
  CHECK (floor_id IS NOT NULL OR outcome IS NOT NULL)   -- a floor item names its floor, a failed-capture item its outcome
);

CREATE TABLE reviewer_evals (               -- one row per evaluation of a model and prompt version (3.14, 10.4)
  eval_id INTEGER PRIMARY KEY,
  eval_set_version TEXT NOT NULL, model_id TEXT NOT NULL, prompt_hash TEXT NOT NULL,
  items INTEGER NOT NULL, metrics_json TEXT NOT NULL,   -- precision and recall per category, tier 1 recall, ECE
  passed_gate INTEGER NOT NULL, run_at TEXT NOT NULL
);

CREATE TABLE soak_samples (                 -- 10.11: one row per process per 60 s sample on the soak rig
  run_id     TEXT NOT NULL REFERENCES runs(run_id),
  sampled_at TEXT NOT NULL,
  pid        INTEGER NOT NULL,
  metric     TEXT NOT NULL,                 -- working_set, private_bytes, gdi_objects, user_objects, handles, threads, cpu, heap_after_gc, clock_tick
  value      REAL NOT NULL,
  PRIMARY KEY (run_id, sampled_at, pid, metric)
) WITHOUT ROWID;
```

Soak (10.11) stores nothing else of its own: its 24 cycle checkpoints are `outcomes` rows with ids `flow/<workspace-surface-id>/soak/c<nn>`, a budget breach is `assertion_failed (assertion: soak.<metric>)` on the cycle checkpoint, a freeze or crash during soak is the app-failure trio of 10.1 (never `env_error`), and the curves the RC report draws come from this table.

Canary states: `detected` when the produced outcome is in the mutant's expected list; `missed` when a non-money mutant was absorbed but its class stayed at or above the rolling floor that night; `detector_degraded` when a non-money miss took its class below the floor; `detector_blind` for any missed money-cell mutant; `generation_failed` when the mutant could not be generated (its synthetic outcome is `harness_error (reason: canary_generation_failed)`). One mutant's `generation_failed` is only its row state; the run's canary state is `generation_failed` only when a critical checkpoint has zero generated mutants or the stage generated none, and otherwise `detector_blind` or `detector_degraded` when any row has that state, else `ok` (10.3). The Nightly run's `summary_json` and the `detector_state` key of `system_state` carry the resulting banner state with its start time, which is what the Reporter stamps on every report until a Nightly run passes. The Nightly run's outcome stays `complete` whatever these rows say: a `detector_blind` or `generation_failed` canary state sets `system_state.detector_state`, pages P1 and puts the banner on every report until the next green canary, and the run is `aborted_harness (reason: canary_generation_failed)` only when no canary could run at all (10.3).

Synthetic outcomes live apart from real ones. A mutant's outcome is a row of `synthetic_outcomes`, keyed (`run_id`, `mutant_id`, `checkpoint_id`) and referenced by its `canary_results` row, because one checkpoint carries many mutants in one Nightly run; `outcomes` holds real rows only: the A/A pass, the canary flows and the run's scoped checkpoints, including the `harness_error` rows a synthetic cause writes on them (`canary_generation_failed`, `canary_path_divergence`) (10.3). The `defect_class` of a `canary_results` row of kind `mutant` is one of the 26 classes of 10.3; for kinds `safety_canary`, `fire_drill` and `path_equivalence` it equals the kind and `mutant_id` names the item (the blind detector, the drill item, the re-evaluated pair), and a CHECK enforces both. The fire drill and the safety canaries of 6.3 to 6.10 are rows with `money_cell = 1`, so a miss is `detector_blind` with the blind detector named in `mutant_id`; a drill item not blocked is in addition the checkpoint outcome `harness_error (reason: drill_failed:<item>)`, and a drill that placed an order is not a canary state but the run outcome `aborted_safety (reason: drill_order_placed)` (6.5). A `path_equivalence` row that misses is `harness_error (reason: canary_path_divergence)` on the re-evaluated pair (10.3).

**Egress, coverage, artifacts, proofs, incidents and system state.**

```sql
CREATE TABLE egress_log (                   -- 6.10: every outbound request
  egress_id         INTEGER PRIMARY KEY,
  run_id            TEXT REFERENCES runs(run_id),   -- NULL for the daily digest and the model check
  shard_id          TEXT, checkpoint_id TEXT,
  sent_at           TEXT NOT NULL,
  destination       TEXT NOT NULL,          -- host name
  purpose           TEXT NOT NULL CHECK (purpose IN ('review','batch_review','agent_step',
                      'explorer_step','pr_comment','jira','slack','digest','preflight','model_check')),
  status            TEXT NOT NULL CHECK (status IN ('sent','refused','failed')),
  response_status   INTEGER,
  artifact_sha256   TEXT,                   -- redacted artifact that left, when one did
  bytes             INTEGER NOT NULL,
  redaction_version TEXT,
  model_id TEXT, request_id TEXT,
  refusal_reason    TEXT,                   -- destination not allowlisted, redaction version missing
  CHECK (status <> 'refused' OR refusal_reason IS NOT NULL)
);

CREATE TABLE outbox (                       -- 9.1: every outbound post is a job before it is a send
  job_id          INTEGER PRIMARY KEY,
  run_id          TEXT REFERENCES runs(run_id),
  kind            TEXT NOT NULL CHECK (kind IN ('pr_comment','commit_status','jira_create','jira_comment',
                                                'jira_transition',
                    'slack','page','digest','manifest_pr','mask_pr')),
  destination     TEXT NOT NULL,
  payload_sha256  TEXT NOT NULL,            -- the payload is an artifact; the row carries its hash
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error      TEXT,
  state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','sent','failed','abandoned')),
  created_at      TEXT NOT NULL, sent_at TEXT,
  closed_by TEXT, closed_at TEXT,           -- a human closes an abandoned job (9.1)
  CHECK ((state = 'sent') = (sent_at IS NOT NULL)),
  CHECK ((closed_by IS NULL) = (closed_at IS NULL)),
  CHECK (closed_by IS NULL OR (state = 'abandoned' AND closed_by NOT IN ('reviewer','system','orchestrator',
                                                                         'reporter')))
);

CREATE TABLE coverage_cells (               -- merge, RC and nightly runs only; PR runs keep the matrix in runs.summary_json
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  surface_id       TEXT NOT NULL,
  persona          TEXT NOT NULL,
  entitlement      TEXT NOT NULL,
  tier             INTEGER NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('covered','not_run','not_registered')),
  not_run_reason   TEXT CHECK (not_run_reason IN ('entitlement','budget','scope','quarantine','base_step_failed',
                                                  'app_crash',
                                                  'model_unavailable') OR not_run_reason LIKE 'step_failed:%'
                     OR not_run_reason LIKE 'blocked:%'),   -- 2.12; blocked:<outcome> names the outcome that blocked every checkpoint of the cell
  worst_outcome    TEXT,                    -- covered cells: the worst usable outcome
  checkpoint_count INTEGER NOT NULL DEFAULT 0,
  profile_id       TEXT,                    -- the login entitlement profile the cell's column names (2.12)
  PRIMARY KEY (run_id, surface_id, persona, entitlement),
  CHECK ((state = 'not_run') = (not_run_reason IS NOT NULL)),
  CHECK ((state = 'covered') = (worst_outcome IS NOT NULL))
);

CREATE TRIGGER trg_coverage_run_type BEFORE INSERT ON coverage_cells
WHEN (SELECT run_type FROM runs r WHERE r.run_id = NEW.run_id) NOT IN ('merge','rc','nightly')
BEGIN SELECT RAISE(ABORT, 'coverage_cells rows exist only for merge, RC and nightly runs'); END;

CREATE TABLE artifacts (                    -- index of the artifact store; a row exists only for verified bytes on disk
  sha256            TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,          -- capture, crop, heatmap, diff_mask, ocr_json, bounds_json, csv_export,
                                            -- agent_frame, video, crash, log_slice, attestation, gate, request,
                                            -- manifest, ledger, journal, mutant
  media_type        TEXT NOT NULL,
  bytes             INTEGER NOT NULL,
  width INTEGER, height INTEGER,
  redaction_passed  INTEGER NOT NULL CHECK (redaction_passed = 1),   -- images: redaction stage; text: plaintext scan
  redaction_version TEXT,
  set_path          TEXT NOT NULL,          -- runs/<run_id>, baselines/<lineage>/<build_id>, eval/<version>
  first_run_id      TEXT,
  created_at        TEXT NOT NULL,
  pinned            INTEGER NOT NULL DEFAULT 0,   -- referenced by a baseline or an unclosed finding
  audit_state       TEXT NOT NULL DEFAULT 'ok' CHECK (audit_state IN ('ok','missing','corrupt')),
  last_audit_at     TEXT,
  deleted_at        TEXT
);

CREATE TABLE artifact_sets (                -- one row per copy of an object in a set; retention, backup and audit act per set (SD-82)
  sha256            TEXT NOT NULL REFERENCES artifacts(sha256),
  set_path          TEXT NOT NULL,          -- runs/<run_id>, baselines/<lineage>/<build_id>, eval/<version>
  created_at        TEXT NOT NULL,
  deleted_at        TEXT,
  quarantined_at    TEXT,                   -- a secret-scan hit quarantined the set (6.9)
  PRIMARY KEY (sha256, set_path)
) WITHOUT ROWID;

CREATE TABLE post_run_proofs (              -- 6.6: ORDER SAFETY PROOF, one per shard; a run with no shard has one run-level row
  proof_id          INTEGER PRIMARY KEY,
  shard_id          TEXT UNIQUE REFERENCES shards(shard_id),   -- NULL: the run-level row of a run that started no session (SD-61)
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  login_alias       TEXT REFERENCES logins(login_alias),
  verdict           TEXT NOT NULL CHECK (verdict IN ('proven_no_sends','proven_declared_sends','proven_no_session',
                                                     'unproven','orders_sent')),   -- proven_no_session: no token issued, read from the gateway log; orders_sent: a clean read found an undeclared row (6.6)
  reason            TEXT,
  send_policy_ref   TEXT NOT NULL,          -- risk owner decision id from policy/send_policy.yaml
  counts_json       TEXT NOT NULL,          -- working, filled, canceled, position rows: start, end, new, expected; net liq start, end, drift
  cancel_all        TEXT NOT NULL CHECK (cancel_all IN ('not_needed','performed_verified','performed_unverified')),
  paper_mode_assertions INTEGER NOT NULL, paper_mode_mismatches INTEGER NOT NULL,
  gateway_events INTEGER NOT NULL, gateway_drops INTEGER NOT NULL,
  unreadable_count  INTEGER NOT NULL DEFAULT 0,   -- unreadable identity or Monitor reads in the session (6.6)
  refusals_count    INTEGER NOT NULL DEFAULT 0,   -- slow-half refusals (6.6 gateway line)
  attestation_ref   TEXT,                   -- the attestation screenshots artifact (6.3)
  start_snapshot_sha256 TEXT, end_snapshot_sha256 TEXT,
  produced_at       TEXT NOT NULL,
  verified_by TEXT, verified_at TEXT,       -- human clearance of an unproven verdict, set once
  CHECK (verdict <> 'unproven' OR reason IS NOT NULL),
  CHECK (shard_id IS NULL OR login_alias IS NOT NULL),
  CHECK (verified_by IS NULL OR verified_by NOT IN ('reviewer','system','orchestrator','reporter'))
);

CREATE UNIQUE INDEX ux_proof_run_level ON post_run_proofs(run_id) WHERE shard_id IS NULL;

CREATE TRIGGER trg_proof_clearance BEFORE UPDATE ON post_run_proofs
WHEN OLD.verified_by IS NOT NULL OR OLD.verdict <> 'unproven'
  OR NEW.proof_id IS NOT OLD.proof_id OR NEW.shard_id IS NOT OLD.shard_id OR NEW.run_id IS NOT OLD.run_id
  OR NEW.login_alias IS NOT OLD.login_alias OR NEW.verdict IS NOT OLD.verdict OR NEW.reason IS NOT OLD.reason
  OR NEW.send_policy_ref IS NOT OLD.send_policy_ref OR NEW.counts_json IS NOT OLD.counts_json
  OR NEW.cancel_all IS NOT OLD.cancel_all OR NEW.paper_mode_assertions IS NOT OLD.paper_mode_assertions
  OR NEW.paper_mode_mismatches IS NOT OLD.paper_mode_mismatches OR NEW.gateway_events IS NOT OLD.gateway_events
  OR NEW.gateway_drops IS NOT OLD.gateway_drops OR NEW.unreadable_count IS NOT OLD.unreadable_count
  OR NEW.refusals_count IS NOT OLD.refusals_count OR NEW.attestation_ref IS NOT OLD.attestation_ref
  OR NEW.start_snapshot_sha256 IS NOT OLD.start_snapshot_sha256 OR NEW.end_snapshot_sha256 IS NOT OLD.end_snapshot_sha256
  OR NEW.produced_at IS NOT OLD.produced_at
BEGIN SELECT RAISE(ABORT, 'a proof is written once; only its clearance (verified_by, verified_at) is set, once (6.6)'); END;

CREATE TABLE incidents (                    -- 6.12: S1 to S3 rows, linked from the report
  incident_id   INTEGER PRIMARY KEY,
  run_id        TEXT REFERENCES runs(run_id),
  rig_id        TEXT, login_alias TEXT,
  severity      TEXT NOT NULL CHECK (severity IN ('S1','S2','S3')),
  trigger       TEXT NOT NULL,
  opened_at     TEXT NOT NULL,
  closed_at TEXT, closed_by TEXT, close_reason TEXT, ticket_ref TEXT, postmortem_ref TEXT,
  postmortem_due_at TEXT,                   -- S1 and S2: five business days after opened_at (6.12)
  CHECK ((closed_at IS NULL) = (closed_by IS NULL)),
  CHECK (closed_by IS NULL OR closed_by NOT IN ('reviewer','system','orchestrator','reporter'))
);

CREATE TABLE safety_quarantines (           -- 6.5, 6.12, 10.5: a flow, rig, login or activity taken out of service
  quarantine_id     INTEGER PRIMARY KEY,
  target_kind       TEXT NOT NULL CHECK (target_kind IN ('flow','rig','login','activity')),
  target            TEXT NOT NULL,
  reason            TEXT NOT NULL CHECK (reason IN ('flake','aa_drift','aa_floor','maintenance_budget','fire_drill',
                      'safety_canary','kill_switch','gateway_drops','fallback_rate')),   -- 10.5 (SD-96)
  run_id            TEXT REFERENCES runs(run_id),
  detail            TEXT,
  ticket_ref        TEXT,
  opened_at         TEXT NOT NULL,
  reenabled_by TEXT, reenabled_at TEXT, reenable_reason TEXT,   -- human-only, with reason and ticket (6.12)
  CHECK ((reenabled_by IS NULL) = (reenabled_at IS NULL)),
  CHECK (reenabled_by IS NULL OR (reenable_reason IS NOT NULL AND ticket_ref IS NOT NULL
                                  AND reenabled_by NOT IN ('reviewer','system','orchestrator','reporter')))
);

CREATE TABLE model_usage (                  -- 11.4: the usage block of every model response, per component
  usage_id          INTEGER PRIMARY KEY,
  run_id            TEXT REFERENCES runs(run_id),   -- NULL for the model check and digests
  component         TEXT NOT NULL CHECK (component IN ('reviewer','agent_rescue','explorer')),
  model_id          TEXT NOT NULL,
  request_id        TEXT,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  at                TEXT NOT NULL
);

CREATE TABLE escapes (                      -- 10.9 G+1, 9.11: a production bug and the checkpoint that should have shown it
  escape_id         INTEGER PRIMARY KEY,
  release           TEXT NOT NULL,          -- the released build
  ticket_ref        TEXT NOT NULL,
  escape_class      TEXT NOT NULL CHECK (escape_class IN ('covered_missed','not_run','not_registered',
                                                          'not_detectable')),
  checkpoint_id     TEXT REFERENCES checkpoints(checkpoint_id),
  surface_id        TEXT,
  tier              INTEGER CHECK (tier IN (1,2,3)),
  entered_by        TEXT NOT NULL CHECK (entered_by NOT IN ('reviewer','system','orchestrator','reporter')),
  entered_at        TEXT NOT NULL,
  UNIQUE (release, ticket_ref),
  CHECK ((escape_class = 'not_registered') = (checkpoint_id IS NULL))
);

CREATE TABLE maintenance_items (            -- 11.5: flow maintenance, its owner and the hours logged
  item_id           INTEGER PRIMARY KEY,
  kind              TEXT NOT NULL,          -- update_scripted_flow, maintenance_task, anchor_patch, ...
  ref               TEXT NOT NULL,          -- flow, step or anchor template id
  owner             TEXT NOT NULL,
  run_id            TEXT REFERENCES runs(run_id),
  ticket_ref        TEXT,
  detail            TEXT,
  hours             REAL NOT NULL DEFAULT 0 CHECK (hours >= 0),
  opened_at         TEXT NOT NULL,
  closed_at TEXT, closed_by TEXT,
  CHECK ((closed_at IS NULL) = (closed_by IS NULL))
);

CREATE TABLE triage_assignments (           -- 9.12: who holds a finding and its SLA clock
  assignment_id     INTEGER PRIMARY KEY,
  finding_id        INTEGER NOT NULL REFERENCES findings(finding_id),
  assignee          TEXT NOT NULL,
  assigned_by       TEXT NOT NULL,          -- a person, or system for the escalation's assignment to the rotation
  assigned_at       TEXT NOT NULL,
  claimed_at        TEXT,
  released_at       TEXT,
  escalation_step   INTEGER NOT NULL DEFAULT 0 CHECK (escalation_step BETWEEN 0 AND 4),   -- the 9.12 steps reached
  sla_due_at        TEXT
);

CREATE UNIQUE INDEX ux_assignment_current ON triage_assignments(finding_id) WHERE released_at IS NULL;

CREATE TABLE proposals (                    -- 9.6 two-person rule: a committer's proposal waits for a second human
  proposal_id       INTEGER PRIMARY KEY,
  finding_id        INTEGER NOT NULL REFERENCES findings(finding_id),
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  action            TEXT NOT NULL CHECK (action IN ('confirm_bug','mark_intended','request_mask','noise_signature',
                                                    'agent_mistake','expected_behavior','approve_baseline',
                                                    'reject_baseline','fixed_in_build')),
  proposed_by       TEXT NOT NULL CHECK (proposed_by NOT IN ('reviewer','system','orchestrator','reporter')),
  proposed_at       TEXT NOT NULL,
  ticket_ref        TEXT,
  reason            TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed','confirmed','declined')),
  decided_by        TEXT,
  decided_at        TEXT,
  decline_reason    TEXT,
  decision_id       INTEGER REFERENCES decisions(decision_id),
  CHECK ((state = 'proposed') = (decided_by IS NULL)),
  CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
  CHECK (decided_by IS NULL OR (decided_by <> proposed_by AND decided_by NOT IN ('reviewer','system','orchestrator',
                                                                                 'reporter'))),
  CHECK (state <> 'declined' OR decline_reason IS NOT NULL),
  CHECK (state <> 'confirmed' OR decision_id IS NOT NULL)
);

CREATE UNIQUE INDEX ux_proposal_open ON proposals(finding_id) WHERE state = 'proposed';

CREATE TABLE system_state (                 -- keys: last_backup_at, last_backup_error, last_integrity_check_at,
  key TEXT PRIMARY KEY,                     --   last_restore_drill_at, last_artifact_audit_at, kill_switch_state,
  value TEXT NOT NULL,                      --   detector_state, canaries_pending, nightly_skipped_for, reviewer_disabled,
  updated_at TEXT NOT NULL                  --   harness_freeze, schema_version, schema_sha256, free_bytes_at_last_preflight
);
```

**Indexes.** Run health counts (env errors, harness errors, not settled, quarantined, agent fallbacks, gateway drops, store failures) are computed by the Reporter over these indexes and stored in the run's summary JSON.

```sql
CREATE INDEX ix_runs_type_time   ON runs(run_type, started_at);
CREATE INDEX ix_runs_pair        ON runs(pair_id);
CREATE INDEX ix_shards_run       ON shards(run_id, state);
CREATE INDEX ix_outcomes_cp_run  ON outcomes(checkpoint_id, run_id);      -- history per checkpoint
CREATE INDEX ix_outcomes_run_out ON outcomes(run_id, outcome, reason);
CREATE INDEX ix_outcomes_pending ON outcomes(run_id) WHERE review_state = 'pending';
CREATE INDEX ix_steps_run_seq    ON step_events(run_id, shard_id, seq);
CREATE INDEX ix_steps_notable    ON step_events(run_id, step_outcome)
  WHERE step_outcome IN ('passed_after_retry','passed_via_agent_fallback','failed','blocked_by_gateway');
CREATE INDEX ix_steps_dropped    ON step_events(run_id) WHERE gate_decision IN ('dropped','refused');
CREATE INDEX ix_findings_open    ON findings(triage_state, tier, created_at) WHERE triage_state = 'open';
CREATE INDEX ix_findings_run     ON findings(run_id, kind);
CREATE INDEX ix_decisions_sig    ON decisions(checkpoint_id, region_id, category) WHERE revoked_at IS NULL;
CREATE INDEX ix_reviews_run      ON reviews(run_id);
CREATE INDEX ix_reviews_model    ON reviews(model_id, prompt_hash);
CREATE INDEX ix_baselines_cp     ON baselines(checkpoint_id, lineage, state);
CREATE INDEX ix_regions_commit   ON regions(config_commit, checkpoint_id);
CREATE INDEX ix_masks_commit     ON masks(config_commit, checkpoint_id);
CREATE INDEX ix_flake_events_cp  ON flake_events(checkpoint_id, created_at);
CREATE INDEX ix_canary_run       ON canary_results(run_id, money_cell, state);
CREATE INDEX ix_egress_run       ON egress_log(run_id, sent_at);
CREATE INDEX ix_artifacts_sweep  ON artifacts(set_path, pinned, created_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_incidents_open   ON incidents(rig_id) WHERE closed_at IS NULL;
CREATE INDEX ix_outbox_due       ON outbox(state, next_attempt_at) WHERE state IN ('queued','failed');
CREATE INDEX ix_soak_run         ON soak_samples(run_id, metric, sampled_at);

CREATE INDEX ix_extractions_cp   ON oracle_extractions(run_id, checkpoint_id, side);

CREATE INDEX ix_synthetic_run    ON synthetic_outcomes(run_id, mutant_id);

CREATE INDEX ix_artifact_sets    ON artifact_sets(set_path, created_at) WHERE deleted_at IS NULL;

CREATE INDEX ix_usage_time       ON model_usage(component, at);

CREATE INDEX ix_quarantine_open  ON safety_quarantines(target_kind, target) WHERE reenabled_at IS NULL;

CREATE INDEX ix_maintenance_open ON maintenance_items(owner, opened_at) WHERE closed_at IS NULL;
```

### 8.4 Artifact layout on disk

Every file is stored under its SHA-256 within a set, and every set has a manifest mapping human names to hashes. Hashing is for integrity and linking, not for space: screenshots of a trading UI are almost never byte-identical across runs (quotes, clocks and anti-aliased text change bytes even inside masked regions), so 8.8 assumes no cross-run deduplication.

```
RIG   C:\sentinel\run\<shard_id>\
        run.db, run.db-wal, run.db-shm   Run Store
        writer.lock                      PID of the single writer
        heartbeat.json                   10.2
        results.jsonl                    append-only ledger of every database row of this shard
        gateway_drops.jsonl              drop-log drainer's journal of the hook host's ring (5.4.2)
        watchdog_events.jsonl            Watchdog journal
        manifest.json                    names -> hashes for this shard
        objects\<aa>\<sha256>.<ext>      aa = first two hex digits of the hash

CONTROL HOST   D:\sentinel\artifacts\
  incoming\<shard_id>\            upload staging; moved into runs\ after hash and ledger verification
  runs\<run_id>\
    manifest.json                 union of the shards' manifests
    shards\<n>\results.jsonl      the ledgers and journals, kept verbatim
    objects\<aa>\<sha256>.<ext>
  baselines\<lineage>\<build_id>\
    manifest.json                 the approved set for this build in this lineage
    objects\<aa>\<sha256>.<ext>
  eval\<eval_set_version>\        manifest.json and objects, same shape
  quarantine\<run_id>\            objects moved here by the plaintext scan; security clears them
```

Manifest entries carry `name` (for example `flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07/candidate/capture.png`), `sha256`, `kind`, `bytes`, `media_type`, `checkpoint_id`, `side`, `redaction_version` and `captured_at`; the header carries the run or build id, the shard, the config commit, the harness commit and `created_at`. A name may map to a hash present in another set; the object is written again under this set because sets are the unit of retention and backup. `artifacts` holds one row per hash, and `artifact_sets` (8.3) one row per copy, keyed (`sha256`, `set_path`) with its own `created_at`, `deleted_at` and `quarantined_at`, so retention, backup and the weekly audit (8.6) work per set: deleting or quarantining one set's copy never touches another set's.

Write path on the rig. Capture runs the redaction stage, hashes the redacted bytes, writes them to a temporary name under `objects`, flushes, verifies the hash by reading back, and renames into place; only then does the Orchestrator queue the batch that names the hash. A write that fails (disk full, I/O error) is retried once; a second failure ends the checkpoint `harness_error (reason: artifact_persist_failed)` and the run can no longer be `complete` (8.9). A failure with `ENOSPC` is the same checkpoint outcome and also makes the Orchestrator request the Watchdog's immediate free-space check; only that check, the Watchdog's free-space check, ends the run, as `aborted_harness (reason: disk_full)` with every remaining checkpoint `harness_error (reason: disk_full)` (10.2, 8.9). An artifact without a redaction version is refused by the writer, so no path exists by which an unredacted image reaches a stored object on either host. Text artifacts (crash files, log slices, exports, reviewer requests, ledgers, journals) pass the post-run plaintext scan instead (6.9 Credentials and secret steps); a hit moves the shard's objects to `quarantine`, sets `quarantined_at` on their `artifact_sets` rows, and the run ends `aborted_harness (reason: secret_leaked)` with the report withheld.

Write path on the control host. The import streams each object to `incoming`, recomputes the hash, rejects a mismatch, flushes, renames into `objects` and inserts the `artifacts` row (when the hash is new) and the `artifact_sets` row for this set inside the shard's import transaction. A mismatch is re-requested from the rig up to three times; a persisting mismatch marks the shard `import_failed` with reason `upload_hash_mismatch`, the run `aborted_harness (reason: artifact_persist_failed)`, and the rig keeps its copy until a human has looked (8.9).

Kinds and sources: capture (Capture, one per checkpoint per side); crop, heatmap and diff mask (Diff Engine, above threshold only); OCR and bounds sidecars (Oracle, Invariant Checker); CSV export (Oracle); agent frame (Executor in agent mode and the Explorer, JPEG quality 80 at half resolution); video (Capture at 3 frames per second: full length for RC runs and the nightly fire drill, a 30 second ring buffer around failures otherwise); crash (Watchdog: `hs_err_pid*.log`, WER dumps, thread dumps); log slice (Watchdog); attestation and gate screenshots (Orchestrator, every gate pass or fail); request (Reviewer input JSON); ledger and journal (Orchestrator, hook host, Watchdog); mutant (Canary Service).

### 8.5 What lives in git and how a run pins it

The `sentinel-config` repository holds everything that tells a run what to do; its layout and lint are owned by 4.1 Conventions, repository layout and the lint, and the paths below are that layout plus the files this section adds. Baseline images do not live in git; a manifest naming them does. The harness, its generated JSON Schemas (`schemas/` of the `sentinel` repository, 4.1) and `sentinel lint` live in the `sentinel` repository.

```
taxonomy.yaml                                  every closed vocabulary of 4.1: outcome enums, reasons, gates, finding kinds, triage states, decision actions and verdicts, canary states and kinds, reviewer verdicts, quarantine reasons, money roles, popup actions; the source of every CHECK and lookup row
builds/*.yaml                                  build identity and build pairs
manifests/<build_id>.yaml, manifests/mocks/    change manifests
registry/surfaces/<area>/*.yaml                Surface Registry
registry/dependency-map.yaml                   primitive -> surfaces
registry/ladders.yaml                          size-sweep ladders S and F
corpus/workspaces/<id>/                        Workspace Corpus entries and workspace files
corpus/measured_sizes.yaml                     per-workspace gadget sizes from probe P8, the Ladder F corpus sizes; written by the Reporter's registry PR, owned by the registry owner (2.5.2)
corpus/odd-inputs.yaml                         the versioned odd-input corpus with its sampling seed, referenced by type_fixture steps (4.1)
flows/<tier>/*.yaml, flows/RENAMES.yaml        flows; checkpoint rename map
flows/quarantine/<finding-id>.yaml             Explorer candidate flows awaiting promotion (2.9)
regions/<checkpoint_id>.yaml                   Region Registry: freeze, rule, mask, diff, redaction regions and landmarks
regions/landmarks/<landmark_id>.png            landmark templates
redaction/anchors.yaml                         the one global redaction list in regions-file shape: landmarks plus regions of type redaction (6.10)
palettes/<theme>.yaml                          INV-15 sign colors per app theme, owned by the registry owner (2.7)
masks/<checkpoint_id>/*.yaml                   mask records
fixtures/*.yaml, profiles/*.yaml               account and data fixtures; pinned settings profiles
fixtures/margin_rules.yaml                     the per-asset-class buying power brackets of rule R02, pinned in Phase 0 (2.8.3, 4.9)
popups/catalog.yaml                            popup catalog
canaries/mutants.yaml                          the one mutant catalog: M01 to M33 as 12.5 tables them, then M34 on in sequence, classes of 10.3, money_cell flag, seed 20260101 (12.5, 10.3)
gateway/allowlist.yaml                         exact paper account ids per rig, hashed into the fingerprint (6.4)
gateway/default_order_hotkeys.yaml             default order-action chords of the pinned build, read once in Phase 0 (6.2)
gateway/dismiss_allowlist.yaml                 the dismiss allowlist of 6.2
rig/process_allowlist.yaml                     the running-process allowlist of 7.1 (4.16)
rig/thinkorswim.vmoptions.template             the vmoptions template the Rig Manager writes thinkorswim.vmoptions from; B3 and H10 compare its hash (7.3, L-XREF-28)
configs/allowed.yaml                           display configurations, each its own fingerprint, with the work_area that gives Ladder F's MAXW and MAXH (7.1, 2.5.2)
diff_engine.yaml                               Diff Engine parameters (3.7)
soak.yaml                                      soak budgets (10.11)
schedule/windows.yaml                          operating day, named quiet windows, maintenance windows and holiday rules (7.10)
ops/oncall.yaml                                role holders and deputies, the triage roster and the triage and harness on-call rotations (6.12, 9.12, 11.6)
ops/credential_rotation.yaml                   credential rotation calendar (7.7)
model_deprecations.yaml                        vendor deprecation notices the system owner records (10.7)
policy/budgets.yaml                            dollar caps per Explorer session, per run and per week, and reviewer call caps (11.4)
policy/explorer_budget.yaml                    Explorer session budgets; a change needs the Explorer owner's approval (6.8)
policy/release_gate.yaml                       N business days before GA per release train, default 3, with GA and RC cut dates per train (11.2, 10.9)
policy/explorer_deny_list.yaml                 the Explorer deny list (6.8)
policy/egress_allowlist.yaml                   hosts that artifacts and messages may leave for (6.10)
policy/computer_use_ruling.yaml                the DR-07 computer-use ruling, read and validated at preflight G1 (6.11)
policy/reporting.yaml                          the Reporter's settings: commit status mode history, Jira keys, Slack channels, paging, auto-filing switches (9.9)
calibration/<hex>.yaml                         per-region A/A floors and thresholds for one calibration key, exported by the Calibration Service from calibration_floors; <hex> is the calibration key of 4.12 without its sha256: prefix (4.1, 4.16)
quarantine.yaml                                quarantine records: exactly one of checkpoint or flow, owner, ticket, entered, reason from the 10.5 table
policy/send_policy.yaml                        the OPEN paper_send_ok switch, signed by the risk owner
phase0/branches.yaml                           the chosen branch for each unknown, plus the Phase 0 reads other files depend on (4.16)
eval/<eval_set_version>/labels.jsonl           reviewer eval labels; images live in the artifact store
eval/reports/<hex>.yaml                        one eval report per reviewer version tuple of 3.15, <hex> the tuple hash without its prefix (4.16)
baselines/<lineage>/<build_id>/manifest.json   committed by the sign-off bot at RC sign-off
authorization/<version>.md                     the signed authorization of 6.12, referenced by every run
authorization/<version>.yaml                   the machine-readable form of that authorization (6.12)
CODEOWNERS                                     routes flows/ and registry/ to the tier's flow owner and each file of 4.16 to the owner its 4.1 row names (11.5, L-XREF-26)
```

Pinning. At run creation the Orchestrator on the control host side of the Scheduler resolves one commit of `sentinel-config` (HEAD of main, or the commit a config smoke lane names), records it as the run's config commit, builds a tarball of that tree, records its hash, and the Rig Manager ships the same tarball to every shard. Alongside it goes a read-only snapshot of the Central Store's decisions, baselines, regions, masks, calibration floors and quarantine records as of run creation, hashed into `snapshot_set_sha256`, so every shard applies the same inheritance and the run can name the decision set it used. Both sides of a build pair run the same registry, flows, masks, corpus and catalogs, and no run reads a moving branch. The workspace hash in the environment fingerprint is computed from the corpus files in that tarball, so a corpus change produces a new fingerprint and invalidates calibration (7.13 What invalidates calibration). The `regions` and `masks` tables are loaded from the same commit, which is how the Reporter shows mask growth by commit. Harness code is pinned separately as the run's harness commit, installed by the Rig Manager at snapshot build. Run health prints both commits, the tarball hash and the snapshot hash.

Change control. Every pull request to `sentinel-config` runs `sentinel lint` (4.15 Cross-file rules and the lint pipeline): schema validation, checkpoint ids unique across the tree and matching the grammar of 8.3, a rename map whenever an id disappears, a landmark template for every region, masks inside the checkpoint window and under the per-panel coverage budget, every mask with owner, reason, expiry and a human author, `taxonomy.yaml` equal to the taxonomy table of 1.10 and the appendix copy. Masks are created only through this path: the report UI's request mask action opens a pull request with the record prefilled and the requester as author; the Reviewer may only propose, and a proposal is a finding, not a mask. Quarantine records and Explorer candidate flows are opened as pull requests by the Orchestrator and merged by humans. The Nightly run reports orphaned baselines (an approved baseline whose id no longer exists at the current commit and has no rename) as findings and never aborts over them.

Baseline manifests. The Central Store is the live system of record for baseline state. At RC sign-off the sign-off bot exports the approved set for the lineage to `baselines/<lineage>/<build_id>/manifest.json` and commits it, so the released configuration and its approved images are named by one git commit, which the next release's RC run records (gap G32).

### 8.6 Retention and backup

Retention is per artifact class and per set and never deletes a pinned object. The sweep runs nightly after the backup on the control host, sets `deleted_at` on the set's `artifact_sets` row before unlinking that set's copy (8.4), and refuses to remove anything referenced by a baseline row or by a finding that pins its evidence. A finding pins its evidence while it is `open`, `confirmed_bug` or `fixed_pending_verify`, or `intended` with no approved baseline yet; once it is `closed`, `noise`, `agent_mistake` or `expected_behavior` (or `intended` with its baseline approved) its objects are released 180 days after `closed_at`, the time the finding left the pinning states.

| Class | Keep |
|---|---|
| Central Store rows other than step_events | indefinitely |
| step_events rows | 90 days; indefinitely for runs with an open finding, any `blocked_by_gateway` row, an incident or an `aborted_safety` outcome |
| Run Store, ledgers and objects on the rig | until the shard is `imported` plus 7 days, and in any case until the next snapshot revert; a shard that is `import_failed` or `lost` is kept on the rig until a human releases it |
| Ledgers and journals in `runs\<run_id>\shards\` | life of the run's artifact set |
| Baseline images, every state | never deleted |
| Artifacts referenced by a finding that is `open`, `confirmed_bug`, `fixed_pending_verify`, or `intended` without an approved baseline | pinned while the finding is in one of those states; released 180 days after `closed_at` once it leaves them (`closed`, `noise`, `agent_mistake`, `expected_behavior`) |
| Full checkpoint sets of the last 3 `complete` merge runs on main and the last 2 RC runs per release | until superseded |
| PR run artifacts | 14 days after the run, or 7 days after the PR merges or closes, whichever is earlier |
| Merge run artifacts not pinned above | 14 days |
| Nightly run artifacts (A/A pairs, fire drill video, mutants) | 7 days; mutants regenerate deterministically from the seed |
| Calibration run captures | until the next calibration completes plus 30 days |
| Agent and Explorer frames, run video | 14 days unless referenced by a finding |
| Crash artifacts and log slices | 180 days, pinned while a finding references them |
| Reviewer request payloads | 30 days, or the life of the eval item when promoted into the eval set |
| Quarantined objects | until security clears them in writing |

Backup. At 02:00 America/New_York a job on the control host runs `VACUUM INTO` for a consistent snapshot while the service runs, exports decisions, baselines, masks, flake statistics, incidents and quarantine records as JSONL, and mirrors the snapshot, the exports, all of `baselines\` and every pinned object under `runs\` to the backup location with robocopy. Snapshots are kept 7 daily and 8 weekly. On success it writes `last_backup_at` to system state; on any failure it writes `last_backup_error` and raises the 9.10 Store health alert (P2, posted at 06:00 under the after-hours rule of 11.6), and a `last_backup_at` older than 36 hours raises the same alert from the Nightly run's state check as a second line of defence. The retention sweep is skipped on any night whose backup failed, and run health prints "retention skipped: backup failed". Weekly, `PRAGMA integrity_check` runs on the snapshot and records `last_integrity_check_at`; a failure on the snapshot is a service stop and restore (8.9), not a warning.

Artifact audit. Weekly, the control host re-hashes every pinned object and a 5 percent sample of the rest, set by set (each `artifact_sets` copy is checked on its own), writes `audit_state` and `last_audit_at`, and opens an `artifact_missing` finding for any object that is missing or whose hash no longer matches; a pinned object that fails is restored from the backup mirror and the restore is logged.

Restore drill, quarterly, owned by the system owner: restore the latest snapshot and `baselines\` to a scratch host, start the report UI against it, re-diff ten checkpoints of one RC run against the restored baselines, and confirm the outcomes match the original rows. The date goes into `last_restore_drill_at`; older than 100 days is a red row in Run health.

Storage preflight. The `provisioned` gate requires free space on the rig above the larger of 20 GB (R7 in 12.7) and twice the projected shard size, and on the control host above the larger of 100 GB and twice the projected run size (scope count times two sides times the mean capture size from the last calibration run); the failure outcome is in 8.9. Schema migrations run only after a successful `VACUUM INTO` of the pre-migration file.

### 8.7 Report UI data access

The report UI is the FastAPI application of 5.13 Reporter and Integrations (technology in 5.18 Technology choices), on the control host behind SSO; page contents are defined in 9.5 Report UI pages.

Reads open the Central Store with `mode=ro` and `PRAGMA query_only = 1` from a small pool, one short transaction per request. Pages follow the indexes in 8.3: run page (outcomes by run joined to checkpoints and findings, ordered by tier then outcome, with the shard list and import state), checkpoint history, finding page (finding, review and the live decision lookup from 8.3, so the page shows what a new diff would inherit), coverage matrix (coverage cells by run; for a PR run, the matrix from the run's summary JSON), trends (flake stats, canary results by night and defect class, calibration floors by rendering key (7.13), egress by day, mask area by commit), safety (post-run proofs, incidents, blocked logins). Lists paginate at 200 rows. The run page reads the run's summary JSON first and expands to rows on demand; the Reporter renders the PR comment from that summary, written when the last shard imports, which keeps it inside the 45 minute PR run budget.

Artifacts are served at `/a/<sha256>` with the media type from the `artifacts` table and immutable cache headers. A hash whose every `artifact_sets` copy has `deleted_at` set returns a page saying what the object was and when retention removed it, never a broken image; a hash with `audit_state` other than `ok`, or a hash an outcome names that has no `artifacts` row, returns a page saying the evidence is missing and names the finding that records it. PR comments and Slack messages link to these URLs; images are never uploaded to the PR host, Jira or Slack, so only text leaves for those destinations, logged as such in the egress log.

Writes (confirm bug, mark intended with ticket, mark noise, request mask, approve or reject a baseline, revoke a decision, quarantine or un-quarantine, Explorer disposition, clear an unproven proof, close an incident, override a check) post to the Reporter service's local API with the SSO identity of the person acting. The service validates against the constraints in 8.3, writes through the single writer, appends to the day's decisions ledger and returns the row id. `decided_by`, `approved_by`, `quarantined_by`, `retired_by`, `verified_by`, `closed_by` and `check_override_by` always hold a person; service accounts are rejected by the API and by the CHECK constraints. Request mask, quarantine and un-quarantine write no table row of their own; they open the pull requests of 8.5, and the mirror tables follow the merged commit. Metrics jobs (9.11 Metrics dashboard and kill criteria) read the same path and write results into the Nightly run's summary JSON through the writer.

### 8.8 Growth estimates

Planning figures. Checkpoint counts and run frequency come from 11.1 Planning assumptions; the byte sizes below are this section's and are measured in Phase 0: the calibration run records the real mean capture size, and the storage preflight uses that figure, not this table.

| Id | Assumption | Value |
|---|---|---|
| SA-01 | Mean 1920x1080 PNG capture of thinkorswim at 100 percent DPI | 1.5 MB (expected 0.8 to 2.5) |
| SA-02 | Sidecars per capture (OCR boxes, bounds, redaction map) | 0.1 MB |
| SA-03 | Diff products when a checkpoint exceeds its floor (heatmap, two crops, diff mask) | 0.8 MB on 5 percent of checkpoints in a typical merge run, 20 percent on a large UI change |
| SA-04 | Checkpoints per run, a ceiling over 11.1 and 11.2 (PA-13 gives a PR census of about 80 checkpoints, the smoke subset being capped at 40 surfaces by 2.11; the 250 here adds flows and Ladder S sweeps with margin) | PR 250; merge 1,380 (census 900 per 2.4 and PA-02, flows, Ladder S sweeps and splitters 225); RC 2,900 (census 900, tier B census 600, all flows, Ladder F sweeps, soak) |
| SA-05 | Frequency, from 11.1 PA-10, taken as a ceiling | 10 PR runs and 4 merge runs per business day; 1 Nightly run per day; 3 RC runs and 1 Calibration run per month |
| SA-06 | Explorer 1,500 frames at 0.15 MB; run video 0.5 GB per side per 8 hours | as listed |
| SA-07 | Step events 12 rows per checkpoint per side at 350 bytes with indexes; Nightly A/A scope of 10.6 about 550 checkpoints captured twice (11.2) | as listed |

| Run type | Checkpoints | Captures and sidecars | Diff products | Other | Total |
|---|---|---|---|---|---|
| PR run | 250 | 250 x 2 x 1.6 MB = 0.8 GB | 0.1 GB | ring buffer video 0.05 GB | 0.95 GB |
| Merge run | 1,380 | 1,380 x 2 x 1.6 MB = 4.4 GB | 0.1 GB | 0.05 GB | about 4.5 GB |
| RC run | 2,900 | 2,900 x 2 x 1.6 MB = 9.3 GB | 0.5 GB | Explorer 0.2 GB, video 1 GB | about 11 GB |
| Nightly run | 550 A/A pairs plus 750 mutants | 1,100 x 1.6 MB = 1.8 GB | 750 x 1.5 MB = 1.1 GB of mutant images | fire drill video 0.1 GB | about 3 GB |
| Calibration run | 1,380 x 6 passes | 8,280 x 1.6 MB = 13 GB | 0 | 0 | about 13 GB |

Raw growth per business day: 10 x 0.95 + 4 x 4.5 + 3 = about 31 GB, about 0.7 TB per month before retention. Steady state under 8.6 (14 calendar days taken as 10 business days): PR runs 95 GB, merge runs 180 GB, pinned merge sets 14 GB, RC runs 35 GB, nightly 21 GB, calibration 26 GB, artifacts pinned by findings 1 GB, about 370 GB hot. Baselines only grow: an approved set is 2,900 x 1.6 MB = 4.6 GB, and with candidate and retired images kept a lineage adds about 14 GB per release, 170 GB per year at twelve releases. The control host gets a 1 TB local NVMe volume, which holds the hot set and the first two years of baselines with headroom, an alarm at 70 percent and the preflight threshold above; if PA-10 doubles, the volume is 2 TB. The backup location gets 2 TB because it keeps baselines and pinned objects forever, 15 database snapshots and the exports. Each rig needs 40 GB free for the largest shard with its video: an RC shard of about 620 checkpoints is about 2.5 GB of captures plus 1 GB of video, so the R7 floor of 20 GB holds a margin for retries and the 7 day post-import retention. These figures are the ceiling 11.4 cites; the storage preflight uses the measured mean capture size, never this table.

Database. About 9,100 outcome rows per business day (10 x 250 + 4 x 1,380 + 1,100), under 4 MB per day with indexes, under 1 GB per year; step_events about 220,000 rows and 75 MB per day, held to about 7 GB by the 90 day retention; reviews about 700 per day at 3 KB; coverage cells about 15,000 rows per day on merge, RC and Nightly runs, 0.5 GB per year; canary results, egress and shard rows are small. The Central Store reaches about 7 GB in year one and grows about 2 GB per year after, which SQLite handles on local NVMe because every query path in 8.7 is an index lookup. A Run Store holds one shard and stays under 100 MB. SHA-256 of a 1.5 MB capture costs a few milliseconds, on the rig and again on the control host.

### 8.9 Store failure modes

Every store failure has an outcome in the closed taxonomy and a place where a human sees it. A store failure never produces a diff, never leaves a checkpoint without a row, and never lets a run report `complete` (the trigger in 8.3).

| Failure | Detection | Outcome | Who sees it |
|---|---|---|---|
| Database or artifact root on a share, UNC or mapped path | startup preflight (H7) | process refuses to start; run `aborted_harness (reason: preflight.H7)` if during preflight; dead-man alert at the next deadline otherwise | on-call channel |
| Free space below threshold on the rig or the control host | storage preflight at `provisioned` | `env_error (gate: preflight.R7)` on every checkpoint in scope; run `complete_with_env_errors`; the disk alarm of 9.10 | on-call channel, Run health |
| Rig disk full during a run | the Watchdog's free-space check (in its sampling loop, read by the Orchestrator at every flow boundary) below 2 GB, including the immediate check an `ENOSPC` on any write requests (10.2) | run `aborted_harness (reason: disk_full)`; the checkpoint in progress `harness_error (reason: artifact_persist_failed)` when its own write failed with `ENOSPC` (8.4), otherwise `disk_full`, and every remaining checkpoint `harness_error (reason: disk_full)`, the run's abort reason; a heartbeat write that fails with `ENOSPC` is this case, not `heartbeat_unwritable`; run re-queued once | on-call channel, Run health |
| Single artifact write or read-back verification fails on the rig (`ENOSPC` or any other error) | Capture write path | retried once; then checkpoint `harness_error (reason: artifact_persist_failed)`; the run cannot be `complete` and ends `aborted_harness (reason: artifact_persist_failed)` at teardown, unless the free-space check ends it first as `disk_full` | Run health, on-call channel |
| Run Store writer cannot commit (locked, busy timeout and 3 retries, I/O error, `SQLITE_FULL`) | writer thread | shard `aborted_harness (reason: store_write_failed)`; remaining checkpoints `harness_error (reason: store_write_failed)`, the run's abort reason; the ledger already holds the rows for replay | on-call channel, Run health |
| Central Store writer cannot commit | Reporter service writer | the shard stays `shipping` and the import retries with backoff; past the dead-man deadline the run row is `aborted_harness (reason: dead_man)`; rows are intact on the rig | on-call channel |
| Integrity check fails at service start or on the weekly snapshot | startup, weekly job | service stops; operator restores the last good snapshot and replays later ledgers and the decisions ledger; every run since the snapshot is re-imported from the rigs' retained shard directories | on-call channel, system owner |
| Shipped shard rows disagree with the ledger, or a replayed row differs from a stored row with the same natural key | import | shard `import_failed`, run `aborted_harness (reason: import_conflict)`; nothing imported; the rig keeps its copy | on-call channel, system owner |
| Upload hash mismatch | import | re-requested up to 3 times; then shard `import_failed` with `upload_hash_mismatch`, run `aborted_harness (reason: artifact_persist_failed)` | on-call channel, Run health |
| Artifact without a redaction version | rig writer or import | refused; the checkpoint `harness_error (reason: redaction_version_missing)`; egress log row with status refused if a send was attempted | Run health, security digest |
| Secret found by the post-run plaintext scan | teardown scan | objects quarantined, run `aborted_harness (reason: secret_leaked)`, report withheld, credential rotated | security contact, on-call channel |
| Shard lost (rig unreachable past the deadline) | Rig Manager heartbeat, dead-man timer | shard `lost`; run `aborted_harness (reason: dead_man)` with "rig unreachable, run data intact on rig"; imported when the rig returns, after which the run is re-evaluated | on-call channel |
| Artifact named by an outcome missing or corrupt after import | weekly artifact audit, or a report page request | `artifact_missing` finding, tier of the checkpoint; the report shows the hash with a missing marker; pinned objects restored from backup | triage rotation, Run health |
| Backup job fails, or `last_backup_at` older than 36 hours | backup job, Nightly state check | the 9.10 Store health alert (P2, posted at 06:00 per 11.6); red row in Run health; the retention sweep skipped that night | on-call channel, system owner |
| Restore drill older than 100 days | Nightly state check | red row in Run health | system owner |
| Retention sweep would remove a pinned or referenced object | sweep | object kept; count reported | Run health |
| Schema migration fails | migration step | service stops with the pre-migration snapshot intact | on-call channel |
| Decision, baseline or incident write rejected by a constraint | Reporter service API | request refused with the constraint name; nothing written; the UI shows the reason | the acting person; weekly summary |
