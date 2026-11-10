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
-- Everything else is the DDL of 8.3 as written. The section at the end holds the store's additions.

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
                     'session_unavailable','live_identity')),   -- 6.4, 6.5, 6.6, 6.8; cleared by a human
  unblocked_by TEXT, unblocked_at TEXT,
  CHECK ((blocked_at IS NULL) OR (blocked_reason IS NOT NULL))
);

CREATE TABLE rigs (
  rig_id       TEXT PRIMARY KEY,
  tier         TEXT NOT NULL CHECK (tier IN ('A','B')),   -- B = the physical D3D rig
  role         TEXT NOT NULL CHECK (role IN ('pool','nightly','explorer')),   -- fleet of 11.2
  login_alias  TEXT NOT NULL REFERENCES logins(login_alias),
  snapshot_id  TEXT NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1
);

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
  host              TEXT CHECK (host IS NULL OR host IN ('tab','sidebar','flexgrid','detached','dialog')),   -- sweep: the host enum of 4.1
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
  OR (SELECT count(*) FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0) < NEW.scope_count
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.state <> 'imported')
  OR EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND (
        (o.cand_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.cand_capture_sha256))
     OR (o.base_capture_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.base_capture_sha256))
     OR (o.heatmap_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.heatmap_sha256)))))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: env_error, store failure, missing outcome, unimported shard or missing artifact'); END;

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
                                            -- watchdog_hang, watchdog_crash, kill_switch
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
  UNIQUE (shard_id, seq),
  CHECK (gate_decision NOT IN ('dropped','refused') OR gate_reason IS NOT NULL)
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
  created_at       TEXT NOT NULL
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
  status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','superseded','reopened','revoked')),   -- 4.14
  supersedes_decision_id INTEGER REFERENCES decisions(decision_id),
  revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT,
  source_finding_id INTEGER REFERENCES findings(finding_id),
  CHECK ({{decision_action_verdict}}),   -- the 4.14 and 9.6 mapping; request_mask is inert until mask_ref is set
  CHECK (action NOT IN ({{decision_actions:mark_intended,fixed_in_build}}) OR ticket_ref IS NOT NULL),   -- L-DEC-02 at write time
  CHECK (action <> {{decision_actions:reject_baseline}} OR verdict <> {{decision_verdicts:confirmed_bug}} OR ticket_ref IS NOT NULL),
  -- confirm_bug may be written with ticket_ref NULL while Jira is unreachable; the outbox job fills it and the digest lists the row as without ticket until then (9.9)
  CHECK (action <> {{decision_actions:fixed_in_build}} OR fixed_in_build_id IS NOT NULL),
  CHECK (action NOT IN ({{decision_actions:approve_baseline,reject_baseline}}) OR baseline_id IS NOT NULL),
  CHECK (action NOT IN ({{decision_actions:noise_signature,request_mask}}) OR expires_at IS NOT NULL),
  CHECK (decided_by NOT IN ('reviewer','system','orchestrator','reporter')),                        -- L-DEC-01
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK ((revoked_at IS NOT NULL) = (status = 'revoked'))
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
  PRIMARY KEY (checkpoint_id, region_id, config_commit),
  CHECK (money_cell = 0 OR kind IN ('diff','freeze')),   -- L-RGN-04: a money cell is always pixel-diffed, never rule-only or masked
  CHECK (identity_field = 0 OR kind = 'redaction')
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
  quarantined_at TEXT, quarantined_by TEXT, quarantine_owner TEXT, quarantine_ticket_ref TEXT, quarantine_reason TEXT,
  updated_at       TEXT NOT NULL,
  CHECK (quarantined = 0 OR (quarantine_commit IS NOT NULL AND quarantine_owner IS NOT NULL
                             AND quarantine_ticket_ref IS NOT NULL))
);

CREATE TABLE canary_results (
  canary_id         INTEGER PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  kind              TEXT NOT NULL DEFAULT 'mutant' CHECK (kind IN ('mutant','safety_canary','fire_drill','path_equivalence')),   -- 10.3 mutants; 6.3 to 6.10 safety canaries; 6.5 drill items; the nightly re-evaluated real pair of 10.3
  mutant_id         TEXT NOT NULL,          -- M01 to M33 of sentinel-config/canaries/mutants.yaml (12.5); drill items are drill-1 to drill-5; safety canaries name their control (6.3 to 6.10)
  catalog_version   TEXT NOT NULL,
  checkpoint_id     TEXT NOT NULL,
  defect_class      TEXT NOT NULL,          -- the snake_case class names of 10.3 (erased_control, swapped_digit, swapped_label, color_swap,
                                            -- shifted_panel, clipped_label, overlapped_panels, missing_row, header_change, placeholder,
                                            -- font_height, scrollbar, redaction, sub_floor, global_shift, landmark_loss, study_removed,
                                            -- aa_control, wrong_quantity, blank_field, gateway) plus the classes M29 to M33 of 12.5 add
                                            -- (outside_parent, window_offscreen, misaligned_column, focus_order, error_dialog); 26 names, equal to the
                                            -- class enum of 4.11; one list, kept in canaries/mutants.yaml
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
  CHECK (NOT (money_cell = 1 AND state = {{canary_states:missed}})),            -- a money-cell miss is always detector_blind
  CHECK (NOT (money_cell = 0 AND state = {{canary_states:detector_blind}})),
  CHECK (state <> {{canary_states:detector_degraded}} OR (class_rate IS NOT NULL AND class_floor IS NOT NULL)),
  CHECK (state NOT IN ({{canary_states:missed,detector_blind,detector_degraded}}) OR produced_outcome IS NOT NULL)
);

CREATE TABLE calibration_floors (
  floor_id           INTEGER PRIMARY KEY,
  checkpoint_id      TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id          TEXT NOT NULL,         -- '*' for whole window
  fingerprint_sha256 TEXT NOT NULL,         -- a floor is valid for one fingerprint only
  calibration_run_id TEXT NOT NULL REFERENCES runs(run_id),
  samples            INTEGER NOT NULL,      -- A/A pairs measured
  diff_pixels_p50 INTEGER, diff_pixels_p95 INTEGER, diff_pixels_max INTEGER,
  ssim_min           REAL,
  components_max INTEGER, largest_bbox_max TEXT,
  phash_noise_spread INTEGER, fault_separation INTEGER,   -- inputs to the tolerance rule of 9.7
  pairs_json         TEXT,                  -- the per-pair metrics of 3.9 step 3 (15 pairs per checkpoint and region)
  threshold_pixels   INTEGER NOT NULL,      -- derived per 3.9
  threshold_ssim     REAL NOT NULL,
  nonzero_on_frozen  INTEGER NOT NULL DEFAULT 0,   -- 1 marks a flow or environment bug to fix
  exported_commit    TEXT,                  -- sentinel-config commit of calibration/<fingerprint_id>.yaml
  valid_from         TEXT NOT NULL, superseded_at TEXT
);
CREATE UNIQUE INDEX ux_floor_current ON calibration_floors(checkpoint_id, region_id, fingerprint_sha256)
  WHERE superseded_at IS NULL;

CREATE TABLE calibration_fixes (            -- the fix list of 3.9: one row per non-zero floor on a frozen checkpoint
  fix_id         INTEGER PRIMARY KEY,
  floor_id       INTEGER NOT NULL REFERENCES calibration_floors(floor_id),
  checkpoint_id  TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  region_id      TEXT NOT NULL,
  cause_class    TEXT NOT NULL CHECK (cause_class IN ('timing','state','animation','landmark','redaction','rendering','unknown')),
  owner          TEXT NOT NULL,
  ticket_ref     TEXT,
  opened_at      TEXT NOT NULL,             -- age = now minus opened_at; open 5 business days quarantines the checkpoint (10.5)
  closed_at TEXT, closed_by TEXT, floor_exception_ref TEXT,   -- closed by a fix, or by a human-approved floor_exception (3.9)
  CHECK ((closed_at IS NULL) = (closed_by IS NULL))
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
  kind            TEXT NOT NULL CHECK (kind IN ('pr_comment','commit_status','jira_create','jira_comment','jira_transition',
                    'slack','page','digest','manifest_pr','mask_pr')),
  destination     TEXT NOT NULL,
  payload_sha256  TEXT NOT NULL,            -- the payload is an artifact; the row carries its hash
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error      TEXT,
  state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','sent','failed','abandoned')),
  created_at      TEXT NOT NULL, sent_at TEXT,
  CHECK ((state = 'sent') = (sent_at IS NOT NULL))
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

CREATE TABLE post_run_proofs (              -- 6.6: ORDER SAFETY PROOF, one per shard
  shard_id          TEXT PRIMARY KEY REFERENCES shards(shard_id),
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  login_alias       TEXT NOT NULL REFERENCES logins(login_alias),
  verdict           TEXT NOT NULL CHECK (verdict IN ('proven_no_sends','proven_declared_sends','proven_no_session','unproven','orders_sent')),   -- proven_no_session: no token issued, read from the gateway log; orders_sent: a clean read found an undeclared row (6.6)
  reason            TEXT,
  send_policy_ref   TEXT NOT NULL,          -- risk owner decision id from policy/send_policy.yaml
  counts_json       TEXT NOT NULL,          -- working, filled, canceled, position rows: start, end, new, expected; net liq start, end, drift
  cancel_all        TEXT NOT NULL CHECK (cancel_all IN ('not_needed','performed_verified','performed_unverified')),
  paper_mode_assertions INTEGER NOT NULL, paper_mode_mismatches INTEGER NOT NULL,
  gateway_events INTEGER NOT NULL, gateway_drops INTEGER NOT NULL,
  start_snapshot_sha256 TEXT, end_snapshot_sha256 TEXT,
  produced_at       TEXT NOT NULL,
  verified_by TEXT, verified_at TEXT,       -- human clearance of an unproven verdict
  CHECK (verdict <> 'unproven' OR reason IS NOT NULL)
);

CREATE TABLE incidents (                    -- 6.12: S1 to S3 rows, linked from the report
  incident_id   INTEGER PRIMARY KEY,
  run_id        TEXT REFERENCES runs(run_id),
  rig_id        TEXT, login_alias TEXT,
  severity      TEXT NOT NULL CHECK (severity IN ('S1','S2','S3')),
  trigger       TEXT NOT NULL,
  opened_at     TEXT NOT NULL,
  closed_at TEXT, closed_by TEXT, close_reason TEXT, ticket_ref TEXT, postmortem_ref TEXT,
  CHECK ((closed_at IS NULL) = (closed_by IS NULL)),
  CHECK (closed_by IS NULL OR closed_by NOT IN ('reviewer','system','orchestrator','reporter'))
);

CREATE TABLE system_state (                 -- keys: last_backup_at, last_backup_error, last_integrity_check_at,
  key TEXT PRIMARY KEY,                     --   last_restore_drill_at, last_artifact_audit_at, kill_switch_state,
  value TEXT NOT NULL, updated_at TEXT NOT NULL   --   detector_state, schema_version, free_bytes_at_last_preflight
);

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

-- ==========================================================================================================
-- Store additions: not in the 8.3 DDL. Each closes a gap between the 8.3 DDL and a rule the spec states in
-- words; the module docstring of sentinel.store.store lists them with their sections.

-- The checkpoints a run is asked to visit, with the persona of the visit (4.13 checkpoint_outcome.persona has
-- no 8.3 column). runs.scope_count is the count of these rows. With trg_run_complete_scope a run is complete
-- only when every one of them, by id, has an outcome (8.1, 8.3), and the 8.2 import can fill the silent ones.
CREATE TABLE run_scope (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
  persona       TEXT NOT NULL,
  PRIMARY KEY (run_id, checkpoint_id)
) WITHOUT ROWID;

-- The 4.13 outcome record as written, so the store hands back the whole record (persona, classification,
-- diff regions: fields 8.3 has no column for). The `outcomes` columns stay the truth for the later changes.
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

-- 4.13 and 8.3: "Nothing else is ever rewritten." The 8.3 triggers guard outcome, reason, reason_detail,
-- review_state and the evidence columns; these guard every other column, and deletion.
CREATE TRIGGER trg_outcome_keep BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcome is immutable'); END;

CREATE TRIGGER trg_outcome_columns_immutable BEFORE UPDATE OF run_id, checkpoint_id, shard_id, side, gate, rescued,
  base_source_run_id, data_epoch_id, paper_asserted_at, cand_rig_id, base_rig_id, settle_ms, shift_dx, shift_dy,
  threshold_source, recaptures, stability, stability_cause, manifest_entry, primitive, recorded_at ON outcomes
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

-- 8.1 and 8.3: a run is complete only when every scoped checkpoint, by id, has a non-synthetic outcome.
CREATE TRIGGER trg_run_complete_scope BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = {{run_outcomes:complete}} AND EXISTS (
  SELECT 1 FROM run_scope s WHERE s.run_id = NEW.run_id AND NOT EXISTS (
    SELECT 1 FROM outcomes o WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND o.synthetic = 0))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: a scoped checkpoint has no outcome'); END;

-- 1.10 per run and 8.2: `complete` also needs no not_comparable (fingerprint_mismatch) checkpoint, no UNPROVEN or
-- ORDERS SENT post-run proof (6.6) and no shard that ended worse than complete (the run outcome is the worst of the
-- shards').
CREATE TRIGGER trg_run_complete_env BEFORE UPDATE OF outcome ON runs
WHEN NEW.outcome = {{run_outcomes:complete}} AND (
     EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id = NEW.run_id AND o.synthetic = 0
             AND o.outcome = {{checkpoint_outcomes:not_comparable}} AND o.reason = {{reason_codes:fingerprint_mismatch}})
  OR EXISTS (SELECT 1 FROM post_run_proofs p WHERE p.run_id = NEW.run_id AND (p.verdict = 'orders_sent'
             OR (p.verdict = 'unproven' AND p.verified_by IS NULL)))
  OR EXISTS (SELECT 1 FROM shards s WHERE s.run_id = NEW.run_id AND s.shard_outcome <> {{run_outcomes:complete}}))
BEGIN SELECT RAISE(ABORT, 'run cannot be complete: fingerprint mismatch, unproven or orders-sent post-run proof or a shard worse than complete'); END;
