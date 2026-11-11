"""The w4/w5 runtime wiring of the simulated runs (part A of the w4+w5a integration): the ops config files in the run
(authorization, budgets, rate caps, Diff Engine parameters), the decision snapshot set, preflight and the calendar,
the run alerts, the report inputs the runtime produces, and the Nightly store maintenance."""
from __future__ import annotations

import dataclasses
import json
from datetime import date

import pytest

from sentinel.demo import BASE_ID, EXAMPLE, _main_build, clean_scenario, config_invalid_scenario, paper_mode_scenario
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.simrig import MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.pipeline import run as run_mod
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.live import budgeted
from sentinel.pipeline.run import NightlyScenario, run_nightly, run_pipeline
from sentinel.pipeline.sim import SimBuild
from sentinel.policy import Budgets
from sentinel.reviewer import ReviewerConfig
from sentinel.store import Store
from sentinel.store.snapshot import snapshot_sha256

TODAY = date(2026, 11, 10)
BUDGETS = """schema_version: 1
approved_by: asmith
reviewer_items: {pr: 40, merge: 200, rc: 500}
rescue: {max_actions: 10, max_seconds: 30, max_rescued_steps_per_run: 7}
usd: {per_explorer_session: 25, per_run: 20, per_week: 500}
"""


def _row(tmp_path, run_id):
    with Store(tmp_path / "sentinel.db") as s:
        return dict(s.query("SELECT * FROM runs WHERE run_id = ?", (run_id,))[0]), s.run_summary(run_id)


# -- item 6: the ops config files (6.12, 11.4, 6.4, 4.1) --------------------------------------------------------
def test_the_run_plan_carries_the_authorization_in_force_and_the_decision_snapshot_set(tmp_path):
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path)
    row, summary = _row(tmp_path, r.run_id)
    auth = SentinelConfig.load(EXAMPLE).authorization
    assert (row["authorization_ref"], row["authorization_sha256"]) == ("authorization/v1.md", auth.sha256)
    snap = tmp_path / "snapshots" / f"{r.run_id}.db"
    assert row["snapshot_set_sha256"] == snapshot_sha256(snap)            # 8.5: the shipped file's content hash
    assert summary["provenance"]["snapshot_set_sha256"] == row["snapshot_set_sha256"]
    assert row["snapshot_set_sha256"] in summary["provenance"]["row"][1]       # 8.5's provenance text


def test_a_stale_authorization_hash_refuses_the_run_at_g1(tmp_path):
    sc = clean_scenario(TODAY)
    sc = dataclasses.replace(sc, overlay={**sc.overlay, "authorization/v1.md": "# Authorization v1\nedited after "
                                                                               "the signatures\n"})
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert any("authorization" in e and "sha256" in e for e in r.lint_errors)
    row, summary = _row(tmp_path, r.run_id)
    assert row["authorization_sha256"] is None and row["authorization_ref"].startswith("none (")
    assert summary["abort"]["rule_ids"] == ["L-XREF-20"]         # the run-start lint is G1's first half (4.1)


def test_the_authorization_refusal_is_g1s_run_context_half(tmp_path):
    import shutil
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    cfg = SentinelConfig.load(root)
    assert run_mod.authorization_refusal(cfg.repo, cfg) == []
    (root / "authorization" / "v1.md").write_text("edited\n")
    stale = SentinelConfig.load(root)
    (v,) = run_mod.authorization_refusal(stale.repo, stale)
    assert v.rule == "L-XREF-16" and "document_sha256" in v.message and stale.authorization is None


def test_budgets_and_the_authorizations_rate_caps_reach_the_orchestrator(tmp_path, monkeypatch):
    seen = []
    real = run_mod.Orchestrator

    def spy(rig, ctx, sink, **kw):
        if "counters" in kw:                             # a shard's Orchestrator (not a G1 abort's)
            seen.append((kw["counters"].caps, kw["spend"].per_run, kw["spend"].per_week))
        return real(rig, ctx, sink, **kw)
    monkeypatch.setattr(run_mod, "Orchestrator", spy)
    sc = clean_scenario(TODAY)
    caps = dict(SentinelConfig.load(EXAMPLE).authorization.gateway_rate_caps())
    caps_text = "rate_caps: {logins_per_day: 40, symbol_changes_per_hour: 600, staged_orders_per_hour: 60, " \
                "staged_orders_per_day: 300}"
    assert caps                                          # the example's document names the 6.4 caps
    sc = dataclasses.replace(sc, overlay={**sc.overlay, "policy/budgets.yaml": BUDGETS})
    run_pipeline(EXAMPLE, sc, tmp_path)
    assert seen and all(c == (caps, 20.0, 500.0) for c in seen)
    assert caps_text in (EXAMPLE / "authorization" / "v1.yaml").read_text()


def test_the_live_reviewer_takes_the_budget_caps():
    b = Budgets.from_doc({"reviewer_items": {"pr": 40}, "usd": {"per_run": 20, "per_week": 300}})
    cfg = budgeted(ReviewerConfig(), b)
    assert cfg.call_caps["pr"] == 40 and cfg.call_caps["nightly"] == ReviewerConfig().call_caps["nightly"]
    assert (cfg.run_ceiling_usd, cfg.weekly_cap_usd) == (20.0, 300.0)


def test_the_rescue_cap_comes_from_policy_budgets(tmp_path, monkeypatch):
    caps = []
    real = run_mod._ShardWork.run_shards

    def spy(self, specs):
        caps.append((self.rescue_cap.max_steps, self.rescue_cap.max_actions))
        return real(self, specs)
    monkeypatch.setattr(run_mod._ShardWork, "run_shards", spy)
    sc = clean_scenario(TODAY)
    run_pipeline(EXAMPLE, dataclasses.replace(sc, overlay={**sc.overlay, "policy/budgets.yaml": BUDGETS}), tmp_path)
    assert caps == [(7, 10)]


# -- item 7: the report inputs the runtime produces (6.3, 6.6, 3.8) -----------------------------------------------
def test_the_summary_carries_each_shards_manifest_safety_and_the_diff_time_sensitivity(tmp_path):
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path)
    _, summary = _row(tmp_path, r.run_id)
    shards = summary["safety"]["shards"]
    assert [s["shard_id"] for s in shards] == sorted(s["shard_id"] for s in r.shards)
    for s in shards:                                       # 6.3: eleven states, the profile, the evidence hashes
        att = s["attestation"]
        assert att["result"] == "passed" and len(att["rows"]) == 11 and att["profile_sha256"] and att["at"]
        assert set(att["rows"].values()) <= {"match", "not_read"} and att["rows"]["A10"] == "not_read"
        assert len(att["evidence_sha256"]) == 9 and s["cancel_all_located"] is True     # A1 to A9 (no A10 off RC)
    compared = {o.checkpoint_id for o in r.outcomes if o.diff is not None}
    assert compared and compared <= set(summary["sensitivity"])                # 3.8: one row per compared checkpoint
    row = next(iter(summary["sensitivity"].values()))
    assert isinstance(row["panels"], list) and len(row) >= 3
    with Store(tmp_path / "sentinel.db") as s:
        for p in s.post_run_proofs(r.run_id):                # 6.6: refusals counted from the shard's own rows
            refused = s.query("SELECT count(*) FROM step_events WHERE shard_id = ? AND gate_decision = 'refused' "
                              "AND drill = 0", (p.shard_id,))[0][0]
            assert p.refusals_count == refused


def test_every_attestation_result_carries_the_rows_and_hashes():
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(unreadable_reads={"A4": 2}), clock=clock)
    report = Orchestrator(rig, default_context(), MemorySink(()), clock=clock).run()
    att = report.gate("settings_attested")
    assert not att.passed and att.evidence["rows"]["A4"] == "unreadable"
    assert att.evidence["rows"]["A3"] == "match" and "hashes" in att.evidence            # 6.3, report.safety


# -- item 8: the run's alerts, jobs and freeze (9.9, 9.10, 1.5) ---------------------------------------------------
def test_a_runs_alerts_collapse_to_one_per_rule_and_go_out_through_the_outbox(tmp_path):
    r = run_pipeline(EXAMPLE, paper_mode_scenario(TODAY), tmp_path)
    assert (r.status, r.reason) == ("aborted_safety", "live_identity")
    rules = [(a["rule"], a["priority"], len(a["shards"])) for a in r.alerts]
    assert rules == [("safety:aborted_safety", "P1", 4), ("safety:unproven_proof", "P1", 4)]   # 9.10 dedup
    _, summary = _row(tmp_path, r.run_id)
    assert summary["alerts"] == r.alerts == json.loads((tmp_path / "alerts.json").read_text())
    with Store(tmp_path / "sentinel.db") as s:
        jobs = s.query("SELECT kind, destination, payload_sha256 FROM outbox WHERE run_id = ?", (r.run_id,))
    kinds = {j["kind"] for j in jobs}
    assert {"page", "slack", "jira_create"} <= kinds                            # pages, posts, the SENT task (9.9)
    assert any(j["destination"].startswith("SENT#task:") for j in jobs)
    arts = tmp_path / "artifacts"          # the run's set, or artifacts/outbox for the PR host jobs (9.1, 9.2)
    for j in jobs:                                         # every payload is the artifact its hash names
        sha = j["payload_sha256"].removeprefix("sha256:")
        assert list(arts.rglob(f"*{sha}*")), sha
    assert summary["health"]["integrations"]["jira_queued"] >= 1


def test_the_second_aborted_harness_run_in_seven_days_freezes_the_harness(tmp_path):
    first = run_pipeline(EXAMPLE, config_invalid_scenario(TODAY), tmp_path)
    with Store(tmp_path / "sentinel.db") as s:
        assert first.status == "aborted_harness" and not s.get_system_state("harness_freeze")
    sc = dataclasses.replace(config_invalid_scenario(TODAY), run_id=first.run_id.replace("-01", "-02"),
                             clock="11:31:00")
    run_pipeline(EXAMPLE, sc, tmp_path)
    with Store(tmp_path / "sentinel.db") as s:
        assert "2 aborted_harness runs within 7 days" in s.get_system_state("harness_freeze")


# -- item 5: the Nightly store maintenance (8.6, 8.9, 9.10) -------------------------------------------------------
def test_the_nightly_backs_up_sweeps_audits_and_checks_the_store(tmp_path):
    n = run_nightly(EXAMPLE, NightlyScenario("nightly", "run-2026-11-10-nightly-01", SimBuild(BASE_ID),
                                             overlay=dict([_main_build()]), mutants=("M03",)), tmp_path, TODAY)
    lines = n.canary["run_health"]["store"]
    assert lines[0].startswith("Backup ok at ") and lines[1].startswith("Retention swept 0 expired copies")
    assert lines[2].startswith("Artifact audit sample: ")                     # the first audit is due (choice 2)
    _, summary = _row(tmp_path, n.run_id)
    assert summary["health"]["store"] == lines
    labels = {label for label, _ in summary["health_rows"]}
    assert {"Retention", "Artifact audit", "Restore drill", "Preflight"} <= labels      # 12.7 on the Nightly too
    assert len(summary["preflight"]["run-2026-11-10-nightly-01/1"]) == 32
    with Store(tmp_path / "sentinel.db") as s:
        assert s.get_system_state("last_backup_at") and s.get_system_state("last_artifact_audit_at")
        jira_jobs = s.query("SELECT count(*) FROM outbox WHERE run_id = ? AND kind LIKE 'jira_%'", (n.run_id,))[0][0]
    assert any((tmp_path / "backup").iterdir())
    store_alerts = [a for a in n.alerts if a["control"] == "store_health"]
    assert [(a["rule"], a["priority"]) for a in store_alerts] == [("store_health", "P2")]   # no restore drill yet
    assert "Restore drill" in store_alerts[0]["detail"]
    assert summary["health"]["integrations"]["jira_queued"] == jira_jobs


# -- item 1: preflight, the OnDemand stamp (12.7, 7.9) ------------------------------------------------------------
def test_the_preflight_runs_every_item_with_the_one_retry_and_reports_each_shard(tmp_path):
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path)
    _, summary = _row(tmp_path, r.run_id)
    rows = [text for label, text in summary["health_rows"] if label == "Preflight"]
    assert len(rows) == len(r.shards) and all(t.endswith(": 32 of 32 items passed") for t in rows)
    for items in summary["preflight"].values():                    # 12.7: every item with its attempts
        assert len(items) == 32 and all(i["passed"] and len(i["attempts"]) == 1 for i in items)


def test_an_outage_retries_after_60_s_and_a_second_failure_is_the_gates_outcome():
    from datetime import timedelta
    from sentinel.orchestrator.simrig import sim_preflight_pins, RIG_HALVES
    from sentinel.orchestrator.ports import PreflightRead
    clock = ManualClock()
    local = {"R7": lambda: PreflightRead("R7", {"control_free_bytes": 10 ** 15, "run_bytes": 0}),
             "H7": lambda: PreflightRead("H7", {"config_commit_ok": True, "lint_hash_ok": True})}
    ctx = dataclasses.replace(default_context(), preflight_pins=sim_preflight_pins(corpus={"ws": "sha256:" + "a" * 64},
                                                                                     catalogs={}),
                              preflight_local=local)
    passing = SimulatedRig(SimScenario(preflight_down={"N3": clock.now() + timedelta(seconds=30)}), clock=clock)
    orch = Orchestrator(passing, ctx, MemorySink(()), clock=clock)
    orch.run()
    (pf,) = orch.preflight_runs
    assert [(a.item, a.attempt, a.passed) for a in pf.attempts if a.item == "N3"] == [("N3", 1, False), ("N3", 2, True)]
    assert all(pf.report.items.values()) and set(RIG_HALVES) == {"R7", "H7"}
    clock2 = ManualClock()
    down = SimulatedRig(SimScenario(preflight_down={"N3": clock2.now() + timedelta(hours=1)}), clock=clock2)
    report = Orchestrator(down, ctx, MemorySink(()), clock=clock2).run()
    assert report.gate("provisioned").outcomes[0].outcome.gate == "preflight.N3"      # 12.7: env_error subset


def test_the_allowlisted_ondemand_account_is_stamped_ondemand(monkeypatch):
    from sentinel.orchestrator.rig import SelectorRead
    seen = []
    real = Orchestrator._asserted

    def spy(self, read):
        real(self, read)
        seen.append((read.text, self._stamp["mode"]))
    monkeypatch.setattr(Orchestrator, "_asserted", spy)
    ctx = default_context()
    ctx = dataclasses.replace(ctx, allowlist=dataclasses.replace(ctx.allowlist, ondemand_account="OD-77001",
                                                                 ondemand_template="ondemand_toolbar"))
    clock = ManualClock()
    rig = SimulatedRig(SimScenario(selector_reads=[SelectorRead("OD-77001")], ondemand_toolbar=True), clock=clock)
    Orchestrator(rig, ctx, MemorySink(()), clock=clock).run()
    assert ("OD-77001", "onDemand") in seen                              # 7.9: not paperMoney inside OnDemand
    assert all(mode == "paperMoney" for text, mode in seen if text != "OD-77001")
