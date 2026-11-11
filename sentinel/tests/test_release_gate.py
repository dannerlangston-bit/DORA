"""The release gate: policy/release_gate.yaml, the RC deadlines and the G-N conditions (10.9, 11.1 PA-14, 11.2, 11.5,
9.8, 9.12, 10.3, 10.5, 10.11; SD-93; L-XREF-23), `sentinel.release_gate`.

`World` builds the store an RC sign-off reads; test_report_rc.py uses it too."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from sentinel import release_gate as rg
from sentinel.canary import CanaryResult, DetectorState
from sentinel.lint import Repo, lint
from sentinel.lint.checks import release_gate as rg_lint
from sentinel.lint.rules import check_modules
from sentinel.records import CheckpointOutcome, Finding
from sentinel.sla import BusinessClock
from sentinel.store import RunPlan, ScopeEntry, Store
from sentinel.store.store import PostRunProof, ShardPlan

NY = ZoneInfo("America/New_York")
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
WS = "ws.tier-a.active-trader"
SOAK_WS = "ws.tier-b.active-dense"
T1 = f"census/trade.active-trader.ladder/{WS}"
T1B = f"census/trade.order-entry.stock/{WS}"
SOAK = f"flow/{SOAK_WS}/soak/c01"
SW = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
RELEASED = "released-2026.11.0-1111111"
RC_BUILD = "rc-2026.12.0-2222222"
POLICY = rg.ReleaseGatePolicy(trains=(rg.Train("2026.12", date(2026, 12, 10), date(2026, 12, 1)),))
NOW = datetime(2026, 12, 7, 12, tzinfo=NY)                    # G-N of train 2026.12 is Monday 2026-12-07
RC_CUT = datetime(2026, 12, 1, 9, tzinfo=NY)


def ny(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def utc(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


class World:
    """An RC run, the last Nightly and the rows the G-N conditions read; `green()` passes every condition."""

    def __init__(self, store: Store):
        self.s = store
        store.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
        store.register_build(RELEASED, channel="released", source_commit="1111111", pipeline_artifact_url="u",
                             installer_sha256="0" * 64, first_seen_at="2026-10-01T00:00:00Z")

    def rc(self, run_id: str = "rc1", build: str = RC_BUILD, started: datetime = RC_CUT,
           scope: tuple[ScopeEntry, ...] | None = None, train: str | None = "2026.12") -> str:
        if not self.s.query("SELECT 1 FROM builds WHERE build_id = ?", (build,)):
            self.s.register_build(build, channel="rc", source_commit=build[-7:], pipeline_artifact_url="u",
                                  installer_sha256="0" * 64, first_seen_at=utc(started - timedelta(hours=1)))
        pair = self.s.register_pair(build, RELEASED, "rc")
        scope = scope if scope is not None else (
            ScopeEntry(T1, WS, "active_trader", 1, "frozen"), ScopeEntry(T1B, WS, "active_trader", 1, "frozen"),
            ScopeEntry(SOAK, SOAK_WS, "active_trader", 1, "masked_live", flow_class="standard"),
            ScopeEntry(SW, WS, "active_trader", 2, "frozen"))
        self.s.begin_run(RunPlan(run_id=run_id, run_type="rc", config_commit="c0ffee", config_tarball_sha256="t",
                                 snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1.md",
                                 budget_sec=28800, scope=scope, shards=(ShardPlan(f"{run_id}/1", "rig-01"),),
                                 pair_id=pair, started_at=utc(started)))
        if train is not None:
            rg.record_deadlines(self.s, run_id, POLICY.rc_deadlines(train, cut_at=started))
        return run_id

    def put(self, run_id: str, cid: str, outcome: str, reason: str | None = None, **kw) -> None:
        ws = SOAK_WS if "/soak/" in cid else WS
        flow = {"flow_id": "soak", "step_id": cid.rsplit("/", 1)[1]} if "/soak/" in cid else {}
        tier = 2 if cid == SW else 1
        self.s.write_checkpoint(CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, reason=reason,
                                                  workspace_id=ws, persona="active_trader", data_mode="frozen",
                                                  tier=tier, **flow, **kw), shard_id=f"{run_id}/1")

    def finding(self, run_id: str, fid: str, cid: str = T1, kind: str = "collateral", tier: int = 1,
                category: str | None = "collateral", **kw) -> None:
        self.s.write_finding(Finding(fid, run_id, kind, f"{kind} on {cid}", run_id, checkpoint_id=cid, tier=tier,
                                     region_id="rgn.ladder.viewport", phash="0123456789abcdef", category=category,
                                     created_at=utc(ny(2026, 12, 1, 14)), **kw))

    def cell(self, run_id: str, surface: str, state: str = "covered", reason: str | None = None,
             entitlement: str = "margin", tier: int = 1) -> None:
        self.s.write_coverage_cells([{"run_id": run_id, "surface_id": surface, "persona": "active_trader",
                                      "entitlement": entitlement, "tier": tier, "state": state,
                                      "not_run_reason": reason, "worst_outcome": "identical" if state == "covered"
                                      else None, "checkpoint_count": 1 if state == "covered" else 0}])

    def proof(self, run_id: str, verdict: str = "proven_no_sends", reason: str | None = None,
              verified_by: str | None = None) -> None:
        self.s.write_proof(PostRunProof(f"{run_id}/1", run_id, "login-01", verdict, "risk-001", {}, "not_needed",
                                        3, 0, 10, 0, utc(ny(2026, 12, 1, 17)), reason=reason,
                                        verified_by=verified_by, verified_at=utc(NOW) if verified_by else None))

    def finish(self, run_id: str, outcome: str = "complete", reason: str | None = None,
               summary: dict | None = None) -> None:
        self.s.mark_shard_imported(f"{run_id}/1")
        self.s.update("runs", {"run_id": run_id}, {"lifecycle": "finished", "outcome": outcome,
                                                   "abort_reason": reason, "finished_at": utc(ny(2026, 12, 1, 17))})
        if summary:
            self.s.update_summary(run_id, summary)

    def nightly(self, run_id: str = "n1", *, day: int = 6, state: str = "ok", drill: str = "pass",
                divergence: bool = False, write_state: bool = True) -> str:
        self.s.begin_run(RunPlan(run_id=run_id, run_type="nightly", config_commit="c0ffee",
                                 config_tarball_sha256="t", snapshot_set_sha256="s", harness_sha="h",
                                 authorization_ref="auth/v1.md", budget_sec=14400, scope=(),
                                 started_at=utc(ny(2026, 12, day, 22))))
        rows = [CanaryResult(f"drill-{i}", "v1", "drill", "fire_drill", False, 0, ["blocked"], "detected",
                             kind="fire_drill") for i in (1, 2)]
        if drill == "failed":
            rows[1] = CanaryResult("drill-2", "v1", "drill", "fire_drill", False, 0, ["blocked"], "missed",
                                   produced_outcome="harness_error", produced_reason="drill_failed:2",
                                   kind="fire_drill")
        if drill != "none":
            self.s.write_canary_results(run_id, rows)
        if write_state:
            self.s.set_system_state("detector_state", DetectorState(state, run_id, divergence=divergence).to_json())
        outcome, reason = ("aborted_safety", "drill_order_placed") if drill == "placed" else ("complete", None)
        self.s.update("runs", {"run_id": run_id}, {"lifecycle": "finished", "outcome": outcome,
                                                   "abort_reason": reason,
                                                   "finished_at": utc(ny(2026, 12, day + 1, 2))})
        return run_id

    def green(self, run_id: str = "rc1", **rc) -> str:
        """An RC run on which every G-N condition passes."""
        self.rc(run_id, **rc)
        for cid in (T1, T1B, SOAK, SW):
            self.put(run_id, cid, "identical")
        self.cell(run_id, "trade.active-trader.ladder")
        self.cell(run_id, "trade.order-entry.stock")
        self.cell(run_id, "trade.order-entry.stock", "not_run", "entitlement", entitlement="ira")
        self.proof(run_id)
        self.finish(run_id)
        if not self.s.query("SELECT 1 FROM runs WHERE run_type = 'nightly'"):
            self.nightly()
        return run_id


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    yield s
    s.close()


@pytest.fixture
def world(store):
    return World(store)


def keys_failing(result: rg.GateResult) -> list[str]:
    return [c.key for c in result.failing]


# -- the deadlines (choices 1 and 2) --------------------------------------------------------------------------------

def test_ga_on_thursday_with_n3_signs_off_monday_and_the_start_deadline_is_8_hours_earlier():
    d = POLICY.rc_deadlines("2026.12")
    assert d.ga_date == date(2026, 12, 10) and d.business_days == 3 and d.assumed == ()
    assert d.signoff_at == ny(2026, 12, 7, 17) and d.signoff_date == date(2026, 12, 7)
    assert d.start_deadline == ny(2026, 12, 7, 9) == d.signoff_at - timedelta(hours=8)
    assert d.header_line() is None
    assert d.line() == ("Train 2026.12: GA 2026-12-10, RC cut 2026-12-01, N = 3 business days, sign-off due "
                        "Mon 2026-12-07 17:00 EST, RC start deadline Mon 2026-12-07 09:00 EST")


def test_exchange_and_firm_holidays_are_skipped():
    p = rg.ReleaseGatePolicy(trains=(rg.Train("t", date(2026, 11, 30), date(2026, 11, 18)),))
    assert p.rc_deadlines("t").signoff_date == date(2026, 11, 24)        # Thanksgiving, Thursday 26, skipped
    firm = BusinessClock(holidays=frozenset({date(2026, 12, 7)}))
    assert POLICY.rc_deadlines("2026.12", clock=firm).signoff_date == date(2026, 12, 4)
    assert rg.business_days_after(date(2026, 11, 19), 7) == date(2026, 12, 1)


def test_a_train_overrides_n_and_the_cut_time_replaces_the_planned_cut():
    p = rg.ReleaseGatePolicy(trains=(rg.Train("2027.01", date(2027, 1, 14), date(2027, 1, 5), 4),))
    d = p.rc_deadlines("2027.01", cut_at=ny(2027, 1, 6, 9))
    assert (d.business_days, d.signoff_date, d.rc_cut_date) == (4, date(2027, 1, 8), date(2027, 1, 6))
    assert p.business_days("other") == 3


def test_no_file_assumes_n3_and_an_rc_cut_at_g7_and_says_so(tmp_path):
    p = rg.load(tmp_path / "policy" / "release_gate.yaml")
    assert not p.present
    d = rg.rc_deadlines(p, "2026.12", cut_at=ny(2026, 11, 19, 9))
    assert d.assumed == ("N = 3", "RC cut at G-7") and d.source == "missing_file"
    assert (d.ga_date, d.signoff_date) == (date(2026, 12, 1), date(2026, 11, 25))
    assert d.header_line() == "N = 3 and RC cut at G-7 are assumed: no policy/release_gate.yaml"
    unlisted = POLICY.rc_deadlines("2027.02", cut_at=ny(2027, 2, 1, 9))
    assert unlisted.assumed == ("RC cut at G-7",)
    assert unlisted.header_line() == "RC cut at G-7 is assumed: train 2027.02 is not in policy/release_gate.yaml"
    with pytest.raises(rg.ReleaseGateError, match="no GA date"):
        POLICY.rc_deadlines("2027.02")


def test_the_deadlines_resolved_at_rc_cut_stay_on_the_run(world, store):
    world.rc()
    kept = rg.stored_deadlines(store, "rc1")
    assert kept == POLICY.rc_deadlines("2026.12", cut_at=ny(2026, 12, 1, 9))
    assert kept is not None and store.run_summary("rc1")["release_gate"]["signoff_at"] == "2026-12-07T22:00:00+00:00"


# -- the file and L-XREF-23 -----------------------------------------------------------------------------------------

def test_the_example_policy_loads_and_lints_clean():
    p = rg.load(EXAMPLE / "policy" / "release_gate.yaml")
    assert p.present and p.owner == "release.manager" and [t.train for t in p.trains] == ["2026.12", "2027.01"]
    assert p.business_days("2027.01") == 4
    report = lint(EXAMPLE, today=TODAY)
    assert not [v for v in report.violations if v.rule == "L-XREF-23"] and "L-XREF-23" not in report.skipped
    assert rg_lint in check_modules()


def lint_run(tmp_path: Path, doc: object | None) -> list[tuple[str, str, str]]:
    if doc is not None:
        p = tmp_path / rg.POLICY_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
    out: list[tuple[str, str, str]] = []
    rg_lint.check(Repo.load(tmp_path), lambda rule, file, message, path="", severity=None:
                  out.append((rule, path, message)), 0, TODAY)
    return out


def test_l_xref_23_names_duplicate_trains_low_n_and_a_cut_too_late(tmp_path):
    doc = {"schema_version": 1, "default_business_days": 0, "owner": "rm", "trains": [
        {"train": "a", "ga_date": "2026-12-10", "rc_cut_date": "2026-12-07"},
        {"train": "a", "ga_date": "2026-12-10", "rc_cut_date": "2026-12-01", "business_days": 0}]}
    found = lint_run(tmp_path, doc)
    assert [(r, p) for r, p, _ in found] == [("L-XREF-23", "default_business_days"),
                                              ("L-XREF-23", "trains[0].rc_cut_date"), ("L-XREF-23", "trains[1].train"),
                                              ("L-XREF-23", "trains[1].business_days")]
    assert "not earlier than GA 2026-12-10 minus 3 business days (2026-12-07, XNYS)" in found[1][2]
    with pytest.raises(rg.ReleaseGateError, match="listed twice"):
        rg.from_doc(doc)
    assert lint_run(tmp_path / "empty", None) == []                   # no file: assumed, not a violation


# -- the four-input RC gate (1.5, 9.11, 11.5) ------------------------------------------------------------------------

def test_rc_gate_blocks_on_each_input_and_passes_otherwise():
    ok = dict(quarantine_share=0.02, detector_state="ok", tier1_coverage=100.0, tier1_needs_baseline=0)
    assert rg.rc_gate(**ok) == rg.RcGate(False, ())
    assert rg.rc_gate(**{**ok, "quarantine_share": 0.021}).reasons == (
        "quarantine 2.1 percent of tier-1 checkpoints, above 2 percent",)
    assert rg.rc_gate(**{**ok, "detector_state": "detector_degraded"}).reasons == (
        "canary detector state detector_degraded",)
    assert rg.rc_gate(**{**ok, "tier1_coverage": 99.0}).reasons == ("tier-1 coverage 99.0 percent, below 100 percent",)
    assert rg.rc_gate(**{**ok, "tier1_needs_baseline": 2}).blocked
    assert rg.rc_gate(**{**ok, "tier1_coverage": None}).blocked


# -- the G-N conditions (10.9) ----------------------------------------------------------------------------------------

def test_all_green_passes_with_evidence_for_every_condition(world, store):
    world.green()
    result = rg.evaluate(store, "rc1", now=NOW)
    assert result.passed and [c.key for c in result.conditions] == [k for k, _ in rg.CONDITIONS]
    assert all(c.evidence for c in result.conditions)
    ev = {c.key: c.evidence for c in result.conditions}
    assert ev["detector"] == ("detector_state ok (Nightly n1)",)
    assert ev["coverage"][0] == ("tier 1: 2 of 2 cells covered (100.0 percent); 1 explained not_run "
                                 "(entitlement or scope)")
    assert ev["quarantine"] == ("0 of 3 tier-1 checkpoints not_run (quarantine): 0.0 percent (ceiling 2 percent)",)
    assert ev["fire_drill"] == ("Nightly n1: pass: 2 of 2 items blocked",)
    assert ev["proof"] == ("PROVEN on 1 of 1 shards",)
    assert ev["soak"] == ("1 soak cycles within budget",)
    assert result.deadlines == POLICY.rc_deadlines("2026.12", cut_at=ny(2026, 12, 1, 9))
    assert result.checklist()[0] == "[pass] RC run finished and not superseded"


def test_detector_degraded_or_a_stale_or_diverged_or_pending_canary_blocks(world, store):
    world.green()
    world.nightly("n2", day=7, state="detector_degraded")
    c = rg.evaluate(store, "rc1", now=NOW).condition("detector")
    assert not c.passed and c.evidence == ("detector_state detector_degraded (Nightly n2)",)
    store.set_system_state("detector_state", DetectorState("ok", "n1").to_json())
    assert rg.evaluate(store, "rc1", now=NOW).condition("detector").evidence == (
        "detector_state was written by Nightly n1, not by the last Nightly n2",)
    store.set_system_state("detector_state", DetectorState("ok", "n2", divergence=True).to_json())
    store.set_system_state("canaries_pending", "last Nightly skipped for RC run rc1")
    assert rg.evaluate(store, "rc1", now=NOW).condition("detector").evidence == (
        "path equivalence diverged in Nightly n2", "canaries pending: last Nightly skipped for RC run rc1")


def test_a_canary_path_divergence_row_in_the_last_nightly_blocks(world, store):
    world.green()
    store.begin_run(RunPlan(run_id="n2", run_type="nightly", config_commit="c0ffee", config_tarball_sha256="t",
                            snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1.md",
                            budget_sec=14400, scope=(ScopeEntry(T1, WS, "active_trader", 1, "frozen"),),
                            shards=(ShardPlan("n2/1", "rig-01"),), started_at=utc(ny(2026, 12, 7, 22))))
    store.write_checkpoint(CheckpointOutcome(run_id="n2", checkpoint_id=T1, outcome="harness_error",
                                             reason="canary_path_divergence", workspace_id=WS,
                                             persona="active_trader", data_mode="frozen", tier=1,
                                             detail="watchdog event 7"), shard_id="n2/1")
    store.update("runs", {"run_id": "n2"}, {"lifecycle": "finished", "outcome": "complete_with_env_errors"})
    store.set_system_state("detector_state", DetectorState("ok", "n2").to_json())
    c = rg.evaluate(store, "rc1", now=NOW).condition("detector")
    assert c.evidence == ("Nightly n2 has 1 harness_error (reason: canary_path_divergence) rows",)


def test_an_untriaged_tier1_collateral_blocks_and_a_disposition_clears_it(world, store):
    world.rc()
    for cid in (T1B, SOAK, SW):
        world.put("rc1", cid, "identical")
    world.put("rc1", T1, "collateral")
    c = rg.evaluate(store, "rc1", now=NOW).condition("dispositions")
    assert c.evidence == (f"{T1}: collateral, no finding and no decision",)
    world.finding("rc1", "F-1")
    c = rg.evaluate(store, "rc1", now=NOW).condition("dispositions")
    assert c.evidence == ("finding F-1: untriaged tier-1 item at RC sign-off",)
    store.update("findings", {"natural_key": "F-1"}, {"triage_state": "confirmed_bug", "ticket_ref": "TOS-1"})
    assert rg.evaluate(store, "rc1", now=NOW).condition("dispositions").passed


def test_an_unproven_shard_proof_blocks_even_when_cleared_by_hand(world, store):
    world.rc()
    assert rg.evaluate(store, "rc1", now=NOW).condition("proof").evidence == ("shard rc1/1: no post-run proof",)
    world.proof("rc1", "unproven", reason="Working unreadable", verified_by="asmith")
    c = rg.evaluate(store, "rc1", now=NOW).condition("proof")
    assert not c.passed
    assert c.evidence == ("shard rc1/1: unproven (Working unreadable), cleared by hand by asmith",)


def test_an_unexplained_tier1_cell_or_no_matrix_fails_coverage(world, store):
    world.green()
    world.cell("rc1", "trade.positions", "not_run", "budget")
    c = rg.evaluate(store, "rc1", now=NOW).condition("coverage")
    assert not c.passed and c.evidence[1] == "trade.positions x active_trader x margin: not_run (budget)"
    assert c.evidence[0].startswith("tier 1: 2 of 3 cells covered (66.7 percent)")
    world.green("rc2", build="rc-2026.12.1-3333333")
    store.conn.execute("DELETE FROM coverage_cells WHERE run_id = 'rc2'")
    assert rg.evaluate(store, "rc2", now=NOW).condition("coverage").evidence == (
        "no coverage matrix stored for the RC run (coverage_cells, 2.12)",)


def test_quarantine_above_2_percent_of_tier1_checkpoints_blocks(world, store):
    def scope(n):
        return tuple(ScopeEntry(f"census/s{i:02d}.surface/{WS}", WS, "active_trader", 1, "frozen") for i in range(n))
    for run_id, n in (("q49", 49), ("q50", 50)):
        world.rc(run_id, build=f"rc-2026.12.{n}-{n:07d}", scope=scope(n))
        store.write_checkpoint(CheckpointOutcome(run_id=run_id, checkpoint_id=f"census/s00.surface/{WS}",
                                                 outcome="not_run", reason="quarantine", workspace_id=WS,
                                                 persona="active_trader", data_mode="frozen", tier=1),
                               shard_id=f"{run_id}/1")
    q49, q50 = rg.quarantine_share(store, "q49"), rg.quarantine_share(store, "q50")
    assert (q49.quarantined, q49.checkpoints, q50.checkpoints) == (1, 49, 50)
    c49 = rg.evaluate(store, "q49", now=NOW).condition("quarantine")
    assert not c49.passed and c49.evidence == (
        "1 of 49 tier-1 checkpoints not_run (quarantine): 2.0 percent (ceiling 2 percent)",)
    assert rg.evaluate(store, "q50", now=NOW).condition("quarantine").passed


def test_a_pending_tier1_candidate_or_an_unapproved_needs_baseline_blocks(world, store):
    world.rc()
    for cid in (T1, SOAK, SW):
        world.put("rc1", cid, "identical")
    world.put("rc1", T1B, "needs_baseline", "new_surface")
    world.finish("rc1")
    store.insert("baselines", {"checkpoint_id": T1, "lineage": "released", "build_id": RELEASED,
                               "image_sha256": "a" * 64, "state": "candidate", "source_run_id": "rc1",
                               "proposed_at": utc(NOW)})
    c = rg.evaluate(store, "rc1", now=NOW).condition("baselines")
    assert c.evidence == (f"{T1}: candidate #1 from run rc1 pending",
                          f"{T1B}: needs_baseline (reason: new_surface), no approved baseline on released")
    assert rg.evaluate(store, "rc1", now=NOW, lineage="other").condition("baselines").evidence == (
        f"{T1B}: needs_baseline (reason: new_surface), no approved baseline on other",)


def test_soak_breach_needs_a_disposition_and_a_soak_that_did_not_run_never_passes(world, store):
    world.rc()
    for cid in (T1, T1B, SW):
        world.put("rc1", cid, "identical")
    world.put("rc1", SOAK, "assertion_failed", "assertion:soak.gdi_objects")
    world.finding("rc1", "F-soak", cid=SOAK, kind="assertion_failed", category="assertion_failed")
    c = rg.evaluate(store, "rc1", now=NOW).condition("soak")
    assert c.evidence == ("1 soak cycles; 1 budget breaches",
                          f"finding F-soak (assertion_failed) on {SOAK} without a disposition")
    store.update("findings", {"natural_key": "F-soak"}, {"triage_state": "confirmed_bug", "ticket_ref": "TOS-9"})
    assert rg.evaluate(store, "rc1", now=NOW).condition("soak").passed
    world.rc("rc2", build="rc-2026.12.1-3333333")
    world.put("rc2", SOAK, "not_run", "budget")
    c = rg.evaluate(store, "rc2", now=NOW).condition("soak")
    assert c.evidence[1] == "1 of 1 soak cycles did not run or did not finish: not_run (reason: budget)"
    world.rc("rc3", build="rc-2026.12.2-4444444", scope=(ScopeEntry(T1, WS, "active_trader", 1, "frozen"),))
    assert rg.evaluate(store, "rc3", now=NOW).condition("soak").evidence == (
        "soak did not run: no soak cycle checkpoint in the RC run (10.11)",)


@pytest.mark.parametrize("drill, text", [
    ("failed", "Nightly n1: harness_error (reason: drill_failed:2); quarantined: none recorded"),
    ("placed", "Nightly n1: aborted_safety (reason: drill_order_placed)"),
    ("none", "Nightly n1: no fire drill result recorded"),
])
def test_a_failed_or_missing_fire_drill_blocks(world, store, drill, text):
    world.nightly(drill=drill)
    world.green()
    c = rg.evaluate(store, "rc1", now=NOW).condition("fire_drill")
    assert not c.passed and c.evidence == (text,)


def test_a_superseded_run_cannot_be_signed(world, store):
    world.green()
    world.rc("rc2", build="rc-2026.12.1-3333333", started=ny(2026, 12, 2, 9))
    store.update("runs", {"run_id": "rc1"}, {"outcome": "aborted_budget", "abort_reason": "superseded"})
    c = rg.evaluate(store, "rc1", now=NOW).condition("run")
    assert c.evidence == ("RC run rc1 ended aborted_budget (reason: superseded): superseded by "
                          "rc-2026.12.1-3333333; it cannot be signed",)
    store.update_summary("rc1", {"superseded_by": {"run_id": "rc2", "build": "rc-2026.12.9-9999999"}})
    assert rg.superseded_by(store, "rc1") == "rc-2026.12.9-9999999"
    assert rg.superseded_by(store, "rc2") is None
    assert rg.evaluate(store, "rc2", now=NOW).condition("run").evidence == ("RC run rc2 is running",)
    assert rg.evaluate(store, "nope", now=NOW).conditions == (rg.Condition("run", False, ("unknown run nope",)),)
