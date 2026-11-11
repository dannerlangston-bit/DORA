"""The wave-1 controls wired into the simulated PR run (sentinel.pipeline.run): the Orchestrator's boundaries and
budgets (5.2, 6.4, 10.2, 11.2), the paper-asserted stamp and the L-OUT-06 session context (6.4), and the step
events of a relaunch (10.2). Each test runs one clean PR run of the demo on the example config."""
from __future__ import annotations

from datetime import date

import sentinel.pipeline.run as pipeline_run
from sentinel.demo import EXAMPLE, clean_scenario
from sentinel.orchestrator.machine import Orchestrator
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store

TODAY = date(2026, 11, 10)
CHART = "charts.flexible-grid.chart-cell"


def test_every_captured_record_carries_its_sessions_paper_stamp(tmp_path):
    """6.4: every capture is stamped `{account_id_sha256, mode, asserted_at}` at capture time, and the store checks it
    against the session's assertions (`set_session_context`, L-OUT-06) before writing it."""
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.status == "complete"
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    captured = [o for o in outs if o.artifacts]
    assert captured and all(o.paper_assertion and o.paper_assertion["mode"] == "paperMoney"
                            and o.paper_assertion["account_id_sha256"].startswith("sha256:") for o in captured)


def test_a_capture_without_a_paper_assertion_is_aborted_safety_through_the_kill_switch(tmp_path, monkeypatch):
    """L-OUT-06, 6.4: a capture with no passing assertion in its session is never taken; the shard's run is
    `aborted_safety (paper_mode_mismatch)` through the 6.6 kill switch."""
    monkeypatch.setattr(Orchestrator, "paper_stamp", lambda self: None)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "paper_mode_mismatch")
    assert all(s["kill_switch"] for s in r.shards if s["verdict"] == "aborted_safety")
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    assert outs and not [o for o in outs if o.artifacts]
    assert {(o.outcome, o.reason) for o in outs} == {("env_error", "paper_mode_mismatch")}


def test_the_reviewer_refuses_a_frame_whose_stamp_was_not_current(tmp_path, monkeypatch):
    """6.4 Schedule: Capture releases no frame to the Reviewer without a current paper-asserted stamp
    (`Orchestrator.may_release`); the held items need a human (3.11 row 23) and no call is made."""
    monkeypatch.setattr(Orchestrator, "may_release", lambda self, stamp: False)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert r.reviewer_calls == []
    chart = [o for o in r.outcomes if o.surface_id == CHART]
    assert chart and {(o.outcome, o.reason) for o in chart} == {("needs_human", "reviewer_unavailable")}


def test_the_wall_clock_deadline_ends_the_run_at_a_flow_boundary(tmp_path, monkeypatch):
    """11.2, 5.2: past the PR wall budget less the report margin (35 minutes) the next flow boundary ends the run
    `aborted_budget (reason: wall_clock)`; the checkpoints not reached are `not_run (reason: budget)`."""
    monkeypatch.setattr(pipeline_run, "STEP_S", 2400.0)         # each Executor step takes forty simulated minutes
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_budget", "wall_clock")
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
    assert ("not_run", "budget") in {(o.outcome, o.reason) for o in outs}
    assert "wall_clock" in (tmp_path / "pr-comment.md").read_text()


def test_paper_mode_is_asserted_before_every_unit_of_work(tmp_path):
    """6.4 Schedule: the assertion runs at the gate and again before every flow, census visit or sweep ladder
    (`flow_boundary`); the proof row counts every passing assertion."""
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        proofs = store.post_run_proofs(sc.run_id)
    units = {s["shard_id"]: s for s in r.shards}
    for p in proofs:
        gates = units[p.shard_id]["gates"]
        assert p.paper_mode_assertions == sum(1 for g in gates if g["control"] == "paper_mode" and g["passed"]) > 1


def test_a_dead_hook_host_fails_the_step_closed_and_relaunches_on_its_own_budget(tmp_path, monkeypatch):
    """5.4.2, 5.4.8 (DropLedger.record with the shard's budgets): the first `hook_host_dead` refusal of a flow step is
    `watchdog.hook_host_died`: the rest of the flow `not_run (reason: step_failed:<step>)`, a hook-host relaunch
    through every gate (`Orchestrator.relaunch("hook_host")`, a relaunch step event), and the run goes on."""
    from sentinel.demo import backend_scenario
    from sentinel.orchestrator.gateway import SlowDecision
    from sentinel.orchestrator.rig import EmitResult
    from sentinel.orchestrator.simrig import SimulatedRig

    real = SimulatedRig._send_input
    died: list[str] = []

    def send_input(self, action, *, events=()):
        if not died and action.flow_class == "standard" and action.keys:
            died.append(str(action.keys))
            return EmitResult(refused=SlowDecision(True, "hook_host_dead", detail="hook host heartbeat stale"))
        return real(self, action, events=events)

    monkeypatch.setattr(SimulatedRig, "_send_input", send_input)
    sc = backend_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert died and r.status == "complete" and not r.notes[0].startswith("report withheld")
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
        relaunch = store.query("SELECT reason FROM step_events WHERE run_id = ? AND action = 'relaunch'", (sc.run_id,))
        refused = store.query("SELECT gate_reason FROM step_events WHERE run_id = ? AND gate_decision = 'refused'",
                              (sc.run_id,))
    assert any(o.outcome == "not_run" and (o.reason or "").startswith("step_failed:") for o in outs)
    assert all("watchdog_event: wd-" in (o.detail or "") for o in outs if o.outcome == "harness_error")
    assert [x["reason"].split(";")[0] for x in relaunch] == ["cause hook_host"]
    assert [x["gate_reason"] for x in refused] == ["hook_host_dead"]
    assert any(a["control"] == "watchdog" and a["rule"] == "harness_error" for a in r.alerts)


def test_the_drainer_writes_every_gateway_decision_the_login_typing_included(tmp_path):
    """5.4.8, 6.2 Audit trail: the shard's drainer turns every decision into a step event: the Executor's flow input
    under its flow step, and the rig's own LOGIN typing (the secret's keys redacted, 6.9) with no flow."""
    sc = clean_scenario(TODAY)
    run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        rows = store.query("SELECT shard_id, seq, flow_id, keys, gate_decision, token_id FROM step_events "
                           "WHERE run_id = ? AND action = 'gateway_decision' ORDER BY shard_id, seq", (sc.run_id,))
        steps = store.query("SELECT shard_id, min(seq) AS first FROM step_events WHERE run_id = ? AND action = 'step' "
                            "GROUP BY shard_id", (sc.run_id,))
    login = [x for x in rows if x["flow_id"] is None]
    assert login and all(x["gate_decision"] == "emitted" and x["token_id"] is not None for x in login)
    assert any(x["keys"] == "key_down:<redacted>" for x in login)
    assert not any("0x58" in (x["keys"] or "") for x in login)          # the secret's stand-in keys never appear
    last = {}
    for x in rows:
        last[x["shard_id"]] = max(last.get(x["shard_id"], 0), x["seq"])
    assert all(s["first"] > last.get(s["shard_id"], 0) for s in steps)  # one seq per shard, no collision (8.3)


def test_a_census_rule_region_is_asserted_and_its_reads_are_stored(tmp_path, monkeypatch):
    """SD-29, 2.8.2, SD-09: a census checkpoint runs every rule region of its regions file (`checks_for`,
    `RegionRuleChecker`); a failed rule is `assertion_failed (assertion: rule.<region>.<field>)` whose signature region
    is the rule region (`AssertionResult.region`), every read goes to `oracle_extractions`, and the Invariant Checker
    is given the rule regions (`invariant_rule_regions`)."""
    from sentinel import invariants as inv
    ps = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    rel = f"regions/{ps}.yaml"
    text = (EXAMPLE / rel).read_text().replace("rule: {field: price, min: 0, min_rows: 1}",
                                               "rule: {field: price, min: 0, max: 1, min_rows: 1}")
    seen: list[tuple] = []
    real = inv.compare

    def compare(base, cand, rules, **kw):
        seen.append(tuple(r.id for r in rules.rule_regions))
        return real(base, cand, rules, **kw)

    monkeypatch.setattr(inv, "compare", compare)
    sc = clean_scenario(TODAY)
    sc = type(sc)(**{**sc.__dict__, "overlay": dict(sc.overlay, **{rel: text})})
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    (o,) = [o for o in r.outcomes if o.checkpoint_id == ps]
    assert (o.outcome, o.reason, o.assertion_ids) == ("assertion_failed", "assertion:rule.rgn.ps.mark.max",
                                                      ["rule.rgn.ps.mark.max"])
    (f,) = [f for f in r.findings if f.checkpoint_id == ps and f.kind == "assertion_failed"]
    assert f.region_id == "rgn.ps.mark"
    with Store(tmp_path / "sentinel.db") as store:
        rows = store.extractions(sc.run_id, ps)
    assert {(x.side, x.assertion_id) for x in rows} >= {("candidate", "rule.rgn.ps.mark.field"),
                                                       ("base", "rule.rgn.ps.pl-open.field")}
    assert ("rgn.ps.mark", "rgn.ps.pl-open") in seen


def test_the_default_order_chords_come_from_the_gateway_file(tmp_path):
    """5.4.7: `gateway/default_order_hotkeys.yaml` gives the shard's default order chords; a file that breaks
    L-XREF-17 fails the shard closed at `settings_attested` (`GatewayConfigError.result`)."""
    from sentinel.pipeline.config import SentinelConfig, materialize
    from sentinel.pipeline.orchestration import RigScript, order_hotkeys, shard_setup
    cfg = SentinelConfig.load(EXAMPLE)
    ctx, _ = shard_setup(cfg, run_id="run-x", run_type="pr", shard_id="run-x/1", shard_no=1, build_id="b-1",
                         workspace_id="ws.tier-a.active-trader", script=RigScript())
    from sentinel.orchestrator.gateway import Chord
    assert ctx.default_hotkeys().chords == (Chord.parse("Shift+B"), Chord.parse("Shift+S"))
    assert ctx.order_hotkeys is not None and ctx.order_hotkeys.verified
    root = materialize(EXAMPLE, {}, tmp_path / "cfg")
    (root / "gateway" / "default_order_hotkeys.yaml").unlink()
    hotkeys, err = order_hotkeys(SentinelConfig.load(root))
    assert hotkeys is None and err.result.run_status == "aborted_harness" and err.result.gate == "settings_attested"


# -- redaction, secrets and egress (3.6, 6.9, 6.10) ----------------------------------------------------------------

def test_every_stored_image_is_redacted_under_the_runs_redaction_version(tmp_path):
    """3.6, 8.4: images are diffed and stored only after the redaction stage (`redact_pair`); every artifact carries
    the run's redaction version, and run health counts the boxes per side."""
    from sentinel.capture import RedactionList, Sweep, redaction_version
    sc = clean_scenario(TODAY)
    run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    want = redaction_version(RedactionList.load(EXAMPLE / "redaction" / "anchors.yaml", EXAMPLE),
                             Sweep(("D-20481133", "D-20481134")))
    with Store(tmp_path / "sentinel.db") as store:
        versions = {r[0] for r in store.conn.execute("SELECT redaction_version FROM artifacts")}
        health = store.run_summary(sc.run_id)["health"]
    assert versions == {want} and want.startswith("redaction-")
    assert set(health["redaction"]) == {"base", "candidate"}


def test_an_identifier_outside_the_anchors_is_a_redaction_miss_and_is_masked(tmp_path, monkeypatch):
    """3.6, 6.10 stage 2: an account id the anchors do not cover is filled and recorded as a `redaction_miss`
    finding; a miss on the candidate only is tier 1 and never fails the check (3.6, 9.2)."""
    from sentinel.capture import SyntheticOcr
    from sentinel.diff.regions import Rect
    real = SyntheticOcr.from_screen
    target = "census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader"

    def from_screen(screen):
        ocr = real(screen)
        label = next((c for c in screen.components if c.text and c.kind in ("label", "text")), None)
        if label is not None and getattr(screen, "_stray", False):
            ocr._words.append(("D-20481134", Rect(*label.bbox)))      # a stray id drawn over a label
        return ocr

    import sentinel.pipeline.run as pr
    real_capture = pr._Run.capture

    def capture(self, cp, side, tick=0):
        cap = real_capture(self, cp, side, tick)
        if cp.checkpoint_id == target and side == "candidate":
            cap.screen._stray = True
        return cap

    monkeypatch.setattr(pr._Run, "capture", capture)

    monkeypatch.setattr(SyntheticOcr, "from_screen", staticmethod(from_screen))
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    (miss,) = [f for f in r.findings if f.kind == "redaction_miss"]
    assert miss.evidence["pattern"] == "literal_id" and "D-20481134" not in miss.description
    assert (miss.checkpoint_id, miss.side, miss.tier, miss.evidence["candidate_only"]) == (target, "candidate", 1, True)
    assert r.status == "complete"
    # the miss's box is filled on both sides (3.6), so the label under it no longer differs; no failure follows
    assert {o.outcome for o in r.outcomes if o.checkpoint_id == target} <= {"intended", "identical"}
    assert r.commit_status["state"] == "success"


def test_a_redaction_landmark_on_one_side_only_stores_nothing(tmp_path, monkeypatch):
    """6.10: a redaction landmark found on one capture of the pair ends the checkpoint `landmark_missing (reason:
    redaction_landmark:<id>)`, and nothing of it is stored."""
    import sentinel.pipeline.run as pr
    from sentinel.capture import RedactionResult
    from sentinel.diff.engine import Terminal
    target = "census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader"
    real = pr.redact_pair

    def redact(base, cand, anchors, ocr, theme, *, sweep=None, context=None, params=None):
        if context is not None and context.checkpoint_id == target:
            return RedactionResult("redaction-x", None, None, terminal=Terminal(
                "landmark_missing", "redaction_landmark:lm.position-statement-header", side="candidate",
                detail="base score 0.990/0.400, candidate score 0.310/0.290"))
        return real(base, cand, anchors, ocr, theme, sweep=sweep, context=context, params=params)

    monkeypatch.setattr(pr, "redact_pair", redact)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    (o,) = [o for o in r.outcomes if o.checkpoint_id == target]
    assert (o.outcome, o.reason) == ("landmark_missing", "redaction_landmark:lm.position-statement-header")
    assert o.artifacts in (None, {})
    manifest = (tmp_path / "artifacts" / "runs" / sc.run_id / "manifest.json").read_text()
    assert target not in manifest


def test_a_leaked_secret_withholds_the_report(tmp_path, monkeypatch):
    """6.9: a results batch holding a secret's plaintext stops the uploads (`check_upload`); the run is
    `aborted_harness (reason: secret_leaked)` and its report is withheld: no PR comment, no report page."""
    import sentinel.pipeline.run as pr
    sc = clean_scenario(TODAY)
    monkeypatch.setattr(pr, "SIM_SECRET", f"{sc.run_id}/1")          # a value the records do carry (a shard id)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "secret_leaked")
    assert not (tmp_path / "pr-comment.md").exists() and not (tmp_path / "report.html").exists()
    assert r.commit_status["state"] == "error" and "withheld" in r.commit_status["description"]
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(sc.run_id)["secret_scan"]["report_withheld"] is True


def test_the_live_reviewer_is_built_over_the_runs_egress_gate(tmp_path):
    """6.10: one egress gate per run, over the signed allowlist, handed to the live Reviewer's job
    (`live_factory`, `pipeline.live.live_job(gate=)`); its allowlist names the model endpoint and no other host."""
    from sentinel.egress import EgressGate
    from sentinel.reviewer import ReviewerConfig, ReviewerJob
    from test_integration import FakeTransport
    gates: list = []

    def factory(gate):
        gates.append(gate)
        return ReviewerJob(ReviewerConfig(sync_concurrency=1), FakeTransport(), "pr")

    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY, live_factory=factory)
    (gate,) = gates
    assert isinstance(gate, EgressGate) and gate.run_id == sc.run_id and r.reviewer_calls
    assert gate.allowlist.entry("api.anthropic.com") is not None and gate.allowlist.entry("evil.example.com") is None


def test_the_nightly_redaction_canary_m19_is_detected(tmp_path):
    """12.5 M19, 6.10: the planted account id goes through `redact_pair`; the stage masks it with a `redaction_miss`
    box, so the canary is detected (no longer listed as not simulated)."""
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.run import NOT_SIMULATED, NightlyScenario, run_nightly
    from sentinel.pipeline.sim import SimBuild
    n = run_nightly(EXAMPLE, NightlyScenario("nightly", "run-2026-11-10-nightly-01", SimBuild(BASE_ID),
                                             overlay=dict([_main_build()]), mutants=("M19",)), tmp_path)
    (res,) = n.canary["report"].results
    assert (res.mutant_id, res.state) == ("M19", "detected") and "M19" not in NOT_SIMULATED


# -- store integrity (4.13, 8.3, 1.4 G2) ----------------------------------------------------------------------------

def test_records_carry_the_build_pair_their_ids_and_what_the_run_measured(tmp_path):
    """8.3: the build pair is registered before the run row (`register_pair`) and every record carries its id, an
    `oc-<run>-<seq>` outcome id, the entitlement profile, and for a capture its rig, settle time, changed pixels and
    masked share; the store runs strict (L-OUT-02): every record the pipeline writes is complete."""
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        (pair,) = store.query("SELECT pair_id, pair_kind, candidate_build_id FROM build_pairs")
        run_pair = store.query("SELECT pair_id FROM runs WHERE run_id = ?", (sc.run_id,))[0][0]
        outs = store.outcomes(sc.run_id)
        assert all(store.record_errors(o) == [] for o in outs)
    assert (pair["pair_kind"], pair["candidate_build_id"], run_pair) == ("pr", sc.candidate.build_id, pair["pair_id"])
    assert all(o.pair_id == str(pair["pair_id"]) and o.outcome_id.startswith(f"oc-{sc.run_id}-") for o in outs)
    assert len({o.outcome_id for o in outs}) == len(outs) and {o.profile_id for o in outs} == {"margin"}
    captured = [o for o in outs if o.artifacts]
    assert all(o.cand_rig_id and o.base_rig_id == o.cand_rig_id and o.settle_ms and o.masked_pct is not None
               for o in captured)
    assert any(o.diff_pixels for o in captured if o.outcome == "intended") and r.status == "complete"


def test_the_summary_carries_the_full_matrix_and_g2_passes(tmp_path):
    """1.4 G2, 2.12: the summary's coverage holds one cell per surface x persona x profile and its dimensions (one
    entitlement profile per fixture account kind, `coverage.fixture_profiles`: margin and IRA); the post-run check
    (`store.checks.post_run_check`) passes before the report is written."""
    from sentinel.records import PERSONAS
    from sentinel.store.checks import post_run_check
    sc = clean_scenario(TODAY)
    run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        cov = store.run_summary(sc.run_id)["coverage"]
        assert post_run_check(store, sc.run_id, cov["matrix"]["surfaces"], len(PERSONAS), 2) == []
    assert cov["matrix"] == {"surfaces": 8, "personas": len(PERSONAS), "profiles": 2}
    assert len(cov["cells"]) == 8 * len(PERSONAS) * 2
    assert {c["entitlement"] for c in cov["cells"]} == {"margin", "ira"}


def test_a_failed_post_run_check_ends_the_run_and_withholds_the_report(tmp_path, monkeypatch):
    """4.13 L-OUT-07, 1.4 G2: a post-run check failure ends the run `aborted_harness (reason:
    taxonomy_check_failed)` with the report withheld: the comment is the abort block alone."""
    import sentinel.pipeline.run as pr
    monkeypatch.setattr(pr, "post_run_check", lambda store, run_id, *a, **k: ["G2 matrix: 39 cells, expected 40"])
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "taxonomy_check_failed")
    text = (tmp_path / "pr-comment.md").read_text()
    assert "G2 matrix: 39 cells, expected 40" in text and "**Unexpected (" not in text
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(sc.run_id)["post_run_check"]["report_withheld"] is True


# -- safety persistence (6.4 to 6.12) -------------------------------------------------------------------------------

def test_a_safety_abort_persists_its_incident_block_and_disable_and_the_next_run_is_refused(tmp_path):
    """6.4, 6.6, 6.12: `apply_safety_actions` persists the live-identity abort: an S1 incident per rig (its id in
    the abort block and the commit status), the blocked login and the disabled rig; a later run finds no rig it may
    use (`refuse_blocked`) and is never created."""
    import pytest
    from sentinel.demo import paper_mode_scenario
    from sentinel.pipeline.run import RigsRefused
    from sentinel.store import safety as S
    sc = paper_mode_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_safety", "live_identity")
    with Store(tmp_path / "sentinel.db") as store:
        incidents = S.incidents(store, run_id=sc.run_id)
        blocked = store.query("SELECT login_alias, blocked_reason FROM logins WHERE blocked_at IS NOT NULL")
    assert incidents and {i.severity for i in incidents} == {"S1"}
    assert {b["blocked_reason"] for b in blocked} == {"live_identity"} and len(blocked) == 4
    first = min(i.incident_id for i in incidents)
    assert f"Incident: {first}" in r.comment or any(f"Incident: {i.incident_id}" in r.comment for i in incidents)
    nxt = clean_scenario(TODAY, seq=2, clock="12:00:00")
    with pytest.raises(RigsRefused):
        run_pipeline(EXAMPLE, nxt, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_outcome(nxt.run_id) is None


def test_a_quarantined_flow_is_cut_from_the_next_runs_scope(tmp_path):
    """6.6, 10.5: an open safety quarantine on a flow (here the attempted-send scenario's) makes that flow
    `not_run (reason: quarantine)` in the next run on the store (`quarantined_flow`)."""
    from sentinel.demo import STOCK_FLOW, attempted_send_scenario, backend_scenario
    from sentinel.store import safety as S
    a = run_pipeline(EXAMPLE, attempted_send_scenario(TODAY), tmp_path, today=TODAY)
    assert a.status == "aborted_safety"
    with Store(tmp_path / "sentinel.db") as store:
        assert [q.target for q in S.open_quarantines(store) if q.target_kind == "flow"] == [STOCK_FLOW]
    b = run_pipeline(EXAMPLE, backend_scenario(TODAY), tmp_path, today=TODAY)
    stock = [o for o in b.outcomes if o.flow_id == STOCK_FLOW]
    assert stock and {(o.outcome, o.reason) for o in stock} == {("not_run", "quarantine")}


def _nightly(tmp_path, run_id: str = "run-2026-11-10-nightly-01"):
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.run import NightlyScenario, run_nightly
    from sentinel.pipeline.sim import SimBuild
    return run_nightly(EXAMPLE, NightlyScenario("nightly", run_id, SimBuild(BASE_ID), overlay=dict([_main_build()]),
                                                mutants=("M01",)), tmp_path)


def test_the_nightly_runs_the_safety_canaries_the_denied_action_test_and_the_fire_drill(tmp_path):
    """10.3, 6.5, 6.8: the Nightly runs the attestation, identity, proof and secret-scan canaries, the denied-action
    test and the fire drill; their `canary_results` rows (kinds `safety_canary` and `fire_drill`) are written, and the
    next PR run's summary names that Nightly for its Safety section."""
    n = _nightly(tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        rows = store.canary_results(n.run_id)
        safety = store.run_summary(n.run_id)["safety"]
    kinds = {r.kind for r in rows}
    assert {"safety_canary", "fire_drill"} <= kinds
    assert all(r.state == "detected" for r in rows if r.kind in ("safety_canary", "fire_drill"))
    assert len([r for r in rows if r.kind == "fire_drill"]) == 5 and safety["banner"] is None
    pr = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(pr.run_id)["safety"] == {"banner": None, "nightly_run_id": n.run_id}
    assert "Fire drill (last Nightly)" in pr.comment and "Safety canaries (last Nightly)" in pr.comment


def test_a_blind_safety_canary_raises_the_banner_and_quarantines_the_rigs_flows(tmp_path, monkeypatch):
    """10.3, 6.4 Proof: an identity canary that misses a planted crop makes the Nightly detector_blind with the
    safety banner, and its result persists a quarantine of every flow on the rig (`apply_safety_actions`)."""
    import sentinel.pipeline.run as pr
    from sentinel.orchestrator.rig import SelectorRead
    from sentinel.store import safety as S
    real = pr.sc_mod.identity_canary

    def blind(allowlist, **kw):
        return real(allowlist, read=lambda variant: SelectorRead(allowlist.entries[0], crop_sha256="0" * 64))

    monkeypatch.setattr(pr.sc_mod, "identity_canary", blind)
    n = _nightly(tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        summary = store.run_summary(n.run_id)
        quarantines = S.open_quarantines(store)
    assert summary["detector_state"] == "detector_blind" and summary["safety"]["banner"].startswith("DETECTOR BLIND")
    assert any(q.target_kind == "rig" and q.reason == "safety_canary" for q in quarantines)
