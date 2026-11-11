"""The 9.11 metrics, the 1.5 kill criteria, the header stamp and the 11.9 phase exits (`sentinel.metrics`)."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sentinel import metrics as M
from sentinel.records import Escape, StepEvent
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)        # Thursday 17:00 New York
CPS = {f"census/monitor.panel-{i:02d}/ws.tier-a.options-trader": (1 if i < 4 else 2, "frozen" if i % 2 == 0 else
                                                                   "masked_live") for i in range(8)}
CP = next(iter(CPS))


def ny(d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, d, hh, mm, tzinfo=NY)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


class World:
    """A store with rigs, builds and checkpoints, and helpers that write the 8.3 rows the metrics read."""

    def __init__(self, store: Store):
        self.s, self.n = store, 0
        for i in range(1, 3):
            store.register_rig(f"rig-0{i}", login_alias=f"vault://paper/rig-0{i}", owner="asmith",
                               snapshot_id="snap-1", allowlist_entry=i)
        store.register_checkpoints([ScopeEntry(c, "ws.tier-a.options-trader", "options_trader", t, m)
                                    for c, (t, m) in CPS.items()], config_commit="c1")
        for b, ch in (("main-1", "main"), ("main-2", "main")):
            store.register_build(b, channel=ch, source_commit=b, pipeline_artifact_url="u", installer_sha256="x" * 64)
        self.pair = store.register_pair("main-2", "main-1", "merge")
        self.seq: dict[str, int] = {}

    def run(self, run_type: str, started: datetime, *, minutes: float | None = None, outcome: str | None = None,
            reason: str | None = None, harness: str = "h1", config: str = "c1", shards: int = 0,
            pair: bool = False) -> str:
        self.n += 1
        rid = f"run-2026-11-{self.n:02d}-{run_type}-01"
        self.s.begin_run(RunPlan(run_id=rid, run_type=run_type, config_commit=config, config_tarball_sha256="a" * 64,
                                 snapshot_set_sha256="b" * 64, harness_sha=harness, authorization_ref="auth/v1.md",
                                 budget_sec=2700, scope=(), started_at=iso(started),
                                 pair_id=self.pair if pair else None,
                                 shards=tuple(ShardPlan(f"{rid}/{k}", f"rig-0{k}") for k in range(1, shards + 1))))
        if minutes is not None:
            self.finish(rid, minutes, outcome, reason)
        return rid

    def finish(self, rid: str, minutes: float, outcome: str | None = None, reason: str | None = None) -> None:
        """End the run and its shards `minutes` after it started; `complete` imports the shards first (8.2)."""
        started = datetime.fromisoformat(self.s.query("SELECT started_at FROM runs WHERE run_id = ?", (rid,))[0][0])
        end = iso(started + timedelta(minutes=minutes))
        for (shard,) in self.s.query("SELECT shard_id FROM shards WHERE run_id = ?", (rid,)):
            done = {"state": "imported", "imported_at": end} if outcome == "complete" else {}
            self.s.update("shards", {"shard_id": shard}, {"finished_at": end, **done})
        self.s.update("runs", {"run_id": rid}, {"lifecycle": "finished", "finished_at": end,
                                                "outcome": outcome or "complete_with_env_errors",
                                                "abort_reason": reason})

    def finding(self, run_id: str, created: datetime, *, kind: str = "collateral", triage: str = "open",
                synthetic: int = 0, held: int = 0, tier: int = 1) -> int:
        self.n += 1
        return self.s.insert("findings", {"natural_key": f"f-{self.n}", "run_id": run_id, "checkpoint_id": CP,
                                          "kind": kind, "tier": tier, "description": "d", "evidence_json": "{}",
                                          "triage_state": triage, "synthetic": synthetic, "held": held,
                                          "created_at": iso(created)})

    def decide(self, finding_id: int, at: datetime, run_id: str, *, revoked: bool = False) -> int:
        row = {"checkpoint_id": CP, "region_id": "rgn.x", "phash": b"\x00" * 8, "category": "collateral",
               "action": "confirm_bug", "verdict": "confirmed_bug", "source": "report_ui", "decided_by": "triage.lead",
               "decided_at": iso(at), "run_id": run_id, "reason": "seen", "source_finding_id": finding_id}
        if revoked:
            row.update(status="revoked", revoked_at=iso(at), revoked_by="asmith", revoke_reason="wrong")
        did = self.s.insert("decisions", row)
        self.s.update("findings", {"finding_id": finding_id}, {"decision_id": did})
        return did

    def inherit(self, finding_id: int, decision_id: int) -> None:
        self.s.update("findings", {"finding_id": finding_id}, {"decision_id": decision_id})

    def propose(self, finding_id: int, run_id: str, at: datetime) -> None:
        self.s.insert("proposals", {"finding_id": finding_id, "run_id": run_id, "action": "mark_intended",
                                    "proposed_by": "pr.author", "proposed_at": iso(at), "reason": "r",
                                    "ticket_ref": "TOS-1"})

    def outcome(self, run_id: str, cid: str, outcome: str, reason: str | None = None, **kw) -> None:
        self.s.insert("outcomes", {"run_id": run_id, "checkpoint_id": cid, "outcome": outcome, "reason": reason,
                                   "recorded_at": iso(NOW - timedelta(days=1)), **kw})

    def step(self, run_id: str, status: str, flow_id: str = "flow.a") -> None:
        shard = f"{run_id}/1"
        self.seq[shard] = self.seq.get(shard, 0) + 1
        self.s.write_events([StepEvent(run_id, shard, "candidate", self.seq[shard], "orchestrator",
                                       iso(NOW - timedelta(days=1)), 0, "scripted", "click", flow_id=flow_id,
                                       step_id=f"s{self.seq[shard]}", step_outcome=status)])

    def canary(self, run_id: str, cls: str, state: str, money: bool = False) -> None:
        self.n += 1
        produced = None if state in ("detected", "generation_failed") else "identical"
        self.s.insert("canary_results", {"run_id": run_id, "mutant_id": f"M{self.n:02d}", "catalog_version": "v1",
                                         "checkpoint_id": CP, "defect_class": cls, "money_cell": int(money), "seed": 1,
                                         "expected_json": "[]", "state": state, "produced_outcome": produced,
                                         "created_at": iso(NOW - timedelta(days=1))})


@pytest.fixture
def world(tmp_path):
    s = Store(tmp_path / "central.db")
    yield World(s)
    s.close()


WEEK = M.Window(NOW - timedelta(days=7), NOW, label="7 d")


# -- false-positive rate ---------------------------------------------------------------------------------------------

def seed_fp(w: World) -> str:
    rid = w.run("merge", ny(11, 9))
    for state in ("noise", "noise", "expected_behavior", "agent_mistake", "intended", "confirmed_bug", "confirmed_bug",
                  "open", "open", "open"):
        w.finding(rid, ny(11, 10), triage=state)
    w.finding(rid, ny(11, 10), triage="noise", synthetic=1)              # a mutant
    w.finding(rid, ny(11, 10), triage="noise", held=1)                   # held Explorer finding
    w.finding(rid, ny(11, 10), kind="intended", triage="intended")       # listed in Intended, not Unexpected
    seen = w.finding(rid, ny(11, 10), triage="confirmed_bug")
    did = w.decide(seen, ny(11, 11), rid)
    w.inherit(w.finding(rid, ny(11, 12), triage="noise"), did)          # suppressed: inherited, never shown
    w.finding(rid, ny(1, 10), triage="noise")                            # outside the window
    return rid


def test_false_positive_rate_counts_noise_expected_agent_mistake_and_undeclared_intended(world):
    rid = seed_fp(world)
    fp = M.false_positive(world.s, WEEK)
    assert (fp.rate.num, fp.rate.den) == (5, 11) and (fp.noise, fp.intent) == (4, 1)
    run = M.false_positive(world.s, M.Window.run(world.s, rid))
    assert run.rate.den == 12                                           # the run's own findings, the old one included
    assert M.pr_header_fragment(world.s, NOW) == "false-positive rate (30 d): 50.0%"


def test_the_pr_header_fragment_with_no_finding_shown(world):
    assert M.pr_header_fragment(world.s, NOW) == "false-positive rate (30 d): n/a"


# -- time to triage --------------------------------------------------------------------------------------------------

def test_time_to_triage_caps_each_finding_at_10_business_minutes_and_sums_per_run(world):
    pr = world.run("pr", ny(11, 9, 30))
    f1 = world.finding(pr, ny(11, 10))
    world.decide(f1, ny(11, 10, 4), pr)                                   # 4 min
    f2 = world.finding(pr, ny(11, 10))
    world.decide(f2, ny(11, 10, 30), pr)                                  # 30 min, capped at 10
    f3 = world.finding(pr, ny(11, 16, 55))
    world.decide(f3, ny(12, 9, 3), pr)                                    # 5 + 3 business minutes overnight
    f4 = world.finding(pr, ny(11, 10))
    world.propose(f4, pr, ny(11, 10, 2))                                  # proposals count from proposal time
    world.decide(f4, ny(11, 11), pr)
    world.finding(pr, ny(12, 16, 50))                                     # open: runs to the window end, 10 min
    t = {x.run_type: x for x in M.triage(world.s, WEEK)}["pr"]
    assert t.units == 1 and t.mean_min == pytest.approx(4 + 10 + 8 + 2 + 10)
    assert t.p90_latency_min == pytest.approx(30)


def test_merge_runs_of_one_morning_sum_into_one_unit(world):
    for hh in (8, 13):
        rid = world.run("merge", ny(10, hh))
        f = world.finding(rid, ny(11, 9))
        world.decide(f, ny(11, 9, 6), rid)
    other = world.run("merge", ny(11, 8))
    world.decide(world.finding(other, ny(12, 9)), ny(12, 9, 2), other)
    t = {x.run_type: x for x in M.triage(world.s, WEEK)}["merge"]
    assert (t.units, t.mean_min, t.max_min) == (2, 7.0, 12.0)


# -- flake, coverage, canaries, floors, reviewer, harness ------------------------------------------------------------

def test_a_verdict_flip_and_an_unstable_rerun_count_toward_the_flake_rate(world):
    a, b, c = list(CPS)[:3]
    r1 = world.run("merge", ny(10, 9), pair=True)
    r2 = world.run("merge", ny(11, 9), pair=True)
    world.outcome(r1, a, "identical")
    world.outcome(r2, a, "collateral")                                    # a flip on one build pair
    world.outcome(r1, b, "identical")
    world.outcome(r2, b, "identical")
    world.outcome(r2, c, "not_settled", "unstable")                      # an unstable targeted re-run
    assert M.flake_rate(world.s, WEEK) == M.Rate(2, 5)
    assert M.flake_rate(world.s, M.Window.run(world.s, r2)) == M.Rate(1, 3)


def test_coverage_drops_entitlement_scope_and_unregistered_cells_from_the_denominator(world):
    merge = world.run("merge", ny(11, 9))
    cells = [("monitor.a", 1, "covered", None), ("monitor.b", 1, "covered", None), ("monitor.c", 1, "covered", None),
             ("monitor.d", 1, "not_run", "entitlement"), ("trade.a", 2, "covered", None),
             ("trade.b", 2, "covered", None), ("trade.c", 2, "not_run", "blocked:env_error"),
             ("trade.d", 2, "not_run", "scope"), ("tools.new", 2, "not_registered", None)]
    world.s.write_coverage_cells({"run_id": merge, "surface_id": s, "persona": "options_trader",
                                  "entitlement": "margin", "tier": t, "state": st, "not_run_reason": why,
                                  "worst_outcome": "identical" if st == "covered" else None} for s, t, st, why in cells)
    pr = world.run("pr", ny(11, 10))
    world.s.update_summary(pr, {"coverage": {"cells": [
        {"surface_id": "monitor.a", "tier": 1, "state": "covered", "not_run_reason": None},
        {"surface_id": "monitor.b", "tier": 1, "state": "not_run", "not_run_reason": "budget"}]}})
    assert M.coverage(world.s, WEEK, ("merge",)) == {1: M.Rate(3, 3), 2: M.Rate(2, 3)}
    assert M.coverage(world.s, WEEK)[1] == M.Rate(4, 5)


def test_canary_pass_rate_per_class_money_cells_and_nights(world):
    night = world.run("nightly", ny(11, 22))
    world.canary(night, "swapped_digit", "detected", money=True)
    world.canary(night, "swapped_digit", "detected", money=True)
    for state in ("detected", "detected", "detected", "missed", "generation_failed"):
        world.canary(night, "erased_control", state)
    c = M.canary(world.s, WEEK)
    assert c.money == M.Rate(2, 2) and c.other == M.Rate(3, 4)
    assert c.by_class == {"erased_control": M.Rate(3, 4), "swapped_digit": M.Rate(2, 2)}
    assert c.nights == ((night, M.Rate(2, 2), M.Rate(3, 4)),)


def test_aa_floor_fix_list_of_the_current_key(world):
    cal = world.run("calibration", ny(7, 22))
    frozen, live = list(CPS)[0], list(CPS)[1]
    for key, cid, nonzero, when in (("sha256:old", frozen, 1, ny(1, 1)), ("sha256:new", frozen, 1, ny(8, 1)),
                                    ("sha256:new", list(CPS)[2], 0, ny(8, 1)), ("sha256:new", live, 0, ny(8, 1))):
        world.s.insert("calibration_floors", {"checkpoint_id": cid, "region_id": "*", "rendering_key_sha256": key,
                                              "calibration_run_id": cal, "samples": 15, "threshold_component_area": 4,
                                              "threshold_changed_px": 4, "threshold_delta_e": 2.0,
                                              "threshold_shift_px": 1, "nonzero_on_frozen": nonzero,
                                              "valid_from": iso(when)})
    a = M.aa_floor(world.s)
    assert (a.rendering_key, a.checkpoints, a.frozen_checkpoints, a.fix_list, a.masked_live_regions) == (
        "sha256:new", 3, 2, (frozen,), 1)


def test_reviewer_quality_newest_eval_per_tuple_and_overturn_rate(world):
    rid = world.run("merge", ny(11, 9))
    for at, p in ((ny(9, 9), 0.91), (ny(11, 9), 0.96)):
        world.s.write_reviewer_eval(eval_set_version="e1", model_id="claude-x", prompt_hash="p1", items=400,
                                    metrics={"precision": {"matches_manifest": p}}, passed_gate=p >= 0.95,
                                    run_at=iso(at))
    world.decide(world.finding(rid, ny(11, 10)), ny(11, 10, 5), rid, revoked=True)
    world.decide(world.finding(rid, ny(11, 10)), ny(11, 10, 6), rid)
    r = M.reviewer_quality(world.s, WEEK)
    assert len(r.evals) == 1 and r.evals[0].passed_gate
    assert M.eval_value(r.evals[0].metrics, "matches_manifest_precision") == 0.96
    assert M.eval_value({"does_not_match_manifest": {"recall": 0.97}}, "does_not_match_manifest_recall") == 0.97
    assert M.eval_value({}, "does_not_match_manifest_tier1_recall") is None
    assert r.overturn == M.Rate(1, 2)


def test_harness_health_shares_and_cost_per_run(world):
    a = world.run("pr", ny(10, 9), shards=2)
    world.run("pr", ny(10, 11), minutes=10, outcome="aborted_harness", reason="hook_lost", shards=1)
    world.run("merge", ny(11, 9), minutes=60, outcome="aborted_budget", reason="wall_clock", shards=1)
    world.run("merge", ny(11, 13))                                         # still running: not counted in shares
    world.outcome(a, list(CPS)[0], "identical")
    world.outcome(a, list(CPS)[1], "env_error", "gate", gate="authenticated")
    for status in ("passed", "passed", "passed_via_agent_fallback", "passed"):
        world.step(a, status)
    world.s.insert("model_usage", {"run_id": a, "component": "reviewer", "model_id": "claude-x", "cost_usd": 2.0,
                                   "at": iso(ny(10, 9, 20))})
    world.finish(a, 30)
    h = M.harness(world.s, WEEK)
    assert h.aborted == {"pr": M.Rate(1, 2), "merge": M.Rate(1, 1), "all": M.Rate(2, 3)}
    assert h.env_error == M.Rate(1, 2) and h.fallback == M.Rate(1, 4)
    assert h.rig_minutes_per_run == pytest.approx((60 + 10 + 60) / 4) and h.model_usd_per_run == 0.5


# -- stamp, export, header -------------------------------------------------------------------------------------------

def review(w: World, run_id: str, prompt: str, at: datetime) -> None:
    w.n += 1
    w.s.insert("reviews", {"run_id": run_id, "checkpoint_id": CP, "diff_hash": f"d{w.n}", "model_id": "claude-x",
                           "prompt_hash": prompt, "request_sha256": "r" * 64, "response_json": "{}",
                           "verdict": "matches_manifest", "mapped_outcome": "intended", "confidence": 0.9,
                           "created_at": iso(at)})


def test_every_number_is_stamped_with_commits_model_and_prompt_hash_range(world):
    r1 = world.run("merge", ny(10, 9), harness="h1")
    r2 = world.run("merge", ny(11, 9), harness="h2")
    review(world, r1, "p1", ny(10, 10))
    review(world, r2, "p2", ny(11, 10))
    st = M.stamp(world.s, WEEK)
    assert st.text() == "harness h1..h2 (2), config c1, model claude-x, prompt hash p1..p2 (2)"
    seed_fp(world)
    line = M.header_line(world.s, NOW, WEEK)
    assert line.startswith("metrics (7 d): false-positive rate 45.5% (30 d 50.0%) | flake ")
    assert line.endswith("| harness h1..h2 (2), config c1, model claude-x, prompt hash p1..p2 (2)")


def test_metrics_page_and_csv_export(world):
    seed_fp(world)
    world.s.write_escape(Escape("main-1", "TOS-9", "covered_missed", "triage.lead", iso(ny(11, 12)), CP, tier=1))
    m = M.compute(world.s, WEEK)
    lines = m.lines()
    assert lines[1] == "false-positive rate 45.5% (5 of 11): detector noise 4, undeclared intent 1"
    assert "escapes main-1: 1 covered_missed" in lines
    rows = list(csv.reader(io.StringIO(M.to_csv(m))))
    assert rows[0] == ["metric", "scope", "numerator", "denominator", "value"]
    assert rows[2][0] == "stamp" and ["false_positive_rate", "all", "5", "11", "0.454545"] in rows
    assert m.escapes.covered_tier1 == {"main-1": 1}


def test_the_default_window_starts_at_the_last_release(world):
    assert M.Window.since_last_release(world.s, NOW).start == NOW - timedelta(days=30)
    world.s.register_build("released-1", channel="released", source_commit="r1", pipeline_artifact_url="u",
                           installer_sha256="y" * 64, released_at=iso(ny(5, 18)))
    w = M.Window.since_last_release(world.s, NOW)
    assert w.start == ny(5, 18) and w.label == "since the last release"
    with pytest.raises(ValueError):
        M.Window.run(world.s, "run-unknown")


# -- kill criteria ---------------------------------------------------------------------------------------------------

def verdicts(**kw) -> dict[str, bool]:
    return {v.criterion: v.fired for v in M.kill_criteria(M.KillState(**kw))}


def test_kill_criteria_fire_at_the_stated_thresholds():
    assert not any(verdicts().values())
    assert verdicts(releases=3, fp_rate_30d=0.51)["stop_pr_comments"]
    assert verdicts(releases=3, fp_rate_30d=0.51)["revert_informational"]      # 9.2: or the kill criterion fires
    assert not verdicts(releases=2, fp_rate_30d=0.9)["stop_pr_comments"]
    assert not verdicts(releases=3, fp_rate_30d=0.50)["stop_pr_comments"]
    assert verdicts(fp_daily_30d=(0.2,) + (0.36,) * 10)["revert_informational"]
    assert not verdicts(fp_daily_30d=(0.36,) * 9 + (0.34,))["revert_informational"]
    assert not verdicts(fp_daily_30d=(0.36,) * 9)["revert_informational"]
    assert verdicts(flake_by_release=(0.2, 0.11, 0.12))["no_new_flows"]
    assert not verdicts(flake_by_release=(0.11, 0.09))["no_new_flows"]
    assert verdicts(quarantine_share=0.021)["rc_gate_quarantine"] and not verdicts(quarantine_share=0.02)[
        "rc_gate_quarantine"]
    assert verdicts(triage_over_2x_by_release=(False, True, True))["cut_tier3_scope"]
    assert verdicts(covered_tier1_escapes_by_release=(1, 2))["system_owner_review"]
    assert not verdicts(covered_tier1_escapes_by_release=(1, 0))["system_owner_review"]
    assert verdicts(tier1_merge_coverage=0.99)["rc_gate_coverage"]
    assert not verdicts(tier1_merge_coverage=1.0)["rc_gate_coverage"]
    assert verdicts(money_cell_misses=1)["detector_blind"]
    assert verdicts(reviewer_intended_precision=0.84)["intended_disabled"]
    assert not verdicts(reviewer_intended_precision=0.85)["intended_disabled"]
    assert verdicts(aborted_harness_7d=2)["harness_freeze"] and not verdicts(aborted_harness_7d=1)["harness_freeze"]


def test_kill_state_from_the_store_stops_pr_comments_after_three_releases(world):
    for i, d in enumerate((1, 4, 8)):
        world.s.register_build(f"released-{i}", channel="released", source_commit=f"r{i}", pipeline_artifact_url="u",
                               installer_sha256=str(i) * 64, released_at=iso(ny(d, 18)))
    rid = world.run("merge", ny(11, 9))
    for state in ("noise", "noise", "intended", "open"):
        world.finding(rid, ny(11, 10), triage=state)
    world.s.write_escape(Escape("released-1", "TOS-1", "covered_missed", "triage.lead", iso(ny(5, 9)), CP, tier=1))
    world.s.write_escape(Escape("released-2", "TOS-2", "covered_missed", "triage.lead", iso(ny(9, 9)), CP, tier=1))
    state = M.kill_state(world.s, NOW)
    assert state.releases == 3 and state.fp_rate_30d == 0.75
    assert state.covered_tier1_escapes_by_release == (0, 1, 1)
    assert len(state.fp_daily_30d) == 10
    assert M.stop_pr_comments(world.s, NOW) and M.revert_informational(world.s, NOW)
    assert M.fired(M.kill_criteria(state), "system_owner_review")


# -- phase exits -----------------------------------------------------------------------------------------------------

def statuses(exit_: M.PhaseExit) -> dict[str, M.Criterion]:
    return {c.item: c for c in exit_.criteria}


def test_phase_1_fails_when_one_week_has_a_pr_run_of_47_minutes(world):
    world.run("pr", ny(2, 10), minutes=40)
    world.run("merge", ny(2, 12), minutes=150)
    world.run("pr", ny(10, 10), minutes=47)
    world.run("merge", ny(10, 12), minutes=170)
    c = statuses(M.phase_exit(world.s, 1, now=NOW))
    budget = c["PR runs inside 45 minutes and merge runs inside 3 hours for two consecutive weeks"]
    assert budget.status == M.FAIL and "took 47 min in week 2" in budget.evidence
    assert not M.phase_exit(world.s, 1, now=NOW).passed


def test_phase_1_budgets_pass_inside_and_need_both_weeks(tmp_path):
    with Store(tmp_path / "a.db") as s:
        w = World(s)
        for d in (2, 10):
            w.run("pr", ny(d, 10), minutes=44)
            w.run("merge", ny(d, 12), minutes=179)
        c = statuses(M.phase_exit(s, 1, now=NOW))
        assert c["PR runs inside 45 minutes and merge runs inside 3 hours for two consecutive weeks"].status == M.PASS
    with Store(tmp_path / "b.db") as s:
        w = World(s)
        w.run("pr", ny(10, 10), minutes=30)
        c = statuses(M.phase_exit(s, 1, now=NOW))
        crit = c["PR runs inside 45 minutes and merge runs inside 3 hours for two consecutive weeks"]
        assert crit.status == M.NOT_MEASURABLE and "week 1 has no finished pr run" in crit.evidence


def test_acceptance_without_a_real_branch_reports_partial(world):
    item = "acceptance test passes in both forms on two consecutive days (10.10)"
    partial = statuses(M.phase_exit(world.s, 1, now=NOW, acceptance=[M.AcceptanceResult(date(2026, 11, 10), True,
                                                                                        None)]))[item]
    assert (partial.status, partial.evidence) == (M.NOT_MEASURABLE, "partial: no real branch")
    both = [M.AcceptanceResult(date(2026, 11, 6), True, True), M.AcceptanceResult(date(2026, 11, 9), True, True)]
    assert statuses(M.phase_exit(world.s, 1, now=NOW, acceptance=both))[item].status == M.PASS   # Fri, Mon
    gap = [M.AcceptanceResult(date(2026, 11, 5), True, True), M.AcceptanceResult(date(2026, 11, 9), True, True)]
    assert statuses(M.phase_exit(world.s, 1, now=NOW, acceptance=gap))[item].status == M.FAIL
    assert statuses(M.phase_exit(world.s, 1, now=NOW))[item].status == M.NOT_MEASURABLE


def test_phase_1_metric_items_read_the_phase_1_targets(world):
    seed_fp(world)
    c = statuses(M.phase_exit(world.s, 1, now=NOW))
    fp = c["false-positive rate under 35 percent"]
    assert fp.status == M.FAIL and fp.evidence == "50.0% (6 of 12), target under 35 percent"   # 14 days back
    assert c["coverage tier 1 at least 100 percent (merge runs)"].status == M.NOT_MEASURABLE
    assert c["fire drill passes seven consecutive nights with zero aborted_safety false alarms"].status == \
        M.NOT_MEASURABLE
    assert c["A/A floor recorded for every census checkpoint and zero on frozen ones"].status == M.FAIL


def test_phase_2_reads_reviewer_gates_and_the_pilot_exit(world):
    world.s.write_reviewer_eval(eval_set_version="e1", model_id="claude-x", prompt_hash="p1", items=400,
                                metrics={"precision": {"matches_manifest": 0.96},
                                         "recall": {"does_not_match_manifest": 0.94},
                                         "tier1_recall": {"does_not_match_manifest": 0.99}},
                                passed_gate=False, run_at=iso(ny(11, 9)))
    rid = world.run("merge", ny(11, 9))
    world.finding(rid, ny(11, 10), triage="confirmed_bug")
    c = statuses(M.phase_exit(world.s, 2, now=NOW))
    assert c["Reviewer matches manifest precision at least 0.95"].status == M.PASS
    assert c["Reviewer does not match manifest tier1 recall at least 0.98"].status == M.PASS
    assert c["Reviewer does not match manifest recall at least 0.95"].status == M.FAIL
    assert c["at least one real pre-release catch on an undeclared registered surface"].status == M.PASS
    assert c["two release cycles"].status == M.FAIL
    assert c["every tier-1 surface has a flow with assertions"].status == M.NOT_MEASURABLE
    assert c["false-positive rate under 20 percent"].status == M.PASS


def test_phase_3_quarantine_rc_budget_and_fallback_per_flow(world):
    rc = world.run("rc", ny(10, 6), shards=1)
    for status in ("passed",) * 19 + ("passed_via_agent_fallback",):
        world.step(rc, status, "flow.a")
    for status in ("passed",) * 9 + ("passed_via_agent_fallback",):
        world.step(rc, status, "flow.b")
    world.finish(rc, 470, "complete")
    lines = M.phase_exit(world.s, 3, now=NOW).lines()
    assert lines[0] == "phase 3 exit: not met"
    c = statuses(M.phase_exit(world.s, 3, now=NOW))
    assert c["one RC run completed inside its 8-hour budget"].status == M.PASS
    assert c["quarantine under 2 percent of tier-1 checkpoints"].status == M.PASS
    fb = c["fallback rate per flow under 5 percent"]
    assert fb.status == M.FAIL and fb.evidence == "flow.a 5%, flow.b 10%"
    assert c["at least 80 percent of flows authored or repaired through the Recorder in the last four weeks"].status \
        == M.NOT_MEASURABLE
    with pytest.raises(ValueError):
        M.phase_exit(world.s, 4, now=NOW)


FINGERPRINT = "fingerprint identical across ten launches per rig"


def fingerprints(w: World, shas: list[str]) -> None:
    """One read per shard side on rig-01, two per run, in time order."""
    for k in range(0, len(shas), 2):
        rid = w.run("merge", ny(10, 6 + k), minutes=60, shards=1)
        for j, side in enumerate(("base", "candidate")[:len(shas) - k]):
            w.s.insert("env_fingerprints", {"run_id": rid, "shard_id": f"{rid}/1", "side": side, "rig_id": "rig-01",
                                            "build_id": "main-1", "fingerprint_sha256": shas[k + j],
                                            "fields_json": "{}", "read_at": iso(ny(10, 6 + k, j))})


def test_phase_1_fingerprint_needs_ten_identical_reads_per_rig(tmp_path):
    for name, shas, status in (("few", ["fp-1"] * 4, M.NOT_MEASURABLE), ("same", ["fp-1"] * 10, M.PASS),
                               ("moved", ["fp-1"] * 9 + ["fp-2"], M.FAIL)):
        with Store(tmp_path / f"{name}.db") as s:
            w = World(s)
            fingerprints(w, shas)
            assert statuses(M.phase_exit(s, 1, now=NOW))[FINGERPRINT].status == status, name


def test_phase_1_every_scoped_checkpoint_has_an_outcome(world):
    world.run("merge", ny(11, 9), minutes=100)
    done = statuses(M.phase_exit(world.s, 1, now=NOW))[
        "every checkpoint has exactly one outcome and every not_run prints its reason"]
    assert done.status == M.PASS and done.evidence == ("1 finished runs: 0 scoped checkpoints without an outcome, "
                                                       "0 not_run without a reason")
    assert json.dumps([x.status for x in M.phase_exit(world.s, 1, now=NOW).criteria])
