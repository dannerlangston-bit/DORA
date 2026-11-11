"""The Explorer stage (2.9, 6.8): steering, findings, candidate flows, the single replay, metrics, pause, DR-07 B."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sentinel import explorer_findings as ef
from sentinel import explorer_steering as es
from sentinel.agent.session import Observation, RawFinding, TraceAction
from sentinel.agent.tools import TOOLSET_SCHEMA_VERSION
from sentinel.lint.repo import Repo
from sentinel.oddinput import OddInputCorpus
from sentinel.orchestrator import explorer
from sentinel.orchestrator.explorer import ExplorerBudget, ExplorerMetrics
from sentinel.records import RunOutcome
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.taxonomy import is_checkpoint_id

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SURFACES = [yaml.safe_load(p.read_text()) for p in sorted((EXAMPLE / "registry" / "surfaces").glob("**/*.yaml"))]
CONTROLS = frozenset(f"{s['id']}.{c['id']}" for s in SURFACES for c in s.get("controls") or ())
PS = "monitor.activity.position-statement"
TOGGLE = f"{PS}.section-toggle"
CTX = ef.FlowContext(owner="explorer-owner", persona="active_trader", workspace="ws.tier-a.active-trader",
                     fixture="fx.rig-01.paper")


# -- steering ----------------------------------------------------------------------------------------------------
def test_steering_ranks_a_surface_with_zero_flow_checkpoints_first():
    counts = es.flow_checkpoint_counts([
        ("flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s06", "identical"),
        ("flow/trade.order-entry.stock/flow.trade.order-ticket.stock.limit/s07", "collateral"),
        ("flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s08", "not_run"),
        ("flow/charts.flexible-grid.chart-cell/flow.charts.study/cp1", None),
        ("census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader", "identical")])
    assert counts == {"trade.order-entry.stock": 2, "charts.flexible-grid.chart-cell": 1}
    targets = es.target_list(SURFACES, counts, {"LadderModel"})
    zero = [t for t in targets if t.flow_checkpoints == 0]
    assert targets[0].flow_checkpoints == 0 and targets[0].rank == 1
    assert [t.surface_id for t in zero[:2]] == ["charts.flexible-grid.active-trader-cell", "trade.active-trader.ladder"]
    assert zero[0].touched == ("LadderModel",)                          # touched primitives break the tie
    assert targets[-1].surface_id == "trade.order-entry.stock"


def test_steering_lists_only_reachable_surfaces_and_its_version_tracks_the_list():
    t = es.target_list(SURFACES, {}, (), reachable={PS, "charts.flexible-grid.chart-cell"}, denied_surfaces={PS})
    assert [x.surface_id for x in t] == ["charts.flexible-grid.chart-cell"]
    full = es.target_list(SURFACES, {}, ())
    assert es.steering_version(full) == es.steering_version(es.target_list(SURFACES, {}, ()))
    assert es.steering_version(t) != es.steering_version(full)
    assert t[0].to_dict()["rank"] == 1


def test_odd_inputs_come_from_the_seeded_corpus():
    corpus = OddInputCorpus.load(EXAMPLE / "corpus" / "odd-inputs.yaml")
    refs = es.odd_input_values(corpus, 20261111, 3)
    assert len(refs) == 3 and all(r.startswith("odd_inputs.") for r in refs)
    assert refs == es.odd_input_values(corpus, 20261111, 3)


# -- findings and dedupe -----------------------------------------------------------------------------------------
def raw(obs="INV-03", seq=4, conf=0.9, crop="c" * 64, surface=PS) -> RawFinding:
    return RawFinding(Observation(surface, obs, crop, "high", conf, {"cell": "12.50"}), seq)


def found(*raws: RawFinding) -> list[ef.ExplorerCandidate]:
    return ef.candidates(raws, session_id="exp-1", trace_ref="a" * 64, build_id="rc-1998.2.0-4f1c9a2")


def test_one_finding_per_signature_in_a_session():
    cs = found(raw(seq=4, conf=0.5), raw(seq=9, conf=0.8), raw("INV-07", seq=6))
    assert [(c.observation, c.seq, c.confidence) for c in cs] == [("INV-03", 4, 0.8), ("INV-07", 6, 0.9)]
    assert cs[0].kind == "invariant" and found(raw("assertion:qty"))[0].kind == "assertion"
    assert cs[0].signature == {"surface_id": PS, "observation": "INV-03", "crop_sha256": "c" * 64}


def store_with_run(tmp_path, run_id="rc-n1", started="2026-11-04T10:00:00Z") -> Store:
    s = Store(tmp_path / "central.db")
    s.register_rig("rig-06", login_alias="explorer-06", owner="ops", snapshot_id="snap-6", allowlist_entry=6,
                   role="explorer", login_kind="explorer")
    begin(s, run_id, started)
    return s


def begin(s: Store, run_id: str, started: str) -> None:
    s.begin_run(RunPlan(run_id=run_id, run_type="rc", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                        snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                        budget_sec=8 * 3600, scope=(ScopeEntry(f"census/{PS}/ws.tier-a.active-trader",
                                                               "ws.tier-a.active-trader", "active_trader", 1,
                                                               "masked_live"),),
                        shards=(ShardPlan(f"{run_id}/1", "rig-06"),), started_at=started))


def test_the_same_signature_on_night_2_updates_last_seen(tmp_path):
    s = store_with_run(tmp_path)
    [c] = found(raw())
    s.write_finding(ef.to_finding(c, finding_id="f1", run_id="rc-n1", replayed="reproduced",
                                  candidate_flow="flows/quarantine/f1.yaml"))
    begin(s, "rc-n2", "2026-11-11T10:00:00Z")
    prior = ef.prior_findings(s)
    new, again = ef.dedupe(found(raw(), raw("INV-07")), prior)
    assert [c.observation for c in new] == ["INV-07"] and len(again) == 1
    old, _ = again[0]
    assert (old.first_seen_run_id, old.last_seen_run_id) == ("rc-n1", "rc-n1")
    ef.mark_seen_again(s, old, "rc-n2", "2026-11-11T12:00:00Z")
    p2 = ef.prior_findings(s)[c.key]
    assert (p2.finding_id, p2.first_seen_run_id, p2.last_seen_run_id) == (old.finding_id, "rc-n1", "rc-n2")
    assert len(s.query("SELECT 1 FROM findings WHERE kind = 'explorer_candidate'")) == 1
    s.close()


def test_a_held_finding_is_stored_with_its_explorer_block(tmp_path):
    s = store_with_run(tmp_path)
    [c] = found(raw())
    f = ef.to_finding(c, finding_id="f9", run_id="rc-n1", replayed="not_reproduced", candidate_flow=None, held=True,
                      triage_state="agent_mistake", trace=[TraceAction(2, "click_named", {}), TraceAction(4, "press", {})])
    assert f.evidence["step_events"] == {"first_seq": 2, "last_seq": 4} and f.explorer.replayed == "not_reproduced"
    s.write_finding(f)
    [row] = s.query("SELECT held, triage_state FROM findings")
    assert (row["held"], row["triage_state"]) == (1, "agent_mistake")
    s.close()


# -- the candidate flow ------------------------------------------------------------------------------------------
TRACE = [
    TraceAction(1, "click_named", {"target": "Positions"}, ("Positions",), stable_state=PS),
    TraceAction(2, "click_named", {"target": TOGGLE}, (TOGGLE,), click_crop_sha256="d" * 64),
    TraceAction(3, "zoom", {"region": {"x": 0, "y": 0, "w": 10, "h": 10}}),
    TraceAction(4, "press", {"key": "shift_tab"}),
    TraceAction(5, "scroll", {"target": TOGGLE, "notches": -3}, (TOGGLE,)),
    TraceAction(6, "type_fixture", {"fixture_id": "fixture.symbols.aapl.symbol"}),
    TraceAction(7, "press", {"key": "page_down"}, agent_error=True),
]


@pytest.fixture(scope="module")
def repo() -> Repo:
    return Repo.load(EXAMPLE)


def test_the_candidate_is_cut_since_the_last_stable_state_and_lints_clean(repo):
    [c] = found(raw(seq=7))
    flow = ef.candidate_flow("exp-rc-1-0007", "rc-1", c, TRACE, CTX, CONTROLS)
    assert flow.path == "flows/quarantine/exp-rc-1-0007.yaml" and flow.flow_id == "flow.explorer.exp-rc-1-0007"
    doc = flow.doc
    assert doc["class"] == "explorer_candidate" and doc["origin"] == {"kind": "explorer", "run_id": "rc-1",
                                                                      "finding_ref": "exp-rc-1-0007"}
    assert [st["type"] for st in doc["steps"]] == ["reset_input_state", "click_named", "key_chord", "wheel",
                                                   "type_fixture", "press", "checkpoint"]
    assert doc["steps"][1]["note"].endswith("d" * 64) and doc["steps"][4]["target"] == TOGGLE
    assert doc["steps"][5]["key"] == "pagedown" and doc["budget"]["max_steps"] <= 400
    assert is_checkpoint_id(flow.checkpoint_id) and flow.problems == ()
    assert ef.lint_candidate(repo, flow, today=date(2026, 11, 10)) == []


def test_a_candidate_on_an_ocr_word_or_over_budget_fails_its_lint(repo):
    [c] = found(raw(seq=2))
    ocr = ef.candidate_flow("x1", "rc-1", c, [TraceAction(2, "click_named", {}, ("AAPL",))], CTX, CONTROLS)
    errors = ef.lint_candidate(repo, ocr, today=date(2026, 11, 10))
    assert errors and "no registry control" in errors[0]
    tight = replace(CTX, budget=ExplorerBudget(max_actions=400, max_minutes=90))
    flow = ef.candidate_flow("x2", "rc-1", c, TRACE[:2], tight, CONTROLS)
    flow = replace(flow, doc={**flow.doc, "budget": {"max_duration_s": 6000, "max_steps": 500}})
    assert any("L-FLW-18" in e for e in ef.lint_candidate(repo, flow, today=date(2026, 11, 10)))


# -- the replay --------------------------------------------------------------------------------------------------
class Runner:
    def __init__(self, result: ef.ReplayResult):
        self.result, self.calls = result, 0

    def replay(self, flow):
        self.calls += 1
        return self.result


def run(cs, result, lint=lambda f: []):
    return ef.process(cs, TRACE, run_id="rc-1", ctx=CTX, controls=CONTROLS, runner=Runner(result), lint=lint)


def test_a_reproduced_invariant_gives_invariant_violation():
    [r] = run(found(raw("INV-03", seq=7)), ef.ReplayResult(invariant_ids=("INV-03",)))
    assert r.replayed == "reproduced" and (r.outcome.outcome, r.outcome.reason) == ("invariant_violation", "INV-03")


def test_assertions_and_other_observations_reproduce_through_their_own_outcomes():
    [a] = run(found(raw("assertion:qty_default", seq=7)), ef.ReplayResult(assertion_ids=("qty_default",)))
    assert (a.outcome.outcome, a.outcome.reason) == ("assertion_failed", "assertion:qty_default")
    [o] = run(found(raw("clipped_label", seq=7)), ef.ReplayResult(observations=("clipped_label",)))
    assert (o.outcome.outcome, o.outcome.reason) == ("needs_baseline", "explorer_replay")


def test_a_finding_that_does_not_reproduce_is_held_and_listed_as_not_reproduced():
    [r] = run(found(raw("INV-03", seq=7)), ef.ReplayResult(invariant_ids=("INV-05",)))
    assert r.replayed == "not_reproduced" and r.outcome is None and r.hold == "agent_mistake"
    [r2] = run(found(raw("INV-03", seq=5)), ef.ReplayResult())
    assert r2.hold == "expected_behavior"


def test_a_candidate_that_fails_lint_is_not_replayed():
    runner = Runner(ef.ReplayResult(invariant_ids=("INV-03",)))
    [r] = ef.process(found(raw(seq=7)), TRACE, run_id="rc-1", ctx=CTX, controls=CONTROLS, runner=runner,
                     lint=lambda f: ["L-FLW-02 error x"])
    assert r.replayed == "lint_failed" and runner.calls == 0 and r.lint_errors == ("L-FLW-02 error x",)


def test_the_bounded_mode_spends_at_most_eight_replays():
    [c] = found(raw("INV-03", seq=7))
    long = [TraceAction(i, "press", {"key": "tab"}) for i in range(1, 21)] + [TraceAction(21, "press", {"key": "up"})]
    flow = ef.candidate_flow("t", "rc-1", replace(c, seq=21), long, CTX, CONTROLS)

    def rebuild(actions):
        return replace(flow, actions=tuple(actions))

    class Last:
        calls = 0

        def replay(self, f):
            self.calls += 1
            return ef.ReplayResult(invariant_ids=("INV-03",) if f.actions and f.actions[-1].seq == 21 else ())

    runner = Last()
    best, spent = ef.bounded_trim(flow, replace(c, seq=21), runner, rebuild)
    assert spent == 8 == runner.calls and len(best.actions) == len(flow.actions) - 8
    with pytest.raises(ValueError):
        ef.bounded_trim(flow, c, runner, rebuild, max_replays=9)


# -- metrics, pause, window, DR-07 B -----------------------------------------------------------------------------
def summary(precision, steering="s1", toolset=TOOLSET_SCHEMA_VERSION):
    return {"precision": precision, "steering_version": steering, "toolset_schema_version": toolset}


def test_precision_under_30_percent_on_two_consecutive_rc_runs_pauses_the_explorer():
    hist = [summary(0.25), summary(0.2)]                               # newest first: 0.2 then 0.25
    line = explorer.pause_line(hist, steering_version="s1", toolset_schema_version=TOOLSET_SCHEMA_VERSION)
    assert line.startswith("Explorer: paused") and "25%, 20%" in line
    assert explorer.pause_line([summary(0.25), summary(0.4)], steering_version="s1",
                               toolset_schema_version=TOOLSET_SCHEMA_VERSION) is None
    assert explorer.pause_line(hist, steering_version="s2", toolset_schema_version=TOOLSET_SCHEMA_VERSION) is None
    assert explorer.pause_line(hist, steering_version="s1", toolset_schema_version="sha256:other") is None
    assert explorer.pause_line([summary(0.25), summary(None), summary(0.1)], steering_version="s1",
                               toolset_schema_version=TOOLSET_SCHEMA_VERSION).startswith("Explorer: paused")
    assert explorer.pause_line([summary(0.1)], steering_version="s1", toolset_schema_version=TOOLSET_SCHEMA_VERSION) \
        is None


def test_the_metrics_and_their_lines():
    m = ExplorerMetrics(emitted=5, reproduced=1, actions=200, agent_errors=4, deny_hits=2, budget_hit="minutes",
                        usd=12.5, steering_version="s1", toolset_schema_version=TOOLSET_SCHEMA_VERSION)
    assert m.precision == 0.2 and m.agent_error_ratio == 0.02
    lines = m.health_lines()
    assert lines[0].startswith("Explorer: precision 20% (1 of 5)") and "Explorer budget hit: minutes" in lines[1]
    assert m.to_summary()["precision"] == 0.2
    none = ExplorerMetrics(0, 0, 0, 0, 0, None, 0.0)
    assert none.precision is None and "n/a" in none.health_lines()[0]


def test_the_session_starts_after_the_last_shard_and_is_cut_at_the_deadline():
    t0 = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)
    deadline = t0 + timedelta(hours=7, minutes=50)
    on_time = explorer.session_window(t0 + timedelta(hours=6, minutes=20), deadline)
    assert on_time.end == deadline and not on_time.cut_by_deadline and "90 of 90" in on_time.health
    late = explorer.session_window(t0 + timedelta(hours=7), deadline)
    assert late.end == deadline and late.cut_by_deadline and "50 of 90" in late.health
    gone = explorer.session_window(deadline, deadline)
    assert gone.start is None and "not run" in gone.health


def test_dr07_b_prints_the_not_available_line():
    r = explorer.not_available_dr07b()
    assert r.detail == "Explorer: not available under DR-07 B" and r.outcomes == () and r.run is None


def test_the_store_keeps_the_explorer_summary_for_the_pause_rule(tmp_path):
    s = store_with_run(tmp_path)
    m = ExplorerMetrics(4, 1, 100, 1, 0, None, 3.0, "s1", TOOLSET_SCHEMA_VERSION)
    s.update_summary("rc-n1", {"rc": {"explorer": m.to_summary()}})
    assert s.run_summary("rc-n1")["rc"]["explorer"]["precision"] == 0.25
    s.write_checkpoint(_out("rc-n1"))
    s.end_run(RunOutcome(run_id="rc-n1", run_type="rc", status="complete_with_env_errors", last_gate="reported"))
    s.close()


def _out(run_id):
    from sentinel.records import CheckpointOutcome
    return CheckpointOutcome(run_id=run_id, checkpoint_id=f"census/{PS}/ws.tier-a.active-trader", outcome="identical",
                             workspace_id="ws.tier-a.active-trader", persona="active_trader", data_mode="masked_live")
