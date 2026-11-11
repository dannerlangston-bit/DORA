-- The Sentinel schema of 8.3 Schema, phases 1 to 3, the same in the Run Store and the Central Store.
--
-- This file is a TEMPLATE. sentinel.store.store.render_schema() fills every double-brace placeholder from
-- config/taxonomy.yaml when a database is created (4.1 rule L-OUT-01), so no list of outcomes, reasons, finding
-- kinds, triage states, decision values, review states or canary states below is hand-copied:
--   <list>             every value of that taxonomy list, as quoted SQL literals
--   <list>:a,b         the named values, each checked to be in that list (schema creation fails otherwise)
--   reason_codes:x     a reason code that some outcome lists under `reasons`
--   not_run_reason     the coverage_cells reason check, generated from reasons.not_run
--   decision_action_verdict  the 4.14 action -> verdict mapping, from sentinel.records.ACTION_VERDICT
-- The outcome_reasons seed is generated the same way (store.seed_rows) and inserted after this script.
-- Rendered, every statement equals the DDL blocks of 8.3 (tests/test_schema_mirror.py); 8.3 prints the lists as
-- literals. The 8.3 blocks group the statements by subject; this file keeps one creation order.

CREATE TABLE outcome_reasons (
  outcome     TEXT NOT NULL,              -- a checkpoint outcome or a run outcome
  reason      TEXT NOT NULL,
  parametric  INTEGER NOT NULL DEFAULT 0 CHECK (parametric IN (0,1)),
  detail_means TEXT,                      -- what reason_detail holds when parametric
  owner_section TEXT NOT NULL,            -- the subsection whose rule produces it
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  PRIMARY KEY (outcome, reason)
) WITHOUT ROWID;

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
  outcome           TEXT CHECK (outcome IN ({{run_outcomes}})),
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
  CHECK (abort_reason IS NULL OR outcome LIKE 'aborted_%' OR outcome = {{run_outcomes:complete_with_env_errors}}),   -- the cause of a complete_with_env_errors run lives here too
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
  shard_outcome    TEXT CHECK (shard_outcome IN ({{run_outcomes}})),
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
  outcome             TEXT NOT NULL CHECK (outcome IN ({{checkpoint_outcomes}})),
  reason              TEXT,                 -- from outcome_reasons
  reason_detail       TEXT,                 -- step id, landmark id, decision id, probe id, all fired invariant ids, assertion id and values, differing fingerprint fields
  rescued             INTEGER NOT NULL DEFAULT 0 CHECK (rescued IN (0,1)),   -- reached through passed_via_agent_fallback (5.5)
  base_source_run_id  TEXT REFERENCES runs(run_id),   -- the run whose base capture was reused (7.4, base_reused)
  data_epoch_id       TEXT,                 -- hash of the 7.9 epoch tuple, equal on both sides
  paper_asserted_at   TEXT,                 -- the paper-mode assertion the capture was released under (6.4)
  review_state        TEXT NOT NULL DEFAULT {{review_states:none}} CHECK (review_state IN ({{review_states}})),   -- 4.13; none when no review applies
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
  CHECK (outcome <> {{checkpoint_outcomes:identical}} OR reason IS NULL),
  CHECK (reason IS NOT NULL OR outcome IN ({{checkpoint_outcomes:identical,intended,collateral}})),
  CHECK (reason IS NOT {{reason_codes:decision}} OR decision_id IS NOT NULL),   -- decision:<id> of 4.13
  CHECK (stability_cause IS NULL OR stability = 'unknown'),
  CHECK (review_state <> {{review_states:pending}} OR outcome = {{checkpoint_outcomes:needs_human}}),
  CHECK (review_state = {{review_states:none}} OR outcome IN ({{checkpoint_outcomes:needs_human,intended,collateral,noise}})),
  CHECK (synthetic = 0 OR decision_id IS NULL),    -- synthetic rows never match a decision (10.3)
  CHECK (gate IS NULL OR outcome = {{checkpoint_outcomes:env_error}}),
  CHECK (outcome <> {{checkpoint_outcomes:env_error}} OR reason <> {{reason_codes:gate}} OR gate IS NOT NULL),
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
WHEN NOT (OLD.outcome = {{checkpoint_outcomes:needs_human}} AND OLD.review_state = {{review_states:pending}} AND NEW.review_state = {{review_states:done}}
          AND (   (NEW.outcome IN ({{checkpoint_outcomes:intended,collateral,noise,needs_human}}) AND NEW.review_id IS NOT NULL)
               -- end of review without a verdict (4.13): per-run spend ceiling, or the Reviewer job gave up
               OR (NEW.outcome = {{checkpoint_outcomes:needs_human}} AND NEW.reason IN ({{reason_codes:reviewer_cap,reviewer_unavailable}})
                   AND NEW.review_id IS NULL)))
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

CREATE TRIGGER trg_outcome_evidence_immutable BEFORE UPDATE OF cand_capture_sha256, base_capture_sha256,
  heatmap_sha256, diff_pixels, diff_pct, ssim, components, largest_bbox, masked_pct, captured_at, synthetic ON outcomes
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;

CREATE TRIGGER trg_review_state BEFORE UPDATE OF review_state ON outcomes
WHEN NOT (OLD.review_state = {{review_states:pending}} AND NEW.review_state = {{review_states:done}})
BEGIN SELECT RAISE(ABORT, 'review_state moves only from pending to done'); END;

CREATE TRIGGER trg_run_complete BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = {{run_outcomes:complete}} AND (
     EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0 AND o.outcome = {{checkpoint_outcomes:env_error}})
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.outcome = {{checkpoint_outcomes:harness_error}}
             AND o.reason IN ({{reason_codes:store_write_failed,artifact_persist_failed,redaction_version_missing}}))
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0
             AND o.outcome = {{checkpoint_outcomes:not_comparable}} AND o.reason = {{reason_codes:fingerprint_mismatch}})   -- 1.10 per run
  OR EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = NEW.run_id AND p.verdict = 'unproven' AND p.verified_by IS NULL)   -- 6.6
  OR (SELECT count(*) FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0) < NEW.scope_count
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.state <> 'imported')
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND (
        (o.cand_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.cand_capture_sha256))
     OR (o.base_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.base_capture_sha256))
     OR (o.heatmap_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.heatmap_sha256)))))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: env_error, store failure, fingerprint mismatch, unproven proof, missing outcome, unimported shard or missing artifact'); END;

CREATE TABLE outcomes_superseded AS SELECT o.*, 1 AS attempt FROM outcomes o WHERE 0;   -- no triggers; filled by the import of a returning rig

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
                                            -- foreign_window, export, clipboard, recovery_login, explorer_action,
                                            -- main_frame_missing
  locator_level     TEXT CHECK (locator_level IN ('jab','image','ocr','agent')),
  attempt           INTEGER NOT NULL DEFAULT 1,   -- 2 = the single permitted retry
  step_outcome      TEXT CHECK (step_outcome IN ({{step_outcomes}})),
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
  verdict          TEXT NOT NULL CHECK (verdict IN ({{reviewer_verdicts:matches_manifest,partially_matches_manifest,does_not_match_manifest,no_visible_difference,cannot_judge}})),
  mapped_outcome   TEXT NOT NULL CHECK (mapped_outcome IN ({{checkpoint_outcomes:intended,collateral,noise,needs_human}})),
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
  task             TEXT CHECK (task IS NULL OR task IN ('manifest_match','describe_collateral','manifest_visibility')),   -- 3.13 tasks A to C
  intent           TEXT,                    -- the declared intent the item was judged against (3.13)
  UNIQUE (run_id, checkpoint_id, diff_hash, model_id, prompt_hash)
);

CREATE TABLE findings (
  finding_id       INTEGER PRIMARY KEY,
  natural_key      TEXT NOT NULL UNIQUE,    -- run, checkpoint, kind, region or step, invariant or assertion id
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id    TEXT REFERENCES checkpoints(checkpoint_id),
  kind             TEXT NOT NULL CHECK (kind IN ({{finding_kinds}})),
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
  category         TEXT CHECK (category IN ({{checkpoint_outcomes:intended,collateral,noise,needs_human,invariant_violation,assertion_failed}})),
  cluster_id       TEXT,
  description      TEXT NOT NULL,
  evidence_json    TEXT NOT NULL,           -- artifact hashes, step_events range, raw and expected values, candidate flow path
  decision_id      INTEGER,                 -- the decision this finding inherited or produced
  ticket_ref       TEXT,
  triage_state     TEXT NOT NULL DEFAULT {{triage_states:open}} CHECK (triage_state IN ({{triage_states}})),
  synthetic        INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),
  first_seen_run_id TEXT,
  created_at       TEXT NOT NULL,
  detail           TEXT,                    -- what the finding's reason points at, e.g. static_impact (1.8, 2.10)
  closed_at        TEXT,                    -- starts the 180-day release of its evidence (8.6)
  contested        INTEGER NOT NULL DEFAULT 0 CHECK (contested IN (0,1)),   -- conflicting decisions on the signature (9.7)
  held             INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0,1)),        -- held from the report: Explorer, not reproduced (2.9)
  explorer_json    TEXT,                    -- the 4.13 explorer block of an explorer_candidate finding
  CHECK (explorer_json IS NULL OR kind = {{finding_kinds:explorer_candidate}})
);

CREATE TABLE decisions (
  decision_id       INTEGER PRIMARY KEY,
  checkpoint_id     TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id         TEXT NOT NULL,          -- region id, or grid cell g-<row>-<col>
  phash             BLOB NOT NULL,          -- 64-bit perceptual hash of the anchored crop (3.7)
  hamming_tolerance INTEGER NOT NULL DEFAULT 8 CHECK (hamming_tolerance BETWEEN 0 AND 64),   -- 6 for tier 1; calibration may lower (9.7)
  category          TEXT NOT NULL CHECK (category IN ({{checkpoint_outcomes:intended,collateral,noise,needs_human,invariant_violation,assertion_failed}})),
  action            TEXT NOT NULL CHECK (action IN ({{decision_actions}})),                          -- the UI action of 4.14 and 9.6
  verdict           TEXT NOT NULL CHECK (verdict IN ({{decision_verdicts}})),   -- the stored effect
  source            TEXT NOT NULL CHECK (source IN ({{decision_sources}})),
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
  status            TEXT NOT NULL DEFAULT {{decision_statuses:active}} CHECK (status IN ({{decision_statuses}})),   -- 4.14
  supersedes_decision_id INTEGER REFERENCES decisions(decision_id),
  revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT,
  source_finding_id INTEGER REFERENCES findings(finding_id),
  bbox_q4           TEXT,                   -- x,y,w,h of the largest component quantized to 4 px: the fallback key at or below noise spread (4.14)
  role              TEXT CHECK (role IS NULL OR role IN ('triage_rotation','flow_owner','system_owner','release_manager','pr_author','ticket_resolver')),   -- 4.14, 9.12, L-DEC-01
  reopen_count      INTEGER NOT NULL DEFAULT 0 CHECK (reopen_count >= 0),   -- fixed_in_build signatures that returned (4.14)
  CHECK ({{decision_action_verdict}}),   -- the 4.14 and 9.6 mapping; request_mask is inert until mask_ref is set
  CHECK (action NOT IN ({{decision_actions:mark_intended,fixed_in_build}}) OR ticket_ref IS NOT NULL),   -- L-DEC-02 at write time
  CHECK (action <> {{decision_actions:reject_baseline}} OR verdict <> {{decision_verdicts:confirmed_bug}} OR ticket_ref IS NOT NULL),
  -- confirm_bug may be written with ticket_ref NULL while Jira is unreachable; the outbox job fills it and the digest lists the row as without ticket until then (9.9)
  CHECK (action <> {{decision_actions:fixed_in_build}} OR fixed_in_build_id IS NOT NULL),
  CHECK (action NOT IN ({{decision_actions:approve_baseline,reject_baseline}}) OR baseline_id IS NOT NULL),
  CHECK (action NOT IN ({{decision_actions:noise_signature,request_mask}}) OR expires_at IS NOT NULL),
  CHECK (decided_by NOT IN ('reviewer','system','orchestrator','reporter')),                        -- L-DEC-01
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK ((revoked_at IS NOT NULL) = (status = {{decision_statuses:revoked}}))
);

CREATE TRIGGER trg_decision_two_person BEFORE INSERT ON decisions
WHEN NEW.action = {{decision_actions:mark_intended}}
 AND (SELECT tier FROM checkpoints c WHERE c.checkpoint_id = NEW.checkpoint_id) IN (1, 2)
 AND NEW.decided_by = (SELECT pr_author FROM runs r WHERE r.run_id = NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'two-person rule: the PR author cannot mark a tier 1 or 2 diff intended alone'); END;

CREATE TRIGGER trg_decision_money_cell BEFORE INSERT ON decisions
WHEN NEW.action IN ({{decision_actions:noise_signature,request_mask}})
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
 AND NOT (OLD.mask_ref IS NULL AND NEW.mask_ref IS NOT NULL AND OLD.action = {{decision_actions:request_mask}})
BEGIN SELECT RAISE(ABORT, 'mask_ref is set once, on a request_mask decision (4.14)'); END;

-- a confirm_bug written while Jira was unreachable gets its ticket from the outbox once (L-DEC-02, 9.9)
CREATE TRIGGER trg_decision_ticket_ref BEFORE UPDATE OF ticket_ref ON decisions
WHEN NEW.ticket_ref IS NOT OLD.ticket_ref
 AND NOT (OLD.ticket_ref IS NULL AND NEW.ticket_ref IS NOT NULL AND OLD.action = {{decision_actions:confirm_bug}})
BEGIN SELECT RAISE(ABORT, 'ticket_ref is filled once, on a confirm_bug decision written without one (9.9)'); END;

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
     (SELECT outcome FROM runs r WHERE r.run_id = NEW.source_run_id) IS NOT {{run_outcomes:complete}}
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
  money_role      TEXT CHECK (money_role IS NULL OR money_role IN ({{money_roles}})),   -- 4.8 (SD-28)
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
  quarantine_reason TEXT CHECK (quarantine_reason IS NULL OR quarantine_reason IN ({{quarantine_reasons}})),   -- 10.5 (SD-96)
  updated_at       TEXT NOT NULL,
  CHECK (quarantined = 0 OR (quarantine_commit IS NOT NULL AND quarantine_owner IS NOT NULL
                             AND quarantine_ticket_ref IS NOT NULL))
);

CREATE TABLE synthetic_outcomes (           -- 10.3: what each canary mutant produced; outcomes holds real rows only (SD-91)
  synthetic_id      INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  mutant_id         TEXT NOT NULL,
  checkpoint_id     TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  outcome           TEXT NOT NULL CHECK (outcome IN ({{checkpoint_outcomes}})),
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
  kind              TEXT NOT NULL DEFAULT {{canary_kinds:mutant}} CHECK (kind IN ({{canary_kinds}})),   -- 10.3 mutants; 6.3 to 6.10 safety canaries; 6.5 drill items; the nightly re-evaluated real pair of 10.3
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
  state             TEXT NOT NULL CHECK (state IN ({{canary_states}})),
  absorbed_by       TEXT,                   -- mask id, decision id, threshold or landmark that swallowed a miss
  class_rate REAL, class_floor REAL,        -- that night's class detection rate and rolling floor
  reviewer_verdict TEXT, reviewer_confidence REAL,
  mask_version TEXT, threshold_version TEXT, model_id TEXT, prompt_hash TEXT,
  created_at        TEXT NOT NULL,
  synthetic_ref     INTEGER REFERENCES synthetic_outcomes(synthetic_id),   -- the mutant's produced outcome row (SD-91)
  CHECK ((kind = {{canary_kinds:mutant}} AND defect_class IN ({{mutant_classes}}))
      OR (kind <> {{canary_kinds:mutant}} AND defect_class = kind)),   -- SD-81: a safety canary, drill item or path-equivalence pair names its kind
  CHECK (NOT (money_cell = 1 AND state = {{canary_states:missed}})),            -- a money-cell miss is always detector_blind
  CHECK (NOT (money_cell = 0 AND state = {{canary_states:detector_blind}})),
  CHECK (state <> {{canary_states:detector_degraded}} OR (class_rate IS NOT NULL AND class_floor IS NOT NULL)),
  CHECK (state NOT IN ({{canary_states:missed,detector_blind,detector_degraded}}) OR produced_outcome IS NOT NULL)
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
  outcome        TEXT CHECK (outcome IS NULL OR outcome IN ({{checkpoint_outcomes:not_settled,landmark_missing,env_error,harness_error}})),   -- a failed capture's outcome (3.9 step 2)
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

CREATE TABLE egress_log (                   -- 6.10: every outbound request
  egress_id         INTEGER PRIMARY KEY,
  run_id            TEXT REFERENCES runs(run_id),   -- NULL for the daily digest and the model check
  shard_id          TEXT, checkpoint_id TEXT,
  sent_at           TEXT NOT NULL,
  destination       TEXT NOT NULL,          -- host name
  purpose           TEXT NOT NULL CHECK (purpose IN ('review','batch_review','agent_step',
                      'explorer_step','pr_comment','jira','slack','digest','preflight','model_check','page')),
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
  kind            TEXT NOT NULL CHECK (kind IN ('pr_comment','commit_status','jira_create','jira_comment','jira_transition',
                    'slack','page','digest','manifest_pr','mask_pr','quarantine_pr','flow_pr','anchor_pr')),
                                            -- quarantine_pr: 10.5; flow_pr, anchor_pr: 9.6
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
  CHECK (closed_by IS NULL OR (state = 'abandoned' AND closed_by NOT IN ('reviewer','system','orchestrator','reporter')))
);

CREATE TABLE coverage_cells (               -- merge, RC and nightly runs only; PR runs keep the matrix in runs.summary_json
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  surface_id       TEXT NOT NULL,
  persona          TEXT NOT NULL,
  entitlement      TEXT NOT NULL,
  tier             INTEGER NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('covered','not_run','not_registered')),
  not_run_reason   TEXT CHECK ({{not_run_reason}}
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
  verdict           TEXT NOT NULL CHECK (verdict IN ('proven_no_sends','proven_declared_sends','proven_no_session','unproven','orders_sent')),   -- proven_no_session: no token issued, read from the gateway log; orders_sent: a clean read found an undeclared row (6.6)
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
  reason            TEXT NOT NULL CHECK (reason IN ({{quarantine_reasons}})),   -- 10.5 (SD-96)
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
  escape_class      TEXT NOT NULL CHECK (escape_class IN ('covered_missed','not_run','not_registered','not_detectable')),
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
  action            TEXT NOT NULL CHECK (action IN ({{decision_actions}})),
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
  CHECK (decided_by IS NULL OR (decided_by <> proposed_by AND decided_by NOT IN ('reviewer','system','orchestrator','reporter'))),
  CHECK (state <> 'declined' OR decline_reason IS NOT NULL),
  CHECK (state <> 'confirmed' OR decision_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_proposal_open ON proposals(finding_id) WHERE state = 'proposed';

CREATE TABLE system_state (                 -- keys: last_backup_at, last_backup_error, last_integrity_check_at,
  key TEXT PRIMARY KEY,                     --   last_restore_drill_at, last_artifact_audit_at, kill_switch_state,
  value TEXT NOT NULL,                      --   detector_state, canaries_pending, nightly_skipped_for, reviewer_disabled,
  updated_at TEXT NOT NULL                  --   harness_freeze, schema_version, schema_sha256, free_bytes_at_last_preflight,
);                                          --   alert_undelivered

CREATE INDEX ix_runs_type_time   ON runs(run_type, started_at);
CREATE INDEX ix_runs_pair        ON runs(pair_id);
CREATE INDEX ix_shards_run       ON shards(run_id, state);
CREATE INDEX ix_outcomes_cp_run  ON outcomes(checkpoint_id, run_id);      -- history per checkpoint
CREATE INDEX ix_outcomes_run_out ON outcomes(run_id, outcome, reason);
CREATE INDEX ix_outcomes_pending ON outcomes(run_id) WHERE review_state = {{review_states:pending}};
CREATE INDEX ix_steps_run_seq    ON step_events(run_id, shard_id, seq);
CREATE INDEX ix_steps_notable    ON step_events(run_id, step_outcome)
  WHERE step_outcome IN ({{step_outcomes:passed_after_retry,passed_via_agent_fallback,failed,blocked_by_gateway}});
CREATE INDEX ix_steps_dropped    ON step_events(run_id) WHERE gate_decision IN ('dropped','refused');
CREATE INDEX ix_findings_open    ON findings(triage_state, tier, created_at) WHERE triage_state = {{triage_states:open}};
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
 AND NOT (OLD.review_state = {{review_states:pending}} AND NEW.review_state = {{review_states:done}})
BEGIN SELECT RAISE(ABORT, 'review_id is set only when the review ends (4.13)'); END;

-- F65: every env_error names its gate, from the gates list of taxonomy.yaml (4.13).
CREATE TRIGGER trg_outcome_env_gate BEFORE INSERT ON outcomes
WHEN NEW.outcome = {{checkpoint_outcomes:env_error}} AND (NEW.gate IS NULL OR NEW.gate NOT IN ({{gates}}))
BEGIN SELECT RAISE(ABORT, 'env_error needs a gate from taxonomy.yaml (F65)'); END;

-- SD-78: fingerprint_mismatch is a plain reason whose reason_detail, the differing field names, is never empty.
CREATE TRIGGER trg_outcome_fingerprint_detail BEFORE INSERT ON outcomes
WHEN NEW.outcome = {{checkpoint_outcomes:not_comparable}} AND NEW.reason = {{reason_codes:fingerprint_mismatch}}
 AND (NEW.reason_detail IS NULL OR trim(NEW.reason_detail) = '')
BEGIN SELECT RAISE(ABORT, 'fingerprint_mismatch names the differing fields in reason_detail (SD-78)'); END;

-- 8.1 and 8.3: a run is complete only when every scoped checkpoint, by id, has a non-synthetic outcome.
CREATE TRIGGER trg_run_complete_scope BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = {{run_outcomes:complete}} AND EXISTS (
  SELECT 1 FROM run_scope s WHERE s.run_id = NEW.run_id AND NOT EXISTS (
    SELECT 1 FROM outcomes o WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND o.synthetic = 0))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: a scoped checkpoint has no outcome'); END;

-- 6.6 and 8.2: `complete` also needs no ORDERS SENT post-run proof and no shard that ended worse than complete (the
-- run outcome is the worst of the shards'); trg_run_complete holds the 1.10 conditions.
CREATE TRIGGER trg_run_complete_env BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = {{run_outcomes:complete}} AND (
     EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = NEW.run_id AND p.verdict = 'orders_sent')
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.shard_outcome <> {{run_outcomes:complete}}))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: an orders-sent post-run proof or a shard worse than complete'); END;
